from __future__ import annotations

import calendar
import hashlib
import html
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

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


def _poll_mode() -> str:
    return (os.getenv("X_POLL_MODE") or "auto").strip().lower()


def _bearer_token() -> str:
    return (os.getenv("X_BEARER_TOKEN") or "").strip()


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


def _parse_x_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


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
        avatar_url=None,
    )
    return XFeedItem(key=key, post=post)


def _api_media_map(payload: dict[str, Any]) -> dict[str, list[TelegramAttachment]]:
    media_by_key = {
        str(media.get("media_key")): media
        for media in ((payload.get("includes") or {}).get("media") or [])
        if media.get("media_key")
    }
    out: dict[str, list[TelegramAttachment]] = {}
    for tweet in payload.get("data") or []:
        tweet_id = str(tweet.get("id") or "")
        keys = ((tweet.get("attachments") or {}).get("media_keys") or [])
        attachments: list[TelegramAttachment] = []
        for media_key in keys:
            media = media_by_key.get(str(media_key))
            if not media:
                continue
            media_type = str(media.get("type") or "")
            url = media.get("url") or media.get("preview_image_url")
            if not url:
                continue
            kind = "image" if media_type == "photo" else "video" if media_type in {"video", "animated_gif"} else "file"
            title = str(media.get("alt_text") or media_type or "미디어")
            attachments.append(TelegramAttachment(kind=kind, url=str(url), title=title[:120]))
        out[tweet_id] = attachments
    return out


def _api_user_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(user.get("id")): user
        for user in ((payload.get("includes") or {}).get("users") or [])
        if user.get("id")
    }


def _tweet_urls(tweet: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for item in ((tweet.get("entities") or {}).get("urls") or []):
        url = item.get("expanded_url") or item.get("unwound_url") or item.get("url")
        if url and "twitter.com/" not in str(url) and "x.com/" not in str(url):
            urls.append(str(url))
    return urls


def _tweet_to_item(
    username: str,
    tweet: dict[str, Any],
    attachments: list[TelegramAttachment],
    *,
    user: dict[str, Any] | None = None,
) -> XFeedItem:
    tweet_id = str(tweet.get("id") or "")
    text = str(tweet.get("text") or "").strip() or "(본문 없음)"
    post = TelegramPost(
        id=_stable_int(tweet_id),
        channel=f"x/{username}",
        text=text,
        date=_parse_x_datetime(tweet.get("created_at")),
        url=f"https://x.com/{username}/status/{tweet_id}",
        attachments=attachments,
        source_label=f"X @{username}",
        source_url=f"https://x.com/{username}",
        avatar_url=str((user or {}).get("profile_image_url") or "") or None,
    )
    return XFeedItem(key=tweet_id, post=post)


async def _fetch_og_image(session: aiohttp.ClientSession, url: str) -> TelegramAttachment | None:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=12), allow_redirects=True) as response:
            if response.status >= 400:
                return None
            content_type = response.headers.get("Content-Type", "")
            if "text/html" not in content_type:
                return None
            html_body = await response.text(errors="ignore")
    except (aiohttp.ClientError, TimeoutError, UnicodeDecodeError):
        return None

    soup = BeautifulSoup(html_body, "html.parser")
    image = None
    title = ""
    for selector in (
        ('meta[property="og:image"]', "content"),
        ('meta[name="twitter:image"]', "content"),
        ('meta[property="twitter:image"]', "content"),
    ):
        node = soup.select_one(selector[0])
        if node and node.get(selector[1]):
            image = str(node.get(selector[1]))
            break
    title_node = soup.select_one('meta[property="og:title"], meta[name="twitter:title"], title')
    if title_node:
        title = str(title_node.get("content") or title_node.get_text(" ", strip=True) or "링크 이미지")
    if not image:
        return None
    if image.startswith("//"):
        image = "https:" + image
    elif image.startswith("/"):
        from urllib.parse import urljoin

        image = urljoin(url, image)
    return TelegramAttachment(kind="preview", url=image, title=title or "링크 이미지")


