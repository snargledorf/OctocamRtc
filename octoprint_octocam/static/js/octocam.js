/* global OCTOPRINT_VIEWMODELS, OctoPrint, ko, $ */
$(function () {
    function OctocamViewModel(parameters) {
        var self = this;

        self.settingsViewModel = parameters[0];

        // Player state observables
        self.connectionState = ko.observable("idle"); // idle | connecting | connected | error
        self.candidateType = ko.observable(""); // host | srflx | relay | ""
        self.errorMessage = ko.observable("");

        // Settings diagnostic observables
        self.isTestingCloudflare = ko.observable(false);
        self.cloudflareTestOk = ko.observable(false);
        self.cloudflareTestMessage = ko.observable("");

        self.isCheckingVaapi = ko.observable(false);
        self.vaapiStatusOk = ko.observable(false);
        self.vaapiStatusMessage = ko.observable("");

        // Editable list of static STUN/TURN servers
        self.iceServersList = ko.observableArray([]);

        // Orientation observables synced from plugin settings
        self.flipH = ko.pureComputed(function () {
            var p = self._pluginSettings();
            return p && p.flip_h ? Boolean(p.flip_h()) : false;
        });

        self.flipV = ko.pureComputed(function () {
            var p = self._pluginSettings();
            return p && p.flip_v ? Boolean(p.flip_v()) : false;
        });

        self.rotate90 = ko.pureComputed(function () {
            var p = self._pluginSettings();
            return p && p.rotate90 ? Boolean(p.rotate90()) : false;
        });

        self.viewportCss = ko.pureComputed(function () {
            var p = self._pluginSettings();
            var ratio = p && p.aspect_ratio ? p.aspect_ratio() : "16:9";
            return ratio === "4:3" ? "ratio-4-3" : "ratio-16-9";
        });

        self.connectionBadgeText = ko.pureComputed(function () {
            var state = self.connectionState();
            var cand = self.candidateType();
            if (state === "connected") {
                if (cand === "relay") {
                    return "Live (TURN Relay)";
                }
                if (cand === "srflx" || cand === "prflx") {
                    return "Live (STUN)";
                }
                return "Live (Direct)";
            }
            if (state === "connecting") {
                return "Connecting WebRTC...";
            }
            if (state === "error") {
                return self.errorMessage() || "Stream Offline";
            }
            return "Idle";
        });

        self.statusBadgeClass = ko.pureComputed(function () {
            var state = self.connectionState();
            if (state === "connected") {
                return "badge-connected";
            }
            if (state === "connecting") {
                return "badge-connecting";
            }
            if (state === "error") {
                return "badge-error";
            }
            return "badge-idle";
        });

        // Internal WebRTC runtime fields
        self._pc = null;
        self._reconnectTimer = null;
        self._statsTimer = null;
        self._reconnectAttempt = 0;
        self._isVisible = false;

        self._pluginSettings = function () {
            if (
                self.settingsViewModel &&
                self.settingsViewModel.settings &&
                self.settingsViewModel.settings.plugins &&
                self.settingsViewModel.settings.plugins.octocam
            ) {
                return self.settingsViewModel.settings.plugins.octocam;
            }
            return null;
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

        self.testCloudflareTurn = function () {
            var p = self._pluginSettings();
            if (!p) {
                return;
            }
            self.isTestingCloudflare(true);
            self.cloudflareTestMessage("Requesting short-lived credentials from Cloudflare Calls TURN API...");
            self.cloudflareTestOk(true);

            OctoPrint.simpleApiCommand("octocam", "test_cloudflare", {
                turn_key_id: p.cloudflare_turn_key_id(),
                api_token: p.cloudflare_api_token(),
                ttl: Number(p.cloudflare_ttl() || 3600)
            })
                .done(function (response) {
                    self.cloudflareTestOk(true);
                    var count = (response.iceServers && response.iceServers.length) || 0;
                    self.cloudflareTestMessage(
                        (response.message || "Success!") + " (" + count + " ICE server entry generated)"
                    );
                })
                .fail(function (jqXHR) {
                    self.cloudflareTestOk(false);
                    var err =
                        (jqXHR.responseJSON && jqXHR.responseJSON.error) ||
                        jqXHR.statusText ||
                        "Failed to generate Cloudflare TURN credentials.";
                    self.cloudflareTestMessage(err);
                })
                .always(function () {
                    self.isTestingCloudflare(false);
                });
        };

        self.checkVaapiStatus = function () {
            var p = self._pluginSettings();
            var device = p && p.vaapi_device ? p.vaapi_device() : "/dev/dri/renderD128";
            self.isCheckingVaapi(true);
            self.vaapiStatusMessage("Probing Linux VAAPI hardware encoder on " + device + "...");
            self.vaapiStatusOk(true);

            OctoPrint.simpleApiCommand("octocam", "check_vaapi", {
                vaapi_device: device
            })
                .done(function (response) {
                    self.vaapiStatusOk(Boolean(response.available));
                    self.vaapiStatusMessage(response.message || "VAAPI check completed.");
                })
                .fail(function (jqXHR) {
                    self.vaapiStatusOk(false);
                    var err =
                        (jqXHR.responseJSON && jqXHR.responseJSON.error) ||
                        "Failed to query VAAPI hardware status.";
                    self.vaapiStatusMessage(err);
                })
                .always(function () {
                    self.isCheckingVaapi(false);
                });
        };

        // ~~ WebRTC Player Lifecycle ~~

        self.reconnectStream = function () {
            self._reconnectAttempt = 0;
            self.stopStream();
            self.startStream();
        };

        self.toggleFullscreen = function () {
            var videoEl = document.getElementById("octocam_webrtc_video");
            if (!videoEl) {
                return;
            }
            if (document.fullscreenElement) {
                document.exitFullscreen();
            } else if (videoEl.requestFullscreen) {
                videoEl.requestFullscreen();
            }
        };

        self.startStream = function () {
            var videoEl = document.getElementById("octocam_webrtc_video");
            if (!videoEl) {
                return;
            }
            if (self._pc) {
                return;
            }

            self._clearTimers();
            self.connectionState("connecting");
            self.candidateType("");
            self.errorMessage("");

            OctoPrint.simpleApiCommand("octocam", "get_ice_servers")
                .done(function (iceConfig) {
                    var iceServers = (iceConfig && iceConfig.iceServers) || [
                        { urls: ["stun:stun.cloudflare.com:3478"] }
                    ];
                    var forceRelay = Boolean(iceConfig && iceConfig.forceTurnRelay);
                    self._negotiatePeerConnection(videoEl, iceServers, forceRelay);
                })
                .fail(function () {
                    // Fallback to default Cloudflare STUN if endpoint fails
                    self._negotiatePeerConnection(
                        videoEl,
                        [{ urls: ["stun:stun.cloudflare.com:3478"] }],
                        false
                    );
                });
        };

        self._negotiatePeerConnection = function (videoEl, iceServers, forceRelay) {
            var rtcConfig = {
                iceServers: iceServers,
                iceTransportPolicy: forceRelay ? "relay" : "all"
            };

            var pc = new RTCPeerConnection(rtcConfig);
            self._pc = pc;

            var transceiver = pc.addTransceiver("video", { direction: "recvonly" });
            if (
                transceiver &&
                typeof transceiver.setCodecPreferences === "function" &&
                typeof RTCRtpReceiver !== "undefined" &&
                typeof RTCRtpReceiver.getCapabilities === "function"
            ) {
                try {
                    var caps = RTCRtpReceiver.getCapabilities("video");
                    if (caps && Array.isArray(caps.codecs)) {
                        var h264Codecs = [];
                        var otherCodecs = [];
                        caps.codecs.forEach(function (c) {
                            var mime = (c.mimeType || "").toLowerCase();
                            if (mime === "video/h264") {
                                h264Codecs.push(c);
                            } else {
                                otherCodecs.push(c);
                            }
                        });
                        if (h264Codecs.length > 0) {
                            transceiver.setCodecPreferences(h264Codecs.concat(otherCodecs));
                        }
                    }
                } catch (e) {
                    // Ignore if browser rejects codec preference ordering
                }
            }

            pc.ontrack = function (event) {
                if (event.track.kind === "video" && event.streams && event.streams[0]) {
                    videoEl.srcObject = event.streams[0];
                }
            };

            pc.onconnectionstatechange = function () {
                if (self._pc !== pc) {
                    return;
                }
                var st = pc.connectionState;
                if (st === "connected") {
                    self.connectionState("connected");
                    self._reconnectAttempt = 0;
                    self._inspectSelectedCandidatePair(pc);
                } else if (st === "failed" || st === "disconnected") {
                    self.connectionState("error");
                    self.errorMessage("Connection lost — reconnecting...");
                    self._scheduleReconnect();
                } else if (st === "closed") {
                    self.connectionState("idle");
                }
            };

            pc.createOffer()
                .then(function (offer) {
                    return pc.setLocalDescription(offer);
                })
                .then(function () {
                    return self._waitForIceGathering(pc, 2500);
                })
                .then(function () {
                    var localDesc = pc.localDescription;
                    return OctoPrint.simpleApiCommand("octocam", "offer", {
                        sdp: localDesc.sdp,
                        type: localDesc.type
                    });
                })
                .then(function (answer) {
                    if (self._pc !== pc) {
                        return;
                    }
                    return pc.setRemoteDescription(new RTCSessionDescription(answer));
                })
                .catch(function (err) {
                    if (self._pc !== pc) {
                        return;
                    }
                    self.connectionState("error");
                    var msg =
                        (err && err.responseJSON && err.responseJSON.error) ||
                        (err && err.message) ||
                        "WebRTC negotiation failed";
                    self.errorMessage(msg);
                    self._scheduleReconnect();
                });
        };

        self._waitForIceGathering = function (pc, timeoutMs) {
            if (pc.iceGatheringState === "complete") {
                return Promise.resolve();
            }
            return new Promise(function (resolve) {
                var timer = setTimeout(function () {
                    pc.removeEventListener("icegatheringstatechange", checkState);
                    resolve();
                }, timeoutMs || 2500);

                function checkState() {
                    if (pc.iceGatheringState === "complete") {
                        clearTimeout(timer);
                        pc.removeEventListener("icegatheringstatechange", checkState);
                        resolve();
                    }
                }

                pc.addEventListener("icegatheringstatechange", checkState);
            });
        };

        self._inspectSelectedCandidatePair = function (pc) {
            if (!pc || !pc.getStats) {
                return;
            }
            self._statsTimer = setTimeout(function () {
                if (self._pc !== pc) {
                    return;
                }
                pc.getStats()
                    .then(function (stats) {
                        var selectedLocalCandidateId = null;
                        var selectedRemoteCandidateId = null;
                        stats.forEach(function (report) {
                            if (report.type === "transport" && report.selectedCandidatePairId) {
                                var pair = stats.get(report.selectedCandidatePairId);
                                if (pair) {
                                    selectedLocalCandidateId = pair.localCandidateId;
                                    selectedRemoteCandidateId = pair.remoteCandidateId;
                                }
                            } else if (
                                report.type === "candidate-pair" &&
                                report.state === "succeeded" &&
                                report.nominated
                            ) {
                                selectedLocalCandidateId = report.localCandidateId;
                                selectedRemoteCandidateId = report.remoteCandidateId;
                            }
                        });

                        var localCand = selectedLocalCandidateId
                            ? stats.get(selectedLocalCandidateId)
                            : null;
                        var remoteCand = selectedRemoteCandidateId
                            ? stats.get(selectedRemoteCandidateId)
                            : null;

                        if (
                            (localCand && localCand.candidateType === "relay") ||
                            (remoteCand && remoteCand.candidateType === "relay")
                        ) {
                            self.candidateType("relay");
                        } else if (
                            (localCand && localCand.candidateType === "srflx") ||
                            (remoteCand && remoteCand.candidateType === "srflx")
                        ) {
                            self.candidateType("srflx");
                        } else if (localCand && localCand.candidateType) {
                            self.candidateType(localCand.candidateType);
                        }
                    })
                    .catch(function () {
                        // Ignore stats errors on older browsers
                    });
            }, 600);
        };

        self._scheduleReconnect = function () {
            if (!self._isVisible) {
                return;
            }
            self._clearTimers();
            var delay = Math.min(15000, 2000 * Math.pow(1.5, self._reconnectAttempt));
            self._reconnectAttempt += 1;
            self._reconnectTimer = setTimeout(function () {
                if (self._isVisible) {
                    self.stopStream();
                    self.startStream();
                }
            }, delay);
        };

        self.stopStream = function () {
            self._clearTimers();
            if (self._pc) {
                try {
                    self._pc.close();
                } catch (e) {
                    // Ignore close error
                }
                self._pc = null;
            }
            var videoEl = document.getElementById("octocam_webrtc_video");
            if (videoEl && videoEl.srcObject) {
                videoEl.srcObject = null;
            }
            self.connectionState("idle");
            self.candidateType("");
        };

        self._clearTimers = function () {
            if (self._reconnectTimer) {
                clearTimeout(self._reconnectTimer);
                self._reconnectTimer = null;
            }
            if (self._statsTimer) {
                clearTimeout(self._statsTimer);
                self._statsTimer = null;
            }
        };

        // ~~ OctoPrint Lifecycle Hooks ~~

        self.onBeforeBinding = function () {
            self._loadIceServersFromSettings();
        };

        self.onSettingsBeforeSave = function () {
            self._syncIceServersToSettings();
        };

        self.onSettingsHidden = function () {
            self.cloudflareTestMessage("");
            self.vaapiStatusMessage("");
        };

        self.onEventSettingsUpdated = function () {
            if (self._isVisible) {
                self.reconnectStream();
            }
        };

        self._hideUnconfiguredClassicBanner = function () {
            // If Classic Webcam is unconfigured and showing its "? Webcam not configured..."
            // placeholder alongside OctoCam, hide that placeholder container.
            var classicContainer = document.getElementById("webcam_plugin_classicwebcam");
            if (classicContainer && !classicContainer.querySelector("img, video")) {
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
                self.startStream();
            } else {
                self.stopStream();
            }
        };

        // Fallback tab change detection if rendered directly on Control tab
        self.onAfterTabChange = function (current) {
            if (current === "#control") {
                self._isVisible = true;
                self._hideUnconfiguredClassicBanner();
                self.startStream();
            } else if (self._isVisible && current !== "#control") {
                self._isVisible = false;
                self.stopStream();
            }
        };
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: OctocamViewModel,
        dependencies: ["settingsViewModel"],
        elements: ["#settings_plugin_octocam", "#webcam_plugin_octocam"]
    });
});
