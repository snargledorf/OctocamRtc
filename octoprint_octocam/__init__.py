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

import requests

from octoprint_octocam.turn_provider import TurnCredentialError, TurnCredentialProvider
from octoprint_octocam.webrtc_engine import (
    EngineConfig,
    WebRTCEngine,
    resolve_go2rtc_mjpeg_url,
    resolve_go2rtc_snapshot_url,
    resolve_go2rtc_webrtc_url,
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
    """OctoPrint WebRTC Webcam Provider Plugin powered by go2rtc with Cloudflare TURN support."""

    def __init__(self) -> None:
        super().__init__()
        self._logger = getattr(
            self, "_logger", logging.getLogger("octoprint.plugins.octocam")
        )
        self._turn_provider = TurnCredentialProvider(logger=self._logger)
        self._webrtc_engine = WebRTCEngine(logger_instance=self._logger)

    def initialize(self) -> None:
        if hasattr(super(), "initialize"):
            super().initialize()  # type: ignore[misc]
        self._turn_provider._logger = self._logger
        self._webrtc_engine._logger = self._logger
        self._logger.info("OctoCam plugin initialized (go2rtc backend).")

    # ~~ StartupPlugin mixin ~~

    def on_after_startup(self) -> None:
        self._turn_provider._logger = self._logger
        self._webrtc_engine._logger = self._logger
        go2rtc_url = str(self._settings.get(["go2rtc_url"]) or "http://127.0.0.1:1984")
        go2rtc_stream = str(self._settings.get(["go2rtc_stream"]) or "octocam")
        self._logger.info(
            "[OctoCam] Startup configuration: go2rtc_url='%s', go2rtc_stream='%s'",
            go2rtc_url,
            go2rtc_stream,
        )
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

        self._sync_webcam_stream_url()

    # ~~ SettingsPlugin mixin ~~

    def get_settings_defaults(self) -> Dict[str, Any]:
        return {
            "webcam_name": "OctoCam WebRTC",
            "go2rtc_url": "http://127.0.0.1:1984",
            "go2rtc_stream": "octocam",
            "stream_url": "",
            "snapshot_url": "",
            "sync_octoprint_stream": True,
            "aspect_ratio": "16:9",  # "16:9" or "4:3"
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
        self._sync_webcam_stream_url()

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
        base_display_name = str(self._settings.get(["webcam_name"]) or "OctoCam WebRTC")
        aspect_ratio_str = str(self._settings.get(["aspect_ratio"]) or "16:9")
        go2rtc_url = str(self._settings.get(["go2rtc_url"]) or "http://127.0.0.1:1984").strip()
        active_stream = str(self._settings.get(["go2rtc_stream"]) or "octocam").strip()
        custom_stream_url = str(self._settings.get(["stream_url"]) or "").strip()
        custom_snapshot_url = str(self._settings.get(["snapshot_url"]) or "").strip()

        # Query all configured streams from go2rtc so all streams are selectable in OctoPrint
        available = self._webrtc_engine.get_available_streams(go2rtc_url)
        streams_to_register = list(available) if available else []
        if active_stream and active_stream not in streams_to_register:
            streams_to_register.insert(0, active_stream)
        if not streams_to_register:
            streams_to_register = ["octocam"]

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

            configs = []
            for stream in streams_to_register:
                is_primary = (stream == active_stream)
                cam_name = "octocam" if is_primary else f"octocam_{stream}"
                display = (
                    base_display_name
                    if len(streams_to_register) == 1
                    else f"{base_display_name} ({stream})"
                )
                stream_display = (
                    custom_stream_url
                    if (custom_stream_url and is_primary)
                    else resolve_go2rtc_mjpeg_url(go2rtc_url, stream)
                )
                snap_display = (
                    custom_snapshot_url
                    if (custom_snapshot_url and is_primary)
                    else resolve_go2rtc_snapshot_url(go2rtc_url, stream)
                )

                configs.append(
                    Webcam(
                        name=cam_name,
                        displayName=display,
                        flipH=False,
                        flipV=False,
                        rotate90=False,
                        snapshotDisplay=snap_display,
                        canSnapshot=True,
                        compat=WebcamCompatibility(
                            stream=stream_display,
                            streamTimeout=15,
                            streamRatio=ratio,
                            snapshot=snap_display,
                            snapshotTimeout=5,
                            snapshotSslValidation=True,
                        ),
                        extras={
                            "streamRatio": ratio.value if hasattr(ratio, "value") else aspect_ratio_str,
                            "go2rtcStream": stream,
                        },
                    )
                )
            return configs
        except Exception as exc:
            self._logger.warning("Falling back to dict webcam configs: %s", exc)
            dict_configs = []
            for stream in streams_to_register:
                is_primary = (stream == active_stream)
                cam_name = "octocam" if is_primary else f"octocam_{stream}"
                display = (
                    base_display_name
                    if len(streams_to_register) == 1
                    else f"{base_display_name} ({stream})"
                )
                stream_display = (
                    custom_stream_url
                    if (custom_stream_url and is_primary)
                    else resolve_go2rtc_mjpeg_url(go2rtc_url, stream)
                )
                snap_display = (
                    custom_snapshot_url
                    if (custom_snapshot_url and is_primary)
                    else resolve_go2rtc_snapshot_url(go2rtc_url, stream)
                )
                dict_configs.append(
                    {
                        "name": cam_name,
                        "displayName": display,
                        "flipH": False,
                        "flipV": False,
                        "rotate90": False,
                        "stream": stream_display,
                        "streamDisplay": stream_display,
                        "snapshotDisplay": snap_display,
                        "canSnapshot": True,
                        "go2rtcStream": stream,
                        "compat": {
                            "stream": stream_display,
                            "snapshot": snap_display,
                            "streamRatio": aspect_ratio_str,
                        },
                    }
                )
            return dict_configs

    def take_webcam_snapshot(self, provided_webcam: Any) -> Iterable[bytes]:
        config = self._build_engine_config(include_turn=False)
        cam_name = (
            getattr(provided_webcam, "name", "")
            if not isinstance(provided_webcam, dict)
            else str(provided_webcam.get("name", ""))
        )
        stream_override = None
        if cam_name.startswith("octocam_"):
            stream_override = cam_name[len("octocam_") :]
        jpeg_bytes = self._webrtc_engine.capture_snapshot(
            config, stream_override=stream_override
        )
        return [jpeg_bytes]

    # ~~ SimpleApiPlugin mixin ~~

    def get_api_commands(self) -> Dict[str, List[str]]:
        return {
            "get_ice_servers": [],
            "get_available_streams": [],
            "offer": ["sdp", "type"],
            "test_cloudflare": [],
            "test_go2rtc": [],
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

        if command == "get_available_streams":
            go2rtc_url = (
                data.get("go2rtc_url")
                or self._settings.get(["go2rtc_url"])
                or "http://127.0.0.1:1984"
            )
            streams = self._webrtc_engine.get_available_streams(go2rtc_url)
            selected = str(self._settings.get(["go2rtc_stream"]) or "octocam")
            return self._json_response(
                {
                    "streams": streams,
                    "selected": selected,
                }
            )

        if command == "offer":
            sdp = data.get("sdp", "")
            sdp_type = data.get("type", "offer")
            stream_override = data.get("stream")
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
                    stream_override=stream_override,
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

        if command == "test_go2rtc":
            go2rtc_url = (
                data.get("go2rtc_url")
                or self._settings.get(["go2rtc_url"])
                or "http://127.0.0.1:1984"
            ).strip().rstrip("/")
            stream_name = (
                data.get("go2rtc_stream")
                or self._settings.get(["go2rtc_stream"])
                or "octocam"
            ).strip()
            custom_stream_url = str(self._settings.get(["stream_url"]) or "").strip()
            mjpeg_url = custom_stream_url or resolve_go2rtc_mjpeg_url(go2rtc_url, stream_name)

            available_streams = self._webrtc_engine.get_available_streams(go2rtc_url)
            stream_found = stream_name in available_streams
            if stream_found:
                msg = f"Connected to go2rtc! Stream '{stream_name}' is available."
            elif available_streams:
                msg = (
                    f"Connected to go2rtc, but stream '{stream_name}' was not found. "
                    f"Available streams: {', '.join(available_streams)}"
                )
            else:
                msg = (
                    f"Connected to go2rtc at {go2rtc_url}, but no streams are currently configured."
                )

            return self._json_response(
                {
                    "ok": bool(available_streams),
                    "stream_found": stream_found,
                    "message": msg,
                    "streams": available_streams,
                    "mjpeg_url": mjpeg_url,
                }
            )

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
            go2rtc_url=self._settings.get(["go2rtc_url"]) or "http://127.0.0.1:1984",
            go2rtc_stream=self._settings.get(["go2rtc_stream"]) or "octocam",
            stream_url=self._settings.get(["stream_url"]) or "",
            snapshot_url=self._settings.get(["snapshot_url"]) or "",
            ice_servers=ice_servers,
        )

    def _sync_webcam_stream_url(self) -> None:
        """
        Synchronizes OctoPrint's classic/legacy webcam stream and snapshot URLs
        with go2rtc's MJPEG stream and JPEG snapshot endpoints.
        Allows integrations like Home Assistant to stream the camera.
        """
        if (
            not hasattr(self, "_settings")
            or not hasattr(self._settings, "global_get")
            or not hasattr(self._settings, "global_set")
        ):
            return
        if not bool(self._settings.get_boolean(["sync_octoprint_stream"])):
            return

        go2rtc_url = str(self._settings.get(["go2rtc_url"]) or "http://127.0.0.1:1984").strip()
        stream = str(self._settings.get(["go2rtc_stream"]) or "octocam").strip()
        custom_stream_url = str(self._settings.get(["stream_url"]) or "").strip()
        custom_snapshot_url = str(self._settings.get(["snapshot_url"]) or "").strip()

        mjpeg_url = custom_stream_url or resolve_go2rtc_mjpeg_url(go2rtc_url, stream)
        snap_url = custom_snapshot_url or resolve_go2rtc_snapshot_url(go2rtc_url, stream)

        try:
            changed = False
            if self._settings.global_get(["webcam", "streamUrl"]) != mjpeg_url:
                self._settings.global_set(["webcam", "streamUrl"], mjpeg_url)
                changed = True
            if self._settings.global_get(["plugins", "classicwebcam", "stream"]) != mjpeg_url:
                self._settings.global_set(["plugins", "classicwebcam", "stream"], mjpeg_url)
                changed = True
            if snap_url:
                if self._settings.global_get(["webcam", "snapshotUrl"]) != snap_url:
                    self._settings.global_set(["webcam", "snapshotUrl"], snap_url)
                    changed = True
                if self._settings.global_get(["plugins", "classicwebcam", "snapshot"]) != snap_url:
                    self._settings.global_set(["plugins", "classicwebcam", "snapshot"], snap_url)
                    changed = True

            if changed and hasattr(self._settings, "save"):
                self._settings.save()
                self._logger.info(
                    "[OctoCam] Synchronized OctoPrint webcam stream URL: '%s' and snapshot URL: '%s'",
                    mjpeg_url,
                    snap_url,
                )
        except Exception as exc:
            self._logger.warning("[OctoCam] Failed to sync OctoPrint webcam stream URL: %s", exc)

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
