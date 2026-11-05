"""The daily recap Reel: the day's top stories as a calm, readable vertical video.

Made from a fixed template in the Just News colours (the logo's beige, near-black, a red accent),
so every recap looks the same and only the day's data changes:
  - cover (1 s, shown at once with no animation, so anyone seeing the Reel knows the day): the
    logo, "TODAY'S TOP NEWS", the date and the number of stories; it is also the Reel's cover image
  - one card per story (4-6 s: until all its parts are in, plus 1.5 s still): small logo and
    "1 / 5" at the top, the category, the headline, a
    modest photo card with a slow zoom when there is a licensed photo, then the key facts one by
    one, and the source
  - closing card (5 s): the logo and "Follow @handle"
Each card fades into the next card's empty background, then its parts rise in one by one. Pillow draws at 1080x1920;
the ffmpeg bundled with `imageio-ffmpeg` encodes at 720x1280 with bounded memory and a silent
track (Instagram's API can't add music from its library). No models are involved.
"""

from __future__ import annotations

import difflib
import json
import math
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw

from .carousel import _cover_crop, _lines
from .graphics import Fonts
from .models import title_tokens
from .regions import detect_region

W, H, FPS = 1080, 1920, 30
OUT_W, OUT_H = 720, 1280  # encoded size: a standard Reel size that fits the server's memory (2 GB, no swap)
LEFT, RIGHT = 84, 960     # Instagram's buttons cover the right edge; its caption covers the bottom
INTRO, OUTRO, FADE = 1.0, 5.0, 0.5  # seconds
STORY_MIN, STORY_MAX, HOLD = 4.0, 6.0, 1.5  # a story card: everything in, then HOLD seconds still
FACT_GAP = 0.9            # seconds between one fact and the next
PART_FADE = 0.6           # seconds for one part (headline, photo, a fact) to fade in
RISE = 36                 # pixels a part rises while it fades in
COVER_AT_MS = 500         # the Reel's cover: the cover card (complete from the first frame)
# Hashtags (Instagram allows 5): two broad ones that bring in people browsing news, then the day's
# strongest topic tags.
REACH_TAGS = ["#IndiaNews", "#DailyNews"]
TAG_LIMIT = 5
# Order and selection by attention: what readers care about and what the day's news is talking
# about, leaning Indian. Interest is Jev's audience-interest score from evaluation; heat is how many
# of the day's discovered headlines share the story's topic (e.g. dozens on US election day).
REGION_BONUS = {"India": 6, "South India": 3, "International": 0}
HEAT_MAX = 25  # points for the day's hottest topic; others get a log-scaled share
SOUTH_INDIA_MIN = 1  # at least this many South Indian stories, when the day had them

BG = (231, 207, 138)      # the logo's background
INK = (24, 22, 18)
MUTED = (88, 74, 42)
ACCENT = (172, 33, 33)
LOGO = Path(__file__).parent / "assets" / "logo.jpg"
LOGO_ART = (150, 280, 750, 615)  # the icon and wordmark inside the 900 px logo, for small headers


def day_bounds(day: str, tz: str) -> tuple[str, str]:
    """UTC start and end of a local calendar day ("2026-11-04")."""
    start = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(tz))
    to_utc = lambda d: d.astimezone(timezone.utc).isoformat(timespec="seconds")  # noqa: E731
    return to_utc(start), to_utc(start + timedelta(days=1))


def region_of(r) -> str:
    return r["region"] or detect_region(r["title"] or r["headline"], "")


def _stems(text: str) -> set[str]:
    return {w[:5] for w in title_tokens(text) if len(w) >= 3}


def related_count(title: str, titles: list[str]) -> int:
    """How many of the day's headlines share at least two word stems with this one."""
    mine = _stems(title)
    return sum(1 for t in titles if t != title and len(mine & _stems(t)) >= 2)


def heat_points(related: int, hottest: int) -> float:
    """Relative to the day's hottest topic, so a busy news day doesn't give every story full marks."""
    return HEAT_MAX * math.log1p(related) / math.log1p(hottest) if hottest else 0.0


def priority(r, heat: dict | None = None) -> float:
    """Attention: 40% audience interest + 30% importance + today's topic heat + the region bonus."""
    heat = heat or {}
    overall = r["overall"] or 0
    interest = (r["interest"] if "interest" in r.keys() and r["interest"] is not None else overall) or 0
    related = heat.get(r["article_id"] if "article_id" in r.keys() else None, 0)
    return (0.4 * interest + 0.3 * overall + heat_points(related, max(heat.values(), default=0))
            + REGION_BONUS.get(region_of(r), 0))


