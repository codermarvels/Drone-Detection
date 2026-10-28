"""SQLite log of every track, so detections survive restarts."""

from __future__ import annotations

import csv
import io
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    run TEXT NOT NULL,
    id INTEGER NOT NULL,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    center_hz REAL,
    width_hz REAL,
    peak_db REAL,
    hopping INTEGER,
    label TEXT,
    confidence REAL,
    drone INTEGER,
    captures INTEGER,
    summary TEXT,
    PRIMARY KEY (run, id)
);
CREATE INDEX IF NOT EXISTS tracks_last_seen ON tracks(last_seen);
"""


class Store:
    def __init__(self, path: Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()

    def upsert(self, rows: list[dict]) -> None:
        if not rows:
            return
        with self.lock:
            self.db.executemany(
                """INSERT INTO tracks VALUES (:run,:id,:first_seen,:last_seen,:center_hz,:width_hz,:peak_db,
                   :hopping,:label,:confidence,:drone,:captures,:summary)
                   ON CONFLICT(run,id) DO UPDATE SET last_seen=excluded.last_seen, center_hz=excluded.center_hz,
                   width_hz=excluded.width_hz, peak_db=excluded.peak_db, hopping=excluded.hopping,
                   label=excluded.label, confidence=excluded.confidence, drone=excluded.drone,
                   captures=excluded.captures, summary=excluded.summary""",
                rows,
            )
            self.db.commit()

    def since(self, t: float, limit: int | None = 500) -> list[dict]:
        with self.lock:
            cur = self.db.execute("SELECT * FROM tracks WHERE last_seen >= ? ORDER BY last_seen DESC LIMIT ?",
                                  (t, -1 if limit is None else limit))
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def csv_since(self, t: float) -> str:
        """Every track seen since t, oldest first, as CSV for a spreadsheet."""
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["first_seen", "last_seen", "duration_s", "band", "frequency_mhz", "width_mhz", "peak_db",
                    "hopping", "class", "confidence", "drone", "captures", "summary", "run", "track"])
        for r in reversed(self.since(t, limit=None)):
            f = r["center_hz"] or 0
            w.writerow([
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["first_seen"])),
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["last_seen"])),
                round(r["last_seen"] - r["first_seen"], 1),
                "5.8 GHz" if f > 4e9 else "2.4 GHz",
                round(f / 1e6, 3), round((r["width_hz"] or 0) / 1e6, 3),
                None if r["peak_db"] is None else round(r["peak_db"], 1),
                "yes" if r["hopping"] else "no", r["label"] or "",
                "" if r["confidence"] is None or not r["label"] else round(r["confidence"], 3),
                "yes" if r["drone"] else "no", r["captures"], r["summary"], r["run"], r["id"],
            ])
        return out.getvalue()

    def close(self) -> None:
        with self.lock:
            self.db.close()
