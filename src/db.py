from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class Subscription:
    id: int
    guild_id: int
    discord_channel_id: int
    telegram_channel: str
    keywords: list[str]
    enabled: bool
    last_message_id: int


@dataclass(frozen=True)
class XSubscription:
    id: int
    guild_id: int
    discord_channel_id: int
    username: str
    keywords: list[str]
    enabled: bool
    last_item_key: str


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_telegram_channel(value: str) -> str:
    channel = value.strip()
    if channel.startswith("https://t.me/s/"):
        channel = channel.removeprefix("https://t.me/s/")
    elif channel.startswith("https://t.me/"):
        channel = channel.removeprefix("https://t.me/")
    return channel.strip().strip("/").lstrip("@")


def normalize_x_username(value: str) -> str:
    username = value.strip()
    for prefix in ("https://x.com/", "https://twitter.com/", "x.com/", "twitter.com/"):
        if username.startswith(prefix):
            username = username.removeprefix(prefix)
            break
    return username.strip().strip("/").lstrip("@").split("/")[0]


def parse_keywords(value: str | None) -> list[str]:
    if not value:
        return []
    out: list[str] = []
    for item in value.replace("\n", ",").split(","):
        keyword = item.strip()
        if keyword and keyword not in out:
            out.append(keyword)
    return out


