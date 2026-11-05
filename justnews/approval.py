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
from zoneinfo import ZoneInfo
from typing import Callable

from .agents.publisher import field_of, instagram_caption, source_line
from .agents.x_publisher import x_text
from .config import Settings
from .quiet import local_time, quiet_until
from .db import Database
from .assistant import fallback
from .whatsapp import MAX_TEXT, Incoming

log = logging.getLogger("justnews.approval")

STALE_SECONDS = 300  # older messages are only acted on if a shortlist is open
CONTEXT_HOURS = 48   # how long a finished shortlist stays available for "send me the templates"
PLATFORM_LABELS = {"instagram": "Instagram", "x": "X"}
ACK_RE = re.compile(r"^\s*(ok(ay)?|k|kk|thanks?( you)?|thx|ty|tq|great|nice|cool|good|super|awesome|perfect|"
                    r"got it|fine|noted|done|👍|🙏|❤️|👌|🔥|💯|✅)(\s*(ok(ay)?|thanks?( you)?|👍|🙏|❤️|👌|🔥|💯|✅))*"
                    r"[\s!.]*$", re.I)
PREVIEW_MAX = 300   # characters of the Instagram caption shown in the shortlist
CAPTION_LIMIT = 1000  # WhatsApp image captions are cut around 1024
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
                 owner: str | None = None, assistant=None,
                 publishers: dict[str, Callable[[str, list[str]], object]] | None = None,
                 researcher: Callable[..., None] | None = None,
                 editor: Callable[[str, list[str], str], dict[str, str]] | None = None):
        self.settings = settings
        self.db = db
        self.client = client
        self.designer = designer  # (run_id, approved article ids) -> image result
        self.owner = owner or settings.whatsapp_owner
        self.assistant = assistant  # free-text understanding; None -> keyword rules only
        # Platform label -> (run_id, article ids) -> PublishResult; empty -> posting not connected.
        self.publishers = publishers or {}
        # (topic, the owner's words, owner's instruction) -> starts an on-demand search that sends its own list.
        self.researcher = researcher
        # (run_id, article ids, the owner's instruction) -> {article id: the writer's reply}: rewrites posts.
        self.editor = editor

    # --- outgoing ---------------------------------------------------------------
    def request(self, run_id: str, title: str | None = None, limit: int | None = None) -> dict:
        posts = self.db.posts_awaiting_approval(run_id, limit or self.settings.max_posts_per_run)
        if not posts:
            log.info("Run %s: no draft posts waiting for approval", run_id)
            return {"run_id": run_id, "requested": 0, "status": "nothing to approve"}
        items = {str(i): p["article_id"] for i, p in enumerate(posts, 1)}
        expires = datetime.now(timezone.utc) + timedelta(hours=self.settings.approval_expiry_hours)
        request_id = self.db.create_approval_request(run_id, items, expires.isoformat(timespec="seconds"), "sent")
        self._send_shortlist(request_id, title)
        status = "shortlist sent"
        log.info("Run %s: approval request %d for %d posts (%s)", run_id, request_id, len(items), status)
        return {"run_id": run_id, "request_id": request_id, "requested": len(items), "status": status}

    def _send(self, text: str) -> None:
        self.client.send_text(self.owner, text)

    def _react(self, msg: Incoming | None, emoji: str) -> None:
        """An emoji on the owner's message instead of (or before) a text reply."""
        if msg is not None and hasattr(self.client, "react"):
            self.client.react(self.owner, msg, emoji)

    def _send_shortlist(self, request_id: int, title: str | None = None) -> None:
        req = self.db.approval_request(request_id)
        items = json.loads(req["items"])
        posts = {p["article_id"]: p for p in self.db.posts_for_run(req["run_id"])}
        blocks = [self._format_item(n, posts[aid]) for n, aid in items.items() if aid in posts]
        local = datetime.now(ZoneInfo(self.settings.timezone)).strftime("%-I:%M %p")
        header = f"{title} · {local} – {len(blocks)} {'story' if len(blocks) == 1 else 'stories'}" if title \
            else f"📰 *Just News* · {local} – {len(blocks)} new stories"
        following = datetime.now(timezone.utc) + timedelta(minutes=self.settings.cycle_minutes)
        quiet_end = quiet_until(self.settings, following)
        when = (f"at {local_time(self.settings, quiet_end)} (quiet hours until then)" if quiet_end
                else f"in about {_duration(self.settings.cycle_minutes)}")
        footer = (f"Reply with the numbers to post, e.g. *1 3* or *1-3*, or *all* / *none*. "
                  f"If you skip this list, it's ignored and the next one comes {when}.")
        for text in _chunks([header] + blocks, footer):
            self._send(text)
        self.db.set_approval_status(request_id, "sent")

    @staticmethod
    def _format_item(n: str, p) -> str:
        ig, x = field_of(p, "ig_caption"), field_of(p, "x_post")
        if ig or x:  # the platform versions; the full ones come with "send me the templates"
            ig = " ".join((ig or "").split())  # one paragraph, so each story stays one block
            preview = ig if len(ig) <= PREVIEW_MAX else ig[:PREVIEW_MAX - 1].rsplit(" ", 1)[0] + "…"
            body = [f"📸 {preview}"] * bool(ig) + [f"𝕏 {x}"] * bool(x)
        else:
            body = [p["caption"]]
        lines = [f"*{n}. {p['headline']}*", *body, " ".join(json.loads(p["hashtags"])), source_line(p),
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
                self._react(msg, "👍")
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
                self._react(msg, "👍")
                return self._approve(req, chosen, msg.text)
        if ACK_RE.match(text):  # "ok", "thanks", 👍: a heart, no reply and no model call
            self._react(msg, "❤️")
            return "acknowledged"
        return self._converse(text, req, msg)

    def _approve(self, req, chosen: list[int], reply_text: str) -> str:
        items = json.loads(req["items"])
        approved = [items[str(n)] for n in chosen]
        rejected = [aid for aid in items.values() if aid not in approved]
        self.db.set_post_approval(approved, "approved")
        self.db.set_post_approval(rejected, "rejected")
        self.db.set_pending_approval(req["id"], None)
        self.db.set_approval_status(req["id"], "answered", reply=reply_text)
        if not approved:
            return "none approved"  # the 👍 on the owner's message says it all
        numbers = _join(chosen)
        result = self.designer(req["run_id"], approved)
        made = getattr(result, "photos", 0) + getattr(result, "graphics", 0)
        failed = getattr(result, "failed", 0)
        if not self.publishers:
            text = f"Done! ✅ I made the template{'s' if made != 1 else ''} for {numbers}."
            if failed:
                text += f" {failed} didn't work out, so I've logged the error."
            text += (" Say *send me the templates* if you'd like to see them here. Posting isn't connected yet, "
                     "so they're saved as approved.")
            return self._reply(text, f"approved {', '.join(map(str, chosen))}")

        number_of = {aid: n for n, aid in items.items()}
        blocks, total = [], 0
        for label, publish in self.publishers.items():
            try:
                published = publish(req["run_id"], approved)
            except Exception as exc:  # one platform's crash must not hide the other's links
                log.exception("Publishing to %s failed", label)
                blocks.append(f"Not posted on {label}: {type(exc).__name__}, see the logs")
                continue
            by_link: dict[str, list[str]] = {}
            for aid, link in published.posted:
                by_link.setdefault(link or "(posted)", []).append(number_of[aid])
            singles = [f"{ns[0]}. {link}" for link, ns in by_link.items() if len(ns) == 1]
            for link, ns in by_link.items():
                if len(ns) > 1:  # several stories in one post: an Instagram carousel
                    blocks.append(f"Posted {_join(ns)} to {label} as one carousel:\n{link}")
            if singles:
                blocks.append(f"Posted {len(singles)} to {label}:\n" + "\n".join(singles))
            total += len(published.posted)
            queued = [number_of[aid] for aid, _ in getattr(published, "queued", [])]
            if queued:
                note = getattr(published, "note", "")
                blocks.append(f"Queued {_join(queued)} for {label}" + (f" ({note})" if note else "") +
                              ". I'll send the links when they're up.")
                total += len(queued)
            problems: dict[str, list[str]] = {}
            for aid, why in published.failed + published.skipped:
                problems.setdefault(why[:150], []).append(number_of.get(aid, "?"))
            if problems:
                blocks.append(f"Not posted on {label}: " + "; ".join(f"{_join(ns)} ({why})" for why, ns in problems.items()))
        text = "\n\n".join(blocks)
        text = f"Done! ✅ {text}" if total else f"I made the templates, but nothing could be posted.\n\n{text}".rstrip()
        return self._reply(text, f"approved {', '.join(map(str, chosen))}, posted {total}")

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
                         "posted": [PLATFORM_LABELS.get(x, x) for x in self.db.published_platforms(aid)],
                         "template": posts[aid]["image_path"], "caption": posts[aid]["caption"],
                         "hashtags": " ".join(json.loads(posts[aid]["hashtags"])),
                         "instagram_text": instagram_caption(posts[aid]), "x_text": x_text(posts[aid]),
                         "attribution": posts[aid]["attribution"], "source": posts[aid]["source_name"]}
                for n, aid in json.loads(req["items"]).items() if aid in posts}

    def _converse(self, text: str, open_req, msg: Incoming | None = None) -> str:
        req = self._recent_request(open_req)
        stories = self._stories(req)
        context = {
            "shortlist_open": open_req is not None,
            "waiting_for_yes_on": json.loads(open_req["pending"]) if open_req is not None and open_req["pending"] else None,
            "posting_connected_to": list(self.publishers),
            "stories": [{"number": n, "headline": s["headline"], "facts": s["caption"][:300],
                         "source": s["source"], "approval": s["approval"],
                         "template_ready": bool(s["template"]), "posted_to": s["posted"]}
                        for n, s in stories.items()],
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
            self._react(msg, "👍")
            return self._approve(open_req, [], text)
        if intent == "edit":
            return self._edit(req, stories, numbers, out.get("instruction") or text, msg)
        if intent == "status":
            return self._reply(reply or self._summary(stories), "status")
        if intent == "research":
            query = out.get("query") or text
            if self.researcher is None:
                return self._reply("I can't search on request here yet.", "research unavailable")
            self._react(msg, "👀")  # the list itself is the answer
            self.researcher(query, text, out.get("instruction") or "")
            return f"research {query!r}"
        return self._reply(reply or fallback(text)["reply"], f"chat ({intent})")

    def _edit(self, req, stories: dict[int, dict], numbers: list[int], instruction: str, msg) -> str:
        """The owner's change to listed stories goes to the content creator; the new versions come back."""
        if self.editor is None or req is None:
            return self._reply("I can't rewrite stories here yet.", "edit unavailable")
        if not numbers:
            return self._reply("Which story should I change? Tell me its number, e.g. *make 2 simpler*.", "edit: which?")
        self._react(msg, "✍️")
        items = json.loads(req["items"])
        replies = self.editor(req["run_id"], [items[str(n)] for n in numbers], instruction)
        posts = {p["article_id"]: p for p in self.db.posts_for_run(req["run_id"])}
        for n in numbers:
            aid = items[str(n)]
            note = replies.get(aid, "")
            self._send(f"✏️ *Updated {n}*\n" + self._format_item(str(n), posts[aid]) +
                       (f"\n\n_Writer: {note}_" if note else ""))
        return f"edited {numbers}"

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
            s = stories[n]  # exactly what each platform gets
            self.client.send_image(self.owner, s["template"], f"*{n}.* 📸 Instagram\n{s['instagram_text']}"[:CAPTION_LIMIT])
            self._send(f"*{n}.* 𝕏 X\n{s['x_text']}")
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


def _duration(minutes: int) -> str:
    return f"{minutes // 60} hour{'s' if minutes >= 120 else ''}" if minutes % 60 == 0 else f"{minutes} minutes"


def _join(numbers: list[int]) -> str:
    nums = [str(n) for n in numbers]
    return nums[0] if len(nums) == 1 else ", ".join(nums[:-1]) + " and " + nums[-1]
