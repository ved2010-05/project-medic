/**
 * app.js — shared front-end logic for the MEDIC "Hospital Central" dashboard.
 *
 * One file, loaded on every page (see base.html). Each screen's setup
 * function only runs if that page's root element is present in the DOM, so
 * this file is safe to include everywhere without extra <script> plumbing.
 *
 * Talks ONLY to the frozen endpoints in docs/http-api-v1.md (browser-facing
 * table). No frameworks, no CDN — plain fetch() + DOM updates, per the
 * project's "runs on our own hotspot, no internet at demo time" rule.
 *
 * RULES THIS FILE FOLLOWS (see ARCHITECTURE.md + dashboard task brief):
 *   - Never blank the screen on a failed fetch. Keep showing the last good
 *     data and flip on the offline banner instead.
 *   - Keep polling forever. A dropped Wi-Fi frame is expected, not fatal.
 *   - This file makes NO decisions about safety or auth — it only displays
 *     what the server (app.py) tells it and posts what a human clicked.
 *
 * BENCH TEST: open any page in a browser with the Flask app NOT running.
 * The offline banner should appear within ~2 failed polls, the page must
 * stay visible (not blank), and it should recover automatically once the
 * server is started (no page reload needed).
 */

(function () {
    "use strict";

    // ------------------------------------------------------------------
    // 0. Small shared constants
    // ------------------------------------------------------------------

    // Default robot id used to pre-fill inputs before any robot has ever
    // reported in. Matches medic.common.DEFAULTS["robot_id"] on the Pi side
    // — see pi-deploy/medic/common.py. Purely a UI convenience; the server
    // is the source of truth for which robots actually exist.
    var DEFAULT_ROBOT_ID = "medic-01";

    // Poll cadences per screen. Kept independent of the Pi<->dashboard poll
    // interval (500 ms, medic.common.DEFAULTS["poll_interval"]) — a human
    // staring at a browser does not need sub-second refresh, and a slower
    // browser poll is kinder to the Pi's Flask process during a demo.
    var POLL_FLEET_MS = 1500;
    var POLL_AUDIT_MS = 1500;
    var POLL_TEMP_MS = 5000;
    var POLL_TASKS_MS = 1500;
    var POLL_TELEOP_MS = 1000;
    var POLL_CONFIG_MS = 4000;

    // Canonical event kinds + severities the dashboard understands, mirrored
    // from docs/http-api-v1.md so the audit page can label anything the
    // server sends even if the vocabulary grows later.
    var SEVERITY = {
        robot_online: "info", robot_offline: "warn", dispatch: "info",
        depart: "info", marker_seen: "info", marker_lost: "red",
        station_mismatch: "red", arrive: "info", scan_staff: "info",
        scan_patient: "info", auth_ok: "info", auth_refused: "red",
        auth_timeout: "red", dispense_ok: "info", dispense_fail: "red",
        latch_open: "info", latch_close: "info", temp_reading: "info",
        temp_excursion: "warn", sound_alert: "warn", obstacle_hold: "warn",
        estop: "red", safehold_comms: "red", teleop_nudge: "warn",
        task_complete: "info"
    };

    // Overrides that mean "something is actively wrong or overriding the
    // robot's own plan" vs. just "a human is driving it right now."
    var ALARM_OVERRIDES = { OBSTACLE_HOLD: true, SAFEHOLD_COMMS: true, ESTOPPED: true };

    var TASK_STATES = [
        "dispatched", "en_route", "arrived", "awaiting_auth",
        "dispensing", "complete"
    ];
    var TASK_STATES_BAD = { refused: true, aborted: true };

    // ------------------------------------------------------------------
    // 1. Fetch helpers — never throw, always feed the offline banner
    // ------------------------------------------------------------------

    var failStreak = 0;
    var lastOkAt = null; // Date.now() of the last successful call, or null

    function markSuccess() {
        failStreak = 0;
        lastOkAt = Date.now();
        setConnState("ok");
    }

    function markFailure() {
        failStreak += 1;
        // Require two misses in a row before we alarm the room — a single
        // dropped poll on Wi-Fi is normal and not worth a banner flash.
        if (failStreak >= 2) {
            setConnState(failStreak >= 6 ? "down" : "stale");
        }
    }

    function setConnState(state) {
        var dot = document.getElementById("conn-dot");
        var text = document.getElementById("conn-text");
        var banner = document.getElementById("offline-banner");
        if (dot) {
            dot.classList.remove("ok", "stale", "down");
            dot.classList.add(state);
        }
        if (text) {
            text.textContent =
                state === "ok" ? "Connected" :
                state === "stale" ? "Connection slow…" :
                "Dashboard unreachable";
        }
        if (banner) {
            if (state === "down") {
                banner.classList.remove("hidden");
            } else {
                banner.classList.add("hidden");
            }
        }
    }

    /** GET JSON. Returns the parsed body, or null on any failure. Never throws. */
    function getJSON(url) {
        return fetch(url, { method: "GET", cache: "no-store" })
            .then(function (resp) {
                if (!resp.ok) { throw new Error("HTTP " + resp.status); }
                return resp.json();
            })
            .then(function (data) { markSuccess(); return data; })
            .catch(function (err) {
                markFailure();
                console.warn("GET failed:", url, err);
                return null;
            });
    }

    /** POST JSON. Returns the parsed body, or null on any failure. Never throws. */
    function postJSON(url, body) {
        return fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body || {})
        })
            .then(function (resp) {
                if (!resp.ok) { throw new Error("HTTP " + resp.status); }
                return resp.json();
            })
            .then(function (data) { markSuccess(); return data; })
            .catch(function (err) {
                markFailure();
                console.warn("POST failed:", url, err);
                return null;
            });
    }

    /** Run fn() now, then every ms — fn may return a Promise; overlap-safe. */
    function poll(fn, ms) {
        var running = false;
        function tick() {
            if (running) { return; } // never stack up overlapping polls
            running = true;
            Promise.resolve(fn()).finally(function () { running = false; });
        }
        tick();
        setInterval(tick, ms);
    }

    // ------------------------------------------------------------------
    // 2. Small formatting / DOM helpers
    // ------------------------------------------------------------------

    function esc(s) {
        if (s === null || s === undefined) { return ""; }
        return String(s)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
    }

    function el(tag, attrs, children) {
        var node = document.createElement(tag);
        attrs = attrs || {};
        Object.keys(attrs).forEach(function (k) {
            if (k === "class") { node.className = attrs[k]; }
            else if (k === "text") { node.textContent = attrs[k]; }
            else { node.setAttribute(k, attrs[k]); }
        });
        (children || []).forEach(function (c) { node.appendChild(c); });
        return node;
    }

    /** Parse a timestamp (ISO string, or already-numeric ms/seconds) to a Date, or null. */
    function toDate(ts) {
        if (ts === null || ts === undefined || ts === "") { return null; }
        if (typeof ts === "number") {
            // Heuristic: values under ~10 billion are seconds, not ms.
            return new Date(ts < 1e11 ? ts * 1000 : ts);
        }
        var d = new Date(ts);
        return isNaN(d.getTime()) ? null : d;
    }

    /** "3s ago" / "2m 4s ago" / "1h 12m ago", for last-seen / timestamps. */
    function fmtAge(ts) {
        var d = toDate(ts);
        if (!d) { return "never"; }
        var secs = Math.max(0, Math.round((Date.now() - d.getTime()) / 1000));
        if (secs < 60) { return secs + "s ago"; }
        var mins = Math.floor(secs / 60);
        if (mins < 60) { return mins + "m " + (secs % 60) + "s ago"; }
        var hrs = Math.floor(mins / 60);
        return hrs + "h " + (mins % 60) + "m ago";
    }

    /** Wall-clock time for a table cell, local time, falls back to raw text. */
    function fmtClock(ts) {
        var d = toDate(ts);
        if (!d) { return ts ? esc(ts) : "—"; }
        return d.toLocaleTimeString([], { hour12: false });
    }

    function severityFor(kind, given) {
        return given || SEVERITY[kind] || "info";
    }

    function severityBadge(sev) {
        var cls = sev === "red" ? "badge-red badge-strong" :
                   sev === "warn" ? "badge-warn" : "badge-info";
        return '<span class="badge ' + cls + '">' + esc(sev.toUpperCase()) + "</span>";
    }

    // ------------------------------------------------------------------
    // 3. Robot-id inputs — populated (best-effort) from /api/v1/fleet, but
    //    always usable as free text so a page never gets stuck just
    //    because the fleet endpoint hasn't answered yet.
    // ------------------------------------------------------------------

    var fleetIdsCache = [];

    function refreshRobotIdCache() {
        return getJSON("/api/v1/fleet").then(function (data) {
            if (data && Array.isArray(data.robots)) {
                fleetIdsCache = data.robots.map(function (r) { return r.robot_id; }).filter(Boolean);
                document.querySelectorAll(".robot-id-list").forEach(function (dl) {
                    dl.innerHTML = "";
                    fleetIdsCache.forEach(function (id) {
                        dl.appendChild(el("option", { value: id }));
                    });
                });
            }
            return data;
        });
    }

    /** First known robot id, or the shared default if none have reported yet. */
    function firstRobotId() {
        return fleetIdsCache.length ? fleetIdsCache[0] : DEFAULT_ROBOT_ID;
    }

    // ------------------------------------------------------------------
    // 4. Nav highlighting — compares the current path to each link's href,
    //    so templates don't need any server-side "active page" plumbing.
    // ------------------------------------------------------------------

    function highlightNav() {
        var path = window.location.pathname.replace(/\/+$/, "") || "/";
        document.querySelectorAll(".topbar nav a").forEach(function (a) {
            var href = a.getAttribute("href").replace(/\/+$/, "") || "/";
            if (href === path) { a.classList.add("active"); }
        });
    }

    // ------------------------------------------------------------------
    // 5. Fleet page
    // ------------------------------------------------------------------

    function initFleet() {
        var root = document.getElementById("fleet-root");
        if (!root) { return; }

        function stateBadgeClass(state) {
            var s = String(state || "").toUpperCase();
            if (s.indexOf("WAIT_AUTH") !== -1 || s === "DISPENSING") { return "badge-warn"; }
            return "badge-info";
        }

        function render(robots) {
            root.innerHTML = "";
            if (!robots || robots.length === 0) {
                root.appendChild(el("div", {
                    class: "empty-note",
                    text: "No robots reporting yet. Waiting for a robot_id to appear " +
                          "on /api/v1/fleet — this is normal before task_bridge.py starts."
                }));
                return;
            }
            robots.forEach(function (r) {
                var online = r.online === true;
                var alarm = r.ovr && ALARM_OVERRIDES[r.ovr];
                var card = el("div", {
                    class: "robot-card " + (online ? "is-online" : "is-offline") + (alarm ? " has-alarm" : "")
                });

                var head = el("div", { class: "robot-id" }, [document.createTextNode(r.robot_id || "?")]);
                card.appendChild(head);

                var badges = el("div", { class: "stack" });
                var badgeRow = el("div");
                badgeRow.innerHTML =
                    '<span class="badge ' + (online ? "badge-online" : "badge-offline") + '">' +
                    (online ? "ONLINE" : "OFFLINE") + "</span> " +
                    '<span class="badge ' + stateBadgeClass(r.state) + '">' +
                    esc(r.state || "unknown") + "</span> " +
                    (r.ovr ? '<span class="badge ' + (alarm ? "badge-red badge-strong" : "badge-warn") + '">' +
                        esc(r.ovr) + "</span>" : "");
                badges.appendChild(badgeRow);
                card.appendChild(badges);

                var dl = el("dl");
                function row(term, value) {
                    dl.appendChild(el("dt", { text: term }));
                    dl.appendChild(el("dd", { text: value }));
                }
                row("Last seen", fmtAge(r.last_seen));
                row("Cold-box temp", (r.temp_c === null || r.temp_c === undefined) ? "—" : r.temp_c + " °C");
                row("Current task", r.task_id ? "#" + r.task_id : "none");
                card.appendChild(dl);

                root.appendChild(card);
            });
        }

        poll(function () {
            return getJSON("/api/v1/fleet").then(function (data) {
                if (data && Array.isArray(data.robots)) {
                    fleetIdsCache = data.robots.map(function (r) { return r.robot_id; }).filter(Boolean);
                    render(data.robots);
                }
                // On failure: do nothing — keep the last render on screen.
            });
        }, POLL_FLEET_MS);
    }

    // ------------------------------------------------------------------
    // 6. Dispatch page
    // ------------------------------------------------------------------

    function initDispatch() {
        var form = document.getElementById("dispatch-form");
        if (!form) { return; }

        var patientSelect = document.getElementById("dispatch-patient");
        var prescriptionBox = document.getElementById("dispatch-prescription");
        var robotInput = document.getElementById("dispatch-robot");
        var msgBox = document.getElementById("dispatch-result");
        var liveSection = document.getElementById("dispatch-live");
        var liveBody = document.getElementById("dispatch-live-body");
        var progressList = document.getElementById("dispatch-progress");
        var recentBody = document.getElementById("dispatch-recent-body");

        var patientsById = {};
        var trackedTaskId = null;

        function loadPatients() {
            return getJSON("/api/v1/patients").then(function (data) {
                if (!data || !Array.isArray(data.patients)) { return; }
                patientsById = {};
                var prevValue = patientSelect.value;
                patientSelect.innerHTML = "";
                if (data.patients.length === 0) {
                    patientSelect.appendChild(el("option", { value: "", text: "No patients in mock DB" }));
                    return;
                }
                data.patients.forEach(function (p) {
                    patientsById[p.patient_id] = p;
                    var label = p.patient_id + " — " + (p.name || "unnamed") +
                        (p.room ? " (Room " + p.room + ")" : "");
                    patientSelect.appendChild(el("option", { value: p.patient_id, text: label }));
                });
                if (prevValue && patientsById[prevValue]) { patientSelect.value = prevValue; }
                showPrescription();
            });
        }

        function showPrescription() {
            var p = patientsById[patientSelect.value];
            if (!p) {
                prescriptionBox.textContent = "Pick a patient to see their prescription.";
                return;
            }
            prescriptionBox.textContent =
                (p.prescription || "(no prescription on file)") +
                (p.room ? "  ·  Room " + p.room : "");
        }

        patientSelect.addEventListener("change", showPrescription);

        // --- Destination: named stations from the taught map ---------------
        // The marker-number box stays in the DOM and stays authoritative --
        // the dropdown just writes into it. That way one code path reads the
        // destination on submit, and a robot with no map (or a marker taught
        // but not yet named) is still fully dispatchable by typing an ID.
        var destSelect = document.getElementById("dispatch-destination");
        var markerInput = document.getElementById("dispatch-marker");
        var routeHint = document.getElementById("dispatch-route");
        var MANUAL = "__manual__";
        var haveMap = false;
        var markerNames = {};  // marker_id -> taught name, for the task readouts

        function destLabel(markerId) {
            var name = markerNames[markerId];
            return name ? name + " (" + markerId + ")" : String(markerId);
        }

        function loadDestinations() {
            return getJSON("/api/v1/map").then(function (data) {
                if (!data || !Array.isArray(data.markers)) { return; }
                markerNames = {};
                data.markers.forEach(function (m) {
                    if (m.name) { markerNames[m.marker_id] = m.name; }
                });
                var stations = data.markers.filter(function (m) {
                    return m.kind === "station" && m.name;
                });
                haveMap = stations.length > 0;
                if (!haveMap) {
                    // Nothing taught yet. Say so plainly rather than showing an
                    // empty dropdown that looks broken.
                    destSelect.classList.add("hidden");
                    markerInput.classList.remove("hidden");
                    setRouteHint();
                    return;
                }
                var prev = destSelect.value;
                destSelect.innerHTML = "";
                stations.forEach(function (m) {
                    destSelect.appendChild(el("option", {
                        value: String(m.marker_id),
                        text: m.name + " (marker " + m.marker_id + ")"
                    }));
                });
                destSelect.appendChild(el("option", {
                    value: MANUAL, text: "Other — type a marker ID…"
                }));
                // Keep the operator's choice across the 10 s refresh; otherwise
                // the box resets itself under their cursor mid-dispatch.
                if (prev && optionExists(prev)) { destSelect.value = prev; }
                else if (optionExists(markerInput.value)) { destSelect.value = markerInput.value; }
                destSelect.classList.remove("hidden");
                applyDestChoice();
            });
        }

        function optionExists(value) {
            var opts = destSelect.options;
            for (var i = 0; i < opts.length; i++) {
                if (opts[i].value === String(value)) { return true; }
            }
            return false;
        }

        function applyDestChoice() {
            if (!haveMap) { return; }
            if (destSelect.value === MANUAL) {
                markerInput.classList.remove("hidden");
            } else {
                markerInput.classList.add("hidden");
                markerInput.value = destSelect.value;
            }
            setRouteHint();
        }

        function setRouteHint() {
            var to = parseInt(markerInput.value, 10);
            if (!routeHint) { return; }
            if (isNaN(to)) { routeHint.textContent = ""; return; }
            if (!haveMap) {
                routeHint.textContent =
                    "No map taught yet — using the built-in route. Teach the floor on the Map page.";
                return;
            }
            getJSON("/api/v1/map/route?to=" + to).then(function (r) {
                if (!r) { routeHint.textContent = ""; return; }
                if (r.known) {
                    routeHint.textContent =
                        "Route from marker " + r.from + ": " + r.route.join(" → ") +
                        "  (" + r.hops + " hop" + (r.hops === 1 ? "" : "s") + ")";
                } else {
                    // Not fatal: dispatch still works and degrades to a single
                    // hop, but the operator deserves to know before pressing go.
                    routeHint.textContent =
                        "No taught route to marker " + to +
                        " — the robot will drive straight at it and stop if it is not there.";
                }
            });
        }

        destSelect.addEventListener("change", applyDestChoice);
        markerInput.addEventListener("change", setRouteHint);

        function renderProgress(state) {
            progressList.innerHTML = "";
            if (TASK_STATES_BAD[state]) {
                var li = el("li", { class: "step-bad", text: state.toUpperCase() });
                progressList.appendChild(li);
                return;
            }
            var reachedIdx = TASK_STATES.indexOf(state);
            TASK_STATES.forEach(function (s, i) {
                var cls = "";
                if (i < reachedIdx) { cls = "step-done"; }
                else if (i === reachedIdx) { cls = "step-current"; }
                progressList.appendChild(el("li", { class: cls, text: s }));
            });
        }

        function renderTasksTable(tasks) {
            recentBody.innerHTML = "";
            tasks.slice(0, 10).forEach(function (t) {
                var tr = document.createElement("tr");
                if (t.task_id === trackedTaskId) { tr.classList.add("row-flash"); }
                tr.innerHTML =
                    "<td>#" + esc(t.task_id) + "</td>" +
                    "<td>" + esc(t.robot_id) + "</td>" +
                    "<td>" + esc(t.patient_id) + "</td>" +
                    "<td>" + esc(t.state) + "</td>" +
                    "<td>" + esc(destLabel(t.destination_marker)) + "</td>" +
                    "<td>" + fmtClock(t.created_ts) + "</td>";
                recentBody.appendChild(tr);
            });
            if (tasks.length === 0) {
                recentBody.innerHTML = '<tr><td colspan="6" class="empty-note">No tasks dispatched yet.</td></tr>';
            }
        }

        function pollTasks() {
            return getJSON("/api/v1/tasks?limit=20").then(function (data) {
                if (!data || !Array.isArray(data.tasks)) { return; }
                renderTasksTable(data.tasks);
                if (trackedTaskId) {
                    var mine = data.tasks.filter(function (t) { return t.task_id === trackedTaskId; })[0];
                    if (mine) {
                        liveSection.classList.remove("hidden");
                        liveBody.textContent =
                            "Task #" + mine.task_id + " · " + (mine.patient_name || mine.patient_id) +
                            " · " + mine.units + " unit(s)" + (mine.cold_item ? " + cold item" : "") +
                            " · to " + destLabel(mine.destination_marker);
                        renderProgress(mine.state);
                    }
                }
            });
        }

        form.addEventListener("submit", function (evt) {
            evt.preventDefault();
            var patientId = patientSelect.value;
            var units = parseInt(document.getElementById("dispatch-units").value, 10);
            var coldItem = document.getElementById("dispatch-cold").checked;
            var marker = parseInt(document.getElementById("dispatch-marker").value, 10);
            var robotId = (robotInput.value || DEFAULT_ROBOT_ID).trim();

            msgBox.className = "form-msg";
            msgBox.classList.add("hidden");

            if (!patientId) {
                msgBox.textContent = "Pick a patient first.";
                msgBox.classList.remove("hidden");
                msgBox.classList.add("err");
                return;
            }
            if (!units || units < 1) {
                msgBox.textContent = "Units must be at least 1.";
                msgBox.classList.remove("hidden");
                msgBox.classList.add("err");
                return;
            }
            if (!marker && marker !== 0) {
                msgBox.textContent = "Destination marker is required.";
                msgBox.classList.remove("hidden");
                msgBox.classList.add("err");
                return;
            }

            var submitBtn = document.getElementById("dispatch-submit");
            submitBtn.disabled = true;

            postJSON("/api/v1/tasks", {
                patient_id: patientId,
                units: units,
                cold_item: coldItem,
                destination_marker: marker,
                robot_id: robotId
            }).then(function (data) {
                submitBtn.disabled = false;
                msgBox.classList.remove("hidden");
                if (data && data.task_id) {
                    trackedTaskId = data.task_id;
                    msgBox.classList.add("ok");
                    msgBox.classList.remove("err");
                    msgBox.textContent = "Dispatched — task #" + data.task_id + ". Tracking below.";
                    pollTasks();
                } else {
                    msgBox.classList.add("err");
                    msgBox.classList.remove("ok");
                    msgBox.textContent = "Dispatch failed — dashboard unreachable or rejected the request. " +
                        "Nothing was sent to the robot.";
                }
            });
        });

        loadPatients();
        loadDestinations();
        refreshRobotIdCache().then(function () {
            if (!robotInput.value) { robotInput.value = firstRobotId(); }
        });
        poll(loadPatients, 10000); // patient list rarely changes; light polling
        poll(loadDestinations, 10000); // picks up markers named mid-teach-run
        poll(pollTasks, POLL_TASKS_MS);
    }

    // ------------------------------------------------------------------
    // 7. Audit page — the product. Tails via since_id, filters client-side
    //    request params, never wipes the table on a failed poll.
    // ------------------------------------------------------------------

    function initAudit() {
        var body = document.getElementById("audit-body");
        if (!body) { return; }

        var kindSel = document.getElementById("audit-kind");
        var sevSel = document.getElementById("audit-severity");
        var robotInput = document.getElementById("audit-robot");
        var pauseBtn = document.getElementById("audit-pause");
        var clearBtn = document.getElementById("audit-clear");
        var countTotal = document.getElementById("audit-count-total");
        var countRed = document.getElementById("audit-count-red");

        var MAX_ROWS = 300; // cap DOM growth over a long demo/rehearsal
        var sinceId = 0;
        var paused = false;
        var totalSeen = 0;
        var redSeen = 0;

        function currentQuery(extra) {
            var params = new URLSearchParams();
            params.set("limit", "50");
            if (extra && extra.since_id !== undefined) { params.set("since_id", extra.since_id); }
            if (kindSel.value) { params.set("kind", kindSel.value); }
            if (sevSel.value) { params.set("severity", sevSel.value); }
            if (robotInput.value.trim()) { params.set("robot_id", robotInput.value.trim()); }
            return params.toString();
        }

        function resetTable() {
            body.innerHTML = "";
            sinceId = 0;
            totalSeen = 0;
            redSeen = 0;
            countTotal.textContent = "0";
            countRed.textContent = "0";
        }

        function addRow(ev) {
            var placeholder = document.getElementById("audit-empty-row");
            if (placeholder) { placeholder.remove(); }

            var sev = severityFor(ev.kind, ev.severity);
            var tr = document.createElement("tr");
            tr.className = "sev-" + sev + " row-flash";
            tr.innerHTML =
                "<td>" + fmtClock(ev.ts || ev.received_ts) + "</td>" +
                "<td>" + esc(ev.robot_id) + "</td>" +
                "<td>" + severityBadge(sev) + "</td>" +
                "<td>" + esc(ev.kind) + "</td>" +
                "<td>" + (ev.task_id ? "#" + esc(ev.task_id) : "—") + "</td>" +
                '<td class="detail-cell">' + esc(ev.detail || "") + "</td>";
            body.insertBefore(tr, body.firstChild); // newest first — visible without scrolling
            setTimeout(function () { tr.classList.remove("row-flash"); }, 1800);

            totalSeen += 1;
            if (sev === "red") { redSeen += 1; }
            countTotal.textContent = String(totalSeen);
            countRed.textContent = String(redSeen);

            while (body.children.length > MAX_ROWS) {
                body.removeChild(body.lastChild);
            }
        }

        function tick() {
            if (paused) { return Promise.resolve(); }
            return getJSON("/api/v1/events?" + currentQuery({ since_id: sinceId })).then(function (data) {
                if (!data || !Array.isArray(data.events)) { return; }
                // Events arrive oldest-first per since_id semantics; append
                // in that order so addRow's "insert at top" keeps the
                // newest overall event visible at the very top.
                data.events.forEach(function (ev) { addRow(ev); });
                if (typeof data.max_id === "number") {
                    sinceId = data.max_id;
                } else if (data.events.length) {
                    var maxSeen = data.events.reduce(function (m, ev) {
                        return ev.id !== undefined && ev.id > m ? ev.id : m;
                    }, sinceId);
                    sinceId = maxSeen;
                }
            });
        }

        pauseBtn.addEventListener("click", function () {
            paused = !paused;
            pauseBtn.textContent = paused ? "Resume" : "Pause";
            pauseBtn.classList.toggle("btn-secondary", !paused);
        });

        clearBtn.addEventListener("click", function () {
            kindSel.value = "";
            sevSel.value = "";
            robotInput.value = "";
            resetTable();
        });

        [kindSel, sevSel].forEach(function (elm) {
            elm.addEventListener("change", resetTable);
        });
        robotInput.addEventListener("change", resetTable);

        refreshRobotIdCache();
        poll(tick, POLL_AUDIT_MS);
    }

    // ------------------------------------------------------------------
    // 8. Temp page — plain <canvas>, no charting library.
    // ------------------------------------------------------------------

    function initTemp() {
        var canvas = document.getElementById("temp-canvas");
        if (!canvas) { return; }

        var robotInput = document.getElementById("temp-robot");
        var minutesSel = document.getElementById("temp-minutes");
        var minTile = document.getElementById("temp-min");
        var maxTile = document.getElementById("temp-max");
        var lastTile = document.getElementById("temp-last");
        var excCountTile = document.getElementById("temp-exc-count");
        var excList = document.getElementById("temp-excursion-list");

        var lastData = null; // keep last good payload so a failed poll never blanks the chart

        function excursionRange(e) {
            // The excursion object shape isn't spelled out beyond "excursions:
            // [...]" in docs/http-api-v1.md, so this reads whichever fields
            // are present rather than assuming one exact shape.
            var start = e.start_ts || e.ts || e.start || null;
            var end = e.end_ts || e.end || e.ts || start;
            return { start: toDate(start), end: toDate(end), raw: e };
        }

        function draw(data) {
            var wrap = canvas.parentElement;
            var cssW = wrap.clientWidth;
            var cssH = 340;
            var dpr = window.devicePixelRatio || 1;
            canvas.width = Math.round(cssW * dpr);
            canvas.height = Math.round(cssH * dpr);
            var ctx = canvas.getContext("2d");
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            ctx.clearRect(0, 0, cssW, cssH);

            var series = (data && Array.isArray(data.series)) ? data.series : [];
            var padL = 46, padR = 14, padT = 16, padB = 26;
            var plotW = Math.max(10, cssW - padL - padR);
            var plotH = Math.max(10, cssH - padT - padB);

            var styles = getComputedStyle(document.documentElement);
            var gridColor = styles.getPropertyValue("--border").trim() || "#ccc";
            var lineColor = styles.getPropertyValue("--brand").trim() || "#1d4ed8";
            var textColor = styles.getPropertyValue("--muted").trim() || "#666";
            var excColor = "rgba(230, 130, 20, 0.28)";

            if (series.length === 0) {
                ctx.fillStyle = textColor;
                ctx.font = "16px sans-serif";
                ctx.fillText("No temperature data yet for this robot.", padL, padT + 20);
                return;
            }

            var points = series.map(function (p) {
                return { t: toDate(p.ts), c: Number(p.c) };
            }).filter(function (p) { return p.t && !isNaN(p.c); });

            if (points.length === 0) {
                ctx.fillStyle = textColor;
                ctx.font = "16px sans-serif";
                ctx.fillText("Temperature series present but unreadable.", padL, padT + 20);
                return;
            }

            var tMin = points[0].t.getTime();
            var tMax = points[points.length - 1].t.getTime();
            if (tMax === tMin) { tMax = tMin + 1; }

            var cVals = points.map(function (p) { return p.c; });
            var cLo = Math.min.apply(null, cVals);
            var cHi = Math.max.apply(null, cVals);
            // Pad the vertical range a little so the line isn't glued to an edge.
            var span = Math.max(1, cHi - cLo);
            cLo -= span * 0.15;
            cHi += span * 0.15;

            function x(t) { return padL + ((t - tMin) / (tMax - tMin)) * plotW; }
            function y(c) { return padT + plotH - ((c - cLo) / (cHi - cLo)) * plotH; }

            // Excursion bands, drawn first so the line sits on top.
            var excursions = (data && Array.isArray(data.excursions)) ? data.excursions : [];
            excList.innerHTML = "";
            excursions.forEach(function (raw) {
                var e = excursionRange(raw);
                if (e.start) {
                    var x0 = x(Math.max(tMin, e.start.getTime()));
                    var x1 = e.end ? x(Math.min(tMax, e.end.getTime())) : x0 + 3;
                    ctx.fillStyle = excColor;
                    ctx.fillRect(Math.min(x0, x1), padT, Math.max(3, Math.abs(x1 - x0)), plotH);
                }
                var label = raw.detail || raw.reason ||
                    (e.start ? e.start.toLocaleTimeString([], { hour12: false }) : "excursion");
                excList.appendChild(el("li", { text: label }));
            });

            // Gridlines + axis labels (Y = °C).
            ctx.strokeStyle = gridColor;
            ctx.fillStyle = textColor;
            ctx.font = "12px sans-serif";
            ctx.lineWidth = 1;
            var steps = 4;
            for (var i = 0; i <= steps; i++) {
                var cy = padT + (plotH / steps) * i;
                var cVal = cHi - ((cHi - cLo) / steps) * i;
                ctx.beginPath();
                ctx.moveTo(padL, cy);
                ctx.lineTo(padL + plotW, cy);
                ctx.stroke();
                ctx.fillText(cVal.toFixed(1) + "°C", 2, cy + 4);
            }

            // The line itself.
            ctx.strokeStyle = lineColor;
            ctx.lineWidth = 2.5;
            ctx.beginPath();
            points.forEach(function (p, i) {
                var px = x(p.t.getTime()), py = y(p.c);
                if (i === 0) { ctx.moveTo(px, py); } else { ctx.lineTo(px, py); }
            });
            ctx.stroke();

            // Dots on each sample so sparse 30 s-interval readings are visible.
            ctx.fillStyle = lineColor;
            points.forEach(function (p) {
                ctx.beginPath();
                ctx.arc(x(p.t.getTime()), y(p.c), 2.5, 0, Math.PI * 2);
                ctx.fill();
            });

            // X axis start/end labels.
            ctx.fillStyle = textColor;
            ctx.fillText(points[0].t.toLocaleTimeString([], { hour12: false }), padL, cssH - 6);
            var lastLabel = points[points.length - 1].t.toLocaleTimeString([], { hour12: false });
            ctx.fillText(lastLabel, padL + plotW - ctx.measureText(lastLabel).width, cssH - 6);

            minTile.textContent = (typeof data.min_c === "number" ? data.min_c : cLo).toFixed(1) + " °C";
            maxTile.textContent = (typeof data.max_c === "number" ? data.max_c : cHi).toFixed(1) + " °C";
            lastTile.textContent = points[points.length - 1].c.toFixed(1) + " °C  (" + fmtAge(points[points.length - 1].t) + ")";
            excCountTile.textContent = String(excursions.length);
        }

        function load() {
            var robotId = (robotInput.value || DEFAULT_ROBOT_ID).trim();
            var minutes = minutesSel.value || "60";
            var url = "/api/v1/temp?robot_id=" + encodeURIComponent(robotId) +
                "&minutes=" + encodeURIComponent(minutes);
            return getJSON(url).then(function (data) {
                if (data) {
                    lastData = data;
                    draw(data);
                } else if (lastData) {
                    draw(lastData); // keep the last good chart on screen
                }
            });
        }

        robotInput.addEventListener("change", load);
        minutesSel.addEventListener("change", load);
        window.addEventListener("resize", function () {
            if (lastData) { draw(lastData); }
        });

        refreshRobotIdCache().then(function () {
            if (!robotInput.value) { robotInput.value = firstRobotId(); }
            load();
        });
        poll(load, POLL_TEMP_MS);
    }

    // ------------------------------------------------------------------
    // 9. Teleop page — the R14 parachute. Must always work, buttons AND
    //    arrow keys, every nudge visibly logged.
    // ------------------------------------------------------------------

    function initTeleop() {
        var pad = document.getElementById("teleop-pad");
        if (!pad) { return; }

        var robotInput = document.getElementById("teleop-robot");
        var speedRange = document.getElementById("teleop-speed");
        var speedReadout = document.getElementById("teleop-speed-readout");
        var lastBox = document.getElementById("teleop-last");
        var queuedBox = document.getElementById("teleop-queued");

        function currentSpeed() {
            var v = parseFloat(speedRange.value);
            return isNaN(v) ? 0.3 : Math.min(1, Math.max(0, v));
        }

        speedRange.addEventListener("input", function () {
            speedReadout.textContent = currentSpeed().toFixed(2);
        });
        speedReadout.textContent = currentSpeed().toFixed(2);

        function sendCmd(cmd) {
            var robotId = (robotInput.value || DEFAULT_ROBOT_ID).trim();
            var spd = cmd === "stop" ? 0 : currentSpeed();
            var btn = pad.querySelector('[data-cmd="' + cmd + '"]');
            if (btn) {
                btn.classList.add("pressed");
                setTimeout(function () { btn.classList.remove("pressed"); }, 200);
            }
            lastBox.textContent = "Sending " + cmd + " @ " + spd.toFixed(2) + " to " + robotId + " …";
            postJSON("/api/v1/teleop", { robot_id: robotId, cmd: cmd, spd: spd }).then(function (data) {
                var stamp = new Date().toLocaleTimeString([], { hour12: false });
                if (data && data.ok) {
                    lastBox.textContent =
                        "[" + stamp + "] " + cmd.toUpperCase() + " @ " + spd.toFixed(2) +
                        " -> " + robotId + "  (seq " + data.seq + ", logged as teleop_nudge)";
                } else {
                    lastBox.textContent =
                        "[" + stamp + "] " + cmd.toUpperCase() + " -> " + robotId +
                        "  FAILED — dashboard unreachable or rejected it. Nothing was sent to the robot.";
                }
            });
        }

        pad.querySelectorAll("button[data-cmd]").forEach(function (btn) {
            btn.addEventListener("click", function () { sendCmd(btn.getAttribute("data-cmd")); });
        });

        // Arrow-key + space control, ignored while typing in a text/number field.
        document.addEventListener("keydown", function (evt) {
            var tag = (document.activeElement && document.activeElement.tagName) || "";
            if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") { return; }
            var map = {
                ArrowUp: "fwd", ArrowDown: "back",
                ArrowLeft: "left", ArrowRight: "right",
                " ": "stop", Escape: "stop"
            };
            var cmd = map[evt.key];
            if (cmd) {
                evt.preventDefault();
                sendCmd(cmd);
            }
        });

        function pollQueued() {
            var robotId = (robotInput.value || DEFAULT_ROBOT_ID).trim();
            return getJSON("/api/v1/teleop?robot_id=" + encodeURIComponent(robotId)).then(function (data) {
                if (!data) { return; }
                queuedBox.textContent = data.cmd ?
                    "Queued for the robot: " + data.cmd + " @ " + data.spd + " (seq " + data.seq + ")" :
                    "Nothing queued.";
            });
        }

        refreshRobotIdCache().then(function () {
            if (!robotInput.value) { robotInput.value = firstRobotId(); }
        });
        poll(pollQueued, POLL_TELEOP_MS);

        // -- sound-threshold slider (also lives on this page, per the doc) --
        var soundForm = document.getElementById("sound-config-form");
        if (soundForm) {
            var thresholdRange = document.getElementById("sound-threshold");
            var thresholdReadout = document.getElementById("sound-threshold-readout");
            var configMsg = document.getElementById("sound-config-msg");
            var configDump = document.getElementById("sound-config-dump");
            var applying = false;

            thresholdRange.addEventListener("input", function () {
                thresholdReadout.textContent = parseFloat(thresholdRange.value).toFixed(2);
            });

            function loadConfig() {
                var robotId = (robotInput.value || DEFAULT_ROBOT_ID).trim();
                return getJSON("/api/v1/config?robot_id=" + encodeURIComponent(robotId)).then(function (data) {
                    if (!data) { return; }
                    if (!applying && document.activeElement !== thresholdRange &&
                        typeof data.sound_threshold === "number") {
                        thresholdRange.value = data.sound_threshold;
                        thresholdReadout.textContent = data.sound_threshold.toFixed(2);
                    }
                    var bits = [];
                    if (typeof data.config_rev === "number") { bits.push("config_rev " + data.config_rev); }
                    if (typeof data.sound_min_ms === "number") { bits.push("min_ms " + data.sound_min_ms); }
                    if (typeof data.sound_refractory_s === "number") { bits.push("refractory " + data.sound_refractory_s + "s"); }
                    if (data.teleop_enabled !== undefined) { bits.push("teleop_enabled=" + data.teleop_enabled); }
                    if (data.nav) { bits.push("nav=" + JSON.stringify(data.nav)); }
                    configDump.textContent = bits.join("  ·  ") || "(no config reported)";
                });
            }

            soundForm.addEventListener("submit", function (evt) {
                evt.preventDefault();
                var robotId = (robotInput.value || DEFAULT_ROBOT_ID).trim();
                applying = true;
                configMsg.classList.add("hidden");
                postJSON("/api/v1/config", {
                    robot_id: robotId,
                    sound_threshold: parseFloat(thresholdRange.value)
                }).then(function (data) {
                    applying = false;
                    configMsg.classList.remove("hidden");
                    if (data && data.ok) {
                        configMsg.className = "form-msg ok";
                        configMsg.textContent = "Applied — config_rev now " + data.config_rev + ".";
                    } else {
                        configMsg.className = "form-msg err";
                        configMsg.textContent = "Failed to apply — dashboard unreachable or rejected it.";
                    }
                    loadConfig();
                });
            });

            poll(loadConfig, POLL_CONFIG_MS);
        }
    }

    // ------------------------------------------------------------------
    // 10. Boot
    // ------------------------------------------------------------------

    // ------------------------------------------------------------------
    // 9b. Live console — audit trail + system log + hardware, in one place.
    // ------------------------------------------------------------------
    function initLive() {
        var evBox = document.getElementById("live-events");
        var sysBox = document.getElementById("live-syslog");
        if (!evBox || !sysBox) return;   // not on this page

        var strip = document.getElementById("live-strip");
        var footer = document.getElementById("live-footer");
        var redOnly = document.getElementById("live-red-only");
        var errOnly = document.getElementById("live-errors-only");

        var sinceId = 0;       // audit-event cursor
        var logCursor = "";    // journald cursor
        var MAX_ROWS = 400;    // keep the DOM bounded on a Pi

        function trim(box) {
            while (box.childElementCount > MAX_ROWS) box.removeChild(box.firstChild);
        }
        // Only auto-scroll when the user is already at the bottom, so reading
        // back through history doesn't yank you away every poll.
        function atBottom(box) {
            return box.scrollHeight - box.scrollTop - box.clientHeight < 40;
        }
        function hhmmss(d) {
            return ("0" + d.getHours()).slice(-2) + ":" +
                   ("0" + d.getMinutes()).slice(-2) + ":" +
                   ("0" + d.getSeconds()).slice(-2);
        }
        function setChip(id, text, cls) {
            var el = document.getElementById(id);
            if (!el) return;
            el.lastElementChild.textContent = text;
            el.className = "chip " + (cls || "");
        }

        function pollEvents() {
            var url = "/api/v1/events?limit=60" +
                      (sinceId ? "&since_id=" + sinceId : "");
            fetch(url).then(function (r) { return r.json(); }).then(function (d) {
                var rows = d.events || [];
                var stick = atBottom(evBox);
                rows.forEach(function (e) {
                    if (redOnly && redOnly.checked && e.severity === "info") return;
                    var line = document.createElement("div");
                    line.className = "logline sev-" + e.severity;
                    line.textContent =
                        hhmmss(new Date(e.ts || Date.now())) + "  " +
                        e.kind + "  " + (e.detail || "");
                    evBox.appendChild(line);
                });
                // Only jump the cursor to max_id once we've caught up, or a
                // capped page would silently skip everything in between.
                sinceId = rows.length ? rows[rows.length - 1].id : (d.max_id || sinceId);
                trim(evBox);
                if (stick) evBox.scrollTop = evBox.scrollHeight;
            }).catch(function () { /* offline banner already covers this */ });
        }

        function pollLogs() {
            fetch("/api/v1/logs?limit=120" +
                  (logCursor ? "&cursor=" + encodeURIComponent(logCursor) : ""))
                .then(function (r) { return r.json(); })
                .then(function (d) {
                    var stick = atBottom(sysBox);
                    (d.lines || []).forEach(function (l) {
                        var bad = (l.level === "err" || l.level === "crit" ||
                                   l.level === "alert" || l.level === "emerg");
                        var warn = (l.level === "warning");
                        if (errOnly && errOnly.checked && !bad && !warn) return;
                        var line = document.createElement("div");
                        line.className = "logline " +
                            (bad ? "sev-red" : warn ? "sev-warn" : "sev-info");
                        line.textContent =
                            hhmmss(new Date(l.ts * 1000)) + "  [" + l.unit + "] " + l.msg;
                        sysBox.appendChild(line);
                    });
                    if (d.cursor) logCursor = d.cursor;
                    trim(sysBox);
                    if (stick) sysBox.scrollTop = sysBox.scrollHeight;
                }).catch(function () {});
        }

        function pollSystem() {
            fetch("/api/v1/system").then(function (r) { return r.json(); })
                .then(function (d) {
                    var hw = d.hardware || {}, sv = d.services || {};
                    setChip("chip-mcu", hw.mcu_serial ? "connected" : "absent",
                            hw.mcu_serial ? "ok" : "bad");
                    setChip("chip-cam", hw.camera ? "present" : "absent",
                            hw.camera ? "ok" : "bad");
                    // No mic yet is expected, so it's a soft warning not an error.
                    setChip("chip-mic", hw.mic ? "present" : "absent",
                            hw.mic ? "ok" : "warn");
                    ["dashboard", "bridge", "nav", "ears"].forEach(function (n) {
                        var s = sv[n] || {};
                        var good = s.active === "active";
                        // ears/nav intentionally disabled without hardware -> warn, not error
                        var cls = good ? "ok"
                                : (s.enabled === "disabled" ? "warn" : "bad");
                        setChip("svc-" + n, s.active || "?", cls);
                    });
                }).catch(function () {});
        }

        function pollFleet() {
            fetch("/api/v1/fleet").then(function (r) { return r.json(); })
                .then(function (d) {
                    var r0 = (d.robots || [])[0];
                    if (!r0) return;
                    setChip("chip-robot", r0.robot_id || "?",
                            r0.online ? "ok" : "bad");
                    setChip("chip-state", r0.last_state || "—", "");
                    setChip("chip-ovr", r0.last_ovr || "none",
                            r0.last_ovr ? "warn" : "ok");
                    if (footer) {
                        footer.textContent = "last robot contact: " +
                            (r0.last_seen || "never") + " · updated " +
                            hhmmss(new Date());
                    }
                }).catch(function () {});
        }

        // --- camera feed ------------------------------------------------
        // Snapshot polling, not MJPEG: an MJPEG stream pins a server thread
        // for as long as the tab is open, and this Pi is also running nav,
        // the bridge and the dashboard. A dropped snapshot costs one frame;
        // a wedged stream costs the whole page.
        var cam = document.getElementById("live-cam");
        var camOff = document.getElementById("live-cam-off");
        var camStatus = document.getElementById("live-cam-status");
        var camBusy = false, camFrames = 0, camSince = Date.now();

        function pollCam() {
            if (!cam || camBusy) return;      // never queue up behind a slow frame
            camBusy = true;
            var url = "/api/v1/camera.jpg?t=" + Date.now();  // defeat the cache
            var probe = new Image();
            probe.onload = function () {
                camBusy = false;
                cam.src = probe.src;          // swap only once fully decoded: no flicker
                cam.style.display = "block";
                if (camOff) camOff.style.display = "none";
                camFrames++;
            };
            probe.onerror = function () {
                camBusy = false;
                // 503 = nav isn't publishing (not running, or camera stalled).
                cam.style.display = "none";
                if (camOff) camOff.style.display = "block";
            };
            probe.src = url;
        }

        setInterval(function () {
            var secs = (Date.now() - camSince) / 1000;
            if (secs >= 3 && camStatus) {
                camStatus.textContent = camFrames
                    ? "receiving ~" + (camFrames / secs).toFixed(1) + " fps"
                    : "no frames — check that medic-nav is active and the CSI ribbon is seated";
                camFrames = 0; camSince = Date.now();
            }
        }, 3000);

        pollSystem(); pollFleet(); pollEvents(); pollLogs(); pollCam();
        setInterval(pollCam, 250);   // ~4 fps, matching nav's publish rate
        setInterval(pollEvents, 1000);
        setInterval(pollLogs, 1500);
        setInterval(pollFleet, 2000);
        setInterval(pollSystem, 5000);   // subprocess calls — don't hammer the Pi
        if (strip) strip.title = "updates automatically; no need to refresh";
    }

    // ------------------------------------------------------------------
    // 9c. Marker map — teach-mode naming + route testing.
    // ------------------------------------------------------------------
    function initMap() {
        var tableEl = document.getElementById("map-table");
        if (!tableEl) return;                       // not on this page

        var unnamedCard = document.getElementById("unnamed-card");
        var unnamedList = document.getElementById("unnamed-list");
        var unnamedCount = document.getElementById("unnamed-count");
        var summary = document.getElementById("map-summary");
        var fromSel = document.getElementById("route-from");
        var toSel = document.getElementById("route-to");
        var routeBtn = document.getElementById("route-go");
        var routeOut = document.getElementById("route-result");

        var lastMarkers = [];

        function label(m) {
            return m.name ? (m.marker_id + " — " + m.name) : (m.marker_id + " (unnamed)");
        }

        function nameMarker(id, name, kind) {
            fetch("/api/v1/map/marker", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({marker_id: id, name: name, kind: kind})
            }).then(function (r) { return r.json(); })
              .then(function () { load(); })
              .catch(function () {});
        }

        function renderUnnamed(unnamed) {
            unnamedList.innerHTML = "";
            if (!unnamed.length) { unnamedCard.style.display = "none"; return; }
            unnamedCard.style.display = "";
            unnamedCount.textContent = "(" + unnamed.length + " waiting)";
            unnamed.forEach(function (u) {
                var row = document.createElement("div");
                row.className = "namerow";
                var idTag = document.createElement("b");
                idTag.textContent = "marker " + u.marker_id;
                var seen = document.createElement("span");
                seen.className = "muted";
                seen.textContent = "seen " + u.sightings + "x";
                var input = document.createElement("input");
                input.type = "text";
                input.placeholder = "e.g. Room 4B / Corridor corner";
                var kind = document.createElement("select");
                ["station", "waypoint"].forEach(function (k) {
                    var o = document.createElement("option");
                    o.value = k; o.textContent = k; kind.appendChild(o);
                });
                var btn = document.createElement("button");
                btn.textContent = "Save";
                function save() {
                    var v = input.value.trim();
                    if (v) nameMarker(u.marker_id, v, kind.value);
                }
                btn.onclick = save;
                input.onkeydown = function (e) { if (e.key === "Enter") save(); };
                [idTag, seen, input, kind, btn].forEach(function (el) { row.appendChild(el); });
                unnamedList.appendChild(row);
            });
        }

        function renderTable(markers, edges) {
            var byId = {};
            markers.forEach(function (m) { byId[m.marker_id] = m; });
            var links = {};
            edges.forEach(function (e) {
                (links[e.from_id] = links[e.from_id] || []).push(e.to_id);
                (links[e.to_id] = links[e.to_id] || []).push(e.from_id);
            });

            var t = document.createElement("table");
            t.innerHTML = "<thead><tr><th>ID</th><th>NAME</th><th>KIND</th>" +
                          "<th>SEEN</th><th>CONNECTS TO</th></tr></thead>";
            var tb = document.createElement("tbody");
            if (!markers.length) {
                tb.innerHTML = "<tr><td colspan='5'><i>Nothing taught yet — " +
                               "run the teach loop above.</i></td></tr>";
            }
            markers.forEach(function (m) {
                var tr = document.createElement("tr");
                var conn = (links[m.marker_id] || []).sort(function (a, b) { return a - b; });
                var connTxt = conn.length
                    ? conn.map(function (c) {
                          return byId[c] && byId[c].name ? byId[c].name : ("#" + c);
                      }).join(", ")
                    : "— isolated";
                tr.innerHTML =
                    "<td>" + m.marker_id + "</td>" +
                    "<td>" + (m.name || "<i class='muted'>unnamed</i>") + "</td>" +
                    "<td>" + m.kind + "</td>" +
                    "<td>" + m.sightings + "</td>" +
                    "<td" + (conn.length ? "" : " class='muted'") + ">" + connTxt + "</td>";
                tb.appendChild(tr);
            });
            t.appendChild(tb);
            tableEl.innerHTML = "";
            tableEl.appendChild(t);
        }

        function fillSelects(markers) {
            [fromSel, toSel].forEach(function (sel) {
                if (!sel) return;
                var prev = sel.value;
                sel.innerHTML = "";
                markers.forEach(function (m) {
                    var o = document.createElement("option");
                    o.value = m.marker_id; o.textContent = label(m);
                    sel.appendChild(o);
                });
                if (prev) sel.value = prev;
            });
        }

        function load() {
            fetch("/api/v1/map").then(function (r) { return r.json(); })
                .then(function (d) {
                    lastMarkers = d.markers || [];
                    renderUnnamed(d.unnamed || []);
                    renderTable(lastMarkers, d.edges || []);
                    fillSelects(lastMarkers);
                    var msg = lastMarkers.length + " markers, " +
                              (d.edges || []).length + " confirmed links " +
                              "(a link counts once seen " + d.min_edge_sightings +
                              "x together).";
                    if (d.provisional_edges) {
                        msg += "  " + d.provisional_edges + " more link(s) seen but " +
                               "not yet confirmed — drive past them again.";
                    }
                    summary.textContent = msg;
                }).catch(function () {});
        }

        if (routeBtn) {
            routeBtn.onclick = function () {
                var a = fromSel.value, b = toSel.value;
                if (!a || !b) return;
                fetch("/api/v1/map/route?from=" + a + "&to=" + b)
                    .then(function (r) { return r.json(); })
                    .then(function (d) {
                        if (!d.known) {
                            routeOut.innerHTML = "<b class='bad'>No known route.</b> " +
                                "The robot has never seen these connected — teach the " +
                                "missing link before dispatching there.";
                            return;
                        }
                        var byId = {};
                        lastMarkers.forEach(function (m) { byId[m.marker_id] = m; });
                        var names = d.route.map(function (id) {
                            return byId[id] && byId[id].name ? byId[id].name : ("#" + id);
                        });
                        routeOut.innerHTML = "<b class='ok'>" + d.hops + " hop(s):</b> " +
                            names.join("  →  ") +
                            "  <span class='muted'>(" + d.route.join(" → ") + ")</span>";
                    }).catch(function () {});
            };
        }

        load();
        setInterval(load, 2000);   // new markers appear while you teach
    }

    document.addEventListener("DOMContentLoaded", function () {
        highlightNav();
        initMap();
        initFleet();
        initDispatch();
        initAudit();
        initTemp();
        initTeleop();
        initLive();
    });
})();
