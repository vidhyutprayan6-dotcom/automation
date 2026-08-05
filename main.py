#!/usr/bin/env python3
"""
BlackBird silent UI agent — macOS profile-creation automation.

BlackBird (Electron) rejects instant programmatic clicks because it monitors
mouse acceleration, speed, and trajectory. This script moves the cursor along
randomized cubic Bezier curves so Quartz Event Tap sees human-like motion,
then clicks. UI targets are found via OpenCV template matching on rendered
pixels (HTML/CSS), not AppleScript/System Events.
"""

from __future__ import annotations

import argparse
import math
import os
import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

import numpy as np
import pyautogui

# OpenCV is pulled in by opencv-python; confidence= matching needs it installed.
try:
    import cv2  # noqa: F401
except ImportError as exc:
    raise SystemExit(
        "opencv-python is not installed. Activate the venv and run:\n"
        "  pip install -r requirements.txt\n"
        f"Original error: {exc}"
    ) from exc

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
APP_PATH = "/Applications/BlackBird.app"

# Cropped template images (place next to this script)
IMG_NEW_PROFILE = SCRIPT_DIR / "new_profile.png"
IMG_HTTP_TAB = SCRIPT_DIR / "http_tab.png"
IMG_PROXY_INPUT = SCRIPT_DIR / "proxy_input.png"
IMG_CREATE_PROFILE = SCRIPT_DIR / "create_profile.png"

DEFAULT_CONFIDENCE = 0.9
LOCATE_RETRIES = 3
LOCATE_RETRY_WAIT = 1.0
APP_LAUNCH_WAIT = (3.0, 5.0)  # seconds after open, before first locate

# Never let pyautogui abort mid-curve on corner fail-safe during automation
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.05


# ---------------------------------------------------------------------------
# Human-like pauses / typing
# ---------------------------------------------------------------------------

def human_pause(lo: float = 0.8, hi: float = 1.8) -> None:
    """Simulate an operator glancing at the UI between major actions."""
    time.sleep(random.uniform(lo, hi))


def human_type(text: str) -> None:
    """Type character-by-character; never paste (BlackBird may flag paste)."""
    interval = random.uniform(0.05, 0.15)
    pyautogui.write(text, interval=interval)


# ---------------------------------------------------------------------------
# Bezier mouse movement (anti-detect core)
# ---------------------------------------------------------------------------

def _cubic_bezier(
    t: float,
    p0: Tuple[float, float],
    p1: Tuple[float, float],
    p2: Tuple[float, float],
    p3: Tuple[float, float],
) -> Tuple[float, float]:
    """Evaluate a cubic Bezier at parameter t in [0, 1]."""
    u = 1.0 - t
    x = (
        (u ** 3) * p0[0]
        + 3 * (u ** 2) * t * p1[0]
        + 3 * u * (t ** 2) * p2[0]
        + (t ** 3) * p3[0]
    )
    y = (
        (u ** 3) * p0[1]
        + 3 * (u ** 2) * t * p1[1]
        + 3 * u * (t ** 2) * p2[1]
        + (t ** 3) * p3[1]
    )
    return x, y


def _random_control_points(
    start: Tuple[float, float],
    end: Tuple[float, float],
) -> Tuple[Tuple[float, float], Tuple[float, float]]:
    """
    Generate two control points near the start→end segment with random
    perpendicular offset so each path has a unique arc (never a straight line).
    """
    sx, sy = start
    ex, ey = end
    dx, dy = ex - sx, ey - sy
    dist = math.hypot(dx, dy) or 1.0
    # Unit perpendicular for “human” curve bow
    nx, ny = -dy / dist, dx / dist
    bow = random.uniform(0.15, 0.45) * dist
    bow *= random.choice([-1.0, 1.0])

    t1 = random.uniform(0.25, 0.45)
    t2 = random.uniform(0.55, 0.75)
    # Slight along-path jitter so acceleration profile isn't identical every run
    j1 = random.uniform(-0.08, 0.08) * dist
    j2 = random.uniform(-0.08, 0.08) * dist

    p1 = (
        sx + dx * t1 + nx * bow + (dx / dist) * j1,
        sy + dy * t1 + ny * bow + (dy / dist) * j1,
    )
    # Second control often bows the other way slightly (S-ish human paths)
    bow2 = bow * random.uniform(-0.6, 0.4)
    p2 = (
        sx + dx * t2 + nx * bow2 + (dx / dist) * j2,
        sy + dy * t2 + ny * bow2 + (dy / dist) * j2,
    )
    return p1, p2


