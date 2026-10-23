import numpy as np

from dronedet.detect import Baseline, Tracker, find_candidates
from dronedet.radio.sim import Emitter, SimSource


def run_sim(cfg, scene, seconds):
    band = cfg.band
    src = SimSource(band, scene=scene, realtime=False, duration_s=seconds, seed=1)
    bl = Baseline(band.n_bins, src.sweep_s)
    tk = Tracker()
    for f in src.frames():
        lvl = bl.update(f.power_db)
        if bl.ready:
            tk.update(f.t, find_candidates(f.power_db, lvl, band.bin_centers(), band.bin_hz))
    return [t for t in tk.active() if t.confirmed]


def test_candidates_merge_adjacent_bins(cfg):
    band = cfg.band
    p = np.full(band.n_bins, -95.0)
    centers = band.bin_centers()
    p[(centers > 2430e6) & (centers < 2444e6)] = -60
    c = find_candidates(p, np.full(band.n_bins, -95.0), centers, band.bin_hz)
    assert len(c) == 1
    assert abs(c[0].center_hz - 2437e6) < 0.5e6
    assert 13e6 <= c[0].width_hz <= 15e6


def test_baseline_does_not_swallow_busy_emitter(cfg):
    band = cfg.band
    bl = Baseline(band.n_bins, frame_s=0.1, window_s=5)
    centers = band.bin_centers()
    busy = (centers > 2430e6) & (centers < 2444e6)
    rng = np.random.default_rng(0)
    for _ in range(200):  # on 95% of the time
        p = -95 + rng.normal(0, 1, band.n_bins)
        if rng.random() < 0.95:
            p[busy] = -60
        lvl = bl.update(p)
    assert lvl[busy].max() < -85


def test_wifi_is_fixed_and_drone_link_hops(cfg):
    tracks = run_sim(cfg, [Emitter("wifi", 30, center_hz=2412e6), Emitter("dji_ocusync", 28)], 15)
    wifi = [t for t in tracks if abs(t.center_hz - 2412e6) < 3e6 and not t.hopping]
    hop = [t for t in tracks if t.hopping and 7e6 < t.width_hz < 15e6]
    assert wifi, [t.summary() for t in tracks]
    assert hop, [t.summary() for t in tracks]


def test_quiet_band_has_no_tracks(cfg):
    assert run_sim(cfg, [], 10) == []
