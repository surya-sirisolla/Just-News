"""Agent 5 - Publisher: posts owner-approved templates to Instagram (and X, see x_publisher.py).

Runs only for posts the owner approved on WhatsApp (or explicitly via `python -m justnews publish`).
`Publisher` holds what every platform shares: each post is published at most once (the
`publications` table), a daily cap, a stop on token errors, and next-cycle retries that first check
whether a "failed" post actually went live. On Instagram, for each post:
  1. convert the template to JPEG (the only image format Instagram accepts)
  2. upload it to a private S3 bucket and create a temporary (1-hour) link
  3. POST /<IG_ID>/media with image_url + caption -> a media container; wait until Instagram has
     fetched the image (status FINISHED)
  4. POST /<IG_ID>/media_publish -> the live post; read its permalink
Posting stops at JUSTNEWS_MAX_POSTS_PER_DAY, well under Instagram's 100 API posts per 24 hours.
"""

from __future__ import annotations

import io
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests
from PIL import Image

from ..config import Settings
from ..db import Database

log = logging.getLogger("justnews.publisher")

PLATFORM = "instagram"
CAPTION_MAX = 2200
HASHTAG_MAX = 5  # Instagram's own limit per post
INSIGHT_NAMES = {"reach": "reach", "saved": "saves", "shares": "shares", "views": "views"}
URL_LIFETIME = 3600
POLL_SECONDS, POLL_ATTEMPTS = 3, 20
TIMEOUT = 60
MAX_ATTEMPTS = 3  # a failed post is retried in later cycles up to this many times
RETRY_HOURS = 2   # ...but only while the news is fresh; older failures are left alone


class PublishError(Exception):
    def __init__(self, message: str, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal  # bad token / permissions: stop trying the other posts


def to_jpeg(path: str) -> bytes:
    with Image.open(path) as img:
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=92, optimize=True)
    return buf.getvalue()


def field_of(p, key: str):
    """A post column that older rows (or test dicts) may not have."""
    try:
        return p[key]
    except (KeyError, IndexError):
        return None


TRACKING_RE = re.compile(r"[?&](?:utm_\w+|at_\w+|traffic_source|ref|cmpid|ocid)=[^&]*")


def source_line(p) -> str:
    """The attribution, without the link when it is a long Google News redirect and without
    tracking parameters otherwise."""
    if "news.google.com/" in (field_of(p, "url") or "") and field_of(p, "source_name"):
        return f"Source: {p['source_name']}"
    text = p["attribution"]
    while TRACKING_RE.search(text):
        text = TRACKING_RE.sub(lambda m: "?" if m.group(0)[0] == "?" else "", text, count=1)
    return re.sub(r"\?&", "?", text).rstrip("?&")


def instagram_caption(p) -> str:
    """The Instagram version (or headline + caption for older posts), hashtags, source and (for
    licensed photos) the photo credit."""
    body = field_of(p, "ig_caption") or "\n\n".join(x for x in (p["headline"], p["caption"]) if x)
    tags = json.loads(p["hashtags"])[:HASHTAG_MAX]
    parts = [body, " ".join(tags), source_line(p)]
    if p["image_kind"] == "photo" and p["image_credit"]:
        parts.append(p["image_credit"])  # CC BY/BY-SA require attribution
    text = "\n\n".join(x for x in parts if x)
    return text if len(text) <= CAPTION_MAX else text[: CAPTION_MAX - 1] + "…"


class S3MediaStore:
    """Private bucket; Instagram gets a short-lived presigned link, so nothing is public."""

    def __init__(self, bucket: str, region: str):
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        self.s3 = boto3.client("s3", region_name=region,
                               config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))

    def put(self, key: str, data: bytes) -> str:
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType="image/jpeg")
        return self.s3.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": key},
                                              ExpiresIn=URL_LIFETIME)

    def delete(self, key: str) -> None:
        self.s3.delete_object(Bucket=self.bucket, Key=key)


@dataclass
class PublishResult:
    posted: list[tuple[str, str]] = field(default_factory=list)   # (article_id, permalink)
    failed: list[tuple[str, str]] = field(default_factory=list)   # (article_id, reason)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (article_id, reason)


