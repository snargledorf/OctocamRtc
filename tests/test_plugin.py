# coding=utf-8
from __future__ import annotations

import unittest
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

from octoprint_octocam import OctocamPlugin


class _FakeSettings:
    def __init__(
        self,
        data: Dict[str, Any],
        global_data: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._data = data
        self._global_data = global_data if global_data is not None else {}
        self.saved = False

    def get(self, path: List[str]) -> Any:
        return self._data.get(path[0])

    def get_boolean(self, path: List[str]) -> bool:
        return bool(self._data.get(path[0], False))

    def get_int(self, path: List[str]) -> int:
        return int(self._data.get(path[0], 0))

    def global_get(self, path: List[str]) -> Any:
        curr: Any = self._global_data
        for p in path:
            if not isinstance(curr, dict) or p not in curr:
                return None
            curr = curr[p]
        return curr

    def global_set(self, path: List[str], value: Any) -> None:
        curr: Any = self._global_data
        for p in path[:-1]:
            if p not in curr or not isinstance(curr[p], dict):
                curr[p] = {}
            curr = curr[p]
        curr[path[-1]] = value

    def save(self) -> None:
        self.saved = True


class TestOctocamPlugin(unittest.TestCase):
    def test_plugin_defaults_and_restricted_paths(self) -> None:
        plugin = OctocamPlugin()
        defaults = plugin.get_settings_defaults()

        self.assertEqual(defaults["go2rtc_url"], "http://127.0.0.1:1984")
        self.assertEqual(defaults["go2rtc_stream"], "octocam")
        self.assertEqual(defaults["stream_url"], "")
        self.assertTrue(defaults["sync_octoprint_stream"])
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
        self.assertEqual(payload["mjpeg_url"], "http://127.0.0.1:1984/api/stream.mjpeg?src=octocam")

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

        # Check MJPEG stream URLs on dict configs
        by_name = {c["name"]: c for c in configs}
        self.assertEqual(
            by_name["octocam"]["compat"]["stream"],
            "http://127.0.0.1:1984/api/stream.mjpeg?src=bed_cam",
        )
        self.assertEqual(
            by_name["octocam_nozzle_cam"]["compat"]["stream"],
            "http://127.0.0.1:1984/api/stream.mjpeg?src=nozzle_cam",
        )

    def test_sync_webcam_stream_url_updates_global_settings(self) -> None:
        plugin = OctocamPlugin()
        settings_data = plugin.get_settings_defaults()
        settings_data["go2rtc_url"] = "http://192.168.1.50:1984"
        settings_data["go2rtc_stream"] = "main_cam"
        plugin._settings = _FakeSettings(settings_data)

        plugin._sync_webcam_stream_url()

        self.assertEqual(
            plugin._settings.global_get(["webcam", "streamUrl"]),
            "http://192.168.1.50:1984/api/stream.mjpeg?src=main_cam",
        )
        self.assertEqual(
            plugin._settings.global_get(["plugins", "classicwebcam", "stream"]),
            "http://192.168.1.50:1984/api/stream.mjpeg?src=main_cam",
        )
        self.assertEqual(
            plugin._settings.global_get(["webcam", "snapshotUrl"]),
            "http://192.168.1.50:1984/api/frame.jpeg?src=main_cam",
        )
        self.assertTrue(plugin._settings.saved)

    def test_sync_webcam_stream_url_custom_override(self) -> None:
        plugin = OctocamPlugin()
        settings_data = plugin.get_settings_defaults()
        settings_data["stream_url"] = "http://10.0.0.5:1984/api/stream.mjpeg?src=octocam_mjpeg"
        plugin._settings = _FakeSettings(settings_data)

        plugin._sync_webcam_stream_url()

        self.assertEqual(
            plugin._settings.global_get(["webcam", "streamUrl"]),
            "http://10.0.0.5:1984/api/stream.mjpeg?src=octocam_mjpeg",
        )
        self.assertEqual(
            plugin._settings.global_get(["plugins", "classicwebcam", "stream"]),
            "http://10.0.0.5:1984/api/stream.mjpeg?src=octocam_mjpeg",
        )

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
