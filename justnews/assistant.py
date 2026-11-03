"""Conversational layer for the WhatsApp bot: understands free-form messages from the owner.

Plain replies ("1 3", "all", "none", "list") are handled by code without a model. Anything else
goes to Gemini (free tier), together with the current shortlist. Gemini returns an intent, the
story numbers it refers to, and a short natural reply; code then checks the numbers and performs
the action. When no Gemini model answers, keyword rules give a plainer reply instead.
"""

from __future__ import annotations

import json
import logging
import re

from .llm import LLMError

log = logging.getLogger("justnews.assistant")

INTENTS = ["approve", "send_templates", "show_shortlist", "reject_all", "status", "chat"]

SYSTEM_PROMPT = """You are the Just News assistant, chatting on WhatsApp with the account owner,
who decides which news stories get a designed template (image) and get posted.

Talk like a friendly, capable colleague: warm, natural and brief (1-2 short sentences), at most one
emoji. Reply in the same language as the owner's latest message: English by default, and Hindi,
Hinglish or Telugu only when that message is written that way.
Use only the facts in CONTEXT; never invent stories, numbers or templates. Say a story was posted
somewhere only if that platform is in its posted_to. Approving a story posts it to every platform in
posting_connected_to (none means templates are made but not posted). If asked whether you are a bot
or an AI, say honestly that you are an AI assistant.

Choose one intent:
- approve: the owner wants templates made for specific stories ("do 1 and 3", "go with the
  Apple one"). Put the story numbers in `numbers`. Their wording may be vague, so the app asks
  them to confirm before acting.
- send_templates: the owner wants to see templates or images ("send me the template",
  "show me number 2"). Put the numbers in `numbers`, or leave it empty for all ready templates.
- show_shortlist: they want the list of stories again.
- reject_all: they want none of the stories.
- status: they ask what's going on, what's pending, or what's ready.
- chat: anything else (greetings, thanks, questions). Answer helpfully, and mention that they can
  reply with story numbers or ask for templates when that helps.

`reply` is the message to send back. For send_templates, write a short line to go before the
images. For approve, write nothing important: the app writes the confirmation question."""

SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": INTENTS},
        "numbers": {"type": "array", "items": {"type": "integer"}},
        "reply": {"type": "string"},
    },
    "required": ["intent", "numbers", "reply"],
}

TEMPLATE_WORDS = re.compile(r"\b(template|templates|image|images|pic|pics|picture|photo|design|designs|send)\b", re.I)
LIST_WORDS = re.compile(r"\b(list|stories|news|shortlist|options)\b", re.I)
STATUS_WORDS = re.compile(r"\b(status|pending|ready|done|update)\b", re.I)


def fallback(text: str) -> dict:
    """Keyword rules, used when no Gemini model is available."""
    numbers = [int(n) for n in re.findall(r"\d+", text)]
    if TEMPLATE_WORDS.search(text):
        return {"intent": "send_templates", "numbers": numbers, "reply": "Here you go 👇"}
    if LIST_WORDS.search(text):
        return {"intent": "show_shortlist", "numbers": [], "reply": ""}
    if STATUS_WORDS.search(text):
        return {"intent": "status", "numbers": [], "reply": ""}
    return {"intent": "chat", "numbers": [], "reply": (
        "Hi! Reply with story numbers (like *1 3*) and I'll make the templates, or say "
        "*send me the templates* to see the ones that are ready.")}


class Assistant:
    def __init__(self, clients: list):
        self.clients = clients  # GeminiClient-like objects, tried in order

    def interpret(self, text: str, context: dict) -> dict:
        prompt = f"CONTEXT:\n{json.dumps(context, ensure_ascii=False)}\n\nOWNER SAYS:\n{text[:1000]}"
        for client in self.clients:
            try:
                out = client.generate_json(SYSTEM_PROMPT, prompt, SCHEMA)
            except LLMError as exc:
                log.warning("  %s could not answer (%s); trying the next model", client.model, str(exc)[:120])
                continue
            if isinstance(out, dict) and out.get("intent") in INTENTS:
                numbers = [n for n in out.get("numbers") or [] if isinstance(n, int)]
                return {"intent": out["intent"], "numbers": numbers, "reply": str(out.get("reply") or "")[:1000]}
        return fallback(text)
