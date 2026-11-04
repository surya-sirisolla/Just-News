"""Instagram carousel slides (1080x1350, 4:5): a cover, one slide per story, and a closing slide.

The cover's label is "BREAKING NEWS" only when the lead story is under an hour old ("LATEST" otherwise).
Story slides show the licensed photo (with its credit) or, without one, the story's short image
text on its category colour. Pillow only: no models and no video.
"""

from __future__ import annotations

from PIL import Image, ImageDraw

from .graphics import CATEGORY_COLORS, DEFAULT_COLOR, Fonts, _wrap

SIZE = W, H = 1080, 1350
LEFT, RIGHT = 72, 1008
DARK, WHITE, GREY, RED = (12, 14, 20), (255, 255, 255), (172, 178, 188), (220, 38, 38)
PHOTO_H = 620
MAX_STORIES = 8  # Instagram allows 10 slides: the cover, 8 stories and the closing slide


def _cover_crop(img: Image.Image, w: int, h: int) -> Image.Image:
    s = max(w / img.width, h / img.height)
    im = img.convert("RGB").resize((int(img.width * s) + 1, int(img.height * s) + 1), Image.LANCZOS)
    x, y = (im.width - w) // 2, (im.height - h) // 2
    return im.crop((x, y, x + w, y + h))


def _darken_bottom(img: Image.Image, start: int, end: int, strength: int = 245) -> Image.Image:
    mask = Image.new("L", (1, img.height))
    for y in range(img.height):
        mask.putpixel((0, y), int(strength * min(1.0, max(0.0, (y - start) / (end - start)))))
    return Image.composite(Image.new("RGB", img.size, DARK), img, mask.resize(img.size))


def _lines(d: ImageDraw.ImageDraw, text: str, font, width: int, limit: int) -> list[str]:
    out = _wrap(d, text, font, width)
    if len(out) > limit:
        out = out[:limit]
        out[-1] = out[-1].rstrip(".,;: ") + "…"
    return out


def _pill(d: ImageDraw.ImageDraw, x: int, y: int, text: str, color, font) -> None:
    w = d.textlength(text, font=font)
    d.rounded_rectangle([x, y, x + w + 44, y + 58], radius=29, fill=color)
    d.text((x + 22, y + 10), text, font=font, fill=WHITE)


def _hint(d: ImageDraw.ImageDraw, text: str, y: int, fonts: Fonts) -> None:
    f = fonts.get(34, bold=True)
    d.text((RIGHT - d.textlength(text + "  ›", font=f), y), text + "  ›", font=f, fill=WHITE)


def cover(lead: dict, more: int, label: str, date: str, brand: str, fonts: Fonts) -> Image.Image:
    photo = lead.get("photo")
    if photo:
        img = _darken_bottom(_cover_crop(photo, W, H), 380, 1000)
    else:  # no photo: the lead story's category colour, fading to dark under the headline
        img = _darken_bottom(Image.new("RGB", SIZE, CATEGORY_COLORS.get(lead["category"], DEFAULT_COLOR)), 200, 1100, 235)
    d = ImageDraw.Draw(img)
    d.text((LEFT, 40), brand.upper(), font=fonts.get(40, bold=True), fill=WHITE)
    bf = fonts.get(54, bold=True)
    d.rectangle([0, 96, LEFT + d.textlength(label, font=bf) + 110, 196], fill=RED)
    d.ellipse([LEFT, 130, LEFT + 32, 162], fill=WHITE)
    d.text((LEFT + 56, 114), label, font=bf, fill=WHITE)
    hf = fonts.get(84, bold=True)
    head = _lines(d, lead["headline"], hf, RIGHT - LEFT, 4)
    y = 860 - (len(head) - 3) * 96
    for i, line in enumerate(head):
        d.text((LEFT, y + i * 96), line, font=hf, fill=WHITE)
    y += len(head) * 96 + 24
    meta = f"+ {more} more {'story' if more == 1 else 'stories'}  ·  {date}" if more else date
    d.text((LEFT, y), meta, font=fonts.get(38, bold=True), fill=GREY)
    if more:
        _hint(d, "Swipe for more", y - 4, fonts)
    if photo and lead.get("credit"):
        d.text((LEFT, H - 40), lead["credit"], font=fonts.get(22), fill=GREY, anchor="ls")
    return img


