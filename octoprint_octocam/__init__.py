# coding=utf-8
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional

try:
    import flask
except ImportError:  # pragma: no cover - fallback for test environments without Flask
    flask = None  # type: ignore[assignment]

try:
    import octoprint.plugin

    _BaseStartupPlugin = octoprint.plugin.StartupPlugin
    _BaseSettingsPlugin = octoprint.plugin.SettingsPlugin
    _BaseAssetPlugin = octoprint.plugin.AssetPlugin
    _BaseTemplatePlugin = octoprint.plugin.TemplatePlugin
    _BaseSimpleApiPlugin = octoprint.plugin.SimpleApiPlugin
    _BaseWebcamProviderPlugin = octoprint.plugin.WebcamProviderPlugin
    _BaseShutdownPlugin = octoprint.plugin.ShutdownPlugin
except ImportError:  # pragma: no cover - fallback for unit testing outside OctoPrint
    class _BaseStartupPlugin:
        pass

    class _BaseSettingsPlugin:
        pass

    class _BaseAssetPlugin:
        pass

    class _BaseTemplatePlugin:
        pass

    class _BaseSimpleApiPlugin:
        pass

    class _BaseWebcamProviderPlugin:
        pass

    class _BaseShutdownPlugin:
        pass

from octoprint_octocam.turn_provider import TurnCredentialError, TurnCredentialProvider
from octoprint_octocam.webrtc_engine import (
    EngineConfig,
    WebRTCEngine,
    probe_vaapi_support,
)


