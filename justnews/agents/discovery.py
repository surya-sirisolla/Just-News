"""Agent 1 - News Discovery.

Fetches every configured source in parallel, normalises entries into Article
objects, drops obviously irrelevant/stale/duplicate items, balances the result
per category and stores it in SQLite for the evaluator.
"""

from __future__ import annotations

import calendar
import html
import logging
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import feedparser

from ..config import Settings
from ..db import Database
from ..http import FetchError, fetch, make_session
from ..models import Article, canonical_url, normalize_title
from ..regions import detect_region
from ..sources import AI_KEYWORDS, IRRELEVANT_PATTERNS, SOURCES, Source

log = logging.getLogger("justnews.discovery")

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_FEED_LINK = re.compile(r"<link[^>]+type=[\"']application/(?:rss|atom)\+xml[\"'][^>]*>", re.I)
_HREF = re.compile(r"href=[\"']([^\"']+)[\"']", re.I)
_IRRELEVANT = [re.compile(p, re.I) for p in IRRELEVANT_PATTERNS]
_AI_RE = re.compile(r"\b(" + "|".join(re.escape(k) for k in AI_KEYWORDS) + r")\b", re.I)


def clean_text(raw: str, limit: int = 500) -> str:
    text = _WS.sub(" ", html.unescape(_TAG.sub(" ", raw or ""))).strip()
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


@dataclass
class SourceResult:
    source: str
    ok: bool
    entries: int = 0
    error: str = ""


@dataclass
class DiscoveryResult:
    run_id: str
    articles: list[Article]
    sources: list[SourceResult]
    raw_entries: int
    filtered: Counter = field(default_factory=Counter)

    def stats(self) -> dict:
        return {
            "sources_total": len(self.sources),
            "sources_ok": sum(s.ok for s in self.sources),
            "sources_failed": [{"source": s.source, "error": s.error} for s in self.sources if not s.ok],
            "raw_entries": self.raw_entries,
            "filtered_out": dict(self.filtered),
            "discovered": len(self.articles),
            "by_category": dict(Counter(a.category for a in self.articles)),
        }


