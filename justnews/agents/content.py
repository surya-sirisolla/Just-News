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
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..config import Settings
from ..db import Database
from ..llm import GeminiClient, LLMError, claude_fallback
from ..plain import draft_notes, summary as plain_summary
from ..regions import select_mix

log = logging.getLogger("justnews.content")

HEADLINE_MAX = 80
CAPTION_MAX = 280      # fits X/Twitter; the attribution and hashtags are added separately
IMAGE_TEXT_MAX = 60
IG_CAPTION_MAX = 900   # Instagram allows 2200; hashtags, source and credit are added after
X_POST_MAX = 240       # X allows 280; the source line (and a rare hashtag) are added after
FRESH_HOURS = 1        # "JUST IN:" only for stories this new
HASHTAGS = (0, 5)  # none is fine when nothing fits; never a post held back for its tags
GENERIC_TAGS = {"news", "worldnews", "breakingnews", "breaking", "latest", "latestnews", "newsupdate", "update",
                "updates", "trending", "viral", "headlines", "todaynews", "dailynews", "justnews", "newsalert"}
TAG_WORDS_MAX = 3  # "USMidterms", "ArtificialIntelligence" yes; "AppleRecordQuarterSales" no
REVIEW_THRESHOLD = 0.5
# What a story is about, shown on its slide; the feed's section is only a fallback.
TOPICS = ["India", "Politics", "Business", "Technology", "AI", "Weather", "Environment", "Health", "Science",
          "Sports", "Crime", "World"]
TOPIC_WORDS = [  # first match wins: the keyword fallback when the model gives no topic
    ("Weather", r"\b(rain|rainfall|cyclone|monsoon|flood|floods|imd|heatwave|heat wave|storm|snowfall|thunderstorm)\b"),
    ("Environment", r"\b(aqi|air quality|pollution|smog|climate|emissions?|stubble)\b"),
    ("Health", r"\b(hospital|disease|virus|outbreak|vaccine|dengue|cancer|health|patients?)\b"),
    ("Sports", r"\b(cricket|ipl|football|match|tournament|olympics?|world cup|test series|odi|t20)\b"),
    ("Crime", r"\b(arrested|murder|killed|police|fraud|scam|raid|custody|accused)\b"),
    ("Politics", r"\b(election|elections|minister|bjp|congress|parliament|lok sabha|assembly|president|senate|"
                 r"democrats|republicans|chief minister|polls?)\b"),
    ("AI", r"\b(ai|artificial intelligence|openai|chatgpt|gemini|llm)\b"),
]

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
- hashtags: up to 5 hashtags in CamelCase without the # sign, most useful first, that people
  actually search or follow: the main person, organisation, product, place or event (e.g. "Apple",
  "ISRO", "Delhi", "USMidterms", "AirPollution"), then the broader topic (e.g.
  "ArtificialIntelligence"). Never generic filler ("News", "WorldNews", "BreakingNews", "Latest",
  "Trending"), never made-up compounds nobody searches ("AppleRecordQuarterSales"), not the
  publisher's name, no year unless the source states it. Fewer good tags beat more weak ones; give
  none if nothing fits.
- x_hashtag: one hashtag for X only when the story belongs to a big, widely followed event or topic
  that people track by that tag (e.g. "USMidterms", "IPL2026", "COP31", "UnionBudget"); otherwise
  "". Most stories should have none.
- topic: what the story is about, one of: TOPICS_LIST. The section you are given is the
  feed's and is often too broad (a rain alert from a business feed is "Weather", not "Business").
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
- x_post: the X version, written like a newspaper's feed on X (The Hindu): one or two plain
  sentences that report the news like a lede: who did what, where, and when if the source says,
  with the specific names and numbers the source gives; 120-240 characters. Do not start with
  "According to": state the news, name who made a claim ("Iran says ..."), and end with
  "<publisher> reports." when it is the publisher's own reporting or forecast. No emoji, flags,
  labels ("BREAKING", "JUST IN"), questions, hashtags or links. Word it differently from the
  ig_caption hook. Follow the PLAYBOOK's X rules.
Both versions use ONLY facts from the headline or summary, keep the neutral tone, and contain no
hashtags and no links (they are added separately). The PLAYBOOK never overrides these rules.

