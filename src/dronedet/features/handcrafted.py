"""Hand-built features from one IQ capture.

These explain a detection in plain terms (bandwidth, burst timing) on the
dashboard, and are the input for a gradient-boosted model later.
"""

from __future__ import annotations

import numpy as np


def iq_features(iq: np.ndarray, fs: float) -> dict:
    n = len(iq)
    # Envelope power, smoothed over ~10 us
    k = max(1, int(10e-6 * fs))
    pw = np.abs(iq) ** 2
    env = np.convolve(pw, np.ones(k, dtype=np.float32) / k, mode="same")[::k]
    floor = float(np.percentile(env, 20)) + 1e-12
    on = env > floor * 4  # ~6 dB above the floor
    duty = float(on.mean())
    edges = np.flatnonzero(np.diff(on.astype(np.int8)))
    # Burst lengths and gaps from on/off runs
    runs = np.diff(np.r_[0, edges + 1, len(on)])
    states = on[np.r_[0, edges + 1]]
    dt = k / fs
    bursts = runs[states] * dt
    gaps = runs[~states] * dt
    # Average spectrum
    nfft = 1024
    m = n // nfft
    seg = iq[: m * nfft].reshape(m, nfft) * np.hanning(nfft)
    spec = np.fft.fftshift((np.abs(np.fft.fft(seg, axis=1)) ** 2).mean(axis=0))
    sdb = 10 * np.log10(spec + 1e-12)
    peak = int(np.argmax(sdb))
    occ = sdb > sdb[peak] - 10
    bw = float(occ.sum()) * fs / nfft
    band = spec[occ]
    flat = float(np.exp(np.mean(np.log(band + 1e-12))) / (np.mean(band) + 1e-12)) if band.size else 0.0
    return {
        "duty_cycle": round(duty, 3),
        "n_bursts": int(states.sum()),
        "burst_ms": round(float(np.median(bursts)) * 1e3, 3) if bursts.size else 0.0,
        "gap_ms": round(float(np.median(gaps)) * 1e3, 3) if gaps.size else 0.0,
        "occupied_bw_hz": bw,
        "spectral_flatness": round(flat, 3),
        "peak_offset_hz": (peak - nfft / 2) * fs / nfft,
        "snr_db": round(float(10 * np.log10(env.max() / floor)), 1),
    }
