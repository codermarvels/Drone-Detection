"""Spectrogram tiles: the CNN's input.

One function makes tiles for both training and live use, so the model never
sees a preprocessing mismatch. Change TILE_SPEC and you must retrain.
"""

from __future__ import annotations

import numpy as np

TILE_SPEC = {
    "version": 1,
    "nfft": 1024,
    "time_bins": 256,
    "freq_bins": 256,
    "floor_db": -5.0,   # clip range relative to the tile's noise floor
    "ceil_db": 45.0,
}

_WINDOWS: dict[int, np.ndarray] = {}


def tile(iq: np.ndarray, spec: dict = TILE_SPEC) -> np.ndarray:
    """IQ capture -> float32 [time_bins, freq_bins] log-power tile in [0, 1].

    Axis 0 is time (oldest first), axis 1 is frequency (low to high across
    the capture bandwidth). Values are dB above the tile's median (its noise
    floor), so gain changes shift little.
    """
    nfft, nt, nf = spec["nfft"], spec["time_bins"], spec["freq_bins"]
    if len(iq) < nfft:
        iq = np.pad(iq, (0, nfft - len(iq)))
    win = _WINDOWS.get(nfft)
    if win is None:
        win = _WINDOWS[nfft] = np.hanning(nfft).astype(np.float32)
    starts = np.linspace(0, len(iq) - nfft, nt).astype(np.int64)
    idx = starts[:, None] + np.arange(nfft)[None, :]
    frames = iq[idx] * win
    p = np.abs(np.fft.fftshift(np.fft.fft(frames, axis=1), axes=1)) ** 2
    p = p.reshape(nt, nf, nfft // nf).mean(axis=2)
    db = 10 * np.log10(p + 1e-12)
    db -= np.median(db)
    lo, hi = spec["floor_db"], spec["ceil_db"]
    return ((np.clip(db, lo, hi) - lo) / (hi - lo)).astype(np.float32)
