"""Load band, class and app settings from the config/ folder."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


def find_root() -> Path:
    """Repo root: $DRONEDET_HOME, else the nearest folder holding config/."""
    env = os.environ.get("DRONEDET_HOME")
    if env:
        return Path(env).resolve()
    for p in [Path.cwd(), *Path.cwd().parents]:
        if (p / "config" / "bands.yaml").exists():
            return p
    # Editable install from a clone: src/dronedet/config.py -> repo root.
    return Path(__file__).resolve().parents[2]


@dataclass
class Band:
    id: str
    label: str
    f_min_hz: float
    f_max_hz: float
    bin_hz: float
    display_min_hz: float
    display_max_hz: float
    lna_db: int
    vga_db: int
    amp: bool
    sample_rate: float
    dwell_ms: float

    @property
    def n_bins(self) -> int:
        return int(round((self.f_max_hz - self.f_min_hz) / self.bin_hz))

    def bin_centers(self):
        import numpy as np

        return self.f_min_hz + (np.arange(self.n_bins) + 0.5) * self.bin_hz

    @property
    def capture_samples(self) -> int:
        return int(self.sample_rate * self.dwell_ms / 1000)


@dataclass
class ClassInfo:
    id: str
    label: str
    drone: bool
    bands: list[str]


@dataclass
class Config:
    root: Path
    bands: dict[str, Band]
    enabled: list[str]
    classes: list[ClassInfo]
    decision: dict
    app: dict = field(default_factory=dict)

    @property
    def band(self) -> Band:
        """The active band. v1 runs one band at a time."""
        return self.bands[self.enabled[0]]

    def class_ids(self) -> list[str]:
        return [c.id for c in self.classes]

    def class_by_id(self, cid: str) -> ClassInfo | None:
        return next((c for c in self.classes if c.id == cid), None)

    def path(self, key: str) -> Path:
        p = Path(self.app["paths"][key])
        return p if p.is_absolute() else self.root / p


def _band(bid: str, d: dict) -> Band:
    s, g, c = d["sweep"], d["gain"], d["capture"]
    disp = d.get("display", {"f_min_mhz": s["f_min_mhz"], "f_max_mhz": s["f_max_mhz"]})
    return Band(
        id=bid,
        label=d.get("label", bid),
        f_min_hz=s["f_min_mhz"] * 1e6,
        f_max_hz=s["f_max_mhz"] * 1e6,
        bin_hz=float(s["bin_hz"]),
        display_min_hz=disp["f_min_mhz"] * 1e6,
        display_max_hz=disp["f_max_mhz"] * 1e6,
        lna_db=int(g["lna_db"]),
        vga_db=int(g["vga_db"]),
        amp=bool(g["amp"]),
        sample_rate=float(c["sample_rate"]),
        dwell_ms=float(c["dwell_ms"]),
    )


def load(root: Path | None = None) -> Config:
    root = Path(root) if root else find_root()
    cdir = root / "config"
    bands_y = yaml.safe_load((cdir / "bands.yaml").read_text())
    classes_y = yaml.safe_load((cdir / "classes.yaml").read_text())
    app_y = yaml.safe_load((cdir / "app.yaml").read_text())
    bands = {bid: _band(bid, d) for bid, d in (bands_y.get("bands") or {}).items()}
    enabled = [b for b in bands_y.get("enabled", []) if b in bands]
    if not enabled:
        raise ValueError("config/bands.yaml: no enabled band is defined")
    classes = [ClassInfo(c["id"], c["label"], bool(c["drone"]), list(c.get("bands", []))) for c in classes_y["classes"]]
    return Config(root=root, bands=bands, enabled=enabled, classes=classes, decision=classes_y["decision"], app=app_y)
