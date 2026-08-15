#!/usr/bin/env python3
"""
Layout-variation tests for stripe_vision.

The real captures prove the detector copes with genuine rendering; these cases
prove it copes with layouts the captures do not contain — other languages, the
ZIP row, the phone row shown while save-info is ticked, inline error lines,
different scroll offsets and real screen resolutions.

A field passes when its click point lands inside the rectangle the renderer
actually drew, which is the only thing that matters for a click.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from stripe_vision import detect_stripe_form  # noqa: E402
from synth_stripe import render  # noqa: E402

CHECKED = ("email", "card_number", "card_expiry", "card_cvc", "card_name", "pay")

VARIANTS = [
    dict(lang="en", with_zip=False, toggle_checked=False),
    dict(lang="en", with_zip=False, toggle_checked=True),
    dict(lang="en", with_zip=True, toggle_checked=False),
    dict(lang="en", with_zip=True, toggle_checked=True),
    dict(lang="en", with_zip=True, toggle_checked=True, errors=True),
    dict(lang="de", with_zip=False, toggle_checked=True),
    dict(lang="de", with_zip=True, toggle_checked=False),
    dict(lang="fr", with_zip=False, toggle_checked=False),
    dict(lang="fr", with_zip=True, toggle_checked=True),
    dict(lang="ja", with_zip=False, toggle_checked=True),
    dict(lang="ja", with_zip=True, toggle_checked=False),
    dict(lang="en", with_zip=False, toggle_checked=True, scroll=18),
    dict(lang="de", with_zip=True, toggle_checked=True, scroll=-12),
]

SCREENS = [
    ("1024x576", (1024, 576), 1.0),
    ("1920x1080", (1920, 1080), 1.0),
    ("2560x1440", (2560, 1440), 1.0),
    ("3840x2160 retina", (1920, 1080), 2.0),
]


def inside(box, point, margin: int = 1) -> bool:
    x0, y0, x1, y1 = box
    x, y = point
    return (x0 + margin) <= x <= (x1 - margin) and (y0 + margin) <= y <= (y1 - margin)


def main() -> int:
    passed = failed = 0
    for screen_name, screen, scale in SCREENS:
        for variant in VARIANTS:
            page = render(screen=screen, scale=scale, **variant)
            form = detect_stripe_form(np.asarray(page.image))
            title = (
                f"{screen_name:<17} lang={variant['lang']} "
                f"zip={int(variant['with_zip'])} toggle={int(variant['toggle_checked'])} "
                f"err={int(variant.get('errors', False))} scroll={variant.get('scroll', 0):>3}"
            )

            if form is None:
                print(f"FAIL {title}  -> no detection")
                failed += 1
                continue

            problems = []
            for key in CHECKED:
                point = form.points.get(key)
                if point is None:
                    problems.append(f"{key} missing")
                    continue
                truth = page.boxes[key]
                if not inside(truth, point):
                    problems.append(f"{key} {point} outside {truth}")

            if page.has_zip:
                point = form.points.get("zip")
                if point is None or not inside(page.boxes["zip"], point):
                    problems.append(f"zip {point} outside {page.boxes.get('zip')}")
            elif form.has_zip:
                problems.append("zip reported but page has none")

            point = form.points.get("save_toggle")
            if point is None or not inside(page.boxes["save_toggle"], point, margin=0):
                problems.append(f"save_toggle {point} outside {page.boxes['save_toggle']}")
            elif form.toggle_checked != page.toggle_checked:
                problems.append(
                    f"toggle_checked={form.toggle_checked} "
                    f"expected {page.toggle_checked}"
                )

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
    raise SystemExit(main())
