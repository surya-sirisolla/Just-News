"""Offline tests for Agent 5 (Instagram publisher): fake Graph API and fake S3, no network."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

from PIL import Image

from justnews.agents.publisher import InstagramPublisher, instagram_caption
from justnews.approval import ApprovalService
from justnews.config import Settings
from justnews.db import Database
from justnews.models import Article, Evaluation
from justnews.whatsapp import Incoming, OutboxClient

NOW = datetime.now(timezone.utc)
IG = "17841400000000000"


class FakeStore:
    def __init__(self):
        self.objects, self.deleted = {}, []

    def put(self, key, data):
        self.objects[key] = data
        return f"https://bucket.s3.amazonaws.com/{key}?X-Amz-Signature=abc"

    def delete(self, key):
        self.deleted.append(key)


class FakeGraph:
    """Answers like the Instagram Graph API; `fail` makes a matching call return an error."""

    def __init__(self, statuses=("IN_PROGRESS", "FINISHED"), quota_used=0, fail=None, publish_timeout=None,
                 flaky_gets=0):
        self.headers, self.calls = {}, []
        self.statuses, self.quota_used, self.fail = list(statuses), quota_used, fail
        self.n = 0
        self.live = []          # media actually on the account: (id, caption, permalink)
        self.captions = {}      # container id -> caption
        self.published = set()  # container ids that went live
        self.publish_timeout = publish_timeout  # None, "after_publishing" or "before_publishing"
        self.flaky_gets = flaky_gets

    def request(self, method, url, data=None, params=None, timeout=None):
        path = url.split("/v23.0/", 1)[1]
        self.calls.append((method, path, data or params))
        if method == "GET" and self.flaky_gets:
            self.flaky_gets -= 1
            import requests
            raise requests.ReadTimeout("slow")
        if self.fail and self.fail[0] in path:
            return NS(status_code=400, content=b"x", text="err", json=lambda: {"error": {"code": self.fail[1], "message": "nope"}})
        if path.endswith("content_publishing_limit"):
            body = {"data": [{"quota_usage": self.quota_used, "config": {"quota_total": 100, "quota_duration": 86400}}]}
        elif path.endswith("/media") and method == "GET":
            body = {"data": [{"id": i, "caption": c, "permalink": l} for i, c, l in reversed(self.live)]}
        elif path.endswith("/media"):
            self.n += 1
            self.captions[f"container{self.n}"] = data["caption"]
            body = {"id": f"container{self.n}"}
        elif path.startswith("container"):
            if path in self.published:
                body = {"status_code": "PUBLISHED"}
            else:
                body = {"status_code": self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]}
        elif path.endswith("media_publish"):
            cid = data["creation_id"]
            mode, self.publish_timeout = self.publish_timeout, None
            if mode != "before_publishing":
                self.published.add(cid)
                self.live.append((f"media{cid[-1]}", self.captions[cid], f"https://www.instagram.com/p/POST{cid[-1]}/"))
            if mode:
                import requests
                raise requests.ReadTimeout("Read timed out. (read timeout=60)")
            body = {"id": f"media{cid[-1]}"}
        else:
            body = {"permalink": f"https://www.instagram.com/p/POST{path[-1]}/"}
        return NS(status_code=200, content=b"x", json=lambda: body)


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings = replace(Settings(), db_path=self.root / "t.db", instagram_token="tok", instagram_user_id=IG,
                                s3_bucket="b", max_posts_per_day=10, whatsapp_owner="919800000001")
        self.db = Database(self.settings.db_path)
        self.ids = self.make_posts(3)

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()

    def make_posts(self, n, run_id="r1"):
        arts = [Article(title=f"Story {i}", url=f"https://bbc.co.uk/{i}", source_name="BBC News", category="World",
                        published_at=NOW - timedelta(hours=1)) for i in range(1, n + 1)]
        self.db.create_run(run_id)
        self.db.save_discovered(run_id, arts)
        self.db.save_evaluations(run_id, [Evaluation(a.id, 80, 90, 90, 80, 90 - i, "Recommended", "ok")
                                          for i, a in enumerate(arts)])
        for i, a in enumerate(arts, 1):
            self.db.save_post(run_id, a.id, {"headline": f"Headline {i}", "caption": f"Caption {i}.",
                                             "hashtags": ["#World", "#News"], "attribution": f"Source: BBC News – {a.url}",
                                             "image_text": "x", "visual": "photo", "status": "draft"}, "gemini-test")
            png = self.root / f"t{i}.png"
            Image.new("RGBA", (1080, 1080), (10, 20, 30, 255)).save(png)
            kind = "photo" if i == 1 else "graphic"
            self.db.save_image(run_id, a.id, {"kind": kind, "path": str(png),
                                              "credit": "File photo: Jane / Wikimedia Commons, CC BY 4.0" if i == 1 else None})
        return [a.id for a in arts]

    def publisher(self, graph=None, store=None):
        self.graph, self.store = graph or FakeGraph(), store or FakeStore()
        return InstagramPublisher(self.settings, self.db, store=self.store, session=self.graph, sleep=lambda s: None)

    def test_publishes_with_jpeg_upload_container_wait_and_permalink(self):
        result = self.publisher().publish("r1", self.ids[:2])
        self.assertEqual([link for _, link in result.posted],
                         ["https://www.instagram.com/p/POST1/", "https://www.instagram.com/p/POST2/"])
        jpeg = next(iter(self.store.objects.values()))
        self.assertEqual(jpeg[:3], b"\xff\xd8\xff")  # a JPEG, converted from the PNG template
        create = next(d for m, p, d in self.graph.calls if p.endswith("/media"))
        self.assertTrue(create["image_url"].startswith("https://bucket.s3.amazonaws.com/posts/r1/"))
        self.assertIn("File photo: Jane / Wikimedia Commons, CC BY 4.0", create["caption"])
        self.assertEqual(len(self.store.deleted), 2)  # uploads cleaned up after posting
        self.assertEqual(self.graph.headers["Authorization"], "Bearer tok")  # token never in the URL
        self.assertFalse(any("tok" in p for _, p, _ in self.graph.calls))

    def test_never_posts_the_same_story_twice(self):
        self.publisher().publish("r1", [self.ids[0]])
        again = self.publisher().publish("r1", [self.ids[0]])
        self.assertEqual(again.posted, [])
        self.assertEqual(again.skipped, [(self.ids[0], "already posted")])

    def test_daily_limit(self):
        self.settings = replace(self.settings, max_posts_per_day=1)
        result = self.publisher().publish("r1", self.ids)
        self.assertEqual(len(result.posted), 1)
        self.assertTrue(all("daily limit" in why for _, why in result.skipped))

    def test_bad_token_stops_and_processing_error_is_reported(self):
        result = self.publisher(FakeGraph(fail=("/media", 190))).publish("r1", self.ids)
        self.assertEqual(len(result.failed), 3)
        self.assertEqual(sum(1 for c in self.graph.calls if c[1].endswith("/media")), 1)  # stopped after the first
        result = self.publisher(FakeGraph(statuses=("ERROR",))).publish("r1", [self.ids[1]])
        self.assertIn("could not process", result.failed[0][1])

    def test_timeout_after_instagram_published_counts_as_posted_once(self):
        result = self.publisher(FakeGraph(publish_timeout="after_publishing")).publish("r1", [self.ids[1]])
        self.assertEqual(result.posted, [(self.ids[1], "https://www.instagram.com/p/POST1/")])
        self.assertEqual(len(self.graph.live), 1)
        self.assertEqual(self.db.publication(self.ids[1], "instagram")["status"], "published")

    def test_timeout_before_publishing_is_a_failure_then_retried_next_cycle(self):
        graph = FakeGraph(publish_timeout="before_publishing")
        result = self.publisher(graph).publish("r1", [self.ids[1]])
        self.assertEqual(len(result.failed), 1)
        self.assertEqual(graph.live, [])
        self.db.set_post_approval([self.ids[1]], "approved")
        pub = InstagramPublisher(self.settings, self.db, store=self.store, session=graph, sleep=lambda s: None)
        again = pub.retry_failed()
        self.assertEqual(len(again.posted), 1)
        self.assertEqual(len(graph.live), 1)

    def test_retry_finds_a_post_that_went_live_instead_of_posting_again(self):
        graph = FakeGraph()
        pub = self.publisher(graph)
        pub.publish("r1", [self.ids[1]])
        self.db.save_publication(self.ids[1], "instagram", "failed", error="ReadTimeout")  # what we wrongly recorded
        self.db.set_post_approval([self.ids[1]], "approved")
        result = pub.retry_failed()
        self.assertEqual([link for _, link in result.posted], ["https://www.instagram.com/p/POST1/"])
        self.assertEqual(len(graph.live), 1)  # not posted twice
        self.assertEqual(self.db.publication(self.ids[1], "instagram")["status"], "published")

    def test_retries_stop_after_three_attempts(self):
        self.db.set_post_approval([self.ids[1]], "approved")
        for _ in range(3):
            self.db.save_publication(self.ids[1], "instagram", "failed", error="boom")
        self.assertEqual(self.db.failed_publications("instagram", 3, 6), [])

    def test_old_failures_are_not_retried(self):
        self.db.set_post_approval([self.ids[1]], "approved")
        self.db.save_publication(self.ids[1], "instagram", "failed", error="no credits")
        self.assertEqual(len(self.db.failed_publications("instagram", 3, 6)), 1)
        self.db.conn.execute("UPDATE publications SET updated_at = '2026-01-01T00:00:00+00:00'")
        self.assertEqual(self.db.failed_publications("instagram", 3, 6), [])

    def test_transient_read_errors_are_retried(self):
        result = self.publisher(FakeGraph(flaky_gets=2)).publish("r1", [self.ids[0]])
        self.assertEqual(len(result.posted), 1)

    def test_caption_respects_instagram_limits(self):
        p = {"headline": "H", "caption": "c" * 3000, "hashtags": '["#a"]', "attribution": "Source: X",
             "image_kind": "graphic", "image_credit": None}
        self.assertLessEqual(len(instagram_caption(p)), 2200)

    def test_whatsapp_approval_posts_and_replies_with_links(self):
        pub = self.publisher()
        service = ApprovalService(self.settings, self.db, OutboxClient(self.db),
                                  designer=lambda run_id, ids: NS(photos=0, graphics=len(ids), failed=0),
                                  publishers={"Instagram": pub.publish})
        service.request("r1")
        self.assertEqual(service.handle_message(Incoming("m1", "919800000001", "1 2", 0)), "approved 1, 2, posted 2")
        reply = [r[0] for r in self.db.conn.execute("SELECT body FROM wa_outbox ORDER BY id")][-1]
        self.assertIn("Posted 2 to Instagram", reply)
        self.assertIn("1. https://www.instagram.com/p/POST1/", reply)


if __name__ == "__main__":
    unittest.main()
