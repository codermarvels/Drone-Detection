// Dashboard. Plain JS, no external files, so it works offline.
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var el = function (tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  };
  var SVGNS = "http://www.w3.org/2000/svg";

  var ICONS = {
    overview: "M3 12l9-8 9 8M5 10v10h14V10",
    spectrum: "M3 17l4-6 4 4 4-9 6 11",
    detections: "M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18M12 8v4l3 2",
    map: "M9 4L3 6v14l6-2 6 2 6-2V4l-6 2-6-2zM9 4v14M15 6v14",
    recordings: "M12 4a4 4 0 0 1 4 4v4a4 4 0 0 1-8 0V8a4 4 0 0 1 4-4M5 12a7 7 0 0 0 14 0M12 19v2",
    model: "M12 3l8 4.5v9L12 21l-8-4.5v-9L12 3zM12 12l8-4.5M12 12v9M12 12L4 7.5",
    settings: "M12 9a3 3 0 1 0 0 6a3 3 0 1 0 0-6M19 12h2M3 12h2M12 3v2M12 19v2M17 7l1.4-1.4M5.6 18.4L7 17M17 17l1.4 1.4M5.6 5.6L7 7"
  };
  var VIEWS = [["overview", "Overview"], ["spectrum", "Spectrum"], ["detections", "Detections"], ["map", "Map"],
               ["recordings", "Recordings"], ["model", "AI Model"], ["settings", "Settings"]];
  var DRONE_PATH = '<circle cx="5.5" cy="5.5" r="2.5"/><circle cx="18.5" cy="5.5" r="2.5"/><circle cx="5.5" cy="18.5" r="2.5"/><circle cx="18.5" cy="18.5" r="2.5"/><rect x="9.5" y="9.5" width="5" height="5" rx="1.2"/>';

  var S = { seq: 0, state: null, view: "overview", hours: 0, selected: null, history: [], wfInit: false,
            gen: null, sessions: [], settings: null, editing: null, jobSeen: null };

  function icon(d, stroke) {
    var s = document.createElementNS(SVGNS, "svg");
    s.setAttribute("width", "17"); s.setAttribute("height", "17"); s.setAttribute("viewBox", "0 0 24 24");
    s.setAttribute("fill", "none"); s.setAttribute("stroke-width", "1.8"); s.setAttribute("stroke-linecap", "round");
    s.setAttribute("stroke-linejoin", "round"); s.setAttribute("aria-hidden", "true");
    if (stroke) s.setAttribute("stroke", stroke);
    var p = document.createElementNS(SVGNS, "path"); p.setAttribute("d", d); s.appendChild(p);
    return s;
  }

  // ---- navigation -------------------------------------------------------
  function buildNav() {
    var nav = $("nav");
    VIEWS.forEach(function (v) {
      var a = el("a"); a.href = "#" + v[0]; a.dataset.view = v[0];
      a.appendChild(icon(ICONS[v[0]]));
      a.appendChild(el("span", "lbl", v[1]));
      if (v[0] === "detections") { var b = el("span", "badge"); b.id = "nav-badge"; b.hidden = true; a.appendChild(b); }
      nav.appendChild(a);
    });
  }
  function setView() {
    var v = (location.hash || "#overview").slice(1);
    if (!ICONS[v]) v = "overview";
    S.view = v;
    document.body.dataset.view = v;
    VIEWS.forEach(function (x) { if (x[0] === v) $("view-title").textContent = x[1]; });
    Array.prototype.forEach.call($("nav").children, function (a) { a.classList.toggle("on", a.dataset.view === v); });
    if (v === "recordings" || v === "model") loadSessions();
    if (v === "settings" || v === "model") loadSettings();
    if (v === "detections" && S.hours === 0) setRange(1);
    requestAnimationFrame(function () { drawSpectrum(); resizeWaterfall(); renderMap(); });
  }

  // ---- formatting -------------------------------------------------------
  function ghz(hz) { return (hz / 1e9).toFixed(3) + " GHz"; }
  function mhz(hz) { return (hz / 1e6).toFixed(hz < 2e6 ? 1 : 0) + " MHz"; }
  function ago(t, now) {
    var s = Math.max(0, now - t);
    if (s < 3) return "Now";
    if (s < 60) return Math.round(s) + " s";
    if (s < 3600) return Math.round(s / 60) + " min";
    return new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  function classInfo(id) {
    var cs = (S.state && S.state.classes) || [];
    for (var i = 0; i < cs.length; i++) if (cs[i].id === id) return cs[i];
    return null;
  }
  function trackName(tr) {
    if (tr.label && tr.label !== "unknown") { var c = classInfo(tr.label); return c ? c.label : tr.label; }
    if (tr.label === "unknown") return "Unknown signal";
    return tr.hopping ? "Unclassified hopping signal" : "Unclassified signal";
  }
  function trackTone(tr) {
    if (tr.drone) return { color: "#FF3B30", tint: "#FFF1F0" };
    if (tr.label === "unknown" || (!tr.label && tr.hopping)) return { color: "#FF9500", tint: "#FFF6E8" };
    return { color: "#6E6E73", tint: "#F2F2F7" };
  }

  // ---- data -------------------------------------------------------------
  function api(path, body) {
    var opt = body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {};
    return fetch(path, opt).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) {
        if (!r.ok) throw new Error(d.error || ("Request failed (" + r.status + ")"));
        return d;
      });
    });
  }
  function act(path, body, then) {
    return api(path, body || {}).then(function (r) { if (then) then(r); kick(); return r; })
      .catch(function (e) { alert(e.message); });
  }
  var pollTimer = null;
  function poll() {
    clearTimeout(pollTimer);
    var since = S.seq;
    api("/api/state?since=" + since).then(function (st) {
      if (S.gen !== st.control.generation) {  // the detector restarted: start the waterfall afresh
        S.gen = st.control.generation; S.seq = 0; wfRows = [];
        paintWaterfall(true);
        if (since) return;  // fetch again with since=0
      }
      S.state = st;
      addWaterfall(st.waterfall);
      S.seq = st.seq;
      render();
    }).catch(function () {
      $("dev-dot").style.background = "#FF3B30";
      $("dev-status").textContent = "The app is not running. Open it with the Drone Detector launcher.";
      if (!$("job").hidden) {  // the job's last state is stale now: say so rather than look stuck
        $("job").className = "card job failed";
        $("job-title").textContent = "Lost contact with the app";
        $("job-stage").textContent = "The app stopped responding. Look in its Terminal window for an error, then open it again with the Drone Detector launcher.";
        $("job-bar").hidden = true; $("job-cancel").hidden = true; $("job-close").hidden = false;
      }
    }).then(function () { pollTimer = setTimeout(poll, 500); });
  }
  function kick() { pollTimer && clearTimeout(pollTimer); pollTimer = setTimeout(poll, 50); }
  function loadHistory() {
    if (S.hours === 0) return;
    api("/api/detections?hours=" + S.hours).then(function (d) { S.history = d.rows; renderDetections(); });
  }

  // ---- render -----------------------------------------------------------
  var SRC_NAMES = { hackrf: "HackRF One", sim: "Simulator", replay: "Replay", none: "HackRF One" };
  function busyJob() { var j = S.state && S.state.control.job; return j && j.state === "running" ? j : null; }
  function render() {
    var st = S.state, c = st.control, job = busyJob();
    var src = c.running ? st.source : (c.source || "hackrf");
    $("dev-name").textContent = SRC_NAMES[src] || src;
    var dot, txt;
    if (job && job.kind === "calibrate") { dot = "#FF9500"; txt = "Calibrating…"; }
    else if (!c.running) { dot = c.error ? "#FF3B30" : "#AEAEB2"; txt = c.error ? "Could not start" : "Stopped"; }
    else if (st.source_problem) { dot = "#FF3B30"; txt = "Not receiving · check the HackRF"; }
    else {
      dot = st.paused ? "#FF9500" : "#34C759";
      txt = (st.source === "hackrf" ? "Receiving · receive-only" : st.source === "sim" ? "Simulated input · no radio" : "Replaying recorded files") +
        " · " + st.stats.sweeps_per_s + " sweeps/s";
    }
    $("dev-dot").style.background = dot;
    $("dev-status").textContent = txt;
    var other = st.bands.filter(function (b) { return !b.enabled; }).map(function (b) { return b.label; });
    $("dev-antenna").textContent = "Band: " + st.band.label + (other.length ? " · " + other.join(", ") + " later" : "");
    renderControls(); renderJob();
    renderStatus(); drawSpectrum(); renderBand(); renderDetections(); renderAI(); renderRecord();
    if (S.view === "model") { renderModel(); renderTrain(); }
    if (S.view === "settings") renderSettings();
    if (!S.mapDone) renderMap();
  }

  // Start/stop and input choice, in the header, the sidebar and Settings.
  function renderControls() {
    var c = S.state.control, job = busyJob(), calib = job && job.kind === "calibrate";
    var runTxt = c.running ? "Stop detector" : "Start detector";
    [$("btn-run"), $("s-run")].forEach(function (b) { b.textContent = runTxt; b.disabled = !!calib; });
    var seg = $("src-seg"), cur = c.running ? S.state.source : c.source;
    if (seg.childElementCount !== c.sources.length) {
      seg.textContent = "";
      c.sources.forEach(function (x) {
        var b = el("button", "", x.id === "hackrf" ? "HackRF" : x.label); b.type = "button"; b.dataset.src = x.id;
        b.title = "Run the detector on " + x.label;
        seg.appendChild(b);
      });
      var sel = $("s-source"); sel.textContent = "";
      c.sources.forEach(function (x) { var o = el("option", "", x.label); o.value = x.id; sel.appendChild(o); });
    }
    Array.prototype.forEach.call(seg.children, function (b) { b.classList.toggle("on", b.dataset.src === cur); b.disabled = !!calib; });
    if (document.activeElement !== $("s-source") && cur) $("s-source").value = cur;
  }
  function startStop() {
    var c = S.state.control;
    if (c.running) act("/api/stop");
    else act("/api/start", { source: c.source || null });
  }

  // The background job banner: calibration, training, installs, map download.
  function renderJob() {
    var j = S.state.control.job, box = $("job");
    var key = j ? j.kind + j.started : null;
    if (!j || S.jobClosed === key) { box.hidden = true; return; }
    if (j.state !== "running" && S.jobSeen !== key + j.state) {
      S.jobSeen = key + j.state;
      if (j.kind === "calibrate") S.settings = null, loadSettings();
      if (j.kind === "synth" || j.kind === "train") loadSessions();
      if (j.kind === "install") S.settings = null, loadSettings();
      if (j.kind === "tiles") { S.mapDone = false; renderMap(); }
    }
    box.hidden = false;
    box.className = "card job " + j.state;
    var title = j.title;
    if (j.state === "done") title += " finished";
    if (j.state === "failed") title += " failed";
    if (j.state === "cancelled") title += " cancelled";
    $("job-title").textContent = title;
    var stage = j.state === "running" ? (j.stage || "Working") + " · " + elapsed(Date.now() / 1000 - j.started) : j.state === "failed" ? j.error : jobSummary(j);
    $("job-stage").textContent = stage || "";
    var pct = j.state === "done" ? 100 : j.progress == null ? null : Math.round(j.progress * 100);
    $("job-bar").classList.toggle("indeterminate", pct == null && j.state === "running");
    $("job-fill").style.width = (pct == null ? 30 : pct) + "%";
    $("job-bar").hidden = j.state !== "running" && j.state !== "done";
    $("job-cancel").hidden = j.state !== "running";
    $("job-close").hidden = j.state === "running";
    var log = j.log.join("\n");
    if ($("job-log").textContent !== log) $("job-log").textContent = log;
  }
  function elapsed(s) {
    s = Math.max(0, Math.round(s));
    return s < 60 ? s + " s" : Math.floor(s / 60) + " min " + (s % 60 < 10 ? "0" : "") + (s % 60) + " s";
  }
  function jobSummary(j) {
    var r = j.result || {};
    if (j.kind === "train") {
      var s = "New model loaded. Held-out accuracy " + (r.test_accuracy == null ? "not measured" : (r.test_accuracy * 100).toFixed(1) + "% on " + r.n_test + " captures") + ".";
      if (r.warnings && r.warnings.length) s += " " + r.warnings[0];
      return s;
    }
    if (j.kind === "calibrate") return "Noise floor " + r.noise_floor_db + " dB over " + r.sweeps + " sweeps. Details on the Settings page.";
    if (j.kind === "install") return "PyTorch is ready. You can train now.";
    if (j.kind === "synth") return (r.sessions || 0) + " synthetic sessions written. They only test the pipeline.";
    if (j.kind === "tiles") return "Map tiles saved. The map now works offline.";
    return "";
  }

  function renderStatus() {
    var st = S.state, tr = st.tracks;
    var drones = tr.filter(function (t) { return t.drone; });
    var open = tr.filter(function (t) { return !t.drone && (t.label === "unknown" || (!t.label && t.hopping)); });
    var head, sub, c = st.control, job = busyJob();
    if (job && job.kind === "calibrate") { head = "Calibrating"; sub = "Measuring the noise floor; the detector restarts afterwards."; }
    else if (!c.running && c.error) { head = "Could not start the detector"; sub = c.error; }
    else if (!c.running) { head = "Detector stopped"; sub = "Press Start detector to watch " + st.band.label + "."; }
    else if (st.source_problem) { head = "HackRF not responding"; sub = st.source_problem; }
    else if (st.paused) { head = "Scanning paused"; sub = "Press Resume scan to keep watching the band."; }
    else if (drones.length) {
      head = drones.length + (drones.length === 1 ? " drone nearby" : " drones nearby");
      var d = drones[0];
      sub = "Strongest is " + trackName(d) + " on " + ghz(d.center_hz) + " (" + Math.round(d.confidence * 100) + "% confidence).";
    } else if (open.length) {
      head = open.length + (open.length === 1 ? " unidentified signal" : " unidentified signals");
      sub = st.model.loaded ? "No class won the vote yet; watching." : "No AI model loaded, so signals are detected but not classified.";
    } else { head = "No drones detected"; sub = tr.length + " background " + (tr.length === 1 ? "signal" : "signals") + " tracked on " + st.band.label + "."; }
    $("headline").textContent = head;
    $("subline").textContent = sub;
    var idle = !c.running || st.paused;
    $("live-tag").textContent = !c.running ? "· STOPPED" : st.paused ? "· PAUSED" : "· LIVE";
    $("live-tag").classList.toggle("paused", idle);
    $("status-icon").classList.toggle("alert", drones.length > 0);
    $("st-signals").textContent = tr.length;
    $("st-ids").textContent = "—";
    var strong = tr.reduce(function (m, t) { return Math.max(m, t.peak_db); }, -Infinity);
    $("st-strong").textContent = isFinite(strong) ? String(Math.round(strong)).replace("-", "−") : "—";
    var badge = $("nav-badge"); badge.hidden = !drones.length; badge.textContent = drones.length;
  }

  function renderBand() {
    var st = S.state, seg = $("band-seg");
    $("band-range").textContent = (st.band.display_min_hz / 1e9).toFixed(3) + " – " + (st.band.display_max_hz / 1e9).toFixed(4).replace(/0$/, "") + " GHz";
    if (seg.childElementCount !== st.bands.length) {
      seg.textContent = "";
      st.bands.forEach(function (b) {
        var btn = el("button", b.id === st.band.id ? "on" : "", b.enabled ? b.label : b.label + " · soon");
        btn.type = "button"; btn.disabled = !b.enabled;
        if (!b.enabled) btn.title = "Needs the dual-band antenna and a band entry in config/bands.yaml";
        seg.appendChild(btn);
      });
      if (st.bands.length === 1) {
        var soon = el("button", "", "5.8 GHz · soon"); soon.type = "button"; soon.disabled = true;
        soon.title = "Needs the dual-band antenna"; seg.appendChild(soon);
      }
      var ax = $("axis"); ax.textContent = "";
      var lo = st.band.f_min_hz, hi = st.band.f_max_hz;
      for (var i = 0; i <= 4; i++) ax.appendChild(el("span", "", ((lo + (hi - lo) * i / 4) / 1e9).toFixed(3)));
    }
  }

  // Spectrum: canvas line of the latest frame, with the baseline dashed.
  function drawSpectrum() {
    var st = S.state, c = $("spec");
    if (!st || !c.offsetWidth) return;
    var dpr = window.devicePixelRatio || 1, w = c.offsetWidth, h = c.offsetHeight;
    if (c.width !== w * dpr || c.height !== h * dpr) { c.width = w * dpr; c.height = h * dpr; }
    var g = c.getContext("2d"); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, h);
    if (!st.spectrum.length) {
      g.fillStyle = "#6E6E73"; g.font = "13px -apple-system, system-ui, sans-serif"; g.textAlign = "center";
      g.fillText(st.control.running ? "Waiting for the first sweep…" : "Detector stopped", w / 2, h / 2);
      return;
    }
    var lo = -105, hi = -35, n = st.spectrum.length;
    var X = function (i) { return (i + 0.5) / n * w; }, Y = function (v) { return h - (Math.max(lo, Math.min(hi, v)) - lo) / (hi - lo) * h; };
    g.strokeStyle = "#F2F2F7"; g.lineWidth = 1;
    [0.25, 0.5, 0.75].forEach(function (f) { g.beginPath(); g.moveTo(0, h * f); g.lineTo(w, h * f); g.stroke(); });
    var grad = g.createLinearGradient(0, 0, 0, h); grad.addColorStop(0, "rgba(0,122,255,0.28)"); grad.addColorStop(1, "rgba(0,122,255,0)");
    g.beginPath(); g.moveTo(0, h);
    st.spectrum.forEach(function (v, i) { g.lineTo(X(i), Y(v)); });
    g.lineTo(w, h); g.closePath(); g.fillStyle = grad; g.fill();
    g.beginPath(); st.spectrum.forEach(function (v, i) { i ? g.lineTo(X(i), Y(v)) : g.moveTo(X(i), Y(v)); });
    g.strokeStyle = "#007AFF"; g.lineWidth = 2; g.lineJoin = "round"; g.stroke();
    if (st.baseline.length) {
      g.beginPath(); st.baseline.forEach(function (v, i) { i ? g.lineTo(X(i), Y(v)) : g.moveTo(X(i), Y(v)); });
      g.strokeStyle = "#AEAEB2"; g.lineWidth = 1; g.setLineDash([3, 4]); g.stroke(); g.setLineDash([]);
    }
    // Mark the selected track
    var sel = selectedTrack();
    if (sel) {
      var x = (sel.center_hz - st.band.f_min_hz) / (st.band.f_max_hz - st.band.f_min_hz) * w;
      g.strokeStyle = "#FF3B30"; g.setLineDash([3, 4]); g.beginPath(); g.moveTo(x, 0); g.lineTo(x, h); g.stroke(); g.setLineDash([]);
    }
  }

  // Waterfall: newest row at the top, scrolling down.
  var STOPS = [[0xF4, 0xF6, 0xFB], [0xE3, 0xEC, 0xFB], [0xC7, 0xDA, 0xFB], [0x94, 0xBB, 0xFA], [0x5B, 0x8D, 0xEF], [0x7B, 0x61, 0xFF], [0xE0, 0x45, 0x7B]];
  var LUT = (function () {
    var out = new Uint8ClampedArray(256 * 3);
    for (var i = 0; i < 256; i++) {
      var v = Math.max(0, (i / 255 - 0.12) / 0.88) * (STOPS.length - 1), k = Math.min(STOPS.length - 2, Math.floor(v)), f = v - k;
      for (var j = 0; j < 3; j++) out[i * 3 + j] = STOPS[k][j] + (STOPS[k + 1][j] - STOPS[k][j]) * f;
    }
    return out;
  })();
  var wfRows = [];
  function resizeWaterfall() {
    var c = $("wf"); if (!c.offsetWidth || !S.state) return;
    var n = S.state.spectrum.length || 360, rows = Math.round(c.offsetHeight / 2);
    if (c.width !== n || c.height !== rows) { c.width = n; c.height = rows; paintWaterfall(); }
  }
  function addWaterfall(wf) {
    wf.rows.forEach(function (r) {
      var bin = atob(r[1]), a = new Uint8Array(bin.length);
      for (var i = 0; i < bin.length; i++) a[i] = bin.charCodeAt(i);
      wfRows.unshift(a);
    });
    if (wfRows.length > 400) wfRows.length = 400;
    if (wf.rows.length) { resizeWaterfall(); paintWaterfall(); }
  }
  function paintWaterfall(clear) {
    var c = $("wf"); if (!c.width) return;
    if (clear) { var gc = c.getContext("2d"); gc.fillStyle = "#F4F6FB"; gc.fillRect(0, 0, c.width, c.height); $("wf-caption").textContent = "Waterfall"; return; }
    if (!wfRows.length) return;
    var g = c.getContext("2d"), img = g.createImageData(c.width, c.height), d = img.data;
    for (var y = 0; y < c.height && y < wfRows.length; y++) {
      var row = wfRows[y];
      for (var x = 0; x < c.width; x++) {
        var v = row[Math.floor(x * row.length / c.width)], o = (y * c.width + x) * 4;
        d[o] = LUT[v * 3]; d[o + 1] = LUT[v * 3 + 1]; d[o + 2] = LUT[v * 3 + 2]; d[o + 3] = 255;
      }
    }
    for (var yy = wfRows.length; yy < c.height; yy++) for (var xx = 0; xx < c.width; xx++) { var p = (yy * c.width + xx) * 4; d[p] = 0xF4; d[p + 1] = 0xF6; d[p + 2] = 0xFB; d[p + 3] = 255; }
    g.putImageData(img, 0, 0);
    var top = S.state && S.state.tracks.filter(function (t) { return t.hopping; })[0];
    $("wf-caption").textContent = "Waterfall" + (top ? " · " + (top.label && top.label !== "unknown" ? "hopping pattern classified as " + trackName(top) : "hopping signal near " + ghz(top.center_hz)) : "");
  }

  // Detections
  function selectedTrack() {
    var tr = (S.state && S.state.tracks) || [];
    for (var i = 0; i < tr.length; i++) if (tr[i].id === S.selected) return tr[i];
    return tr[0] || null;
  }
  function renderDetections() {
    var st = S.state; if (!st) return;
    var list = $("det-list"), rows, live = S.hours === 0;
    rows = live ? st.tracks : S.history;
    if (S.view === "overview" && rows.length > 6) rows = rows.slice(0, 6);
    $("det-title").textContent = live ? "Detections" : "Detections · last " + (S.hours === 1 ? "hour" : "24 hours");
    list.textContent = "";
    if (!rows.length) { list.appendChild(el("div", "empty", live ? "Nothing above the noise floor right now." : "No detections in this period.")); return; }
    var sel = selectedTrack();
    rows.forEach(function (t) {
      var b = el("button", "det" + (live && sel && sel.id === t.id ? " sel" : "")); b.type = "button";
      var tone = trackTone(t);
      var ic = el("div", "det-icon"); ic.style.background = tone.tint;
      ic.innerHTML = '<svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="' + tone.color + '" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + DRONE_PATH + "</svg>";
      var mid = el("div", "col"); mid.style.flex = "1";
      mid.appendChild(el("span", "det-name", trackName(t)));
      mid.appendChild(el("span", "det-sub", t.summary + " · " + ghz(t.center_hz) + " · " + String(Math.round(t.peak_db)).replace("-", "−") + " dB"));
      var right = el("div", "det-right");
      right.appendChild(el("span", "det-conf", t.label ? Math.round(t.confidence * 100) + "%" : "–"));
      right.appendChild(el("span", "muted tiny", ago(t.last_seen, st.time)));
      b.appendChild(ic); b.appendChild(mid); b.appendChild(right);
      if (live) b.addEventListener("click", function () { S.selected = t.id; renderDetections(); renderAI(); drawSpectrum(); });
      list.appendChild(b);
    });
  }

  function renderAI() {
    var st = S.state, t = selectedTrack(), m = st.model;
    var note = $("ai-note"), bars = $("ai-bars");
    bars.textContent = "";
    note.hidden = true;
    if (!m.loaded) {
      note.hidden = false;
      note.textContent = "No AI model is loaded yet. Signals are detected but not classified. Record labelled sessions, then train a model on the AI Model page.";
    } else if (m.warnings && m.warnings.length) {
      note.hidden = false; note.textContent = m.warnings[0];
    }
    if (!t) { $("ai-label").textContent = "No signal selected"; $("ai-sub").textContent = ""; $("ai-track").textContent = ""; setRing(null); return; }
    $("ai-track").textContent = "Track " + t.id + " · " + ghz(t.center_hz);
    $("ai-label").textContent = trackName(t);
    if (!m.loaded) { $("ai-sub").textContent = t.summary; setRing(null); return; }
    if (!t.label) { $("ai-sub").textContent = "Collecting captures (" + t.votes + " of " + 3 + " needed)"; }
    else if (t.label === "unknown") $("ai-sub").textContent = "No class won 3 of the last 5 captures";
    else $("ai-sub").textContent = "Most likely match · " + t.votes + " captures";
    setRing(t.label ? t.confidence : null);
    if (t.probs) {
      var order = t.probs.map(function (p, i) { return [p, i]; }).sort(function (a, b) { return b[0] - a[0]; });
      order.slice(0, 4).forEach(function (o) {
        var c = classInfo(m.classes[o[1]]), pct = (o[0] * 100);
        var r = el("div", "bar"), top = el("div", "bar-top");
        top.appendChild(el("span", "", c ? c.label : m.classes[o[1]]));
        var v = el("span", "muted", (pct < 10 ? pct.toFixed(1) : Math.round(pct)) + "%"); v.style.fontVariantNumeric = "tabular-nums"; top.appendChild(v);
        var tr = el("div", "bar-track"), f = el("div", "bar-fill"); f.style.width = pct.toFixed(1) + "%"; tr.appendChild(f);
        r.appendChild(top); r.appendChild(tr); bars.appendChild(r);
      });
    }
    if (t.features && t.features.duty_cycle != null) {
      var f = t.features;
      bars.appendChild(el("span", "muted tiny", "Last capture: " + mhz(f.occupied_bw_hz) + " wide, duty " + Math.round(f.duty_cycle * 100) + "%, bursts " + f.burst_ms + " ms, SNR " + f.snr_db + " dB"));
    }
  }
  function setRing(v) {
    var pct = v == null ? 0 : Math.round(v * 100);
    $("ring").style.background = "conic-gradient(#007AFF 0 " + pct + "%, #E8E8ED " + pct + "% 100%)";
    $("ring-v").textContent = v == null ? "–" : pct + "%";
  }

  // Recording
  function classOptions(sel, blank) {
    sel.textContent = "";
    sel.appendChild(el("option", "", blank)).value = "";
    S.state.classes.forEach(function (c) { var o = el("option", "", c.label + (c.drone ? " (drone)" : "")); o.value = c.id; sel.appendChild(o); });
  }
  function renderRecord() {
    var r = S.state.recording, b = $("btn-record"), st = S.state;
    b.classList.toggle("recording", r.on);
    var n = r.count + (r.on && r.target ? " / " + r.target : "");
    $("rec-text").textContent = r.on ? "Recording · " + n : "Record";
    $("f-submit").textContent = r.on ? "Stop recording" : "Start recording";
    $("f-submit").disabled = !!(busyJob() && busyJob().kind === "calibrate");
    var msg = "";
    if (r.on) msg = "Recording " + r.session + " · " + n + " captures" + (r.failures ? " · " + r.failures + " failed" : "");
    else if (r.last) msg = "Saved " + r.last.count + " captures to " + r.last.session + (r.last.failures ? " (" + r.last.failures + " captures failed)" : "") + ".";
    $("rec-status").textContent = msg;
    var recKey = r.on + "|" + (r.last && r.last.session);
    if (S.recKey !== recKey) { S.recKey = recKey; if (S.view === "recordings") loadSessions(); }
    if (!$("f-class").childElementCount) classOptions($("f-class"), "Unlabelled (label later)");
    var srcNote = $("rec-src-note"), src = st.control.running ? st.source : st.control.source;
    srcNote.hidden = src !== "sim";
    srcNote.textContent = "The input is the simulator, so these captures are simulated signals, not your drone. Pick HackRF in the sidebar to record real ones.";
  }
  function toggleRecord(label) {
    var on = !S.state.recording.on;
    act("/api/record", Object.assign({ on: on }, label || {}), function (r) { S.state.recording = r; renderRecord(); loadSessions(); });
  }
  function loadSessions() {
    api("/api/sessions").then(function (d) {
      S.sessions = d.sessions;
      $("cap-dir").textContent = d.dir;
      renderSessions(); renderTrain(true);
    });
  }
  function renderSessions() {
    var tb = $("sess-table").querySelector("tbody"); tb.textContent = "";
    var ss = S.sessions, total = ss.reduce(function (a, s) { return a + s.captures; }, 0);
    $("sess-sum").textContent = ss.length ? ss.length + (ss.length === 1 ? " session · " : " sessions · ") + total + " captures" : "";
    if (!ss.length) { var tr0 = el("tr"), td0 = el("td", "muted", "No sessions yet."); td0.colSpan = 8; tr0.appendChild(td0); tb.appendChild(tr0); return; }
    ss.forEach(function (s) {
      var lab = s.label || {}, tr = el("tr"), c = classInfo(lab["class"]);
      var cls = el("td", lab["class"] ? "" : "warn", c ? c.label : lab["class"] || "Unlabelled");
      tr.appendChild(el("td", "mono", s.session));
      tr.appendChild(cls);
      [lab.drone_model || "", lab.distance_m != null && lab.distance_m !== "" ? lab.distance_m + " m" : "", lab.environment || "",
       s.recording && S.state.recording.on ? S.state.recording.count : s.captures,
       s.synthetic ? "synthetic" : s.recording ? "recording…" : (SRC_NAMES[s.source] || s.source || "")]
        .forEach(function (v) { tr.appendChild(el("td", "", String(v))); });
      var acts = el("td", "acts");
      var eb = el("button", "link", "Edit"); eb.type = "button";
      eb.addEventListener("click", function () { S.editing = S.editing === s.session ? null : s.session; renderSessions(); });
      var db = el("button", "link danger", "Delete"); db.type = "button"; db.disabled = s.recording;
      db.addEventListener("click", function () {
        if (!confirm("Delete session " + s.session + " and its " + s.captures + " captures? This cannot be undone.")) return;
        act("/api/sessions/delete", { session: s.session }, loadSessions);
      });
      acts.appendChild(eb); acts.appendChild(db); tr.appendChild(acts);
      tb.appendChild(tr);
      if (S.editing === s.session) tb.appendChild(editRow(s));
    });
  }
  function editRow(s) {
    var lab = s.label || {}, tr = el("tr", "edit"), td = el("td"); td.colSpan = 8;
    var f = el("form", "form");
    var field = function (name, node) { var l = el("label", "", name); l.appendChild(node); f.appendChild(l); return node; };
    var cls = field("Class", el("select")); classOptions(cls, "Unlabelled"); cls.value = lab["class"] || "";
    var dev = field("Drone or device", el("input")); dev.value = lab.drone_model || "";
    var dist = field("Distance (m)", el("input")); dist.type = "number"; dist.min = 0; dist.value = lab.distance_m == null ? "" : lab.distance_m;
    var env = field("Environment", el("select"));
    ["", "outdoor", "indoor"].forEach(function (v) { var o = el("option", "", v || "–"); o.value = v; env.appendChild(o); }); env.value = lab.environment || "";
    var notes = el("input"); var nl = el("label", "wide", "Notes"); nl.appendChild(notes); f.appendChild(nl); notes.value = lab.notes || "";
    var row = el("div", "wide rec-row"), save = el("button", "pill dark", "Save label"), cancel = el("button", "pill", "Cancel");
    save.type = "submit"; cancel.type = "button";
    cancel.addEventListener("click", function () { S.editing = null; renderSessions(); });
    row.appendChild(save); row.appendChild(cancel); f.appendChild(row);
    f.addEventListener("submit", function (e) {
      e.preventDefault();
      act("/api/sessions/label", { session: s.session, label: { "class": cls.value || null, drone_model: dev.value || null,
        distance_m: dist.value === "" ? null : +dist.value, environment: env.value || null, notes: notes.value || null } },
        function () { S.editing = null; loadSessions(); });
    });
    td.appendChild(f); tr.appendChild(td);
    return tr;
  }

  // Training panel: what data there is per class, and the train button.
  function renderTrain(force) {
    if (!S.state || (!force && S.view !== "model")) return;
    var per = {}, real = S.sessions.some(function (s) { return !s.synthetic && s.label && s.label["class"]; });
    var useSynth = $("t-synth").checked || !real;
    S.state.classes.forEach(function (c) { per[c.id] = { sessions: 0, captures: 0 }; });
    var unlabelled = 0;
    S.sessions.forEach(function (s) {
      var cid = s.label && s.label["class"];
      if (!cid) { unlabelled++; return; }
      if (s.synthetic && !useSynth) return;
      if (!per[cid]) return;
      per[cid].sessions++; per[cid].captures += s.captures;
    });
    var key = JSON.stringify([per, unlabelled, useSynth]);
    if (S.trainKey !== key) {
      S.trainKey = key;
      var tb = $("data-table").querySelector("tbody"); tb.textContent = "";
      S.state.classes.forEach(function (c) {
        var p = per[c.id], tr = el("tr");
        var ready = p.sessions >= 2 ? "Yes" : p.sessions === 1 ? "Needs 1 more session" : "No data";
        [c.label + (c.drone ? " (drone)" : ""), p.sessions, p.captures].forEach(function (v) { tr.appendChild(el("td", "", String(v))); });
        tr.appendChild(el("td", p.sessions >= 2 ? "ok" : "warn", ready));
        tb.appendChild(tr);
      });
      var n = S.state.classes.filter(function (c) { return per[c.id].sessions >= 2; }).length;
      $("train-sum").textContent = n + " of " + S.state.classes.length + " classes ready" +
        (unlabelled ? " · " + unlabelled + " unlabelled " + (unlabelled === 1 ? "session" : "sessions") + " left out" : "") +
        (!real && S.sessions.length ? " · synthetic data only" : "");
    }
    var tools = S.settings ? S.settings.training_tools && !S.settings.training_tools_conflict : true;
    var clash = !!(S.settings && S.settings.training_tools_conflict);
    $("tools-note").hidden = tools;
    $("tools-text").textContent = clash
      ? "PyTorch was installed with pip, and on this Mac it clashes with the conda environment (training stops with an OpenMP error). Repair it once: this swaps in conda-forge's PyTorch (a few hundred MB, needs internet)."
      : "Training needs PyTorch, which is not installed in this Python yet. Install it once (about 300 MB, needs internet), then train offline from then on.";
    $("btn-install").textContent = clash ? "Repair training tools" : "Install training tools";
    var job = busyJob();
    $("btn-train").disabled = !tools || !!job;
    $("btn-synth").disabled = !!job;
    $("btn-install").disabled = !!job;
  }

  // AI model page
  function renderModel() {
    var m = S.state.model, body = $("model-body");
    if (S.modelRendered === JSON.stringify(m)) return;
    S.modelRendered = JSON.stringify(m);
    body.textContent = "";
    if (!m.loaded) {
      body.appendChild(el("p", "muted", "No model yet. The detector still finds and tracks signals; it just cannot name them."));
      body.appendChild(el("p", "muted", "To make one: record labelled sessions on the Recordings page, then press Train model above. The model is written to models/cnn.onnx with a model card beside it, and the detector loads it straight away."));
      return;
    }
    (m.warnings || []).forEach(function (w) { body.appendChild(el("div", "note", w)); });
    var kv = el("dl", "kv");
    var add = function (k, v) { kv.appendChild(el("dt", "", k)); kv.appendChild(el("dd", "", v)); };
    add("Trained", m.trained_at ? new Date(m.trained_at).toLocaleString() : "?");
    add("Parameters", String(m.params));
    add("Training data", (m.data.real_samples || 0) + " real captures, " + ((m.data.sessions_used || 0)) + " sessions" + (m.synthetic_only ? " (synthetic only)" : ""));
    add("Test split", m.metrics.split || "?");
    add("Held-out accuracy", m.metrics.test_accuracy == null ? "not measured" : (m.metrics.test_accuracy * 100).toFixed(1) + "% on " + m.metrics.n_test + " captures" + (m.synthetic_only ? " of synthetic signals" : ""));
    body.appendChild(kv);
    var wrap = el("div", "table-wrap"), t = el("table", "table"), h = el("tr");
    ["Class", "Sessions", "Captures", "Held-out recall"].forEach(function (x) { h.appendChild(el("th", "", x)); });
    var thead = el("thead"); thead.appendChild(h); t.appendChild(thead);
    var tb = el("tbody");
    m.classes.forEach(function (cid) {
      var c = classInfo(cid), pc = (m.data.per_class || {})[cid] || {}, rc = (m.metrics.per_class_recall || {})[cid];
      var tr = el("tr");
      [c ? c.label : cid, pc.sessions || 0, pc.samples || 0, rc == null ? "–" : Math.round(rc * 100) + "%"].forEach(function (v) { tr.appendChild(el("td", "", String(v))); });
      tb.appendChild(tr);
    });
    t.appendChild(tb); wrap.appendChild(t); body.appendChild(wrap);
  }

  function renderSettings() {
    var st = S.state, body = $("settings-body");
    var key = JSON.stringify([st.band, st.source, st.receiver, st.stats.noise_floor_db && st.stats.noise_floor_db.toFixed(0), Math.round(st.stats.capture_duty * 100)]);
    if (S.settingsKey === key) return;
    S.settingsKey = key; body.textContent = "";
    var kv = el("dl", "kv");
    var add = function (k, v) { kv.appendChild(el("dt", "", k)); kv.appendChild(el("dd", "", v)); };
    add("Active band", st.band.label + " (" + (st.band.f_min_hz / 1e6) + "–" + (st.band.f_max_hz / 1e6) + " MHz, " + (st.band.bin_hz / 1e3) + " kHz bins)");
    add("Other bands", st.bands.filter(function (b) { return !b.enabled; }).map(function (b) { return b.label; }).join(", ") || "5.8 GHz: add ism_5g8 to config/bands.yaml when the dual-band antenna arrives");
    add("Input", st.control.running ? SRC_NAMES[st.source] || st.source : "stopped");
    add("Noise floor", st.stats.noise_floor_db == null ? "–" : st.stats.noise_floor_db.toFixed(1) + " dB");
    add("Capture duty", Math.round(st.stats.capture_duty * 100) + "% of time in IQ capture");
    add("Receiver", (st.receiver.lat || 0) + ", " + (st.receiver.lon || 0));
    if (S.settings) add("Config folder", S.settings.root + "/config");
    body.appendChild(kv);
  }

  function loadSettings() {
    api("/api/settings").then(function (d) {
      S.settings = d;
      fillSettings(d);
      renderTrain(true);
    });
  }
  function fillSettings(d) {
    var lna = $("s-lna"), vga = $("s-vga");
    if (!lna.childElementCount) {
      for (var i = 0; i <= 40; i += 8) { var o = el("option", "", String(i)); o.value = i; lna.appendChild(o); }
      for (var j = 0; j <= 62; j += 2) { var p = el("option", "", String(j)); p.value = j; vga.appendChild(p); }
    }
    lna.value = d.gain.lna_db; vga.value = d.gain.vga_db; $("s-amp").checked = !!d.gain.amp; $("amp-note").hidden = !d.gain.amp;
    var rc = d.receiver || {};
    $("s-name").value = rc.name || "Receiver";
    $("s-lat").value = rc.lat || ""; $("s-lon").value = rc.lon || "";
    var cal = d.calibration, cb = $("cal-body"); cb.textContent = "";
    if (!cal) { $("cal-when").textContent = "Not run yet."; return; }
    $("cal-when").textContent = "Last run " + new Date(cal.time * 1000).toLocaleString() + " on " + (SRC_NAMES[cal.source] || cal.source) +
      (cal.gain ? " · LNA " + cal.gain.lna_db + " / VGA " + cal.gain.vga_db + (cal.gain.amp ? " / amp on" : "") : "");
    var kv = el("dl", "kv");
    kv.appendChild(el("dt", "", "Noise floor")); kv.appendChild(el("dd", "", cal.noise_floor_db + " dB over " + cal.sweeps + " sweeps (HackRF dB, not calibrated dBm)"));
    cb.appendChild(kv);
    var wrap = el("div", "table-wrap"), t = el("table", "table"), h = el("tr");
    ["Frequency", "Peak", "Above floor"].forEach(function (x) { h.appendChild(el("th", "", x)); });
    var th = el("thead"); th.appendChild(h); t.appendChild(th);
    var tb = el("tbody");
    cal.strongest.forEach(function (b) {
      var tr = el("tr");
      [b.mhz.toFixed(2) + " MHz", b.db + " dB", "+" + b.above_floor_db + " dB"].forEach(function (v) { tr.appendChild(el("td", "", v)); });
      tb.appendChild(tr);
    });
    t.appendChild(tb); wrap.appendChild(t); cb.appendChild(wrap);
  }

  // Map: offline tiles from data/tiles/{z}/{x}/{y}.png, served by the detector.
  function renderMap() {
    if (!S.state || !$("map").offsetWidth) return;
    var rc = S.state.receiver || {}, lat = +rc.lat || 0, lon = +rc.lon || 0;
    var note = $("map-note");
    if (!lat && !lon) note.textContent = "Set your receiver's location on the Settings page. ";
    else note.textContent = "";
    note.textContent += "Drone and pilot positions need DroneID decoding, which comes after v1.";
    $("map-sub").textContent = rc.name || "Receiver";
    api("/api/tiles").then(function (d) {
      var box = $("map-tiles"); box.textContent = "";
      if (!d.zooms.length) { note.textContent = "No offline map yet: press Download map on the Settings page once while online. " + note.textContent; S.mapDone = true; return; }
      var z = d.zooms.filter(function (q) { return q <= 16; }).pop() || d.zooms[0], v = "?v=" + d.version;
      var n = Math.pow(2, z), xf = (lon + 180) / 360 * n, r = lat * Math.PI / 180;
      var yf = (1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2 * n;
      var W = $("map").offsetWidth, H = $("map").offsetHeight;
      var x0 = Math.floor(xf - W / 512) - 1, x1 = Math.floor(xf + W / 512) + 1, y0 = Math.floor(yf - H / 512) - 1, y1 = Math.floor(yf + H / 512) + 1;
      for (var x = x0; x <= x1; x++) for (var y = y0; y <= y1; y++) {
        if (y < 0 || y >= n) continue;
        var img = new Image(); img.alt = ""; img.src = "/tiles/" + z + "/" + ((x % n) + n) % n + "/" + y + ".png" + v;
        img.style.left = (W / 2 + (x - xf) * 256) + "px"; img.style.top = (H / 2 + (y - yf) * 256) + "px";
        img.onerror = function () { this.remove(); };
        box.appendChild(img);
      }
      var attr = el("span", "map-attr", "© OpenStreetMap contributors"); box.appendChild(attr);
      S.mapDone = true;
    });
  }

  // ---- controls ---------------------------------------------------------
  function setRange(h) {
    S.hours = h;
    Array.prototype.forEach.call($("range").children, function (b) { b.classList.toggle("on", +b.dataset.hours === h); });
    loadHistory(); renderDetections();
  }
  $("range").addEventListener("click", function (e) { var b = e.target.closest("button"); if (b) setRange(+b.dataset.hours); });
  $("btn-run").addEventListener("click", startStop);
  $("s-run").addEventListener("click", startStop);
  $("src-seg").addEventListener("click", function (e) {
    var b = e.target.closest("button"); if (!b || b.disabled) return;
    var c = S.state.control;
    if (c.running && S.state.source === b.dataset.src) return;
    act("/api/start", { source: b.dataset.src });
  });
  $("s-source").addEventListener("change", function () { act("/api/start", { source: $("s-source").value }); });
  $("job-cancel").addEventListener("click", function () { act("/api/job/cancel"); });
  $("job-close").addEventListener("click", function () { var j = S.state.control.job; S.jobClosed = j.kind + j.started; renderJob(); });
  $("train-form").addEventListener("submit", function (e) {
    e.preventDefault();
    act("/api/train", { epochs: +$("t-epochs").value || 20, include_synthetic: $("t-synth").checked });
  });
  $("t-synth").addEventListener("change", function () { renderTrain(true); });
  $("btn-synth").addEventListener("click", function () {
    if (confirm("Write simulated sessions for every class? They are marked synthetic, and training leaves them out once you have real recordings.")) act("/api/synth");
  });
  $("btn-install").addEventListener("click", function () { act("/api/install-training"); });
  $("btn-cal").addEventListener("click", function () { act("/api/calibrate"); });
  $("s-amp").addEventListener("change", function () { $("amp-note").hidden = !$("s-amp").checked; });
  $("gain-form").addEventListener("submit", function (e) {
    e.preventDefault();
    act("/api/settings/gain", { lna_db: +$("s-lna").value, vga_db: +$("s-vga").value, amp: $("s-amp").checked }, function (r) {
      $("gain-msg").textContent = "Saved" + (r.restarted ? ", detector restarted." : ".");
    });
  });
  $("loc-form").addEventListener("submit", function (e) {
    e.preventDefault();
    act("/api/settings/receiver", { lat: +$("s-lat").value, lon: +$("s-lon").value, name: $("s-name").value }, function () {
      $("loc-msg").textContent = "Saved."; S.mapDone = false; S.settingsKey = null;
    });
  });
  $("btn-geo").addEventListener("click", function () {
    if (!navigator.geolocation) { $("loc-msg").textContent = "This browser cannot give a location; type it in."; return; }
    $("loc-msg").textContent = "Asking the browser…";
    navigator.geolocation.getCurrentPosition(function (p) {
      $("s-lat").value = p.coords.latitude.toFixed(6); $("s-lon").value = p.coords.longitude.toFixed(6);
      $("loc-msg").textContent = "Filled in (±" + Math.round(p.coords.accuracy) + " m). Press Save location.";
    }, function (err) { $("loc-msg").textContent = "No location from the browser (" + err.message + "). Type it in, e.g. from a maps app."; },
    { timeout: 15000 });
  });
  try { $("s-tileurl").value = localStorage.getItem("dronedet.tileurl") || ""; } catch (e) { /* storage off */ }
  $("btn-tiles").addEventListener("click", function () {
    var url = $("s-tileurl").value.trim();
    try { localStorage.setItem("dronedet.tileurl", url); } catch (e) { /* storage off */ }
    act("/api/tiles", { radius_km: +$("s-radius").value, url: url || null });
  });
  $("btn-tiles-del").addEventListener("click", function () {
    if (!confirm("Delete the downloaded map tiles? The map stays blank until you download it again.")) return;
    act("/api/tiles/delete", {}, function () { $("tiles-msg").textContent = "Deleted."; S.mapDone = false; renderMap(); });
  });
  $("btn-quit").addEventListener("click", function () {
    if (!confirm("Quit the drone detector? The dashboard stops until you open the launcher again.")) return;
    api("/api/shutdown", {}).then(function () { document.body.innerHTML = '<p style="padding:40px;font:15px -apple-system,system-ui,sans-serif">The drone detector has stopped. You can close this tab.</p>'; });
  });
  $("btn-record").addEventListener("click", function () {
    if (S.state.recording.on) return toggleRecord();
    location.hash = "#recordings";
  });
  $("rec-form").addEventListener("submit", function (e) {
    e.preventDefault();
    if (S.state.recording.on) return toggleRecord();
    var mode = $("f-mode").value;
    toggleRecord({ "class": $("f-class").value || null, drone_model: $("f-model").value || null,
                   distance_m: $("f-dist").value ? +$("f-dist").value : null, environment: $("f-env").value || null,
                   notes: $("f-notes").value || null, mode: mode, count: +$("f-count").value || null });
  });
  window.addEventListener("hashchange", setView);
  window.addEventListener("resize", function () { S.mapDone = false; drawSpectrum(); resizeWaterfall(); renderMap(); });
  setInterval(loadHistory, 5000);

  buildNav(); setView(); poll();
})();
