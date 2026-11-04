"""Agent 2 - News Evaluator.

Scores each article discovered in a run on relevance, credibility, freshness and
audience interest, detects duplicates (same URL in an earlier run, or the same
story from different outlets in this or earlier runs) and decides whether it is
worth posting.

Scoring split:
  freshness         - deterministic, from article age
  credibility       - curated publisher table; LLM estimate for unknown publishers
  relevance         - LLM
  audience_interest - LLM
  duplicates        - SQLite (exact URL), title similarity and LLM same-story judgement
"""

from __future__ import annotations

import json
import re
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone

from ..config import Settings
from ..db import Database
from ..llm import LLMError, make_llm_client
from ..models import Article, Evaluation, title_similarity
from ..sources import SOURCE_CREDIBILITY

log = logging.getLogger("justnews.evaluator")

WEIGHTS = {"relevance": 0.30, "audience_interest": 0.30, "credibility": 0.20, "freshness": 0.20}
FRESHNESS_HALF_LIFE_H = 24
FUZZY_DUPLICATE_THRESHOLD = 0.6
HISTORY_LIMIT = 80

SYSTEM_PROMPT = """You are the News Evaluator for "Just News", a social media account that posts
important, credible, timely stories for a broad audience in India, especially South India: Indian and
South Indian news first, plus Business, Technology, AI and major World news. Evaluate each article from
its headline, description, publisher and age.

Score each article with integers 0-100:
- relevance: newsworthiness and importance within its category. High for significant developments
  (major launches, research breakthroughs, policy/regulation, elections, large deals, earnings that move
  markets, conflicts, disasters). Low for minor updates, opinion/analysis columns, listicles, how-tos,
  reviews, evergreen features, celebrity gossip or hyper-local news.
- audience_interest: how likely a broad social audience in India is to care, click and share. World
  news scores high only when it matters to Indian readers too.
- credibility: trustworthiness of the publisher as a news source (established outlets high; unknown
  blogs, press releases, content farms, tabloids low).

duplicate_of: if the article reports the SAME story/event as another article (an earlier one in this
batch, one in ALREADY REVIEWED THIS RUN, or one in PREVIOUSLY COVERED), put that article's id.
Different outlets covering the same event ARE duplicates. A follow-up with materially new facts is NOT.
Otherwise use an empty string.

verdict: "post" if it is worth posting, otherwise "discard".
reason: one sentence, max 25 words, explaining why it is worth posting or should be discarded."""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "articles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "relevance": {"type": "integer"},
                    "audience_interest": {"type": "integer"},
                    "credibility": {"type": "integer"},
                    "duplicate_of": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["post", "discard"]},
                    "reason": {"type": "string"},
                },
                "required": ["id", "relevance", "audience_interest", "credibility", "duplicate_of", "verdict",
                             "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["articles"],
    "additionalProperties": False,
}


def freshness_score(published_at: datetime | None, now: datetime) -> int:
    if published_at is None:
        return 30
    age_h = max((now - published_at).total_seconds() / 3600, 0)
    return round(100 * 0.5 ** (age_h / FRESHNESS_HALF_LIFE_H))


_CREDIBILITY_PATTERNS = [(re.compile(rf"\b{re.escape(k)}\b"), v) for k, v in SOURCE_CREDIBILITY.items()]


def known_credibility(source_name: str) -> int | None:
    name = source_name.lower()
    matches = [score for pattern, score in _CREDIBILITY_PATTERNS if pattern.search(name)]
    return max(matches) if matches else None


def clamp(v) -> int:
    try:
        return max(0, min(100, int(v)))
    except (TypeError, ValueError):
        return 0


