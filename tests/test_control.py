"""The dashboard's controls: start/stop, recording, labels, settings, jobs. Simulator only."""

import json
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

from dronedet import config
from dronedet.control import Controller
from dronedet.output.web import serve

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cfg_rw(tmp_path):
    """A config loaded from a copy of config/, so tests can write to it."""
    shutil.copytree(ROOT / "config", tmp_path / "config")
    c = config.load(tmp_path)
    c.app["paths"]["data"] = str(tmp_path / "data")
    c.app["paths"]["model"] = str(tmp_path / "models" / "cnn.onnx")
    return c


def wait(pred, timeout=20.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def server(cfg_rw):
    ctl = Controller(cfg_rw, None, source_opts={"fast": True})
    httpd = serve(ctl, None, cfg_rw.path("data"), "127.0.0.1", 0)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path, body=None, headers=None):
        h = {"Content-Type": "application/json", **(headers or {})}
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), headers=h)
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    yield ctl, call
    ctl.close()
    httpd.shutdown()


def test_set_gain_keeps_comments(cfg_rw):
    config.set_band_gain(cfg_rw, "ism_2g4", 24, 16, False)
    text = (cfg_rw.root / "config" / "bands.yaml").read_text()
    assert "gain: {lna_db: 24, vga_db: 16, amp: false}" in text
    assert "# Adding 5.8 GHz later" in text and "#   gain: {lna_db: 32, vga_db: 24" in text  # 5.8 block untouched
    assert config.load(cfg_rw.root).band.lna_db == 24
    with pytest.raises(ValueError):
        config.set_band_gain(cfg_rw, "ism_2g4", 20, 16, False)  # not a multiple of 8


def test_set_receiver(cfg_rw):
    config.set_receiver(cfg_rw, 25.2048, 55.2708, 'Roof "east"')
    y = yaml.safe_load((cfg_rw.root / "config" / "app.yaml").read_text())
    assert y["receiver"] == {"lat": 25.2048, "lon": 55.2708, "name": 'Roof "east"'}
    assert y["detect"]["threshold_db"] == 8
    with pytest.raises(ValueError):
        config.set_receiver(cfg_rw, 95, 0, "x")


def test_start_stop_and_switch(server):
    ctl, call = server
    st = call("/api/state")
    assert st["control"]["running"] is False and st["source"] == "none"
    assert call("/api/start", {"source": "sim"})["running"]
    assert wait(lambda: call("/api/state")["stats"]["sweeps"] > 10)
    gen = call("/api/state")["control"]["generation"]
    st = call("/api/stop", {})
    assert not st["running"] and st["generation"] > gen
    st = call("/api/start", {"source": "replay"})  # no sweep file given: a clear error, not a crash
    assert not st["running"] and "sweep" in st["error"]


def test_record_band_label_delete(server):
    ctl, call = server
    r = call("/api/record", {"on": True, "class": "wifi", "drone_model": "router", "distance_m": 3,
                             "mode": "band", "count": 6})
    assert r["on"] and r["mode"] == "band"  # the detector started by itself
    assert wait(lambda: not call("/api/state")["recording"]["on"])
    last = call("/api/state")["recording"]["last"]
    assert last["count"] == 6
    sess = call("/api/sessions")["sessions"]
    assert sess[0]["session"] == last["session"] and sess[0]["captures"] == 6
    lab = call("/api/sessions/label", {"session": last["session"], "label": {"class": "bluetooth", "notes": "fixed"}})
    assert lab["label"]["class"] == "bluetooth" and lab["label"]["drone_model"] == "router"
    with pytest.raises(urllib.error.HTTPError):
        call("/api/sessions/label", {"session": "../config", "label": {"class": "wifi"}})
    call("/api/sessions/delete", {"session": last["session"]})
    assert call("/api/sessions")["sessions"] == []


def test_post_needs_json_from_same_origin(server):
    ctl, call = server
    with pytest.raises(urllib.error.HTTPError) as e:
        call("/api/shutdown", {}, headers={"Content-Type": "text/plain"})
    assert e.value.code == 403
    with pytest.raises(urllib.error.HTTPError) as e:
        call("/api/shutdown", {}, headers={"Origin": "http://evil.example"})
    assert e.value.code == 403
    assert not ctl.shutdown.is_set()


def test_calibrate_job(server):
    ctl, call = server
    call("/api/start", {"source": "sim"})
    job = call("/api/calibrate", {})
    assert job["state"] == "running"
    assert wait(lambda: call("/api/state")["control"]["job"]["state"] != "running")
    st = call("/api/state")
    assert st["control"]["job"]["state"] == "done", st["control"]["job"]
    assert st["control"]["running"]  # restarted afterwards
    cal = call("/api/settings")["calibration"]
    assert cal["sweeps"] == 100 and len(cal["strongest"]) == 8


def test_gain_endpoint(server):
    ctl, call = server
    r = call("/api/settings/gain", {"lna_db": 16, "vga_db": 30, "amp": False})
    assert r["gain"] == {"lna_db": 16, "vga_db": 30, "amp": False}
    assert call("/api/settings")["gain"]["lna_db"] == 16


