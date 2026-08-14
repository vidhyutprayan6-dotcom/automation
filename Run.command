#!/bin/bash
cd "$(dirname "$0")"
chmod +x run.sh 2>/dev/null || true
echo "If the mouse does not move:"
echo "  System Settings → Privacy & Security → Accessibility"
echo "  Enable Terminal (and BlackBirdAutomation if using dist/)"
echo ""
./run.sh
echo ""
read -r -p "Press Enter to close..."
