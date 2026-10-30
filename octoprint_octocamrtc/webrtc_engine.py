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
    """Configuration snapshot for the go2rtc WebRTC media engine."""

    go2rtc_url: str = "http://127.0.0.1:1984"
    go2rtc_stream: str = "octocamrtc"
    stream_url: str = ""
    snapshot_url: str = ""
    ice_servers: List[Dict[str, Any]] = field(default_factory=list)


def resolve_go2rtc_webrtc_url(go2rtc_url: str, stream_name: str = "octocamrtc") -> str:
    """
    Resolves the full go2rtc WebRTC / WHEP endpoint URL.
    Supports either a base URL (e.g. 'http://127.0.0.1:1984') or an already full
    endpoint URL (e.g. 'http://127.0.0.1:1984/api/webrtc?src=cam').
    """
    raw = (go2rtc_url or "").strip().rstrip("/")
    if not raw:
        raw = "http://127.0.0.1:1984"
    if "/api/webrtc" in raw or "/api/whep" in raw:
        return raw
    stream = (stream_name or "octocamrtc").strip()
    return f"{raw}/api/webrtc?src={stream}"


def resolve_go2rtc_mjpeg_url(go2rtc_url: str, stream_name: str = "octocamrtc") -> str:
    """
    Resolves the go2rtc live MJPEG stream URL for `stream_name`.
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
        self, go2rtc_url: str, timeout: float = 3.0
    ) -> List[str]:
        """Queries go2rtc `/api/streams` and returns the list of stream names."""
        api_url = resolve_go2rtc_streams_url(go2rtc_url)
        try:
            resp = requests.get(api_url, timeout=timeout)
            if resp.status_code >= 400:
                self._logger.warning(
                    "[OctoCamRTC go2rtc] Querying streams from '%s' returned HTTP %d",
                    api_url,
                    resp.status_code,
                )
                return []
            data = resp.json() if resp.text else {}
            if isinstance(data, dict):
                return sorted(list(data.keys()))
            return []
        except Exception as exc:
            self._logger.debug(
                "[OctoCamRTC go2rtc] Failed to query streams from '%s': %s",
                api_url,
                exc,
            )
            return []

    def handle_offer(
        self,
        sdp: str,
        sdp_type: str,
        config: EngineConfig,
        stream_override: Optional[str] = None,
    ) -> Dict[str, str]:
        """
        Processes a client WebRTC SDP offer by proxying it to go2rtc
        and returning the SDP answer `{ "sdp": ..., "type": "answer" }`.
        """
        stream = (stream_override or config.go2rtc_stream or "octocamrtc").strip()
        webrtc_url = resolve_go2rtc_webrtc_url(config.go2rtc_url, stream)

        self._logger.info(
            "[OctoCamRTC go2rtc] Proxying WebRTC SDP offer to go2rtc stream '%s' (%s)",
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
                f"Failed to contact go2rtc server at '{webrtc_url}': {exc}"
            ) from exc

        if response.status_code >= 400:
            raise RuntimeError(
                f"go2rtc server returned HTTP {response.status_code}: {response.text[:250]}"
            )

        answer_sdp = response.text
        content_type = response.headers.get("Content-Type", "")
        if "application/json" in content_type:
            try:
                data = response.json()
                if isinstance(data, dict) and "sdp" in data:
                    answer_sdp = data["sdp"]
            except Exception:
                pass

        return {
            "sdp": answer_sdp,
            "type": "answer",
        }

    def capture_snapshot(
        self, config: EngineConfig, stream_override: Optional[str] = None
    ) -> bytes:
        """
        Captures a JPEG snapshot from:
        1. `config.snapshot_url` (if explicitly configured), or
        2. go2rtc's live frame API (`/api/frame.jpeg?src=<stream>`).
        """
        stream = (stream_override or config.go2rtc_stream or "octocamrtc").strip()
        snapshot_url = (config.snapshot_url or "").strip()
        if not snapshot_url:
            snapshot_url = resolve_go2rtc_snapshot_url(config.go2rtc_url, stream)

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
        self, config: EngineConfig, stream: Optional[str] = None
    ) -> Optional[bytes]:
        """Fetches a fresh snapshot and updates the in-memory cache without raising exceptions."""
        try:
            return self.capture_snapshot(config, stream_override=stream)
        except Exception as exc:
            self._logger.debug(
                "[OctoCamRTC go2rtc] Background snapshot update failed for '%s': %s",
                stream or config.go2rtc_stream,
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
