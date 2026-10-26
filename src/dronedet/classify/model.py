"""Run the exported CNN with onnxruntime.

A model is two files: `cnn.onnx` and a `cnn.json` model card beside it,
written by training/train_cnn.py. The card holds the class order, the tile
spec the model was trained on, what data it saw, and the accuracy measured
on held-out sessions. The dashboard shows the card as is, so it never
claims more than training measured.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..features.spectrogram import TILE_SPEC

log = logging.getLogger(__name__)


@dataclass
class ModelCard:
    classes: list[str]
    tile_spec: dict
    trained_at: str = ""
    data: dict = field(default_factory=dict)     # sessions/samples per class, synthetic or real
    metrics: dict = field(default_factory=dict)  # held-out accuracy, per-class recall, confusion
    params: int = 0
    raw: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "ModelCard":
        d = json.loads(Path(path).read_text())
        return cls(classes=d["classes"], tile_spec=d["tile_spec"], trained_at=d.get("trained_at", ""),
                   data=d.get("data", {}), metrics=d.get("metrics", {}), params=d.get("params", 0), raw=d)

    @property
    def synthetic_only(self) -> bool:
        return bool(self.data.get("synthetic_only", False))


class OnnxClassifier:
    def __init__(self, onnx_path: Path, threads: int = 2):
        import onnxruntime as ort

        self.path = Path(onnx_path)
        self.card = ModelCard.load(self.path.with_suffix(".json"))
        if self.card.tile_spec != TILE_SPEC:
            log.warning("model was trained with a different tile spec; retrain it: %s", self.card.tile_spec)
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        so.inter_op_num_threads = 1
        self.sess = ort.InferenceSession(str(self.path), so, providers=["CPUExecutionProvider"])
        self.input = self.sess.get_inputs()[0].name

    @property
    def classes(self) -> list[str]:
        return self.card.classes

    def predict(self, tiles: np.ndarray) -> np.ndarray:
        """tiles: [N, H, W] or [H, W] float32 -> [N, C] probabilities."""
        x = np.asarray(tiles, dtype=np.float32)
        if x.ndim == 2:
            x = x[None]
        logits = self.sess.run(None, {self.input: x[:, None]})[0]
        e = np.exp(logits - logits.max(axis=1, keepdims=True))
        return e / e.sum(axis=1, keepdims=True)


def load_classifier(onnx_path: Path) -> OnnxClassifier | None:
    """The classifier, or None if no model has been trained yet."""
    p = Path(onnx_path)
    if not p.exists() or not p.with_suffix(".json").exists():
        log.warning("no model at %s: tracks will be detected but not classified. "
                    "Record labelled sessions, then train (dashboard AI Model page, or `dronedet train`).", p)
        return None
    try:
        return OnnxClassifier(p)
    except Exception as e:  # a corrupt or mismatched model should not stop detection
        log.error("could not load %s: %s", p, e)
        return None
