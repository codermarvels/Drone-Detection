"""Everything the dashboard can start or change, so no command line is needed.

The Controller owns the detector Pipeline and can stop it, swap its input
(HackRF or simulator) and start it again while the web server keeps
running. Slow work (calibration, training, installing the training tools,
fetching map tiles) runs as one background job at a time, whose progress the
dashboard polls.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

from . import config as config_mod
from .config import Config
from .output.store import Store
from .pipeline import Pipeline
from .radio.base import NullSource

log = logging.getLogger(__name__)

SOURCE_LABELS = {"hackrf": "HackRF One", "sim": "Simulator", "replay": "Replay"}


class Cancelled(Exception):
    pass


def make_source(cfg: Config, kind: str, sweeps: str | None = None, iq: str | None = None, fast: bool = False,
                loop: bool = False, duration: float | None = None, seed: int = 0):
    """Open an input: "hackrf", "sim", "replay", or "auto" (the HackRF if its tools are installed)."""
    band = cfg.band
    if kind == "auto":
        kind = "hackrf" if hackrf_tools_available() else "sim"
        if kind == "sim":
            log.warning("HackRF tools not found: running the simulator. Use --source hackrf to insist on the radio.")
    if kind == "hackrf":
        from .radio.hackrf import HackRFSource

        return HackRFSource(band)
    if kind == "sim":
        from .radio.sim import SimSource

        return SimSource(band, realtime=not fast, duration_s=duration, seed=seed)
    if kind == "replay":
        from .radio.replay import ReplaySource

        if not sweeps:
            raise ValueError("replay needs a sweep file: dronedet run --source replay --sweeps sweep.csv")
        return ReplaySource(band, sweep_csv=sweeps, iq_dir=iq, realtime=not fast, loop=loop)
    raise ValueError(f"unknown input {kind}")


def training_tools_available() -> bool:
    importlib.invalidate_caches()
    return all(importlib.util.find_spec(m) is not None for m in ("torch", "onnx"))


def hackrf_tools_available() -> bool:
    from .radio.hackrf import tools_available

    return tools_available()


class Job:
    """One background task with progress the dashboard can show."""

    def __init__(self, kind: str, title: str):
        self.kind, self.title = kind, title
        self.state = "running"   # running | done | failed | cancelled
        self.stage = ""
        self.progress: float | None = None
        self.log: list[str] = []
        self.result: dict | None = None
        self.error: str | None = None
        self.started = time.time()
        self.finished: float | None = None
        self.cancel = threading.Event()
        self.proc: subprocess.Popen | None = None

    def say(self, line: str) -> None:
        self.log.append(line)
        del self.log[:-80]

    def check(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()

    def as_dict(self) -> dict:
        return {"kind": self.kind, "title": self.title, "state": self.state, "stage": self.stage,
                "progress": self.progress, "log": self.log[-20:], "result": self.result, "error": self.error,
                "started": self.started, "finished": self.finished}


class Controller:
    def __init__(self, cfg: Config, store: Store | None, model_path: Path | None = None,
                 source_opts: dict | None = None):
        self.cfg = cfg
        self.store = store
        self.model_path = Path(model_path) if model_path else cfg.path("model")
        self.source_opts = source_opts or {}
        self.lock = threading.RLock()
        self.kind: str | None = None      # the input last asked for
        self.error: str | None = None     # why the last start failed
        self.generation = 0               # bumps on every (re)start, so the dashboard can reset its waterfall
        self.pipeline = self._idle()
        self.job: Job | None = None
        self.shutdown = threading.Event()

    # -- the detector -----------------------------------------------------
    def _idle(self) -> Pipeline:
        return Pipeline(self.cfg, NullSource(), model_path=self.model_path, store=None)

    def sources(self) -> list[str]:
        out = ["hackrf", "sim"]
        if self.source_opts.get("sweeps"):
            out.append("replay")
        return out

    def make_source(self, kind: str):
        return make_source(self.cfg, kind, **self.source_opts)

    @property
    def running(self) -> bool:
        return self.pipeline.running

    def start(self, kind: str | None = None) -> dict:
        """Start the detector on an input, stopping the current one first."""
        with self.lock:
            if self.job and self.job.state == "running" and self.job.kind == "calibrate" \
                    and threading.current_thread().name != "job-calibrate":
                raise RuntimeError("calibration is using the radio; wait for it to finish")
            kind = kind or self.kind or "auto"
            self._stop_pipeline()
            self.kind = kind
            self.generation += 1
            try:
                src = self.make_source(kind)
            except Exception as e:
                self.error = str(e)
                log.error("could not start %s: %s", kind, e)
                self.pipeline = self._idle()
                return self.status()
            self.kind = src.name
            self.error = None
            self.pipeline = Pipeline(self.cfg, src, model_path=self.model_path, store=self.store)
            self.pipeline.start()
            return self.status()

    def stop(self) -> dict:
        with self.lock:
            self._stop_pipeline()
            self.generation += 1
            self.pipeline = self._idle()
            return self.status()

    def _stop_pipeline(self) -> None:
        if self.pipeline.running or not isinstance(self.pipeline.source, NullSource):
            self.pipeline.stop()

    def record(self, on: bool, label: dict, mode: str = "tracks", target: int | None = None) -> dict:
        with self.lock:
            if on and not self.running:
                self.start()
                if not self.running:
                    raise RuntimeError(self.error or "the detector could not start")
            return self.pipeline.set_recording(on, label, mode=mode, target=target)

    def status(self) -> dict:
        job = self.job
        return {
            "running": self.running,
            "source": self.kind,
            "sources": [{"id": s, "label": SOURCE_LABELS[s]} for s in self.sources()],
            "error": self.error,
            "generation": self.generation,
            "job": job.as_dict() if job else None,
        }

    def snapshot(self, since: int = 0) -> dict:
        snap = self.pipeline.snapshot(since)
        snap["control"] = self.status()
        return snap

    def settings(self) -> dict:
        b = self.cfg.band
        return {
            "band": {"id": b.id, "label": b.label},
            "gain": {"lna_db": b.lna_db, "vga_db": b.vga_db, "amp": b.amp},
            "receiver": self.cfg.app.get("receiver", {}),
            "calibration": self.last_calibration(),
            "hackrf_tools": hackrf_tools_available(),
            "training_tools": training_tools_available(),
            "root": str(self.cfg.root),
        }

    # -- settings ---------------------------------------------------------
    def set_gain(self, lna_db: int, vga_db: int, amp: bool) -> dict:
        with self.lock:
            config_mod.set_band_gain(self.cfg, self.cfg.band.id, lna_db, vga_db, amp)
            restarted = False
            if self.running and self.kind == "hackrf":
                self.start("hackrf")  # hackrf_sweep takes the gain on its command line
                restarted = True
        return {"gain": self.settings()["gain"], "restarted": restarted}

    def set_receiver(self, lat: float, lon: float, name: str) -> dict:
        with self.lock:
            return config_mod.set_receiver(self.cfg, lat, lon, name)

    # -- sessions ---------------------------------------------------------
    @property
    def captures_dir(self) -> Path:
        return self.cfg.path("data") / "captures"

    def session_dir(self, name: str) -> Path:
        if not name or "/" in name or "\\" in name or name.startswith("."):
            raise ValueError("bad session name")
        d = self.captures_dir / name
        if not (d / "session.json").is_file():
            raise ValueError(f"no session {name}")
        return d

    def label_session(self, name: str, label: dict) -> dict:
        from .recording import LABEL_FIELDS, set_label

        cls = label.get("class")
        if cls and not self.cfg.class_by_id(cls):
            raise ValueError(f"unknown class {cls}")
        d = self.session_dir(name)
        vals = {k: label.get(k) for k in LABEL_FIELDS if k in label}
        for k, v in vals.items():
            if v is None:
                vals[k] = ""  # an emptied field clears it (set_label skips None)
        if "distance_m" in vals and vals["distance_m"] != "":
            vals["distance_m"] = float(vals["distance_m"])
        return set_label(d, **vals)["label"]

    def delete_session(self, name: str) -> None:
        d = self.session_dir(name)
        rec = self.pipeline.recorder
        if rec is not None and rec.dir.resolve() == d.resolve():
            raise ValueError("this session is still recording; stop it first")
        shutil.rmtree(d)

    # -- background jobs --------------------------------------------------
    def _run_job(self, kind: str, title: str, work) -> dict:
        with self.lock:
            if self.job and self.job.state == "running":
                raise RuntimeError(f"{self.job.title} is still running")
            job = self.job = Job(kind, title)

        def run():
            try:
                job.result = work(job)
                job.state = "done"
            except Cancelled:
                job.state = "cancelled"
            except Exception as e:  # shown on the dashboard
                log.exception("%s failed", title)
                job.state, job.error = "failed", str(e) or e.__class__.__name__
            finally:
                job.finished = time.time()

        threading.Thread(target=run, daemon=True, name=f"job-{kind}").start()
        return job.as_dict()

    def cancel_job(self) -> dict | None:
        job = self.job
        if job and job.state == "running":
            job.cancel.set()
            if job.proc and job.proc.poll() is None:
                job.proc.terminate()
        return job.as_dict() if job else None

    def _subprocess(self, job: Job, cmd: list[str], cwd: Path | None = None) -> None:
        job.say("$ " + " ".join(cmd))
        job.proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                    bufsize=1)
        assert job.proc.stdout
        for line in job.proc.stdout:
            job.say(line.rstrip())
        code = job.proc.wait()
        job.check()
        if code != 0:
            raise RuntimeError(f"exited with code {code}: {job.log[-1] if job.log else ''}")

    # calibration
    def calibration_path(self) -> Path:
        return self.cfg.path("data") / "calibration.json"

    def last_calibration(self) -> dict | None:
        try:
            return json.loads(self.calibration_path().read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def calibrate(self, n_sweeps: int = 100, timeout_s: float = 30.0) -> dict:
        def work(job: Job):
            with self.lock:
                kind = self.kind or "auto"
                was_running = self.running
                self.stop()  # the HackRF can only be opened once
            try:
                job.stage = "Taking sweeps"
                src = self.make_source(kind)
                timer = threading.Timer(timeout_s, src.close)
                timer.start()
                sweeps = []
                try:
                    for f in src.frames():
                        sweeps.append(f.power_db)
                        job.progress = len(sweeps) / n_sweeps
                        if len(sweeps) >= n_sweeps or job.cancel.is_set():
                            break
                finally:
                    timer.cancel()
                    src.close()
                job.check()
                if not sweeps:
                    raise RuntimeError("No sweep received. Check that the HackRF is plugged in and the antenna "
                                       "is on; `hackrf_info` should list it.")
                result = summarize_sweeps(np.stack(sweeps), self.cfg.band.bin_centers())
                result.update({"source": src.name, "time": time.time(),
                               "gain": {"lna_db": self.cfg.band.lna_db, "vga_db": self.cfg.band.vga_db,
                                        "amp": self.cfg.band.amp}})
                p = self.calibration_path()
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(result, indent=2))
                return result
            finally:
                if was_running:
                    self.start(kind)

        return self._run_job("calibrate", "Calibration", work)

    # training
    def train(self, epochs: int = 20, include_synthetic: bool = False, min_snr: float = 6.0) -> dict:
        if not training_tools_available():
            raise RuntimeError("Training needs PyTorch and ONNX. Press Install training tools first.")
        epochs = max(1, min(int(epochs), 200))

        def work(job: Job):
            # Training runs in its own Python process: PyTorch and onnxruntime in one process can
            # abort it on macOS (two OpenMP runtimes), and a crash there must not take the
            # dashboard and the detector down with it. Cancel terminates the process.
            job.stage = "Starting the training process"
            job.progress = 0.0
            cmd = self._train_cmd(epochs, include_synthetic, min_snr)
            job.say("$ " + " ".join(cmd))
            job.proc = subprocess.Popen(cmd, cwd=self.cfg.root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, bufsize=1, env={**os.environ, "PYTHONUNBUFFERED": "1"})
            assert job.proc.stdout
            result = None
            for line in job.proc.stdout:
                line = line.rstrip()
                msg = None
                if line.startswith("{"):
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        pass
                if msg is None:
                    if line:
                        job.say(line)
                    continue
                if "stage" in msg:
                    job.stage = msg["stage"]
                if msg.get("fraction") is not None:
                    job.progress = float(msg["fraction"])
                if "result" in msg:
                    result = msg["result"]
            code = job.proc.wait()
            job.check()
            if code != 0 or result is None:
                tail = [ln for ln in job.log[-6:] if ln.strip()]
                raise RuntimeError(f"the training process stopped (exit code {code}): "
                                   + (tail[-1] if tail else "no output"))
            job.stage = "Loading the new model"
            result["loaded"] = self.pipeline.reload_model()
            acc = result.get("test_accuracy")
            job.say("Held-out accuracy: " + ("not measured" if acc is None
                                             else f"{acc * 100:.1f}% on {result.get('n_test')} captures"))
            for w in result.get("warnings", []):
                job.say("Warning: " + w)
            return result

        return self._run_job("train", "Training", work)

    def _train_cmd(self, epochs: int, include_synthetic: bool, min_snr: float) -> list[str]:
        cmd = [sys.executable, "-m", "dronedet.cli", "--root", str(self.cfg.root), "train", "--progress-json",
               "--epochs", str(epochs), "--min-snr", str(min_snr), "--data", str(self.captures_dir),
               "--out", str(self.model_path)]
        return cmd + (["--include-synthetic"] if include_synthetic else [])

    def install_training_tools(self) -> dict:
        def work(job: Job):
            job.stage = "Installing PyTorch and ONNX (a few hundred MB, needs internet once)"
            self._subprocess(job, [sys.executable, "-m", "pip", "install", "torch>=2.2", "onnx>=1.15",
                                   "onnxscript>=0.1"])
            if not training_tools_available():
                raise RuntimeError("pip finished but PyTorch still cannot be imported")
            return {"ok": True}

        return self._run_job("install", "Installing training tools", work)

    def make_synthetic(self, sessions: int = 4, captures: int = 30) -> dict:
        def work(job: Job):
            from .training.synth import make_sessions

            job.stage = "Writing synthetic sessions"
            drones = {c.id for c in self.cfg.classes if c.drone}
            dirs = make_sessions(self.captures_dir, self.cfg.class_ids(), drones, sessions_per_class=sessions,
                                 captures_per_session=captures)
            job.say(f"Wrote {len(dirs)} synthetic sessions. They are for testing the pipeline only.")
            return {"sessions": len(dirs)}

        return self._run_job("synth", "Making synthetic test data", work)

    def fetch_tiles(self, radius_km: float = 1.5) -> dict:
        rc = self.cfg.app.get("receiver", {})
        lat, lon = float(rc.get("lat") or 0), float(rc.get("lon") or 0)
        if not lat and not lon:
            raise RuntimeError("Set the receiver location first.")
        script = self.cfg.root / "tools" / "cache_tiles.py"
        if not script.is_file():
            raise RuntimeError(f"{script} is missing")
        radius_km = max(0.2, min(float(radius_km), 5.0))

        def work(job: Job):
            job.stage = "Downloading map tiles (needs internet once)"
            self._subprocess(job, [sys.executable, str(script), "--lat", str(lat), "--lon", str(lon),
                                   "--radius-km", str(radius_km), "--out", str(self.cfg.path("data") / "tiles")],
                             cwd=self.cfg.root)
            return {"ok": True}

        return self._run_job("tiles", "Downloading map tiles", work)

    def close(self) -> None:
        self.cancel_job()
        self._stop_pipeline()


def summarize_sweeps(sweeps: np.ndarray, centers: np.ndarray, top_n: int = 8, spacing_hz: float = 5e6) -> dict:
    """Noise floor and the strongest signals over a stack of sweeps (what `dronedet calibrate` prints).

    Peaks closer than spacing_hz to a stronger one are skipped, so one wide
    Wi-Fi channel shows as one line rather than eight neighbouring bins.
    """
    peak = sweeps.max(axis=0)
    floor = float(np.median(np.percentile(sweeps, 10, axis=0)))
    top = []
    for i in np.argsort(peak)[::-1]:
        if all(abs(centers[i] - centers[j]) >= spacing_hz for j in top):
            top.append(int(i))
            if len(top) == top_n:
                break
    top.sort()
    return {"sweeps": int(len(sweeps)), "noise_floor_db": round(floor, 1),
            "strongest": [{"mhz": round(float(centers[i]) / 1e6, 2), "db": round(float(peak[i]), 1),
                           "above_floor_db": round(float(peak[i]) - floor, 1)} for i in top]}
