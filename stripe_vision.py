#!/usr/bin/env python3
"""
Screen-based locator for the Stripe checkout payment form.

The checkout layout is not stable across proxies: the country decides whether a
ZIP row exists, whether a phone row is shown, and how tall the localized labels
are, so every row can sit at a different y from one run to the next. Instead of
fixed coordinates this module reads the geometry straight off a screenshot.

Nothing here depends on the interface language. The form is found through its
drawing, not its words:

  * the submit button is the only large saturated-blue rectangle on the page,
    which fixes the form column and the bottom of the search area,
  * each input is outlined by a 1 px border, so a row is a pair of horizontal
    border lines carrying a matching vertical border on the right,
  * that vertical border is what separates a real input from the label gap
    above it: an input's side border is one row tall, while the panel container
    that also runs down the same area is far taller,
  * the email input spans the whole form column; the payment-panel rows are
    inset from it,
  * panel rows always appear in the same order, and the two rows that share a
    border are card-number/expiry-cvc and country/ZIP.

Coordinates are returned in the screenshot's own pixel space; the caller scales
them to click space.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

# Reference width the pixel thresholds below were tuned on. Everything is
# multiplied by (image_width / REFERENCE_WIDTH) so the same detector works on a
# 1024 px capture, a 1920 px screen and a 3840 px Retina grab.
REFERENCE_WIDTH = 1024.0

# A border pixel only has to be this much darker than the paper beside it. The
# margin is small on purpose: a 1 px hairline loses most of its contrast when
# the capture is downscaled, and the comparison is against immediate neighbours
# rather than an absolute white, so noise does not accumulate.
EDGE_DELTA = 1.5

Box = Tuple[int, int, int, int]  # x0, y0, x1, y1 inclusive


@dataclass
class StripeForm:
    """Everything the checkout filler needs, in screenshot pixel space."""

    boxes: Dict[str, Box] = field(default_factory=dict)
    points: Dict[str, Tuple[int, int]] = field(default_factory=dict)
    has_zip: bool = False
    toggle_checked: Optional[bool] = None
    notes: List[str] = field(default_factory=list)

    def missing(self, required: Sequence[str]) -> List[str]:
        return [k for k in required if k not in self.points]


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _luminance(rgb: np.ndarray) -> np.ndarray:
    return rgb.astype(np.float64).mean(axis=2)


def _components(mask: np.ndarray, max_pixels: int = 400_000) -> List[Box]:
    """Bounding boxes of 8-connected True regions, via an iterative flood fill."""
    h, w = mask.shape
    if not mask.any() or mask.sum() > max_pixels:
        return []
    seen = np.zeros_like(mask, dtype=bool)
    out: List[Box] = []
    ys, xs = np.nonzero(mask)
    for sy, sx in zip(ys, xs):
        if seen[sy, sx]:
            continue
        stack = [(int(sy), int(sx))]
        seen[sy, sx] = True
        x0 = x1 = int(sx)
        y0 = y1 = int(sy)
        while stack:
            cy, cx = stack.pop()
            if cx < x0:
                x0 = cx
            elif cx > x1:
                x1 = cx
            if cy < y0:
                y0 = cy
            elif cy > y1:
                y1 = cy
            for ny in range(cy - 1, cy + 2):
                if ny < 0 or ny >= h:
                    continue
                for nx in range(cx - 1, cx + 2):
                    if 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
        out.append((x0, y0, x1, y1))
    return out


def _runs(flags: np.ndarray) -> List[Tuple[int, int]]:
    """Inclusive (start, end) index pairs of every True run in a 1-D array."""
    out: List[Tuple[int, int]] = []
    start = None
    for i, v in enumerate(flags):
        if v and start is None:
            start = i
        elif not v and start is not None:
            out.append((start, i - 1))
            start = None
    if start is not None:
        out.append((start, len(flags) - 1))
    return out


def _close_gaps(flags: np.ndarray, k: int) -> np.ndarray:
    """
    Bridge holes of up to k-1 samples along axis 0, so anti-aliasing cannot
    split one border line into several short pieces.
    """
    if k < 2:
        return flags
    grown = flags.copy()
    for shift in range(1, k):
        grown[:-shift] |= flags[shift:]
    shrunk = grown.copy()
    for shift in range(1, k):
        shrunk[shift:] &= grown[:-shift]
    return shrunk | flags


def _run_around(flags: np.ndarray, index: int) -> Optional[Tuple[int, int]]:
    """Inclusive bounds of the True run covering index, or None."""
    if not flags[index]:
        return None
    start = index
    while start > 0 and flags[start - 1]:
        start -= 1
    end = index
    last = len(flags) - 1
    while end < last and flags[end + 1]:
        end += 1
    return start, end


# ---------------------------------------------------------------------------
# Anchor 1 — the submit button
# ---------------------------------------------------------------------------

def find_submit_button(rgb: np.ndarray) -> Optional[Box]:
    """
    Locate the blue Pay / Subscribe / Start-trial button.

    Matched on colour and shape only, so the caption may say anything in any
    language. The macOS Dock is the other big blue thing on screen and is
    rejected by its extreme aspect ratio and its place in the bottom strip.
    """
    h, w = rgb.shape[:2]
    s = w / REFERENCE_WIDTH
    a = rgb.astype(np.int32)
    r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    blue = (b > 120) & (b - r > 45) & (b - g > 25) & (g > 40)
    blue[int(h * 0.92):, :] = False

    best: Optional[Box] = None
    best_area = 0
    for x0, y0, x1, y1 in _components(blue):
        bw, bh = x1 - x0 + 1, y1 - y0 + 1
        if not (70 * s <= bw <= 620 * s) or not (16 * s <= bh <= 90 * s):
            continue
        if not (2.5 <= bw / bh <= 14.0):
            continue
        if blue[y0:y1 + 1, x0:x1 + 1].mean() < 0.80:
            continue
        if bw * bh > best_area:
            best_area, best = bw * bh, (x0, y0, x1, y1)
    return best


# ---------------------------------------------------------------------------
# Anchor 2 — border lines
# ---------------------------------------------------------------------------

def _vertical_edge_mask(lum: np.ndarray, s: float) -> np.ndarray:
    """True where a pixel is darker than the paper a few columns either side."""
    gap = max(2, int(round(3 * s)))
    left = np.empty_like(lum)
    right = np.empty_like(lum)
    left[:, gap:] = lum[:, :-gap]
    left[:, :gap] = lum[:, :gap]
    right[:, :-gap] = lum[:, gap:]
    right[:, -gap:] = lum[:, -gap:]
    mask = (lum < left - EDGE_DELTA) & (lum < right - EDGE_DELTA)
    mask[:, :gap] = False
    mask[:, -gap:] = False
    return mask


def _horizontal_borders(
    lum: np.ndarray,
    x0: int,
    x1: int,
    y_top: int,
    y_bottom: int,
    s: float,
) -> List[int]:
    """
    Rows darker than the paper both above and below across most of the form
    width. Comparing against local neighbours instead of an absolute threshold
    keeps this working under Night Shift and with softened 1 px borders.
    """
    gap = max(2, int(round(3 * s)))
    y_from = max(y_top, gap)
    y_to = min(y_bottom, lum.shape[0] - gap - 1)
    if y_to <= y_from:
        return []

    band = lum[y_from:y_to, x0:x1 + 1]
    above = lum[y_from - gap:y_to - gap, x0:x1 + 1]
    below = lum[y_from + gap:y_to + gap, x0:x1 + 1]
    hit = ((band < above - EDGE_DELTA) & (band < below - EDGE_DELTA)).mean(axis=1)
    rows = [y_from + i for i, v in enumerate(hit) if v > 0.75]

    merged: List[int] = []
    run: List[int] = []
    tol = max(2, int(round(2 * s)))
    for y in rows:
        if run and y - run[-1] > tol:
            merged.append(int(round(sum(run) / len(run))))
            run = []
        run.append(y)
    if run:
        merged.append(int(round(sum(run) / len(run))))
    return merged


def _right_edge_for_band(
    vmask: np.ndarray,
    vclosed: np.ndarray,
    top: int,
    bottom: int,
    x_lo: int,
    x_hi: int,
    s: float,
) -> Optional[int]:
    """
    The x of the row's own right-hand border, or None if the band is only a gap
    between two inputs.

    A row border is a short vertical run that covers this band and little else.
    The payment panel's container border lives in the same strip but runs the
    whole height of the panel, so it is filtered out by length.
    """
    pad = max(2, int(round(3 * s)))
    y0, y1 = top + pad, bottom - pad
    if y1 <= y0:
        return None
    height = bottom - top
    max_len = max(int(round(3.2 * height)), int(round(30 * s)))
    min_len = max(0.45 * height, 4 * s)
    middle = (y0 + y1) // 2

    x_from = max(0, x_lo)
    x_to = min(vmask.shape[1], x_hi + 1)
    coverage = vmask[y0:y1 + 1, x_from:x_to].mean(axis=0)

    best_x: Optional[int] = None
    best_cov = 0.0
    for offset in np.argsort(-coverage):
        cov = float(coverage[offset])
        if cov < 0.45 or cov <= best_cov:
            break
        span = _run_around(vclosed[:, x_from + int(offset)], middle)
        if span is None:
            continue
        length = span[1] - span[0] + 1
        # Too short is stray text; too long is the payment panel's own container
        # rule, which keeps going well past the band. A field's border stops
        # with the field, or with the row stacked onto it.
        if length < min_len or length > max_len:
            continue
        best_cov, best_x = cov, x_from + int(offset)
        break
    return best_x


def _detect_rows(
    lum: np.ndarray,
    vmask: np.ndarray,
    vclosed: np.ndarray,
    fx0: int,
    fx1: int,
    y_top: int,
    y_bottom: int,
    s: float,
) -> List[Dict[str, object]]:
    """Every input rectangle between y_top and y_bottom, ordered top to bottom."""
    borders = _horizontal_borders(lum, fx0, fx1, y_top, y_bottom, s)
    centre = (fx0 + fx1) / 2.0
    x_lo = int(round(fx1 - 16 * s))
    x_hi = int(round(fx1 + 8 * s))

    rows: List[Dict[str, object]] = []
    min_h, max_h = 13 * s, 30 * s
    for top, bottom in zip(borders, borders[1:]):
        if not (min_h <= bottom - top <= max_h):
            continue
        right = _right_edge_for_band(vmask, vclosed, top, bottom, x_lo, x_hi, s)
        if right is None:
            continue
        # The checkout column is centred, so the left edge mirrors the right.
        left = int(round(2 * centre - right))
        rows.append({"box": (left, top, right, bottom), "right": right})
    return rows


def _find_column_divider(vmask: np.ndarray, box: Box, s: float) -> Optional[int]:
    """The vertical rule that splits the expiry | CVC row into two cells."""
    x0, y0, x1, y1 = box
    pad = max(2, int(round(3 * s)))
    ya, yb = y0 + pad, y1 - pad
    lo = max(0, x0 + int(round(25 * s)))
    hi = min(vmask.shape[1], x1 - int(round(25 * s)) + 1)
    if yb <= ya or hi <= lo:
        return None
    coverage = vmask[ya:yb + 1, lo:hi].mean(axis=0)
    offset = int(np.argmax(coverage))
    if float(coverage[offset]) >= 0.6:
        return lo + offset
    return None


# ---------------------------------------------------------------------------
# Anchor 3 — the save-information checkbox
# ---------------------------------------------------------------------------

def _find_checkbox(
    lum: np.ndarray,
    fx0: int,
    y_top: int,
    y_bottom: int,
    s: float,
) -> Optional[Tuple[Box, bool]]:
    """
    The small square left of the "save my information" label, and whether it is
    ticked. Only the narrow strip at the form's left edge is searched, which is
    where the box sits however long the label text happens to be.
    """
    if y_bottom - y_top < 4 * s:
        return None
    x_lo = max(0, int(round(fx0 - 8 * s)))
    x_hi = min(lum.shape[1] - 1, int(round(fx0 + 34 * s)))
    strip = lum[y_top:y_bottom, x_lo:x_hi + 1]
    if strip.size == 0:
        return None

    paper = float(np.percentile(strip, 92))
    mask = strip < paper - 10.0
    lo_side, hi_side = 7 * s, 20 * s

    found: List[Tuple[Box, bool]] = []
    for x0, y0, x1, y1 in _components(mask):
        bw, bh = x1 - x0 + 1, y1 - y0 + 1
        if not (lo_side <= bw <= hi_side and lo_side <= bh <= hi_side):
            continue
        if abs(bw - bh) > max(3.0, 5 * s):
            continue
        gx0, gy0 = x0 + x_lo, y0 + y_top
        gx1, gy1 = x1 + x_lo, y1 + y_top
        inset = max(1, int(round(2 * s)))
        inner = lum[gy0 + inset:gy1 - inset + 1, gx0 + inset:gx1 - inset + 1]
        if inner.size == 0:
            continue
        found.append(((gx0, gy0, gx1, gy1), float(inner.mean()) < paper - 55.0))

    if not found:
        return None
    # Topmost square wins: the save-info row always comes before anything else.
    found.sort(key=lambda c: c[0][1])
    return found[0]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

REQUIRED_FIELDS = (
    "email",
    "card_number",
    "card_expiry",
    "card_cvc",
    "card_name",
    "pay",
)


def detect_stripe_form(rgb: np.ndarray) -> Optional[StripeForm]:
    """
    Read a checkout screenshot and return every field's click point.

    Returns None when the button anchor or the card rows cannot be found, which
    is the caller's signal that the page is not the checkout, or not ready yet.
    """
    if rgb.ndim != 3 or rgb.shape[2] < 3:
        return None
    rgb = rgb[:, :, :3]
    h, w = rgb.shape[:2]
    s = w / REFERENCE_WIDTH
    lum = _luminance(rgb)

    button = find_submit_button(rgb)
    if button is None:
        return None
    bx0, by0, bx1, by1 = button

    form = StripeForm()
    form.boxes["pay"] = button
    form.points["pay"] = ((bx0 + bx1) // 2, (by0 + by1) // 2)

    vmask = _vertical_edge_mask(lum, s)
    vclosed = _close_gaps(vmask, max(2, int(round(3 * s))))
    rows = _detect_rows(
        lum, vmask, vclosed, bx0, bx1, int(h * 0.04), by0 - int(round(4 * s)), s
    )
    if len(rows) < 4:
        form.notes.append(f"only {len(rows)} input rows found")
        return None

    # Rows reaching the far right of the column sit outside the payment panel
    # (that is the email row); the inset ones are the panel's own fields.
    outer_right = max(int(r["right"]) for r in rows)
    inset_cut = outer_right - max(3.0, 4 * s)
    panel_rows = [r for r in rows if int(r["right"]) < inset_cut]
    outer_rows = [r for r in rows if int(r["right"]) >= inset_cut]
    if len(panel_rows) < 4:
        form.notes.append(f"only {len(panel_rows)} panel rows found")
        return None

    # --- card group: the first two panel rows that share a border.
    card_idx = None
    for i in range(len(panel_rows) - 1):
        top_box = panel_rows[i]["box"]
        low_box = panel_rows[i + 1]["box"]
        if abs(low_box[1] - top_box[3]) <= max(2.0, 3 * s):
            card_idx = i
            break
    if card_idx is None:
        form.notes.append("card number / expiry pair not found")
        return None

    card_box: Box = panel_rows[card_idx]["box"]  # type: ignore[assignment]
    exp_box: Box = panel_rows[card_idx + 1]["box"]  # type: ignore[assignment]
    rest = panel_rows[card_idx + 2:]
    if len(rest) < 2:
        form.notes.append("cardholder name / country rows not found")
        return None

    name_box: Box = rest[0]["box"]  # type: ignore[assignment]
    country_box: Box = rest[1]["box"]  # type: ignore[assignment]

    form.boxes["card_number"] = card_box
    form.boxes["card_expiry_row"] = exp_box
    form.boxes["card_name"] = name_box
    form.boxes["country"] = country_box

    # --- ZIP: the row stacked straight under the country select. A phone row
    # sits well below, after the save-info block, so it fails this test.
    if len(rest) >= 3:
        zip_box: Box = rest[2]["box"]  # type: ignore[assignment]
        if abs(zip_box[1] - country_box[3]) <= max(2.0, 3 * s):
            form.has_zip = True
            form.boxes["zip"] = zip_box
            form.points["zip"] = _centre(zip_box)

    # --- email: the full-width row closest above the payment panel.
    above = [r for r in outer_rows if r["box"][3] <= card_box[1]]
    if not above:
        above = [r for r in rows if r["box"][3] <= card_box[1]]
    if not above:
        form.notes.append("email row not found")
        return None
    form.boxes["email"] = above[-1]["box"]  # type: ignore[assignment]

    form.points["email"] = _centre(form.boxes["email"])
    form.points["card_number"] = _centre(card_box)
    form.points["card_name"] = _centre(name_box)
    form.points["country"] = _centre(country_box)

    # --- expiry | CVC share one row, split by a vertical rule.
    ex0, ey0, ex1, ey1 = exp_box
    ey = (ey0 + ey1) // 2
    divider = _find_column_divider(vmask, exp_box, s)
    if divider is None:
        divider = ex0 + int(round((ex1 - ex0) * 0.47))
        form.notes.append("expiry/CVC divider not found; used proportional split")
    form.points["card_expiry"] = ((ex0 + divider) // 2, ey)
    # Stay clear of the card-scan glyph parked at the right end of the CVC cell.
    cvc_right = max(divider + int(round(10 * s)), ex1 - int(round(34 * s)))
    form.points["card_cvc"] = ((divider + cvc_right) // 2, ey)
    form.boxes["card_expiry"] = (ex0, ey0, divider, ey1)
    form.boxes["card_cvc"] = (divider, ey0, ex1, ey1)

    # --- save-information checkbox, between the last panel row and the button.
    last_bottom = max(
        b[3] for k, b in form.boxes.items() if k not in ("pay", "card_expiry", "card_cvc")
    )
    found = _find_checkbox(
        lum, card_box[0], last_bottom + int(round(4 * s)), by0, s
    )
    if found is not None:
        cb_box, checked = found
        form.boxes["save_toggle"] = cb_box
        form.points["save_toggle"] = _centre(cb_box)
        form.toggle_checked = checked
    else:
        form.notes.append("save-info checkbox not found")

    return form


def _centre(box: Box) -> Tuple[int, int]:
    x0, y0, x1, y1 = box
    return ((x0 + x1) // 2, (y0 + y1) // 2)
