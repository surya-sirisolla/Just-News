"""Agent 3 - Content Creator.

Turns the evaluator's recommended articles into draft social posts: a short headline, a caption,
hashtags, a source attribution, the text for the image, a suggestion for the image designer
(licensed photo or text graphic, plus a photo search query), and a separate version for each
platform: an explainer-style Instagram caption and a short wire-style X post.

When the engagement strategist (strategist.py) is on, the writing follows its playbook, and the
strategist reviews the drafts: the drafts that get notes are revised once, and the content creator
may push back when a note would need facts the source doesn't give.

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

from datetime import datetime, timezone

from ..config import Settings
from ..db import Database
from ..llm import GeminiClient, LLMError

log = logging.getLogger("justnews.content")

HEADLINE_MAX = 80
CAPTION_MAX = 280      # fits X/Twitter; the attribution and hashtags are added separately
IMAGE_TEXT_MAX = 60
IG_CAPTION_MAX = 900   # Instagram allows 2200; hashtags, source and credit are added after
X_POST_MAX = 230       # X allows 280; hashtags, source and credit are added after
FRESH_HOURS = 1        # "JUST IN:" only for stories this new
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
- ig_caption: the Instagram version, 120-700 characters, for people who read. Line 1 is the hook,
  at most 120 characters: the key fact stated plainly. Then 1-2 short paragraphs separated by a
  blank line ("\n\n") with the details the source gives. No interpretation or context the source
  does not state; when the summary is short, so is the caption. Follow the PLAYBOOK's Instagram rules.
- x_post: the X version, at most 200 characters, for people who scan: one or two lines that lead
  with the news itself. It must read differently from the ig_caption hook. Follow the PLAYBOOK's
  X rules. Start with "JUST IN:" only when the article has "fresh": true.
Both versions use ONLY facts from the headline or summary, keep the neutral tone, and contain no
hashtags and no links (they are added separately). The PLAYBOOK never overrides these rules.
"""

REVISE_PROMPT = SYSTEM_PROMPT + """
You wrote DRAFTS for these articles, and a reviewer sent NOTES: the engagement strategist (about
form) or the fact checker (about facts not in the source). Rewrite each draft following the notes
where they fit the facts; for a fact-check note, remove or rephrase whatever the headline and
summary do not state, in every field. If a note would need facts the source does not give, or would
break a rule above, keep that part and say why in `reply`. `reply` is one short sentence to the
reviewer about what you changed."""
FACT_CHECKER = "fact-checker"  # Jev, in the discussion
ALSO_COVERED_RE = re.compile(r"\s*Also covered by:.*$", re.S)

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
                    "ig_caption": {"type": "string"},
                    "x_post": {"type": "string"},
                },
                "required": ["id", "headline", "caption", "hashtags", "image_text", "visual", "image_query",
                             "ig_caption", "x_post"],
            },
        },
    },
    "required": ["posts"],
}

REVISE_SCHEMA = json.loads(json.dumps(RESPONSE_SCHEMA))
REVISE_SCHEMA["properties"]["posts"]["items"]["properties"]["reply"] = {"type": "string"}
REVISE_SCHEMA["properties"]["posts"]["items"]["required"].append("reply")

LINK_RE = re.compile(r"https?://\S+|\bwww\.\S+|(?<![\w&])#\w+", re.I)  # links and hashtags
LABEL_RE = re.compile(r"^\s*(?:\W{0,4}\s*)?(BREAKING(?: NEWS)?|JUST IN)\s*[:\-–]\s*", re.I)


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:-")
    return cut + "…"


