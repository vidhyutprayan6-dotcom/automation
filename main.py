#!/usr/bin/env python3
"""
BlackBird STATE-A UI agent — macOS end-to-end workflow automation.

Workflow count is driven ONLY by data.txt: one proxy line → one workflow
(top → bottom). Cards (card.txt) and emails (email.txt) are paired to each
proxy cyclically — lengths may differ; pairing wraps with index % len(...).

One workflow = New profile → proxy → Refresh → Play → if enabled →
Continue without → Stripe URL → email+card → Pay; if inactive → skip
Stripe and continue with the next data.txt proxy.

STATE A is the frozen calibration restored on recoverable failure.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pyautogui
import pyperclip

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

def _resolve_script_dir() -> Path:
    """
    Folder that holds editable data.txt / card.txt / email.txt.

    Source run  → directory of main.py
    Frozen bin  → directory of the executable
    Frozen .app → folder that contains Xxx.app (so clients can edit data beside the app)
    """
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parent

    exe = Path(sys.executable).resolve()
    # .../BlackBirdAutomation.app/Contents/MacOS/<binary>
    try:
        if (
            exe.parent.name == "MacOS"
            and exe.parent.parent.name == "Contents"
            and exe.parents[2].suffix == ".app"
        ):
            app_parent = exe.parents[3]
            if (app_parent / "data.txt").is_file():
                return app_parent
            resources = exe.parent.parent / "Resources"
            if (resources / "data.txt").is_file():
                return resources
            return app_parent
    except IndexError:
        pass
    return exe.parent


SCRIPT_DIR = _resolve_script_dir()
APP_PATH = "/Applications/BlackBird.app"
BASE_SCREEN = (1920, 1080)

# Two remembered layouts (1920x1080). Auto-picked from BlackBird window position.
# legacy_centered = original wide/centered window
# top_left        = window pinned near top-left (current screenshots)
LAYOUTS: Dict[str, Dict[str, Tuple[int, int]]] = {
    "legacy_centered": {
        "new_profile": (1455, 104),
        "new_proxy": (1409, 287),
        "proxy_input": (1274, 381),
        "create_profile": (1451, 738),
        "open_profile": (906, 201),
        "play_profile": (627, 205),
        "continue_without": (1245, 592),  # case A — centered/right System Components
        "address_bar": (970, 57),
        "dismiss_ok": (960, 540),  # approximate center dialog (legacy)
    },
    "top_left": {
        # Recalibrated from Aug 9 screenshots (BlackBird at top-left)
        "new_profile": (1234, 70),
        # New Proxy = 3rd Connection segment (measured from Aug 10 screenshot)
        "new_proxy": (1192, 258),
        "proxy_input": (1020, 346),
        "create_profile": (1224, 703),
        "open_profile": (684, 173),   # proxy refresh icon
        "play_profile": (403, 172),   # ▶ play
        "continue_without": (920, 705),  # System Components bottom-right Continue without
        "address_bar": (970, 57),
        "dismiss_ok": (959, 395),     # "BlackBird Network is not open" → OK
    },
}

# All known "Continue without" positions (dialog moves between runs).
# Tried after modal-frame / AX click. Prefer bottom-right of System Components.
CONTINUE_WITHOUT_CANDIDATES: List[Tuple[str, Tuple[int, int]]] = [
    ("modal_br_920_705", (920, 705)),    # System Components bottom-right (common)
    ("modal_br_980_720", (980, 720)),
    ("modal_br_850_680", (850, 680)),
    ("case_C_left_lower", (404, 682)),
    ("case_B_left_mid", (287, 596)),
    ("case_A_centered", (1245, 592)),
]

ACTIVE_LAYOUT = "top_left"
LAYOUT_FORCE: Optional[str] = None  # set by --layout when not auto
COORDS: Dict[str, Optional[Tuple[int, int]]] = dict(LAYOUTS[ACTIVE_LAYOUT])

# ---------------------------------------------------------------------------
# STATE A — frozen known-good calibration (revert target on errors)
# ---------------------------------------------------------------------------
STATE_A: Dict[str, object] = {
    "layout": "top_left",
    "coords": dict(LAYOUTS["top_left"]),
    "stripe": {
        # Exact click centers (STATE A, 1920x1080 scrolled checkout)
        "email": (1093, 182),
        "card_number": (1112, 353),
        "card_expiry": (1091, 393),
        "card_cvc": (1258, 391),
        "card_name": (1118, 466),
        # Uncheck "Save my information…" before Pay (red box on screenshot)
        "save_toggle": (1052, 620),
        "pay": (1224, 823),
    },
    "continue_candidates": list(CONTINUE_WITHOUT_CANDIDATES),
    "stripe_url": "https://buy.stripe.com/fZu7sL6GT8mkdu037idnW03",
}

DATA_FILE = SCRIPT_DIR / "data.txt"
CARD_FILE = SCRIPT_DIR / "card.txt"
STRIPE_CHECKOUT_URL = str(STATE_A["stripe_url"])

DEFAULT_CONFIDENCE = 0.9
LOCATE_RETRIES = 3
LOCATE_RETRY_WAIT = 1.0
APP_LAUNCH_WAIT = (3.0, 5.0)

# Fixed delays from the approved workflow spec
DELAY_STEP = 3.0          # after New profile / New Proxy / input / refresh
DELAY_AFTER_CONTINUE = 15.0
DELAY_STRIPE_LOAD = 30.0
DELAY_BETWEEN_CARD_FIELDS = 3.0
DELAY_AFTER_STRIPE_SCROLL = 3.0  # after scroll-to-bottom, before Email click
DELAY_AFTER_PAY = 60.0           # wait after Pay for payment processing
# After Play: wait for proxy to enable (Continue modal AND/OR proxy browser).
# If neither appears → proxy inactive → skip Stripe and start next workflow.
CONTINUE_MODAL_APPEAR_TIMEOUT = 12.0

STRIPE_COORDS: Dict[str, Tuple[int, int]] = dict(STATE_A["stripe"])  # type: ignore[arg-type]

EMAIL_FILE = SCRIPT_DIR / "email.txt"
DEFAULT_EMAIL = "trioleo2947@outlook.com"
RESULTS_FILE = SCRIPT_DIR / "logs" / "last_run.json"

# Back-compat aliases used elsewhere
STRIPE_LOAD_WAIT = DELAY_STRIPE_LOAD
BROWSER_APPEAR_WAIT = DELAY_AFTER_CONTINUE

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.05


# ---------------------------------------------------------------------------
# macOS permissions + reliable mouse (Quartz)
# ---------------------------------------------------------------------------

def _open_accessibility_settings() -> None:
    """Open the Accessibility privacy pane (best-effort)."""
    urls = (
        "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
        "x-apple.systempreferences:com.apple.Settings.PrivacySecurity.Privacy_Accessibility",
    )
    for url in urls:
        try:
            subprocess.run(["open", url], check=False, capture_output=True)
            return
        except Exception:  # noqa: BLE001
            continue


def _accessibility_trusted(*, prompt: bool = False) -> bool:
    """True if this process may control the Mac (mouse / System Events)."""
    if sys.platform != "darwin":
        return False
    try:
        from ApplicationServices import (  # type: ignore[import-untyped]
            AXIsProcessTrusted,
            AXIsProcessTrustedWithOptions,
            kAXTrustedCheckOptionPrompt,
        )
        from Foundation import NSDictionary  # type: ignore[import-untyped]

        if prompt:
            opts = NSDictionary.dictionaryWithObject_forKey_(
                True, kAXTrustedCheckOptionPrompt
            )
            return bool(AXIsProcessTrustedWithOptions(opts))
        return bool(AXIsProcessTrusted())
    except Exception:  # noqa: BLE001
        # Older / minimal installs — probe via osascript
        out = subprocess.run(
            [
                "osascript",
                "-e",
                'tell application "System Events" to get name of first process',
            ],
            capture_output=True,
            text=True,
        )
        err = (out.stderr or "") + (out.stdout or "")
        if "not allowed assistive access" in err or "(-25211)" in err:
            return False
        return out.returncode == 0


def _permission_target_name() -> str:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).name
    # Running under python → grant Terminal (or iTerm) Accessibility
    term = os.environ.get("TERM_PROGRAM", "")
    if "iTerm" in term:
        return "iTerm"
    if "Apple_Terminal" in term or term == "Apple_Terminal":
        return "Terminal"
    return "Terminal (or the app hosting python3)"


def ensure_macos_input_permissions() -> None:
    """
    Hard-require Accessibility. Without it, Quartz/osascript log clicks but
    the real mouse cursor never moves (macOS silently blocks the events).
    """
    if sys.platform != "darwin":
        return

    target = _permission_target_name()
    print(f"[INFO] Checking Accessibility for: {target}")

    # First call may show the system prompt
    trusted = _accessibility_trusted(prompt=True)
    if trusted:
        print("[INFO] Accessibility: GRANTED — mouse / System Events allowed")
        return

    print("")
    print("=" * 60)
    print("[ERROR] Accessibility is NOT granted — mouse control is blocked.")
    print("[ERROR] Your log line 'osascript is not allowed assistive access'")
    print("[ERROR] means macOS ignored every click (cursor will not move).")
    print("")
    print(f"[ERROR] Fix (required): turn ON Accessibility for → {target}")
    print("[ERROR]   System Settings → Privacy & Security → Accessibility")
    print(f"[ERROR]   Enable the toggle for «{target}»")
    print("[ERROR]   Also enable Screen Recording for the same app (screenshots).")
    print("[ERROR]   Then FULLY QUIT this program and run it again.")
    print("=" * 60)
    _open_accessibility_settings()
    # Give the user a moment to see the message / Settings pane
    time.sleep(1.5)
    sys.exit(3)


def _quartz_warp(x: float, y: float) -> None:
    """Force the hardware cursor to (x, y). Requires Accessibility."""
    import Quartz

    Quartz.CGWarpMouseCursorPosition(Quartz.CGPointMake(float(x), float(y)))
    Quartz.CGAssociateMouseAndMouseCursorPosition(True)


def _post_mouse_event(event_type: int, x: float, y: float, button: int) -> None:
    """Post a mouse event to every useful tap location."""
    import Quartz

    point = Quartz.CGPointMake(float(x), float(y))
    ev = Quartz.CGEventCreateMouseEvent(None, event_type, point, button)
    if ev is None:
        raise RuntimeError(f"CGEventCreateMouseEvent failed type={event_type}")
    for tap in (
        Quartz.kCGHIDEventTap,
        Quartz.kCGSessionEventTap,
        Quartz.kCGAnnotatedSessionEventTap,
    ):
        try:
            Quartz.CGEventPost(tap, ev)
        except Exception:  # noqa: BLE001
            continue


def verify_mouse_control_or_exit() -> None:
    """Move the cursor a few pixels; abort if macOS blocked the move."""
    if sys.platform != "darwin":
        return
    import Quartz

    try:
        cur = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        x0, y0 = float(cur.x), float(cur.y)
    except Exception:  # noqa: BLE001
        x0, y0 = [float(v) for v in pyautogui.position()]

    tx, ty = x0 + 12.0, y0 + 8.0
    print(f"[INFO] mouse self-test: from ({x0:.0f},{y0:.0f}) → ({tx:.0f},{ty:.0f})")
    try:
        _quartz_warp(tx, ty)
        time.sleep(0.08)
        _post_mouse_event(
            Quartz.kCGEventMouseMoved, tx, ty, Quartz.kCGMouseButtonLeft
        )
        time.sleep(0.08)
        cur2 = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        x1, y1 = float(cur2.x), float(cur2.y)
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] mouse self-test exception: {exc}")
        x1, y1 = x0, y0

    # Restore
    try:
        _quartz_warp(x0, y0)
    except Exception:  # noqa: BLE001
        pass

    moved = abs(x1 - x0) > 2.0 or abs(y1 - y0) > 2.0
    if moved:
        print(f"[INFO] mouse self-test: OK (observed ({x1:.0f},{y1:.0f}))")
        return

    print("")
    print("[ERROR] mouse self-test FAILED — cursor did not move.")
    print("[ERROR] macOS is blocking input events (Accessibility off or stale).")
    print(f"[ERROR] Enable Accessibility for «{_permission_target_name()}», then re-run.")
    print("[ERROR] After changing the toggle: quit this app completely and start again.")
    _open_accessibility_settings()
    sys.exit(3)


# ---------------------------------------------------------------------------
# Human-like pauses / typing / paste
# ---------------------------------------------------------------------------

def human_pause(lo: float = 0.8, hi: float = 1.8) -> None:
    time.sleep(random.uniform(lo, hi))


def _release_modifier_keys() -> None:
    """Ensure Shift/Cmd/Option/Ctrl are fully up (critical between workflows)."""
    try:
        from Quartz import CGEventCreateKeyboardEvent, CGEventPost, kCGHIDEventTap
    except ImportError:
        for key in ("shift", "command", "option", "control", "fn"):
            try:
                pyautogui.keyUp(key)
            except Exception:  # noqa: BLE001
                pass
        return

    # left+right: shift, control, option, command
    for keycode in (56, 60, 59, 62, 58, 61, 55, 54):
        ev = CGEventCreateKeyboardEvent(None, keycode, False)
        CGEventPost(kCGHIDEventTap, ev)
    time.sleep(0.03)


def _quartz_left_click(x: float, y: float) -> None:
    """
    Explicit left-button down/up via Quartz at (x, y).
    Warps cursor first so the click lands even if bezier move was blocked.
    """
    import Quartz

    xf, yf = float(x), float(y)
    _quartz_warp(xf, yf)
    time.sleep(0.03)
    _post_mouse_event(Quartz.kCGEventMouseMoved, xf, yf, Quartz.kCGMouseButtonLeft)
    time.sleep(0.02)
    _post_mouse_event(Quartz.kCGEventLeftMouseDown, xf, yf, Quartz.kCGMouseButtonLeft)
    time.sleep(0.05)
    _post_mouse_event(Quartz.kCGEventLeftMouseUp, xf, yf, Quartz.kCGMouseButtonLeft)
    print(f"[INFO] Quartz left-click at ({int(round(xf))}, {int(round(yf))})")


def human_click_at(x: float, y: float) -> None:
    """Move with bezier, release modifiers, then Quartz left-click (always fires)."""
    _release_modifier_keys()
    cur_x, cur_y = pyautogui.position()
    move_mouse_humanly(cur_x, cur_y, x, y)
    _release_modifier_keys()
    time.sleep(0.05)
    try:
        _quartz_left_click(x, y)
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] Quartz click failed ({exc}); falling back to pyautogui.click")
        _release_modifier_keys()
        try:
            _quartz_warp(x, y)
        except Exception:  # noqa: BLE001
            pass
        pyautogui.click(int(round(x)), int(round(y)))
    _release_modifier_keys()


def human_move_to(x: float, y: float) -> None:
    """Bezier move only — no mouse-down (avoids blue drag-select on Stripe)."""
    _release_modifier_keys()
    cur_x, cur_y = pyautogui.position()
    move_mouse_humanly(cur_x, cur_y, x, y)
    _release_modifier_keys()


def fast_click_at(x: float, y: float) -> None:
    """Short bezier + Quartz click — used for Continue without (must be ASAP)."""
    _release_modifier_keys()
    cur_x, cur_y = pyautogui.position()
    move_mouse_humanly(cur_x, cur_y, x, y, duration=random.uniform(0.08, 0.18))
    _release_modifier_keys()
    try:
        _quartz_left_click(x, y)
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] Quartz fast-click failed ({exc}); pyautogui fallback")
        _release_modifier_keys()
        pyautogui.click(int(round(x)), int(round(y)))
    _release_modifier_keys()


def _type_char_quartz(ch: str) -> None:
    """Unicode key event — exact character, no Shift-key bugs."""
    from Quartz import (
        CGEventCreateKeyboardEvent,
        CGEventKeyboardSetUnicodeString,
        CGEventPost,
        kCGHIDEventTap,
    )

    down = CGEventCreateKeyboardEvent(None, 0, True)
    CGEventKeyboardSetUnicodeString(down, len(ch), ch)
    CGEventPost(kCGHIDEventTap, down)

    up = CGEventCreateKeyboardEvent(None, 0, False)
    CGEventKeyboardSetUnicodeString(up, len(ch), ch)
    CGEventPost(kCGHIDEventTap, up)


def human_type(text: str) -> None:
    """Type proxy text exactly (Quartz Unicode)."""
    print(f"[INFO] Exact text to type ({len(text)} chars): {text!r}")
    _release_modifier_keys()
    time.sleep(0.05)
    for ch in text:
        if ch == "\n":
            pyautogui.press("enter")
        elif ch == "\t":
            pyautogui.press("tab")
        else:
            _type_char_quartz(ch)
        time.sleep(random.uniform(0.05, 0.14))
    _release_modifier_keys()
    print(f"[INFO] Finished typing {len(text)} characters")


def paste_exact(text: str) -> None:
    """
    Copy exact Unicode to clipboard and Cmd+V via AppleScript.
    pyautogui.hotkey('command','v') often types a lone 'v' when Cmd is lost.
    """
    print(f"[INFO] Exact text to paste ({len(text)} chars): {text!r}")
    _release_modifier_keys()
    pyperclip.copy(text)
    clipped = pyperclip.paste()
    if clipped != text:
        raise RuntimeError(
            f"Clipboard mismatch.\nExpected: {text!r}\nGot: {clipped!r}"
        )
    time.sleep(0.15)
    script = r"""
    tell application "System Events"
      keystroke "v" using {command down}
    end tell
    """
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True)
    time.sleep(0.35)
    _release_modifier_keys()
    print("[INFO] Paste complete (AppleScript Cmd+V)")


def press_enter() -> None:
    _release_modifier_keys()
    time.sleep(0.05)
    script = r"""
    tell application "System Events"
      key code 36
    end tell
    """
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True)
    _release_modifier_keys()
    print("[INFO] Enter key pressed")


def clear_field_macos() -> None:
    """Select-all + delete via AppleScript (avoids stuck-Cmd typing 'a'/'v')."""
    _release_modifier_keys()
    script = r"""
    tell application "System Events"
      keystroke "a" using {command down}
      delay 0.08
      key code 51
    end tell
    """
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True)
    _release_modifier_keys()
    time.sleep(0.15)


# ---------------------------------------------------------------------------
# Bezier mouse movement
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
    WHY BEZIER: BlackBird watches path/speed/acceleration.
    Instant teleports are discarded; curved motion is accepted.
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

    use_quartz = sys.platform == "darwin"
    t0 = time.perf_counter()
    for i, u in enumerate(eased, start=1):
        x, y = _cubic_bezier(float(u), start, p1, p2, end)
        x += random.uniform(-0.6, 0.6)
        y += random.uniform(-0.6, 0.6)
        if use_quartz:
            try:
                _quartz_warp(x, y)
            except Exception:  # noqa: BLE001
                pyautogui.moveTo(int(round(x)), int(round(y)), _pause=False)
        else:
            pyautogui.moveTo(int(round(x)), int(round(y)), _pause=False)
        target = t0 + duration * (i / steps)
        delay = target - time.perf_counter()
        if delay > 0:
            time.sleep(delay)

    if use_quartz:
        try:
            _quartz_warp(end_x, end_y)
        except Exception:  # noqa: BLE001
            pyautogui.moveTo(int(round(end_x)), int(round(end_y)), _pause=False)
    else:
        pyautogui.moveTo(int(round(end_x)), int(round(end_y)), _pause=False)


# ---------------------------------------------------------------------------
# Coordinates
# ---------------------------------------------------------------------------

def scale_point(x: int, y: int) -> Tuple[int, int]:
    sw, sh = pyautogui.size()
    bw, bh = BASE_SCREEN
    if (sw, sh) == (bw, bh):
        return x, y
    return int(round(x * sw / bw)), int(round(y * sh / bh))


def click_target(
    key: str,
    label: str,
    *,
    activate_app: bool = True,
) -> bool:
    """Click a calibrated coordinate with human-like motion."""
    # activate_app no longer forces BlackBird keep-on-top / relaunch
    if activate_app and not is_blackbird_running():
        print(f"[ERROR] {label}: BlackBird is not running (will not relaunch)")
        return False
    if activate_app:
        detect_and_apply_layout()
        time.sleep(0.15)

    base = COORDS.get(key)
    if base is None:
        print(f"[ERROR] No calibrated coordinates for '{key}'.")
        return False

    x, y = scale_point(*base)
    print(
        f"[INFO] {label}: coordinate click at ({x}, {y}) "
        f"[layout={ACTIVE_LAYOUT}, calibrated {base} on {BASE_SCREEN[0]}x{BASE_SCREEN[1]}]"
    )
    human_click_at(x, y)
    return True


# ---------------------------------------------------------------------------
# Layout selection + app focus
# ---------------------------------------------------------------------------

def set_layout(name: str) -> None:
    """Switch active coordinate map (legacy_centered | top_left)."""
    global ACTIVE_LAYOUT, COORDS
    if name not in LAYOUTS:
        raise ValueError(f"Unknown layout {name!r}; choose from {list(LAYOUTS)}")
    changed = name != ACTIVE_LAYOUT
    ACTIVE_LAYOUT = name
    COORDS = dict(LAYOUTS[name])
    if changed:
        print(f"[INFO] Active layout: {ACTIVE_LAYOUT}")
        for key, pt in COORDS.items():
            print(f"[INFO]   {key}: {pt}")


def restore_state_a(reason: str = "") -> None:
    """Revert coordinates / URL / Stripe map to frozen STATE A."""
    global STRIPE_COORDS, STRIPE_CHECKOUT_URL, CONTINUE_WITHOUT_CANDIDATES
    print(f"[INFO] Restoring STATE A{': ' + reason if reason else ''}")
    set_layout(str(STATE_A["layout"]))
    COORDS.clear()
    COORDS.update(STATE_A["coords"])  # type: ignore[arg-type]
    STRIPE_COORDS = dict(STATE_A["stripe"])  # type: ignore[arg-type]
    CONTINUE_WITHOUT_CANDIDATES = list(STATE_A["continue_candidates"])  # type: ignore[arg-type]
    STRIPE_CHECKOUT_URL = str(STATE_A["stripe_url"])
    print("[INFO] STATE A restored")


def get_blackbird_window_origin() -> Optional[Tuple[int, int]]:
    """Return (x, y) of BlackBird window 1, or None if unavailable."""
    script = """
    tell application "System Events"
      if not (exists process "BlackBird") then return ""
      tell process "BlackBird"
        if (count of windows) = 0 then return ""
        set p to position of window 1
        return ((item 1 of p) as text) & "," & ((item 2 of p) as text)
      end tell
    end tell
    """
    try:
        out = subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            text=True,
        )
        text = (out.stdout or "").strip()
        if not text or "," not in text:
            return None
        xs, ys = text.split(",", 1)
        return int(float(xs)), int(float(ys))
    except Exception:  # noqa: BLE001
        return None


def detect_and_apply_layout(forced: Optional[str] = None) -> str:
    """
    Pick coordinates for the current BlackBird position.
    top_left if window origin is near the top-left; else legacy_centered.
    """
    choice = forced or LAYOUT_FORCE
    if choice and choice != "auto":
        set_layout(choice)
        return choice

    origin = get_blackbird_window_origin()
    if origin is None:
        print("[WARN] Could not read BlackBird window position; using top_left")
        set_layout("top_left")
        return "top_left"

    x, y = origin
    print(f"[INFO] BlackBird window origin: ({x}, {y})")
    if x <= 220 and y <= 80:
        set_layout("top_left")
        return "top_left"
    set_layout("legacy_centered")
    return "legacy_centered"


def is_blackbird_running() -> bool:
    """True only if the BlackBird process is currently alive."""
    script = r"""
    tell application "System Events"
      if exists process "BlackBird" then
        return "yes"
      else
        return "no"
      end if
    end tell
    """
    out = subprocess.run(
        ["osascript", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )
    return (out.stdout or "").strip() == "yes"


def activate_blackbird() -> bool:
    """
    Soft check only — NEVER relaunches BlackBird if the user closed it.
    NEVER brings the manager to the front.
    """
    if not is_blackbird_running():
        print("[INFO] BlackBird is off — will not relaunch or activate")
        return False
    return True


# When True (after Play / Continue), ONLY proxy browser or Continue modal may
# be raised. The BlackBird manager window must NEVER be brought to front.
_BROWSER_FRONT_MODE = False
# Once a proxy browser has been opened this run, keep it above the manager
# even between workflows (manager must never take precedence).
_PROXY_BROWSER_SEEN = False
# Launch BlackBird at most once per script run; never reopen if user quits it.
_BLACKBIRD_LAUNCHED_ONCE = False


def _is_browser_window_name(wname: str) -> bool:
    n = (wname or "").lower()
    if any(s in n for s in ("new profile", "system components", "network is not open")):
        return False
    keys = (
        "http", "https", "profile", "stripe", "about:", "new tab",
        "start page", "buy.stripe", "localhost",
    )
    return any(k in n for k in keys)


# Shared AppleScript fragment: classify BlackBird windows. Manager is NEVER raised.
_AX_CLASSIFY_WINDOWS = r"""
        set browserWins to {}
        set managerWins to {}
        set modalWins to {}

        repeat with w in windows
          try
            set wname to name of w as text

            set isModal to false
            if wname contains "System Components" then set isModal to true
            if wname contains "Continue" then set isModal to true
            try
              if exists button "Continue without" of w then set isModal to true
            end try
            try
              if exists sheet 1 of w then
                if exists button "Continue without" of sheet 1 of w then set isModal to true
              end if
            end try

            set isManager to false
            if wname is "BlackBird" then set isManager to true
            if wname contains "All Profiles" then set isManager to true
            if wname is "Profiles" then set isManager to true
            try
              if exists (first button of w whose name contains "New profile") then set isManager to true
            end try
            try
              if exists (first button of w whose name contains "+ New profile") then set isManager to true
            end try
            try
              if exists (first button of w whose name contains "Quick Profile") then set isManager to true
            end try

            set isBrowser to false
            if wname contains "http" then set isBrowser to true
            if wname contains "https" then set isBrowser to true
            if wname contains "stripe" then set isBrowser to true
            if wname contains "Stripe" then set isBrowser to true
            if wname contains "about:" then set isBrowser to true
            if wname contains "New Tab" then set isBrowser to true
            if wname contains "Start Page" then set isBrowser to true
            if wname contains "buy.stripe" then set isBrowser to true
            -- Proxy browser chrome is usually "Profile N" (not the All Profiles manager)
            if wname starts with "Profile " then set isBrowser to true
            if wname starts with "Profile" and wname does not contain "Profiles" then set isBrowser to true

            -- Manager wins over ambiguous "Profile" titles when it has New-profile chrome
            if isManager and isBrowser then
              if wname contains "All Profiles" then
                set isBrowser to false
              else if wname starts with "Profile" then
                -- "Profile 1" browser window: prefer browser
                set isManager to false
              end if
            end if

            if isModal then
              set end of modalWins to w
            else if isBrowser then
              set end of browserWins to w
            else if isManager then
              set end of managerWins to w
            else if wname contains "New profile" then
              -- profile sheet: do not raise
            else
              -- Unknown non-manager → treat as proxy browser chrome
              set end of browserWins to w
            end if
          end try
        end repeat
