"""Хранилище: просмотренные проекты, изменяемые настройки, счётчики."""

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

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stats (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);
"""


class Storage:
    def __init__(self, path: Path) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(seen_projects)")}
        if "notified" not in cols:
            self._conn.execute(
                "ALTER TABLE seen_projects ADD COLUMN notified INTEGER NOT NULL DEFAULT 0"
            )

    # ------------------------------------------------------------------ #
    # Проекты
    # ------------------------------------------------------------------ #
    def is_seen(self, project_id: int) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM seen_projects WHERE id = ? LIMIT 1", (project_id,)
        )
        return cur.fetchone() is not None

    def mark_seen(self, project: dict[str, Any], notified: bool = False) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO seen_projects
                (id, title, price, offers, payload, notified_at, notified)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project["id"],
                project.get("title"),
                project.get("price"),
                project.get("offers"),
                json.dumps(project, ensure_ascii=False),
                int(time.time()),
                int(notified),
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
        return int(self._conn.execute("SELECT COUNT(*) c FROM seen_projects").fetchone()["c"])

    def project_stats(self) -> dict[str, Any]:
        day = int(time.time()) - 86400
        week = int(time.time()) - 7 * 86400
        row = self._conn.execute(
            """
            SELECT
                COUNT(*)                                        AS total,
                COALESCE(SUM(notified), 0)                      AS notified,
                COALESCE(SUM(notified_at >= ?), 0)              AS seen_day,
                COALESCE(SUM(notified AND notified_at >= ?), 0) AS notified_day,
                COALESCE(SUM(notified AND notified_at >= ?), 0) AS notified_week,
                COALESCE(AVG(CASE WHEN notified THEN price END), 0) AS avg_price
            FROM seen_projects
            """,
            (day, day, week),
        ).fetchone()
        return dict(row)

    def purge_older_than(self, days: int = 30) -> int:
        threshold = int(time.time()) - days * 86400
        cur = self._conn.execute(
            "DELETE FROM seen_projects WHERE notified_at < ?", (threshold,)
        )
        self._conn.commit()
        return cur.rowcount

    def reset_projects(self) -> int:
        cur = self._conn.execute("DELETE FROM seen_projects")
        self._conn.commit()
        return cur.rowcount

    # ------------------------------------------------------------------ #
    # Настройки поверх .env
    # ------------------------------------------------------------------ #
    def get_settings(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self._conn.execute("SELECT key, value FROM settings")}

    def set_setting(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value)
        )
        self._conn.commit()

    def del_setting(self, key: str) -> None:
        self._conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # Счётчики
    # ------------------------------------------------------------------ #
    def incr(self, key: str, amount: int = 1) -> None:
        self._conn.execute(
            """
            INSERT INTO stats (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = value + excluded.value
            """,
            (key, amount),
        )
        self._conn.commit()

    def counters(self) -> dict[str, int]:
        return {r["key"]: r["value"] for r in self._conn.execute("SELECT key, value FROM stats")}

    def reset_counters(self) -> None:
        self._conn.execute("DELETE FROM stats")
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
