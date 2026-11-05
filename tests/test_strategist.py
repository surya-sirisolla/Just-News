"""Offline tests for Agent 6 (engagement strategist), its discussion with the content creator, and the
separate Instagram and X versions. Fake models and fake platforms, no network."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

from justnews import cli
from justnews.agents import content as content_mod
from justnews.agents.content import ContentCreatorAgent, _paragraphs, _x_post
from justnews.agents.publisher import instagram_caption
from justnews.agents.strategist import BASE_PLAYBOOK, StrategistAgent, code_notes
from justnews.agents.x_publisher import x_text
from justnews.config import Settings
from justnews.db import Database
from justnews.llm import LLMError, Usage
from justnews.models import Article, Evaluation

NOW = datetime.now(timezone.utc)


def ago(hours: float) -> str:
    return (NOW - timedelta(hours=hours)).isoformat(timespec="seconds")


class FakeModel:
    """Answers the content creator's and strategist's prompts with canned JSON, by prompt type."""

    def __init__(self, answers: dict, model="gemini-test"):
        self.answers, self.model, self.prompts = answers, model, []
        self.usage = Usage(model, 0, 0)

    def generate_json(self, system, prompt, schema):
        self.prompts.append((system, prompt))
        self.usage.calls += 1
        kind = ("revise" if "NOTES" in system else "write" if "write social media posts" in system
                else "review" if "drafts" in system else "playbook")
        answer = self.answers.get(kind)
        if answer is None:
            raise LLMError(f"no answer for {kind}")
        return answer(prompt) if callable(answer) else answer


class FakePlatform:
    def __init__(self, platform, label, metrics=None, benchmark=None):
        self.platform, self.label = platform, label
        self.metric_calls, self.benchmark_calls = [], []
        self._metrics, self._benchmark = metrics or {}, benchmark

    def metrics(self, ids):
        self.metric_calls.append(sorted(ids))
        return {i: self._metrics.get(i, {"likes": 5, "comments": 1}) for i in ids}

    def benchmark(self, accounts, n):
        self.benchmark_calls.append(list(accounts))
        return list(self._benchmark or [])


def draft(k, **kw):
    d = {"id": k, "headline": f"Headline {k}", "caption": f"Caption {k}.", "hashtags": ["World", "News", "Story"],
         "image_text": "Text", "visual": "graphic", "image_query": "",
         "ig_caption": f"Hook for {k}.\n\nWhat happened.\n\nWould you switch?", "x_post": f"🇮🇳 Wire alert {k}\n\nOne detail."}
    return {**d, **kw}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings = replace(Settings(), db_path=self.root / "t.db", benchmark_instagram=["bbcnews"],
                                benchmark_x=["BBCWorld"], benchmark_every_hours=72, gemini_api_key="")
        self.db = Database(self.settings.db_path)

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()

    def articles(self, n=2, run_id="r1", hours_old=1):
        arts = [Article(title=f"Story {i}", url=f"https://news.google.com/rss/articles/{i}", source_name="BBC News",
                        category="World", published_at=NOW - timedelta(hours=hours_old)) for i in range(1, n + 1)]
        self.db.create_run(run_id)
        self.db.save_discovered(run_id, arts)
        self.db.save_evaluations(run_id, [Evaluation(a.id, 80, 90, 90, 80, 90 - i, "Recommended", "ok")
                                          for i, a in enumerate(arts)])
        return arts

    def publish(self, article_id, platform, media_id, hours_ago):
        self.db.save_publication(article_id, platform, "published", media_id=media_id, permalink="https://p")
        self.db.conn.execute("UPDATE publications SET updated_at = ? WHERE article_id = ? AND platform = ?",
                             (ago(hours_ago), article_id, platform))
        self.db.conn.commit()


class TextTests(unittest.TestCase):
    def test_x_post_credits_the_outlet_at_the_end_like_a_newspaper(self):
        self.assertEqual(_x_post("According to The Guardian, Democrats are poised to win the House."),
                         "Democrats are poised to win the House, The Guardian reports.")
        p = {"x_post": "Democrats are poised to win the House, The Guardian reports.", "headline": "h", "caption": "c",
             "hashtags": "[]", "source_name": "The Guardian", "image_kind": "graphic", "image_credit": None,
             "x_hashtag": "#USMidterms"}
        self.assertEqual(x_text(p), "Democrats are poised to win the House, The Guardian reports. #USMidterms")

    def test_x_post_is_plain_like_a_newspaper_feed(self):
        self.assertEqual(_x_post("BREAKING: Apple opens store"), "Apple opens store")
        self.assertEqual(_x_post("JUST IN: Apple opens store"), "Apple opens store")
        self.assertEqual(_x_post("🇮🇳 JUST IN: India wins"), "India wins")
        self.assertEqual(_x_post("🇮🇳 India wins the series"), "India wins the series")
        self.assertEqual(_x_post("Apple opens store #Apple https://x.co/a"), "Apple opens store")

    def test_instagram_caption_keeps_paragraphs_without_tags_or_links(self):
        text = _paragraphs("Hook line.\n\n\nSecond para #News\nThird www.site.com para", 900)
        self.assertEqual(text, "Hook line.\n\nSecond para\n\nThird para")

    def test_platform_texts_differ_and_use_their_own_version(self):
        p = {"headline": "Apple opens a store", "caption": "Short caption.", "hashtags": json.dumps(["#Apple", "#India", "#Retail"]),
             "attribution": "Source: BBC News – https://news.google.com/rss/articles/CBMi123", "image_kind": "graphic",
             "image_credit": None, "source_name": "BBC News", "url": "https://news.google.com/rss/articles/CBMi123",
             "ig_caption": "Apple opened its first store in Pune.\n\nIt is the third in India.\n\nWould you visit?",
             "x_post": "Apple opened its first store in Pune on Friday, its third in India."}
        ig, x = instagram_caption(p), x_text(p)
        self.assertTrue(ig.startswith("Apple opened its first store in Pune.\n\n"))
        self.assertIn("Source: BBC News", ig)  # the long Google News link is dropped
        self.assertTrue(ig.endswith("Source: BBC News\n\n#Apple #India #Retail"))  # the hashtags come last
        self.assertEqual(x, "Apple opened its first store in Pune on Friday, its third in India.\n\nSource: BBC News")

    def test_x_post_is_a_news_line_and_one_detail_line(self):
        self.assertEqual(_x_post("Karnataka bus fares go up by as much as 35%.\n\nThe hike follows a milk price rise."
                                 "\n\nA third paragraph.\n\nSource: BusinessLine"),
                         "Karnataka bus fares go up by as much as 35%.\n\nThe hike follows a milk price rise.")
        self.assertEqual(_x_post("JUST IN: Fares rise.\n" + "detail " * 40), "Fares rise.")  # too long: the news line alone

    def test_x_credit_moves_to_its_own_source_line(self):
        from justnews.agents.content import _drop_credit
        self.assertEqual(_drop_credit("Fares rise.\n\nIt follows a milk price rise, BusinessLine reports.", "BusinessLine"),
                         "Fares rise.\n\nIt follows a milk price rise.")
        self.assertEqual(_drop_credit("Fares rise, BusinessLine reports.", "BusinessLine"),
                         "Fares rise, BusinessLine reports.")  # a single line keeps its credit

    def test_carousel_lists_the_instagram_hooks(self):
        from justnews.agents.publisher import carousel_caption
        base = {"hashtags": json.dumps(["#Delhi"]), "attribution": "Source: Mint", "image_kind": "graphic",
                "image_credit": None, "source_name": "Mint", "url": "https://mint.com/a"}
        posts = [{**base, "headline": "Delhi AQI hits 605", "ig_caption": "Delhi is breathing air at AQI 605 😷\n\nMore."},
                 {**base, "headline": "Rain alert in the south", "ig_caption": None}]
        caption = carousel_caption(posts)
        self.assertIn("1. Delhi is breathing air at AQI 605 😷\n2. Rain alert in the south", caption)
        self.assertTrue(caption.endswith("Sources: Mint\n\n#Delhi"))

    def test_code_notes(self):
        self.assertEqual(code_notes(draft("P1")), [])
        notes = code_notes(draft("P1", ig_caption="x" * 140, x_post="x" * 140))
        self.assertTrue(any("first line is 140" in n for n in notes))
        self.assertTrue(any("short paragraphs" in n for n in notes))
        self.assertTrue(any("repeats the Instagram hook" in n for n in notes))
        notes = code_notes(draft("P1", ig_caption="Flights were checked according to BBC News.\n\nMore.",
                                 x_post="According to BBC News, planes were checked"), "BBC News")
        self.assertEqual(notes, ["Instagram: don't end the hook with the attribution; move it to the next paragraph.",
                                 "X: don't open with 'According to'; state the news (the source line is added after).",
                                 "X: write the news line, a blank line, then one short detail line."])

    def test_source_line_drops_tracking_parameters(self):
        from justnews.agents.publisher import source_line
        url = "https://www.bbc.co.uk/news/articles/c6x?at_medium=RSS&at_campaign=rss"
        self.assertEqual(source_line({"attribution": f"Source: BBC News – {url}", "url": url}),
                         "Source: BBC News – https://www.bbc.co.uk/news/articles/c6x")
        url = "https://site.com/a?id=5&utm_source=rss"
        self.assertEqual(source_line({"attribution": f"Source: Site – {url}", "url": url}),
                         "Source: Site – https://site.com/a?id=5")


class StrategistTests(Base):
    def test_metrics_are_read_only_at_checkpoints(self):
        a1, a2 = self.articles()
        for a in (a1, a2):
            self.db.save_post("r1", a.id, {**draft("P"), "hashtags": ["#World"], "attribution": "Source: BBC News",
                                           "status": "draft"}, "m")
        self.publish(a1.id, "x", "t1", hours_ago=7)    # past the 6h checkpoint
        self.publish(a2.id, "x", "t2", hours_ago=2)    # not yet
        self.publish(a1.id, "instagram", "ig1", hours_ago=80)  # never read: all three checkpoints at once
        x, ig = FakePlatform("x", "X"), FakePlatform("instagram", "Instagram")
        s = StrategistAgent(self.settings, self.db, [ig, x], clients=[])
        self.assertEqual(s.refresh_metrics(), 2)
        self.assertEqual((x.metric_calls, ig.metric_calls), ([["t1"]], [["ig1"]]))
        row = self.db.conn.execute("SELECT checks, likes FROM post_metrics WHERE platform = 'instagram'").fetchone()
        self.assertEqual(tuple(row), (3, 5))
        self.assertEqual(s.refresh_metrics(), 0)  # nothing new is due
        self.assertEqual(len(x.metric_calls), 1)

    def test_benchmarks_are_read_on_schedule_and_failures_wait(self):
        posts = [{"post_id": "b1", "account": "BBCWorld", "text": "🇺🇸 JUST IN: ...", "likes": 900, "comments": 40}]
        x = FakePlatform("x", "X", benchmark=posts)
        ig = FakePlatform("instagram", "Instagram", benchmark=[])  # e.g. missing permission
        s = StrategistAgent(self.settings, self.db, [ig, x], clients=[])
        self.assertEqual(s.refresh_benchmarks(), 1)
        self.assertEqual(s.refresh_benchmarks(), 0)  # X: fresh enough; Instagram: retried after 6 hours
        self.assertEqual((len(x.benchmark_calls), len(ig.benchmark_calls)), (1, 1))

    def test_playbook_starts_from_research_and_learns_from_evidence(self):
        self.articles()
        s = StrategistAgent(self.settings, self.db, [], clients=[])
        self.assertEqual(s.update_playbook("r1"), "research defaults")
        self.assertEqual(json.loads(self.db.latest_playbook()["playbook"]), BASE_PLAYBOOK)
        self.assertEqual(s.update_playbook("r1"), "kept (from research defaults, 0h old)")

        self.db.save_benchmarks("x", [{"post_id": f"b{i}", "account": "BBCWorld", "text": f"post {i}",
                                       "likes": i * 100, "comments": i} for i in range(1, 6)])
        self.db.conn.execute("UPDATE playbooks SET created_at = ?", (ago(7),))
        self.db.conn.commit()
        model = FakeModel({"playbook": {"instagram": ["Lead with the number."] * 2 + [f"rule {i}" for i in range(10)],
                                        "x": ["Start with a flag emoji."], "observations": ["They post explainers."],
                                        "experiments": ["Try carousels."]}})
        s = StrategistAgent(self.settings, self.db, [], clients=[model])
        self.assertEqual(s.update_playbook("r1"), "updated by gemini-test")
        pb = json.loads(self.db.latest_playbook()["playbook"])
        self.assertEqual(pb["instagram"][0], "Lead with the number.")
        self.assertEqual(len(pb["instagram"]), 7)  # duplicates dropped, at most 7 rules
        self.assertEqual(pb["observations"], ["They post explainers."])
        evidence = json.loads(model.prompts[0][1].split("EVIDENCE:\n", 1)[1])
        labels = [(p["text"], p["label"]) for p in evidence["others"]["x"]]
        self.assertEqual(labels[0], ("post 5", "top"))
        self.assertIn(("post 1", "weak"), labels)
        # With only our own numbers and no benchmarks, observations about other accounts are dropped.
        self.db.conn.execute("DELETE FROM benchmarks")
        self.db.save_post("r1", self.db.articles_for_run("r1")[0].id, {**draft("P"), "hashtags": ["#W"],
                          "attribution": "Source: BBC News", "status": "draft"}, "m")
        self.publish(self.db.articles_for_run("r1")[0].id, "instagram", "ig1", hours_ago=30)
        self.db.save_metrics(self.db.articles_for_run("r1")[0].id, "instagram", 2, {"likes": 3})
        self.db.conn.execute("UPDATE playbooks SET created_at = ?", (ago(7),))
        self.db.conn.commit()
        self.assertEqual(s.update_playbook("r1"), "updated by gemini-test")
        self.assertEqual(json.loads(self.db.latest_playbook()["playbook"])["observations"], [])
        messages = [m["body"] for m in self.db.agent_messages("r1")]
        self.assertTrue(messages[-1].startswith("Updated playbook: Instagram: Lead with the number."))


class DiscussionTests(Base):
    def test_strategist_reviews_and_content_creator_revises_once(self):
        a1, a2 = self.articles(hours_old=5)
        write = {"posts": [draft("P1", ig_caption="A hook that is far too long " * 6, x_post="JUST IN: Wire P1"),
                           draft("P2")]}
        review = {"reviews": [{"id": "P1", "ok": True, "notes": ""},
                              {"id": "P2", "ok": False, "notes": "X: put the number first."}]}
        revise = {"posts": [{**draft("P1"), "reply": "Shortened the hook."},
                            {**draft("P2", x_post="🇮🇳 3rd store: Apple opens in Pune"),
                             "reply": "Moved the number first."}]}
        model = FakeModel({"write": write, "review": review, "revise": revise})
        strategist = StrategistAgent(self.settings, self.db, [], clients=[model])
        result = ContentCreatorAgent(self.settings, self.db, clients=[model], verifier=False,
                                     strategist=strategist).run("r1")
        self.assertEqual(result.created, 2)
        posts = {p["article_id"]: p for p in self.db.posts_for_run("r1")}
        self.assertEqual(posts[a2.id]["x_post"], "3rd store: Apple opens in Pune")  # the flag is dropped
        self.assertEqual(posts[a1.id]["ig_caption"], "Hook for P1.\n\nWhat happened.\n\nWould you switch?")
        talk = [(m["sender"], m["recipient"], m["body"]) for m in self.db.agent_messages("r1")]
        self.assertTrue(talk[0][2].startswith("Instagram: the first line is"))
        self.assertEqual(talk[1], ("strategist", "content-creator", "X: put the number first."))
        self.assertIn(("content-creator", "strategist", "Moved the number first."), talk)
        revise_prompt = json.loads(model.prompts[2][1].split("DRAFTS AND NOTES:\n", 1)[1])
        self.assertEqual([i["id"] for i in revise_prompt], ["P1", "P2"])
        self.assertIn("PLAYBOOK", model.prompts[0][1])  # the playbook goes into the first draft too

    def test_also_covered_by_is_not_given_to_the_writer_as_a_fact(self):
        items = ContentCreatorAgent._items({"P1": {"category": "World", "source_name": "BBC", "title": "T",
                                                   "description": "Fact one. Also covered by: CNN, NYT",
                                                   "fresh": False}})
        self.assertEqual(items[0]["summary"], "Fact one.")

    def test_fact_check_flags_get_one_revision_and_a_second_check(self):
        a1, a2 = self.articles()

        class Jev:
            usage, calls = Usage("jev", 0, 0), []

            def review_posts(self, posts, sources):
                self.calls.append(sorted(posts))
                flags = {"P1": "Jev: may state facts not in the source (p=0.80)",
                         "P2": "Jev: may state facts not in the source (p=0.70)"}
                if len(self.calls) == 1:
                    return flags
                return {k: flags[k] for k in posts if posts[k]["x"] == "still adds a cause"}

        revise = {"posts": [{**draft("P1"), "reply": "Removed 'transatlantic'."},
                            {**draft("P2", x_post="still adds a cause"), "reply": "Rephrased."}]}
        model = FakeModel({"write": {"posts": [draft("P1"), draft("P2")]}, "review": {"reviews": []},
                           "revise": revise})
        strategist = StrategistAgent(self.settings, self.db, [], clients=[model])
        jev = Jev()
        result = ContentCreatorAgent(self.settings, self.db, clients=[model], verifier=jev,
                                     strategist=strategist).run("r1")
        self.assertEqual(jev.calls, [["P1", "P2"], ["P1", "P2"]])
        self.assertEqual(result.needs_review, 1)
        posts = {p["article_id"]: p for p in self.db.posts_for_run("r1")}
        self.assertEqual((posts[a1.id]["status"], posts[a2.id]["status"]), ("draft", "needs_review"))
        talk = [(m["sender"], m["recipient"], m["body"]) for m in self.db.agent_messages("r1")]
        self.assertIn(("content-creator", "fact-checker", "Removed 'transatlantic'."), talk)
        self.assertIn(("fact-checker", "content-creator", "Checked again: fine now."), talk)
        self.assertTrue(any(t[2].startswith("Still flagged: Jev") for t in talk))

    def test_good_drafts_are_not_revised_and_a_failed_revision_keeps_them(self):
        self.articles(n=1)
        model = FakeModel({"write": {"posts": [draft("P1", x_post="")]}, "review": {"reviews": []}})
        strategist = StrategistAgent(self.settings, self.db, [], clients=[model])
        ContentCreatorAgent(self.settings, self.db, clients=[model], verifier=False, strategist=strategist).run("r1")
        post = self.db.posts_for_run("r1")[0]
        self.assertIsNone(post["x_post"])  # missing X version: noted, revision failed, falls back at publish
        self.assertIn("Headline P1", x_text(post))

    def test_without_strategist_the_saved_playbook_is_used(self):
        self.articles(n=1)
        self.db.save_playbook({**BASE_PLAYBOOK, "x": ["Custom X rule."]}, {}, "test")
        model = FakeModel({"write": {"posts": [draft("P1")]}})
        ContentCreatorAgent(self.settings, self.db, clients=[model], verifier=False).run("r1")
        self.assertIn("Custom X rule.", model.prompts[0][1])
        self.assertEqual(len(model.prompts), 1)  # no review round


class CycleOrderTests(Base):
    def test_strategist_prepares_before_posts_are_written(self):
        order = []
        fake = NS(prepare=lambda run_id: order.append("strategist"), playbook=BASE_PLAYBOOK)
        with mock.patch.object(cli, "settings", replace(self.settings, strategist=True)), \
                mock.patch.object(cli, "StrategistAgent", lambda *a, **k: fake), \
                mock.patch.object(cli, "ContentCreatorAgent",
                                  lambda *a, strategist=None: NS(run=lambda run_id: order.append(("content", strategist)))):
            cli.write_posts(self.db, "r1")
        self.assertEqual(order, ["strategist", ("content", fake)])

    def test_strategist_is_off_by_default(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("JUSTNEWS_STRATEGIST", None)
            self.assertFalse(Settings().strategist)
            with mock.patch.object(cli, "settings", replace(self.settings, strategist=Settings().strategist)):
                self.assertIsNone(cli.make_strategist(self.db))

    def test_strategist_can_be_switched_off(self):
        with mock.patch.object(cli, "settings", replace(self.settings, strategist=False)):
            self.assertIsNone(cli.make_strategist(self.db))


if __name__ == "__main__":
    unittest.main()
