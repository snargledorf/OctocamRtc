# coding=utf-8
from __future__ import annotations

import io
import unittest
from unittest.mock import MagicMock, patch

from octoprint_octocam.webrtc_engine import (
    EngineConfig,
    WebRTCEngine,
    apply_pil_transforms,
    resolve_go2rtc_snapshot_url,
    resolve_go2rtc_streams_url,
    resolve_go2rtc_webrtc_url,
)


class TestWebRTCEngine(unittest.TestCase):
    def test_url_resolvers(self) -> None:
        # Base URL resolver
        webrtc_url = resolve_go2rtc_webrtc_url("http://127.0.0.1:1984", "mycam")
        self.assertEqual(webrtc_url, "http://127.0.0.1:1984/api/webrtc?src=mycam")

        # Full URL resolver preserves path
        direct_url = resolve_go2rtc_webrtc_url(
            "http://127.0.0.1:1984/api/webrtc?src=custom"
        )
        self.assertEqual(direct_url, "http://127.0.0.1:1984/api/webrtc?src=custom")

        # Snapshot URL resolvers
        snap1 = resolve_go2rtc_snapshot_url("http://127.0.0.1:1984", "mycam")
        self.assertEqual(snap1, "http://127.0.0.1:1984/api/frame.jpeg?src=mycam")

        snap2 = resolve_go2rtc_snapshot_url(
            "http://127.0.0.1:1984/api/webrtc?src=custom"
        )
        self.assertEqual(snap2, "http://127.0.0.1:1984/api/frame.jpeg?src=custom")

        # Streams API URL resolver
        streams_url = resolve_go2rtc_streams_url("http://127.0.0.1:1984")
        self.assertEqual(streams_url, "http://127.0.0.1:1984/api/streams")

        streams_url_from_api = resolve_go2rtc_streams_url("http://127.0.0.1:1984/api/webrtc?src=cam")
        self.assertEqual(streams_url_from_api, "http://127.0.0.1:1984/api/streams")

    def test_get_available_streams(self) -> None:
        engine = WebRTCEngine()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '{"stream2": {}, "stream1": {}}'
        mock_resp.json.return_value = {"stream2": {}, "stream1": {}}

        with patch("octoprint_octocam.webrtc_engine.requests.get", return_value=mock_resp):
            streams = engine.get_available_streams("http://127.0.0.1:1984")

        self.assertEqual(streams, ["stream1", "stream2"])

    def test_get_available_streams_failure_returns_empty_list(self) -> None:
        engine = WebRTCEngine()
        with patch("octoprint_octocam.webrtc_engine.requests.get", side_effect=RuntimeError("connection refused")):
            streams = engine.get_available_streams("http://127.0.0.1:1984")
        self.assertEqual(streams, [])

    def test_handle_offer_proxies_to_go2rtc_text_answer(self) -> None:
        engine = WebRTCEngine()
        config = EngineConfig(
            go2rtc_url="http://127.0.0.1:1984",
            go2rtc_stream="octocam",
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\n"
        mock_resp.headers = {"Content-Type": "application/sdp"}

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
        mock_post.assert_called_once_with(
            "http://127.0.0.1:1984/api/webrtc?src=octocam",
            data=b"v=0\r\no=client 0 0 IN IP4 127.0.0.1\r\n",
            headers={"Content-Type": "application/sdp", "Accept": "application/sdp, application/json"},
            timeout=10.0,
        )

    def test_handle_offer_with_stream_override_and_json_response(self) -> None:
        engine = WebRTCEngine()
        config = EngineConfig(
            go2rtc_url="http://127.0.0.1:1984",
            go2rtc_stream="default_stream",
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.text = '{"type": "answer", "sdp": "v=0...json_answer"}'
        mock_resp.json.return_value = {"type": "answer", "sdp": "v=0...json_answer"}

        with patch(
            "octoprint_octocam.webrtc_engine.requests.post", return_value=mock_resp
        ) as mock_post:
            answer = engine.handle_offer(
                sdp="v=0...offer",
                sdp_type="offer",
                config=config,
                stream_override="bed_cam",
            )

        self.assertEqual(answer["sdp"], "v=0...json_answer")
        self.assertEqual(answer["type"], "answer")
        mock_post.assert_called_once_with(
            "http://127.0.0.1:1984/api/webrtc?src=bed_cam",
            data=b"v=0...offer",
            headers={"Content-Type": "application/sdp", "Accept": "application/sdp, application/json"},
            timeout=10.0,
        )

    def test_capture_snapshot(self) -> None:
        engine = WebRTCEngine()
        config = EngineConfig(
            go2rtc_url="http://127.0.0.1:1984",
            go2rtc_stream="nozzle_cam",
        )

        fake_jpeg = b"\xff\xd8\xff\xe0fake_jpeg_bytes"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = fake_jpeg

        with patch("octoprint_octocam.webrtc_engine.requests.get", return_value=mock_resp) as mock_get:
            result = engine.capture_snapshot(config)

        self.assertEqual(result, fake_jpeg)
        mock_get.assert_called_once_with(
            "http://127.0.0.1:1984/api/frame.jpeg?src=nozzle_cam",
            timeout=5.0,
        )

    def test_apply_pil_transforms_calls_expected_transpose_operations(self) -> None:
        fake_img = MagicMock()
        fake_img.transpose.return_value = fake_img

        mock_pil = MagicMock()
        mock_pil.Transpose.FLIP_LEFT_RIGHT = "FLIP_LR"
        mock_pil.Transpose.FLIP_TOP_BOTTOM = "FLIP_TB"
        mock_pil.Transpose.ROTATE_90 = "ROT_90"

        with patch.dict("sys.modules", {"PIL": MagicMock(Image=mock_pil)}):
            out = apply_pil_transforms(
                fake_img, flip_h=True, flip_v=True, rotate90=True
            )

        self.assertIs(out, fake_img)
        self.assertEqual(fake_img.transpose.call_count, 3)


if __name__ == "__main__":
    unittest.main()
