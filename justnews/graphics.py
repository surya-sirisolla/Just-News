"""Pillow rendering for post images: a clean white-background text graphic, or a licensed photo with
a text overlay. Text is drawn by code, so it is always spelled exactly as written."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .models import title_tokens

Image.MAX_IMAGE_PIXELS = 40_000_000  # refuse decompression bombs from downloaded files

CATEGORY_COLORS = {
    "Technology": (37, 99, 235), "AI": (124, 58, 237), "Business": (5, 150, 105),
    "Politics": (220, 38, 38), "World": (217, 119, 6), "India": (190, 18, 60), "South India": (13, 148, 136),
    "Weather": (2, 132, 199), "Environment": (101, 163, 13), "Health": (219, 39, 119), "Science": (79, 70, 229),
    "Sports": (234, 88, 12), "Crime": (71, 85, 105),
}
DEFAULT_COLOR = (17, 24, 39)
INK = (17, 24, 39)
MUTED = (75, 85, 99)

FONT_CANDIDATES = {
    True: ["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf",
           "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
           "C:/Windows/Fonts/arialbd.ttf"],
    False: ["/System/Library/Fonts/Supplemental/Arial.ttf", "/Library/Fonts/Arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf",
            "C:/Windows/Fonts/arial.ttf"],
}


class Fonts:
    def __init__(self, bold_path: str = "", regular_path: str = ""):
        self.paths = {True: self._find(bold_path, True), False: self._find(regular_path, False)}

    @staticmethod
    def _find(override: str, bold: bool) -> str | None:
        for p in ([override] if override else []) + FONT_CANDIDATES[bold]:
            if p and Path(p).exists():
                return p
        return None  # fall back to Pillow's bundled font

    def get(self, size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
        path = self.paths[bold]
        return ImageFont.truetype(path, size) if path else ImageFont.load_default(size)


def drawable(text: str, font) -> str:
    """The rupee sign as "Rs " when the font has no glyph for it (it would print as a box)."""
    if "₹" in text and font.getmask("₹").getbbox() == font.getmask(chr(0xE0001)).getbbox():
        return text.replace("₹", "Rs ").replace("Rs  ", "Rs ")
    return text


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    lines, line = [], ""
    for word in drawable(text, font).split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def _fit(draw, text: str, fonts: Fonts, bold: bool, max_w: int, max_h: int, start: int, minimum: int,
         spacing: float = 1.15):
    """Largest font size at which `text` wraps into the box; returns (font, lines, line_height)."""
    size = start
    while True:
        font = fonts.get(size, bold)
        lines = _wrap(draw, text, font, max_w)
        line_h = int(size * spacing)
        widest = max(draw.textlength(l, font=font) for l in lines)
        if (len(lines) * line_h <= max_h and widest <= max_w) or size <= minimum:
            return font, lines, line_h
        size -= 4


def _draw_lines(draw, lines, font, xy, line_h, fill):
    x, y = xy
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        y += line_h
    return y


def _label(draw, text: str, font, xy, color, on_dark: bool = False) -> int:
    """Rounded category pill; returns its height."""
    x, y = xy
    pad_x, pad_y = 22, 12
    w = draw.textlength(text, font=font)
    h = font.size + 2 * pad_y
    draw.rounded_rectangle((x, y, x + w + 2 * pad_x, y + h), radius=h // 2, fill=color)
    draw.text((x + pad_x, y + pad_y - 2), text, font=font, fill=(255, 255, 255))
    return h


def _subtext(post: dict) -> str:
    """Line under the big text: the headline, or the caption's first sentence when the headline mostly
    repeats the image text."""
    big, head = title_tokens(post["image_text"]), title_tokens(post["headline"])
    if head and len(big & head) / len(head) < 0.6:
        return post["headline"]
    first = post.get("caption", "").split(". ")[0].rstrip(".")
    return (first[:157].rsplit(" ", 1)[0] + "…") if len(first) > 160 else (first + "." if first else "")


def render_graphic(post: dict, size: tuple[int, int], fonts: Fonts, brand: str) -> Image.Image:
    """White background, category pill, large image text, headline, source and brand footer."""
    w, h = size
    accent = CATEGORY_COLORS.get(post["category"], DEFAULT_COLOR)
    img = Image.new("RGB", size, (255, 255, 255))
    draw = ImageDraw.Draw(img)
    m = int(w * 0.08)
    draw.rectangle((0, 0, w, int(h * 0.012)), fill=accent)

    top = m + int(h * 0.01)
    top += _label(draw, post["category"].upper(), fonts.get(int(w * 0.026), True), (m, top), accent) + int(h * 0.05)

    footer_h = int(h * 0.13)
    body_bottom = h - footer_h - int(h * 0.03)
    headline = _subtext(post)
    head_h = int((body_bottom - top) * 0.32) if headline else 0
    font, lines, line_h = _fit(draw, post["image_text"], fonts, True, w - 2 * m, body_bottom - top - head_h,
                               int(w * 0.105), int(w * 0.05), spacing=1.12)
    gap = int(h * 0.065)
    if headline:
        hfont, hlines, hline_h = _fit(draw, headline, fonts, False, w - 2 * m, head_h - gap,
                                      int(w * 0.04), int(w * 0.026), spacing=1.3)
    block = len(lines) * line_h + (gap + len(hlines) * hline_h if headline else 0)
    y = top + max(0, (body_bottom - top - block) * 2 // 5)  # centred, slightly above the middle
    y = _draw_lines(draw, lines, font, (m, y), line_h, INK)
    if headline:
        y += int(h * 0.025)
        draw.rectangle((m, y, m + int(w * 0.12), y + 6), fill=accent)
        _draw_lines(draw, hlines, hfont, (m, y + gap - int(h * 0.025)), hline_h, MUTED)

    fy = h - footer_h
    draw.line((m, fy, w - m, fy), fill=(229, 231, 235), width=3)
    small = fonts.get(int(w * 0.026))
    draw.text((m, fy + int(footer_h * 0.33)), f"Source: {post['source']}", font=small, fill=MUTED)
    bfont = fonts.get(int(w * 0.03), True)
    draw.text((w - m - draw.textlength(brand, font=bfont), fy + int(footer_h * 0.31)), brand, font=bfont, fill=accent)
    return img


def _cover(photo: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Scale and centre-crop to fill `size`."""
    w, h = size
    scale = max(w / photo.width, h / photo.height)
    resized = photo.resize((round(photo.width * scale), round(photo.height * scale)), Image.LANCZOS)
    left, top = (resized.width - w) // 2, (resized.height - h) // 2
    return resized.crop((left, top, left + w, top + h))


