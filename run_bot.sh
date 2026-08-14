#!/usr/bin/env bash
# Start the Telegram controller (keeps running; use /start /stop /status in Telegram)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "ERROR: Automation + bot are intended for macOS VPS."
  exit 1
fi

if [[ ! -f .env ]]; then
  echo "ERROR: missing .env — copy .env.example to .env and set TELEGRAM_BOT_TOKEN."
  exit 1
fi

if ! grep -qE '^TELEGRAM_BOT_TOKEN=.+' .env 2>/dev/null; then
  echo "ERROR: set TELEGRAM_BOT_TOKEN in .env"
  exit 1
fi

if [[ ! -f telegram_bot.py ]]; then
  echo "ERROR: telegram_bot.py missing in $ROOT"
  exit 1
fi

# Prove this folder has the 5-button UI before starting
for needle in 'KeyboardButton("/card")' 'KeyboardButton("/proxy")' 'v2026-08-14-summary'; do
  if ! grep -qF "$needle" telegram_bot.py; then
    echo "ERROR: telegram_bot.py is outdated (missing: $needle)"
    echo "Pull/copy the latest project files into: $ROOT"
    exit 1
  fi
done

echo "[run_bot] Project: $ROOT"
echo "[run_bot] UI check OK — buttons: /status /start /card /proxy /stop"

if [[ ! -x .venv/bin/python ]]; then
  echo "Creating .venv ..."
  python3 -m venv .venv
fi

echo "Installing dependencies (includes python-telegram-bot)..."
.venv/bin/python -m pip install --upgrade pip >/dev/null
.venv/bin/python -m pip install -r requirements.txt

mkdir -p logs

# Stop any older telegram_bot.py using the same file (avoid two bots / stale UI)
if command -v pgrep >/dev/null 2>&1; then
  OLD_PIDS="$(pgrep -f "[p]ython.*telegram_bot.py" || true)"
  if [[ -n "${OLD_PIDS}" ]]; then
    echo "[run_bot] Stopping old telegram_bot.py process(es): $OLD_PIDS"
    # shellcheck disable=SC2086
    kill $OLD_PIDS 2>/dev/null || true
    sleep 1
  fi
fi

echo "Starting Telegram bot (Ctrl+C to stop the bot only; use /stop for automation)..."
echo "After start, open Telegram and send /start — you must see UI v2026-08-14-summary"
exec .venv/bin/python telegram_bot.py
