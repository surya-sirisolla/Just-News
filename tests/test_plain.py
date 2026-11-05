"""Plain language: the free checker, and the one rewrite round it triggers. No network."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from justnews.agents.content import ContentCreatorAgent
from justnews.config import Settings
from justnews.db import Database
from justnews.plain import draft_notes, plain_notes
from test_strategist import FakeModel, draft
from test_pipeline import article


class CheckerTests(unittest.TestCase):
    def test_flags_newspaper_shorthand_and_unexplained_terms(self):
        notes = plain_notes("SC rejects woman's plea as Centre earmarks funds amid row", "headline")
        self.assertIn('headline: say "request to the court" instead of "plea"', notes)
        self.assertIn('headline: "SC" is unclear to most readers: write "Supreme Court" or explain it', notes)
        self.assertIn('headline: say "sets aside" instead of "earmarks"', notes)
        self.assertTrue(any('"amid"' in n for n in notes) and any('"row"' in n for n in notes))

    def test_explained_terms_and_plain_text_pass(self):
        self.assertEqual(plain_notes("The State Election Commission (SEC) reviewed the cyclone damage."), [])
        self.assertEqual(plain_notes("Agniveers (soldiers hired under the four-year Agnipath scheme) get two chances."), [])
        self.assertEqual(plain_notes("The Supreme Court turned down the woman's request, The Hindu reports."), [])
        self.assertEqual(plain_notes("ISRO and the BJP are known names; so are AI and the US."), [])

    def test_long_sentences_and_legal_sections(self):
        long = " ".join(["word"] * 24) + "."
        self.assertTrue(plain_notes(long)[0].startswith("split the sentence"))
        # the closing credit doesn't count towards the length
        self.assertEqual(plain_notes(" ".join(["word"] * 21) + ", The Times of India reports."), [])
        self.assertIn("drop legal section numbers", plain_notes("It upheld Section 9(1)(c) of the Act.")[0])

    def test_draft_notes_cover_every_text_field(self):
        notes = draft_notes({"headline": "HC raps police", "caption": "Fine.", "x_post": "Gates opened, 71,000 cusecs."})
        self.assertEqual(len(notes), 3)
        self.assertTrue(notes[0].startswith("headline:") and notes[2].startswith("x_post:"))


class RewriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = replace(Settings(), db_path=Path(self.tmp.name) / "t.db", gemini_api_key="")
        self.db = Database(self.settings.db_path)
        arts = [article("SC rejects woman's plea", "https://toi.com/1"), article("Kerala rains", "https://toi.com/2")]
        for a in arts:
            a.region = "India"
        self.db.create_run("r1")
        self.db.save_discovered("r1", arts)
        from justnews.models import Evaluation
        self.db.save_evaluations("r1", [Evaluation(a.id, 80, 90, 90, 80, 85 - i, "Recommended", "ok")
                                        for i, a in enumerate(arts)])
        self.ids = [a.id for a in arts]

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()

    def test_hard_drafts_get_one_plain_rewrite_and_easy_ones_are_left_alone(self):
        hard = draft("P1", headline="SC rejects woman's plea", caption="The SC rejected the plea.",
                     x_post="The SC rejected the plea, TOI reports.")
        easy = draft("P2", headline="Heavy rain shuts Kerala schools", caption="Schools in Kerala are shut.",
                     x_post="Heavy rain shut schools in Kerala, The Hindu reports.")
        plain = draft("P1", headline="Supreme Court turns down woman's request", caption="The Supreme Court said no.",
                      x_post="The Supreme Court turned down the woman's request, TOI reports.")
        model = FakeModel({"write": {"posts": [hard, easy]}, "revise": {"posts": [{**plain, "reply": "Used plain words."}]}})
        ContentCreatorAgent(self.settings, self.db, clients=[model], verifier=False).run("r1")
        posts = {p["article_id"]: p for p in self.db.posts_for_run("r1")}
        self.assertEqual(posts[self.ids[0]]["headline"], "Supreme Court turns down woman's request")
        self.assertEqual(posts[self.ids[1]]["headline"], "Heavy rain shuts Kerala schools")
        revise_prompt = model.prompts[1][1]
        self.assertIn('in headline, caption, x_post: \\"SC\\" is unclear', revise_prompt)  # once, not per field
        self.assertNotIn("Kerala", revise_prompt.split("DRAFTS AND NOTES:", 1)[1])  # only the hard one went back
        talk = [(m["sender"], m["body"][:16]) for m in self.db.agent_messages("r1")]
        self.assertIn(("plain-language editor", "Make it plain: i"), talk)
        self.assertIn(("content-creator", "Used plain words"), talk)
        self.assertIn("PLAIN LANGUAGE", model.prompts[0][0])  # the rules are in the writing prompt


if __name__ == "__main__":
    unittest.main()
