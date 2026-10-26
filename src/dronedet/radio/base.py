"""Types shared by every radio source."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Protocol

import numpy as np


@dataclass
class SweepFrame:
    """Power in dB per bin at time t (s): one sweep, or several max-held."""

    t: float
    power_db: np.ndarray
    n_sweeps: int = 1


@dataclass
class Capture:
    """A short IQ capture of one 20 MHz slice."""

    t: float
    center_hz: float
    sample_rate: float
    iq: np.ndarray  # complex64
    duration_s: float  # radio time spent, including retune
    meta: dict = field(default_factory=dict)


class Source(Protocol):
    name: str

    def frames(self) -> Iterator[SweepFrame]:
        """Yield individual sweeps until the source ends or close() is called."""

    def capture(self, center_hz: float) -> Capture | None:
        """Pause sweeping, record one IQ capture at center_hz, resume."""

    def close(self) -> None: ...


class FrameAggregator:
    """Max-holds individual sweeps into frames of frame_s seconds.

    Used for the dashboard's spectrum and waterfall: hackrf_sweep runs much
    faster than a screen needs, and a hopping link is in a given bin for
    only a moment, so max-hold keeps the hops while cutting the row rate.
    Detection runs on the individual sweeps, where hops are still separate.
    """

    def __init__(self, frame_s: float):
        self.frame_s = frame_s
        self._acc: np.ndarray | None = None
        self._t0 = 0.0
        self._n = 0

    def add(self, t: float, sweep: np.ndarray) -> SweepFrame | None:
        if self._acc is None:
            self._acc, self._t0, self._n = sweep.copy(), t, 1
            return None
        np.fmax(self._acc, sweep, out=self._acc)
        self._n += 1
        if t - self._t0 >= self.frame_s:
            frame = SweepFrame(t=t, power_db=self._acc, n_sweeps=self._n)
            self._acc = None
            return frame
        return None


class NullSource:
    """No radio: what the dashboard shows while the detector is stopped."""

    name = "none"

    def frames(self) -> Iterator[SweepFrame]:
        return iter(())

    def capture(self, center_hz: float) -> Capture | None:
        return None

    def close(self) -> None:
        pass
