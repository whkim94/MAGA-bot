from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.custom.message import Message

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TelegramPost:
    id: int
    channel: str
    text: str
    date: datetime | None
    url: str


def keyword_matches(text: str, keywords: Sequence[str]) -> bool:
    if not keywords:
        return True
    folded = text.casefold()
    return any(keyword.casefold() in folded for keyword in keywords)


def build_telegram_client(data_dir: Path) -> TelegramClient:
    api_id = (os.getenv("TELEGRAM_API_ID") or "").strip()
    api_hash = (os.getenv("TELEGRAM_API_HASH") or "").strip()
    if not api_id or not api_hash:
        raise RuntimeError("TELEGRAM_API_ID and TELEGRAM_API_HASH are required.")

    session = (os.getenv("TELEGRAM_SESSION") or "").strip()
    if session:
        return TelegramClient(StringSession(session), int(api_id), api_hash)

    session_path = data_dir / "telegram"
    log.warning(
        "TELEGRAM_SESSION is not set. Falling back to file session at %s. "
        "For Railway, creating TELEGRAM_SESSION locally is recommended.",
        session_path,
    )
    return TelegramClient(str(session_path), int(api_id), api_hash)


def message_url(channel: str, message_id: int) -> str:
    return f"https://t.me/{channel}/{message_id}"


def to_post(channel: str, message: Message) -> TelegramPost:
    text = (message.message or "").strip()
    return TelegramPost(
        id=int(message.id),
        channel=channel,
        text=text,
        date=message.date,
        url=message_url(channel, int(message.id)),
    )


async def fetch_new_posts(
    client: TelegramClient,
    *,
    channel: str,
    min_id: int,
    limit: int,
) -> list[TelegramPost]:
    posts: list[TelegramPost] = []
    async for message in client.iter_messages(channel, min_id=min_id, limit=limit, reverse=True):
        if not getattr(message, "id", None):
            continue
        if not (message.message or "").strip():
            continue
        posts.append(to_post(channel, message))
    return posts


async def fetch_recent_posts(
    client: TelegramClient,
    *,
    channel: str,
    limit: int,
) -> list[TelegramPost]:
    raw = await client.get_messages(channel, limit=limit)
    posts = [
        to_post(channel, message)
        for message in raw
        if getattr(message, "id", None) and (message.message or "").strip()
    ]
    posts.sort(key=lambda post: post.id)
    return posts


async def fetch_posts_since(
    client: TelegramClient,
    *,
    channel: str,
    since: datetime,
    limit: int = 100,
) -> list[TelegramPost]:
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    posts: list[TelegramPost] = []
    async for message in client.iter_messages(channel, limit=limit):
        date = message.date
        if date is not None and date < since:
            break
        if not getattr(message, "id", None) or not (message.message or "").strip():
            continue
        posts.append(to_post(channel, message))
    posts.sort(key=lambda post: post.id)
    return posts
