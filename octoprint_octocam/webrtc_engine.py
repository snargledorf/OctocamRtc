# coding=utf-8
from __future__ import annotations

import asyncio
import fractions
import io
import logging
import os
import queue as queue_mod
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

logger = logging.getLogger("octoprint.plugins.octocam")


def _terminate_subprocess(proc: Optional[subprocess.Popen[bytes]]) -> None:
    """Safely closes pipes, kills a subprocess, and reaps it to prevent zombie processes and pipe leaks."""
    if proc is None:
        return
    for pipe in (proc.stdin, proc.stdout, proc.stderr):
        if pipe is not None:
            try:
                pipe.close()
            except Exception:
                pass
    try:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=1.0)
    except Exception:
        pass


def _silence_pyav_swscaler_warnings() -> None:
    """Suppresses noisy FFmpeg libswscale 'deprecated pixel format used' warnings in PyAV."""
    try:
        import av.logging

        av.logging.set_level(av.logging.ERROR)
        if hasattr(av.logging, "set_libav_level"):
            av.logging.set_libav_level(av.logging.ERROR)
    except Exception:
        pass


_silence_pyav_swscaler_warnings()


@dataclass
class EngineConfig:
    """Configuration snapshot for the WebRTC media engine."""

    backend_mode: str = "aiortc"  # "aiortc" or "whep"
    stream_source: str = "/dev/video0"
    input_format: str = "auto"  # "auto", "v4l2", "mjpeg", "rtsp"
    resolution: str = "1280x720"
    framerate: int = 30
    bitrate: str = "2500k"
    hwaccel_mode: str = "vaapi"  # "vaapi", "v4l2m2m", "none"
    vaapi_device: str = "/dev/dri/renderD128"
    whep_url: str = ""
    snapshot_url: str = ""
    flip_h: bool = False
    flip_v: bool = False
    rotate90: bool = False
    ice_servers: List[Dict[str, Any]] = field(default_factory=list)


def parse_bitrate_bps(bitrate_str: str, default_bps: int = 2_500_000) -> int:
    """Converts strings like '2500k' or '4M' into integer bits per second."""
    raw = (bitrate_str or "").strip().lower()
    if not raw:
        return default_bps
    try:
        if raw.endswith("k"):
            return int(float(raw[:-1]) * 1_000)
        if raw.endswith("m"):
            return int(float(raw[:-1]) * 1_000_000)
        return int(raw)
    except ValueError:
        return default_bps


def parse_resolution(
    resolution_str: str, default: Tuple[int, int] = (1280, 720)
) -> Tuple[int, int]:
    """Parses a 'WIDTHxHEIGHT' string into integer `(width, height)`."""
    raw = (resolution_str or "").strip().lower()
    if "x" not in raw:
        return default
    try:
        w_str, h_str = raw.split("x", 1)
        return max(16, int(w_str)), max(16, int(h_str))
    except ValueError:
        return default


