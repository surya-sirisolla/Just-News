"""Offline tests: no network, no API key. Run with `python -m unittest discover tests`."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from justnews.agents.evaluator import NewsEvaluatorAgent, freshness_score, known_credibility
from justnews.config import Settings
from justnews.db import Database
from justnews.llm import LLMError, Usage
from justnews.models import Article, article_id, canonical_url

NOW = datetime.now(timezone.utc)


class FakeLLM:
    """Stands in for the LLM client. `script` maps article title -> overrides for its row."""

    model = "fake"

    def __init__(self, script: dict[str, dict] | None = None, fail: bool = False, fatal: bool = False):
        self.script = script or {}
        self.fail = fail
        self.fatal = fatal
        self.prompts: list[str] = []
        self.usage = Usage("fake", 0, 0)

    def generate_json(self, system, prompt, schema):
        self.prompts.append(prompt)
        if self.fail:
            self.usage.failed_calls += 1
            raise LLMError("boom", fatal=self.fatal)
        self.usage.calls += 1
        import json
        items = json.loads(prompt.split("ARTICLES TO EVALUATE (return one object per id):\n", 1)[1])
        rows = []
        for it in items:
            row = {"id": it["id"], "relevance": 90, "audience_interest": 90, "credibility": 80,
                   "duplicate_of": "", "verdict": "post", "reason": "Important story."}
            row.update(self.script.get(it["title"], {}))
            if callable(row["duplicate_of"]):
                row["duplicate_of"] = row["duplicate_of"](prompt)
            rows.append(row)
        return {"articles": rows}


def article(title, url, source="BBC News", hours_old=1.0, category="World"):
    return Article(title=title, url=url, source_name=source, category=category,
                   published_at=NOW - timedelta(hours=hours_old))


class ModelTests(unittest.TestCase):
    def test_canonical_url_strips_tracking_and_case(self):
        a = canonical_url("https://WWW.Example.com/story/?utm_source=x&id=5#top")
        self.assertEqual(a, "https://example.com/story?id=5")
        self.assertEqual(article_id("https://example.com/story/?utm_medium=rss"), article_id("https://example.com/story"))

    def test_freshness_decays(self):
        self.assertEqual(freshness_score(NOW, NOW), 100)
        self.assertEqual(freshness_score(NOW - timedelta(hours=24), NOW), 50)
        self.assertEqual(freshness_score(None, NOW), 30)

    def test_known_credibility(self):
        self.assertEqual(known_credibility("BBC News"), 94)
        self.assertEqual(known_credibility("Reuters"), 96)
        self.assertIsNone(known_credibility("Some Random Blog"))


class EvaluatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = replace(Settings(), db_path=Path(self.tmp.name) / "t.db", eval_batch_size=2)
        self.db = Database(self.settings.db_path)

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()

    def run_eval(self, run_id, articles, llm):
        self.db.create_run(run_id)
        self.db.save_discovered(run_id, articles)
        return NewsEvaluatorAgent(self.settings, self.db, llm=llm).run(run_id)

    def by_title(self, result, articles):
        ids = {a.id: a.title for a in articles}
        return {ids[e.article_id]: e for e in result.evaluations}

    def test_cross_source_duplicate_keeps_best_version(self):
        arts = [
            article("Cyber-attack hits shop freezers in seven European countries", "https://bbc.co.uk/1"),
            article("Supermarket freezers across Europe hit by cyberattack", "https://euronews.com/2", source="Euronews"),
            article("Ceasefire agreed in Ukrainian city", "https://npr.org/3", source="NPR"),
        ]
        # The LLM links the Euronews copy to the BBC one even though the headlines differ.
        llm = FakeLLM({"Supermarket freezers across Europe hit by cyberattack":
                       {"credibility": 70, "duplicate_of": lambda p: "A1"}})
        res = self.by_title(self.run_eval("r1", arts, llm), arts)
        self.assertEqual(res[arts[0].title].decision, "Recommended")
        self.assertEqual(res[arts[1].title].decision, "Rejected")
        self.assertEqual(res[arts[1].title].duplicate_of, arts[0].id)
        self.assertEqual(res[arts[2].title].decision, "Recommended")

    def test_best_copy_kept_even_if_llm_discarded_it_as_a_duplicate(self):
        arts = [
            article("Freezers in Europe hit by cyberattack", "https://euronews.com/1", source="Euronews"),
            article("Shop freezers in seven European countries hit by cyber-attack", "https://bbc.co.uk/2"),
        ]
        # The LLM likes the first copy, then discards the (more credible) BBC copy as its duplicate.
        llm = FakeLLM({"Shop freezers in seven European countries hit by cyber-attack":
                       {"verdict": "discard", "duplicate_of": lambda p: "A1", "reason": "Same as A1."}})
        res = self.by_title(self.run_eval("r1", arts, llm), arts)
        bbc, euronews = res[arts[1].title], res[arts[0].title]
        self.assertEqual(bbc.decision, "Recommended")  # BBC scores higher on credibility
        self.assertEqual(bbc.reason, "Important story. (Best of 2 outlets covering this story.)")  # no "A1" ids
        self.assertEqual(euronews.decision, "Rejected")
        self.assertEqual(euronews.duplicate_of, arts[1].id)

    def test_previously_covered_across_runs(self):
        first = [article("Central bank raises interest rates", "https://bbc.co.uk/rates")]
        self.run_eval("r1", first, FakeLLM())
        # Same URL again -> rejected without any LLM call.
        again = FakeLLM()
        res = self.run_eval("r2", first, again)
        self.assertEqual(res.evaluations[0].decision, "Rejected")
        self.assertIn("Previously covered", res.evaluations[0].reason)
        self.assertEqual(again.usage.calls, 0)
        # Same story from another outlet -> LLM sees it in PREVIOUSLY COVERED and links it.
        other = [article("Bank lifts rates to fight inflation", "https://cnbc.com/rates", source="CNBC")]
        llm = FakeLLM({"Bank lifts rates to fight inflation": {"duplicate_of": lambda p: "H1"}})
        res = self.run_eval("r3", other, llm)
        self.assertIn("Central bank raises interest rates", llm.prompts[0])
        self.assertEqual(res.evaluations[0].decision, "Rejected")
        self.assertIn("Previously covered", res.evaluations[0].reason)

    def test_rejected_stories_are_not_previously_covered(self):
        self.run_eval("r1", [article("Columnist muses on the midterms", "https://x.com/col")],
                      FakeLLM({"Columnist muses on the midterms": {"verdict": "discard"}}))
        llm = FakeLLM()
        self.run_eval("r2", [article("Senator endorses rival in midterms", "https://x.com/news")], llm)
        self.assertNotIn("PREVIOUSLY COVERED", llm.prompts[0])

    def test_low_score_and_discard_are_rejected(self):
        arts = [article("Old minor update", "https://x.com/1", hours_old=40),
                article("Celebrity gossip item", "https://x.com/2")]
        llm = FakeLLM({"Old minor update": {"relevance": 60, "audience_interest": 50},
                       "Celebrity gossip item": {"verdict": "discard", "reason": "Gossip."}})
        res = self.by_title(self.run_eval("r1", arts, llm), arts)
        self.assertEqual(res["Old minor update"].decision, "Rejected")
        self.assertIn("below threshold", res["Old minor update"].reason)
        self.assertEqual(res["Celebrity gossip item"].decision, "Rejected")
        self.assertEqual(res["Celebrity gossip item"].reason, "Gossip.")

    def test_llm_failure_leaves_articles_for_next_run(self):
        arts = [article("Story one", "https://x.com/1")]
        res = self.run_eval("r1", arts, FakeLLM(fail=True))
        self.assertEqual(res.evaluations, [])
        self.assertEqual(res.not_evaluated, 1)
        # Not marked as evaluated, so the next run scores it normally.
        res = self.run_eval("r2", arts, FakeLLM())
        self.assertEqual(res.evaluations[0].decision, "Recommended")

    def test_fatal_llm_error_stops_after_first_batch(self):
        arts = [article(f"Story {i}", f"https://x.com/{i}") for i in range(5)]
        llm = FakeLLM(fail=True, fatal=True)
        res = self.run_eval("r1", arts, llm)
        self.assertEqual(len(llm.prompts), 1)  # batch size 2, but no further batches attempted
        self.assertEqual(res.not_evaluated, 5)
        self.assertEqual(res.llm_error, "boom")


class DiscoveryFilterTests(unittest.TestCase):
    def test_filters_promos_stale_offtopic_and_already_evaluated(self):
        from collections import Counter
        from justnews.agents.discovery import NewsDiscoveryAgent
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings = replace(Settings(), db_path=Path(tmp.name) / "t.db")
        db = Database(settings.db_path)
        self.addCleanup(db.conn.close)
        seen = article("Old news already handled", "https://bbc.co.uk/seen")
        db.create_run("r0")
        db.save_discovered("r0", [seen])
        db.save_evaluations("r0", [__import__("justnews.models", fromlist=["Evaluation"]).Evaluation(
            seen.id, 1, 1, 1, 1, 1, "Rejected", "x")])
        arts = [
            seen,
            article("Mattress Deals: Save $600 Before Black Friday", "https://x.com/deal"),
            article("Parliament passes budget", "https://x.com/stale", hours_old=100),
            article("New bakery opens downtown", "https://x.com/bakery", category="AI"),
            article("OpenAI releases new model", "https://x.com/ai", category="AI"),
            article("OpenAI releases new model", "https://x.com/ai/?utm_source=rss", category="AI"),
        ]
        filtered = Counter()
        kept = NewsDiscoveryAgent(settings, db)._filter(arts, filtered)
        self.assertEqual([a.title for a in kept], ["OpenAI releases new model"])
        self.assertEqual(filtered, Counter({"already evaluated in an earlier run": 1,
                                            "irrelevant (deals/guides/promo)": 1, "stale": 1,
                                            "off-topic for category": 1, "duplicate in this run": 1}))


if __name__ == "__main__":
    unittest.main()
