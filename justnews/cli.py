"""Command line entry points. Each agent can be run on its own (this is what the
PicoClaw skills call), or both together with `run`.

  python -m justnews discover            # Agent 1 -> prints run id + discovery stats
  python -m justnews evaluate [RUN_ID]   # Agent 2 on a run (default: latest)
  python -m justnews report   [RUN_ID]   # structured report (--format md|json)
  python -m justnews create   [RUN_ID]   # Agent 3: draft posts for recommended articles
  python -m justnews design   [RUN_ID]   # Agent 4: an image for each draft post
  python -m justnews whatsapp            # WhatsApp bridge: link a spare number (QR), then keep running
  python -m justnews request-approval [RUN_ID]  # queue the shortlist for the owner on WhatsApp
  python -m justnews reply "1 3"         # apply an approval reply from the command line
  python -m justnews publish  [RUN_ID]   # Agent 5: post approved templates to Instagram
  python -m justnews run                 # agents 1-3, then the approval request (or agent 4 if
                                         # WhatsApp isn't configured), then the report
  python -m justnews serve [--every 60]  # production: WhatsApp bridge + a news cycle every hour
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

from .agents.content import ContentCreatorAgent
from .agents.discovery import NewsDiscoveryAgent
from .agents.evaluator import NewsEvaluatorAgent
from .agents.images import ImageDesignerAgent
from .agents.publisher import InstagramPublisher
from .approval import ApprovalService
from .assistant import Assistant
from .whatsapp import Incoming, OutboxClient, WhatsAppBridge, WhatsAppError, bridge_running
from .config import settings
from .db import Database
from .llm import GeminiClient, LLMError
from .logging_setup import setup_logging
from .report import REPORTS_DIR, build_report, render

log = logging.getLogger("justnews")


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _resolve_run(db: Database, run_id: str | None) -> str:
    run_id = run_id or db.latest_run_id()
    if not run_id:
        raise SystemExit("No runs found. Run `python -m justnews discover` first.")
    return run_id


def cmd_discover(db: Database, args) -> str:
    result = NewsDiscoveryAgent(settings, db).run(args.run_id or new_run_id())
    print(json.dumps({"run_id": result.run_id, **result.stats()}, indent=2))
    return result.run_id


def cmd_evaluate(db: Database, args) -> str:
    run_id = _resolve_run(db, args.run_id)
    result = NewsEvaluatorAgent(settings, db).run(run_id)
    print(json.dumps({"run_id": run_id, **result.stats()}, indent=2))
    if result.llm_error and not result.evaluations:
        raise LLMError(result.llm_error)
    return run_id


def cmd_create(db: Database, args) -> str:
    run_id = _resolve_run(db, args.run_id)
    result = ContentCreatorAgent(settings, db).run(run_id)
    print(json.dumps({"run_id": run_id, **result.stats()}, indent=2))
    if result.error and not result.created and result.pending:
        raise LLMError(result.error)
    return run_id


def cmd_design(db: Database, args) -> str:
    run_id = _resolve_run(db, args.run_id)
    result = ImageDesignerAgent(settings, db).run(run_id)
    print(json.dumps({"run_id": run_id, **result.stats()}, indent=2))
    return run_id


class ConsoleClient:
    """Stand-in for WhatsApp when it isn't configured: prints the messages instead."""

    def send_text(self, to: str, text: str) -> str:
        print(f"\n----- WhatsApp message -----\n{text}\n----------------------------")
        return f"console-{uuid.uuid4().hex[:12]}"

    def send_image(self, to: str, path: str, caption: str) -> str:
        print(f"\n----- WhatsApp image: {path} -----\n{caption}\n----------------------------")
        return f"console-{uuid.uuid4().hex[:12]}"


