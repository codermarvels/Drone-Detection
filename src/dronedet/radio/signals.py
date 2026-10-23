"""Synthetic 2.4 GHz waveforms, for the simulator and for pipeline tests.

These are simplified stand-ins with the right shape (bandwidth, burst
timing, hopping, modulation family). They are good enough to exercise the
code end to end. They are NOT a substitute for real HackRF recordings when
training a model you intend to trust.

Every generator returns complex64 baseband at sample rate fs, length n,
with roughly unit power while the signal is on. `band_lo`/`band_hi` are
the hop range relative to the capture centre; hops outside [-fs/2, fs/2]
are simply not seen, as with the real 20 MHz capture window.
"""

from __future__ import annotations

import numpy as np

TAU = 2 * np.pi


def _mix(x: np.ndarray, f_off: float, fs: float, t0: int = 0) -> np.ndarray:
    n = np.arange(t0, t0 + len(x))
    return (x * np.exp(1j * TAU * f_off * n / fs)).astype(np.complex64)


def ofdm(rng, n_samples: int, fs: float, bw: float, nfft: int = 64, cp: int = 16) -> np.ndarray:
    """Random-QPSK OFDM of occupied bandwidth bw, centred at 0 Hz."""
    k = max(2, int(bw / fs * nfft) // 2)
    sym_len = nfft + cp
    n_sym = n_samples // sym_len + 1
    X = np.zeros((n_sym, nfft), dtype=np.complex64)
    used = np.r_[1 : k + 1, nfft - k : nfft]  # skip DC like 802.11
    qpsk = (rng.integers(0, 2, (n_sym, len(used))) * 2 - 1) + 1j * (rng.integers(0, 2, (n_sym, len(used))) * 2 - 1)
    X[:, used] = qpsk / np.sqrt(2)
    x = np.fft.ifft(X, axis=1) * np.sqrt(nfft / len(used)) * np.sqrt(nfft)
    x = np.concatenate([x[:, -cp:], x], axis=1).reshape(-1)
    return x[:n_samples].astype(np.complex64)


def lora_chirps(rng, n_samples: int, fs: float, bw: float, sf: int) -> np.ndarray:
    """LoRa-style up-chirps with random symbol values."""
    sym = int(round((2**sf) / bw * fs))
    n_sym = n_samples // sym + 1
    t = np.arange(sym) / fs
    T = sym / fs
    out = np.empty(n_sym * sym, dtype=np.complex64)
    for i in range(n_sym):
        shift = rng.integers(0, 2**sf) / 2**sf
        f = bw * (((t / T) + shift) % 1.0) - bw / 2
        phase = TAU * np.cumsum(f) / fs
        out[i * sym : (i + 1) * sym] = np.exp(1j * phase)
    return out[:n_samples]


def gfsk(rng, n_samples: int, fs: float, rate: float = 1e6, h: float = 0.32, bt: float = 0.5) -> np.ndarray:
    """Bluetooth-style GFSK."""
    sps = int(fs / rate)
    n_bits = n_samples // sps + 2
    bits = rng.integers(0, 2, n_bits) * 2 - 1
    sq = np.repeat(bits, sps).astype(np.float32)
    # Gaussian pulse shaping
    span = 3 * sps
    tt = np.arange(-span, span + 1) / sps
    sigma = np.sqrt(np.log(2)) / (TAU * bt)
    g = np.exp(-(tt**2) / (2 * sigma**2))
    g /= g.sum()
    freq = np.convolve(sq, g, mode="same")
    phase = np.pi * h * np.cumsum(freq) / sps
    return np.exp(1j * phase[:n_samples]).astype(np.complex64)


def _bursts(rng, n: int, fs: float, mean_len_s: float, duty: float, jitter: float = 0.5):
    """Random on/off burst schedule: list of (start, length) in samples."""
    out, i = [], int(rng.uniform(0, mean_len_s / max(duty, 1e-3)) * fs * rng.random())
    while i < n:
        L = max(16, int(mean_len_s * fs * rng.uniform(1 - jitter, 1 + jitter)))
        out.append((i, min(L, n - i)))
        gap = L * (1 - duty) / max(duty, 1e-3)
        i += L + max(16, int(gap * rng.uniform(1 - jitter, 1 + jitter)))
    return out


def _periodic(rng, n: int, fs: float, period_s: float, len_s: float):
    out, i = [], int(rng.uniform(0, period_s) * fs)
    while i < n:
        L = int(len_s * fs)
        out.append((i, min(L, n - i)))
        i += int(period_s * fs)
    return out


def wifi(rng, n, fs, f_off=0.0, duty=None, **_):
    """802.11g/n OFDM on a fixed 20 MHz channel, bursty traffic plus beacons."""
    duty = duty if duty is not None else rng.uniform(0.03, 0.35)
    x = np.zeros(n, dtype=np.complex64)
    sched = _bursts(rng, n, fs, rng.uniform(0.1e-3, 1.2e-3), duty) + _periodic(rng, n, fs, 0.1024, 0.4e-3)
    for s, L in sched:
        x[s : s + L] = ofdm(rng, L, fs, 16.6e6)
    return _mix(x, f_off, fs)


def wifi_drone(rng, n, fs, f_off=0.0, **_):
    """Wi-Fi drone video link: same OFDM, but a near-continuous stream."""
    x = np.zeros(n, dtype=np.complex64)
    for s, L in _bursts(rng, n, fs, rng.uniform(1.0e-3, 3.0e-3), rng.uniform(0.6, 0.9), jitter=0.3):
        x[s : s + L] = ofdm(rng, L, fs, 16.6e6)
    return _mix(x, f_off, fs)


def ocusync(rng, n, fs, band_lo=-40e6, band_hi=40e6, bw=None, **_):
    """DJI OcuSync-style link: ~10 MHz OFDM frames hopping across the band."""
    bw = bw or rng.choice([10e6, 10e6, 20e6])
    nfft = int(fs / 15e3)  # LTE-like 15 kHz spacing
    cp = nfft // 14
    x = np.zeros(n, dtype=np.complex64)
    hops = np.arange(band_lo + bw / 2, band_hi - bw / 2 + 1, bw / 2)
    for s, L in _bursts(rng, n, fs, rng.uniform(0.6e-3, 2.0e-3), rng.uniform(0.4, 0.75), jitter=0.3):
        f = rng.choice(hops)
        if abs(f) + bw / 2 > fs / 2:
            continue
        x[s : s + L] = _mix(ofdm(rng, L, fs, bw, nfft, cp), f, fs, s)
    return x


def elrs(rng, n, fs, band_lo=-40e6, band_hi=40e6, **_):
    """ExpressLRS 2.4: LoRa packets at 50-500 Hz, one hop channel per packet."""
    rate = rng.choice([50, 150, 250, 500])
    sf = {50: 8, 150: 7, 250: 6, 500: 5}[rate]
    bw = 812.5e3
    pkt_s = min(0.9 / rate, (8 + 12) * (2**sf) / bw)
    x = np.zeros(n, dtype=np.complex64)
    chans = np.arange(band_lo + 0.5e6, band_hi - 0.5e6, 1e6)
    for s, L in _periodic(rng, n, fs, 1.0 / rate, pkt_s):
        f = rng.choice(chans)
        if abs(f) + bw / 2 > fs / 2:
            continue
        x[s : s + L] = _mix(lora_chirps(rng, L, fs, bw, sf), f, fs, s)
    return x


def bluetooth(rng, n, fs, band_lo=-40e6, band_hi=40e6, **_):
    """Bluetooth classic: 1 MHz GFSK, 625 us slots, 1600 hops/s."""
    x = np.zeros(n, dtype=np.complex64)
    chans = np.arange(band_lo + 0.5e6, band_hi, 1e6)
    slot = int(625e-6 * fs)
    busy = rng.uniform(0.2, 0.8)
    for s in range(int(rng.uniform(0, slot)), n, slot):
        if rng.random() > busy:
            continue
        f = rng.choice(chans)
        if abs(f) + 0.5e6 > fs / 2:
            continue
        L = min(int(rng.choice([366e-6, 366e-6, 1.6e-3]) * fs), n - s)
        x[s : s + L] = _mix(gfsk(rng, L, fs), f, fs, s)
    return x


def microwave(rng, n, fs, f_off=0.0, mains_hz=None, **_):
    """Microwave oven magnetron: noisy carrier wandering a few MHz, on for half of each mains cycle."""
    mains = mains_hz or rng.choice([50, 60])
    t = np.arange(n) / fs
    on = (np.sin(TAU * mains * t + rng.uniform(0, TAU)) > 0).astype(np.float32)
    wander = 4e6 * np.sin(TAU * mains * t) + np.cumsum(rng.normal(0, 2e5, n)) / np.sqrt(n) * 50
    phase = TAU * np.cumsum(f_off + wander) / fs
    return (on * np.exp(1j * phase)).astype(np.complex64)


def noise(rng, n, fs, **_):
    return np.zeros(n, dtype=np.complex64)


GENERATORS = {
    "dji_ocusync": ocusync,
    "wifi_drone": wifi_drone,
    "elrs_2g4": elrs,
    "wifi": wifi,
    "bluetooth": bluetooth,
    "microwave": microwave,
    "noise": noise,
}


def render(rng, parts: list[tuple[str, float, dict]], n: int, fs: float, noise_power: float = 1.0) -> np.ndarray:
    """Sum signals over complex Gaussian noise.

    parts: (class_id, snr_db, kwargs) per signal. SNR is per-sample while on.
    """
    x = (rng.normal(0, 1, n) + 1j * rng.normal(0, 1, n)).astype(np.complex64) * np.float32(np.sqrt(noise_power / 2))
    for cid, snr_db, kw in parts:
        sig = GENERATORS[cid](rng, n, fs, **kw)
        x += sig * np.float32(np.sqrt(noise_power * 10 ** (snr_db / 10)))
    return x
