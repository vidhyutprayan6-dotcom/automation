#!/usr/bin/env python3
"""
Renders Stripe-checkout look-alikes with known field rectangles.

The client only supplied Australian and United States captures, but the
automation has to cope with any country the proxy lands in. This module builds
pages with the same drawing rules as the real checkout while varying everything
that shifts the layout: label wording and length, the ZIP row, the phone row
that appears while save-info is ticked, inline error lines, scroll offset and
screen resolution. Each page comes with the exact rectangles it drew, so a test
can assert that a detected click point really lands inside the right input.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Tuple

from PIL import Image, ImageDraw, ImageFont

Box = Tuple[int, int, int, int]

PAPER = (255, 255, 255)
BORDER = (222, 224, 228)
PANEL_BORDER = (228, 230, 234)
LABEL = (48, 52, 62)
PLACEHOLDER = (150, 154, 162)
BLUE = (26, 106, 214)
ERROR = (206, 60, 55)
CHECK_ON = (30, 34, 44)


@dataclass
class Page:
    image: Image.Image
    boxes: Dict[str, Box] = field(default_factory=dict)
    has_zip: bool = False
    toggle_checked: bool = False


LANGUAGES = {
    "en": {
        "email": "Email",
        "method": "Payment method",
        "card_tab": "Card",
        "card_info": "Card information",
        "name": "Cardholder name",
        "country": "Country or region",
        "save": "Save my information for faster checkout",
        "phone": "Phone number",
        "pay": "Start trial",
        "required": "REQUIRED",
    },
    "de": {
        "email": "E-Mail-Adresse",
        "method": "Zahlungsmethode",
        "card_tab": "Karte",
        "card_info": "Kartendaten und Zahlungsinformationen",
        "name": "Name des Karteninhabers",
        "country": "Land oder Region",
        "save": "Meine Informationen für einen schnelleren Bezahlvorgang speichern",
        "phone": "Telefonnummer",
        "pay": "Kostenlosen Testzeitraum starten",
        "required": "ERFORDERLICH",
    },
    "fr": {
        "email": "Adresse e-mail",
        "method": "Moyen de paiement",
        "card_tab": "Carte",
        "card_info": "Informations de la carte bancaire",
        "name": "Nom du titulaire de la carte",
        "country": "Pays ou région",
        "save": "Enregistrer mes informations pour un paiement plus rapide",
        "phone": "Numéro de téléphone",
        "pay": "Démarrer l'essai",
        "required": "OBLIGATOIRE",
    },
    "ja": {
        "email": "メールアドレス",
        "method": "支払い方法",
        "card_tab": "カード",
        "card_info": "カード情報",
        "name": "カード名義人",
        "country": "国または地域",
        "save": "次回のお支払いのために情報を保存する",
        "phone": "電話番号",
        "pay": "無料トライアルを開始",
        "required": "必須",
    },
}


def _font(size: int) -> ImageFont.ImageFont:
    for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf", "Helvetica.ttc"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render(
    *,
    scale: float = 1.0,
    lang: str = "en",
    with_zip: bool = False,
    toggle_checked: bool = False,
    errors: bool = False,
    scroll: int = 0,
    screen: Tuple[int, int] = (1024, 576),
) -> Page:
    """Draw one checkout page and report where every input ended up."""
    words = LANGUAGES[lang]
    W, H = int(screen[0] * scale), int(screen[1] * scale)
    img = Image.new("RGB", (W, H), (250, 249, 247))
    d = ImageDraw.Draw(img)

    u = scale * (screen[0] / 1024.0)  # one reference pixel in output pixels

    def px(v: float) -> int:
        return int(round(v * u))

    # Browser chrome, so the page is not the only thing on screen.
    d.rectangle((0, 0, W, px(44)), fill=(246, 246, 248))
    d.rectangle((0, px(44), W, H), fill=PAPER)
    d.rectangle((0, H - px(46), W, H), fill=(228, 230, 236))

    fx0, fx1 = px(551), px(754)
    inner0, inner1 = fx0 + px(9), fx1 - px(9)
    row_h = px(20)
    label_gap = px(21)
    f_label = _font(max(7, px(7.5)))
    f_input = _font(max(7, px(8)))

    boxes: Dict[str, Box] = {}
    y = px(60) - scroll

    def label(text: str, *, x: int, required: bool = False) -> None:
        nonlocal y
        d.text((x, y), text, font=f_label, fill=LABEL)
        if required and errors:
            d.text((fx1 - px(40), y), words["required"], font=f_label, fill=ERROR)
        y += label_gap - row_h + px(11)

    def input_row(key: str, placeholder: str, *, x0: int, x1: int) -> Box:
        nonlocal y
        box = (x0, y, x1, y + row_h)
        d.rounded_rectangle(box, radius=px(4), fill=PAPER, outline=BORDER, width=1)
        d.text((x0 + px(8), y + px(5)), placeholder, font=f_input, fill=PLACEHOLDER)
        boxes[key] = box
        y += row_h + px(1)
        return box

    label(words["email"], x=fx0, required=True)
    input_row("email", "email@example.com", x0=fx0, x1=fx1)
    y += label_gap

    d.text((fx0, y), words["method"], font=f_label, fill=LABEL)
    y += px(14)

    panel_top = y
    d.text((inner0, y + px(6)), words["card_tab"], font=f_label, fill=LABEL)
    y += px(40)

    label(words["card_info"], x=inner0, required=True)
    card_box = input_row("card_number", "1234 1234 1234 1234", x0=inner0, x1=inner1)
    for i, colour in enumerate(((26, 42, 132), (235, 90, 30), (0, 120, 200), (200, 40, 60))):
        bx = inner1 - px(10) - i * px(14)
        d.rounded_rectangle(
            (bx - px(11), card_box[1] + px(5), bx, card_box[1] + px(14)),
            radius=px(2),
            fill=colour,
        )

    y = card_box[3]
    exp_box = input_row("card_expiry_row", "", x0=inner0, x1=inner1)
    divider = (inner0 + inner1) // 2 - px(6)
    d.line((divider, exp_box[1] + 1, divider, exp_box[3] - 1), fill=BORDER, width=1)
    d.text((inner0 + px(8), exp_box[1] + px(5)), "MM / YY", font=f_input, fill=PLACEHOLDER)
    d.text((divider + px(8), exp_box[1] + px(5)), "CVC", font=f_input, fill=PLACEHOLDER)
    d.rounded_rectangle(
        (inner1 - px(20), exp_box[1] + px(5), inner1 - px(6), exp_box[1] + px(14)),
        radius=px(2),
        outline=PLACEHOLDER,
    )
    boxes["card_expiry"] = (inner0, exp_box[1], divider, exp_box[3])
    boxes["card_cvc"] = (divider, exp_box[1], inner1 - px(22), exp_box[3])
    y = exp_box[3] + px(1)

    y += label_gap - px(10)
    label(words["name"], x=inner0, required=True)
    input_row("card_name", "Full name on card", x0=inner0, x1=inner1)

    y += label_gap - px(10)
    label(words["country"], x=inner0)
    country = input_row("country", "United States" if with_zip else "Australia",
                        x0=inner0, x1=inner1)
    d.polygon(
        [
            (inner1 - px(14), country[1] + px(8)),
            (inner1 - px(8), country[1] + px(8)),
            (inner1 - px(11), country[1] + px(12)),
        ],
        fill=LABEL,
    )

    if with_zip:
        y = country[3]
        input_row("zip", "ZIP", x0=inner0, x1=inner1)

    panel_bottom = y + px(8)
    d.rounded_rectangle(
        (fx0, panel_top, fx1, panel_bottom), radius=px(6), outline=PANEL_BORDER, width=1
    )
    y = panel_bottom + px(14)

    cb = (fx0 + px(5), y, fx0 + px(16), y + px(11))
    if toggle_checked:
        d.rounded_rectangle(cb, radius=px(2), fill=CHECK_ON, outline=CHECK_ON)
        d.line((cb[0] + px(3), cb[1] + px(6), cb[0] + px(5), cb[1] + px(8)),
               fill=PAPER, width=max(1, px(1.4)))
        d.line((cb[0] + px(5), cb[1] + px(8), cb[0] + px(9), cb[1] + px(3)),
               fill=PAPER, width=max(1, px(1.4)))
    else:
        d.rounded_rectangle(cb, radius=px(2), fill=PAPER, outline=(178, 182, 190))
    boxes["save_toggle"] = cb
    d.text((fx0 + px(22), y), words["save"], font=f_label, fill=LABEL)
    y += px(24)

    if toggle_checked:
        label(words["phone"], x=fx0)
        input_row("phone", "+1 (201) 555-0123", x0=inner0, x1=inner1)
        y += px(10)

    pay = (fx0, y, fx1, y + px(30))
    d.rounded_rectangle(pay, radius=px(5), fill=BLUE)
    tw = d.textlength(words["pay"], font=f_input)
    d.text(((pay[0] + pay[2] - tw) / 2, pay[1] + px(10)), words["pay"],
           font=f_input, fill=PAPER)
    boxes["pay"] = pay

    return Page(image=img, boxes=boxes, has_zip=with_zip, toggle_checked=toggle_checked)
