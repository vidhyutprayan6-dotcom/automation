#!/usr/bin/env python3
"""
BlackBird silent UI agent — macOS profile-creation automation.

BlackBird rejects instant teleport-clicks (acceleration / trajectory checks).
We move along randomized cubic Bezier curves, then click.

Primary targeting: fixed coordinates calibrated from a 1920x1080 full-screen
screenshot. Template matching is only a fallback for targets without coords yet.
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
from typing import Dict, Optional, Tuple

import numpy as np
import pyautogui

try:
    import cv2  # noqa: F401
except ImportError as exc:
    raise SystemExit(
        "opencv-python is not installed. Activate the venv and run:\n"
        "  .venv/bin/python -m pip install -r requirements.txt\n"
        f"Original error: {exc}"
    ) from exc

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
APP_PATH = "/Applications/BlackBird.app"

# Screenshot calibration baseline (your Mac reported 1920x1080)
BASE_SCREEN = (1920, 1080)

# Absolute click targets on BASE_SCREEN (1920x1080), from your marked screenshots.
COORDS: Dict[str, Optional[Tuple[int, int]]] = {
    "new_profile": (1455, 104),      # + New profile (top-right toolbar)
    "new_proxy": (1409, 287),        # Connection → New Proxy
    "proxy_input": (1274, 381),      # user:pass@host:port field
    "create_profile": (1451, 738),   # Create profile (bottom-right)
    "open_profile": (906, 201),      # top-row proxy refresh/open icon
}

DATA_FILE = SCRIPT_DIR / "data.txt"

IMG_NEW_PROFILE = SCRIPT_DIR / "new_profile.png"
IMG_NEW_PROXY = SCRIPT_DIR / "http_tab.png"
IMG_PROXY_INPUT = SCRIPT_DIR / "proxy_input.png"
IMG_CREATE_PROFILE = SCRIPT_DIR / "create_profile.png"

TEMPLATES = {
    "new_profile": IMG_NEW_PROFILE,
    "new_proxy": IMG_NEW_PROXY,
    "proxy_input": IMG_PROXY_INPUT,
    "create_profile": IMG_CREATE_PROFILE,
    "open_profile": None,
}

DEFAULT_CONFIDENCE = 0.9
LOCATE_RETRIES = 3
LOCATE_RETRY_WAIT = 1.0
APP_LAUNCH_WAIT = (3.0, 5.0)

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.05


# ---------------------------------------------------------------------------
# Human-like pauses / typing
# ---------------------------------------------------------------------------

def human_pause(lo: float = 0.8, hi: float = 1.8) -> None:
    time.sleep(random.uniform(lo, hi))


def human_type(text: str) -> None:
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
    WHY BEZIER: BlackBird watches mouse path / speed / acceleration.
    Instant pyautogui.click(x, y) teleports are discarded. A curved path with
    random control points looks like a real hand, so the click is accepted.
    """
    sx, sy = start
    ex, ey = end
    dx, dy = ex - sx, ey - sy
    dist = math.hypot(dx, dy) or 1.0
    nx, ny = -dy / dist, dx / dist
    bow = random.uniform(0.15, 0.45) * dist * random.choice([-1.0, 1.0])
    t1, t2 = random.uniform(0.25, 0.45), random.uniform(0.55, 0.75)
    j1, j2 = random.uniform(-0.08, 0.08) * dist, random.uniform(-0.08, 0.08) * dist
    p1 = (
        sx + dx * t1 + nx * bow + (dx / dist) * j1,
        sy + dy * t1 + ny * bow + (dy / dist) * j1,
    )
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
    if duration is None:
        duration = random.uniform(0.3, 0.8)

    start = (float(start_x), float(start_y))
    end = (float(end_x), float(end_y))
    p1, p2 = _random_control_points(start, end)

    dist = math.hypot(end[0] - start[0], end[1] - start[1])
    steps = max(25, min(90, int(dist / 4) + random.randint(20, 35)))
    ts = np.linspace(0.0, 1.0, steps + 1, dtype=np.float64)[1:]
    eased = ts * ts * (3.0 - 2.0 * ts)

    t0 = time.perf_counter()
    for i, u in enumerate(eased, start=1):
        x, y = _cubic_bezier(float(u), start, p1, p2, end)
        x += random.uniform(-0.6, 0.6)
        y += random.uniform(-0.6, 0.6)
        pyautogui.moveTo(int(round(x)), int(round(y)), _pause=False)
        target = t0 + duration * (i / steps)
        delay = target - time.perf_counter()
        if delay > 0:
            time.sleep(delay)

    pyautogui.moveTo(int(round(end_x)), int(round(end_y)), _pause=False)


