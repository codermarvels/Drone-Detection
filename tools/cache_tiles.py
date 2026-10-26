#!/usr/bin/env python3
"""Download map tiles around the receiver once, so the dashboard map works offline.

Run this once while online (or press Download map on the dashboard's Settings page):

    python tools/cache_tiles.py --lat 51.5007 --lon -0.1246 --radius-km 1

Tiles go to data/tiles/{z}/{x}/{y}.png. The default server is OpenStreetMap.
Its tile usage policy (https://operations.osmfoundation.org/policies/tiles/)
asks for an identifying User-Agent, a gentle request rate, and no bulk
downloads, so this script sends one, waits between tiles, and refuses more
than 250 tiles from it. For a bigger area, use a provider that allows
offline caching and pass its URL with --url, or put an MBTiles file at
data/map.mbtiles, which the dashboard serves directly.

Tiles are written to a new folder that replaces the old one only when the
download succeeds. Anything that is not a real map tile (an error, an HTML
page, or OpenStreetMap's "Access blocked" image, which is the same picture
for every tile) is never saved.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import shutil
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

OSM = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
OSM_LIMIT = 250
USER_AGENT = "dronedet-map-cache/0.2 (+https://github.com/codermarvels/Drone-Detection; one-off personal offline cache)"
PNG, JPEG = b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"


class Blocked(Exception):
    pass


def tile_xy(lat: float, lon: float, z: int) -> tuple[float, float]:
    n = 2**z
    r = math.radians(lat)
    return (lon + 180) / 360 * n, (1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * n


def plan(lat: float, lon: float, radius_km: float, zooms: list[int]) -> list[tuple[int, int, int]]:
    jobs = []
    for z in zooms:
        x, y = tile_xy(lat, lon, z)
        m_per_tile = 40075016.686 * math.cos(math.radians(lat)) / 2**z  # metres per tile at this latitude
        r = max(1, math.ceil(radius_km * 1000 / m_per_tile))
        for tx in range(int(x) - r, int(x) + r + 1):
            for ty in range(int(y) - r, int(y) + r + 1):
                if 0 <= ty < 2**z:
                    jobs.append((z, tx % 2**z, ty))
    return jobs


def fetch(url: str) -> bytes:
    """One tile, or an exception saying why it is not a usable tile."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "image/png,image/*"})
    with urllib.request.urlopen(req, timeout=20) as r:
        body = r.read()
        ctype = r.headers.get("Content-Type", "")
        if r.status != 200:
            raise urllib.error.HTTPError(url, r.status, "not 200", r.headers, None)
    if not ctype.startswith("image/") or not (body.startswith(PNG) or body.startswith(JPEG)):
        raise ValueError(f"not an image ({ctype or 'no content type'}, {len(body)} bytes)")
    return body


def download(jobs, url: str, out: Path, delay: float, log=print) -> int:
    """Fetch every tile into out. Raises Blocked if the server answers with one picture for all of them."""
    seen: Counter[str] = Counter()
    zooms_of: dict[str, set[int]] = {}
    got = failed = 0
    for i, (z, x, y) in enumerate(jobs):
        try:
            body = fetch(url.format(z=z, x=x, y=y))
        except urllib.error.HTTPError as e:
            failed += 1
            if e.code in (403, 418, 429):
                raise Blocked(f"the tile server refused the download (HTTP {e.code})") from e
            log(f"skip {z}/{x}/{y}: HTTP {e.code}")
            continue
        except (OSError, ValueError) as e:
            failed += 1
            log(f"skip {z}/{x}/{y}: {e}")
            if failed >= 10 and got == 0:
                raise Blocked(f"no tile could be downloaded ({e})") from e
            continue
        h = hashlib.sha1(body).hexdigest()
        seen[h] += 1
        zooms_of.setdefault(h, set()).add(z)
        # A real map has many different tiles. The same image for most tiles across several zoom
        # levels is a block or error page served as a picture (OpenStreetMap's "Access blocked").
        common, n = seen.most_common(1)[0]
        if i >= 7 and n >= 6 and n / (got + 1) > 0.6 and len(zooms_of[common]) > 1:
            raise Blocked("the tile server answers every tile with the same picture, which is how "
                          "OpenStreetMap shows an 'Access blocked' notice")
        p = out / str(z) / str(x) / f"{y}.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
        got += 1
        if (i + 1) % 10 == 0 or i + 1 == len(jobs):
            log(f"{i + 1}/{len(jobs)} tiles", flush=True)
        time.sleep(delay)
    if got == 0:
        raise Blocked("no tile could be downloaded")
    return got


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--radius-km", type=float, default=1.0)
    ap.add_argument("--zooms", default="13,14,15,16")
    ap.add_argument("--url", default=OSM, help="tile URL template with {z} {x} {y}")
    ap.add_argument("--out", default="data/tiles")
    ap.add_argument("--delay", type=float, default=0.5, help="seconds between tiles")
    args = ap.parse_args()

    jobs = plan(args.lat, args.lon, args.radius_km, [int(v) for v in args.zooms.split(",")])
    if args.url == OSM and len(jobs) > OSM_LIMIT:
        sys.exit(f"{len(jobs)} tiles requested; OpenStreetMap's tile policy allows at most {OSM_LIMIT} for offline "
                 "use. Use a smaller --radius-km, fewer --zooms, or another provider via --url.")
    out = Path(args.out)
    tmp = out.with_name(out.name + ".new")
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"Downloading {len(jobs)} tiles from {args.url.split('/{z}')[0]}", flush=True)
    try:
        got = download(jobs, args.url, tmp, args.delay, log=lambda *a, **k: print(*a, flush=True))
    except Blocked as e:
        shutil.rmtree(tmp, ignore_errors=True)
        sys.exit(f"Map download stopped: {e}. Nothing was saved. Try again later, use another tile provider "
                 "(a tile URL with your own key), or put an MBTiles file at data/map.mbtiles.")
    shutil.rmtree(out, ignore_errors=True)  # also clears any bad tiles from an earlier download
    tmp.rename(out)
    print(f"Saved {got} tiles ({len(jobs)} in the area) into {out}. Map data © OpenStreetMap contributors.")


if __name__ == "__main__":
    main()
