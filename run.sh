#!/usr/bin/env bash
#
# BlackBird Automation launcher (macOS).
#
#   Terminal:      bash run.sh
#   Double-click:  Run.command
#
# "bash run.sh" is the documented form on purpose: it works even when the
# executable bit did not survive the trip to this machine, which happens with
# an unzip, an AnyDesk copy, or a checkout made on Windows. "./run.sh" needs
# that bit and fails with "permission denied" without it.
#
# Written for the bash 3.2 that ships with macOS, so: no empty-array
# expansions and no bare "$@" under "set -u" — both abort on that version.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 1
cd "$ROOT" || exit 1

PYTHON=".venv/bin/python"
IMPORTS="import cv2, numpy, pyautogui, pyperclip, Quartz, stripe_vision"

die() {
  echo ""
  echo "=============================================="
  echo " STOPPED"
  echo "=============================================="
  while [[ $# -gt 0 ]]; do
    echo " $1"
    shift
  done
  echo ""
  exit 1
}

echo "=============================================="
echo " BlackBird Automation"
echo "=============================================="
echo " Folder: $ROOT"
echo ""

# --- 1. macOS only ----------------------------------------------------------
SYSTEM="$(uname -s 2>/dev/null || echo unknown)"
if [[ "$SYSTEM" != "Darwin" ]]; then
  die "This tool controls the macOS interface, so it only runs on a Mac." \
      "" \
      "This machine reports: $SYSTEM"
fi

# --- 2. project files -------------------------------------------------------
MISSING=""
for f in main.py stripe_vision.py requirements.txt data.txt card.txt email.txt; do
  if [[ ! -f "$f" ]]; then
    MISSING="$MISSING $f"
  fi
done
if [[ -n "$MISSING" ]]; then
  die "These project files are missing from the folder above:" \
      "  $MISSING" \
      "" \
      "Copy the whole project folder across, not just a few files."
fi

# --- 3. a working python3 ---------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
  die "python3 is not installed on this Mac." \
      "" \
      "Install Apple's developer tools, then run this again:" \
      "  xcode-select --install"
fi
if ! python3 -c "import sys" >/dev/null 2>&1; then
  # /usr/bin/python3 can exist as a stub that only prompts to install the
  # Command Line Tools; it passes "command -v" but cannot actually run.
  die "python3 was found but cannot run yet." \
      "" \
      "Finish the developer tools install, then run this again:" \
      "  xcode-select --install"
fi

# --- 4. the private environment --------------------------------------------
if [[ ! -x "$PYTHON" ]] || ! "$PYTHON" -c "import sys" >/dev/null 2>&1; then
  echo "Creating the Python environment (first run only)..."
  rm -rf .venv
  if ! python3 -m venv .venv; then
    die "Could not create the Python environment in:" \
        "  $ROOT/.venv" \
        "" \
        "Check the folder is writable, then run this again."
  fi
fi

# --- 5. dependencies --------------------------------------------------------
# Installing is skipped when everything already imports, so a repeat run starts
# in seconds instead of re-checking every package with pip.
if "$PYTHON" -c "$IMPORTS" >/dev/null 2>&1; then
  echo "Dependencies already installed."
else
  echo "Installing dependencies (first run takes a few minutes)..."
  "$PYTHON" -m pip install --upgrade pip setuptools wheel >/dev/null 2>&1 || true
  if ! "$PYTHON" -m pip install -r requirements.txt; then
    die "Could not install the required Python packages." \
        "" \
        "This is almost always no internet connection on this Mac." \
        "Check the connection and run this again."
  fi
fi

if ! "$PYTHON" -c "$IMPORTS; print(' All components loaded.')"; then
  die "A required component still will not load (see the error above)." \
      "" \
      "Try a clean rebuild:" \
      "  rm -rf .venv && bash run.sh"
fi

# --- 6. go ------------------------------------------------------------------
echo ""
echo "Starting in 3 seconds — leave BlackBird visible on screen."
sleep 1; echo " 2..."
sleep 1; echo " 1..."
sleep 1
echo ""

# Split on $# because bash 3.2 treats "$@" as unset when there are no
# arguments and "set -u" is on.
if [[ $# -gt 0 ]]; then
  exec "$PYTHON" main.py "$@"
fi
exec "$PYTHON" main.py
