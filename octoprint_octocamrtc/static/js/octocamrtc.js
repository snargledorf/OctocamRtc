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

        // Gateway URL computed observable
        self.gatewayUrl = ko.pureComputed(function () {
            var p = self._pluginSettings();
            var raw = (p && p.gateway_url && p.gateway_url()) ||
                      (p && p.go2rtc_url && p.go2rtc_url()) ||
                      "http://127.0.0.1:8080";
            return (raw || "").trim().replace(/\/+$/, "");
        });

        // HTML Boolean attribute helpers for <webrtc-stream>
        self.autoplayAttr = ko.pureComputed(function () {
            var p = self._pluginSettings();
            var val = (p && p.player_autoplay !== undefined) ? ko.unwrap(p.player_autoplay) :
                      (p && p.iframe_autoplay !== undefined) ? ko.unwrap(p.iframe_autoplay) : true;
            return Boolean(val) ? "" : null;
        });

        self.mutedAttr = ko.pureComputed(function () {
            var p = self._pluginSettings();
            var val = (p && p.player_muted !== undefined) ? ko.unwrap(p.player_muted) :
                      (p && p.iframe_muted !== undefined) ? ko.unwrap(p.iframe_muted) : true;
            return Boolean(val) ? "" : null;
        });

        self.controlsAttr = ko.pureComputed(function () {
            var p = self._pluginSettings();
            var val = (p && p.player_controls !== undefined) ? ko.unwrap(p.player_controls) :
                      (p && p.iframe_controls !== undefined) ? ko.unwrap(p.iframe_controls) : true;
            return Boolean(val) ? "" : null;
        });

        self.posterAttr = ko.pureComputed(function () {
            var p = self._pluginSettings();
            var val = (p && p.player_poster !== undefined) ? ko.unwrap(p.player_poster) :
                      (p && p.iframe_poster !== undefined) ? ko.unwrap(p.iframe_poster) : true;
            return Boolean(val) ? "" : null;
        });

        // Backward compatibility computed for iframe URL
        self.iframeSrc = ko.pureComputed(function () {
            var base = self.gatewayUrl();
            if (base.indexOf("/embed") !== -1) {
                base = base.split("/embed")[0];
            } else if (base.indexOf("/api/") !== -1) {
                base = base.split("/api/")[0];
            }
            return base + "/embed?stream=" + encodeURIComponent(self.selectedStream() || "octocamrtc");
        });

        // Web Component script loader
        self.ensureWebComponentScript = function (gatewayUrl) {
            var scriptId = "octocamrtc_webrtc_stream_script";
            var existing = document.getElementById(scriptId);
            var base = (gatewayUrl || "http://127.0.0.1:8080").replace(/\/+$/, "");
            if (base.indexOf("/api/") !== -1) {
                base = base.split("/api/")[0];
            } else if (base.indexOf("/embed") !== -1) {
                base = base.split("/embed")[0];
            }
            var targetUrl = base + "/webrtc-stream.js";
            if (existing) {
                if (existing.src === targetUrl) {
                    return;
                }
                if (existing.parentNode) {
                    existing.parentNode.removeChild(existing);
                }
            }
            var script = document.createElement("script");
            script.id = scriptId;
            script.src = targetUrl;
            script.async = true;
            document.head.appendChild(script);
        };

        // <webrtc-stream> DOM Element Helper
        self._getPlayerElement = function () {
            return (
                document.getElementById("octocamrtc_player_element") ||
                document.querySelector("webrtc-stream")
            );
        };

        // Backward compatibility alias
        self._getIframeElement = self._getPlayerElement;

        // Custom Element Event Listeners
        self._bindPlayerEvents = function (playerEl) {
            if (!playerEl || playerEl._octocamrtcBound) {
                return;
            }
            playerEl._octocamrtcBound = true;

            playerEl.addEventListener("webrtc:connected", function (e) {
                console.log("[OctoCamRTC] Stream connected:", e.detail && e.detail.stream);
                self.connectionState("connected");
                self.errorMessage("");
            });

            playerEl.addEventListener("webrtc:disconnected", function () {
                console.log("[OctoCamRTC] Stream disconnected");
                self.connectionState("error");
                self.errorMessage("Stream disconnected");
            });

            playerEl.addEventListener("webrtc:error", function (e) {
                console.error("[OctoCamRTC] Stream error:", e.detail);
                self.connectionState("error");
                var msg = (e.detail && (e.detail.message || e.detail.error)) || "Stream error";
                self.errorMessage(typeof msg === "string" ? msg : JSON.stringify(msg));
            });

            playerEl.addEventListener("webrtc:statuschange", function (e) {
                console.log("[OctoCamRTC] Status changed:", e.detail && e.detail.status);
                var status = e.detail && e.detail.status;
                if (status === "connecting") {
                    self.connectionState("connecting");
                } else if (status === "connected" || status === "playing") {
                    self.connectionState("connected");
                    self.errorMessage("");
                } else if (status === "disconnected" || status === "failed" || status === "error") {
                    self.connectionState("error");
                } else if (status === "idle" || status === "paused") {
                    self.connectionState("idle");
                }
            });
        };

        // Player Actions
        self.playStream = function () {
            var el = self._getPlayerElement();
            if (el) {
                self._bindPlayerEvents(el);
                if (typeof el.play === "function") {
                    el.play();
                }
            }
        };

        self.pauseStream = function () {
            var el = self._getPlayerElement();
            if (el && typeof el.pause === "function") {
                el.pause();
            }
        };

        self.reconnectStream = function () {
            var el = self._getPlayerElement();
            if (el) {
                self._bindPlayerEvents(el);
                if (typeof el.reconnect === "function") {
                    el.reconnect();
                }
            }
        };

        self.captureSnapshot = function () {
            var el = self._getPlayerElement();
            if (el && typeof el.captureSnapshot === "function") {
                return el.captureSnapshot();
            }
            return Promise.reject(new Error("captureSnapshot not supported"));
        };

        self.setStream = function (streamName) {
            if (streamName) {
                self.selectedStream(streamName);
                var el = self._getPlayerElement();
                if (el) {
                    el.setAttribute("stream", streamName);
                    if ("stream" in el) {
                        el.stream = streamName;
                    }
                }
            }
        };

        self.toggleFullscreen = function () {
            var el = self._getPlayerElement();
            if (!el) {
                return;
            }
            if (document.fullscreenElement) {
                document.exitFullscreen();
            } else if (el.requestFullscreen) {
                el.requestFullscreen();
            }
        };

        // Backward compatibility aliases
        self.startStream = self.playStream;
        self.stopStream = self.pauseStream;

        // Switch stream listener
        self.selectedStream.subscribe(function (newStream) {
            if (newStream) {
                var el = self._getPlayerElement();
                if (el) {
                    el.setAttribute("stream", newStream);
                    if ("stream" in el) {
                        el.stream = newStream;
                    }
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
            self.ensureWebComponentScript(self.gatewayUrl());
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
            self.ensureWebComponentScript(self.gatewayUrl());
            if (self._isVisible) {
                self.playStream();
            }
        };

        self._hideUnconfiguredClassicBanner = function () {
            var classicContainer = document.getElementById("webcam_plugin_classicwebcam");
            if (classicContainer && !classicContainer.querySelector("img, video, iframe, webrtc-stream")) {
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
                self.ensureWebComponentScript(self.gatewayUrl());
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
                self.ensureWebComponentScript(self.gatewayUrl());
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
