# Architecture

The full design lives in the doc [HackRF drone detector: architecture](https://claude.ai/artifact/T5o3w6aBJFP1s7PRECb5tB). Where v1 differs from it:

- **CNN first, gradient-boosted model later.** The doc starts with LightGBM on hand features. v1 ships the CNN path because it was asked for; `features/handcrafted.py` already computes the features for a LightGBM model and the dashboard shows them.
- **Smaller CNN.** About 43 k parameters instead of a 2 M MobileNetV3-Small, so it runs in float32 on a Pi without int8 quantisation. Easy to widen (`TileCNN(width=...)`) once there is enough data to justify it.
- **Baseline.** A low percentile capped near the band's noise floor, instead of a 30 s median, so a Wi-Fi drone streaming video most of the time is not absorbed into its own baseline.
- **Detection on single sweeps.** Candidates are found per sweep, where hops are still separate; max-held frames are used only for the dashboard.
- **Training code** sits in `src/dronedet/training/` (run with `dronedet train`) rather than a top-level `training/` folder, so it installs with the package. `tools/record.py` became `dronedet record`.
- **Public dataset loaders** are not written yet.
