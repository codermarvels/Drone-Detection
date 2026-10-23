# Drone Detection

Passive drone detector for the **HackRF One**. It sweeps the 2.4 GHz ISM band, tracks drone-like emitters, records short IQ bursts of them, and a small CNN running locally names the protocol (DJI OcuSync, Wi-Fi drone, ExpressLRS 2.4, or background such as Wi-Fi and Bluetooth). Everything runs offline on your own computer or a Raspberry Pi once the repo is cloned. The HackRF only ever receives.

**Version 1 covers 2.4 GHz only**, because that is the antenna in hand. 5.8 GHz (FPV video) is one entry in `config/bands.yaml` plus new training data when the dual-band antenna arrives. Design: [HackRF drone detector: architecture](https://claude.ai/artifact/T5o3w6aBJFP1s7PRECb5tB).

## What works today, and what does not yet

| Part | Status |
|---|---|
| Sweep 2400–2490 MHz with `hackrf_sweep`, live spectrum and waterfall | Written for the HackRF; the parser is tested against hackrf_sweep's line format, but the first real run is still to do |
| Detection and tracking of fixed-channel and hopping emitters | Works, tested on the simulator |
| IQ capture of each new track with `hackrf_transfer` (50 ms at 20 Msps) | Written for the HackRF, first real run still to do |
| Recording and labelling sessions (SigMF files + `session.json`) | Works |
| CNN training, ONNX export, on-device inference, 3-of-5 track vote | Works end to end |
| **A trained model you can trust** | **Not yet.** There is no real labelled data, so no model ships with the repo. See [The AI model](#the-ai-model). |
| Map with drone and pilot positions | Needs DJI DroneID decoding, planned after v1. The map shows your receiver on cached offline tiles. |
| 5.8 GHz | Needs the dual-band antenna |

## Install

Needs Python 3.10+ and the HackRF tools.

```bash
# HackRF tools: Debian/Ubuntu/Raspberry Pi OS
sudo apt install hackrf
# macOS
brew install hackrf

git clone https://github.com/codermarvels/drone-detection.git
cd drone-detection
python3 -m venv .venv && source .venv/bin/activate
pip install -e .            # detector only: numpy, PyYAML, onnxruntime
pip install -e ".[train]"   # add PyTorch for training (laptop, not needed on the Pi)
```

## First hardware check

With the 2.4 GHz antenna on the HackRF:

```bash
hackrf_info                     # should list the board and firmware
dronedet calibrate --source hackrf
```

`calibrate` prints the noise floor and the strongest bins. Nearby Wi-Fi should show up as raised bins around 2412, 2437 or 2462 MHz. If nothing is raised, check the antenna connection.

## Run the detector

```bash
dronedet run                    # uses the HackRF if its tools are installed, else the simulator
dronedet run --source hackrf    # insist on the radio
dronedet run --source sim       # no radio: a simulated scene with Wi-Fi, Bluetooth, a DJI link at 20 s and ELRS at 40 s
```

Open the dashboard at <http://localhost:8080>. It has the live spectrum and waterfall, the detections list, the AI analysis for the selected signal, the map, recordings, the model card, and settings. Detections are also logged to `data/detections.sqlite`.

Without a model, the detector still finds and tracks signals and flags unidentified hopping emitters; it just cannot name them.

To replay recorded files instead of the radio:

```bash
hackrf_sweep -f 2400:2490 -w 250000 -l 32 -g 20 > sweep.csv     # record a sweep log (Ctrl+C to stop)
dronedet run --source replay --sweeps sweep.csv --iq data/captures/<session>
```

## The AI model

The classifier is a small CNN (about 43,000 parameters) that looks at a 256 × 256 spectrogram of each 50 ms IQ capture. It runs through onnxruntime in a few milliseconds on a laptop, so it is light enough for a Raspberry Pi. A track's label is a vote over its last 5 captures: a class must win 3 of them with at least 60% mean confidence, otherwise the signal is reported as unknown rather than forced into a drone class.

**The model needs real labelled recordings made with this HackRF and antenna, and those do not exist yet.** So the repo ships the pipeline to make one rather than a model. After training, `models/cnn.json` (the model card) records which data the model saw and the accuracy measured on held-out recording sessions, and the dashboard's AI Model page shows exactly that. Nothing in the app claims an accuracy that was not measured.

### 1. Record labelled sessions

Either use the dashboard (Recordings page: pick the class, fill in the device and distance, press Start recording), or the command line:

```bash
# DJI drone flying, controller on, 50 m away
dronedet record --source hackrf --class dji_ocusync --drone "DJI Mini 4 Pro" --distance 50 --env outdoor --count 300

# The same place with no drone: background
dronedet record --source hackrf --class wifi --count 300
dronedet record --source hackrf --class noise --count 100      # somewhere quiet

dronedet sessions                      # list what you have
dronedet label <session> --class elrs_2g4    # fix a label afterwards
```

Good data matters more than anything else:

- Record each drone or controller at 3 or more distances, indoors and out, on different days. Each run is one session.
- Record background at the same places without the drone.
- Have at least 2 sessions per class; 5 or more is better. Training holds out whole sessions to test on, so a class with one session gets an optimistic score, and the model card warns about it.
- Captures of a signal class with nothing above the noise floor are dropped automatically at training time.

Class ids are in `config/classes.yaml`: `dji_ocusync`, `wifi_drone`, `elrs_2g4`, `wifi`, `bluetooth`, `microwave`, `noise`.

### 2. Train

```bash
dronedet train --epochs 20
```

This writes `models/cnn.onnx` and `models/cnn.json`, and prints held-out accuracy and recall per class. Restart `dronedet run` to load it. Training runs fine on a laptop CPU.

### Trying the pipeline without a drone

```bash
dronedet synth          # writes synthetic sessions, clearly marked synthetic
dronedet train
dronedet run --source sim
```

This checks that everything works. The synthetic signals are simplified stand-ins, so a model trained on them has never seen a real drone: its card and the dashboard say so in a warning. On synthetic held-out sessions the test model reached about 80% accuracy, with ELRS mostly missed; that number says nothing about real-world performance. When real sessions exist, `train` leaves synthetic ones out unless you pass `--include-synthetic`.

Public datasets (DroneRF, CardRF, DroneDetect, RFUAV) were recorded on other radios. Loaders for them are not written yet, and their links and licences still need checking.

## Offline map

The map uses tiles cached on disk in `data/tiles`. Fetch them once while online, for the area around your receiver:

```bash
python tools/cache_tiles.py --lat 51.5007 --lon -0.1246 --radius-km 1.5
```

Then set `receiver.lat` and `receiver.lon` in `config/app.yaml`. The script uses OpenStreetMap by default and refuses more than 250 tiles, as their tile policy requires; pass `--url` to use another provider for a bigger area.

## Configuration

| File | What it holds |
|---|---|
| `config/bands.yaml` | Bands to sweep, gains, capture rate. `ism_5g8` is ready, commented out. |
| `config/classes.yaml` | Classes (model output order), which ones are drones, vote rules |
| `config/app.yaml` | Receiver location, detection thresholds, capture duty limit, web host and port |

By default the dashboard only listens on `127.0.0.1`. On a Pi you reach from another machine, set `web.host: 0.0.0.0`.

### Adding 5.8 GHz later

1. Fit the dual-band 2.4/5.8 GHz antenna.
2. In `config/bands.yaml`, uncomment `ism_5g8` and set `enabled: [ism_5g8]`.
3. Uncomment the FPV classes in `config/classes.yaml`, record 5.8 GHz sessions, and retrain.

## Raspberry Pi

See [deploy/pi/README.md](deploy/pi/README.md) for the udev rule and a systemd service. A Pi 5 with the active cooler is recommended; use a powered USB hub if the HackRF browns out.

## How it works

```
hackrf_sweep ─> sweeps ─> baseline ─> candidates ─> tracker ─┬─> dashboard / SQLite
                                                             │
           hackrf_transfer <─ capture scheduler <────────────┘ new or uncertain track
                 │
                 └─> 50 ms IQ ─> spectrogram tile ─> CNN (ONNX) ─> 3-of-5 vote ─> label
```

- **Baseline:** per bin, a low percentile of recent power, capped near the band's noise floor so an emitter that is on most of the time cannot hide itself.
- **Tracker:** wide signals are matched by frequency; narrow ones (up to 15 MHz) can also be matched by bandwidth, so a hopping link becomes one track. Known limit: ELRS and Bluetooth have similar widths and share a track until the classifier separates them.
- **Captures:** one HackRF cannot sweep and capture at once, so the sweep pauses for each 50 ms capture, capped at 30% of the time. For hopping tracks the capture window is placed away from Wi-Fi channels.

Code layout: `src/dronedet/radio` (HackRF, simulator, replay), `detect`, `features`, `classify`, `training`, `output` (SQLite, web server, dashboard), `pipeline.py`, `cli.py`.

## Tests

```bash
pip install -e ".[dev,train]"
pytest
```

The tests use the simulator and synthetic files, so no HackRF is needed.

## Legal

Receiving the ISM bands is generally lawful, but decoding or storing the contents of other people's transmissions can be restricted depending on your country. This software never transmits. Jamming or spoofing drones is illegal for private users almost everywhere and is out of scope.
