# Telegram to Discord Bot

Python Discord bot for forwarding Telegram channel posts to Discord with slash commands.

## Stack

- `discord.py` for Discord slash commands
- public `https://t.me/s` scraping by default
- optional `Telethon` API mode when Telegram API credentials are available
- `SQLite` stored under Railway Volume
- Railway worker deployment

## Railway Volume

Add a Volume to the Railway service and mount it at:

```text
/app/data
```

The bot stores `bot.sqlite3` there. Resolution order is:

```text
BOT_DATA_DIR -> RAILWAY_VOLUME_MOUNT_PATH -> ./data
```

## Environment Variables

Copy `.env.example` to `.env` locally, then set the same variables in Railway.

```env
DISCORD_TOKEN=
DISCORD_CLIENT_ID=
DISCORD_GUILD_ID=
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_SESSION=
BOT_DATA_DIR=/app/data
POLL_INTERVAL_SECONDS=60
FETCH_LIMIT_PER_CHANNEL=30
```

## Telegram Session

By default, the bot can read public Telegram channels through `https://t.me/s/<channel>` without Telegram API credentials. This works for public channels only and only the latest public page history is available.

For a more stable API mode, set Telegram credentials later. Telegram channel reads use a user session, not the Discord bot account.

1. Create `api_id` and `api_hash` at <https://my.telegram.org>.
2. Put them in local `.env`.
3. Run:

```bash
python scripts/create_telegram_session.py
```

4. Copy the printed value into Railway as `TELEGRAM_SESSION`.

## Commands

- `/tg-add` registers a Telegram channel to a Discord channel.
- `/tg-remove` removes a registered subscription.
- `/tg-list` shows registered subscriptions.
- `/tg-keywords` replaces the keyword filter for a subscription.
- `/tg-test` sends the most recent matching post for testing.
- `/tg-summary` sends a recent post list for the last N hours.

Forwarded Discord messages include the Telegram post text, source channel, original link, KST time, and public media found on `t.me/s` pages. The first image/video preview is shown in the embed, and additional files/previews are listed as attachment links.

Example:

```text
/tg-add telegram_channel: WeCryptoTogether target_channel: #crypto-news keywords: 업비트,TGE,에어드랍
```

When a channel is added, the bot sets the current latest Telegram message as the baseline. Only newer messages are forwarded.

## Discord Invite

Create the invite URL from Discord Developer Portal with both scopes:

```text
bot
applications.commands
```

Required bot permissions:

```text
View Channels
Send Messages
Embed Links
Read Message History
Use Slash Commands
```

If Railway logs show `403 Forbidden (error code: 50001): Missing Access` while syncing commands, check:

- `DISCORD_GUILD_ID` is the server ID, not a channel ID.
- The bot is already invited to that server.
- The invite URL included `applications.commands`.
- The bot has permission to view/send in the target Discord channel.

## Deploy

Railway uses:

```text
python -m src.main
```

from `railway.toml`.
