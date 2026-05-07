from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from .bot_env import env_int
from .bot_paths import DATA_DIR, DB_PATH, log_storage_diagnostics
from .db import BotDatabase, parse_keywords
from .discord_format import large_media_content, post_embed, summary_media_posts, summary_text
from .telegram_client import (
    build_telegram_reader,
    keyword_matches,
)
from .x_client import BASELINE_PENDING, XReader, normalize_x_username, x_keyword_matches

load_dotenv()

LOG_LEVEL = (os.getenv("BOT_LOG_LEVEL") or "INFO").strip().upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
)
log = logging.getLogger("telegram-discord-bot")


class TelegramDiscordBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.db = BotDatabase(DB_PATH)
        self.telegram = build_telegram_reader(DATA_DIR)
        self.x_reader = XReader()
        self.poll_task: asyncio.Task[None] | None = None
        self.poll_interval = env_int("POLL_INTERVAL_SECONDS", 60, minimum=15)
        self.fetch_limit = env_int("FETCH_LIMIT_PER_CHANNEL", 30, minimum=1)

    async def setup_hook(self) -> None:
        self.db.init()
        log_storage_diagnostics(log)

        await self.telegram.start()
        await self.x_reader.start()
        if not await self.telegram.is_user_authorized():
            raise RuntimeError(
                "Telegram session is not authorized. Run scripts/create_telegram_session.py "
                "locally and set TELEGRAM_SESSION on Railway."
            )

        guild_id = (os.getenv("DISCORD_GUILD_ID") or "").strip()
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            try:
                synced = await self.tree.sync(guild=guild)
                log.info("Synced %d slash commands to guild %s", len(synced), guild_id)
            except discord.Forbidden:
                log.error(
                    "Cannot sync slash commands to guild %s. Check DISCORD_GUILD_ID, "
                    "invite the bot to that server, and include the applications.commands scope.",
                    guild_id,
                )
                synced = await self.tree.sync()
                log.info("Fell back to global slash command sync (%d commands).", len(synced))
        else:
            synced = await self.tree.sync()
            log.info("Synced %d global slash commands", len(synced))

        self.poll_task = asyncio.create_task(self.poll_loop(), name="telegram-poll-loop")

    async def close(self) -> None:
        if self.poll_task:
            self.poll_task.cancel()
        await self.telegram.disconnect()
        await self.x_reader.close()
        self.db.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Discord bot logged in as %s (%s)", self.user, self.user.id if self.user else "-")

    async def poll_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("poll loop failed")
            await asyncio.sleep(self.poll_interval)

    async def poll_once(self) -> None:
        await self.poll_telegram_once()
        await self.poll_x_once()

    async def poll_telegram_once(self) -> None:
        subscriptions = self.db.list_subscriptions(enabled_only=True)
        for sub in subscriptions:
            try:
                channel = self.get_channel(sub.discord_channel_id)
                if channel is None:
                    channel = await self.fetch_channel(sub.discord_channel_id)
                if not isinstance(channel, discord.abc.Messageable):
                    log.warning("Discord channel %s is not messageable", sub.discord_channel_id)
                    continue

                posts = await self.telegram.fetch_new_posts(
                    channel=sub.telegram_channel,
                    min_id=sub.last_message_id,
                    limit=self.fetch_limit,
                )
                for post in posts:
                    matched = [kw for kw in sub.keywords if kw.casefold() in post.text.casefold()]
                    if not keyword_matches(post.text, sub.keywords):
                        self.db.set_last_message_id(sub.id, post.id)
                        continue
                    await channel.send(
                        content=large_media_content(post),
                        embed=post_embed(post, matched_keywords=matched),
                    )
                    self.db.mark_delivered(sub.id, post.id)
                    self.db.set_last_message_id(sub.id, post.id)
                    log.info("Delivered @%s/%s to #%s", sub.telegram_channel, post.id, sub.discord_channel_id)
            except Exception:
                log.exception("Failed polling subscription id=%s @%s", sub.id, sub.telegram_channel)

    async def poll_x_once(self) -> None:
        subscriptions = self.db.list_x_subscriptions(enabled_only=True)
        for sub in subscriptions:
            try:
                channel = self.get_channel(sub.discord_channel_id)
                if channel is None:
                    channel = await self.fetch_channel(sub.discord_channel_id)
                if not isinstance(channel, discord.abc.Messageable):
                    log.warning("Discord channel %s is not messageable", sub.discord_channel_id)
                    continue

                items = await self.x_reader.fetch_new(
                    sub.username,
                    last_item_key=sub.last_item_key,
                    limit=self.fetch_limit,
                )
                if sub.last_item_key == BASELINE_PENDING:
                    recent = await self.x_reader.fetch_recent(sub.username, limit=1)
                    if recent:
                        self.db.set_x_last_item_key(sub.id, recent[-1].key)
                        log.info("Initialized X baseline for @%s", sub.username)
                    continue
                for item in items:
                    post = item.post
                    matched = [kw for kw in sub.keywords if kw.casefold() in post.text.casefold()]
                    if not x_keyword_matches(post.text, sub.keywords):
                        self.db.set_x_last_item_key(sub.id, item.key)
                        continue
                    await channel.send(
                        content=large_media_content(post),
                        embed=post_embed(post, matched_keywords=matched),
                    )
                    self.db.mark_x_delivered(sub.id, item.key)
                    self.db.set_x_last_item_key(sub.id, item.key)
                    log.info("Delivered X @%s/%s to #%s", sub.username, item.key, sub.discord_channel_id)
            except RuntimeError as exc:
                log.warning("Failed polling X subscription id=%s @%s: %s", sub.id, sub.username, exc)
            except Exception:
                log.exception("Unexpected X polling error subscription id=%s @%s", sub.id, sub.username)

    async def latest_message_id(self, telegram_channel: str) -> int:
        posts = await self.telegram.fetch_recent_posts(channel=telegram_channel, limit=1)
        return posts[-1].id if posts else 0

    async def latest_x_item_key(self, username: str) -> str:
        items = await self.x_reader.fetch_recent(username, limit=1)
        return items[-1].key if items else ""


