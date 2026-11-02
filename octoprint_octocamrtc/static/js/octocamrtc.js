/* global OCTOPRINT_VIEWMODELS, OctoPrint, ko, $ */
$(function () {
    function OctocamRtcViewModel(parameters) {
        var self = this;

        self.settingsViewModel = parameters[0];

        // Player state observables
        self.connectionState = ko.observable("idle"); // idle | connecting | connected | error
        self.candidateType = ko.observable(""); // host | srflx | relay | ""
        self.errorMessage = ko.observable("");

        // Multi-stream observables
        self.availableStreams = ko.observableArray([]);
        self.selectedStream = ko.observable("octocamrtc");
        self.isLoadingStreams = ko.observable(false);

        // Settings diagnostic observables
        self.isTestingGateway = ko.observable(false);
        self.gatewayTestOk = ko.observable(false);
        self.gatewayTestMessage = ko.observable("");

        // Backward compatibility aliases
        self.isTestingGo2rtc = self.isTestingGateway;
        self.go2rtcTestOk = self.gatewayTestOk;
        self.go2rtcTestMessage = self.gatewayTestMessage;

        // Editable list of static STUN/TURN servers
        self.iceServersList = ko.observableArray([]);

        // Internal tracking
        self._isVisible = false;

        self._pluginSettings = function () {
            if (
                self.settingsViewModel &&
                self.settingsViewModel.settings &&
                self.settingsViewModel.settings.plugins
            ) {
                if (self.settingsViewModel.settings.plugins.octocamrtc) {
                    return self.settingsViewModel.settings.plugins.octocamrtc;
                }
                if (self.settingsViewModel.settings.plugins.octocam) {
                    return self.settingsViewModel.settings.plugins.octocam;
                }
            }
            return null;
        };

        self.viewportCss = ko.pureComputed(function () {
            var p = self._pluginSettings();
            var ratio = p && p.aspect_ratio ? p.aspect_ratio() : "16:9";
            return ratio === "4:3" ? "ratio-4-3" : "ratio-16-9";
        });

        // Gateway Embed iFrame URL computation
        self.iframeSrc = ko.pureComputed(function () {
            var p = self._pluginSettings();
            var gatewayUrl = (p && p.gateway_url && p.gateway_url()) ||
                             (p && p.go2rtc_url && p.go2rtc_url()) ||
                             "http://127.0.0.1:8080";
            var stream = self.selectedStream() ||
                         (p && p.go2rtc_stream ? p.go2rtc_stream() : "octocamrtc");

            var autoplay = (p && p.iframe_autoplay !== undefined) ? Boolean(ko.unwrap(p.iframe_autoplay)) : true;
            var muted = (p && p.iframe_muted !== undefined) ? Boolean(ko.unwrap(p.iframe_muted)) : true;
            var controls = (p && p.iframe_controls !== undefined) ? Boolean(ko.unwrap(p.iframe_controls)) : true;
            var poster = (p && p.iframe_poster !== undefined) ? Boolean(ko.unwrap(p.iframe_poster)) : true;
            var fit = (p && p.iframe_fit !== undefined) ? (ko.unwrap(p.iframe_fit) || "contain") : "contain";
            var reconnect = (p && p.iframe_reconnect !== undefined) ? Boolean(ko.unwrap(p.iframe_reconnect)) : true;

            var base = (gatewayUrl || "").trim().replace(/\/+$/, "");
            if (base.indexOf("/embed") !== -1) {
                base = base.split("/embed")[0];
            } else if (base.indexOf("/api/") !== -1) {
                base = base.split("/api/")[0];
            }

            var params = [
                "stream=" + encodeURIComponent(stream),
                "autoplay=" + (autoplay ? "true" : "false"),
                "muted=" + (muted ? "true" : "false"),
                "controls=" + (controls ? "true" : "false"),
                "poster=" + (poster ? "true" : "false"),
                "fit=" + encodeURIComponent(fit),
                "reconnect=" + (reconnect ? "true" : "false")
            ];

            return base + "/embed?" + params.join("&");
        });

        // iFrame DOM Element Helper
        self._getIframeElement = function () {
            return (
                document.getElementById("octocamrtc_stream_frame") ||
                document.querySelector("#octocamrtc_webcam_container iframe") ||
                document.querySelector(".octocamrtc-stream-frame")
            );
        };

        // PostMessage helper to send commands to the embedded player
        self.sendIframeCommand = function (message) {
            var iframe = self._getIframeElement();
            if (iframe && iframe.contentWindow) {
                iframe.contentWindow.postMessage(message, "*");
            }
        };

        // Player Actions
        self.playStream = function () {
            self.sendIframeCommand({ action: "play" });
        };

        self.pauseStream = function () {
            self.sendIframeCommand({ action: "pause" });
        };

        self.reconnectStream = function () {
            self.sendIframeCommand({ action: "reconnect" });
        };

        self.setStream = function (streamName) {
            if (streamName) {
                self.selectedStream(streamName);
                self.sendIframeCommand({ action: "setStream", stream: streamName });
            }
        };

        self.mute = function () {
            self.sendIframeCommand({ action: "mute" });
        };

        self.unmute = function () {
            self.sendIframeCommand({ action: "unmute" });
        };

        self.toggleFullscreen = function () {
            var iframe = self._getIframeElement();
            if (!iframe) {
                return;
            }
            if (document.fullscreenElement) {
                document.exitFullscreen();
            } else if (iframe.requestFullscreen) {
                iframe.requestFullscreen();
            }
        };

        // Backward compatibility aliases
        self.startStream = self.playStream;
        self.stopStream = self.pauseStream;

        // Switch stream listener
        self.selectedStream.subscribe(function (newStream) {
            if (newStream) {
                self.sendIframeCommand({ action: "setStream", stream: newStream });
            }
        });

        // Listen for player events from iframe
        window.addEventListener("message", function (event) {
            if (event.data && event.data.source === "webrtc-stream-gateway") {
                console.log("[OctoCamRTC] Gateway player event:", event.data.event, event.data);
                var ev = event.data.event;
                if (ev === "connecting") {
                    self.connectionState("connecting");
                } else if (ev === "connected" || ev === "playing") {
                    self.connectionState("connected");
                    self.errorMessage("");
                } else if (ev === "disconnected" || ev === "error") {
                    self.connectionState("error");
                    if (event.data.error || event.data.message) {
                        self.errorMessage(event.data.error || event.data.message);
                    }
                } else if (ev === "paused" || ev === "idle") {
                    self.connectionState("idle");
                }

                if (event.data.candidateType) {
                    self.candidateType(event.data.candidateType);
                }
                if (event.data.stream && event.data.stream !== self.selectedStream()) {
                    self.selectedStream(event.data.stream);
                }
            }
        });

        // ~~ Stream Discovery Helpers ~~

        self.loadAvailableStreams = function () {
            var p = self._pluginSettings();
            var url = (p && p.gateway_url && p.gateway_url()) ||
                      (p && p.go2rtc_url && p.go2rtc_url()) ||
                      "http://127.0.0.1:8080";
            self.isLoadingStreams(true);

            OctoPrint.simpleApiCommand("octocamrtc", "get_available_streams", {
                gateway_url: url
            })
                .done(function (response) {
                    var list = (response && response.streams) || [];
                    self.availableStreams(list);
                    if (list.length > 0) {
                        var currentConfigured = (p && p.go2rtc_stream && p.go2rtc_stream()) || "";
                        if (!currentConfigured || list.indexOf(currentConfigured) === -1) {
                            if (p && p.go2rtc_stream) {
                                p.go2rtc_stream(list[0]);
                            }
                            self.selectedStream(list[0]);
                        } else {
                            self.selectedStream(currentConfigured);
                        }
                    }
                })
                .fail(function () {
                    // Non-fatal if gateway isn't reachable yet
                })
                .always(function () {
                    self.isLoadingStreams(false);
                });
        };

        // ~~ Settings UI Table Helpers ~~

        self._loadIceServersFromSettings = function () {
            var p = self._pluginSettings();
            if (!p || !p.ice_servers) {
                return;
            }
            var raw = ko.unwrap(p.ice_servers) || [];
            var mapped = raw.map(function (entry) {
                var urls = ko.unwrap(entry.urls);
                if (Array.isArray(urls)) {
                    urls = urls.join(", ");
                }
                return {
                    urls: ko.observable(urls || ""),
                    username: ko.observable(ko.unwrap(entry.username) || ""),
                    credential: ko.observable(ko.unwrap(entry.credential) || "")
                };
            });
            self.iceServersList(mapped);
        };

        self._syncIceServersToSettings = function () {
            var p = self._pluginSettings();
            if (!p || !p.ice_servers) {
                return;
            }
            var serialized = self.iceServersList().map(function (item) {
                return {
                    urls: (item.urls() || "").trim(),
                    username: (item.username() || "").trim(),
                    credential: (item.credential() || "").trim()
                };
            }).filter(function (item) {
                return item.urls.length > 0;
            });
            p.ice_servers(serialized);
        };

        self.addIceServer = function () {
            self.iceServersList.push({
                urls: ko.observable("stun:stun.cloudflare.com:3478"),
                username: ko.observable(""),
                credential: ko.observable("")
            });
        };

        self.removeIceServer = function (item) {
            self.iceServersList.remove(item);
        };

        // ~~ Settings Diagnostic Actions ~~

        self.testGatewayConnection = function () {
            var p = self._pluginSettings();
            var url = (p && p.gateway_url && p.gateway_url()) ||
                      (p && p.go2rtc_url && p.go2rtc_url()) ||
                      "http://127.0.0.1:8080";
            var stream = (p && p.go2rtc_stream && p.go2rtc_stream()) || "octocamrtc";

            self.isTestingGateway(true);
            self.gatewayTestMessage("Connecting to WebRTC Stream Gateway at " + url + "...");
            self.gatewayTestOk(true);

            OctoPrint.simpleApiCommand("octocamrtc", "test_gateway", {
                gateway_url: url,
                go2rtc_stream: stream
            })
                .done(function (response) {
                    self.gatewayTestOk(Boolean(response.ok));
                    self.gatewayTestMessage(response.message || "WebRTC Stream Gateway connection verified.");
                    if (response.streams && response.streams.length > 0) {
                        self.availableStreams(response.streams);
                    }
                })
                .fail(function (jqXHR) {
                    self.gatewayTestOk(false);
                    var err =
                        (jqXHR.responseJSON && jqXHR.responseJSON.error) ||
                        (jqXHR.responseJSON && jqXHR.responseJSON.message) ||
                        "Failed to connect to WebRTC Stream Gateway.";
                    self.gatewayTestMessage(err);
                })
                .always(function () {
                    self.isTestingGateway(false);
                });
        };

        // Backward compatibility alias
        self.testGo2rtcConnection = self.testGatewayConnection;

        // ~~ OctoPrint Lifecycle Hooks ~~

        self.onBeforeBinding = function () {
            self._loadIceServersFromSettings();
            self.loadAvailableStreams();
        };

        self.onSettingsShown = function () {
            self.loadAvailableStreams();
        };

        self.onSettingsBeforeSave = function () {
            self._syncIceServersToSettings();
        };

        self.onSettingsHidden = function () {
            self.gatewayTestMessage("");
        };

        self.onEventSettingsUpdated = function () {
            var p = self._pluginSettings();
            if (p && p.go2rtc_stream) {
                self.selectedStream(p.go2rtc_stream());
            }
            if (self._isVisible) {
                self.playStream();
            }
        };

        self._hideUnconfiguredClassicBanner = function () {
            var classicContainer = document.getElementById("webcam_plugin_classicwebcam");
            if (classicContainer && !classicContainer.querySelector("img, video, iframe")) {
                classicContainer.style.display = "none";
            }
            $("#webcam_container p, #control p").each(function () {
                var txt = ($(this).text() || "").trim();
                if (txt.indexOf("Webcam not configured") !== -1) {
                    $(this).closest("div").hide();
                }
            });
        };

        // OctoPrint 1.9+ WebcamProviderPlugin visibility callback
        self.onWebcamVisibilityChange = function (visible) {
            self._isVisible = Boolean(visible);
            if (visible) {
                self._hideUnconfiguredClassicBanner();
                self.playStream();
            } else {
                self.pauseStream();
            }
        };

        // Fallback tab change detection if rendered directly on Control tab
        self.onAfterTabChange = function (current) {
            if (current === "#control") {
                self._isVisible = true;
                self._hideUnconfiguredClassicBanner();
                self.playStream();
            } else if (self._isVisible && current !== "#control") {
                self._isVisible = false;
                self.pauseStream();
            }
        };
    }

    var OctocamViewModel = OctocamRtcViewModel;

    OCTOPRINT_VIEWMODELS.push({
        construct: OctocamRtcViewModel,
        dependencies: ["settingsViewModel"],
        elements: [
            "#settings_plugin_octocamrtc",
            "#webcam_plugin_octocamrtc",
            "#settings_plugin_octocam",
            "#webcam_plugin_octocam"
        ]
    });
});
