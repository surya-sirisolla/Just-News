"""Offline tests for the Jev backend: a fake TypeSafe client, no network or API key."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

from justnews.agents.evaluator import NewsEvaluatorAgent
from justnews.config import Settings
from justnews.db import Database
from justnews.jev import JevScorer, is_candidate_pair
from justnews.models import Article

NOW = datetime.now(timezone.utc)


def score(level: int, levels: int, confidence: float = 1.0):
    probs = {i: 1.0 if i == level else 0.0 for i in range(levels)}
    return NS(score=float(level), legend={i: "" for i in range(levels)}, probabilities=probs, confidence=confidence)


class FakeTypeSafe:
    """Answers by headline: `script[headline]` = (relevance level 0-4, interest level 0-3, news noul)."""

    def __init__(self, script, same_events=()):
        self.script = script
        self.same = {frozenset(p) for p in same_events}
        self.calls = []

    def system_one(self, state, questions):
        self.calls.append((state, set(questions)))
        if "same_event" in questions:
            pair = frozenset((state["article_a"]["headline"], state["article_b"]["headline"]))
            answers = {"same_event": score(2 if pair in self.same else 0, 3)}
        else:
            rel, interest, news = self.script[state["article"]["headline"]]
            answers = {"relevance": score(rel, 5), "audience_interest": score(interest, 4), "is_news_event": NS(noul=news),
                       "is_roundup": NS(noul=0.9 if state["article"]["headline"].startswith("Top stories") else 0.05)}
            if "credibility" in questions:
                answers["credibility"] = score(1, 4)
        return NS(answers=answers, usage=NS(input_tokens=100, output_tokens=10))


def jev(fake) -> JevScorer:
    s = JevScorer("test-key", "jev-test", 0.042, workers=2)
    s.client = fake
    return s


def article(title, url, source="BBC News", hours_old=1.0, category="World"):
    return Article(title=title, url=url, source_name=source, category=category,
                   published_at=NOW - timedelta(hours=hours_old))


class JevTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = replace(Settings(), db_path=Path(self.tmp.name) / "t.db")
        self.db = Database(self.settings.db_path)

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()

    def run_eval(self, run_id, arts, fake):
        self.db.create_run(run_id)
        self.db.save_discovered(run_id, arts)
        res = NewsEvaluatorAgent(self.settings, self.db, llm=jev(fake)).run(run_id)
        titles = {a.id: a.title for a in arts}
        return {titles[e.article_id]: e for e in res.evaluations}, res

    def test_candidate_pairs_need_shared_names_or_words(self):
        self.assertTrue(is_candidate_pair("Anthropic posts $17B revenue", "Filing shows Anthropic losses"))
        self.assertTrue(is_candidate_pair("Shop freezers hit by cyber-attack", "Cyber-attack hits freezers in shops"))
        self.assertFalse(is_candidate_pair("Ryanair may freeze pay", "Uganda withdraws troops from Somalia"))
        # Possessives and word forms; one shared word is enough within a category.
        self.assertTrue(is_candidate_pair("A fresh wave of cyberattacks has hit Europe’s supermarket refrigerators",
                                          "Ikea among shops affected by fridge cyber-hacks – Europe live"))
        wall_st = "Wall St rises as oil falls heading into US midterm elections"
        market = "Stock market today: Dow, S&P 500, Nasdaq recover from sell-off as oil prices fall"
        self.assertTrue(is_candidate_pair(wall_st, market, same_category=True))
        self.assertFalse(is_candidate_pair(wall_st, "Bank of England holds rates"))

    def test_scores_reasons_and_duplicates(self):
        arts = [
            article("Shop freezers in seven European countries hit by cyber-attack", "https://bbc.co.uk/1"),
            article("Cyberattacks hit supermarket freezers in Europe", "https://euronews.com/2", source="Euronews"),
            article("Why I stopped using my smart fridge", "https://x.com/3", source="Some Blog"),
        ]
        fake = FakeTypeSafe(
            {arts[0].title: (3, 3, 0.95), arts[1].title: (3, 3, 0.95), arts[2].title: (0, 1, 0.1)},
            same_events=[(arts[0].title, arts[1].title)])
        res, result = self.run_eval("r1", arts, fake)
        bbc, euro, blog = res[arts[0].title], res[arts[1].title], res[arts[2].title]
        self.assertEqual((bbc.relevance, bbc.audience_interest, bbc.credibility), (75, 100, 94))
        self.assertEqual(bbc.decision, "Recommended")
        self.assertIn("Significant news, highly shareable", bbc.reason)
        self.assertEqual(euro.decision, "Rejected")
        self.assertEqual(euro.duplicate_of, bbc.article_id)
        self.assertEqual(blog.decision, "Rejected")
        self.assertEqual(blog.credibility, 50)  # unknown publisher -> Jev credibility question
        self.assertIn("opinion/feature/service", blog.reason)
        self.assertEqual(result.usage["output_tokens"], 30 + 10)  # tracked, though not billed
        self.assertEqual(result.usage["cost_usd"], round(result.usage["input_tokens"] * 0.042 / 1e6, 6))

    def test_roundup_does_not_chain_unrelated_stories(self):
        anthropic = article("Anthropic posts $17B quarterly revenue", "https://x.com/a", source="Reuters", category="AI")
        roundup = article("Top stories: Anthropic revenue jumps; Marvell buys Lumentum", "https://x.com/r",
                          source="Yahoo Finance", category="Business")
        marvell = article("Marvell could be making a mistake with Lumentum purchase", "https://x.com/m",
                          source="Barron's", category="Business")
        fake = FakeTypeSafe({anthropic.title: (4, 3, 0.9), roundup.title: (2, 2, 0.9), marvell.title: (3, 2, 0.9)},
                            same_events=[(anthropic.title, roundup.title), (roundup.title, marvell.title)])
        res, _ = self.run_eval("r1", [anthropic, roundup, marvell], fake)
        self.assertEqual(res[anthropic.title].decision, "Recommended")
        self.assertEqual(res[roundup.title].decision, "Rejected")
        self.assertIn("round-up", res[roundup.title].reason)
        self.assertIsNone(res[marvell.title].duplicate_of)  # not merged into the Anthropic story
        self.assertFalse(any("Top stories" in str(state) for state, q in fake.calls if "same_event" in q))

    def test_previously_covered_story_from_another_outlet(self):
        first = [article("Central bank raises interest rates", "https://bbc.co.uk/rates")]
        self.run_eval("r1", first, FakeTypeSafe({first[0].title: (3, 2, 0.9)}))
        again = [article("Bank lifts interest rates to fight inflation", "https://cnbc.com/rates", source="CNBC")]
        fake = FakeTypeSafe({again[0].title: (3, 2, 0.9)}, same_events=[(again[0].title, first[0].title)])
        res, _ = self.run_eval("r2", again, fake)
        self.assertEqual(res[again[0].title].decision, "Rejected")
        self.assertIn("Previously covered", res[again[0].title].reason)

    def test_auth_error_is_fatal_and_leaves_articles_for_next_run(self):
        import httpx2
        from typesafe_sdk import TypeSafeAuthenticationError

        class Denied:
            def system_one(self, state, questions):
                raise TypeSafeAuthenticationError(401, {}, httpx2.Headers(), message="invalid API key")

        arts = [article("Story one", "https://x.com/1"), article("Story two", "https://x.com/2")]
        res, result = self.run_eval("r1", arts, Denied())
        self.assertEqual(res, {})
        self.assertEqual(result.not_evaluated, 2)
        self.assertTrue(result.llm_error)


if __name__ == "__main__":
    unittest.main()