def approval_service(db: Database) -> ApprovalService:
    def designer(run_id: str, article_ids: list[str]):
        return ImageDesignerAgent(settings, db).run(run_id, article_ids)

    assistant = None
    if settings.gemini_api_key:
        assistant = Assistant([GeminiClient(settings.gemini_api_key, m, 0.0, 0.0, thinking_level="minimal",
                                            min_interval=settings.gemini_min_interval, temperature=0.6,
                                            timeout=20, retries=0) for m in settings.gemini_chat_models])
    publisher = None
    if settings.instagram_enabled:
        def publisher(run_id: str, article_ids: list[str]):
            return InstagramPublisher(settings, db).publish(run_id, article_ids)
    if settings.whatsapp_enabled:
        return ApprovalService(settings, db, OutboxClient(db), designer, assistant=assistant, publisher=publisher)
    log.warning("WHATSAPP_OWNER_NUMBER is not set; approval messages are printed here instead")
    return ApprovalService(settings, db, ConsoleClient(), designer, owner="console", assistant=assistant,
                           publisher=publisher)


def cmd_request_approval(db: Database, args) -> str:
    run_id = _resolve_run(db, args.run_id)
    result = approval_service(db).request(run_id)
    if settings.whatsapp_enabled:
        running = bridge_running(settings.whatsapp_heartbeat)
        result = {"channel": "whatsapp", "bridge_running": running, **result}
        if not running and result.get("requested"):
            result["status"] = "queued; sent when the WhatsApp bridge runs (python -m justnews whatsapp)"
    else:
        result = {"channel": "console (WHATSAPP_OWNER_NUMBER not set)", **result}
    print(json.dumps(result, indent=2))
    return run_id


def cmd_reply(db: Database, args) -> str:
    service = approval_service(db)
    outcome = service.handle_message(Incoming(f"cli-{uuid.uuid4().hex[:12]}", service.owner, args.text, 0))
    print(json.dumps({"outcome": outcome}))
    return ""


def cmd_whatsapp(db: Database, args) -> str:
    if not settings.whatsapp_enabled:
        raise SystemExit("Set WHATSAPP_OWNER_NUMBER in .env (your own number, with country code, no +) first.")
    service = approval_service(db)
    bridge = WhatsAppBridge(settings.whatsapp_session, settings.whatsapp_owner, db, service.handle_message,
                            settings.whatsapp_heartbeat, settings.whatsapp_qr_png,
                            pair_phone=(args.pair_code or "").lstrip("+").replace(" ", "") or None)
    log.info("Starting the WhatsApp bridge (Ctrl+C to stop); session file: %s", settings.whatsapp_session)
    bridge.run()
    return ""


def cmd_publish(db: Database, args) -> str:
    """Post a run's approved templates that aren't on Instagram yet (approvals normally do this)."""
    if not settings.instagram_enabled:
        raise SystemExit("Set INSTAGRAM_ACCESS_TOKEN, INSTAGRAM_USER_ID and JUSTNEWS_S3_BUCKET in .env first.")
    run_id = _resolve_run(db, args.run_id)
    approved = [p["article_id"] for p in db.posts_for_run(run_id) if p["approval"] == "approved" and p["image_path"]]
    result = InstagramPublisher(settings, db).publish(run_id, approved)
    print(json.dumps({"run_id": run_id, "posted": result.posted, "failed": result.failed,
                      "skipped": result.skipped}, indent=2))
    return run_id


def cmd_report(db: Database, args) -> str:
    run_id = _resolve_run(db, args.run_id)
    report = build_report(db, run_id)
    text = render(report, args.format)
    REPORTS_DIR.mkdir(exist_ok=True)
    path = REPORTS_DIR / f"run-{run_id}.{'json' if args.format == 'json' else 'md'}"
    path.write_text(text, encoding="utf-8")
    print(text)
    log.info("Report saved to %s", path)
    return run_id


def run_cycle(db: Database, run_id: str | None = None) -> str:
    """Agents 1-3, then the WhatsApp shortlist (or Agent 4 for every draft without WhatsApp)."""
    run_id = run_id or new_run_id()
    NewsDiscoveryAgent(settings, db).run(run_id)
    NewsEvaluatorAgent(settings, db).run(run_id)
    try:
        ContentCreatorAgent(settings, db).run(run_id)
    except LLMError as exc:  # e.g. no Gemini key: keep the evaluation results and report them
        log.error("Skipping post creation: %s", exc)
    if settings.whatsapp_enabled:
        # Queued for the WhatsApp bridge; images are made only for the posts the owner picks.
        approval_service(db).request(run_id)
    else:
        ImageDesignerAgent(settings, db).run(run_id)
    REPORTS_DIR.mkdir(exist_ok=True)
    (REPORTS_DIR / f"run-{run_id}.md").write_text(render(build_report(db, run_id), "md"), encoding="utf-8")
    return run_id


