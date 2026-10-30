/* global OCTOPRINT_VIEWMODELS, OctoPrint, ko, $ */
$(function () {
    function OctocamViewModel(parameters) {
        var self = this;

        self.settingsViewModel = parameters[0];

        // Player state observables
        self.connectionState = ko.observable("idle"); // idle | connecting | connected | error
        self.candidateType = ko.observable(""); // host | srflx | relay | ""
        self.errorMessage = ko.observable("");

        // Snapshot poster preview observables
        self.showPoster = ko.observable(true);
        self.posterUrl = ko.observable("");

        // Multi-stream observables
        self.availableStreams = ko.observableArray([]);
        self.selectedStream = ko.observable("octocam");
        self.isLoadingStreams = ko.observable(false);

        // Settings diagnostic observables
        self.isTestingCloudflare = ko.observable(false);
        self.cloudflareTestOk = ko.observable(false);
        self.cloudflareTestMessage = ko.observable("");

        self.isTestingGo2rtc = ko.observable(false);
        self.go2rtcTestOk = ko.observable(false);
        self.go2rtcTestMessage = ko.observable("");

        // Editable list of static STUN/TURN servers
        self.iceServersList = ko.observableArray([]);

        self.viewportCss = ko.pureComputed(function () {
            var p = self._pluginSettings();
            var ratio = p && p.aspect_ratio ? p.aspect_ratio() : "16:9";
            return ratio === "4:3" ? "ratio-4-3" : "ratio-16-9";
        });

        self.connectionBadgeText = ko.pureComputed(function () {
            var state = self.connectionState();
            var cand = self.candidateType();
            var streamName = self.selectedStream() || "Stream";
            if (state === "connected") {
                var proto = "Direct";
                if (cand === "relay") {
                    proto = "TURN Relay";
                } else if (cand === "srflx" || cand === "prflx") {
                    proto = "STUN";
                }
                return streamName + " (" + proto + ")";
            }
            if (state === "connecting") {
                return "Connecting " + streamName + "...";
            }
            if (state === "error") {
                return self.errorMessage() || (streamName + " Offline");
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

        self.posterNoticeText = ko.pureComputed(function () {
            var state = self.connectionState();
            if (state === "connecting") {
                return "Loading live feed...";
            }
            if (state === "error") {
                return self.errorMessage() || "Stream offline — showing recent snapshot";
            }
            return "Snapshot preview";
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

        // Poster & Snapshot Helpers
        self.refreshPoster = function () {
            var p = self._pluginSettings();
            var stream =
                self.selectedStream() ||
                (p && p.go2rtc_stream ? p.go2rtc_stream() : "octocam");
            self.posterUrl(
                "/plugin/octocam/snapshot?src=" +
                    encodeURIComponent(stream) +
                    "&t=" +
                    Date.now()
            );
            self.showPoster(true);
        };

        self._bindVideoElementListeners = function (videoEl) {
            if (!videoEl || videoEl._octocamBound) {
                return;
            }
            videoEl._octocamBound = true;

            // When the live video track begins rendering frames, smoothly cross-fade out the snapshot poster
            videoEl.addEventListener("playing", function () {
                self.showPoster(false);
            });
            videoEl.addEventListener("loadeddata", function () {
                self.showPoster(false);
            });
            // When stream empties or stalls during reconnect, display the latest snapshot
            videoEl.addEventListener("emptied", function () {
                self.showPoster(true);
            });
            videoEl.addEventListener("waiting", function () {
                if (self.connectionState() !== "connected") {
                    self.showPoster(true);
                }
            });
        };

        // Switch stream listener
        self.selectedStream.subscribe(function () {
            self.refreshPoster();
            if (self._isVisible && self.connectionState() !== "idle") {
                self.reconnectStream();
            }
        });

        // ~~ Stream Discovery Helpers ~~

        self.loadAvailableStreams = function () {
            var p = self._pluginSettings();
            var url = p && p.go2rtc_url ? p.go2rtc_url() : "http://127.0.0.1:1984";
            self.isLoadingStreams(true);

            OctoPrint.simpleApiCommand("octocam", "get_available_streams", {
                go2rtc_url: url
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
                    // Non-fatal if go2rtc isn't reachable yet
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

        self.testGo2rtcConnection = function () {
            var p = self._pluginSettings();
            var url = p && p.go2rtc_url ? p.go2rtc_url() : "http://127.0.0.1:1984";
            var stream = p && p.go2rtc_stream ? p.go2rtc_stream() : "octocam";

            self.isTestingGo2rtc(true);
            self.go2rtcTestMessage("Connecting to go2rtc at " + url + "...");
            self.go2rtcTestOk(true);

            OctoPrint.simpleApiCommand("octocam", "test_go2rtc", {
                go2rtc_url: url,
                go2rtc_stream: stream
            })
                .done(function (response) {
                    self.go2rtcTestOk(Boolean(response.ok));
                    self.go2rtcTestMessage(response.message || "go2rtc connection verified.");
                    if (response.streams && response.streams.length > 0) {
                        self.availableStreams(response.streams);
                    }
                })
                .fail(function (jqXHR) {
                    self.go2rtcTestOk(false);
                    var err =
                        (jqXHR.responseJSON && jqXHR.responseJSON.error) ||
                        "Failed to connect to go2rtc.";
                    self.go2rtcTestMessage(err);
                })
                .always(function () {
                    self.isTestingGo2rtc(false);
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

            self._bindVideoElementListeners(videoEl);
            self.refreshPoster();

            var p = self._pluginSettings();
            var activeStream =
                self.selectedStream() ||
                (p && p.go2rtc_stream ? p.go2rtc_stream() : "octocam");

            OctoPrint.simpleApiCommand("octocam", "get_ice_servers")
                .done(function (iceConfig) {
                    var iceServers = (iceConfig && iceConfig.iceServers) || [
                        { urls: ["stun:stun.cloudflare.com:3478"] }
                    ];
                    var forceRelay = Boolean(iceConfig && iceConfig.forceTurnRelay);
                    self._negotiatePeerConnection(videoEl, iceServers, forceRelay, activeStream);
                })
                .fail(function () {
                    // Fallback to default Cloudflare STUN if endpoint fails
                    self._negotiatePeerConnection(
                        videoEl,
                        [{ urls: ["stun:stun.cloudflare.com:3478"] }],
                        false,
                        activeStream
                    );
                });
        };

        self._negotiatePeerConnection = function (videoEl, iceServers, forceRelay, streamName) {
            var rtcConfig = {
                iceServers: iceServers,
                iceTransportPolicy: forceRelay ? "relay" : "all"
            };

            var pc = new RTCPeerConnection(rtcConfig);
            self._pc = pc;

            pc.addTransceiver("video", { direction: "recvonly" });

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
                    if (videoEl && videoEl.readyState >= 2) {
                        self.showPoster(false);
                    }
                    self._inspectSelectedCandidatePair(pc);
                } else if (st === "failed" || st === "disconnected") {
                    self.connectionState("error");
                    self.errorMessage("Connection lost — reconnecting...");
                    self.showPoster(true);
                    self.refreshPoster();
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
                        type: localDesc.type,
                        stream: streamName
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
            self.showPoster(true);
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
            self.loadAvailableStreams();
        };

        self.onSettingsShown = function () {
            self.loadAvailableStreams();
        };

        self.onSettingsBeforeSave = function () {
            self._syncIceServersToSettings();
        };

        self.onSettingsHidden = function () {
            self.cloudflareTestMessage("");
            self.go2rtcTestMessage("");
        };

        self.onEventSettingsUpdated = function () {
            var p = self._pluginSettings();
            if (p && p.go2rtc_stream) {
                self.selectedStream(p.go2rtc_stream());
            }
            if (self._isVisible) {
                self.reconnectStream();
            }
        };

        self._hideUnconfiguredClassicBanner = function () {
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
