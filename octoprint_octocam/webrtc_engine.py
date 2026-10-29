# coding=utf-8
from __future__ import annotations

import asyncio
import io
import logging
import os
import shutil
import subprocess
import threading
import weakref
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

import requests

logger = logging.getLogger("octoprint.plugins.octocam")
_ACTIVE_HW_ENCODERS: "weakref.WeakSet[Any]" = weakref.WeakSet()


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


def build_media_player_kwargs(config: EngineConfig) -> Dict[str, Any]:
    """
    Constructs `aiortc.contrib.media.MediaPlayer` arguments including Linux V4L2/MJPEG/RTSP
    format selection and VAAPI hardware decode options when available.
    """
    source = (config.stream_source or "/dev/video0").strip()
    input_fmt = (config.input_format or "auto").strip().lower()

    resolved_format: Optional[str] = None
    if input_fmt != "auto":
        resolved_format = input_fmt
    elif source.startswith("/dev/video"):
        resolved_format = "v4l2"

    options: Dict[str, str] = {
        "fflags": "nobuffer",
        "flags": "low_delay",
    }

    if config.resolution and "x" in config.resolution:
        options["video_size"] = config.resolution.strip()
    if config.framerate and int(config.framerate) > 0:
        options["framerate"] = str(int(config.framerate))

    if source.startswith("rtsp://"):
        options["rtsp_transport"] = "tcp"
        options["stimeout"] = "5000000"

    hw_mode = (config.hwaccel_mode or "none").strip().lower()
    vaapi_dev = (config.vaapi_device or "/dev/dri/renderD128").strip()
    if hw_mode == "vaapi" and os.path.exists(vaapi_dev) and os.access(vaapi_dev, os.R_OK | os.W_OK):
        options["hwaccel"] = "vaapi"
        options["hwaccel_device"] = vaapi_dev
        # Request MJPEG from V4L2 webcams when auto-detecting so VAAPI can hardware-decode
        if resolved_format == "v4l2" and input_fmt == "auto":
            options["input_format"] = "mjpeg"

    result: Dict[str, Any] = {
        "file": source,
        "options": options,
    }
    if resolved_format:
        result["format"] = resolved_format

    return result


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


# Linux V4L2 ioctl constants (<linux/videodev2.h>)
V4L2_CID_BASE = 0x00980900
V4L2_CID_HFLIP = V4L2_CID_BASE + 20  # 0x00980914
V4L2_CID_VFLIP = V4L2_CID_BASE + 21  # 0x00980915
V4L2_CID_ROTATE = V4L2_CID_BASE + 34  # 0x00980922
VIDIOC_S_CTRL = 0xC008561C  # _IOWR('V', 28, struct v4l2_control)


