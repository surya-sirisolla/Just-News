"""WhatsApp approval: the owner chooses which draft posts get an image and get posted.

  request(run_id)   send the owner a numbered shortlist of draft posts (headline, caption,
                    hashtags, source, link). Text only: no images are sent for confirmation.
  handle_message()  read the owner's reply ("1 3 5", "1-3", "all", "none"), mark those posts
                    approved and the rest rejected, design images for the approved ones only, and
                    send one short text acknowledgement. Anything else ("send me the templates",
                    "do the Apple one", "what's pending?") goes to the assistant (justnews/assistant.py),
                    which answers in natural language; templates are sent as images only when asked.

Messages go out through the linked-device bridge (justnews/whatsapp.py), or are printed when
WhatsApp isn't set up. Only the configured owner number can approve.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .config import Settings
from .db import Database
from .assistant import fallback
from .whatsapp import MAX_TEXT, Incoming

log = logging.getLogger("justnews.approval")

STALE_SECONDS = 300  # older messages are only acted on if a shortlist is open
CONTEXT_HOURS = 48   # how long a finished shortlist stays available for "send me the templates"
SHOW_WORDS = {"list", "show list", "shortlist"}
NONE_WORDS = {"none", "skip", "0", "nothing"}
YES_WORDS = {"yes", "y", "yes please", "yeah", "yep", "ok", "okay", "confirm", "go ahead", "sure", "haan", "ha", "avunu"}
NO_WORDS = {"no", "n", "nope", "cancel", "wait", "stop", "vaddu"}


def parse_selection(text: str, count: int) -> list[int] | None:
    """"1 3 5", "1,3", "1-3", "all" or "none" -> sorted item numbers ([] for none); None if unreadable."""
    t = text.strip().lower()
    if t in ("all", "everything", "all of them"):
        return list(range(1, count + 1))
    if t in NONE_WORDS:
        return []
    if not re.fullmatch(r"[\d\s,\-–&and]+", t) or not re.search(r"\d", t):
        return None
    chosen: set[int] = set()
    for a, b in re.findall(r"(\d+)\s*[-–]\s*(\d+)", t):
        chosen.update(range(int(a), int(b) + 1))
    t = re.sub(r"\d+\s*[-–]\s*\d+", " ", t)
    chosen.update(int(n) for n in re.findall(r"\d+", t))
    if not chosen or any(n < 1 or n > count for n in chosen):
        return None
    return sorted(chosen)


def _chunks(blocks: list[str], footer: str) -> list[str]:
    """Join item blocks into messages under WhatsApp's 4096-character limit; footer goes last."""
    messages, current = [], ""
    for block in blocks + [footer]:
        if current and len(current) + 2 + len(block) > MAX_TEXT - 50:
            messages.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}" if current else block
    messages.append(current)
    return messages


