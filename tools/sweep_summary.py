#!/usr/bin/env python3
"""Summarise a hackrf_sweep CSV: average power per MHz across all sweeps.

Usage: python tools/sweep_summary.py sweep_24.csv [--step 2]
"""
import argparse
import csv
from collections import defaultdict

import numpy as np

WIFI_24 = {1: 2412, 6: 2437, 11: 2462}


def load(path):
    """Return {bin_center_hz: [power_db, ...]} over every sweep in the file."""
    bins = defaultdict(list)
    with open(path, newline="") as f:
        for row in csv.reader(f):
            if len(row) < 7:
                continue
            hz_low = float(row[2])
            bin_width = float(row[4])
            for i, value in enumerate(row[6:]):
                bins[hz_low + (i + 0.5) * bin_width].append(float(value))
    return bins


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv")
    parser.add_argument("--step", type=float, default=2.0, help="MHz per output row")
    args = parser.parse_args()

    bins = load(args.csv)
    if not bins:
        raise SystemExit("no sweep rows found")
    sweeps = max(len(v) for v in bins.values())

    # Group bins into step-MHz buckets; mean = typical level, max = bursts.
    buckets = defaultdict(lambda: ([], []))
    for hz, values in bins.items():
        key = int(hz / 1e6 // args.step * args.step)
        buckets[key][0].append(np.mean(values))
        buckets[key][1].append(np.max(values))

    rows = sorted((mhz, float(np.mean(m)), float(np.max(x))) for mhz, (m, x) in buckets.items())
    floor = float(np.median([r[1] for r in rows]))

    print(f"{len(bins)} bins, ~{sweeps} sweeps, noise floor (median of means) {floor:.1f} dB\n")
    print("  MHz     mean    max   mean above floor")
    for mhz, mean, peak in rows:
        above = mean - floor
        bar = "#" * min(40, max(0, int(round(above * 2))))
        tag = "".join(f"  <- Wi-Fi ch{ch}" for ch, f in WIFI_24.items() if mhz <= f < mhz + args.step)
        print(f"{mhz:6d}  {mean:6.1f}  {peak:6.1f}  {bar}{tag}")

    busy = [r for r in rows if r[1] - floor >= 3]
    print("\nBusy ranges (mean >= 3 dB above floor):",
          ", ".join(f"{r[0]}-{r[0] + args.step:g} MHz" for r in busy) or "none")


if __name__ == "__main__":
    main()
