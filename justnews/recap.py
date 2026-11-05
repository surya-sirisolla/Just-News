"""The daily recap Reel: the stories posted today, as a short vertical video (1080x1920, 30 fps).

An intro card, about 3.5 seconds per story (its licensed photo with a slow zoom, or its short text on
its category colour; headline, one fact, source), and a closing "Follow" card. Slides push in from
the right; story-style bars at the top show progress. Pillow draws the frames and the ffmpeg build
bundled with `imageio-ffmpeg` encodes them at 720x1280 with bounded memory, with a silent audio track (Instagram's API can't add
music from its library). No models are involved.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw

from .carousel import DARK, GREY, RED, WHITE, _cover_crop, _darken_bottom, _lines, _pill
from .graphics import CATEGORY_COLORS, DEFAULT_COLOR, Fonts

W, H, FPS = 1080, 1920, 30
OUT_W, OUT_H = 720, 1280  # encoded size: a standard Reel size that fits the server's memory (2 GB, no swap)
LEFT, RIGHT = 72, 960            # Instagram's buttons cover the right edge
INTRO, STORY, OUTRO, PUSH = 2.5, 3.5, 3.0, 0.3  # seconds
TOP_H = 900                      # photo / colour band height


def day_bounds(day: str, tz: str) -> tuple[str, str]:
    """UTC start and end of a local calendar day ("2026-11-04")."""
    start = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(tz))
    to_utc = lambda d: d.astimezone(timezone.utc).isoformat(timespec="seconds")  # noqa: E731
    return to_utc(start), to_utc(start + timedelta(days=1))


def pick(rows: list, limit: int) -> list:
    """The best `limit` stories, shown in the order they were posted."""
    best = sorted(rows, key=lambda r: -(r["overall"] or 0))[:limit]
    return sorted(best, key=lambda r: (r["posted_at"], r["posted_seq"]))


def caption(rows: list, date_label: str, tags: list[str], today: bool = True) -> str:
    sources = list(dict.fromkeys(r["source_name"] for r in rows))
    credits = list(dict.fromkeys(r["image_credit"] for r in rows if r["photo_path"] and r["image_credit"]))
    opening = f"Today's news in {len(rows)} stories ({date_label}):" if today \
        else f"The news of {date_label} in {len(rows)} stories:"
    parts = [opening,
             "\n".join(f"{i}. {r['headline']}" for i, r in enumerate(rows, 1)), " ".join(tags),
             "Sources: " + ", ".join(sources)]
    if credits:
        parts.append("Photos: " + "; ".join(credits))  # CC BY/BY-SA require attribution
    return "\n\n".join(p for p in parts if p)[:2200]


def _ease(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


class Segment:
    """One card of the video: a background (maybe zooming) and a text layer that fades in."""

    def __init__(self, seconds: float, background, overlay: Image.Image):
        self.seconds, self.background, self.overlay = seconds, background, overlay

    def frame(self, t: float) -> Image.Image:
        img = self.background(t / self.seconds).copy()
        a = _ease(t / 0.5)
        layer = self.overlay
        if a < 1:
            layer = layer.copy()
            layer.putalpha(layer.getchannel("A").point(lambda v: int(v * a)))
        img.paste(layer, (0, int((1 - a) * 30)), layer)
        return img


def _text_layer() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    return layer, ImageDraw.Draw(layer)


def _static(img: Image.Image):
    return lambda p: img


def intro(count: int, date_label: str, brand: str, fonts: Fonts, today: bool = True) -> Segment:
    bg = Image.new("RGB", (W, H), DARK)
    layer, d = _text_layer()
    d.text((LEFT, 300), brand.upper(), font=fonts.get(48, bold=True), fill=WHITE)
    bf = fonts.get(60, bold=True)
    label = "TODAY'S NEWS" if today else "THE DAY'S NEWS"
    d.rectangle([0, 700, LEFT + d.textlength(label, font=bf) + 110, 810], fill=RED)
    d.ellipse([LEFT, 737, LEFT + 34, 771], fill=WHITE)
    d.text((LEFT + 58, 722), label, font=bf, fill=WHITE)
    d.text((LEFT, 880), date_label, font=fonts.get(110, bold=True), fill=WHITE)
    d.text((LEFT, 1030), f"{count} stories in {round(INTRO + count * STORY + OUTRO)} seconds",
           font=fonts.get(52), fill=GREY)
    return Segment(INTRO, _static(bg), layer)


def story(r, n: int, total: int, fonts: Fonts) -> Segment:
    color = CATEGORY_COLORS.get(r["category"], DEFAULT_COLOR)
    base = Image.new("RGB", (W, H), DARK)
    photo = Image.open(r["photo_path"]).convert("RGB") if r["photo_path"] and Path(r["photo_path"]).exists() else None
    if photo is not None:
        big = _cover_crop(photo, int(W * 1.12), int(TOP_H * 1.12))

        def background(p: float) -> Image.Image:  # slow zoom out of the photo
            z = 1.12 - 0.12 * p
            w, h = int(W * z), int(TOP_H * z)
            x, y = (big.width - w) // 2, (big.height - h) // 2
            img = base.copy()
            img.paste(_darken_bottom(big.crop((x, y, x + w, y + h)).resize((W, TOP_H)), 650, TOP_H, 210), (0, 0))
            return img
    else:
        band = Image.new("RGB", (W, TOP_H), color)
        bd = ImageDraw.Draw(band)
        f = fonts.get(104, bold=True)
        text = _lines(bd, r["image_text"], f, RIGHT - LEFT, 3)
        y0 = (TOP_H - len(text) * 118) // 2 + 60
        for i, line in enumerate(text):
            bd.text((LEFT, y0 + i * 118), line, font=f, fill=WHITE)
        base.paste(band, (0, 0))
        background = _static(base)
    layer, d = _text_layer()
    _pill(d, LEFT, 150, r["category"].upper(), color if photo is not None else DARK, fonts.get(36, bold=True))
    cf = fonts.get(36, bold=True)
    d.text((RIGHT - d.textlength(f"{n}/{total}", font=cf), 160), f"{n}/{total}", font=cf, fill=WHITE)
    if photo is not None and r["image_credit"]:
        d.text((LEFT, TOP_H - 24), r["image_credit"], font=fonts.get(24), fill=GREY, anchor="ls")
    hf = fonts.get(70, bold=True)
    head = _lines(d, r["headline"], hf, RIGHT - LEFT, 4)
    y = TOP_H + 60
    for i, line in enumerate(head):
        d.text((LEFT, y + i * 84), line, font=hf, fill=WHITE)
    y += len(head) * 84 + 30
    d.rectangle([LEFT, y, LEFT + 120, y + 8], fill=color)
    fact = (r["caption"] or "").split(". ")[0].rstrip(".") + "."
    bf = fonts.get(44)
    for i, line in enumerate(_lines(d, fact, bf, RIGHT - LEFT, 3)):
        d.text((LEFT, y + 40 + i * 58), line, font=bf, fill=GREY)
    d.text((LEFT, 1580), f"Source: {r['source_name']}", font=fonts.get(36), fill=GREY)
    return Segment(STORY, background, layer)


def outro(handle: str, brand: str, fonts: Fonts, today: bool = True) -> Segment:
    bg = Image.new("RGB", (W, H), DARK)
    layer, d = _text_layer()
    d.text((LEFT, 700), "That's today's news." if today else "That's the day's news.", font=fonts.get(80, bold=True),
           fill=WHITE)
    d.rectangle([LEFT, 840, LEFT + 120, 848], fill=RED)
    d.text((LEFT, 900), f"Follow @{handle}" if handle else "Follow us", font=fonts.get(64, bold=True), fill=WHITE)
    d.text((LEFT, 990), "for the news that matters, every day", font=fonts.get(44), fill=GREY)
    d.text((LEFT, 1500), brand.upper(), font=fonts.get(48, bold=True), fill=RED)
    return Segment(OUTRO, _static(bg), layer)


def render(rows: list, date_label: str, handle: str, brand: str, fonts: Fonts, out: Path,
           today: bool = True) -> Image.Image:
    """Write the video to `out`; returns the cover image (the intro card, fully drawn)."""
    segments = [intro(len(rows), date_label, brand, fonts, today)]
    segments += [story(r, n, len(rows), fonts) for n, r in enumerate(rows, 1)]
    segments.append(outro(handle, brand, fonts, today))
    starts = [sum(s.seconds for s in segments[:i]) for i in range(len(segments))]
    total = sum(s.seconds for s in segments)
    out.parent.mkdir(parents=True, exist_ok=True)
    ff = subprocess.Popen(encoder_command(out, total), stdin=subprocess.PIPE)
    try:
        for f in range(int(total * FPS)):
            t = f / FPS
            i = max(k for k, s in enumerate(starts) if t >= s)
            img = segments[i].frame(t - starts[i])
            if i > 0 and t - starts[i] < PUSH:  # the new card pushes the old one out to the left
                a = _ease((t - starts[i]) / PUSH)
                prev = segments[i - 1].frame(t - starts[i - 1])
                canvas = Image.new("RGB", (W, H), DARK)
                canvas.paste(prev, (-int(W * a), 0))
                canvas.paste(img, (W - int(W * a), 0))
                img = canvas
            if 0 < i < len(segments) - 1:  # progress bars for the stories
                d = ImageDraw.Draw(img)
                n, gap = len(segments) - 2, 8
                seg = (W - 80 - gap * (n - 1)) / n
                for k in range(n):
                    x0 = 40 + k * (seg + gap)
                    fill = 1.0 if k < i - 1 else (min(1.0, (t - starts[i]) / STORY) if k == i - 1 else 0.0)
                    d.rectangle([x0, 60, x0 + seg, 66], fill=(90, 94, 104))
                    d.rectangle([x0, 60, x0 + seg * fill, 66], fill=WHITE)
            ff.stdin.write(img.resize((OUT_W, OUT_H), Image.BILINEAR).tobytes())
    finally:
        ff.stdin.close()
        if ff.wait() != 0:
            raise RuntimeError("ffmpeg could not encode the recap video")
    return segments[0].frame(segments[0].seconds)


def encoder_command(out: Path, seconds: float) -> list[str]:
    """ffmpeg with its memory bounded: at defaults it took ~1.5 GB for a 40-second 1080p video and the
    server killed the whole service; this takes ~260 MB. One thread, a short lookahead, a small input
    queue, and a silent track of a fixed length (no -shortest, which buffers)."""
    import imageio_ffmpeg

    return [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{OUT_W}x{OUT_H}", "-r", str(FPS), "-thread_queue_size", "2",
            "-i", "-", "-f", "lavfi", "-t", f"{seconds:.2f}", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-filter_threads", "1", "-c:v", "libx264", "-preset", "veryfast", "-threads", "1",
            "-x264-params", "rc-lookahead=5:sync-lookahead=0:ref=2", "-crf", "21", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-movflags", "+faststart", str(out)]


def hashtags(rows: list) -> list[list[str]]:
    return [json.loads(r["hashtags"]) for r in rows]
