"""Agent 5 on X: posts owner-approved templates to X (API v2), signed as the account itself.

Like a newsroom feed (The Hindu's on X): one post per story, a plain news sentence, rarely a
hashtag, and posts spaced out (JUSTNEWS_X_SPACING_MINUTES) rather than in a burst. Approved stories
are queued; `post_due()` (run every 30 seconds by `serve`) posts each one when its time comes:
  1. only for a licensed photo: POST /2/media/upload (media_category=tweet_image) -> a media id
     (X is for text: a story with only a text template is posted as text)
  2. POST /2/tweets with the text (and the media id) -> the live post
A queued story not posted within JUSTNEWS_X_QUEUE_MAX_HOURS is dropped instead of posted stale.
The text carries no link: on X's pay-per-use pricing a post with a URL costs about 13x more, so the
source is named instead. X also rejects a post whose text matches a recent one, which is a second
guard (after the `publications` table) against posting the same story twice.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import logging
import re
import secrets
import time
import urllib.parse as up
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from ..config import Settings
from ..db import Database
from ..quiet import local_time
from .publisher import TIMEOUT, Publisher, PublishError, PublishResult, field_of, to_jpeg

log = logging.getLogger("justnews.publisher")

API = "https://api.x.com/2"
TEXT_MAX = 280
NO_CREDITS = "X needs Pay-Per-Use credits for posting (console.x.com → Billing)"

URL_RE = re.compile(r"https?://\S+|\bwww\.\S+", re.I)
# A bare domain (e.g. "Reuters.com") would be auto-linked, and a link makes the post cost more.
DOMAIN_RE = re.compile(r"\b([\w-]+)(?:\.[\w-]+)*\.(?:com|org|net|io|ai|co|in|uk|news|tv|gov|info)\b", re.I)
TCO_RE = re.compile(r"\s*https://t\.co/\w+\s*$")


class DuplicatePost(PublishError):
    """X refused the text because the same post is already on the account."""


def _q(s) -> str:
    return up.quote(str(s), safe="~")


def oauth1_header(method: str, url: str, consumer: tuple[str, str], token: tuple[str, str],
                  params: dict | None = None) -> str:
    """OAuth 1.0a HMAC-SHA1 header. Only query parameters are signed (bodies here are JSON or multipart)."""
    oauth = {"oauth_consumer_key": consumer[0], "oauth_nonce": secrets.token_hex(16),
             "oauth_signature_method": "HMAC-SHA1", "oauth_timestamp": str(int(time.time())),
             "oauth_token": token[0], "oauth_version": "1.0"}
    signed = {**oauth, **{k: str(v) for k, v in (params or {}).items()}}
    base = "&".join([method.upper(), _q(url), _q("&".join(f"{_q(k)}={_q(v)}" for k, v in sorted(signed.items())))])
    key = f"{_q(consumer[1])}&{_q(token[1])}".encode()
    oauth["oauth_signature"] = base64.b64encode(hmac.new(key, base.encode(), hashlib.sha1).digest()).decode()
    return "OAuth " + ", ".join(f'{_q(k)}="{_q(v)}"' for k, v in sorted(oauth.items()))


def x_length(text: str) -> int:
    """Length as X counts it: most Latin text counts 1 per character; CJK and emoji count 2."""
    def weight(c: str) -> int:
        o = ord(c)
        return 1 if o <= 0x10FF or 0x2000 <= o <= 0x200D or 0x2010 <= o <= 0x201F or 0x2032 <= o <= 0x2037 else 2
    return sum(weight(c) for c in text)


def _unlink(text: str) -> str:
    return DOMAIN_RE.sub(lambda m: m.group(1), URL_RE.sub("", text)).strip()


def _cut(text: str, room: int) -> str:
    if x_length(text) <= room:
        return text
    while text and x_length(text) > room - x_length("…"):  # X counts the ellipsis as 2
        text = text[:-1]
    return text.rstrip() + "…"


def x_text(p) -> str:
    """The X version: the content creator's news sentence (or headline + caption for older posts),
    a hashtag only when the story has an established one, then the source and (for licensed
    photos) the photo credit, in 280. The caption, then the hashtag, are dropped first."""
    x_post = _unlink(field_of(p, "x_post") or "")
    body = x_post or _unlink(p["headline"])
    tag = field_of(p, "x_hashtag") or ""
    publisher = _unlink(p["source_name"])
    # "..., The Guardian reports." already credits the source; then only a photo credit follows.
    source = "" if publisher and publisher.lower() in body.lower() else f"Source: {publisher}"
    if p["image_kind"] == "photo" and p["image_credit"]:
        credit = _unlink(p["image_credit"])  # CC BY/BY-SA require attribution
        source = f"{source} · {credit}" if source else credit
    caption = "" if x_post else _unlink(p["caption"])
    tagged = f"{body} {tag}" if tag else body
    for parts in ([tagged, caption, source], [tagged, source], [body, source]):
        text = "\n\n".join(x for x in parts if x)
        if x_length(text) <= TEXT_MAX:
            return text
    return _cut(body, TEXT_MAX - x_length(source) - 2) + ("\n\n" + source if source else "")


def _comparable(text: str) -> str:
    """Text as X returns it (HTML-escaped, with the image's t.co link) -> as we sent it."""
    return " ".join(html.unescape(TCO_RE.sub("", text or "")).split())


class XPublisher(Publisher):
    platform = "x"
    label = "X"

    @property
    def daily_cap(self) -> int:
        return self.settings.x_max_posts_per_day

    def __init__(self, settings: Settings, db: Database, session=None, sleep=time.sleep,
                 spacing: float | None = None, clock=None):
        super().__init__(settings, db, sleep)
        self.session = session or requests.Session()
        self.spacing = settings.x_spacing_minutes if spacing is None else spacing  # minutes between posts
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.consumer = (settings.x_api_key, settings.x_api_secret)
        self.token = (settings.x_access_token, settings.x_access_token_secret)
        self.user_id = settings.x_access_token.split("-", 1)[0]  # access tokens start with the account's ID

    def _call(self, method: str, path: str, retries: int = 0, params: dict | None = None, **kwargs) -> dict:
        """One API call. Only pass `retries` for calls that are safe to repeat (reads, media
        upload: unused media just expires). Never for creating the post."""
        url = f"{API}/{path}"
        for attempt in range(retries + 1):
            headers = {"Authorization": oauth1_header(method, url, self.consumer, self.token, params)}
            try:
                resp = self.session.request(method, url, headers=headers, params=params, timeout=TIMEOUT, **kwargs)
            except requests.RequestException as exc:
                error = PublishError(f"{type(exc).__name__}: {exc}")
            else:
                body = resp.json() if resp.content else {}
                if resp.status_code in (200, 201):
                    return body
                error = self._error(resp.status_code, body)
                if error.fatal or resp.status_code < 500:
                    raise error
            if attempt < retries:
                self.sleep(2 * (attempt + 1))
        raise error

    @staticmethod
    def _error(status: int, body: dict) -> PublishError:
        detail = body.get("detail") or "; ".join(e.get("message", "") for e in body.get("errors", [])) or str(body)[:200]
        if status == 402 or body.get("reason") == "client-not-enrolled" or "credits" in detail.lower():
            return PublishError(NO_CREDITS, fatal=True)
        if "duplicate" in detail.lower():
            return DuplicatePost(detail)
        if status == 429:
            return PublishError("X rate limit reached; it will be retried next cycle")
        return PublishError(f"X error {status}: {detail}", fatal=status in (401, 403))

    def _recent_media(self) -> list[dict]:
        params = {"max_results": 10, "exclude": "replies,retweets"}
        return self._call("GET", f"users/{self.user_id}/tweets", retries=2, params=params).get("data", [])

    def _find_live(self, p, recent: list[dict] | None = None) -> tuple[str, str] | None:
        text = _comparable(x_text(p))
        for t in recent if recent is not None else self._recent_media():
            if _comparable(t.get("text")) == text:
                return t["id"], f"https://x.com/i/status/{t['id']}"
        return None

    # --- reading engagement (for the strategist; X charges $0.005 per post read) ---------
    @staticmethod
    def _metrics_of(t: dict) -> dict:
        m = t.get("public_metrics", {})
        return {"likes": m.get("like_count"), "comments": m.get("reply_count"),
                "shares": (m.get("retweet_count") or 0) + (m.get("quote_count") or 0),
                "saves": m.get("bookmark_count"), "views": m.get("impression_count")}

    def metrics(self, media_ids: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for i in range(0, len(media_ids), 100):
            try:
                data = self._call("GET", "tweets", retries=1, params={
                    "ids": ",".join(media_ids[i:i + 100]), "tweet.fields": "public_metrics"}).get("data", [])
            except PublishError as exc:
                log.warning("  could not read X metrics: %s", exc)
                break
            out.update({t["id"]: self._metrics_of(t) for t in data})
        return out

    def benchmark(self, accounts: list[str], per_account: int) -> list[dict]:
        """Recent original posts (no replies or reposts) of other accounts."""
        try:
            users = self._call("GET", "users/by", retries=1, params={
                "usernames": ",".join(accounts), "user.fields": "public_metrics"}).get("data", [])
        except PublishError as exc:
            log.warning("  X benchmarks unavailable: %s", exc)
            return []
        posts = []
        for u in users:
            try:
                tweets = self._call("GET", f"users/{u['id']}/tweets", retries=1, params={
                    "max_results": max(5, min(per_account, 100)), "exclude": "replies,retweets",
                    "tweet.fields": "public_metrics,created_at,attachments"}).get("data", [])
            except PublishError as exc:
                log.warning("  X benchmark @%s unavailable: %s", u.get("username"), exc)
                if exc.fatal:
                    break
                continue
            for t in tweets[:per_account]:
                posts.append({"post_id": t["id"], "account": u["username"],
                              "followers": u.get("public_metrics", {}).get("followers_count"),
                              "text": t.get("text", "")[:1000],
                              "media_type": "media" if t.get("attachments", {}).get("media_keys") else "text",
                              "posted_at": t.get("created_at"), **self._metrics_of(t)})
        return posts

    def _publish_one(self, run_id: str, p) -> tuple[str, str]:
        body = {"text": x_text(p)}
        if p["image_kind"] == "photo":  # a real licensed photo; a text template adds nothing to a tweet
            media = self._call("POST", "media/upload", retries=1, data={"media_category": "tweet_image"},
                               files={"media": ("post.jpg", to_jpeg(p["image_path"]), "image/jpeg")})["data"]["id"]
            body["media"] = {"media_ids": [media]}
        try:
            post_id = self._call("POST", "tweets", json=body)["data"]["id"]
        except DuplicatePost:
            live = self._find_live(p)
            log.warning("  X says this post already exists%s", f": {live[1]}" if live else "")
            return live or ("", "")
        except PublishError as exc:
            # A slow answer doesn't mean it failed: check before reporting failure.
            if exc.fatal:
                raise
            try:
                live = self._find_live(p)
            except PublishError:
                live = None
            if live is None:
                raise
            log.warning("  post call failed but the post is live: %s", live[1])
            return live
        return post_id, f"https://x.com/i/status/{post_id}"

    # --- the queue: one post per story, spaced out ----------------------------------------
    def publish(self, run_id: str, article_ids: list[str]) -> PublishResult:
        """Queue the stories, one every `spacing` minutes after anything already queued. With no
        spacing (the `publish` command, tests) they are posted right away."""
        result = PublishResult()
        posts = {p["article_id"]: p for p in self.db.posts_for_run(run_id)}
        now = self.clock()
        waiting = self.db.queued_publications(self.platform)
        at = max([now] + [datetime.fromisoformat(r["scheduled_at"]) + timedelta(minutes=self.spacing)
                          for r in waiting[-1:]])
        batch = f"{run_id}@{now.isoformat(timespec='seconds')}"
        for aid in article_ids:
            if not self._ready(aid, posts.get(aid), result):
                continue
            when = at.isoformat(timespec="seconds")
            self.db.queue_publication(aid, self.platform, when, batch)
            result.queued.append((aid, when))
            at += timedelta(minutes=self.spacing)
        if not self.spacing:
            for done_batch, done in self.post_due():
                if done_batch == batch:
                    result.posted += done.posted
                    result.failed += done.failed
            result.queued = []
        elif result.queued:
            times = [local_time(self.settings, datetime.fromisoformat(w)) for _, w in result.queued]
            result.note = (f"at {times[0]}" if len(times) == 1
                           else f"one every {self.spacing:g} minutes, {times[0]} to {times[-1]}")
        return result

    def post_due(self) -> list[tuple[str, PublishResult]]:
        """Post the queued stories whose time has come. Returns the batches (one per approval) that
        are now complete, each with what happened to its stories."""
        now = self.clock()
        due = [r for r in self.db.queued_publications(self.platform)
               if datetime.fromisoformat(r["scheduled_at"]) <= now]
        if not due:
            return []
        budget = self.daily_cap - self.posted_today(now)
        stop = False
        for r in due:
            aid = r["article_id"]
            if stop:
                self.db.save_publication(aid, self.platform, "blocked", error="not attempted after a token/permission error")
                continue
            if now - datetime.fromisoformat(r["scheduled_at"]) > timedelta(hours=self.settings.x_queue_max_hours):
                self.db.save_publication(aid, self.platform, "expired",
                                         error=f"not posted within {self.settings.x_queue_max_hours:g} hours, so dropped")
                continue
            if budget <= 0:
                self.db.save_publication(aid, self.platform, "skipped",
                                         error=f"daily limit of {self.daily_cap} posts reached")
                continue
            p = next((x for x in self.db.posts_for_run(r["run_id"]) if x["article_id"] == aid), None)
            if p is None or not p["image_path"] or not Path(p["image_path"]).exists():
                self.db.save_publication(aid, self.platform, "blocked", error="no template image")
                continue
            done = PublishResult()
            stop = not self._post(r["run_id"], p, done)
            budget -= len(done.posted)
        finished = []
        for batch in dict.fromkeys(r["batch"] for r in due):
            rows = self.db.batch_publications(self.platform, batch)
            if any(row["status"] == "queued" for row in rows):
                continue
            result = PublishResult()
            for row in rows:
                if row["status"] == "published":
                    result.posted.append((row["article_id"], row["permalink"] or ""))
                else:
                    result.failed.append((row["article_id"], row["error"] or row["status"]))
            finished.append((batch, result))
        return finished
