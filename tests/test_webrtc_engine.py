# coding=utf-8
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import MagicMock, patch

from octoprint_octocam.webrtc_engine import (
    EncodedH264Frame,
    EngineConfig,
    FFmpegH264Stream,
    WebRTCEngine,
    _extract_sdp_video_codecs,
    _silence_pyav_swscaler_warnings,
    apply_pil_transforms,
    build_ffmpeg_h264_cmd,
    build_ffmpeg_vf_filter,
    get_candidate_pipeline_modes,
    install_hardware_h264_encoder,
    parse_bitrate_bps,
    parse_resolution,
    probe_vaapi_support,
    split_h264_annexb_frames,
)


class TestWebRTCEngine(unittest.TestCase):
    def test_parse_bitrate_bps_and_resolution(self) -> None:
        self.assertEqual(parse_bitrate_bps("2500k"), 2_500_000)
        self.assertEqual(parse_bitrate_bps("4M"), 4_000_000)
        self.assertEqual(parse_bitrate_bps("1500000"), 1_500_000)
        self.assertEqual(parse_bitrate_bps("invalid", default_bps=2_000_000), 2_000_000)

        self.assertEqual(parse_resolution("1920x1080"), (1920, 1080))
        self.assertEqual(parse_resolution("invalid"), (1280, 720))

    def test_build_ffmpeg_vf_filter_modes(self) -> None:
        self.assertEqual(
            build_ffmpeg_vf_filter(
                flip_h=True,
                flip_v=True,
                rotate90=True,
                width=1280,
                height=720,
                pipeline_mode="vaapi_hw",
            ),
            "scale_vaapi=w=1280:h=720:format=nv12,transpose_vaapi=dir=hflip,transpose_vaapi=dir=vflip,transpose_vaapi=dir=cclock",
        )
        self.assertEqual(
            build_ffmpeg_vf_filter(
                flip_h=True,
                flip_v=False,
                rotate90=True,
                width=1280,
                height=720,
                pipeline_mode="vaapi",
            ),
            "scale=1280:720,hflip,transpose=cclock,format=nv12,hwupload",
        )
        self.assertEqual(
            build_ffmpeg_vf_filter(
                flip_h=False,
                flip_v=True,
                rotate90=False,
                width=1280,
                height=720,
                pipeline_mode="software",
            ),
            "scale=1280:720,vflip,format=yuv420p",
        )
        self.assertEqual(
            build_ffmpeg_vf_filter(
                flip_h=True,
                flip_v=False,
                rotate90=False,
                pipeline_mode="snapshot",
            ),
            "hflip",
        )

    def test_build_ffmpeg_h264_cmd_single_process_vaapi(self) -> None:
        config = EngineConfig(
            stream_source="/dev/video0",
            input_format="auto",
            resolution="1920x1080",
            framerate=30,
            bitrate="2500k",
            hwaccel_mode="vaapi",
            vaapi_device="/dev/dri/renderD128",
            flip_h=True,
        )
        cmd = build_ffmpeg_h264_cmd(
            config, pipeline_mode="vaapi_hw", ffmpeg_bin="/usr/bin/ffmpeg"
        )
        self.assertIn("-nostdin", cmd)
        self.assertIn("-hwaccel", cmd)
        self.assertIn("vaapi", cmd)
        self.assertIn("-input_format", cmd)
        self.assertIn("mjpeg", cmd)
        self.assertIn("h264_vaapi", cmd)
        self.assertIn(
            "scale_vaapi=w=1920:h=1080:format=nv12,transpose_vaapi=dir=hflip", cmd
        )
        self.assertEqual(cmd[-1], "pipe:1")

    def test_get_candidate_pipeline_modes(self) -> None:
        vaapi_cfg = EngineConfig(
            hwaccel_mode="vaapi", vaapi_device="/dev/dri/renderD128"
        )
        with patch(
            "octoprint_octocam.webrtc_engine.os.path.exists", return_value=True
        ), patch("octoprint_octocam.webrtc_engine.os.access", return_value=True):
            self.assertEqual(
                get_candidate_pipeline_modes(vaapi_cfg),
                ["vaapi_hw", "vaapi", "software"],
            )

        with patch(
            "octoprint_octocam.webrtc_engine.os.path.exists", return_value=False
        ):
            self.assertEqual(get_candidate_pipeline_modes(vaapi_cfg), ["software"])

        v4l2_cfg = EngineConfig(hwaccel_mode="v4l2m2m")
        self.assertEqual(
            get_candidate_pipeline_modes(v4l2_cfg), ["v4l2m2m", "software"]
        )

    def test_split_h264_annexb_frames_and_keyframe_sps_pps_injection(self) -> None:
        sps = b"\x00\x00\x00\x01\x67\x42\x00\x1f"
        pps = b"\x00\x00\x00\x01\x68\xce\x3c\x80"
        idr = b"\x00\x00\x00\x01\x65\x88\x84\x21"
        p_frame_1 = b"\x00\x00\x00\x01\x41\x9a\x22\x11"
        p_frame_2 = b"\x00\x00\x00\x01\x41\x9b\x33\x44"

        buf = bytearray(sps + pps + idr + p_frame_1 + p_frame_2)
        frames = split_h264_annexb_frames(buf, flush_if_vcl=True)

        self.assertEqual(len(frames), 3)
        self.assertEqual(frames[0], sps + pps + idr)
        self.assertEqual(frames[1], p_frame_1)
        self.assertEqual(frames[2], p_frame_2)
        self.assertEqual(len(buf), 0)

        # Verify FFmpegH264Stream prepends cached SPS/PPS if a later IDR arrives without SPS/PPS
        stream = FFmpegH264Stream(EngineConfig())
        stream._process_access_unit(sps + pps + idr)
        self.assertEqual(stream.latest_keyframe, sps + pps + idr)

        bare_idr = b"\x00\x00\x00\x01\x65\x88\x99\x00"
        stream._process_access_unit(bare_idr)
        self.assertEqual(stream.latest_keyframe, sps + pps + bare_idr)

    def test_passthrough_h264_encoder_forwards_encoded_frames(self) -> None:
        class DummyBaseEncoder:
            target_bitrate = 2_500_000

            @staticmethod
            def _split_bitstream(bitstream: bytes):
                yield bitstream.lstrip(b"\x00\x01")

            def _packetize(self, packages):
                return list(packages)

            def pack(self, packet):
                raise AssertionError("base pack should not be called for EncodedH264Frame")

            def encode(self, frame, force_keyframe=False):
                return [b"software-fallback"], 999

        fake_codecs = MagicMock(H264Encoder=DummyBaseEncoder)
        fake_h264 = MagicMock(H264Encoder=DummyBaseEncoder, spec=["H264Encoder"])

        install_hardware_h264_encoder(
            aiortc_codecs_module=fake_codecs,
            aiortc_h264_module=fake_h264,
        )

        encoder = fake_h264.H264Encoder()
        hw_frame = EncodedH264Frame(
            bitstream=b"\x00\x00\x00\x01\x65\x88\x00",
            pts=3000,
        )
        payloads, timestamp = encoder.encode(hw_frame, False)
        self.assertEqual(payloads, [b"\x65\x88\x00"])
        self.assertEqual(timestamp, 3000)

        pack_payloads, pack_timestamp = encoder.pack(hw_frame)
        self.assertEqual(pack_payloads, [b"\x65\x88\x00"])
        self.assertEqual(pack_timestamp, 3000)
        self.assertEqual(bytes(hw_frame), b"\x00\x00\x00\x01\x65\x88\x00")

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

    def test_silence_pyav_swscaler_warnings(self) -> None:
        mock_av_logging = MagicMock()
        mock_av_logging.ERROR = 16
        mock_av = MagicMock(logging=mock_av_logging)

        with patch.dict(
            "sys.modules",
            {"av": mock_av, "av.logging": mock_av_logging},
        ):
            _silence_pyav_swscaler_warnings()
            mock_av_logging.set_level.assert_called_with(16)

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

    def test_stream_subscriber_and_pc_cleanup(self) -> None:
        engine = WebRTCEngine()
        mock_stream = MagicMock()
        engine._stream = mock_stream

        fake_pc_sub_track = MagicMock()
        fake_pc = MagicMock()
        fake_pc._octocam_sub_track = fake_pc_sub_track

        async def fake_close():
            return None

        fake_pc.close = fake_close
        engine._pcs.add(fake_pc)
        asyncio.run(engine._cleanup_pc(fake_pc))

        fake_pc_sub_track.stop.assert_called_once()
        mock_stream.stop.assert_called_once()
        self.assertIsNone(engine._stream)


if __name__ == "__main__":
    unittest.main()