def human_click_at(x: float, y: float) -> None:
    cur_x, cur_y = pyautogui.position()
    move_mouse_humanly(cur_x, cur_y, x, y)
    pyautogui.click()


# ---------------------------------------------------------------------------
# Coordinates + optional template fallback
# ---------------------------------------------------------------------------

def scale_point(x: int, y: int) -> Tuple[int, int]:
    """Map a point from BASE_SCREEN (1920x1080) onto the current screen size."""
    sw, sh = pyautogui.size()
    bw, bh = BASE_SCREEN
    if (sw, sh) == (bw, bh):
        return x, y
    sx = sw / bw
    sy = sh / bh
    return int(round(x * sx)), int(round(y * sy))


def locate_on_screen(
    image_path: Path,
    confidence: float = DEFAULT_CONFIDENCE,
    retries: int = LOCATE_RETRIES,
    retry_wait: float = LOCATE_RETRY_WAIT,
) -> Optional[Tuple[int, int]]:
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
            print(f"[WARN] locate miss ({attempt}/{retries}): {image_path.name}")
        except Exception as exc:  # noqa: BLE001
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


def click_target(
    key: str,
    label: str,
    confidence: float = DEFAULT_CONFIDENCE,
    coords_only: bool = False,
) -> bool:
    """
    Click a UI target:
      1) calibrated coordinates (preferred — what you asked for)
      2) template match fallback if coords are None / coords_only is False
    """
    activate_blackbird()
    human_pause(0.4, 0.8)

    base = COORDS.get(key)
    if base is not None:
        x, y = scale_point(*base)
        print(
            f"[INFO] {label}: coordinate click at ({x}, {y}) "
            f"[calibrated {base} on {BASE_SCREEN[0]}x{BASE_SCREEN[1]}]"
        )
        human_click_at(x, y)
        return True

    if coords_only:
        print(
            f"[ERROR] No calibrated coordinates for '{key}'. "
            "Send a full 1920x1080 screenshot with that control marked."
        )
        return False

    template = TEMPLATES.get(key)
    if template is None:
        print(f"[ERROR] Unknown target key: {key}")
        return False

    print(f"[INFO] Looking for: {label} via template ({template.name})")
    center = locate_on_screen(template, confidence=confidence)
    if center is None:
        return False
    x, y = center
    print(f"[INFO] Found {label} at ({x}, {y}) — moving humanly + click")
    human_click_at(x, y)
    return True


# ---------------------------------------------------------------------------
# App launch / focus
# ---------------------------------------------------------------------------

def activate_blackbird() -> None:
    """Bring BlackBird to the front so clicks hit its window, not VS Code."""
    subprocess.run(
        ["osascript", "-e", 'tell application "BlackBird" to activate'],
        check=False,
        capture_output=True,
    )
    time.sleep(0.4)


def launch_blackbird() -> None:
    if not Path(APP_PATH).exists():
        print(f"[ERROR] App not found at {APP_PATH}")
        sys.exit(1)
    print(f"[INFO] Launching {APP_PATH}")
    subprocess.Popen(["open", APP_PATH])  # noqa: S603
    human_pause(*APP_LAUNCH_WAIT)
    activate_blackbird()
    print(
        "[INFO] BlackBird should be in front. "
        "Do not cover it with VS Code / Terminal while the script runs."
    )
    # Short countdown so you can Alt-Tab if needed
    for sec in (3, 2, 1):
        print(f"[INFO] Starting in {sec}...")
        time.sleep(1)


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------

def clear_field_macos() -> None:
    pyautogui.hotkey("command", "a")
    time.sleep(random.uniform(0.08, 0.2))
    pyautogui.press("backspace")
    time.sleep(random.uniform(0.1, 0.25))


def load_proxies(path: Path) -> list[str]:
    """
    Read proxy lines from data.txt (one proxy per line).
    Accepts: user:pass@host:port
    Skips blank lines and lines that do not look like proxies.
    """
    if not path.is_file():
        raise FileNotFoundError(f"data file not found: {path}")

    proxies: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Proxy shape: user:pass@host:port
        if "@" in line and line.count(":") >= 2:
            proxies.append(line)
        else:
            print(f"[WARN] Skipping non-proxy line in {path.name}: {line[:48]}...")
    return proxies