def pick(rows: list, limit: int, heat: dict | None = None) -> list:
    """The day's top `limit` stories by attention, in that order, so the hottest story leads and the
    order changes with the day's news. At least SOUTH_INDIA_MIN South Indian stories are kept when
    the day had them (in place of the lowest-ranked others)."""
    key = lambda r: (-priority(r, heat), r["posted_at"], r["posted_seq"])  # noqa: E731
    ranked = sorted(rows, key=key)
    chosen = ranked[:limit]
    south_left = [r for r in ranked[limit:] if region_of(r) == "South India"]
    while sum(region_of(r) == "South India" for r in chosen) < SOUTH_INDIA_MIN and south_left and chosen:
        drop = next((r for r in reversed(chosen) if region_of(r) != "South India"), None)
        if drop is None:
            break
        chosen.remove(drop)
        chosen.append(south_left.pop(0))
    return sorted(chosen, key=key)


def caption(rows: list, date_label: str, tags: list[str], today: bool = True) -> str:
    sources = list(dict.fromkeys(r["source_name"] for r in rows))
    credits = list(dict.fromkeys(r["image_credit"] for r in rows if r["photo_path"] and r["image_credit"]))
    opening = f"Today's top {len(rows)} stories ({date_label}):" if today \
        else f"The top {len(rows)} stories of {date_label}:"
    parts = [opening,
             "\n".join(f"{i}. {r['headline']}" for i, r in enumerate(rows, 1)), " ".join(tags),
             "Sources: " + ", ".join(sources)]
    if credits:
        parts.append("Photos: " + "; ".join(credits))  # CC BY/BY-SA require attribution
    return "\n\n".join(p for p in parts if p)[:2200]


def facts(r, limit: int = 3) -> list[str]:
    """The story's key facts as short points: its Instagram text (or caption), sentence by sentence,
    without the question to readers or "according to <source>" (the source is shown below)."""
    text = (r["ig_caption"] if "ig_caption" in r.keys() and r["ig_caption"] else r["caption"]) or ""
    source = re.escape(r["source_name"] or "")
    out = []
    for s in (s.strip() for s in re.split(r"(?<=[.!])\s+|\n+", text)):
        if not s or s.endswith("?") or _same(s, r["headline"] or ""):
            continue  # a question to readers, or the headline said again
        s = re.sub(rf",?\s*according to {source}\s*\.?$", ".", s, flags=re.I)
        s = re.sub(rf"^According to {source},\s*", "", s, flags=re.I)
        s = s[:1].upper() + s[1:]
        if len(s) > 12 and s not in out:
            out.append(s if len(s) <= 170 else s[:169].rsplit(" ", 1)[0] + "…")
    return out[:limit]


def _same(a: str, b: str) -> bool:
    words = lambda s: re.findall(r"[a-z0-9]+", s.lower())  # noqa: E731
    return difflib.SequenceMatcher(None, words(a), words(b)).ratio() > 0.6


def _ease(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)  # smoothstep: starts and ends softly


class Card:
    """One card of the video: a static base, plus parts that fade and rise in at their own times."""

    def __init__(self, seconds: float, base: Image.Image):
        self.seconds, self.base = seconds, base
        self.parts: list[tuple[float, object, tuple[int, int]]] = []  # (start, sprite or f(t) -> sprite, xy)

    def add(self, start: float, sprite, xy: tuple[int, int]) -> None:
        self.parts.append((start, sprite, xy))

    def frame(self, t: float) -> Image.Image:
        img = self.base.copy()
        for start, sprite, (x, y) in self.parts:
            a = _ease((t - start) / PART_FADE)
            if a <= 0:
                continue
            sp = sprite(t) if callable(sprite) else sprite
            if a < 1:
                sp = sp.copy()
                sp.putalpha(sp.getchannel("A").point(lambda v, a=a: int(v * a)))
            img.paste(sp, (x, y + int((1 - a) * RISE)), sp)
        return img


_PROBE = ImageDraw.Draw(Image.new("RGB", (1, 1)))


def _text(lines: list[str], font, fill, line_h: int, width: int = RIGHT - LEFT) -> Image.Image:
    sprite = Image.new("RGBA", (width, max(1, len(lines)) * line_h + 20), (0, 0, 0, 0))
    d = ImageDraw.Draw(sprite)
    for i, line in enumerate(lines):
        d.text((0, i * line_h), line, font=font, fill=fill)
    return sprite


def _centred(text: str, font, fill) -> tuple[Image.Image, int]:
    """A one-line sprite and the x that centres it."""
    w = int(_PROBE.textlength(text, font=font)) + 10
    return _text([text], font, fill, font.size + 20, w), (W - w) // 2


