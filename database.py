"""
SQLite deduplication layer — user ID primary key, source tracking,
timestamp per entry. Thread-safe via aiosqlite.
"""
import asyncio
import csv
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import aiosqlite

logger = logging.getLogger("database")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id       INTEGER PRIMARY KEY,
    username      TEXT,
    first_name    TEXT,
    last_name     TEXT,
    phone         TEXT,
    source        TEXT NOT NULL,
    scraped_at    TEXT NOT NULL,
    added_to_dest INTEGER DEFAULT 0,
    added_at      TEXT,
    add_error     TEXT
);

CREATE INDEX IF NOT EXISTS idx_source ON users(source);
CREATE INDEX IF NOT EXISTS idx_added ON users(added_to_dest);
"""


class Database:
    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db: Optional[aiosqlite.Connection] = None

    async def init(self):
        self.db = await aiosqlite.connect(str(self.db_path))
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript(SCHEMA)
        await self.db.commit()
        logger.info(f"Database initialized: {self.db_path}")

    async def upsert_user(
        self,
        user_id: int,
        username: str = "",
        first_name: str = "",
        last_name: str = "",
        phone: str = "",
        source: str = "",
    ) -> bool:
        """Insert or update user. Returns True if new, False if existing."""
        now = datetime.now(timezone.utc).isoformat()
        cursor = await self.db.execute(
            "SELECT user_id FROM users WHERE user_id = ?", (user_id,)
        )
        existing = await cursor.fetchone()
        if existing:
            await self.db.execute(
                """UPDATE users SET username=?, first_name=?, last_name=?,
                   phone=?, source=?, scraped_at=?
                   WHERE user_id=?""",
                (username, first_name, last_name, phone, source, now, user_id),
            )
            await self.db.commit()
            return False
        else:
            await self.db.execute(
                """INSERT INTO users (user_id, username, first_name, last_name,
                   phone, source, scraped_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (user_id, username, first_name, last_name, phone, source, now),
            )
            await self.db.commit()
            return True

    async def mark_added(self, user_id: int, error: str = ""):
        """Mark a user as added to destination."""
        now = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            "UPDATE users SET added_to_dest=1, added_at=?, add_error=? WHERE user_id=?",
            (now, error, user_id),
        )
        await self.db.commit()

    async def mark_add_error(self, user_id: int, error: str):
        """Mark add failure with error."""
        now = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            "UPDATE users SET add_error=? WHERE user_id=?",
            (error, user_id),
        )
        await self.db.commit()

    async def get_unadded_users(self, limit: int = 50) -> List[Dict]:
        """Get users not yet added to destination, oldest first."""
        cursor = await self.db.execute(
            "SELECT * FROM users WHERE added_to_dest=0 ORDER BY scraped_at ASC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def count_users(self) -> int:
        cursor = await self.db.execute("SELECT COUNT(*) as c FROM users")
        row = await cursor.fetchone()
        return row["c"]

    async def count_added(self) -> int:
        cursor = await self.db.execute("SELECT COUNT(*) as c FROM users WHERE added_to_dest=1")
        row = await cursor.fetchone()
        return row["c"]

    async def count_by_source(self) -> dict:
        cursor = await self.db.execute(
            "SELECT source, COUNT(*) as c FROM users GROUP BY source"
        )
        rows = await cursor.fetchall()
        return {r["source"]: r["c"] for r in rows}

    async def export_csv(self, path: str) -> int:
        """Export all users to CSV. Returns count."""
        cursor = await self.db.execute("SELECT * FROM users ORDER BY scraped_at DESC")
        rows = await cursor.fetchall()
        if not rows:
            return 0
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=dict(rows[0]).keys())
            writer.writeheader()
            for row in rows:
                writer.writerow(dict(row))
        logger.info(f"Exported {len(rows)} users to {path}")
        return len(rows)

    async def close(self):
        if self.db:
            await self.db.close()
