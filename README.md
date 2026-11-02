# OctoCamRTC (OctoPrint-OctocamRtc)

**OctoCamRTC** is an OctoPrint 1.9+ `WebcamProviderPlugin` that delivers ultra-low-latency **WebRTC video streaming** and instant snapshot capture powered by a [webrtcstreamgateway](https://github.com/snargledorf) and [go2rtc](https://github.com/AlexxIT/go2rtc) media server backend. It features a responsive **Zero-JS iFrame Embed player**, dynamic **Cloudflare Calls TURN** credential generation, automatic **multi-stream discovery**, and an in-viewer camera switcher.

---

## Architecture & Design

OctoCamRTC uses a decoupled architecture separating media streaming from OctoPrint plugin management:

- **WebRtcStreamGateway as the Media Engine & Embedded Player**: Video capture (V4L2, USB, RTSP, Raspberry Pi cameras), hardware transcoding (VAAPI, NVENC, QuickSync, V4L2 M2M), and WebRTC streaming are handled directly by the gateway. The gateway hosts a fully responsive player complete with poster fallback, live connection status badges, controls, error recovery, and auto-reconnection via `/embed`.
- **OctoCamRTC as the Orchestrator**: The plugin embeds the Zero-JS iFrame player, connects to the parent window `postMessage` API for playback control and stream switching, and provides snapshot endpoints and OctoPrint webcam registration.
- **Dynamic Cloudflare Calls TURN**: WebRTC ICE candidates and short-lived credentials are generated on demand via Cloudflare Calls, establishing reliable peer connections across symmetric NATs, firewalls, and Cloudflare Tunnels without opening UDP ports.

```text
  ┌─────────────────────────────────────────────────────────────┐
  │                 Web Browser / OctoPrint UI                  │
  │     (OctoCamRTC Viewmodel + Zero-JS iFrame Embed Player)    │
  └──────────────┬───────────────────────────────▲──────────────┘
                 │ 1. postMessage Commands       │
                 │    (play, pause, setStream)   │
                 ▼                               │ 3. Sub-second
  ┌───────────────────────────────────────────┐  │    WebRTC Stream
  │             OctoPrint Server              │  │    (via STUN / TURN)
  │  ┌─────────────────────────────────────┐  │  │
  │  │ OctoCamRTC Plugin                   │  │  │
  │  │  - Gateway Embed Configuration     ├──┼──┘
  │  │  - Multi-stream Discovery & Sync    │  │
  │  │  - Background Snapshot Cache Worker │  │
  │  └──────────────────┬──────────────────┘  │
  └─────────────────────┼─────────────────────┘
                        │
       ┌────────────────┴────────────────┐
       │ 2. Discover Streams  2. Snapshot│
       ▼                                 ▼
┌────────────────────────────────────────────────────────────────┐
│             WebRtcStreamGateway (e.g. port 5000/8080)          │
│  - Responsive iFrame Player (/embed)                           │
│  - WebRTC Streaming & Cloudflare TURN                          │
│  - Multi-stream Media Capture & Snapshots                      │
└───────────────────────────────┬────────────────────────────────┘
                                │
                                ▼
                    ┌───────────────────────┐
                    │     Camera Feeds      │
                    │ (USB, V4L2, RTSP, Pi) │
                    └───────────────────────┘
```

### Zero-JS iFrame Embed Player

The gateway hosts a fully responsive player complete with poster fallback, live connection status badges, controls, error recovery, and auto-reconnection:

```html
<iframe
  src="http://localhost:5000/embed?stream=octocamrtc"
  width="640"
  height="360"
  allow="autoplay; fullscreen"
  style="border: 0; border-radius: 8px; overflow: hidden;"
  loading="lazy">
</iframe>
```

#### Query Parameters

| Parameter | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `stream` | string | Configured default | Target stream name registered in go2rtc. |
| `autoplay` | boolean | `true` | Automatically starts playback on load. |
| `muted` | boolean | `true` | Mutes audio (required by modern browsers for unprompted autoplay). |
| `controls` | boolean | `true` | Displays player controls bar (play/pause, mute, snapshot, fullscreen). |
| `poster` | boolean | `true` | Shows live cached snapshot from `/api/snapshot` while connecting. |
| `fit` | `contain` \| `cover` | `contain` | CSS object-fit mode for video stream. |
| `reconnect` | boolean | `true` | Automatically attempts exponential backoff reconnection on disconnection. |
| `gateway` | string | Same-origin | Base URL of the gateway (if hosting the iframe page from a different domain). |

#### Parent Window `postMessage` API

The embedded iframe communicates bidirectionally with OctoPrint via standard `window.postMessage`:

```javascript
const iframe = document.querySelector('iframe');

// Send commands to player
iframe.contentWindow.postMessage({ action: 'play' }, '*');
iframe.contentWindow.postMessage({ action: 'pause' }, '*');
iframe.contentWindow.postMessage({ action: 'reconnect' }, '*');
iframe.contentWindow.postMessage({ action: 'setStream', stream: 'front_door' }, '*');
iframe.contentWindow.postMessage({ action: 'mute' }, '*');
iframe.contentWindow.postMessage({ action: 'unmute' }, '*');

// Listen to player events from iframe
window.addEventListener('message', (event) => {
  if (event.data?.source === 'webrtc-stream-gateway') {
    console.log('Player event:', event.data.event, event.data);
  }
});
```

---

## Key Features

- **Zero-JS iFrame Player Embed**: Direct integration with `webrtcstreamgateway`'s responsive player iframe, eliminating complex in-browser WebRTC negotiation code while providing poster fallback, controls, connection status badges, and exponential backoff recovery.
- **Lightweight Gateway Backend**: Pure Python plugin with minimal dependencies (`requests`), offloading capture, encoding, and streaming to the high-performance media server.
- **Dynamic Cloudflare Calls TURN**: Generates short-lived TURN credentials via the Cloudflare Calls API with in-memory TTL caching and automatic renewal.
- **Automatic Multi-Stream Discovery**: Queries the gateway (`GET /api/streams`) and registers every stream as an OctoPrint webcam (`octocamrtc` and `octocamrtc_<stream>`).
- **In-Viewer Stream Switcher**: Switch between any discovered camera stream directly from the OctoPrint webcam tab without reloading the page via `postMessage`.
- **Instant Snapshot Background Caching**: A dedicated background worker pre-fetches snapshots (`/api/snapshot`) so opening the webcam tab immediately shows a fresh preview frame while WebRTC negotiates.
- **Snapshot Endpoint for Timelapses & Integrations**: Exposes `/plugin/octocamrtc/snapshot?src=<stream>` for OctoPrint timelapses, Home Assistant, OctoApp, and Mobileraker.
- **MJPEG & Snapshot URL Synchronization**: Automatically syncs OctoPrint's global stream and snapshot URLs to the gateway's native endpoints (`/api/stream.mjpeg`, `/api/snapshot`).
- **Dynamic Cloudflare Calls TURN**: Generates short-lived TURN credentials via the Cloudflare Calls API (`/v1/turn/keys/{TURN_KEY_ID}/credentials/generate`) with in-memory TTL caching and automatic renewal.
- **Automatic Multi-Stream Discovery**: Queries go2rtc (`GET /api/streams`) and registers every stream as an OctoPrint webcam (`octocamrtc` and `octocamrtc_<stream>`).
- **In-Viewer Stream Switcher**: Switch between any discovered go2rtc camera stream directly from the OctoPrint webcam tab without reloading the page.
- **Instant Snapshot Background Caching**: A dedicated background worker pre-fetches snapshots (`/api/frame.jpeg`) so opening the webcam tab immediately shows a fresh preview frame while WebRTC negotiates.
- **Snapshot Endpoint for Timelapses & Integrations**: Exposes `/plugin/octocamrtc/snapshot?src=<stream>` for OctoPrint timelapses, Home Assistant, OctoApp, and Mobileraker.
- **MJPEG & Snapshot URL Synchronization**: Automatically syncs OctoPrint's global stream and snapshot URLs to go2rtc's native endpoints (`/api/stream.mjpeg`, `/api/frame.jpeg`).
- **Static STUN/TURN Fallback**: Configurable fallback ICE servers if Cloudflare is disabled or unreachable.
- **Force TURN Relay Mode**: Option to enforce `iceTransportPolicy: 'relay'` for troubleshooting strict firewall or corporate network environments.

---

## Prerequisites

1. **OctoPrint 1.9+**
2. **go2rtc**: A running `go2rtc` binary (or container) accessible to OctoPrint (defaults to `http://127.0.0.1:1984`).
   - Download the latest standalone release binary from [go2rtc releases](https://github.com/AlexxIT/go2rtc/releases).

### Sample `go2rtc.yaml` Configuration

Place a `go2rtc.yaml` configuration file in the same directory as your `go2rtc` executable or `/etc/go2rtc.yaml`:

```yaml
streams:
  # USB / V4L2 Webcams (Linux)
  octocamrtc:
    - exec:ffmpeg -f v4l2 -input_format mjpeg -video_size 1920x1080 -framerate 30 -i /dev/video0 -c:v copy -an -f rtsp {output}

  # Secondary camera (e.g. nozzle cam or bed cam)
  nozzle_cam:
    - exec:ffmpeg -f v4l2 -input_format mjpeg -video_size 1280x720 -framerate 30 -i /dev/video2 -c:v copy -an -f rtsp {output}

  # RTSP IP Camera
  enclosure_cam:
    - rtsp://admin:password@192.168.1.100:554/h264Preview_01_main

api:
  listen: "127.0.0.1:1984"

webrtc:
  listen: ":8555"
```

> **Tip:** Hardware encoding (e.g., Intel VAAPI via `h264_vaapi`, Raspberry Pi `h264_v4l2m2m`, or NVENC) can be configured directly inside your `go2rtc.yaml` ffmpeg commands without touching OctoPrint.

---

## Installation

Activate your OctoPrint virtual environment and install OctoCamRTC:

```bash
source ~/oprint/bin/activate
pip install .
```

Or install via OctoPrint's **Plugin Manager** using this repository URL:
```text
https://github.com/snargledorf/OctocamRtc/archive/refs/heads/main.zip
```

---

## Configuration

Navigate to **Settings** -> **OctoCamRTC (WebRTC)** in the OctoPrint web interface:

### 1. go2rtc Media Server
- **go2rtc Server URL**: The address of your go2rtc instance (default: `http://127.0.0.1:1984`).
- **Test Connection**: Validates connectivity and retrieves available streams.
- **Camera Stream**: Choose the primary camera stream discovered from go2rtc (or click **Discover Streams** to refresh).
- **Snapshot Background Caching**: Enable to pre-fetch snapshots periodically (default: 5 seconds) for instant poster display.
- **Sync OctoPrint Stream & Snapshot URLs**: Keeps OctoPrint's global webcam settings aligned with go2rtc's MJPEG and JPEG snapshot endpoints.

### 2. Cloudflare Calls TURN Setup (Recommended for Remote Access)
1. In the [Cloudflare Dashboard](https://dash.cloudflare.com/), navigate to **Calls** -> **TURN Keys**.
2. Click **Create TURN Key** and copy your **TURN Key ID** and **API Token**.
3. In OctoPrint's **OctoCamRTC** settings:
   - Check **Enable Dynamic Cloudflare TURN Credentials**.
   - Paste your **Cloudflare TURN Key ID** and **Cloudflare API Token**.
   - Set **Credential TTL** (default: 3600 seconds).
   - Click **Test Cloudflare TURN Credentials** to verify.
4. *(Optional)* Enable **Force TURN Relay** if your network blocks direct WebRTC peer connections.

### 3. Static STUN / TURN Fallback
If not using Cloudflare Calls, specify static STUN or TURN servers (e.g., `stun:stun.cloudflare.com:3478` or custom Coturn credentials).

---

## Snapshot & Integration Endpoints

OctoCamRTC provides endpoints compatible with OctoPrint timelapses, Home Assistant, and mobile apps:

| Purpose | Endpoint URL | Description |
| :--- | :--- | :--- |
| **Primary Snapshot** | `http://<octoprint-ip>/plugin/octocamrtc/snapshot` | Returns latest JPEG snapshot for default stream |
| **Named Stream Snapshot** | `http://<octoprint-ip>/plugin/octocamrtc/snapshot?src=<stream>` | Returns latest JPEG snapshot for a specific stream |
| **Live MJPEG Stream** | `http://<go2rtc-ip>:1984/api/stream.mjpeg?src=<stream>` | Standard MJPEG stream for legacy clients |
| **Direct go2rtc Snapshot** | `http://<go2rtc-ip>:1984/api/frame.jpeg?src=<stream>` | Direct live capture from go2rtc |

---

## Development & Testing

### Running Unit Tests

Run the test suite using Python's standard `unittest`:

```powershell
python -m unittest discover -s tests -v
```

### Building Distribution Package

To build the release `.zip` archive in `dist/`:

```powershell
python .agents/skills/build-package/scripts/build_dist.py
```

---

## License

AGPLv3 — see [LICENSE](file:///C:/Users/Ryan/source/repos/Octocam/setup.py) for details.
