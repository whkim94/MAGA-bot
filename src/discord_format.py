from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import discord

from .telegram_client import TelegramPost

KST = ZoneInfo("Asia/Seoul")
DISCORD_EMBED_DESC_LIMIT = 4096


def _shorten(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def format_time_kst(value: datetime | None) -> str:
    if value is None:
        return "-"
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return value.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")


def post_embed(post: TelegramPost, *, matched_keywords: list[str]) -> discord.Embed:
    title = f"Telegram: @{post.channel}"
    if matched_keywords:
        title += " · " + ", ".join(matched_keywords[:5])
    embed = discord.Embed(
        title=title[:256],
        description=_shorten(post.text, DISCORD_EMBED_DESC_LIMIT - 250),
        url=post.url,
        color=0x2B90D9,
    )
    embed.add_field(name="원문", value=f"[Telegram에서 보기]({post.url})", inline=True)
    embed.add_field(name="시간", value=format_time_kst(post.date), inline=True)
    embed.set_footer(text=f"message_id={post.id}")
    return embed


def summary_text(channel: str, posts: list[TelegramPost], *, hours: int) -> str:
    if not posts:
        return f"@{channel}: 최근 {hours}시간 메시지가 없습니다."

    lines = [f"@{channel} 최근 {hours}시간 요약 ({len(posts)}건)"]
    for post in posts[-20:]:
        first_line = post.text.splitlines()[0] if post.text else "(본문 없음)"
        lines.append(f"- {format_time_kst(post.date)} · {_shorten(first_line, 160)}")
        lines.append(f"  {post.url}")
    if len(posts) > 20:
        lines.append(f"... 외 {len(posts) - 20}건")
    return _shorten("\n".join(lines), 3900)
