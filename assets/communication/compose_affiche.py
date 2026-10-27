#!/usr/bin/env python3
"""Compose the TAFWITA official launch poster at 1080x1350."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "fond-lancement.jpg"
OUT = ROOT / "TAFWITA-lancement-officiel-1080x1350.png"
OUT_JPG = ROOT / "TAFWITA-lancement-officiel-1080x1350.jpg"

W, H = 1080, 1350
SCALE = 3  # render at 3x then downsample for crisp type
CW, CH = W * SCALE, H * SCALE

FONT_DIR = Path("/usr/share/fonts/truetype/macos")
FONT_REG = str(FONT_DIR / "Inter-Regular.ttf")
FONT_MED = str(FONT_DIR / "Inter-Medium.ttf")
FONT_SEMI = str(FONT_DIR / "Inter-SemiBold.ttf")
FONT_BOLD = str(FONT_DIR / "Inter-Bold.ttf")

# Chart TAFWITA (screenshot) + or premium
WHITE = (247, 252, 250, 255)
MINT = (46, 196, 182, 255)  # WITA — chart TAFWITA
MINT_SOFT = (168, 240, 228, 255)
GOLD = (224, 186, 74, 255)
GOLD_BRIGHT = (240, 206, 110, 255)
GOLD_LINE = (218, 178, 72, 255)
GOLD_DIM = (196, 156, 58, 220)
MUTED = (210, 232, 228, 250)
TAGLINE = (236, 246, 244, 250)
NAVY = (6, 20, 28, 255)


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size * SCALE)


def text_size(draw: ImageDraw.ImageDraw, text: str, fnt: ImageFont.FreeTypeFont) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=fnt)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def draw_centered(
    draw: ImageDraw.ImageDraw,
    text: str,
    y: int,
    fnt: ImageFont.FreeTypeFont,
    fill,
    tracking: int = 0,
) -> int:
    """Draw centered text, optional letter-spacing in 1x pixels. Returns bottom y."""
    if tracking == 0:
        w, h = text_size(draw, text, fnt)
        x = (CW - w) // 2
        draw.text((x, y), text, font=fnt, fill=fill)
        return y + h

    tracking_px = tracking * SCALE
    widths = []
    total = 0
    for i, ch in enumerate(text):
        cw, _ = text_size(draw, ch, fnt)
        widths.append(cw)
        total += cw
        if i < len(text) - 1:
            total += tracking_px
    x = (CW - total) // 2
    max_h = 0
    for i, ch in enumerate(text):
        draw.text((x, y), ch, font=fnt, fill=fill)
        _, ch_h = text_size(draw, ch, fnt)
        max_h = max(max_h, ch_h)
        x += widths[i] + tracking_px
    return y + max_h


def wrap_centered(text: str, fnt: ImageFont.FreeTypeFont, max_width: int, draw: ImageDraw.ImageDraw) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = word if not current else f"{current} {word}"
        w, _ = text_size(draw, trial, fnt)
        if w <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def rounded_rect(draw, xy, radius, fill=None, outline=None, width=1):
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)


def prepare_background() -> Image.Image:
    src = Image.open(SRC).convert("RGB")
    sw, sh = src.size
    target_ratio = W / H
    src_ratio = sw / sh
    if src_ratio > target_ratio:
        new_w = int(sh * target_ratio)
        left = (sw - new_w) // 2
        src = src.crop((left, 0, left + new_w, sh))
    else:
        new_h = int(sw / target_ratio)
        # Keep a little more of the lower ECG and gold frame
        top = int((sh - new_h) * 0.28)
        src = src.crop((0, top, sw, top + new_h))
    src = src.resize((CW, CH), Image.Resampling.LANCZOS)

    # Subtle center veil so type stays readable without flattening the gradient
    veil = Image.new("RGBA", (CW, CH), (0, 0, 0, 0))
    vdraw = ImageDraw.Draw(veil)
    # vertical soft band
    cx, cy = CW // 2, int(CH * 0.42)
    for i in range(18, 0, -1):
        alpha = int(18 * (i / 18))
        rx = int(CW * 0.42 * (i / 18) + CW * 0.12)
        ry = int(CH * 0.38 * (i / 18) + CH * 0.10)
        vdraw.ellipse(
            (cx - rx, cy - ry, cx + rx, cy + ry),
            fill=(4, 22, 28, alpha),
        )
    veil = veil.filter(ImageFilter.GaussianBlur(radius=80 * SCALE / 3))
    base = src.convert("RGBA")
    return Image.alpha_composite(base, veil)


def compose() -> Image.Image:
    canvas = prepare_background()
    overlay = Image.new("RGBA", (CW, CH), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    f_brand = font(FONT_BOLD, 96)
    f_kicker = font(FONT_MED, 17)
    f_date = font(FONT_SEMI, 32)
    f_tag = font(FONT_REG, 23)
    f_badge = font(FONT_MED, 17)
    f_foot = font(FONT_REG, 16)

    y = int(CH * 0.188)
    cx = CW // 2

    # Small gold ornament above the brand
    line_w = 70 * SCALE
    stroke = max(3, int(1.6 * SCALE))
    draw.line(
        (cx - line_w // 2, y, cx - 8 * SCALE, y),
        fill=GOLD_LINE,
        width=stroke,
    )
    draw.line(
        (cx + 8 * SCALE, y, cx + line_w // 2, y),
        fill=GOLD_LINE,
        width=stroke,
    )
    d = 6 * SCALE
    draw.polygon(
        [(cx, y - d), (cx + d, y), (cx, y + d), (cx - d, y)],
        fill=GOLD_BRIGHT,
        outline=GOLD,
    )

    y += 46 * SCALE

    # TAFWITA wordmark — TAF blanc, WITA teal marque, un seul mot
    taf, wita = "TAF", "WITA"
    gap = 0
    w_taf, h_taf = text_size(draw, taf, f_brand)
    w_wita, h_wita = text_size(draw, wita, f_brand)
    total = w_taf + gap + w_wita
    x0 = (CW - total) // 2
    # soft glow behind the wordmark
    glow = Image.new("RGBA", (CW, CH), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    gdraw.text((x0, y), taf, font=f_brand, fill=(255, 255, 255, 70))
    gdraw.text((x0 + w_taf + gap, y), wita, font=f_brand, fill=(46, 196, 182, 80))
    glow = glow.filter(ImageFilter.GaussianBlur(radius=12 * SCALE))
    overlay = Image.alpha_composite(overlay, glow)
    draw = ImageDraw.Draw(overlay)

    draw.text((x0, y), taf, font=f_brand, fill=WHITE)
    draw.text((x0 + w_taf + gap, y), wita, font=f_brand, fill=MINT)
    y += max(h_taf, h_wita) + 30 * SCALE

    # Fine gold rule
    rule = 108 * SCALE
    draw.line(
        (cx - rule // 2, y, cx + rule // 2, y),
        fill=GOLD_LINE,
        width=max(3, int(1.7 * SCALE)),
    )
    y += 38 * SCALE

    y = draw_centered(draw, "LANCEMENT OFFICIEL", y, f_kicker, GOLD_BRIGHT, tracking=8)
    y += 44 * SCALE

    # Date in a thin gold frame
    date = "29 NOVEMBRE 2026"
    dw, dh = text_size(draw, date, f_date)
    pad_x, pad_y = 40 * SCALE, 18 * SCALE
    box_w, box_h = dw + pad_x * 2, dh + pad_y * 2
    bx0 = (CW - box_w) // 2
    by0 = y
    bx1, by1 = bx0 + box_w, by0 + box_h
    radius = 3 * SCALE
    gold_stroke = max(3, int(2.0 * SCALE))
    rounded_rect(
        draw,
        (bx0, by0, bx1, by1),
        radius=radius,
        fill=(6, 32, 36, 120),
        outline=GOLD_BRIGHT,
        width=gold_stroke,
    )
    inset = 5 * SCALE
    rounded_rect(
        draw,
        (bx0 + inset, by0 + inset, bx1 - inset, by1 - inset),
        radius=max(1, radius - 1),
        outline=GOLD_DIM,
        width=max(2, int(0.9 * SCALE)),
    )
    # gold corner ticks
    tick = 12 * SCALE
    tw = max(2, int(1.4 * SCALE))
    corners = [
        (bx0, by0, 1, 1),
        (bx1, by0, -1, 1),
        (bx0, by1, 1, -1),
        (bx1, by1, -1, -1),
    ]
    for x, yy, dx, dy in corners:
        draw.line((x, yy, x + dx * tick, yy), fill=GOLD_BRIGHT, width=tw)
        draw.line((x, yy, x, yy + dy * tick), fill=GOLD_BRIGHT, width=tw)

    tx = bx0 + (box_w - dw) // 2
    ty = by0 + (box_h - dh) // 2 - 2 * SCALE
    draw.text((tx, ty), date, font=f_date, fill=WHITE)
    y = by1 + 58 * SCALE

    tagline = "Une nouvelle expérience digitale conçue pour accompagner les professionnels de santé."
    max_w = int(CW * 0.72)
    lines = wrap_centered(tagline, f_tag, max_w, draw)
    line_gap = 10 * SCALE
    for i, line in enumerate(lines):
        y = draw_centered(draw, line, y, f_tag, TAGLINE)
        y += line_gap
    y += 46 * SCALE

    # Badge — Réservé aux médecins
    badge = "Réservé aux médecins"
    bw, bh = text_size(draw, badge, f_badge)
    bpad_x, bpad_y = 22 * SCALE, 11 * SCALE
    bb_w, bb_h = bw + bpad_x * 2, bh + bpad_y * 2
    bbx0 = (CW - bb_w) // 2
    bby0 = y
    bbx1, bby1 = bbx0 + bb_w, bby0 + bb_h
    rounded_rect(
        draw,
        (bbx0, bby0, bbx1, bby1),
        radius=bb_h // 2,
        fill=(10, 48, 50, 110),
        outline=MINT,
        width=max(2, int(1.2 * SCALE)),
    )
    draw.text(
        (bbx0 + (bb_w - bw) // 2, bby0 + (bb_h - bh) // 2 - 1 * SCALE),
        badge,
        font=f_badge,
        fill=MINT_SOFT,
    )
    y = bby1 + 48 * SCALE

    draw_centered(draw, "Plus d'informations prochainement", y, f_foot, MUTED, tracking=1)

    composed = Image.alpha_composite(canvas, overlay)
    final = composed.resize((W, H), Image.Resampling.LANCZOS).convert("RGB")
    return final


def main() -> None:
    poster = compose()
    poster.save(OUT, "PNG", optimize=True)
    poster.save(OUT_JPG, "JPEG", quality=95, subsampling=0)
    print(f"Wrote {OUT} {poster.size}")
    print(f"Wrote {OUT_JPG} {poster.size}")


if __name__ == "__main__":
    main()
