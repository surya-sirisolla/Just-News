"""Agent 3 - Content Creator.

Turns the evaluator's recommended articles into draft social posts: a short headline, a caption,
hashtags, a source attribution, the text for the image, and a suggestion for the image designer
(licensed photo or text graphic, plus a photo search query).

Gemini (free tier) writes the text. Free-tier quotas are small, so all posts in a run go out in a
single request, requests are spaced out, and when a model's daily quota is used up the next model
in GEMINI_CONTENT_MODELS is tried. Code then owns everything that must be exact: the source
attribution, hashtag format and length limits. When a TypeSafe key is set, Jev checks each
caption against its source and flags unsupported facts or a sensational tone for human review.
Posts are drafts: nothing is published.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field

from ..config import Settings
from ..db import Database
from ..llm import GeminiClient, LLMError

log = logging.getLogger("justnews.content")

HEADLINE_MAX = 80
CAPTION_MAX = 280      # fits X/Twitter; the attribution and hashtags are added separately
IMAGE_TEXT_MAX = 60
HASHTAGS = (3, 5)
REVIEW_THRESHOLD = 0.5

SYSTEM_PROMPT = """You write social media posts for "Just News", a neutral news account that shares
important stories in Technology, AI, Business, Politics and World News.

For each article you get only its headline, summary, publisher and section. Write:
- headline: a short, clear headline, at most 70 characters. No clickbait, no "BREAKING", no emojis.
- caption: 1-2 plain sentences, at most 250 characters, saying what happened and why it matters.
  Use ONLY facts stated in the headline or summary. Do not add numbers, names, quotes, dates or
  causes that are not given. Attribute claims to the publisher ("according to BBC News") when the
  story reports a claim or allegation. When summarizing a claim about a group or a person, keep the
  source's exact qualifiers or quote them; never broaden or soften them. Neutral tone: no opinions,
  no hype, no emojis, no hashtags, no links.
- hashtags: 3-5 relevant topic hashtags in CamelCase without the # sign (e.g.
  "ArtificialIntelligence"). Not the publisher's name. No years unless the source states that year.
- image_text: 2-8 words for a graphic over the image, at most 50 characters, factual, no punctuation
  at the end.
- visual: "photo" when a generic photo of a company, product, place, object or public figure in
  their public role would illustrate the story honestly; "graphic" for deaths, crime, accidents,
  disasters, health tragedies, stories about private individuals, or when no generic photo fits.
