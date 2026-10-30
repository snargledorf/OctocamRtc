# OctoPrint-OctocamRtc

**OctoCamRTC** is an OctoPrint 1.9+ `WebcamProviderPlugin` that brings low-latency **WebRTC video streaming** to OctoPrint with configurable **STUN/TURN servers** (including native **Cloudflare Calls TURN** short-lived credential generation), **Linux VAAPI hardware acceleration** (`h264_vaapi`), and optional **WHEP** media server proxying.

## Key Features

- **Dynamic Cloudflare Calls TURN Support**: Automatically generates short-lived TURN credentials via the Cloudflare Calls API (`https://rtc.live.cloudflare.com/v1/turn/keys/{TURN_KEY_ID}/credentials/generate`) with in-memory TTL caching.
- **Both Server-Side & Client-Side TURN**: Supplies resolved STUN/TURN `iceServers` to both the browser viewer and the OctoPrint host's `aiortc` peer connection so WebRTC streams connect reliably across symmetric NATs, CGNAT, or Cloudflare Tunnels without UDP port forwarding.
- **Linux VAAPI Hardware Encoding & Decoding**: Supports `/dev/dri/renderD128` (`h264_vaapi`) and Raspberry Pi V4L2 M2M (`h264_v4l2m2m`), with automatic fallback to `libx264` ultrafast zero-latency software encoding.
- **WHEP Proxy Mode**: Optionally proxy SDP offers/answers to an external WHEP-compatible server such as `go2rtc` or `MediaMTX`.
- **Native OctoPrint 1.9+ Webcam Integration**: Integrates directly with OctoPrint's multi-webcam system and supports snapshots for timelapses and notifications.

---

## Installation

Activate your OctoPrint virtual environment and install the plugin:

```bash
source ~/oprint/bin/activate
pip install .
```

### Linux System Dependencies (Debian / Ubuntu / Distrobox)

For `aiortc` (PyAV) and Linux VAAPI hardware encoding:

```bash
sudo apt update
sudo apt install -y ffmpeg vainfo intel-media-va-driver-non-free mesa-va-drivers
```

Ensure the user running OctoPrint belongs to the `video` and `render` groups so it can access `/dev/video*` and `/dev/dri/renderD128`:

```bash
sudo usermod -aG video,render $USER
```

---

## Cloudflare Calls TURN Setup

1. Log in to the **Cloudflare Dashboard** and navigate to **Calls** -> **TURN Keys**.
2. Click **Create TURN Key** and copy both the **TURN Key ID** and **API Token**.
3. In OctoPrint, open **Settings** -> **OctoCamRTC (WebRTC)**:
   - Enable **Dynamic Cloudflare TURN Credentials**.
   - Paste your **Cloudflare TURN Key ID** and **Cloudflare TURN API Token**.
   - Click **Test Cloudflare TURN** to verify credential generation.
4. Select **OctoCamRTC WebRTC** as your active webcam under **Settings** -> **Classic Webcam / Webcams**.
