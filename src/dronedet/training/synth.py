"""Generate synthetic recording sessions.

For checking that the pipeline works end to end. A model trained only on
these has never seen a real drone: its model card says so, and the
dashboard shows that warning.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from ..radio import signals
from ..radio.base import Capture
from ..recording import Recorder


def make_sessions(root: Path, classes: list[str], drone_classes: set[str], sessions_per_class: int = 4,
                  captures_per_session: int = 30, fs: float = 20e6, n: int = 1_000_000, seed: int = 0) -> list[Path]:
    rng = np.random.default_rng(seed)
    out = []
    for cid in classes:
        if cid not in signals.GENERATORS:
            continue
        for s in range(sessions_per_class):
            srng = np.random.default_rng(rng.integers(1 << 31))
            # Fixed per session, varied across sessions, like a real recording.
            base_snr = srng.uniform(8, 30)
            kw = {}
            if cid in ("wifi", "wifi_drone", "microwave"):
                kw["f_off"] = float(srng.uniform(-3e6, 3e6))
            if cid == "dji_ocusync":
                kw["bw"] = float(srng.choice([10e6, 20e6]))
            rec = Recorder(root, label={"class": cid, "notes": "synthetic"}, name=f"synth-{cid}-{s}",
                           band="ism_2g4", source="synthetic", synthetic=True)
            for _ in range(captures_per_session):
                parts = [(cid, base_snr + srng.normal(0, 3), dict(kw))] if cid != "noise" else []
                if cid in drone_classes and srng.random() < 0.3:
                    parts.append(("wifi", srng.uniform(5, 20), {"f_off": float(srng.uniform(-8e6, 8e6))}))
                iq = signals.render(srng, parts, n, fs)
                rec.add(Capture(t=0.0, center_hz=2440e6, sample_rate=fs, iq=iq, duration_s=n / fs))
            out.append(rec.dir)
    return out
