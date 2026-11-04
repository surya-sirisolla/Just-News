"""India-first mix (regions.py) and on-demand research from WhatsApp. Fakes only, no network."""

from __future__ import annotations

import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from justnews import cli
from justnews.agents.evaluator import NewsEvaluatorAgent
from justnews.assistant import clean_query, fallback
from justnews.config import Settings
from justnews.db import Database
from justnews.regions import INDIA, INTERNATIONAL, SOUTH_INDIA, detect_region, select_mix
from justnews.whatsapp import Incoming
from test_approval import OWNER, Base
from test_pipeline import FakeLLM, article


class RegionTests(unittest.TestCase):
    def test_detect_region_from_text_then_source(self):
        cases = [
            ("Hyderabad metro phase 2 gets Centre's nod", "", INTERNATIONAL, SOUTH_INDIA),
            ("Kerala floods: schools shut in Kochi", "", INDIA, SOUTH_INDIA),
            ("Revanth Reddy meets PM Modi on funds", "", INDIA, SOUTH_INDIA),  # South wins over India
            ("Indian seafarer killed in attack near Oman", "", INTERNATIONAL, INDIA),
            ("Sensex closes 1,200 points higher", "", INTERNATIONAL, INDIA),
            ("Local body polls: ruling party sweeps wards", "", INDIA, INDIA),  # national feed, no place named
            ("Trump says US will stop Israel aid", "", INTERNATIONAL, INTERNATIONAL),
            ("Indiana passes new school law", "", INTERNATIONAL, INTERNATIONAL),  # "Indiana" is not India
            ("Growing reliance on AI chatbots", "", INTERNATIONAL, INTERNATIONAL),
        ]
        for title, desc, source_region, expected in cases:
            self.assertEqual(detect_region(title, desc, source_region), expected, title)

    def test_mix_caps_world_news_and_keeps_room_for_south_india(self):
        def row(i, region, score):
            return {"id": i, "region": region, "overall": score, "title": i, "description": ""}
        rows = ([row(f"w{i}", INTERNATIONAL, 95 - i) for i in range(5)] +       # strong world news
                [row(f"weak{i}", INTERNATIONAL, 80) for i in range(3)] +        # ordinary world news
                [row(f"in{i}", INDIA, 90 - i) for i in range(8)] +
                [row(f"s{i}", SOUTH_INDIA, 75 - i) for i in range(6)])
        picked = select_mix(rows, 10)
        regions = [r["region"] for r in picked]
        self.assertEqual(regions.count(INTERNATIONAL), 2)  # 20%, and only the important ones
        self.assertEqual({r["id"] for r in picked if r["region"] == INTERNATIONAL}, {"w0", "w1"})
        self.assertEqual(regions.count(SOUTH_INDIA), 4)    # 40% kept for South India
        self.assertEqual(regions.count(INDIA), 4)
        self.assertEqual([r["overall"] for r in picked], sorted((r["overall"] for r in picked), reverse=True))

    def test_mix_without_important_world_news_is_all_india(self):
        rows = [{"id": "w", "region": INTERNATIONAL, "overall": 84, "title": "w", "description": ""},
                {"id": "i", "region": None, "overall": 70, "title": "Chennai rains shut schools", "description": ""}]
        self.assertEqual([(r["id"], r["region"]) for r in select_mix(rows, 10)], [("i", SOUTH_INDIA)])


class ResearchQueryTests(unittest.TestCase):
    def test_owner_wording_becomes_a_search(self):
        for text, query in [("research on aqi today", "aqi"), ("any news on Chennai rains?", "Chennai rains"),
                            ("can you find the latest on ISRO", "ISRO"), ("Hyderabad metro news this week", "Hyderabad metro")]:
            self.assertEqual(clean_query(text), query, text)
        self.assertEqual(fallback("research on aqi today")["intent"], "research")
        self.assertEqual(fallback("send me the templates")["intent"], "send_templates")
        self.assertEqual((cli.research_window("aqi today"), cli.research_window("ISRO this week"),
                          cli.research_window("rains yesterday")), ("1d", "7d", "2d"))


class ResearchConversationTests(Base):
    def test_research_request_starts_a_search_and_answers_at_once(self):
        asked = []
        self.service.researcher = lambda query, text: asked.append((query, text))
        self.service.assistant = mock.Mock(interpret=lambda text, ctx: {
            "intent": "research", "numbers": [], "query": "AQI", "reply": "On it! Checking today's AQI news 🔎"})
        self.assertEqual(self.say("research on aqi today"), "research 'AQI'")
        self.assertEqual(asked, [("AQI", "research on aqi today")])
        self.assertEqual(self.outbox()[-1], "On it! Checking today's AQI news 🔎")

    def test_research_shortlist_has_its_own_title(self):
        self.service.request("r1", title="🔎 *AQI*", limit=2)
        header = self.outbox()[0]
        self.assertTrue(header.startswith("🔎 *AQI* · "))
        self.assertIn("2 stories", header)


class FreshLookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = replace(Settings(), db_path=Path(self.tmp.name) / "t.db")
        self.db = Database(self.settings.db_path)

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()

    def test_research_rescores_stories_seen_before_and_reoffers_their_posts(self):
        a = article("Delhi AQI slips to very poor", "https://thehindu.com/aqi", source="The Hindu", category="India")
        self.db.create_run("r1")
        self.db.save_discovered("r1", [a])
        NewsEvaluatorAgent(self.settings, self.db, llm=FakeLLM()).run("r1")
        self.db.save_post("r1", a.id, {"headline": "AQI", "caption": "c", "hashtags": [], "attribution": "s",
                                       "image_text": "x", "visual": "graphic", "status": "draft"}, "m")
        self.db.set_post_approval([a.id], "requested")  # offered in a list that was skipped
        self.db.create_run("r2-research")
        self.db.save_discovered("r2-research", [a])
        normal = NewsEvaluatorAgent(self.settings, self.db, llm=FakeLLM()).run("r2-research")
        self.assertEqual(normal.evaluations[0].decision, "Rejected")  # a normal cycle skips repeats
        fresh = NewsEvaluatorAgent(self.settings, self.db, llm=FakeLLM()).run("r2-research", fresh_look=True)
        self.assertEqual(fresh.evaluations[0].decision, "Recommended")
        self.assertEqual(self.db.adopt_posts("r2-research", [a.id]), 1)
        self.assertEqual([p["article_id"] for p in self.db.posts_awaiting_approval("r2-research", 5)], [a.id])

    def test_research_keeps_only_stories_about_the_topic(self):
        arts = [article("Mumbai AQI slips to poor as haze returns", "https://toi.com/1", category="India"),
                article("Tiger Shroff case: police question relative", "https://toi.com/2", category="India"),
                article("Mumbai air quality worst in two years", "https://toi.com/3", category="India")]
        self.db.create_run("r-research")
        self.db.save_discovered("r-research", arts)
        NewsEvaluatorAgent(self.settings, self.db, llm=FakeLLM()).run("r-research", fresh_look=True)
        with mock.patch.object(cli, "settings", replace(self.settings, typesafe_api_key="")):
            kept = cli.on_topic(self.db, "r-research", [a.id for a in arts], "Mumbai AQI")
        self.assertEqual(kept, [arts[0].id, arts[2].id])  # "air quality" counts as AQI
        decisions = {r["article_id"]: r["decision"] for r in self.db.evaluations_for_run("r-research")}
        self.assertEqual(decisions[arts[1].id], "Rejected")

    def test_research_asks_jev_whether_each_story_is_on_topic(self):
        arts = [article("Mumbai AQI slips to poor", "https://toi.com/1"), article("Delhi stubble fires", "https://toi.com/2")]
        self.db.create_run("r-research")
        self.db.save_discovered("r-research", arts)
        jev = mock.Mock()
        jev.return_value.about_topic.return_value = {arts[0].id: 0.94, arts[1].id: 0.08}
        with mock.patch.object(cli, "settings", replace(self.settings, typesafe_api_key="k")), \
                mock.patch("justnews.jev.JevScorer", jev):
            self.assertEqual(cli.on_topic(self.db, "r-research", [a.id for a in arts], "Mumbai AQI"), [arts[0].id])

    def test_a_week_long_search_keeps_week_old_stories(self):
        seen = {}

        class Discovery:
            def __init__(self, settings, db):
                seen["max_age_hours"] = settings.max_age_hours

            def run(self, run_id, sources, include_seen):
                seen["sources"] = sources

        with mock.patch.object(cli, "NewsDiscoveryAgent", Discovery), \
                mock.patch.object(cli, "NewsEvaluatorAgent"), mock.patch.object(cli, "ContentCreatorAgent"), \
                mock.patch.object(cli, "on_topic", return_value=[]):
            cli.research(self.db, "BMC AQI audit", "7d")
        self.assertEqual(seen["max_age_hours"], 7 * 24 + 12)
        self.assertIn("when%3A7d", seen["sources"][0].url)

    def test_research_job_sends_a_shortlist_or_says_nothing_was_found(self):
        sent = []
        service = mock.Mock(owner="o", client=mock.Mock(send_text=lambda to, text: sent.append(text)))
        service.request.return_value = {"requested": 0}
        with mock.patch.object(cli, "approval_service", return_value=service), \
                mock.patch.object(cli, "research", return_value="run-research") as run:
            cli.research_job("AQI", "research on aqi this week", self.db)
        run.assert_called_once_with(self.db, "AQI", "7d")
        service.request.assert_called_once_with("run-research", title="🔎 *AQI*", limit=Settings().research_max_posts)
        with mock.patch.object(cli, "approval_service", return_value=service), \
                mock.patch.object(cli, "research", return_value="run-research"):
            cli.research_job("aqi", "research on aqi today", self.db)
        self.assertEqual(service.request.call_args.kwargs["title"], "🔎 *AQI*")
        self.assertIn("couldn't find recent news worth posting on AQI", sent[0])


if __name__ == "__main__":
    unittest.main()
