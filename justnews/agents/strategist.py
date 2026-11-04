"""Agent 6 - Engagement strategist: learns what gets attention, then discusses each draft with the
content creator before it reaches the owner.

Each cycle, before posts are written:
  1. metrics: reads our recent posts' engagement at fixed ages (CHECKPOINTS), which keeps X cheap
     (X charges $0.005 per post read; Instagram reads are free)
  2. benchmarks: every JUSTNEWS_BENCHMARK_EVERY_HOURS, reads recent posts of well-known news
     accounts (Instagram Business Discovery, X timelines)
  3. playbook: when there is new evidence, Gemini turns it into rules for each platform, starting
     from research-based defaults (BASE_PLAYBOOK) and told how small our sample is
While posts are written (content.py):
  4. review: code checks and a Gemini critique compare each draft with the playbook; the content
     creator revises the drafts that got notes, once, and may push back. The exchange is saved in
     `agent_messages` and shown in the run report.
The strategist comments on form only (hook, length, structure, tone, call to action). Facts still
come only from the source, and Jev checks the final text.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from datetime import datetime, timezone
from statistics import median
from zoneinfo import ZoneInfo

from ..config import Settings
from ..db import Database
from ..llm import GeminiClient, LLMError
from .publisher import field_of

log = logging.getLogger("justnews.strategist")

NAME = "strategist"
# Hours after posting at which engagement is read: early pickup, then the settled numbers.
CHECKPOINTS = {"instagram": (3, 24, 72), "x": (6, 48)}
PLAYBOOK_MIN_HOURS, PLAYBOOK_MAX_HOURS = 6, 24  # refresh on new evidence, but not more often than this
BENCHMARK_RETRY_HOURS = 6                       # after a failed benchmark read
HOOK_MAX = 125      # Instagram cuts captions in the feed after about this many characters
X_POST_MAX = 245    # leaves room for the source line in 280
MAX_RULES, RULE_MAX = 7, 220

# Starting rules, from research on what 2026 feed ranking rewards (sources in the README): on
# Instagram shares, saves and comments outweigh likes; on X replies and reposts do, links hurt and
# hashtags barely matter. Big news accounts write differently for each: a readable explainer on
# Instagram, a one-line wire alert on X.
BASE_PLAYBOOK = {
    "instagram": [
        "First line is the hook: the most important fact, stated plainly, in under 120 characters "
        "(the feed cuts the caption there). Don't end it with 'according to ...': attribute in the next paragraph.",
        "Then 1-2 short paragraphs separated by blank lines with the details the source gives: who, what, "
        "where, numbers, who said it. Say why it matters only when the source says so; never add your own reading.",
        "When the source gives little, keep it short (a hook and one paragraph); never pad.",
        "Write it so people want to send it to a friend or save it: concrete, useful, no jargon.",
        "End with one short question about the reader's own experience (e.g. 'Have you tried it?'). Skip it for "
        "deaths, disasters, crime, court cases and allegations; never ask readers to take a side in a political "
        "or legal dispute.",
        "No emoji.",
    ],
    "x": [
        "Write it like a newspaper's feed on X (The Hindu): one or two plain sentences that report the news "
        "like a lede: who did what, where, and when if the source says.",
        "Use the specific names and numbers the source gives.",
        "No emoji, flags, labels ('BREAKING', 'JUST IN'), questions or calls to action.",
        "Don't start with 'According to': state the news, name who made a claim ('Iran says ...'), and end with "
        "'<publisher> reports.' for the publisher's own reporting or forecasts.",
        "Word it differently from the Instagram caption.",
    ],
    "observations": [],
    "experiments": [],
}

PLAYBOOK_PROMPT = """You are the engagement strategist for "Just News", a neutral news account on
Instagram and X. You get the current playbook, our recent posts with their engagement, and recent
posts of large news accounts with theirs. Update the playbook: concrete, checkable writing rules
for each platform that should earn more shares, saves, comments and reposts.

- Our account is new. With fewer than 20 measured posts on a platform, treat our numbers as weak
  evidence and lean on what works for the large accounts, comparing their posts with each other.
- Rules are about the text: hook, length, structure, tone, emoji, question or call to action, what
  to lead with. Both platforms show plain text (no bold, no markdown). Never rules that would add
  facts, opinions, clickbait, outrage or "BREAKING".
- Only state what the EVIDENCE shows. When "others" is empty for a platform, you know nothing about
  other accounts there: no observations about them.
