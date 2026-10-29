# OctoCam — Project Architecture & Development Guidelines

OctoCam (`OctoPrint-Octocam`) is an OctoPrint 1.9+ `WebcamProviderPlugin` providing low-latency WebRTC video streaming, dynamic Cloudflare Calls TURN credential generation, and Linux hardware-accelerated H.264 encoding (`vaapi` and `v4l2m2m`).

## Core Architecture & Key Files

- `octoprint_octocam/__init__.py`: `OctocamPlugin` implementation (`StartupPlugin`, `SettingsPlugin`, `AssetPlugin`, `TemplatePlugin`, `SimpleApiPlugin`, `WebcamProviderPlugin`, `ShutdownPlugin`).
- `octoprint_octocam/webrtc_engine.py`: WebRTC signaling (`aiortc` or external `whep` proxy), single-process FFmpeg H.264 pipeline (`FFmpegH264Stream`), `PassthroughH264Encoder`, and snapshot capture.
- `octoprint_octocam/turn_provider.py`: `TurnCredentialProvider` for Cloudflare Calls TURN short-lived credential generation and caching.
- `octoprint_octocam/static/js/octocam.js` & `templates/`: KnockoutJS viewmodel and Jinja2 templates for the OctoPrint webcam player and settings UI.
- `tests/`: Unit test suites (`test_plugin.py`, `test_turn_provider.py`, `test_webrtc_engine.py`).

## Video Pipeline Guardrails

1. **Single-Process FFmpeg Pipeline**:
   - Let a single `ffmpeg` subprocess (`FFmpegH264Stream` / `build_ffmpeg_h264_cmd`) handle webcam capture (`v4l2`, `mjpeg`, `rtsp`), decoding, `-vf` scaling/orientation filters (`scale_vaapi`/`transpose_vaapi` or CPU `scale`/`hflip`/`vflip`/`transpose`), and H.264 encoding (`h264_vaapi`, `h264_v4l2m2m`, or `libx264`) outputting Annex-B H.264 (`-f h264 -flush_packets 1 pipe:1`).
   - Do **not** split decoding and encoding across separate `ffmpeg` subprocesses or pipe uncompressed raw frames (`nv12` / `yuv420p`) through Python.
   - Do **not** split orientation transforms across V4L2 `ioctl` controls and PyAV `av.filter.Graph`; keep stream transforms inside FFmpeg's `-vf` filtergraph and snapshot URL transforms in `apply_pil_transforms`.
2. **H.264 Passthrough & Snapshots**:
   - Pre-encoded `EncodedH264Frame` access units from `FFmpegH264Stream` pass directly into `aiortc` RTP packetization via `PassthroughH264Encoder` (`install_hardware_h264_encoder`).
   - `FFmpegH264Stream` caches the latest self-contained `SPS + PPS + IDR` keyframe so new WebRTC viewers render immediately and `capture_snapshot` can decode the cached keyframe without reopening `/dev/videoX`.

## Testing & Building

- **Run unit tests**:
  ```powershell
  python -m unittest discover -s tests -v
  ```
- **Build distribution archive (`dist/OctoPrint-Octocam-<version>.zip`)**:
  Activate the `build-package` skill or run `python .agents/skills/build-package/scripts/build_dist.py`.
