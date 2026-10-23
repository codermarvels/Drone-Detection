import numpy as np

from dronedet.radio.hackrf import SweepAssembler, parse_sweep_line
from dronedet.radio.iq import ci8_to_complex, complex_to_ci8, read_sigmf, write_sigmf
from dronedet.radio.replay import ReplaySource


def _lines(band, levels):
    """Fake hackrf_sweep output: 5 MHz lines with 20 bins of 250 kHz, in the tool's
    interleaved order (each 20 MHz step reports its quarters out of order)."""
    out = []
    order = [b + o for b in range(int(band.f_min_hz), int(band.f_max_hz), 20_000_000)
             for o in (0, 10_000_000, 5_000_000, 15_000_000) if b + o < band.f_max_hz]
    for low in order:
        vals = [levels(low + (i + 0.5) * 250_000) for i in range(20)]
        out.append(f"2026-10-23, 12:00:00.000000, {low}, {low + 5_000_000}, 250000.00, 20, " + ", ".join(f"{v:.1f}" for v in vals))
    return out


def test_parse_line():
    hz, w, v = parse_sweep_line("2026-10-23, 12:00:00.1, 2400000000, 2405000000, 250000.00, 20, -70.5, -71.0")
    assert hz == 2400e6 and w == 250e3 and list(v) == [-70.5, -71.0]
    assert parse_sweep_line("garbage") is None


def test_assembler_builds_full_sweeps(cfg):
    band = cfg.band
    lvl = lambda f: -60.0 if abs(f - 2437e6) < 8e6 else -95.0
    asm = SweepAssembler(band)
    sweeps = [s for _ in range(3) for line in _lines(band, lvl) if (s := asm.feed(line)) is not None]
    assert len(sweeps) == 2  # the third is still open
    s = sweeps[0]
    assert s.shape == (band.n_bins,)
    centers = band.bin_centers()
    assert s[np.argmin(abs(centers - 2437e6))] == -60.0
    assert s[np.argmin(abs(centers - 2412e6))] == -95.0


def test_ci8_roundtrip_and_sigmf(tmp_path):
    rng = np.random.default_rng(0)
    iq = (rng.normal(size=1000) + 1j * rng.normal(size=1000)).astype(np.complex64)
    q = complex_to_ci8(iq)
    back = ci8_to_complex(q)
    assert back.shape == iq.shape
    assert np.corrcoef(back.real, iq.real)[0, 1] > 0.99
    meta = write_sigmf(tmp_path / "cap", iq, 20e6, 2440e6)
    iq2, m = read_sigmf(meta)
    assert m["global"]["core:datatype"] == "ci8"
    assert m["captures"][0]["core:frequency"] == 2440e6
    assert np.allclose(iq2, back)


def test_replay_sweep_csv(cfg, tmp_path):
    band = cfg.band
    csv = tmp_path / "sweep.csv"
    lines = []
    for k in range(10):
        lines += [l.replace("12:00:00.000000", f"12:00:{k:02d}.000000") for l in _lines(band, lambda f: -90.0)]
    csv.write_text("\n".join(lines) + "\n")
    frames = list(ReplaySource(band, sweep_csv=csv).frames())
    assert len(frames) == 9
    assert frames[1].t - frames[0].t == 1.0
