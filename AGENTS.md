# OctoCamRTC — Project Architecture & Development Guidelines

OctoCamRTC (`OctoPrint-OctocamRtc`) is an OctoPrint 1.9+ `WebcamProviderPlugin` providing low-latency WebRTC video streaming and snapshots powered by a `WebRtcStreamGateway` / `go2rtc` media server wrapper, featuring a `<webrtc-stream>` Web Component player with poster fallback, live connection status badges, controls, error recovery, auto-reconnection, and multi-stream selection.

## Core Architecture & Key Files

- `octoprint_octocamrtc/__init__.py`: `OctocamRtcPlugin` implementation (`StartupPlugin`, `SettingsPlugin`, `AssetPlugin`, `TemplatePlugin`, `SimpleApiPlugin`, `WebcamProviderPlugin`, `ShutdownPlugin`). Registers all discovered gateway streams as OctoPrint webcams and exposes settings/API commands.
- `octoprint_octocamrtc/webrtc_engine.py`: `WebRTCEngine` managing embed URL resolution (`/embed`), script resolution (`/webrtc-stream.js`), SDP offers/answers proxying (`/api/webrtc`), snapshot queries (`/api/snapshot`), and stream discovery (`/api/streams`).
- `octoprint_octocamrtc/static/js/octocamrtc.js` & `templates/`: KnockoutJS viewmodel and Jinja2 templates embedding the responsive `<webrtc-stream>` Web Component, dynamically loading `/webrtc-stream.js`, listening to custom events (`webrtc:connected`, `webrtc:disconnected`, `webrtc:error`, `webrtc:statuschange`), invoking DOM methods (`play()`, `pause()`, `reconnect()`, `captureSnapshot()`), and managing stream switching.
- `tests/`: Unit test suites (`test_plugin.py`, `test_webrtc_engine.py`).

## Media Pipeline Guardrails

1. **WebRtcStreamGateway / go2rtc as Single Source of Truth**:
   - Media capture, hardware encoding, WebRTC streaming, and player controls are offloaded to `webrtcstreamgateway` / `go2rtc`.
   - The webcam view uses the `<webrtc-stream>` Web Component directly integrated into the DOM (Shadow DOM style isolation).
   - OctoCamRTC acts as a lightweight HTTP/WHEP proxy and snapshot provider.
   - Do **not** reintroduce in-Python FFmpeg subprocess management, PyAV decoding, or raw frame piping.
2. **Multi-Stream Support**:
   - Streams configured in `go2rtc` are discovered via `GET /api/streams` and exposed to OctoPrint's webcam system as well as in-UI dropdowns.
   - Snapshot requests for stream-specific webcams (`octocamrtc_<stream>`) query `/api/frame.jpeg?src=<stream>`.

## Testing & Building

- **Run unit tests**:
  ```powershell
  python -m unittest discover -s tests -v
  ```
- **Build distribution archive (`dist/OctoPrint-OctocamRtc-<version>.zip`)**:
  Activate the `build-package` skill or run `python .agents/skills/build-package/scripts/build_dist.py`.
