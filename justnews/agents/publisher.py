"""Agent 5 - Publisher: posts owner-approved templates to Instagram (and X, see x_publisher.py).

Runs only for posts the owner approved on WhatsApp (or explicitly via `python -m justnews publish`).
`Publisher` holds what every platform shares: each post is published at most once (the
`publications` table), a daily cap, a stop on token errors, and next-cycle retries that first check
whether a "failed" post actually went live.

On Instagram, the stories approved together go out as one carousel (cover, a slide per story,
closing slide; see carousel.py), and a story approved alone as a single image post:
  1. render each image as JPEG (the only image format Instagram accepts)
  2. upload it to a private S3 bucket and create a temporary (1-hour) link
  3. POST /<IG_ID>/media with image_url (+ is_carousel_item for slides) -> a container; then, for
     a carousel, POST /<IG_ID>/media with media_type=CAROUSEL, the children and the caption; wait
     until Instagram has fetched everything (status FINISHED)
  4. POST /<IG_ID>/media_publish -> the live post; read its permalink
A carousel counts as one post towards JUSTNEWS_MAX_POSTS_PER_DAY (Instagram allows 100 a day).
"""

from __future__ import annotations

import io
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from PIL import Image

from .. import carousel
from ..config import Settings
from ..db import Database
from ..graphics import Fonts

log = logging.getLogger("justnews.publisher")

PLATFORM = "instagram"
CAPTION_MAX = 2200
HASHTAG_MAX = 5  # Instagram's own limit per post
IG_HOOK_MAX = 120  # longer first lines are paragraphs, not hooks: carousels list the headline instead
INSIGHT_NAMES = {"reach": "reach", "saved": "saves", "shares": "shares", "views": "views"}
URL_LIFETIME = 3600
POLL_SECONDS, POLL_ATTEMPTS = 3, 20
REEL_POLL_SECONDS, REEL_POLL_ATTEMPTS = 6, 60  # Instagram takes a minute or more to process a video
TIMEOUT = 60
MAX_ATTEMPTS = 3  # a failed post is retried in later cycles up to this many times
RETRY_HOURS = 2   # ...but only while the news is fresh; older failures are left alone
BREAKING_HOURS = 1   # a carousel cover says "BREAKING NEWS" only for a lead story this new...
BREAKING_SCORE = 85  # ...and this important; otherwise "LATEST"
PHOTO_LEAD_MARGIN = 10  # a story with a photo may lead the carousel if it scores within this of the best


class PublishError(Exception):
    def __init__(self, message: str, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal  # bad token / permissions: stop trying the other posts


def jpeg_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=92, optimize=True)
    return buf.getvalue()


def to_jpeg(path: str) -> bytes:
    with Image.open(path) as img:
        return jpeg_bytes(img)


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
    """The Instagram version (or headline + caption for older posts), the source, (for licensed
    photos) the photo credit, and the hashtags last."""
    body = field_of(p, "ig_caption") or "\n\n".join(x for x in (p["headline"], p["caption"]) if x)
    tags = json.loads(p["hashtags"])[:HASHTAG_MAX]
    parts = [body, source_line(p)]
    if p["image_kind"] == "photo" and p["image_credit"]:
        parts.append(p["image_credit"])  # CC BY/BY-SA require attribution
    parts.append(" ".join(tags))
    text = "\n\n".join(x for x in parts if x)
    return text if len(text) <= CAPTION_MAX else text[: CAPTION_MAX - 1] + "…"


def merge_hashtags(tag_lists: list[list[str]], limit: int = HASHTAG_MAX) -> list[str]:
    """One set of hashtags for several stories: tags shared by more stories first, then every story's
    best tag before any story's second, so the set covers as many stories as it can."""
    order: dict[str, tuple[int, int, int]] = {}
    for s, tags in enumerate(tag_lists):
        for rank, tag in enumerate(tags):
            key = tag.lower()
            count = order[key][0] + 1 if key in order else 1
            order[key] = (count, *(order[key][1:] if key in order else (rank, s)))
    names = {t.lower(): t for tags in tag_lists for t in tags}
    ranked = sorted(order, key=lambda k: (-order[k][0], order[k][1], order[k][2]))
    return [names[k] for k in ranked[:limit]]


def ig_hook(p) -> str:
    """The Instagram caption's first line (its everyday-words hook), else the headline."""
    hook = (field_of(p, "ig_caption") or "").split("\n", 1)[0].strip()
    return hook if 0 < len(hook) <= IG_HOOK_MAX else p["headline"]


