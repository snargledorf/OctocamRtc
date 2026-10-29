# coding=utf-8
from __future__ import annotations

import asyncio
import io
import logging
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

import requests

logger = logging.getLogger(__name__)


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

    return {
        "available": bool(device_accessible and (ffmpeg_vaapi_works or pyav_h264_vaapi)),
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


def install_hardware_h264_encoder(
    aiortc_codecs_module: Any,
    aiortc_h264_module: Any,
    hwaccel_mode: str,
    vaapi_device: str,
    target_bitrate: int,
    framerate: int = 30,
) -> None:
    """
    Hooks `aiortc.codecs.h264.H264Encoder` to support Linux hardware H.264 encoding
    (`vaapi` via `h264_vaapi` or `v4l2m2m` via `h264_v4l2m2m`) with automatic fallback
    to `libx264` software encoding.
    """
    import fractions

    import av

    if not hasattr(aiortc_h264_module, "_OriginalH264Encoder"):
        aiortc_h264_module._OriginalH264Encoder = aiortc_h264_module.H264Encoder

    base_encoder_cls = aiortc_h264_module._OriginalH264Encoder
    mode = (hwaccel_mode or "none").strip().lower()
    device = (vaapi_device or "/dev/dri/renderD128").strip()
    fps = max(1, int(framerate or 30))

    if mode not in ("vaapi", "v4l2m2m"):
        aiortc_h264_module.H264Encoder = base_encoder_cls
        aiortc_codecs_module.H264Encoder = base_encoder_cls
        return

    class HardwareH264Encoder(base_encoder_cls):  # type: ignore[misc, valid-type]
        def __init__(self) -> None:
            super().__init__()
            self._hw_failed = False
            self._hw_codec: Optional[Any] = None
            self._vaapi_proc: Optional[subprocess.Popen[bytes]] = None
            self._hw_dims: Optional[tuple[int, int]] = None

        def _encode_frame(self, frame: Any, force_keyframe: bool):
            if self._hw_failed:
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
                logger.warning(
                    "Hardware H.264 encoder (%s) failed (%s); falling back to libx264 software encoding.",
                    mode,
                    exc,
                )
                self._cleanup_hw()
                self._hw_failed = True

            yield from super()._encode_frame(frame, force_keyframe)

        def _encode_v4l2m2m(self, frame: Any, force_keyframe: bool):
            dims = (frame.width, frame.height)
            if self._hw_codec is None or self._hw_dims != dims:
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

            if frame.format.name != "yuv420p":
                frame = frame.reformat(format="yuv420p")
            if force_keyframe:
                frame.pict_type = av.video.frame.PictureType.I
            else:
                frame.pict_type = av.video.frame.PictureType.NONE

            for packet in self._hw_codec.encode(frame):
                yield from self._split_bitstream(bytes(packet))

        def _encode_vaapi(self, frame: Any, force_keyframe: bool):
            if not (os.path.exists(device) and os.access(device, os.R_OK | os.W_OK)):
                raise RuntimeError(f"VAAPI device {device} is not accessible")

            ffmpeg_bin = shutil.which("ffmpeg")
            if not ffmpeg_bin:
                raise RuntimeError("ffmpeg binary is required for VAAPI hardware encoding")

            dims = (frame.width, frame.height)
            if (
                self._vaapi_proc is None
                or self._vaapi_proc.poll() is not None
                or self._hw_dims != dims
            ):
                self._cleanup_hw()
                bitrate_val = int(self.target_bitrate or target_bitrate)
                cmd = [
                    ffmpeg_bin,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-fflags",
                    "nobuffer",
                    "-flags",
                    "low_delay",
                    "-f",
                    "rawvideo",
                    "-pix_fmt",
                    "yuv420p",
                    "-s",
                    f"{frame.width}x{frame.height}",
                    "-r",
                    str(fps),
                    "-i",
                    "pipe:0",
                    "-vaapi_device",
                    device,
                    "-vf",
                    "format=nv12,hwupload",
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
                self._vaapi_proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                )
                self._hw_dims = dims
                self._start_vaapi_reader(self._vaapi_proc)

            if frame.format.name != "yuv420p":
                frame = frame.reformat(format="yuv420p")

            raw_bytes = frame.to_ndarray().tobytes()
            assert self._vaapi_proc.stdin is not None
            self._vaapi_proc.stdin.write(raw_bytes)
            self._vaapi_proc.stdin.flush()

            chunk = self._drain_vaapi_output(timeout=0.05)
            if chunk:
                yield from self._split_bitstream(chunk)

        def _start_vaapi_reader(self, proc: subprocess.Popen[bytes]) -> None:
            import queue as queue_mod

            self._vaapi_queue: queue_mod.Queue[bytes] = queue_mod.Queue()

            def _reader() -> None:
                stdout = proc.stdout
                if stdout is None:
                    return
                try:
                    while proc.poll() is None:
                        data = os.read(stdout.fileno(), 65536)
                        if not data:
                            break
                        self._vaapi_queue.put(data)
                except Exception:
                    pass

            reader_thread = threading.Thread(
                target=_reader,
                name="OctocamVAAPIReader",
                daemon=True,
            )
            reader_thread.start()

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
            if self._vaapi_proc is not None:
                try:
                    self._vaapi_proc.kill()
                except Exception:
                    pass
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

        self._configure_encoder_hardware(config)

        rtc_ice_servers = build_rtc_ice_servers(config.ice_servers)
        rtc_config = RTCConfiguration(iceServers=rtc_ice_servers)
        pc = RTCPeerConnection(configuration=rtc_config)
        self._pcs.add(pc)

        @pc.on("connectionstatechange")
        async def on_connectionstatechange() -> None:
            self._logger.info("WebRTC peer connection state: %s", pc.connectionState)
            if pc.connectionState in ("failed", "closed", "disconnected"):
                await self._cleanup_pc(pc)

        video_track = self._get_or_create_video_track(config)
        pc.addTrack(video_track)

        offer = RTCSessionDescription(sdp=sdp, type=sdp_type)
        await pc.setRemoteDescription(offer)

        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        return {
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type,
        }

    def _configure_encoder_hardware(self, config: EngineConfig) -> None:
        """
        Configures `aiortc` H.264 target bitrate and installs the hardware-aware
        `H264Encoder` (`h264_vaapi` / `h264_v4l2m2m` with fallback to `libx264`).
        """
        try:
            import aiortc.codecs as aiortc_codecs
            import aiortc.codecs.h264 as aiortc_h264

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
            )
        except Exception as exc:
            self._logger.debug("Could not configure hardware H264 encoder: %s", exc)

    def _get_or_create_video_track(self, config: EngineConfig) -> Any:
        from aiortc.contrib.media import MediaPlayer, MediaRelay

        signature = (
            f"{config.stream_source}|{config.input_format}|{config.resolution}|"
            f"{config.framerate}|{config.hwaccel_mode}|{config.vaapi_device}"
        )

        if self._player is None or self._active_source_signature != signature:
            self._close_player()
            player_kwargs = build_media_player_kwargs(config)
            self._logger.info(
                "Opening camera media player for '%s' (options=%s)",
                player_kwargs["file"],
                player_kwargs.get("options"),
            )
            try:
                self._player = MediaPlayer(**player_kwargs)
            except Exception as exc:
                # If VAAPI hwaccel decode failed on open, retry without hwaccel
                opts = dict(player_kwargs.get("options") or {})
                if "hwaccel" in opts:
                    self._logger.warning(
                        "Opening stream with VAAPI hwaccel failed (%s); retrying with software decode.",
                        exc,
                    )
                    opts.pop("hwaccel", None)
                    opts.pop("hwaccel_device", None)
                    player_kwargs["options"] = opts
                    self._player = MediaPlayer(**player_kwargs)
                else:
                    raise

            self._relay = MediaRelay()
            self._active_source_signature = signature

        if self._player.video is None:
            raise RuntimeError(
                f"Media source '{config.stream_source}' does not provide a video stream."
            )

        assert self._relay is not None
        return self._relay.subscribe(self._player.video)

    async def _cleanup_pc(self, pc: Any) -> None:
        if pc in self._pcs:
            self._pcs.discard(pc)
        try:
            await pc.close()
        except Exception:
            pass
        if not self._pcs:
            self._close_player()

    def _close_player(self) -> None:
        if self._player is not None:
            try:
                if getattr(self._player, "video", None) is not None:
                    self._player.video.stop()
            except Exception:
                pass
            self._player = None
            self._relay = None
            self._active_source_signature = None

    async def _shutdown_async(self) -> None:
        coros = [pc.close() for pc in list(self._pcs)]
        self._pcs.clear()
        if coros:
            await asyncio.gather(*coros, return_exceptions=True)
        self._close_player()

    def capture_snapshot(self, config: EngineConfig) -> bytes:
        """
        Captures a JPEG snapshot from `snapshot_url` (if configured) or grabs a single
        frame from `stream_source` via PyAV / Pillow.
        """
        snapshot_url = (config.snapshot_url or "").strip()
        if snapshot_url:
            resp = requests.get(snapshot_url, timeout=5.0)
            resp.raise_for_status()
            return resp.content

        import av
        from PIL import Image

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
            for frame in container.decode(video=0):
                pil_img: Image.Image = frame.to_image()
                buf = io.BytesIO()
                pil_img.save(buf, format="JPEG", quality=85)
                return buf.getvalue()
        finally:
            container.close()

        raise RuntimeError("No video frame could be decoded for snapshot.")
