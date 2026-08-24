#!/usr/bin/env python3
"""
BlackBird STATE-A UI agent — macOS end-to-end workflow automation.

Workflow count is driven ONLY by data.txt: one proxy line → one workflow
(top → bottom). Cards cycle across proxies. Each proxy gets its own freshly
generated email: random name + random 4-digit number on email.txt's domain.

One workflow = New profile → proxy → Create → Refresh → observe BlackBird's
country/unreachable status → Play → 40s fixed wait → direct address-bar click →
Stripe URL → 60s load → email+card → Pay → 60s → close the browser; then the
next data.txt proxy.

Checkout fields are located by reading the page off the screen rather than from
fixed coordinates, so the same run works whatever country the proxy resolves to.

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

from stripe_vision import StripeForm, detect_stripe_form

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
        # SOCKS5 = 3rd protocol chip (HTTP/HTTPS/SOCKS5/SSH); same offset as top_left
        "socks5_button": (1539, 365),
        "proxy_input": (1274, 381),
        "create_profile": (1451, 738),
        "open_profile": (906, 201),
        "play_profile": (627, 205),
        "continue_without": (1245, 592),  # case A — centered/right System Components
        # Center of the address bar marked in the client's 1024x576 screenshot.
        # Stored in the 1920x1080 calibration used by scale_point().
        "address_bar": (960, 57),
        "dismiss_ok": (960, 540),  # approximate center dialog (legacy)
    },
    "top_left": {
        # Recalibrated from Aug 9 screenshots (BlackBird at top-left)
        "new_profile": (1234, 70),
        # New Proxy = 3rd Connection segment (measured from Aug 10 screenshot)
        "new_proxy": (1192, 258),
        # SOCKS5 = 3rd protocol chip; red-box center from client screenshot → 1920x1080
        "socks5_button": (1322, 336),
        "proxy_input": (1020, 346),
        "create_profile": (1224, 703),
        "open_profile": (684, 173),   # proxy refresh icon
        "play_profile": (403, 172),   # ▶ play
        "continue_without": (920, 705),  # System Components bottom-right Continue without
        # Center of the address bar marked in the client's 1024x576 screenshot.
        "address_bar": (960, 57),
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
# http = original New Proxy → input; socks5 = New Proxy → SOCKS5 → input
PROXY_TYPE = "http"

# ---------------------------------------------------------------------------
# STATE A — frozen known-good calibration (revert target on errors)
# ---------------------------------------------------------------------------
STATE_A: Dict[str, object] = {
    "layout": "top_left",
    "coords": dict(LAYOUTS["top_left"]),
    # No Stripe coordinates live here on purpose. The checkout moves with the
    # proxy's country — a ZIP row appears, labels change length, error lines push
    # rows down — so its fields are read off the screen at run time instead
    # (see stripe_vision.py).
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
# After Play: give the proxy browser this long to open before clicking its
# address bar. Browser-window title monitoring is deliberately not used.
DELAY_AFTER_CONTINUE = 40.0
DELAY_STRIPE_LOAD = 60.0  # full Stripe page load before touching the form
DELAY_BETWEEN_CARD_FIELDS = 3.0
DELAY_AFTER_STRIPE_SCROLL = 3.0  # after scroll-to-bottom, before Email click
DELAY_AFTER_PAY = 60.0           # wait after Pay for payment processing
# Browser close control marked by the client, measured in the supplied image.
# Scale this screenshot point to the live screen before every unconditional click.
BROWSER_CLOSE_REFERENCE_SCREEN = (1024, 576)
BROWSER_CLOSE_REFERENCE_POINT = (14, 30)
# After Play: wait for proxy to enable (Continue modal AND/OR proxy browser).
# If neither appears → proxy inactive → skip Stripe and start next workflow.
CONTINUE_MODAL_APPEAR_TIMEOUT = 12.0

EMAIL_FILE = SCRIPT_DIR / "email.txt"
DEFAULT_EMAIL = "trioleo2947@outlook.com"
RESULTS_FILE = SCRIPT_DIR / "logs" / "last_run.json"

# Real first/last names for generated checkout emails, so the local part looks
# like a genuine person (e.g. jamesbrooks3184@...).
EMAIL_FIRST_NAMES = (
    "james", "john", "robert", "michael", "william", "david", "richard",
    "joseph", "thomas", "charles", "christopher", "daniel", "matthew", "anthony",
    "mark", "donald", "steven", "paul", "andrew", "joshua", "kenneth", "kevin",
    "brian", "george", "edward", "ronald", "timothy", "jason", "jeffrey", "ryan",
    "jacob", "gary", "nicholas", "eric", "jonathan", "stephen", "larry", "justin",
    "scott", "brandon", "benjamin", "samuel", "gregory", "alexander", "patrick",
    "frank", "raymond", "jack", "dennis", "jerry", "tyler", "aaron", "henry",
    "mary", "patricia", "jennifer", "linda", "elizabeth", "barbara", "susan",
    "jessica", "sarah", "karen", "nancy", "lisa", "margaret", "betty", "sandra",
    "ashley", "kimberly", "emily", "donna", "michelle", "carol", "amanda",
    "dorothy", "melissa", "deborah", "stephanie", "rebecca", "laura", "sharon",
    "cynthia", "kathleen", "amy", "angela", "shirley", "anna", "brenda", "emma",
    "olivia", "sophia", "hannah", "grace", "chloe", "victoria", "natalie",
)
EMAIL_LAST_NAMES = (
    "smith", "johnson", "williams", "brown", "jones", "garcia", "miller",
    "davis", "rodriguez", "martinez", "hernandez", "lopez", "gonzalez", "wilson",
    "anderson", "thomas", "taylor", "moore", "jackson", "martin", "lee",
    "perez", "thompson", "white", "harris", "sanchez", "clark", "ramirez",
    "lewis", "robinson", "walker", "young", "allen", "king", "wright", "scott",
    "torres", "nguyen", "hill", "flores", "green", "adams", "nelson", "baker",
    "hall", "rivera", "campbell", "mitchell", "carter", "roberts", "phillips",
    "evans", "turner", "parker", "collins", "edwards", "stewart", "morris",
    "murphy", "cook", "rogers", "morgan", "cooper", "peterson", "bailey",
    "reed", "kelly", "howard", "cox", "ward", "brooks", "bennett", "gray",
    "james", "watson", "price", "bell", "wood", "barnes", "ross", "henderson",
)
# Guards against the same address being generated twice in one run.
_USED_GENERATED_EMAILS: set = set()

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


def _release_mouse_buttons() -> None:
    """Guarantee left/right/other buttons are UP before any cursor move."""
    try:
        import Quartz

        cur = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        x, y = float(cur.x), float(cur.y)
        for button, up_type in (
            (Quartz.kCGMouseButtonLeft, Quartz.kCGEventLeftMouseUp),
            (Quartz.kCGMouseButtonRight, Quartz.kCGEventRightMouseUp),
            (Quartz.kCGMouseButtonCenter, Quartz.kCGEventOtherMouseUp),
        ):
            _post_mouse_event(up_type, x, y, button)
    except Exception:  # noqa: BLE001
        try:
            pyautogui.mouseUp(button="left")
            pyautogui.mouseUp(button="right")
        except Exception:  # noqa: BLE001
            pass


def _quartz_left_click(x: float, y: float) -> None:
    """
    One stationary left click: cursor is already at (x, y).

    Down and Up are posted at the identical point with no MouseMoved between
    them. Any movement while the button is down paints the blue drag-select
    seen on Stripe.
    """
    import Quartz

    xf, yf = float(x), float(y)
    _release_mouse_buttons()
    _quartz_warp(xf, yf)
    time.sleep(0.05)
    _post_mouse_event(Quartz.kCGEventLeftMouseDown, xf, yf, Quartz.kCGMouseButtonLeft)
    time.sleep(0.04)
    _post_mouse_event(Quartz.kCGEventLeftMouseUp, xf, yf, Quartz.kCGMouseButtonLeft)
    _release_mouse_buttons()
    print(f"[INFO] Quartz left-click at ({int(round(xf))}, {int(round(yf))})")


def human_click_at(x: float, y: float) -> None:
    """Move first (button UP), then one stationary Quartz click."""
    _release_modifier_keys()
    _release_mouse_buttons()
    cur_x, cur_y = pyautogui.position()
    move_mouse_humanly(cur_x, cur_y, x, y)
    _release_modifier_keys()
    _release_mouse_buttons()
    time.sleep(0.08)
    try:
        _quartz_left_click(x, y)
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] Quartz click failed ({exc}); falling back to pyautogui.click")
        _release_modifier_keys()
        _release_mouse_buttons()
        try:
            _quartz_warp(x, y)
        except Exception:  # noqa: BLE001
            pass
        pyautogui.click(int(round(x)), int(round(y)))
    _release_modifier_keys()
    _release_mouse_buttons()


def human_move_to(x: float, y: float) -> None:
    """Bezier move only — button must stay UP the whole time (no drag-select)."""
    _release_modifier_keys()
    _release_mouse_buttons()
    cur_x, cur_y = pyautogui.position()
    move_mouse_humanly(cur_x, cur_y, x, y)
    _release_modifier_keys()
    _release_mouse_buttons()


def fast_click_at(x: float, y: float) -> None:
    """Short bezier (button UP) + one stationary Quartz click."""
    _release_modifier_keys()
    _release_mouse_buttons()
    cur_x, cur_y = pyautogui.position()
    move_mouse_humanly(cur_x, cur_y, x, y, duration=random.uniform(0.08, 0.18))
    _release_modifier_keys()
    _release_mouse_buttons()
    try:
        _quartz_left_click(x, y)
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] Quartz fast-click failed ({exc}); pyautogui fallback")
        _release_modifier_keys()
        _release_mouse_buttons()
        pyautogui.click(int(round(x)), int(round(y)))
    _release_modifier_keys()
    _release_mouse_buttons()


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


def dismiss_autofill_popup() -> None:
    """
    Escape once, to close any browser autofill list hanging over the next field.

    Escape does not clear Stripe inputs, and no modal is open at this point.
    """
    _release_modifier_keys()
    script = r"""
    tell application "System Events"
      key code 53
    end tell
    """
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True)
    _release_modifier_keys()


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
    # Never drag: button must be UP for the entire path.
    _release_mouse_buttons()
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
    """Revert BlackBird coordinates / URL to frozen STATE A."""
    global STRIPE_CHECKOUT_URL, CONTINUE_WITHOUT_CANDIDATES
    print(f"[INFO] Restoring STATE A{': ' + reason if reason else ''}")
    set_layout(str(STATE_A["layout"]))
    COORDS.clear()
    COORDS.update(STATE_A["coords"])  # type: ignore[arg-type]
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


def minimize_all_proxy_browsers_for_completion() -> None:
    """
    Normal batch completion only: keep every proxy browser process/session alive,
    minimize all browser windows, and leave the BlackBird manager active.

    This does not close a browser, stop a proxy, or alter the /stop behavior.
    """
    script = f"""
    tell application "System Events"
      if not (exists process "BlackBird") then return "blackbird_off"
      tell process "BlackBird"
{_AX_CLASSIFY_WINDOWS}
        set minimizedCount to 0
        repeat with w in browserWins
          try
            set value of attribute "AXMinimized" of w to true
            set minimizedCount to minimizedCount + 1
          end try
        end repeat
        repeat with w in managerWins
          try
            set value of attribute "AXMinimized" of w to false
          end try
        end repeat
        set frontmost to true
        repeat with w in managerWins
          try
            perform action "AXRaise" of w
          end try
          try
            set value of attribute "AXMain" of w to true
          end try
        end repeat
        return "browsers_minimized:" & minimizedCount & ";manager_active:" & (count of managerWins)
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
    error = (out.stderr or "").strip()
    if result:
        print(f"[INFO] Normal-completion window state → {result}")
    elif error:
        print(f"[WARN] Could not minimize completion browsers: {error[:200]}")
    else:
        print("[WARN] Could not confirm normal-completion window state")


