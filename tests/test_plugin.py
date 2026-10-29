# coding=utf-8
from __future__ import annotations

import unittest
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

from octoprint_octocam import OctocamPlugin


class _FakeSettings:
    def __init__(self, data: Dict[str, Any]) -> None:
        self._data = data

    def get(self, path: List[str]) -> Any:
        return self._data.get(path[0])

    def get_boolean(self, path: List[str]) -> bool:
        return bool(self._data.get(path[0], False))

    def get_int(self, path: List[str]) -> int:
        return int(self._data.get(path[0], 0))


class TestOctocamPlugin(unittest.TestCase):
    def test_plugin_defaults_and_restricted_paths(self) -> None:
        plugin = OctocamPlugin()
        defaults = plugin.get_settings_defaults()

        self.assertEqual(defaults["backend_mode"], "aiortc")
        self.assertEqual(defaults["hwaccel_mode"], "vaapi")
        self.assertEqual(defaults["vaapi_device"], "/dev/dri/renderD128")
        self.assertTrue(defaults["cloudflare_enabled"])

        restricted = plugin.get_settings_restricted_paths()
        self.assertIn(["cloudflare_api_token"], restricted["admin"])

    def test_plugin_api_get_ice_servers(self) -> None:
        plugin = OctocamPlugin()
        plugin._settings = _FakeSettings(plugin.get_settings_defaults())

        with patch.object(
            plugin,
            "_json_response",
            side_effect=lambda payload, status=200: (payload, status),
        ):
            payload, status = plugin.on_api_command("get_ice_servers", {})

        self.assertEqual(status, 200)
        self.assertIn("iceServers", payload)
        self.assertFalse(payload["forceTurnRelay"])

    def test_plugin_api_offer_routes_to_engine(self) -> None:
        plugin = OctocamPlugin()
        plugin._settings = _FakeSettings(plugin.get_settings_defaults())
        plugin._webrtc_engine.handle_offer = MagicMock(
            return_value={"sdp": "v=0...answer", "type": "answer"}
        )

        with patch.object(
            plugin,
            "_json_response",
            side_effect=lambda payload, status=200: (payload, status),
        ):
            payload, status = plugin.on_api_command(
                "offer", {"sdp": "v=0...offer", "type": "offer"}
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload["type"], "answer")
        plugin._webrtc_engine.handle_offer.assert_called_once()


if __name__ == "__main__":
    unittest.main()
