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
    region        TEXT,               -- South India / India / International
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
    photo_path    TEXT,               -- the licensed photo itself, for carousel slides
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approval_requests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    items         TEXT NOT NULL,      -- JSON {"1": article_id, ...}: the numbers shown to the owner
    status        TEXT NOT NULL,      -- sent / answered / expired / superseded
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    reply         TEXT,
    answered_at   TEXT
);
CREATE TABLE IF NOT EXISTS wa_inbound (
    message_id    TEXT PRIMARY KEY,   -- a message can arrive twice after a reconnect; handle it once
    sender        TEXT NOT NULL,
    body          TEXT,
    received_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wa_outbox (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    to_number     TEXT NOT NULL,
    body          TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    sent_at       TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    error         TEXT,              -- last failure; given up after the bridge's maximum attempts
    media_path    TEXT               -- image to send, with `body` as its caption
);
CREATE TABLE IF NOT EXISTS publications (
    article_id    TEXT NOT NULL REFERENCES posts(article_id),
    platform      TEXT NOT NULL,      -- "instagram" or "x"
    status        TEXT NOT NULL,      -- queued / published / failed (retried) / blocked (account problem: not retried)
    media_id      TEXT,
    permalink     TEXT,
    error         TEXT,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (article_id, platform)
);
CREATE TABLE IF NOT EXISTS post_metrics (
    article_id    TEXT NOT NULL,
    platform      TEXT NOT NULL,
    checks        INTEGER NOT NULL,   -- how many of the strategist's checkpoints have been read
    likes         INTEGER,
    comments      INTEGER,
    shares        INTEGER,            -- Instagram shares / X reposts + quotes
    saves         INTEGER,            -- Instagram saves / X bookmarks
    views         INTEGER,            -- Instagram views (needs insights) / X impressions
    reach         INTEGER,
    fetched_at    TEXT NOT NULL,
    PRIMARY KEY (article_id, platform)
);
CREATE TABLE IF NOT EXISTS benchmarks (  -- recent posts of other news accounts
    platform      TEXT NOT NULL,
    post_id       TEXT NOT NULL,
    account       TEXT NOT NULL,
    followers     INTEGER,
    text          TEXT,
    media_type    TEXT,
    likes         INTEGER,
    comments      INTEGER,
    shares        INTEGER,
    views         INTEGER,
    posted_at     TEXT,
    fetched_at    TEXT NOT NULL,
    PRIMARY KEY (platform, post_id)
);
CREATE TABLE IF NOT EXISTS playbooks (   -- the strategist's current rules for each platform
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    playbook      TEXT NOT NULL,      -- JSON
    evidence      TEXT,               -- JSON: what it was based on
    model         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_messages (  -- the strategist <-> content creator discussion
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    article_id    TEXT,
    sender        TEXT NOT NULL,
    recipient     TEXT NOT NULL,
    body          TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_state (     -- small key/value notes, e.g. when a read was last tried
    key           TEXT PRIMARY KEY,
    value         TEXT,
    updated_at    TEXT NOT NULL
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
        # The WhatsApp bridge reads and writes from its event, inbox and outbox threads.
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(runs)")}
        for column in ("content", "images", "strategy"):  # added with agents 3, 4 and 6
            if column not in existing:
                self.conn.execute(f"ALTER TABLE runs ADD COLUMN {column} TEXT")
        if "approval" not in {r["name"] for r in self.conn.execute("PRAGMA table_info(posts)")}:
            # NULL (not asked) / requested / approved / rejected
            self.conn.execute("ALTER TABLE posts ADD COLUMN approval TEXT")
        if "region" not in {r["name"] for r in self.conn.execute("PRAGMA table_info(articles)")}:
            self.conn.execute("ALTER TABLE articles ADD COLUMN region TEXT")
        post_columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(posts)")}
        for column in ("ig_caption", "x_post", "x_hashtag"):  # platform versions
            if column not in post_columns:
                self.conn.execute(f"ALTER TABLE posts ADD COLUMN {column} TEXT")
        if "media_path" not in {r["name"] for r in self.conn.execute("PRAGMA table_info(wa_outbox)")}:
            self.conn.execute("ALTER TABLE wa_outbox ADD COLUMN media_path TEXT")
        pub_columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(publications)")}
        if "attempts" not in pub_columns:
            self.conn.execute("ALTER TABLE publications ADD COLUMN attempts INTEGER NOT NULL DEFAULT 1")
        for column in ("scheduled_at", "batch"):  # the X queue: when to post, and which approval it belongs to
            if column not in pub_columns:
                self.conn.execute(f"ALTER TABLE publications ADD COLUMN {column} TEXT")
        if "photo_path" not in {r["name"] for r in self.conn.execute("PRAGMA table_info(images)")}:
            self.conn.execute("ALTER TABLE images ADD COLUMN photo_path TEXT")
        if "pending" not in {r["name"] for r in self.conn.execute("PRAGMA table_info(approval_requests)")}:
            # JSON list of story numbers awaiting the owner's "yes" (approvals given in free text)
            self.conn.execute("ALTER TABLE approval_requests ADD COLUMN pending TEXT")
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
        assert stage in ("discovery", "evaluation", "content", "images", "strategy")
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
            "strategy": json.loads(row["strategy"]) if row["strategy"] else None,
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
                                            published_at, first_run_id, last_run_id, first_seen_at, region)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET last_run_id = excluded.last_run_id,
                                                     region = COALESCE(articles.region, excluded.region)""",
                    (a.id, a.title, normalize_title(a.title), a.description, a.url, a.source_name, a.feed,
                     a.category, _iso(a.published_at), run_id, run_id, now, a.region or None),
                )

    def articles_for_run(self, run_id: str) -> list[Article]:
        rows = self.conn.execute("SELECT * FROM articles WHERE last_run_id = ? ORDER BY category, published_at DESC",
                                 (run_id,)).fetchall()
        return [
            Article(id=r["id"], title=r["title"], url=r["url"], source_name=r["source_name"], category=r["category"],
                    published_at=datetime.fromisoformat(r["published_at"]) if r["published_at"] else None,
                    description=r["description"] or "", feed=r["feed"] or "", region=r["region"] or "")
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
                       image_text, visual, image_query, status, review_note, model, created_at, ig_caption, x_post,
                       x_hashtag)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (article_id, run_id, post["headline"], post["caption"], json.dumps(post["hashtags"]),
                 post["attribution"], post["image_text"], post["visual"], post.get("image_query"),
                 post["status"], post.get("review_note"), model, _now(), post.get("ig_caption"), post.get("x_post"),
                 post.get("x_hashtag")))

    def posts_for_run(self, run_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT p.*, a.title, a.url, a.source_name, a.category, a.published_at, e.overall,
                      i.kind AS image_kind, i.path AS image_path, i.credit AS image_credit, i.photo_path,
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
                       creator, credit, note, created_at, photo_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (article_id, run_id, image["kind"], image["path"], image.get("source_page"), image.get("license"),
                 image.get("license_url"), image.get("creator"), image.get("credit"), image.get("note"), _now(),
                 image.get("photo_path")))

    # --- WhatsApp approval ------------------------------------------------------
    def create_approval_request(self, run_id: str, items: dict[str, str], expires_at: str, status: str) -> int:
        with self.tx() as c:
            c.execute("UPDATE approval_requests SET status = 'superseded' WHERE status IN ('awaiting_window', 'sent')")
            cur = c.execute("INSERT INTO approval_requests(run_id, items, status, created_at, expires_at) "
                            "VALUES (?, ?, ?, ?, ?)", (run_id, json.dumps(items), status, _now(), expires_at))
            c.executemany("UPDATE posts SET approval = 'requested' WHERE article_id = ?", [(a,) for a in items.values()])
            return cur.lastrowid

    def open_approval_request(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM approval_requests WHERE status IN ('awaiting_window', 'sent') "
                                 "ORDER BY id DESC LIMIT 1").fetchone()

    def approval_request(self, request_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM approval_requests WHERE id = ?", (request_id,)).fetchone()

    def latest_approval_request(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM approval_requests ORDER BY id DESC LIMIT 1").fetchone()

    def set_approval_status(self, request_id: int, status: str, reply: str | None = None) -> None:
        with self.tx() as c:
            if reply is None:
                c.execute("UPDATE approval_requests SET status = ? WHERE id = ?", (status, request_id))
            else:
                c.execute("UPDATE approval_requests SET status = ?, reply = ?, answered_at = ? WHERE id = ?",
                          (status, reply, _now(), request_id))

    def set_pending_approval(self, request_id: int, numbers: list[int] | None) -> None:
        with self.tx() as c:
            c.execute("UPDATE approval_requests SET pending = ? WHERE id = ?",
                      (json.dumps(numbers) if numbers is not None else None, request_id))

    def set_post_approval(self, article_ids: list[str], approval: str) -> None:
        with self.tx() as c:
            c.executemany("UPDATE posts SET approval = ? WHERE article_id = ?", [(approval, a) for a in article_ids])

    def reject(self, run_id: str, article_id: str, reason: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE evaluations SET decision = 'Rejected', reason = ? WHERE run_id = ? AND article_id = ?",
                      (reason, run_id, article_id))

    def adopt_posts(self, run_id: str, article_ids: list[str]) -> int:
        """Move unpublished posts written in earlier runs into `run_id` (on-demand research), so a
        story offered before can be offered again without writing it twice."""
        with self.tx() as c:
            return c.executemany(
                """UPDATE posts SET run_id = ?, approval = NULL WHERE article_id = ? AND article_id NOT IN
                   (SELECT article_id FROM publications WHERE status IN ('published', 'queued'))""",
                [(run_id, a) for a in article_ids]).rowcount

    def posts_awaiting_approval(self, run_id: str, limit: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT p.*, a.source_name, a.url, a.category, e.overall FROM posts p
               JOIN articles a ON a.id = p.article_id
               LEFT JOIN evaluations e ON e.article_id = p.article_id AND e.run_id = p.run_id
               WHERE p.run_id = ? AND p.approval IS NULL ORDER BY e.overall DESC LIMIT ?""",
            (run_id, limit)).fetchall()

    def record_inbound(self, message_id: str, sender: str, body: str) -> bool:
        """Store an incoming message; False if it was already handled (redelivered after a reconnect)."""
        with self.tx() as c:
            cur = c.execute("INSERT OR IGNORE INTO wa_inbound VALUES (?, ?, ?, ?)", (message_id, sender, body, _now()))
            return cur.rowcount == 1

    # --- WhatsApp outbox (written by any command, sent by the bridge) -------------
    def enqueue_message(self, to_number: str, body: str, media_path: str | None = None) -> int:
        with self.tx() as c:
            return c.execute("INSERT INTO wa_outbox(to_number, body, created_at, media_path) VALUES (?, ?, ?, ?)",
                             (to_number, body, _now(), media_path)).lastrowid

    def pending_messages(self, max_attempts: int = 5) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM wa_outbox WHERE sent_at IS NULL AND attempts < ? ORDER BY id",
                                 (max_attempts,)).fetchall()

    def mark_message_sent(self, message_id: int) -> None:
        with self.tx() as c:
            c.execute("UPDATE wa_outbox SET sent_at = ?, error = NULL WHERE id = ?", (_now(), message_id))

    def block_message(self, message_id: int, reason: str) -> None:
        """Never send this message (e.g. addressed to someone other than the owner)."""
        with self.tx() as c:
            c.execute("UPDATE wa_outbox SET attempts = 999, error = ? WHERE id = ?", (reason, message_id))

    def mark_message_failed(self, message_id: int, error: str, max_attempts: int) -> None:
        with self.tx() as c:
            c.execute("UPDATE wa_outbox SET attempts = attempts + 1, error = ? WHERE id = ?", (error, message_id))

    # --- publications (publisher) ------------------------------------------------
    def publication(self, article_id: str, platform: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM publications WHERE article_id = ? AND platform = ?",
                                 (article_id, platform)).fetchone()

    def save_publication(self, article_id: str, platform: str, status: str, media_id: str | None = None,
                         permalink: str | None = None, error: str | None = None) -> None:
        previous = self.publication(article_id, platform)
        attempts = (previous["attempts"] + 1) if previous is not None and status == "failed" else 1
        # A queued post keeps its batch and slot, so the owner's summary lists it in order.
        batch, slot = (previous["batch"], previous["scheduled_at"]) if previous is not None else (None, None)
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO publications(article_id, platform, status, media_id, permalink, error, "
                      "updated_at, attempts, batch, scheduled_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (article_id, platform, status, media_id, permalink, error, _now(), attempts, batch, slot))

    # --- the X queue: posts spaced out over time ----------------------------------
    def queue_publication(self, article_id: str, platform: str, scheduled_at: str, batch: str) -> None:
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO publications(article_id, platform, status, updated_at, attempts, "
                      "scheduled_at, batch) VALUES (?, ?, 'queued', ?, 1, ?, ?)",
                      (article_id, platform, _now(), scheduled_at, batch))

    def queued_publications(self, platform: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT pub.article_id, pub.scheduled_at, pub.batch, p.run_id FROM publications pub
               JOIN posts p ON p.article_id = pub.article_id
               WHERE pub.platform = ? AND pub.status = 'queued' ORDER BY pub.scheduled_at""", (platform,)).fetchall()

    def batch_publications(self, platform: str, batch: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM publications WHERE platform = ? AND batch = ? "
                                 "ORDER BY scheduled_at, rowid", (platform, batch)).fetchall()

    def published_between(self, start: str, end: str) -> list[sqlite3.Row]:
        """Stories published (on any platform) between two UTC times, once each, in posting order."""
        return self.conn.execute(
            """SELECT p.*, a.title, a.url, a.source_name, a.category, a.published_at, a.region,
                      i.kind AS image_kind, i.path AS image_path, i.credit AS image_credit, i.photo_path,
                      MIN(pub.updated_at) AS posted_at, MIN(pub.rowid) AS posted_seq,
                      (SELECT MAX(e.overall) FROM evaluations e WHERE e.article_id = p.article_id) AS overall
               FROM publications pub JOIN posts p ON p.article_id = pub.article_id
               JOIN articles a ON a.id = p.article_id
               LEFT JOIN images i ON i.article_id = p.article_id
               WHERE pub.status = 'published' AND pub.updated_at >= ? AND pub.updated_at < ?
               GROUP BY p.article_id ORDER BY posted_at, posted_seq""", (start, end)).fetchall()

    def post_headline(self, article_id: str) -> str | None:
        row = self.conn.execute("SELECT headline FROM posts WHERE article_id = ?", (article_id,)).fetchone()
        return row[0] if row else None

    def published_platforms(self, article_id: str) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT platform FROM publications WHERE article_id = ? AND status = 'published' ORDER BY platform",
            (article_id,))]

    def failed_publications(self, platform: str, max_attempts: int, within_hours: float) -> list[sqlite3.Row]:
        since = (datetime.now(timezone.utc) - timedelta(hours=within_hours)).isoformat(timespec="seconds")
        return self.conn.execute(
            """SELECT pub.article_id, p.run_id FROM publications pub JOIN posts p ON p.article_id = pub.article_id
               WHERE pub.platform = ? AND pub.status = 'failed' AND pub.attempts < ? AND p.approval = 'approved'
                 AND pub.updated_at >= ?""",
            (platform, max_attempts, since)).fetchall()

    def published_since(self, platform: str, hours: float) -> int:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
        return self.conn.execute("SELECT COUNT(*) FROM publications WHERE platform = ? AND status = 'published' "
                                 "AND updated_at >= ?", (platform, since)).fetchone()[0]

    # --- strategist: engagement, benchmarks, playbook, discussion --------------------
    def published_posts(self, platform: str, days: float) -> list[sqlite3.Row]:
        """Our live posts with their latest metrics, newest first."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        return self.conn.execute(
            """SELECT pub.article_id, pub.media_id, pub.updated_at AS published_at, p.headline, p.caption,
                      p.ig_caption, p.x_post, p.hashtags, a.category, i.kind AS image_kind,
                      m.checks, m.likes, m.comments, m.shares, m.saves, m.views, m.reach, m.fetched_at
               FROM publications pub JOIN posts p ON p.article_id = pub.article_id
               JOIN articles a ON a.id = pub.article_id
               LEFT JOIN images i ON i.article_id = pub.article_id
               LEFT JOIN post_metrics m ON m.article_id = pub.article_id AND m.platform = pub.platform
               WHERE pub.platform = ? AND pub.status = 'published' AND pub.media_id <> '' AND pub.updated_at >= ?
               ORDER BY pub.updated_at DESC""", (platform, since)).fetchall()

    def save_metrics(self, article_id: str, platform: str, checks: int, metrics: dict) -> None:
        with self.tx() as c:
            c.execute("""INSERT OR REPLACE INTO post_metrics(article_id, platform, checks, likes, comments, shares,
                             saves, views, reach, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                      (article_id, platform, checks, metrics.get("likes"), metrics.get("comments"),
                       metrics.get("shares"), metrics.get("saves"), metrics.get("views"), metrics.get("reach"), _now()))

    def save_benchmarks(self, platform: str, posts: list[dict]) -> None:
        now = _now()
        with self.tx() as c:
            for b in posts:
                c.execute("""INSERT OR REPLACE INTO benchmarks(platform, post_id, account, followers, text, media_type,
                                 likes, comments, shares, views, posted_at, fetched_at)
                             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                          (platform, b["post_id"], b["account"], b.get("followers"), b.get("text"), b.get("media_type"),
                           b.get("likes"), b.get("comments"), b.get("shares"), b.get("views"), b.get("posted_at"), now))

    def benchmarks(self, platform: str, days: float) -> list[sqlite3.Row]:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        return self.conn.execute("SELECT * FROM benchmarks WHERE platform = ? AND fetched_at >= ? ORDER BY fetched_at DESC",
                                 (platform, since)).fetchall()

    def last_benchmark_at(self, platform: str) -> str | None:
        return self.conn.execute("SELECT MAX(fetched_at) FROM benchmarks WHERE platform = ?", (platform,)).fetchone()[0]

    def last_metrics_at(self) -> str | None:
        return self.conn.execute("SELECT MAX(fetched_at) FROM post_metrics").fetchone()[0]

    def save_playbook(self, playbook: dict, evidence: dict, model: str) -> int:
        with self.tx() as c:
            return c.execute("INSERT INTO playbooks(created_at, playbook, evidence, model) VALUES (?, ?, ?, ?)",
                             (_now(), json.dumps(playbook, ensure_ascii=False), json.dumps(evidence), model)).lastrowid

    def latest_playbook(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM playbooks ORDER BY id DESC LIMIT 1").fetchone()

    def add_agent_message(self, run_id: str, sender: str, recipient: str, body: str, article_id: str | None = None) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO agent_messages(run_id, article_id, sender, recipient, body, created_at) "
                      "VALUES (?, ?, ?, ?, ?, ?)", (run_id, article_id, sender, recipient, body, _now()))

    def get_state(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM agent_state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_state(self, key: str, value: str) -> None:
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO agent_state(key, value, updated_at) VALUES (?, ?, ?)", (key, value, _now()))

    def agent_messages(self, run_id: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM agent_messages WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