class BotDatabase:
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")

    def init(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                discord_channel_id INTEGER NOT NULL,
                telegram_channel TEXT NOT NULL,
                keywords_json TEXT NOT NULL DEFAULT '[]',
                enabled INTEGER NOT NULL DEFAULT 1,
                last_message_id INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(guild_id, telegram_channel, discord_channel_id)
            );

            CREATE TABLE IF NOT EXISTS delivered_messages (
                subscription_id INTEGER NOT NULL,
                telegram_message_id INTEGER NOT NULL,
                delivered_at TEXT NOT NULL,
                PRIMARY KEY(subscription_id, telegram_message_id),
                FOREIGN KEY(subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS x_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                discord_channel_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                keywords_json TEXT NOT NULL DEFAULT '[]',
                enabled INTEGER NOT NULL DEFAULT 1,
                last_item_key TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(guild_id, username, discord_channel_id)
            );

            CREATE TABLE IF NOT EXISTS delivered_x_items (
                subscription_id INTEGER NOT NULL,
                item_key TEXT NOT NULL,
                delivered_at TEXT NOT NULL,
                PRIMARY KEY(subscription_id, item_key),
                FOREIGN KEY(subscription_id) REFERENCES x_subscriptions(id) ON DELETE CASCADE
            );
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def add_subscription(
        self,
        *,
        guild_id: int,
        discord_channel_id: int,
        telegram_channel: str,
        keywords: Iterable[str],
    ) -> int:
        now = utc_now_iso()
        channel = normalize_telegram_channel(telegram_channel)
        cur = self.conn.execute(
            """
            INSERT INTO subscriptions (
                guild_id, discord_channel_id, telegram_channel, keywords_json,
                enabled, last_message_id, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, 1, 0, ?, ?)
            ON CONFLICT(guild_id, telegram_channel, discord_channel_id)
            DO UPDATE SET
                keywords_json=excluded.keywords_json,
                enabled=1,
                updated_at=excluded.updated_at
            RETURNING id
            """,
            (
                guild_id,
                discord_channel_id,
                channel,
                json.dumps(list(keywords), ensure_ascii=False),
                now,
                now,
            ),
        )
        row = cur.fetchone()
        self.conn.commit()
        return int(row["id"])

    def remove_subscription(self, *, guild_id: int, subscription_id: int) -> bool:
        cur = self.conn.execute(
            "DELETE FROM subscriptions WHERE guild_id = ? AND id = ?",
            (guild_id, subscription_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def set_keywords(self, *, guild_id: int, subscription_id: int, keywords: Iterable[str]) -> bool:
        cur = self.conn.execute(
            """
            UPDATE subscriptions
            SET keywords_json = ?, updated_at = ?
            WHERE guild_id = ? AND id = ?
            """,
            (
                json.dumps(list(keywords), ensure_ascii=False),
                utc_now_iso(),
                guild_id,
                subscription_id,
            ),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def set_last_message_id(self, subscription_id: int, message_id: int) -> None:
        self.conn.execute(
            """
            UPDATE subscriptions
            SET last_message_id = MAX(last_message_id, ?), updated_at = ?
            WHERE id = ?
            """,
            (message_id, utc_now_iso(), subscription_id),
        )
        self.conn.commit()

    def mark_delivered(self, subscription_id: int, message_id: int) -> bool:
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO delivered_messages (
                subscription_id, telegram_message_id, delivered_at
            )
            VALUES (?, ?, ?)
            """,
            (subscription_id, message_id, utc_now_iso()),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def get_subscription(self, *, guild_id: int, subscription_id: int) -> Subscription | None:
        row = self.conn.execute(
            "SELECT * FROM subscriptions WHERE guild_id = ? AND id = ?",
            (guild_id, subscription_id),
        ).fetchone()
        return self._subscription_from_row(row) if row else None

    def list_subscriptions(self, *, guild_id: int | None = None, enabled_only: bool = False) -> list[Subscription]:
        sql = "SELECT * FROM subscriptions"
        conditions: list[str] = []
        params: list[int] = []
        if guild_id is not None:
            conditions.append("guild_id = ?")
            params.append(guild_id)
        if enabled_only:
            conditions.append("enabled = 1")
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY id ASC"
        rows = self.conn.execute(sql, params).fetchall()
        return [self._subscription_from_row(row) for row in rows]

    def add_x_subscription(
        self,
        *,
        guild_id: int,
        discord_channel_id: int,
        username: str,
        keywords: Iterable[str],
    ) -> int:
        now = utc_now_iso()
        normalized = normalize_x_username(username)
        cur = self.conn.execute(
            """
            INSERT INTO x_subscriptions (
                guild_id, discord_channel_id, username, keywords_json,
                enabled, last_item_key, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, 1, '', ?, ?)
            ON CONFLICT(guild_id, username, discord_channel_id)
            DO UPDATE SET
                keywords_json=excluded.keywords_json,
                enabled=1,
                updated_at=excluded.updated_at
            RETURNING id
            """,
            (
                guild_id,
                discord_channel_id,
                normalized,
                json.dumps(list(keywords), ensure_ascii=False),
                now,
                now,
            ),
        )
        row = cur.fetchone()
        self.conn.commit()
        return int(row["id"])

    def remove_x_subscription(self, *, guild_id: int, subscription_id: int) -> bool:
        cur = self.conn.execute(
            "DELETE FROM x_subscriptions WHERE guild_id = ? AND id = ?",
            (guild_id, subscription_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def set_x_last_item_key(self, subscription_id: int, item_key: str) -> None:
        self.conn.execute(
            """
            UPDATE x_subscriptions
            SET last_item_key = ?, updated_at = ?
            WHERE id = ?
            """,
            (item_key, utc_now_iso(), subscription_id),
        )
        self.conn.commit()

    def mark_x_delivered(self, subscription_id: int, item_key: str) -> bool:
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO delivered_x_items (
                subscription_id, item_key, delivered_at
            )
            VALUES (?, ?, ?)
            """,
            (subscription_id, item_key, utc_now_iso()),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def get_x_subscription(self, *, guild_id: int, subscription_id: int) -> XSubscription | None:
        row = self.conn.execute(
            "SELECT * FROM x_subscriptions WHERE guild_id = ? AND id = ?",
            (guild_id, subscription_id),
        ).fetchone()
        return self._x_subscription_from_row(row) if row else None

    def list_x_subscriptions(
        self, *, guild_id: int | None = None, enabled_only: bool = False
    ) -> list[XSubscription]:
        sql = "SELECT * FROM x_subscriptions"
        conditions: list[str] = []
        params: list[int] = []
        if guild_id is not None:
            conditions.append("guild_id = ?")
            params.append(guild_id)
        if enabled_only:
            conditions.append("enabled = 1")
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY id ASC"
        rows = self.conn.execute(sql, params).fetchall()
        return [self._x_subscription_from_row(row) for row in rows]

    @staticmethod
    def _subscription_from_row(row: sqlite3.Row) -> Subscription:
        try:
            keywords = json.loads(row["keywords_json"] or "[]")
        except json.JSONDecodeError:
            keywords = []
        return Subscription(
            id=int(row["id"]),
            guild_id=int(row["guild_id"]),
            discord_channel_id=int(row["discord_channel_id"]),
            telegram_channel=str(row["telegram_channel"]),
            keywords=[str(item) for item in keywords],
            enabled=bool(row["enabled"]),
            last_message_id=int(row["last_message_id"]),
        )

    @staticmethod
    def _x_subscription_from_row(row: sqlite3.Row) -> XSubscription:
        try:
            keywords = json.loads(row["keywords_json"] or "[]")
        except json.JSONDecodeError:
            keywords = []
        return XSubscription(
            id=int(row["id"]),
            guild_id=int(row["guild_id"]),
            discord_channel_id=int(row["discord_channel_id"]),
            username=str(row["username"]),
            keywords=[str(item) for item in keywords],
            enabled=bool(row["enabled"]),
            last_item_key=str(row["last_item_key"] or ""),
        )