PLAIN LANGUAGE, for every field: write so someone who doesn't follow the news understands it on the
first read.
- Everyday words, short sentences (at most about 20 words), one idea per sentence, active voice.
- No newspaper shorthand: not "earmarks, mulls, slams, eyes, nod, curbs, probe, row, amid, sparks,
  U-turn, plea, apex court, the Centre" but "sets aside, considers, criticises, plans, approval,
  limits, investigation, dispute, during, leads to, reversal, request to the court, Supreme Court,
  the central government".
- Spell out or explain abbreviations and special terms the first time: "Supreme Court" not "SC",
  "State Election Commission" not "SEC", "Agniveers (soldiers hired under the four-year Agnipath
  scheme)", "71,000 cubic feet of water per second" not "71,000 cusecs". Drop legal section numbers
  and say what the law is about. Well-known names (BJP, ISRO, IPL, AI, US) can stay.
- Simpler words must not change the facts: keep the same people, places, numbers and claims.
""".replace("TOPICS_LIST", ", ".join(TOPICS))

REVISE_PROMPT = SYSTEM_PROMPT + """
You wrote DRAFTS for these articles, and a reviewer sent NOTES: the engagement strategist (about
form), the plain-language editor (hard words, unexplained abbreviations, long sentences), the fact
checker (about facts not in the source), or the owner (their wishes on wording, length and what to
lead with; if they ask for a fact or figure the headline and summary don't state, leave it out and
say so in `reply`, suggesting they ask for a search for a report that has it). Rewrite each draft following the notes
where they fit the facts; for a fact-check note, remove or rephrase whatever the headline and
summary do not state, in every field. If a note would need facts the source does not give, or would
break a rule above, keep that part and say why in `reply`. `reply` is one short sentence to the
reviewer about what you changed."""
FACT_CHECKER = "fact-checker"  # Jev, in the discussion
EDITOR = "plain-language editor"  # plain.py, in the discussion
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
                    "topic": {"type": "string", "enum": TOPICS},
                    "visual": {"type": "string", "enum": ["photo", "graphic"]},
                    "image_query": {"type": "string"},
                    "ig_caption": {"type": "string"},
                    "x_post": {"type": "string"},
                    "x_hashtag": {"type": "string"},
                },
                "required": ["id", "headline", "caption", "hashtags", "image_text", "topic", "visual", "image_query",
                             "ig_caption", "x_post", "x_hashtag"],
            },
        },
    },
    "required": ["posts"],
}

REVISE_SCHEMA = json.loads(json.dumps(RESPONSE_SCHEMA))
REVISE_SCHEMA["properties"]["posts"]["items"]["properties"]["reply"] = {"type": "string"}
REVISE_SCHEMA["properties"]["posts"]["items"]["required"].append("reply")

LINK_RE = re.compile(r"https?://\S+|\bwww\.\S+|(?<![\w&])#\w+", re.I)  # links and hashtags
ACCORDING_RE = re.compile(r"^According to (?:a report (?:by|from|in) )?(?P<src>[^,]{2,60}),\s*(?P<rest>.+)$", re.I | re.S)
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


def _x_post(text: str) -> str:
    """One plain paragraph like a newspaper's feed: no links, hashtags, labels or leading emoji."""
    text = _clip(LINK_RE.sub("", text or ""), X_POST_MAX)
    m = LABEL_RE.match(text)
    if m:
        text = text[m.end():]
    text = re.sub(r"^\W+", "", text).strip()  # leading flags or emoji
    m = ACCORDING_RE.match(text)
    if m:  # "According to The Guardian, X." -> "X, The Guardian reports." (newspaper-feed style)
        rest = m.group("rest").rstrip(" .")
        text = _clip(f"{rest[:1].upper()}{rest[1:]}, {m.group('src')} reports.", X_POST_MAX)
    return text[:1].upper() + text[1:]


def _is_fresh(a) -> bool:
    try:
        published = datetime.fromisoformat(a["published_at"])
    except (TypeError, ValueError, KeyError, IndexError):
        return False
    return (datetime.now(timezone.utc) - published).total_seconds() <= FRESH_HOURS * 3600


