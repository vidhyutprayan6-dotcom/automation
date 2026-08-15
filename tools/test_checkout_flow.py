#!/usr/bin/env python3
"""
End-to-end check of fill_stripe_checkout against a simulated checkout.

The detector tests only prove that fields can be found. This one drives the
real fill_stripe_checkout through a page that behaves like the live one: clicks
land on whichever rectangle actually contains them, typing goes into the field
that was last clicked, and ticking the save-info box adds or removes the phone
row so the page reflows underneath the automation exactly as Stripe's does.

It fails if any value lands in the wrong field, if any required input is left
blank, if the save-info box does not end up unticked, or if Pay is not pressed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import main  # noqa: E402
from synth_stripe import render  # noqa: E402

CARD = {
    "raw": "4242424242424242|12/29|123|Jane Roe",
    "number": "4242424242424242",
    "expiry": "12/29",
    "mm": "12",
    "yy": "29",
    "cvc": "123",
    "name": "Jane Roe",
}
EMAIL = "jamesbrooks3184@outlook.com"

# Where each typed value is expected to end up.
EXPECTED = {
    "email": EMAIL,
    "card_number": CARD["number"],
    "card_expiry": CARD["expiry"],
    "card_cvc": CARD["cvc"],
    "card_name": CARD["name"],
}


class FakeCheckout:
    """A clickable, reflowing stand-in for the live Stripe page."""

    def __init__(self, **variant) -> None:
        self.variant = variant
        self.toggle_checked = bool(variant.get("toggle_checked", False))
        self.values: Dict[str, str] = {}
        self.focus: Optional[str] = None
        self.clicks: List[Tuple[int, int, Optional[str]]] = []
        self.stray_clicks: List[Tuple[int, int]] = []
        self.paid = False
        self._render()

    def _render(self) -> None:
        opts = dict(self.variant)
        opts["toggle_checked"] = self.toggle_checked
        self.page = render(**opts)

    @property
    def screen(self) -> Tuple[int, int]:
        return self.page.image.size

    def capture(self) -> np.ndarray:
        return np.asarray(self.page.image)

    def _hit(self, x: int, y: int) -> Optional[str]:
        for key, (x0, y0, x1, y1) in self.page.boxes.items():
            if key == "card_expiry_row":
                continue
            if x0 <= x <= x1 and y0 <= y <= y1:
                return key
        return None

    def click(self, x: int, y: int) -> None:
        key = self._hit(x, y)
        self.clicks.append((x, y, key))
        if key is None:
            self.stray_clicks.append((x, y))
            self.focus = None
            return
        if key == "save_toggle":
            self.toggle_checked = not self.toggle_checked
            self.focus = None
            self._render()
        elif key == "pay":
            self.paid = True
            self.focus = None
        else:
            self.focus = key

    def type(self, text: str) -> None:
        if self.focus is None:
            return
        self.values[self.focus] = self.values.get(self.focus, "") + text


def run_case(**variant) -> List[str]:
    page = FakeCheckout(**variant)
    problems: List[str] = []

    noop = lambda *a, **k: None  # noqa: E731

    patches = {
        "grab_screen": page.capture,
        "human_move_to": noop,
        "_quartz_left_click": page.click,
        "_release_modifier_keys": noop,
        "_release_mouse_buttons": noop,
        "dismiss_autofill_popup": noop,
        "ensure_browser_covers_blackbird": lambda *a, **k: True,
        "demote_manager_windows": noop,
        "scroll_browser_to_bottom": noop,
        "wait_seconds": noop,
        "paste_exact": page.type,
        "human_type": page.type,
    }
    saved = {name: getattr(main, name) for name in patches}
    saved_sleep = main.time.sleep
    saved_size = main.pyautogui.size

    for name, fn in patches.items():
        setattr(main, name, fn)
    main.time.sleep = noop
    main.pyautogui.size = lambda: page.screen
    try:
        ok = main.fill_stripe_checkout(CARD, EMAIL)
    finally:
        for name, fn in saved.items():
            setattr(main, name, fn)
        main.time.sleep = saved_sleep
        main.pyautogui.size = saved_size

    if not ok:
        problems.append("fill_stripe_checkout returned False")
    if page.stray_clicks:
        problems.append(f"{len(page.stray_clicks)} click(s) missed every field: "
                        f"{page.stray_clicks[:3]}")
    for key, expected in EXPECTED.items():
        got = page.values.get(key)
        if got != expected:
            problems.append(f"{key}: got {got!r}, expected {expected!r}")
    if page.page.has_zip:
        zip_value = page.values.get("zip", "")
        if not (len(zip_value) == 5 and zip_value.isdigit()):
            problems.append(f"zip: got {zip_value!r}, expected 5 digits")
    elif "zip" in page.values:
        problems.append("zip filled on a form that has no postal-code row")
    if page.toggle_checked:
        problems.append("save-info checkbox left ticked")
    if not page.paid:
        problems.append("Pay was never clicked")
    return problems


def main_() -> int:
    variants = [
        dict(lang="en", with_zip=False, toggle_checked=False),
        dict(lang="en", with_zip=False, toggle_checked=True),
        dict(lang="en", with_zip=True, toggle_checked=False),
        dict(lang="en", with_zip=True, toggle_checked=True),
        dict(lang="de", with_zip=True, toggle_checked=True),
        dict(lang="fr", with_zip=False, toggle_checked=True),
        dict(lang="ja", with_zip=True, toggle_checked=False),
        dict(lang="en", with_zip=True, toggle_checked=True, errors=True),
        dict(lang="en", with_zip=True, toggle_checked=True, screen=(1920, 1080)),
        dict(lang="de", with_zip=False, toggle_checked=True, screen=(1920, 1080)),
    ]
    passed = failed = 0
    for variant in variants:
        title = " ".join(f"{k}={v}" for k, v in variant.items())
        problems = run_case(**variant)
        if problems:
            print(f"FAIL {title}")
            for p in problems:
                print(f"       {p}")
            failed += 1
        else:
            print(f"ok   {title}")
            passed += 1
    print("-" * 78)
    print(f"{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main_())
