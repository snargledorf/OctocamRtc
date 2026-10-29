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

    def test_build_engine_config_includes_flip_and_rotate(self) -> None:
        plugin = OctocamPlugin()
        settings_data = plugin.get_settings_defaults()
        settings_data["flip_h"] = True
        settings_data["flip_v"] = True
        settings_data["rotate90"] = True
        plugin._settings = _FakeSettings(settings_data)

        cfg = plugin._build_engine_config(include_turn=False)
        self.assertTrue(cfg.flip_h)
        self.assertTrue(cfg.flip_v)
        self.assertTrue(cfg.rotate90)

    def test_get_webcam_configurations_always_sets_string_snapshot_display(self) -> None:
        plugin = OctocamPlugin()
        plugin._settings = _FakeSettings(plugin.get_settings_defaults())

        configs = list(plugin.get_webcam_configurations())
        self.assertEqual(len(configs), 1)
        cam = configs[0]
        snapshot_display = (
            cam["snapshotDisplay"]
            if isinstance(cam, dict)
            else getattr(cam, "snapshotDisplay", None)
        )
        self.assertIsInstance(snapshot_display, str)
        self.assertTrue(len(snapshot_display) > 0)


if __name__ == "__main__":
    unittest.main()