def move_mouse_humanly(
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
    duration: Optional[float] = None,
) -> None:
    """
    Move the OS cursor from (start) → (end) along a cubic Bezier curve.

    WHY BEZIER (read this later):
    BlackBird's bot-detection watches Quartz / Chromium input for unnatural
    mouse signals: zero travel time, perfect straight lines, constant velocity,
    and instant teleport-clicks (e.g. pyautogui.click(x, y) with no prior path).
    Instant coordinate jumps are discarded. A Bezier with randomized control
    points produces varying trajectory, speed, and acceleration — the same
    class of motion a real hand produces — so the Event Tap / anti-detect
    layer accepts the subsequent click as human.

    Implementation notes:
    - Uses pyautogui.moveTo which on macOS posts real CGEvent mouse moves
      (not a DirectInput inject; pydirectinput is Windows-only).
    - Duration is randomized in [0.3, 0.8] s unless overridden.
    - Ease-in/ease-out sampling so velocity is not linear along the path.
    """
    if duration is None:
        duration = random.uniform(0.3, 0.8)

    start = (float(start_x), float(start_y))
    end = (float(end_x), float(end_y))
    p1, p2 = _random_control_points(start, end)

    # More steps for longer paths; keep frame-ish cadence (~120–180 Hz feel)
    dist = math.hypot(end[0] - start[0], end[1] - start[1])
    steps = max(25, min(90, int(dist / 4) + random.randint(20, 35)))

    # Smoothstep ease so start/end are slower (human micro-corrections)
    ts = np.linspace(0.0, 1.0, steps + 1, dtype=np.float64)[1:]
    eased = ts * ts * (3.0 - 2.0 * ts)

    t0 = time.perf_counter()
    for i, u in enumerate(eased, start=1):
        x, y = _cubic_bezier(float(u), start, p1, p2, end)
        # Tiny orthogonal noise (~sub-pixel to 1.5px) mimics tremor
        x += random.uniform(-0.6, 0.6)
        y += random.uniform(-0.6, 0.6)
        pyautogui.moveTo(int(round(x)), int(round(y)), _pause=False)

        # Pace remaining time so total ≈ duration
        target = t0 + duration * (i / steps)
        now = time.perf_counter()
        delay = target - now
        if delay > 0:
            time.sleep(delay)

    # Snap exactly onto target (no leftover float drift)
    pyautogui.moveTo(int(round(end_x)), int(round(end_y)), _pause=False)


def human_click_at(x: float, y: float) -> None:
    """Bezier-move to (x, y), then click — never teleport-click."""
    cur_x, cur_y = pyautogui.position()
    move_mouse_humanly(cur_x, cur_y, x, y)
    # Click immediately after the curve completes (required by anti-detect flow)
    pyautogui.click()


# ---------------------------------------------------------------------------
# Image recognition with retries
# ---------------------------------------------------------------------------

def locate_on_screen(
    image_path: Path,
    confidence: float = DEFAULT_CONFIDENCE,
    retries: int = LOCATE_RETRIES,
    retry_wait: float = LOCATE_RETRY_WAIT,
) -> Optional[Tuple[int, int]]:
    """
    Find the center of `image_path` on screen via OpenCV template matching
    (pyautogui.locateCenterOnScreen + confidence). Retries on miss.
    """
    if not image_path.is_file():
        print(f"[ERROR] Template missing: {image_path}")
        return None

    last_err: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            point = pyautogui.locateCenterOnScreen(
                str(image_path),
                confidence=confidence,
            )
            if point is not None:
                return int(point.x), int(point.y)
            print(
                f"[WARN] locate miss ({attempt}/{retries}): {image_path.name}"
            )
        except Exception as exc:  # noqa: BLE001 — surface CV / screen errors
            last_err = exc
            print(
                f"[WARN] locate error ({attempt}/{retries}) "
                f"{image_path.name}: {exc}"
            )
        if attempt < retries:
            time.sleep(retry_wait)

    if last_err:
        print(f"[ERROR] Failed to locate {image_path.name}: {last_err}")
    else:
        print(
            f"[ERROR] Failed to locate {image_path.name} "
            f"after {retries} attempts (confidence={confidence})"
        )
    return None


