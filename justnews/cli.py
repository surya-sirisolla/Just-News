"""Command line entry points. Each agent can be run on its own (this is what the
PicoClaw skills call), or both together with `run`.

  python -m justnews discover            # Agent 1 -> prints run id + discovery stats
  python -m justnews evaluate [RUN_ID]   # Agent 2 on a run (default: latest)
  python -m justnews report   [RUN_ID]   # structured report (--format md|json)
  python -m justnews create   [RUN_ID]   # Agent 3: draft posts for recommended articles
  python -m justnews design   [RUN_ID]   # Agent 4: an image for each draft post
  python -m justnews run                 # all four agents, then the report
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone

from .agents.content import ContentCreatorAgent
from .agents.discovery import NewsDiscoveryAgent
from .agents.evaluator import NewsEvaluatorAgent
from .agents.images import ImageDesignerAgent
from .config import settings
from .db import Database
from .llm import LLMError
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


def cmd_run(db: Database, args) -> str:
    run_id = args.run_id or new_run_id()
    NewsDiscoveryAgent(settings, db).run(run_id)
    NewsEvaluatorAgent(settings, db).run(run_id)
    try:
        ContentCreatorAgent(settings, db).run(run_id)
    except LLMError as exc:  # e.g. no Gemini key: keep the evaluation results and report them
        log.error("Skipping post creation: %s", exc)
    ImageDesignerAgent(settings, db).run(run_id)
    args.run_id = run_id
    return cmd_report(db, args)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="justnews", description="Just News agents")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, fn in [("discover", cmd_discover), ("evaluate", cmd_evaluate), ("create", cmd_create),
                     ("design", cmd_design), ("report", cmd_report), ("run", cmd_run)]:
        p = sub.add_parser(name)
        p.add_argument("run_id", nargs="?", default=None)
        p.add_argument("--format", choices=["md", "json"], default="md")
        p.set_defaults(func=fn)
    args = parser.parse_args(argv)

    setup_logging(settings.log_dir, settings.log_level)
    db = Database(settings.db_path)
    try:
        args.func(db, args)
    except LLMError as exc:
        log.error("LLM unavailable: %s", exc)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception:
        log.exception("Command %s failed", args.command)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
