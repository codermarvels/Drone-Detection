"""Command line: dronedet run | record | label | sessions | synth | train | calibrate."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from pathlib import Path

from . import config as config_mod

log = logging.getLogger("dronedet")


def _source(args, cfg):
    band = cfg.band
    kind = args.source
    if kind == "auto":
        from .radio.hackrf import tools_available

        kind = "hackrf" if tools_available() else "sim"
        if kind == "sim":
            log.warning("HackRF tools not found: running the simulator. Use --source hackrf to insist on the radio.")
    if kind == "hackrf":
        from .radio.hackrf import HackRFSource

        return HackRFSource(band)
    if kind == "sim":
        from .radio.sim import SimSource

        return SimSource(band, realtime=not getattr(args, "fast", False), duration_s=getattr(args, "duration", None),
                         seed=getattr(args, "seed", 0))
    if kind == "replay":
        from .radio.replay import ReplaySource

        return ReplaySource(band, sweep_csv=args.sweeps, iq_dir=args.iq, realtime=not getattr(args, "fast", False),
                            loop=getattr(args, "loop", False))
    raise SystemExit(f"unknown source {kind}")


def cmd_run(args, cfg):
    from .output.store import Store
    from .output.web import serve
    from .pipeline import Pipeline

    src = _source(args, cfg)
    store = Store(cfg.path("data") / "detections.sqlite")
    pipe = Pipeline(cfg, src, model_path=args.model, store=store)
    httpd = None
    if not args.no_web:
        host = args.host or cfg.app["web"]["host"]
        port = args.port or cfg.app["web"]["port"]
        httpd = serve(pipe, store, cfg.path("data"), host, port)
        print(f"Dashboard: http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}")
    print(f"Watching {cfg.band.label} with input '{src.name}'. Ctrl+C to stop.")
    stop = {"flag": False}

    def _sig(*_):
        stop["flag"] = True

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    pipe.start()
    try:
        while not stop["flag"] and pipe._thread and pipe._thread.is_alive():
            time.sleep(0.5)
    finally:
        pipe.stop()
        if httpd:
            httpd.shutdown()
        store.close()
    if args.no_web:
        print(json.dumps([{k: v for k, v in r.items() if k in ("id", "label", "confidence", "summary", "center_hz")}
                          for r in pipe.snapshot()["tracks"]], indent=2))


def cmd_record(args, cfg):
    from .recording import Recorder

    if args.cls and not cfg.class_by_id(args.cls):
        raise SystemExit(f"unknown class {args.cls}; choose from: {', '.join(cfg.class_ids())}")
    src = _source(args, cfg)
    b = cfg.band
    label = {"class": args.cls, "drone_model": args.drone, "distance_m": args.distance,
             "environment": args.env, "notes": args.notes}
    rec = Recorder(cfg.path("data") / "captures", label=label, band=b.id,
                   gain={"lna_db": b.lna_db, "vga_db": b.vga_db, "amp": b.amp}, source=src.name)
    half = b.sample_rate / 2
    if args.center:
        centers = [args.center * 1e6]
    else:  # step the 20 MHz window across the band
        import numpy as np

        centers = list(np.arange(b.f_min_hz + half, b.f_max_hz - half + 1, b.sample_rate))
    print(f"Recording {args.count} captures into {rec.dir}")
    frames = src.frames()
    try:
        for i in range(args.count):
            next(frames, None)  # let the source tick (the simulator's clock, the HackRF's sweep)
            cap = src.capture(centers[i % len(centers)])
            if cap is None:
                print("capture failed; is the HackRF connected?", file=sys.stderr)
                continue
            rec.add(cap)
            print(f"\r{rec.count}/{args.count} at {cap.center_hz / 1e6:.1f} MHz", end="", flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        src.close()
    print(f"\nSaved {rec.count} captures. Session: {rec.dir.name}")


def cmd_label(args, cfg):
    from .recording import set_label

    if args.cls and not cfg.class_by_id(args.cls):
        raise SystemExit(f"unknown class {args.cls}; choose from: {', '.join(cfg.class_ids())}")
    d = Path(args.session)
    if not d.exists():
        d = cfg.path("data") / "captures" / args.session
    m = set_label(d, **{"class": args.cls, "drone_model": args.drone, "distance_m": args.distance,
                       "environment": args.env, "notes": args.notes})
    print(json.dumps(m["label"], indent=2))


def cmd_sessions(args, cfg):
    from .recording import list_sessions

    rows = list_sessions(cfg.path("data") / "captures")
    if not rows:
        print("No sessions yet. Record some with `dronedet record` or the dashboard's Recordings page.")
    for m in rows:
        lab = m.get("label") or {}
        print(f"{m['session']:<45} {lab.get('class') or 'UNLABELLED':<14} {len(m.get('captures', [])):>5} captures"
              f"{'  (synthetic)' if m.get('synthetic') else ''}")


def cmd_synth(args, cfg):
    from .training.synth import make_sessions

    drones = {c.id for c in cfg.classes if c.drone}
    out = cfg.path("data") / "captures"
    dirs = make_sessions(out, cfg.class_ids(), drones, sessions_per_class=args.sessions,
                         captures_per_session=args.captures, seed=args.seed)
    print(f"Wrote {len(dirs)} synthetic sessions under {out}. They are marked synthetic; "
          "a model trained only on them is for testing the pipeline, not for real use.")


def cmd_train(args, cfg):
    from .training import dataset, train

    drones = {c.id for c in cfg.classes if c.drone}
    data = Path(args.data) if args.data else cfg.path("data") / "captures"
    ds = dataset.load(data, cfg.class_ids(), drones, cache_dir=cfg.path("data") / "cache" / "tiles",
                      min_snr_db=args.min_snr)
    if not args.include_synthetic and ds.report.get("real_samples", 0) and ds.synthetic.any():
        keep = ~ds.synthetic
        ds.X, ds.y, ds.session, ds.synthetic = ds.X[keep], ds.y[keep], ds.session[keep], ds.synthetic[keep]
        print("Real captures found, so synthetic sessions are left out (use --include-synthetic to mix them in).")
    print(f"{len(ds.y)} captures from {ds.report['sessions_used']} sessions "
          f"({ds.report['captures_dropped_empty']} empty captures dropped).")
    out = Path(args.out) if args.out else cfg.path("model")
    card = train.train(ds, out, drones, epochs=args.epochs, seed=args.seed)
    m = card["metrics"]
    acc = "not measured (no test data)" if m["test_accuracy"] is None else f"{m['test_accuracy'] * 100:.1f}%"
    print(f"\nSaved {out} and {out.with_suffix('.json')}")
    print(f"Held-out accuracy: {acc} on {m['n_test']} captures, split {m['split']}")
    for c, r in m["per_class_recall"].items():
        print(f"  {c:<14} recall {'–' if r is None else f'{r * 100:.0f}%'}")
    for w in card["warnings"]:
        print(f"WARNING: {w}")


def cmd_calibrate(args, cfg):
    import numpy as np

    src = _source(args, cfg)
    try:
        sweeps = [f.power_db for _, f in zip(range(args.sweeps_n), src.frames())]
    finally:
        src.close()
    if not sweeps:
        raise SystemExit("No sweep received. Check `hackrf_info` and the antenna.")
    sweeps = np.stack(sweeps)
    sw = sweeps.max(axis=0)
    centers = cfg.band.bin_centers()
    floor = float(np.median(np.percentile(sweeps, 10, axis=0)))
    print(f"Noise floor: {floor:.1f} dB over {len(sweeps)} sweeps (HackRF dB, not calibrated dBm)")
    top = np.argsort(sw)[::-1][:8]
    for i in sorted(top):
        print(f"  {centers[i] / 1e6:8.2f} MHz  {sw[i]:6.1f} dB  (+{sw[i] - floor:.1f})")
    print("Strongest bins (max over the sweeps). Nearby Wi-Fi usually shows around 2412, 2437 or 2462 MHz.")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dronedet", description=__doc__)
    ap.add_argument("--root", help="folder holding config/ (default: current repo)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_source(p, default="auto"):
        p.add_argument("--source", choices=["auto", "hackrf", "sim", "replay"], default=default)
        p.add_argument("--sweeps", help="replay: hackrf_sweep CSV file")
        p.add_argument("--iq", help="replay: folder of SigMF captures")
        p.add_argument("--fast", action="store_true", help="sim/replay: run faster than real time")
        p.add_argument("--seed", type=int, default=0)

    p = sub.add_parser("run", help="detect and classify, with the dashboard")
    add_source(p)
    p.add_argument("--model", help="ONNX model (default from config/app.yaml)")
    p.add_argument("--host"); p.add_argument("--port", type=int)
    p.add_argument("--no-web", action="store_true")
    p.add_argument("--duration", type=float, help="sim: stop after this many seconds")
    p.add_argument("--loop", action="store_true", help="replay: loop the file")
    p.set_defaults(fn=cmd_run)

    def add_label(p):
        p.add_argument("--class", dest="cls", help="class id from config/classes.yaml")
        p.add_argument("--drone", help="drone or device model, e.g. 'DJI Mini 4 Pro'")
        p.add_argument("--distance", type=float, help="metres from the antenna")
        p.add_argument("--env", choices=["indoor", "outdoor"])
        p.add_argument("--notes")

    p = sub.add_parser("record", help="record labelled IQ captures for training")
    add_source(p)
    add_label(p)
    p.add_argument("--count", type=int, default=200)
    p.add_argument("--center", type=float, help="capture centre in MHz (default: step across the band)")
    p.add_argument("--interval", type=float, default=0.2, help="seconds between captures")
    p.set_defaults(fn=cmd_record)

    p = sub.add_parser("label", help="set or fix a session's label")
    p.add_argument("session", help="session folder or name")
    add_label(p)
    p.set_defaults(fn=cmd_label)

    p = sub.add_parser("sessions", help="list recording sessions")
    p.set_defaults(fn=cmd_sessions)

    p = sub.add_parser("synth", help="write synthetic sessions (pipeline testing only)")
    p.add_argument("--sessions", type=int, default=4, help="sessions per class")
    p.add_argument("--captures", type=int, default=30, help="captures per session")
    p.add_argument("--seed", type=int, default=0)
    p.set_defaults(fn=cmd_synth)

    p = sub.add_parser("train", help="train the CNN and export models/cnn.onnx")
    p.add_argument("--data", help="captures folder (default data/captures)")
    p.add_argument("--out", help="output .onnx path (default from config/app.yaml)")
    p.add_argument("--epochs", type=int, default=20)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--min-snr", type=float, default=6.0, help="drop signal-class captures weaker than this")
    p.add_argument("--include-synthetic", action="store_true")
    p.set_defaults(fn=cmd_train)

    p = sub.add_parser("calibrate", help="a few seconds of sweeps: noise floor and strongest bins")
    add_source(p)
    p.add_argument("--sweeps-n", type=int, default=100, help="number of sweeps to take")
    p.set_defaults(fn=cmd_calibrate)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = config_mod.load(Path(args.root) if args.root else None)
    args.fn(args, cfg)


if __name__ == "__main__":
    main()
