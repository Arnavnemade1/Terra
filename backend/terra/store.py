"""Read models and artifacts. Temporal remains the workflow state authority."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, database: Path):
        self.database = database
        database.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS observations (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS investigations (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def save_observations(self, observations: list[dict]) -> None:
        now = utc_now()
        with self.connect() as db:
            db.executemany(
                "INSERT INTO observations VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, fetched_at=excluded.fetched_at",
                [(str(item["id"]), json.dumps(item, allow_nan=False), now) for item in observations],
            )

    def observation(self, identifier: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM observations WHERE id=?", (identifier,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def save_investigation(self, item: dict) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO investigations VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
                (item["id"], json.dumps(item, allow_nan=False), item["created_at"], item["updated_at"]),
            )

    def investigation(self, identifier: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM investigations WHERE id=?", (identifier,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def investigations(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM investigations ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(row["payload"]) for row in rows]
