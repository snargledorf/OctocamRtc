# coding=utf-8
from __future__ import annotations

import logging
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger("octoprint.plugins.octocamrtc")


@dataclass
class EngineConfig:
    """Configuration snapshot for the WebRTC stream gateway media engine."""

    gateway_url: str = "http://127.0.0.1:8080"
    stream_name: str = "octocamrtc"
    stream_url: str = ""
    snapshot_url: str = ""
    ice_servers: List[Dict[str, Any]] = field(default_factory=list)
    go2rtc_url: Optional[str] = None
    go2rtc_stream: Optional[str] = None

    def __post_init__(self) -> None:
        if self.go2rtc_url is not None and self.gateway_url == "http://127.0.0.1:8080":
            self.gateway_url = self.go2rtc_url
        if self.go2rtc_url is None:
            self.go2rtc_url = self.gateway_url

        if self.go2rtc_stream is not None and self.stream_name == "octocamrtc":
            self.stream_name = self.go2rtc_stream
        if self.go2rtc_stream is None:
            self.go2rtc_stream = self.stream_name


def resolve_gateway_script_url(gateway_url: str) -> str:
    """
    Resolves the URL to the webrtc-stream.js web component script on WebRtcStreamGateway.
    Defaults to '{gateway_url}/webrtc-stream.js'.
    """
    raw = (gateway_url or "").strip().rstrip("/")
    if not raw:
        raw = "http://127.0.0.1:8080"
    if "/api/" in raw:
        raw = raw.split("/api/")[0]
    elif "/embed" in raw:
        raw = raw.split("/embed")[0]
    elif "/webrtc-stream.js" in raw:
        return raw
    return f"{raw}/webrtc-stream.js"


def resolve_gateway_embed_url(
    gateway_url: str,
    stream: str = "octocamrtc",
    autoplay: bool = True,
    muted: bool = True,
    controls: bool = True,
    poster: bool = True,
    fit: str = "contain",
    reconnect: bool = True,
    gateway: Optional[str] = None,
) -> str:
    """
    Resolves the Zero-JS iFrame Embed endpoint URL on WebRtcStreamGateway.
    Defaults to '{gateway_url}/embed?stream={stream}&autoplay=true&...'.
    """
    raw = (gateway_url or "").strip().rstrip("/")
    if not raw:
        raw = "http://127.0.0.1:8080"
    if "/embed" in raw:
        base = raw.split("/embed")[0]
    elif "/api/" in raw:
        base = raw.split("/api/")[0]
    else:
        base = raw

    params = [
        ("stream", (stream or "octocamrtc").strip()),
        ("autoplay", "true" if autoplay else "false"),
        ("muted", "true" if muted else "false"),
        ("controls", "true" if controls else "false"),
        ("poster", "true" if poster else "false"),
        ("fit", (fit or "contain").strip()),
        ("reconnect", "true" if reconnect else "false"),
    ]
    if gateway:
        params.append(("gateway", gateway.strip()))

    query_str = urllib.parse.urlencode(params)
    return f"{base}/embed?{query_str}"


def resolve_gateway_webrtc_url(gateway_url: str, stream: str = "octocamrtc") -> str:
    """
    Resolves the full WebRTC endpoint URL on WebRtcStreamGateway.
    Defaults to '{gateway_url}/api/webrtc?src={stream}'.
    """
    raw = (gateway_url or "").strip().rstrip("/")
    if not raw:
        raw = "http://127.0.0.1:8080"
    if "/api/webrtc" in raw or "/api/whep" in raw:
        return raw
    s = (stream or "octocamrtc").strip()
    return f"{raw}/api/webrtc?src={s}"


def resolve_gateway_snapshot_url(gateway_url: str, stream: str = "octocamrtc") -> str:
    """
    Resolves the snapshot endpoint URL on WebRtcStreamGateway.
    Defaults to '{gateway_url}/api/snapshot?src={stream}'.
    """
    raw = (gateway_url or "").strip().rstrip("/")
    if not raw:
        raw = "http://127.0.0.1:8080"
    if "/api/snapshot" in raw or "/api/frame.jpeg" in raw:
        return raw
    if "/api/" in raw:
        base = raw.split("/api/")[0]
        parsed = urllib.parse.urlparse(raw)
        params = urllib.parse.parse_qs(parsed.query)
        s = params.get("src", [stream or "octocamrtc"])[0]
        return f"{base}/api/snapshot?src={s}"
    s = (stream or "octocamrtc").strip()
    return f"{raw}/api/snapshot?src={s}"