def story(s: dict, n: int, total: int, fonts: Fonts) -> Image.Image:
    color = CATEGORY_COLORS.get(s["category"], DEFAULT_COLOR)
    img = Image.new("RGB", SIZE, DARK)
    photo = s.get("photo")
    if photo:
        img.paste(_darken_bottom(_cover_crop(photo, W, PHOTO_H), 420, PHOTO_H, 200), (0, 0))
    else:  # no licensed photo: the story's short image text on its category colour
        band = Image.new("RGB", (W, PHOTO_H), color)
        bd = ImageDraw.Draw(band)
        f = fonts.get(92, bold=True)
        text = _lines(bd, s["image_text"], f, RIGHT - LEFT, 3)
        y0 = (PHOTO_H - len(text) * 104) // 2 + 20
        for i, line in enumerate(text):
            bd.text((LEFT, y0 + i * 104), line, font=f, fill=WHITE)
        img.paste(band, (0, 0))
    d = ImageDraw.Draw(img)
    _pill(d, LEFT, 52, s["category"].upper(), color if photo else DARK, fonts.get(32, bold=True))
    cf = fonts.get(32, bold=True)
    count = f"{n}/{total}"
    d.text((RIGHT - d.textlength(count, font=cf), 62), count, font=cf, fill=WHITE)
    if photo and s.get("credit"):
        d.text((LEFT, PHOTO_H - 22), s["credit"], font=fonts.get(22), fill=GREY, anchor="ls")
    hf = fonts.get(64, bold=True)
    head = _lines(d, s["headline"], hf, RIGHT - LEFT, 3)
    y = PHOTO_H + 60
    for i, line in enumerate(head):
        d.text((LEFT, y + i * 76), line, font=hf, fill=WHITE)
    y += len(head) * 76 + 30
    d.rectangle([LEFT, y, LEFT + 120, y + 8], fill=color)
    y += 40
    bf = fonts.get(40)
    for i, line in enumerate(_lines(d, s["caption"], bf, RIGHT - LEFT, 4)):
        d.text((LEFT, y + i * 54), line, font=bf, fill=GREY)
    d.text((LEFT, H - 92), f"Source: {s['source']}", font=fonts.get(32), fill=GREY)
    if n < total:
        _hint(d, "Next", H - 92, fonts)
    return img


def closing(total: int, handle: str, brand: str, fonts: Fonts) -> Image.Image:
    img = Image.new("RGB", SIZE, DARK)
    d = ImageDraw.Draw(img)
    d.text((LEFT, 470), "That's the news", font=fonts.get(84, bold=True), fill=WHITE)
    d.text((LEFT, 570), "this hour.", font=fonts.get(84, bold=True), fill=WHITE)
    d.text((LEFT, 720), f"{total} {'story' if total == 1 else 'stories'}, checked against their sources.",
           font=fonts.get(40), fill=GREY)
    d.rectangle([LEFT, 830, LEFT + 120, 838], fill=RED)
    d.text((LEFT, 880), f"Follow @{handle}" if handle else "Follow us", font=fonts.get(56, bold=True), fill=WHITE)
    d.text((LEFT, 950), "for the news that matters, every hour", font=fonts.get(40), fill=GREY)
    d.text((LEFT, H - 92), brand.upper(), font=fonts.get(40, bold=True), fill=RED)
    return img


def render(stories: list[dict], label: str, date: str, handle: str, brand: str, fonts: Fonts) -> list[Image.Image]:
    """Slides for 2-8 stories, lead story first."""
    total = len(stories)
    return ([cover(stories[0], total - 1, label, date, brand, fonts)]
            + [story(s, n, total, fonts) for n, s in enumerate(stories, 1)]
            + [closing(total, handle, brand, fonts)])
