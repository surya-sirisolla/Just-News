"""WhatsApp as a linked device, the way OpenClaw does it: no Meta account or credentials.

A spare WhatsApp number is linked to this bridge once, by scanning a QR code (WhatsApp >
Settings > Linked devices > Link a device) or entering a pairing code. The session is kept in a
local SQLite file, so later starts reconnect without scanning. The bot then uses that number to
message the owner's number and read the owner's replies.

This speaks the WhatsApp Web protocol through whatsmeow (via the neonize package). It is not an
official API: WhatsApp may restrict or ban numbers it flags, so use a spare number and keep the
volume low. Here that means only a shortlist and a short reply per news cycle, sent only to you.

Only one process may hold the session, so other commands never connect. They queue messages in
the `wa_outbox` table, and the bridge sends them.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger("justnews.whatsapp")

MAX_TEXT = 4096          # keep messages a readable size; long shortlists are split
SEND_GAP_SECONDS = 1.5   # pause between messages, so the account looks human
MAX_ATTEMPTS = 5


class WhatsAppError(Exception):
    pass


@dataclass
class Incoming:
    message_id: str
    sender: str      # phone number, digits only
    text: str
    timestamp: int


class OutboxClient:
    """What the approval service sends through: queues messages for the bridge to deliver."""

    def __init__(self, db):
        self.db = db

    def send_text(self, to: str, text: str) -> str:
        return str(self.db.enqueue_message(to, text))

    def send_image(self, to: str, path: str, caption: str) -> str:
        return str(self.db.enqueue_message(to, caption, media_path=path))


def message_text(msg) -> str:
    m = msg.Message
    return m.conversation or m.extendedTextMessage.text or ""


DIRECT_CHAT_SERVERS = {"s.whatsapp.net", "lid"}  # one-to-one chats; not groups, broadcasts, Status or channels


def incoming_from_owner(msg, owner: str, resolve_lid: Callable | None = None) -> Incoming | None:
    """Turn a neonize MessageEv into an Incoming only if it is a text message the owner sent in
    their direct chat with the bot; otherwise None, and nothing is ever sent back. Anyone else,
    groups, broadcast lists, Status updates (even the owner's own) and channels are ignored. Newer
    WhatsApp accounts address people by a hidden ID (LID) rather than their number, so the
    alternate sender or a LID lookup is checked too."""
    src = msg.Info.MessageSource
    if src.IsFromMe or src.IsGroup or src.Chat.Server not in DIRECT_CHAT_SERVERS:
        return None
    if src.Chat.User not in (src.Sender.User, src.SenderAlt.User):
        return None  # in a direct chat the chat is the sender; anything else is not a private message
    numbers = {src.Sender.User, src.SenderAlt.User} - {""}
    if owner not in numbers and resolve_lid and src.Sender.Server == "lid":
        try:
            numbers.add(resolve_lid(src.Sender).User)
        except Exception:  # lookup is best-effort
            pass
    if owner not in numbers:
        return None
    text = message_text(msg).strip()
    if not text:
        return None
    return Incoming(msg.Info.ID, owner, text, int(msg.Info.Timestamp or 0))


class WhatsAppBridge:
    """Holds the linked-device session: sends queued messages and passes the owner's replies on."""

    def __init__(self, session_path: Path, owner: str, db, on_message: Callable[[Incoming], str],
                 heartbeat_path: Path, qr_png_path: Path, pair_phone: str | None = None):
        self.session_path = session_path
        self.owner = owner
        self.db = db
        self.on_message = on_message
        self.heartbeat_path = heartbeat_path
        self.qr_png_path = qr_png_path
        self.pair_phone = pair_phone
        self.client = None
        self.ready = threading.Event()
        self.inbox: queue.Queue[Incoming] = queue.Queue()

    # --- threads ---------------------------------------------------------------
    def _handle_inbox(self) -> None:
        while True:
            msg = self.inbox.get()
            try:  # designing images takes a while; this keeps WhatsApp's event thread free
                log.info("Owner said %r -> %s", msg.text[:40], self.on_message(msg))
            except Exception:
                log.exception("Failed to handle a WhatsApp message")

    def send_pending(self, gap: float = SEND_GAP_SECONDS) -> int:
        """Send every queued message once; failures are retried on later passes. Returns how many were sent."""
        from neonize.utils import build_jid

        sent = 0
        for row in self.db.pending_messages(MAX_ATTEMPTS):
            if row["to_number"] != self.owner:  # the bot only ever talks to the owner
                self.db.block_message(row["id"], "blocked: recipient is not the owner")
                log.error("Refused to send WhatsApp message %d to a number other than the owner", row["id"])
                continue
            try:
                if row["media_path"]:
                    self.client.send_image(build_jid(row["to_number"]), Path(row["media_path"]).read_bytes(),
                                           caption=row["body"])
                else:
                    self.client.send_message(build_jid(row["to_number"]), row["body"])
                self.db.mark_message_sent(row["id"])
                sent += 1
                log.info("Sent WhatsApp message %d (%d chars)", row["id"], len(row["body"]))
            except Exception as exc:
                self.db.mark_message_failed(row["id"], f"{type(exc).__name__}: {exc}", MAX_ATTEMPTS)
                log.warning("Sending WhatsApp message %d failed: %s", row["id"], exc)
            time.sleep(gap)
        return sent

    def _send_outbox(self) -> None:
        while True:
            self.heartbeat_path.write_text(str(time.time()))
            if self.ready.is_set():
                self.send_pending()
            time.sleep(2)

    # --- connection --------------------------------------------------------------
    def run(self) -> None:
        """Connect (showing a QR code or pairing code the first time) and serve until stopped."""
        from neonize.client import NewClient
        from neonize.events import ConnectedEv, LoggedOutEv, MessageEv, PairStatusEv, TemporaryBanEv

        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        client = NewClient(str(self.session_path))
        self.client = client
        paired = {"code_shown": False}

        def on_qr(_, data: bytes) -> None:
            if self.pair_phone:
                if not paired["code_shown"]:
                    paired["code_shown"] = True
                    code = client.PairPhone(self.pair_phone, show_push_notification=True)
                    print(f"\nOn the spare phone: WhatsApp > Linked devices > Link a device > "
                          f"Link with phone number instead, then enter:  {code}\n", flush=True)
                return
            import segno
            qr = segno.make_qr(data)
            qr.save(str(self.qr_png_path), scale=8, border=2)
            print("\nOn the spare phone: WhatsApp > Settings > Linked devices > Link a device, and scan:",
                  flush=True)
            qr.terminal(compact=True)
            print(f"(Also saved as {self.qr_png_path}. Codes refresh every ~20 seconds.)\n", flush=True)

        client.event.qr(on_qr)

        @client.event(PairStatusEv)
        def _paired(_, ev) -> None:
            log.info("Linked as %s", ev.ID.User)
            self.qr_png_path.unlink(missing_ok=True)

        @client.event(ConnectedEv)
        def _connected(_, __) -> None:
            log.info("WhatsApp connected; approvals are accepted only from %s", self.owner)
            self.ready.set()

        @client.event(LoggedOutEv)
        def _logged_out(_, __) -> None:
            self.ready.clear()
            log.error("This device was unlinked from WhatsApp. Delete %s and run the bridge again to relink.",
                      self.session_path)

        @client.event(TemporaryBanEv)
        def _banned(_, ev) -> None:
            self.ready.clear()
            log.error("WhatsApp temporarily banned the linked number: %s", ev)

        @client.event(MessageEv)
        def _message(c, msg) -> None:
            incoming = incoming_from_owner(msg, self.owner, c.get_pn_from_lid)
            if incoming:
                self.inbox.put(incoming)

        threading.Thread(target=self._handle_inbox, name="wa-inbox", daemon=True).start()
        threading.Thread(target=self._send_outbox, name="wa-outbox", daemon=True).start()
        client.connect()  # blocks until stopped (Ctrl+C)


def bridge_running(heartbeat_path: Path, max_age: float = 120) -> bool:
    try:
        return time.time() - float(heartbeat_path.read_text()) < max_age
    except (OSError, ValueError):
        return False
