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
import re
import time
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .agents.content import ContentCreatorAgent
from .agents.discovery import NewsDiscoveryAgent
from .agents.evaluator import NewsEvaluatorAgent
from .agents.images import ImageDesignerAgent
from .agents.publisher import InstagramPublisher, Publisher
from .agents.strategist import StrategistAgent
from .agents.x_publisher import XPublisher
from .approval import ApprovalService
from .assistant import Assistant, clean_query
from .whatsapp import Incoming, OutboxClient, WhatsAppBridge, WhatsAppError, bridge_running
from .config import settings
from .db import Database
from .graphics import Fonts
from .llm import GeminiClient, LLMError
from .logging_setup import setup_logging
from . import recap
from .agents.publisher import merge_hashtags
from .quiet import local_time, quiet_until
from .report import REPORTS_DIR, build_report, render
from .sources import GN_INDIA, Source, google_search

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


def make_strategist(db: Database) -> StrategistAgent | None:
    return StrategistAgent(settings, db, make_publishers(db)) if settings.strategist else None


def write_posts(db: Database, run_id: str):
    """Agent 6 (strategist) prepares the playbook, then Agent 3 writes and discusses the drafts."""
    strategist = make_strategist(db)
    if strategist:
        strategist.prepare(run_id)
    return ContentCreatorAgent(settings, db, strategist=strategist).run(run_id)


def cmd_strategy(db: Database, args) -> str:
    """Read engagement and benchmarks, refresh the playbook, and print it."""
    run_id = _resolve_run(db, args.run_id)
    stats = StrategistAgent(settings, db, make_publishers(db)).prepare(run_id)
    row = db.latest_playbook()
    print(json.dumps({"run_id": run_id, **stats, "playbook": json.loads(row["playbook"]) if row else None,
                      "playbook_from": row["model"] if row else None}, indent=2, ensure_ascii=False))
    return run_id


def cmd_create(db: Database, args) -> str:
    run_id = _resolve_run(db, args.run_id)
    result = write_posts(db, run_id)
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

    def react(self, to: str, message, emoji: str) -> str:
        print(f"\n----- WhatsApp reaction {emoji} on your message: {message.text[:60]!r} -----")
        return f"console-{uuid.uuid4().hex[:12]}"


def make_publishers(db: Database, immediate: bool = False) -> list[Publisher]:
    """Every platform whose keys are set, in the order posts go out. X queues its posts (spaced out)
    unless `immediate`."""
    publishers: list[Publisher] = []
    if settings.instagram_enabled:
        publishers.append(InstagramPublisher(settings, db))
    if settings.x_enabled:
        publishers.append(XPublisher(settings, db, spacing=0 if immediate else None))
    return publishers


def approval_service(db: Database) -> ApprovalService:
    def designer(run_id: str, article_ids: list[str]):
        return ImageDesignerAgent(settings, db).run(run_id, article_ids)

    assistant = None
    if settings.gemini_api_key:
        assistant = Assistant([GeminiClient(settings.gemini_api_key, m, 0.0, 0.0, thinking_level="minimal",
                                            min_interval=settings.gemini_min_interval, temperature=0.6,
                                            timeout=20, retries=0) for m in settings.gemini_chat_models])
    publishers = {p.label: (lambda run_id, ids, p=p: p.publish(run_id, ids)) for p in make_publishers(db)}

    def editor(run_id: str, article_ids: list[str], instruction: str) -> dict[str, str]:
        return ContentCreatorAgent(settings, db).revise_posts(run_id, article_ids, instruction)

    if settings.whatsapp_enabled:
        return ApprovalService(settings, db, OutboxClient(db), designer, assistant=assistant, publishers=publishers,
                               researcher=start_research, editor=editor)
    log.warning("WHATSAPP_OWNER_NUMBER is not set; approval messages are printed here instead")
    return ApprovalService(settings, db, ConsoleClient(), designer, owner="console", assistant=assistant,
                           publishers=publishers, editor=editor,
                           researcher=lambda query, text, instruction="": research_job(query, text, db, instruction))


def research_window(text: str) -> str:
    """How far back to search: "this week" -> 7 days, "yesterday" -> 2 days, otherwise 1 day."""
    if re.search(r"\b(week|7 days|few days)\b", text, re.I):
        return "7d"
    return "2d" if re.search(r"\byesterday\b", text, re.I) else "1d"


