from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS trends (
    id INTEGER PRIMARY KEY,
    keyword TEXT NOT NULL,
    score REAL NOT NULL,
    origin TEXT NOT NULL,
    sample_title TEXT,
    captured_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_trends_captured ON trends(captured_at);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    source_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    permission_ref TEXT,
    attribution TEXT,
    origin TEXT NOT NULL UNIQUE,      -- local path or URL
    file_path TEXT,
    title TEXT,
    duration REAL,
    transcript_path TEXT,
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,               -- clip | ai_original
    source_id INTEGER REFERENCES sources(id),
    start REAL,
    end REAL,
    topic TEXT,
    title TEXT NOT NULL,
    description TEXT,
    tags TEXT,                        -- json list
    hook TEXT,
    commentary TEXT,
    transcript TEXT,
    score REAL,
    synthetic INTEGER NOT NULL DEFAULT 0,
    file_path TEXT,
    status TEXT NOT NULL,             -- ready | needs_review | rejected | uploaded | failed
    publish_at TEXT,
    youtube_id TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    uploaded_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_clips_status ON clips(status);

CREATE TABLE IF NOT EXISTS stats (
    clip_id INTEGER NOT NULL REFERENCES clips(id),
    captured_at TEXT NOT NULL,
    views INTEGER, likes INTEGER, comments INTEGER
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self.conn:
            yield self.conn

    def q(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, *args: Any) -> sqlite3.Row | None:
        return self.conn.execute(sql, args).fetchone()

    def insert(self, table: str, row: dict[str, Any]) -> int:
        row = {k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in row.items()}
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with self.tx() as c:
            cur = c.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values()))
            return int(cur.lastrowid)

    def update(self, table: str, row_id: int, **fields: Any) -> None:
        fields = {k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in fields.items()}
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*fields.values(), row_id))