class Publisher:
    """Shared publishing loop; subclasses post one story and look up recent posts on the account."""

    name = "publisher"
    platform = ""  # key in the publications table
    label = ""     # shown to the owner

    def __init__(self, settings: Settings, db: Database, sleep=time.sleep):
        self.settings = settings
        self.db = db
        self.sleep = sleep

    def remaining_quota(self) -> int | None:
        """Posts the platform itself still allows today; None when it doesn't say."""
        return None

    def _publish_one(self, run_id: str, p) -> tuple[str, str]:
        raise NotImplementedError

    def _recent_media(self) -> list[dict]:
        raise NotImplementedError

    def _find_live(self, p, recent: list[dict] | None = None) -> tuple[str, str] | None:
        raise NotImplementedError

    def publish(self, run_id: str, article_ids: list[str]) -> PublishResult:
        result = PublishResult()
        posts = {p["article_id"]: p for p in self.db.posts_for_run(run_id)}
        budget = self.settings.max_posts_per_day - self.db.published_since(self.platform, 24)
        try:
            quota = self.remaining_quota()
        except PublishError as exc:
            log.error("%s quota check failed: %s", self.label, exc)
            result.failed += [(aid, str(exc)) for aid in article_ids]
            return result
        if quota is not None:
            budget = min(budget, quota)
        for aid in article_ids:
            p = posts.get(aid)
            done = self.db.publication(aid, self.platform)
            if done and done["status"] == "published":
                result.skipped.append((aid, "already posted"))
                continue
            if p is None or not p["image_path"] or not Path(p["image_path"]).exists():
                result.failed.append((aid, "no template image"))
                continue
            if budget <= 0:
                result.skipped.append((aid, f"daily limit of {self.settings.max_posts_per_day} posts reached"))
                continue
            try:
                media_id, link = self._publish_one(run_id, p)
            except PublishError as exc:
                log.error("  %s post failed for %r: %s", self.label, p["headline"][:60], exc)
                # A token, permission or credits problem is "blocked": never retried by itself, so a
                # story approved hours ago doesn't go out late once the account is fixed.
                self.db.save_publication(aid, self.platform, "blocked" if exc.fatal else "failed", error=str(exc))
                result.failed.append((aid, str(exc)))
                if exc.fatal:
                    result.failed += [(a, "not attempted after a token/permission error")
                                      for a in article_ids[article_ids.index(aid) + 1:]]
                    break
                continue
            self.db.save_publication(aid, self.platform, "published", media_id=media_id, permalink=link)
            result.posted.append((aid, link))
            budget -= 1
            log.info("  posted to %s: %s", self.label, link)
        return result

    def retry_failed(self) -> PublishResult:
        """Next-cycle retry of approved posts that failed. Anything that actually went live is first
        reconciled as posted, so a retry never duplicates a post."""
        result = PublishResult()
        failed = self.db.failed_publications(self.platform, MAX_ATTEMPTS, RETRY_HOURS)
        if not failed:
            return result
        try:
            recent = self._recent_media()
        except PublishError as exc:
            log.warning("Could not check %s before retrying: %s", self.label, exc)
            return result
        retry: dict[str, list[str]] = {}
        for row in failed:
            p = next((x for x in self.db.posts_for_run(row["run_id"]) if x["article_id"] == row["article_id"]), None)
            if p is None:
                continue
            live = self._find_live(p, recent)
            if live:
                self.db.save_publication(p["article_id"], self.platform, "published", media_id=live[0], permalink=live[1])
                result.posted.append((p["article_id"], live[1]))
                log.info("  reconciled: %r was already live at %s", p["headline"][:50], live[1])
            else:
                retry.setdefault(row["run_id"], []).append(p["article_id"])
        for run_id, ids in retry.items():
            again = self.publish(run_id, ids)
            result.posted += again.posted
            result.failed += again.failed
            result.skipped += again.skipped
        return result


