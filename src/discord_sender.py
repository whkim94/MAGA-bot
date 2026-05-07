from __future__ import annotations

import io
import re
from urllib.parse import urlparse

import aiohttp
import discord

from .discord_format import first_visual_attachment, post_embed
from .telegram_client import TelegramPost

MAX_IMAGE_BYTES = 8 * 1024 * 1024


def _extension_from_content_type(content_type: str) -> str:
    content_type = content_type.split(";", 1)[0].strip().lower()
    return {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }.get(content_type, ".jpg")


def _safe_filename(url: str, content_type: str) -> str:
    path = urlparse(url).path
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", path.rsplit("/", 1)[-1].split(".", 1)[0])[:40]
    if not stem:
        stem = "image"
    return f"{stem}{_extension_from_content_type(content_type)}"


async def _download_image(url: str) -> tuple[discord.File, str] | None:
    try:
        async with aiohttp.ClientSession(
            headers={"User-Agent": "Mozilla/5.0 DiscordBot/1.0"}
        ) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as response:
                if response.status >= 400:
                    return None
                content_type = response.headers.get("Content-Type", "")
                if not content_type.lower().startswith("image/"):
                    return None
                data = await response.content.read(MAX_IMAGE_BYTES + 1)
                if len(data) > MAX_IMAGE_BYTES:
                    return None
    except (aiohttp.ClientError, TimeoutError):
        return None

    filename = _safe_filename(url, content_type)
    return discord.File(io.BytesIO(data), filename=filename), filename


async def send_post_message(
    channel: discord.abc.Messageable,
    post: TelegramPost,
    *,
    matched_keywords: list[str],
    prefix: str | None = None,
) -> None:
    visual = first_visual_attachment(post)
    downloaded = await _download_image(visual.url) if visual else None
    if downloaded:
        file, filename = downloaded
        embed = post_embed(
            post,
            matched_keywords=matched_keywords,
            image_url_override=f"attachment://{filename}",
            image_used_override=visual.url,
        )
        await channel.send(content=prefix, embed=embed, file=file)
        return

    embed = post_embed(post, matched_keywords=matched_keywords)
    await channel.send(content=prefix, embed=embed)