def raise_manager_to_front() -> None:
    """
    Bring the BlackBird MANAGER window to the front for the profile-setup phase.

    Manager buttons (New profile / New Proxy / Create / Refresh / Play) are
    clicked by coordinate, so the manager must sit on top for those clicks to
    land. This is only ever called BEFORE the current proxy's browser exists
    (i.e. between finishing one proxy and creating the next). A prior successful
    browser is closed after its payment wait; this function still handles
    failure paths where a browser may remain. Once Play opens the new browser,
    begin_browser_front_mode() puts that proxy browser on top.
    """
    script = f"""
    tell application "System Events"
      if not (exists process "BlackBird") then return "none"
      tell process "BlackBird"
{_AX_CLASSIFY_WINDOWS}
        repeat with w in managerWins
          try
            set value of attribute "AXMinimized" of w to false
          end try
        end repeat
        set frontmost to true
        repeat with w in managerWins
          try
            perform action "AXRaise" of w
          end try
          try
            set value of attribute "AXMain" of w to true
          end try
        end repeat
        return "manager_front:" & (count of managerWins)
      end tell
    end tell
    """
    out = subprocess.run(
        ["osascript", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )
    print(f"[INFO] Manager raised to front → {(out.stdout or '').strip() or 'none'}")
    time.sleep(0.3)


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
    """Click the marked proxy-browser address bar once → paste URL → Enter."""
    print(f"[INFO] Entering URL in address bar (exact): {url!r}")
    _release_modifier_keys()
    bar = COORDS.get("address_bar")
    if bar is None:
        print("[ERROR] Address bar coordinate is not configured")
        return False

    x, y = scale_point(*bar)
    print(f"[INFO] Direct address-bar click at marked location ({x}, {y})")
    _release_mouse_buttons()
    click_point_once(x, y, "proxy browser address bar")
    time.sleep(0.25)
    subprocess.run(
        [
            "osascript",
            "-e",
            'tell application "System Events" to keystroke "a" using {command down}',
        ],
        check=False,
        capture_output=True,
    )
    _release_modifier_keys()

    paste_exact(url)
    time.sleep(0.25)
    press_enter()
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
    """
    How many BlackBird proxy-browser windows are open (not the manager).

    Title-based, so it can undercount when a browser carries an unexpected name.
    Treat it as one hint among several, never as proof that no browser exists.
    """
    script = f"""
    tell application "System Events"
      if not (exists process "BlackBird") then return "0"
      tell process "BlackBird"
{_AX_CLASSIFY_WINDOWS}
        return (count of browserWins) as text
      end tell
    end tell
    """
    try:
        out = subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=25,
        )
    except subprocess.TimeoutExpired:
        print("[WARN] Browser-count query timed out — reporting 0 (not proof of absence)")
        return 0
    text = (out.stdout or "").strip()
    try:
        return max(0, int(text))
    except ValueError:
        return 0