def locate_and_click(
    image_path: Path,
    label: str,
    confidence: float = DEFAULT_CONFIDENCE,
) -> bool:
    """Locate a template, human-move + click. Returns False on failure."""
    print(f"[INFO] Looking for: {label} ({image_path.name})")
    center = locate_on_screen(image_path, confidence=confidence)
    if center is None:
        return False
    x, y = center
    print(f"[INFO] Found {label} at ({x}, {y}) — moving humanly + click")
    human_click_at(x, y)
    return True


# ---------------------------------------------------------------------------
# App launch
# ---------------------------------------------------------------------------

def launch_blackbird() -> None:
    """Launch BlackBird via macOS `open` (subprocess)."""
    if not Path(APP_PATH).exists():
        print(f"[ERROR] App not found at {APP_PATH}")
        sys.exit(1)
    print(f"[INFO] Launching {APP_PATH}")
    subprocess.Popen(["open", APP_PATH])  # noqa: S603 — fixed app path
    human_pause(*APP_LAUNCH_WAIT)


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------

def clear_field_macos() -> None:
    """Select-all then delete (Cmd+A, Backspace) — macOS Electron field clear."""
    pyautogui.hotkey("command", "a")
    time.sleep(random.uniform(0.08, 0.2))
    pyautogui.press("backspace")
    time.sleep(random.uniform(0.1, 0.25))


def create_profile(proxy: str, confidence: float = DEFAULT_CONFIDENCE) -> int:
    """
    Full New-profile flow:
      New profile → HTTP tab → proxy field → type proxy → Create profile
    Returns 0 on success, 1 on failure.
    """
    launch_blackbird()
    human_pause()

    # 1) New profile
    if not locate_and_click(IMG_NEW_PROFILE, "New profile", confidence):
        return 1
    human_pause()

    # 2) Wait for slide-out modal, then HTTP tab
    # Extra short wait so the modal animation finishes before matching
    time.sleep(random.uniform(0.5, 1.0))
    if not locate_and_click(IMG_HTTP_TAB, "HTTP tab", confidence):
        return 1
    human_pause()

    # 3) Proxy input field — click, clear, type
    if not locate_and_click(IMG_PROXY_INPUT, "Proxy input field", confidence):
        return 1
    human_pause(0.4, 0.9)
    clear_field_macos()
    print(f"[INFO] Typing proxy ({len(proxy)} chars) with human intervals")
    human_type(proxy)
    human_pause()

    # 4) Create profile
    if not locate_and_click(IMG_CREATE_PROFILE, "Create profile", confidence):
        return 1

    print("[INFO] Profile creation flow completed successfully.")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="BlackBird human-like profile creation (Bezier + CV)."
    )
    p.add_argument(
        "--proxy",
        default=os.environ.get("BLACKBIRD_PROXY", "user:pass@host:port"),
        help="Proxy string user:pass@host:port "
        "(or set BLACKBIRD_PROXY). Default: user:pass@host:port",
    )
    p.add_argument(
        "--confidence",
        type=float,
        default=DEFAULT_CONFIDENCE,
        help="OpenCV template match confidence (default: 0.9)",
    )
    return p.parse_args()


def main() -> None:
    if sys.platform != "darwin":
        print(
            "[WARN] This script is designed for macOS (Quartz / Accessibility). "
            f"Current platform: {sys.platform}"
        )

    args = parse_args()

    missing = [
        name
        for name, path in (
            ("new_profile.png", IMG_NEW_PROFILE),
            ("http_tab.png", IMG_HTTP_TAB),
            ("proxy_input.png", IMG_PROXY_INPUT),
            ("create_profile.png", IMG_CREATE_PROFILE),
        )
        if not path.is_file()
    ]
    if missing:
        print(
            "[ERROR] Missing template image(s) in script directory:\n  - "
            + "\n  - ".join(missing)
        )
        print(f"Expected directory: {SCRIPT_DIR}")
        sys.exit(1)

    # Bring focus / screen size sanity
    w, h = pyautogui.size()
    print(f"[INFO] Screen size: {w}x{h}")
    print(f"[INFO] Templates dir: {SCRIPT_DIR}")
    print(f"[INFO] Proxy: {args.proxy}")

    code = create_profile(args.proxy, confidence=args.confidence)
    sys.exit(code)


if __name__ == "__main__":
    main()