def cmd_run(db: Database, args) -> str:
    args.run_id = run_cycle(db, args.run_id)
    return cmd_report(db, args)


def seconds_until_due(db: Database, every_minutes: int) -> float:
    """Time left until the next cycle, counted from the last one, so a restart or deploy doesn't
    send an extra shortlist."""
    last = db.latest_run_id()
    if not last:
        return 0.0
    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(db.get_run(last)["started_at"])).total_seconds()
    return max(0.0, every_minutes * 60 - elapsed)


def scheduler(every_minutes: int, stop: threading.Event) -> None:
    """Run a news cycle every `every_minutes` (the first when one is due), on its own connection."""
    db = Database(settings.db_path)
    wait = seconds_until_due(db, every_minutes)
    if wait:
        log.info("Next news cycle in %.0f minutes (the last one ran recently)", wait / 60)
        stop.wait(wait)
    while not stop.is_set():
        started = time.monotonic()
        try:
            run_id = run_cycle(db)
            log.info("Scheduled cycle %s finished in %.0fs", run_id, time.monotonic() - started)
        except Exception:  # keep the schedule alive; the next cycle may succeed
            log.exception("Scheduled news cycle failed")
        stop.wait(max(60, every_minutes * 60 - (time.monotonic() - started)))


def cmd_serve(db: Database, args) -> str:
    """The production service: WhatsApp bridge plus an hourly news cycle, in one process."""
    if not settings.whatsapp_enabled:
        raise SystemExit("Set WHATSAPP_OWNER_NUMBER in .env first.")
    stop = threading.Event()
    threading.Thread(target=scheduler, args=(args.every, stop), name="scheduler", daemon=True).start()
    log.info("Serving: news cycle every %d minutes, WhatsApp approvals from %s", args.every, settings.whatsapp_owner)
    try:
        return cmd_whatsapp(db, args)
    finally:
        stop.set()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="justnews", description="Just News agents")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, fn in [("discover", cmd_discover), ("evaluate", cmd_evaluate), ("create", cmd_create),
                     ("design", cmd_design), ("request-approval", cmd_request_approval), ("publish", cmd_publish),
                     ("report", cmd_report), ("run", cmd_run)]:
        p = sub.add_parser(name)
        p.add_argument("run_id", nargs="?", default=None)
        p.add_argument("--format", choices=["md", "json"], default="md")
        p.set_defaults(func=fn)
    p = sub.add_parser("reply", help='apply an approval reply, e.g. reply "1 3"')
    p.add_argument("text")
    p.set_defaults(func=cmd_reply)
    p = sub.add_parser("serve", help="production: WhatsApp bridge + a news cycle every --every minutes")
    p.add_argument("--every", type=int, default=settings.cycle_minutes, help="minutes between cycles")
    p.add_argument("--pair-code", metavar="SPARE_NUMBER", default=None, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_serve)
    p = sub.add_parser("whatsapp", help="run the WhatsApp bridge (links a spare number on first run)")
    p.add_argument("--pair-code", metavar="SPARE_NUMBER", default=None,
                   help="link with a pairing code for this number instead of scanning a QR code")
    p.set_defaults(func=cmd_whatsapp)
    args = parser.parse_args(argv)

    setup_logging(settings.log_dir, settings.log_level)
    db = Database(settings.db_path)
    try:
        args.func(db, args)
    except LLMError as exc:
        log.error("LLM unavailable: %s", exc)
        return 2
    except WhatsAppError as exc:
        log.error("WhatsApp error: %s", exc)
        return 3
    except KeyboardInterrupt:
        return 130
    except Exception:
        log.exception("Command %s failed", args.command)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