def render_photo(photo: Image.Image, post: dict, credit: str, size: tuple[int, int], fonts: Fonts,
                 brand: str) -> Image.Image:
    """Licensed photo with a dark gradient, image text, source, brand and the photo credit."""
    w, h = size
    accent = CATEGORY_COLORS.get(post["category"], DEFAULT_COLOR)
    img = _cover(photo.convert("RGB"), size)

    shade = Image.new("L", (1, h))
    start = int(h * 0.42)
    for y in range(h):
        shade.putpixel((0, y), 0 if y < start else min(225, int(225 * (y - start) / (h - start) * 1.4)))
    img = Image.composite(Image.new("RGB", size, (0, 0, 0)), img, shade.resize(size))
    draw = ImageDraw.Draw(img)
    m = int(w * 0.07)

    _label(draw, post["category"].upper(), fonts.get(int(w * 0.026), True), (m, m), accent)

    credit_font = fonts.get(int(w * 0.019))
    footer_y = h - m - int(w * 0.03)
    text_bottom = footer_y - int(h * 0.05)
    font, lines, line_h = _fit(draw, post["image_text"], fonts, True, w - 2 * m, int(h * 0.3),
                               int(w * 0.085), int(w * 0.045), spacing=1.12)
    y = text_bottom - len(lines) * line_h
    draw.rectangle((m, y - int(h * 0.03), m + int(w * 0.12), y - int(h * 0.03) + 6), fill=accent)
    _draw_lines(draw, lines, font, (m, y), line_h, (255, 255, 255))

    small = fonts.get(int(w * 0.024))
    draw.text((m, footer_y), f"Source: {post['source']}", font=small, fill=(229, 231, 235))
    bfont = fonts.get(int(w * 0.028), True)
    draw.text((w - m - draw.textlength(brand, font=bfont), footer_y - 2), brand, font=bfont, fill=(255, 255, 255))
    credit_text = credit if draw.textlength(credit, font=credit_font) <= w - 2 * m else credit[:90] + "…"
    draw.text((m, h - int(m * 0.55)), credit_text, font=credit_font, fill=(209, 213, 219))
    return img
