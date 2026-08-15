#!/bin/bash
# Double-click this file on macOS to start the automation.
# The window stays open on success OR failure so the message can be read.
cd "$(dirname "$0")" || exit 1

echo "=============================================="
echo " BlackBird Automation"
echo "=============================================="
echo ""
echo "If the mouse does not move once it starts:"
echo "  System Settings -> Privacy & Security -> Accessibility"
echo "  switch Terminal on, then run this again."
echo ""

# Called through bash rather than ./run.sh, so a lost executable bit — from an
# unzip, an AnyDesk copy, or a checkout made on Windows — cannot stop it.
bash run.sh
STATUS=$?

echo ""
if [[ $STATUS -ne 0 ]]; then
  echo "=============================================="
  echo " Automation stopped with error code: $STATUS"
  echo "=============================================="
  echo "Read the message above — it says what to fix."
fi
echo ""
read -r -p "Press Enter to close this window..."
exit "$STATUS"
