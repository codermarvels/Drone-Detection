#!/usr/bin/env python3
"""Download map tiles around the receiver once, so the dashboard map works offline.

Run this once while online:

    python tools/cache_tiles.py --lat 51.5007 --lon -0.1246 --radius-km 2

Tiles go to data/tiles/{z}/{x}/{y}.png. The default server is OpenStreetMap,
whose tile policy forbids bulk downloads, so this script refuses to fetch
more than 250 tiles from it. For a bigger area, use a tile provider that
allows offline caching and pass its URL with --url.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
import urllib.request
from pathlib import Path

OSM = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
OSM_LIMIT = 250


def tile_xy(lat: float, lon: float, z: int) -> tuple[float, float]:
    n = 2**z
    r = math.radians(lat)
    return (lon + 180) / 360 * n, (1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * n


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--radius-km", type=float, default=1.5)
    ap.add_argument("--zooms", default="13,14,15,16")
    ap.add_argument("--url", default=OSM, help="tile URL template with {z} {x} {y}")
    ap.add_argument("--out", default="data/tiles")
    args = ap.parse_args()

    jobs = []
    for z in [int(v) for v in args.zooms.split(",")]:
        x, y = tile_xy(args.lat, args.lon, z)
        # metres per tile at this latitude
        m_per_tile = 40075016.686 * math.cos(math.radians(args.lat)) / 2**z
        r = max(1, math.ceil(args.radius_km * 1000 / m_per_tile))
        for tx in range(int(x) - r, int(x) + r + 1):
            for ty in range(int(y) - r, int(y) + r + 1):
                jobs.append((z, tx % 2**z, ty))
    if args.url == OSM and len(jobs) > OSM_LIMIT:
        sys.exit(f"{len(jobs)} tiles requested; OpenStreetMap's tile policy allows at most {OSM_LIMIT} for offline "
                 "use. Use a smaller --radius-km, fewer --zooms, or another provider via --url.")
    out = Path(args.out)
    got = 0
    for z, x, y in jobs:
        p = out / str(z) / str(x) / f"{y}.png"
        if p.exists():
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(args.url.format(z=z, x=x, y=y),
                                     headers={"User-Agent": "dronedet-offline-cache/0.1 (personal use)"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                p.write_bytes(r.read())
            got += 1
        except OSError as e:
            print(f"skip {z}/{x}/{y}: {e}", file=sys.stderr)
        time.sleep(0.2)  # be gentle with the tile server
    print(f"Fetched {got} tiles ({len(jobs)} in the area) into {out}. Map data © OpenStreetMap contributors.")


if __name__ == "__main__":
    main()
