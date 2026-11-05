"""Offline tests for Agent 5 on X: a fake X API, no network."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from types import SimpleNamespace as NS

import requests
from PIL import Image

from justnews.agents.x_publisher import NO_CREDITS, XPublisher, oauth1_header, x_length, x_text
from justnews.approval import ApprovalService
from justnews.config import Settings
from justnews.db import Database
from justnews.models import Article, Evaluation
from justnews.whatsapp import Incoming, OutboxClient

NOW = datetime.now(timezone.utc)
OWNER = "919800000001"


class FakeX:
    """Answers like X API v2. `fail` = (path fragment, status, body) makes matching calls fail."""

    def __init__(self, fail=None, post_timeout=None):
        self.calls, self.live, self.fail = [], [], fail
        self.post_timeout = post_timeout  # None, "after_posting" or "before_posting"

    def request(self, method, url, headers=None, params=None, timeout=None, **kw):
        path = url.split("/2/", 1)[1]
        self.calls.append((method, path, headers, kw))
        if self.fail and self.fail[0] in path:
            return NS(status_code=self.fail[1], content=b"x", json=lambda: self.fail[2])
        if path == "media/upload":
            body = {"data": {"id": f"m{len(self.calls)}", "media_key": "3_1"}}
        elif path == "tweets":
            text = kw["json"]["text"]
            if any(t["raw"] == text for t in self.live):
                return NS(status_code=403, content=b"x", json=lambda: {
                    "detail": "You are not allowed to create a Tweet with duplicate content.", "status": 403})
            mode, self.post_timeout = self.post_timeout, None
            pid = str(1000 + len(self.live))
            if mode != "before_posting":
                # X returns text HTML-escaped and with the image's t.co link appended
                self.live.append({"id": pid, "raw": text, "text": escape(text, quote=False) + " https://t.co/abc123"})
            if mode:
                raise requests.ReadTimeout("Read timed out.")
            body = {"data": {"id": pid, "text": text}}
        elif path.startswith("users/") and path.endswith("/tweets"):
            body = {"data": [{"id": t["id"], "text": t["text"]} for t in reversed(self.live)]}
        else:
            body = {}
        return NS(status_code=201 if method == "POST" else 200, content=b"x", json=lambda: body)


def post_row(**kw):
    row = {"headline": "Apple opens its first store in Pune", "caption": "The store opens on Friday with 80 staff.",
           "hashtags": json.dumps(["#Apple", "#India", "#Retail", "#Tech"]), "source_name": "Reuters.com",
           "image_kind": "graphic", "image_credit": None}
    return {**row, **kw}


class TextTests(unittest.TestCase):
    def test_x_has_its_own_smaller_daily_cap(self):
        from justnews.config import Settings
        from justnews.agents.publisher import InstagramPublisher
        s = Settings()
        self.assertEqual((s.x_max_posts_per_day, s.max_posts_per_day), (15, 20))
        self.assertEqual(XPublisher.daily_cap.fget(NS(settings=s)), 15)
        self.assertEqual(InstagramPublisher.daily_cap.fget(NS(settings=s)), 20)

    def test_text_has_source_and_no_links(self):
        text = x_text(post_row(caption="Details at https://example.com/x and www.example.org today."))
        self.assertIn("Source: Reuters", text)
        self.assertNotIn("http", text)
        self.assertNotIn(".com", text)
        self.assertNotIn("www.", text)
        self.assertNotIn("#", text)  # like a newspaper's feed: no hashtags unless the story has an established one
        tagged = x_text(post_row(x_post="Voting began in the US midterm elections on Tuesday.", x_hashtag="#USMidterms"))
        self.assertEqual(tagged, "Voting began in the US midterm elections on Tuesday. #USMidterms\n\nSource: Reuters")

    def test_long_text_drops_caption_then_hashtags_but_keeps_credit(self):
        credit = "File photo: Jane Doe / Wikimedia Commons, CC BY-SA 4.0"
        text = x_text(post_row(caption="c " * 200, image_kind="photo", image_credit=credit))
        self.assertLessEqual(x_length(text), 280)
        self.assertIn(credit, text)
        self.assertNotIn("c c c", text)
        text = x_text(post_row(headline="Long headline " * 30, image_kind="photo", image_credit=credit))
        self.assertLessEqual(x_length(text), 280)
        self.assertTrue(text.endswith(credit))

    def test_x_counts_emoji_and_cjk_double(self):
        self.assertEqual(x_length("abc"), 3)
        self.assertEqual(x_length("日本"), 4)
        self.assertEqual(x_length("✅"), 2)

    def test_oauth1_signature_matches_known_example(self):
        # Example from X's "Creating a signature" documentation.
        import justnews.agents.x_publisher as xp
        orig_time, orig_nonce = xp.time.time, xp.secrets.token_hex
        xp.time.time, xp.secrets.token_hex = lambda: 1318622958, lambda n: "kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg"
        try:
            header = oauth1_header(
                "POST", "https://api.twitter.com/1.1/statuses/update.json",
                ("xvz1evFS4wEEPTGEFPHBog", "kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw"),
                ("370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb", "LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE"),
                params={"include_entities": "true",
                        "status": "Hello Ladies + Gentlemen, a signed OAuth request!"})
        finally:
            xp.time.time, xp.secrets.token_hex = orig_time, orig_nonce
        self.assertIn('oauth_signature="hCtSmYh%2BiHYCEqBWrE7C7hYmtUk%3D"', header)


class XPublisherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings = replace(Settings(), db_path=self.root / "t.db", x_max_posts_per_day=10, whatsapp_owner=OWNER,
                                x_api_key="ck", x_api_secret="cs", x_access_token="42-tok", x_access_token_secret="ts",
                                instagram_token="")
        self.db = Database(self.settings.db_path)
        arts = [Article(title=f"Story {i}", url=f"https://bbc.co.uk/{i}", source_name="BBC News", category="World",
                        published_at=NOW - timedelta(hours=1)) for i in (1, 2, 3)]
        self.db.create_run("r1")
        self.db.save_discovered("r1", arts)
        self.db.save_evaluations("r1", [Evaluation(a.id, 80, 90, 90, 80, 90 - i, "Recommended", "ok")
                                        for i, a in enumerate(arts)])
        for i, a in enumerate(arts, 1):
            self.db.save_post("r1", a.id, {"headline": f"Headline {i}", "caption": f"Caption {i} & more.",
                                           "hashtags": ["#World", "#News"], "attribution": f"Source: BBC News – {a.url}",
                                           "image_text": "x", "visual": "graphic", "status": "draft"}, "gemini-test")
            png = self.root / f"t{i}.png"
            Image.new("RGBA", (1080, 1080), (10, 20, 30, 255)).save(png)
            self.db.save_image("r1", a.id, {"kind": "photo" if i == 1 else "graphic", "path": str(png),
                                            "credit": "File photo: Jane / Wikimedia Commons, CC0" if i == 1 else None})
        self.ids = [a.id for a in arts]

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()

    def publisher(self, x=None, spacing=0, clock=None):
        self.x = x or FakeX()
        return XPublisher(self.settings, self.db, session=self.x, sleep=lambda s: None, spacing=spacing, clock=clock)

    def test_stories_are_queued_and_posted_one_every_few_minutes(self):
        t0 = datetime(2026, 11, 4, 4, 0, tzinfo=timezone.utc)  # 9:30 AM IST
        now = [t0]
        pub = self.publisher(spacing=5, clock=lambda: now[0])
        result = pub.publish("r1", self.ids)
        self.assertEqual([w for _, w in result.queued],
                         ["2026-11-04T04:00:00+00:00", "2026-11-04T04:05:00+00:00", "2026-11-04T04:10:00+00:00"])
        self.assertEqual(result.note, "one every 5 minutes, 9:30 AM to 9:40 AM")
        self.assertEqual(result.posted, [])
        self.assertEqual(pub.post_due(), [])  # the first is posted; the batch isn't finished yet
        self.assertEqual(len(self.x.live), 1)
        now[0] = t0 + timedelta(minutes=4)
        self.assertEqual(pub.post_due(), [])
        self.assertEqual(len(self.x.live), 1)  # not yet time for the second
        later = self.publisher(x=self.x, spacing=5, clock=lambda: now[0]).publish("r1", [])  # nothing new
        self.assertEqual(later.queued, [])
        now[0] = t0 + timedelta(minutes=10)
        (batch, done), = pub.post_due()
        self.assertEqual(len(done.posted), 3)
        self.assertEqual([a for a, _ in done.posted], self.ids)  # in queue order
        self.assertEqual(len(self.x.live), 3)

    def test_a_second_approval_queues_after_the_first(self):
        t0 = datetime(2026, 11, 4, 4, 0, tzinfo=timezone.utc)
        pub = self.publisher(spacing=5, clock=lambda: t0)
        pub.publish("r1", self.ids[:2])
        second = pub.publish("r1", [self.ids[2]])
        self.assertEqual(second.queued[0][1], "2026-11-04T04:10:00+00:00")

    def test_stale_queue_items_are_dropped_not_posted(self):
        t0 = datetime(2026, 11, 4, 4, 0, tzinfo=timezone.utc)
        now = [t0]
        pub = self.publisher(spacing=5, clock=lambda: now[0])
        pub.publish("r1", [self.ids[0]])
        now[0] = t0 + timedelta(hours=4)  # e.g. the server was down
        (_, done), = pub.post_due()
        self.assertEqual(self.x.live, [])
        self.assertIn("not posted within 3 hours", done.failed[0][1])
        self.assertEqual(self.db.publication(self.ids[0], "x")["status"], "expired")
        self.assertEqual(self.db.failed_publications("x", 3, 2), [])  # and not retried later

    def test_daily_limit_applies_to_the_queue(self):
        self.settings = replace(self.settings, x_max_posts_per_day=1)
        result = self.publisher().publish("r1", self.ids[:2])
        self.assertEqual(len(result.posted), 1)
        self.assertIn("daily limit", result.failed[0][1])

    def test_daily_limit_resets_at_midnight_not_after_24_hours(self):
        pub = XPublisher(replace(self.settings, timezone="Asia/Kolkata"), self.db)
        ids = [r["id"] for r in self.db.conn.execute("SELECT id FROM articles")]
        for aid, at in zip(ids, ["2026-11-04T16:00:00+00:00",    # 9:30 PM IST yesterday
                                 "2026-11-04T18:45:00+00:00",    # 12:15 AM IST today
                                 "2026-11-05T05:00:00+00:00"]):  # 10:30 AM IST today
            self.db.conn.execute("INSERT INTO publications(article_id, platform, status, updated_at) "
                                 "VALUES (?, 'x', 'published', ?)", (aid, at))
        self.db.conn.commit()
        noon = datetime(2026, 11, 5, 7, 48, tzinfo=timezone.utc)  # 1:18 PM IST
        self.assertEqual(pub.posted_today(noon), 2)  # yesterday evening's post no longer counts

    def test_a_photo_goes_up_with_the_post_and_a_text_template_does_not(self):
        result = self.publisher().publish("r1", self.ids[:2])
        self.assertEqual([link for _, link in result.posted],
                         ["https://x.com/i/status/1000", "https://x.com/i/status/1001"])
        upload = self.x.calls[0]
        self.assertEqual(upload[1], "media/upload")
        self.assertEqual(upload[3]["files"]["media"][1][:3], b"\xff\xd8\xff")  # JPEG
        self.assertEqual(upload[3]["data"], {"media_category": "tweet_image"})
        post = self.x.calls[1][3]["json"]
        self.assertEqual(post["media"], {"media_ids": ["m1"]})
        text_only = self.x.calls[2]  # story 2 has only a text template: posted as text, nothing uploaded
        self.assertEqual(text_only[1], "tweets")
        self.assertNotIn("media", text_only[3]["json"])
        self.assertEqual(sum(1 for c in self.x.calls if c[1] == "media/upload"), 1)
        self.assertIn("Source: BBC News", post["text"])
        self.assertNotIn("http", post["text"])
        self.assertTrue(all(c[2]["Authorization"].startswith("OAuth ") for c in self.x.calls))
        self.assertEqual(self.db.publication(self.ids[0], "x")["status"], "published")

    def test_no_credits_stops_with_a_clear_reason(self):
        body = {"title": "Client Forbidden", "reason": "client-not-enrolled", "detail": "attach to a Project"}
        result = self.publisher(FakeX(fail=("tweets", 403, body))).publish("r1", self.ids)
        self.assertEqual(result.failed[0][1], NO_CREDITS)
        self.assertEqual(len(result.failed), 3)
        self.assertEqual(sum(1 for c in self.x.calls if c[1] == "tweets"), 1)  # stopped after the first

    def test_timeout_after_posting_counts_as_posted_once(self):
        result = self.publisher(FakeX(post_timeout="after_posting")).publish("r1", [self.ids[0]])
        self.assertEqual(result.posted, [(self.ids[0], "https://x.com/i/status/1000")])
        self.assertEqual(len(self.x.live), 1)

    def test_timeout_before_posting_fails_then_retry_posts_once(self):
        x = FakeX(post_timeout="before_posting")
        self.assertEqual(len(self.publisher(x).publish("r1", [self.ids[0]]).failed), 1)
        self.db.set_post_approval([self.ids[0]], "approved")
        again = self.publisher(x).retry_failed()
        self.assertEqual(len(again.posted), 1)
        self.assertEqual(len(x.live), 1)

    def test_retry_reconciles_a_post_that_is_live(self):
        x = FakeX()
        self.publisher(x).publish("r1", [self.ids[0]])
        self.db.save_publication(self.ids[0], "x", "failed", error="ReadTimeout")
        self.db.set_post_approval([self.ids[0]], "approved")
        result = self.publisher(x).retry_failed()
        self.assertEqual([link for _, link in result.posted], ["https://x.com/i/status/1000"])
        self.assertEqual(len(x.live), 1)
        self.assertFalse(any(c[1] == "tweets" for c in x.calls[2:]))  # no second post attempt

    def test_whatsapp_reply_lists_each_platform(self):
        ig = NS(publish=lambda run_id, ids: NS(posted=[(a, f"https://www.instagram.com/p/{a[:4]}/") for a in ids],
                                               failed=[], skipped=[]))
        xpub = self.publisher(FakeX(fail=("tweets", 402, {"title": "Payment Required"})))
        service = ApprovalService(self.settings, self.db, OutboxClient(self.db),
                                  designer=lambda run_id, ids: NS(photos=0, graphics=len(ids), failed=0),
                                  publishers={"Instagram": ig.publish, "X": xpub.publish})
        service.request("r1")
        self.assertEqual(service.handle_message(Incoming("m1", OWNER, "1 2", 0)), "approved 1, 2, posted 2")
        reply = [r[0] for r in self.db.conn.execute("SELECT body FROM wa_outbox ORDER BY id")][-1]
        self.assertIn("Posted 2 to Instagram", reply)
        self.assertIn(f"Not posted on X: 1 ({NO_CREDITS})", reply)
        self.assertIn("2 (not attempted after a token/permission error)", reply)


if __name__ == "__main__":
    unittest.main()
