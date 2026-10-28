"""End to end in simulated mode: no HackRF needed."""

import json
import time

import pytest

from dronedet.output.store import Store
from dronedet.pipeline import Pipeline
from dronedet.radio.sim import Emitter, SimSource
from dronedet.recording import list_sessions, set_label


def scene():
    return [Emitter("wifi", 30, center_hz=2412e6), Emitter("dji_ocusync", 28, t_on=2.0, kwargs={"bw": 10e6})]


def test_runs_without_a_model(cfg, tmp_path):
    src = SimSource(cfg.band, scene=scene(), realtime=False, duration_s=12, seed=2)
    store = Store(tmp_path / "d.sqlite")
    p = Pipeline(cfg, src, store=store)
    assert p.clf is None
    p.run()
    snap = p.snapshot()
    assert snap["model"] == {"loaded": False}
    assert any(t["hopping"] for t in snap["tracks"])
    assert all(t["label"] is None for t in snap["tracks"])
    assert snap["waterfall"]["rows"]
    json.dumps(snap)  # the dashboard needs plain JSON
    import time
    assert store.since(time.time() - 3600)


def test_recording_writes_labelled_session(cfg):
    src = SimSource(cfg.band, scene=scene(), realtime=False, duration_s=10, seed=3)
    p = Pipeline(cfg, src)
    p.set_recording(True, {"class": None, "drone_model": "test"})
    p.run()
    st = p.recording_status()
    assert st["count"] > 0
    root = cfg.path("data") / "captures"
    sess = list_sessions(root)
    assert len(sess) == 1 and len(sess[0]["captures"]) == st["count"]
    set_label(sess[0]["path"], **{"class": "dji_ocusync"})
    assert list_sessions(root)[0]["label"]["class"] == "dji_ocusync"


@pytest.mark.slow
def test_train_export_and_classify(cfg):
    torch = pytest.importorskip("torch")
    pytest.importorskip("onnx")
    from dronedet.classify import load_classifier
    from dronedet.training import dataset, synth, train

    drones = {c.id for c in cfg.classes if c.drone}
    classes = ["dji_ocusync", "wifi", "noise"]
    root = cfg.path("data") / "captures"
    synth.make_sessions(root, classes, drones, sessions_per_class=2, captures_per_session=6, n=200_000)
    ds = dataset.load(root, classes, drones)
    assert ds.report["synthetic_only"]
    out = cfg.path("model")
    card = train.train(ds, out, drones, epochs=2, threads=2)
    assert out.exists()
    assert card["warnings"] and "synthetic" in card["warnings"][0].lower()
    assert card["metrics"]["split"] == "by_session"
    clf = load_classifier(out)
    probs = clf.predict(ds.X[:3])
    assert probs.shape == (3, 3)
    assert abs(float(probs.sum(axis=1).mean()) - 1) < 1e-4

    src = SimSource(cfg.band, scene=scene(), realtime=False, duration_s=10, seed=4)
    p = Pipeline(cfg, src)
    assert p.clf is not None
    p.run()
    assert any(t["votes"] > 0 for t in p.snapshot()["tracks"])


def test_store_csv_export(tmp_path):
    store = Store(tmp_path / "d.sqlite")
    now = time.time()
    store.upsert([{"run": "r1", "id": 1, "first_seen": now - 5, "last_seen": now, "center_hz": 2.437e9,
                   "width_hz": 2e7, "peak_db": -40.0, "hopping": 0, "label": "wifi", "confidence": 0.9,
                   "drone": 0, "captures": 2, "summary": "wide steady"}])
    store.close()
    store = Store(tmp_path / "d.sqlite")  # reopened, as after a restart
    lines = store.csv_since(0).splitlines()
    assert lines[0].startswith("first_seen,last_seen")
    assert len(lines) == 2 and ",2.4 GHz,2437.0,20.0,-40.0,no,wifi,0.9,no,2," in lines[1]
    assert len(store.csv_since(now + 60).splitlines()) == 1
