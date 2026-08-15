#!/usr/bin/env python3
"""
Offline check for stripe_vision against the client's screenshots.

Each case lists the field centres the client circled in red, so a run reports a
real pass/fail rather than just "something was found". Detected boxes are also
drawn onto <name>-detected.png for eyeballing.

Two of the four captures are marked up, and the marker strokes sit directly on
the 1 px field borders the detector reads, erasing them. Those two are kept as
reference only: they are the source of the measured ground truth, and they are
what shows that the Australian and United States forms put the same fields at
different heights, but they cannot be scored. The unmarked capture of each of
those two layouts carries the pass/fail, and tools/test_stripe_vision_synth.py
covers the layout combinations no capture contains.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stripe_vision import detect_stripe_form  # noqa: E402

ASSETS = Path(
    r"C:\Users\Administrator\.cursor\projects\f-MY-PROJECT-automation\assets"
)
PREFIX = (
    "c__Users_Administrator_AppData_Roaming_Cursor_User_workspaceStorage_"
    "061093a6e6fe72d8e31891ced23ba3d0_images_"
)

# Red-box centres measured from the client's annotated captures.
CASES = [
    {
        "name": "AU-fields",
        "file": "image-ca9f9ada-5454-4ba2-9b7d-2a2aa3de8c82",
        "expect": {
            "email": (601, 68),
            "card_number": (604, 160),
            "card_expiry": (584, 180),
            "card_cvc": (669, 179),
            "card_name": (598, 221),
            "save_toggle": (562, 301),
        },
        "has_zip": False,
        "toggle_checked": True,
        "reference_only": True,
    },
    {
        "name": "AU-button",
        "file": "image-2a4c967c-1849-4d12-9572-f85c0a834982",
        "expect": {"pay": (653, 418)},
        "has_zip": False,
        "toggle_checked": False,
    },
    {
        "name": "US-fields",
        "file": "image-e8fe4ddb-8acc-4848-898b-f774adc13be9",
        "expect": {
            "email": (595, 86),
            "card_number": (603, 179),
            "card_expiry": (589, 197),
            "card_cvc": (669, 198),
            "card_name": (601, 238),
            "save_toggle": (562, 319),
        },
        "has_zip": True,
        "toggle_checked": True,
        "reference_only": True,
    },
    {
        "name": "US-button",
        "file": "image-b78f52ad-2f13-4eeb-bc87-2c14bd40ec5f",
        "expect": {"pay": (651, 408)},
        "has_zip": True,
        "toggle_checked": False,
    },
]

def remove_annotations(img: Image.Image) -> Image.Image:
    """
    Paint out the client's red marker rectangles.

    Two captures have red boxes drawn just inside the fields they point at,
    which is an artefact of the annotation rather than anything the automation
    will ever see. Each red pixel is replaced by its nearest unmarked
    neighbour — along the column for the long horizontal strokes and along the
    row for the short vertical ones. Copying a real pixel rather than blending
    matters here, because a blend would smear away the 1 px hairlines that the
    detector is being asked to find.
    """
    a = np.asarray(img.convert("RGB")).astype(np.int16)
    r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    # Catch the anti-aliased pink skirt as well as the solid stroke, but do not
    # dilate: a marker line can sit one pixel from a real field border, and
    # widening the mask would consume the border along with the ink.
    grown = (r - g > 14) & (r - b > 14) & (r > 110)
    if not grown.any():
        return img

    h, w = grown.shape
    out = a.copy()
    ys, xs = np.nonzero(grown)
    for y, x in zip(ys, xs):
        up = next((yy for yy in range(y - 1, -1, -1) if not grown[yy, x]), None)
        down = next((yy for yy in range(y + 1, h) if not grown[yy, x]), None)
        left = next((xx for xx in range(x - 1, -1, -1) if not grown[y, xx]), None)
        right = next((xx for xx in range(x + 1, w) if not grown[y, xx]), None)
        options = []
        if up is not None:
            options.append((y - up, (up, x)))
        if down is not None:
            options.append((down - y, (down, x)))
        if left is not None:
            options.append((x - left, (y, left)))
        if right is not None:
            options.append((right - x, (y, right)))
        if options:
            out[y, x] = a[min(options)[1]]
    return Image.fromarray(out.clip(0, 255).astype(np.uint8))


COLORS = {
    "email": (0, 160, 0),
    "card_number": (0, 90, 220),
    "card_expiry": (200, 120, 0),
    "card_cvc": (160, 0, 180),
    "card_name": (0, 150, 150),
    "country": (120, 120, 120),
    "zip": (220, 60, 0),
    "save_toggle": (255, 0, 255),
    "pay": (0, 0, 0),
}


def main() -> int:
    failures = 0
    for case in CASES:
        reference_only = case.get("reference_only", False)
        path = ASSETS / (PREFIX + case["file"] + ".png")
        img = remove_annotations(Image.open(path).convert("RGB"))
        form = detect_stripe_form(np.asarray(img))

        print("=" * 74)
        suffix = "   [reference only — marker ink covers the field borders]" if reference_only else ""
        print(f"{case['name']}   {img.size[0]}x{img.size[1]}{suffix}")
        if reference_only:
            print("  measured ground truth from the red boxes:")
            for key, pt in case["expect"].items():
                print(f"    {key:<13} ({pt[0]:4d},{pt[1]:4d})")
            print(f"    has_zip={case['has_zip']} toggle_checked={case['toggle_checked']}")
            print(f"  detector on the repaired image: "
                  f"{'no detection' if form is None else 'detected'}")
            continue
        if form is None:
            print("  DETECTION FAILED")
            failures += 1
            continue

        for note in form.notes:
            print(f"  note: {note}")

        for key in (
            "email",
            "card_number",
            "card_expiry",
            "card_cvc",
            "card_name",
            "country",
            "zip",
            "save_toggle",
            "pay",
        ):
            pt = form.points.get(key)
            if pt is None:
                continue
            exp = case["expect"].get(key)
            if exp is None:
                print(f"  {key:<13} -> ({pt[0]:4d},{pt[1]:4d})")
                continue
            dx, dy = pt[0] - exp[0], pt[1] - exp[1]
            # A click anywhere inside the box works, so allow generous slack on
            # x and keep y tight because rows are only ~20 px apart.
            ok = abs(dx) <= 45 and abs(dy) <= 6
            if not ok:
                failures += 1
            print(
                f"  {key:<13} -> ({pt[0]:4d},{pt[1]:4d})  expected ({exp[0]:4d},{exp[1]:4d})"
                f"  d=({dx:+3d},{dy:+3d})  {'ok' if ok else 'FAIL'}"
            )

        for key in case["expect"]:
            if key not in form.points:
                print(f"  {key:<13} -> MISSING   FAIL")
                failures += 1

        if form.has_zip != case["has_zip"]:
            print(f"  has_zip -> {form.has_zip} expected {case['has_zip']}   FAIL")
            failures += 1
        else:
            print(f"  has_zip -> {form.has_zip}  ok")

        if form.toggle_checked != case["toggle_checked"]:
            print(
                f"  toggle_checked -> {form.toggle_checked} "
                f"expected {case['toggle_checked']}   FAIL"
            )
            failures += 1
        else:
            print(f"  toggle_checked -> {form.toggle_checked}  ok")

        out = img.copy()
        draw = ImageDraw.Draw(out)
        for key, box in form.boxes.items():
            if key == "card_expiry_row":
                continue
            color = COLORS.get(key, (255, 0, 0))
            draw.rectangle(box, outline=color, width=1)
        for key, pt in form.points.items():
            color = COLORS.get(key, (255, 0, 0))
            draw.line((pt[0] - 4, pt[1], pt[0] + 4, pt[1]), fill=color, width=1)
            draw.line((pt[0], pt[1] - 4, pt[0], pt[1] + 4), fill=color, width=1)
        dest = Path(__file__).resolve().parent / f"{case['name']}-detected.png"
        out.save(dest)
        print(f"  overlay: {dest}")

    print("=" * 74)
    print("ALL CHECKS PASSED" if failures == 0 else f"{failures} CHECK(S) FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
