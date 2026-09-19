"""Small async SQLite data layer.

SQLite is deliberately the default: it is easy to deploy and is sufficient
for a single bot worker. The tables are portable to Postgres/Mongo later.
All mutations are guarded by one asyncio lock to avoid quota races.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import time
from datetime import date
from pathlib import Path
from typing import Any

import aiosqlite


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT NOT NULL DEFAULT '',
    first_name TEXT NOT NULL DEFAULT '',
    plan TEXT NOT NULL DEFAULT 'free',
    expires_at INTEGER NOT NULL DEFAULT 0,
    expiry_notice_at INTEGER NOT NULL DEFAULT 0,
    referral_pass_until INTEGER NOT NULL DEFAULT 0,
    referred_by INTEGER,
    referral_verified INTEGER NOT NULL DEFAULT 0,
    downloads_today INTEGER NOT NULL DEFAULT 0,
    uploads_today INTEGER NOT NULL DEFAULT 0,
    usage_date TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    blocked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_users_referred_by ON users(referred_by);
CREATE TABLE IF NOT EXISTS payments (
    payment_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    plan_key TEXT NOT NULL,
    amount INTEGER NOT NULL,
    utr TEXT NOT NULL DEFAULT '',
    proof_file_id TEXT NOT NULL DEFAULT '',
    proof_type TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL DEFAULT 0,
    reviewed_at INTEGER,
    reviewed_by INTEGER
);
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
CREATE TABLE IF NOT EXISTS files (
    file_id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    telegram_file_id TEXT NOT NULL,
    media_type TEXT NOT NULL,
    file_name TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
"""