def run_one_profile(
    proxy: str,
    index: int,
    total: int,
    confidence: float = DEFAULT_CONFIDENCE,
    coords_only: bool = True,
    launch: bool = False,
) -> bool:
    """
    One complete workflow:
      New profile → New Proxy → type proxy from data.txt → Create profile
      → click top-row open/refresh icon (verify browser opens)
    """
    print(f"[INFO] === Workflow {index}/{total} ===")
    print(f"[INFO] Proxy: {proxy}")

    if launch:
        launch_blackbird()
    else:
        activate_blackbird()
    human_pause()

    # 1) New profile
    if not click_target("new_profile", "New profile", confidence, coords_only):
        return False
    human_pause()
    time.sleep(random.uniform(0.9, 1.5))

    # 2) New Proxy
    if not click_target("new_proxy", "New Proxy", confidence, coords_only):
        return False
    human_pause()
    time.sleep(random.uniform(0.5, 1.0))

    # 3) Proxy input from data.txt
    if not click_target("proxy_input", "Proxy input field", confidence, coords_only):
        return False
    human_pause(0.4, 0.9)
    clear_field_macos()
    print(f"[INFO] Typing proxy from data.txt ({len(proxy)} chars)")
    human_type(proxy)
    human_pause()

    # 4) Create profile (coordinates unchanged — already working)
    if not click_target("create_profile", "Create profile", confidence, coords_only):
        return False
    human_pause()
    time.sleep(random.uniform(1.2, 2.0))  # list refresh; new row at top

    # 5) Open/activate the new top-row profile (browser launch check)
    if not click_target("open_profile", "Open profile (proxy icon)", confidence, coords_only):
        return False

    print(
        "[INFO] Open-profile clicked. Waiting to observe whether the browser opens..."
    )
    time.sleep(random.uniform(4.0, 6.0))
    print(f"[INFO] Workflow {index}/{total} finished.")
    return True


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="BlackBird profile workflow (data.txt + coordinate Bezier clicks)."
    )
    p.add_argument(
        "--data",
        default=str(DATA_FILE),
        help="Path to data.txt (one proxy per line). Default: ./data.txt",
    )
    p.add_argument(
        "--proxy",
        default=None,
        help="Optional single proxy override (skips data.txt)",
    )
    p.add_argument(
        "--confidence",
        type=float,
        default=DEFAULT_CONFIDENCE,
        help="Template-match confidence when falling back to images",
    )
    p.add_argument(
        "--coords-only",
        action="store_true",
        default=True,
        help="Use calibrated coordinates only (default: on)",
    )
    p.add_argument(
        "--allow-templates",
        action="store_true",
        help="Fall back to PNG template matching if a coord is missing",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process only the first N proxies (0 = all)",
    )
    return p.parse_args()


def main() -> None:
    if sys.platform != "darwin":
        print(
            "[WARN] Designed for macOS. "
            f"Current platform: {sys.platform}"
        )

    args = parse_args()
    coords_only = not args.allow_templates
    w, h = pyautogui.size()
    print(f"[INFO] Screen size: {w}x{h} (coords calibrated for {BASE_SCREEN[0]}x{BASE_SCREEN[1]})")
    print(f"[INFO] Script dir: {SCRIPT_DIR}")
    for key, pt in COORDS.items():
        print(f"[INFO] Coord {key}: {pt}")

    if args.proxy:
        proxies = [args.proxy.strip()]
        print("[INFO] Using --proxy override (1 entry)")
    else:
        data_path = Path(args.data)
        try:
            proxies = load_proxies(data_path)
        except FileNotFoundError as exc:
            print(f"[ERROR] {exc}")
            sys.exit(1)
        print(f"[INFO] Loaded {len(proxies)} proxy line(s) from {data_path}")

    if not proxies:
        print("[ERROR] No proxy lines found. Put user:pass@host:port lines in data.txt")
        sys.exit(1)

    if args.limit and args.limit > 0:
        proxies = proxies[: args.limit]

    total = len(proxies)
    ok = 0
    for i, proxy in enumerate(proxies, start=1):
        success = run_one_profile(
            proxy,
            index=i,
            total=total,
            confidence=args.confidence,
            coords_only=coords_only,
            launch=(i == 1),
        )
        if success:
            ok += 1
        else:
            print(f"[ERROR] Workflow {i}/{total} failed; stopping.")
            break
        if i < total:
            human_pause(1.0, 2.0)

    print(f"[INFO] Done. Successful workflows: {ok}/{total}")
    sys.exit(0 if ok == total else 1)


if __name__ == "__main__":
    main()
