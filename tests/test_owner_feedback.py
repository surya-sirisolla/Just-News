"""Reactions instead of replies, and the owner's instructions reaching the content creator. No network."""

from __future__ import annotations

import json
import time
import unittest
from types import SimpleNamespace as NS
from unittest import mock

from justnews.agents.content import ContentCreatorAgent
from justnews.assistant import fallback
from justnews.whatsapp import Incoming
from test_approval import OWNER, Base
from test_strategist import FakeModel, draft


class ReactionTests(Base):
    def reactions(self):
        return [r["body"] for r in self.db.conn.execute("SELECT body FROM wa_outbox WHERE reaction_to IS NOT NULL ORDER BY id")]

    def texts(self):
        return [r["body"] for r in self.db.conn.execute("SELECT body FROM wa_outbox WHERE reaction_to IS NULL ORDER BY id")]

    def test_thanks_gets_a_heart_and_no_model_call(self):
        self.service.request("r1")
        self.service.assistant = mock.Mock()
        before = len(self.texts())
        for i, text in enumerate(["thanks", "ok 👍", "Thank you!", "🙏"]):
            self.assertEqual(self.say(text, mid=f"ack{i}"), "acknowledged")
        self.assertEqual(self.reactions(), ["❤️"] * 4)
        self.assertEqual(len(self.texts()), before)  # no text replies
        self.service.assistant.interpret.assert_not_called()

    def test_numbers_get_a_thumbs_up_first_and_none_only_a_thumbs_up(self):
        self.service.request("r1")
        self.assertEqual(self.say("1 3"), "approved 1, 3")
        self.assertEqual(self.reactions(), ["👍"])
        self.make_posts(2, run_id="r2")
        self.service.request("r2")
        before = len(self.texts())
        self.assertEqual(self.say("none"), "none approved")
        self.assertEqual(self.reactions(), ["👍", "👍"])
        self.assertEqual(len(self.texts()), before)  # no "Okay, I'll skip…" text any more

    def test_reaction_points_at_the_owners_message(self):
        self.service.request("r1")
        self.service.handle_message(Incoming("WAMSG1", OWNER, "2", int(time.time()), chat="555@lid", sender_jid="555@lid"))
        ref = json.loads(self.db.conn.execute("SELECT reaction_to FROM wa_outbox WHERE reaction_to IS NOT NULL").fetchone()[0])
        self.assertEqual(ref, {"id": "WAMSG1", "chat": "555@lid", "sender": "555@lid"})


class EditTests(Base):
    def test_edit_goes_to_the_writer_and_the_new_version_comes_back(self):
        self.service.request("r1")
        calls = []

        def editor(run_id, ids, instruction):
            calls.append((run_id, ids, instruction))
            self.db.conn.execute("UPDATE posts SET headline = 'Simpler headline 2' WHERE article_id = ?", (ids[0],))
            self.db.conn.commit()
            return {ids[0]: "The source says 373, not 600+, so I kept 373."}

        self.service.editor = editor
        self.service.assistant = mock.Mock(interpret=lambda text, ctx: {
            "intent": "edit", "numbers": [2], "instruction": "make it simpler", "query": "", "reply": ""})
        self.assertEqual(self.say("make 2 simpler"), "edited [2]")
        self.assertEqual(calls, [("r1", [self.ids[1]], "make it simpler")])
        update = self.outbox()[-1]
        self.assertTrue(update.startswith("✏️ *Updated 2*\n*2. Simpler headline 2*"))
        self.assertIn("_Writer: The source says 373, not 600+, so I kept 373._", update)
        reaction = self.db.conn.execute("SELECT body FROM wa_outbox WHERE reaction_to IS NOT NULL").fetchone()[0]
        self.assertEqual(reaction, "✍️")

    def test_edit_without_a_story_number_asks_which(self):
        self.service.request("r1")
        self.service.editor = mock.Mock()
        self.service.assistant = mock.Mock(interpret=lambda text, ctx: {
            "intent": "edit", "numbers": [], "instruction": "simpler", "query": "", "reply": ""})
        self.say("make it simpler")
        self.assertIn("Which story should I change?", self.outbox()[-1])
        self.service.editor.assert_not_called()

    def test_the_assistant_sees_each_storys_facts_and_source(self):
        self.service.request("r1")
        seen = []
        self.service.assistant = mock.Mock(interpret=lambda text, ctx: seen.append(ctx) or {
            "intent": "chat", "numbers": [], "instruction": "", "query": "", "reply": "Story 1 says…"})
        self.say("what does story 1 say about the numbers?")
        story = seen[0]["stories"][0]
        self.assertEqual((story["facts"], story["source"]), ("Caption 1.", "BBC News"))

    def test_fallback_understands_an_edit(self):
        out = fallback("make 2 shorter please")
        self.assertEqual((out["intent"], out["numbers"]), ("edit", [2]))


class WriterTests(Base):
    def test_revise_posts_rewrites_keeps_approval_and_redesigns_the_image(self):
        self.db.set_post_approval([self.ids[0]], "requested")
        self.db.save_image("r1", self.ids[0], {"kind": "graphic", "path": "/tmp/old.png"})
        revise = {"posts": [{**draft("P1", headline="Plain headline 1", x_post="Plain words, BBC News reports."),
                             "reply": "Made it simpler; the source has no 600+ figure, so I left that out."}]}
        model = FakeModel({"revise": revise})
        jev = NS(usage=NS(as_dict=lambda: {}), review_posts=lambda posts, sources: {})
        replies = ContentCreatorAgent(self.settings, self.db, clients=[model], verifier=jev).revise_posts(
            "r1", [self.ids[0]], "make it simpler and use 600+")
        self.assertEqual(replies, {self.ids[0]: "Made it simpler; the source has no 600+ figure, so I left that out."})
        post = next(p for p in self.db.posts_for_run("r1") if p["article_id"] == self.ids[0])
        self.assertEqual((post["headline"], post["approval"], post["image_path"]), ("Plain headline 1", "requested", None))
        self.assertIn("The owner asks: make it simpler and use 600+", model.prompts[0][1])
        talk = [(m["sender"], m["recipient"]) for m in self.db.agent_messages("r1")]
        self.assertEqual(talk, [("owner", "content-creator"), ("content-creator", "owner")])

    def test_owner_guidance_reaches_the_first_draft(self):
        from justnews.models import Article, Evaluation
        from datetime import datetime, timedelta, timezone
        a = Article(title="Delhi AQI crosses 600", url="https://ani.com/1", source_name="ANI", category="India",
                    published_at=datetime.now(timezone.utc) - timedelta(hours=1), region="India")
        self.db.create_run("rr")
        self.db.save_discovered("rr", [a])
        self.db.save_evaluations("rr", [Evaluation(a.id, 80, 90, 90, 80, 80, "Recommended", "ok")])
        model = FakeModel({"write": {"posts": [draft("P1", headline="Delhi AQI crosses 600")]}})
        ContentCreatorAgent(self.settings, self.db, clients=[model], verifier=False).run(
            "rr", mix=False, guidance="Lead with the AQI above 600")
        self.assertTrue(model.prompts[0][1].startswith("OWNER'S NOTE"))
        self.assertIn("Lead with the AQI above 600", model.prompts[0][1])


if __name__ == "__main__":
    unittest.main()
