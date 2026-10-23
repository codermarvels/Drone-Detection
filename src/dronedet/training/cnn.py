"""A small CNN for 1 x 256 x 256 spectrogram tiles.

Depthwise-separable blocks (MobileNet style) keep it to about 30 k
parameters and roughly 30 MFLOPs per tile, so it runs in a few
milliseconds on a Raspberry Pi 5 with onnxruntime.
"""

from __future__ import annotations

import torch
from torch import nn


def _dw(cin: int, cout: int, stride: int) -> nn.Sequential:
    return nn.Sequential(
        nn.Conv2d(cin, cin, 3, stride, 1, groups=cin, bias=False),
        nn.BatchNorm2d(cin),
        nn.ReLU(inplace=True),
        nn.Conv2d(cin, cout, 1, bias=False),
        nn.BatchNorm2d(cout),
        nn.ReLU(inplace=True),
    )


class TileCNN(nn.Module):
    def __init__(self, n_classes: int, width: int = 16):
        super().__init__()
        w = width
        self.features = nn.Sequential(
            nn.Conv2d(1, w, 3, 2, 1, bias=False), nn.BatchNorm2d(w), nn.ReLU(inplace=True),  # 128
            _dw(w, 2 * w, 2),       # 64
            _dw(2 * w, 4 * w, 2),   # 32
            _dw(4 * w, 6 * w, 2),   # 16
            _dw(6 * w, 8 * w, 2),   # 8
            _dw(8 * w, 8 * w, 1),
        )
        self.head = nn.Sequential(nn.AdaptiveAvgPool2d(1), nn.Flatten(), nn.Dropout(0.2), nn.Linear(8 * w, n_classes))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.head(self.features(x))


def n_params(m: nn.Module) -> int:
    return sum(p.numel() for p in m.parameters())
