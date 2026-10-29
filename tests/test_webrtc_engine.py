# coding=utf-8
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from octoprint_octocam.webrtc_engine import (
    EngineConfig,
    FFmpegFrameFilter,
    WebRTCEngine,
    _extract_sdp_video_codecs,
    _silence_pyav_swscaler_warnings,
    apply_pil_transforms,
    build_ffmpeg_vf_filter,
    build_media_player_kwargs,
    configure_v4l2_camera_transforms,
    create_transformed_video_track,
    create_vaapi_hwdecode_track,
    install_hardware_h264_encoder,
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
        self.assertEqual(kwargs["options"]["input_format"], "mjpeg")

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

    def test_create_transformed_video_track_noop_when_disabled(self) -> None:
        sentinel_track = MagicMock()
        result = create_transformed_video_track(
            sentinel_track, flip_h=False, flip_v=False, rotate90=False
        )
        self.assertIs(result, sentinel_track)

    def test_build_ffmpeg_vf_filter(self) -> None:
        self.assertEqual(
            build_ffmpeg_vf_filter(
                flip_h=True,
                flip_v=True,
                rotate90=True,
                include_vaapi_hwupload=True,
                use_vaapi_transpose=True,
            ),
            "format=nv12,hwupload,scale_vaapi=format=nv12,transpose_vaapi=dir=hflip,transpose_vaapi=dir=vflip,transpose_vaapi=dir=cclock",
        )
        self.assertEqual(
            build_ffmpeg_vf_filter(
                flip_h=True,
                flip_v=False,
                rotate90=True,
                vaapi_hwdecode=True,
                use_vaapi_transpose=True,
            ),
            "scale_vaapi=format=nv12,transpose_vaapi=dir=hflip,transpose_vaapi=dir=cclock",
        )
        self.assertEqual(
            build_ffmpeg_vf_filter(
                flip_h=True,
                flip_v=True,
                rotate90=True,
                include_vaapi_hwupload=True,
                use_vaapi_transpose=False,
            ),
            "hflip,vflip,transpose=cclock,format=nv12,hwupload",
        )
        self.assertEqual(
            build_ffmpeg_vf_filter(
                flip_h=True, flip_v=False, rotate90=False, include_vaapi_hwupload=False
            ),
            "hflip",
        )

    def test_configure_v4l2_camera_transforms_partial_hardware_support(self) -> None:
        # Simulate a V4L2 camera that supports horizontal_flip and vertical_flip in hardware,
        # but does not support 90-degree rotation in hardware.
        def fake_set_ctrl(device_path: str, ctrl_name: str, cid: int, value: int) -> bool:
            return ctrl_name in ("horizontal_flip", "vertical_flip")

        with patch("octoprint_octocam.webrtc_engine.os.path.exists", return_value=True), patch(
            "octoprint_octocam.webrtc_engine._set_v4l2_control", side_effect=fake_set_ctrl
        ):
            applied = configure_v4l2_camera_transforms(
                stream_source="/dev/video0",
                flip_h=True,
                flip_v=True,
                rotate90=True,
            )

        self.assertTrue(applied["flip_h_applied"])
        self.assertTrue(applied["flip_v_applied"])
        self.assertFalse(applied["rotate90_applied"])

    def test_ffmpeg_frame_filter_graph_construction(self) -> None:
        ff_filter = FFmpegFrameFilter(flip_h=True, flip_v=True, rotate90=True)
        fake_frame = MagicMock()
        fake_frame.width = 1280
        fake_frame.height = 720
        fake_frame.format.name = "yuv420p"
        fake_frame.pts = 1000
        fake_frame.time_base = "1/30"

        mock_graph = MagicMock()
        mock_out_frame = MagicMock()
        mock_graph.pull.return_value = mock_out_frame

        mock_av = MagicMock()
        mock_av.filter.Graph.return_value = mock_graph

        with patch.dict("sys.modules", {"av": mock_av}):
            result = ff_filter.transform(fake_frame)

        self.assertIs(result, mock_out_frame)
        self.assertEqual(result.pts, 1000)
        self.assertEqual(result.time_base, "1/30")
        mock_graph.add.assert_any_call("hflip")
        mock_graph.add.assert_any_call("vflip")
        mock_graph.add.assert_any_call("transpose", "cclock")
        mock_graph.configure.assert_called_once()
        mock_graph.push.assert_called_once_with(fake_frame)

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

    def test_silence_pyav_swscaler_warnings_and_persistent_reformatter(self) -> None:
        mock_av_logging = MagicMock()
        mock_av_logging.ERROR = 16
        mock_reformatter_instance = MagicMock()
        mock_reformatter_cls = MagicMock(return_value=mock_reformatter_instance)
        mock_av = MagicMock()
        mock_av.logging = mock_av_logging

        with patch.dict(
            "sys.modules",
            {
                "av": mock_av,
                "av.logging": mock_av_logging,
                "av.video.reformatter": MagicMock(VideoReformatter=mock_reformatter_cls),
            },
        ):
            _silence_pyav_swscaler_warnings()
            mock_av_logging.set_level.assert_called_with(16)

            class DummyBaseEncoder:
                target_bitrate = 2_500_000

                def _encode_frame(self, frame, force_keyframe):
                    yield b"encoded"

            fake_codecs = MagicMock(H264Encoder=DummyBaseEncoder)
            fake_h264 = MagicMock(H264Encoder=DummyBaseEncoder, spec=["H264Encoder"])

            install_hardware_h264_encoder(
                aiortc_codecs_module=fake_codecs,
                aiortc_h264_module=fake_h264,
                hwaccel_mode="none",
                vaapi_device="/dev/dri/renderD128",
                target_bitrate=2_500_000,
                framerate=30,
            )

            encoder = fake_h264.H264Encoder()
            frame1 = MagicMock()
            frame1.format.name = "yuvj422p"
            frame2 = MagicMock()
            frame2.format.name = "yuvj422p"

            list(encoder._encode_frame(frame1, False))
            list(encoder._encode_frame(frame2, False))

            # VideoReformatter is instantiated once and reused across frames
            mock_reformatter_cls.assert_called_once()
            self.assertEqual(mock_reformatter_instance.reformat.call_count, 2)
            frame1.reformat.assert_not_called()
            frame2.reformat.assert_not_called()

    def test_extract_sdp_video_codecs_and_prefer_h264(self) -> None:
        sample_sdp = (
            "v=0\r\n"
            "m=video 9 UDP/TLS/RTP/SAVPF 96 98\r\n"
            "a=rtpmap:96 VP8/90000\r\n"
            "a=rtpmap:98 H264/90000\r\n"
        )
        self.assertEqual(
            _extract_sdp_video_codecs(sample_sdp),
            ["VP8/90000", "H264/90000"],
        )

        vp8_codec = MagicMock(mimeType="video/VP8")
        h264_codec = MagicMock(mimeType="video/H264")
        fake_codecs_mod = MagicMock(CODECS={"video": [vp8_codec, h264_codec]})
        fake_transceiver = MagicMock(kind="video")
        fake_pc = MagicMock()
        fake_pc.getTransceivers.return_value = [fake_transceiver]

        engine = WebRTCEngine()
        with patch.dict(
            "sys.modules",
            {"aiortc": MagicMock(codecs=fake_codecs_mod), "aiortc.codecs": fake_codecs_mod},
        ):
            engine._prefer_h264_on_pc(fake_pc)

        fake_transceiver.setCodecPreferences.assert_called_once_with(
            [h264_codec, vp8_codec]
        )

    def test_create_vaapi_hwdecode_track_returns_none_when_device_missing(self) -> None:
        config = EngineConfig(
            hwaccel_mode="vaapi",
            vaapi_device="/dev/dri/renderD128",
        )
        with patch("octoprint_octocam.webrtc_engine.os.path.exists", return_value=False):
            track, gpu_transpose = create_vaapi_hwdecode_track(config)
        self.assertIsNone(track)
        self.assertFalse(gpu_transpose)


if __name__ == "__main__":
    unittest.main()
