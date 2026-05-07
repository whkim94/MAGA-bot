from __future__ import annotations

import io
import re
from urllib.parse import urlparse

import aiohttp
import discord
from PIL import Image, ImageOps, UnidentifiedImageError

from .discord_format import first_visual_attachment, post_embed
from .telegram_client import TelegramPost

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000


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


def _normalize_image(data: bytes) -> tuple[bytes, str]:
    try:
        with Image.open(io.BytesIO(data)) as image:
            image = ImageOps.exif_transpose(image)
            if image.width * image.height > MAX_IMAGE_PIXELS:
                image.thumbnail((4000, 4000))
            if image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            out = io.BytesIO()
            image.save(out, format="PNG", optimize=True)
            return out.getvalue(), "image/png"
    except (UnidentifiedImageError, OSError, ValueError):
        return data, "application/octet-stream"


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

    normalized_data, normalized_type = _normalize_image(data)
    if normalized_type != "image/png" or len(normalized_data) > MAX_IMAGE_BYTES:
        return None
    filename = re.sub(r"\.[A-Za-z0-9]+$", "", _safe_filename(url, content_type)) + ".png"
    return discord.File(io.BytesIO(normalized_data), filename=filename), filename


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
        file, _filename = downloaded
        embed = post_embed(
            post,
            matched_keywords=matched_keywords,
            image_used_override=visual.url,
        )
        await channel.send(content=prefix, embed=embed, file=file)
        return

    embed = post_embed(post, matched_keywords=matched_keywords)
    await channel.send(content=prefix, embed=embed)