def list_blackbird_windows() -> Optional[List[Tuple[str, int, int, bool]]]:
    """
    Every BlackBird window as (name, width, height, minimized).

    Returns None when the window list cannot be read at all (BlackBird gone,
    AppleScript error, Accessibility hiccup). None means "unknown", which is
    deliberately different from an empty list: callers must not treat a failed
    query as proof that no window exists.
    """
    script = r"""
    tell application "System Events"
      if not (exists process "BlackBird") then return "NOPROC"
      tell process "BlackBird"
        set winList to "OK"
        repeat with w in windows
          set wname to "?"
          try
            set wname to name of w as text
          end try
          set ww to 0
          set wh to 0
          try
            set wsize to size of w
            set ww to item 1 of wsize
            set wh to item 2 of wsize
          end try
          set wmin to false
          try
            set wmin to value of attribute "AXMinimized" of w
          end try
          set rowText to wname & "|" & (ww as text) & "|" & (wh as text)
          set rowText to rowText & "|" & (wmin as text)
          set winList to winList & linefeed & rowText
        end repeat
        return winList
      end tell
    end tell
    """
    try:
        out = subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except subprocess.TimeoutExpired:
        print("[WARN] Window list query timed out")
        return None
    except OSError as exc:
        print(f"[WARN] Window list query could not run: {exc}")
        return None
    text = (out.stdout or "").strip()
    if not text.startswith("OK"):
        err = (out.stderr or "").strip()
        print(f"[WARN] Could not list BlackBird windows: {err[:160] or text[:160] or 'no output'}")
        return None
    windows: List[Tuple[str, int, int, bool]] = []
    for raw in text.splitlines()[1:]:
        raw = raw.strip()
        if not raw or raw.count("|") < 3:
            continue
        name, w_txt, h_txt, min_txt = raw.rsplit("|", 3)
        try:
            width = int(float(w_txt))
            height = int(float(h_txt))
        except ValueError:
            continue
        windows.append((name, width, height, min_txt.strip().lower() == "true"))
    return windows


def _is_modal_window_name(name: str) -> bool:
    low = name.lower()
    return "system components" in low or "continue" in low


def count_large_app_windows(windows: List[Tuple[str, int, int, bool]]) -> int:
    """
    Count real BlackBird windows: manager and proxy browsers, no modals/toasts.

    Title strings are ignored on purpose. Play either adds a real window or it
    does not, and that delta is what tells an active proxy from a dead one.
    """
    total = 0
    for name, width, height, _minimized in windows:
        if _is_modal_window_name(name):
            continue
        if width < 400 or height < 300:
            continue
        total += 1
    return total


def describe_windows(windows: Optional[List[Tuple[str, int, int, bool]]]) -> str:
    if windows is None:
        return "unreadable"
    if not windows:
        return "none"
    return "; ".join(
        f"{name!r} {width}x{height}{' min' if minimized else ''}"
        for name, width, height, minimized in windows
    )


def capture_browser_baseline() -> Dict[str, object]:
    """Window state just before Play, used to spot the browser Play opens."""
    windows = list_blackbird_windows()
    baseline: Dict[str, object] = {
        "classified": count_proxy_browser_windows(),
        "windows": windows,
        "large": count_large_app_windows(windows) if windows is not None else None,
    }
    print(
        f"[INFO] Pre-Play window baseline: large={baseline['large']} "
        f"classified_browsers={baseline['classified']} | {describe_windows(windows)}"
    )
    return baseline


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


def retry_manager_click(action, label: str, attempts: int = 3) -> bool:
    """
    Run a manager-phase click and retry it if it did not register.

    Every step of the setup phase must actually happen. A click can be lost when
    a system modal appears over the button or the manager slips behind a window,
    so a failed attempt clears the modal, re-raises the manager and clicks again.
    The happy path is untouched: a click that works first time returns at once.
    """
    for attempt in range(1, attempts + 1):
        if action():
            if attempt > 1:
                print(f"[INFO] {label}: succeeded on attempt {attempt}/{attempts}")
            return True
        print(f"[WARN] {label}: attempt {attempt}/{attempts} did not register — retrying")
        _release_modifier_keys()
        _release_mouse_buttons()
        ensure_network_warning_dismissed()
        restore_manager_windows()
        raise_manager_to_front()
        time.sleep(1.0)
    print(f"[ERROR] {label}: still not performed after {attempts} attempts")
    return False


def _browser_signal(baseline: Dict[str, object], *, cheap_only: bool) -> Optional[str]:
    """
    Reason the proxy browser looks open, or None if nothing indicates one.

    Several independent signals are used because window titles in BlackBird are
    not dependable: a proxy browser and the manager can carry the same name, so
    a title-based count alone can miss a browser that is plainly on screen. The
    cheap window-list signal is authoritative; the title-based ones only add
    evidence and are skipped while polling.
    """
    base_large = baseline.get("large")
    windows = list_blackbird_windows()
    if windows is None:
        # The window list could not be read. Never call a proxy dead on a failed
        # query — assume the browser is there and let the Stripe steps decide.
        return "window list unreadable — assuming browser is open"

    large = count_large_app_windows(windows)
    if isinstance(base_large, int) and large > base_large:
        return f"new window after Play (large {base_large} → {large})"
    if isinstance(base_large, int) and base_large == 0 and large > 0:
        return f"window present where none existed before Play (large={large})"
    if cheap_only:
        return None

    classified = count_proxy_browser_windows()
    base_classified = baseline.get("classified")
    if isinstance(base_classified, int) and classified > base_classified:
        return f"classified browser count {base_classified} → {classified}"
    if classified > 0 and get_proxy_browser_frame() is not None:
        return "proxy browser window frame readable"
    return None


def proxy_browser_appeared(baseline: Dict[str, object], grace: float = 10.0) -> bool:
    """
    True if Play opened this proxy's browser window.

    An inactive proxy never opens a browser, which is how the run tells the two
    apart. Grace polling only covers a browser that is slow to register; it
    cannot invent one. On a negative result the full window list is logged so a
    misdetection can be diagnosed from the run log.
    """
    signal = _browser_signal(baseline, cheap_only=False)
    if signal:
        print(f"[INFO] Proxy browser detected: {signal}")
        return True

    deadline = time.perf_counter() + max(0.0, grace)
    while time.perf_counter() < deadline:
        time.sleep(1.0)
        signal = _browser_signal(baseline, cheap_only=True)
        if signal:
            print(f"[INFO] Proxy browser detected during grace check: {signal}")
            return True

    # Last word goes to the full check, including the title-based signals.
    signal = _browser_signal(baseline, cheap_only=False)
    if signal:
        print(f"[INFO] Proxy browser detected on final check: {signal}")
        return True

    print("[WARN] No proxy browser after Play — proxy looks inactive")
    print(f"[WARN]   before Play: {describe_windows(baseline.get('windows'))}")
    print(f"[WARN]   after  Play: {describe_windows(list_blackbird_windows())}")
    return False


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

def _is_ipv4(value: str) -> bool:
    parts = value.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False


def _is_port(value: str) -> bool:
    return value.isdigit() and 1 <= int(value) <= 65535


def _looks_like_host(value: str) -> bool:
    """IPv4 or hostname — must contain a dot and no spaces."""
    return bool(value) and "." in value and not any(ch.isspace() for ch in value)


