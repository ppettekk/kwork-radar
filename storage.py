"""Хранилище просмотренных проектов (SQLite)."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_projects (
    id          INTEGER PRIMARY KEY,
    title       TEXT,
    price       INTEGER,
    offers      INTEGER,
    payload     TEXT,
    notified_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_seen_notified_at ON seen_projects (notified_at);
"""


class Storage:
    def __init__(self, path: Path) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def is_seen(self, project_id: int) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM seen_projects WHERE id = ? LIMIT 1", (project_id,)
        )
        return cur.fetchone() is not None

    def mark_seen(self, project: dict[str, Any]) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO seen_projects (id, title, price, offers, payload, notified_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                project["id"],
                project.get("title"),
                project.get("price"),
                project.get("offers"),
                json.dumps(project, ensure_ascii=False),
                int(time.time()),
            ),
        )
        self._conn.commit()

    def get_payload(self, project_id: int) -> dict[str, Any] | None:
        cur = self._conn.execute(
            "SELECT payload FROM seen_projects WHERE id = ?", (project_id,)
        )
        row = cur.fetchone()
        return json.loads(row["payload"]) if row else None

    def count(self) -> int:
        cur = self._conn.execute("SELECT COUNT(*) AS c FROM seen_projects")
        return int(cur.fetchone()["c"])

    def purge_older_than(self, days: int = 30) -> int:
        threshold = int(time.time()) - days * 86400
        cur = self._conn.execute(
            "DELETE FROM seen_projects WHERE notified_at < ?", (threshold,)
        )
        self._conn.commit()
        return cur.rowcount

    def close(self) -> None:
        self._conn.close()
