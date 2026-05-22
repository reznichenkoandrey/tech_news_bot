"""
Header banner generator for the daily digest Telegram message.

Renders a 1280x640 PNG with a vertical gradient background, the date in large
Inter Display Bold, a subtitle line, and a pill-shaped badge with the news
count. Fonts are bundled under assets/fonts so output is identical on macOS
and Ubuntu CI.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
FONT_BOLD = REPO_ROOT / "assets" / "fonts" / "Inter-Bold.ttf"
FONT_REGULAR = REPO_ROOT / "assets" / "fonts" / "Inter-Regular.ttf"

KYIV_TZ = ZoneInfo("Europe/Kyiv")

WIDTH = 1280
HEIGHT = 640

# Gradient stops (top → bottom). Deep indigo into electric violet.
GRADIENT_TOP = (15, 10, 38)
GRADIENT_BOTTOM = (45, 27, 105)

# Accent ribbon under the date.
ACCENT_COLOR = (167, 139, 250)  # violet-400

# Pill badge.
BADGE_BG = (255, 255, 255, 28)  # white at ~11% over the gradient
BADGE_DOT = (167, 139, 250)
TEXT_PRIMARY = (255, 255, 255)
TEXT_SECONDARY = (203, 213, 225)  # slate-300

UK_MONTHS = {
    1: "січня", 2: "лютого", 3: "березня", 4: "квітня",
    5: "травня", 6: "червня", 7: "липня", 8: "серпня",
    9: "вересня", 10: "жовтня", 11: "листопада", 12: "грудня",
}


def _render_gradient(width: int, height: int) -> Image.Image:
    """Vertical two-stop linear gradient as the base layer."""
    base = Image.new("RGB", (width, height), GRADIENT_TOP)
    draw = ImageDraw.Draw(base)
    for y in range(height):
        t = y / (height - 1)
        r = round(GRADIENT_TOP[0] + (GRADIENT_BOTTOM[0] - GRADIENT_TOP[0]) * t)
        g = round(GRADIENT_TOP[1] + (GRADIENT_BOTTOM[1] - GRADIENT_TOP[1]) * t)
        b = round(GRADIENT_TOP[2] + (GRADIENT_BOTTOM[2] - GRADIENT_TOP[2]) * t)
        draw.line([(0, y), (width, y)], fill=(r, g, b))
    return base


def _format_date_uk(now: datetime) -> str:
    return f"{now.day} {UK_MONTHS[now.month]} {now.year}"


def _load_font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size=size)


def _draw_pill(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    *,
    pad_x: int = 28,
    pad_y: int = 14,
    dot: bool = True,
) -> tuple[int, int]:
    """Draw a rounded-rectangle pill at xy (top-left). Returns its (w, h)."""
    x, y = xy
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    # Account for descender offset so vertical centering looks right.
    ascent, _ = font.getmetrics()
    dot_w = 18 if dot else 0
    dot_gap = 14 if dot else 0
    pill_w = pad_x * 2 + dot_w + dot_gap + text_w
    pill_h = pad_y * 2 + ascent
    draw.rounded_rectangle(
        [x, y, x + pill_w, y + pill_h],
        radius=pill_h // 2,
        fill=BADGE_BG,
    )
    cursor = x + pad_x
    if dot:
        dot_r = dot_w // 2
        cy = y + pill_h // 2
        draw.ellipse(
            [cursor, cy - dot_r, cursor + dot_w, cy + dot_r],
            fill=BADGE_DOT,
        )
        cursor += dot_w + dot_gap
    # textbbox returns offset from origin — subtract bbox[1] to align top.
    draw.text((cursor, y + pad_y - bbox[1]), text, font=font, fill=TEXT_PRIMARY)
    return pill_w, pill_h


def render_digest_banner(
    output_path: Path | str,
    *,
    count: int,
    profile_name: str = "AI/Tech дайджест",
    now: datetime | None = None,
) -> Path:
    """
    Render the digest header banner to output_path (PNG). Returns the path.

    Raises OSError if a font file is missing — caller decides to fall back.
    """
    if now is None:
        now = datetime.now(KYIV_TZ)

    if not FONT_BOLD.exists() or not FONT_REGULAR.exists():
        raise OSError(f"Banner fonts missing under {FONT_BOLD.parent}")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    img = _render_gradient(WIDTH, HEIGHT).convert("RGBA")
    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    font_subtitle = _load_font(FONT_REGULAR, 38)
    font_date = _load_font(FONT_BOLD, 132)
    font_badge = _load_font(FONT_REGULAR, 30)

    margin_x = 96
    cursor_y = 130

    # Eyebrow / subtitle line.
    draw.text(
        (margin_x, cursor_y),
        profile_name.upper(),
        font=font_subtitle,
        fill=TEXT_SECONDARY,
    )
    cursor_y += 64

    # Big date.
    date_text = _format_date_uk(now)
    draw.text((margin_x, cursor_y), date_text, font=font_date, fill=TEXT_PRIMARY)
    bbox = draw.textbbox((margin_x, cursor_y), date_text, font=font_date)
    cursor_y = bbox[3] + 24

    # Accent ribbon.
    draw.rounded_rectangle(
        [margin_x, cursor_y, margin_x + 96, cursor_y + 8],
        radius=4,
        fill=ACCENT_COLOR,
    )
    cursor_y += 48

    # Pill badge with count.
    plural = "новин" if count == 0 or 5 <= count % 100 <= 20 or count % 10 == 0 or count % 10 >= 5 else (
        "новина" if count % 10 == 1 else "новини"
    )
    _draw_pill(
        draw,
        (margin_x, cursor_y),
        f"{count} {plural}  ·  оновлено о {now.strftime('%H:%M')}",
        font_badge,
    )

    final = Image.alpha_composite(img, overlay).convert("RGB")
    final.save(output_path, format="PNG", optimize=True)
    logger.info("Banner rendered: %s (%dx%d)", output_path, WIDTH, HEIGHT)
    return output_path


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO_ROOT / "data" / "banner_preview.png"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 15
    name = sys.argv[3] if len(sys.argv) > 3 else "AI/Tech дайджест"
    render_digest_banner(out, count=n, profile_name=name)
    print(f"wrote {out}")
