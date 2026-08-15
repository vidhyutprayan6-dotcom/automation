#!/bin/bash
# Double-click this file on macOS to start the Telegram bot.
# The window stays open on success OR failure so you can read the message.
cd "$(dirname "$0")" || exit 1

echo "=============================================="
echo " BlackBird Telegram Bot"
echo "=============================================="
echo ""

# Called through bash rather than ./run_bot.sh, so a lost executable bit — from
# an unzip, an AnyDesk copy, or a checkout made on Windows — cannot stop it.
bash run_bot.sh
STATUS=$?

echo ""
if [[ $STATUS -ne 0 ]]; then
  echo "Bot exited with error code: $STATUS"
  echo "Common causes:"
  echo "  1) Project files are outdated — copy latest main.py / telegram_bot.py / run_bot.sh"
  echo "  2) Missing .env with TELEGRAM_BOT_TOKEN=..."
  echo "  3) Not running on macOS"
else
  echo "Bot process ended (code 0)."
fi
echo ""
read -r -p "Press Enter to close this window..."
exit "$STATUS"
