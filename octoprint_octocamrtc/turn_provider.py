# coding=utf-8
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

CLOUDFLARE_TURN_API_TEMPLATE = (
    "https://rtc.live.cloudflare.com/v1/turn/keys/{turn_key_id}/credentials/generate"
)
DEFAULT_STUN_SERVER = "stun:stun.cloudflare.com:3478"


class TurnCredentialError(Exception):
    """Raised when generating TURN credentials from Cloudflare fails."""


class TurnCredentialProvider:
    """Resolves WebRTC ICE servers from Cloudflare Calls TURN API and static config."""

    def __init__(self, logger: Optional[logging.Logger] = None) -> None:
        self._logger = logger or logging.getLogger(__name__)
        self._lock = threading.Lock()
        self._cached_cf_servers: List[Dict[str, Any]] = []
        self._cache_expires_at: float = 0.0
        self._cache_key: Tuple[str, str, int] = ("", "", 0)

    def invalidate_cache(self) -> None:
        """Clears cached Cloudflare TURN credentials."""
        with self._lock:
            self._cached_cf_servers = []
            self._cache_expires_at = 0.0
            self._cache_key = ("", "", 0)

    def get_ice_servers(
        self,
        cloudflare_enabled: bool = False,
        cloudflare_turn_key_id: str = "",
        cloudflare_api_token: str = "",
        cloudflare_ttl: int = 3600,
        static_ice_servers: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Builds a combined list of ICE servers (`iceServers` format for WebRTC).
        Merges dynamic Cloudflare TURN servers (if enabled and configured) with
        user-specified static STUN/TURN servers.
        """
        merged: List[Dict[str, Any]] = []

        # 1. Resolve Cloudflare dynamic TURN credentials if enabled
        key_id = (cloudflare_turn_key_id or "").strip()
        api_token = (cloudflare_api_token or "").strip()
        ttl = max(60, min(int(cloudflare_ttl or 3600), 172800))

        if cloudflare_enabled and key_id and api_token:
            try:
                cf_servers = self._get_or_fetch_cloudflare_servers(
                    turn_key_id=key_id,
                    api_token=api_token,
                    ttl=ttl,
                )
                merged.extend(cf_servers)
            except TurnCredentialError as exc:
                self._logger.warning(
                    "Failed to generate Cloudflare TURN credentials, falling back to static ICE servers: %s",
                    exc,
                )

        # 2. Append static STUN/TURN servers
        normalized_static = self._normalize_static_servers(static_ice_servers or [])
        for server in normalized_static:
            if not self._is_duplicate_url(merged, server["urls"]):
                merged.append(server)

        # 3. Guarantee at least one STUN server if nothing was configured
        if not merged:
            merged.append({"urls": [DEFAULT_STUN_SERVER]})

        return merged

    def test_cloudflare_credentials(
        self,
        turn_key_id: str,
        api_token: str,
        ttl: int = 3600,
    ) -> List[Dict[str, Any]]:
        """
        Directly requests fresh credentials from Cloudflare Calls TURN API without
        using the cache. Raises TurnCredentialError if invalid.
        """
        key_id = (turn_key_id or "").strip()
        token = (api_token or "").strip()
        if not key_id or not token:
            raise TurnCredentialError(
                "Both Cloudflare TURN Key ID and API Token are required."
            )

        servers = self._fetch_cloudflare_turn_credentials(
            turn_key_id=key_id,
            api_token=token,
            ttl=max(60, min(int(ttl or 3600), 172800)),
        )
        with self._lock:
            self._cached_cf_servers = servers
            self._cache_key = (key_id, token, ttl)
            self._cache_expires_at = time.monotonic() + (ttl * 0.8)
        return servers

    def _get_or_fetch_cloudflare_servers(
        self,
        turn_key_id: str,
        api_token: str,
        ttl: int,
    ) -> List[Dict[str, Any]]:
        now = time.monotonic()
        requested_key = (turn_key_id, api_token, ttl)

        with self._lock:
            if (
                self._cached_cf_servers
                and self._cache_key == requested_key
                and now < self._cache_expires_at
            ):
                return [dict(item) for item in self._cached_cf_servers]

        servers = self._fetch_cloudflare_turn_credentials(
            turn_key_id=turn_key_id,
            api_token=api_token,
            ttl=ttl,
        )

        with self._lock:
            self._cached_cf_servers = servers
            self._cache_key = requested_key
            # Refresh after 80% of TTL has elapsed
            self._cache_expires_at = time.monotonic() + (ttl * 0.8)

        return [dict(item) for item in servers]

    def _fetch_cloudflare_turn_credentials(
        self,
        turn_key_id: str,
        api_token: str,
        ttl: int,
    ) -> List[Dict[str, Any]]:
        url = CLOUDFLARE_TURN_API_TEMPLATE.format(turn_key_id=turn_key_id)
        headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        }
        payload = {"ttl": ttl}

        try:
            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=10.0,
            )
        except requests.RequestException as exc:
            raise TurnCredentialError(
                f"Network error contacting Cloudflare TURN API: {exc}"
            ) from exc

        if response.status_code >= 400:
            detail = response.text[:300]
            raise TurnCredentialError(
                f"Cloudflare TURN API returned HTTP {response.status_code}: {detail}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise TurnCredentialError(
                "Cloudflare TURN API returned invalid JSON."
            ) from exc

        ice_servers_raw = data.get("iceServers")
        if not ice_servers_raw:
            raise TurnCredentialError(
                "Cloudflare TURN API response did not contain 'iceServers'."
            )

        # Cloudflare returns either a single dict `{"iceServers": {"urls": [...], ...}}`
        # or a list `{"iceServers": [{"urls": [...], ...}]}`
        if isinstance(ice_servers_raw, dict):
            ice_servers_list = [ice_servers_raw]
        elif isinstance(ice_servers_raw, list):
            ice_servers_list = ice_servers_raw
        else:
            raise TurnCredentialError(
                "Unexpected 'iceServers' format in Cloudflare response."
            )

        normalized = self._normalize_static_servers(ice_servers_list)
        if not normalized:
            raise TurnCredentialError(
                "No valid ICE server URLs found in Cloudflare TURN response."
            )

        return normalized

    @staticmethod
    def _normalize_static_servers(
        raw_servers: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        for entry in raw_servers:
            if not isinstance(entry, dict):
                continue

            raw_urls = entry.get("urls") or entry.get("url")
            if not raw_urls:
                continue

            if isinstance(raw_urls, str):
                urls = [
                    u.strip()
                    for u in raw_urls.split(",")
                    if u.strip()
                ]
            elif isinstance(raw_urls, list):
                urls = [
                    str(u).strip()
                    for u in raw_urls
                    if str(u).strip()
                ]
            else:
                continue

            if not urls:
                continue

            server_dict: Dict[str, Any] = {"urls": urls}
            username = (entry.get("username") or "").strip()
            credential = (entry.get("credential") or "").strip()

            if username:
                server_dict["username"] = username
            if credential:
                server_dict["credential"] = credential

            normalized.append(server_dict)

        return normalized

    @staticmethod
    def _is_duplicate_url(
        existing: List[Dict[str, Any]], candidate_urls: List[str]
    ) -> bool:
        existing_set = set()
        for item in existing:
            urls = item.get("urls", [])
            if isinstance(urls, str):
                existing_set.add(urls)
            elif isinstance(urls, list):
                existing_set.update(urls)
        return all(u in existing_set for u in candidate_urls)