def now() -> int:
    return int(time.time())


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        self.lock = asyncio.Lock()

    async def connect(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        # Lightweight migrations for databases created by an earlier version.
        for table, column, definition in (
            ("users", "expiry_notice_at", "INTEGER NOT NULL DEFAULT 0"),
            ("payments", "expires_at", "INTEGER NOT NULL DEFAULT 0"),
            ("payments", "proof_type", "TEXT NOT NULL DEFAULT ''"),
        ):
            cursor = await self.conn.execute(f"PRAGMA table_info({table})")
            columns = {row[1] for row in await cursor.fetchall()}
            await cursor.close()
            if column not in columns:
                await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    def _db(self) -> aiosqlite.Connection:
        if not self.conn:
            raise RuntimeError("Database is not connected")
        return self.conn

    async def upsert_user(self, user_id: int, username: str, first_name: str, referred_by: int | None = None) -> dict[str, Any]:
        timestamp = now()
        async with self.lock:
            db = self._db()
            existing = await self.get_user(user_id)
            if existing:
                await db.execute(
                    "UPDATE users SET username=?, first_name=?, updated_at=?, blocked=0 WHERE user_id=?",
                    (username, first_name, timestamp, user_id),
                )
            else:
                await db.execute(
                    """INSERT INTO users
                    (user_id, username, first_name, referred_by, created_at, updated_at, usage_date)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (user_id, username, first_name, referred_by, timestamp, timestamp, date.today().isoformat()),
                )
            await db.commit()
        return (await self.get_user(user_id)) or {}

    async def get_user(self, user_id: int) -> dict[str, Any] | None:
        cursor = await self._db().execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row else None

    async def mark_referral_verified(self, user_id: int) -> int | None:
        async with self.lock:
            db = self._db()
            user = await self.get_user(user_id)
            if not user or user["referral_verified"] or not user["referred_by"]:
                return None
            await db.execute("UPDATE users SET referral_verified=1, updated_at=? WHERE user_id=?", (now(), user_id))
            cursor = await db.execute(
                "SELECT COUNT(*) AS count FROM users WHERE referred_by=? AND referral_verified=1",
                (user["referred_by"],),
            )
            row = await cursor.fetchone()
            await cursor.close()
            count = int(row["count"])
            if count and count % 3 == 0:
                current = await self.get_user(user["referred_by"])
                pass_until = max(now(), int(current["referral_pass_until"] if current else 0)) + 86_400
                await db.execute(
                    "UPDATE users SET referral_pass_until=?, updated_at=? WHERE user_id=?",
                    (pass_until, now(), user["referred_by"]),
                )
            await db.commit()
            return user["referred_by"]

    async def effective_plan(self, user_id: int) -> str:
        user = await self.get_user(user_id)
        if not user:
            return "free"
        timestamp = now()
        if int(user["expires_at"]) > timestamp:
            return str(user["plan"])
        if int(user["referral_pass_until"]) > timestamp:
            return "referral_pass"
        return "free"

    async def consume_quota(self, user_id: int, kind: str, limit: int | None) -> tuple[bool, int | None]:
        """Atomically consume a daily quota. Returns (allowed, remaining)."""
        if kind not in {"downloads", "uploads"}:
            raise ValueError("kind must be downloads or uploads")
        field = f"{kind}_today"
        async with self.lock:
            db = self._db()
            user = await self.get_user(user_id)
            if not user:
                return False, 0
            today = date.today().isoformat()
            count = int(user[field]) if user["usage_date"] == today else 0
            if limit is not None and count >= limit:
                return False, 0
            new_count = count + 1
            await db.execute(
                f"UPDATE users SET {field}=?, usage_date=?, updated_at=? WHERE user_id=?",
                (new_count, today, now(), user_id),
            )
            await db.commit()
            return True, None if limit is None else max(0, limit - new_count)

    async def create_payment(self, user_id: int, plan_key: str, amount: int, ttl_seconds: int = 600) -> str:
        payment_id = secrets.token_urlsafe(9)
        await self._db().execute(
            "INSERT INTO payments(payment_id,user_id,plan_key,amount,created_at,expires_at) VALUES (?,?,?,?,?,?)",
            (payment_id, user_id, plan_key, amount, now(), now() + ttl_seconds),
        )
        await self._db().commit()
        return payment_id

    async def attach_payment_submission(
        self,
        payment_id: str,
        user_id: int,
        utr: str = "",
        proof_file_id: str = "",
        proof_type: str = "",
    ) -> bool:
        cursor = await self._db().execute(
            "UPDATE payments SET utr=?, proof_file_id=?, proof_type=? "
            "WHERE payment_id=? AND user_id=? AND status='pending' AND expires_at>?",
            (utr[:120], proof_file_id, proof_type[:20], payment_id, user_id, now()),
        )
        await self._db().commit()
        return cursor.rowcount == 1

    async def pending_payments(self, limit: int = 20) -> list[dict[str, Any]]:
        cursor = await self._db().execute(
            "SELECT * FROM payments "
            "WHERE status='pending' AND (expires_at=0 OR expires_at>?) "
            "ORDER BY created_at ASC LIMIT ?",
            (now(), limit),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        await cursor.close()
        return rows

    async def review_payment(self, payment_id: str, reviewer_id: int, approved: bool) -> dict[str, Any] | None:
        async with self.lock:
            db = self._db()
            cursor = await db.execute(
                "SELECT * FROM payments WHERE payment_id=? AND status='pending' AND (expires_at=0 OR expires_at>?)",
                (payment_id, now()),
            )
            payment = await cursor.fetchone()
            await cursor.close()
            if not payment:
                return None
            payment = dict(payment)
            status = "approved" if approved else "rejected"
            await db.execute(
                "UPDATE payments SET status=?, reviewed_at=?, reviewed_by=? WHERE payment_id=?",
                (status, now(), reviewer_id, payment_id),
            )
            if approved:
                current = await self.get_user(payment["user_id"])
                from .SHIV_config import PLANS
                plan = PLANS[payment["plan_key"]]
                start = max(now(), int(current["expires_at"]) if current else 0)
                await db.execute(
                    "UPDATE users SET plan=?, expires_at=?, updated_at=? WHERE user_id=?",
                    (("beta" if plan.key.startswith("beta") else "pro"), start + plan.days * 86_400, now(), payment["user_id"]),
                )
            await db.commit()
            return payment

    async def grant(self, user_id: int, plan: str, days: int) -> bool:
        async with self.lock:
            user = await self.get_user(user_id)
            if not user:
                return False
            start = max(now(), int(user["expires_at"]))
            await self._db().execute(
                "UPDATE users SET plan=?, expires_at=?, updated_at=? WHERE user_id=?",
                (plan, start + days * 86_400, now(), user_id),
            )
            await self._db().commit()
            return True

    async def revoke(self, user_id: int) -> bool:
        cursor = await self._db().execute(
            "UPDATE users SET plan='free', expires_at=0, referral_pass_until=0, updated_at=? WHERE user_id=?",
            (now(), user_id),
        )
        await self._db().commit()
        return cursor.rowcount == 1

    async def refresh_expired_premium(self) -> list[dict[str, Any]]:
        """Reset expired paid plans and return the affected users."""
        async with self.lock:
            timestamp = now()
            cursor = await self._db().execute(
                "SELECT user_id, plan, expires_at FROM users "
                "WHERE plan IN ('pro', 'beta') AND expires_at>0 AND expires_at<=?",
                (timestamp,),
            )
            rows = [dict(row) for row in await cursor.fetchall()]
            await cursor.close()
            if rows:
                await self._db().execute(
                    "UPDATE users SET plan='free', expires_at=0, updated_at=? "
                    "WHERE plan IN ('pro', 'beta') AND expires_at>0 AND expires_at<=?",
                    (timestamp, timestamp),
                )
                await self._db().commit()
            return rows

    async def save_file(self, owner_id: int, telegram_file_id: str, media_type: str, file_name: str) -> int:
        cursor = await self._db().execute(
            "INSERT INTO files(owner_id,telegram_file_id,media_type,file_name,created_at) VALUES(?,?,?,?,?)",
            (owner_id, telegram_file_id, media_type, file_name[:200], now()),
        )
        await self._db().commit()
        return int(cursor.lastrowid)

    async def get_file(self, file_row_id: int, owner_id: int) -> dict[str, Any] | None:
        cursor = await self._db().execute(
            "SELECT * FROM files WHERE file_id=? AND owner_id=?", (file_row_id, owner_id)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row else None

    async def all_user_ids(self) -> list[int]:
        cursor = await self._db().execute("SELECT user_id FROM users WHERE blocked=0 ORDER BY user_id")
        rows = [int(row["user_id"]) for row in await cursor.fetchall()]
        await cursor.close()
        return rows

    async def expiring_users(self) -> list[dict[str, Any]]:
        timestamp = now()
        cursor = await self._db().execute(
            """SELECT * FROM users
               WHERE expires_at>? AND expires_at<=? AND expiry_notice_at<expires_at
               ORDER BY expires_at ASC""",
            (timestamp, timestamp + 86_400),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        await cursor.close()
        return rows

    async def mark_expiry_notice(self, user_id: int, expires_at: int) -> None:
        await self._db().execute(
            "UPDATE users SET expiry_notice_at=?, updated_at=? WHERE user_id=?",
            (now(), now(), user_id),
        )
        await self._db().commit()
