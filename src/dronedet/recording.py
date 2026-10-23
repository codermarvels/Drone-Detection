"""Recording sessions: labelled IQ captures for training.

A session is one folder under data/captures/:

    data/captures/2026-10-23_1405_mavic3_50m/
        session.json          label and conditions for every capture in it
        cap_0001.sigmf-meta   SigMF metadata
        cap_0001.sigmf-data   raw ci8 IQ (2 MB per 50 ms capture)
        ...

The label lives in session.json, so a whole session can be labelled (or
relabelled) after recording, and the training split is by session so
near-identical bursts never land in both train and test. A capture can
override the session label with its own "class" entry.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from .radio.base import Capture
from .radio.iq import write_sigmf

LABEL_FIELDS = ("class", "drone_model", "distance_m", "environment", "notes")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40]


def new_session_dir(root: Path, name: str | None = None) -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    d = Path(root) / (f"{stamp}_{_slug(name)}" if name else stamp)
    d.mkdir(parents=True, exist_ok=False)
    return d


class Recorder:
    """Writes captures into one session folder."""

    def __init__(self, root: Path, label: dict | None = None, name: str | None = None,
                 band: str = "", gain: dict | None = None, source: str = "", synthetic: bool = False):
        self.dir = new_session_dir(root, name or (label or {}).get("class"))
        self._lock = threading.Lock()
        self.meta = {
            "session": self.dir.name,
            "created": datetime.now(timezone.utc).isoformat(),
            "label": {k: (label or {}).get(k) for k in LABEL_FIELDS},
            "band": band,
            "gain": gain or {},
            "source": source,
            "synthetic": synthetic,
            "captures": [],
        }
        self._save()

    @property
    def count(self) -> int:
        return len(self.meta["captures"])

    def add(self, cap: Capture, track_id: int | None = None, extra: dict | None = None) -> Path:
        with self._lock:
            name = f"cap_{self.count + 1:04d}"
            write_sigmf(self.dir / name, cap.iq, cap.sample_rate, cap.center_hz, t=None,
                        extra={"dronedet:session": self.dir.name})
            entry = {"file": name, "t": cap.t, "center_hz": cap.center_hz, "track_id": track_id, **(extra or {})}
            self.meta["captures"].append(entry)
            self._save()
            return self.dir / name

    def _save(self) -> None:
        tmp = self.dir / "session.json.tmp"
        tmp.write_text(json.dumps(self.meta, indent=2))
        tmp.replace(self.dir / "session.json")


def list_sessions(root: Path) -> list[dict]:
    out = []
    for p in sorted(Path(root).glob("*/session.json")):
        try:
            m = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        m["path"] = str(p.parent)
        out.append(m)
    return out


def set_label(session_dir: Path, **label) -> dict:
    """Update a session's label fields. Pass None to leave a field as is."""
    p = Path(session_dir) / "session.json"
    m = json.loads(p.read_text())
    for k, v in label.items():
        if k in LABEL_FIELDS and v is not None:
            m["label"][k] = v
    p.write_text(json.dumps(m, indent=2))
    return m