def carousel_caption(posts: list) -> str:
    """The stories' hooks, numbered, then shared hashtags, sources and (for licensed photos) the
    photo credits."""
    headlines = "\n".join(f"{i}. {ig_hook(p)}" for i, p in enumerate(posts, 1))
    tags = merge_hashtags([json.loads(p["hashtags"]) for p in posts])
    sources = list(dict.fromkeys(field_of(p, "source_name") or p["attribution"] for p in posts))
    credits = [p["image_credit"] for p in posts if p["image_kind"] == "photo" and p["image_credit"]]
    parts = [f"{len(posts)} stories this hour. Swipe through:", headlines, "Sources: " + ", ".join(sources)]
    if credits:
        parts.append("Photos: " + "; ".join(dict.fromkeys(credits)))  # CC BY/BY-SA require attribution
    parts.append(" ".join(tags))
    text = "\n\n".join(x for x in parts if x)
    return text if len(text) <= CAPTION_MAX else text[: CAPTION_MAX - 1] + "…"


def story_groups(ids: list[str], size: int) -> list[list[str]]:
    """Split into the fewest groups of at most `size`, as even as possible (10 -> 5 + 5)."""
    if not ids:
        return []
    n = -(-len(ids) // size)
    per = -(-len(ids) // n)
    return [ids[i:i + per] for i in range(0, len(ids), per)]


class S3MediaStore:
    """Private bucket; Instagram gets a short-lived presigned link, so nothing is public."""

    def __init__(self, bucket: str, region: str):
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        self.s3 = boto3.client("s3", region_name=region,
                               config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))

    def put(self, key: str, data: bytes, content_type: str = "image/jpeg") -> str:
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)
        return self.s3.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": key},
                                              ExpiresIn=URL_LIFETIME)

    def delete(self, key: str) -> None:
        self.s3.delete_object(Bucket=self.bucket, Key=key)


