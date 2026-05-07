from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import discord

from .telegram_client import TelegramAttachment, TelegramPost

KST = ZoneInfo("Asia/Seoul")
DISCORD_EMBED_DESC_LIMIT = 4096
ATTACHMENT_FIELD_LIMIT = 1024
SUMMARY_MEDIA_LIMIT = 5


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


def _post_title(post: TelegramPost, matched_keywords: list[str]) -> str:
    first_line = next((line.strip() for line in post.text.splitlines() if line.strip()), "")
    if first_line and first_line != "(미디어/첨부만 있는 게시물)":
        title = f"[{post.channel}] {_shorten(first_line, 180)}"
    else:
        title = f"[{post.channel}] 새 게시물"
    if matched_keywords:
        title += " · " + ", ".join(matched_keywords[:5])
    return title[:256]


def _channel_color(channel: str) -> int:
    palette = [0x2B90D9, 0xF1C40F, 0x9B59B6, 0x2ECC71, 0xE67E22, 0xE74C3C, 0x1ABC9C]
    return palette[sum(ord(ch) for ch in channel.casefold()) % len(palette)]


def _attachment_label(attachment: TelegramAttachment) -> str:
    labels = {
        "image": "이미지",
        "preview": "미리보기",
        "video": "비디오",
        "file": "파일",
        "audio": "오디오",
    }
    return labels.get(attachment.kind, "첨부")


def _first_visual_attachment(attachments: list[TelegramAttachment]) -> TelegramAttachment | None:
    for kind in ("image", "video", "preview"):
        for attachment in attachments:
            if attachment.kind == kind:
                return attachment
    return None


def large_media_content(post: TelegramPost, *, prefix: str | None = None) -> str | None:
    """Return message content that lets Discord render a larger native media preview."""
    visual = _first_visual_attachment(post.attachments)
    if not visual:
        return prefix
    if visual.kind != "image":
        return prefix
    if prefix:
        return f"{prefix}\n{visual.url}"
    return visual.url


def _attachment_field(attachments: list[TelegramAttachment], *, image_used: str | None) -> str:
    lines: list[str] = []
    for idx, attachment in enumerate(attachments, start=1):
        if image_used and attachment.url == image_used:
            continue
        label = _attachment_label(attachment)
        title = attachment.title or label
        lines.append(f"{idx}. {label} · [{_shorten(title, 60)}]({attachment.url})")
    if not lines:
        return ""
    return _shorten("\n".join(lines), ATTACHMENT_FIELD_LIMIT)


def post_embed(post: TelegramPost, *, matched_keywords: list[str]) -> discord.Embed:
    embed = discord.Embed(
        title=_post_title(post, matched_keywords),
        description=_shorten(post.text, DISCORD_EMBED_DESC_LIMIT - 450),
        url=post.url,
        color=_channel_color(post.channel),
    )
    embed.set_author(name=f"@{post.channel}", url=f"https://t.me/s/{post.channel}")

    image_used: str | None = None
    visual = _first_visual_attachment(post.attachments)
    if visual:
        embed.set_image(url=visual.url)
        image_used = visual.url

    attachment_lines = _attachment_field(post.attachments, image_used=image_used)
    if attachment_lines:
        embed.add_field(name="첨부 / 미디어", value=attachment_lines, inline=False)

    embed.add_field(name="원문", value=f"[Telegram에서 보기]({post.url})", inline=True)
    embed.add_field(name="시간", value=format_time_kst(post.date), inline=True)
    if post.attachments:
        embed.add_field(name="미디어", value=f"{len(post.attachments)}개", inline=True)
    embed.set_footer(text=f"Telegram message_id={post.id}")
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


def summary_media_posts(posts: list[TelegramPost], *, limit: int = SUMMARY_MEDIA_LIMIT) -> list[TelegramPost]:
    media_posts = [post for post in posts if _first_visual_attachment(post.attachments)]
    return media_posts[-limit:]
