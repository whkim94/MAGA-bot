from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from .bot_env import env_int
from .bot_paths import DATA_DIR, DB_PATH, log_storage_diagnostics
from .db import BotDatabase, parse_keywords
from .discord_format import post_embed, summary_text
from .telegram_client import (
    build_telegram_client,
    fetch_new_posts,
    fetch_posts_since,
    fetch_recent_posts,
    keyword_matches,
)

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
        self.telegram = build_telegram_client(DATA_DIR)
        self.poll_task: asyncio.Task[None] | None = None
        self.poll_interval = env_int("POLL_INTERVAL_SECONDS", 60, minimum=15)
        self.fetch_limit = env_int("FETCH_LIMIT_PER_CHANNEL", 30, minimum=1)

    async def setup_hook(self) -> None:
        self.db.init()
        log_storage_diagnostics(log)

        await self.telegram.start()
        if not await self.telegram.is_user_authorized():
            raise RuntimeError(
                "Telegram session is not authorized. Run scripts/create_telegram_session.py "
                "locally and set TELEGRAM_SESSION on Railway."
            )

        guild_id = (os.getenv("DISCORD_GUILD_ID") or "").strip()
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d slash commands to guild %s", len(synced), guild_id)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d global slash commands", len(synced))

        self.poll_task = asyncio.create_task(self.poll_loop(), name="telegram-poll-loop")

    async def close(self) -> None:
        if self.poll_task:
            self.poll_task.cancel()
        await self.telegram.disconnect()
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
        subscriptions = self.db.list_subscriptions(enabled_only=True)
        for sub in subscriptions:
            try:
                channel = self.get_channel(sub.discord_channel_id)
                if channel is None:
                    channel = await self.fetch_channel(sub.discord_channel_id)
                if not isinstance(channel, discord.abc.Messageable):
                    log.warning("Discord channel %s is not messageable", sub.discord_channel_id)
                    continue

                posts = await fetch_new_posts(
                    self.telegram,
                    channel=sub.telegram_channel,
                    min_id=sub.last_message_id,
                    limit=self.fetch_limit,
                )
                for post in posts:
                    matched = [kw for kw in sub.keywords if kw.casefold() in post.text.casefold()]
                    if not keyword_matches(post.text, sub.keywords):
                        self.db.set_last_message_id(sub.id, post.id)
                        continue
                    await channel.send(embed=post_embed(post, matched_keywords=matched))
                    self.db.mark_delivered(sub.id, post.id)
                    self.db.set_last_message_id(sub.id, post.id)
                    log.info("Delivered @%s/%s to #%s", sub.telegram_channel, post.id, sub.discord_channel_id)
            except Exception:
                log.exception("Failed polling subscription id=%s @%s", sub.id, sub.telegram_channel)

    async def latest_message_id(self, telegram_channel: str) -> int:
        posts = await fetch_recent_posts(self.telegram, channel=telegram_channel, limit=1)
        return posts[-1].id if posts else 0


bot = TelegramDiscordBot()


def _guild_id(interaction: discord.Interaction) -> int:
    if interaction.guild_id is None:
        raise app_commands.AppCommandError("서버 안에서만 사용할 수 있습니다.")
    return int(interaction.guild_id)


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
        f"키워드: `{kw_text}`\n"
        f"기준 message_id: `{latest_id}` 이후 새 글부터 전송합니다.",
        ephemeral=True,
    )


@bot.tree.command(name="tg-remove", description="텔레그램 채널 연결을 삭제합니다.")
@app_commands.describe(subscription_id="/tg-list에서 보이는 ID")
@app_commands.default_permissions(manage_guild=True)
async def tg_remove(interaction: discord.Interaction, subscription_id: int) -> None:
    ok = bot.db.remove_subscription(guild_id=_guild_id(interaction), subscription_id=subscription_id)
    await interaction.response.send_message(
        "삭제 완료." if ok else "해당 ID를 찾지 못했습니다.",
        ephemeral=True,
    )


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
    posts = await fetch_recent_posts(bot.telegram, channel=sub.telegram_channel, limit=10)
    post = next((item for item in reversed(posts) if keyword_matches(item.text, sub.keywords)), None)
    if post is None:
        await interaction.followup.send("키워드 조건에 맞는 최근 글이 없습니다.", ephemeral=True)
        return
    matched = [kw for kw in sub.keywords if kw.casefold() in post.text.casefold()]
    await channel.send(content="테스트 전송입니다.", embed=post_embed(post, matched_keywords=matched))
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
    posts = await fetch_posts_since(bot.telegram, channel=sub.telegram_channel, since=since)
    posts = [post for post in posts if keyword_matches(post.text, sub.keywords)]
    channel = bot.get_channel(sub.discord_channel_id) or await bot.fetch_channel(sub.discord_channel_id)
    await channel.send(summary_text(sub.telegram_channel, posts, hours=hours))
    await interaction.followup.send("요약 목록 전송 완료.", ephemeral=True)


def main() -> None:
    token = (os.getenv("DISCORD_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("DISCORD_TOKEN is required.")
    bot.run(token)


if __name__ == "__main__":
    main()
