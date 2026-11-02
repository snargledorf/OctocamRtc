# OctoCamRTC (OctoPrint-OctocamRtc)

**OctoCamRTC** is an OctoPrint 1.9+ `WebcamProviderPlugin` that delivers ultra-low-latency **WebRTC video streaming** and instant snapshot capture powered by a [webrtcstreamgateway](https://github.com/snargledorf) and [go2rtc](https://github.com/AlexxIT/go2rtc) media server backend. It features a responsive **`<webrtc-stream>` Web Component player**, dynamic **Cloudflare Calls TURN** credential generation, automatic **multi-stream discovery**, and an in-viewer camera switcher.

---

## Architecture & Design

OctoCamRTC uses a decoupled architecture separating media streaming from OctoPrint plugin management:

- **WebRtcStreamGateway as the Media Engine & Embedded Player**: Video capture (V4L2, USB, RTSP, Raspberry Pi cameras), hardware transcoding (VAAPI, NVENC, QuickSync, V4L2 M2M), and WebRTC streaming are handled directly by the gateway. The gateway exposes both a custom `<webrtc-stream>` web component (`/webrtc-stream.js`) and a standalone embed player (`/embed`).
- **OctoCamRTC as the Orchestrator**: The plugin integrates the `<webrtc-stream>` custom element directly into the DOM (with Shadow DOM style isolation), binds to its lifecycle methods (`play()`, `pause()`, `reconnect()`, `captureSnapshot()`) and custom events, and provides snapshot endpoints and OctoPrint webcam registration.
- **Dynamic Cloudflare Calls TURN**: WebRTC ICE candidates and short-lived credentials are generated on demand via Cloudflare Calls, establishing reliable peer connections across symmetric NATs, firewalls, and Cloudflare Tunnels without opening UDP ports.

```text
  ┌─────────────────────────────────────────────────────────────┐
  │                 Web Browser / OctoPrint UI                  │
  │     (OctoCamRTC Viewmodel + <webrtc-stream> Component)      │
  └──────────────┬───────────────────────────────▲──────────────┘
                 │ 1. DOM Methods & Events       │
                 │    (play(), pause(), events)  │
                 ▼                               │ 3. Sub-second
  ┌───────────────────────────────────────────┐  │    WebRTC Stream
  │             OctoPrint Server              │  │    (via STUN / TURN)
  │  ┌─────────────────────────────────────┐  │  │
  │  │ OctoCamRTC Plugin                   │  │  │
  │  │  - Web Component Script Loader     ├──┼──┘
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
│  - <webrtc-stream> Web Component (/webrtc-stream.js)           │
│  - Zero-JS iFrame Embed Player (/embed)                        │
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

### `<webrtc-stream>` Web Component

The `<webrtc-stream>` element integrates directly into the DOM with Shadow DOM style isolation and provides poster fallback, live connection status badges, controls, error recovery, and auto-reconnection:

```html
<!-- 1. Include script once -->
<script src="http://localhost:5000/webrtc-stream.js"></script>

<!-- 2. Drop custom element into your layout -->
<webrtc-stream
  gateway="http://localhost:5000"
  stream="octocamrtc"
  autoplay
  muted
  controls
  poster>
</webrtc-stream>
```

#### Custom Element Methods & Events

```javascript
const playerEl = document.querySelector('webrtc-stream');

// Methods
playerEl.play();
playerEl.pause();
playerEl.reconnect();
playerEl.captureSnapshot(); // Returns Promise<Blob>

// Events
playerEl.addEventListener('webrtc:connected', (e) => console.log('Stream connected:', e.detail.stream));
playerEl.addEventListener('webrtc:disconnected', () => console.log('Stream disconnected'));
playerEl.addEventListener('webrtc:error', (e) => console.error('Stream error:', e.detail));
playerEl.addEventListener('webrtc:statuschange', (e) => console.log('Status changed:', e.detail.status));
```

---

## Key Features

- **`<webrtc-stream>` Web Component Player**: Direct DOM integration using `webrtcstreamgateway`'s custom element, with full Shadow DOM style encapsulation, DOM methods (`play()`, `pause()`, `reconnect()`, `captureSnapshot()`), and custom WebRTC lifecycle events.
- **Lightweight Gateway Backend**: Pure Python plugin with minimal dependencies (`requests`), offloading capture, encoding, and streaming to the high-performance media server.
- **Dynamic Cloudflare Calls TURN**: Generates short-lived TURN credentials via the Cloudflare Calls API with in-memory TTL caching and automatic renewal.
- **Automatic Multi-Stream Discovery**: Queries the gateway (`GET /api/streams`) and registers every stream as an OctoPrint webcam (`octocamrtc` and `octocamrtc_<stream>`).
- **In-Viewer Stream Switcher**: Switch between any discovered camera stream directly from the OctoPrint webcam tab without reloading the page.
- **Instant Snapshot Background Caching**: A dedicated background worker pre-fetches snapshots (`/api/snapshot`) so opening the webcam tab immediately shows a fresh preview frame while WebRTC negotiates.
- **Snapshot Endpoint for Timelapses & Integrations**: Exposes `/plugin/octocamrtc/snapshot?src=<stream>` for OctoPrint timelapses, Home Assistant, OctoApp, and Mobileraker.
- **MJPEG & Snapshot URL Synchronization**: Automatically syncs OctoPrint's global stream and snapshot URLs to the gateway's native endpoints (`/api/stream.mjpeg`, `/api/snapshot`).
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
