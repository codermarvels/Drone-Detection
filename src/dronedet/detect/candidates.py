"""Change detection: bins well above their baseline, merged into candidates."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class Candidate:
    center_hz: float
    width_hz: float
    peak_db: float
    excess_db: float  # peak above baseline


def find_candidates(power_db: np.ndarray, baseline: np.ndarray, bin_centers: np.ndarray,
                    bin_hz: float, threshold_db: float = 8.0, min_bins: int = 2,
                    max_gap_bins: int = 1) -> list[Candidate]:
    hot = power_db > baseline + threshold_db
    if not hot.any():
        return []
    # Close gaps of up to max_gap_bins so one notch does not split a signal.
    idx = np.flatnonzero(hot)
    groups = np.split(idx, np.flatnonzero(np.diff(idx) > max_gap_bins + 1) + 1)
    out = []
    for g in groups:
        lo, hi = g[0], g[-1]
        if hi - lo + 1 < min_bins:
            continue
        seg = power_db[lo : hi + 1]
        lin = 10 ** (seg / 10)
        center = float(np.sum(bin_centers[lo : hi + 1] * lin) / np.sum(lin))
        out.append(Candidate(center_hz=center, width_hz=float((hi - lo + 1) * bin_hz),
                             peak_db=float(seg.max()),
                             excess_db=float((seg - baseline[lo : hi + 1]).max())))
    return out
