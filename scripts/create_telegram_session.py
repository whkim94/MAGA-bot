from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def main() -> None:
    load_dotenv(ROOT / ".env")
    api_id = (os.getenv("TELEGRAM_API_ID") or "").strip()
    api_hash = (os.getenv("TELEGRAM_API_HASH") or "").strip()
    if not api_id or not api_hash:
        raise RuntimeError("Set TELEGRAM_API_ID and TELEGRAM_API_HASH first.")

    async with TelegramClient(StringSession(), int(api_id), api_hash) as client:
        print("\nTELEGRAM_SESSION value:\n")
        print(client.session.save())
        print("\nAdd this value to Railway Variables as TELEGRAM_SESSION.\n")


if __name__ == "__main__":
    asyncio.run(main())