def normalize_proxy_line(line: str) -> Tuple[Optional[str], bool]:
    """
    Normalize one data.txt line to Username:Password:IP:Port for BlackBird.

    Accepts:
      - Username:Password:IP:Port (stored / typed format)
      - IP:Port:Username:Password (paste format → converted)
      - Username:Password@IP:Port (legacy → converted)
    """
    cleaned = line.strip().strip("\ufeff")
    if not cleaned or any(ch.isspace() for ch in cleaned):
        return None, False

    if "@" in cleaned:
        creds, _, hostport = cleaned.rpartition("@")
        host, _, port = hostport.rpartition(":")
        if (
            creds.count(":") == 1
            and _looks_like_host(host)
            and _is_port(port)
        ):
            return f"{creds}:{host}:{port}", True
        return None, False

    parts = cleaned.split(":")
    if len(parts) != 4:
        return None, False
    a, b, c, d = parts
    if _is_ipv4(a) and _is_port(b) and c and d:
        return f"{c}:{d}:{a}:{b}", True
    if _looks_like_host(c) and _is_port(d) and a and b:
        return cleaned, False
    return None, False


def load_proxies(path: Path) -> List[str]:
    """
    Read data.txt top → bottom, one proxy per non-empty line (order preserved).
    Workflow count MUST equal len(returned list).
    Stored / typed format: Username:Password:IP:Port
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
        proxy, converted = normalize_proxy_line(line)
        if proxy is None:
            print(f"[WARN] Skipping non-proxy line {file_line_no}: {line[:64]!r}")
            continue
        proxies.append(proxy)
        if converted and proxy != line:
            print(
                f"[INFO] data.txt line {file_line_no} → proxy[{len(proxies)}]: "
                f"{proxy!r} (normalized from {line!r})"
            )
        else:
            print(
                f"[INFO] data.txt line {file_line_no} → proxy[{len(proxies)}]: {proxy!r}"
            )
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
    Load email.txt. Only the first address's domain is used — main() generates a
    random name and 4-digit number per proxy.
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
    print(f"[INFO] email.txt loaded: domain source={emails[0]!r}")
    return emails


def cyclic_pick(items: List[str], zero_based_index: int) -> str:
    """Pick items[i % len(items)] — natural wrap when lists differ in length."""
    if not items:
        raise ValueError("cyclic_pick: empty list")
    return items[zero_based_index % len(items)]


def random_email_for_proxy(seed_email: str) -> str:
    """
    Build a checkout email from a real first + last name and a random 4-digit
    number.

    Only the domain is taken from email.txt; the name and number are generated
    fresh from real-world name lists so the address looks like a real person.
    Example: trioleo2947@outlook.com → jamesbrooks3184@outlook.com
    """
    _, separator, domain = seed_email.strip().partition("@")
    if not separator or not domain:
        raise ValueError(f"Invalid seed email: {seed_email!r}")

    candidate = ""
    for _ in range(500):
        first = random.choice(EMAIL_FIRST_NAMES)
        last = random.choice(EMAIL_LAST_NAMES)
        candidate = f"{first}{last}{random.randint(1000, 9999)}@{domain}"
        if candidate not in _USED_GENERATED_EMAILS:
            break
    _USED_GENERATED_EMAILS.add(candidate)
    return candidate


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


def click_point_once(x: int, y: int, label: str) -> bool:
    """
    Exactly ONE stationary click at (x, y).

    Move (button UP) → pause → down/up at the same point. No double-click,
    no select-all, no drag: a drag paints a blue selection instead of placing
    the caret, which is what used to send text into the wrong field.
    """
    if _BROWSER_FRONT_MODE:
        ensure_browser_covers_blackbird()
    print(f"[INFO] Stripe {label}: ONE click at ({x}, {y})")
    _release_modifier_keys()
    _release_mouse_buttons()
    human_move_to(x, y)
    time.sleep(0.15)
    _release_modifier_keys()
    _release_mouse_buttons()
    try:
        _quartz_left_click(x, y)
    except Exception:  # noqa: BLE001
        pyautogui.click(int(round(x)), int(round(y)))
    _release_modifier_keys()
    _release_mouse_buttons()
    time.sleep(0.3)
    if _BROWSER_FRONT_MODE:
        ensure_browser_covers_blackbird()
    return True


# ---------------------------------------------------------------------------
# Stripe checkout — fields located from the screen
# ---------------------------------------------------------------------------

class StripeLayout:
    """A detected checkout, with its points already mapped to click space."""

    def __init__(self, form: StripeForm, shot_size: Tuple[int, int]) -> None:
        self.form = form
        screen_w, screen_h = pyautogui.size()
        shot_w, shot_h = shot_size
        self.sx = screen_w / float(shot_w)
        self.sy = screen_h / float(shot_h)

    @property
    def has_zip(self) -> bool:
        return self.form.has_zip

    @property
    def toggle_checked(self) -> Optional[bool]:
        return self.form.toggle_checked

    def point(self, key: str) -> Optional[Tuple[int, int]]:
        pt = self.form.points.get(key)
        if pt is None:
            return None
        return int(round(pt[0] * self.sx)), int(round(pt[1] * self.sy))

    def describe(self) -> str:
        parts = []
        for key in (
            "email",
            "card_number",
            "card_expiry",
            "card_cvc",
            "card_name",
            "zip",
            "save_toggle",
            "pay",
        ):
            pt = self.point(key)
            if pt is not None:
                parts.append(f"{key}={pt}")
        return " ".join(parts)


def grab_screen() -> Optional[np.ndarray]:
    """Full-screen RGB capture, or None if the screenshot call fails."""
    try:
        return np.asarray(pyautogui.screenshot().convert("RGB"))
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] Screen capture failed: {exc}")
        return None


def locate_stripe_form(
    reason: str,
    *,
    attempts: int = 6,
    pause: float = 1.5,
    require: Tuple[str, ...] = (),
) -> Optional[StripeLayout]:
    """
    Read the checkout off the screen, retrying while the page settles.

    Retries matter because the form is re-read at several points in the flow and
    the page reflows underneath us: unchecking save-info removes the phone row,
    and a rejected value adds an error line that pushes every later row down.
    """
    for attempt in range(1, attempts + 1):
        if _BROWSER_FRONT_MODE:
            ensure_browser_covers_blackbird()
        shot = grab_screen()
        if shot is not None:
            form = detect_stripe_form(shot)
            if form is not None:
                missing = form.missing(require) if require else []
                if not missing:
                    layout = StripeLayout(form, (shot.shape[1], shot.shape[0]))
                    print(
                        f"[INFO] Stripe form located ({reason}, attempt {attempt}): "
                        f"{layout.describe()}"
                    )
                    print(
                        f"[INFO] Stripe form: zip_field={form.has_zip} "
                        f"save_toggle_checked={form.toggle_checked}"
                    )
                    for note in form.notes:
                        print(f"[WARN] Stripe form note: {note}")
                    return layout
                print(
                    f"[WARN] Stripe form incomplete ({reason}, attempt "
                    f"{attempt}/{attempts}): missing {', '.join(missing)}"
                )
            else:
                print(
                    f"[WARN] Stripe form not recognised ({reason}, attempt "
                    f"{attempt}/{attempts})"
                )
        if attempt < attempts:
            time.sleep(pause)
    print(f"[ERROR] Could not locate the Stripe form on screen ({reason})")
    return None


def fill_stripe_field_at(
    point: Tuple[int, int],
    label: str,
    text: str,
    *,
    use_paste: bool = False,
) -> bool:
    """One click on the located input, then type/paste into it."""
    click_point_once(point[0], point[1], label)
    time.sleep(0.35)
    print(f"[INFO] Stripe {label}: entering {text!r}")
    if use_paste:
        paste_exact(text)
    else:
        human_type(text)
    time.sleep(0.25)
    _release_modifier_keys()
    dismiss_autofill_popup()
    wait_seconds(DELAY_BETWEEN_CARD_FIELDS, f"interval after {label}")
    print(f"[INFO] Stripe {label}: done")
    return True


def random_zip_code() -> str:
    """A five-digit postal code for checkouts that ask for one."""
    return f"{random.randint(10000, 99999)}"


def uncheck_save_toggle(layout: StripeLayout) -> bool:
    """
    Leave the save-information box unticked.

    An already-unticked box is left alone; clicking it would turn it back on and
    bring the phone row with it.
    """
    if layout.toggle_checked is None:
        print("[ERROR] Save-info checkbox state could not be read")
        return False
    if not layout.toggle_checked:
        print("[INFO] Stripe save-info checkbox already unchecked — no click needed")
        return True

    point = layout.point("save_toggle")
    if point is None:
        print("[ERROR] Save-info checkbox is checked but has no click point")
        return False

    for attempt in range(1, 4):
        click_point_once(point[0], point[1], f"Save-info checkbox (uncheck, try {attempt})")
        wait_seconds(2.0, "page reflow after toggling save-info")
        current = locate_stripe_form("verify save-info unchecked", attempts=3, pause=1.0)
        if current is None:
            print("[WARN] Could not re-read the form after unchecking save-info")
            return False
        if current.toggle_checked is False:
            print("[INFO] Stripe save-info checkbox is now unchecked")
            return True
        point = current.point("save_toggle") or point
        print(f"[WARN] Save-info checkbox still checked after attempt {attempt}")
    return False


def scroll_browser_to_bottom() -> None:
    """
    Scroll to the very bottom with keyboard ONLY.

    The mouse pointer must not move at all during this step. Field clicks start
    only after scroll returns and the caller waits 3 seconds.
    """
    ensure_browser_covers_blackbird()
    demote_manager_windows(minimize=True)
    ensure_browser_covers_blackbird()
    _release_modifier_keys()
    _release_mouse_buttons()

    print("[INFO] Scrolling to VERY BOTTOM via keyboard (mouse stays still)...")
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
    print("[INFO] Scroll-to-bottom complete — mouse still unmoved; ready for 3s settle")


def fill_stripe_checkout(card: Dict[str, str], email: str) -> bool:
    """
    scroll (mouse frozen) → wait 3s → read the form off the screen →
    one click per field, top to bottom → ZIP if the country asks for one →
    leave save-info unchecked → Pay → wait 60s

    Every click point comes from the current screenshot, so a longer country
    name, a translated label or an extra error line moves the target with the
    page instead of leaving the automation typing into the wrong box.
    """
    ensure_browser_covers_blackbird()
    demote_manager_windows(minimize=True)

    print(
        "[INFO] Stripe checkout: scroll → 3s settle → locate fields on screen → "
        "fill every input → UNCHECK save-info → Pay → wait 60s"
    )
    print(f"[INFO] Card row: {card['raw']!r}")
    print(
        f"[INFO] Parsed: number={card['number']!r} "
        f"expiry={card['expiry']!r} (MM={card.get('mm')!r} YY={card.get('yy')!r}) "
        f"cvc={card['cvc']!r} name={card['name']!r}"
    )
    if not email or "@" not in email:
        email = DEFAULT_EMAIL
    print(f"[INFO] Email to enter: {email!r}")

    # Mouse must not move until the scroll finishes and the page settles.
    _release_mouse_buttons()
    scroll_browser_to_bottom()
    wait_seconds(DELAY_AFTER_STRIPE_SCROLL, "settle after scroll (cursor still frozen)")
    ensure_browser_covers_blackbird()
    _release_mouse_buttons()

    layout = locate_stripe_form(
        "before filling",
        require=("email", "card_number", "card_expiry", "card_cvc", "card_name", "pay"),
    )
    if layout is None:
        print("[ERROR] Stripe checkout aborted: the payment form was not found")
        return False

    # Filled top to bottom, exactly as the form reads. The page is re-read
    # before each field because a rejected value adds an inline error line that
    # pushes every row below it down; reusing the first snapshot is how text
    # ends up in the wrong box.
    steps = [
        ("email", "1 Email", email, True),
        ("card_number", "2 Card number", card["number"], True),
        ("card_expiry", "3 Expiry MM/YY", card["expiry"], False),
        ("card_cvc", "4 CVC", card["cvc"], False),
        ("card_name", "5 Cardholder name", card["name"], True),
    ]
    for key, label, value, use_paste in steps:
        current = locate_stripe_form(f"before {label}", attempts=4, require=(key,))
        if current is None:
            print(f"[ERROR] Stripe {label}: the form could not be re-read")
            return False
        layout = current
        point = layout.point(key)
        if point is None:
            print(f"[ERROR] Stripe {label}: no location for this field")
            return False
        if not fill_stripe_field_at(point, label, value, use_paste=use_paste):
            return False

    # Countries such as the United States add a postal-code row under the
    # country select. It is required, so it must never be left blank.
    if layout.has_zip:
        current = locate_stripe_form("before 6 ZIP", attempts=4, require=("zip",))
        zip_point = current.point("zip") if current else None
        if zip_point is None:
            print("[ERROR] Stripe ZIP: the postal-code row could not be re-read")
            return False
        if not fill_stripe_field_at(zip_point, "6 ZIP", random_zip_code()):
            return False
    else:
        print("[INFO] Stripe ZIP: this country's form has no postal-code row")

    # Typing can add or clear inline error lines, which moves every later row,
    # so the checkbox and the button are located again rather than reused.
    wait_seconds(1.5, "settle before save-info checkbox")
    ensure_browser_covers_blackbird()
    _release_modifier_keys()
    _release_mouse_buttons()

    toggle_layout = locate_stripe_form(
        "before save-info checkbox", require=("save_toggle", "pay")
    )
    if toggle_layout is None:
        print(
            "[ERROR] Stripe checkout aborted: the save-info checkbox could not be "
            "read, so it cannot be guaranteed unchecked"
        )
        return False
    if not uncheck_save_toggle(toggle_layout):
        print("[ERROR] Stripe checkout aborted: could not uncheck save-info")
        return False

    final = locate_stripe_form("before Pay", require=("pay",))
    if final is None:
        print("[ERROR] Stripe checkout aborted: Pay button not found after reflow")
        return False
    pay_point = final.point("pay")
    if pay_point is None:
        print("[ERROR] Stripe Pay: no location for the button")
        return False

    ensure_browser_covers_blackbird()
    _release_modifier_keys()
    _release_mouse_buttons()
    time.sleep(0.25)
    click_point_once(pay_point[0], pay_point[1], "7 Pay button")
    print("[INFO] Pay clicked — waiting 60s for payment processing...")
    # Hold the browser on top for the whole wait: the close button is clicked
    # right afterwards, and a manager window drifting in front would swallow it.
    wait_seconds_keep_browser_front(DELAY_AFTER_PAY, "payment processing after Pay")
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


def detect_proxy_country_indicator() -> bool:
    """
    Detect the country-flag badge BlackBird shows after a successful Refresh.

    The badge is the small flag-and-country pill at the bottom center of the
    manager, as marked in the client's 1024x576 screenshot. Detection uses only
    that narrow area, so profile icons and the macOS dock cannot trigger it.
    """
    try:
        shot = np.array(pyautogui.screenshot())
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] Country-indicator screenshot failed: {exc}")
        return False
    h, w = shot.shape[:2]
    x0, x1 = int(w * 430 / 1024), int(w * 570 / 1024)
    y0, y1 = int(h * 365 / 576), int(h * 410 / 576)
    region = shot[y0:y1, x0:x1, :3].astype(np.int16)
    if region.size == 0:
        return False
    maximum = region.max(axis=2)
    minimum = region.min(axis=2)
    saturated = (maximum - minimum > 35) & (maximum > 70)
    dark_text = maximum < 150
    color_pixels = int(saturated.sum())
    dark_pixels = int(dark_text.sum())
    hit = color_pixels > 20 and dark_pixels > 15
    if hit:
        print(
            "[INFO] Proxy country indicator detected after Refresh "
            f"(color={color_pixels}, dark={dark_pixels})"
        )
    return hit


def observe_proxy_status_after_refresh(seconds: float = DELAY_STEP) -> str:
    """
    Observe BlackBird's own post-Refresh status for the required delay.

    Returns active when the country-flag badge appears, unreachable only for
    BlackBird's explicit red error, and unknown when neither is visible. Unknown
    is not treated as inactive; the browser workflow is still attempted.
    """
    deadline = time.perf_counter() + max(0.0, float(seconds))
    status = "unknown"
    while True:
        if detect_proxy_unreachable():
            status = "unreachable"
            break
        if detect_proxy_country_indicator():
            status = "active"
            break
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            break
        time.sleep(min(0.35, remaining))

    remaining = deadline - time.perf_counter()
    if remaining > 0:
        time.sleep(remaining)
    if status == "unknown":
        print(
            "[WARN] No country badge was captured after Refresh; status is "
            "unknown, so the workflow will still try the browser"
        )
    return status


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


def click_socks5_protocol() -> bool:
    """
    Protocol row → SOCKS5 (3rd of HTTP/HTTPS/SOCKS5/SSH) after New Proxy.
    AX by label first; calibrated socks5_button fallback.
    """
    if click_ax_button(["SOCKS5", "Socks5", "socks5"], "SOCKS5"):
        return True
    return click_target(
        "socks5_button", "SOCKS5 protocol button", activate_app=False
    )


def _close_one_proxy_browser_ax() -> str:
    """Try native Accessibility close actions on one proxy-browser window."""
    script = f"""
    tell application "System Events"
      if not (exists process "BlackBird") then return "blackbird_off"
      tell process "BlackBird"
{_AX_CLASSIFY_WINDOWS}
        if (count of browserWins) = 0 then return "no_browser"
        set w to item 1 of browserWins
        repeat with mw in managerWins
          try
            set value of attribute "AXMain" of mw to false
          end try
        end repeat
        try
          set value of attribute "AXMinimized" of w to false
        end try
        try
          perform action "AXRaise" of w
        end try
        try
          set value of attribute "AXMain" of w to true
        end try
        try
          set focused of w to true
        end try
        set frontmost to true
        delay 0.2

        try
          set cb to first button of w whose subrole is "AXCloseButton"
          perform action "AXPress" of cb
          return "AXPress close button"
        end try
        try
          set cb to first button of w whose description is "close button"
          click cb
          return "click close button"
        end try
        try
          perform action "AXClose" of w
          return "AXClose window"
        end try
        return "AX close unavailable"
      end tell
    end tell
    """
    out = subprocess.run(
        ["osascript", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )
    return (out.stdout or "").strip() or (out.stderr or "").strip() or "no result"


def _proxy_browser_close_point() -> Optional[Tuple[int, int]]:
    """
    Raise one proxy browser and return its close-button center.

    Prefer the live AX close-button rectangle. For custom Chromium chrome, fall
    back to the indicated top-left control: approximately 14 px right and 9 px
    below the browser window's top-left corner.
    """
    script = f"""
    tell application "System Events"
      if not (exists process "BlackBird") then return ""
      tell process "BlackBird"
{_AX_CLASSIFY_WINDOWS}
        if (count of browserWins) = 0 then return ""
        set w to item 1 of browserWins
        repeat with mw in managerWins
          try
            set value of attribute "AXMain" of mw to false
          end try
        end repeat
        try
          set value of attribute "AXMinimized" of w to false
        end try
        try
          perform action "AXRaise" of w
        end try
        try
          set value of attribute "AXMain" of w to true
        end try
        try
          set focused of w to true
        end try
        set frontmost to true
        delay 0.25

        try
          set cb to first button of w whose subrole is "AXCloseButton"
          set bp to position of cb
          set bs to size of cb
          set cx to (item 1 of bp) + ((item 1 of bs) / 2)
          set cy to (item 2 of bp) + ((item 2 of bs) / 2)
          return (cx as integer as text) & "," & (cy as integer as text)
        end try

        set wp to position of w
        return (((item 1 of wp) + 14) as integer as text) & "," & ¬
          (((item 2 of wp) + 9) as integer as text)
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
    if text.count(",") != 1:
        return None
    try:
        x, y = (int(float(value)) for value in text.split(","))
        return x, y
    except ValueError:
        return None


