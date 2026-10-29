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

        self.assertEqual(defaults["go2rtc_url"], "http://127.0.0.1:1984")
        self.assertEqual(defaults["go2rtc_stream"], "octocam")
        self.assertTrue(defaults["cloudflare_enabled"])

        restricted = plugin.get_settings_restricted_paths()
        self.assertIn(["cloudflare_api_token"], restricted["admin"])

    def test_plugin_api_get_available_streams(self) -> None:
        plugin = OctocamPlugin()
        plugin._settings = _FakeSettings(plugin.get_settings_defaults())
        plugin._webrtc_engine.get_available_streams = MagicMock(
            return_value=["bed_cam", "nozzle_cam"]
        )

        with patch.object(
            plugin,
            "_json_response",
            side_effect=lambda payload, status=200: (payload, status),
        ):
            payload, status = plugin.on_api_command("get_available_streams", {})

        self.assertEqual(status, 200)
        self.assertEqual(payload["streams"], ["bed_cam", "nozzle_cam"])
        self.assertEqual(payload["selected"], "octocam")

    def test_plugin_api_test_go2rtc(self) -> None:
        plugin = OctocamPlugin()
        plugin._settings = _FakeSettings(plugin.get_settings_defaults())
        plugin._webrtc_engine.get_available_streams = MagicMock(
            return_value=["octocam", "cam2"]
        )

        with patch.object(
            plugin,
            "_json_response",
            side_effect=lambda payload, status=200: (payload, status),
        ):
            payload, status = plugin.on_api_command("test_go2rtc", {})

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["stream_found"])
        self.assertIn("octocam", payload["streams"])

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

    def test_plugin_api_offer_routes_to_engine_with_stream(self) -> None:
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
                "offer", {"sdp": "v=0...offer", "type": "offer", "stream": "bed_cam"}
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload["type"], "answer")
        plugin._webrtc_engine.handle_offer.assert_called_once()
        call_kwargs = plugin._webrtc_engine.handle_offer.call_args[1]
        self.assertEqual(call_kwargs["stream_override"], "bed_cam")

    def test_build_engine_config(self) -> None:
        plugin = OctocamPlugin()
        settings_data = plugin.get_settings_defaults()
        settings_data["go2rtc_url"] = "http://192.168.1.50:1984"
        settings_data["go2rtc_stream"] = "custom_cam"
        plugin._settings = _FakeSettings(settings_data)

        cfg = plugin._build_engine_config(include_turn=False)
        self.assertEqual(cfg.go2rtc_url, "http://192.168.1.50:1984")
        self.assertEqual(cfg.go2rtc_stream, "custom_cam")

    def test_get_webcam_configurations_registers_all_go2rtc_streams(self) -> None:
        plugin = OctocamPlugin()
        settings_data = plugin.get_settings_defaults()
        settings_data["go2rtc_stream"] = "bed_cam"
        plugin._settings = _FakeSettings(settings_data)
        plugin._webrtc_engine.get_available_streams = MagicMock(
            return_value=["bed_cam", "nozzle_cam"]
        )

        configs = list(plugin.get_webcam_configurations())
        self.assertEqual(len(configs), 2)

        names = [
            c["name"] if isinstance(c, dict) else c.name for c in configs
        ]
        self.assertIn("octocam", names)  # Primary active stream
        self.assertIn("octocam_nozzle_cam", names)

    def test_take_webcam_snapshot_handles_stream_specific_webcams(self) -> None:
        plugin = OctocamPlugin()
        plugin._settings = _FakeSettings(plugin.get_settings_defaults())
        plugin._webrtc_engine.capture_snapshot = MagicMock(return_value=b"jpeg_bytes")

        fake_webcam = {"name": "octocam_nozzle_cam"}
        result = list(plugin.take_webcam_snapshot(fake_webcam))
        self.assertEqual(result, [b"jpeg_bytes"])
        plugin._webrtc_engine.capture_snapshot.assert_called_once()
        call_kwargs = plugin._webrtc_engine.capture_snapshot.call_args[1]
        self.assertEqual(call_kwargs["stream_override"], "nozzle_cam")


if __name__ == "__main__":
    unittest.main()
