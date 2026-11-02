# coding=utf-8
from __future__ import annotations

import io
import unittest
from unittest.mock import MagicMock, patch

from octoprint_octocamrtc.webrtc_engine import (
    EngineConfig,
    WebRTCEngine,
    resolve_gateway_embed_url,
    resolve_gateway_health_url,
    resolve_gateway_ice_servers_url,
    resolve_gateway_snapshot_url,
    resolve_gateway_streams_url,
    resolve_gateway_webrtc_url,
    resolve_go2rtc_mjpeg_url,
    resolve_go2rtc_snapshot_url,
    resolve_go2rtc_streams_url,
    resolve_go2rtc_webrtc_url,
)


class TestWebRTCEngine(unittest.TestCase):
    def test_engine_config_defaults_and_aliases(self) -> None:
        cfg = EngineConfig()
        self.assertEqual(cfg.gateway_url, "http://127.0.0.1:8080")
        self.assertEqual(cfg.stream_name, "octocamrtc")
        self.assertEqual(cfg.go2rtc_url, "http://127.0.0.1:8080")
        self.assertEqual(cfg.go2rtc_stream, "octocamrtc")

        # Compatibility init with go2rtc args
        legacy_cfg = EngineConfig(go2rtc_url="http://10.0.0.5:1984", go2rtc_stream="custom_cam")
        self.assertEqual(legacy_cfg.gateway_url, "http://10.0.0.5:1984")
        self.assertEqual(legacy_cfg.stream_name, "custom_cam")

    def test_gateway_url_resolvers(self) -> None:
        # Gateway WebRTC URL resolver
        webrtc_url = resolve_gateway_webrtc_url("http://127.0.0.1:8080", "cam1")
        self.assertEqual(webrtc_url, "http://127.0.0.1:8080/api/webrtc?src=cam1")

        # Full URL preserves path
        direct_url = resolve_gateway_webrtc_url("http://127.0.0.1:8080/api/webrtc?src=direct")
        self.assertEqual(direct_url, "http://127.0.0.1:8080/api/webrtc?src=direct")

        # Snapshot URL resolver
        snap1 = resolve_gateway_snapshot_url("http://127.0.0.1:8080", "cam1")
        self.assertEqual(snap1, "http://127.0.0.1:8080/api/snapshot?src=cam1")

        # Streams discovery URL resolver
        streams_url = resolve_gateway_streams_url("http://127.0.0.1:8080")
        self.assertEqual(streams_url, "http://127.0.0.1:8080/api/streams")

        # ICE servers URL resolver
        ice_url = resolve_gateway_ice_servers_url("http://127.0.0.1:8080")
        self.assertEqual(ice_url, "http://127.0.0.1:8080/api/ice-servers")

        # Health check URL resolver
        health_url = resolve_gateway_health_url("http://127.0.0.1:8080")
        self.assertEqual(health_url, "http://127.0.0.1:8080/health")

        # Gateway Embed iFrame URL resolver
        embed_default = resolve_gateway_embed_url("http://localhost:5000")
        self.assertEqual(
            embed_default,
            "http://localhost:5000/embed?stream=octocamrtc&autoplay=true&muted=true&controls=true&poster=true&fit=contain&reconnect=true",
        )

        embed_custom = resolve_gateway_embed_url(
            "http://192.168.1.100:8080/api/webrtc",
            stream="bed_cam",
            autoplay=False,
            muted=False,
            controls=False,
            poster=False,
            fit="cover",
            reconnect=False,
            gateway="http://gateway.lan:5000",
        )
        self.assertEqual(
            embed_custom,
            "http://192.168.1.100:8080/embed?stream=bed_cam&autoplay=false&muted=false&controls=false&poster=false&fit=cover&reconnect=false&gateway=http%3A%2F%2Fgateway.lan%3A5000",
        )

    def test_legacy_go2rtc_url_resolvers(self) -> None:
        webrtc_url = resolve_go2rtc_webrtc_url("http://127.0.0.1:1984", "mycam")
        self.assertEqual(webrtc_url, "http://127.0.0.1:1984/api/webrtc?src=mycam")

        snap1 = resolve_go2rtc_snapshot_url("http://127.0.0.1:1984", "mycam")
        self.assertEqual(snap1, "http://127.0.0.1:1984/api/frame.jpeg?src=mycam")

        mjpeg1 = resolve_go2rtc_mjpeg_url("http://127.0.0.1:1984", "mycam")
        self.assertEqual(mjpeg1, "http://127.0.0.1:1984/api/stream.mjpeg?src=mycam")

        streams_url = resolve_go2rtc_streams_url("http://127.0.0.1:1984")
        self.assertEqual(streams_url, "http://127.0.0.1:1984/api/streams")

    def test_get_available_streams_dict_and_list_formats(self) -> None:
        engine = WebRTCEngine()

        # 1. Dict format (go2rtc style)
        mock_resp_dict = MagicMock(status_code=200, text='{"stream2": {}, "stream1": {}}')
        mock_resp_dict.json.return_value = {"stream2": {}, "stream1": {}}

        with patch("octoprint_octocamrtc.webrtc_engine.requests.get", return_value=mock_resp_dict):
            streams_dict = engine.get_available_streams("http://127.0.0.1:8080")
        self.assertEqual(streams_dict, ["stream1", "stream2"])

        # 2. List format (gateway style)
        mock_resp_list = MagicMock(status_code=200, text='["streamB", "streamA"]')
        mock_resp_list.json.return_value = ["streamB", "streamA"]

        with patch("octoprint_octocamrtc.webrtc_engine.requests.get", return_value=mock_resp_list):
            streams_list = engine.get_available_streams("http://127.0.0.1:8080")
        self.assertEqual(streams_list, ["streamA", "streamB"])

    def test_get_available_streams_failure_returns_empty_list(self) -> None:
        engine = WebRTCEngine()
        with patch("octoprint_octocamrtc.webrtc_engine.requests.get", side_effect=RuntimeError("connection refused")):
            streams = engine.get_available_streams("http://127.0.0.1:8080")
        self.assertEqual(streams, [])

    def test_get_ice_servers(self) -> None:
        engine = WebRTCEngine()
        mock_resp = MagicMock(status_code=200, text='{"iceServers": [{"urls": "stun:stun.cloudflare.com:3478"}], "forceTurnRelay": true}')
        mock_resp.json.return_value = {
            "iceServers": [{"urls": "stun:stun.cloudflare.com:3478"}],
            "forceTurnRelay": True,
        }

        with patch("octoprint_octocamrtc.webrtc_engine.requests.get", return_value=mock_resp) as mock_get:
            result = engine.get_ice_servers("http://127.0.0.1:8080")

        self.assertEqual(len(result["iceServers"]), 1)
        self.assertTrue(result["forceTurnRelay"])
        mock_get.assert_called_once_with("http://127.0.0.1:8080/api/ice-servers", timeout=3.0)

    def test_get_ice_servers_failure(self) -> None:
        engine = WebRTCEngine()
        with patch("octoprint_octocamrtc.webrtc_engine.requests.get", side_effect=RuntimeError("gateway offline")):
            result = engine.get_ice_servers("http://127.0.0.1:8080")
        self.assertEqual(result["iceServers"], [])
        self.assertFalse(result["forceTurnRelay"])

    def test_handle_offer_proxies_to_gateway_text_answer(self) -> None:
        engine = WebRTCEngine()
        config = EngineConfig(gateway_url="http://127.0.0.1:8080", stream_name="octocamrtc")

        mock_resp = MagicMock(status_code=200, text="v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\n")
        mock_resp.headers = {"Content-Type": "application/sdp"}

        with patch(
            "octoprint_octocamrtc.webrtc_engine.requests.post", return_value=mock_resp
        ) as mock_post:
            answer = engine.handle_offer(
                sdp="v=0\r\no=client 0 0 IN IP4 127.0.0.1\r\n",
                sdp_type="offer",
                config=config,
            )

        self.assertEqual(answer["type"], "answer")
        self.assertIn("v=0", answer["sdp"])
        mock_post.assert_called_once_with(
            "http://127.0.0.1:8080/api/webrtc?src=octocamrtc",
            data=b"v=0\r\no=client 0 0 IN IP4 127.0.0.1\r\n",
            headers={"Content-Type": "application/sdp", "Accept": "application/sdp, application/json"},
            timeout=10.0,
        )

    def test_handle_offer_with_stream_override_and_json_response(self) -> None:
        engine = WebRTCEngine()
        config = EngineConfig(gateway_url="http://127.0.0.1:8080", stream_name="default_stream")

        mock_resp = MagicMock(status_code=200, headers={"Content-Type": "application/json"})
        mock_resp.text = '{"type": "answer", "sdp": "v=0...json_answer"}'
        mock_resp.json.return_value = {"type": "answer", "sdp": "v=0...json_answer"}

        with patch(
            "octoprint_octocamrtc.webrtc_engine.requests.post", return_value=mock_resp
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
            "http://127.0.0.1:8080/api/webrtc?src=bed_cam",
            data=b"v=0...offer",
            headers={"Content-Type": "application/sdp", "Accept": "application/sdp, application/json"},
            timeout=10.0,
        )

    def test_capture_snapshot(self) -> None:
        engine = WebRTCEngine()
        config = EngineConfig(gateway_url="http://127.0.0.1:8080", stream_name="nozzle_cam")

        fake_jpeg = b"\xff\xd8\xff\xe0fake_jpeg_bytes"
        mock_resp = MagicMock(status_code=200, content=fake_jpeg)

        with patch("octoprint_octocamrtc.webrtc_engine.requests.get", return_value=mock_resp) as mock_get:
            result = engine.capture_snapshot(config)

        self.assertEqual(result, fake_jpeg)
        mock_get.assert_called_once_with(
            "http://127.0.0.1:8080/api/snapshot?src=nozzle_cam",
            timeout=5.0,
        )
        cached = engine.get_cached_snapshot("nozzle_cam")
        self.assertEqual(cached, fake_jpeg)

    def test_check_health(self) -> None:
        engine = WebRTCEngine()

        # Success
        mock_ok = MagicMock(status_code=200, text="Healthy")
        with patch("octoprint_octocamrtc.webrtc_engine.requests.get", return_value=mock_ok):
            res_ok = engine.check_health("http://127.0.0.1:8080")
        self.assertTrue(res_ok["ok"])
        self.assertEqual(res_ok["status_code"], 200)

        # Failure
        with patch("octoprint_octocamrtc.webrtc_engine.requests.get", side_effect=RuntimeError("connection refused")):
            res_err = engine.check_health("http://127.0.0.1:8080")
        self.assertFalse(res_err["ok"])
        self.assertEqual(res_err["status_code"], 0)

    def test_get_cached_snapshot_with_max_age(self) -> None:
        engine = WebRTCEngine()
        config = EngineConfig(gateway_url="http://127.0.0.1:8080", stream_name="cam")
        mock_resp = MagicMock(status_code=200, content=b"jpeg1")

        with patch("octoprint_octocamrtc.webrtc_engine.requests.get", return_value=mock_resp):
            engine.capture_snapshot(config)

        self.assertEqual(engine.get_cached_snapshot("cam", max_age=10.0), b"jpeg1")
        self.assertIsNone(engine.get_cached_snapshot("cam", max_age=-1.0))
        self.assertIsNone(engine.get_cached_snapshot("nonexistent"))

    def test_update_snapshot_cache_handles_errors_gracefully(self) -> None:
        engine = WebRTCEngine()
        config = EngineConfig(gateway_url="http://127.0.0.1:8080", stream_name="cam")

        with patch(
            "octoprint_octocamrtc.webrtc_engine.requests.get",
            side_effect=RuntimeError("connection failed"),
        ):
            result = engine.update_snapshot_cache(config)

        self.assertIsNone(result)
        self.assertIsNone(engine.get_cached_snapshot("cam"))

    def test_clear_snapshot_cache(self) -> None:
        engine = WebRTCEngine()
        config = EngineConfig(gateway_url="http://127.0.0.1:8080", stream_name="cam1")
        mock_resp = MagicMock(status_code=200, content=b"data")

        with patch("octoprint_octocamrtc.webrtc_engine.requests.get", return_value=mock_resp):
            engine.capture_snapshot(config, stream_override="cam1")
            engine.capture_snapshot(config, stream_override="cam2")

        self.assertIsNotNone(engine.get_cached_snapshot("cam1"))
        self.assertIsNotNone(engine.get_cached_snapshot("cam2"))

        engine.clear_snapshot_cache("cam1")
        self.assertIsNone(engine.get_cached_snapshot("cam1"))
        self.assertIsNotNone(engine.get_cached_snapshot("cam2"))

        engine.clear_snapshot_cache()
        self.assertIsNone(engine.get_cached_snapshot("cam2"))


if __name__ == "__main__":
    unittest.main()

