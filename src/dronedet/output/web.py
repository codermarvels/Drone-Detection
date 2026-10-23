"""Local dashboard server. Standard library only, no internet needed.

Binds to 127.0.0.1 by default; set web.host in config/app.yaml to 0.0.0.0
to reach a Raspberry Pi from another machine on your network.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..recording import list_sessions

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
TILE_RE = re.compile(r"^/tiles/(\d+)/(\d+)/(\d+)\.png$")


def make_handler(pipeline, store, data_dir: Path):
    tiles_dir = data_dir / "tiles"
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
                return self._json(pipeline.snapshot(since))
            if u.path == "/api/detections":
                hours = float(q.get("hours", ["1"])[0])
                rows = store.since(time.time() - hours * 3600) if store else []
                return self._json({"rows": rows})
            if u.path == "/api/sessions":
                out = []
                for m in list_sessions(captures_dir):
                    out.append({"session": m["session"], "created": m.get("created"), "label": m.get("label"),
                                "captures": len(m.get("captures", [])), "synthetic": m.get("synthetic", False),
                                "source": m.get("source")})
                return self._json({"sessions": out[::-1], "dir": str(captures_dir)})
            if u.path == "/api/tiles":
                zooms = sorted(int(p.name) for p in tiles_dir.glob("*") if p.name.isdigit()) if tiles_dir.exists() else []
                return self._json({"zooms": zooms})
            m = TILE_RE.match(u.path)
            if m:
                return self._file(tiles_dir / m.group(1) / m.group(2) / f"{m.group(3)}.png", cache=True)
            self.send_error(404)

        def do_POST(self):
            u = urlparse(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad json"}, 400)
            if u.path == "/api/pause":
                pipeline.paused = bool(body.get("paused", not pipeline.paused))
                return self._json({"paused": pipeline.paused})
            if u.path == "/api/record":
                label = {k: body.get(k) for k in ("class", "drone_model", "distance_m", "environment", "notes")}
                return self._json(pipeline.set_recording(bool(body.get("on")), label))
            self.send_error(404)

    return Handler


def serve(pipeline, store, data_dir: Path, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(pipeline, store, Path(data_dir)))
    threading.Thread(target=httpd.serve_forever, daemon=True, name="web").start()
    return httpd
