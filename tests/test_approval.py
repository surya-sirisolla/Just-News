"""Offline tests for WhatsApp approval and the linked-device bridge (fake clients, no network)."""

from __future__ import annotations

import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

from neonize.proto import Neonize_pb2 as N
from neonize.proto.waE2E import WAWebProtobufsE2E_pb2 as E2E

from justnews.approval import ApprovalService, _chunks, parse_selection
from justnews.config import Settings
from justnews.db import Database
from justnews.models import Article, Evaluation
from justnews.whatsapp import MAX_TEXT, Incoming, OutboxClient, WhatsAppBridge, incoming_from_owner

OWNER = "919800000001"
NOW = datetime.now(timezone.utc)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings = replace(Settings(), db_path=self.root / "t.db", whatsapp_owner=OWNER, max_posts_per_run=10)
        self.db = Database(self.settings.db_path)
        self.designed = []
        self.service = ApprovalService(self.settings, self.db, OutboxClient(self.db), self.designer)
        self.ids = self.make_posts(3)

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()

    def designer(self, run_id, article_ids):
        self.designed.append(list(article_ids))
        for aid in article_ids:
            path = self.root / f"t{self.ids.index(aid) + 1 if aid in getattr(self, 'ids', []) else aid}.png"
            path.write_bytes(b"png")
            self.db.save_image(run_id, aid, {"kind": "graphic", "path": str(path)})
        return NS(photos=0, graphics=len(article_ids), failed=0)

    def make_posts(self, n, run_id="r1"):
        arts = [Article(title=f"Story {i}", url=f"https://bbc.co.uk/{i}", source_name="BBC News", category="World",
                        published_at=NOW - timedelta(hours=1)) for i in range(1, n + 1)]
        self.db.create_run(run_id)
        self.db.save_discovered(run_id, arts)
        self.db.save_evaluations(run_id, [Evaluation(a.id, 80, 90, 90, 80, 90 - i, "Recommended", "ok")
                                          for i, a in enumerate(arts)])
        for i, a in enumerate(arts, 1):
            self.db.save_post(run_id, a.id, {"headline": f"Headline {i}", "caption": f"Caption {i}.",
                                             "hashtags": ["#World", "#News", "#Story"],
                                             "attribution": f"Source: BBC News – {a.url}", "image_text": f"Text {i}",
                                             "visual": "graphic", "status": "needs_review" if i == 2 else "draft",
                                             "review_note": "Jev: check this" if i == 2 else None}, "gemini-test")
        return [a.id for a in arts]

    def say(self, text, sender=OWNER, mid=None):
        return self.service.handle_message(Incoming(mid or f"in.{time.monotonic_ns()}", sender, text, 0))

    def outbox(self):
        return [r["body"] for r in self.db.conn.execute("SELECT body FROM wa_outbox WHERE media_path IS NULL ORDER BY id")]

    def sent_images(self):
        return [(r["media_path"], r["body"]) for r in
                self.db.conn.execute("SELECT media_path, body FROM wa_outbox WHERE media_path IS NOT NULL ORDER BY id")]


class SelectionTests(unittest.TestCase):
    def test_parse_selection(self):
        cases = {"1 3": [1, 3], "1,3,3": [1, 3], "2-4": [2, 3, 4], "1 and 4": [1, 4], "all": [1, 2, 3, 4, 5],
                 "none": [], "skip": [], "6": None, "0 2": None, "post the first one": None, "": None}
        for text, expected in cases.items():
            self.assertEqual(parse_selection(text, 5), expected, text)

    def test_long_shortlists_split(self):
        msgs = _chunks(["x" * 1500] * 7, "footer")
        self.assertTrue(all(len(m) <= MAX_TEXT for m in msgs))
        self.assertTrue(msgs[-1].endswith("footer"))