def _set_v4l2_control(
    device_path: str,
    ctrl_name: str,
    cid: int,
    value: int,
) -> bool:
    """
    Attempts to set a V4L2 control on `device_path` using native Linux `fcntl.ioctl`
    (`VIDIOC_S_CTRL`) first, falling back to `v4l2-ctl` if available.
    Returns True if the camera accepted the control setting.
    """
    import struct

    try:
        import fcntl

        fd = os.open(device_path, os.O_RDWR | os.O_NONBLOCK)
        try:
            ctrl_buf = struct.pack("<Ii", cid, int(value))
            fcntl.ioctl(fd, VIDIOC_S_CTRL, ctrl_buf)
            logger.info(
                "[OctoCam HW] V4L2 ioctl VIDIOC_S_CTRL succeeded on %s: %s (0x%08x) = %d",
                device_path,
                ctrl_name,
                cid,
                int(value),
            )
            return True
        finally:
            os.close(fd)
    except Exception as exc:
        logger.debug(
            "[OctoCam HW] V4L2 ioctl VIDIOC_S_CTRL not supported/failed on %s for %s=%d: %s",
            device_path,
            ctrl_name,
            int(value),
            exc,
        )

    v4l2_ctl = shutil.which("v4l2-ctl")
    if v4l2_ctl:
        try:
            proc = subprocess.run(
                [
                    v4l2_ctl,
                    "-d",
                    device_path,
                    f"--set-ctrl={ctrl_name}={int(value)}",
                ],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            if proc.returncode == 0:
                logger.info(
                    "[OctoCam HW] v4l2-ctl succeeded on %s: %s = %d",
                    device_path,
                    ctrl_name,
                    int(value),
                )
                return True
            logger.debug(
                "[OctoCam HW] v4l2-ctl failed on %s for %s=%d: %s",
                device_path,
                ctrl_name,
                int(value),
                (proc.stderr or proc.stdout or "").strip(),
            )
        except Exception as exc:
            logger.debug(
                "[OctoCam HW] v4l2-ctl exception on %s for %s=%d: %s",
                device_path,
                ctrl_name,
                int(value),
                exc,
            )

    return False


def configure_v4l2_camera_transforms(
    stream_source: str,
    flip_h: bool = False,
    flip_v: bool = False,
    rotate90: bool = False,
) -> Dict[str, bool]:
    """
    Checks if `stream_source` is a local V4L2 webcam (`/dev/video*`) that supports
    in-camera hardware flipping/rotation (`horizontal_flip`, `vertical_flip`, `rotate`).
    Applies the requested states directly on the camera and returns which requested
    transforms were handled in camera hardware so they do not need to be repeated
    during encoding.
    """
    source = (stream_source or "").strip()
    if not source.startswith("/dev/video") or not os.path.exists(source):
        logger.info(
            "[OctoCam HW] Stream source '%s' is not a local /dev/video* device; skipping V4L2 in-camera hardware transform controls.",
            source,
        )
        return {
            "flip_h_applied": False,
            "flip_v_applied": False,
            "rotate90_applied": False,
        }

    hflip_supported = _set_v4l2_control(
        source, "horizontal_flip", V4L2_CID_HFLIP, 1 if flip_h else 0
    )
    vflip_supported = _set_v4l2_control(
        source, "vertical_flip", V4L2_CID_VFLIP, 1 if flip_v else 0
    )
    # In V4L2, `rotate` is clockwise degrees, so 270 deg CW == 90 deg CCW
    rotate_supported = _set_v4l2_control(
        source, "rotate", V4L2_CID_ROTATE, 270 if rotate90 else 0
    )

    result = {
        "flip_h_applied": bool(flip_h and hflip_supported),
        "flip_v_applied": bool(flip_v and vflip_supported),
        "rotate90_applied": bool(rotate90 and rotate_supported),
    }
    logger.info(
        "[OctoCam HW] V4L2 camera hardware transform check on '%s': requested=(flip_h=%s, flip_v=%s, rotate90=%s), supported=(hflip=%s, vflip=%s, rotate=%s), applied_in_camera=%s",
        source,
        flip_h,
        flip_v,
        rotate90,
        hflip_supported,
        vflip_supported,
        rotate_supported,
        result,
    )
    return result


def build_ffmpeg_vf_filter(
    flip_h: bool = False,
    flip_v: bool = False,
    rotate90: bool = False,
    include_vaapi_hwupload: bool = False,
    use_vaapi_transpose: bool = True,
    vaapi_hwdecode: bool = False,
) -> str:
    """
    Constructs an FFmpeg `-vf` filtergraph string.
    - When `vaapi_hwdecode=True` (frames decoded directly onto VAAPI surfaces via
      `-hwaccel vaapi -hwaccel_output_format vaapi`), uses `scale_vaapi=format=nv12`
      on the GPU to convert decoded surfaces (such as 422H MJPEG surfaces) to `nv12`
      before applying `transpose_vaapi` and `h264_vaapi`.
    - When `include_vaapi_hwupload=True` and `use_vaapi_transpose=True`, uploads `nv12`
      frames to a VAAPI surface (`format=nv12,hwupload,scale_vaapi=format=nv12`) and
      performs flips/rotations on the GPU via `transpose_vaapi`.
    """
    filters: List[str] = []
    if vaapi_hwdecode:
        filters.append("scale_vaapi=format=nv12")
        if use_vaapi_transpose:
            if flip_h:
                filters.append("transpose_vaapi=dir=hflip")
            if flip_v:
                filters.append("transpose_vaapi=dir=vflip")
            if rotate90:
                filters.append("transpose_vaapi=dir=cclock")
        return ",".join(filters)

    if include_vaapi_hwupload and use_vaapi_transpose:
        filters.append("format=nv12")
        filters.append("hwupload")
        filters.append("scale_vaapi=format=nv12")
        if flip_h:
            filters.append("transpose_vaapi=dir=hflip")
        if flip_v:
            filters.append("transpose_vaapi=dir=vflip")
        if rotate90:
            filters.append("transpose_vaapi=dir=cclock")
        return ",".join(filters)

    if flip_h:
        filters.append("hflip")
    if flip_v:
        filters.append("vflip")
    if rotate90:
        filters.append("transpose=cclock")
    if include_vaapi_hwupload:
        filters.append("format=nv12")
        filters.append("hwupload")
    return ",".join(filters)


class FFmpegFrameFilter:
    """
    Applies FFmpeg `libavfilter` (`hflip`, `vflip`, `transpose=cclock`) to PyAV
    `VideoFrame` instances using a persistent `av.filter.Graph`.
    """

    def __init__(
        self,
        flip_h: bool = False,
        flip_v: bool = False,
        rotate90: bool = False,
    ) -> None:
        self.flip_h = flip_h
        self.flip_v = flip_v
        self.rotate90 = rotate90
        self._graph: Optional[Any] = None
        self._frame_sig: Optional[tuple[int, int, str]] = None

    @property
    def active(self) -> bool:
        return bool(self.flip_h or self.flip_v or self.rotate90)

    def transform(self, frame: Any) -> Any:
        if not self.active:
            return frame

        import av

        _silence_pyav_swscaler_warnings()
        pts = frame.pts
        time_base = frame.time_base
        sig = (frame.width, frame.height, frame.format.name)

        if self._graph is None or self._frame_sig != sig:
            graph = av.filter.Graph()
            nodes = [graph.add_buffer(template=frame)]
            if self.flip_h:
                nodes.append(graph.add("hflip"))
            if self.flip_v:
                nodes.append(graph.add("vflip"))
            if self.rotate90:
                nodes.append(graph.add("transpose", "cclock"))
            nodes.append(graph.add("buffersink"))

            for prev_node, next_node in zip(nodes, nodes[1:]):
                prev_node.link_to(next_node)

            graph.configure()
            self._graph = graph
            self._frame_sig = sig

        self._graph.push(frame)
        out_frame = self._graph.pull()
        out_frame.pts = pts
        out_frame.time_base = time_base
        return out_frame


def create_transformed_video_track(
    source_track: Any,
    flip_h: bool = False,
    flip_v: bool = False,
    rotate90: bool = False,
) -> Any:
    """
    Wraps an `aiortc.MediaStreamTrack` so decoded frames are transformed by FFmpeg's
    `libavfilter` (`hflip`, `vflip`, `transpose=cclock`) prior to encoding.
    """
    if not (flip_h or flip_v or rotate90):
        return source_track

    from aiortc import MediaStreamTrack

    class TransformedVideoStreamTrack(MediaStreamTrack):
        kind = "video"

        def __init__(self, track: Any) -> None:
            super().__init__()
            self._track = track
            self._ffmpeg_filter = FFmpegFrameFilter(
                flip_h=flip_h,
                flip_v=flip_v,
                rotate90=rotate90,
            )

        async def recv(self) -> Any:
            frame = await self._track.recv()
            return self._ffmpeg_filter.transform(frame)

        def stop(self) -> None:
            super().stop()
            if hasattr(self._track, "stop"):
                self._track.stop()

    return TransformedVideoStreamTrack(source_track)


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


def create_vaapi_hwdecode_track(
    config: EngineConfig,
    flip_h: bool = False,
    flip_v: bool = False,
    rotate90: bool = False,
    log: Optional[logging.Logger] = None,
) -> tuple[Optional[Any], bool]:
    """
    Attempts to open `config.stream_source` using the `ffmpeg` CLI with VAAPI hardware
    decoding (`-hwaccel vaapi -hwaccel_device <dev> -hwaccel_output_format vaapi`) and GPU
    `scale_vaapi=format=nv12` (+ optional `transpose_vaapi`), outputting raw `nv12` frames
    wrapped as `av.VideoFrame` objects.

    Returns `(track, transforms_applied_on_gpu)`. If VAAPI hardware decoding is unavailable
    or fails to produce a frame, logs the diagnostic reason and returns `(None, False)` so
    the caller can fall back to `MediaPlayer`.
    """
    active_log = log or logger
    hw_mode = (config.hwaccel_mode or "none").strip().lower()
    device = (config.vaapi_device or "/dev/dri/renderD128").strip()
    if hw_mode != "vaapi":
        return None, False

    if not (os.path.exists(device) and os.access(device, os.R_OK | os.W_OK)):
        active_log.warning(
            "[OctoCam HW] Cannot use VAAPI hardware decoding: device '%s' is missing or not readable/writable.",
            device,
        )
        return None, False

    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        active_log.warning(
            "[OctoCam HW] Cannot use VAAPI hardware decoding: 'ffmpeg' binary not found in PATH."
        )
        return None, False

    try:
        import fractions
        import queue as queue_mod
        import time

        import av
        import numpy as np
        from aiortc import MediaStreamTrack
    except Exception as exc:
        active_log.warning(
            "[OctoCam HW] Cannot initialize VAAPI hardware decode track (missing dependency: %s).",
            exc,
        )
        return None, False

    res_str = (config.resolution or "1280x720").strip().lower()
    try:
        in_w_str, in_h_str = res_str.split("x", 1)
        in_w = max(16, int(in_w_str))
        in_h = max(16, int(in_h_str))
    except Exception:
        in_w, in_h = 1280, 720

    fps = max(1, int(config.framerate or 30))
    source = (config.stream_source or "/dev/video0").strip()
    input_fmt = (config.input_format or "auto").strip().lower()
    needs_transform = bool(flip_h or flip_v or rotate90)

    # Try with GPU transpose_vaapi first if transforms are requested; if the driver lacks
    # VAAPI VPP mirror/rotation support, retry VAAPI hardware decode with scale_vaapi only.
    transpose_attempts = [True, False] if needs_transform else [False]

    for use_gpu_transpose in transpose_attempts:
        out_w = in_h if (use_gpu_transpose and rotate90) else in_w
        out_h = in_w if (use_gpu_transpose and rotate90) else in_h
        frame_byte_size = (out_w * out_h * 3) // 2

        vf_parts = [f"scale_vaapi=w={in_w}:h={in_h}:format=nv12"]
        if use_gpu_transpose:
            if flip_h:
                vf_parts.append("transpose_vaapi=dir=hflip")
            if flip_v:
                vf_parts.append("transpose_vaapi=dir=vflip")
            if rotate90:
                vf_parts.append("transpose_vaapi=dir=cclock")
        vf_parts.append("hwdownload")
        vf_parts.append("format=nv12")
        vf_chain = ",".join(vf_parts)

        cmd: List[str] = [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-hwaccel",
            "vaapi",
            "-hwaccel_device",
            device,
            "-hwaccel_output_format",
            "vaapi",
        ]

        if source.startswith("/dev/video"):
            cmd.extend(["-f", "v4l2"])
            if input_fmt in ("auto", "mjpeg"):
                cmd.extend(["-input_format", "mjpeg"])
            elif input_fmt != "v4l2":
                cmd.extend(["-input_format", input_fmt])
            cmd.extend(
                [
                    "-video_size",
                    f"{in_w}x{in_h}",
                    "-framerate",
                    str(fps),
                ]
            )
        elif source.startswith("rtsp://"):
            cmd.extend(["-rtsp_transport", "tcp"])

        cmd.extend(
            [
                "-i",
                source,
                "-vf",
                vf_chain,
                "-an",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "nv12",
                "pipe:1",
            ]
        )

        active_log.info(
            "[OctoCam HW] Starting FFmpeg VAAPI hardware decode subprocess (gpu_transpose=%s): %s",
            use_gpu_transpose,
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
            active_log.warning(
                "[OctoCam HW] Failed to launch FFmpeg VAAPI decode subprocess: %s",
                exc,
            )
            return None, False

        frame_queue: queue_mod.Queue[bytes] = queue_mod.Queue(maxsize=2)
        stderr_lines: List[str] = []

        def _stderr_reader(p: subprocess.Popen[bytes], lines_out: List[str]) -> None:
            stderr_pipe = p.stderr
            if stderr_pipe is None:
                return
            try:
                for raw_line in iter(stderr_pipe.readline, b""):
                    text = raw_line.decode("utf-8", errors="replace").strip()
                    if text:
                        lines_out.append(text)
                        if len(lines_out) > 25:
                            lines_out.pop(0)
                        active_log.info("[OctoCam HW] [ffmpeg vaapi-decode] %s", text)
            except Exception:
                pass

        def _stdout_reader(
            p: subprocess.Popen[bytes],
            q: queue_mod.Queue[bytes],
            expected_bytes: int,
        ) -> None:
            stdout_pipe = p.stdout
            if stdout_pipe is None:
                return
            buf = bytearray()
            fd = stdout_pipe.fileno()
            try:
                while p.poll() is None:
                    chunk = os.read(fd, max(65536, expected_bytes - len(buf)))
                    if not chunk:
                        break
                    buf.extend(chunk)
                    while len(buf) >= expected_bytes:
                        frame_bytes = bytes(buf[:expected_bytes])
                        del buf[:expected_bytes]
                        if q.full():
                            try:
                                q.get_nowait()
                            except queue_mod.Empty:
                                pass
                        q.put_nowait(frame_bytes)
            except Exception:
                pass

        threading.Thread(
            target=_stderr_reader,
            args=(proc, stderr_lines),
            name="OctocamVAAPIDecodeStderr",
            daemon=True,
        ).start()
        threading.Thread(
            target=_stdout_reader,
            args=(proc, frame_queue, frame_byte_size),
            name="OctocamVAAPIDecodeStdout",
            daemon=True,
        ).start()

        # Wait up to 2.5s for the first hardware-decoded nv12 frame
        first_frame_bytes: Optional[bytes] = None
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            try:
                first_frame_bytes = frame_queue.get(timeout=0.05)
                break
            except queue_mod.Empty:
                if proc.poll() is not None:
                    break

        if first_frame_bytes is None:
            rc = proc.poll()
            _terminate_subprocess(proc)
            stderr_summary = " | ".join(stderr_lines) or "no stderr output"
            active_log.warning(
                "[OctoCam HW] FFmpeg VAAPI hardware decode attempt (gpu_transpose=%s) did not produce frames (rc=%s, stderr='%s').",
                use_gpu_transpose,
                rc,
                stderr_summary,
            )
            continue

        # Put the first frame back so the consumer receives it immediately
        frame_queue.put_nowait(first_frame_bytes)
        active_log.info(
            "[OctoCam HW] VAAPI hardware decode ACTIVE on '%s': received first GPU-decoded nv12 frame (%dx%d, %d bytes, gpu_transpose=%s).",
            device,
            out_w,
            out_h,
            len(first_frame_bytes),
            use_gpu_transpose,
        )

        class FFmpegVAAPIVideoStreamTrack(MediaStreamTrack):
            kind = "video"

            def __init__(
                self,
                vaapi_proc: subprocess.Popen[bytes],
                q: queue_mod.Queue[bytes],
                width: int,
                height: int,
                framerate_val: int,
            ) -> None:
                super().__init__()
                self._proc = vaapi_proc
                self._queue = q
                self._width = width
                self._height = height
                self._fps = framerate_val
                self._pts = 0
                self._time_base = fractions.Fraction(1, 90000)
                self._pts_step = int(90000 // framerate_val)
                self._last_frame: Optional[Any] = None
                self._frames_delivered = 0

            async def recv(self) -> Any:
                loop = asyncio.get_running_loop()
                raw: Optional[bytes] = None
                try:
                    raw = await loop.run_in_executor(
                        None, lambda: self._queue.get(timeout=1.0)
                    )
                except Exception:
                    if self._proc.poll() is not None:
                        raise RuntimeError(
                            f"FFmpeg VAAPI hardware decode process exited with code {self._proc.returncode}"
                        )

                if raw is not None:
                    arr = np.frombuffer(raw, dtype=np.uint8).reshape(
                        (self._height * 3 // 2, self._width)
                    )
                    video_frame = av.VideoFrame.from_ndarray(arr, format="nv12")
                    self._last_frame = video_frame
                    self._frames_delivered += 1
                    if self._frames_delivered % 300 == 0:
                        active_log.info(
                            "[OctoCam HW] VAAPI hardware decode heartbeat: %d nv12 frames decoded (%dx%d).",
                            self._frames_delivered,
                            self._width,
                            self._height,
                        )
                elif self._last_frame is not None:
                    video_frame = self._last_frame
                else:
                    raise RuntimeError("No frame available from VAAPI hardware decode")

                self._pts += self._pts_step
                video_frame.pts = self._pts
                video_frame.time_base = self._time_base
                return video_frame

            def stop(self) -> None:
                super().stop()
                self._last_frame = None
                while not self._queue.empty():
                    try:
                        self._queue.get_nowait()
                    except Exception:
                        break
                if self._proc is not None:
                    _terminate_subprocess(self._proc)
                    self._proc = None  # type: ignore[assignment]

        return (
            FFmpegVAAPIVideoStreamTrack(
                vaapi_proc=proc,
                q=frame_queue,
                width=out_w,
                height=out_h,
                framerate_val=fps,
            ),
            use_gpu_transpose,
        )

    return None, False


def install_hardware_h264_encoder(
    aiortc_codecs_module: Any,
    aiortc_h264_module: Any,
    hwaccel_mode: str,
    vaapi_device: str,
    target_bitrate: int,
    framerate: int = 30,
    vaapi_flip_h: bool = False,
    vaapi_flip_v: bool = False,
    vaapi_rotate90: bool = False,
    logger_instance: Optional[logging.Logger] = None,
) -> None:
    """
    Hooks `aiortc.codecs.h264.H264Encoder` to support Linux hardware H.264 encoding
    (`vaapi` via `h264_vaapi` with GPU `transpose_vaapi`, or `v4l2m2m` via `h264_v4l2m2m`)
    with automatic fallback to `libx264` software encoding.
    """
    import fractions

    import av

    _silence_pyav_swscaler_warnings()
    active_log = logger_instance or logger

    if not hasattr(aiortc_h264_module, "_OriginalH264Encoder"):
        aiortc_h264_module._OriginalH264Encoder = aiortc_h264_module.H264Encoder

    base_encoder_cls = aiortc_h264_module._OriginalH264Encoder
    mode = (hwaccel_mode or "none").strip().lower()
    device = (vaapi_device or "/dev/dri/renderD128").strip()
    fps = max(1, int(framerate or 30))

    active_log.info(
        "[OctoCam HW] Installing H264Encoder hook: mode='%s', vaapi_device='%s', target_bitrate=%d bps, fps=%d, encode_transforms=(flip_h=%s, flip_v=%s, rotate90=%s)",
        mode,
        device,
        target_bitrate,
        fps,
        vaapi_flip_h,
        vaapi_flip_v,
        vaapi_rotate90,
    )

    class HardwareH264Encoder(base_encoder_cls):  # type: ignore[misc, valid-type]
        def __init__(self) -> None:
            super().__init__()
            self._hw_failed = mode not in ("vaapi", "v4l2m2m")
            self._hw_codec: Optional[Any] = None
            self._vaapi_proc: Optional[subprocess.Popen[bytes]] = None
            self._hw_dims: Optional[tuple[int, int]] = None
            self._reformatter: Optional[Any] = None
            self._use_vaapi_transpose = True
            self._vaapi_stderr: List[str] = []
            self._logged_first_frame_in = False
            self._logged_first_hw_packet = False
            self._hw_frames_encoded = 0
            self._hw_bytes_encoded = 0
            self._fallback_filter = FFmpegFrameFilter(
                flip_h=vaapi_flip_h,
                flip_v=vaapi_flip_v,
                rotate90=vaapi_rotate90,
            )
            _ACTIVE_HW_ENCODERS.add(self)
            active_log.info(
                "[OctoCam HW] H264Encoder instance created by aiortc (configured_mode='%s', initial_hw_active=%s).",
                mode,
                not self._hw_failed,
            )

        def _reformat_frame(self, frame: Any, target_format: str) -> Any:
            """
            Reformats `frame` to `target_format` using a persistent `VideoReformatter`
            so `SwsContext` is reused across frames instead of re-initialized every frame.
            """
            if frame.format.name == target_format:
                return frame
            if self._reformatter is None:
                try:
                    from av.video.reformatter import VideoReformatter

                    self._reformatter = VideoReformatter()
                except Exception:
                    self._reformatter = False
            if self._reformatter:
                return self._reformatter.reformat(frame, format=target_format)
            return frame.reformat(format=target_format)

        def _encode_frame(self, frame: Any, force_keyframe: bool):
            if not self._logged_first_frame_in:
                self._logged_first_frame_in = True
                active_log.info(
                    "[OctoCam HW] H264Encoder._encode_frame received first frame: %dx%d, pix_fmt='%s', mode='%s', hw_failed=%s, force_keyframe=%s",
                    getattr(frame, "width", -1),
                    getattr(frame, "height", -1),
                    getattr(getattr(frame, "format", None), "name", "unknown"),
                    mode,
                    self._hw_failed,
                    force_keyframe,
                )

            if self._hw_failed:
                if mode == "vaapi" and self._fallback_filter.active:
                    frame = self._fallback_filter.transform(frame)
                frame = self._reformat_frame(frame, "yuv420p")
                yield from super()._encode_frame(frame, force_keyframe)
                return

            try:
                if mode == "v4l2m2m":
                    yield from self._encode_v4l2m2m(frame, force_keyframe)
                    return
                if mode == "vaapi":
                    yield from self._encode_vaapi(frame, force_keyframe)
                    return
            except Exception as exc:
                active_log.warning(
                    "[OctoCam HW] Hardware H.264 encoder (%s) FAILED (%s); falling back to CPU libx264 software encoding.",
                    mode,
                    exc,
                )
                self._cleanup_hw()
                self._hw_failed = True

            if mode == "vaapi" and self._fallback_filter.active:
                frame = self._fallback_filter.transform(frame)
            frame = self._reformat_frame(frame, "yuv420p")
            yield from super()._encode_frame(frame, force_keyframe)

        def _encode_v4l2m2m(self, frame: Any, force_keyframe: bool):
            dims = (frame.width, frame.height)
            if self._hw_codec is None or self._hw_dims != dims:
                active_log.info(
                    "[OctoCam HW] Initializing V4L2 M2M H.264 hardware encoder ('h264_v4l2m2m') for %dx%d @ %d fps.",
                    frame.width,
                    frame.height,
                    fps,
                )
                codec = av.CodecContext.create("h264_v4l2m2m", "w")
                codec.width = frame.width
                codec.height = frame.height
                codec.bit_rate = self.target_bitrate or target_bitrate
                codec.pix_fmt = "yuv420p"
                codec.framerate = fractions.Fraction(fps, 1)
                codec.time_base = fractions.Fraction(1, fps)
                codec.options = {"num_capture_buffers": "16", "num_output_buffers": "8"}
                self._hw_codec = codec
                self._hw_dims = dims

            frame = self._reformat_frame(frame, "yuv420p")
            if force_keyframe:
                frame.pict_type = av.video.frame.PictureType.I
            else:
                frame.pict_type = av.video.frame.PictureType.NONE

            for packet in self._hw_codec.encode(frame):
                raw_pkt = bytes(packet)
                if not self._logged_first_hw_packet:
                    self._logged_first_hw_packet = True
                    active_log.info(
                        "[OctoCam HW] V4L2 M2M H.264 hardware encoder ACTIVE: first packet produced (%d bytes).",
                        len(raw_pkt),
                    )
                yield from self._split_bitstream(raw_pkt)

        def _launch_vaapi_ffmpeg(self, width: int, height: int) -> None:
            self._cleanup_hw()
            self._vaapi_stderr = []
            ffmpeg_bin = shutil.which("ffmpeg")
            if not ffmpeg_bin:
                raise RuntimeError("ffmpeg binary is required for VAAPI hardware encoding")

            bitrate_val = int(self.target_bitrate or target_bitrate)
            vf_chain = build_ffmpeg_vf_filter(
                flip_h=vaapi_flip_h,
                flip_v=vaapi_flip_v,
                rotate90=vaapi_rotate90,
                include_vaapi_hwupload=True,
                use_vaapi_transpose=self._use_vaapi_transpose,
            )
            cmd = [
                ffmpeg_bin,
                "-hide_banner",
                "-loglevel",
                "warning",
                "-fflags",
                "nobuffer",
                "-flags",
                "low_delay",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "nv12",
                "-s",
                f"{width}x{height}",
                "-r",
                str(fps),
                "-i",
                "pipe:0",
                "-vaapi_device",
                device,
                "-vf",
                vf_chain,
                "-c:v",
                "h264_vaapi",
                "-profile:v",
                "578",  # Constrained Baseline
                "-bf",
                "0",
                "-g",
                str(fps),
                "-b:v",
                str(bitrate_val),
                "-f",
                "h264",
                "pipe:1",
            ]
            active_log.info(
                "[OctoCam HW] Launching FFmpeg VAAPI H.264 hardware encoder (use_vaapi_transpose=%s): %s",
                self._use_vaapi_transpose,
                " ".join(cmd),
            )
            self._vaapi_proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            self._hw_dims = (width, height)
            self._start_vaapi_reader(self._vaapi_proc)

        def _encode_vaapi(self, frame: Any, force_keyframe: bool):
            if not (os.path.exists(device) and os.access(device, os.R_OK | os.W_OK)):
                raise RuntimeError(f"VAAPI device {device} is not accessible")

            dims = (frame.width, frame.height)
            # Check if an existing ffmpeg VAAPI subprocess died unexpectedly
            if self._vaapi_proc is not None and self._vaapi_proc.poll() is not None:
                rc = self._vaapi_proc.returncode
                stderr_msg = " | ".join(self._vaapi_stderr) or "no stderr output"
                has_transforms = bool(vaapi_flip_h or vaapi_flip_v or vaapi_rotate90)
                if self._use_vaapi_transpose and has_transforms:
                    active_log.warning(
                        "[OctoCam HW] FFmpeg h264_vaapi with GPU transpose_vaapi exited (rc=%s, stderr='%s'); retrying h264_vaapi with pre-upload software flip/transpose.",
                        rc,
                        stderr_msg,
                    )
                    self._use_vaapi_transpose = False
                    self._cleanup_hw()
                else:
                    raise RuntimeError(
                        f"FFmpeg h264_vaapi subprocess exited with code {rc}: {stderr_msg}"
                    )

            just_launched = False
            if self._vaapi_proc is None or self._hw_dims != dims:
                self._launch_vaapi_ffmpeg(frame.width, frame.height)
                just_launched = True

            # Reuse a single persistent VideoReformatter so SwsContext is not recreated per frame
            frame = self._reformat_frame(frame, "nv12")
            raw_bytes = frame.to_ndarray().tobytes()

            try:
                assert self._vaapi_proc is not None and self._vaapi_proc.stdin is not None
                self._vaapi_proc.stdin.write(raw_bytes)
                self._vaapi_proc.stdin.flush()
            except BrokenPipeError as exc:
                rc = self._vaapi_proc.poll() if self._vaapi_proc else None
                stderr_msg = " | ".join(self._vaapi_stderr) or str(exc)
                has_transforms = bool(vaapi_flip_h or vaapi_flip_v or vaapi_rotate90)
                if self._use_vaapi_transpose and has_transforms:
                    active_log.warning(
                        "[OctoCam HW] FFmpeg h264_vaapi broken pipe with GPU transpose_vaapi (rc=%s, stderr='%s'); retrying h264_vaapi with pre-upload flip/transpose.",
                        rc,
                        stderr_msg,
                    )
                    self._use_vaapi_transpose = False
                    self._launch_vaapi_ffmpeg(frame.width, frame.height)
                    assert self._vaapi_proc is not None and self._vaapi_proc.stdin is not None
                    self._vaapi_proc.stdin.write(raw_bytes)
                    self._vaapi_proc.stdin.flush()
                    just_launched = True
                else:
                    raise RuntimeError(
                        f"FFmpeg h264_vaapi broken pipe (rc={rc}): {stderr_msg}"
                    ) from exc

            # Give ffmpeg slightly longer on the first frame to initialize VAAPI
            drain_timeout = 0.35 if just_launched else 0.05
            chunk = self._drain_vaapi_output(timeout=drain_timeout)

            if not chunk and self._vaapi_proc is not None and self._vaapi_proc.poll() is not None:
                rc = self._vaapi_proc.returncode
                stderr_msg = " | ".join(self._vaapi_stderr) or "no stderr output"
                has_transforms = bool(vaapi_flip_h or vaapi_flip_v or vaapi_rotate90)
                if self._use_vaapi_transpose and has_transforms:
                    active_log.warning(
                        "[OctoCam HW] FFmpeg h264_vaapi exited during initialization with transpose_vaapi (rc=%s, stderr='%s'); retrying with pre-upload flip/transpose.",
                        rc,
                        stderr_msg,
                    )
                    self._use_vaapi_transpose = False
                    self._launch_vaapi_ffmpeg(frame.width, frame.height)
                    assert self._vaapi_proc is not None and self._vaapi_proc.stdin is not None
                    self._vaapi_proc.stdin.write(raw_bytes)
                    self._vaapi_proc.stdin.flush()
                    chunk = self._drain_vaapi_output(timeout=0.35)
                    if not chunk and self._vaapi_proc.poll() is not None:
                        stderr_msg2 = " | ".join(self._vaapi_stderr) or "no stderr output"
                        raise RuntimeError(
                            f"FFmpeg h264_vaapi failed after retry (rc={self._vaapi_proc.returncode}): {stderr_msg2}"
                        )
                else:
                    raise RuntimeError(
                        f"FFmpeg h264_vaapi failed on startup (rc={rc}): {stderr_msg}"
                    )

            if chunk:
                nals = list(self._split_bitstream(chunk))
                self._hw_frames_encoded += 1
                self._hw_bytes_encoded += len(chunk)
                if not self._logged_first_hw_packet:
                    self._logged_first_hw_packet = True
                    active_log.info(
                        "[OctoCam HW] VAAPI H.264 hardware encoder ACTIVE on '%s': produced first H.264 bitstream chunk (%d bytes, %d NAL units, %dx%d, gpu_transpose=%s).",
                        device,
                        len(chunk),
                        len(nals),
                        frame.width,
                        frame.height,
                        self._use_vaapi_transpose,
                    )
                elif self._hw_frames_encoded % 300 == 0:
                    active_log.info(
                        "[OctoCam HW] VAAPI H.264 hardware encoder heartbeat: %d chunks encoded (%d bytes total).",
                        self._hw_frames_encoded,
                        self._hw_bytes_encoded,
                    )
                yield from nals

        def _start_vaapi_reader(self, proc: subprocess.Popen[bytes]) -> None:
            import queue as queue_mod

            vaapi_queue: queue_mod.Queue[bytes] = queue_mod.Queue(maxsize=32)
            self._vaapi_queue = vaapi_queue
            stderr_list = self._vaapi_stderr

            # Do not reference `self` inside the reader threads so `HardwareH264Encoder`
            # can be garbage-collected when its peer connection closes.
            def _reader(
                p: subprocess.Popen[bytes], q: queue_mod.Queue[bytes]
            ) -> None:
                stdout = p.stdout
                if stdout is None:
                    return
                try:
                    while p.poll() is None:
                        data = os.read(stdout.fileno(), 65536)
                        if not data:
                            break
                        if q.full():
                            try:
                                q.get_nowait()
                            except queue_mod.Empty:
                                pass
                        q.put_nowait(data)
                except Exception:
                    pass

            def _stderr_reader(
                p: subprocess.Popen[bytes], lines_out: List[str]
            ) -> None:
                stderr = p.stderr
                if stderr is None:
                    return
                try:
                    for raw_line in iter(stderr.readline, b""):
                        line_str = raw_line.decode("utf-8", errors="replace").strip()
                        if line_str:
                            lines_out.append(line_str)
                            if len(lines_out) > 25:
                                lines_out.pop(0)
                            active_log.warning(
                                "[OctoCam HW] [ffmpeg h264_vaapi stderr] %s", line_str
                            )
                except Exception:
                    pass

            threading.Thread(
                target=_reader,
                args=(proc, vaapi_queue),
                name="OctocamVAAPIReader",
                daemon=True,
            ).start()
            threading.Thread(
                target=_stderr_reader,
                args=(proc, stderr_list),
                name="OctocamVAAPIStderr",
                daemon=True,
            ).start()

        def _drain_vaapi_output(self, timeout: float = 0.05) -> bytes:
            import queue as queue_mod

            chunks: List[bytes] = []
            try:
                first = self._vaapi_queue.get(timeout=timeout)
                chunks.append(first)
                while True:
                    chunks.append(self._vaapi_queue.get_nowait())
            except queue_mod.Empty:
                pass
            return b"".join(chunks)

        def _cleanup_hw(self) -> None:
            self._hw_codec = None
            if getattr(self, "_vaapi_queue", None) is not None:
                while not self._vaapi_queue.empty():
                    try:
                        self._vaapi_queue.get_nowait()
                    except Exception:
                        break
            if self._vaapi_proc is not None:
                _terminate_subprocess(self._vaapi_proc)
                self._vaapi_proc = None

        def __del__(self) -> None:
            self._cleanup_hw()

    aiortc_h264_module.H264Encoder = HardwareH264Encoder
    aiortc_codecs_module.H264Encoder = HardwareH264Encoder


class WebRTCEngine:
    """
    Manages either the built-in `aiortc` + FFmpeg/VAAPI WebRTC server or proxies
    SDP offers/answers to a local WHEP server (`go2rtc`, `MediaMTX`, etc.).
    """

    def __init__(self, logger_instance: Optional[logging.Logger] = None) -> None:
        self._logger = logger_instance or logger
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._pcs: Set[Any] = set()
        self._player: Optional[Any] = None
        self._relay: Optional[Any] = None
        self._active_source_signature: Optional[str] = None
        self._latest_jpeg: Optional[bytes] = None

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
        """Closes all active peer connections, stops the camera player, and stops the loop."""
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
        Routes to either the WHEP proxy or the built-in `aiortc` + VAAPI engine.
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

        video_track = self._get_or_create_video_track(config)
        self._configure_encoder_hardware(config)

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

    def _configure_encoder_hardware(self, config: EngineConfig) -> None:
        """
        Configures `aiortc` H.264 target bitrate, prioritizes `video/H264` in `aiortc.codecs.CODECS`,
        and installs the hardware-aware `H264Encoder` (`h264_vaapi` with GPU `transpose_vaapi` /
        `h264_v4l2m2m` with fallback to `libx264`).
        """
        try:
            import aiortc.codecs as aiortc_codecs
            import aiortc.codecs.h264 as aiortc_h264

            # Reorder aiortc's global video codec list so video/H264 is prioritized over video/VP8
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
                    self._logger.info(
                        "[OctoCam HW] Prioritized video/H264 in aiortc.codecs.CODECS['video']: %s",
                        [getattr(c, "mimeType", "") for c in aiortc_codecs.CODECS["video"]],
                    )

            target_bps = parse_bitrate_bps(config.bitrate)
            aiortc_h264.DEFAULT_BITRATE = target_bps
            aiortc_h264.MAX_BITRATE = max(target_bps * 2, 5_000_000)

            install_hardware_h264_encoder(
                aiortc_codecs_module=aiortc_codecs,
                aiortc_h264_module=aiortc_h264,
                hwaccel_mode=config.hwaccel_mode,
                vaapi_device=config.vaapi_device,
                target_bitrate=target_bps,
                framerate=config.framerate,
                vaapi_flip_h=bool(getattr(self, "_rem_flip_h", config.flip_h)),
                vaapi_flip_v=bool(getattr(self, "_rem_flip_v", config.flip_v)),
                vaapi_rotate90=bool(getattr(self, "_rem_rotate90", config.rotate90)),
                logger_instance=self._logger,
            )
        except Exception as exc:
            self._logger.warning(
                "[OctoCam HW] Could not configure hardware H264 encoder: %s", exc
            )

    def _get_or_create_video_track(self, config: EngineConfig) -> Any:
        from aiortc.contrib.media import MediaPlayer, MediaRelay

        _silence_pyav_swscaler_warnings()

        signature = (
            f"{config.stream_source}|{config.input_format}|{config.resolution}|"
            f"{config.framerate}|{config.hwaccel_mode}|{config.vaapi_device}|"
            f"{int(config.flip_h)}|{int(config.flip_v)}|{int(config.rotate90)}"
        )

        if (
            getattr(self, "_source_track", None) is None
            or self._active_source_signature != signature
        ):
            self._close_player()

            # 1. Check if the source webcam supports in-camera hardware flipping/rotation
            cam_hw = configure_v4l2_camera_transforms(
                stream_source=config.stream_source,
                flip_h=config.flip_h,
                flip_v=config.flip_v,
                rotate90=config.rotate90,
            )
            rem_flip_h = bool(config.flip_h and not cam_hw["flip_h_applied"])
            rem_flip_v = bool(config.flip_v and not cam_hw["flip_v_applied"])
            rem_rotate90 = bool(config.rotate90 and not cam_hw["rotate90_applied"])

            # 2. Attempt FFmpeg VAAPI hardware decoding (`-hwaccel vaapi ... -vf scale_vaapi=format=nv12`)
            # when `hwaccel_mode == "vaapi"` is selected.
            vaapi_track, gpu_transpose_done = create_vaapi_hwdecode_track(
                config=config,
                flip_h=rem_flip_h,
                flip_v=rem_flip_v,
                rotate90=rem_rotate90,
                log=self._logger,
            )

            if vaapi_track is not None:
                self._vaapi_decode_track = vaapi_track
                if gpu_transpose_done:
                    self._rem_flip_h = False
                    self._rem_flip_v = False
                    self._rem_rotate90 = False
                else:
                    self._rem_flip_h = rem_flip_h
                    self._rem_flip_v = rem_flip_v
                    self._rem_rotate90 = rem_rotate90
                self._source_track = vaapi_track
                self._logger.info(
                    "[OctoCam HW] Using FFmpeg VAAPI hardware decode track (gpu_transpose_done=%s, remaining_encode_transforms=(%s,%s,%s)).",
                    gpu_transpose_done,
                    self._rem_flip_h,
                    self._rem_flip_v,
                    self._rem_rotate90,
                )
            else:
                self._rem_flip_h = rem_flip_h
                self._rem_flip_v = rem_flip_v
                self._rem_rotate90 = rem_rotate90

                player_kwargs = build_media_player_kwargs(config)
                self._logger.info(
                    "[OctoCam HW] Opening PyAV MediaPlayer for '%s' (format=%s, options=%s, camera_hw_transforms=%s, remaining_flip=(%s,%s,%s))",
                    player_kwargs["file"],
                    player_kwargs.get("format"),
                    player_kwargs.get("options"),
                    cam_hw,
                    rem_flip_h,
                    rem_flip_v,
                    rem_rotate90,
                )
                try:
                    self._player = MediaPlayer(**player_kwargs)
                except Exception as exc:
                    opts = dict(player_kwargs.get("options") or {})
                    if "hwaccel" in opts:
                        self._logger.warning(
                            "[OctoCam HW] Opening MediaPlayer with VAAPI options failed (%s); retrying MediaPlayer with software decode options.",
                            exc,
                        )
                        opts.pop("hwaccel", None)
                        opts.pop("hwaccel_device", None)
                        player_kwargs["options"] = opts
                        self._player = MediaPlayer(**player_kwargs)
                    else:
                        raise

                if self._player.video is None:
                    self._close_player()
                    raise RuntimeError(
                        f"Media source '{config.stream_source}' does not provide a video stream."
                    )

                hw_mode = (config.hwaccel_mode or "none").strip().lower()
                vaapi_dev = (config.vaapi_device or "/dev/dri/renderD128").strip()
                vaapi_gpu_transpose_active = bool(
                    hw_mode == "vaapi"
                    and os.path.exists(vaapi_dev)
                    and os.access(vaapi_dev, os.R_OK | os.W_OK)
                    and shutil.which("ffmpeg")
                )

                if vaapi_gpu_transpose_active:
                    self._logger.info(
                        "[OctoCam HW] VAAPI hardware encoding is enabled; delegating remaining transforms (flip_h=%s, flip_v=%s, rotate90=%s) to VAAPI encode pipeline.",
                        rem_flip_h,
                        rem_flip_v,
                        rem_rotate90,
                    )
                    self._source_track = self._player.video
                else:
                    self._logger.info(
                        "[OctoCam HW] Using CPU FFmpegFrameFilter for remaining transforms (flip_h=%s, flip_v=%s, rotate90=%s).",
                        rem_flip_h,
                        rem_flip_v,
                        rem_rotate90,
                    )
                    self._source_track = create_transformed_video_track(
                        source_track=self._player.video,
                        flip_h=rem_flip_h,
                        flip_v=rem_flip_v,
                        rotate90=rem_rotate90,
                    )

            self._relay = MediaRelay()
            self._active_source_signature = signature

        assert self._relay is not None
        assert getattr(self, "_source_track", None) is not None
        try:
            return self._relay.subscribe(self._source_track, buffered=False)
        except TypeError:
            return self._relay.subscribe(self._source_track)

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
            self._close_player()

    def _close_player(self) -> None:
        for enc in list(_ACTIVE_HW_ENCODERS):
            try:
                enc._cleanup_hw()
            except Exception:
                pass
        if getattr(self, "_vaapi_decode_track", None) is not None:
            try:
                self._vaapi_decode_track.stop()
            except Exception:
                pass
            self._vaapi_decode_track = None
        if getattr(self, "_source_track", None) is not None:
            try:
                self._source_track.stop()
            except Exception:
                pass
            self._source_track = None
        if self._player is not None:
            for attr in ("video", "audio"):
                track = getattr(self._player, attr, None)
                if track is not None:
                    try:
                        track.stop()
                    except Exception:
                        pass
            self._player = None
        self._relay = None
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
        self._close_player()

    def capture_snapshot(self, config: EngineConfig) -> bytes:
        """
        Captures a JPEG snapshot from `snapshot_url` (if configured) or grabs a single
        frame from `stream_source` via PyAV / Pillow, applying `flip_h`, `flip_v`, and
        `rotate90` directly to the image.
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

        # If a live WebRTC stream is already open on the camera, grab a frame from the active
        # MediaRelay so we don't collide with V4L2 exclusive device access (/dev/videoX EBUSY).
        if (
            self._loop is not None
            and self._relay is not None
            and getattr(self, "_source_track", None) is not None
        ):
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._capture_from_active_relay_async(), self._loop
                )
                return future.result(timeout=5.0)
            except Exception as exc:
                self._logger.debug(
                    "Could not capture snapshot from active WebRTC relay (%s); falling back to direct open.",
                    exc,
                )

        import av

        _silence_pyav_swscaler_warnings()

        cam_hw = configure_v4l2_camera_transforms(
            stream_source=config.stream_source,
            flip_h=config.flip_h,
            flip_v=config.flip_v,
            rotate90=config.rotate90,
        )
        rem_flip_h = bool(config.flip_h and not cam_hw["flip_h_applied"])
        rem_flip_v = bool(config.flip_v and not cam_hw["flip_v_applied"])
        rem_rotate90 = bool(config.rotate90 and not cam_hw["rotate90_applied"])

        player_kwargs = build_media_player_kwargs(config)
        container_opts = dict(player_kwargs.get("options") or {})
        container_opts.pop("hwaccel", None)
        container_opts.pop("hwaccel_device", None)

        container = av.open(
            player_kwargs["file"],
            format=player_kwargs.get("format"),
            options=container_opts,
            timeout=5.0,
        )
        try:
            ffmpeg_filter = FFmpegFrameFilter(
                flip_h=rem_flip_h,
                flip_v=rem_flip_v,
                rotate90=rem_rotate90,
            )
            for frame in container.decode(video=0):
                transformed_frame = ffmpeg_filter.transform(frame)
                pil_img = transformed_frame.to_image()
                buf = io.BytesIO()
                pil_img.save(buf, format="JPEG", quality=85)
                return buf.getvalue()
        finally:
            container.close()

        raise RuntimeError("No video frame could be decoded for snapshot.")

    async def _capture_from_active_relay_async(self) -> bytes:
        assert self._relay is not None
        assert getattr(self, "_source_track", None) is not None
        try:
            sub_track = self._relay.subscribe(self._source_track, buffered=False)
        except TypeError:
            sub_track = self._relay.subscribe(self._source_track)
        try:
            frame = await asyncio.wait_for(sub_track.recv(), timeout=4.0)
        finally:
            try:
                sub_track.stop()
            except Exception:
                pass
        # If transforms were delegated to the encoder (`_rem_flip_*`), apply them for the snapshot.
        if getattr(self, "_source_track", None) in (
            getattr(self._player, "video", None),
            getattr(self, "_vaapi_decode_track", None),
        ):
            ff_filter = FFmpegFrameFilter(
                flip_h=bool(getattr(self, "_rem_flip_h", False)),
                flip_v=bool(getattr(self, "_rem_flip_v", False)),
                rotate90=bool(getattr(self, "_rem_rotate90", False)),
            )
            frame = ff_filter.transform(frame)
        pil_img = frame.to_image()
        buf = io.BytesIO()
        pil_img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