def topic_of(text: str, section: str) -> str:
    """The story's topic from its words; the feed's section when nothing matches."""
    low = text.lower()
    return next((t for t, pattern in TOPIC_WORDS if re.search(pattern, low)),
                section if section in TOPICS else "India" if section == "South India" else "World")


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
        # Capitalise each word, but keep brand spellings with inner capitals ("iPhone", "eBay").
        tag = "".join(w if w[:1].isupper() or w.isdigit() or any(c.isupper() for c in w[1:]) else w.capitalize()
                      for w in words)[:30]
        if tag.lower() in GENERIC_TAGS or len(re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|\d+", tag)) > TAG_WORDS_MAX:
            continue
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
                       for m in settings.gemini_content_models] + claude_fallback(settings)
        self.clients = clients
        self.verifier = verifier if verifier is not None else self._default_verifier()

    def _default_verifier(self):
        if not self.settings.typesafe_api_key:
            return False
        from ..jev import JevScorer
        return JevScorer(self.settings.typesafe_api_key, "jev-1.14.0", 0.042)

    # --- public entry point -------------------------------------------------
    def run(self, run_id: str, limit: int | None = None, mix: bool = True, guidance: str = "") -> ContentResult:
        """Posts for the run's recommended stories: by default the India-first mix of
        `max_posts_per_run` (regions.py); on-demand research takes the best `limit` as they are.
        `guidance` is the owner's note for this batch (e.g. what to lead with)."""
        result = ContentResult(run_id)
        self.guidance = guidance
        limit = limit or self.settings.max_posts_per_run
        candidates = self.db.recommended_without_post(run_id, 500)
        if mix:
            s = self.settings
            articles = select_mix(candidates, limit, s.international_share, s.international_min_score,
                                  s.south_india_share)
            log.info("Run %s: picked %s from %d recommended", run_id,
                     dict(Counter(a["region"] or "International" for a in articles)), len(candidates))
        else:
            articles = candidates[:limit]
        log.info("Run %s: writing posts for %d recommended articles", run_id, len(articles))
        if articles:
            keys = {f"P{i + 1}": {**dict(a), "fresh": _is_fresh(a)} for i, a in enumerate(articles)}
            drafts, model = self._write(keys, result)
            if drafts and self.strategist:
                notes = self.strategist.review(run_id, keys, drafts)
                if notes:
                    drafts.update(self._revise(run_id, keys, drafts, notes))
            if drafts:  # hard words and long sentences get one rewrite (a free check; a model call only if needed)
                plain = {k: draft_notes(d) for k, d in drafts.items()}
                plain = {k: f"Make it plain: {plain_summary(n)}." for k, n in plain.items() if n}
                for k, note in plain.items():
                    self.db.add_agent_message(run_id, EDITOR, self.name, note, keys[k]["id"])
                if plain:
                    drafts.update(self._revise(run_id, keys, drafts, plain, reviewer=EDITOR))
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

    def _owner_note(self) -> str:
        note = getattr(self, "guidance", "")
        return (f"OWNER'S NOTE (follow it for what to lead with and how to word it, but state only facts in "
                f"each article's headline or summary):\n{note}\n\n") if note else ""

    def _write(self, keys: dict, result: ContentResult) -> tuple[dict[str, dict], str]:
        playbook = {k: v for k, v in self._playbook().items() if k in ("instagram", "x")}
        prompt = (self._owner_note() + "PLAYBOOK (from the engagement strategist):\n" +
                  json.dumps(playbook, ensure_ascii=False) +
                  "\n\nARTICLES:\n" + json.dumps(self._items(keys), ensure_ascii=False, indent=0))
        deadline = time.monotonic() + self.settings.gemini_content_time_budget
        for client in self.clients:
            if time.monotonic() > deadline and isinstance(client, GeminiClient):
                log.warning("  content time budget used up; %s skipped", client.model)
                continue  # the paid fallback still gets its turn
            try:
                rows = client.generate_json(SYSTEM_PROMPT, prompt, RESPONSE_SCHEMA).get("posts", [])
                log.info("  %s wrote %d posts", client.model, len(rows))
                return {r["id"]: r for r in rows if isinstance(r, dict) and r.get("id") in keys}, client.model
            except LLMError as exc:
                result.error = str(exc)
                # Unavailable model, daily quota used up, timeouts or overload: try the next free model.
                log.warning("  %s failed (%s); trying the next model", client.model, str(exc)[:160])
        log.error("  no model could write posts; they stay pending for the next run")
        return {}, ""

    def _revise(self, run_id: str, keys: dict, drafts: dict[str, dict], notes: dict[str, str],
                reviewer: str = "strategist") -> dict[str, dict]:
        """One revision round for the drafts a reviewer commented on; replies are saved as messages."""
        playbook = {k: v for k, v in self._playbook().items() if k in ("instagram", "x")}
        items = [{**item, "draft": {f: drafts[item["id"]].get(f) for f in RESPONSE_SCHEMA["properties"]["posts"]
                                    ["items"]["properties"] if f != "id"}, "notes": notes[item["id"]]}
                 for item in self._items({k: keys[k] for k in notes if k in drafts})]
        prompt = (self._owner_note() + "PLAYBOOK (from the engagement strategist):\n" +
                  json.dumps(playbook, ensure_ascii=False) +
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
        source_text = f"{a['title']} {a['description'] or ''}"
        hashtags = normalize_hashtags(draft.get("hashtags", []), source_text, a["source_name"])
        if not headline or not caption or not image_text or len(hashtags) < HASHTAGS[0]:
            log.warning("  incomplete draft for %r; leaving it for the next run", a["title"][:60])
            return None
        visual = draft.get("visual") if draft.get("visual") in ("photo", "graphic") else "graphic"
        query = _clip(draft.get("image_query", ""), 60) if visual == "photo" else ""
        topic = draft.get("topic") if draft.get("topic") in TOPICS else topic_of(source_text, a["category"])
        return {"headline": headline, "caption": caption, "hashtags": hashtags, "image_text": image_text,
                "topic": topic, "attribution": attribution(a["source_name"], a["url"]), "visual": visual if query else "graphic",
                "image_query": query, "status": "draft", "review_note": None,
                "ig_caption": _paragraphs(draft.get("ig_caption", ""), IG_CAPTION_MAX) or None,
                "x_post": _x_post(draft.get("x_post", "")) or None,
                "x_hashtag": next(iter(normalize_hashtags([draft.get("x_hashtag") or ""], source_text,
                                                          a["source_name"])), None)}

    # --- the owner's changes to written posts --------------------------------------------
    def revise_posts(self, run_id: str, article_ids: list[str], instruction: str) -> dict[str, str]:
        """One rewrite of posts already written, with the owner's words as the note, then Jev's fact
        check. Returns the writer's reply per article (e.g. why a requested figure was left out)."""
        posts = {p["article_id"]: p for p in self.db.posts_for_run(run_id)}
        rows = {r["id"]: r for r in self.db.conn.execute(
            f"SELECT * FROM articles WHERE id IN ({','.join('?' * len(article_ids))})", article_ids)}
        ids = [a for a in article_ids if a in posts and a in rows]
        keys = {f"P{i + 1}": {**dict(rows[a]), "fresh": _is_fresh(rows[a])} for i, a in enumerate(ids)}
        drafts = {k: {f: posts[a["id"]][f] for f in ("headline", "caption", "image_text", "topic", "visual", "image_query",
                                                     "ig_caption", "x_post", "x_hashtag")}
                  | {"hashtags": json.loads(posts[a["id"]]["hashtags"])} for k, a in keys.items()}
        for k, a in keys.items():
            self.db.add_agent_message(run_id, "owner", self.name, instruction, a["id"])
        revised = self._revise(run_id, keys, drafts, {k: f"The owner asks: {instruction}" for k in keys},
                               reviewer="owner")
        if not revised:
            return {a: "I couldn't rewrite it right now (no model answered); try again in a minute." for a in ids}
        merged = {k: {**drafts[k], **d} for k, d in revised.items()}
        reviews = self._verify({k: keys[k] for k in merged}, merged)
        replies = {}
        for k, d in merged.items():
            aid, old = keys[k]["id"], posts[keys[k]["id"]]
            post = self._finalize(d, keys[k])
            if post is None:
                replies[aid] = "The rewrite came back incomplete, so I kept the old version."
                continue
            if k in reviews:
                post["status"], post["review_note"] = "needs_review", reviews[k]
            self.db.save_post(run_id, aid, post, old["model"])
            if old["approval"]:
                self.db.set_post_approval([aid], old["approval"])
            if (post["headline"], post["image_text"]) != (old["headline"], old["image_text"]):
                self.db.delete_image(aid)  # the template shows the old text: it is designed again
            replies[aid] = " ".join(str(d.get("reply") or "").split())[:300]
        self._record_usage(run_id)
        return replies

    def _record_usage(self, run_id: str) -> None:
        for u in [c.usage.as_dict() for c in self.clients if c.usage.calls or c.usage.failed_calls]:
            self.db.record_llm_usage(run_id, f"{self.name}:{u['model']}", u)

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