class XReader:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._user_id_cache: dict[str, str] = {}

    async def start(self) -> None:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
            )
        }
        if _bearer_token():
            headers["Authorization"] = f"Bearer {_bearer_token()}"
        self._session = aiohttp.ClientSession(headers=headers)
        base = os.getenv("NITTER_BASE_URL") or os.getenv("RSSHUB_BASE_URL") or "https://nitter.net"
        if _bearer_token() and _poll_mode() != "rss":
            log.info("X reader mode: X API v2 (%s)", _poll_mode())
        else:
            log.info("X reader mode: RSS feed bridge (%s)", base)

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    async def fetch_recent(self, username: str, *, limit: int) -> list[XFeedItem]:
        if _bearer_token() and _poll_mode() != "rss":
            try:
                return await self._fetch_recent_api(username, limit=limit)
            except Exception as exc:
                if _poll_mode() == "api":
                    raise
                log.warning("X API failed for @%s; falling back to RSS: %s", username, exc)
        return await self._fetch_recent_rss(username, limit=limit)

    async def _api_get_json(self, url: str, *, params: dict[str, str | int] | None = None) -> dict[str, Any]:
        if self._session is None:
            raise RuntimeError("XReader.start() was not called.")
        async with self._session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as response:
            raw = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"X API HTTP {response.status}: {raw[:500]}")
            data = await response.json()
        return data

    async def _user_id(self, username: str) -> str:
        normalized = normalize_x_username(username)
        if normalized in self._user_id_cache:
            return self._user_id_cache[normalized]
        payload = await self._api_get_json(f"https://api.x.com/2/users/by/username/{normalized}")
        data = payload.get("data") or {}
        user_id = str(data.get("id") or "")
        if not user_id:
            raise RuntimeError(f"X API did not return a user id for @{normalized}: {payload}")
        self._user_id_cache[normalized] = user_id
        return user_id

    async def _fetch_recent_api(self, username: str, *, limit: int) -> list[XFeedItem]:
        normalized = normalize_x_username(username)
        user_id = await self._user_id(normalized)
        params: dict[str, str | int] = {
            "max_results": max(5, min(100, limit)),
            "tweet.fields": "created_at,attachments,entities,referenced_tweets,author_id",
            "expansions": "attachments.media_keys,author_id",
            "media.fields": "media_key,type,url,preview_image_url,alt_text",
            "user.fields": "profile_image_url,verified,verified_type,name,username",
            "exclude": "replies,retweets",
        }
        payload = await self._api_get_json(f"https://api.x.com/2/users/{user_id}/tweets", params=params)
        media_map = _api_media_map(payload)
        user_map = _api_user_map(payload)
        tweets = payload.get("data") or []
        items: list[XFeedItem] = []
        for tweet in tweets:
            tweet_id = str(tweet.get("id") or "")
            attachments = list(media_map.get(tweet_id, []))
            if not attachments and self._session is not None:
                for url in _tweet_urls(tweet)[:2]:
                    og = await _fetch_og_image(self._session, url)
                    if og:
                        attachments.append(og)
                        break
            user = user_map.get(str(tweet.get("author_id") or ""))
            items.append(_tweet_to_item(normalized, tweet, attachments, user=user))
        items.reverse()
        return items[-limit:]

    async def _fetch_recent_rss(self, username: str, *, limit: int) -> list[XFeedItem]:
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
        if _bearer_token() and _poll_mode() != "rss" and last_item_key and last_item_key != BASELINE_PENDING:
            try:
                return await self._fetch_new_api(username, since_id=last_item_key, limit=limit)
            except Exception as exc:
                if _poll_mode() == "api":
                    raise
                log.warning("X API incremental fetch failed for @%s; falling back to recent feed: %s", username, exc)
        items = await self.fetch_recent(username, limit=limit)
        if last_item_key == BASELINE_PENDING:
            return []
        if not last_item_key:
            return items
        for idx, item in enumerate(items):
            if item.key == last_item_key:
                return items[idx + 1 :]
        return items

    async def _fetch_new_api(self, username: str, *, since_id: str, limit: int) -> list[XFeedItem]:
        normalized = normalize_x_username(username)
        user_id = await self._user_id(normalized)
        params: dict[str, str | int] = {
            "max_results": max(5, min(100, limit)),
            "since_id": since_id,
            "tweet.fields": "created_at,attachments,entities,referenced_tweets,author_id",
            "expansions": "attachments.media_keys,author_id",
            "media.fields": "media_key,type,url,preview_image_url,alt_text",
            "user.fields": "profile_image_url,verified,verified_type,name,username",
            "exclude": "replies,retweets",
        }
        payload = await self._api_get_json(f"https://api.x.com/2/users/{user_id}/tweets", params=params)
        media_map = _api_media_map(payload)
        user_map = _api_user_map(payload)
        tweets = payload.get("data") or []
        items: list[XFeedItem] = []
        for tweet in tweets:
            tweet_id = str(tweet.get("id") or "")
            attachments = list(media_map.get(tweet_id, []))
            if not attachments and self._session is not None:
                for url in _tweet_urls(tweet)[:2]:
                    og = await _fetch_og_image(self._session, url)
                    if og:
                        attachments.append(og)
                        break
            user = user_map.get(str(tweet.get("author_id") or ""))
            items.append(_tweet_to_item(normalized, tweet, attachments, user=user))
        items.reverse()
        return items[-limit:]