def _close_one_proxy_browser_cmd_w() -> str:
    """Raise one verified proxy browser and send macOS Close Window."""
    script = f"""
    tell application "System Events"
      if not (exists process "BlackBird") then return "blackbird_off"
      tell process "BlackBird"
{_AX_CLASSIFY_WINDOWS}
        if (count of browserWins) = 0 then return "no_browser"
        set w to item 1 of browserWins
        repeat with mw in managerWins
          try
            set value of attribute "AXMain" of mw to false
          end try
        end repeat
        try
          set value of attribute "AXMinimized" of w to false
        end try
        try
          perform action "AXRaise" of w
        end try
        try
          set value of attribute "AXMain" of w to true
        end try
        try
          set focused of w to true
        end try
        set frontmost to true
        delay 0.25
        keystroke "w" using {{command down}}
        return "Cmd+W"
      end tell
    end tell
    """
    out = subprocess.run(
        ["osascript", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )
    return (out.stdout or "").strip() or (out.stderr or "").strip() or "no result"


def close_current_proxy_browser() -> bool:
    """
    Close the proxy-browser window(s) after the post-Pay wait.

    Called once per workflow, only after the card connection succeeded and the
    ~60s payment wait has elapsed. It closes the proxy browser window(s) but
    never quits BlackBird and never touches the manager window, so the manager
    stays alive for the next proxy (and for /stop to control). Earlier proxies
    were already closed in their own iteration, so normally only the current
    proxy's browser is open at this point.
    """
    if not is_blackbird_running():
        print("[INFO] BlackBird is off — no proxy browser to close")
        return True

    # Required first action for EVERY successful workflow. The 60-second wait
    # has already completed in fill_stripe_checkout before this function is
    # called. Do not gate this click on AX window detection: that detection was
    # the reason the old implementation could skip the close entirely.
    screen_w, screen_h = pyautogui.size()
    ref_w, ref_h = BROWSER_CLOSE_REFERENCE_SCREEN
    ref_x, ref_y = BROWSER_CLOSE_REFERENCE_POINT
    close_x = max(1, int(round(ref_x * screen_w / ref_w)))
    close_y = max(1, int(round(ref_y * screen_h / ref_h)))
    print(
        "[INFO] UNCONDITIONAL browser close click after 60s payment wait: "
        f"reference=({ref_x},{ref_y})/{ref_w}x{ref_h} → "
        f"live=({close_x},{close_y})/{screen_w}x{screen_h}"
    )
    _release_modifier_keys()
    _release_mouse_buttons()
    fast_click_at(close_x, close_y)
    time.sleep(1.2)

    initial = count_proxy_browser_windows()
    if initial <= 0:
        print(
            "[INFO] Unconditional close click complete; no proxy browser was "
            "detected afterward"
        )
        return True

    print(f"[INFO] Closing all proxy browsers after payment wait: {initial} open")
    # Normally one browser exists. The larger bound also clears leftovers from a
    # prior failed close without risking an unbounded loop.
    max_rounds = initial + 3
    for round_no in range(1, max_rounds + 1):
        before = count_proxy_browser_windows()
        if before <= 0:
            _release_modifier_keys()
            print("[INFO] Proxy browser close VERIFIED: 0 browser windows remain")
            return True

        ax_result = _close_one_proxy_browser_ax()
        time.sleep(0.7)
        after_ax = count_proxy_browser_windows()
        print(
            f"[INFO] Browser close round {round_no}: AX={ax_result!r}; "
            f"windows {before} → {after_ax}"
        )
        if after_ax < before:
            continue

        close_point = _proxy_browser_close_point()
        if close_point is not None:
            print(
                "[INFO] AX close did not work — physically clicking browser "
                f"close control at {close_point}"
            )
            _release_modifier_keys()
            _release_mouse_buttons()
            fast_click_at(close_point[0], close_point[1])
            time.sleep(0.9)
        after_click = count_proxy_browser_windows()
        print(f"[INFO] Browser close coordinate check: {after_ax} → {after_click}")
        if after_click < after_ax:
            continue

        cmd_result = _close_one_proxy_browser_cmd_w()
        time.sleep(0.9)
        after_cmd = count_proxy_browser_windows()
        print(
            f"[INFO] Browser close keyboard fallback={cmd_result!r}; "
            f"windows {after_click} → {after_cmd}"
        )
        if after_cmd < after_click:
            continue

        # The screenshot's custom close control is at screen (14, 30) when the
        # browser starts at (0, 21). If AX omitted the true window frame, retry
        # both common title-bar offsets around the live top-left corner.
        frame = get_proxy_browser_frame()
        if frame is not None:
            x, y, _width, _height = frame
            for offset_y in (9, 14):
                point = (x + 14, y + offset_y)
                print(f"[INFO] Browser close hard coordinate retry at {point}")
                _release_mouse_buttons()
                fast_click_at(point[0], point[1])
                time.sleep(0.7)
                if count_proxy_browser_windows() < after_cmd:
                    break

    remaining = count_proxy_browser_windows()
    _release_modifier_keys()
    if remaining <= 0:
        print("[INFO] Proxy browser close VERIFIED: 0 browser windows remain")
        return True
    print(
        "[ERROR] Proxy browser close FAILED after AX, physical click, and Cmd+W; "
        f"{remaining} browser window(s) still open"
    )
    return False


def close_proxy_browser_best_effort(reason: str) -> None:
    """
    Close this proxy's browser on a failure path.

    The successful path treats a stuck browser as fatal, but a workflow that
    already failed must not also abort the remaining proxies — a leftover window
    would otherwise sit in front of the manager and swallow the next New profile
    click, so it is closed here on a best-effort basis.
    """
    print(f"[INFO] Closing proxy browser after failure: {reason}")
    try:
        if close_current_proxy_browser():
            return
        print(f"[WARN] Proxy browser still open after failure close ({reason})")
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] Proxy browser close raised on failure path ({reason}): {exc}")


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
    # Setup phase for the NEXT proxy begins here. Successful payment paths close
    # their browser first; failure paths can still leave one open, so bring the
    # manager to the front for its coordinate-driven controls.
    raise_manager_to_front()
    _release_modifier_keys()
    time.sleep(0.3)
    print("[INFO] Ready for next proxy — manager on top for New profile clicks")


