"""Local dashboard server. Standard library only, no internet needed.

Binds to 127.0.0.1 by default; set web.host in config/app.yaml to 0.0.0.0
to reach a Raspberry Pi from another machine on your network. Anyone who
can reach the port can then also start, stop, record and train, so only do
that on a network you trust.

The POST endpoints change files and start jobs, so they only accept JSON
bodies (which a browser will not send cross-site without asking first) and
refuse requests whose Origin is another site.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import re
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..recording import list_sessions

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
TILE_RE = re.compile(r"^/tiles/(\d+)/(\d+)/(\d+)\.png$")


def mbtiles_tile(path: Path, z: int, x: int, y: int) -> bytes | None:
    """One tile from an MBTiles file (SQLite; rows are in TMS order, so y is flipped)."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = db.execute("SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                         (z, x, (1 << z) - 1 - y)).fetchone()
    finally:
        db.close()
    return bytes(row[0]) if row else None


def mbtiles_zooms(path: Path) -> list[int]:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return sorted(r[0] for r in db.execute("SELECT DISTINCT zoom_level FROM tiles"))
    finally:
        db.close()


def make_handler(ctl, store, data_dir: Path):
    tiles_dir = data_dir / "tiles"
    mbtiles = data_dir / "map.mbtiles"   # an offline map file you bring yourself, served before data/tiles
    captures_dir = data_dir / "captures"

    class Handler(BaseHTTPRequestHandler):
        server_version = "dronedet"

        def log_message(self, fmt, *args):  # quieter than the default
            log.debug("%s %s", self.address_string(), fmt % args)

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _file(self, path: Path, cache: bool = False):
            if not path.is_file():
                self.send_error(404)
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            if cache:
                self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path in ("/", "/index.html"):
                return self._file(STATIC / "index.html")
            if u.path.startswith("/static/"):
                name = u.path[len("/static/"):]
                if "/" in name or name.startswith("."):
                    return self.send_error(404)
                return self._file(STATIC / name)
            if u.path == "/api/state":
                since = int(q.get("since", ["0"])[0] or 0)
                return self._json(ctl.snapshot(since))
            if u.path == "/api/settings":
                return self._json(ctl.settings())
            if u.path == "/api/detections":
                hours = float(q.get("hours", ["1"])[0])
                rows = store.since(time.time() - hours * 3600) if store else []
                return self._json({"rows": rows})
            if u.path == "/api/detections.csv":
                hours = float(q.get("hours", ["0"])[0] or 0)
                since = time.time() - hours * 3600 if hours > 0 else 0
                body = (store.csv_since(since) if store else "").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition",
                                 'attachment; filename="detections-%s.csv"' % time.strftime("%Y%m%d-%H%M"))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if u.path == "/api/sessions":
                out = []
                rec = ctl.pipeline.recorder
                for m in list_sessions(captures_dir):
                    out.append({"session": m["session"], "created": m.get("created"), "label": m.get("label"),
                                "captures": len(m.get("captures", [])), "synthetic": m.get("synthetic", False),
                                "source": m.get("source"), "gain": m.get("gain"),
                                "recording": bool(rec and rec.dir.name == m["session"])})
                return self._json({"sessions": out[::-1], "dir": str(captures_dir)})
            if u.path == "/api/tiles":
                if mbtiles.is_file():
                    try:
                        return self._json({"zooms": mbtiles_zooms(mbtiles), "source": "mbtiles",
                                           "version": int(mbtiles.stat().st_mtime)})
                    except sqlite3.Error as e:
                        log.warning("cannot read %s: %s", mbtiles, e)
                zooms = sorted(int(p.name) for p in tiles_dir.glob("*") if p.name.isdigit()) if tiles_dir.exists() else []
                # version changes whenever the tiles are replaced, so the browser never shows old ones
                return self._json({"zooms": zooms, "source": "tiles",
                                   "version": int(tiles_dir.stat().st_mtime) if tiles_dir.exists() else 0})
            m = TILE_RE.match(u.path)
            if m:
                z, x, y = (int(v) for v in m.groups())
                if mbtiles.is_file():
                    try:
                        body = mbtiles_tile(mbtiles, z, x, y)
                    except sqlite3.Error:
                        body = None
                    if body is None:
                        return self.send_error(404)
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg" if body[:3] == b"\xff\xd8\xff" else "image/png")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "max-age=86400")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                return self._file(tiles_dir / m.group(1) / m.group(2) / f"{m.group(3)}.png", cache=True)
            self.send_error(404)

        def _same_origin(self) -> bool:
            origin = self.headers.get("Origin")
            if not origin:
                return True
            return urlparse(origin).netloc == self.headers.get("Host")

        def do_POST(self):
            u = urlparse(self.path)
            if not self.headers.get("Content-Type", "").startswith("application/json") or not self._same_origin():
                return self._json({"error": "JSON requests from the dashboard only"}, 403)
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad json"}, 400)
            fn = ROUTES.get(u.path)
            if fn is None:
                return self.send_error(404)
            try:
                return self._json(fn(body))
            except (ValueError, RuntimeError, KeyError, TypeError) as e:
                return self._json({"error": str(e)}, 400)

    def pause(b):
        ctl.pipeline.paused = bool(b.get("paused", not ctl.pipeline.paused))
        return {"paused": ctl.pipeline.paused}

    def record(b):
        label = {k: b.get(k) for k in ("class", "drone_model", "distance_m", "environment", "notes")}
        if label["class"] and not ctl.cfg.class_by_id(label["class"]):
            raise ValueError(f"unknown class {label['class']}")
        target = int(b["count"]) if b.get("count") else None
        return ctl.record(bool(b.get("on")), label, mode=b.get("mode") or "tracks", target=target)

    def delete_session(b):
        ctl.delete_session(b["session"])
        return {"ok": True}

    def shutdown(b):
        ctl.shutdown.set()
        return {"ok": True}

    ROUTES = {
        "/api/start": lambda b: ctl.start(b.get("source")),
        "/api/stop": lambda b: ctl.stop(),
        "/api/pause": pause,
        "/api/record": record,
        "/api/sessions/label": lambda b: {"label": ctl.label_session(b["session"], b.get("label") or {})},
        "/api/sessions/delete": delete_session,
        "/api/calibrate": lambda b: ctl.calibrate(),
        "/api/train": lambda b: ctl.train(epochs=b.get("epochs") or 20,
                                          include_synthetic=bool(b.get("include_synthetic"))),
        "/api/install-training": lambda b: ctl.install_training_tools(),
        "/api/synth": lambda b: ctl.make_synthetic(),
        "/api/tiles": lambda b: ctl.fetch_tiles(b.get("radius_km") or 1.0, b.get("url")),
        "/api/tiles/delete": lambda b: ctl.delete_tiles(),
        "/api/job/cancel": lambda b: {"job": ctl.cancel_job()},
        "/api/settings/gain": lambda b: ctl.set_gain(b["lna_db"], b["vga_db"], bool(b.get("amp"))),
        "/api/settings/receiver": lambda b: {"receiver": ctl.set_receiver(b["lat"], b["lon"], b.get("name", ""))},
        "/api/shutdown": shutdown,
    }

    return Handler


def serve(ctl, store, data_dir: Path, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    """Serve the dashboard for a control.Controller in a background thread."""
    httpd = ThreadingHTTPServer((host, port), make_handler(ctl, store, Path(data_dir)))
    threading.Thread(target=httpd.serve_forever, daemon=True, name="web").start()
    return httpd
