"""Offline map: the tile downloader never saves block pages, and MBTiles files are served."""

import importlib.util
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from dronedet.output.web import mbtiles_tile, mbtiles_zooms

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cache_tiles", ROOT / "tools" / "cache_tiles.py")
ct = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ct)

PNG = b"\x89PNG\r\n\x1a\n"
JOBS = ct.plan(25.2, 55.27, 0.5, [14, 15, 16])


def run(monkeypatch, tmp_path, fake):
    monkeypatch.setattr(ct, "fetch", fake)
    return ct.download(JOBS, ct.OSM, tmp_path / "tiles", delay=0, log=lambda *a, **k: None)


def test_real_tiles_are_saved(monkeypatch, tmp_path):
    got = run(monkeypatch, tmp_path, lambda url: PNG + url.encode())
    assert got == len(JOBS) and len(list((tmp_path / "tiles").rglob("*.png"))) == len(JOBS)


def test_block_picture_stops_the_download(monkeypatch, tmp_path):
    with pytest.raises(ct.Blocked):
        run(monkeypatch, tmp_path, lambda url: PNG + b"access blocked")


def test_http_403_stops_the_download(monkeypatch, tmp_path):
    def fake(url):
        raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)

    with pytest.raises(ct.Blocked):
        run(monkeypatch, tmp_path, fake)
    assert not list(tmp_path.rglob("*.png"))


def test_fetch_rejects_non_images(monkeypatch):
    class Resp:
        status = 200
        headers = {"Content-Type": "text/html"}

        def read(self):
            return b"<html>blocked</html>"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: Resp())
    with pytest.raises(ValueError):
        ct.fetch("https://example.org/1/2/3.png")


def test_osm_user_agent_identifies_the_app():
    assert "github.com/codermarvels" in ct.USER_AGENT


def test_mbtiles(tmp_path):
    p = tmp_path / "map.mbtiles"
    db = sqlite3.connect(p)
    db.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)")
    db.execute("INSERT INTO tiles VALUES (2, 1, 0, ?)", (PNG + b"x",))  # TMS row 0 = XYZ y 3 at zoom 2
    db.commit()
    db.close()
    assert mbtiles_zooms(p) == [2]
    assert mbtiles_tile(p, 2, 1, 3) == PNG + b"x"
    assert mbtiles_tile(p, 2, 1, 0) is None
