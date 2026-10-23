"""Per-bin baseline of the local RF environment."""

from __future__ import annotations

import numpy as np


class Baseline:
    """Per-bin noise baseline over the last window_s.

    Each bin's baseline is a low percentile of its recent power, capped at
    the band-wide noise floor plus cap_db. The percentile follows slow
    changes such as gain drift; the cap stops an emitter that is on most of
    the time (Wi-Fi, a drone streaming video) from raising its own baseline
    and hiding itself. Single-bin spurs stay above the cap and are dropped
    later by the candidate min_bins rule.
    """

    def __init__(self, n_bins: int, frame_s: float, window_s: float = 30.0,
                 percentile: float = 10.0, update_every_s: float = 1.0, cap_db: float = 4.0):
        self.depth = max(10, int(window_s / frame_s))
        self.buf = np.full((self.depth, n_bins), np.nan, dtype=np.float32)
        self.i = 0
        self.count = 0
        self.percentile = percentile
        self.cap_db = cap_db
        self.every = max(1, int(update_every_s / frame_s))
        self.level = np.full(n_bins, np.nan, dtype=np.float32)

    def update(self, power_db: np.ndarray) -> np.ndarray:
        self.buf[self.i] = power_db
        self.i = (self.i + 1) % self.depth
        self.count += 1
        if self.count <= 5 or self.count % self.every == 0:
            hist = self.buf if self.count >= self.depth else self.buf[: self.count]
            lvl = np.nanpercentile(hist, self.percentile, axis=0)
            floor = np.nanpercentile(lvl, 25)
            self.level = np.minimum(lvl, floor + self.cap_db).astype(np.float32)
        return self.level

    @property
    def ready(self) -> bool:
        return self.count >= 5

    def noise_floor_db(self) -> float:
        return float(np.nanmedian(self.level)) if self.ready else float("nan")