def _pill(text: str, font, fill, ink) -> Image.Image:
    w, h = int(_PROBE.textlength(text, font=font)) + 52, font.size + 34
    sprite = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(sprite)
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=h // 2, fill=fill)
    d.text((26, 14), text, font=font, fill=ink)
    return sprite


def _rounded(img: Image.Image, radius: int = 36) -> Image.Image:
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, img.width - 1, img.height - 1], radius=radius, fill=255)
    out = img.convert("RGBA")
    out.putalpha(mask)
    return out


def _logo(size: int, art_only: bool = False) -> Image.Image:
    """The logo's black artwork on a transparent background (its beige would show as a square)."""
    img = Image.open(LOGO).convert("RGB")
    if art_only:  # just the icon and wordmark, sized by height, for the small header
        img = img.crop(LOGO_ART)
        img = img.resize((int(img.width * size / img.height), size), Image.LANCZOS)
    else:
        img = img.resize((size, size), Image.LANCZOS)
    alpha = img.convert("L").point(lambda v: max(0, min(255, (170 - v) * 3)))  # dark ink -> opaque
    art = Image.new("RGBA", img.size, INK + (255,))
    art.putalpha(alpha)
    return art


def cover(count: int, date_label: str, weekday: str, fonts: Fonts, today: bool = True) -> Card:
    """The fixed cover: the same layout every day, only the date and the count change. It is drawn
    complete from the first frame (no animation), so the date is the first thing anyone sees."""
    base = Image.new("RGB", (W, H), BG)
    logo = _logo(640)
    base.paste(logo, ((W - 640) // 2, 230), logo)
    pill = _pill("TODAY'S TOP NEWS" if today else "THE DAY'S TOP NEWS", fonts.get(46, bold=True), INK, BG)
    base.paste(pill, ((W - pill.width) // 2, 930), pill)
    for text, font, fill, y in [(weekday, fonts.get(52), MUTED, 1080),
                                (date_label, fonts.get(96, bold=True), INK, 1150),
                                (f"{count} stories, explained simply", fonts.get(44), MUTED, 1300)]:
        if text:
            sprite, x = _centred(text, font, fill)
            base.paste(sprite, (x, y), sprite)
    return Card(INTRO, base)


def story(r, n: int, total: int, fonts: Fonts) -> Card:
    base = Image.new("RGB", (W, H), BG)
    logo = _logo(78, art_only=True)
    base.paste(logo, (LEFT, 120), logo)
    d = ImageDraw.Draw(base)
    counter, cf = f"{n} / {total}", fonts.get(40, bold=True)
    d.text((RIGHT - d.textlength(counter, font=cf), 138), counter, font=cf, fill=INK)
    d.line([(LEFT, 238), (RIGHT, 238)], fill=MUTED, width=2)
    card = Card(STORY_MAX, base)
    photo_path = r["photo_path"] if r["photo_path"] and Path(r["photo_path"]).exists() else None
    hf, ff = fonts.get(80, bold=True), fonts.get(50)
    head = _lines(_PROBE, r["headline"], hf, RIGHT - LEFT, 4)
    points = [_lines(_PROBE, f, ff, RIGHT - LEFT - 50, 4) for f in facts(r, 2 if photo_path else 3)]
    pw, ph = RIGHT - LEFT, 470
    # The whole block (category, headline, photo, facts) is centred between the header and the source.
    height = 100 + len(head) * 96 + 50 + (ph + 70 if photo_path else 0)
    while points and height + sum(len(p) * 68 + 44 for p in points) > 1300:
        points.pop()
    height += sum(len(p) * 68 + 44 for p in points)
    y = 270 + max(0, (1300 - height) // 2)
    card.add(0.0, _pill(r["category"].upper(), fonts.get(34, bold=True), ACCENT, (255, 255, 255)), (LEFT, y))
    y += 100
    card.add(0.15, _text(head, hf, INK, 96), (LEFT, y))
    y += len(head) * 96 + 50
    if photo_path:
        big = _cover_crop(Image.open(photo_path).convert("RGB"), int(pw * 1.08), int(ph * 1.08))

        def photo(t: float) -> Image.Image:  # a slow zoom out over the card
            z = 1.08 - 0.08 * min(1.0, t / STORY_MAX)
            w, h = int(pw * z), int(ph * z)
            x0, y0 = (big.width - w) // 2, (big.height - h) // 2
            return _rounded(big.crop((x0, y0, x0 + w, y0 + h)).resize((pw, ph), Image.BILINEAR))

        card.add(0.6, photo, (LEFT, y))
        if r["image_credit"]:
            card.add(0.6, _text([r["image_credit"]], fonts.get(24), MUTED, 30), (LEFT, y + ph + 10))
        y += ph + 70
    start = 1.2 if photo_path else 0.8
    for lines in points:
        point = Image.new("RGBA", (RIGHT - LEFT, len(lines) * 68 + 20), (0, 0, 0, 0))
        ImageDraw.Draw(point).ellipse([0, 24, 20, 44], fill=ACCENT)
        text = _text(lines, ff, INK, 68, RIGHT - LEFT - 50)
        point.paste(text, (50, 0), text)
        card.add(start, point, (LEFT, y))
        y += len(lines) * 68 + 44
        start += FACT_GAP
    card.add(start, _text([f"Source: {r['source_name']}"], fonts.get(36), MUTED, 44), (LEFT, 1600))
    # As long as the card needs: the fade in, every part in, then a still moment to read.
    card.seconds = min(STORY_MAX, max(STORY_MIN, FADE + start + PART_FADE + HOLD))
    return card


def outro(handle: str, fonts: Fonts, today: bool = True) -> Card:
    card = Card(OUTRO, Image.new("RGB", (W, H), BG))
    card.add(0.0, _logo(520), ((W - 520) // 2, 300))
    for text, font, fill, y, start in [
            ("That's today's news." if today else "That's the day's news.", fonts.get(72, bold=True), INK, 900, 0.6),
            (f"Follow @{handle}" if handle else "Follow us", fonts.get(60, bold=True), ACCENT, 1030, 1.1),
            ("for clear, simple news every day", fonts.get(44), MUTED, 1120, 1.4)]:
        sprite, x = _centred(text, font, fill)
        card.add(start, sprite, (x, y))
    return card


def render(rows: list, date_label: str, handle: str, brand: str, fonts: Fonts, out: Path,
           today: bool = True, weekday: str = "") -> Image.Image:
    """Write the video to `out`; returns the cover image (the cover card, fully drawn)."""
    cards = [cover(len(rows), date_label, weekday, fonts, today)]
    cards += [story(r, n, len(rows), fonts) for n, r in enumerate(rows, 1)]
    cards.append(outro(handle, fonts, today))
    starts = [sum(c.seconds for c in cards[:i]) for i in range(len(cards))]
    total = sum(c.seconds for c in cards)
    out.parent.mkdir(parents=True, exist_ok=True)
    ff = subprocess.Popen(encoder_command(out, total), stdin=subprocess.PIPE)
    try:
        for f in range(int(total * FPS)):
            t = f / FPS
            i = max(k for k, s in enumerate(starts) if t >= s)
            local = t - starts[i]
            if i > 0 and local < FADE:  # the previous card fades into this card's empty background...
                img = Image.blend(cards[i - 1].frame(t - starts[i - 1]), cards[i].base, _ease(local / FADE))
            else:  # ...and only then do this card's parts rise in, so the two never overlap
                img = cards[i].frame(local - (FADE if i > 0 else 0))
            ff.stdin.write(img.resize((OUT_W, OUT_H), Image.BILINEAR).tobytes())
    finally:
        ff.stdin.close()
        if ff.wait() != 0:
            raise RuntimeError("ffmpeg could not encode the recap video")
    return cards[0].frame(cards[0].seconds)


def encoder_command(out: Path, seconds: float) -> list[str]:
    """ffmpeg with its memory bounded: at defaults it took ~1.5 GB for a 40-second 1080p video and the
    server killed the whole service; this takes ~100-260 MB. One thread, a short lookahead, a small
    input queue, and a silent track of a fixed length (no -shortest, which buffers)."""
    import imageio_ffmpeg

    return [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{OUT_W}x{OUT_H}", "-r", str(FPS), "-thread_queue_size", "2",
            "-i", "-", "-f", "lavfi", "-t", f"{seconds:.2f}", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-filter_threads", "1", "-c:v", "libx264", "-preset", "veryfast", "-threads", "1",
            "-x264-params", "rc-lookahead=5:sync-lookahead=0:ref=2", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-movflags", "+faststart", str(out)]


def hashtags(rows: list) -> list[list[str]]:
    return [json.loads(r["hashtags"]) for r in rows]


def reel_tags(topic_tags: list[str]) -> list[str]:
    """The broad discovery tags first, then real topic tags (already ranked; generic filler such as
    #WorldNews from older posts is skipped), at most TAG_LIMIT."""
    from .agents.content import GENERIC_TAGS

    seen = {t.lower() for t in REACH_TAGS}
    topics = [t for t in topic_tags if t.lower() not in seen and t.lstrip("#").lower() not in GENERIC_TAGS
              and t.lstrip("#").lower() not in {"business", "businessnews", "technews", "politics"}]
    return (REACH_TAGS + topics)[:TAG_LIMIT]