@pytest.mark.slow
def test_train_job_loads_model(server):
    pytest.importorskip("torch")
    pytest.importorskip("onnx")
    ctl, call = server
    from dronedet.training import synth

    drones = {c.id for c in ctl.cfg.classes if c.drone}
    synth.make_sessions(ctl.captures_dir, ["dji_ocusync", "wifi", "noise"], drones, sessions_per_class=2,
                        captures_per_session=6, n=200_000)
    call("/api/start", {"source": "sim"})
    assert call("/api/state")["model"] == {"loaded": False}
    call("/api/train", {"epochs": 2})
    assert wait(lambda: call("/api/state")["control"]["job"]["state"] != "running", timeout=300)
    st = call("/api/state")
    assert st["control"]["job"]["state"] == "done", st["control"]["job"]
    assert st["model"]["loaded"] and st["model"]["synthetic_only"]


FAKE_TRAIN = """
import json, sys, time
epochs = int(sys.argv[1])
print("some log line", flush=True)
for ep in range(epochs):
    print(json.dumps({"stage": f"Training · epoch {ep + 1} of {epochs}", "fraction": (ep + 1) / epochs}), flush=True)
    time.sleep(0.05)
if epochs == 13:
    print("OMP: Error #15: Initializing libomp.dylib", flush=True)
    sys.exit(134)
print(json.dumps({"result": {"test_accuracy": 0.5, "n_test": 2, "split": "by_session", "warnings": []}}), flush=True)
"""


def test_train_job_runs_in_its_own_process(server, monkeypatch):
    """Progress, result, a crash and cancel, with a stand-in for the training process."""
    import sys

    from dronedet import control

    ctl, call = server
    monkeypatch.setattr(control, "training_tools_available", lambda: True)
    monkeypatch.setattr(ctl, "_train_cmd", lambda epochs, *a: [sys.executable, "-c", FAKE_TRAIN, str(epochs)])

    def finished():
        assert wait(lambda: call("/api/state")["control"]["job"]["state"] != "running")
        return call("/api/state")["control"]["job"]

    call("/api/start", {"source": "sim"})
    call("/api/train", {"epochs": 3})
    call("/api/stop", {})  # stopping the detector mid-training does not touch the job
    job = finished()
    assert job["state"] == "done" and job["result"]["test_accuracy"] == 0.5, job
    assert job["progress"] == 1.0 and "some log line" in job["log"]

    call("/api/train", {"epochs": 13})  # the training process dies: the job fails, the app keeps running
    job = finished()
    assert job["state"] == "failed" and "Repair training tools" in job["error"], job
    assert call("/api/start", {"source": "sim"})["running"]

    call("/api/train", {"epochs": 200})
    assert wait(lambda: (call("/api/state")["control"]["job"]["progress"] or 0) > 0.02)
    call("/api/job/cancel", {})
    assert finished()["state"] == "cancelled"


def test_train_cli_reports_progress(cfg_rw, monkeypatch, capsys):
    """`dronedet train --progress-json` prints the lines the dashboard reads."""
    from dronedet import cli
    from dronedet.training import synth
    from dronedet.training import train as train_mod

    drones = {c.id for c in cfg_rw.classes if c.drone}
    synth.make_sessions(cfg_rw.path("data") / "captures", ["wifi", "noise"], drones, sessions_per_class=1,
                        captures_per_session=2, n=200_000)

    def fake_train(ds, out, drones, epochs, seed, progress):
        assert len(ds.y) == 4
        progress({"epoch": 1, "epochs": epochs, "fraction": 1.0, "loss": None})
        return {"metrics": {"test_accuracy": None, "n_test": 0, "split": "x", "per_class_recall": {}},
                "warnings": []}

    monkeypatch.setattr(train_mod, "train", fake_train)
    cli.main(["--root", str(cfg_rw.root), "train", "--progress-json", "--epochs", "1",
              "--data", str(cfg_rw.path("data") / "captures"), "--out", str(cfg_rw.path("model"))])
    msgs = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.startswith("{")]
    assert any(m.get("stage", "").startswith("Reading recordings · 4 of 4") for m in msgs)
    assert msgs[-1]["result"]["split"] == "x"


def test_pip_torch_in_conda_env_is_a_conflict(tmp_path, monkeypatch):
    import importlib.util
    import sys

    from dronedet import control

    (tmp_path / "conda-meta").mkdir()
    conda = tmp_path / "bin" / "conda"
    conda.parent.mkdir()
    conda.write_text("")
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    monkeypatch.setenv("CONDA_EXE", str(conda))
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: object() if name == "torch" else real(name, *a))
    assert control.conda_exe() == str(conda)
    assert control.torch_conflicts_with_conda()  # torch importable, but no conda-meta/pytorch-*.json
    (tmp_path / "conda-meta" / "pytorch-2.5.1-cpu_generic_py311.json").write_text("{}")
    assert not control.torch_conflicts_with_conda()