def _paragraphs(text: str, limit: int) -> str:
    """Like _clip, but keeps paragraph breaks (at most one blank line between paragraphs)."""
    paras = [" ".join(LINK_RE.sub("", p).split()) for p in re.split(r"\n+", text or "")]
    out = "\n\n".join(p for p in paras if p)
    if len(out) <= limit:
        return out
    return out[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:-") + "…"


def _x_post(text: str, fresh: bool) -> str:
    """One paragraph, no links or hashtags; never "BREAKING", and "JUST IN" only for fresh news."""
    text = _clip(LINK_RE.sub("", text or ""), X_POST_MAX)
    m = LABEL_RE.match(text)
    if m and (m.group(1).upper().startswith("BREAKING") or not fresh):
        text = text[: m.start(1)].strip() + " " + text[m.end():].lstrip()
        text = text.strip()
        text = text[:1].upper() + text[1:]
    return text


def _is_fresh(a) -> bool:
    try:
        published = datetime.fromisoformat(a["published_at"])
    except (TypeError, ValueError, KeyError, IndexError):
        return False
    return (datetime.now(timezone.utc) - published).total_seconds() <= FRESH_HOURS * 3600


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

    def __init__(self, settings: Settings, db: Database, clients: list | None = None, verifier=None,
                 strategist=None):
        self.settings = settings
        self.db = db
        self.strategist = strategist  # StrategistAgent: playbook + review; None -> latest saved playbook
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
            keys = {f"P{i + 1}": {**dict(a), "fresh": _is_fresh(a)} for i, a in enumerate(articles)}
            drafts, model = self._write(keys, result)
            if drafts and self.strategist:
                notes = self.strategist.review(run_id, keys, drafts)
                if notes:
                    drafts.update(self._revise(run_id, keys, drafts, notes))
            reviews = self._verify(keys, drafts) if drafts else {}
            if reviews:  # the fact checker's notes get one revision round too, then a second check
                for k, note in reviews.items():
                    self.db.add_agent_message(run_id, FACT_CHECKER, self.name, note, keys[k]["id"])
                revised = self._revise(run_id, keys, drafts, {
                    k: f"{n}. Remove or rephrase anything the headline and summary do not state."
                    for k, n in reviews.items()}, reviewer=FACT_CHECKER)
                drafts.update(revised)
                if revised:
                    again = self._verify({k: keys[k] for k in revised}, revised)
                    reviews = {k: n for k, n in reviews.items() if k not in revised or k in again}
                    reviews.update(again)
                    for k in revised:
                        self.db.add_agent_message(run_id, FACT_CHECKER, self.name,
                                                  f"Still flagged: {again[k]}" if k in again else "Checked again: fine now.",
                                                  keys[k]["id"])
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
    def _playbook(self) -> dict:
        if self.strategist:
            return self.strategist.playbook
        from .strategist import current_playbook
        return current_playbook(self.db)

    @staticmethod
    def _items(keys: dict) -> list[dict]:
        # Discovery notes which other outlets ran a Google News story; that is not a fact to report.
        return [{"id": k, "section": a["category"], "publisher": a["source_name"], "headline": a["title"],
                 "summary": ALSO_COVERED_RE.sub("", a["description"] or "")[:400], "fresh": a["fresh"]}
                for k, a in keys.items()]

    def _write(self, keys: dict, result: ContentResult) -> tuple[dict[str, dict], str]:
        playbook = {k: v for k, v in self._playbook().items() if k in ("instagram", "x")}
        prompt = ("PLAYBOOK (from the engagement strategist):\n" + json.dumps(playbook, ensure_ascii=False) +
                  "\n\nARTICLES:\n" + json.dumps(self._items(keys), ensure_ascii=False, indent=0))
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

    def _revise(self, run_id: str, keys: dict, drafts: dict[str, dict], notes: dict[str, str],
                reviewer: str = "strategist") -> dict[str, dict]:
        """One revision round for the drafts a reviewer commented on; replies are saved as messages."""
        playbook = {k: v for k, v in self._playbook().items() if k in ("instagram", "x")}
        items = [{**item, "draft": {f: drafts[item["id"]].get(f) for f in RESPONSE_SCHEMA["properties"]["posts"]
                                    ["items"]["properties"] if f != "id"}, "notes": notes[item["id"]]}
                 for item in self._items({k: keys[k] for k in notes if k in drafts})]
        prompt = ("PLAYBOOK (from the engagement strategist):\n" + json.dumps(playbook, ensure_ascii=False) +
                  "\n\nDRAFTS AND NOTES:\n" + json.dumps(items, ensure_ascii=False, indent=0))
        for client in self.clients:
            try:
                rows = client.generate_json(REVISE_PROMPT, prompt, REVISE_SCHEMA).get("posts", [])
            except LLMError as exc:
                log.warning("  %s could not revise (%s); trying the next model", client.model, str(exc)[:160])
                continue
            revised = {r["id"]: r for r in rows if isinstance(r, dict) and r.get("id") in notes}
            for k, r in revised.items():
                reply = " ".join(str(r.get("reply") or "Revised.").split())[:400]
                self.db.add_agent_message(run_id, self.name, reviewer, reply, keys[k]["id"])
            log.info("  %s revised %d drafts after the strategist's notes", client.model, len(revised))
            return revised
        log.warning("  no model could revise; the first drafts are kept")
        return {}

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
                "image_query": query, "status": "draft", "review_note": None,
                "ig_caption": _paragraphs(draft.get("ig_caption", ""), IG_CAPTION_MAX) or None,
                "x_post": _x_post(draft.get("x_post", ""), a["fresh"]) or None}

    # --- verification -------------------------------------------------------
    def _verify(self, keys: dict, drafts: dict[str, dict]) -> dict[str, str]:
        """Jev checks each caption against its source; returns {key: review note} for flagged posts."""
        if not self.verifier:
            return {}
        try:
            return self.verifier.review_posts(
                {k: {"headline": d.get("headline", ""), "caption": d.get("caption", ""),
                     "instagram": d.get("ig_caption", ""), "x": d.get("x_post", ""),
                     "hashtags": d.get("hashtags", [])} for k, d in drafts.items()},
                {k: {"headline": a["title"], "summary": (a["description"] or "")[:400], "publisher": a["source_name"]}
                 for k, a in keys.items() if k in drafts})
        except LLMError as exc:
            log.warning("  Jev review unavailable (%s); posts saved as drafts without the check", exc)
            return {}
