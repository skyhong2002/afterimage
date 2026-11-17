"""SQLite storage. One file, WAL mode, schema created on start."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS works (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  kind TEXT NOT NULL,
  links TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS images (
  id TEXT PRIMARY KEY,
  original TEXT NOT NULL,            -- file name inside originals/
  caption TEXT NOT NULL,
  device TEXT NOT NULL CHECK (device IN ('desktop', 'phone')),
  width INTEGER NOT NULL,
  height INTEGER NOT NULL,
  bytes INTEGER NOT NULL,
  palette TEXT NOT NULL DEFAULT '[]',
  dhash TEXT,                        -- 64-bit difference hash, hex; NULL for near-flat images
  emotion TEXT,
  reviewed INTEGER NOT NULL DEFAULT 0,
  personal INTEGER NOT NULL DEFAULT 0,
  confidence TEXT,                   -- AI suggestion confidence h/m/l, cleared by a human review
  note TEXT,
  source_url TEXT,
  source TEXT,
  source_ref TEXT,                   -- "<source>:<id>" of the external item it came from
  artist TEXT,
  origin TEXT NOT NULL DEFAULT 'imported',   -- 'collection' (migrated) or 'imported'
  added_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS images_by_source_ref ON images(source_ref);
CREATE TABLE IF NOT EXISTS image_works (
  image_id TEXT NOT NULL REFERENCES images(id) ON DELETE CASCADE,
  work_id TEXT NOT NULL REFERENCES works(id),
  position INTEGER NOT NULL DEFAULT 0,  -- the order the owner gave; the first work is the main one
  PRIMARY KEY (image_id, work_id)
);
CREATE INDEX IF NOT EXISTS image_works_by_work ON image_works(work_id);
CREATE TABLE IF NOT EXISTS similar (
  image_id TEXT NOT NULL REFERENCES images(id) ON DELETE CASCADE,
  rank INTEGER NOT NULL,
  other_id TEXT NOT NULL,
  PRIMARY KEY (image_id, rank)
);
CREATE TABLE IF NOT EXISTS candidates (
  id TEXT PRIMARY KEY,               -- "<source>:<source id>"
  source TEXT NOT NULL,
  source_url TEXT,
  thumb_url TEXT,
  preview_url TEXT,
  full_url TEXT NOT NULL,
  width INTEGER,
  height INTEGER,
  favorites INTEGER,
  artist TEXT,
  dhash TEXT,                        -- difference hash of the thumbnail, to re-check against the collection later
  work_query TEXT NOT NULL,
  work_kind TEXT NOT NULL,
  reason TEXT,
  nearest_id TEXT,
  distance INTEGER,
  status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'accepted', 'rejected')),
  found_at TEXT NOT NULL,
  decided_at TEXT
);
CREATE TABLE IF NOT EXISTS targets (
  key TEXT PRIMARY KEY,              -- "<kind>:<normalised title>"
  title TEXT NOT NULL,
  kind TEXT NOT NULL,
  last_at TEXT,                      -- most recent dated activity in infovore (ISO), if any
  rating REAL,                       -- best rating as 0..1, if any
  plays INTEGER NOT NULL DEFAULT 1,  -- how many log entries
  priority REAL NOT NULL DEFAULT 0,
  scanned_at TEXT,
  found INTEGER NOT NULL DEFAULT 0,
  note TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS edits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  image_id TEXT NOT NULL,
  at TEXT NOT NULL,
  actor TEXT NOT NULL,
  before TEXT NOT NULL,
  after TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(title: str) -> str:
    """ASCII slug; titles with no ASCII letters get a stable hash-based id."""
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if base:
        return base
    h = 0
    for ch in title:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return f"w-{h:x}"


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Bring a database made by an older version up to date (CREATE IF NOT EXISTS cannot add columns)."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(candidates)")}
        if "dhash" not in cols:
            self._conn.execute("ALTER TABLE candidates ADD COLUMN dhash TEXT")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """One writer at a time; commit on success, roll back on error."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def all(self, sql: str, args: tuple | list = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple | list = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, args).fetchone()

    def backup(self, dest: Path) -> None:
        with self._lock:
            target = sqlite3.connect(dest)
            try:
                self._conn.backup(target)
            finally:
                target.close()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- works ----------------------------------------------------------

    def unique_work_id(self, title: str) -> str:
        base = slugify(title)
        out, n = base, 2
        while self.one("SELECT 1 FROM works WHERE id=?", (out,)):
            out, n = f"{base}-{n}", n + 1
        return out

    def find_work_by_title(self, title: str) -> sqlite3.Row | None:
        key = re.sub(r"[^\w]+", " ", title.lower()).strip()
        for w in self.all("SELECT * FROM works"):
            if re.sub(r"[^\w]+", " ", w["title"].lower()).strip() == key:
                return w
        return None

    def add_work(self, title: str, kind: str, links: dict[str, Any] | None = None) -> str:
        with self.tx() as c:
            wid = self.unique_work_id(title)
            c.execute("INSERT INTO works(id, title, kind, links, created_at) VALUES (?,?,?,?,?)",
                      (wid, title.strip(), kind, json.dumps(links or {}, ensure_ascii=False), now()))
        return wid
