# OctoCam — Project Architecture & Development Guidelines

OctoCam (`OctoPrint-Octocam`) is an OctoPrint 1.9+ `WebcamProviderPlugin` providing low-latency WebRTC video streaming and snapshots powered by a `go2rtc` media server wrapper, with dynamic Cloudflare Calls TURN credential generation and multi-stream selection.

## Core Architecture & Key Files

- `octoprint_octocam/__init__.py`: `OctocamPlugin` implementation (`StartupPlugin`, `SettingsPlugin`, `AssetPlugin`, `TemplatePlugin`, `SimpleApiPlugin`, `WebcamProviderPlugin`, `ShutdownPlugin`). Registers all discovered go2rtc streams as OctoPrint webcams.
- `octoprint_octocam/webrtc_engine.py`: `WebRTCEngine` proxying WebRTC SDP offers/answers to `go2rtc` (`/api/webrtc`), snapshot queries (`/api/frame.jpeg`), and stream discovery (`/api/streams`).
- `octoprint_octocam/turn_provider.py`: `TurnCredentialProvider` for Cloudflare Calls TURN short-lived credential generation and caching.
- `octoprint_octocam/static/js/octocam.js` & `templates/`: KnockoutJS viewmodel and Jinja2 templates for the OctoPrint webcam player (with stream switcher) and settings UI (with stream discovery & connection testing).
- `tests/`: Unit test suites (`test_plugin.py`, `test_turn_provider.py`, `test_webrtc_engine.py`).

## Media Pipeline Guardrails

1. **go2rtc as Single Source of Truth**:
   - Media capture, hardware encoding, and WebRTC streaming are offloaded to `go2rtc`.
   - OctoCam acts as a lightweight HTTP/WHEP proxy and snapshot provider.
   - Do **not** reintroduce in-Python FFmpeg subprocess management, PyAV decoding, or raw frame piping.
2. **Multi-Stream Support**:
   - Streams configured in `go2rtc` are discovered via `GET /api/streams` and exposed to OctoPrint's webcam system as well as in-UI dropdowns.
   - Snapshot requests for stream-specific webcams (`octocam_<stream>`) query `/api/frame.jpeg?src=<stream>`.

## Testing & Building

- **Run unit tests**:
  ```powershell
  python -m unittest discover -s tests -v
  ```
- **Build distribution archive (`dist/OctoPrint-Octocam-<version>.zip`)**:
  Activate the `build-package` skill or run `python .agents/skills/build-package/scripts/build_dist.py`.
