"""IQ sample conversion and SigMF files.

The HackRF delivers interleaved signed 8-bit I/Q. Captures are stored as
SigMF (`.sigmf-data` raw ci8 plus a `.sigmf-meta` JSON), which keeps them
small (2 bytes per sample) and readable by other SDR tools.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np


def ci8_to_complex(raw: bytes | np.ndarray) -> np.ndarray:
    a = np.frombuffer(raw, dtype=np.int8) if isinstance(raw, (bytes, bytearray)) else raw.astype(np.int8)
    a = a[: len(a) // 2 * 2].astype(np.float32) / 128.0
    return (a[0::2] + 1j * a[1::2]).astype(np.complex64)


def complex_to_ci8(iq: np.ndarray, scale: float | None = None) -> np.ndarray:
    """Quantise to int8 the way the HackRF would. Scales to ~-3 dBFS peaks."""
    if scale is None:
        peak = float(np.max(np.abs(np.concatenate([iq.real, iq.imag])))) or 1.0
        scale = 0.7 / peak
    out = np.empty(2 * len(iq), dtype=np.int8)
    out[0::2] = np.clip(np.round(iq.real * scale * 128), -127, 127)
    out[1::2] = np.clip(np.round(iq.imag * scale * 128), -127, 127)
    return out


def write_sigmf(base: Path, iq: np.ndarray, sample_rate: float, center_hz: float,
                t: float | None = None, extra: dict | None = None) -> Path:
    """Write base.sigmf-data and base.sigmf-meta. Returns the meta path."""
    base = Path(base)
    base.parent.mkdir(parents=True, exist_ok=True)
    data = iq if iq.dtype == np.int8 else complex_to_ci8(iq)
    (base.with_suffix(".sigmf-data")).write_bytes(data.tobytes())
    when = datetime.fromtimestamp(t if t is not None else datetime.now().timestamp(), tz=timezone.utc)
    meta = {
        "global": {
            "core:datatype": "ci8",
            "core:sample_rate": sample_rate,
            "core:version": "1.0.0",
            "core:hw": "HackRF One",
            "core:recorder": "dronedet",
            **(extra or {}),
        },
        "captures": [{"core:sample_start": 0, "core:frequency": center_hz,
                      "core:datetime": when.isoformat().replace("+00:00", "Z")}],
        "annotations": [],
    }
    mpath = base.with_suffix(".sigmf-meta")
    mpath.write_text(json.dumps(meta, indent=2))
    return mpath


def read_sigmf(meta_path: Path) -> tuple[np.ndarray, dict]:
    """Read a capture. Returns (complex64 IQ, meta dict)."""
    meta_path = Path(meta_path)
    meta = json.loads(meta_path.read_text())
    dtype = meta["global"]["core:datatype"]
    raw = meta_path.with_suffix(".sigmf-data").read_bytes()
    if dtype == "ci8":
        iq = ci8_to_complex(raw)
    elif dtype == "cf32_le":
        iq = np.frombuffer(raw, dtype=np.complex64).copy()
    elif dtype == "ci16_le":
        a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        iq = (a[0::2] + 1j * a[1::2]).astype(np.complex64)
    else:
        raise ValueError(f"{meta_path}: unsupported datatype {dtype}")
    return iq, meta
