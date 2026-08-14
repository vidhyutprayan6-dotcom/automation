#!/usr/bin/env bash
# Start from Terminal:  chmod +x run.sh && ./run.sh
# Or double-click Run.command
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

echo "=============================================="
echo " BlackBird Automation"
echo "=============================================="

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "ERROR: This tool runs on macOS only."
  exit 1
fi

for f in main.py requirements.txt data.txt card.txt email.txt; do
  if [[ ! -f "$f" ]]; then
    echo "ERROR: missing $f"
    exit 1
  fi
done

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not found."
  exit 1
fi

if [[ ! -x ".venv/bin/python" ]]; then
  echo "First run: creating Python environment..."
  python3 -m venv .venv
fi

echo "Installing dependencies..."
.venv/bin/python -m pip install --upgrade pip setuptools wheel >/dev/null
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -c "import cv2, pyautogui, numpy, pyperclip, Quartz; print('Ready.')"

echo "Starting in 3 seconds — leave BlackBird visible."
sleep 1; echo "2..."; sleep 1; echo "1..."; sleep 1
exec .venv/bin/python main.py "$@"
