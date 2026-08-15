#!/usr/bin/env python3
"""
Set the Telegram bot's profile photo using the bot token.

This uses the Bot API method setMyProfilePhoto, so it works even when BotFather
says "You don't have any bots yet" (that happens when you are signed into a
different Telegram account than the one that created the bot). Ownership does
not matter here — only the token does.

Usage:
    python set_bot_avatar.py                     # uses telegram_bot_avatar.jpg
    python set_bot_avatar.py path/to/photo.jpg   # uses a different image
"""
from __future__ import annotations

import sys
from pathlib import Path

import requests
from dotenv import load_dotenv
import os

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_PHOTO = PROJECT_DIR / "telegram_bot_avatar.jpg"
API = "https://api.telegram.org"


def load_token() -> str:
    load_dotenv(PROJECT_DIR / ".env")
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        sys.exit("ERROR: TELEGRAM_BOT_TOKEN is not set in .env")
    return token


def main() -> None:
    token = load_token()
    photo_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PHOTO
    if not photo_path.is_file():
        sys.exit(f"ERROR: image not found: {photo_path}")
    if photo_path.suffix.lower() not in (".jpg", ".jpeg"):
        sys.exit("ERROR: Telegram requires a .jpg image for a static profile photo")

    me = requests.get(f"{API}/bot{token}/getMe", timeout=30).json()
    if not me.get("ok"):
        sys.exit(f"ERROR: token rejected by Telegram: {me}")
    bot = me["result"]
    print(f"Bot: @{bot.get('username')}  ({bot.get('first_name')})")

    with photo_path.open("rb") as fh:
        resp = requests.post(
            f"{API}/bot{token}/setMyProfilePhoto",
            data={"photo": '{"type":"static","photo":"attach://avatar"}'},
            files={"avatar": (photo_path.name, fh, "image/jpeg")},
            timeout=60,
        ).json()

    if resp.get("ok"):
        print(f"SUCCESS: avatar updated for @{bot.get('username')}")
        print("Open the bot chat in Telegram to see the new picture.")
    else:
        sys.exit(f"FAILED: {resp}")


if __name__ == "__main__":
    main()