def research(db: Database, query: str, window: str = "1d", guidance: str = "") -> str:
    """On-demand research: Google News (India edition) for the query, scored with a fresh look,
    then posts for the best few. Returns the run ID; the stories wait for approval as usual."""
    run_id = f"{new_run_id()}-research"
    sources = [Source("Google News", google_search(query, window, GN_INDIA), "India", kind="google_news", region="India")]
    hours = int(window.rstrip("d")) * 24 + 12  # the age limit follows the search window
    NewsDiscoveryAgent(replace(settings, max_per_category=20, max_age_hours=max(settings.max_age_hours, hours)),
                       db).run(run_id, sources=sources, include_seen=True)
    NewsEvaluatorAgent(replace(settings, recommend_threshold=settings.research_threshold), db).run(run_id, fresh_look=True)
    picked = [r["article_id"] for r in db.evaluations_for_run(run_id) if r["decision"] == "Recommended"]
    picked = on_topic(db, run_id, picked, query)
    db.adopt_posts(run_id, picked)  # stories written before (e.g. in a skipped list) are offered again
    ContentCreatorAgent(settings, db).run(run_id, limit=settings.research_max_posts, mix=False, guidance=guidance)
    return run_id


TOPIC_PROBABILITY = 0.5
SYNONYMS = {"aqi": ["aqi", "air quality", "air pollution", "pollution", "smog"],
            "rain": ["rain", "rains", "rainfall", "flood", "floods", "monsoon"]}


def on_topic(db: Database, run_id: str, article_ids: list[str], query: str) -> list[str]:
    """Keep only the stories about what the owner asked: Jev decides when its key is set, otherwise
    every word of the query (or a synonym) must appear in the headline or summary."""
    articles = {a.id: a for a in db.articles_for_run(run_id) if a.id in article_ids}
    if settings.typesafe_api_key:
        from .jev import JevScorer
        probs = JevScorer(settings.typesafe_api_key, "jev-1.14.0", 0.042).about_topic(list(articles.values()), query)
        keep = {aid for aid, p in probs.items() if p >= TOPIC_PROBABILITY}
    else:
        words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2]
        keep = {aid for aid, a in articles.items()
                if all(any(s in f"{a.title} {a.description}".lower() for s in SYNONYMS.get(w, [w])) for w in words)}
    for aid in set(articles) - keep:
        db.reject(run_id, aid, f"Not about {query}")
    log.info("Research %s: %d of %d recommended stories are about %r", run_id, len(keep), len(articles), query)
    return [aid for aid in article_ids if aid in keep]


def research_job(query: str, text: str, db: Database | None = None, instruction: str = "") -> dict:
    """Search, then send the shortlist (or say nothing was found) to the owner."""
    db = db or Database(settings.db_path)  # a background thread uses its own connection
    service = approval_service(db)
    try:
        run_id = research(db, query, research_window(text), instruction)
        topic = query.upper() if query.isalpha() and len(query) <= 4 else query[:1].upper() + query[1:]  # "AQI"
        result = service.request(run_id, title=f"🔎 *{topic}*", limit=settings.research_max_posts)
        if not result.get("requested"):
            service.client.send_text(service.owner, f"I couldn't find recent news worth posting on {query}. "
                                                    "Try other words, or ask about the past week.")
        return result
    except Exception as exc:
        log.exception("Research on %r failed", query)
        service.client.send_text(service.owner, f"Sorry, the search on {query} failed ({type(exc).__name__}).")
        return {"error": str(exc)}


def start_research(query: str, text: str, instruction: str = "") -> None:
    """Runs in the background, so the WhatsApp bot keeps answering while it searches."""
    threading.Thread(target=research_job, args=(query, text, None, instruction), name="research", daemon=True).start()


def cmd_research(db: Database, args) -> str:
    print(json.dumps(research_job(clean_query(args.topic), args.topic, db), indent=2))
    return ""


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
    """Post a run's approved templates that aren't on Instagram/X yet (approvals normally do this)."""
    publishers = make_publishers(db, immediate=True)  # a manual run posts to X now, not queued
    if not publishers:
        raise SystemExit("Set the Instagram (INSTAGRAM_ACCESS_TOKEN, INSTAGRAM_USER_ID, JUSTNEWS_S3_BUCKET) "
                         "or X (X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET) keys in .env first.")
    run_id = _resolve_run(db, args.run_id)
    approved = [p["article_id"] for p in db.posts_for_run(run_id) if p["approval"] == "approved" and p["image_path"]]
    out = {"run_id": run_id}
    for publisher in publishers:
        result = publisher.publish(run_id, approved)
        out[publisher.platform] = {"posted": result.posted, "failed": result.failed, "skipped": result.skipped}
    print(json.dumps(out, indent=2))
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


