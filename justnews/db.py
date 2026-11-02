"""SQLite persistence. This is the hand-off point between the agents and the
history used to detect duplicates across executions."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from .models import Article, Evaluation, normalize_title

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    started_at    TEXT NOT NULL,
    discovery     TEXT,          -- JSON stats from the discovery agent
    evaluation    TEXT           -- JSON stats from the evaluator agent
);
CREATE TABLE IF NOT EXISTS articles (
    id            TEXT PRIMARY KEY,   -- sha1 of canonical URL
    title         TEXT NOT NULL,
    title_norm    TEXT NOT NULL,
    description   TEXT,
    url           TEXT NOT NULL,
    source_name   TEXT NOT NULL,
    feed          TEXT,
    category      TEXT NOT NULL,
    published_at  TEXT,
    first_run_id  TEXT NOT NULL,
    last_run_id   TEXT NOT NULL,
    first_seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_articles_last_run ON articles(last_run_id);
CREATE INDEX IF NOT EXISTS idx_articles_title_norm ON articles(title_norm);
CREATE TABLE IF NOT EXISTS evaluations (
    run_id            TEXT NOT NULL,
    article_id        TEXT NOT NULL REFERENCES articles(id),
    relevance         INTEGER,
    credibility       INTEGER,
    freshness         INTEGER,
    audience_interest INTEGER,
    overall           INTEGER,
    decision          TEXT NOT NULL,
    reason            TEXT,
    duplicate_of      TEXT,
    llm_scored        INTEGER NOT NULL DEFAULT 1,
    evaluated_at      TEXT NOT NULL,
    PRIMARY KEY (run_id, article_id)
);
CREATE TABLE IF NOT EXISTS posts (
    article_id    TEXT PRIMARY KEY REFERENCES articles(id),
    run_id        TEXT NOT NULL,
    headline      TEXT NOT NULL,
    caption       TEXT NOT NULL,
    hashtags      TEXT NOT NULL,      -- JSON list, each starting with #
    attribution   TEXT NOT NULL,
    image_text    TEXT NOT NULL,
    visual        TEXT NOT NULL,      -- "photo" or "graphic" (content creator's suggestion)
    image_query   TEXT,
    status        TEXT NOT NULL,      -- "draft" or "needs_review"; nothing is ever published
    review_note   TEXT,
    model         TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS images (
    article_id    TEXT PRIMARY KEY REFERENCES posts(article_id),
    run_id        TEXT NOT NULL,
    kind          TEXT NOT NULL,      -- "photo" (licensed) or "graphic" (generated)
    path          TEXT NOT NULL,
    source_page   TEXT,               -- licensed photo's page (e.g. Wikimedia Commons file page)
    license       TEXT,
    license_url   TEXT,
    creator       TEXT,
    credit        TEXT,               -- attribution line, also drawn on the image
    note          TEXT,               -- why this kind was chosen
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_usage (
    run_id        TEXT NOT NULL,
    agent         TEXT NOT NULL,
    model         TEXT NOT NULL,
    calls         INTEGER NOT NULL,
    failed_calls  INTEGER NOT NULL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd      REAL NOT NULL
);
"""


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(runs)")}
        for column in ("content", "images"):  # added with agents 3 and 4
            if column not in existing:
                self.conn.execute(f"ALTER TABLE runs ADD COLUMN {column} TEXT")
        self.conn.commit()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self.conn:
            yield self.conn

    # --- runs -------------------------------------------------------------
    def create_run(self, run_id: str) -> None:
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO runs(run_id, started_at) VALUES (?, ?)", (run_id, _now()))

    def save_run_stats(self, run_id: str, stage: str, stats: dict) -> None:
        assert stage in ("discovery", "evaluation", "content", "images")
        with self.tx() as c:
            c.execute(f"UPDATE runs SET {stage} = ? WHERE run_id = ?", (json.dumps(stats), run_id))

    def get_run(self, run_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if not row:
            return None
        return {
            "run_id": row["run_id"],
            "started_at": row["started_at"],
            "discovery": json.loads(row["discovery"]) if row["discovery"] else None,
            "evaluation": json.loads(row["evaluation"]) if row["evaluation"] else None,
            "content": json.loads(row["content"]) if row["content"] else None,
            "images": json.loads(row["images"]) if row["images"] else None,
        }

    def latest_run_id(self) -> str | None:
        row = self.conn.execute("SELECT run_id FROM runs ORDER BY started_at DESC, run_id DESC LIMIT 1").fetchone()
        return row["run_id"] if row else None

    # --- articles ---------------------------------------------------------
    def save_discovered(self, run_id: str, articles: list[Article]) -> None:
        now = _now()
        with self.tx() as c:
            for a in articles:
                c.execute(
                    """INSERT INTO articles(id, title, title_norm, description, url, source_name, feed, category,
                                            published_at, first_run_id, last_run_id, first_seen_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET last_run_id = excluded.last_run_id""",
                    (a.id, a.title, normalize_title(a.title), a.description, a.url, a.source_name, a.feed,
                     a.category, _iso(a.published_at), run_id, run_id, now),
                )

    def articles_for_run(self, run_id: str) -> list[Article]:
        rows = self.conn.execute("SELECT * FROM articles WHERE last_run_id = ? ORDER BY category, published_at DESC",
                                 (run_id,)).fetchall()
        return [
            Article(id=r["id"], title=r["title"], url=r["url"], source_name=r["source_name"], category=r["category"],
                    published_at=datetime.fromisoformat(r["published_at"]) if r["published_at"] else None,
                    description=r["description"] or "", feed=r["feed"] or "")
            for r in rows
        ]

    # --- history for duplicate detection ------------------------------------
    def evaluated_ids(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT DISTINCT article_id FROM evaluations")}

    def prior_evaluation(self, article_id: str, run_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT run_id, decision, evaluated_at FROM evaluations WHERE article_id = ? AND run_id != ? "
            "ORDER BY evaluated_at DESC LIMIT 1", (article_id, run_id)).fetchone()

    def recent_history(self, run_id: str, days: int) -> list[sqlite3.Row]:
        """Stories recommended in earlier runs within the last `days`, newest first. Only these count
        as "previously covered"; a story rejected earlier may deserve a better article now."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        return self.conn.execute(
            """SELECT a.id, a.title, a.source_name, a.category, e.decision, e.evaluated_at
               FROM evaluations e JOIN articles a ON a.id = e.article_id
               WHERE e.run_id != ? AND e.evaluated_at >= ? AND e.decision = 'Recommended'
                 AND e.duplicate_of IS NULL
               ORDER BY e.evaluated_at DESC""",
            (run_id, since)).fetchall()

    # --- evaluations ------------------------------------------------------
    def save_evaluations(self, run_id: str, evaluations: list[Evaluation]) -> None:
        now = _now()
        with self.tx() as c:
            for e in evaluations:
                c.execute(
                    """INSERT OR REPLACE INTO evaluations(run_id, article_id, relevance, credibility, freshness,
                           audience_interest, overall, decision, reason, duplicate_of, llm_scored, evaluated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (run_id, e.article_id, e.relevance, e.credibility, e.freshness, e.audience_interest, e.overall,
                     e.decision, e.reason, e.duplicate_of, int(e.llm_scored), now),
                )

    def evaluations_for_run(self, run_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT e.*, a.title, a.url, a.source_name, a.category, a.published_at
               FROM evaluations e JOIN articles a ON a.id = e.article_id
               WHERE e.run_id = ? ORDER BY e.overall DESC""", (run_id,)).fetchall()

    def record_llm_usage(self, run_id: str, agent: str, usage: dict) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM llm_usage WHERE run_id = ? AND agent = ?", (run_id, agent))
            c.execute(
                "INSERT INTO llm_usage VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, agent, usage["model"], usage["calls"], usage["failed_calls"], usage["input_tokens"],
                 usage["output_tokens"], usage["cost_usd"]),
            )

    def llm_usage_for_run(self, run_id: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM llm_usage WHERE run_id = ?", (run_id,)).fetchall()

    # --- posts (content creator) ----------------------------------------------
    def recommended_without_post(self, run_id: str, limit: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT a.*, e.overall FROM evaluations e JOIN articles a ON a.id = e.article_id
               LEFT JOIN posts p ON p.article_id = a.id
               WHERE e.run_id = ? AND e.decision = 'Recommended' AND p.article_id IS NULL
               ORDER BY e.overall DESC LIMIT ?""", (run_id, limit)).fetchall()

    def save_post(self, run_id: str, article_id: str, post: dict, model: str) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT OR REPLACE INTO posts(article_id, run_id, headline, caption, hashtags, attribution,
                       image_text, visual, image_query, status, review_note, model, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (article_id, run_id, post["headline"], post["caption"], json.dumps(post["hashtags"]),
                 post["attribution"], post["image_text"], post["visual"], post.get("image_query"),
                 post["status"], post.get("review_note"), model, _now()))

    def posts_for_run(self, run_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT p.*, a.title, a.url, a.source_name, a.category, a.published_at, e.overall,
                      i.kind AS image_kind, i.path AS image_path, i.credit AS image_credit,
                      i.license AS image_license, i.source_page AS image_source, i.note AS image_note
               FROM posts p JOIN articles a ON a.id = p.article_id
               LEFT JOIN evaluations e ON e.article_id = p.article_id AND e.run_id = p.run_id
               LEFT JOIN images i ON i.article_id = p.article_id
               WHERE p.run_id = ? ORDER BY e.overall DESC""", (run_id,)).fetchall()

    # --- images (image designer) ----------------------------------------------
    def save_image(self, run_id: str, article_id: str, image: dict) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT OR REPLACE INTO images(article_id, run_id, kind, path, source_page, license, license_url,
                       creator, credit, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (article_id, run_id, image["kind"], image["path"], image.get("source_page"), image.get("license"),
                 image.get("license_url"), image.get("creator"), image.get("credit"), image.get("note"), _now()))