bot = TelegramDiscordBot()


def _guild_id(interaction: discord.Interaction) -> int:
    if interaction.guild_id is None:
        raise app_commands.AppCommandError("서버 안에서만 사용할 수 있습니다.")
    return int(interaction.guild_id)


def _parse_bulk_import_lines(text: str) -> list[tuple[str, int | None]]:
    parsed: list[tuple[str, int | None]] = []
    for line in text.splitlines():
        channel_match = re.search(r"@([A-Za-z0-9_]+)", line)
        if not channel_match:
            continue
        last_match = re.search(r"\blast=(\d+)", line)
        parsed.append(
            (
                channel_match.group(1),
                int(last_match.group(1)) if last_match else None,
            )
        )
    return parsed


@bot.tree.command(name="tg-add", description="텔레그램 채널을 현재 서버의 Discord 채널로 연결합니다.")
@app_commands.describe(
    telegram_channel="예: WeCryptoTogether 또는 https://t.me/s/WeCryptoTogether",
    target_channel="알림을 보낼 Discord 채널",
    keywords="쉼표로 구분. 비워두면 모든 글을 전송합니다.",
)
@app_commands.default_permissions(manage_guild=True)
async def tg_add(
    interaction: discord.Interaction,
    telegram_channel: str,
    target_channel: discord.TextChannel,
    keywords: str | None = None,
) -> None:
    await interaction.response.defer(ephemeral=True)
    parsed_keywords = parse_keywords(keywords)
    sub_id = bot.db.add_subscription(
        guild_id=_guild_id(interaction),
        discord_channel_id=target_channel.id,
        telegram_channel=telegram_channel,
        keywords=parsed_keywords,
    )
    sub = bot.db.get_subscription(guild_id=_guild_id(interaction), subscription_id=sub_id)
    if sub is None:
        await interaction.followup.send("등록 후 설정을 찾지 못했습니다.", ephemeral=True)
        return

    latest_id = await bot.latest_message_id(sub.telegram_channel)
    bot.db.set_last_message_id(sub.id, latest_id)
    kw_text = ", ".join(parsed_keywords) if parsed_keywords else "전체"
    await interaction.followup.send(
        f"등록 완료: `#{target_channel.name}` <- `@{sub.telegram_channel}`\n"
        f"구독 ID: `{sub.id}` (`/tg-test`, `/tg-keywords`, `/tg-remove`에 사용)\n"
        f"키워드: `{kw_text}`\n"
        f"기준 Telegram message_id: `{latest_id}` 이후 새 글부터 전송합니다.",
        ephemeral=True,
    )