class NewsDiscoveryAgent:
    name = "news-discovery"

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.session = make_session()
        self.include_seen = False

    # --- public entry point -------------------------------------------------
    def run(self, run_id: str, sources: list[Source] | None = None, include_seen: bool = False) -> DiscoveryResult:
        """All configured sources; or, for on-demand research, the given ones, including stories
        already seen in earlier runs (`include_seen`)."""
        self.include_seen = include_seen
        sources = sources if sources is not None else [s for s in SOURCES if s.category in self.settings.categories]
        log.info("Run %s: fetching %d sources across %s", run_id, len(sources), ", ".join(self.settings.categories))

        with ThreadPoolExecutor(max_workers=8) as pool:
            fetched = list(pool.map(self._fetch_source, sources))

        source_results = [r for r, _ in fetched]
        candidates = [a for _, arts in fetched for a in arts]
        filtered: Counter = Counter()
        kept = self._filter(candidates, filtered)
        selected = self._balance(kept, filtered)

        result = DiscoveryResult(run_id, selected, source_results, len(candidates), filtered)
        self.db.create_run(run_id)
        self.db.save_discovered(run_id, selected)
        self.db.save_run_stats(run_id, "discovery", result.stats())
        log.info("Run %s: %d raw entries -> %d discovered (filtered: %s)",
                 run_id, len(candidates), len(selected), dict(filtered) or "none")
        return result

    # --- fetching -----------------------------------------------------------
    def _fetch_source(self, src: Source) -> tuple[SourceResult, list[Article]]:
        try:
            feed_url = self._discover_feed_url(src) if src.kind == "site" else src.url
            resp = fetch(self.session, feed_url, self.settings.request_timeout, self.settings.request_retries)
            parsed = feedparser.parse(resp.content)
            if not parsed.entries:
                reason = f"no entries ({parsed.bozo_exception})" if parsed.bozo else "no entries"
                raise FetchError(reason)
            articles = [a for a in (self._to_article(src, e) for e in parsed.entries) if a]
            log.info("  ok   %-36s %3d entries", src.label, len(parsed.entries))
            return SourceResult(src.label, True, len(parsed.entries)), articles
        except FetchError as exc:
            log.warning("  FAIL %-36s %s", src.label, exc)
            return SourceResult(src.label, False, error=str(exc)), []
        except Exception as exc:  # a broken source must never stop the run
            log.exception("  FAIL %-36s unexpected error", src.label)
            return SourceResult(src.label, False, error=f"{type(exc).__name__}: {exc}"), []

    def _discover_feed_url(self, src: Source) -> str:
        """Find a website's RSS/Atom feed from its homepage <link rel="alternate"> tags."""
        page = fetch(self.session, src.url, self.settings.request_timeout, self.settings.request_retries)
        for tag in _FEED_LINK.findall(page.text[:300_000]):
            if m := _HREF.search(tag):
                return urljoin(page.url, html.unescape(m.group(1)))
        raise FetchError("no RSS/Atom feed advertised on homepage")

    def _to_article(self, src: Source, entry) -> Article | None:
        title = clean_text(entry.get("title", ""), 300)
        link = entry.get("link", "")
        if not title or not link:
            return None
        published = None
        if ts := (entry.get("published_parsed") or entry.get("updated_parsed")):
            published = datetime.fromtimestamp(calendar.timegm(ts), tz=timezone.utc)

        publisher = src.publisher
        description = clean_text(entry.get("summary", ""))
        if src.kind == "google_news":
            # Google News titles look like "Headline - Publisher"; the publisher is also in <source>.
            gn_source = (entry.get("source") or {}).get("title", "")
            if gn_source:
                publisher = gn_source
                title = title.removesuffix(f" - {gn_source}").strip()
            description = self._google_description(entry.get("summary", ""), publisher)
        elif description.lower().startswith(title.lower()):
            description = description[len(title):].strip(" -–:")

        return Article(title=title, url=link, source_name=publisher, category=src.category,
                       published_at=published, description=description, feed=src.label,
                       region=detect_region(title, description, src.region))

    @staticmethod
    def _google_description(summary_html: str, publisher: str) -> str:
        """Google News items have no summary; topic feeds list other outlets covering the story."""
        outlets = [clean_text(o, 60) for o in re.findall(r"<font[^>]*>(.*?)</font>", summary_html or "", re.S)]
        others = [o for o in dict.fromkeys(outlets) if o and o != publisher]
        return f"Also covered by: {', '.join(others[:6])}" if others else ""

    # --- filtering ----------------------------------------------------------
    def _filter(self, candidates: list[Article], filtered: Counter) -> list[Article]:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=self.settings.max_age_hours)
        seen_urls: set[str] = set()
        seen_titles: set[tuple[str, str]] = set()
        already_evaluated = self.db.evaluated_ids()  # so slots go to stories not seen before
        kept: list[Article] = []

        # Direct publisher feeds win over Google News copies of the same link/headline, and the
        # specific AI category wins over general Technology when a story appears in both feeds.
        for a in sorted(candidates, key=lambda a: (a.feed.startswith("Google News"), a.category != "AI")):
            if a.published_at and a.published_at > now + timedelta(hours=1):
                a.published_at = now  # clock-skewed feeds
            reason = None
            if a.published_at is None:
                reason = "missing date"
            elif a.published_at < cutoff:
                reason = "stale"
            elif any(p.search(a.title) for p in _IRRELEVANT):
                reason = "irrelevant (deals/guides/promo)"
            elif a.category == "AI" and not _AI_RE.search(f"{a.title} {a.description}"):
                reason = "off-topic for category"
            elif canonical_url(a.url) in seen_urls or (a.category, normalize_title(a.title)) in seen_titles:
                reason = "duplicate in this run"
            elif a.id in already_evaluated and not self.include_seen:
                reason = "already evaluated in an earlier run"
            if reason:
                filtered[reason] += 1
                continue
            seen_urls.add(canonical_url(a.url))
            seen_titles.add((a.category, normalize_title(a.title)))
            kept.append(a)
        return kept

    def _balance(self, articles: list[Article], filtered: Counter) -> list[Article]:
        """Keep the freshest `max_per_category` per category, round-robin across feeds for diversity."""
        by_cat: dict[str, dict[str, list[Article]]] = defaultdict(lambda: defaultdict(list))
        for a in sorted(articles, key=lambda a: a.published_at, reverse=True):
            by_cat[a.category][a.feed].append(a)

        selected: list[Article] = []
        for cat in self.settings.categories:
            queues = list(by_cat.get(cat, {}).values())
            picked: list[Article] = []
            while queues and len(picked) < self.settings.max_per_category:
                for q in list(queues):
                    if len(picked) >= self.settings.max_per_category:
                        break
                    picked.append(q.pop(0))
                    if not q:
                        queues.remove(q)
            overflow = sum(len(q) for q in queues)
            if overflow:
                filtered["over category limit"] += overflow
            selected.extend(picked)
        return selected
