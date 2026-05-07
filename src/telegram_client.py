from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import aiohttp
from bs4 import BeautifulSoup
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.custom.message import Message

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TelegramAttachment:
    kind: str
    url: str
    title: str


@dataclass(frozen=True)
class TelegramPost:
    id: int
    channel: str
    text: str
    date: datetime | None
    url: str
    attachments: list[TelegramAttachment]
    source_label: str | None = None
    source_url: str | None = None
    avatar_url: str | None = None


def keyword_matches(text: str, keywords: Sequence[str]) -> bool:
    if not keywords:
        return True
    folded = text.casefold()
    return any(keyword.casefold() in folded for keyword in keywords)


class TelegramReader:
    def __init__(self, data_dir: Path):
        self._client = _build_telethon_client(data_dir)
        self._session: aiohttp.ClientSession | None = None
        self.mode = "telethon" if self._client else "public-web"

    async def start(self) -> None:
        if self._client:
            await self._client.start()
            log.info("Telegram reader mode: Telethon API")
            return
        self._session = aiohttp.ClientSession(
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
                )
            }
        )
        log.warning("Telegram reader mode: public t.me/s scraper. Private channels and older history are unavailable.")

    async def disconnect(self) -> None:
        if self._client:
            await self._client.disconnect()
        if self._session:
            await self._session.close()

    async def is_user_authorized(self) -> bool:
        if not self._client:
            return True
        return bool(await self._client.is_user_authorized())

    async def fetch_new_posts(self, *, channel: str, min_id: int, limit: int) -> list[TelegramPost]:
        if self._client:
            return await _fetch_new_posts_telethon(self._client, channel=channel, min_id=min_id, limit=limit)
        posts = await self._fetch_public_posts(channel=channel, limit=limit)
        return [post for post in posts if post.id > min_id]

    async def fetch_recent_posts(self, *, channel: str, limit: int) -> list[TelegramPost]:
        if self._client:
            return await _fetch_recent_posts_telethon(self._client, channel=channel, limit=limit)
        return await self._fetch_public_posts(channel=channel, limit=limit)

    async def fetch_posts_since(self, *, channel: str, since: datetime, limit: int = 100) -> list[TelegramPost]:
        if self._client:
            return await _fetch_posts_since_telethon(self._client, channel=channel, since=since, limit=limit)
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        posts = await self._fetch_public_posts(channel=channel, limit=limit)
        return [post for post in posts if post.date is None or post.date >= since]

    async def _fetch_public_posts(self, *, channel: str, limit: int) -> list[TelegramPost]:
        if self._session is None:
            raise RuntimeError("TelegramReader.start() was not called.")
        url = f"https://t.me/s/{channel}"
        async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
            response.raise_for_status()
            html = await response.text()
        posts = parse_public_channel_html(channel, html)
        return posts[-limit:]


def build_telegram_reader(data_dir: Path) -> TelegramReader:
    return TelegramReader(data_dir)


def _build_telethon_client(data_dir: Path) -> TelegramClient | None:
    api_id = (os.getenv("TELEGRAM_API_ID") or "").strip()
    api_hash = (os.getenv("TELEGRAM_API_HASH") or "").strip()
    if not api_id or not api_hash:
        return None

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
        attachments=[],
        source_label=f"@{channel}",
        source_url=f"https://t.me/s/{channel}",
    )


def _absolute_url(url: str) -> str:
    url = url.strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return "https://t.me" + url
    return url


def _style_url(style: str | None) -> str | None:
    if not style:
        return None
    match = re.search(r"url\(['\"]?([^'\")]+)['\"]?\)", style)
    if not match:
        return None
    return _absolute_url(match.group(1))


def _node_text(node: object, *, fallback: str) -> str:
    try:
        text = node.get_text(" ", strip=True)  # type: ignore[attr-defined]
    except AttributeError:
        return fallback
    return text or fallback


def _public_attachments(node: object) -> list[TelegramAttachment]:
    attachments: list[TelegramAttachment] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, url: str | None, title: str) -> None:
        if not url:
            return
        normalized = _absolute_url(url)
        key = (kind, normalized)
        if key in seen:
            return
        seen.add(key)
        attachments.append(TelegramAttachment(kind=kind, url=normalized, title=title[:120]))

    for photo in node.select(".tgme_widget_message_photo_wrap"):  # type: ignore[attr-defined]
        add("image", _style_url(photo.get("style")), "이미지")

    for preview in node.select(".link_preview_image, .tgme_widget_message_link_preview_right_image"):  # type: ignore[attr-defined]
        add("preview", _style_url(preview.get("style")), "링크 미리보기")

    for video in node.select(".tgme_widget_message_video_thumb, .tgme_widget_message_video_player"):  # type: ignore[attr-defined]
        add("video", _style_url(video.get("style")), "비디오")

    for document in node.select(".tgme_widget_message_document_wrap"):  # type: ignore[attr-defined]
        title_node = document.select_one(".tgme_widget_message_document_title")
        title = _node_text(title_node, fallback="첨부파일") if title_node else _node_text(document, fallback="첨부파일")
        add("file", document.get("href"), title)

    for audio in node.select(".tgme_widget_message_voice, .tgme_widget_message_audio"):  # type: ignore[attr-defined]
        add("audio", None, _node_text(audio, fallback="오디오"))

    return attachments


def _public_channel_avatar(soup: BeautifulSoup) -> str | None:
    for selector in (
        ".tgme_channel_info_header .tgme_page_photo_image img",
        ".tgme_page_photo_image img",
        ".tgme_channel_info_header img",
    ):
        node = soup.select_one(selector)
        if node and node.get("src"):
            return _absolute_url(str(node.get("src")))
    return None


def parse_public_channel_html(channel: str, html: str) -> list[TelegramPost]:
    soup = BeautifulSoup(html, "html.parser")
    avatar_url = _public_channel_avatar(soup)
    posts: list[TelegramPost] = []
    for node in soup.select(".tgme_widget_message"):
        data_post = node.get("data-post") or ""
        match = re.search(r"/(\d+)$", data_post)
        if not match:
            continue
        message_id = int(match.group(1))

        attachments = _public_attachments(node)
        text_node = node.select_one(".tgme_widget_message_text")
        text = text_node.get_text("\n", strip=True) if text_node else ""
        if not text and not attachments:
            continue

        dt: datetime | None = None
        time_node = node.select_one("time")
        raw_dt = time_node.get("datetime") if time_node else None
        if raw_dt:
            try:
                dt = datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
            except ValueError:
                dt = None

        posts.append(
            TelegramPost(
                id=message_id,
                channel=channel,
                text=text or "(미디어/첨부만 있는 게시물)",
                date=dt,
                url=message_url(channel, message_id),
                attachments=attachments,
                source_label=f"@{channel}",
                source_url=f"https://t.me/s/{channel}",
                avatar_url=avatar_url,
            )
        )
    posts.sort(key=lambda post: post.id)
    return posts


async def _fetch_new_posts_telethon(
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


async def _fetch_recent_posts_telethon(
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


async def _fetch_posts_since_telethon(
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