def probe_vaapi_support(vaapi_device: str = "/dev/dri/renderD128") -> Dict[str, Any]:
    """
    Probes Linux VAAPI hardware availability, device permissions, and FFmpeg/PyAV
    `h264_vaapi` encoder readiness.
    """
    device = (vaapi_device or "/dev/dri/renderD128").strip()
    device_exists = os.path.exists(device)
    device_accessible = os.access(device, os.R_OK | os.W_OK) if device_exists else False

    pyav_h264_vaapi = False
    try:
        import av

        pyav_h264_vaapi = "h264_vaapi" in av.codecs_available
    except Exception:
        pyav_h264_vaapi = False

    ffmpeg_path = shutil.which("ffmpeg")
    ffmpeg_vaapi_works = False
    diagnostic_message = ""

    if not device_exists:
        diagnostic_message = f"VAAPI render device '{device}' does not exist."
    elif not device_accessible:
        diagnostic_message = (
            f"VAAPI render device '{device}' exists but is not readable/writable by the "
            "OctoPrint user. Add the user to the 'render' and 'video' groups."
        )
    elif ffmpeg_path:
        try:
            proc = subprocess.run(
                [
                    ffmpeg_path,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-vaapi_device",
                    device,
                    "-f",
                    "lavfi",
                    "-i",
                    "nullsrc=s=128x128:d=0.04:r=25",
                    "-vf",
                    "format=nv12,hwupload",
                    "-c:v",
                    "h264_vaapi",
                    "-frames:v",
                    "1",
                    "-f",
                    "null",
                    "-",
                ],
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
            if proc.returncode == 0:
                ffmpeg_vaapi_works = True
                diagnostic_message = (
                    f"VAAPI hardware encoding ('h264_vaapi') verified on {device}."
                )
            else:
                stderr_snippet = (proc.stderr or "").strip()[:240]
                diagnostic_message = (
                    f"FFmpeg VAAPI test failed on {device}: {stderr_snippet}"
                )
        except Exception as exc:
            diagnostic_message = f"Error running FFmpeg VAAPI test: {exc}"
    else:
        if pyav_h264_vaapi:
            diagnostic_message = (
                f"Device {device} is accessible and PyAV reports 'h264_vaapi' support."
            )
        else:
            diagnostic_message = (
                f"Device {device} is accessible, but neither 'ffmpeg' binary nor PyAV "
                "'h264_vaapi' was detected."
            )

    available = bool(device_accessible and (ffmpeg_vaapi_works or pyav_h264_vaapi))
    logger.info(
        "[OctoCam HW] VAAPI probe result for '%s': available=%s, exists=%s, r/w=%s, ffmpeg=%s, ffmpeg_vaapi_works=%s, pyav_h264_vaapi=%s, message='%s'",
        device,
        available,
        device_exists,
        device_accessible,
        ffmpeg_path,
        ffmpeg_vaapi_works,
        pyav_h264_vaapi,
        diagnostic_message,
    )
    return {
        "available": available,
        "device": device,
        "device_exists": device_exists,
        "device_accessible": device_accessible,
        "pyav_h264_vaapi": pyav_h264_vaapi,
        "ffmpeg_vaapi_works": ffmpeg_vaapi_works,
        "message": diagnostic_message,
    }


def build_ffmpeg_vf_filter(
    flip_h: bool = False,
    flip_v: bool = False,
    rotate90: bool = False,
    width: Optional[int] = None,
    height: Optional[int] = None,
    pipeline_mode: str = "software",
) -> str:
    """
    Constructs the FFmpeg `-vf` filtergraph string for a given pipeline mode:
    - `"vaapi_hw"`: Decoded frames are already on VAAPI surfaces; uses `scale_vaapi`
      and `transpose_vaapi` so frames stay in GPU memory end-to-end.
    - `"vaapi"`: Decoded frames are in system memory; scales and applies flips/rotation
      in software before uploading `nv12` surfaces to the GPU via `hwupload`.
    - `"v4l2m2m"` / `"software"`: Scales, flips/rotates, and converts to `yuv420p`.
    - `"snapshot"`: Only applies requested `hflip`, `vflip`, and `transpose=cclock`.
    """
    filters: List[str] = []

    if pipeline_mode == "vaapi_hw":
        if width and height:
            filters.append(f"scale_vaapi=w={width}:h={height}:format=nv12")
        else:
            filters.append("scale_vaapi=format=nv12")
        if flip_h:
            filters.append("transpose_vaapi=dir=hflip")
        if flip_v:
            filters.append("transpose_vaapi=dir=vflip")
        if rotate90:
            filters.append("transpose_vaapi=dir=cclock")
        return ",".join(filters)

    if width and height and pipeline_mode != "snapshot":
        filters.append(f"scale={width}:{height}")

    if flip_h:
        filters.append("hflip")
    if flip_v:
        filters.append("vflip")
    if rotate90:
        filters.append("transpose=cclock")

    if pipeline_mode == "vaapi":
        filters.append("format=nv12")
        filters.append("hwupload")
    elif pipeline_mode in ("v4l2m2m", "software"):
        filters.append("format=yuv420p")

    return ",".join(filters)


def build_ffmpeg_input_args(
    config: EngineConfig,
    pipeline_mode: str = "software",
) -> List[str]:
    """Builds the FFmpeg global/hwaccel and demuxer input arguments for `config`."""
    source = (config.stream_source or "/dev/video0").strip()
    input_fmt = (config.input_format or "auto").strip().lower()
    device = (config.vaapi_device or "/dev/dri/renderD128").strip()
    width, height = parse_resolution(config.resolution)
    fps = max(1, int(config.framerate or 30))

    args: List[str] = [
        "-fflags",
        "nobuffer",
        "-flags",
        "low_delay",
    ]

    if pipeline_mode == "vaapi_hw":
        args.extend(
            [
                "-hwaccel",
                "vaapi",
                "-hwaccel_device",
                device,
                "-hwaccel_output_format",
                "vaapi",
            ]
        )
    elif pipeline_mode == "vaapi":
        args.extend(["-vaapi_device", device])

    if source.startswith("/dev/video") or input_fmt == "v4l2":
        args.extend(["-f", "v4l2"])
        if input_fmt == "mjpeg" or (input_fmt == "auto" and pipeline_mode == "vaapi_hw"):
            args.extend(["-input_format", "mjpeg"])
        elif input_fmt not in ("auto", "v4l2"):
            args.extend(["-input_format", input_fmt])
        args.extend(
            [
                "-video_size",
                f"{width}x{height}",
                "-framerate",
                str(fps),
            ]
        )
    elif source.startswith("rtsp://") or input_fmt == "rtsp":
        args.extend(["-rtsp_transport", "tcp"])
    elif input_fmt == "mjpeg":
        args.extend(["-f", "mjpeg"])

    args.extend(["-i", source])
    return args


def build_ffmpeg_h264_cmd(
    config: EngineConfig,
    pipeline_mode: str = "software",
    ffmpeg_bin: str = "ffmpeg",
) -> List[str]:
    """
    Builds a single end-to-end FFmpeg command that reads `config.stream_source`,
    decodes, scales/flips/rotates, and encodes low-latency H.264 Annex-B to `pipe:1`.
    """
    width, height = parse_resolution(config.resolution)
    fps = max(1, int(config.framerate or 30))
    bitrate_bps = parse_bitrate_bps(config.bitrate)

    cmd: List[str] = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "warning",
    ]
    cmd.extend(build_ffmpeg_input_args(config, pipeline_mode=pipeline_mode))

    vf_chain = build_ffmpeg_vf_filter(
        flip_h=config.flip_h,
        flip_v=config.flip_v,
        rotate90=config.rotate90,
        width=width,
        height=height,
        pipeline_mode=pipeline_mode,
    )
    if vf_chain:
        cmd.extend(["-vf", vf_chain])

    cmd.append("-an")

    if pipeline_mode in ("vaapi_hw", "vaapi"):
        cmd.extend(
            [
                "-c:v",
                "h264_vaapi",
                "-profile:v",
                "578",  # Constrained Baseline
                "-bf",
                "0",
                "-g",
                str(fps),
                "-b:v",
                str(bitrate_bps),
            ]
        )
    elif pipeline_mode == "v4l2m2m":
        cmd.extend(
            [
                "-c:v",
                "h264_v4l2m2m",
                "-bf",
                "0",
                "-g",
                str(fps),
                "-b:v",
                str(bitrate_bps),
            ]
        )
    else:
        cmd.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-tune",
                "zerolatency",
                "-profile:v",
                "baseline",
                "-bf",
                "0",
                "-g",
                str(fps),
                "-b:v",
                str(bitrate_bps),
            ]
        )

    cmd.extend(["-f", "h264", "-flush_packets", "1", "pipe:1"])
    return cmd


