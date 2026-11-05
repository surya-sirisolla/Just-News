"""Offline tests for Agent 5 (Instagram publisher): fake Graph API and fake S3, no network."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

from PIL import Image

from justnews.agents.publisher import (InstagramPublisher, carousel_caption, instagram_caption, merge_hashtags,
                                      story_groups)
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

    def put(self, key, data, content_type="image/jpeg"):
        self.objects[key] = data
        self.types = {**getattr(self, "types", {}), key: content_type}
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
        self.containers = {}    # container id -> what was sent
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
            self.captions[f"container{self.n}"] = data.get("caption")
            self.containers[f"container{self.n}"] = dict(data)
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
                self.live.append((f"media{cid[9:]}", self.captions[cid], f"https://www.instagram.com/p/POST{cid[9:]}/"))
            if mode:
                import requests
                raise requests.ReadTimeout("Read timed out. (read timeout=60)")
            body = {"id": f"media{cid[9:]}"}
        elif path == IG:
            body = {"username": "just_newzzzz"}
        else:
            body = {"permalink": f"https://www.instagram.com/p/POST{path[5:]}/"}
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

    def test_a_single_story_is_a_normal_post(self):
        result = self.publisher().publish("r1", self.ids[:1])
        self.assertEqual([link for _, link in result.posted], ["https://www.instagram.com/p/POST1/"])
        jpeg = next(iter(self.store.objects.values()))
        self.assertEqual(jpeg[:3], b"\xff\xd8\xff")  # a JPEG, converted from the PNG template
        create = next(d for m, p, d in self.graph.calls if p.endswith("/media"))
        self.assertTrue(create["image_url"].startswith("https://bucket.s3.amazonaws.com/posts/r1/"))
        self.assertIn("File photo: Jane / Wikimedia Commons, CC BY 4.0", create["caption"])
        self.assertEqual(len(self.store.deleted), 1)  # upload cleaned up after posting
        self.assertEqual(self.graph.headers["Authorization"], "Bearer tok")  # token never in the URL
        self.assertFalse(any("tok" in p for _, p, _ in self.graph.calls))

    def test_stories_approved_together_go_out_as_one_carousel(self):
        result = self.publisher().publish("r1", self.ids)
        children = {c: d for c, d in self.graph.containers.items() if d.get("is_carousel_item") == "true"}
        parent = next(d for d in self.graph.containers.values() if d.get("media_type") == "CAROUSEL")
        self.assertEqual(len(children), 4)  # story 1 as the cover, stories 2-3, closing: no story twice
        self.assertEqual(parent["children"].split(","), list(children))
        self.assertIn("1. Headline 1\n2. Headline 2\n3. Headline 3", parent["caption"])
        self.assertIn("Photos: File photo: Jane / Wikimedia Commons, CC BY 4.0", parent["caption"])
        self.assertEqual(sum(1 for _, p, _ in self.graph.calls if p.endswith("media_publish")), 1)
        self.assertEqual(len({link for _, link in result.posted}), 1)  # all three: the same post
        self.assertEqual([a for a, _ in result.posted], self.ids)
        slide = Image.open(__import__("io").BytesIO(next(iter(self.store.objects.values()))))
        self.assertEqual(slide.size, (1080, 1350))
        self.assertEqual(len(self.store.deleted), 4)
        again = self.publisher().publish("r1", self.ids)
        self.assertEqual([why for _, why in again.skipped], ["already posted"] * 3)

    def test_a_story_with_a_photo_leads_the_carousel(self):
        photo = self.root / "photo.jpg"
        Image.new("RGB", (1600, 1200), (200, 180, 160)).save(photo)
        self.db.conn.execute("UPDATE images SET photo_path = ? WHERE article_id = ?", (str(photo), self.ids[2]))
        self.db.conn.commit()
        result = self.publisher().publish("r1", self.ids)
        self.assertEqual([a for a, _ in result.posted], [self.ids[2], self.ids[0], self.ids[1]])
        parent = next(d for d in self.graph.containers.values() if d.get("media_type") == "CAROUSEL")
        self.assertTrue(parent["caption"].split("\n\n")[1].startswith("1. Headline 3"))

    def test_carousel_is_found_live_after_a_timeout(self):
        graph = FakeGraph(publish_timeout="after_publishing")
        result = self.publisher(graph).publish("r1", self.ids[:2])
        self.assertEqual(len(result.posted), 2)
        self.assertEqual(len(graph.live), 1)
        self.db.save_publication(self.ids[0], "instagram", "failed", error="ReadTimeout")
        self.db.set_post_approval([self.ids[0]], "approved")
        again = self.publisher(graph).retry_failed()  # reconciled by its numbered headline, not posted again
        self.assertEqual(len(again.posted), 1)
        self.assertEqual(len(graph.live), 1)

    def test_a_weak_story_with_a_photo_does_not_lead(self):
        photo = self.root / "photo.jpg"
        Image.new("RGB", (1600, 1200), (200, 180, 160)).save(photo)
        self.db.conn.execute("UPDATE images SET photo_path = ? WHERE article_id = ?", (str(photo), self.ids[2]))
        self.db.conn.execute("UPDATE evaluations SET overall = 50 WHERE article_id = ?", (self.ids[2],))
        self.db.conn.commit()
        result = self.publisher().publish("r1", self.ids)
        self.assertEqual(result.posted[0][0], self.ids[0])  # the top story keeps the cover

    def test_breaking_only_for_new_and_major_lead_stories(self):
        pub = self.publisher()
        recent = (NOW - timedelta(minutes=20)).isoformat()
        self.assertEqual(pub._cover_label({"published_at": recent, "overall": 90}), "BREAKING NEWS")
        self.assertEqual(pub._cover_label({"published_at": recent, "overall": 70}), "LATEST")  # new, not major
        self.assertEqual(pub._cover_label({"published_at": (NOW - timedelta(hours=3)).isoformat(), "overall": 95}),
                         "LATEST")

    def test_rupee_sign_never_prints_as_a_box(self):
        from justnews.graphics import Fonts, drawable
        font = Fonts().get(40)
        text = drawable("BMC's ₹11.28-crore audit", font)
        self.assertTrue(text == "BMC's ₹11.28-crore audit" or text == "BMC's Rs 11.28-crore audit")
        self.assertNotEqual(font.getmask(text[6]).getbbox(), font.getmask(chr(0xE0001)).getbbox())

    def test_grouping_hashtags_and_caption(self):
        self.assertEqual([len(g) for g in story_groups(list("abcdefghij"), 8)], [5, 5])
        self.assertEqual([len(g) for g in story_groups(list("abcdefgh"), 8)], [8])
        self.assertEqual([len(g) for g in story_groups(list("abcdefghi"), 8)], [5, 4])
        self.assertEqual(merge_hashtags([["#USMidterms", "#Trump"], ["#Elections", "#USMidterms"], ["#Apple"]]),
                         ["#USMidterms", "#Elections", "#Apple", "#Trump"])  # shared first, then each story's best
        posts = self.db.posts_for_run("r1")
        self.assertTrue(carousel_caption(posts).startswith("3 stories this hour. Swipe through:"))

    def test_never_posts_the_same_story_twice(self):
        self.publisher().publish("r1", [self.ids[0]])
        again = self.publisher().publish("r1", [self.ids[0]])
        self.assertEqual(again.posted, [])
        self.assertEqual(again.skipped, [(self.ids[0], "already posted")])

    def test_daily_limit_counts_a_carousel_as_one_post(self):
        self.settings = replace(self.settings, max_posts_per_day=1)
        result = self.publisher().publish("r1", self.ids[:2])
        self.assertEqual(len(result.posted), 2)  # one carousel
        result = self.publisher().publish("r1", self.ids[2:])
        self.assertTrue(result.skipped and all("daily limit" in why for _, why in result.skipped))

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

    def test_account_problems_are_blocked_not_retried(self):
        self.publisher(FakeGraph(fail=("/media", 190))).publish("r1", [self.ids[1]])  # bad token
        self.db.set_post_approval([self.ids[1]], "approved")
        self.assertEqual(self.db.publication(self.ids[1], "instagram")["status"], "blocked")
        self.assertEqual(self.db.failed_publications("instagram", 3, 2), [])
        graph = FakeGraph()
        self.assertEqual(self.publisher(graph).retry_failed().posted, [])  # fixed token: still not posted late
        self.assertEqual(graph.live, [])

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
        self.assertIn("Posted 1 and 2 to Instagram as one carousel:\nhttps://www.instagram.com/p/POST4/", reply)


if __name__ == "__main__":
    unittest.main()
