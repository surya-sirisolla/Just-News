"""Agent 5 - Publisher: posts owner-approved templates to Instagram.

Runs only for posts the owner approved on WhatsApp (or explicitly via `python -m justnews publish`).
For each post:
  1. convert the template to JPEG (the only image format Instagram accepts)
  2. upload it to a private S3 bucket and create a temporary (1-hour) link
  3. POST /<IG_ID>/media with image_url + caption -> a media container; wait until Instagram has
     fetched the image (status FINISHED)
  4. POST /<IG_ID>/media_publish -> the live post; read its permalink
Each post is published at most once (the `publications` table), and posting stops at
JUSTNEWS_MAX_POSTS_PER_DAY, well under Instagram's 100 API posts per 24 hours.
"""

from __future__ import annotations

import io
import json
import logging
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
HASHTAG_MAX = 30
URL_LIFETIME = 3600
POLL_SECONDS, POLL_ATTEMPTS = 3, 20


class PublishError(Exception):
    def __init__(self, message: str, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal  # bad token / permissions: stop trying the other posts


def to_jpeg(path: str) -> bytes:
    with Image.open(path) as img:
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=92, optimize=True)
    return buf.getvalue()


def instagram_caption(p) -> str:
    """Headline, caption, hashtags, source and (for licensed photos) the photo credit."""
    tags = json.loads(p["hashtags"])[:HASHTAG_MAX]
    parts = [p["headline"], p["caption"], " ".join(tags), p["attribution"]]
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


class InstagramPublisher:
    name = "publisher"

    def __init__(self, settings: Settings, db: Database, store=None, session=None, sleep=time.sleep):
        self.settings = settings
        self.db = db
        self.store = store or S3MediaStore(settings.s3_bucket, settings.aws_region)
        self.session = session or requests.Session()
        # Token in a header, never in URLs or logs.
        self.session.headers["Authorization"] = f"Bearer {settings.instagram_token}"
        self.base = f"https://graph.facebook.com/{settings.instagram_api_version}"
        self.ig = settings.instagram_user_id
        self.sleep = sleep

    # --- Graph API ----------------------------------------------------------------
    def _call(self, method: str, path: str, **data) -> dict:
        try:
            resp = self.session.request(method, f"{self.base}/{path}", data=data if method == "POST" else None,
                                        params=data if method == "GET" else None, timeout=30)
        except requests.RequestException as exc:
            raise PublishError(f"{type(exc).__name__}: {exc}") from exc
        body = resp.json() if resp.content else {}
        if resp.status_code != 200:
            err = body.get("error", {})
            fatal = err.get("code") in (10, 190, 200) or resp.status_code in (401, 403)
            raise PublishError(f"Instagram error {err.get('code')}: {err.get('message', resp.text[:200])}", fatal)
        return body

    def remaining_quota(self) -> int:
        data = self._call("GET", f"{self.ig}/content_publishing_limit", fields="quota_usage,config")["data"][0]
        return data["config"]["quota_total"] - data["quota_usage"]

    # --- publishing -------------------------------------------------------------------
    def publish(self, run_id: str, article_ids: list[str]) -> PublishResult:
        result = PublishResult()
        posts = {p["article_id"]: p for p in self.db.posts_for_run(run_id)}
        budget = self.settings.max_posts_per_day - self.db.published_since(PLATFORM, 24)
        try:
            budget = min(budget, self.remaining_quota())
        except PublishError as exc:
            log.error("Instagram quota check failed: %s", exc)
            result.failed += [(aid, str(exc)) for aid in article_ids]
            return result
        for aid in article_ids:
            p = posts.get(aid)
            done = self.db.publication(aid, PLATFORM)
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
                log.error("  Instagram post failed for %r: %s", p["headline"][:60], exc)
                self.db.save_publication(aid, PLATFORM, "failed", error=str(exc))
                result.failed.append((aid, str(exc)))
                if exc.fatal:
                    result.failed += [(a, "not attempted after a token/permission error")
                                      for a in article_ids[article_ids.index(aid) + 1:]]
                    break
                continue
            self.db.save_publication(aid, PLATFORM, "published", media_id=media_id, permalink=link)
            result.posted.append((aid, link))
            budget -= 1
            log.info("  posted to Instagram: %s", link)
        return result

    def _publish_one(self, run_id: str, p) -> tuple[str, str]:
        key = f"posts/{run_id}/{p['article_id']}.jpg"
        try:
            url = self.store.put(key, to_jpeg(p["image_path"]))
        except Exception as exc:  # S3 credentials, bucket or network
            raise PublishError(f"could not upload the image to S3: {exc}") from exc
        container = self._call("POST", f"{self.ig}/media", image_url=url, caption=instagram_caption(p))["id"]
        for _ in range(POLL_ATTEMPTS):  # Instagram downloads and processes the image first
            status = self._call("GET", container, fields="status_code,status").get("status_code")
            if status == "FINISHED":
                break
            if status in ("ERROR", "EXPIRED"):
                raise PublishError(f"Instagram could not process the image (status {status})")
            self.sleep(POLL_SECONDS)
        else:
            raise PublishError("Instagram took too long to process the image")
        media_id = self._call("POST", f"{self.ig}/media_publish", creation_id=container)["id"]
        link = self._call("GET", media_id, fields="permalink").get("permalink", "")
        try:
            self.store.delete(key)  # Instagram has its own copy now
        except Exception:
            log.warning("  could not delete %s from S3 (the lifecycle rule will)", key)
        return media_id, link
