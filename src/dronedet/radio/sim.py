"""Simulated HackRF: a scripted 2.4 GHz scene, for tests and demos without hardware.

The sweep side draws each emitter's footprint at the instant of each
sweep, like hackrf_sweep would: a hopping link shows at one place per sweep. The capture side
renders real IQ for whichever emitters fall inside the 20 MHz window, using
the waveforms in signals.py.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterator

import numpy as np

from ..config import Band
from . import signals
from .base import Capture, SweepFrame

NOISE_FLOOR_DB = -95.0


@dataclass
class Emitter:
    cid: str                 # class id from config/classes.yaml
    power_db: float          # dB above the noise floor while on
    center_hz: float = 0.0   # fixed-channel emitters (wifi, wifi_drone, microwave)
    t_on: float = 0.0
    t_off: float = float("inf")
    kwargs: dict = field(default_factory=dict)

    def active(self, t: float) -> bool:
        return self.t_on <= t < self.t_off


# Per-snapshot footprint: (probability on, width Hz, hops?)
_FOOTPRINT = {
    "wifi": (0.25, 17e6, False),
    "wifi_drone": (0.8, 17e6, False),
    "dji_ocusync": (0.6, 10e6, True),
    "elrs_2g4": (0.35, 0.8e6, True),
    "bluetooth": (0.5, 1e6, True),
    "microwave": (0.5, 8e6, False),
    "noise": (0.0, 0.0, False),
}


def default_scene(band: Band) -> list[Emitter]:
    """Background Wi-Fi and Bluetooth, then a DJI link at 20 s and an ELRS link at 40 s."""
    return [
        Emitter("wifi", 30, center_hz=2412e6),
        Emitter("wifi", 22, center_hz=2437e6),
        Emitter("bluetooth", 18),
        Emitter("dji_ocusync", 28, t_on=20.0, kwargs={"bw": 10e6}),
        Emitter("elrs_2g4", 20, t_on=40.0),
    ]


class SimSource:
    name = "sim"

    def __init__(self, band: Band, scene: list[Emitter] | None = None, sweep_s: float = 0.02,
                 realtime: bool = True, duration_s: float | None = None, seed: int = 0,
                 retune_s: float = 0.03):
        self.band = band
        self.scene = scene if scene is not None else default_scene(band)
        self.sweep_s = sweep_s
        self.realtime = realtime
        self.duration_s = duration_s
        self.rng = np.random.default_rng(seed)
        self.retune_s = retune_s
        self.t = 0.0
        self._t_wall0 = time.time()
        self._closed = False
        self._bins = band.bin_centers()
        self._hop_lo, self._hop_hi = band.display_min_hz, band.display_max_hz

    # -- sweep side -------------------------------------------------------
    def _snapshot(self, out: np.ndarray, e: Emitter) -> None:
        p_on, width, hops = _FOOTPRINT[e.cid]
        if self.rng.random() > p_on or width <= 0:
            return
        if hops:
            c = self.rng.uniform(self._hop_lo + width / 2, self._hop_hi - width / 2)
        else:
            c = e.center_hz + (self.rng.normal(0, 2e6) if e.cid == "microwave" else 0.0)
        d = np.abs(self._bins - c) / (width / 2)
        shape = np.where(d <= 1, 0.0, -80 * (d - 1))  # flat top, steep skirts
        lvl = NOISE_FLOOR_DB + e.power_db + self.rng.normal(0, 1.0) + shape
        np.fmax(out, lvl, out=out)

    def _sweep(self) -> np.ndarray:
        snap = (NOISE_FLOOR_DB + self.rng.normal(0, 1.2, self.band.n_bins)).astype(np.float32)
        for e in self.scene:
            if e.active(self.t):
                self._snapshot(snap, e)
        return snap

    def frames(self) -> Iterator[SweepFrame]:
        while not self._closed:
            if self.duration_s is not None and self.t >= self.duration_s:
                return
            self.t += self.sweep_s
            if self.realtime:
                lag = (self._t_wall0 + self.t) - time.time()
                if lag > 0:
                    time.sleep(lag)
            yield SweepFrame(t=self.t, power_db=self._sweep())

    # -- capture side -----------------------------------------------------
    def capture(self, center_hz: float) -> Capture | None:
        fs = self.band.sample_rate
        n = self.band.capture_samples
        parts = []
        for e in self.scene:
            if not e.active(self.t) or e.cid == "noise":
                continue
            _, width, hops = _FOOTPRINT[e.cid]
            if hops:
                kw = {"band_lo": self._hop_lo - center_hz, "band_hi": self._hop_hi - center_hz}
            else:
                off = e.center_hz - center_hz
                if abs(off) - width / 2 > fs / 2:
                    continue
                kw = {"f_off": off}
            parts.append((e.cid, e.power_db, {**kw, **e.kwargs}))
        iq = signals.render(self.rng, parts, n, fs)
        t0 = self.t
        dur = n / fs + self.retune_s
        self.t += dur
        return Capture(t=t0, center_hz=center_hz, sample_rate=fs, iq=iq, duration_s=dur,
                       meta={"sim_truth": sorted({p[0] for p in parts})})

    def close(self) -> None:
        self._closed = True