def get_candidate_pipeline_modes(config: EngineConfig) -> List[str]:
    """
    Returns the ordered list of FFmpeg pipeline modes to attempt for `config`,
    falling back gracefully from hardware decode+encode to software encode.
    """
    hw_mode = (config.hwaccel_mode or "none").strip().lower()
    device = (config.vaapi_device or "/dev/dri/renderD128").strip()

    if hw_mode == "vaapi":
        if os.path.exists(device) and os.access(device, os.R_OK | os.W_OK):
            return ["vaapi_hw", "vaapi", "software"]
        logger.warning(
            "[OctoCam HW] VAAPI device '%s' is missing or inaccessible; falling back to software H.264.",
            device,
        )
        return ["software"]

    if hw_mode == "v4l2m2m":
        return ["v4l2m2m", "software"]

    return ["software"]


def _find_annexb_nal_ranges(data: bytes) -> List[Tuple[int, int, int]]:
    """
    Finds all Annex-B NAL units in `data`.
    Returns a list of `(start_code_pos, nal_payload_start, nal_payload_end)`.
    """
    starts: List[Tuple[int, int]] = []
    pos = 0
    while True:
        idx = data.find(b"\x00\x00\x01", pos)
        if idx == -1:
            break
        sc_pos = idx - 1 if (idx > 0 and data[idx - 1] == 0) else idx
        nal_start = idx + 3
        starts.append((sc_pos, nal_start))
        pos = nal_start

    ranges: List[Tuple[int, int, int]] = []
    for i, (sc_pos, nal_start) in enumerate(starts):
        nal_end = starts[i + 1][0] if i + 1 < len(starts) else len(data)
        if nal_start < nal_end:
            ranges.append((sc_pos, nal_start, nal_end))
    return ranges


def split_h264_annexb_frames(
    buf: bytearray,
    flush_if_vcl: bool = True,
) -> List[bytes]:
    """
    Splits an H.264 Annex-B buffer into complete per-frame Access Units, removing
    emitted bytes from `buf` in-place.
    """
    data = bytes(buf)
    ranges = _find_annexb_nal_ranges(data)
    if not ranges:
        return []

    frames: List[bytes] = []
    au_start = ranges[0][0]
    has_vcl = False

    for sc_pos, nal_start, nal_end in ranges:
        nal_type = data[nal_start] & 0x1F
        first_mb_is_zero = (nal_end - nal_start < 2) or (
            (data[nal_start + 1] & 0x80) != 0
        )

        if has_vcl and (
            nal_type in (6, 7, 8, 9) or (nal_type in (1, 5) and first_mb_is_zero)
        ):
            frames.append(data[au_start:sc_pos])
            au_start = sc_pos
            has_vcl = False

        if nal_type in (1, 5):
            has_vcl = True

    if flush_if_vcl and has_vcl:
        frames.append(data[au_start:])
        del buf[:]
    elif au_start > 0:
        del buf[:au_start]

    return frames


class EncodedH264Frame:
    """Lightweight container for a pre-encoded H.264 Annex-B access unit produced by FFmpeg."""

    __slots__ = ("bitstream", "pts", "time_base", "is_keyframe")

    def __init__(
        self,
        bitstream: bytes,
        pts: int = 0,
        time_base: fractions.Fraction = fractions.Fraction(1, 90000),
        is_keyframe: bool = False,
    ) -> None:
        self.bitstream = bitstream
        self.pts = pts
        self.time_base = time_base
        self.is_keyframe = is_keyframe