class ApprovalFlowTests(Base):
    def test_shortlist_is_queued_as_text_and_nothing_designed(self):
        result = self.service.request("r1")
        self.assertEqual(result["requested"], 3)
        [text] = self.outbox()
        for expected in ("*1. Headline 1*", "Caption 1.", "#World #News #Story", "Source: BBC News – https://bbc.co.uk/1",
                         "⚠️ Jev: check this", "*all* / *none*"):
            self.assertIn(expected, text)
        self.assertEqual(self.designed, [])

    def test_reply_designs_only_chosen_posts(self):
        self.service.request("r1")
        self.assertEqual(self.say("1 3"), "approved 1, 3")
        self.assertEqual(self.designed, [[self.ids[0], self.ids[2]]])
        approvals = dict(self.db.conn.execute("SELECT article_id, approval FROM posts").fetchall())
        self.assertEqual([approvals[i] for i in self.ids], ["approved", "rejected", "approved"])
        self.assertIn("I made the templates for 1 and 3", self.outbox()[-1])
        self.assertEqual(self.sent_images(), [])  # templates are not pushed unasked

    def test_list_none_and_non_owner(self):
        self.service.request("r1")
        self.assertEqual(self.say("list"), "shortlist sent")
        self.assertEqual(self.say("1", sender="15550000000"), "ignored: not the owner")
        self.assertEqual(self.say("none"), "none approved")
        self.assertEqual(self.designed, [])

    def test_duplicate_delivery_and_expiry(self):
        self.service.request("r1")
        self.assertEqual(self.say("2", mid="same"), "approved 2")
        self.assertEqual(self.say("2", mid="same"), "ignored: duplicate delivery")
        self.make_posts(2, run_id="r2")
        self.service.request("r2")
        self.db.conn.execute("UPDATE approval_requests SET expires_at = ?", ((NOW - timedelta(hours=1)).isoformat(),))
        self.say("1")  # the list has expired, so "1" is no longer an approval
        self.assertEqual(len(self.designed), 1)

    def test_old_redelivered_message_gets_no_reply(self):
        old = int((NOW - timedelta(hours=2)).timestamp())
        out = self.service.handle_message(Incoming("old1", OWNER, "hello", old))
        self.assertEqual(out, "ignored: old message redelivered after a reconnect")
        self.assertEqual(self.outbox(), [])

    def test_new_request_supersedes_old(self):
        self.service.request("r1")
        new_ids = self.make_posts(2, run_id="r2")
        self.service.request("r2")
        self.assertEqual(self.say("2"), "approved 2")
        self.assertEqual(self.designed, [[new_ids[1]]])


class FakeAssistant:
    """Stands in for Gemini: returns a fixed interpretation and records the context it saw."""

    def __init__(self, intent, numbers=(), reply=""):
        self.out = {"intent": intent, "numbers": list(numbers), "reply": reply}
        self.contexts = []

    def interpret(self, text, context):
        self.contexts.append(context)
        return self.out


class ConversationTests(Base):
    def use(self, assistant):
        self.service.assistant = assistant
        return assistant

    def test_send_me_the_templates_sends_ready_images_only(self):
        self.service.request("r1")
        self.say("1 3")
        self.use(FakeAssistant("send_templates", [], "Sure, here they are!"))
        self.assertEqual(self.say("send me the templates"), "sent templates [1, 3]")
        images = self.sent_images()
        self.assertEqual([Path(p).name for p, _ in images], ["t1.png", "t3.png"])
        self.assertIn("*1. Headline 1*", images[0][1])
        self.assertIn("Sure, here they are!", self.outbox())

    def test_asking_for_a_missing_template_says_so(self):
        self.service.request("r1")
        self.say("1")
        self.use(FakeAssistant("send_templates", [1, 2], "Here you go"))
        self.say("send 1 and 2")
        self.assertEqual(len(self.sent_images()), 1)
        self.assertIn("2 is not made yet", self.outbox()[-1])

    def test_free_text_approval_needs_a_yes(self):
        self.service.request("r1")
        assistant = self.use(FakeAssistant("approve", [2, 9], "ok"))  # 9 is not on the list
        self.assertEqual(self.say("go with the second one"), "asked to confirm [2]")
        self.assertEqual(self.designed, [])
        self.assertIn("Just to be sure", self.outbox()[-1])
        self.assertEqual(assistant.contexts[0]["stories"][0]["headline"], "Headline 1")
        self.assertEqual(self.say("yes"), "approved 2")
        self.assertEqual(self.designed, [[self.ids[1]]])

    def test_no_cancels_a_pending_approval(self):
        self.service.request("r1")
        self.use(FakeAssistant("approve", [1]))
        self.say("do the first")
        self.assertEqual(self.say("no"), "confirmation cancelled")
        self.assertEqual(self.designed, [])

    def test_chat_reply_is_natural_text_from_the_assistant(self):
        self.service.request("r1")
        self.use(FakeAssistant("chat", [], "Morning! 3 stories are waiting for you."))
        self.assertEqual(self.say("hey"), "chat (chat)")
        self.assertEqual(self.outbox()[-1], "Morning! 3 stories are waiting for you.")

    def test_without_gemini_keyword_rules_still_work(self):
        self.service.request("r1")
        self.say("3")
        self.service.assistant = None
        self.assertEqual(self.say("pls send me template 3"), "sent templates [3]")
        self.say("hello there")
        self.assertIn("Reply with story numbers", self.outbox()[-1])

    def test_assistant_falls_back_when_every_model_fails(self):
        from justnews.assistant import Assistant
        from justnews.llm import LLMError

        class Down:
            model = "gemini-down"

            def generate_json(self, *a):
                raise LLMError("HTTP 503: high demand")

        out = Assistant([Down()]).interpret("send me the templates", {})
        self.assertEqual(out["intent"], "send_templates")


