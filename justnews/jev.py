"""Evaluator backend on TypeSafe's Jev, a System One model for typed decisions.

Jev answers typed questions (Score, Noul) about a `state` with calibrated probabilities. It does
not generate text, so it makes the judgments and our code does everything else:

  per article (one request, questions answered in parallel)
    relevance          Score, 5 described levels   -> 0-100
    audience_interest  Score, 4 described levels   -> 0-100
    is_news_event      Noul: reports a new event, rather than opinion/review/how-to/promo
    is_roundup         Noul: bundles several unrelated stories (digest) -> discard, skip duplicate checks
    credibility        Score, 4 levels (only for publishers missing from SOURCE_CREDIBILITY)

  per candidate pair (pre-filtered in code by shared names/words, one request each)
    same_event         Score: different / related or follow-up / same event

The reason shown in reports is assembled from the levels Jev picked. Dates, ages and arithmetic
stay in code, as TypeSafe recommends.
"""

from __future__ import annotations

import itertools
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor

from typesafe_sdk import (
    Noul,
    RetryPolicy,
    Score,
    TypeSafeAPIError,
    TypeSafeAuthenticationError,
    TypeSafeBadRequestError,
    TypeSafeClient,
    TypeSafeError,
    TypeSafeNotFoundError,
    TypeSafePermissionDeniedError,
    TypeSafeUnprocessableEntityError,
)

from .llm import LLMError, Usage
from .models import STOPWORDS, Article, title_tokens

log = logging.getLogger("justnews.jev")

FATAL_ERRORS = (TypeSafeAuthenticationError, TypeSafePermissionDeniedError, TypeSafeNotFoundError,
                TypeSafeBadRequestError, TypeSafeUnprocessableEntityError)
SAME_EVENT_PROBABILITY = 0.5   # P(level "same event") needed to treat a pair as a duplicate
NEWS_EVENT_THRESHOLD = 0.5     # is_news_event noul needed for a "post" verdict
LOW_CONFIDENCE = 0.4           # flag Score answers this unsure in the reason

# (description sent to Jev, short label used in the reason)
RELEVANCE_LEVELS = [
    ("Not a news report: an opinion column, review, how-to guide, listicle, quiz, promotion or trivia",
     "not a news report"),
    ("Minor or niche news: a small product update, routine announcement, or local single-incident story",
     "minor or niche news"),
    ("Moderately notable news: a meaningful development for one industry, region or community",
     "moderately notable news"),
    ("Significant news: a major company, policy, legal, market or conflict development with wide impact",
     "significant news"),
    ("Major breaking news: a historic or globally important event that will dominate headlines",
     "major breaking news"),
]
INTEREST_LEVELS = [
    ("Few people outside a narrow specialist group would care about it", "narrow appeal"),
    ("Interesting mainly to people who already follow this specific topic", "appeals to topic followers"),
    ("Of broad interest: many general readers would click on it", "broad audience interest"),
    ("Highly shareable: most people would want to read and discuss it", "highly shareable"),
]
CREDIBILITY_LEVELS = [
    ("An unreliable source: tabloid, content farm, press release, promotional or anonymous site", 25),
    ("A little-known website, niche blog or aggregator with limited editorial standards", 50),
    ("An established regional, trade or specialist news outlet with editorial standards", 75),
    ("A major national or international news organization with a strong reputation", 92),
]
SAME_EVENT_LEVELS = [
    "They report different events or topics",
    "They cover the same broader topic, or one is a follow-up that adds materially new facts",
    "They report the same specific event or announcement, possibly from different outlets",
]

ARTICLE_QUESTIONS = {
    "relevance": Score(
        instructions="How newsworthy and important is the article in `article`?",
        criteria=[d for d, _ in RELEVANCE_LEVELS]),
    "audience_interest": Score(
        instructions="How interested would a broad, international social media audience be in the article in `article`?",
        criteria=[d for d, _ in INTEREST_LEVELS]),
    "is_news_event": Noul(
        instructions="Does the article in `article` report a specific new event, announcement or development?"),
    "is_roundup": Noul(
        instructions="Is the article in `article` a news round-up or digest that bundles several unrelated stories?"),
}
CREDIBILITY_QUESTION = Score(
    instructions="How credible is `article.publisher` as a news source?",
    criteria=[d for d, _ in CREDIBILITY_LEVELS])
PAIR_QUESTION = Score(
    instructions="How do the news reports `article_a` and `article_b` relate to each other?",
    criteria=SAME_EVENT_LEVELS)


def _entities(title: str) -> set[str]:
    """Capitalised words (people, companies, places): a cheap same-story signal. Headlines usually
    open with their subject ("Anthropic posts..."), so the first word counts unless it is common."""
    words = [re.sub(r"['’]s$", "", w).strip("'’.") for w in re.findall(r"[A-Za-z][\w'’&.-]*", title)]
    return {w.lower() for w in words if w[:1].isupper() and len(w) > 2 and w.lower() not in STOPWORDS}