- Keep what works, change what the evidence contradicts. At most 7 rules per platform.
- observations: what the large accounts in the evidence do that we do not, citing the account
  (at most 5, each one sentence).
- experiments: small things to try next (at most 3)."""

PLAYBOOK_SCHEMA = {
    "type": "object",
    "properties": {
        "instagram": {"type": "array", "items": {"type": "string"}},
        "x": {"type": "array", "items": {"type": "string"}},
        "observations": {"type": "array", "items": {"type": "string"}},
        "experiments": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["instagram", "x", "observations", "experiments"],
}

REVIEW_PROMPT = """You are the engagement strategist for "Just News". The content creator wrote
these drafts. Check each one against the PLAYBOOK, for Instagram (ig_caption) and X (x_post)
separately. Comment on form only: hook, length, structure, tone, emoji, the closing question, and
whether the two versions really differ. Never ask for facts that are not in the draft or for hype.
Set ok to true when a draft already follows the playbook well; otherwise give at most two short,
specific notes the writer can act on."""

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "reviews": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "ok": {"type": "boolean"}, "notes": {"type": "string"}},
                "required": ["id", "ok", "notes"],
            },
        },
    },
    "required": ["reviews"],
}


def current_playbook(db: Database) -> dict:
    row = db.latest_playbook()
    return json.loads(row["playbook"]) if row else BASE_PLAYBOOK


def _hours_since(iso: str | None) -> float:
    if not iso:
        return float("inf")
    return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds() / 3600


def _clean_list(items, limit: int, length: int) -> list[str]:
    out = []
    for item in items or []:
        text = " ".join(str(item).split())[:length]
        if text and text not in out:
            out.append(text)
    return out[:limit]


def engagement(row) -> float:
    """One number to rank posts: comments, shares and saves count more than likes."""
    n = lambda key: field_of(row, key) or 0  # benchmarks have no saves
    return n("likes") + 3 * n("comments") + 4 * n("shares") + 2 * n("saves")


def code_notes(draft: dict, publisher: str = "") -> list[str]:
    """Hard limits and checks that need no model."""
    notes = []
    ig, x = (draft.get("ig_caption") or "").strip(), (draft.get("x_post") or "").strip()
    if not ig:
        notes.append("Instagram: the ig_caption is missing.")
    else:
        hook = ig.split("\n", 1)[0]
        if len(hook) > HOOK_MAX:
            notes.append(f"Instagram: the first line is {len(hook)} characters; the hook must fit in {HOOK_MAX - 5}.")
        if "\n" not in ig:
            notes.append("Instagram: split it into a hook line and 2-3 short paragraphs.")
        if re.search(r"\b(according to|reports?|says)\b[^.?!]*[.?!]?$", hook, re.I):
            notes.append("Instagram: don't end the hook with the attribution; move it to the next paragraph.")
    if not x:
        notes.append("X: the x_post is missing.")
    else:
        if len(x) > X_POST_MAX:
            notes.append(f"X: {len(x)} characters; keep it under 240.")
        if re.match(r"\s*according to\b", x, re.I):
            notes.append("X: don't open with 'According to'; state the news and end with '<publisher> reports.'")
        if ig and difflib.SequenceMatcher(None, x.lower(), ig.split("\n", 1)[0].lower()).ratio() > 0.8:
            notes.append("X: it repeats the Instagram hook; write it as a short wire-style alert instead.")
    return notes


class StrategistAgent:
    name = NAME

    def __init__(self, settings: Settings, db: Database, publishers: list | None = None, clients: list | None = None):
        self.settings = settings
        self.db = db
        self.publishers = publishers or []
        if clients is None:
            # The fast "lite" models first: reviews are short, and this leaves the better models'
            # free daily quota for writing the posts.
            clients = [GeminiClient(settings.gemini_api_key, m, 0.0, 0.0,  # free tier: no charge
                                    thinking_level="low", min_interval=settings.gemini_min_interval,
                                    temperature=0.3, timeout=45, retries=1)
                       for m in settings.gemini_chat_models] if settings.gemini_api_key else []
        self.clients = clients
        self.playbook = current_playbook(db)

    # --- before writing -----------------------------------------------------------
    def prepare(self, run_id: str) -> dict:
        """Read new engagement and benchmarks, then refresh the playbook. Never fails the cycle."""
        stats = {"metrics_read": 0, "benchmarks_read": 0}
        for step, key in ((self.refresh_metrics, "metrics_read"), (self.refresh_benchmarks, "benchmarks_read")):
            try:
                stats[key] = step()
            except Exception:
                log.exception("  strategist step %s failed", step.__name__)
        try:
            stats["playbook"] = self.update_playbook(run_id)
        except Exception:
            log.exception("  playbook update failed; keeping the current one")
            stats["playbook"] = "kept (update failed)"
        self.db.save_run_stats(run_id, "strategy", stats)
        self._record_usage(run_id)
        log.info("Run %s: strategist %s", run_id, stats)
        return stats

    def refresh_metrics(self) -> int:
        read = 0
        for pub in self.publishers:
            checkpoints = CHECKPOINTS.get(pub.platform, ())
            due = {}
            for row in self.db.published_posts(pub.platform, days=max(checkpoints, default=0) / 24 + 1):
                age = _hours_since(row["published_at"])
                passed = sum(1 for h in checkpoints if age >= h)
                if passed > (row["checks"] or 0):
                    due[row["media_id"]] = (row["article_id"], passed)
            if not due:
                continue
            metrics = pub.metrics(list(due))
            for media_id, values in metrics.items():
                article_id, passed = due[media_id]
                self.db.save_metrics(article_id, pub.platform, passed, values)
            read += len(metrics)
            log.info("  read engagement of %d %s posts", len(metrics), pub.label)
        return read

    def refresh_benchmarks(self) -> int:
        read = 0
        accounts = {"instagram": self.settings.benchmark_instagram, "x": self.settings.benchmark_x}
        for pub in self.publishers:
            names = accounts.get(pub.platform) or []
            tried = self.db.get_state(f"benchmark_tried:{pub.platform}")
            if not names or _hours_since(self.db.last_benchmark_at(pub.platform)) < self.settings.benchmark_every_hours \
                    or _hours_since(tried) < BENCHMARK_RETRY_HOURS:
                continue
            self.db.set_state(f"benchmark_tried:{pub.platform}", datetime.now(timezone.utc).isoformat(timespec="seconds"))
            posts = pub.benchmark(names, self.settings.benchmark_posts)
            self.db.save_benchmarks(pub.platform, posts)
            read += len(posts)
            log.info("  read %d recent posts of %s on %s", len(posts), ", ".join(names), pub.label)
        return read

    def evidence(self) -> dict:
        tz = ZoneInfo(self.settings.timezone)
        ours, others = {}, {}
        for platform, text_col in (("instagram", "ig_caption"), ("x", "x_post")):
            rows = [r for r in self.db.published_posts(platform, days=30) if r["checks"]]
            ours[platform] = {"measured_posts": len(rows), "posts": [
                {"text": (r[text_col] or f"{r['headline']}\n{r['caption']}")[:300], "category": r["category"],
                 "image": r["image_kind"], "hour": datetime.fromisoformat(r["published_at"]).astimezone(tz).hour,
                 "likes": r["likes"], "comments": r["comments"], "shares": r["shares"], "saves": r["saves"],
                 "views": r["views"]} for r in sorted(rows, key=engagement, reverse=True)[:30]]}
            by_account: dict[str, list] = {}
            for r in self.db.benchmarks(platform, days=self.settings.benchmark_every_hours / 24 + 7):
                by_account.setdefault(r["account"], []).append(r)
            picked = []
            for account, rows in by_account.items():
                rows.sort(key=engagement, reverse=True)
                typical = median(engagement(r) for r in rows)
                for r, label in [(x, "top") for x in rows[:3]] + [(x, "weak") for x in rows[-1:] if len(rows) > 3]:
                    picked.append({"account": account, "followers": r["followers"], "label": label,
                                   "times_typical": round(engagement(r) / typical, 1) if typical else None,
                                   "format": r["media_type"], "text": (r["text"] or "")[:300],
                                   "likes": r["likes"], "comments": r["comments"], "shares": r["shares"],
                                   "views": r["views"]})
            others[platform] = picked
        return {"ours": ours, "others": others}

    def update_playbook(self, run_id: str) -> str:
        latest = self.db.latest_playbook()
        age = _hours_since(latest["created_at"]) if latest else float("inf")
        newest = max(filter(None, [self.db.last_metrics_at(), self.db.last_benchmark_at("instagram"),
                                   self.db.last_benchmark_at("x")]), default=None)
        fresh_evidence = newest is not None and (latest is None or newest > latest["created_at"])
        if latest and age < PLAYBOOK_MAX_HOURS and not (fresh_evidence and age >= PLAYBOOK_MIN_HOURS):
            return f"kept (from {latest['model']}, {age:.0f}h old)"
        evidence = self.evidence()
        has_data = any(evidence["others"].values()) or any(v["measured_posts"] for v in evidence["ours"].values())
        if not has_data or not self.clients:
            if latest is None:
                self.db.save_playbook(BASE_PLAYBOOK, {}, "research defaults")
                self._say(run_id, "Starting playbook (research defaults): " + self._summary(BASE_PLAYBOOK))
            return "research defaults" if latest is None else f"kept ({'no new data' if not has_data else 'no model'})"
        prompt = ("CURRENT PLAYBOOK:\n" + json.dumps(self.playbook, ensure_ascii=False) +
                  "\n\nEVIDENCE:\n" + json.dumps(evidence, ensure_ascii=False))
        out, model = self._ask(PLAYBOOK_PROMPT, prompt, PLAYBOOK_SCHEMA)
        if not out:
            return "kept (no model answered)"
        playbook = {"instagram": _clean_list(out.get("instagram"), MAX_RULES, RULE_MAX) or BASE_PLAYBOOK["instagram"],
                    "x": _clean_list(out.get("x"), MAX_RULES, RULE_MAX) or BASE_PLAYBOOK["x"],
                    # Without benchmark posts, anything said about other accounts would be invented.
                    "observations": _clean_list(out.get("observations"), 5, RULE_MAX)
                    if any(evidence["others"].values()) else [],
                    "experiments": _clean_list(out.get("experiments"), 3, RULE_MAX)}
        counts = {p: v["measured_posts"] for p, v in evidence["ours"].items()}
        self.db.save_playbook(playbook, {"our_measured_posts": counts,
                                         "benchmark_posts": {p: len(v) for p, v in evidence["others"].items()}}, model)
        self.playbook = playbook
        self._say(run_id, "Updated playbook: " + self._summary(playbook))
        return f"updated by {model}"

    # --- while writing: the discussion with the content creator --------------------
    def review(self, run_id: str, keys: dict, drafts: dict[str, dict]) -> dict[str, str]:
        """Notes for the drafts that should change ({key: notes}); every note is also saved as a message."""
        notes = {k: code_notes(d, keys[k].get("source_name", "")) for k, d in drafts.items()}
        if self.clients:
            items = [{"id": k, "fresh": keys[k].get("fresh", False), "headline": d.get("headline"),
                      "ig_caption": d.get("ig_caption"), "x_post": d.get("x_post")} for k, d in drafts.items()]
            out, _ = self._ask(REVIEW_PROMPT, "PLAYBOOK:\n" + json.dumps(self.playbook, ensure_ascii=False) +
                               "\n\nDRAFTS:\n" + json.dumps(items, ensure_ascii=False), REVIEW_SCHEMA)
            for r in (out or {}).get("reviews", []):
                if isinstance(r, dict) and r.get("id") in notes and not r.get("ok") and str(r.get("notes", "")).strip():
                    notes[r["id"]].append(" ".join(str(r["notes"]).split())[:400])
        flagged = {k: " ".join(v) for k, v in notes.items() if v}
        for k in drafts:
            self.db.add_agent_message(run_id, NAME, "content-creator", flagged.get(k, "Looks good, no changes."),
                                      keys[k]["id"])
        log.info("  strategist reviewed %d drafts, asked for changes on %d", len(drafts), len(flagged))
        self._record_usage(run_id)
        return flagged

    # --- helpers --------------------------------------------------------------------
    def _ask(self, system: str, prompt: str, schema: dict) -> tuple[dict | None, str]:
        for client in self.clients:
            try:
                return client.generate_json(system, prompt, schema), client.model
            except LLMError as exc:
                log.warning("  %s could not answer (%s); trying the next model", client.model, str(exc)[:120])
        return None, ""

    def _say(self, run_id: str, body: str) -> None:
        self.db.add_agent_message(run_id, NAME, "content-creator", body)

    @staticmethod
    def _summary(playbook: dict) -> str:
        return (f"Instagram: {' / '.join(playbook['instagram'][:3])} | X: {' / '.join(playbook['x'][:3])}")[:1500]

    def _record_usage(self, run_id: str) -> None:
        for c in self.clients:
            if c.usage.calls or c.usage.failed_calls:
                self.db.record_llm_usage(run_id, f"{NAME}:{c.model}", c.usage.as_dict())  # running totals
