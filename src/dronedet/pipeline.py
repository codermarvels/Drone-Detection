"""The detector loop: sweep -> baseline -> candidates -> tracks -> IQ capture -> CNN -> vote."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .classify import decide, load_classifier
from .classify.voting import UNKNOWN
from .config import Config
from .detect import Baseline, Track, Tracker, find_candidates
from .features import iq_features, tile
from .output.store import Store
from .radio.base import FrameAggregator, Source
from .recording import Recorder

log = logging.getLogger(__name__)

WF_MIN_DB, WF_MAX_DB = -105.0, -35.0


@dataclass
class Alert:
    t: float
    track_id: int
    label: str
    text: str


@dataclass
class State:
    """What the dashboard reads. Guarded by Pipeline.lock."""

    seq: int = 0
    spectrum: list[float] = field(default_factory=list)
    baseline: list[float] = field(default_factory=list)
    waterfall: deque = field(default_factory=lambda: deque(maxlen=240))  # (seq, bytes)
    sweeps: int = 0
    captures: int = 0
    capture_time: float = 0.0
    alerts: deque = field(default_factory=lambda: deque(maxlen=50))


class Pipeline:
    def __init__(self, cfg: Config, source: Source, model_path: Path | None = None, store: Store | None = None):
        self.cfg = cfg
        self.band = cfg.band
        self.source = source
        d, c = cfg.app["detect"], cfg.app["capture"]
        self.threshold_db = float(d["threshold_db"])
        self.min_bins = int(d["min_bins"])
        self.frame_s = float(d["frame_ms"]) / 1000
        self.max_duty = float(c["max_duty"])
        self.cap_interval = float(c["per_track_interval_s"])
        self.caps_per_track = int(c["captures_per_track"])
        self.decision = cfg.decision
        self.bin_centers = self.band.bin_centers()
        self.baseline: Baseline | None = None
        self._baseline_window = float(d["baseline_s"])
        self.tracker = Tracker(timeout_s=float(d["track_timeout_s"]))
        self.agg = FrameAggregator(self.frame_s)
        self.model_path = Path(model_path or cfg.path("model"))
        self.clf = load_classifier(self.model_path)
        self.drone_ids = {k.id for k in cfg.classes if k.drone}
        self.store = store
        self.state = State()
        self.lock = threading.Lock()
        self.paused = False
        self.recorder: Recorder | None = None
        self.rec_mode = "tracks"          # "tracks": captures the detector takes; "band": step across the band
        self.rec_target: int | None = None
        self.rec_interval = 0.2           # band mode: source seconds between captures, like `dronedet record`
        self._rec_last = -1e9
        self._rec_failures = 0
        self.last_recording: dict | None = None
        self.run_id = time.strftime("%Y%m%d-%H%M%S")
        self._t0: float | None = None
        self._wall0 = time.time()
        self._last_flush = -1e9
        self._last_sweep_t: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- time -------------------------------------------------------------
    def wall(self, t: float) -> float:
        """Source time -> wall-clock time (sim and replay run on their own clock)."""
        return self._wall0 + (t - (self._t0 or t))

    # -- control ----------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, daemon=True, name="pipeline")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.source.close()
        if self._thread:
            self._thread.join(timeout=5)
        self._flush(force=True)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def reload_model(self) -> bool:
        """Load the model file again (after training) and restart every track's vote."""
        clf = load_classifier(self.model_path)
        with self.lock:
            self.clf = clf
            for tr in self.tracker.active():
                tr.votes.clear()
                tr.label, tr.confidence, tr.probs = None, 0.0, None
        return clf is not None

    def set_recording(self, on: bool, label: dict | None = None, mode: str = "tracks",
                      target: int | None = None) -> dict:
        """Start or stop a recording session.

        mode "tracks" saves every capture the detector takes of the signals it
        tracks. mode "band" steps the 20 MHz capture window across the band
        every rec_interval seconds, like `dronedet record`, so a hopping link
        is caught wherever it is; it stops by itself after `target` captures.
        """
        with self.lock:
            if on and self.recorder is None:
                b = self.band
                self.recorder = Recorder(self.cfg.path("data") / "captures", label=label, band=b.id,
                                         gain={"lna_db": b.lna_db, "vga_db": b.vga_db, "amp": b.amp},
                                         source=self.source.name)
                self.rec_mode = "band" if mode == "band" else "tracks"
                self.rec_target = int(target) if target else None
                self._rec_last = -1e9
                self._rec_failures = 0
            elif not on and self.recorder is not None:
                self._finish_recording()
            return self.recording_status()

    def _finish_recording(self) -> None:
        r = self.recorder
        self.recorder = None
        if r is not None:
            self.last_recording = {"session": r.dir.name, "count": r.count, "failures": self._rec_failures}

    def recording_status(self) -> dict:
        r = self.recorder
        return {"on": r is not None, "session": r.dir.name if r else None, "count": r.count if r else 0,
                "mode": self.rec_mode, "target": self.rec_target, "failures": self._rec_failures,
                "last": self.last_recording}

    # -- loop -------------------------------------------------------------
    def run(self) -> None:
        try:
            for sw in self.source.frames():
                if self._stop.is_set():
                    break
                self.step(sw.t, sw.power_db)
        except Exception:
            log.exception("pipeline stopped")
        finally:
            self._flush(force=True)

    def step(self, t: float, power_db: np.ndarray) -> None:
        if self._t0 is None:
            self._t0 = t
            self._last_sweep_t = t
        if self.baseline is None:
            dt = max(1e-3, t - (self._last_sweep_t or t)) if self.state.sweeps else 0.02
            self.baseline = Baseline(len(power_db), frame_s=dt, window_s=self._baseline_window)
        self._last_sweep_t = t
        with self.lock:
            self.state.sweeps += 1
        if self.paused:
            return
        if self.recorder is not None and self.rec_mode == "band":
            self._record_band(t)
        level = self.baseline.update(power_db)
        frame = self.agg.add(t, power_db)
        if frame is not None:
            self._push_display(frame.power_db, level)
        if not self.baseline.ready:
            return
        cands = find_candidates(power_db, level, self.bin_centers, self.band.bin_hz,
                                self.threshold_db, self.min_bins)
        with self.lock:
            self.tracker.update(t, cands)
        self._maybe_capture(t)
        self._flush(t)

    def _push_display(self, power: np.ndarray, level: np.ndarray) -> None:
        row = np.clip((power - WF_MIN_DB) / (WF_MAX_DB - WF_MIN_DB) * 255, 0, 255).astype(np.uint8)
        with self.lock:
            s = self.state
            s.seq += 1
            s.spectrum = [round(float(v), 1) for v in power]
            s.baseline = [round(float(v), 1) for v in level]
            s.waterfall.append((s.seq, row.tobytes()))

    # -- capture and classify --------------------------------------------
    def _needs_capture(self, tr: Track, t: float) -> bool:
        if not tr.confirmed or t - tr.last_capture < self.cap_interval:
            return False
        if self.recorder is not None:
            return True
        if self.clf is None:
            return False
        settled = tr.label not in (None, UNKNOWN) and tr.captures >= self.caps_per_track
        return not settled or t - tr.last_capture > 10 * self.cap_interval  # re-check settled tracks slowly

    def _band_centers(self) -> list[float]:
        half = self.band.sample_rate / 2
        return list(np.arange(self.band.f_min_hz + half, self.band.f_max_hz - half + 1, self.band.sample_rate))

    def _record_band(self, t: float) -> None:
        rec = self.recorder
        if rec is None or t - self._rec_last < self.rec_interval:
            return
        self._rec_last = t
        centers = self._band_centers()
        cap = self.source.capture(float(centers[rec.count % len(centers)]))
        if cap is None:
            self._rec_failures += 1
            return
        with self.lock:
            self.state.captures += 1
            self.state.capture_time += cap.duration_s
        rec.add(cap, extra={"mode": "band", "truth_hint": cap.meta.get("sim_truth")})
        if self.rec_target and rec.count >= self.rec_target:
            with self.lock:
                self._finish_recording()

    def _maybe_capture(self, t: float) -> None:
        if self.recorder is not None and self.rec_mode == "band":
            return  # the radio is busy recording
        elapsed = max(t - (self._t0 or t), 1e-3)
        if self.state.capture_time / elapsed > self.max_duty:
            return
        with self.lock:
            due = [tr for tr in self.tracker.active() if self._needs_capture(tr, t)]
        if not due:
            return
        tr = min(due, key=lambda x: (x.captures, -x.peak_db))
        cap = self.source.capture(self._capture_center(tr))
        tr.last_capture = t
        if cap is None:
            return
        with self.lock:
            self.state.captures += 1
            self.state.capture_time += cap.duration_s
        self._classify(tr, cap)
        rec = self.recorder
        if rec is not None:
            rec.add(cap, track_id=tr.id, extra={"predicted": tr.label, "truth_hint": cap.meta.get("sim_truth")})
            if self.rec_target and rec.count >= self.rec_target:
                with self.lock:
                    self._finish_recording()

    def _capture_center(self, tr: Track) -> float:
        """Where to put the 20 MHz capture window for this track.

        A fixed-channel track is captured on its centre. For a hopping
        track, pick the window that holds the most of its recent hits while
        overlapping the least with other fixed-channel tracks (Wi-Fi), so
        the tile shows the hopping link rather than the neighbour.
        """
        half = self.band.sample_rate / 2
        lo, hi = self.band.f_min_hz + half, self.band.f_max_hz - half
        if not tr.hopping:
            return float(np.clip(tr.center_hz, lo, hi))
        centers = np.arange(lo, hi + 1, 2.5e6)
        hits = np.array([h[1] for h in list(tr.hits)[-200:]])
        score = np.array([np.mean(np.abs(hits - c) < half) for c in centers])
        with self.lock:
            others = [o for o in self.tracker.active() if o is not tr and o.confirmed and not o.hopping]
        for o in others:
            a, b = o.center_hz - o.width_hz / 2, o.center_hz + o.width_hz / 2
            ov = np.clip(np.minimum(b, centers + half) - np.maximum(a, centers - half), 0, None) / (2 * half)
            score -= 1.5 * ov
        return float(centers[int(np.argmax(score))])

    def _classify(self, tr: Track, cap) -> None:
        feats = iq_features(cap.iq, cap.sample_rate)
        tr.captures += 1
        tr.features = feats
        if self.clf is None:
            return
        probs = self.clf.predict(tile(cap.iq))[0]
        prev = tr.label
        with self.lock:
            tr.votes.append(probs)
            d = decide(list(tr.votes), self.clf.classes, self.decision["min_votes"], self.decision["min_prob"])
            tr.label, tr.confidence, tr.probs = d.label, d.confidence, d.mean_probs
            if tr.label in self.drone_ids and prev != tr.label:
                info = self.cfg.class_by_id(tr.label)
                text = f"{info.label if info else tr.label} at {tr.center_hz / 1e9:.3f} GHz"
                self.state.alerts.append(Alert(self.wall(tr.last_seen), tr.id, tr.label, text))
                log.warning("ALERT: %s (track %d, %.0f%%)", text, tr.id, 100 * tr.confidence)

    # -- outputs ----------------------------------------------------------
    def track_row(self, tr: Track) -> dict:
        info = self.cfg.class_by_id(tr.label) if tr.label else None
        return {
            "run": self.run_id, "id": tr.id,
            "first_seen": self.wall(tr.first_seen), "last_seen": self.wall(tr.last_seen),
            "center_hz": tr.center_hz, "width_hz": tr.width_hz, "peak_db": tr.peak_db,
            "hopping": int(tr.hopping), "label": tr.label, "confidence": tr.confidence,
            "drone": int(bool(info and info.drone)), "captures": tr.captures, "summary": tr.summary(),
        }

    def _flush(self, t: float | None = None, force: bool = False) -> None:
        if self.store is None:
            return
        if not force and t is not None and t - self._last_flush < 1.0:
            return
        if t is not None:
            self._last_flush = t
        with self.lock:
            rows = [self.track_row(tr) for tr in self.tracker.active() if tr.confirmed]
            rows += [self.track_row(tr) for tr in self.tracker.closed if tr.confirmed]
            self.tracker.closed.clear()
        self.store.upsert(rows)

    def snapshot(self, since: int = 0) -> dict:
        """Everything the dashboard needs, as plain JSON types."""
        import base64

        with self.lock:
            s = self.state
            tracks = [self.track_row(tr) | {"probs": tr.probs, "features": tr.features, "votes": len(tr.votes)}
                      for tr in self.tracker.active() if tr.confirmed]
            rows = [(q, base64.b64encode(r).decode()) for q, r in s.waterfall if q > since]
            elapsed = max((self._last_sweep_t or 0) - (self._t0 or 0), 1e-3)
            out = {
                "seq": s.seq,
                "time": self.wall(self._last_sweep_t or 0) if self._t0 is not None else time.time(),
                "source": self.source.name,
                "source_problem": getattr(self.source, "problem", None),
                "paused": self.paused,
                "band": {"id": self.band.id, "label": self.band.label, "f_min_hz": self.band.f_min_hz,
                         "f_max_hz": self.band.f_max_hz, "bin_hz": self.band.bin_hz,
                         "display_min_hz": self.band.display_min_hz, "display_max_hz": self.band.display_max_hz},
                "bands": [{"id": b.id, "label": b.label, "enabled": b.id in self.cfg.enabled} for b in self.cfg.bands.values()],
                "spectrum": s.spectrum,
                "baseline": s.baseline,
                "waterfall": {"min_db": WF_MIN_DB, "max_db": WF_MAX_DB, "rows": rows},
                "tracks": sorted(tracks, key=lambda r: (-r["drone"], -r["peak_db"])),
                "alerts": [a.__dict__ for a in list(s.alerts)[-10:]],
                "stats": {"sweeps": s.sweeps, "sweeps_per_s": round(s.sweeps / elapsed, 1) if self._t0 is not None else 0,
                          "captures": s.captures, "capture_duty": round(s.capture_time / elapsed, 3),
                          "noise_floor_db": self.baseline.noise_floor_db() if self.baseline else None},
                "model": self.model_info(),
                "recording": self.recording_status(),
                "classes": [{"id": c.id, "label": c.label, "drone": c.drone} for c in self.cfg.classes],
                "receiver": self.cfg.app.get("receiver", {}),
            }
        return out

    def model_info(self) -> dict:
        if self.clf is None:
            return {"loaded": False}
        c = self.clf.card
        return {"loaded": True, "classes": c.classes, "trained_at": c.trained_at, "params": c.params,
                "data": c.data, "metrics": c.metrics, "warnings": c.raw.get("warnings", []),
                "synthetic_only": c.synthetic_only}
