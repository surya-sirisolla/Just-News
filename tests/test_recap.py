"""The daily recap Reel: which stories, when, posted once. Real rendering (shortened), fake Instagram."""

from __future__ import annotations

import os
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

    def test_todays_stories_once_each_most_important_first(self):
        self.published(self.ids[:2])
        self.published(self.ids[:1], platform="x")          # on both platforms: counted once
        yesterday = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds")
        self.published(self.ids[2:], when=yesterday)        # not today
        rows = self.db.published_between(*recap.day_bounds(self.day, "Asia/Kolkata"))
        self.assertEqual([r["article_id"] for r in rows], self.ids[:2])
        self.assertEqual([r["headline"] for r in recap.pick(rows, 1)], ["Headline 2"])  # the best one
        picked = recap.pick(rows, 5)
        self.assertEqual([r["headline"] for r in picked], ["Headline 2", "Headline 1"])  # most important first
        text = recap.caption(picked, "4 Nov 2026", ["#Delhi"])
        self.assertTrue(text.startswith("Today's top 2 stories (4 Nov 2026):\n\n1. Headline 2\n2. Headline 1"))

    def test_a_past_days_recap_does_not_say_today(self):
        self.published(self.ids[:2])
        rows = self.db.published_between(*recap.day_bounds(self.day, "Asia/Kolkata"))
        self.assertTrue(recap.caption(rows, "4 Nov 2026", [], today=False).startswith("The top 2 stories of 4 Nov 2026:"))

    def test_the_hottest_story_leads_and_the_order_changes_with_the_day(self):
        def row(aid, title, region, overall, interest):
            return {"article_id": aid, "title": title, "headline": title, "region": region, "overall": overall,
                    "interest": interest, "posted_at": "2026-11-04T10:00:00+00:00", "posted_seq": 0}
        rows = [row("a", "Cabinet approves package", "India", 75, 53),
                row("b", "AI godfather warns about AI", "International", 80, 85),
                row("c", "US midterm election results", "International", 72, 50),
                row("d", "Tamil Nadu limits firecrackers", "South India", 73, 72)]
        quiet_day = recap.pick(rows, 4)
        self.assertEqual(quiet_day[0]["title"], "AI godfather warns about AI")  # most interesting leads
        titles = ["US midterm election live", "Midterm election: Democrats lead", "US election results midterm",
                  "Midterm election turnout", "Election day in US midterm"] * 2
        heat = {r["article_id"]: recap.related_count(r["title"], titles) for r in rows}
        self.assertGreater(heat["c"], 5)
        self.assertEqual(recap.pick(rows, 4, heat)[0]["title"], "US midterm election results")  # the day's hot topic
        self.assertEqual(recap.pick([row("x", "Chennai rains", None, 70, 70), row("y", "World", "International", 70, 70)],
                                    2)[0]["title"], "Chennai rains")  # region bonus, worked out from the title

    def test_a_south_indian_story_is_always_in_when_the_day_had_one(self):
        def row(title, region, overall, seq):
            return {"title": title, "headline": title, "region": region, "overall": overall,
                    "posted_at": "2026-11-04T10:00:00+00:00", "posted_seq": seq}
        rows = [row(f"India {i}", "India", 80 - i, i) for i in range(5)] + [row("Karnataka fares", "South India", 50, 9)]
        picked = recap.pick(rows, 5)
        self.assertEqual(len(picked), 5)
        self.assertIn("Karnataka fares", [r["title"] for r in picked])
        self.assertNotIn("India 4", [r["title"] for r in picked])  # the lowest-priority one made room

    def test_story_cards_last_as_long_as_needed_up_to_six_seconds(self):
        def row(caption, photo=None):
            return {"headline": "Delhi AQI hits 350", "category": "India", "source_name": "The Hindu",
                    "caption": caption, "ig_caption": None, "photo_path": photo, "image_credit": None}
        Row = type("Row", (dict,), {"keys": dict.keys})
        full = recap.story(Row(row("The city recorded its worst air in 287 days. Schools will stay shut on "
                                   "Friday. Trucks are banned from entering the city.")), 1, 5, Fonts())
        bare = recap.story(Row(row("Delhi AQI hits 350.")), 1, 5, Fonts())
        self.assertEqual(full.seconds, 6.0)  # three facts: the 6-second maximum
        self.assertEqual(bare.seconds, 4.0)  # headline only: the 4-second minimum
        self.assertEqual(recap.INTRO, 1.0)
        self.assertEqual(recap.reel_tags(["#WorldNews", "#USMidterms", "#indianews", "#Diwali", "#TamilNadu", "#Cricket"]),
                         ["#IndiaNews", "#DailyNews", "#USMidterms", "#Diwali", "#TamilNadu"])

    def test_cover_is_complete_from_the_first_frame(self):
        card = recap.cover(5, "4 November 2026", "Wednesday", Fonts())
        self.assertEqual(card.frame(0).tobytes(), card.frame(recap.INTRO).tobytes())

    def test_facts_are_short_points_without_the_headline_question_or_credit(self):
        row = {"headline": "Delhi AQI hits 350", "source_name": "The Hindu", "caption": "c",
               "ig_caption": "Delhi AQI hits 350.\n\nThe city recorded its worst air in 287 days, according to "
                             "The Hindu.\n\nSchools will stay shut on Friday.\n\nHave you noticed the smog?"}
        row = type("Row", (dict,), {"keys": dict.keys})(row)
        self.assertEqual(recap.facts(row), ["The city recorded its worst air in 287 days.",
                                            "Schools will stay shut on Friday."])

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
                    publish_reel=lambda key, video, caption, cover_at_ms: posted.append((key, caption)) or ("m1", "https://ig/reel/1"))):
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
        with mock.patch.multiple(recap, INTRO=0.4, STORY_MIN=0.4, STORY_MAX=0.4, OUTRO=0.4, FADE=0.1):
            cover = recap.render(rows, "4 Nov 2026", "just_newzzzz", "Just News", Fonts(), out,
                                 music=recap.MUSIC / "calm-keys.m4a")
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

    def test_music_rotates_by_day_and_can_be_named_or_off(self):
        days = ["2026-11-04", "2026-11-05", "2026-11-06", "2026-11-07"]
        self.assertEqual(len({recap.music_for(d, "rotate") for d in days}), 4)
        self.assertEqual(recap.music_for("2026-11-04", "calm-keys").name, "calm-keys.m4a")
        self.assertIsNone(recap.music_for("2026-11-04", "off"))
        with mock.patch.dict(os.environ):  # the default, whatever the server's .env overrides
            os.environ.pop("JUSTNEWS_RECAP_MUSIC", None)
            self.assertEqual(Settings().recap_music, "calm-keys")  # the owner's pick
        cmd = " ".join(recap.encoder_command(Path("o.mp4"), 37.0, recap.MUSIC / "calm-keys.m4a"))
        self.assertIn("-stream_loop -1 -t 37.00 -i", cmd)
        self.assertIn("afade=t=out:st=34.50:d=2.5", cmd)
        self.assertNotIn("anullsrc", cmd)

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
        media_id, link = pub.publish_reel("posts/recaps/2026-11-04", b"video", "caption", cover_at_ms=2000)
        reel = next(d for d in graph.containers.values() if d.get("media_type") == "REELS")
        self.assertEqual(reel["share_to_feed"], "true")
        self.assertTrue(reel["video_url"].endswith("posts/recaps/2026-11-04.mp4?X-Amz-Signature=abc"))
        self.assertEqual(reel["thumb_offset"], "2000")
        self.assertNotIn("cover_url", reel)  # a separate cover image made Instagram reject the Reel
        self.assertEqual(store.types["posts/recaps/2026-11-04.mp4"], "video/mp4")
        self.assertEqual(len(store.deleted), 1)
        self.assertTrue(link.startswith("https://www.instagram.com/p/POST"))

    def write_video(self) -> Path:
        path = self.root / "v.mp4"
        path.write_bytes(b"mp4")
        return path


if __name__ == "__main__":
    unittest.main()