def resolve_gateway_streams_url(gateway_url: str) -> str:
    """Resolves the streams discovery endpoint URL on WebRtcStreamGateway ('{gateway_url}/api/streams')."""
    raw = (gateway_url or "").strip().rstrip("/")
    if not raw:
        raw = "http://127.0.0.1:8080"
    if "/api/" in raw:
        raw = raw.split("/api/")[0]
    return f"{raw}/api/streams"


def resolve_gateway_ice_servers_url(gateway_url: str) -> str:
    """Resolves the ICE servers endpoint URL on WebRtcStreamGateway ('{gateway_url}/api/ice-servers')."""
    raw = (gateway_url or "").strip().rstrip("/")
    if not raw:
        raw = "http://127.0.0.1:8080"
    if "/api/" in raw:
        raw = raw.split("/api/")[0]
    return f"{raw}/api/ice-servers"


def resolve_gateway_health_url(gateway_url: str) -> str:
    """Resolves the health check endpoint URL on WebRtcStreamGateway ('{gateway_url}/health')."""
    raw = (gateway_url or "").strip().rstrip("/")
    if not raw:
        raw = "http://127.0.0.1:8080"
    if "/api/" in raw:
        raw = raw.split("/api/")[0]
    elif "/health" in raw:
        return raw
    return f"{raw}/health"


def resolve_go2rtc_webrtc_url(go2rtc_url: str, stream_name: str = "octocamrtc") -> str:
    """Compatibility wrapper delegating to resolve_gateway_webrtc_url."""
    raw = (go2rtc_url or "").strip().rstrip("/")
    if not raw:
        raw = "http://127.0.0.1:1984"
    if "/api/webrtc" in raw or "/api/whep" in raw:
        return raw
    stream = (stream_name or "octocamrtc").strip()
    return f"{raw}/api/webrtc?src={stream}"


def resolve_go2rtc_mjpeg_url(go2rtc_url: str, stream_name: str = "octocamrtc") -> str:
    """
    Resolves the live MJPEG stream URL for `stream_name`.
    Extracts base URL and stream name if a full WHEP or stream endpoint was supplied.
    """
    raw = (go2rtc_url or "").strip().rstrip("/")
    if not raw:
        raw = "http://127.0.0.1:1984"
    if "/api/" in raw:
        base = raw.split("/api/")[0]
        parsed = urllib.parse.urlparse(raw)
        params = urllib.parse.parse_qs(parsed.query)
        stream = params.get("src", [stream_name or "octocamrtc"])[0]
        return f"{base}/api/stream.mjpeg?src={stream}"
    stream = (stream_name or "octocamrtc").strip()
    return f"{raw}/api/stream.mjpeg?src={stream}"


def resolve_go2rtc_snapshot_url(go2rtc_url: str, stream_name: str = "octocamrtc") -> str:
    """
    Resolves the go2rtc live snapshot JPEG URL for `stream_name`.
    Extracts base URL and stream name if a full WHEP endpoint was supplied.
    """
    raw = (go2rtc_url or "").strip().rstrip("/")
    if not raw:
        raw = "http://127.0.0.1:1984"
    if "/api/" in raw:
        base = raw.split("/api/")[0]
        parsed = urllib.parse.urlparse(raw)
        params = urllib.parse.parse_qs(parsed.query)
        stream = params.get("src", [stream_name or "octocamrtc"])[0]
        return f"{base}/api/frame.jpeg?src={stream}"
    stream = (stream_name or "octocamrtc").strip()
    return f"{raw}/api/frame.jpeg?src={stream}"


def resolve_go2rtc_streams_url(go2rtc_url: str) -> str:
    """Resolves the go2rtc REST API endpoint URL for querying configured streams."""
    raw = (go2rtc_url or "").strip().rstrip("/")
    if not raw:
        raw = "http://127.0.0.1:1984"
    if "/api/" in raw:
        raw = raw.split("/api/")[0]
    return f"{raw}/api/streams"