@bot.tree.command(name="tg-bulk-import", description="/tg-list 형식 텍스트를 붙여넣어 여러 채널을 한 번에 등록합니다.")
@app_commands.describe(
    target_channel="알림을 보낼 Discord 채널",
    subscriptions_text="@channel 및 last=123 형식이 들어간 여러 줄 텍스트",
)
@app_commands.default_permissions(manage_guild=True)
async def tg_bulk_import(
    interaction: discord.Interaction,
    target_channel: discord.TextChannel,
    subscriptions_text: str,
) -> None:
    await interaction.response.defer(ephemeral=True)
    rows = _parse_bulk_import_lines(subscriptions_text)
    if not rows:
        await interaction.followup.send("가져올 채널을 찾지 못했습니다. `@channel` 형식이 포함된 텍스트를 붙여넣어 주세요.", ephemeral=True)
        return

    guild_id = _guild_id(interaction)
    imported: list[str] = []
    failed: list[str] = []
    for telegram_channel, last_message_id in rows:
        try:
            sub_id = bot.db.add_subscription(
                guild_id=guild_id,
                discord_channel_id=target_channel.id,
                telegram_channel=telegram_channel,
                keywords=[],
            )
            baseline = last_message_id
            if baseline is None:
                baseline = await bot.latest_message_id(telegram_channel)
            bot.db.set_last_message_id(sub_id, baseline)
            imported.append(f"@{telegram_channel} last={baseline}")
        except Exception as exc:
            log.exception("bulk import failed for @%s", telegram_channel)
            failed.append(f"@{telegram_channel}: {exc}")

    message = [
        f"Bulk import 완료: {len(imported)}개 등록 -> `#{target_channel.name}`",
        "키워드: `전체`",
    ]
    if imported:
        message.append("\n".join(imported[:25]))
    if failed:
        message.append("실패:\n" + "\n".join(failed[:10]))
    await interaction.followup.send("\n".join(message)[:1900], ephemeral=True)


@bot.tree.command(name="tg-remove", description="텔레그램 채널 연결을 삭제합니다.")
@app_commands.describe(subscription_id="/tg-list에서 보이는 ID")
@app_commands.default_permissions(manage_guild=True)
async def tg_remove(interaction: discord.Interaction, subscription_id: int) -> None:
    ok = bot.db.remove_subscription(guild_id=_guild_id(interaction), subscription_id=subscription_id)
    await interaction.response.send_message(
        "삭제 완료." if ok else "해당 ID를 찾지 못했습니다.",
        ephemeral=True,
    )


@bot.tree.command(name="x-add", description="X 계정을 Discord 채널로 연결합니다.")
@app_commands.describe(
    username="예: gorochi0315 또는 https://x.com/gorochi0315",
    target_channel="알림을 보낼 Discord 채널",
    keywords="쉼표로 구분. 비워두면 모든 글을 전송합니다.",
)
@app_commands.default_permissions(manage_guild=True)
async def x_add(
    interaction: discord.Interaction,
    username: str,
    target_channel: discord.TextChannel,
    keywords: str | None = None,
) -> None:
    await interaction.response.defer(ephemeral=True)
    normalized = normalize_x_username(username)
    parsed_keywords = parse_keywords(keywords)
    sub_id = bot.db.add_x_subscription(
        guild_id=_guild_id(interaction),
        discord_channel_id=target_channel.id,
        username=normalized,
        keywords=parsed_keywords,
    )
    baseline_note = "현재 최신 항목 이후 새 글부터 전송합니다."
    try:
        latest_key = await bot.latest_x_item_key(normalized)
        if latest_key:
            bot.db.set_x_last_item_key(sub_id, latest_key)
    except Exception as exc:
        log.warning("Could not initialize X baseline for @%s during /x-add: %s", normalized, exc)
        bot.db.set_x_last_item_key(sub_id, BASELINE_PENDING)
        baseline_note = "RSS 브리지가 일시 실패해서 다음 polling 때 기준점을 잡습니다. 기존 글은 전송하지 않습니다."
    kw_text = ", ".join(parsed_keywords) if parsed_keywords else "전체"
    await interaction.followup.send(
        f"X 등록 완료: `#{target_channel.name}` <- `@{normalized}`\n"
        f"구독 ID: `{sub_id}` (`/x-test`, `/x-remove`에 사용)\n"
        f"키워드: `{kw_text}`\n"
        f"{baseline_note}",
        ephemeral=True,
    )


