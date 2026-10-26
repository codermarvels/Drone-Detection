"""The real HackRF One, driven through the hackrf_sweep and hackrf_transfer tools.

One HackRF cannot sweep and stream IQ at the same time, so capture() stops
the sweep process, records a short IQ burst with hackrf_transfer, and
restarts the sweep. The HackRF stays in receive mode throughout: nothing in
this module can transmit.
"""

from __future__ import annotations

import logging
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Iterator

import numpy as np

from ..config import Band
from .base import Capture, SweepFrame
from .iq import ci8_to_complex

log = logging.getLogger(__name__)


def parse_sweep_line(line: str):
    """Parse one hackrf_sweep CSV line.

    Format: date, time, hz_low, hz_high, hz_bin_width, num_samples, dB, dB, ...
    Returns (hz_low, bin_width, values) or None for anything else.
    """
    parts = line.strip().split(",")
    if len(parts) < 7:
        return None
    try:
        hz_low = float(parts[2])
        bin_w = float(parts[4])
        vals = np.array([float(p) for p in parts[6:]], dtype=np.float32)
    except ValueError:
        return None
    return hz_low, bin_w, vals


class SweepAssembler:
    """Collects hackrf_sweep lines into full sweeps on the band's fixed bin grid."""

    def __init__(self, band: Band):
        self.band = band
        self._cur = np.full(band.n_bins, np.nan, dtype=np.float32)
        self._last_low: float | None = None
        self._filled = False
        self._span = band.f_max_hz - band.f_min_hz

    def feed(self, line: str) -> np.ndarray | None:
        p = parse_sweep_line(line)
        if p is None:
            return None
        hz_low, bin_w, vals = p
        done = None
        # A sweep restarts when the tuning jumps back by more than half the span.
        if self._last_low is not None and hz_low < self._last_low - self._span / 2 and self._filled:
            done = self._finish()
        self._last_low = hz_low
        centers = hz_low + (np.arange(len(vals)) + 0.5) * bin_w
        idx = np.floor((centers - self.band.f_min_hz) / self.band.bin_hz).astype(int)
        ok = (idx >= 0) & (idx < self.band.n_bins)
        if ok.any():
            np.fmax.at(self._cur, idx[ok], vals[ok])
            self._filled = True
        return done

    def _finish(self) -> np.ndarray:
        out = self._cur
        # Fill any bins the sweep skipped from their neighbours.
        if np.isnan(out).any():
            good = ~np.isnan(out)
            if good.any():
                out = np.interp(np.arange(len(out)), np.flatnonzero(good), out[good]).astype(np.float32)
            else:
                out = np.full_like(out, -100.0)
        self._cur = np.full(self.band.n_bins, np.nan, dtype=np.float32)
        self._filled = False
        return out


def tools_available() -> bool:
    return bool(shutil.which("hackrf_sweep") and shutil.which("hackrf_transfer"))


class HackRFSource:
    name = "hackrf"

    def __init__(self, band: Band):
        if not tools_available():
            raise RuntimeError(
                "hackrf_sweep/hackrf_transfer not found. Install the HackRF tools "
                "(apt install hackrf, or brew install hackrf) and plug in the HackRF."
            )
        self.band = band
        self._q: queue.Queue[SweepFrame] = queue.Queue(maxsize=500)
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()
        self._closed = False
        self.problem: str | None = None  # shown on the dashboard while the radio is not delivering sweeps
        self._start_sweep()

    def _sweep_cmd(self) -> list[str]:
        b = self.band
        return [
            "hackrf_sweep",
            "-f", f"{int(b.f_min_hz / 1e6)}:{int(np.ceil(b.f_max_hz / 1e6))}",
            "-w", str(int(b.bin_hz)),
            "-l", str(b.lna_db), "-g", str(b.vga_db), "-a", "1" if b.amp else "0",
        ]

    def _start_sweep(self) -> None:
        self._proc = subprocess.Popen(self._sweep_cmd(), stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self._reader = threading.Thread(target=self._read, args=(self._proc,), daemon=True)
        self._reader.start()

    def _stop_sweep(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self._reader:
            self._reader.join(timeout=2)

    def _read(self, proc: subprocess.Popen) -> None:
        asm = SweepAssembler(self.band)
        assert proc.stdout
        for line in proc.stdout:
            sweep = asm.feed(line)
            if sweep is not None:
                frame = SweepFrame(t=time.time(), power_db=sweep)
                try:
                    self._q.put_nowait(frame)
                except queue.Full:  # the pipeline is behind; drop the oldest frame
                    try:
                        self._q.get_nowait()
                    except queue.Empty:
                        pass
                    self._q.put_nowait(frame)

    def frames(self) -> Iterator[SweepFrame]:
        while not self._closed:
            try:
                frame = self._q.get(timeout=1.0)
            except queue.Empty:
                if self._proc and self._proc.poll() is not None and not self._lock.locked():
                    log.warning("hackrf_sweep exited (code %s); restarting", self._proc.returncode)
                    self.problem = (f"hackrf_sweep stopped (exit code {self._proc.returncode}) and is being "
                                    "restarted. Is the HackRF plugged in, and not in use by another program?")
                    time.sleep(1)
                    if not self._closed:
                        self._start_sweep()
                continue
            self.problem = None
            yield frame

    def capture(self, center_hz: float) -> Capture | None:
        b = self.band
        with self._lock:
            t0 = time.time()
            self._stop_sweep()
            with tempfile.TemporaryDirectory() as td:
                path = Path(td) / "cap.ci8"
                cmd = ["hackrf_transfer", "-r", str(path), "-f", str(int(center_hz)),
                       "-s", str(int(b.sample_rate)), "-n", str(b.capture_samples),
                       "-l", str(b.lna_db), "-g", str(b.vga_db), "-a", "1" if b.amp else "0"]
                try:
                    subprocess.run(cmd, check=True, timeout=10,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    raw = path.read_bytes()
                except (subprocess.SubprocessError, OSError) as e:
                    log.warning("IQ capture failed: %s", e)
                    raw = b""
            if not self._closed:
                self._start_sweep()
            if not raw:
                return None
            return Capture(t=t0, center_hz=center_hz, sample_rate=b.sample_rate,
                           iq=ci8_to_complex(raw), duration_s=time.time() - t0)

    def close(self) -> None:
        self._closed = True
        self._stop_sweep()