@dataclass
class PublishResult:
    posted: list[tuple[str, str]] = field(default_factory=list)   # (article_id, permalink)
    failed: list[tuple[str, str]] = field(default_factory=list)   # (article_id, reason)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (article_id, reason)
    queued: list[tuple[str, str]] = field(default_factory=list)   # (article_id, when, ISO): posted later
    note: str = ""                                                # e.g. how the queue is spaced

    def merge(self, other: "PublishResult") -> None:
        self.posted += other.posted
        self.failed += other.failed
        self.skipped += other.skipped
        self.queued += other.queued


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

    @property
    def daily_cap(self) -> int:
        return self.settings.max_posts_per_day

    def _budget(self, result: PublishResult, article_ids: list[str]) -> int | None:
        """Posts left today (our cap and the platform's own quota); None when the quota check fails."""
        budget = self.daily_cap - self.db.published_since(self.platform, 24)
        try:
            quota = self.remaining_quota()
        except PublishError as exc:
            log.error("%s quota check failed: %s", self.label, exc)
            result.failed += [(aid, str(exc)) for aid in article_ids]
            return None
        return budget if quota is None else min(budget, quota)

    def _ready(self, aid: str, p, result: PublishResult) -> bool:
        done = self.db.publication(aid, self.platform)
        if done and done["status"] == "published":
            result.skipped.append((aid, "already posted"))
            return False
        if p is None or not p["image_path"] or not Path(p["image_path"]).exists():
            result.failed.append((aid, "no template image"))
            return False
        return True

    def _failed(self, aids: list[str], headline: str, exc: PublishError, result: PublishResult) -> None:
        log.error("  %s post failed for %r: %s", self.label, headline[:60], exc)
        # A token, permission or credits problem is "blocked": never retried by itself, so a story
        # approved hours ago doesn't go out late once the account is fixed.
        for aid in aids:
            self.db.save_publication(aid, self.platform, "blocked" if exc.fatal else "failed", error=str(exc))
            result.failed.append((aid, str(exc)))

    def _post(self, run_id: str, p, result: PublishResult) -> bool:
        """Publish one story; False after a token/permission error (stop posting)."""
        try:
            media_id, link = self._publish_one(run_id, p)
        except PublishError as exc:
            self._failed([p["article_id"]], p["headline"], exc, result)
            return not exc.fatal
        self.db.save_publication(p["article_id"], self.platform, "published", media_id=media_id, permalink=link)
        result.posted.append((p["article_id"], link))
        log.info("  posted to %s: %s", self.label, link)
        return True

    def publish(self, run_id: str, article_ids: list[str]) -> PublishResult:
        result = PublishResult()
        posts = {p["article_id"]: p for p in self.db.posts_for_run(run_id)}
        budget = self._budget(result, article_ids)
        if budget is None:
            return result
        for i, aid in enumerate(article_ids):
            if not self._ready(aid, posts.get(aid), result):
                continue
            if budget <= 0:
                result.skipped.append((aid, f"daily limit of {self.daily_cap} posts reached"))
                continue
            if not self._post(run_id, posts[aid], result):
                result.failed += [(a, "not attempted after a token/permission error") for a in article_ids[i + 1:]]
                break
            budget -= 1
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
        self.fonts = Fonts(settings.font_bold, settings.font_regular)
        self._handle: str | None = None

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
        """(media id, permalink) if this story is already live: as its own post, or as a numbered
        headline in a carousel's caption."""
        caption, numbered = instagram_caption(p).strip(), re.compile(rf"^\d+\. {re.escape(p['headline'].strip())}$", re.M)
        for m in recent if recent is not None else self._recent_media():
            live = (m.get("caption") or "").strip()
            if live == caption or numbered.search(live):
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
    def publish(self, run_id: str, article_ids: list[str]) -> PublishResult:
        """Stories approved together go out as one carousel (single stories as a normal post)."""
        if not self.settings.instagram_carousel:
            return super().publish(run_id, article_ids)
        result = PublishResult()
        posts = {p["article_id"]: p for p in self.db.posts_for_run(run_id)}
        budget = self._budget(result, article_ids)
        if budget is None:
            return result
        ready = [aid for aid in article_ids if self._ready(aid, posts.get(aid), result)]
        groups = story_groups(ready, carousel.MAX_STORIES)
        for g, group in enumerate(groups):
            if budget <= 0:
                result.skipped += [(a, f"daily limit of {self.daily_cap} posts reached") for a in group]
                continue
            if len(group) == 1:
                ok = self._post(run_id, posts[group[0]], result)
            else:
                ok = self._post_carousel(run_id, [posts[a] for a in group], result)
            if not ok:
                result.failed += [(a, "not attempted after a token/permission error") for gr in groups[g + 1:] for a in gr]
                break
            budget -= 1
        return result

    def _post_carousel(self, run_id: str, ps: list, result: PublishResult) -> bool:
        # A strong story with a licensed photo leads, so the cover shows a photo of its own story;
        # a much weaker one never displaces the top story.
        best = max((field_of(p, "overall") or 0) for p in ps)
        lead = next((p for p in ps if self._photo(p) and (field_of(p, "overall") or 0) >= best - PHOTO_LEAD_MARGIN),
                    max(ps, key=lambda p: field_of(p, "overall") or 0))
        ps = [lead] + [p for p in ps if p is not lead]
        aids = [p["article_id"] for p in ps]
        try:
            media_id, link = self._publish_carousel(run_id, ps)
        except PublishError as exc:
            self._failed(aids, f"carousel of {len(ps)}: {ps[0]['headline']}", exc, result)
            return not exc.fatal
        for aid in aids:
            self.db.save_publication(aid, self.platform, "published", media_id=media_id, permalink=link)
            result.posted.append((aid, link))
        log.info("  posted a carousel of %d stories to Instagram: %s", len(ps), link)
        return True

    def _handle_name(self) -> str:
        if self._handle is None:
            try:
                self._handle = self._call("GET", self.ig, retries=1, fields="username").get("username", "")
            except PublishError:
                self._handle = ""
        return self._handle

    def _cover_label(self, lead) -> str:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(lead["published_at"])
        except (TypeError, ValueError, KeyError, IndexError):
            return "LATEST"
        major = (field_of(lead, "overall") or 0) >= BREAKING_SCORE
        return "BREAKING NEWS" if age <= timedelta(hours=BREAKING_HOURS) and major else "LATEST"

    @staticmethod
    def _photo(p) -> str | None:
        path = field_of(p, "photo_path")
        return path if path and Path(path).exists() else None

    def _slides(self, ps: list) -> list[Image.Image]:
        stories = []
        for p in ps:
            photo = Image.open(self._photo(p)) if self._photo(p) else None
            stories.append({"headline": p["headline"], "caption": p["caption"], "category": p["category"],
                            "source": field_of(p, "source_name") or "", "image_text": p["image_text"],
                            "photo": photo, "credit": p["image_credit"] if photo else None})
        date = datetime.now(ZoneInfo(self.settings.timezone)).strftime("%-d %b %Y")
        return carousel.render(stories, self._cover_label(ps[0]), date, self._handle_name(),
                               self.settings.brand_name, self.fonts)

    def _publish_carousel(self, run_id: str, ps: list) -> tuple[str, str]:
        keys, children = [], []
        try:
            for i, slide in enumerate(self._slides(ps)):
                key = f"posts/{run_id}/carousel-{ps[0]['article_id'][:12]}-{i}.jpg"
                url = self._upload(key, jpeg_bytes(slide))
                keys.append(key)
                children.append(self._call("POST", f"{self.ig}/media", retries=1, image_url=url,
                                           is_carousel_item="true")["id"])
            for child in children:
                self._wait(child)
            container = self._call("POST", f"{self.ig}/media", retries=1, media_type="CAROUSEL",
                                   children=",".join(children), caption=carousel_caption(ps))["id"]
            self._wait(container)
            media_id = self._publish_container(container, ps[0])
            return media_id, self._permalink(media_id)
        finally:
            self._cleanup(keys)

    def _publish_one(self, run_id: str, p) -> tuple[str, str]:
        key = f"posts/{run_id}/{p['article_id']}.jpg"
        try:
            url = self._upload(key, to_jpeg(p["image_path"]))
            container = self._call("POST", f"{self.ig}/media", retries=1, image_url=url,
                                   caption=instagram_caption(p))["id"]
            self._wait(container)
            media_id = self._publish_container(container, p)
            return media_id, self._permalink(media_id)
        finally:
            self._cleanup([key])

    def _upload(self, key: str, data: bytes, content_type: str = "image/jpeg") -> str:
        try:
            return self.store.put(key, data, content_type)
        except Exception as exc:  # S3 credentials, bucket or network
            raise PublishError(f"could not upload the image to S3: {exc}") from exc

    def _wait(self, container: str, attempts: int = POLL_ATTEMPTS, interval: float = POLL_SECONDS) -> None:
        for _ in range(attempts):  # Instagram downloads and processes the media first
            answer = self._call("GET", container, retries=2, fields="status_code,status")
            status = answer.get("status_code")
            if status == "FINISHED":
                return
            if status in ("ERROR", "EXPIRED"):
                detail = f": {answer['status']}" if answer.get("status") else ""
                raise PublishError(f"Instagram could not process the media (status {status}{detail})")
            self.sleep(interval)
        raise PublishError("Instagram took too long to process the media")

    def publish_reel(self, key: str, video: bytes, caption: str, cover_at_ms: int = 0) -> tuple[str, str]:
        """Post a video as a Reel (also shown in the feed); its cover is the frame at `cover_at_ms`
        (a separate cover image made Instagram fail the whole Reel). Returns (media id, permalink)."""
        keys = [f"{key}.mp4"]
        try:
            video_url = self._upload(keys[0], video, "video/mp4")
            container = self._call("POST", f"{self.ig}/media", retries=1, media_type="REELS", video_url=video_url,
                                   thumb_offset=str(cover_at_ms), caption=caption, share_to_feed="true")["id"]
            self._wait(container, REEL_POLL_ATTEMPTS, REEL_POLL_SECONDS)
            try:
                media_id = self._call("POST", f"{self.ig}/media_publish", creation_id=container)["id"]
            except PublishError as exc:
                # As with posts: a slow answer may hide a success, so look before reporting failure.
                if exc.fatal:
                    raise
                status = self._call("GET", container, retries=2, fields="status_code").get("status_code")
                live = next((m for m in self._recent_media() if (m.get("caption") or "").strip() == caption.strip()),
                            None) if status == "PUBLISHED" else None
                if live is None:
                    raise
                return live["id"], live.get("permalink", "")
            return media_id, self._permalink(media_id)
        finally:
            self._cleanup(keys)

    def _publish_container(self, container: str, p) -> str:
        try:
            return self._call("POST", f"{self.ig}/media_publish", creation_id=container)["id"]
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
            return live[0]

    def _permalink(self, media_id: str) -> str:
        try:
            return self._call("GET", media_id, retries=2, fields="permalink").get("permalink", "")
        except PublishError:
            return ""  # it's posted; a missing link must not turn that into a failure

    def _cleanup(self, keys: list[str]) -> None:
        for key in keys:  # Instagram has its own copy now (or the post failed)
            try:
                self.store.delete(key)
            except Exception:
                log.warning("  could not delete %s from S3 (the lifecycle rule will)", key)