@bot.tree.command(name="x-list", description="현재 서버의 X 연결 목록을 봅니다.")
async def x_list(interaction: discord.Interaction) -> None:
    subs = bot.db.list_x_subscriptions(guild_id=_guild_id(interaction))
    if not subs:
        await interaction.response.send_message("등록된 X 연결이 없습니다.", ephemeral=True)
        return
    lines = []
    for sub in subs:
        channel = interaction.guild.get_channel(sub.discord_channel_id) if interaction.guild else None
        target = channel.mention if channel else f"`{sub.discord_channel_id}`"
        kw = ", ".join(sub.keywords) if sub.keywords else "전체"
        last = sub.last_item_key[-28:] if sub.last_item_key else "-"
        lines.append(f"`{sub.id}` · X @{sub.username} -> {target} · 키워드: {kw} · last={last}")
    await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)


@bot.tree.command(name="x-remove", description="X 계정 연결을 삭제합니다.")
@app_commands.describe(subscription_id="/x-list에서 보이는 ID")
@app_commands.default_permissions(manage_guild=True)
async def x_remove(interaction: discord.Interaction, subscription_id: int) -> None:
    ok = bot.db.remove_x_subscription(guild_id=_guild_id(interaction), subscription_id=subscription_id)
    await interaction.response.send_message(
        "삭제 완료." if ok else "해당 X 구독 ID를 찾지 못했습니다.",
        ephemeral=True,
    )


@bot.tree.command(name="x-test", description="최근 X 글 1개를 테스트 전송합니다.")
@app_commands.describe(subscription_id="/x-list에서 보이는 ID")
@app_commands.default_permissions(manage_guild=True)
async def x_test(interaction: discord.Interaction, subscription_id: int) -> None:
    await interaction.response.defer(ephemeral=True)
    sub = bot.db.get_x_subscription(guild_id=_guild_id(interaction), subscription_id=subscription_id)
    if sub is None:
        await interaction.followup.send("해당 X 구독 ID를 찾지 못했습니다.", ephemeral=True)
        return
    channel = bot.get_channel(sub.discord_channel_id) or await bot.fetch_channel(sub.discord_channel_id)
    items = await bot.x_reader.fetch_recent(sub.username, limit=10)
    item = next((entry for entry in reversed(items) if x_keyword_matches(entry.post.text, sub.keywords)), None)
    if item is None:
        await interaction.followup.send("키워드 조건에 맞는 최근 X 글이 없습니다.", ephemeral=True)
        return
    matched = [kw for kw in sub.keywords if kw.casefold() in item.post.text.casefold()]
    await channel.send(
        content=large_media_content(item.post, prefix="X 테스트 전송입니다."),
        embed=post_embed(item.post, matched_keywords=matched),
    )
    await interaction.followup.send("X 테스트 전송 완료.", ephemeral=True)


@bot.tree.command(name="tg-list", description="현재 서버의 텔레그램 연결 목록을 봅니다.")
async def tg_list(interaction: discord.Interaction) -> None:
    subs = bot.db.list_subscriptions(guild_id=_guild_id(interaction))
    if not subs:
        await interaction.response.send_message("등록된 연결이 없습니다.", ephemeral=True)
        return
    lines = []
    for sub in subs:
        channel = interaction.guild.get_channel(sub.discord_channel_id) if interaction.guild else None
        target = channel.mention if channel else f"`{sub.discord_channel_id}`"
        kw = ", ".join(sub.keywords) if sub.keywords else "전체"
        lines.append(
            f"`{sub.id}` · @{sub.telegram_channel} -> {target} · 키워드: {kw} · last={sub.last_message_id}"
        )
    await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)


