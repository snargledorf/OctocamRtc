# coding=utf-8
from __future__ import annotations

import unittest
from typing import Any, Dict
from unittest.mock import patch

from octoprint_octocamrtc.turn_provider import (
    DEFAULT_STUN_SERVER,
    TurnCredentialError,
    TurnCredentialProvider,
)


class _DummyResponse:
    def __init__(self, status_code: int, json_data: Dict[str, Any], text: str = "") -> None:
        self.status_code = status_code
        self._json_data = json_data
        self.text = text

    def json(self) -> Dict[str, Any]:
        return self._json_data


class TestTurnProvider(unittest.TestCase):
    def test_cloudflare_turn_generation_and_caching(self) -> None:
        provider = TurnCredentialProvider()
        mock_cf_payload = {
            "iceServers": {
                "urls": [
                    "stun:stun.cloudflare.com:3478",
                    "turn:turn.cloudflare.com:3478?transport=udp",
                    "turns:turn.cloudflare.com:5349?transport=tcp",
                ],
                "username": "ephemeral_user_123",
                "credential": "ephemeral_password_456",
            }
        }

        with patch("octoprint_octocamrtc.turn_provider.requests.post") as mock_post:
            mock_post.return_value = _DummyResponse(201, mock_cf_payload)

            servers_first = provider.get_ice_servers(
                cloudflare_enabled=True,
                cloudflare_turn_key_id="cf_key_id_1",
                cloudflare_api_token="cf_secret_token_1",
                cloudflare_ttl=3600,
                static_ice_servers=[
                    {
                        "urls": "turn:custom.example.com:3478",
                        "username": "alice",
                        "credential": "secret",
                    }
                ],
            )

            # Second call with same parameters should hit in-memory cache
            servers_second = provider.get_ice_servers(
                cloudflare_enabled=True,
                cloudflare_turn_key_id="cf_key_id_1",
                cloudflare_api_token="cf_secret_token_1",
                cloudflare_ttl=3600,
                static_ice_servers=[],
            )

            self.assertEqual(mock_post.call_count, 1)
            self.assertEqual(len(servers_first), 2)
            self.assertEqual(servers_first[0]["username"], "ephemeral_user_123")
            self.assertIn(
                "turns:turn.cloudflare.com:5349?transport=tcp",
                servers_first[0]["urls"],
            )
            self.assertEqual(servers_first[1]["urls"], ["turn:custom.example.com:3478"])
            self.assertEqual(len(servers_second), 1)

    def test_fallback_to_static_when_cloudflare_fails(self) -> None:
        provider = TurnCredentialProvider()

        with patch("octoprint_octocamrtc.turn_provider.requests.post") as mock_post:
            mock_post.return_value = _DummyResponse(401, {}, text="Unauthorized")

            servers = provider.get_ice_servers(
                cloudflare_enabled=True,
                cloudflare_turn_key_id="bad_key",
                cloudflare_api_token="bad_token",
                static_ice_servers=[{"urls": "stun:stun.l.google.com:19302"}],
            )

            self.assertEqual(servers, [{"urls": ["stun:stun.l.google.com:19302"]}])

    def test_default_stun_when_empty(self) -> None:
        provider = TurnCredentialProvider()
        servers = provider.get_ice_servers(
            cloudflare_enabled=False,
            static_ice_servers=[],
        )
        self.assertEqual(servers, [{"urls": [DEFAULT_STUN_SERVER]}])

    def test_test_cloudflare_credentials_raises_on_missing_fields(self) -> None:
        provider = TurnCredentialProvider()
        with self.assertRaises(TurnCredentialError):
            provider.test_cloudflare_credentials(turn_key_id="", api_token="")


if __name__ == "__main__":
    unittest.main()