class ApprovalService:
    def __init__(self, settings: Settings, db: Database, client, designer: Callable[[str, list[str]], object],
                 owner: str | None = None, assistant=None):
        self.settings = settings
        self.db = db
        self.client = client
        self.designer = designer  # (run_id, approved article ids) -> image result
        self.owner = owner or settings.whatsapp_owner
        self.assistant = assistant  # free-text understanding; None -> keyword rules only

    # --- outgoing ---------------------------------------------------------------
    def request(self, run_id: str) -> dict:
        posts = self.db.posts_awaiting_approval(run_id, self.settings.max_posts_per_run)
        if not posts:
            log.info("Run %s: no draft posts waiting for approval", run_id)
            return {"run_id": run_id, "requested": 0, "status": "nothing to approve"}
        items = {str(i): p["article_id"] for i, p in enumerate(posts, 1)}
        expires = datetime.now(timezone.utc) + timedelta(hours=self.settings.approval_expiry_hours)
        request_id = self.db.create_approval_request(run_id, items, expires.isoformat(timespec="seconds"), "sent")
        self._send_shortlist(request_id)
        status = "shortlist sent"
        log.info("Run %s: approval request %d for %d posts (%s)", run_id, request_id, len(items), status)
        return {"run_id": run_id, "request_id": request_id, "requested": len(items), "status": status}

    def _send(self, text: str) -> None:
        self.client.send_text(self.owner, text)

    def _send_shortlist(self, request_id: int) -> None:
        req = self.db.approval_request(request_id)
        items = json.loads(req["items"])
        posts = {p["article_id"]: p for p in self.db.posts_for_run(req["run_id"])}
        blocks = [self._format_item(n, posts[aid]) for n, aid in items.items() if aid in posts]
        header = f"📰 *Just News* – {len(blocks)} stories ready to post"
        footer = (f"Reply with the numbers to create a template and post, e.g. *1 3* or *1-3*, "
                  f"or *all* / *none*. This list expires in {self.settings.approval_expiry_hours:g} hours.")
        for text in _chunks([header] + blocks, footer):
            self._send(text)
        self.db.set_approval_status(request_id, "sent")

    @staticmethod
    def _format_item(n: str, p) -> str:
        lines = [f"*{n}. {p['headline']}*", p["caption"], " ".join(json.loads(p["hashtags"])), p["attribution"],
                 f"_{p['category']}_" + (f" · score {p['overall']}" if p["overall"] is not None else "")]
        if p["status"] == "needs_review":
            lines.append(f"⚠️ {p['review_note']}")
        return "\n".join(lines)

    # --- incoming ---------------------------------------------------------------
    def handle_message(self, msg: Incoming) -> str:
        """Process one incoming WhatsApp message; returns what happened (for logs and the CLI)."""
        if msg.sender != self.owner:
            log.warning("Ignored a message from a number ending %s: not the owner", msg.sender[-4:])
            return "ignored: not the owner"
        if not self.db.record_inbound(msg.message_id, msg.sender, msg.text):
            return "ignored: duplicate delivery"

        text = msg.text.strip()
        lower = text.lower().rstrip("!. ")
        req = self.db.open_approval_request()
        if req and datetime.now(timezone.utc) > datetime.fromisoformat(req["expires_at"]):
            self.db.set_approval_status(req["id"], "expired")
            req = None
        stale = msg.timestamp and datetime.now(timezone.utc).timestamp() - msg.timestamp > STALE_SECONDS
        if req is None and stale:
            return "ignored: old message redelivered after a reconnect"

        if req is not None:
            items = json.loads(req["items"])
            pending = json.loads(req["pending"]) if req["pending"] else None
            if pending is not None and lower in YES_WORDS:
                return self._approve(req, pending, msg.text)
            if pending is not None and lower in NO_WORDS:
                self.db.set_pending_approval(req["id"], None)
                return self._reply("No problem, I haven't changed anything. Reply with the numbers whenever you're ready.",
                                   "confirmation cancelled")
            if lower in SHOW_WORDS:
                self._send_shortlist(req["id"])
                return "shortlist sent"
            chosen = parse_selection(text, len(items))
            if chosen is not None:  # plain numbers / all / none are unambiguous: act at once
                return self._approve(req, chosen, msg.text)
        return self._converse(text, req)

    def _approve(self, req, chosen: list[int], reply_text: str) -> str:
        items = json.loads(req["items"])
        approved = [items[str(n)] for n in chosen]
        rejected = [aid for aid in items.values() if aid not in approved]
        self.db.set_post_approval(approved, "approved")
        self.db.set_post_approval(rejected, "rejected")
        self.db.set_pending_approval(req["id"], None)
        self.db.set_approval_status(req["id"], "answered", reply=reply_text)
        if not approved:
            return self._reply("👍 Okay, I'll skip all of these. Nothing will be posted.", "none approved")
        numbers = _join(chosen)
        result = self.designer(req["run_id"], approved)
        made = getattr(result, "photos", 0) + getattr(result, "graphics", 0)
        failed = getattr(result, "failed", 0)
        text = f"Done! ✅ I made the template{'s' if made != 1 else ''} for {numbers}."
        if failed:
            text += f" {failed} didn't work out, so I've logged the error."
        text += (" Say *send me the templates* if you'd like to see them here. Posting isn't connected yet, "
                 "so they're saved as approved.")
        return self._reply(text, f"approved {', '.join(map(str, chosen))}")

    # --- free-form conversation -------------------------------------------------------
    def _recent_request(self, req):
        if req is not None:
            return req
        last = self.db.latest_approval_request()
        if last and datetime.now(timezone.utc) - datetime.fromisoformat(last["created_at"]) < timedelta(hours=CONTEXT_HOURS):
            return last
        return None

    def _stories(self, req) -> dict[int, dict]:
        if req is None:
            return {}
        posts = {p["article_id"]: p for p in self.db.posts_for_run(req["run_id"])}
        return {int(n): {"headline": posts[aid]["headline"], "approval": posts[aid]["approval"] or "waiting",
                         "template": posts[aid]["image_path"], "caption": posts[aid]["caption"],
                         "hashtags": " ".join(json.loads(posts[aid]["hashtags"])),
                         "attribution": posts[aid]["attribution"]}
                for n, aid in json.loads(req["items"]).items() if aid in posts}

    def _converse(self, text: str, open_req) -> str:
        req = self._recent_request(open_req)
        stories = self._stories(req)
        context = {
            "shortlist_open": open_req is not None,
            "waiting_for_yes_on": json.loads(open_req["pending"]) if open_req is not None and open_req["pending"] else None,
            "stories": [{"number": n, "headline": s["headline"], "approval": s["approval"],
                         "template_ready": bool(s["template"])} for n, s in stories.items()],
        }
        if not stories:
            context["note"] = "No shortlist in the last 48 hours; the next one comes with the next news cycle."
        out = self.assistant.interpret(text, context) if self.assistant else fallback(text)
        intent, numbers, reply = out["intent"], [n for n in out["numbers"] if n in stories], out["reply"].strip()

        if intent == "send_templates":
            return self._send_templates(stories, numbers, reply)
        if intent == "show_shortlist" and req is not None:
            self._send_shortlist(req["id"]) if open_req is not None else self._send(self._summary(stories))
            return "shortlist sent"
        if intent == "approve" and open_req is not None and numbers:
            self.db.set_pending_approval(open_req["id"], numbers)
            names = "; ".join(f"{n}. {stories[n]['headline']}" for n in numbers)
            return self._reply(f"Just to be sure, you want templates for: {names}? Reply *yes* and I'll make them.",
                               f"asked to confirm {numbers}")
        if intent == "approve" and open_req is None:
            return self._reply(reply or "That shortlist is closed now, so I can't add to it. The next one "
                                        "comes with the next news cycle.", "approve after close")
        if intent == "reject_all" and open_req is not None:
            return self._approve(open_req, [], text)
        if intent == "status":
            return self._reply(reply or self._summary(stories), "status")
        return self._reply(reply or fallback(text)["reply"], f"chat ({intent})")

    def _send_templates(self, stories: dict[int, dict], numbers: list[int], reply: str) -> str:
        ready = {n: s for n, s in stories.items() if s["template"] and Path(s["template"]).exists()}
        wanted = numbers or sorted(ready)
        to_send = [n for n in wanted if n in ready]
        missing = [n for n in wanted if n not in ready]
        if not to_send:
            hint = (f"{_join(missing)} {'has' if len(missing) == 1 else 'have'} no template yet."
                    if missing else "There are no templates yet.")
            return self._reply(f"{hint} Reply with the numbers from the list and I'll make them.", "no templates to send")
        self._send(reply or "Here you go 👇")
        for n in to_send[:10]:
            s = stories[n]
            self.client.send_image(self.owner, s["template"],
                                   f"*{n}. {s['headline']}*\n{s['caption']}\n{s['hashtags']}\n{s['attribution']}")
        if missing:
            one = len(missing) == 1
            self._send(f"{_join(missing)} {'is not' if one else 'are not'} made yet. "
                       f"Reply with {'that number' if one else 'those numbers'} if you want {'it' if one else 'them'}.")
        return f"sent templates {to_send}"

    @staticmethod
    def _summary(stories: dict[int, dict]) -> str:
        if not stories:
            return "Nothing's waiting right now. The next shortlist comes with the next news cycle."
        lines = [f"{n}. {s['headline']}: {s['approval']}" + (", template ready" if s["template"] else "")
                 for n, s in stories.items()]
        return "Here's where things stand:\n" + "\n".join(lines)

    def _reply(self, text: str, outcome: str) -> str:
        self._send(text)
        return outcome


def _join(numbers: list[int]) -> str:
    nums = [str(n) for n in numbers]
    return nums[0] if len(nums) == 1 else ", ".join(nums[:-1]) + " and " + nums[-1]