def decode_h264_frame_to_jpeg(h264_annexb: bytes, quality: int = 85) -> bytes:
    """Decodes a single H.264 Annex-B keyframe (SPS + PPS + IDR) into JPEG bytes."""
    try:
        import av

        _silence_pyav_swscaler_warnings()
        codec = av.CodecContext.create("h264", "r")
        packets = list(codec.parse(h264_annexb))
        packets.extend(codec.parse(b""))
        for pkt in packets:
            for frame in codec.decode(pkt):
                pil_img = frame.to_image()
                buf = io.BytesIO()
                pil_img.save(buf, format="JPEG", quality=quality)
                return buf.getvalue()
        for frame in codec.decode(None):
            pil_img = frame.to_image()
            buf = io.BytesIO()
            pil_img.save(buf, format="JPEG", quality=quality)
            return buf.getvalue()
    except Exception as exc:
        logger.debug("PyAV H.264 keyframe snapshot decode failed (%s); trying ffmpeg.", exc)

    ffmpeg_bin = shutil.which("ffmpeg")
    if ffmpeg_bin:
        proc = subprocess.run(
            [
                ffmpeg_bin,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "h264",
                "-i",
                "pipe:0",
                "-frames:v",
                "1",
                "-f",
                "image2",
                "-c:v",
                "mjpeg",
                "-q:v",
                "3",
                "pipe:1",
            ],
            input=h264_annexb,
            capture_output=True,
            timeout=5.0,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout

    raise RuntimeError("Could not decode H.264 keyframe into JPEG snapshot.")


class FFmpegH264Stream:
    """
    Manages a single FFmpeg subprocess that reads the camera source, decodes, applies
    any orientation filters, and outputs an H.264 Annex-B bitstream shared across all
    subscribed WebRTC peer connections.
    """

    READ_CHUNK_SIZE = 262144

    def __init__(
        self,
        config: EngineConfig,
        logger_instance: Optional[logging.Logger] = None,
    ) -> None:
        self.config = config
        self._logger = logger_instance or logger
        self._proc: Optional[subprocess.Popen[bytes]] = None
        self._active_mode: Optional[str] = None
        self._lock = threading.Lock()
        self._subscribers: Set[queue_mod.Queue[bytes]] = set()
        self._sps_nal: Optional[bytes] = None
        self._pps_nal: Optional[bytes] = None
        self._latest_keyframe: Optional[bytes] = None
        self._keyframe_event = threading.Event()
        self._stopped = False
        self._frames_emitted = 0

    @property
    def active_mode(self) -> Optional[str]:
        return self._active_mode

    @property
    def latest_keyframe(self) -> Optional[bytes]:
        return self._latest_keyframe

    def start(self) -> None:
        """Starts the FFmpeg H.264 pipeline, trying hardware modes before software fallback."""
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            raise RuntimeError("The 'ffmpeg' binary is required but was not found in PATH.")

        candidates = get_candidate_pipeline_modes(self.config)
        errors: List[str] = []

        for mode in candidates:
            cmd = build_ffmpeg_h264_cmd(
                self.config, pipeline_mode=mode, ffmpeg_bin=ffmpeg_bin
            )
            self._logger.info(
                "[OctoCam HW] Starting FFmpeg pipeline (mode='%s'): %s",
                mode,
                " ".join(cmd),
            )
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                )
            except Exception as exc:
                errors.append(f"{mode}: {exc}")
                continue

            stderr_lines: List[str] = []
            initial_queue: queue_mod.Queue[bytes] = queue_mod.Queue(maxsize=4)
            with self._lock:
                self._subscribers.add(initial_queue)

            threading.Thread(
                target=self._stderr_reader,
                args=(proc, mode, stderr_lines),
                name=f"OctocamFFmpegStderr-{mode}",
                daemon=True,
            ).start()
            threading.Thread(
                target=self._stdout_reader,
                args=(proc,),
                name=f"OctocamFFmpegStdout-{mode}",
                daemon=True,
            ).start()

            first_frame: Optional[bytes] = None
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                try:
                    first_frame = initial_queue.get(timeout=0.05)
                    break
                except queue_mod.Empty:
                    if proc.poll() is not None:
                        break

            with self._lock:
                self._subscribers.discard(initial_queue)

            if first_frame is not None:
                self._proc = proc
                self._active_mode = mode
                self._logger.info(
                    "[OctoCam HW] FFmpeg H.264 pipeline ACTIVE (mode='%s', first_frame=%d bytes).",
                    mode,
                    len(first_frame),
                )
                return

            rc = proc.poll()
            _terminate_subprocess(proc)
            stderr_summary = " | ".join(stderr_lines) or "no stderr output"
            errors.append(f"{mode} (rc={rc}: {stderr_summary})")
            self._logger.warning(
                "[OctoCam HW] FFmpeg pipeline mode '%s' did not produce video frames (rc=%s, stderr='%s').",
                mode,
                rc,
                stderr_summary,
            )

        raise RuntimeError(
            f"Failed to start FFmpeg video pipeline for '{self.config.stream_source}': {'; '.join(errors)}"
        )

    def _stderr_reader(
        self,
        proc: subprocess.Popen[bytes],
        mode: str,
        lines_out: List[str],
    ) -> None:
        stderr_pipe = proc.stderr
        if stderr_pipe is None:
            return
        try:
            for raw_line in iter(stderr_pipe.readline, b""):
                text = raw_line.decode("utf-8", errors="replace").strip()
                if text:
                    lines_out.append(text)
                    if len(lines_out) > 25:
                        lines_out.pop(0)
                    self._logger.info("[OctoCam HW] [ffmpeg %s] %s", mode, text)
        except Exception:
            pass

    def _stdout_reader(self, proc: subprocess.Popen[bytes]) -> None:
        stdout_pipe = proc.stdout
        if stdout_pipe is None:
            return
        fd = stdout_pipe.fileno()
        buf = bytearray()
        try:
            while not self._stopped and proc.poll() is None:
                chunk = os.read(fd, self.READ_CHUNK_SIZE)
                if not chunk:
                    break
                buf.extend(chunk)
                flush_if_vcl = len(chunk) < self.READ_CHUNK_SIZE
                for au in split_h264_annexb_frames(buf, flush_if_vcl=flush_if_vcl):
                    self._process_access_unit(au)
        except Exception:
            pass

    def _process_access_unit(self, au: bytes) -> None:
        ranges = _find_annexb_nal_ranges(au)
        has_sps = False
        has_pps = False
        is_idr = False

        for sc_pos, nal_start, nal_end in ranges:
            nal_type = au[nal_start] & 0x1F
            if nal_type == 7:
                has_sps = True
                self._sps_nal = au[sc_pos:nal_end]
            elif nal_type == 8:
                has_pps = True
                self._pps_nal = au[sc_pos:nal_end]
            elif nal_type == 5:
                is_idr = True

        if is_idr:
            prefix = b""
            if not has_sps and self._sps_nal:
                prefix += self._sps_nal
            if not has_pps and self._pps_nal:
                prefix += self._pps_nal
            if prefix:
                au = prefix + au
            self._latest_keyframe = au
            self._keyframe_event.set()

        self._frames_emitted += 1
        if self._frames_emitted % 300 == 0:
            self._logger.info(
                "[OctoCam HW] FFmpeg H.264 stream heartbeat (mode='%s'): %d frames encoded.",
                self._active_mode,
                self._frames_emitted,
            )

        with self._lock:
            subscribers = list(self._subscribers)

        for q in subscribers:
            if q.full():
                try:
                    q.get_nowait()
                except queue_mod.Empty:
                    pass
            try:
                q.put_nowait(au)
            except queue_mod.Full:
                pass

    def subscribe(self) -> Any:
        """Creates a new `MediaStreamTrack` subscribed to this FFmpeg H.264 stream."""
        from aiortc import MediaStreamTrack

        q: queue_mod.Queue[bytes] = queue_mod.Queue(maxsize=2)
        if self._latest_keyframe is not None:
            q.put_nowait(self._latest_keyframe)

        with self._lock:
            self._subscribers.add(q)

        stream_ref = self
        fps = max(1, int(self.config.framerate or 30))

        class FFmpegH264StreamTrack(MediaStreamTrack):
            kind = "video"

            def __init__(self) -> None:
                super().__init__()
                self._queue = q
                self._pts = 0
                self._pts_step = int(90000 // fps)
                self._time_base = fractions.Fraction(1, 90000)
                self._last_au: Optional[bytes] = stream_ref.latest_keyframe

            async def recv(self) -> EncodedH264Frame:
                loop = asyncio.get_running_loop()
                au: Optional[bytes] = None
                try:
                    au = await loop.run_in_executor(
                        None, lambda: self._queue.get(timeout=1.0)
                    )
                except Exception:
                    if (
                        stream_ref._proc is not None
                        and stream_ref._proc.poll() is not None
                    ):
                        raise RuntimeError(
                            f"FFmpeg stream process exited with code {stream_ref._proc.returncode}"
                        )

                if au is not None:
                    self._last_au = au
                elif self._last_au is not None:
                    au = self._last_au
                else:
                    raise RuntimeError("No H.264 frame available from FFmpeg stream.")

                self._pts += self._pts_step
                return EncodedH264Frame(
                    bitstream=au,
                    pts=self._pts,
                    time_base=self._time_base,
                )

            def stop(self) -> None:
                super().stop()
                stream_ref._unsubscribe(self._queue)

        return FFmpegH264StreamTrack()

    def _unsubscribe(self, q: queue_mod.Queue[bytes]) -> None:
        with self._lock:
            self._subscribers.discard(q)
        while not q.empty():
            try:
                q.get_nowait()
            except Exception:
                break

    def capture_snapshot_jpeg(self, timeout: float = 2.0) -> bytes:
        """Decodes the most recent IDR keyframe from the live stream into a JPEG image."""
        if self._latest_keyframe is None:
            self._keyframe_event.wait(timeout=timeout)
        keyframe = self._latest_keyframe
        if not keyframe:
            raise RuntimeError("No H.264 keyframe available from active stream.")
        return decode_h264_frame_to_jpeg(keyframe)

    def stop(self) -> None:
        self._stopped = True
        with self._lock:
            subscribers = list(self._subscribers)
            self._subscribers.clear()
        for q in subscribers:
            while not q.empty():
                try:
                    q.get_nowait()
                except Exception:
                    break
        if self._proc is not None:
            _terminate_subprocess(self._proc)
            self._proc = None


def install_hardware_h264_encoder(
    aiortc_codecs_module: Any,
    aiortc_h264_module: Any,
    **_: Any,
) -> None:
    """
    Hooks `aiortc.codecs.h264.H264Encoder` so pre-encoded `EncodedH264Frame` bitstreams
    from `FFmpegH264Stream` pass directly into RTP packetization without re-encoding.
    """
    if not hasattr(aiortc_h264_module, "_OriginalH264Encoder"):
        aiortc_h264_module._OriginalH264Encoder = aiortc_h264_module.H264Encoder

    base_encoder_cls = aiortc_h264_module._OriginalH264Encoder

    class PassthroughH264Encoder(base_encoder_cls):  # type: ignore[misc, valid-type]
        def encode(
            self, frame: Any, force_keyframe: bool = False
        ) -> Tuple[List[bytes], int]:
            bitstream = getattr(frame, "bitstream", None)
            if bitstream is not None:
                packages = self._split_bitstream(bitstream)
                timestamp = int(getattr(frame, "pts", 0))
                return self._packetize(packages), timestamp
            return super().encode(frame, force_keyframe)

        def _encode_frame(self, frame: Any, force_keyframe: bool):
            bitstream = getattr(frame, "bitstream", None)
            if bitstream is not None:
                yield from self._split_bitstream(bitstream)
                return
            yield from super()._encode_frame(frame, force_keyframe)

    aiortc_h264_module.H264Encoder = PassthroughH264Encoder
    aiortc_codecs_module.H264Encoder = PassthroughH264Encoder


def apply_pil_transforms(
    image: Any,
    flip_h: bool = False,
    flip_v: bool = False,
    rotate90: bool = False,
) -> Any:
    """Applies `flip_h`, `flip_v`, and `rotate90` (CCW) to a PIL Image for snapshots."""
    from PIL import Image

    transpose_enum = getattr(Image, "Transpose", Image)
    if flip_h:
        image = image.transpose(transpose_enum.FLIP_LEFT_RIGHT)
    if flip_v:
        image = image.transpose(transpose_enum.FLIP_TOP_BOTTOM)
    if rotate90:
        image = image.transpose(transpose_enum.ROTATE_90)
    return image


def build_rtc_ice_servers(ice_servers: List[Dict[str, Any]]) -> List[Any]:
    """Converts dictionary `iceServers` into `aiortc.RTCIceServer` instances."""
    from aiortc import RTCIceServer

    rtc_servers: List[RTCIceServer] = []
    for item in ice_servers or []:
        urls = item.get("urls") or item.get("url")
        if not urls:
            continue
        kwargs: Dict[str, Any] = {"urls": urls}
        if item.get("username"):
            kwargs["username"] = str(item["username"])
        if item.get("credential"):
            kwargs["credential"] = str(item["credential"])
        rtc_servers.append(RTCIceServer(**kwargs))
    return rtc_servers


def _extract_sdp_video_codecs(sdp: str) -> List[str]:
    """Extracts the list of `a=rtpmap:...` codec strings inside the `m=video` section of an SDP."""
    codecs: List[str] = []
    in_video = False
    for raw_line in (sdp or "").splitlines():
        line = raw_line.strip()
        if line.startswith("m="):
            in_video = line.startswith("m=video")
        elif in_video and line.startswith("a=rtpmap:"):
            parts = line.split(" ", 1)
            if len(parts) == 2:
                codecs.append(parts[1].strip())
    return codecs


class WebRTCEngine:
    """
    Manages either the built-in `aiortc` + single-process FFmpeg WebRTC server or proxies
    SDP offers/answers to a local WHEP server (`go2rtc`, `MediaMTX`, etc.).
    """

    def __init__(self, logger_instance: Optional[logging.Logger] = None) -> None:
        self._logger = logger_instance or logger
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._pcs: Set[Any] = set()
        self._stream: Optional[FFmpegH264Stream] = None
        self._active_source_signature: Optional[str] = None

    def start(self) -> None:
        """Starts the background asyncio loop thread for `aiortc`."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._run_event_loop,
                name="OctocamWebRTCLoop",
                daemon=True,
            )
            self._thread.start()
            self._logger.info("OctoCam WebRTC background event loop started.")

    def stop(self) -> None:
        """Closes all active peer connections, stops the FFmpeg stream, and stops the loop."""
        with self._lock:
            loop = self._loop
            thread = self._thread
            if loop is None:
                return

        try:
            future = asyncio.run_coroutine_threadsafe(self._shutdown_async(), loop)
            future.result(timeout=5.0)
        except Exception as exc:
            self._logger.debug("Error during WebRTC engine async shutdown: %s", exc)

        with self._lock:
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._loop.stop)
            if thread is not None and thread.is_alive():
                thread.join(timeout=3.0)
            self._loop = None
            self._thread = None

    def _run_event_loop(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def handle_offer(
        self,
        sdp: str,
        sdp_type: str,
        config: EngineConfig,
    ) -> Dict[str, str]:
        """
        Processes a client WebRTC SDP offer and returns an SDP answer `{ "sdp": ..., "type": "answer" }`.
        Routes to either the WHEP proxy or the built-in `aiortc` + FFmpeg engine.
        """
        mode = (config.backend_mode or "aiortc").strip().lower()
        if mode == "whep":
            return self._handle_whep_offer(sdp=sdp, config=config)

        self.start()
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(
            self._handle_aiortc_offer_async(sdp=sdp, sdp_type=sdp_type, config=config),
            self._loop,
        )
        return future.result(timeout=20.0)

    def _handle_whep_offer(self, sdp: str, config: EngineConfig) -> Dict[str, str]:
        """Forwards the SDP offer to a local WHEP endpoint (e.g. go2rtc / MediaMTX)."""
        whep_url = (config.whep_url or "").strip()
        if not whep_url:
            raise RuntimeError(
                "WHEP mode is selected, but no WHEP Endpoint URL is configured."
            )

        headers = {
            "Content-Type": "application/sdp",
            "Accept": "application/sdp",
        }
        try:
            response = requests.post(
                whep_url,
                data=sdp.encode("utf-8"),
                headers=headers,
                timeout=10.0,
            )
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Failed to contact WHEP server at '{whep_url}': {exc}"
            ) from exc

        if response.status_code >= 400:
            raise RuntimeError(
                f"WHEP server returned HTTP {response.status_code}: {response.text[:250]}"
            )

        return {
            "sdp": response.text,
            "type": "answer",
        }

    async def _handle_aiortc_offer_async(
        self,
        sdp: str,
        sdp_type: str,
        config: EngineConfig,
    ) -> Dict[str, str]:
        from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription

        offered_codecs = _extract_sdp_video_codecs(sdp)
        self._logger.info(
            "[OctoCam HW] Incoming WebRTC SDP offer (video codecs offered by browser: %s)",
            offered_codecs,
        )

        self._configure_h264_passthrough(config)
        video_track = self._get_or_create_video_track(config)

        rtc_ice_servers = build_rtc_ice_servers(config.ice_servers)
        rtc_config = RTCConfiguration(iceServers=rtc_ice_servers)
        pc = RTCPeerConnection(configuration=rtc_config)
        pc._octocam_sub_track = video_track
        self._pcs.add(pc)

        @pc.on("connectionstatechange")
        async def on_connectionstatechange() -> None:
            self._logger.info("WebRTC peer connection state: %s", pc.connectionState)
            if pc.connectionState in ("failed", "closed", "disconnected"):
                await self._cleanup_pc(pc)

        async def _pc_connect_watchdog() -> None:
            await asyncio.sleep(25.0)
            if pc in self._pcs and getattr(pc, "connectionState", "") in (
                "new",
                "connecting",
            ):
                self._logger.warning(
                    "[OctoCam HW] Peer connection remained in '%s' state after 25s; cleaning up.",
                    pc.connectionState,
                )
                await self._cleanup_pc(pc)

        asyncio.ensure_future(_pc_connect_watchdog())

        pc.addTrack(video_track)
        self._prefer_h264_on_pc(pc)

        offer = RTCSessionDescription(sdp=sdp, type=sdp_type)
        await pc.setRemoteDescription(offer)
        self._prefer_h264_on_pc(pc)

        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        negotiated_codecs = _extract_sdp_video_codecs(pc.localDescription.sdp)
        self._logger.info(
            "[OctoCam HW] Outgoing WebRTC SDP answer negotiated video codecs: %s",
            negotiated_codecs,
        )

        return {
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type,
        }

    def _prefer_h264_on_pc(self, pc: Any) -> None:
        """Sets codec preferences on all video transceivers so `aiortc` selects `video/H264` over `video/VP8`."""
        try:
            import aiortc.codecs as aiortc_codecs

            video_codecs = list(aiortc_codecs.CODECS.get("video", []))
            h264_codecs = [
                c
                for c in video_codecs
                if getattr(c, "mimeType", "").lower() == "video/h264"
            ]
            other_codecs = [
                c
                for c in video_codecs
                if getattr(c, "mimeType", "").lower() != "video/h264"
            ]
            if not h264_codecs:
                return
            preferred = h264_codecs + other_codecs
            for transceiver in pc.getTransceivers():
                if getattr(transceiver, "kind", "") == "video" and hasattr(
                    transceiver, "setCodecPreferences"
                ):
                    transceiver.setCodecPreferences(preferred)
        except Exception as exc:
            self._logger.debug(
                "[OctoCam HW] Could not set H264 codec preferences on transceiver: %s",
                exc,
            )

    def _configure_h264_passthrough(self, config: EngineConfig) -> None:
        """Prioritizes `video/H264` in `aiortc.codecs.CODECS` and installs `PassthroughH264Encoder`."""
        try:
            import aiortc.codecs as aiortc_codecs
            import aiortc.codecs.h264 as aiortc_h264

            if hasattr(aiortc_codecs, "CODECS") and "video" in aiortc_codecs.CODECS:
                video_codecs = list(aiortc_codecs.CODECS["video"])
                h264_codecs = [
                    c
                    for c in video_codecs
                    if getattr(c, "mimeType", "").lower() == "video/h264"
                ]
                other_codecs = [
                    c
                    for c in video_codecs
                    if getattr(c, "mimeType", "").lower() != "video/h264"
                ]
                if h264_codecs:
                    aiortc_codecs.CODECS["video"] = h264_codecs + other_codecs

            target_bps = parse_bitrate_bps(config.bitrate)
            aiortc_h264.DEFAULT_BITRATE = target_bps
            aiortc_h264.MAX_BITRATE = max(target_bps * 2, 5_000_000)

            install_hardware_h264_encoder(
                aiortc_codecs_module=aiortc_codecs,
                aiortc_h264_module=aiortc_h264,
            )
        except Exception as exc:
            self._logger.warning(
                "[OctoCam HW] Could not configure H264 passthrough encoder: %s", exc
            )

    def _get_or_create_video_track(self, config: EngineConfig) -> Any:
        signature = (
            f"{config.stream_source}|{config.input_format}|{config.resolution}|"
            f"{config.framerate}|{config.bitrate}|{config.hwaccel_mode}|"
            f"{config.vaapi_device}|{int(config.flip_h)}|{int(config.flip_v)}|"
            f"{int(config.rotate90)}"
        )

        if (
            self._stream is None
            or self._active_source_signature != signature
            or (
                self._stream._proc is not None
                and self._stream._proc.poll() is not None
            )
        ):
            self._close_stream()
            stream = FFmpegH264Stream(config=config, logger_instance=self._logger)
            stream.start()
            self._stream = stream
            self._active_source_signature = signature

        return self._stream.subscribe()

    async def _cleanup_pc(self, pc: Any) -> None:
        if pc in self._pcs:
            self._pcs.discard(pc)
        sub_track = getattr(pc, "_octocam_sub_track", None)
        if sub_track is not None:
            try:
                sub_track.stop()
            except Exception:
                pass
            pc._octocam_sub_track = None
        try:
            await pc.close()
        except Exception:
            pass
        if not self._pcs:
            self._close_stream()

    def _close_stream(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception:
                pass
            self._stream = None
        self._active_source_signature = None

    async def _shutdown_async(self) -> None:
        for pc in list(self._pcs):
            sub_track = getattr(pc, "_octocam_sub_track", None)
            if sub_track is not None:
                try:
                    sub_track.stop()
                except Exception:
                    pass
                pc._octocam_sub_track = None
        coros = [pc.close() for pc in list(self._pcs)]
        self._pcs.clear()
        if coros:
            await asyncio.gather(*coros, return_exceptions=True)
        self._close_stream()

    def capture_snapshot(self, config: EngineConfig) -> bytes:
        """
        Captures a JPEG snapshot from:
        1. `config.snapshot_url` (if configured),
        2. The active `FFmpegH264Stream`'s latest IDR keyframe (if a stream is running), or
        3. A single-frame FFmpeg / PyAV grab from `config.stream_source`.
        """
        from PIL import Image

        snapshot_url = (config.snapshot_url or "").strip()
        if snapshot_url:
            resp = requests.get(snapshot_url, timeout=5.0)
            resp.raise_for_status()
            if not (config.flip_h or config.flip_v or config.rotate90):
                return resp.content
            pil_img = Image.open(io.BytesIO(resp.content))
            pil_img = apply_pil_transforms(
                pil_img,
                flip_h=config.flip_h,
                flip_v=config.flip_v,
                rotate90=config.rotate90,
            )
            out_buf = io.BytesIO()
            pil_img.save(out_buf, format="JPEG", quality=85)
            return out_buf.getvalue()

        # If a live WebRTC stream is active on the camera, decode its latest IDR keyframe
        # so we don't collide with V4L2 exclusive device access (/dev/videoX EBUSY).
        if self._stream is not None:
            try:
                return self._stream.capture_snapshot_jpeg(timeout=2.0)
            except Exception as exc:
                self._logger.debug(
                    "Could not capture snapshot from active FFmpeg stream (%s); falling back to direct open.",
                    exc,
                )

        ffmpeg_bin = shutil.which("ffmpeg")
        if ffmpeg_bin:
            cmd = [ffmpeg_bin, "-hide_banner", "-loglevel", "error"]
            cmd.extend(build_ffmpeg_input_args(config, pipeline_mode="software"))
            vf_chain = build_ffmpeg_vf_filter(
                flip_h=config.flip_h,
                flip_v=config.flip_v,
                rotate90=config.rotate90,
                pipeline_mode="snapshot",
            )
            if vf_chain:
                cmd.extend(["-vf", vf_chain])
            cmd.extend(
                [
                    "-frames:v",
                    "1",
                    "-f",
                    "image2",
                    "-c:v",
                    "mjpeg",
                    "-q:v",
                    "3",
                    "pipe:1",
                ]
            )
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=5.0,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout:
                return proc.stdout

        import av

        _silence_pyav_swscaler_warnings()
        source = (config.stream_source or "/dev/video0").strip()
        fmt = "v4l2" if source.startswith("/dev/video") else None
        container = av.open(source, format=fmt, timeout=5.0)
        try:
            for frame in container.decode(video=0):
                pil_img = frame.to_image()
                pil_img = apply_pil_transforms(
                    pil_img,
                    flip_h=config.flip_h,
                    flip_v=config.flip_v,
                    rotate90=config.rotate90,
                )
                buf = io.BytesIO()
                pil_img.save(buf, format="JPEG", quality=85)
                return buf.getvalue()
        finally:
            container.close()

        raise RuntimeError("No video frame could be decoded for snapshot.")
