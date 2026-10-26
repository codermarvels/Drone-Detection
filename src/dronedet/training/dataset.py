"""Turn recording sessions into a tile dataset, split by session."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..features.handcrafted import iq_features
from ..features.spectrogram import TILE_SPEC, tile
from ..radio.iq import read_sigmf
from ..recording import list_sessions

log = logging.getLogger(__name__)


@dataclass
class Dataset:
    classes: list[str]
    X: np.ndarray                 # [N, H, W] float32 tiles
    y: np.ndarray                 # [N] int class index
    session: np.ndarray           # [N] session name per sample
    synthetic: np.ndarray         # [N] bool
    report: dict = field(default_factory=dict)


def _cache_key(meta_path: Path) -> str:
    st = meta_path.stat()
    h = hashlib.sha1(f"{meta_path.resolve()}|{st.st_mtime_ns}|{json.dumps(TILE_SPEC, sort_keys=True)}".encode())
    return h.hexdigest()[:16]


def load(captures_root: Path, classes: list[str], drone_classes: set[str], cache_dir: Path | None = None,
         min_snr_db: float = 6.0) -> Dataset:
    """Load every labelled session under captures_root.

    Captures labelled with a signal class but with nothing above the noise
    (SNR below min_snr_db) are dropped, since the emitter was probably not
    in the window when the capture was taken. Unlabelled sessions and
    unknown classes are skipped and counted in the report.
    """
    cache_dir = Path(cache_dir) if cache_dir else None
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
    idx = {c: i for i, c in enumerate(classes)}
    X, y, sess, synth = [], [], [], []
    rep = {"sessions_used": 0, "sessions_unlabelled": 0, "sessions_unknown_class": 0,
           "captures_dropped_empty": 0, "per_class": {c: {"sessions": 0, "samples": 0, "synthetic_samples": 0} for c in classes}}
    for m in list_sessions(captures_root):
        sdir = Path(m["path"])
        default = (m.get("label") or {}).get("class")
        used = False
        for cap in m.get("captures", []):
            cid = cap.get("class") or default
            if not cid:
                continue
            if cid not in idx:
                rep["sessions_unknown_class"] += 1
                break
            mp = sdir / f"{cap['file']}.sigmf-meta"
            if not mp.exists():
                continue
            cached = cache_dir / f"{_cache_key(mp)}.npz" if cache_dir else None
            if cached and cached.exists():
                z = np.load(cached)
                t, snr = z["tile"], float(z["snr"])
            else:
                iq, meta = read_sigmf(mp)
                fs = float(meta["global"]["core:sample_rate"])
                t = tile(iq)
                snr = iq_features(iq, fs)["snr_db"]
                if cached:
                    np.savez_compressed(cached, tile=t, snr=snr)
            if cid != "noise" and snr < min_snr_db:
                rep["captures_dropped_empty"] += 1
                continue
            X.append(t)
            y.append(idx[cid])
            sess.append(m["session"])
            synth.append(bool(m.get("synthetic")))
            pc = rep["per_class"][cid]
            pc["samples"] += 1
            pc["synthetic_samples"] += int(bool(m.get("synthetic")))
            if not used:
                pc["sessions"] += 1
                used = True
        if used:
            rep["sessions_used"] += 1
        elif not default:
            rep["sessions_unlabelled"] += 1
    if not X:
        return Dataset(classes, np.zeros((0, TILE_SPEC["time_bins"], TILE_SPEC["freq_bins"]), np.float32),
                       np.zeros(0, int), np.array([], dtype=object), np.zeros(0, bool), rep)
    rep["synthetic_only"] = bool(all(synth))
    rep["real_samples"] = int(len(synth) - sum(synth))
    return Dataset(classes, np.stack(X), np.array(y), np.array(sess, dtype=object), np.array(synth), rep)


def split_by_session(ds: Dataset, test_frac: float = 0.25, seed: int = 0):
    """Hold out whole sessions per class. Returns (train_idx, test_idx, split_kind).

    A class with a single session cannot be held out by session; its
    samples are split by time order instead, and split_kind says so,
    because that estimate is optimistic.
    """
    rng = np.random.default_rng(seed)
    train, test = [], []
    kind = "by_session"
    for ci in range(len(ds.classes)):
        rows = np.flatnonzero(ds.y == ci)
        if rows.size == 0:
            continue
        sessions = sorted(set(ds.session[rows]))
        if len(sessions) >= 2:
            n_test = max(1, int(round(len(sessions) * test_frac)))
            held = set(rng.choice(sessions, n_test, replace=False))
            for r in rows:
                (test if ds.session[r] in held else train).append(r)
        else:
            kind = "mixed: some classes split within one session (optimistic)"
            cut = int(len(rows) * (1 - test_frac))
            train.extend(rows[:cut])
            test.extend(rows[cut:])
    return np.array(train, int), np.array(test, int), kind


def drop_synthetic_if_real(ds: Dataset) -> bool:
    """Leave synthetic sessions out when real ones exist. Returns True if any were dropped."""
    if not (ds.report.get("real_samples", 0) and ds.synthetic.any()):
        return False
    keep = ~ds.synthetic
    ds.X, ds.y, ds.session, ds.synthetic = ds.X[keep], ds.y[keep], ds.session[keep], ds.synthetic[keep]
    return True
