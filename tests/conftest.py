from pathlib import Path

import pytest

from dronedet import config

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cfg(tmp_path):
    c = config.load(ROOT)
    c.app["paths"]["data"] = str(tmp_path / "data")
    c.app["paths"]["model"] = str(tmp_path / "models" / "cnn.onnx")
    return c
