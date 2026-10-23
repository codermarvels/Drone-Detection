"""Replay recorded files through the pipeline.

Sweeps come from a saved hackrf_sweep CSV:
    hackrf_sweep -f 2400:2490 -w 250000 -l 32 -g 20 > sweep.csv
IQ captures come from a folder of SigMF files (for example a recording
session). A capture request returns the recorded file whose centre is
nearest, in turn, so the classifier sees real recorded waveforms.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Iterator

import numpy as np

from ..config import Band
from .base import Capture, SweepFrame
from .hackrf import SweepAssembler
from .iq import read_sigmf


class ReplaySource:
    name = "replay"

    def __init__(self, band: Band, sweep_csv: Path | None = None, iq_dir: Path | None = None,
                 realtime: bool = False, loop: bool = False):
        if sweep_csv is None and iq_dir is None:
            raise ValueError("replay needs a sweep CSV, an IQ folder, or both")
        self.band = band
        self.sweep_csv = Path(sweep_csv) if sweep_csv else None
        self.realtime = realtime
        self.loop = loop
        self._closed = False
        self._t = 0.0
        self._iq_files = sorted(Path(iq_dir).rglob("*.sigmf-meta")) if iq_dir else []
        self._iq_used: dict[Path, int] = {}

    def _sweep_times(self, line: str) -> float | None:
        parts = line.split(",", 2)
        if len(parts) < 3:
            return None
        try:
            from datetime import datetime

            return datetime.strptime(f"{parts[0].strip()} {parts[1].strip()}", "%Y-%m-%d %H:%M:%S.%f").timestamp()
        except ValueError:
            return None

    def frames(self) -> Iterator[SweepFrame]:
        if self.sweep_csv is None:
            # IQ only: synthesise quiet frames so the pipeline still ticks.
            for _ in range(len(self._iq_files) * 100):
                if self._closed:
                    return
                self._t += 0.02
                yield SweepFrame(self._t, np.full(self.band.n_bins, -95.0, dtype=np.float32))
            return
        while not self._closed:
            asm = SweepAssembler(self.band)
            t_first = wall0 = None
            with open(self.sweep_csv) as f:
                for line in f:
                    if self._closed:
                        return
                    sweep = asm.feed(line)
                    if sweep is None:
                        continue
                    ts = self._sweep_times(line)
                    if ts is None:
                        ts = (t_first or 0.0) + self._t + 0.01
                    if t_first is None:
                        t_first, wall0 = ts, time.time()
                    rel = ts - t_first
                    if self.realtime:
                        lag = wall0 + rel - time.time()
                        if lag > 0:
                            time.sleep(lag)
                    self._t = self._t_base() + rel
                    yield SweepFrame(self._t, sweep)
            if not self.loop:
                return
            self._loop_base = self._t

    def _t_base(self) -> float:
        return getattr(self, "_loop_base", 0.0)

    def capture(self, center_hz: float) -> Capture | None:
        if not self._iq_files:
            return None
        def key(p: Path):
            import json

            meta = json.loads(p.read_text())
            c = meta["captures"][0].get("core:frequency", 0.0)
            return (abs(c - center_hz) > 10e6, self._iq_used.get(p, 0))
        path = min(self._iq_files, key=key)
        self._iq_used[path] = self._iq_used.get(path, 0) + 1
        iq, meta = read_sigmf(path)
        fs = float(meta["global"]["core:sample_rate"])
        c = float(meta["captures"][0].get("core:frequency", center_hz))
        return Capture(t=self._t, center_hz=c, sample_rate=fs, iq=iq, duration_s=len(iq) / fs,
                       meta={"replay_file": str(path)})

    def close(self) -> None:
        self._closed = True
