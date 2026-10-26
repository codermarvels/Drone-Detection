"""Load band, class and app settings from the config/ folder."""

from __future__ import annotations

import json
import os
import re
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


# -- writing settings from the dashboard --------------------------------------
# These edit single lines in place, so the comments in the YAML files survive.

LNA_STEPS = range(0, 41, 8)   # HackRF LNA (IF) gain: 0-40 dB in 8 dB steps
VGA_STEPS = range(0, 63, 2)   # HackRF VGA (baseband) gain: 0-62 dB in 2 dB steps


def _section(lines: list[str], key: str, indent: int) -> tuple[int, int]:
    """Line range [start, end) of the block under `key:` at the given indent."""
    head = re.compile(rf"^{' ' * indent}{re.escape(key)}:\s*(#.*)?$")
    for i, line in enumerate(lines):
        if head.match(line):
            j = i + 1
            while j < len(lines):
                s = lines[j]
                if s.strip() and not s.lstrip().startswith("#") and len(s) - len(s.lstrip()) <= indent:
                    break
                j += 1
            return i + 1, j
    raise ValueError(f"no `{key}:` section found")


def set_band_gain(cfg: Config, band_id: str, lna_db: int, vga_db: int, amp: bool) -> Band:
    """Write a band's gain to config/bands.yaml and apply it to the loaded config."""
    if band_id not in cfg.bands:
        raise ValueError(f"unknown band {band_id}")
    lna_db, vga_db = int(lna_db), int(vga_db)
    if lna_db not in LNA_STEPS:
        raise ValueError("LNA gain must be 0-40 dB in steps of 8")
    if vga_db not in VGA_STEPS:
        raise ValueError("VGA gain must be 0-62 dB in steps of 2")
    path = cfg.root / "config" / "bands.yaml"
    lines = path.read_text().splitlines(keepends=True)
    start, end = _section(lines, "bands", 0)
    b0, b1 = _section(lines[start:end], band_id, 2)
    for i in range(start + b0, start + b1):
        m = re.match(r"^(\s+)gain:\s*\{.*\}(\s*#.*)?\s*$", lines[i])
        if m:
            lines[i] = (f"{m.group(1)}gain: {{lna_db: {lna_db}, vga_db: {vga_db}, amp: {'true' if amp else 'false'}}}"
                        f"{m.group(2) or ''}\n")
            break
    else:
        raise ValueError(f"no `gain: {{...}}` line under {band_id} in bands.yaml")
    path.write_text("".join(lines))
    b = cfg.bands[band_id]
    b.lna_db, b.vga_db, b.amp = lna_db, vga_db, bool(amp)
    return b


def set_receiver(cfg: Config, lat: float, lon: float, name: str) -> dict:
    """Write the receiver location to config/app.yaml and apply it to the loaded config."""
    lat, lon = float(lat), float(lon)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("latitude must be -90 to 90 and longitude -180 to 180")
    name = (name or "Receiver").strip()[:60] or "Receiver"
    path = cfg.root / "config" / "app.yaml"
    lines = path.read_text().splitlines(keepends=True)
    start, end = _section(lines, "receiver", 0)
    values = {"lat": f"{lat:.6f}", "lon": f"{lon:.6f}", "name": json.dumps(name)}
    for i in range(start, end):
        m = re.match(r"^(\s+)(lat|lon|name):", lines[i])
        if m and m.group(2) in values:
            lines[i] = f"{m.group(1)}{m.group(2)}: {values.pop(m.group(2))}\n"
    for k, v in values.items():  # a key missing from the file
        lines.insert(end, f"  {k}: {v}\n")
    path.write_text("".join(lines))
    cfg.app.setdefault("receiver", {}).update({"lat": lat, "lon": lon, "name": name})
    return cfg.app["receiver"]