def retry_failed_posts(db: Database) -> None:
    """Approved posts that failed last time are retried (or found to be live) before new news."""
    for publisher in make_publishers(db):
        try:
            result = publisher.retry_failed()
        except Exception:
            log.exception("Retrying failed %s posts failed", publisher.label)
            continue
        if result.posted and settings.whatsapp_enabled:
            n = len(result.posted)
            lines = "\n".join(f"• {db.post_headline(aid) or 'Story'}: {link or '(posted)'}" for aid, link in result.posted)
            OutboxClient(db).send_text(settings.whatsapp_owner,
                                       f"Update: {n} approved stor{'ies' if n != 1 else 'y'} that failed earlier "
                                       f"{'are' if n != 1 else 'is'} now on {publisher.label} ✅\n{lines}")


def batch_message(db: Database, label: str, result) -> str:
    n, total = len(result.posted), len(result.posted) + len(result.failed)
    text = f"{label}: {n} of {total} {'story is' if total == 1 else 'stories are'} up ✅" if n else \
        f"{label}: none of the {total} queued {'story' if total == 1 else 'stories'} could be posted"
    lines = [f"• {db.post_headline(aid) or 'Story'}: {link or '(posted)'}" for aid, link in result.posted]
    if result.failed:
        lines += ["", "Not posted:"] + [f"• {db.post_headline(aid) or 'Story'} ({why})" for aid, why in result.failed]
    return "\n".join([text, *lines])


def x_queue(stop: threading.Event, every: float = 30.0) -> None:
    """Post queued X stories when their time comes; tell the owner when an approval's batch is done."""
    db = Database(settings.db_path)
    while not stop.is_set():
        try:
            for _, result in XPublisher(settings, db).post_due():
                if settings.whatsapp_enabled:
                    OutboxClient(db).send_text(settings.whatsapp_owner, batch_message(db, "X", result))
        except Exception:  # keep the queue alive
            log.exception("X queue failed")
        stop.wait(every)


RECAP_ATTEMPTS = 3


def make_recap(db: Database, day: str, out_dir) -> tuple[list, str, object] | None:
    """Render the day's recap video; None when fewer than two stories were posted."""
    rows = recap.pick(db.published_between(*recap.day_bounds(day, settings.timezone)), settings.recap_max_stories)
    if len(rows) < 2:
        return None
    date_label = datetime.fromisoformat(day).strftime("%-d %b %Y")
    handle = InstagramPublisher(settings, db)._handle_name() if settings.instagram_enabled else ""
    video = out_dir / f"recap-{day}.mp4"
    cover = recap.render(rows, date_label, handle, settings.brand_name, Fonts(settings.font_bold, settings.font_regular),
                         video, today=day == local_today())
    return rows, str(video), cover


def post_recap(db: Database, day: str, force: bool = False) -> str:
    """Make and post the day's recap Reel once; tells the owner on WhatsApp. Returns what happened.
    `force` retries a day that failed or was skipped; a posted recap is never posted again."""
    key = f"recap:{day}"
    done = db.get_state(key)
    if done and not (force and done.startswith(("failed", "skipped"))):
        return f"already done ({done})"
    tries = int(db.get_state(f"{key}:attempts") or 0) + 1
    db.set_state(f"{key}:attempts", str(tries))
    notify = (lambda text: OutboxClient(db).send_text(settings.whatsapp_owner, text)) if settings.whatsapp_enabled \
        else (lambda text: log.info("Recap: %s", text))
    try:
        made = make_recap(db, day, settings.output_dir / "recaps")
        if made is None:
            db.set_state(key, "skipped: fewer than two stories posted")
            return "skipped: fewer than two stories posted"
        rows, video, cover = made
        date_label = datetime.fromisoformat(day).strftime("%-d %b %Y")
        tags = merge_hashtags(recap.hashtags(rows))
        media_id, link = InstagramPublisher(settings, db).publish_reel(
            f"posts/recaps/{day}", Path(video).read_bytes(), cover,  # posts/: the prefix the server may write
            recap.caption(rows, date_label, tags, day == local_today()))
    except Exception as exc:
        log.exception("Daily recap failed (attempt %d)", tries)
        if tries >= RECAP_ATTEMPTS:
            db.set_state(key, f"failed: {exc}")
            notify(f"Today's recap Reel couldn't be posted ({type(exc).__name__}: {str(exc)[:120]}).")
        return f"failed: {exc}"
    db.set_state(key, link or media_id)
    which = "Today's" if day == local_today() else f"The {date_label}"
    notify(f"🎬 {which} recap Reel is up: {len(rows)} stories in one video.\n{link}")
    return f"posted {link}"