"""


def _sink_manager_windows_script(*, minimize: bool = False) -> str:
    """AppleScript body: demote manager windows (never AXRaise them)."""
    mini = ""
    if minimize:
        mini = r"""
          try
            set value of attribute "AXMinimized" of w to true
          end try
"""
    return f"""
        repeat with w in managerWins
          try
            set value of attribute "AXMain" of w to false
          end try
{mini}
        end repeat
"""


def demote_manager_windows(*, minimize: bool = False) -> None:
    """Push manager behind / optionally minimize so proxy browser can stay visible."""
    script = f"""
    tell application "System Events"
      if not (exists process "BlackBird") then return "none"
      tell process "BlackBird"
{_AX_CLASSIFY_WINDOWS}
{_sink_manager_windows_script(minimize=minimize)}
        return "demoted:" & (count of managerWins)
      end tell
    end tell
    """
    out = subprocess.run(
        ["osascript", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )
    print(f"[INFO] Manager demoted → {(out.stdout or '').strip() or 'none'} minimize={minimize}")


def restore_manager_windows() -> None:
    """Un-minimize manager windows (for next New profile). Does NOT raise above browsers."""
    script = f"""
    tell application "System Events"
      if not (exists process "BlackBird") then return "none"
      tell process "BlackBird"
{_AX_CLASSIFY_WINDOWS}
        repeat with w in managerWins
          try
            set value of attribute "AXMinimized" of w to false
          end try
          try
            set value of attribute "AXMain" of w to false
          end try
        end repeat
        return "restored:" & (count of managerWins)
      end tell
    end tell
    """
    out = subprocess.run(
        ["osascript", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )
    print(f"[INFO] Manager unminimized → {(out.stdout or '').strip() or 'none'}")


def get_proxy_browser_frame() -> Optional[Tuple[int, int, int, int]]:
    """Return (x, y, w, h) of the front proxy-browser window, if any."""
    script = f"""
    tell application "System Events"
      if not (exists process "BlackBird") then return ""
      tell process "BlackBird"
{_AX_CLASSIFY_WINDOWS}
        if (count of browserWins) = 0 then return ""
        set w to item 1 of browserWins
        set p to position of w
        set s to size of w
        return ((item 1 of p) as text) & "," & ((item 2 of p) as text) & "," & ¬
          ((item 1 of s) as text) & "," & ((item 2 of s) as text)
      end tell
    end tell
    """
    out = subprocess.run(
        ["osascript", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )
    text = (out.stdout or "").strip()
    if not text or text.count(",") != 3:
        return None
    try:
        x, y, bw, bh = [int(float(p)) for p in text.split(",")]
        if bw > 100 and bh > 100:
            return x, y, bw, bh
    except ValueError:
        return None
    return None


def ensure_browser_covers_blackbird() -> bool:
    """
    Z-order policy (strict) — proxy browser ALWAYS above manager:
      1) Continue-without modal (if present) → raise ONLY that
      2) else proxy browser → raise ONLY browser(s), demote/minimize manager
      3) NEVER AXRaise the BlackBird manager window
    """
    global _PROXY_BROWSER_SEEN
    # Minimize manager ONLY during Stripe/browser-front phase.
    # Never minimize while waiting for Continue without — that hides the modal.
    minimize_mgr = _BROWSER_FRONT_MODE
    script = f"""
    tell application "System Events"
      if not (exists process "BlackBird") then return "none"
      tell process "BlackBird"
{_AX_CLASSIFY_WINDOWS}
{_sink_manager_windows_script(minimize=minimize_mgr)}

        -- Prefer Continue modal above everything (including browser)
        if (count of modalWins) > 0 then
          repeat with w in modalWins
            try
              perform action "AXRaise" of w
              try
                set value of attribute "AXMain" of w to true
              end try
            end try
          end repeat
          return "raised:modal"
        end if

        if (count of browserWins) = 0 then return "none"

        -- Raise browsers FIRST (before any frontmost), never raise manager
        set raised to ""
        repeat with w in browserWins
          try
            perform action "AXRaise" of w
            try
              set value of attribute "AXMain" of w to true
            end try
            try
              set focused of w to true
            end try
            try
              set raised to (name of w as text)
            end try
          end try
        end repeat

        -- Process frontmost only AFTER browser is AXMain
        set frontmost to true

        -- Demote manager again (frontmost can pop it)
{_sink_manager_windows_script(minimize=minimize_mgr)}

        -- Raise browsers several times to defeat manager pop
        repeat 3 times
          repeat with w in browserWins
            try
              perform action "AXRaise" of w
              try
                set value of attribute "AXMain" of w to true
              end try
            end try
          end repeat
          delay 0.04
        end repeat

        if raised is not "" then return "raised:" & raised
        return "raised:browser"
      end tell
    end tell
    """
    out = subprocess.run(
        ["osascript", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )
    result = (out.stdout or "").strip()
    if result.startswith("raised"):
        if "modal" not in result:
            _PROXY_BROWSER_SEEN = True
        print(f"[INFO] Proxy browser ABOVE manager → {result}")
        return True
    print(f"[WARN] Browser/modal raise failed → {result or 'none'}")
    return False


def begin_browser_front_mode() -> None:
    """After Continue without: proxy browser stays above manager; manager minimized."""
    global _BROWSER_FRONT_MODE, _PROXY_BROWSER_SEEN
    _BROWSER_FRONT_MODE = True
    _PROXY_BROWSER_SEEN = True
    demote_manager_windows(minimize=True)
    ensure_browser_covers_blackbird()
    # Second pass — Electron sometimes re-shows manager once
    time.sleep(0.2)
    demote_manager_windows(minimize=True)
    ensure_browser_covers_blackbird()
    print("[INFO] Browser-front mode ON — manager minimized/behind; proxy browser on top")


def end_browser_front_mode() -> None:
    """
    End Stripe-phase flag. Unminimize manager for next New profile, but do NOT
    raise it above existing proxy browsers.
    """
    global _BROWSER_FRONT_MODE
    _BROWSER_FRONT_MODE = False
    print("[INFO] Browser-front mode OFF")
    restore_manager_windows()
    if _PROXY_BROWSER_SEEN:
        # Keep browsers visually above even after manager is restored
        ensure_browser_covers_blackbird()


def raise_profile_browser() -> bool:
    """Keep proxy browser visually above BlackBird manager."""
    return ensure_browser_covers_blackbird()


def enter_url_in_address_bar(url: str) -> bool:
    """Focus proxy browser (not manager) → address bar → paste URL → Enter."""
    print(f"[INFO] Entering URL in address bar (exact): {url!r}")
    ensure_browser_covers_blackbird()
    _release_modifier_keys()

    # Do NOT set process frontmost alone — that pops the manager.
    # ensure_browser_covers_blackbird already raised the browser window.
    script_focus = r"""
    tell application "System Events"
      delay 0.05
      keystroke "l" using {command down}
      delay 0.15
      keystroke "a" using {command down}
      delay 0.05
    end tell
    """
    subprocess.run(["osascript", "-e", script_focus], check=False, capture_output=True)
    ensure_browser_covers_blackbird()
    _release_modifier_keys()

    bar = COORDS.get("address_bar")
    if bar is not None:
        x, y = scale_point(*bar)
        print(f"[INFO] Address bar coordinate click at ({x}, {y})")
        human_click_at(x, y)
        time.sleep(0.2)
        subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to keystroke "a" using {command down}'],
            check=False,
            capture_output=True,
        )
        _release_modifier_keys()

    paste_exact(url)
    time.sleep(0.25)
    press_enter()
    ensure_browser_covers_blackbird()
    print("[INFO] URL submitted")
    return True


def place_blackbird_on_top() -> None:
    """
    DELETED behavior — absolute no-op.

    Must NEVER bring the BlackBird manager to the front.
    If a proxy browser is already open, re-assert browser (or Continue modal) on top.
    """
    if _BROWSER_FRONT_MODE or _PROXY_BROWSER_SEEN:
        ensure_browser_covers_blackbird()
    # Never activate / AXRaise / pin the manager window.
    return


def network_warning_visible() -> bool:
    """True if the 'BlackBird Network is not open anymore' alert is on screen."""
    script = r"""
    tell application "System Events"
      repeat with pname in {"BlackBird", "BlackBird Network"}
        if exists process pname then
          tell process pname
            repeat with w in windows
              try
                set wname to name of w as text
                if wname contains "not open" or wname contains "Network" then
                  return "yes"
                end if
              end try
              try
                if exists sheet 1 of w then
                  set sname to name of sheet 1 of w as text
                  if sname contains "not open" or sname contains "Network" then
                    return "yes"
                  end if
                  if exists button "OK" of sheet 1 of w then
                    return "yes"
                  end if
                end if
              end try
              try
                repeat with b in buttons of w
                  if name of b as text is "OK" then
                    set btitles to ""
                    try
                      set btitles to value of static text 1 of w as text
                    end try
                    if btitles contains "not open" or btitles contains "Network" then
                      return "yes"
                    end if
                  end if
                end repeat
              end try
            end repeat
          end tell
        end if
      end repeat
    end tell
    return "no"
    """
    out = subprocess.run(
        ["osascript", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )
    visible = (out.stdout or "").strip().lower() == "yes"
    if visible:
        print("[INFO] BlackBird Network warning dialog is VISIBLE")
    return visible


def dismiss_network_warning() -> bool:
    """
    If the 'BlackBird Network is not open anymore' dialog is showing,
    click OK (Accessibility first, then calibrated coordinate).
    Returns True only if the dialog was visible and a dismiss was attempted.
    """
    if not network_warning_visible():
        print("[INFO] No BlackBird Network warning dialog — skip dismiss")
        return False

    print("[INFO] Dismissing BlackBird Network warning (OK)...")
    script = r"""
    set clicked to false
    tell application "System Events"
      repeat with pname in {"BlackBird", "BlackBird Network"}
        if exists process pname then
          tell process pname
            repeat with w in windows
              try
                if exists button "OK" of w then
                  click button "OK" of w
                  set clicked to true
                end if
              end try
              try
                if exists sheet 1 of w then
                  if exists button "OK" of sheet 1 of w then
                    click button "OK" of sheet 1 of w
                    set clicked to true
                  end if
                end if
              end try
            end repeat
          end tell
        end if
      end repeat
    end tell
    if clicked then
      return "clicked"
    else
      return "none"
    end if
    """
    out = subprocess.run(
        ["osascript", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )
    result = (out.stdout or "").strip()
    if result == "clicked":
        print("[INFO] Dismissed warning via Accessibility (OK)")
        time.sleep(0.8)
        if not network_warning_visible():
            return True

    # Coordinate fallback — only when dialog was confirmed visible
    ok = COORDS.get("dismiss_ok")
    if ok is None:
        print("[WARN] dismiss_ok coordinate missing")
        return False
    x, y = scale_point(*ok)
    print(f"[INFO] AX miss — coordinate OK click at ({x}, {y})")
    human_click_at(x, y)
    time.sleep(0.8)
    if not network_warning_visible():
        print("[INFO] Network warning dismissed via coordinate OK")
        return True
    print("[WARN] Network warning still visible after OK click")
    return False


def ensure_network_warning_dismissed(max_attempts: int = 5) -> bool:
    """Keep clicking OK until the network modal is gone (or attempts exhausted)."""
    for attempt in range(1, max_attempts + 1):
        if not network_warning_visible():
            return True
        print(f"[INFO] Network modal dismiss attempt {attempt}/{max_attempts}")
        dismiss_network_warning()
        time.sleep(0.4)
    still = network_warning_visible()
    if still:
        print("[ERROR] BlackBird Network modal still blocking — cannot proceed safely")
    return not still


def raise_system_components_dialog() -> None:
    """
    Raise ONLY the Continue-without / System Components modal.
    Never set process frontmost (that pops the BlackBird manager).
    Never AXRaise the manager window.
    """
    script = f"""
    tell application "System Events"
      set raised to false
      set names to {{"BlackBird", "BlackBird Network"}}
      repeat with pname in names
        if exists process pname then
          tell process pname
{_AX_CLASSIFY_WINDOWS}
{_sink_manager_windows_script()}
            repeat with w in modalWins
              try
                perform action "AXRaise" of w
                try
                  set value of attribute "AXMain" of w to true
                end try
                set raised to true
              end try
            end repeat
          end tell
        end if
      end repeat
    end tell
    if raised then
      return "raised"
    else
      return "none"
    end if
    """
    out = subprocess.run(
        ["osascript", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )
    result = (out.stdout or "").strip()
    print(f"[INFO] Continue-without modal raise: {result or 'none'}")
    time.sleep(0.15)


def wait_seconds_keep_browser_front(seconds: float, reason: str) -> None:
    """Wait while periodically re-asserting browser above BlackBird manager."""
    seconds = max(0.0, float(seconds))
    print(f"[INFO] Waiting {seconds:.0f}s — {reason} (holding browser on top)")
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < seconds:
        demote_manager_windows(minimize=True)
        ensure_browser_covers_blackbird()
        remaining = seconds - (time.perf_counter() - t0)
        time.sleep(min(0.6, max(0.05, remaining)))


def _ax_probe_continue_modal_fast() -> str:
    """Fast title-only probe for System Components (no deep tree walk)."""
    title_script = r"""
    tell application "System Events"
      repeat with pname in {"BlackBird", "BlackBird Network"}
        if exists process pname then
          tell process pname
            repeat with w in windows
              try
                set wname to name of w as text
                if wname contains "System Components" then return "modal:" & wname
                if wname contains "Continue" and wname contains "without" then return "modal:" & wname
              end try
            end repeat
          end tell
        end if
      end repeat
    end tell
    return "gone"
    """
    out = subprocess.run(
        ["osascript", "-e", title_script],
        check=False,
        capture_output=True,
        text=True,
    )
    return (out.stdout or "").strip() or "gone"


def _ax_probe_continue_modal() -> str:
    """
    Probe for System Components / Continue-without modal.
    Fast title check first; deep search only as fallback.
    """
    title_hit = _ax_probe_continue_modal_fast()
    if title_hit != "gone":
        return title_hit

    deep_script = r"""
    tell application "System Events"
      repeat with pname in {"BlackBird", "BlackBird Network"}
        if exists process pname then
          tell process pname
            repeat with w in windows
              try
                set elems to entire contents of w
                repeat with el in elems
                  try
                    set ename to ""
                    try
                      set ename to name of el as text
                    end try
                    if ename is "" then
                      try
                        set ename to description of el as text
                      end try
                    end if
                    if ename contains "Continue without" then return "button:" & ename
                    if ename contains "Continue Without" then return "button:" & ename
                  end try
                end repeat
              end try
            end repeat
          end tell
        end if
      end repeat
    end tell
    return "gone"
    """
    out2 = subprocess.run(
        ["osascript", "-e", deep_script],
        check=False,
        capture_output=True,
        text=True,
    )
    return (out2.stdout or "").strip() or "gone"


def _continue_without_still_visible() -> bool:
    """True if System Components modal is still showing (fast title check)."""
    return _ax_probe_continue_modal_fast() != "gone"


def get_system_components_frame() -> Optional[Tuple[int, int, int, int]]:
    """Return (x, y, w, h) of the System Components window, if present."""
    script = r"""
    tell application "System Events"
      repeat with pname in {"BlackBird", "BlackBird Network"}
        if exists process pname then
          tell process pname
            repeat with w in windows
              try
                set wname to name of w as text
                if wname contains "System Components" then
                  try
                    perform action "AXRaise" of w
                  end try
                  set p to position of w
                  set s to size of w
                  return ((item 1 of p) as text) & "," & ((item 2 of p) as text) & "," & ¬
                    ((item 1 of s) as text) & "," & ((item 2 of s) as text)
                end if
              end try
            end repeat
          end tell
        end if
      end repeat
    end tell
    return ""
    """
    out = subprocess.run(
        ["osascript", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )
    text = (out.stdout or "").strip()
    if not text or text.count(",") != 3:
        return None
    try:
        x, y, bw, bh = [int(float(p)) for p in text.split(",")]
        if bw > 50 and bh > 50:
            return x, y, bw, bh
    except ValueError:
        return None
    return None


def get_continue_without_ax_point() -> Optional[Tuple[int, int]]:
    """Return screen center of Continue without control if AX exposes it."""
    script = r"""
    tell application "System Events"
      repeat with pname in {"BlackBird", "BlackBird Network"}
        if exists process pname then
          tell process pname
            repeat with w in windows
              try
                set elems to entire contents of w
                repeat with el in elems
                  try
                    set ename to ""
                    try
                      set ename to name of el as text
                    end try
                    if ename is "" then
                      try
                        set ename to description of el as text
                      end try
                    end if
                    if ename contains "Continue without" or ename contains "Continue Without" then
                      set p to position of el
                      set s to size of el
                      set cx to (item 1 of p) + ((item 1 of s) / 2)
                      set cy to (item 2 of p) + ((item 2 of s) / 2)
                      return (cx as integer as text) & "," & (cy as integer as text)
                    end if
                  end try
                end repeat
              end try
            end repeat
          end tell
        end if
      end repeat
    end tell
    return ""
    """
    out = subprocess.run(
        ["osascript", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )
    text = (out.stdout or "").strip()
    if not text or "," not in text:
        return None
    try:
        xs, ys = text.split(",", 1)
        return int(float(xs)), int(float(ys))
    except ValueError:
        return None


def click_continue_without_ax() -> bool:
    """Click Continue without via Accessibility (direct + deep + AXPress)."""
    script = r"""
    tell application "System Events"
      set names to {"BlackBird", "BlackBird Network"}
      repeat with pname in names
        if exists process pname then
          tell process pname
            repeat with w in windows
              try
                set wname to name of w as text
                if wname contains "System Components" then
                  try
                    perform action "AXRaise" of w
                  end try
                end if
              end try
              try
                if exists button "Continue without" of w then
                  click button "Continue without" of w
                  return "clicked:direct"
                end if
              end try
              try
                set elems to entire contents of w
                repeat with el in elems
                  try
                    set ename to ""
                    try
                      set ename to name of el as text
                    end try
                    if ename contains "Continue without" or ename contains "Continue Without" then
                      try
                        click el
                        return "clicked:" & ename
                      end try
                      try
                        perform action "AXPress" of el
                        return "pressed:" & ename
                      end try
                    end if
                  end try
                end repeat
              end try
            end repeat
          end tell
        end if
      end repeat
    end tell
    return "none"
    """
    out = subprocess.run(
        ["osascript", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )
    result = (out.stdout or "").strip()
    if result.startswith("clicked") or result.startswith("pressed"):
        print(f"[INFO] Continue without: Accessibility OK ({result})")
        return True
    return False


def click_continue_without_in_modal_frame() -> bool:
    """
    Immediately click Continue without using System Components window geometry.
    Button sits at bottom-right of the modal (not Enable).
    """
    frame = get_system_components_frame()
    if frame is None:
        return False
    x, y, bw, bh = frame
    # Several bottom-right offsets — modal size varies
    offsets = (
        (0.82, 0.90),
        (0.88, 0.92),
        (0.78, 0.88),
        (0.85, 0.86),
        (0.75, 0.91),
    )
    print(f"[INFO] Continue without: modal frame=({x},{y},{bw},{bh}) — clicking ASAP")
    for fx, fy in offsets:
        if not _continue_without_still_visible():
            return True
        cx = int(x + bw * fx)
        cy = int(y + bh * fy)
        print(f"[INFO] Continue without: frame click ({cx}, {cy}) offset=({fx},{fy})")
        _release_modifier_keys()
        fast_click_at(cx, cy)
        time.sleep(0.25)
        if not _continue_without_still_visible():
            print("[INFO] Continue without: dismissed via modal-frame click")
            return True
    return False


def click_continue_without() -> bool:
    """
    Click Continue without IMMEDIATELY when System Components is present.
    Order: AX point → AX click → modal-frame multi-click → calibrated coords.
    """
    probe = _ax_probe_continue_modal_fast()
    if probe == "gone":
        # One deeper check
        probe = _ax_probe_continue_modal()
    if probe == "gone":
        print("[INFO] Continue without: modal not visible — no clicks")
        return False

    print(f"[INFO] Continue without: modal present → {probe} — clicking NOW")
    raise_system_components_dialog()
    _release_modifier_keys()

    # 1) Click exact AX control center if available
    pt = get_continue_without_ax_point()
    if pt is not None:
        print(f"[INFO] Continue without: AX element point ({pt[0]}, {pt[1]})")
        fast_click_at(pt[0], pt[1])
        time.sleep(0.25)
        if not _continue_without_still_visible():
            return True

    # 2) Accessibility click/press
    if click_continue_without_ax():
        time.sleep(0.25)
        if not _continue_without_still_visible():
            return True

    # 3) Modal geometry (most reliable for Electron)
    if click_continue_without_in_modal_frame():
        return True

    if not _continue_without_still_visible():
        return True

    # 4) Calibrated screen candidates
    primary = COORDS.get("continue_without")
    tried: set[Tuple[int, int]] = set()
    ordered: List[Tuple[str, Tuple[int, int]]] = []
    if primary is not None:
        ordered.append((f"layout_{ACTIVE_LAYOUT}", primary))
    for name, p in CONTINUE_WITHOUT_CANDIDATES:
        ordered.append((name, p))

    for name, p in ordered:
        if not _continue_without_still_visible():
            return True
        if p in tried:
            continue
        tried.add(p)
        x, y = scale_point(*p)
        print(f"[INFO] Continue without candidate [{name}] at ({x}, {y})")
        raise_system_components_dialog()
        fast_click_at(x, y)
        time.sleep(0.28)
        if not _continue_without_still_visible():
            print(f"[INFO] Continue without dismissed after [{name}]")
            return True

    print("[ERROR] Continue without: still visible after all click strategies")
    return False


def count_proxy_browser_windows() -> int:
    """How many BlackBird proxy-browser windows are open (not the manager)."""
    script = f"""
    tell application "System Events"
      if not (exists process "BlackBird") then return "0"
      tell process "BlackBird"
{_AX_CLASSIFY_WINDOWS}
        return (count of browserWins) as text
      end tell
    end tell
    """
    out = subprocess.run(
        ["osascript", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )
    text = (out.stdout or "").strip()
    try:
        return max(0, int(text))
    except ValueError:
        return 0


def proxy_enabled_ui_present(*, browser_baseline: int) -> Tuple[bool, str]:
    """
    Proxy enabled if System Components modal is showing OR a new browser appeared.
    """
    probe = _ax_probe_continue_modal_fast()
    if probe != "gone":
        return True, f"system_components_modal:{probe}"
    browsers = count_proxy_browser_windows()
    if browsers > browser_baseline:
        return True, f"browser_windows:{browsers}>baseline:{browser_baseline}"
    return False, f"none(browsers={browsers}, baseline={browser_baseline})"


def wait_seconds(seconds: float, reason: str) -> None:
    seconds = max(0.0, float(seconds))
    print(f"[INFO] Waiting {seconds:.0f}s — {reason}")
    time.sleep(seconds)


def launch_blackbird() -> None:
    """
    Start BlackBird exactly once for this script run.
    If already launched earlier, or if the user quit it afterward, do NOT open again.
    Does not pin BlackBird to the top of the screen.
    """
    global _BLACKBIRD_LAUNCHED_ONCE

    if _BLACKBIRD_LAUNCHED_ONCE:
        print("[INFO] BlackBird already launched once this run — not opening again")
        if not is_blackbird_running():
            print("[INFO] BlackBird was closed by user — leaving it off (no relaunch)")
        return

    if is_blackbird_running():
        _BLACKBIRD_LAUNCHED_ONCE = True
        print("[INFO] BlackBird already running — not launching a second instance")
        ensure_network_warning_dismissed()
        detect_and_apply_layout()
        return

    if not Path(APP_PATH).exists():
        print(f"[ERROR] App not found at {APP_PATH}")
        sys.exit(1)

    print(f"[INFO] Launching {APP_PATH} (single launch for this run)")
    subprocess.Popen(["open", APP_PATH])  # noqa: S603
    _BLACKBIRD_LAUNCHED_ONCE = True
    human_pause(*APP_LAUNCH_WAIT)
    time.sleep(0.8)
    ensure_network_warning_dismissed()
    detect_and_apply_layout()
    print("[INFO] BlackBird started once. Keep-on-top is disabled. Will not relaunch if closed.")
    for sec in (3, 2, 1):
        print(f"[INFO] Starting in {sec}...")
        time.sleep(1)


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

def load_proxies(path: Path) -> List[str]:
    """
    Read data.txt top → bottom, one proxy per non-empty line (order preserved).
    Workflow count MUST equal len(returned list).
    """
    if not path.is_file():
        raise FileNotFoundError(f"data file not found: {path}")
    proxies: List[str] = []
    for file_line_no, raw in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw.strip().strip("\ufeff")
        if not line or line.startswith("#"):
            continue
        if "@" in line and line.count(":") >= 2:
            proxies.append(line)
            print(
                f"[INFO] data.txt line {file_line_no} → proxy[{len(proxies)}]: {line!r}"
            )
        else:
            print(f"[WARN] Skipping non-proxy line {file_line_no}: {line[:48]!r}")
    print(f"[INFO] data.txt loaded: {len(proxies)} proxy line(s) = {len(proxies)} workflow(s)")
    return proxies


def load_cards(path: Path) -> List[str]:
    """
    Raw card lines from card.txt (exact case preserved), top → bottom.
    Count may differ from proxies; main() cycles with index % len(cards).
    """
    if not path.is_file():
        raise FileNotFoundError(f"card file not found: {path}")
    cards: List[str] = []
    for file_line_no, raw in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw.strip().strip("\ufeff")
        if not line or line.startswith("#"):
            continue
        cards.append(line)
        print(f"[INFO] card.txt line {file_line_no} → card[{len(cards)}]: {line!r}")
    print(f"[INFO] card.txt loaded: {len(cards)} card line(s) (cycle over workflows)")
    return cards


def load_emails(path: Path = EMAIL_FILE) -> List[str]:
    """
    Checkout emails from email.txt (one address per line, top → bottom).
    Count may differ from proxies; main() cycles with index % len(emails).
    Falls back to DEFAULT_EMAIL if the file is missing or empty.
    """
    emails: List[str] = []
    if path.is_file():
        for file_line_no, raw in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            line = raw.strip().strip("\ufeff")
            if not line or line.startswith("#"):
                continue
            if "@" not in line:
                print(f"[WARN] Skipping non-email line {file_line_no}: {line[:48]!r}")
                continue
            emails.append(line)
            print(
                f"[INFO] email.txt line {file_line_no} → email[{len(emails)}]: {line!r}"
            )
    if not emails:
        print(f"[WARN] No emails in {path.name} — using default {DEFAULT_EMAIL!r}")
        emails = [DEFAULT_EMAIL]
    print(
        f"[INFO] email.txt loaded: {len(emails)} email(s) (cycle over workflows)"
    )
    return emails


def cyclic_pick(items: List[str], zero_based_index: int) -> str:
    """Pick items[i % len(items)] — natural wrap when lists differ in length."""
    if not items:
        raise ValueError("cyclic_pick: empty list")
    return items[zero_based_index % len(items)]


def parse_card_line(line: str) -> Dict[str, str]:
    """
    card.txt row (pipe-separated, exact case preserved):
      number|mm|yy|cvc|Name
    Example: 4426454031648022|05|28|396|Mario Delgado
      → number, expiry MMYY=0528, cvc, name
    """
    parts = [p.strip() for p in line.split("|")]
    if len(parts) == 5:
        number, mm, yy, cvc, name = parts
        expiry = f"{mm}{yy}"  # Stripe MM / YY field accepts MMYY
    elif len(parts) == 4:
        number, date, cvc, name = parts
        expiry = date.replace("/", "").replace(" ", "")
    else:
        raise ValueError(
            f"Bad card line (need number|mm|yy|cvc|name): {line!r}"
        )
    number_digits = "".join(ch for ch in number if ch.isdigit())
    mm_digits = "".join(ch for ch in (parts[1] if len(parts) == 5 else "") if ch.isdigit())
    yy_digits = "".join(ch for ch in (parts[2] if len(parts) == 5 else "") if ch.isdigit())
    if len(parts) == 5:
        expiry = f"{mm_digits.zfill(2)}{yy_digits.zfill(2)}" if mm_digits and yy_digits else expiry
    cvc_digits = "".join(ch for ch in cvc if ch.isdigit())
    if not number_digits or not expiry or not cvc_digits or not name:
        raise ValueError(f"Incomplete card fields: {line!r}")
    return {
        "number": number_digits,
        "expiry": expiry,  # MMYY
        "mm": mm_digits.zfill(2) if len(parts) == 5 and mm_digits else expiry[:2],
        "yy": yy_digits.zfill(2) if len(parts) == 5 and yy_digits else expiry[2:],
        "cvc": cvc_digits,
        "name": name,
        "raw": line,
    }


def load_email(path: Path = EMAIL_FILE) -> str:
    """Compatibility helper: first email from load_emails()."""
    return load_emails(path)[0]


def click_stripe(key: str, label: str, *, double_tap: bool = False) -> bool:
    """
    Click exact Stripe field coordinate from STATE A.
    Card iframes may use double_tap for focus (no drag).
    """
    if _BROWSER_FRONT_MODE:
        ensure_browser_covers_blackbird()
    base = STRIPE_COORDS.get(key)
    if base is None:
        print(f"[ERROR] Missing Stripe coord: {key}")
        return False
    x, y = scale_point(*base)
    print(f"[INFO] Stripe {label}: exact click at ({x}, {y}) [calibrated {base}]")
    _release_modifier_keys()
    human_move_to(x, y)
    time.sleep(0.12)
    _release_modifier_keys()
    try:
        _quartz_left_click(x, y)
    except Exception:  # noqa: BLE001
        pyautogui.click(int(round(x)), int(round(y)))
    if double_tap:
        time.sleep(0.18)
        _release_modifier_keys()
        try:
            _quartz_left_click(x, y)
        except Exception:  # noqa: BLE001
            pyautogui.click(int(round(x)), int(round(y)))
    _release_modifier_keys()
    time.sleep(0.3)
    if _BROWSER_FRONT_MODE:
        ensure_browser_covers_blackbird()
    return True


def fill_stripe_field(key: str, label: str, text: str, *, use_paste: bool = False) -> bool:
    """Click one Stripe field, clear it, enter the full value, then wait 3s."""
    iframe = key in ("email", "card_number", "card_expiry", "card_cvc", "card_name")
    if not click_stripe(key, label, double_tap=iframe):
        return False
    time.sleep(0.35)
    clear_field_macos()
    time.sleep(0.2)
    print(f"[INFO] Stripe {label}: entering {text!r}")
    if use_paste:
        paste_exact(text)
    else:
        human_type(text)
    time.sleep(0.25)
    _release_modifier_keys()
    wait_seconds(DELAY_BETWEEN_CARD_FIELDS, f"interval after {label}")
    print(f"[INFO] Stripe {label}: done")
    return True


def scroll_browser_to_bottom() -> None:
    """
    Scroll proxy browser to the very bottom using keyboard ONLY.
    Mouse must NOT move during this step — field clicks happen only after scroll.
    """
    # Focus browser via Accessibility (no mouse)
    ensure_browser_covers_blackbird()
    demote_manager_windows(minimize=True)
    ensure_browser_covers_blackbird()
    _release_modifier_keys()

    print("[INFO] Scrolling to VERY BOTTOM via keyboard (mouse stays still)...")
    # End (119) + Page Down (121) sent to BlackBird while browser is AXMain
    script = r"""
    tell application "System Events"
      if not (exists process "BlackBird") then return "none"
      tell process "BlackBird"
        repeat 6 times
          key code 119
          delay 0.06
        end repeat
        repeat 20 times
          key code 121
          delay 0.05
        end repeat
        repeat 4 times
          key code 125
          delay 0.04
        end repeat
        key code 119
        delay 0.1
      end tell
    end tell
    return "scrolled"
    """
    out = subprocess.run(
        ["osascript", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )
    print(f"[INFO] Keyboard scroll → {(out.stdout or '').strip() or 'done'}")
    time.sleep(0.35)
    demote_manager_windows(minimize=True)
    ensure_browser_covers_blackbird()
    print("[INFO] Scroll-to-bottom complete — mouse still unmoved; ready for field clicks")


def fill_stripe_checkout(card: Dict[str, str], email: str) -> bool:
    """
    Scrolled-bottom Stripe fill:
      scroll → wait 3s → email → card fields →
      re-scroll bottom → uncheck save toggle → Pay → wait 60s
    """
    ensure_browser_covers_blackbird()
    demote_manager_windows(minimize=True)

    global STRIPE_COORDS
    STRIPE_COORDS = dict(STATE_A["stripe"])  # type: ignore[arg-type]

    print(
        "[INFO] Stripe checkout: scroll → fields → re-scroll → "
        "UNCHECK save-toggle → Pay → wait 60s"
    )
    print(f"[INFO] Card row: {card['raw']!r}")
    print(
        f"[INFO] Parsed: number={card['number']!r} "
        f"expiry={card['expiry']!r} (MM={card.get('mm')!r} YY={card.get('yy')!r}) "
        f"cvc={card['cvc']!r} name={card['name']!r}"
    )
    print(f"[INFO] Exact click targets: {STRIPE_COORDS}")
    if not email or "@" not in email:
        email = DEFAULT_EMAIL
    print(f"[INFO] Email to enter: {email!r}")

    if not email or "@" not in email:
        print("[ERROR] Missing checkout email")
        return False

    # Mouse must not move until first scroll finishes
    scroll_browser_to_bottom()
    wait_seconds(DELAY_AFTER_STRIPE_SCROLL, "after scroll — mouse may move only now")
    ensure_browser_covers_blackbird()

    if not fill_stripe_field("email", "1 Email", email, use_paste=True):
        return False

    if not fill_stripe_field(
        "card_number", "2 Card number", card["number"], use_paste=True
    ):
        return False

    if not fill_stripe_field("card_expiry", "3 Expiry MM/YY", card["expiry"]):
        return False

    if not fill_stripe_field("card_cvc", "4 CVC", card["cvc"]):
        return False

    if not fill_stripe_field(
        "card_name", "5 Cardholder name", card["name"], use_paste=True
    ):
        return False

    # Re-scroll fully down so save-toggle + Pay match scrolled calibration
    print("[INFO] Re-scrolling to very bottom before save-toggle / Pay...")
    scroll_browser_to_bottom()
    wait_seconds(1.5, "settle after re-scroll for save-toggle")
    ensure_browser_covers_blackbird()
    _release_modifier_keys()

    # Exact single click on checkbox inside red rectangle (do NOT double-tap)
    if not click_stripe(
        "save_toggle", "6 Save-info checkbox (uncheck)", double_tap=False
    ):
        return False
    time.sleep(0.6)

    ensure_browser_covers_blackbird()
    _release_modifier_keys()
    time.sleep(0.25)
    if not click_stripe("pay", "7 Pay button", double_tap=False):
        return False
    print("[INFO] Pay clicked — waiting 60s for payment processing...")
    wait_seconds(DELAY_AFTER_PAY, "payment processing after Pay")
    print("[INFO] Payment wait complete")
    return True


def detect_proxy_unreachable() -> bool:
    """
    True if BlackBird shows the red 'Proxy unreachable' toast
    (bottom of the manager window).
    """
    try:
        shot = np.array(pyautogui.screenshot())
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] screenshot failed: {exc}")
        return False
    h, w = shot.shape[:2]
    # Toast sits in lower-center of the BlackBird panel (top-left layout)
    y0, y1 = int(h * 0.62), int(h * 0.92)
    x0, x1 = int(w * 0.15), int(w * 0.55)
    region = shot[y0:y1, x0:x1]
    r = region[:, :, 0].astype(np.int16)
    g = region[:, :, 1].astype(np.int16)
    b = region[:, :, 2].astype(np.int16)
    # Red error text / border
    red = (r > 170) & (g < 110) & (b < 110) & (r > g + 50)
    # Yellow warning triangle
    yellow = (r > 180) & (g > 140) & (b < 100)
    red_n, yel_n = int(red.sum()), int(yellow.sum())
    hit = red_n > 400 and yel_n > 80
    if hit:
        print(f"[WARN] Proxy unreachable toast detected (red={red_n}, yellow={yel_n})")
    return hit


def wait_and_click_continue_without(
    appear_timeout: float = CONTINUE_MODAL_APPEAR_TIMEOUT,
    *,
    browser_baseline: Optional[int] = None,
) -> str:
    """
    After Play:
      - System Components appears ⇒ click Continue without IMMEDIATELY
      - New proxy browser (no modal) ⇒ proceed
      - Neither within timeout ⇒ inactive ⇒ next proxy (no Stripe)
      - Proxy unreachable ⇒ failed
    Do NOT minimize the manager here (that hides the modal).
    """
    if browser_baseline is None:
        browser_baseline = count_proxy_browser_windows()

    print(
        "[INFO] After Play: polling for System Components / Continue without "
        f"(timeout {appear_timeout:.0f}s; browser_baseline={browser_baseline})..."
    )
    t0 = time.perf_counter()
    attempts = 0

    while time.perf_counter() - t0 < appear_timeout:
        if detect_proxy_unreachable():
            print("[WARN] Proxy unreachable — skip this proxy")
            return "failed"

        # Fast path: System Components title → click Continue without NOW
        probe = _ax_probe_continue_modal_fast()
        if probe != "gone":
            attempts += 1
            print(f"[INFO] System Components detected ({probe}) — clicking Continue without ASAP")
            if click_continue_without():
                print("[INFO] Continue without clicked — proxy active")
                return "ok"
            # Modal still up; keep hammering until timeout
            time.sleep(0.08)
            continue

        # New browser without modal also means proxy enabled
        browsers = count_proxy_browser_windows()
        if browsers > browser_baseline:
            # Re-check modal one more time (often appears with browser)
            probe = _ax_probe_continue_modal_fast()
            if probe != "gone":
                print(f"[INFO] Modal appeared with browser ({probe}) — clicking Continue")
                if click_continue_without():
                    return "ok"
                time.sleep(0.08)
                continue
            print(
                f"[INFO] New proxy browser opened (count {browsers}>{browser_baseline}) "
                "without modal — proceeding"
            )
            return "ok"

        time.sleep(0.08)

    if detect_proxy_unreachable():
        return "failed"

    # Final chance
    probe = _ax_probe_continue_modal_fast()
    if probe != "gone":
        print(f"[INFO] Final Continue attempt ({probe})")
        if click_continue_without():
            return "ok"
        print("[WARN] Modal still visible but Continue click failed")
        return "failed"

    browsers = count_proxy_browser_windows()
    if browsers > browser_baseline:
        print("[INFO] Browser present at timeout, no modal — treating as enabled")
        return "ok"

    print(
        "[WARN] Proxy NOT enabled (no System Components modal, no new browser). "
        "Skipping Continue/URL/Stripe — next data.txt proxy."
    )
    return "inactive"


# ---------------------------------------------------------------------------
# Workflow (STATE A)
# ---------------------------------------------------------------------------

def click_ax_button(titles: List[str], label: str) -> bool:
    """
    Click a BlackBird control by Accessibility name.
    Searches every window via 'entire contents' (Electron nested UI).
    Does NOT set process frontmost (manager must never take precedence).
    """
    title_list = ", ".join(f'"{t}"' for t in titles)
    script = f"""
    set wanted to {{{title_list}}}
    tell application "System Events"
      if not (exists process "BlackBird") then return "none"
      tell process "BlackBird"
        -- NO set frontmost — manager must never be forced on top
        delay 0.05
        repeat with t in wanted
          set tname to contents of t
          repeat with w in windows
            try
              set elems to entire contents of w
              repeat with el in elems
                try
                  set ename to ""
                  try
                    set ename to name of el as text
                  end try
                  if ename is tname or ename contains tname then
                    try
                      set erole to role of el as text
                      if erole is "AXButton" or erole is "AXRadioButton" or erole is "AXCheckBox" or erole is "AXPopUpButton" then
                        click el
                        return "clicked:" & ename
                      end if
                    end try
                    try
                      click el
                      return "clicked:" & ename
                    end try
                  end if
                end try
              end repeat
            end try
          end repeat
        end repeat
      end tell
    end tell
    return "none"
    """
    out = subprocess.run(
        ["osascript", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )
    result = (out.stdout or "").strip()
    if result.startswith("clicked"):
        print(f"[INFO] {label}: Accessibility click OK ({result})")
        if _PROXY_BROWSER_SEEN or _BROWSER_FRONT_MODE:
            ensure_browser_covers_blackbird()
        return True
    err = (out.stderr or "").strip()
    if err:
        print(f"[INFO] {label}: Accessibility miss ({err[:120]}) — coordinates")
    else:
        print(f"[INFO] {label}: Accessibility miss — will use coordinates")
    if _PROXY_BROWSER_SEEN or _BROWSER_FRONT_MODE:
        ensure_browser_covers_blackbird()
    return False


def click_named_or_coord(
    key: str,
    label: str,
    ax_titles: List[str],
    *,
    activate_app: bool = True,
) -> bool:
    """Prefer Accessibility name click, then calibrated coordinate."""
    if activate_app and not is_blackbird_running():
        print(f"[ERROR] {label}: BlackBird is not running (will not relaunch)")
        return False
    if activate_app:
        detect_and_apply_layout()
        time.sleep(0.15)
    if click_ax_button(ax_titles, label):
        return True
    return click_target(key, label, activate_app=False)


def click_new_proxy() -> bool:
    """
    Connection → New Proxy (3rd segment) inside the New profile sheet.
    Do not reposition windows. AX first, then calibrated center.
    """
    if click_ax_button(["New Proxy", "New proxy"], "New Proxy"):
        return True
    return click_target(
        "new_proxy", "New Proxy (3rd Connection segment)", activate_app=False
    )


def close_profile_browsers() -> None:
    """
    INTENTIONALLY A NO-OP.
    Proxy browsers must NEVER be closed/turned off after Pay or between workflows.
    """
    print("[INFO] Skipping browser close — proxy browsers stay open (required)")


def dismiss_open_sheets() -> None:
    """Press Escape to close stray New profile / dialog sheets (not browsers)."""
    _release_modifier_keys()
    pyautogui.press("escape")
    time.sleep(0.4)
    pyautogui.press("escape")
    _release_modifier_keys()
    time.sleep(0.3)


def return_to_blackbird_manager(reason: str = "") -> None:
    """
    After browser work: leave Stripe phase and prepare next profile steps.
    Unminimizes manager for New profile clicks, but does not put it above browsers.
    """
    print(f"[INFO] Returning from browser work{': ' + reason if reason else ''}")
    _release_modifier_keys()
    end_browser_front_mode()
    dismiss_open_sheets()
    restore_state_a(reason or "return to manager")
    if not is_blackbird_running():
        print("[INFO] BlackBird is off — not relaunching or forcing on-top")
        return
    dismiss_network_warning()
    ensure_network_warning_dismissed()
    detect_and_apply_layout()
    restore_manager_windows()
    _release_modifier_keys()
    time.sleep(0.3)
    print("[INFO] Ready for next steps (manager restored but not forced above browsers)")


def create_profile_with_proxy(proxy: str) -> bool:
    """
    New profile → 3s → New Proxy → 3s → type proxy (exact) → 3s → Create.
    Manager is unminimized so New profile is clickable; never left covering browsers after.
    """
    if not is_blackbird_running():
        print("[ERROR] BlackBird is not running — not relaunching")
        return False
    detect_and_apply_layout()
    dismiss_open_sheets()
    restore_manager_windows()
    time.sleep(0.2)

    if not ensure_network_warning_dismissed():
        print("[ERROR] Network modal blocks workflow — will not click New profile")
        return False

    if not click_named_or_coord(
        "new_profile",
        "New profile",
        ["+ New profile", "+ New Profile"],
        activate_app=False,
    ):
        return False
    wait_seconds(DELAY_STEP, "after New profile")

    if not click_new_proxy():
        return False
    wait_seconds(DELAY_STEP, "after New Proxy")

    if not click_target("proxy_input", "Proxy input field", activate_app=False):
        return False
    time.sleep(0.35)
    clear_field_macos()
    print(f"[INFO] Typing proxy from data.txt (exact case): {proxy!r}")
    human_type(proxy)
    wait_seconds(DELAY_STEP, "after proxy input")

    if not click_named_or_coord(
        "create_profile",
        "Create profile",
        ["Create profile", "Create Profile"],
        activate_app=False,
    ):
        return False
    time.sleep(1.2)
    dismiss_network_warning()
    ensure_network_warning_dismissed()
    detect_and_apply_layout()
    if _PROXY_BROWSER_SEEN:
        # Existing browsers stay above manager after create
        ensure_browser_covers_blackbird()
    return True


def refresh_and_play() -> Tuple[bool, int]:
    """
    Refresh (newest row) → 3s → Play.
    Returns (ok, browser_window_count_before_play) so caller can detect a new browser.
    Never raise manager over proxy browsers.
    """
    if not is_blackbird_running():
        print("[ERROR] BlackBird is not running — not relaunching")
        return False, 0
    detect_and_apply_layout()
    time.sleep(0.3)
    if _PROXY_BROWSER_SEEN:
        ensure_browser_covers_blackbird()
    if not click_target("open_profile", "Refresh (proxy icon)", activate_app=False):
        return False, 0
    wait_seconds(DELAY_STEP, "after Refresh")
    browser_baseline = count_proxy_browser_windows()
    print(f"[INFO] Proxy browser count before Play: {browser_baseline}")
    if not click_target("play_profile", "Play (▶)", activate_app=False):
        return False, browser_baseline
    return True, browser_baseline


def run_one_workflow(
    proxy: str,
    card_line: str,
    email: str,
    index: int,
    total: int,
    *,
    launch: bool = False,
    checkout_url: str = STRIPE_CHECKOUT_URL,
) -> str:
    """
    One complete workflow for ONE data.txt proxy line (used once only).

    Returns one of:
      paid          — proxy active and Stripe Pay clicked
      inactive      — proxy not usable (no Stripe)
      stripe_failed — proxy active but URL/card/Pay did not complete
      setup_failed  — New profile / Refresh / Play failed
    """
    try:
        card = parse_card_line(card_line)
    except ValueError as exc:
        print(f"[ERROR] {exc}")
        return "setup_failed"

    print("")
    print("=" * 60)
    print(f"[INFO] WORKFLOW {index}/{total} — proxy line {index} of data.txt")
    print(f"[INFO] Proxy: {proxy!r}")
    print(f"[INFO] Card:  {card_line!r}")
    print(f"[INFO] URL:   {checkout_url!r}")
    print("=" * 60)

    # Critical: clear stuck Cmd/Shift from previous workflow before any clicks
    _release_modifier_keys()
    time.sleep(0.15)

    if launch:
        launch_blackbird()
    else:
        return_to_blackbird_manager(f"start workflow {index}")

    restore_state_a(f"start of workflow {index}/{total}")

    # Exactly ONE use of this data.txt line. If inactive → fail this workflow;
    # main() advances to the next line (do NOT re-enter the same proxy).
    print(f"[INFO] Using data.txt line {index}/{total} once: {proxy!r}")
    if not create_profile_with_proxy(proxy):
        print(f"[ERROR] Workflow {index}/{total}: create profile failed for this line")
        return_to_blackbird_manager(f"workflow {index} create failed")
        return "setup_failed"
    play_ok, browser_baseline = refresh_and_play()
    if not play_ok:
        print(f"[ERROR] Workflow {index}/{total}: Refresh/Play failed for this line")
        return_to_blackbird_manager(f"workflow {index} play failed")
        return "setup_failed"

    # Play → only if proxy enabled (modal and/or new browser): Continue → URL → Stripe
    # If proxy NOT enabled: no Continue/URL/Stripe clicks → next data.txt line / New profile
    status = wait_and_click_continue_without(browser_baseline=browser_baseline)
    if status != "ok":
        print(
            f"[WARN] Workflow {index}/{total}: proxy not usable ({status}). "
            "Skipping URL/Stripe for this line — next workflow starts Create New Profile "
            "with the next data.txt proxy."
        )
        return_to_blackbird_manager(f"workflow {index} proxy inactive/failed")
        return "inactive"

    begin_browser_front_mode()
    wait_seconds_keep_browser_front(
        DELAY_AFTER_CONTINUE, "after Continue without (15s, browser held on top)"
    )

    if not enter_url_in_address_bar(checkout_url):
        return_to_blackbird_manager(f"workflow {index} address bar / URL failed")
        return "stripe_failed"

    wait_seconds_keep_browser_front(
        DELAY_STRIPE_LOAD, "Stripe page full load (browser held on top)"
    )
    ensure_browser_covers_blackbird()

    if not fill_stripe_checkout(card, email):
        return_to_blackbird_manager(f"workflow {index} Stripe/Pay failed")
        return "stripe_failed"

    return_to_blackbird_manager(f"workflow {index}/{total} Pay done")
    print(f"[INFO] WORKFLOW {index}/{total} COMPLETE (paid)")
    return "paid"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="BlackBird STATE-A workflow: all data.txt proxies → Stripe Pay."
    )
    p.add_argument("--data", default=str(DATA_FILE), help="Proxy file (data.txt)")
    p.add_argument("--cards", default=str(CARD_FILE), help="Card file (card.txt)")
    p.add_argument("--email-file", default=str(EMAIL_FILE), help="Email file (email.txt)")
    p.add_argument(
        "--url",
        default=STRIPE_CHECKOUT_URL,
        help="URL pasted into the address bar after browser opens",
    )
    p.add_argument("--proxy", default=None, help="Optional single proxy override")
    p.add_argument(
        "--layout",
        choices=["auto", "top_left", "legacy_centered"],
        default="auto",
        help="Coordinate layout (default auto / STATE A = top_left)",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process only the first N proxies (0 = all)",
    )
    return p.parse_args()


def _write_run_results(
    *,
    outcome: str,
    total: int,
    counts: Dict[str, int],
    error: str = "",
) -> None:
    """Write last_run.json for the Telegram bot completion summary."""
    paid = int(counts.get("paid", 0))
    inactive = int(counts.get("inactive", 0))
    stripe_failed = int(counts.get("stripe_failed", 0))
    setup_failed = int(counts.get("setup_failed", 0))
    active = paid + stripe_failed
    payload = {
        "outcome": outcome,
        "total": total,
        "active": active,
        "inactive": inactive,
        "paid": paid,
        "stripe_failed": stripe_failed,
        "setup_failed": setup_failed,
        "error": error,
    }
    try:
        RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        RESULTS_FILE.write_text(
            json.dumps(payload, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"[INFO] Wrote run summary → {RESULTS_FILE}")
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] Could not write {RESULTS_FILE}: {exc}")


def _startup_environment_report() -> None:
    """Log runtime environment; catch frozen/.exe issues early."""
    frozen = bool(getattr(sys, "frozen", False))
    print(f"[INFO] frozen_binary={frozen}")
    print(f"[INFO] executable={sys.executable}")
    print(f"[INFO] platform={sys.platform}")
    print(f"[INFO] script_dir={SCRIPT_DIR}")
    print(f"[INFO] z-order: manager NEVER on top; only Continue modal or proxy browser")
    print(f"[INFO] place_blackbird_on_top: DELETED (absolute no-op)")
    print(f"[INFO] relaunch-if-closed: DISABLED (single launch only)")
    print(f"[INFO] proxy-inactive: skip Continue/URL/Stripe → next New profile")
    print(f"[INFO] pairing: workflows=len(proxies); cards+emails cycle with %")

    if sys.platform != "darwin":
        print("")
        print("[ERROR] This automation requires macOS (BlackBird + Accessibility + osascript).")
        print("[ERROR] A Windows .exe only opens this console — it cannot control BlackBird.")
        print("[ERROR] On the Mac:")
        print("[ERROR]   1) python3 build_exe.py")
        print("[ERROR]   2) give the client the dist/ folder")
        print("[ERROR]   3) client double-clicks Run.command (opens Terminal and runs)")
        print("[ERROR] Or without building:  chmod +x run.sh && ./run.sh")
        print("")
        if frozen:
            sys.exit(2)
        return

    # macOS: without Accessibility, logs still print "Quartz left-click" but
    # the real cursor never moves (exactly the failure seen in client logs).
    ensure_macos_input_permissions()
    verify_mouse_control_or_exit()


def main() -> None:
    _startup_environment_report()

    args = parse_args()
    global LAYOUT_FORCE
    LAYOUT_FORCE = None if args.layout == "auto" else args.layout

    w, h = pyautogui.size()
    print(f"[INFO] Screen size: {w}x{h} (calibrated {BASE_SCREEN[0]}x{BASE_SCREEN[1]})")
    print(f"[INFO] Script dir: {SCRIPT_DIR}")
    print("[INFO] Loaded STATE A (known-good calibration)")
    restore_state_a("boot")

    try:
        # Proxy list order = data.txt top→bottom. One line → one workflow.
        # Cards + emails cycle independently (any lengths OK).
        if args.proxy:
            proxies = [args.proxy.strip()]
            print("[WARN] --proxy override: running exactly 1 workflow")
        else:
            proxies = load_proxies(Path(args.data))
        cards = load_cards(Path(args.cards))
        emails = load_emails(Path(args.email_file))
    except FileNotFoundError as exc:
        print(f"[ERROR] {exc}")
        sys.exit(1)

    if not proxies:
        print("[ERROR] No proxies in data.txt")
        sys.exit(1)
    if not cards:
        print("[ERROR] No card lines in card.txt")
        sys.exit(1)
    if not emails:
        print("[ERROR] No emails available")
        sys.exit(1)

    if args.limit and args.limit > 0:
        print(
            f"[WARN] --limit {args.limit} truncates data.txt "
            f"({len(proxies)} → {args.limit}). Omit --limit for full file."
        )
        proxies = proxies[: args.limit]

    # Hard rule: workflow count == proxy line count only.
    # Cards/emails wrap: workflow i uses cards[(i-1)%N] and emails[(i-1)%M].
    total = len(proxies)
    print("")
    print("=" * 60)
    print(
        f"[INFO] RULE: {total} data.txt proxy line(s) → exactly {total} workflow(s)"
    )
    print(
        f"[INFO] Cyclic pairing: {len(cards)} card(s), {len(emails)} email(s) "
        "(reuse from top when lists are shorter than proxies)"
    )
    print("[INFO] Reading order: top → bottom for every file")
    print("=" * 60)
    for i, px in enumerate(proxies, 1):
        card_preview = cyclic_pick(cards, i - 1)
        email_preview = cyclic_pick(emails, i - 1)
        card_slot = ((i - 1) % len(cards)) + 1
        email_slot = ((i - 1) % len(emails)) + 1
        print(f"[INFO]   workflow {i}/{total}")
        print(f"[INFO]     proxy[{i}/{total}]: {px!r}")
        print(f"[INFO]     card[{card_slot}/{len(cards)}]: {card_preview!r}")
        print(f"[INFO]     email[{email_slot}/{len(emails)}]: {email_preview!r}")
    print(f"[INFO] Checkout URL: {args.url!r}")
    print("")

    counts = {
        "paid": 0,
        "inactive": 0,
        "stripe_failed": 0,
        "setup_failed": 0,
    }
    # Iterate proxies in file order only — never keyed off card/email length
    try:
        for i, proxy in enumerate(proxies, start=1):
            card_line = cyclic_pick(cards, i - 1)
            email = cyclic_pick(emails, i - 1)
            print("")
            print(f"[INFO] >>> Workflow {i}/{total} — consuming data.txt proxy line {i}")
            print(f"[INFO]     proxy={proxy!r}")
            print(f"[INFO]     card={card_line!r}")
            print(f"[INFO]     email={email!r}")
            result = run_one_workflow(
                proxy,
                card_line,
                email,
                index=i,
                total=total,
                launch=(i == 1),
                checkout_url=args.url,
            )
            if result not in counts:
                result = "setup_failed"
            counts[result] += 1
            print(f"[INFO] <<< Workflow {i}/{total} result={result}")

            if i < total:
                wait_seconds(DELAY_STEP, f"pause before workflow {i + 1}/{total}")
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] Unexpected error — batch aborted: {exc}")
        _write_run_results(
            outcome="error",
            total=total,
            counts=counts,
            error=str(exc),
        )
        sys.exit(1)

    active = counts["paid"] + counts["stripe_failed"]
    print("")
    print("=" * 60)
    print("[INFO] ALL DONE — batch processed every data.txt line")
    print(f"[INFO] Total proxies: {total}")
    print(f"[INFO] Active (executed): {active}/{total}")
    print(f"[INFO] Paid (Stripe OK): {counts['paid']}/{active if active else 0}")
    print(f"[INFO] Inactive (skipped): {counts['inactive']}")
    print(f"[INFO] Stripe/card failed: {counts['stripe_failed']}")
    print(f"[INFO] Setup failed: {counts['setup_failed']}")
    print("=" * 60)
    _write_run_results(outcome="completed", total=total, counts=counts)
    sys.exit(0)


if __name__ == "__main__":
    main()
