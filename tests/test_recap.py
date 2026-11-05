"""The daily recap Reel: which stories, when, posted once. Real rendering (shortened), fake Instagram."""

from __future__ import annotations

import subprocess
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock
from zoneinfo import ZoneInfo

from PIL import Image

from justnews import cli, recap
from justnews.agents.publisher import InstagramPublisher
from justnews.config import Settings
from justnews.db import Database
from justnews.graphics import Fonts
from justnews.models import Article, Evaluation
from test_publisher import FakeGraph, FakeStore

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime.now(timezone.utc)


class RecapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings = replace(Settings(), db_path=self.root / "t.db", output_dir=self.root / "out",
                                instagram_token="tok", instagram_user_id="17841400000000000", s3_bucket="b",
                                whatsapp_owner="919800000001", timezone="Asia/Kolkata", recap_time="21:30")
        self.db = Database(self.settings.db_path)
        arts = [Article(title=f"Story {i}", url=f"https://thehindu.com/{i}", source_name="The Hindu",
                        category="India", published_at=NOW - timedelta(hours=2), region="India") for i in (1, 2, 3)]
        self.db.create_run("r1")
        self.db.save_discovered("r1", arts)
        self.db.save_evaluations("r1", [Evaluation(a.id, 80, 90, 90, 80, 70 + i, "Recommended", "ok")
                                        for i, a in enumerate(arts)])
        for i, a in enumerate(arts, 1):
            self.db.save_post("r1", a.id, {"headline": f"Headline {i}", "caption": f"Fact {i} happened. More.",
                                           "hashtags": ["#Delhi", "#AirPollution"], "attribution": "Source: The Hindu",
                                           "image_text": f"Short {i}", "visual": "graphic", "status": "draft"}, "m")
            self.db.save_image("r1", a.id, {"kind": "graphic", "path": str(self.root / f"t{i}.png")})
        self.ids = [a.id for a in arts]
        self.day = datetime.now(IST).date().isoformat()
        patch = mock.patch.object(cli, "settings", self.settings)
        patch.start()
        self.addCleanup(patch.stop)

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()

    def published(self, ids, platform="instagram", when=None):
        for aid in ids:
            self.db.save_publication(aid, platform, "published", media_id="m", permalink="https://p")
            if when:
                self.db.conn.execute("UPDATE publications SET updated_at = ? WHERE article_id = ? AND platform = ?",
                                     (when, aid, platform))
        self.db.conn.commit()

    def test_todays_stories_once_each_best_first_shown_in_posting_order(self):
        self.published(self.ids[:2])
        self.published(self.ids[:1], platform="x")          # on both platforms: counted once
        yesterday = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds")
        self.published(self.ids[2:], when=yesterday)        # not today
        rows = self.db.published_between(*recap.day_bounds(self.day, "Asia/Kolkata"))
        self.assertEqual([r["article_id"] for r in rows], self.ids[:2])
        self.assertEqual([r["headline"] for r in recap.pick(rows, 1)], ["Headline 2"])  # the best one
        text = recap.caption(rows, "4 Nov 2026", ["#Delhi"])
        self.assertTrue(text.startswith("Today's news in 2 stories (4 Nov 2026):\n\n1. Headline 1\n2. Headline 2"))

    def test_a_past_days_recap_does_not_say_today(self):
        self.published(self.ids[:2])
        rows = self.db.published_between(*recap.day_bounds(self.day, "Asia/Kolkata"))
        self.assertTrue(recap.caption(rows, "4 Nov 2026", [], today=False).startswith("The news of 4 Nov 2026 in 2 stories:"))

    def test_day_bounds_are_the_local_day_in_utc(self):
        self.assertEqual(recap.day_bounds("2026-11-04", "Asia/Kolkata"),
                         ("2026-11-03T18:30:00+00:00", "2026-11-04T18:30:00+00:00"))

    def test_due_from_recap_time_until_quiet_hours(self):
        at = lambda h, m: datetime(2026, 11, 4, h, m, tzinfo=IST)  # noqa: E731
        self.assertFalse(cli.recap_due(at(21, 29), "21:30"))
        self.assertTrue(cli.recap_due(at(21, 30), "21:30"))
        self.assertTrue(cli.recap_due(at(22, 50), "21:30"))  # e.g. after a restart
        self.assertFalse(cli.recap_due(at(23, 0), "21:30"))
        self.assertFalse(cli.recap_due(at(21, 45), "off"))

    def test_posts_once_a_day_and_tells_the_owner(self):
        self.published(self.ids)
        made, posted = [], []
        with mock.patch.object(cli, "make_recap",
                               side_effect=lambda db, day, out: made.append(day) or (self.db.published_between(
                                   *recap.day_bounds(day, "Asia/Kolkata")), str(self.write_video()), Image.new("RGB", (8, 8)))), \
                mock.patch.object(cli, "InstagramPublisher", lambda s, db: NS(
                    publish_reel=lambda key, video, cover, caption: posted.append((key, caption)) or ("m1", "https://ig/reel/1"))):
            self.assertEqual(cli.post_recap(self.db, self.day), "posted https://ig/reel/1")
            self.assertTrue(cli.post_recap(self.db, self.day).startswith("already done"))
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0][0], f"posts/recaps/{self.day}")  # under posts/, which the server may write
        self.assertTrue(cli.post_recap(self.db, self.day, force=True).startswith("already done"))  # never re-posted
        self.assertIn("1. Headline 1", posted[0][1])
        outbox = [r[0] for r in self.db.conn.execute("SELECT body FROM wa_outbox")]
        self.assertEqual(outbox, ["🎬 Today's recap Reel is up: 3 stories in one video.\nhttps://ig/reel/1"])

    def test_skips_a_quiet_day_and_stops_after_three_failures(self):
        self.published(self.ids[:1])
        self.assertEqual(cli.post_recap(self.db, self.day), "skipped: fewer than two stories posted")
        day = (datetime.fromisoformat(self.day) + timedelta(days=1)).date().isoformat()  # not today
        with mock.patch.object(cli, "make_recap", side_effect=RuntimeError("encoder broke")):
            for _ in range(3):
                cli.post_recap(self.db, day)
        self.assertTrue(self.db.get_state(f"recap:{day}").startswith("failed"))
        self.assertEqual(cli.post_recap(self.db, day), f"already done ({self.db.get_state(f'recap:{day}')})")
        with mock.patch.object(cli, "make_recap", return_value=None):
            self.assertEqual(cli.post_recap(self.db, day, force=True), "skipped: fewer than two stories posted")
        outbox = [r[0] for r in self.db.conn.execute("SELECT body FROM wa_outbox")]
        self.assertEqual(len(outbox), 1)  # one message, after the last attempt
        self.assertIn("couldn't be posted", outbox[0])

    def test_scheduler_thread_posts_when_due(self):
        stop, calls = threading.Event(), []
        evening = datetime.now(IST).replace(hour=21, minute=40).astimezone(timezone.utc)
        with mock.patch.object(cli, "utcnow", lambda: evening), mock.patch.object(cli, "Database", return_value=self.db), \
                mock.patch.object(cli, "post_recap", side_effect=lambda db, day: calls.append(day) or stop.set()):
            cli.daily_recap(stop, every=0)
        self.assertEqual(calls, [evening.astimezone(IST).date().isoformat()])

    def test_renders_a_vertical_video(self):
        self.published(self.ids)
        rows = self.db.published_between(*recap.day_bounds(self.day, "Asia/Kolkata"))
        out = self.root / "recap.mp4"
        with mock.patch.multiple(recap, INTRO=0.4, STORY=0.4, OUTRO=0.4, PUSH=0.1):
            cover = recap.render(rows, "4 Nov 2026", "just_newzzzz", "Just News", Fonts(), out)
        self.assertEqual(cover.size, (1080, 1920))  # the cover image stays full size
        import imageio_ffmpeg
        probe = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-i", str(out)], capture_output=True, text=True).stderr
        self.assertIn("720x1280", probe)
        self.assertIn("h264", probe)
        self.assertIn("aac", probe)

    def test_encoder_memory_is_bounded(self):
        cmd = recap.encoder_command(Path("o.mp4"), 37.0)
        joined = " ".join(cmd)
        for flag in ("-threads 1", "-thread_queue_size 2", "rc-lookahead=5", "-t 37.00", "-s 720x1280"):
            self.assertIn(flag, joined)
        self.assertNotIn("-shortest", cmd)

    def test_a_recap_that_never_finished_is_reported_once(self):
        stop = threading.Event()
        day = datetime.now(IST).date().isoformat()
        self.db.set_state(f"recap:{day}:attempts", "3")  # three attempts, all killed mid-way
        evening = datetime.now(IST).replace(hour=21, minute=40).astimezone(timezone.utc)
        ticks = iter(range(3))
        with mock.patch.object(cli, "utcnow", lambda: evening), mock.patch.object(cli, "Database", return_value=self.db), \
                mock.patch.object(cli, "post_recap") as post, \
                mock.patch.object(stop, "wait", side_effect=lambda s: next(ticks, None) is None and stop.set()):
            cli.daily_recap(stop, every=0)
        post.assert_not_called()
        self.assertTrue(self.db.get_state(f"recap:{day}").startswith("failed: no attempt finished"))
        outbox = [r[0] for r in self.db.conn.execute("SELECT body FROM wa_outbox")]
        self.assertEqual(len(outbox), 1)
        self.assertIn("couldn't be made", outbox[0])

    def test_reel_goes_to_instagram_as_reels_with_a_cover(self):
        graph, store = FakeGraph(), FakeStore()
        pub = InstagramPublisher(self.settings, self.db, store=store, session=graph, sleep=lambda s: None)
        media_id, link = pub.publish_reel("recaps/2026-11-04", b"video", Image.new("RGB", (1080, 1920)), "caption")
        reel = next(d for d in graph.containers.values() if d.get("media_type") == "REELS")
        self.assertEqual(reel["share_to_feed"], "true")
        self.assertTrue(reel["video_url"].endswith("recaps/2026-11-04.mp4?X-Amz-Signature=abc"))
        self.assertIn("cover", reel["cover_url"])
        self.assertEqual(store.types["recaps/2026-11-04.mp4"], "video/mp4")
        self.assertEqual(len(store.deleted), 2)
        self.assertTrue(link.startswith("https://www.instagram.com/p/POST"))

    def write_video(self) -> Path:
        path = self.root / "v.mp4"
        path.write_bytes(b"mp4")
        return path


if __name__ == "__main__":
    unittest.main()
