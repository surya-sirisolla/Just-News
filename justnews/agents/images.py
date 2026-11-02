"""Agent 4 - Image Designer.

For each draft post, makes one square image (JUSTNEWS_IMAGE_SIZE, default 1080x1080):

  licensed photo  when the content creator suggested a photo: search Wikimedia Commons, keep only
                  images whose licence allows commercial use and modification (CC0, public domain,
                  CC BY, CC BY-SA by default) with no extra restrictions (personality rights,
                  trademarks), let Jev pick the best fit, crop it, and overlay the image text,
                  source and photo credit.
  text graphic    otherwise, or when no suitable photo is found: a clean white-background graphic
                  with the image text, headline, category, source and brand.

No generative image model is used: Gemini image generation is not on the free tier, and drawing
the text in code guarantees it is spelled exactly right. Images are drafts for review.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode

from PIL import Image, UnidentifiedImageError

from ..config import Settings
from ..db import Database
from ..graphics import Fonts, render_graphic, render_photo
from ..http import FetchError, fetch, make_session
from ..models import title_tokens

log = logging.getLogger("justnews.images")

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
MAX_DOWNLOAD_BYTES = 15 * 1024 * 1024
MIN_PHOTO_SIDE = 800
PHOTO_FIT_THRESHOLD = 0.5   # Jev probability that a photo clearly shows the story's subject
ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp"}
_TAG = re.compile(r"<[^>]+>")


def _meta(info: dict, key: str) -> str:
    return _TAG.sub("", str((info.get("extmetadata", {}).get(key) or {}).get("value", ""))).strip()


def license_allowed(code: str, allowed: list[str]) -> bool:
    """`code` is Commons' machine-readable licence (e.g. "cc-by-sa-4.0", "cc0", "pd").
    NC (non-commercial) and ND (no derivatives) licences are never allowed."""
    code = (code or "").lower()
    if not code or "-nc" in code or "-nd" in code:
        return False
    for a in allowed:
        if a in ("cc0", "pd") and code.startswith(a):
            return True
        if re.fullmatch(rf"{re.escape(a)}(-\d(\.\d)?)?", code):  # "cc-by" matches cc-by-4.0, not cc-by-sa
            return True
    return False


def slugify(text: str, limit: int = 50) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].rstrip("-") or "post"


@dataclass
class ImageResult:
    run_id: str
    photos: int = 0
    graphics: int = 0
    failed: int = 0
    output_dir: str = ""
    usage: list[dict] = field(default_factory=list)

    def stats(self) -> dict:
        return {"photos": self.photos, "graphics": self.graphics, "failed": self.failed,
                "output_dir": self.output_dir, "llm_usage": self.usage}


class ImageDesignerAgent:
    name = "image-designer"

    def __init__(self, settings: Settings, db: Database, chooser=None, session=None):
        self.settings = settings
        self.db = db
        self.fonts = Fonts(settings.font_bold, settings.font_regular)
        self.session = session or make_session()
        contact = settings.contact or "no contact configured"
        # Wikimedia's API policy asks clients to send a descriptive User-Agent with contact details.
        self.session.headers["User-Agent"] = f"JustNews/0.1 (news post drafts; {contact})"
        if chooser is None and settings.typesafe_api_key:
            from ..jev import JevScorer
            chooser = JevScorer(settings.typesafe_api_key, "jev-1.14.0", 0.042)
        self.chooser = chooser

    # --- public entry point -------------------------------------------------
    def run(self, run_id: str) -> ImageResult:
        out_dir = self.settings.output_dir / run_id
        out_dir.mkdir(parents=True, exist_ok=True)
        result = ImageResult(run_id, output_dir=str(out_dir))
        posts = [p for p in self.db.posts_for_run(run_id) if not p["image_path"]]
        log.info("Run %s: designing images for %d posts", run_id, len(posts))
        for i, p in enumerate(posts, 1):
            post = {"headline": p["headline"], "image_text": p["image_text"], "category": p["category"],
                    "source": p["source_name"], "caption": p["caption"]}
            path = out_dir / f"{i:02d}-{slugify(p['headline'])}.png"
            try:
                image = self._design(p, post, path)
            except Exception:  # one bad post or download must not stop the rest
                log.exception("  failed to design an image for %r", p["headline"][:60])
                result.failed += 1
                continue
            self.db.save_image(run_id, p["article_id"], image)
            if image["kind"] == "photo":
                result.photos += 1
            else:
                result.graphics += 1
            log.info("  %-7s %s", image["kind"], path.name)
        if self.chooser and (self.chooser.usage.calls or self.chooser.usage.failed_calls):
            result.usage.append(self.chooser.usage.as_dict())
            self.db.record_llm_usage(run_id, f"{self.name}:{self.chooser.model}", self.chooser.usage.as_dict())
        self.db.save_run_stats(run_id, "images", result.stats())
        log.info("Run %s: %s", run_id, {k: v for k, v in result.stats().items() if k != "llm_usage"})
        return result

    def _design(self, p, post: dict, path: Path) -> dict:
        size = self.settings.image_size
        brand = self.settings.brand_name
        note = "content creator suggested a text graphic"
        if p["visual"] == "photo" and p["image_query"] and self.settings.photo_search:
            found, note = self._find_photo(p["image_query"], post)
            if found:
                photo, meta = found
                render_photo(photo, post, meta["credit"], size, self.fonts, brand).save(path, optimize=True)
                return {"kind": "photo", "path": str(path), "note": note, **meta}
        render_graphic(post, size, self.fonts, brand).save(path, optimize=True)
        return {"kind": "graphic", "path": str(path), "note": note}

    # --- licensed photo search ------------------------------------------------
    def _find_photo(self, query: str, post: dict) -> tuple[tuple[Image.Image, dict] | None, str]:
        try:
            candidates = self._search_commons(query)
        except FetchError as exc:
            return None, f"photo search failed ({exc}); used a text graphic"
        if not candidates:
            return None, f"no suitably licensed photo for '{query}'; used a text graphic"

        key = self._pick(candidates, post, query)
        if key is None:
            return None, f"no licensed photo for '{query}' clearly showed the story's subject; used a text graphic"
        meta = candidates[key]
        try:
            photo = self._download(meta["thumb_url"])
        except (FetchError, UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            return None, f"could not load the chosen photo ({exc}); used a text graphic"
        public = {k: meta[k] for k in ("source_page", "license", "license_url", "creator", "credit")}
        return (photo, public), f"licensed photo from Wikimedia Commons for '{query}'"

    def _search_commons(self, query: str) -> dict[str, dict]:
        params = {
            "action": "query", "format": "json", "generator": "search", "gsrnamespace": 6, "gsrlimit": 15,
            "gsrsearch": f"{query} filetype:bitmap", "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata",
            "iiurlwidth": max(self.settings.image_size) * 3 // 2,
            "iiextmetadatafilter": "LicenseShortName|License|LicenseUrl|Artist|AttributionRequired|"
                                   "Restrictions|NonFree|ObjectName|ImageDescription",
        }
        url = f"{COMMONS_API}?{urlencode(params)}"
        data = fetch(self.session, url, self.settings.request_timeout, self.settings.request_retries).json()
        pages = sorted((data.get("query") or {}).get("pages", {}).values(), key=lambda p: p.get("index", 99))
        out: dict[str, dict] = {}
        for page in pages:
            info = (page.get("imageinfo") or [{}])[0]
            code = _meta(info, "License")
            if (info.get("mime") not in ALLOWED_MIME or min(info.get("width", 0), info.get("height", 0)) < MIN_PHOTO_SIDE
                    or not license_allowed(code, self.settings.image_licenses)
                    or _meta(info, "Restrictions") or _meta(info, "NonFree").lower() == "true"
                    or not info.get("thumburl")):
                continue
            creator = _meta(info, "Artist")[:80] or "Unknown author"
            short = _meta(info, "LicenseShortName") or code
            out[f"c{len(out) + 1}"] = {
                "title": page.get("title", "").removeprefix("File:").rsplit(".", 1)[0],
                "description": (_meta(info, "ImageDescription") or _meta(info, "ObjectName"))[:200],
                "thumb_url": info["thumburl"], "source_page": info.get("descriptionurl"),
                "license": short, "license_url": _meta(info, "LicenseUrl") or None, "creator": creator,
                # "File photo": a licensed library image, not a picture of the reported event.
                "credit": f"File photo: {creator} / Wikimedia Commons, {short}",
            }
            if len(out) == 6:
                break
        return out

    def _pick(self, candidates: dict[str, dict], post: dict, query: str) -> str | None:
        if self.chooser:
            try:
                key, p_fit = self.chooser.choose_photo(
                    {"headline": post["headline"], "image_text": post["image_text"], "photo_subject": query},
                    {k: {"title": c["title"], "description": c["description"] or "(none)"} for k, c in candidates.items()})
                log.info("  Jev photo fit %.2f for %r", p_fit, candidates[key]["title"][:60] if key else None)
                return key if p_fit >= PHOTO_FIT_THRESHOLD else None
            except Exception as exc:  # fall back to keyword matching
                log.warning("  Jev photo choice unavailable (%s); using keyword match", exc)
        wanted = title_tokens(query)
        for k, c in candidates.items():
            if wanted and len(wanted & title_tokens(f"{c['title']} {c['description']}")) >= max(1, len(wanted) - 1):
                return k
        return None

    def _download(self, url: str) -> Image.Image:
        resp = fetch(self.session, url, self.settings.request_timeout, self.settings.request_retries)
        if resp.headers.get("Content-Type", "").split(";")[0] not in ALLOWED_MIME:
            raise FetchError(f"unexpected content type {resp.headers.get('Content-Type')}")
        if len(resp.content) > MAX_DOWNLOAD_BYTES:
            raise FetchError("image too large")
        photo = Image.open(io.BytesIO(resp.content))
        photo.load()
        return photo