@bot.tree.command(name="tg-keywords", description="등록된 연결의 키워드를 교체합니다.")
@app_commands.describe(subscription_id="/tg-list에서 보이는 ID", keywords="쉼표로 구분. 비우면 전체 전송.")
@app_commands.default_permissions(manage_guild=True)
async def tg_keywords(
    interaction: discord.Interaction,
    subscription_id: int,
    keywords: str | None = None,
) -> None:
    parsed_keywords = parse_keywords(keywords)
    ok = bot.db.set_keywords(
        guild_id=_guild_id(interaction),
        subscription_id=subscription_id,
        keywords=parsed_keywords,
    )
    kw_text = ", ".join(parsed_keywords) if parsed_keywords else "전체"
    await interaction.response.send_message(
        f"키워드 업데이트 완료: `{kw_text}`" if ok else "해당 ID를 찾지 못했습니다.",
        ephemeral=True,
    )


@bot.tree.command(name="tg-test", description="최근 텔레그램 글 1개를 테스트 전송합니다.")
@app_commands.describe(subscription_id="/tg-list에서 보이는 ID")
@app_commands.default_permissions(manage_guild=True)
async def tg_test(interaction: discord.Interaction, subscription_id: int) -> None:
    await interaction.response.defer(ephemeral=True)
    sub = bot.db.get_subscription(guild_id=_guild_id(interaction), subscription_id=subscription_id)
    if sub is None:
        await interaction.followup.send("해당 ID를 찾지 못했습니다.", ephemeral=True)
        return
    channel = bot.get_channel(sub.discord_channel_id) or await bot.fetch_channel(sub.discord_channel_id)
    posts = await bot.telegram.fetch_recent_posts(channel=sub.telegram_channel, limit=10)
    post = next((item for item in reversed(posts) if keyword_matches(item.text, sub.keywords)), None)
    if post is None:
        await interaction.followup.send("키워드 조건에 맞는 최근 글이 없습니다.", ephemeral=True)
        return
    matched = [kw for kw in sub.keywords if kw.casefold() in post.text.casefold()]
    await channel.send(
        content=large_media_content(post, prefix="테스트 전송입니다."),
        embed=post_embed(post, matched_keywords=matched),
    )
    await interaction.followup.send("테스트 전송 완료.", ephemeral=True)


@bot.tree.command(name="tg-summary", description="최근 N시간 텔레그램 글을 Discord에 요약 목록으로 보냅니다.")
@app_commands.describe(subscription_id="/tg-list에서 보이는 ID", hours="최근 몇 시간. 기본 24시간.")
async def tg_summary(interaction: discord.Interaction, subscription_id: int, hours: int = 24) -> None:
    await interaction.response.defer(ephemeral=True)
    sub = bot.db.get_subscription(guild_id=_guild_id(interaction), subscription_id=subscription_id)
    if sub is None:
        await interaction.followup.send("해당 ID를 찾지 못했습니다.", ephemeral=True)
        return
    hours = max(1, min(hours, 168))
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    posts = await bot.telegram.fetch_posts_since(channel=sub.telegram_channel, since=since)
    posts = [post for post in posts if keyword_matches(post.text, sub.keywords)]
    channel = bot.get_channel(sub.discord_channel_id) or await bot.fetch_channel(sub.discord_channel_id)
    await channel.send(summary_text(sub.telegram_channel, posts, hours=hours))
    media_posts = summary_media_posts(posts)
    if media_posts:
        await channel.send(f"@{sub.telegram_channel} 대표 이미지/미디어 {len(media_posts)}개")
        for post in media_posts:
            await channel.send(
                content=large_media_content(post),
                embed=post_embed(post, matched_keywords=[]),
            )
    await interaction.followup.send("요약 목록 전송 완료.", ephemeral=True)


def main() -> None:
    token = (os.getenv("DISCORD_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("DISCORD_TOKEN is required.")
    bot.run(token)


if __name__ == "__main__":
    main()
