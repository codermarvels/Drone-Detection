"""Train the tile CNN, measure it on held-out sessions, export ONNX and a model card."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from ..features.spectrogram import TILE_SPEC
from .dataset import Dataset, split_by_session

log = logging.getLogger(__name__)

NOISE_LEVEL = (0 - TILE_SPEC["floor_db"]) / (TILE_SPEC["ceil_db"] - TILE_SPEC["floor_db"])  # tile value at the floor


def augment(x: np.ndarray, rng, background: np.ndarray | None) -> np.ndarray:
    """Frequency shift, gain change of up to +-10 dB, time roll, mixed-in background."""
    x = x.copy()
    nf = x.shape[1]
    shift = int(rng.integers(-nf // 4, nf // 4 + 1))
    if shift:
        x = np.roll(x, shift, axis=1)
        if shift > 0:
            x[:, :shift] = NOISE_LEVEL
        else:
            x[:, shift:] = NOISE_LEVEL
    sig = x > NOISE_LEVEL + 0.05
    x[sig] += rng.uniform(-10, 10) / (TILE_SPEC["ceil_db"] - TILE_SPEC["floor_db"])
    x = np.roll(x, int(rng.integers(0, x.shape[0])), axis=0)
    if background is not None and rng.random() < 0.3:
        x = np.maximum(x, background)
    return np.clip(x, 0, 1)


def train(ds: Dataset, out_path: Path, drone_classes: set[str], epochs: int = 15, batch: int = 32,
          lr: float = 2e-3, seed: int = 0, threads: int | None = None) -> dict:
    import torch
    from torch import nn

    from .cnn import TileCNN, n_params

    if threads:
        torch.set_num_threads(threads)
    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)
    if len(ds.y) == 0:
        raise SystemExit("No labelled captures found. Record and label sessions first (see README).")
    tr, te, split_kind = split_by_session(ds, seed=seed)
    present = sorted(set(ds.y.tolist()))
    missing = [ds.classes[i] for i in range(len(ds.classes)) if i not in present]

    bg_rows = [i for i in tr if ds.classes[ds.y[i]] not in drone_classes]
    counts = np.bincount(ds.y[tr], minlength=len(ds.classes)).astype(np.float32)
    weights = np.where(counts > 0, counts.sum() / np.maximum(counts, 1) / len(present), 0.0)

    model = TileCNN(len(ds.classes))
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=max(1, epochs))
    lossf = nn.CrossEntropyLoss(weight=torch.tensor(weights, dtype=torch.float32))

    history = []
    for ep in range(epochs):
        model.train()
        order = rng.permutation(tr)
        total, n = 0.0, 0
        for i in range(0, len(order), batch):
            rows = order[i : i + batch]
            xb = np.stack([
                augment(ds.X[r], rng,
                        ds.X[rng.choice(bg_rows)] if bg_rows and ds.classes[ds.y[r]] in drone_classes else None)
                for r in rows
            ])
            xb = torch.from_numpy(xb[:, None])
            yb = torch.from_numpy(ds.y[rows])
            opt.zero_grad()
            loss = lossf(model(xb), yb)
            loss.backward()
            opt.step()
            total += loss.item() * len(rows)
            n += len(rows)
        sched.step()
        history.append(round(total / max(n, 1), 4))
        log.info("epoch %d/%d loss %.4f", ep + 1, epochs, history[-1])

    metrics = evaluate(model, ds, te)
    metrics.update({"split": split_kind, "n_train": int(len(tr)), "n_test": int(len(te)), "loss_history": history})

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    export_onnx(model, out_path)
    warnings = []
    if ds.report.get("synthetic_only"):
        warnings.append("Trained on synthetic signals only. Accuracy on real HackRF captures is unmeasured; "
                        "do not rely on this model's labels.")
    if missing:
        warnings.append(f"No training data for: {', '.join(missing)}. The model cannot recognise these.")
    if split_kind != "by_session":
        warnings.append("Some classes have one session only, so their test samples come from the same "
                        "session as training: the accuracy is optimistic. Record more sessions.")
    card = {
        "classes": ds.classes,
        "tile_spec": TILE_SPEC,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "params": n_params(model),
        "data": ds.report,
        "metrics": metrics,
        "warnings": warnings,
    }
    out_path.with_suffix(".json").write_text(json.dumps(card, indent=2))
    return card


def evaluate(model, ds: Dataset, rows: np.ndarray) -> dict:
    import torch

    C = len(ds.classes)
    if len(rows) == 0:
        return {"test_accuracy": None, "per_class_recall": {}, "confusion": []}
    model.eval()
    preds = []
    with torch.no_grad():
        for i in range(0, len(rows), 64):
            xb = torch.from_numpy(ds.X[rows[i : i + 64]][:, None])
            preds.append(model(xb).argmax(1).numpy())
    p = np.concatenate(preds)
    t = ds.y[rows]
    conf = np.zeros((C, C), int)
    np.add.at(conf, (t, p), 1)
    recall = {ds.classes[c]: (round(float(conf[c, c] / conf[c].sum()), 3) if conf[c].sum() else None) for c in range(C)}
    return {"test_accuracy": round(float((p == t).mean()), 4), "per_class_recall": recall, "confusion": conf.tolist()}


def export_onnx(model, path: Path) -> None:
    import torch

    model.eval()
    dummy = torch.zeros(1, 1, TILE_SPEC["time_bins"], TILE_SPEC["freq_bins"])
    torch.onnx.export(model, dummy, str(path), input_names=["tile"], output_names=["logits"],
                      dynamic_axes={"tile": {0: "n"}, "logits": {0: "n"}}, opset_version=17, dynamo=False)
