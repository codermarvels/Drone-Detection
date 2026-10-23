"""Group candidates across frames into tracks.

A fixed-channel emitter (Wi-Fi) is matched by frequency. A narrow emitter
(up to hop_max_width_hz, 15 MHz: OcuSync 10 MHz, ELRS, Bluetooth) may also be
matched by bandwidth alone, so a hopping link becomes one track with many
hops rather than many short candidates.

Known v1 limits: two hopping links of similar width (ELRS and Bluetooth)
share a track until the classifier separates them, and OcuSync in its
20 MHz mode is tracked like a fixed channel.
"""

from __future__ import annotations

import itertools
from collections import Counter, deque
from dataclasses import dataclass, field

import numpy as np

from .candidates import Candidate

_ids = itertools.count(1)


@dataclass
class Track:
    id: int
    first_seen: float
    last_seen: float
    hits: deque = field(default_factory=lambda: deque(maxlen=400))  # (t, center, width, peak)
    votes: deque = field(default_factory=lambda: deque(maxlen=5))   # per-capture probability vectors
    captures: int = 0
    last_capture: float = -1e9
    label: str | None = None        # class id, "unknown", or None (not classified yet)
    confidence: float = 0.0
    probs: list[float] | None = None
    features: dict = field(default_factory=dict)
    closed: bool = False

    def add(self, t: float, c: Candidate) -> None:
        self.hits.append((t, c.center_hz, c.width_hz, c.peak_db))
        self.last_seen = t

    def _recent(self, n: int = 40):
        return list(self.hits)[-n:]

    @property
    def width_hz(self) -> float:
        return float(np.median([h[2] for h in self._recent()]))

    @property
    def center_hz(self) -> float:
        return float(np.median([h[1] for h in self._recent(10)]))

    @property
    def peak_db(self) -> float:
        return float(max(h[3] for h in self._recent()))

    @property
    def hop_count(self) -> int:
        """Distinct frequency positions, in units of the track's width."""
        w = max(self.width_hz, 1.0)
        return len({round(h[1] / w) for h in self._recent(200)})

    @property
    def hopping(self) -> bool:
        cs = [h[1] for h in self._recent(200)]
        return len(cs) >= 4 and self.hop_count >= 4 and (max(cs) - min(cs)) > 3 * self.width_hz

    @property
    def confirmed(self) -> bool:
        """Seen often enough to report: filters one-off blips and fragments."""
        return len(self.hits) >= 8 and self.last_seen - self.first_seen >= 1.0

    def summary(self) -> str:
        mode = "hopping bursts" if self.hopping else "fixed channel"
        return f"{mode} · {self.width_hz / 1e6:.1f} MHz wide"


class Tracker:
    def __init__(self, timeout_s: float = 5.0, hop_max_width_hz: float = 15e6,
                 hop_link_s: float = 1.0, width_ratio: float = 1.6):
        self.timeout_s = timeout_s
        self.hop_max_width_hz = hop_max_width_hz
        self.hop_link_s = hop_link_s
        self.width_ratio = width_ratio
        self.tracks: dict[int, Track] = {}
        self.closed: list[Track] = []

    def _similar_width(self, a: float, b: float) -> bool:
        r = max(a, b) / max(min(a, b), 1.0)
        return r <= self.width_ratio

    def update(self, t: float, cands: list[Candidate]) -> list[Track]:
        """Returns tracks that were opened in this frame."""
        new = []
        used: Counter = Counter()
        for c in sorted(cands, key=lambda c: -c.peak_db):
            tr = self._match_fixed(c) or self._match_hop(t, c, used)
            if tr is None:
                tr = Track(id=next(_ids), first_seen=t, last_seen=t)
                self.tracks[tr.id] = tr
                new.append(tr)
            tr.add(t, c)
            used[tr.id] += 1
        for tid, tr in list(self.tracks.items()):
            if t - tr.last_seen > self.timeout_s:
                tr.closed = True
                self.closed.append(tr)
                del self.tracks[tid]
        return new

    def _match_fixed(self, c: Candidate) -> Track | None:
        """A non-hopping track whose span overlaps at least half of the narrower one."""
        best, best_ov = None, 0.0
        for tr in self.tracks.values():
            if tr.hopping:
                continue
            w = tr.width_hz
            lo = max(tr.center_hz - w / 2, c.center_hz - c.width_hz / 2)
            hi = min(tr.center_hz + w / 2, c.center_hz + c.width_hz / 2)
            ov = (hi - lo) / max(min(w, c.width_hz), 1.0)
            if ov >= 0.5 and max(w, c.width_hz) / max(min(w, c.width_hz), 1.0) <= 2.5 and ov > best_ov:
                best, best_ov = tr, ov
        return best

    def _match_hop(self, t: float, c: Candidate, used: Counter) -> Track | None:
        if c.width_hz > self.hop_max_width_hz:
            return None
        best = None
        for tr in self.tracks.values():
            if t - tr.last_seen > self.hop_link_s or tr.width_hz > self.hop_max_width_hz:
                continue
            if not self._similar_width(tr.width_hz, c.width_hz):
                continue
            # A link occupies one channel per instant, but a max-held frame can
            # show several hops, so allow a few hits per frame.
            if used[tr.id] >= 8:
                continue
            if best is None or len(tr.hits) > len(best.hits):
                best = tr
        return best

    def active(self) -> list[Track]:
        return list(self.tracks.values())
