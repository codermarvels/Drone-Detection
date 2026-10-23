"""Track-level decision: smoothed vote over a track's recent captures."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

UNKNOWN = "unknown"


@dataclass
class Decision:
    label: str | None   # class id, UNKNOWN, or None while there are too few votes
    confidence: float   # mean probability of the winning class over its votes
    votes_for: int
    n_votes: int
    mean_probs: list[float]


def decide(votes: list[np.ndarray], classes: list[str], min_votes: int = 3, min_prob: float = 0.6) -> Decision:
    if not votes:
        return Decision(None, 0.0, 0, 0, [])
    P = np.stack(votes)
    mean = P.mean(axis=0)
    top = P.argmax(axis=1)
    counts = np.bincount(top, minlength=len(classes))
    win = int(counts.argmax())
    conf = float(P[top == win, win].mean())
    n = len(votes)
    if counts[win] >= min_votes and conf >= min_prob:
        label = classes[win]
    elif n >= min_votes:
        label = UNKNOWN
    else:
        label = None
    return Decision(label, conf, int(counts[win]), n, [float(m) for m in mean])
