# coding=utf-8
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from octoprint_octocam.webrtc_engine import (
    EngineConfig,
    WebRTCEngine,
    build_media_player_kwargs,
    parse_bitrate_bps,
    probe_vaapi_support,
)


class TestWebRTCEngine(unittest.TestCase):
    def test_parse_bitrate_bps(self) -> None:
        self.assertEqual(parse_bitrate_bps("2500k"), 2_500_000)
        self.assertEqual(parse_bitrate_bps("4M"), 4_000_000)
        self.assertEqual(parse_bitrate_bps("1500000"), 1_500_000)
        self.assertEqual(parse_bitrate_bps("invalid", default_bps=2_000_000), 2_000_000)

    def test_build_media_player_kwargs_with_vaapi(self) -> None:
        config = EngineConfig(
            stream_source="/dev/video0",
            input_format="auto",
            resolution="1920x1080",
            framerate=30,
            hwaccel_mode="vaapi",
            vaapi_device="/dev/dri/renderD128",
        )

        with patch(
            "octoprint_octocam.webrtc_engine.os.path.exists", return_value=True
        ), patch("octoprint_octocam.webrtc_engine.os.access", return_value=True):
            kwargs = build_media_player_kwargs(config)

        self.assertEqual(kwargs["file"], "/dev/video0")
        self.assertEqual(kwargs["format"], "v4l2")
        self.assertEqual(kwargs["options"]["video_size"], "1920x1080")
        self.assertEqual(kwargs["options"]["framerate"], "30")
        self.assertEqual(kwargs["options"]["hwaccel"], "vaapi")
        self.assertEqual(kwargs["options"]["hwaccel_device"], "/dev/dri/renderD128")

    def test_whep_proxy_mode(self) -> None:
        engine = WebRTCEngine()
        config = EngineConfig(
            backend_mode="whep",
            whep_url="http://127.0.0.1:8889/octocam/whep",
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.text = "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\n"

        with patch(
            "octoprint_octocam.webrtc_engine.requests.post", return_value=mock_resp
        ) as mock_post:
            answer = engine.handle_offer(
                sdp="v=0\r\no=client 0 0 IN IP4 127.0.0.1\r\n",
                sdp_type="offer",
                config=config,
            )

        self.assertEqual(answer["type"], "answer")
        self.assertIn("v=0", answer["sdp"])
        mock_post.assert_called_once()

    def test_probe_vaapi_support_when_device_missing(self) -> None:
        with patch("octoprint_octocam.webrtc_engine.os.path.exists", return_value=False):
            status = probe_vaapi_support("/dev/dri/renderD128")
        self.assertFalse(status["available"])
        self.assertFalse(status["device_exists"])
        self.assertIn("does not exist", status["message"])


if __name__ == "__main__":
    unittest.main()