@dataclass
class EvaluationResult:
    run_id: str
    evaluations: list[Evaluation]
    discovered: int
    not_evaluated: int
    usage: dict
    llm_error: str | None = None

    def stats(self) -> dict:
        decisions = Counter(e.decision for e in self.evaluations)
        return {
            "received": self.discovered,
            "evaluated": len(self.evaluations),
            "not_evaluated_llm_error": self.not_evaluated,
            "recommended": decisions.get("Recommended", 0),
            "rejected": decisions.get("Rejected", 0),
            "duplicates": sum(1 for e in self.evaluations if e.duplicate_of),
            "llm_error": self.llm_error,
            "llm_usage": self.usage,
        }


class _UnionFind:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        self.parent[self.find(a)] = self.find(b)


class NewsEvaluatorAgent:
    name = "news-evaluator"

    def __init__(self, settings: Settings, db: Database, llm=None):
        self.settings = settings
        self.db = db
        self.llm = llm or make_llm_client(settings)
        self.last_error: str | None = None

    def run(self, run_id: str, fresh_look: bool = False) -> EvaluationResult:
        """`fresh_look` (on-demand research): score every article, including ones seen in earlier
        runs, and don't reject stories as covered before; the owner asked for this topic."""
        articles = self.db.articles_for_run(run_id)
        log.info("Run %s: evaluating %d discovered articles with %s", run_id, len(articles), self.llm.model)
        now = datetime.now(timezone.utc)

        # 1. Exact repeats of articles already evaluated in an earlier run: no LLM needed.
        evaluations: dict[str, Evaluation] = {}
        fresh: list[Article] = []
        for a in articles:
            prior = None if fresh_look else self.db.prior_evaluation(a.id, run_id)
            if prior:
                evaluations[a.id] = Evaluation(
                    a.id, 0, known_credibility(a.source_name) or 0, freshness_score(a.published_at, now), 0, 0,
                    "Rejected", f"Previously covered: same URL already evaluated in run {prior['run_id']} "
                                f"({prior['decision']}).", duplicate_of=a.id, llm_scored=False)
            else:
                fresh.append(a)
        log.info("  %d already evaluated in earlier runs, %d need scoring", len(articles) - len(fresh), len(fresh))

        # 2. Scoring, with earlier-run recommendations as context for "previously covered".
        history = [] if fresh_look else self.db.recent_history(run_id, self.settings.history_days)[:HISTORY_LIMIT]
        history_ids = {f"H{i + 1}": row for i, row in enumerate(history)}
        keys = {f"A{i + 1}": a for i, a in enumerate(self._order_by_similarity(fresh))}
        if hasattr(self.llm, "score_articles"):
            llm_rows, not_evaluated, links = self._score_with_jev(keys, history_ids)
        else:
            llm_rows, not_evaluated = self._score_with_llm(keys, history_ids, now)
            links = None

        # 3. Combine scores, resolve duplicate clusters, decide.
        evaluations.update(self._decide(keys, llm_rows, history_ids, now, links))
        result_list = list(evaluations.values())
        self.db.save_evaluations(run_id, result_list)
        usage = self.llm.usage.as_dict()
        self.db.record_llm_usage(run_id, self.name, usage)
        result = EvaluationResult(run_id, result_list, len(articles), not_evaluated, usage, self.last_error)
        self.db.save_run_stats(run_id, "evaluation", result.stats())
        log.info("Run %s: %s", run_id, {k: v for k, v in result.stats().items() if k != "llm_usage"})
        log.info("Run %s: LLM usage %s", run_id, usage)
        return result

    # --- scoring backends -------------------------------------------------------
    def _score_with_jev(self, keys: dict[str, Article], history_ids: dict) -> tuple[dict, int, list]:
        """Typed per-article questions plus pairwise same-event checks (see justnews/jev.py)."""
        try:
            rows = self.llm.score_articles(keys, known_credibility)
            # Round-ups bundle several stories, so they would link unrelated articles together.
            links, covered = self.llm.find_duplicates(
                {k: keys[k] for k, r in rows.items() if not r.get("roundup")}, history_ids)
        except LLMError as exc:
            log.error("  Jev rejected the request; leaving %d articles for the next run: %s", len(keys), exc)
            self.last_error = str(exc)
            return {}, len(keys), []
        for k, hid in covered.items():
            rows[k]["duplicate_of"] = hid
        log.info("  scored %d/%d articles with Jev", len(rows), len(keys))
        return rows, len(keys) - len(rows), links

    def _score_with_llm(self, keys: dict[str, Article], history_ids: dict, now: datetime) -> tuple[dict, int]:
        """Batches of articles to a generative LLM returning JSON rows."""
        llm_rows: dict[str, dict] = {}
        not_evaluated = 0
        batch_keys = list(keys)
        for start in range(0, len(batch_keys), self.settings.eval_batch_size):
            batch = batch_keys[start:start + self.settings.eval_batch_size]
            reviewed = batch_keys[:start]
            try:
                rows = self._score_batch(batch, keys, reviewed, history_ids, now)
            except LLMError as exc:
                if exc.fatal:  # bad key/model/billing: every remaining batch would fail the same way
                    remaining = len(batch_keys) - start
                    log.error("  LLM rejected the request; leaving %d articles for the next run: %s",
                              remaining, exc)
                    not_evaluated += remaining
                    self.last_error = str(exc)
                    break
                log.error("  batch %d-%d failed, leaving unevaluated for the next run: %s",
                          start + 1, start + len(batch), exc)
                not_evaluated += len(batch)
                continue
            for k in batch:
                if k in rows:
                    llm_rows[k] = rows[k]
                else:
                    log.warning("  LLM omitted %s; leaving unevaluated", k)
                    not_evaluated += 1
            log.info("  scored batch %d-%d", start + 1, start + len(batch))
        return llm_rows, not_evaluated

    # --- helpers ------------------------------------------------------------
    @staticmethod
    def _order_by_similarity(articles: list[Article]) -> list[Article]:
        """Place look-alike headlines next to each other so likely duplicates share a batch."""
        ordered: list[Article] = []
        remaining = list(articles)
        while remaining:
            seed = remaining.pop(0)
            group = [seed] + [a for a in remaining if title_similarity(seed.title, a.title) >= 0.3]
            remaining = [a for a in remaining if a not in group]
            ordered.extend(group)
        return ordered

    def _score_batch(self, batch: list[str], keys: dict[str, Article], reviewed: list[str],
                     history_ids: dict, now: datetime) -> dict[str, dict]:
        def describe(k: str) -> dict:
            a = keys[k]
            age = round((now - a.published_at).total_seconds() / 3600, 1) if a.published_at else None
            return {"id": k, "category": a.category, "publisher": a.source_name, "age_hours": age,
                    "title": a.title, "description": a.description[:300]}

        parts = []
        if history_ids:
            parts.append("PREVIOUSLY COVERED (recommended in earlier runs):\n" + "\n".join(
                f"{hid}: [{r['category']}] {r['title']} ({r['source_name']})" for hid, r in history_ids.items()))
        if reviewed:
            parts.append("ALREADY REVIEWED THIS RUN:\n" + "\n".join(
                f"{k}: [{keys[k].category}] {keys[k].title} ({keys[k].source_name})" for k in reviewed))
        parts.append("ARTICLES TO EVALUATE (return one object per id):\n"
                     + json.dumps([describe(k) for k in batch], ensure_ascii=False, indent=0))
        rows = self.llm.generate_json(SYSTEM_PROMPT, "\n\n".join(parts), RESPONSE_SCHEMA).get("articles", [])
        return {r["id"]: r for r in rows if isinstance(r, dict) and r.get("id") in batch}

    def _decide(self, keys: dict[str, Article], llm_rows: dict[str, dict], history_ids: dict,
                now: datetime, links: list[tuple[str, str]] | None = None) -> dict[str, Evaluation]:
        th = self.settings.recommend_threshold
        scored: dict[str, Evaluation] = {}
        verdicts: dict[str, str] = {}
        uf = _UnionFind()
        previously_covered: dict[str, dict] = {}

        for k, row in llm_rows.items():
            a = keys[k]
            cred = known_credibility(a.source_name)
            ev = Evaluation(
                article_id=a.id,
                relevance=clamp(row["relevance"]),
                credibility=cred if cred is not None else clamp(row["credibility"]),
                freshness=freshness_score(a.published_at, now),
                audience_interest=clamp(row["audience_interest"]),
                overall=0, decision="", reason=str(row.get("reason", "")).strip())
            ev.overall = round(sum(getattr(ev, f) * w for f, w in WEIGHTS.items()))
            scored[k] = ev
            verdicts[k] = row.get("verdict", "discard")
            uf.find(k)
            dup = (row.get("duplicate_of") or "").strip()
            if dup in llm_rows and dup != k:
                uf.union(k, dup)
            elif dup in history_ids:
                previously_covered[k] = history_ids[dup]

        # Title similarity backs up the model for near-identical headlines from different outlets.
        ks = list(scored)
        edges = [(k1, k2) for i, k1 in enumerate(ks) for k2 in ks[i + 1:]
                 if title_similarity(keys[k1].title, keys[k2].title) >= FUZZY_DUPLICATE_THRESHOLD]

        clusters: dict[str, list[str]] = {}
        if links is not None:
            # Jev checked every candidate pair directly, so an article is a duplicate only if it is
            # linked to the kept article itself. Chaining through a third article would let a
            # round-up ("Top stories: Anthropic...; Marvell...") merge two unrelated stories.
            adjacent: dict[str, set[str]] = {k: set() for k in scored}
            for k1, k2 in [*links, *edges]:
                if k1 in scored and k2 in scored:
                    adjacent[k1].add(k2)
                    adjacent[k2].add(k1)
            for k in sorted(scored, key=lambda k: (scored[k].overall, scored[k].credibility), reverse=True):
                kept = next((p for p in clusters if p in adjacent[k]), None)
                clusters.setdefault(kept or k, []).append(k)
        else:
            # LLM rows link each article to just one earlier one, so follow chains of links.
            for k1, k2 in edges:
                uf.union(k1, k2)
            for k in scored:
                clusters.setdefault(uf.find(k), []).append(k)

        for members in clusters.values():
            # Keep the strongest version of the story; the rest are duplicates of it.
            primary = max(members, key=lambda k: (scored[k].overall, scored[k].credibility))
            # The LLM often says "discard" for an article only because it saw it as a copy of another,
            # so judge the story by its copies: worth posting if any copy got "post".
            supporters = [m for m in members if verdicts[m] == "post"]
            if supporters and verdicts[primary] != "post":
                verdicts[primary] = "post"
                scored[primary].reason = scored[max(supporters, key=lambda m: scored[m].overall)].reason
            covered = next((previously_covered[m] for m in members if m in previously_covered), None)
            for k in members:
                ev, a = scored[k], keys[k]
                if k != primary:
                    p = keys[primary]
                    ev.decision, ev.duplicate_of = "Rejected", p.id
                    ev.reason = f"Duplicate story: same event as \"{p.title}\" ({p.source_name}), which scored higher."
                elif covered:
                    h = covered
                    ev.decision, ev.duplicate_of = "Rejected", h["id"]
                    ev.reason = (f"Previously covered: same story as \"{h['title']}\" ({h['source_name']}) "
                                 f"evaluated {h['evaluated_at'][:10]}.")
                elif verdicts[k] == "post" and ev.overall >= th:
                    ev.decision = "Recommended"
                elif verdicts[k] == "post":
                    ev.decision = "Rejected"
                    ev.reason = f"Overall {ev.overall} below threshold {th} (freshness {ev.freshness}). {ev.reason}"
                else:
                    ev.decision = "Rejected"
                if len(members) > 1 and k == primary and ev.decision == "Recommended":
                    ev.reason += f" (Best of {len(members)} outlets covering this story.)"
        return {scored[k].article_id: scored[k] for k in scored}