def _stems(title: str) -> set[str]:
    """Crude stems (first 5 letters), so "falls"/"fall" and "cyberattacks"/"cyber-hacks" match."""
    return {w[:5] for w in title_tokens(title) if len(w) >= 3}


def is_candidate_pair(a: str, b: str, same_category: bool = False) -> bool:
    """Cheap recall-oriented pre-filter; Jev makes the actual same-event decision."""
    shared = len(_stems(a) & _stems(b))
    return shared >= 2 or bool(_entities(a) & _entities(b)) or (same_category and shared >= 1)


def _article_state(a: Article) -> dict:
    return {"article": {"headline": a.title, "summary": a.description[:300] or "(none)",
                        "publisher": a.source_name, "section": a.category}}


def _pct(answer) -> int:
    top = max(answer.legend)
    return round(100 * answer.score / top) if top else 0


def _top_level(answer) -> int:
    return max(answer.probabilities, key=answer.probabilities.get)


class JevScorer:
    """Scores articles and finds duplicate pairs. Same `model`/`usage` surface as the LLM clients."""

    def __init__(self, api_key: str, model: str, price_input_per_m: float, workers: int = 6,
                 timeout: float = 30.0):
        if not api_key:
            raise LLMError("TYPESAFE_API_KEY is not set (add it to .env or the environment)", fatal=True)
        self.model = model
        self.workers = workers  # TypeSafe's public endpoint rate-limits above roughly eight in flight
        self.client = TypeSafeClient(api_key=api_key, model=model, timeout=timeout,
                                     base_url="https://api.typesafe.ai", retry=RetryPolicy(max_retries=3))
        self.usage = Usage(model, price_input_per_m, 0.0)  # output tokens are free
        self._lock = threading.Lock()
        self._fatal: LLMError | None = None

    # --- transport ------------------------------------------------------------
    def _ask(self, state, questions):
        if self._fatal:
            raise self._fatal
        try:
            resp = self.client.system_one(state, questions)
        except FATAL_ERRORS as exc:
            err = LLMError(f"HTTP {getattr(exc, 'status', '?')}: {exc}", fatal=True)
            with self._lock:
                self.usage.failed_calls += 1
                self._fatal = err
            raise err from exc
        except (TypeSafeAPIError, TypeSafeError) as exc:
            with self._lock:
                self.usage.failed_calls += 1
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc
        with self._lock:
            self.usage.calls += 1
            self.usage.input_tokens += resp.usage.input_tokens or 0
            self.usage.output_tokens += resp.usage.output_tokens or 0
        return resp

    def _map(self, fn, items):
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            return list(pool.map(fn, items))

    # --- per-article scoring ----------------------------------------------------
    def score_articles(self, keys: dict[str, Article], known_credibility) -> dict[str, dict]:
        """Return evaluator rows keyed like `keys`; articles whose request failed are omitted."""

        def one(k: str):
            a = keys[k]
            questions = dict(ARTICLE_QUESTIONS)
            if known_credibility(a.source_name) is None:
                questions["credibility"] = CREDIBILITY_QUESTION
            try:
                return k, self._ask(_article_state(a), questions)
            except LLMError as exc:
                if not exc.fatal:
                    log.warning("  Jev failed for %s (%s): %s", k, a.title[:60], exc)
                return k, None

        rows: dict[str, dict] = {}
        for k, resp in self._map(one, list(keys)):
            if self._fatal:
                raise self._fatal
            if resp is None:
                continue
            ans = resp.answers
            rel, interest, news = ans["relevance"], ans["audience_interest"], ans["is_news_event"].noul
            cred = None
            if "credibility" in ans:
                c = ans["credibility"]
                cred = round(sum(CREDIBILITY_LEVELS[lvl][1] * p for lvl, p in c.probabilities.items()))
            reason = (f"{RELEVANCE_LEVELS[_top_level(rel)][1].capitalize()}, "
                      f"{INTEREST_LEVELS[_top_level(interest)][1]}")
            roundup = ans["is_roundup"].noul >= NEWS_EVENT_THRESHOLD
            if roundup:
                reason += "; a round-up of several unrelated stories, not one postable story"
            elif news < NEWS_EVENT_THRESHOLD:
                reason += f"; reads as opinion/feature/service content rather than a new event (p={news:.2f})"
            unsure = [n for n, x in (("relevance", rel), ("interest", interest)) if x.confidence < LOW_CONFIDENCE]
            if unsure:
                reason += f"; low Jev confidence on {' and '.join(unsure)}, worth a human look"
            rows[k] = {
                "id": k, "relevance": _pct(rel), "audience_interest": _pct(interest),
                "credibility": cred if cred is not None else 0, "duplicate_of": "",
                "verdict": "post" if news >= NEWS_EVENT_THRESHOLD and not roundup else "discard",
                "reason": reason + ".", "roundup": roundup,
            }
        return rows

    # --- duplicates -------------------------------------------------------------
    def find_duplicates(self, keys: dict[str, Article], history_ids: dict) -> tuple[list[tuple[str, str]], dict[str, str]]:
        """Return (same-event pairs within the run, {article key: history id it repeats})."""
        pairs = [(k1, k2) for k1, k2 in itertools.combinations(keys, 2)
                 if is_candidate_pair(keys[k1].title, keys[k2].title, keys[k1].category == keys[k2].category)]
        pairs += [(k, h) for k in keys for h, row in history_ids.items()
                  if is_candidate_pair(keys[k].title, row["title"], keys[k].category == row["category"])]

        def describe(x: str) -> dict:
            if x in keys:
                a = keys[x]
                return {"headline": a.title, "summary": a.description[:200] or "(none)", "publisher": a.source_name}
            r = history_ids[x]
            return {"headline": r["title"], "publisher": r["source_name"]}

        def one(pair):
            try:
                resp = self._ask({"article_a": describe(pair[0]), "article_b": describe(pair[1])},
                                 {"same_event": PAIR_QUESTION})
            except LLMError as exc:
                if not exc.fatal:
                    log.warning("  Jev duplicate check failed for %s/%s: %s", *pair, exc)
                return pair, 0.0
            return pair, resp.answers["same_event"].probabilities.get(2, 0.0)

        links: list[tuple[str, str]] = []
        covered: dict[str, tuple[str, float]] = {}
        for (x, y), p_same in self._map(one, pairs):
            if p_same < SAME_EVENT_PROBABILITY:
                continue
            if y in history_ids:
                if p_same > covered.get(x, ("", 0.0))[1]:
                    covered[x] = (y, p_same)
            else:
                links.append((x, y))
        if self._fatal:
            raise self._fatal
        log.info("  Jev duplicate check: %d candidate pairs, %d same-event links, %d previously covered",
                 len(pairs), len(links), len(covered))
        return links, {k: h for k, (h, _) in covered.items()}

    # --- content checks (used by the content creator and image designer) ---------
    def review_posts(self, posts: dict[str, dict], sources: dict[str, dict]) -> dict[str, str]:
        """Flag drafts that state facts missing from their source or read as sensational.
        Returns {key: review note} for flagged posts only."""

        def one(k: str):
            try:
                resp = self._ask({"source": sources[k], "post": posts[k]}, REVIEW_QUESTIONS)
            except LLMError as exc:
                if not exc.fatal:
                    log.warning("  Jev review failed for %s: %s", k, exc)
                return k, None
            return k, resp.answers

        notes: dict[str, str] = {}
        for k, ans in self._map(one, [k for k in posts if k in sources]):
            if self._fatal:
                raise self._fatal
            if ans is None:
                continue
            flags = [f"{label} (p={ans[q].noul:.2f})" for q, (threshold, label) in REVIEW_RULES.items()
                     if ans[q].noul >= threshold]
            if flags:
                notes[k] = "Jev: " + "; ".join(flags)
        return notes

    def choose_photo(self, post: dict, candidates: dict[str, dict]) -> tuple[str | None, float]:
        """Return (candidate key, probability it clearly shows the story's subject) for the best photo."""
        if not candidates:
            return None, 0.0
        questions = {f"fit_{k}": Score(instructions=f"How well does the photo described in `photos.{k}` "
                                                    f"illustrate the news story in `post`?",
                                       criteria=PHOTO_FIT_LEVELS) for k in candidates}
        resp = self._ask({"post": post, "photos": candidates}, questions)
        fits = {k: resp.answers[f"fit_{k}"].probabilities.get(2, 0.0) for k in candidates}
        best = max(fits, key=fits.get)
        return best, fits[best]


REVIEW_QUESTIONS = {
    "unsupported_fact": Noul(
        instructions="Does `post` state any fact, number, name, quote or cause that does not appear in `source`?"),
    "sensational": Noul(
        instructions="Is `post` written in a sensational, exaggerated or opinionated tone?"),
    "misdescribes": Noul(
        instructions="Does `post` describe any person or group differently from how `source` describes them, "
                     "for example by dropping or changing qualifiers?"),
}
# (threshold, note) per question. Jev reads literally, so ordinary shortening ("Major automakers" ->
# "Automakers") scores ~0.5-0.7 on "misdescribes"; only clear changes (~0.8) go to review.
REVIEW_RULES = {
    "unsupported_fact": (0.5, "may state facts not in the source"),
    "sensational": (0.6, "tone may be sensational or opinionated"),
    "misdescribes": (0.75, "may describe a person or group differently from the source"),
}
PHOTO_FIT_LEVELS = [
    "The photo is unrelated to the story",
    "The photo shows the general topic, but not the story's main subject",
    "The photo clearly shows the story's main subject: the company, product, place, object or public figure",
]
