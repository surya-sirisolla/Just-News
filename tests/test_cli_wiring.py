"""Tests for how the CLI connects the agents: which publishers are on, what approvals post to,
the hourly retry, `publish`, and the order of a news cycle. Fake publishers, no network."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

from justnews import cli
from justnews.agents.publisher import PublishResult
from justnews.config import Settings
from justnews.db import Database
from justnews.models import Article, Evaluation
from justnews.whatsapp import Incoming

OWNER = "919800000001"
NOW = datetime.now(timezone.utc)
IG_KEYS = dict(instagram_token="tok", instagram_user_id="1784", s3_bucket="b")
X_KEYS = dict(x_api_key="ck", x_api_secret="cs", x_access_token="42-t", x_access_token_secret="ts")
NO_KEYS = dict(instagram_token="", x_api_key="", whatsapp_owner="")


def fake_publisher(platform, label, calls, retry=None, crash=False):
    class Fake:
        def __init__(self, settings, db):
            self.platform, self.label = platform, label

        def publish(self, run_id, ids):
            calls.append((label, run_id, list(ids)))
            if crash:
                raise RuntimeError("boom")
            return PublishResult(posted=[(a, f"https://{platform}.example/{a[:6]}") for a in ids])

        def retry_failed(self):
            calls.append((label, "retry"))
            if crash:
                raise RuntimeError("boom")
            return retry or PublishResult()
    return Fake


class WiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / "t.db")
        self.calls = []
        arts = [Article(title=f"Story {i}", url=f"https://bbc.co.uk/{i}", source_name="BBC News", category="World",
                        published_at=NOW - timedelta(hours=1)) for i in (1, 2, 3)]
        self.db.create_run("r1")
        self.db.save_discovered("r1", arts)
        self.db.save_evaluations("r1", [Evaluation(a.id, 80, 90, 90, 80, 90 - i, "Recommended", "ok")
                                        for i, a in enumerate(arts)])
        for i, a in enumerate(arts, 1):
            self.db.save_post("r1", a.id, {"headline": f"Headline {i}", "caption": f"Caption {i}.",
                                           "hashtags": ["#World", "#News", "#Story"], "attribution": "Source: BBC News",
                                           "image_text": "x", "visual": "graphic", "status": "draft"}, "gemini-test")
        self.ids = [a.id for a in arts]

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()

    def use(self, ig=None, x=None, **settings):
        """Patch the CLI's settings and publisher classes for one test."""
        s = replace(Settings(), db_path=self.root / "t.db", **{**NO_KEYS, **settings})
        patches = [mock.patch.object(cli, "settings", s),
                   mock.patch.object(cli, "InstagramPublisher", ig or fake_publisher("instagram", "Instagram", self.calls)),
                   mock.patch.object(cli, "XPublisher", x or fake_publisher("x", "X", self.calls))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return s

    def outbox(self):
        return [r["body"] for r in self.db.conn.execute("SELECT body FROM wa_outbox ORDER BY id")]

    def test_publishers_follow_the_keys_instagram_first(self):
        self.use()
        self.assertEqual(cli.make_publishers(self.db), [])
        self.use(**IG_KEYS)
        self.assertEqual([p.label for p in cli.make_publishers(self.db)], ["Instagram"])
        self.use(**X_KEYS)
        self.assertEqual([p.label for p in cli.make_publishers(self.db)], ["X"])
        self.use(**IG_KEYS, **X_KEYS)
        self.assertEqual([p.label for p in cli.make_publishers(self.db)], ["Instagram", "X"])

    def test_whatsapp_approval_posts_to_each_platform_with_its_own_publisher(self):
        self.use(**IG_KEYS, **X_KEYS, whatsapp_owner=OWNER)
        service = cli.approval_service(self.db)
        service.designer = lambda run_id, ids: NS(photos=0, graphics=len(ids), failed=0)
        self.assertEqual(list(service.publishers), ["Instagram", "X"])
        service.request("r1")
        outcome = service.handle_message(Incoming("m1", OWNER, "1 3", 0))
        self.assertEqual(outcome, "approved 1, 3, posted 4")
        self.assertEqual(self.calls, [("Instagram", "r1", [self.ids[0], self.ids[2]]),
                                      ("X", "r1", [self.ids[0], self.ids[2]])])
        reply = self.outbox()[-1]
        self.assertIn("Posted 2 to Instagram", reply)
        self.assertIn("Posted 2 to X", reply)

    def test_a_crash_on_one_platform_still_reports_the_other(self):
        self.use(x=fake_publisher("x", "X", self.calls, crash=True), **IG_KEYS, **X_KEYS, whatsapp_owner=OWNER)
        service = cli.approval_service(self.db)
        service.designer = lambda run_id, ids: NS(photos=0, graphics=len(ids), failed=0)
        service.request("r1")
        self.assertEqual(service.handle_message(Incoming("m1", OWNER, "2", 0)), "approved 2, posted 1")
        reply = self.outbox()[-1]
        self.assertIn("Posted 1 to Instagram", reply)
        self.assertIn("Not posted on X: RuntimeError", reply)

    def test_without_publishers_templates_are_made_but_not_posted(self):
        self.use(whatsapp_owner=OWNER)
        service = cli.approval_service(self.db)
        service.designer = lambda run_id, ids: NS(photos=0, graphics=len(ids), failed=0)
        service.request("r1")
        self.assertEqual(service.handle_message(Incoming("m1", OWNER, "1", 0)), "approved 1")
        self.assertIn("Posting isn't connected", self.outbox()[-1])
        self.assertEqual(self.calls, [])

    def test_hourly_retry_runs_per_platform_and_tells_the_owner(self):
        found = PublishResult(posted=[(self.ids[0], "https://x.com/i/status/1")])
        self.use(ig=fake_publisher("instagram", "Instagram", self.calls, crash=True),
                 x=fake_publisher("x", "X", self.calls, retry=found), **IG_KEYS, **X_KEYS, whatsapp_owner=OWNER)
        cli.retry_failed_posts(self.db)
        self.assertEqual(self.calls, [("Instagram", "retry"), ("X", "retry")])  # a crash doesn't stop X
        self.assertEqual(self.outbox(), ["Update: 1 earlier post is now confirmed on X ✅\nhttps://x.com/i/status/1"])

    def test_publish_command_posts_only_approved_stories_with_templates(self):
        self.use(**IG_KEYS, **X_KEYS)
        self.db.set_post_approval([self.ids[0], self.ids[1]], "approved")
        self.db.save_image("r1", self.ids[0], {"kind": "graphic", "path": str(self.root / "t1.png")})
        out = io.StringIO()
        with redirect_stdout(out):
            cli.cmd_publish(self.db, NS(run_id="r1"))
        printed = json.loads(out.getvalue())
        self.assertEqual(set(printed), {"run_id", "instagram", "x"})
        self.assertEqual(self.calls, [("Instagram", "r1", [self.ids[0]]), ("X", "r1", [self.ids[0]])])

    def test_publish_command_needs_keys(self):
        self.use()
        with self.assertRaises(SystemExit):
            cli.cmd_publish(self.db, NS(run_id="r1"))


if __name__ == "__main__":
    unittest.main()