def local_today() -> str:
    return utcnow().astimezone(ZoneInfo(settings.timezone)).date().isoformat()


def recap_due(now_local: datetime, recap_time: str) -> bool:
    """From the recap time until quiet hours start (or midnight), once a day."""
    if recap_time.lower() == "off":
        return False
    hour, minute = (int(x) for x in recap_time.split(":"))
    start = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    end = now_local.replace(hour=23, minute=0, second=0, microsecond=0)
    return start <= now_local < max(end, start + timedelta(minutes=30))


def daily_recap(stop: threading.Event, every: float = 60.0) -> None:
    """At JUSTNEWS_RECAP_TIME each day, post the recap Reel (once; up to three attempts)."""
    db = Database(settings.db_path)
    while not stop.is_set():
        try:
            local = utcnow().astimezone(ZoneInfo(settings.timezone))
            day = local.date().isoformat()
            attempts = int(db.get_state(f"recap:{day}:attempts") or 0)
            if recap_due(local, settings.recap_time) and not db.get_state(f"recap:{day}"):
                if attempts < RECAP_ATTEMPTS:
                    log.info("Daily recap for %s: %s", day, post_recap(db, day))
                else:  # every attempt ended without a result: the process died mid-way (e.g. out of memory)
                    db.set_state(f"recap:{day}", "failed: no attempt finished (the process stopped)")
                    if settings.whatsapp_enabled:
                        OutboxClient(db).send_text(settings.whatsapp_owner, "Today's recap Reel couldn't be made: "
                                                   "the server stopped while making the video. I've logged it.")
        except Exception:
            log.exception("Daily recap check failed")
        stop.wait(every)


def cmd_recap(db: Database, args) -> str:
    """Make today's (or --day's) recap; --dry-run renders the video without posting it."""
    day = args.day or utcnow().astimezone(ZoneInfo(settings.timezone)).date().isoformat()
    if args.dry_run:
        made = make_recap(db, day, settings.output_dir / "recaps")
        print(json.dumps({"day": day, "video": made[1] if made else None,
                          "stories": [r["headline"] for r in made[0]] if made else []}, indent=2))
        return ""
    print(json.dumps({"day": day, "result": post_recap(db, day, force=args.force)}, indent=2))
    return ""


def run_cycle(db: Database, run_id: str | None = None) -> str:
    """Agents 1-2, the strategist (6) and content creator (3), then the WhatsApp shortlist (or Agent 4
    for every draft without WhatsApp)."""
    run_id = run_id or new_run_id()
    retry_failed_posts(db)
    NewsDiscoveryAgent(settings, db).run(run_id)
    NewsEvaluatorAgent(settings, db).run(run_id)
    try:
        write_posts(db, run_id)
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


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
        until = quiet_until(settings, utcnow())
        if until:
            log.info("Quiet hours: no news cycles until %s", local_time(settings, until))
            stop.wait(max(60.0, (until - utcnow()).total_seconds()))
            continue
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
    if settings.x_enabled:
        threading.Thread(target=x_queue, args=(stop,), name="x-queue", daemon=True).start()
    if settings.instagram_enabled:
        threading.Thread(target=daily_recap, args=(stop,), name="daily-recap", daemon=True).start()
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
                     ("report", cmd_report), ("run", cmd_run), ("strategy", cmd_strategy)]:
        p = sub.add_parser(name)
        p.add_argument("run_id", nargs="?", default=None)
        p.add_argument("--format", choices=["md", "json"], default="md")
        p.set_defaults(func=fn)
    p = sub.add_parser("reply", help='apply an approval reply, e.g. reply "1 3"')
    p.add_argument("text")
    p.set_defaults(func=cmd_reply)
    p = sub.add_parser("recap", help="post the day's recap Reel now (--dry-run: only make the video)")
    p.add_argument("--day", default=None, help="YYYY-MM-DD, default today")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="retry a day whose recap failed (never re-posts one)")
    p.set_defaults(func=cmd_recap)
    p = sub.add_parser("research", help='search the news on a topic and send a shortlist, e.g. research "AQI today"')
    p.add_argument("topic")
    p.set_defaults(func=cmd_research)
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