- image_query: for "photo", 2-4 words naming what the photo should show (e.g. "supermarket
  freezer", "Microsoft headquarters"); never a person who is a victim or suspect. Empty for "graphic".
"""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "posts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "headline": {"type": "string"},
                    "caption": {"type": "string"},
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                    "image_text": {"type": "string"},
                    "visual": {"type": "string", "enum": ["photo", "graphic"]},
                    "image_query": {"type": "string"},
                },
                "required": ["id", "headline", "caption", "hashtags", "image_text", "visual", "image_query"],
            },
        },
    },
    "required": ["posts"],
}


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:-")
    return cut + "…"


def normalize_hashtags(raw: list[str], source_text: str = "", publisher: str = "") -> list[str]:
    """CamelCase-ish, letters/digits only, deduplicated case-insensitively, at most 5 tags. Drops tags
    with a year the source never mentions (e.g. #Campaign2024 for a 2026 story) and the publisher's name."""
    tags, seen = [], set()
    publisher_key = re.sub(r"[^a-z0-9]", "", publisher.lower())
    for t in raw or []:
        words = re.findall(r"[A-Za-z0-9]+", str(t))
        if not words:
            continue
        if any(y not in source_text for y in re.findall(r"(?:19|20)\d\d", "".join(words))):
            continue
        if publisher_key and publisher_key in "".join(words).lower():
            continue
        tag = "".join(w if w[:1].isupper() or w.isdigit() else w.capitalize() for w in words)[:30]
        if tag.lower() not in seen and not tag.isdigit():
            seen.add(tag.lower())
            tags.append("#" + tag)
    return tags[: HASHTAGS[1]]


def attribution(publisher: str, url: str) -> str:
    return f"Source: {publisher} – {url}"


@dataclass
class ContentResult:
    run_id: str
    created: int = 0
    needs_review: int = 0
    pending: int = 0
    error: str | None = None
    usage: list[dict] = field(default_factory=list)

    def stats(self) -> dict:
        return {"created": self.created, "needs_review": self.needs_review, "pending": self.pending,
                "error": self.error, "llm_usage": self.usage}


class ContentCreatorAgent:
    name = "content-creator"

    def __init__(self, settings: Settings, db: Database, clients: list | None = None, verifier=None):
        self.settings = settings
        self.db = db
        if clients is None:
            # Free-tier models are sometimes overloaded and stop responding: fail over to the next
            # model quickly instead of retrying a hung one.
            clients = [GeminiClient(settings.gemini_api_key, m, 0.0, 0.0,  # free tier: no charge
                                    thinking_level=settings.gemini_content_thinking_level,
                                    min_interval=settings.gemini_min_interval, temperature=0.4,
                                    timeout=45, retries=1)
                       for m in settings.gemini_content_models]
        self.clients = clients
        self.verifier = verifier if verifier is not None else self._default_verifier()

    def _default_verifier(self):
        if not self.settings.typesafe_api_key:
            return False
        from ..jev import JevScorer
        return JevScorer(self.settings.typesafe_api_key, "jev-1.14.0", 0.042)

    # --- public entry point -------------------------------------------------
    def run(self, run_id: str) -> ContentResult:
        result = ContentResult(run_id)
        articles = self.db.recommended_without_post(run_id, self.settings.max_posts_per_run)
        log.info("Run %s: writing posts for %d recommended articles", run_id, len(articles))
        if articles:
            keys = {f"P{i + 1}": a for i, a in enumerate(articles)}
            drafts, model = self._write(keys, result)
            reviews = self._verify(keys, drafts) if drafts else {}
            for k, a in keys.items():
                post = self._finalize(drafts.get(k), a)
                if post is None:
                    result.pending += 1
                    continue
                if k in reviews:
                    post["status"], post["review_note"] = "needs_review", reviews[k]
                    result.needs_review += 1
                self.db.save_post(run_id, a["id"], post, model)
                result.created += 1
        result.usage = [c.usage.as_dict() for c in self.clients if c.usage.calls or c.usage.failed_calls]
        if self.verifier:
            result.usage.append(self.verifier.usage.as_dict())
        for u in result.usage:
            self.db.record_llm_usage(run_id, f"{self.name}:{u['model']}", u)
        self.db.save_run_stats(run_id, "content", result.stats())
        log.info("Run %s: %s", run_id, {k: v for k, v in result.stats().items() if k != "llm_usage"})
        return result

    # --- writing ------------------------------------------------------------
    def _write(self, keys: dict, result: ContentResult) -> tuple[dict[str, dict], str]:
        items = [{"id": k, "section": a["category"], "publisher": a["source_name"], "headline": a["title"],
                  "summary": (a["description"] or "")[:400]} for k, a in keys.items()]
        prompt = "ARTICLES:\n" + json.dumps(items, ensure_ascii=False, indent=0)
        deadline = time.monotonic() + self.settings.gemini_content_time_budget
        for client in self.clients:
            if time.monotonic() > deadline:
                log.warning("  content time budget used up; remaining models skipped")
                break
            try:
                rows = client.generate_json(SYSTEM_PROMPT, prompt, RESPONSE_SCHEMA).get("posts", [])
                log.info("  %s wrote %d posts", client.model, len(rows))
                return {r["id"]: r for r in rows if isinstance(r, dict) and r.get("id") in keys}, client.model
            except LLMError as exc:
                result.error = str(exc)
                # Unavailable model, daily quota used up, timeouts or overload: try the next free model.
                log.warning("  %s failed (%s); trying the next model", client.model, str(exc)[:160])
        log.error("  no Gemini model could write posts; they stay pending for the next run")
        return {}, ""

    def _finalize(self, draft: dict | None, a) -> dict | None:
        """Validate and clean a model draft; attribution and hashtag format are set by code."""
        if not draft:
            return None
        headline = _clip(draft.get("headline", ""), HEADLINE_MAX)
        caption = _clip(draft.get("caption", ""), CAPTION_MAX)
        image_text = _clip(draft.get("image_text", ""), IMAGE_TEXT_MAX).rstrip(".")
        hashtags = normalize_hashtags(draft.get("hashtags", []), f"{a['title']} {a['description'] or ''}",
                                      a["source_name"])
        if not headline or not caption or not image_text or len(hashtags) < HASHTAGS[0]:
            log.warning("  incomplete draft for %r; leaving it for the next run", a["title"][:60])
            return None
        visual = draft.get("visual") if draft.get("visual") in ("photo", "graphic") else "graphic"
        query = _clip(draft.get("image_query", ""), 60) if visual == "photo" else ""
        return {"headline": headline, "caption": caption, "hashtags": hashtags, "image_text": image_text,
                "attribution": attribution(a["source_name"], a["url"]), "visual": visual if query else "graphic",
                "image_query": query, "status": "draft", "review_note": None}

    # --- verification -------------------------------------------------------
    def _verify(self, keys: dict, drafts: dict[str, dict]) -> dict[str, str]:
        """Jev checks each caption against its source; returns {key: review note} for flagged posts."""
        if not self.verifier:
            return {}
        try:
            return self.verifier.review_posts(
                {k: {"headline": d.get("headline", ""), "caption": d.get("caption", ""),
                     "hashtags": d.get("hashtags", [])} for k, d in drafts.items()},
                {k: {"headline": a["title"], "summary": (a["description"] or "")[:400], "publisher": a["source_name"]}
                 for k, a in keys.items() if k in drafts})
        except LLMError as exc:
            log.warning("  Jev review unavailable (%s); posts saved as drafts without the check", exc)
            return {}
