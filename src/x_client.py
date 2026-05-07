from __future__ import annotations

import calendar
import hashlib
import html
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

import aiohttp
import feedparser
from bs4 import BeautifulSoup

from .telegram_client import TelegramAttachment, TelegramPost

log = logging.getLogger(__name__)
BASELINE_PENDING = "__baseline_pending__"


@dataclass(frozen=True)
class XFeedItem:
    key: str
    post: TelegramPost


def normalize_x_username(value: str) -> str:
    username = value.strip()
    for prefix in ("https://x.com/", "https://twitter.com/", "x.com/", "twitter.com/"):
        if username.startswith(prefix):
            username = username.removeprefix(prefix)
            break
    return username.strip().strip("/").lstrip("@").split("/")[0]


def x_keyword_matches(text: str, keywords: Sequence[str]) -> bool:
    if not keywords:
        return True
    folded = text.casefold()
    return any(keyword.casefold() in folded for keyword in keywords)


def _feed_urls(username: str) -> list[str]:
    template = (os.getenv("X_FEED_URL_TEMPLATE") or "").strip()
    raw_base = (
        os.getenv("NITTER_BASE_URL")
        or os.getenv("RSSHUB_BASE_URL")
        or "https://nitter.net,https://xcancel.com,https://rss.xcancel.com"
    )
    bases = [base.strip().rstrip("/") for base in raw_base.split(",") if base.strip()]
    if template:
        return [template.format(username=username, base=base) for base in bases]
    return [f"{base}/{username}/rss" for base in bases]


def _entry_key(entry: object) -> str:
    for key in ("id", "guid", "link"):
        value = getattr(entry, key, None) or entry.get(key)  # type: ignore[attr-defined]
        if value:
            return str(value)
    raw = repr(entry).encode("utf-8", errors="ignore")
    return hashlib.sha256(raw).hexdigest()


def _stable_int(value: str) -> int:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return int(digest[:12], 16)


def _entry_datetime(entry: object) -> datetime | None:
    parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not parsed:
        return None
    return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)


def _plain_text(value: str) -> str:
    soup = BeautifulSoup(value or "", "html.parser")
    text = soup.get_text("\n", strip=True)
    return html.unescape(text).strip()


def _canonical_x_link(username: str, link: str) -> str:
    match = re.search(r"/status/(\d+)", link)
    if match:
        return f"https://x.com/{username}/status/{match.group(1)}"
    return link or f"https://x.com/{username}"


def _entry_attachments(entry: object) -> list[TelegramAttachment]:
    attachments: list[TelegramAttachment] = []
    seen: set[str] = set()

    def add(kind: str, url: str | None, title: str) -> None:
        if not url:
            return
        if url in seen:
            return
        seen.add(url)
        attachments.append(TelegramAttachment(kind=kind, url=url, title=title[:120]))

    for media in entry.get("media_content", []) or []:  # type: ignore[attr-defined]
        add("image", media.get("url"), "이미지")
    for media in entry.get("media_thumbnail", []) or []:  # type: ignore[attr-defined]
        add("image", media.get("url"), "이미지")
    for enclosure in entry.get("enclosures", []) or []:  # type: ignore[attr-defined]
        mime = str(enclosure.get("type") or "")
        kind = "image" if mime.startswith("image/") else "video" if mime.startswith("video/") else "file"
        add(kind, enclosure.get("href") or enclosure.get("url"), "첨부")

    summary = entry.get("summary", "") or ""  # type: ignore[attr-defined]
    for match in re.finditer(r'<img[^>]+src=["\']([^"\']+)["\']', summary):
        add("image", html.unescape(match.group(1)), "이미지")

    return attachments


def _entry_to_item(username: str, entry: object) -> XFeedItem:
    key = _entry_key(entry)
    title = _plain_text(entry.get("title", "") or "")  # type: ignore[attr-defined]
    summary = _plain_text(entry.get("summary", "") or "")  # type: ignore[attr-defined]
    text = summary or title or "(본문 없음)"
    link = _canonical_x_link(username, str(entry.get("link") or ""))  # type: ignore[attr-defined]
    post = TelegramPost(
        id=_stable_int(key),
        channel=f"x/{username}",
        text=text,
        date=_entry_datetime(entry),
        url=link,
        attachments=_entry_attachments(entry),
        source_label=f"X @{username}",
        source_url=f"https://x.com/{username}",
    )
    return XFeedItem(key=key, post=post)


class XReader:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
                )
            }
        )
        base = os.getenv("NITTER_BASE_URL") or os.getenv("RSSHUB_BASE_URL") or "https://nitter.net"
        log.info("X reader mode: RSS feed bridge (%s)", base)

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    async def fetch_recent(self, username: str, *, limit: int) -> list[XFeedItem]:
        if self._session is None:
            raise RuntimeError("XReader.start() was not called.")
        normalized = normalize_x_username(username)
        errors: list[str] = []
        raw: bytes | None = None
        used_url = ""
        for url in _feed_urls(normalized):
            try:
                async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                    response.raise_for_status()
                    raw = await response.read()
                    used_url = url
                    break
            except (aiohttp.ClientError, TimeoutError) as exc:
                errors.append(f"{url}: {type(exc).__name__}: {exc}")
                continue
        if raw is None:
            raise RuntimeError(f"All X feed bridges failed for @{normalized}: {'; '.join(errors)}")
        feed = feedparser.parse(raw)
        if getattr(feed, "bozo", False):
            log.warning(
                "X feed parse warning for @%s via %s: %s",
                normalized,
                used_url,
                getattr(feed, "bozo_exception", "unknown"),
            )
        items = [_entry_to_item(normalized, entry) for entry in feed.entries]
        items.reverse()
        return items[-limit:]

    async def fetch_new(self, username: str, *, last_item_key: str, limit: int) -> list[XFeedItem]:
        items = await self.fetch_recent(username, limit=limit)
        if last_item_key == BASELINE_PENDING:
            return []
        if not last_item_key:
            return items
        for idx, item in enumerate(items):
            if item.key == last_item_key:
                return items[idx + 1 :]
        return items
