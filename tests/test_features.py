import numpy as np

from dronedet.features import TILE_SPEC, iq_features, tile
from dronedet.radio import signals


def test_tile_shape_and_range():
    rng = np.random.default_rng(0)
    iq = signals.render(rng, [("wifi", 20, {})], 200_000, 20e6)
    t = tile(iq)
    assert t.shape == (TILE_SPEC["time_bins"], TILE_SPEC["freq_bins"])
    assert t.dtype == np.float32 and 0 <= t.min() and t.max() <= 1


def test_tile_is_gain_invariant():
    rng = np.random.default_rng(0)
    iq = signals.render(rng, [("bluetooth", 20, {})], 200_000, 20e6)
    assert np.allclose(tile(iq), tile(iq * 7.5), atol=1e-4)


def test_features_separate_narrow_and_wide():
    rng = np.random.default_rng(1)
    wide = iq_features(signals.render(rng, [("wifi_drone", 20, {})], 400_000, 20e6), 20e6)
    narrow = iq_features(signals.render(rng, [("microwave", 20, {})], 400_000, 20e6), 20e6)
    assert wide["occupied_bw_hz"] > 12e6
    assert wide["duty_cycle"] > 0.4
    assert iq_features(np.zeros(400_000, np.complex64) + 1e-3, 20e6)["n_bursts"] == 0
    assert narrow["snr_db"] > 10
