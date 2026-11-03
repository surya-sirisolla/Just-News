"""Hourly behaviour: skipped shortlists are ignored and replaced; the scheduler keeps running."""

from __future__ import annotations

import threading
import unittest
from unittest import mock

from justnews import cli
from justnews.approval import _duration
from test_approval import Base


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

    def test_duration_wording(self):
        self.assertEqual((_duration(60), _duration(120), _duration(30)), ("1 hour", "2 hours", "30 minutes"))


class SchedulerTests(unittest.TestCase):
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