def create_profile_with_proxy(proxy: str) -> bool:
    """
    New profile → 3s → New Proxy → 3s → [SOCKS5 if proxy_type=socks5] →
    type proxy (exact) → 3s → Create.
    Manager is unminimized so New profile is clickable; never left covering browsers after.
    """
    if not is_blackbird_running():
        print("[ERROR] BlackBird is not running — not relaunching")
        return False
    detect_and_apply_layout()
    dismiss_open_sheets()
    restore_manager_windows()
    # Manager must be on top so New profile / New Proxy / Create clicks land on
    # it, including after a failure path left a proxy browser open.
    raise_manager_to_front()
    time.sleep(0.2)

    if not ensure_network_warning_dismissed():
        print("[ERROR] Network modal blocks workflow — will not click New profile")
        return False

    if not retry_manager_click(
        lambda: click_named_or_coord(
            "new_profile",
            "New profile",
            ["+ New profile", "+ New Profile"],
            activate_app=False,
        ),
        "New profile",
    ):
        return False
    wait_seconds(DELAY_STEP, "after New profile")

    if not retry_manager_click(click_new_proxy, "New Proxy"):
        return False
    wait_seconds(DELAY_STEP, "after New Proxy")

    # Optional only for --proxy-type socks5. HTTP keeps original New Proxy → input.
    if PROXY_TYPE == "socks5":
        if not retry_manager_click(click_socks5_protocol, "SOCKS5"):
            return False
        wait_seconds(DELAY_STEP, "after SOCKS5")

    if not retry_manager_click(
        lambda: click_target("proxy_input", "Proxy input field", activate_app=False),
        "Proxy input field",
    ):
        return False
    time.sleep(0.35)
    clear_field_macos()
    print(f"[INFO] Typing proxy from data.txt (exact case): {proxy!r}")
    human_type(proxy)
    wait_seconds(DELAY_STEP, "after proxy input")

    if not retry_manager_click(
        lambda: click_named_or_coord(
            "create_profile",
            "Create profile",
            ["Create profile", "Create Profile"],
            activate_app=False,
        ),
        "Create profile",
    ):
        return False
    time.sleep(1.2)
    dismiss_network_warning()
    ensure_network_warning_dismissed()
    detect_and_apply_layout()
    # Still in setup — Refresh/Play come next and are also manager clicks, so keep
    # the manager on top rather than raising the old browser.
    raise_manager_to_front()
    return True