class WebRTCEngine:
    """
    Proxies WebRTC SDP offers/answers to a local or remote go2rtc instance
    and handles snapshot captures and stream discovery.
    """

    def __init__(self, logger_instance: Optional[logging.Logger] = None) -> None:
        self._logger = logger_instance or logger
        self._snapshot_cache: Dict[str, Tuple[float, bytes]] = {}
        self._cache_lock = threading.Lock()

    def start(self) -> None:
        """No-op for go2rtc proxy mode."""
        pass

    def stop(self) -> None:
        """No-op for go2rtc proxy mode."""
        pass

    def get_available_streams(
        self, gateway_url: str, timeout: float = 3.0
    ) -> List[str]:
        """Queries WebRtcStreamGateway `/api/streams` and returns the sorted list of stream names."""
        api_url = resolve_gateway_streams_url(gateway_url)
        try:
            resp = requests.get(api_url, timeout=timeout)
            if resp.status_code >= 400:
                self._logger.warning(
                    "[OctoCamRTC] Querying streams from '%s' returned HTTP %d",
                    api_url,
                    resp.status_code,
                )
                return []
            data = resp.json() if resp.text else []
            if isinstance(data, list):
                return sorted([str(s) for s in data])
            if isinstance(data, dict):
                return sorted(list(data.keys()))
            return []
        except Exception as exc:
            self._logger.debug(
                "[OctoCamRTC] Failed to query streams from '%s': %s",
                api_url,
                exc,
            )
            return []

    def get_ice_servers(
        self, gateway_url: str, timeout: float = 3.0
    ) -> Dict[str, Any]:
        """Queries WebRtcStreamGateway `/api/ice-servers` and returns parsed iceServers list and forceTurnRelay."""
        api_url = resolve_gateway_ice_servers_url(gateway_url)
        try:
            resp = requests.get(api_url, timeout=timeout)
            if resp.status_code >= 400:
                self._logger.warning(
                    "[OctoCamRTC] Querying ICE servers from '%s' returned HTTP %d",
                    api_url,
                    resp.status_code,
                )
                return {"iceServers": [], "forceTurnRelay": False}
            data = resp.json() if resp.text else {}
            if isinstance(data, dict):
                ice_servers = data.get("iceServers", data.get("ice_servers", []))
                force_relay = bool(data.get("forceTurnRelay", data.get("force_turn_relay", False)))
                return {
                    "iceServers": ice_servers if isinstance(ice_servers, list) else [],
                    "forceTurnRelay": force_relay,
                }
            if isinstance(data, list):
                return {
                    "iceServers": data,
                    "forceTurnRelay": False,
                }
            return {"iceServers": [], "forceTurnRelay": False}
        except Exception as exc:
            self._logger.debug(
                "[OctoCamRTC] Failed to query ICE servers from '%s': %s",
                api_url,
                exc,
            )
            return {"iceServers": [], "forceTurnRelay": False}

    def handle_offer(
        self,
        sdp: str,
        sdp_type: str = "offer",
        config: Optional[Any] = None,
        stream_override: Optional[str] = None,
        gateway_url: Optional[str] = None,
    ) -> Dict[str, str]:
        """
        Processes a client WebRTC SDP offer by proxying it to WebRtcStreamGateway /api/webrtc
        and returning the SDP answer `{ "sdp": ..., "type": "answer" }`.
        """
        if isinstance(config, str) and gateway_url is None:
            gateway_url = config
            config = None

        base_url = (
            gateway_url
            or getattr(config, "gateway_url", None)
            or getattr(config, "go2rtc_url", None)
            or "http://127.0.0.1:8080"
        )
        stream = (
            stream_override
            or getattr(config, "stream_name", None)
            or getattr(config, "go2rtc_stream", None)
            or "octocamrtc"
        ).strip()
        webrtc_url = resolve_gateway_webrtc_url(base_url, stream)

        self._logger.info(
            "[OctoCamRTC] Proxying WebRTC SDP offer to gateway stream '%s' (%s)",
            stream,
            webrtc_url,
        )

        headers = {
            "Content-Type": "application/sdp",
            "Accept": "application/sdp, application/json",
        }
        try:
            response = requests.post(
                webrtc_url,
                data=sdp.encode("utf-8"),
                headers=headers,
                timeout=10.0,
            )
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Failed to contact gateway server at '{webrtc_url}': {exc}"
            ) from exc

        if response.status_code >= 400:
            raise RuntimeError(
                f"Gateway server returned HTTP {response.status_code}: {response.text[:250]}"
            )

        answer_sdp = response.text
        content_type = response.headers.get("Content-Type", "")
        if "application/json" in content_type or answer_sdp.strip().startswith("{"):
            try:
                data = response.json()
                if isinstance(data, dict):
                    if "sdp" in data:
                        answer_sdp = data["sdp"]
                    elif "answer" in data:
                        answer_sdp = data["answer"]
            except Exception:
                pass

        return {
            "sdp": answer_sdp,
            "type": "answer",
        }

    def capture_snapshot(
        self,
        config: Optional[Any] = None,
        stream_override: Optional[str] = None,
        gateway_url: Optional[str] = None,
    ) -> bytes:
        """
        Captures a JPEG snapshot from:
        1. `config.snapshot_url` (if explicitly configured), or
        2. WebRtcStreamGateway's snapshot API (`/api/snapshot?src=<stream>`).
        """
        if isinstance(config, str) and gateway_url is None:
            gateway_url = config
            config = None

        stream = (
            stream_override
            or getattr(config, "stream_name", None)
            or getattr(config, "go2rtc_stream", None)
            or "octocamrtc"
        ).strip()
        snapshot_url = (getattr(config, "snapshot_url", None) or "").strip()
        if not snapshot_url:
            base_url = (
                gateway_url
                or getattr(config, "gateway_url", None)
                or getattr(config, "go2rtc_url", None)
                or "http://127.0.0.1:8080"
            )
            snapshot_url = resolve_gateway_snapshot_url(base_url, stream)

        try:
            resp = requests.get(snapshot_url, timeout=5.0)
            resp.raise_for_status()
            content = resp.content
            with self._cache_lock:
                self._snapshot_cache[stream] = (time.time(), content)
            return content
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Failed to fetch snapshot from '{snapshot_url}': {exc}"
            ) from exc

    def check_health(
        self, gateway_url: str, timeout: float = 3.0
    ) -> Dict[str, Any]:
        """Queries WebRtcStreamGateway `/health` to verify service reachability."""
        health_url = resolve_gateway_health_url(gateway_url)
        try:
            resp = requests.get(health_url, timeout=timeout)
            is_ok = (200 <= resp.status_code < 300)
            status_text = resp.text.strip() if resp.text else ""
            msg = f"Gateway healthy (HTTP {resp.status_code})" if is_ok else f"Gateway returned HTTP {resp.status_code}"
            if status_text and len(status_text) < 100:
                msg = f"{msg}: {status_text}"
            return {
                "ok": is_ok,
                "status_code": resp.status_code,
                "message": msg,
            }
        except Exception as exc:
            self._logger.debug(
                "[OctoCamRTC] Gateway health check failed for '%s': %s",
                health_url,
                exc,
            )
            return {
                "ok": False,
                "status_code": 0,
                "message": f"Failed to contact gateway at {health_url}: {exc}",
            }

    def get_cached_snapshot(
        self, stream: str = "octocamrtc", max_age: Optional[float] = None
    ) -> Optional[bytes]:
        """
        Returns the most recently cached snapshot bytes for `stream`.
        If `max_age` (seconds) is given, returns None if the cached snapshot is older.
        """
        stream_key = (stream or "octocamrtc").strip()
        with self._cache_lock:
            cached = self._snapshot_cache.get(stream_key)
            if cached is None:
                return None
            ts, data = cached
            if max_age is not None and (time.time() - ts) > max_age:
                return None
            return data

    def update_snapshot_cache(
        self, config: Optional[Any] = None, stream: Optional[str] = None
    ) -> Optional[bytes]:
        """Fetches a fresh snapshot and updates the in-memory cache without raising exceptions."""
        try:
            return self.capture_snapshot(config, stream_override=stream)
        except Exception as exc:
            stream_name = (
                stream
                or getattr(config, "stream_name", None)
                or getattr(config, "go2rtc_stream", None)
                or "octocamrtc"
            )
            self._logger.debug(
                "[OctoCamRTC] Background snapshot update failed for '%s': %s",
                stream_name,
                exc,
            )
            return None

    def clear_snapshot_cache(self, stream: Optional[str] = None) -> None:
        """Clears the snapshot cache for a specific stream or all streams."""
        with self._cache_lock:
            if stream:
                self._snapshot_cache.pop(stream, None)
            else:
                self._snapshot_cache.clear()
