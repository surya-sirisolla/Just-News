"""Hourly behaviour: skipped shortlists are ignored and replaced; the scheduler keeps running."""

from __future__ import annotations

import threading
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest import mock

from justnews import cli
from justnews.approval import _duration
from justnews.config import Settings
from justnews.quiet import parse, quiet_until
from test_approval import Base

IST = Settings(timezone="Asia/Kolkata", quiet_hours="23-7")


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


class SkipTests(Base):
    def test_unanswered_list_is_replaced_and_never_offered_again(self):
        self.service.request("r1")
        new_ids = self.make_posts(2, run_id="r2")
        self.service.request("r2")  # next hour: r1's list closes unanswered
        old = dict(self.db.conn.execute("SELECT article_id, approval FROM posts").fetchall())
        self.assertTrue(all(old[i] == "requested" for i in self.ids))  # skipped, not approved
        self.assertEqual(self.db.posts_awaiting_approval("r1", 10), [])  # never re-offered
        self.assertEqual(self.say("1"), "approved 1")
        self.assertEqual(self.designed, [[new_ids[0]]])  # numbers refer to the newest list
        self.assertIn("next one comes in about 1 hour", self.outbox()[-3])

    def test_reply_after_skipped_lists_posts_only_from_the_latest_list(self):
        posted = []
        self.service.publishers = {"Instagram": lambda run_id, ids: posted.append((run_id, ids)) or
                                   NS(posted=[(a, "link") for a in ids], failed=[], skipped=[])}
        self.service.request("r1")                          # no reply
        self.make_posts(3, run_id="r2")
        self.service.request("r2")                          # no reply
        latest = self.make_posts(3, run_id="r3")
        self.service.request("r3")
        self.assertEqual(self.say("1 2"), "approved 1, 2, posted 2")
        self.assertEqual(posted, [("r3", latest[:2])])
        approved = [r[0] for r in self.db.conn.execute("SELECT article_id FROM posts WHERE approval = 'approved'")]
        self.assertEqual(sorted(approved), sorted(latest[:2]))
        statuses = [r[0] for r in self.db.conn.execute("SELECT status FROM approval_requests ORDER BY id")]
        self.assertEqual(statuses, ["superseded", "superseded", "answered"])

    def test_last_list_before_quiet_hours_says_when_the_next_one_comes(self):
        self.service.settings = replace(self.settings, quiet_hours="23-7")
        with mock.patch("justnews.approval.datetime") as dt:
            dt.now.return_value = utc("2026-11-04T17:02:00")  # 10:32 PM IST
            dt.fromisoformat = datetime.fromisoformat
            self.service.request("r1")
        self.assertIn("next one comes at 7:00 AM (quiet hours until then)", self.outbox()[-1])

    def test_duration_wording(self):
        self.assertEqual((_duration(60), _duration(120), _duration(30)), ("1 hour", "2 hours", "30 minutes"))


class QuietHoursTests(unittest.TestCase):
    def test_window(self):
        self.assertEqual((parse("23-7"), parse("off"), parse(""), parse("5-5")), ((23, 7), None, None, None))
        cases = {"2026-11-04T17:29:00": None,                     # 10:59 PM IST: still on
                 "2026-11-04T17:30:00": "2026-11-05T01:30:00",    # 11:00 PM IST: quiet until 7 AM
                 "2026-11-04T21:30:00": "2026-11-05T01:30:00",    # 3:00 AM IST
                 "2026-11-05T01:30:00": None}                     # 7:00 AM IST: on again
        for now, until in cases.items():
            got = quiet_until(IST, utc(now))
            self.assertEqual(got and got.isoformat(timespec="seconds")[:19], until, now)
        self.assertIsNone(quiet_until(replace(IST, quiet_hours="off"), utc("2026-11-04T21:30:00")))

    def test_scheduler_sleeps_through_quiet_hours(self):
        stop, calls, waits = threading.Event(), [], []
        clock = iter([utc("2026-11-04T21:30:00"), utc("2026-11-04T21:30:00"), utc("2026-11-05T01:30:00")])

        def cycle(db):
            calls.append(1)
            stop.set()
            return "run"

        with mock.patch.object(cli, "settings", IST), mock.patch.object(cli, "utcnow", lambda: next(clock)), \
                mock.patch.object(cli, "run_cycle", side_effect=cycle), mock.patch.object(cli, "Database"), \
                mock.patch.object(cli, "seconds_until_due", return_value=0), \
                mock.patch.object(stop, "wait", side_effect=waits.append):
            cli.scheduler(60, stop)
        self.assertEqual(waits[0], 4 * 3600)  # 3 AM -> slept until 7 AM, then one cycle
        self.assertEqual(len(calls), 1)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(cli, "settings", replace(cli.settings, quiet_hours="off"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_runs_now_then_on_schedule_and_survives_failures(self):
        stop, calls = threading.Event(), []

        def cycle(db):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("feed down")
            if len(calls) == 3:
                stop.set()
            return "run"

        with mock.patch.object(cli, "run_cycle", side_effect=cycle), \
             mock.patch.object(cli, "Database"), mock.patch.object(cli, "seconds_until_due", return_value=0), \
             mock.patch.object(stop, "wait", side_effect=lambda s: None):
            cli.scheduler(60, stop)
        self.assertEqual(len(calls), 3)

    def test_restart_waits_for_the_next_due_time(self):
        import tempfile
        from pathlib import Path
        from justnews.db import Database
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "t.db")
            self.assertEqual(cli.seconds_until_due(db, 60), 0)  # never ran: run now
            db.create_run("r1")  # a cycle just started
            self.assertGreater(cli.seconds_until_due(db, 60), 59 * 60)
            db.conn.close()


if __name__ == "__main__":
    unittest.main()