class OctocamPlugin(
    _BaseStartupPlugin,
    _BaseSettingsPlugin,
    _BaseAssetPlugin,
    _BaseTemplatePlugin,
    _BaseSimpleApiPlugin,
    _BaseWebcamProviderPlugin,
    _BaseShutdownPlugin,
):
    """OctoPrint WebRTC Webcam Provider Plugin with STUN/TURN (Cloudflare TURN) & VAAPI."""

    def __init__(self) -> None:
        super().__init__()
        self._logger = getattr(self, "_logger", logging.getLogger(__name__))
        self._turn_provider = TurnCredentialProvider(logger=self._logger)
        self._webrtc_engine = WebRTCEngine(logger_instance=self._logger)

    # ~~ StartupPlugin mixin ~~

    def on_after_startup(self) -> None:
        try:
            default_webcam = self._settings.global_get(["webcam", "defaultWebcam"])
            classic_stream = self._settings.global_get(
                ["plugins", "classicwebcam", "stream"]
            )
            if not default_webcam or (
                default_webcam in ("classic", "classicwebcam") and not classic_stream
            ):
                self._settings.global_set(["webcam", "defaultWebcam"], "octocam")
                self._settings.save()
                self._logger.info(
                    "Set OctoPrint default webcam to 'octocam' (classicwebcam stream was unconfigured)."
                )
        except Exception as exc:
            self._logger.debug("Could not check/update defaultWebcam on startup: %s", exc)

    # ~~ SettingsPlugin mixin ~~

    def get_settings_defaults(self) -> Dict[str, Any]:
        return {
            "webcam_name": "OctoCam WebRTC",
            "backend_mode": "aiortc",  # "aiortc" or "whep"
            "stream_source": "/dev/video0",
            "input_format": "auto",  # "auto", "v4l2", "mjpeg", "rtsp"
            "resolution": "1280x720",
            "framerate": 30,
            "bitrate": "2500k",
            "aspect_ratio": "16:9",  # "16:9" or "4:3"
            "hwaccel_mode": "vaapi",  # "vaapi", "v4l2m2m", "none"
            "vaapi_device": "/dev/dri/renderD128",
            "whep_url": "http://127.0.0.1:8889/octocam/whep",
            "snapshot_url": "",
            "cloudflare_enabled": True,
            "cloudflare_turn_key_id": "",
            "cloudflare_api_token": "",
            "cloudflare_ttl": 3600,
            "force_turn_relay": False,
            "ice_servers": [
                {
                    "urls": "stun:stun.cloudflare.com:3478",
                    "username": "",
                    "credential": "",
                }
            ],
            "flip_h": False,
            "flip_v": False,
            "rotate90": False,
        }

    def get_settings_restricted_paths(self) -> Dict[str, List[List[str]]]:
        return {
            "admin": [
                ["cloudflare_api_token"],
            ]
        }

    def on_settings_save(self, data: Dict[str, Any]) -> None:
        if hasattr(_BaseSettingsPlugin, "on_settings_save"):
            super().on_settings_save(data)  # type: ignore[misc]
        self._turn_provider.invalidate_cache()

    # ~~ AssetPlugin mixin ~~

    def get_assets(self) -> Dict[str, List[str]]:
        return {
            "js": ["js/octocam.js"],
            "css": ["css/octocam.css"],
        }

    # ~~ TemplatePlugin mixin ~~

    def get_template_configs(self) -> List[Dict[str, Any]]:
        return [
            {
                "type": "settings",
                "name": "OctoCam (WebRTC)",
                "template": "octocam_settings.jinja2",
                "custom_bindings": True,
            },
            {
                "type": "webcam",
                "template": "octocam_webcam.jinja2",
                "custom_bindings": True,
            },
        ]

    # ~~ WebcamProviderPlugin mixin (OctoPrint 1.9+) ~~

    def get_webcam_configurations(self) -> Iterable[Any]:
        display_name = str(self._settings.get(["webcam_name"]) or "OctoCam WebRTC")
        aspect_ratio_str = str(self._settings.get(["aspect_ratio"]) or "16:9")
        stream_source = str(self._settings.get(["stream_source"]) or "/dev/video0").strip()
        snapshot_url = str(self._settings.get(["snapshot_url"]) or "").strip()
        snapshot_display = snapshot_url or f"OctoCam ({stream_source})"

        try:
            from octoprint.schema.webcam import (
                RatioEnum,
                Webcam,
                WebcamCompatibility,
            )

            ratio = (
                RatioEnum.four_three
                if aspect_ratio_str == "4:3"
                else RatioEnum.sixteen_nine
            )
            # Flip and rotation are applied directly in the server encode/decode pipeline
            # so the video stream and snapshots are already oriented for all devices and fullscreen.
            return [
                Webcam(
                    name="octocam",
                    displayName=display_name,
                    flipH=False,
                    flipV=False,
                    rotate90=False,
                    snapshotDisplay=snapshot_display,
                    canSnapshot=True,
                    compat=WebcamCompatibility(
                        stream="webrtc",
                        streamTimeout=15,
                        streamRatio=ratio,
                        snapshot=snapshot_url or "internal://octocam",
                        snapshotTimeout=5,
                        snapshotSslValidation=True,
                    ),
                    extras={
                        "streamRatio": ratio.value if hasattr(ratio, "value") else aspect_ratio_str,
                    },
                )
            ]
        except Exception as exc:
            self._logger.warning("Falling back to dict webcam config: %s", exc)
            return [
                {
                    "name": "octocam",
                    "displayName": display_name,
                    "flipH": False,
                    "flipV": False,
                    "rotate90": False,
                    "snapshotDisplay": snapshot_display,
                    "canSnapshot": True,
                }
            ]

    def take_webcam_snapshot(self, provided_webcam: Any) -> Iterable[bytes]:
        config = self._build_engine_config(include_turn=False)
        jpeg_bytes = self._webrtc_engine.capture_snapshot(config)
        return [jpeg_bytes]

    # ~~ SimpleApiPlugin mixin ~~

    def get_api_commands(self) -> Dict[str, List[str]]:
        return {
            "get_ice_servers": [],
            "offer": ["sdp", "type"],
            "test_cloudflare": [],
            "check_vaapi": [],
        }

    def on_api_command(self, command: str, data: Dict[str, Any]) -> Any:
        if command == "get_ice_servers":
            ice_servers = self._resolve_ice_servers()
            return self._json_response(
                {
                    "iceServers": ice_servers,
                    "forceTurnRelay": bool(
                        self._settings.get_boolean(["force_turn_relay"])
                    ),
                }
            )

        if command == "offer":
            sdp = data.get("sdp", "")
            sdp_type = data.get("type", "offer")
            if not sdp:
                return self._json_response(
                    {"error": "Missing 'sdp' in WebRTC offer payload."},
                    status=400,
                )
            try:
                config = self._build_engine_config(include_turn=True)
                answer = self._webrtc_engine.handle_offer(
                    sdp=sdp,
                    sdp_type=sdp_type,
                    config=config,
                )
                return self._json_response(answer)
            except Exception as exc:
                self._logger.exception("WebRTC offer negotiation failed")
                return self._json_response(
                    {"error": str(exc)},
                    status=500,
                )

        if command == "test_cloudflare":
            key_id = (
                data.get("turn_key_id")
                or self._settings.get(["cloudflare_turn_key_id"])
                or ""
            )
            api_token = (
                data.get("api_token")
                or self._settings.get(["cloudflare_api_token"])
                or ""
            )
            ttl = int(
                data.get("ttl")
                or self._settings.get_int(["cloudflare_ttl"])
                or 3600
            )
            try:
                servers = self._turn_provider.test_cloudflare_credentials(
                    turn_key_id=key_id,
                    api_token=api_token,
                    ttl=ttl,
                )
                return self._json_response(
                    {
                        "ok": True,
                        "iceServers": servers,
                        "message": "Successfully generated short-lived Cloudflare TURN credentials.",
                    }
                )
            except TurnCredentialError as exc:
                return self._json_response(
                    {
                        "ok": False,
                        "error": str(exc),
                    },
                    status=400,
                )

        if command == "check_vaapi":
            vaapi_device = (
                data.get("vaapi_device")
                or self._settings.get(["vaapi_device"])
                or "/dev/dri/renderD128"
            )
            status = probe_vaapi_support(vaapi_device=vaapi_device)
            return self._json_response(status)

        return self._json_response(
            {"error": f"Unknown API command: {command}"}, status=400
        )

    # ~~ ShutdownPlugin mixin ~~

    def on_shutdown(self) -> None:
        self._webrtc_engine.stop()

    # ~~ Internal Helpers ~~

    def _resolve_ice_servers(self) -> List[Dict[str, Any]]:
        cf_enabled = bool(self._settings.get_boolean(["cloudflare_enabled"]))
        cf_key_id = self._settings.get(["cloudflare_turn_key_id"]) or ""
        cf_token = self._settings.get(["cloudflare_api_token"]) or ""
        cf_ttl = int(self._settings.get_int(["cloudflare_ttl"]) or 3600)
        static_servers = self._settings.get(["ice_servers"]) or []

        return self._turn_provider.get_ice_servers(
            cloudflare_enabled=cf_enabled,
            cloudflare_turn_key_id=cf_key_id,
            cloudflare_api_token=cf_token,
            cloudflare_ttl=cf_ttl,
            static_ice_servers=static_servers,
        )

    def _build_engine_config(self, include_turn: bool = True) -> EngineConfig:
        ice_servers = self._resolve_ice_servers() if include_turn else []
        return EngineConfig(
            backend_mode=self._settings.get(["backend_mode"]) or "aiortc",
            stream_source=self._settings.get(["stream_source"]) or "/dev/video0",
            input_format=self._settings.get(["input_format"]) or "auto",
            resolution=self._settings.get(["resolution"]) or "1280x720",
            framerate=int(self._settings.get_int(["framerate"]) or 30),
            bitrate=self._settings.get(["bitrate"]) or "2500k",
            hwaccel_mode=self._settings.get(["hwaccel_mode"]) or "vaapi",
            vaapi_device=self._settings.get(["vaapi_device"])
            or "/dev/dri/renderD128",
            whep_url=self._settings.get(["whep_url"]) or "",
            snapshot_url=self._settings.get(["snapshot_url"]) or "",
            flip_h=bool(self._settings.get_boolean(["flip_h"])),
            flip_v=bool(self._settings.get_boolean(["flip_v"])),
            rotate90=bool(self._settings.get_boolean(["rotate90"])),
            ice_servers=ice_servers,
        )

    @staticmethod
    def _json_response(payload: Dict[str, Any], status: int = 200) -> Any:
        if flask is not None:
            response = flask.jsonify(payload)
            response.status_code = status
            return response
        return payload, status

    # ~~ Softwareupdate hook ~~

    def get_update_information(self) -> Dict[str, Any]:
        return {
            "octocam": {
                "displayName": "OctoCam (WebRTC + TURN)",
                "displayVersion": getattr(self, "_plugin_version", "0.1.0"),
                "type": "github_release",
                "user": "Ryan",
                "repo": "Octocam",
                "current": getattr(self, "_plugin_version", "0.1.0"),
                "pip": "https://github.com/Ryan/Octocam/archive/{target_version}.zip",
            }
        }


__plugin_name__ = "OctoCam (WebRTC)"
__plugin_pythoncompat__ = ">=3.9,<4"


def __plugin_load__() -> None:
    global __plugin_implementation__
    __plugin_implementation__ = OctocamPlugin()

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information
    }