def refresh_and_play() -> Tuple[bool, Dict[str, object]]:
    """
    Refresh → observe BlackBird's country/unreachable indicator for 3s → Play.

    No browser-window title monitoring is performed. The returned status is
    BlackBird's own post-Refresh signal: active, unreachable, or unknown.
    """
    if not is_blackbird_running():
        print("[ERROR] BlackBird is not running — not relaunching")
        return False, {"proxy_status": "unknown"}
    detect_and_apply_layout()
    time.sleep(0.3)
    # Refresh and Play are manager buttons clicked by coordinate — keep the
    # manager on top, including if a failure path left a browser open.
    raise_manager_to_front()
    if not retry_manager_click(
        lambda: click_target("open_profile", "Refresh (proxy icon)", activate_app=False),
        "Refresh (proxy icon)",
    ):
        return False, {"proxy_status": "unknown"}
    proxy_status = observe_proxy_status_after_refresh(DELAY_STEP)
    status_info: Dict[str, object] = {"proxy_status": proxy_status}
    print(f"[INFO] BlackBird post-Refresh proxy status: {proxy_status}")
    if not retry_manager_click(
        lambda: click_target("play_profile", "Play (▶)", activate_app=False),
        "Play (▶)",
    ):
        return False, status_info
    return True, status_info


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
    play_ok, proxy_info = refresh_and_play()
    if not play_ok:
        print(f"[ERROR] Workflow {index}/{total}: Refresh/Play failed for this line")
        return_to_blackbird_manager(f"workflow {index} play failed")
        return "setup_failed"

    # BlackBird's explicit red error is authoritative. A missing country badge
    # is only "unknown" (it may be brief), never proof that the proxy is dead.
    if proxy_info.get("proxy_status") == "unreachable":
        print(
            f"[WARN] Workflow {index}/{total}: proxy INACTIVE — BlackBird "
            "reported Proxy unreachable; moving to the next proxy"
        )
        return_to_blackbird_manager(f"workflow {index} inactive proxy")
        return "inactive"

    # Do not inspect, raise, minimize, or otherwise monitor windows here. Play
    # opens the browser in front. After the required 40s, click the exact address
    # bar location marked by the client and continue.
    wait_seconds(
        DELAY_AFTER_CONTINUE,
        "after Play (40s browser startup buffer; no window monitoring)",
    )

    if not enter_url_in_address_bar(checkout_url):
        close_proxy_browser_best_effort(f"workflow {index} URL failed")
        return_to_blackbird_manager(f"workflow {index} address bar / URL failed")
        return "stripe_failed"

    wait_seconds_keep_browser_front(
        DELAY_STRIPE_LOAD, "Stripe page full load (60s, browser held on top)"
    )
    ensure_browser_covers_blackbird()

    if not fill_stripe_checkout(card, email):
        close_proxy_browser_best_effort(f"workflow {index} Stripe/Pay failed")
        return_to_blackbird_manager(f"workflow {index} Stripe/Pay failed")
        return "stripe_failed"

    # Card connection succeeded and the ~60s payment wait already elapsed inside
    # fill_stripe_checkout. Per updated spec, close this proxy's browser now so
    # an inactive proxy's original browser cannot re-open the card connection.
    closed = close_current_proxy_browser()
    if not closed:
        print("[WARN] Proxy browser still open — running the full close sequence again")
        ensure_browser_covers_blackbird()
        time.sleep(1.0)
        closed = close_current_proxy_browser()
    if not closed:
        raise RuntimeError(
            f"Workflow {index}/{total}: proxy browser could not be closed; "
            "refusing to start the next proxy while the previous browser is open"
        )

    if index < total:
        # More proxies remain: the next workflow needs the manager in front for
        # New profile / Refresh / Play coordinate clicks.
        return_to_blackbird_manager(f"workflow {index}/{total} Pay done — next proxy")
    else:
        # Final proxy: the browser has been closed above. Keep BlackBird itself
        # running (only /stop may quit it); the normal-completion block just
        # re-activates the manager after the batch summary is calculated.
        print(
            "[INFO] Final proxy complete — proxy browser closed; BlackBird "
            "manager stays running until normal-completion window cleanup"
        )
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
    p.add_argument(
        "--proxy-type",
        choices=["http", "socks5"],
        default="http",
        help="After New Proxy: http=input field (default); socks5=click SOCKS5 then input",
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
    print(f"[INFO] z-order: manager on top ONLY during profile setup; browser on top during Stripe")
    print(f"[INFO] place_blackbird_on_top: DELETED (absolute no-op)")
    print(f"[INFO] relaunch-if-closed: DISABLED (single launch only)")
    print(
        "[INFO] after-Play: 40s fixed buffer → direct marked address-bar click "
        "(no browser-window monitoring)"
    )
    print(f"[INFO] Stripe: 60s page load → card fill → Pay → 60s → close browser")
    print(f"[INFO] pairing: workflows=len(proxies); cards cycle; random email per proxy")

    if sys.platform != "darwin":
        print("")
        print("[ERROR] This automation requires macOS (BlackBird + Accessibility + osascript).")
        print("[ERROR] On the Mac, from the project folder, run:  bash run.sh")
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
    global LAYOUT_FORCE, PROXY_TYPE
    LAYOUT_FORCE = None if args.layout == "auto" else args.layout
    PROXY_TYPE = args.proxy_type

    w, h = pyautogui.size()
    print(f"[INFO] Screen size: {w}x{h} (calibrated {BASE_SCREEN[0]}x{BASE_SCREEN[1]})")
    print(f"[INFO] Script dir: {SCRIPT_DIR}")
    print(f"[INFO] Proxy type: {PROXY_TYPE}")
    print("[INFO] Loaded STATE A (known-good calibration)")
    restore_state_a("boot")

    try:
        # Proxy list order = data.txt top→bottom. One line → one workflow.
        # Cards cycle. Emails are generated randomly, one per proxy.
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
    # Cards wrap; every proxy gets its own random name + random 4-digit email.
    total = len(proxies)
    # Generated once so the preview below shows the addresses actually used.
    generated_emails = [random_email_for_proxy(emails[0]) for _ in range(total)]
    print("")
    print("=" * 60)
    print(
        f"[INFO] RULE: {total} data.txt proxy line(s) → exactly {total} workflow(s)"
    )
    print(
        f"[INFO] Pairing: {len(cards)} card(s) cycle; one random email per proxy "
        f"(random name + random 4 digits, domain from {emails[0]!r})"
    )
    print("[INFO] Reading order: top → bottom for every file")
    print("=" * 60)
    for i, px in enumerate(proxies, 1):
        card_preview = cyclic_pick(cards, i - 1)
        email_preview = generated_emails[i - 1]
        card_slot = ((i - 1) % len(cards)) + 1
        print(f"[INFO]   workflow {i}/{total}")
        print(f"[INFO]     proxy[{i}/{total}]: {px!r}")
        print(f"[INFO]     card[{card_slot}/{len(cards)}]: {card_preview!r}")
        print(f"[INFO]     email[random {i}/{total}]: {email_preview!r}")
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
            email = generated_emails[i - 1]
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
    print("[INFO] CORRECT — every data.txt proxy line was processed")
    print(f"[INFO] Total proxies: {total}")
    print(f"[INFO] Active proxies (browser opened): {active}/{total}")
    print(f"[INFO] Cards connected (Stripe Pay clicked): {counts['paid']}/{active if active else 0}")
    print(f"[INFO] Inactive proxies (no browser, skipped): {counts['inactive']}")
    print(f"[INFO] Card/Stripe failed: {counts['stripe_failed']}")
    print(f"[INFO] Setup failed: {counts['setup_failed']}")
    print("=" * 60)
    # Normal completion owns only this automation process. Each proxy browser
    # was closed right after its own payment wait, so this step just re-activates
    # the BlackBird manager (and minimizes any stray browser window as a safety
    # net). It is deliberately not used by /stop or between workflows.
    minimize_all_proxy_browsers_for_completion()
    print(
        "[INFO] Normal completion: proxy browsers were closed after each "
        "payment; BlackBird manager remains running and active. "
        "Only automation is closing."
    )
    _write_run_results(outcome="completed", total=total, counts=counts)
    sys.exit(0)


if __name__ == "__main__":
    main()