def jid(user, server="s.whatsapp.net"):
    return N.JID(User=user, Server=server)


def event(text, sender=jid(OWNER), chat=None, alt=None, from_me=False, group=False, mid="ABC123"):
    src = N.MessageSource(Chat=chat or sender, Sender=sender, IsFromMe=from_me, IsGroup=group)
    if alt:
        src.SenderAlt.CopyFrom(alt)
    return N.Message(Info=N.MessageInfo(MessageSource=src, ID=mid, Timestamp=1762156800),
                     Message=E2E.Message(conversation=text))


class BridgeTests(Base):
    def test_only_direct_text_from_owner_is_accepted(self):
        self.assertEqual(incoming_from_owner(event("1 3"), OWNER), Incoming("ABC123", OWNER, "1 3", 1762156800))
        self.assertIsNone(incoming_from_owner(event("1 3", sender=jid("15550000000")), OWNER))
        self.assertIsNone(incoming_from_owner(event("1 3", from_me=True), OWNER))
        self.assertIsNone(incoming_from_owner(event("1 3", group=True, chat=jid("1203630", "g.us")), OWNER))
        self.assertIsNone(incoming_from_owner(event("   "), OWNER))
        ext = event("")
        ext.Message.CopyFrom(E2E.Message(extendedTextMessage=E2E.ExtendedTextMessage(text="all")))
        self.assertEqual(incoming_from_owner(ext, OWNER).text, "all")

    def test_owner_status_updates_channels_and_spoofed_chats_are_ignored(self):
        status = event("1 3", chat=jid("status", "broadcast"))
        self.assertIsNone(incoming_from_owner(status, OWNER))  # the owner's own Status update
        self.assertIsNone(incoming_from_owner(event("1", chat=jid("1203633", "newsletter")), OWNER))
        self.assertIsNone(incoming_from_owner(event("1", chat=jid("15550000000")), OWNER))

    def test_hidden_number_senders_are_matched(self):
        lid = jid("2763456789012", "lid")
        self.assertIsNotNone(incoming_from_owner(event("1", sender=lid, alt=jid(OWNER)), OWNER))
        self.assertIsNotNone(incoming_from_owner(event("1", sender=lid), OWNER, resolve_lid=lambda j: jid(OWNER)))
        self.assertIsNone(incoming_from_owner(event("1", sender=lid), OWNER, resolve_lid=lambda j: jid("15550000000")))

    def test_bridge_sends_queue_and_retries_failures(self):
        sent = []

        class FakeClient:
            fail_once = True

            def send_message(self, to, text):
                if self.fail_once and "second" in text:
                    self.fail_once = False
                    raise RuntimeError("network")
                sent.append((to.User, text))

            def send_image(self, to, data, caption=None):
                sent.append((to.User, f"image:{len(data)}:{caption}"))

        bridge = WhatsAppBridge(self.root / "s.sqlite3", OWNER, self.db, lambda m: "", self.root / "hb", self.root / "qr.png")
        bridge.client = FakeClient()
        OutboxClient(self.db).send_text(OWNER, "first")
        OutboxClient(self.db).send_text(OWNER, "second")
        OutboxClient(self.db).send_text("15550000000", "to a stranger")
        img = self.root / "t.png"
        img.write_bytes(b"12345")
        OutboxClient(self.db).send_image(OWNER, str(img), "caption")
        self.assertEqual(bridge.send_pending(gap=0), 2)
        self.assertEqual(bridge.send_pending(gap=0), 1)  # the failed one is retried
        self.assertEqual(bridge.send_pending(gap=0), 0)
        self.assertEqual(sent, [(OWNER, "first"), (OWNER, "image:5:caption"), (OWNER, "second")])
        blocked = self.db.conn.execute("SELECT error FROM wa_outbox WHERE to_number = '15550000000'").fetchone()[0]
        self.assertIn("not the owner", blocked)


if __name__ == "__main__":
    unittest.main()