class InstagramPublisher(Publisher):
    platform = PLATFORM
    label = "Instagram"

    def __init__(self, settings: Settings, db: Database, store=None, session=None, sleep=time.sleep):
        super().__init__(settings, db, sleep)
        self.store = store or S3MediaStore(settings.s3_bucket, settings.aws_region)
        self.session = session or requests.Session()
        # Token in a header, never in URLs or logs.
        self.session.headers["Authorization"] = f"Bearer {settings.instagram_token}"
        self.base = f"https://graph.facebook.com/{settings.instagram_api_version}"
        self.ig = settings.instagram_user_id
        self.insights: bool | None = None  # unknown until the first insights call

    # --- Graph API ----------------------------------------------------------------
    def _call(self, method: str, path: str, retries: int = 0, **data) -> dict:
        """One Graph API call. Only pass `retries` for calls that are safe to repeat: reads, and
        creating a container (an unpublished container just expires). Never media_publish."""
        for attempt in range(retries + 1):
            try:
                resp = self.session.request(method, f"{self.base}/{path}", data=data if method == "POST" else None,
                                            params=data if method == "GET" else None, timeout=TIMEOUT)
            except requests.RequestException as exc:
                error = PublishError(f"{type(exc).__name__}: {exc}")
            else:
                body = resp.json() if resp.content else {}
                if resp.status_code == 200:
                    return body
                err = body.get("error", {})
                fatal = err.get("code") in (10, 190, 200) or resp.status_code in (401, 403)
                error = PublishError(f"Instagram error {err.get('code')}: {err.get('message', resp.text[:200])}", fatal)
                if fatal or resp.status_code < 500:
                    raise error
            if attempt < retries:
                self.sleep(2 * (attempt + 1))
        raise error

    def _recent_media(self) -> list[dict]:
        return self._call("GET", f"{self.ig}/media", retries=2, fields="id,caption,permalink", limit=25).get("data", [])

    def _find_live(self, p, recent: list[dict] | None = None) -> tuple[str, str] | None:
        """(media id, permalink) if this exact post is already live on the account."""
        caption = instagram_caption(p)
        for m in recent if recent is not None else self._recent_media():
            if (m.get("caption") or "").strip() == caption.strip():
                return m["id"], m.get("permalink", "")
        return None

    def remaining_quota(self) -> int:
        data = self._call("GET", f"{self.ig}/content_publishing_limit", retries=2, fields="quota_usage,config")["data"][0]
        return data["config"]["quota_total"] - data["quota_usage"]

    # --- reading engagement (for the strategist) ----------------------------------------
    def metrics(self, media_ids: list[str]) -> dict[str, dict]:
        """Likes and comments of our posts, plus reach, views, saves and shares when the token has
        the instagram_manage_insights permission."""
        out: dict[str, dict] = {}
        for mid in media_ids:
            try:
                m = self._call("GET", mid, retries=1, fields="like_count,comments_count")
            except PublishError as exc:
                log.warning("  could not read Instagram post %s: %s", mid, exc)
                if exc.fatal:
                    break
                continue
            row = {"likes": m.get("like_count"), "comments": m.get("comments_count")}
            if self.insights is not False:
                try:
                    data = self._call("GET", f"{mid}/insights", retries=1, metric=",".join(INSIGHT_NAMES)).get("data", [])
                    for d in data:
                        value = (d.get("values") or [{}])[0].get("value", d.get("total_value", {}).get("value"))
                        if d.get("name") in INSIGHT_NAMES:
                            row[INSIGHT_NAMES[d["name"]]] = value
                    self.insights = True
                except PublishError as exc:
                    if exc.fatal:  # (#10): the token lacks instagram_manage_insights
                        log.info("  Instagram insights unavailable (%s); reading likes and comments only", exc)
                        self.insights = False
            out[mid] = row
        return out

    def benchmark(self, accounts: list[str], per_account: int) -> list[dict]:
        """Recent posts of other public business/creator accounts (Business Discovery)."""
        posts = []
        for user in accounts:
            query = ("business_discovery.username(" + user + "){followers_count,media.limit(" + str(per_account) +
                     "){id,caption,like_count,comments_count,timestamp,media_type,media_product_type}}")
            try:
                found = self._call("GET", self.ig, retries=1, fields=query)["business_discovery"]
            except PublishError as exc:
                log.warning("  Instagram benchmark @%s unavailable: %s", user, exc)
                if exc.fatal:  # a missing permission fails the same way for every account
                    break
                continue
            for m in found.get("media", {}).get("data", []):
                kind = "REEL" if m.get("media_product_type") == "REELS" else m.get("media_type")
                posts.append({"post_id": m["id"], "account": user, "followers": found.get("followers_count"),
                              "text": (m.get("caption") or "")[:1000], "media_type": kind,
                              "likes": m.get("like_count"), "comments": m.get("comments_count"),
                              "posted_at": m.get("timestamp")})
        return posts

    # --- publishing -------------------------------------------------------------------
    def _publish_one(self, run_id: str, p) -> tuple[str, str]:
        key = f"posts/{run_id}/{p['article_id']}.jpg"
        try:
            url = self.store.put(key, to_jpeg(p["image_path"]))
        except Exception as exc:  # S3 credentials, bucket or network
            raise PublishError(f"could not upload the image to S3: {exc}") from exc
        container = self._call("POST", f"{self.ig}/media", retries=1, image_url=url, caption=instagram_caption(p))["id"]
        for _ in range(POLL_ATTEMPTS):  # Instagram downloads and processes the image first
            status = self._call("GET", container, retries=2, fields="status_code,status").get("status_code")
            if status == "FINISHED":
                break
            if status in ("ERROR", "EXPIRED"):
                raise PublishError(f"Instagram could not process the image (status {status})")
            self.sleep(POLL_SECONDS)
        else:
            raise PublishError("Instagram took too long to process the image")
        try:
            media_id = self._call("POST", f"{self.ig}/media_publish", creation_id=container)["id"]
        except PublishError as exc:
            # A slow answer doesn't mean it failed: Instagram may have published anyway. Check
            # before reporting failure, so a later retry can't post the same story twice.
            if exc.fatal:
                raise
            status = self._call("GET", container, retries=2, fields="status_code").get("status_code")
            live = self._find_live(p) if status == "PUBLISHED" else None
            if live is None:
                raise
            log.warning("  publish call timed out but the post is live: %s", live[1])
            media_id = live[0]
        try:
            link = self._call("GET", media_id, retries=2, fields="permalink").get("permalink", "")
        except PublishError:
            link = ""  # it's posted; a missing link must not turn that into a failure
        try:
            self.store.delete(key)  # Instagram has its own copy now
        except Exception:
            log.warning("  could not delete %s from S3 (the lifecycle rule will)", key)
        return media_id, link
