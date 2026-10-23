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

  var S = { seq: 0, state: null, view: "overview", hours: 0, selected: null, history: [], wfInit: false };

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
    if (v === "recordings") loadSessions();
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
    return fetch(path, opt).then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); });
  }
  function poll() {
    api("/api/state?since=" + S.seq).then(function (st) {
      S.state = st;
      addWaterfall(st.waterfall);
      S.seq = st.seq;
      render();
      $("dev-dot").style.background = st.paused ? "#FF9500" : "#34C759";
    }).catch(function () {
      $("dev-dot").style.background = "#FF3B30";
      $("dev-status").textContent = "Detector not responding";
    }).then(function () { setTimeout(poll, 500); });
  }
  function loadHistory() {
    if (S.hours === 0) return;
    api("/api/detections?hours=" + S.hours).then(function (d) { S.history = d.rows; renderDetections(); });
  }

  // ---- render -----------------------------------------------------------
  function render() {
    var st = S.state;
    $("dev-name").textContent = st.source === "hackrf" ? "HackRF One" : st.source === "sim" ? "Simulator" : "Replay";
    $("dev-status").textContent = (st.source === "hackrf" ? "Connected · Receive-only" : st.source === "sim" ? "Simulated input · no radio" : "Replaying recorded files") +
      " · " + st.stats.sweeps_per_s + " sweeps/s";
    var other = st.bands.filter(function (b) { return !b.enabled; }).map(function (b) { return b.label; });
    $("dev-antenna").textContent = "Band: " + st.band.label + (other.length ? " · " + other.join(", ") + " later" : "");
    renderStatus(); drawSpectrum(); renderBand(); renderDetections(); renderAI(); renderRecord();
    if (S.view === "model") renderModel();
    if (S.view === "settings") renderSettings();
    if (!S.mapDone) renderMap();
  }

  function renderStatus() {
    var st = S.state, tr = st.tracks;
    var drones = tr.filter(function (t) { return t.drone; });
    var open = tr.filter(function (t) { return !t.drone && (t.label === "unknown" || (!t.label && t.hopping)); });
    var head, sub;
    if (st.paused) { head = "Scanning paused"; sub = "Press Resume scan to keep watching the band."; }
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
    $("live-tag").textContent = st.paused ? "· PAUSED" : "· LIVE";
    $("live-tag").classList.toggle("paused", st.paused);
    $("status-icon").classList.toggle("alert", drones.length > 0);
    $("st-signals").textContent = tr.length;
    $("st-ids").textContent = "—";
    var strong = tr.reduce(function (m, t) { return Math.max(m, t.peak_db); }, -Infinity);
    $("st-strong").textContent = isFinite(strong) ? String(Math.round(strong)).replace("-", "−") : "—";
    var badge = $("nav-badge"); badge.hidden = !drones.length; badge.textContent = drones.length;
    $("btn-pause").textContent = st.paused ? "Resume scan" : "Pause scan";
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
    if (!st || !st.spectrum.length || !c.offsetWidth) return;
    var dpr = window.devicePixelRatio || 1, w = c.offsetWidth, h = c.offsetHeight;
    if (c.width !== w * dpr) { c.width = w * dpr; c.height = h * dpr; }
    var g = c.getContext("2d"); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, h);
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
  function paintWaterfall() {
    var c = $("wf"); if (!c.width || !wfRows.length) return;
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
      note.textContent = "No AI model is loaded yet. Signals are detected but not classified. Record and label captures, then run `dronedet train`.";
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
  function renderRecord() {
    var r = S.state.recording, b = $("btn-record");
    b.classList.toggle("recording", r.on);
    $("rec-text").textContent = r.on ? "Recording · " + r.count : "Record";
    $("f-submit").textContent = r.on ? "Stop recording (" + r.count + " captures)" : "Start recording";
    var sel = $("f-class");
    if (!sel.childElementCount) {
      sel.appendChild(el("option", "", "Unlabelled (label later)")).value = "";
      S.state.classes.forEach(function (c) { var o = el("option", "", c.label + (c.drone ? " (drone)" : "")); o.value = c.id; sel.appendChild(o); });
    }
  }
  function toggleRecord(label) {
    var on = !S.state.recording.on;
    api("/api/record", Object.assign({ on: on }, label || {})).then(function (r) { S.state.recording = r; renderRecord(); loadSessions(); });
  }
  function loadSessions() {
    api("/api/sessions").then(function (d) {
      $("cap-dir").textContent = d.dir;
      var tb = $("sess-table").querySelector("tbody"); tb.textContent = "";
      if (!d.sessions.length) { var tr = el("tr"), td = el("td", "muted", "No sessions yet."); td.colSpan = 5; tr.appendChild(td); tb.appendChild(tr); return; }
      d.sessions.forEach(function (s) {
        var tr = el("tr"), c = classInfo(s.label && s.label["class"]);
        [s.session, c ? c.label : (s.label && s.label["class"]) || "unlabelled", (s.label && s.label.drone_model) || "", s.captures, s.synthetic ? "synthetic" : s.source]
          .forEach(function (v) { tr.appendChild(el("td", "", String(v))); });
        tb.appendChild(tr);
      });
    });
  }

  // AI model page
  function renderModel() {
    var m = S.state.model, body = $("model-body");
    if (S.modelRendered === JSON.stringify(m)) return;
    S.modelRendered = JSON.stringify(m);
    body.textContent = "";
    if (!m.loaded) {
      body.appendChild(el("p", "muted", "No model yet. The detector still finds and tracks signals; it just cannot name them."));
      body.appendChild(el("p", "muted", "To make one: record labelled sessions (Recordings page or `dronedet record`), then run `dronedet train`. The model is written to models/cnn.onnx with a model card beside it, and is picked up on the next start."));
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
    if (body.childElementCount) return;
    var kv = el("dl", "kv");
    var add = function (k, v) { kv.appendChild(el("dt", "", k)); kv.appendChild(el("dd", "", v)); };
    add("Active band", st.band.label + " (" + (st.band.f_min_hz / 1e6) + "–" + (st.band.f_max_hz / 1e6) + " MHz, " + (st.band.bin_hz / 1e3) + " kHz bins)");
    add("Other bands", st.bands.filter(function (b) { return !b.enabled; }).map(function (b) { return b.label; }).join(", ") || "5.8 GHz: add ism_5g8 to config/bands.yaml when the dual-band antenna arrives");
    add("Input", st.source);
    add("Noise floor", st.stats.noise_floor_db == null ? "–" : st.stats.noise_floor_db.toFixed(1) + " dB");
    add("Capture duty", Math.round(st.stats.capture_duty * 100) + "% of time in IQ capture");
    add("Receiver", (st.receiver.lat || 0) + ", " + (st.receiver.lon || 0));
    body.appendChild(kv);
  }

  // Map: offline tiles from data/tiles/{z}/{x}/{y}.png, served by the detector.
  function renderMap() {
    if (!S.state || !$("map").offsetWidth) return;
    var rc = S.state.receiver || {}, lat = +rc.lat || 0, lon = +rc.lon || 0;
    var note = $("map-note");
    if (!lat && !lon) note.textContent = "Set your receiver's lat/lon in config/app.yaml. ";
    else note.textContent = "";
    note.textContent += "Drone and pilot positions need DroneID decoding, which comes after v1.";
    $("map-sub").textContent = rc.name || "Receiver";
    api("/api/tiles").then(function (d) {
      var box = $("map-tiles"); box.textContent = "";
      if (!d.zooms.length) { note.textContent = "No offline map tiles yet: run tools/cache_tiles.py once while online. " + note.textContent; S.mapDone = true; return; }
      var z = d.zooms.filter(function (q) { return q <= 16; }).pop() || d.zooms[0];
      var n = Math.pow(2, z), xf = (lon + 180) / 360 * n, r = lat * Math.PI / 180;
      var yf = (1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2 * n;
      var W = $("map").offsetWidth, H = $("map").offsetHeight;
      var x0 = Math.floor(xf - W / 512) - 1, x1 = Math.floor(xf + W / 512) + 1, y0 = Math.floor(yf - H / 512) - 1, y1 = Math.floor(yf + H / 512) + 1;
      for (var x = x0; x <= x1; x++) for (var y = y0; y <= y1; y++) {
        if (y < 0 || y >= n) continue;
        var img = new Image(); img.alt = ""; img.src = "/tiles/" + z + "/" + ((x % n) + n) % n + "/" + y + ".png";
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
  $("btn-pause").addEventListener("click", function () { api("/api/pause", { paused: !S.state.paused }); });
  $("btn-record").addEventListener("click", function () {
    if (S.state.recording.on) return toggleRecord();
    location.hash = "#recordings";
  });
  $("rec-form").addEventListener("submit", function (e) {
    e.preventDefault();
    if (S.state.recording.on) return toggleRecord();
    toggleRecord({ "class": $("f-class").value || null, drone_model: $("f-model").value || null,
                   distance_m: $("f-dist").value ? +$("f-dist").value : null, environment: $("f-env").value || null,
                   notes: $("f-notes").value || null });
  });
  window.addEventListener("hashchange", setView);
  window.addEventListener("resize", function () { S.mapDone = false; drawSpectrum(); resizeWaterfall(); renderMap(); });
  setInterval(loadHistory, 5000);

  buildNav(); setView(); poll();
})();
