"""Structured run report (Markdown text or JSON) built from SQLite."""

from __future__ import annotations

import json
import os
from datetime import datetime

from .agents.publisher import instagram_caption
from .agents.x_publisher import x_text
from .config import PROJECT_ROOT
from .db import Database

REPORTS_DIR = PROJECT_ROOT / "reports"


def _date(iso: str | None) -> str:
    return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M UTC") if iso else "unknown"


def build_report(db: Database, run_id: str) -> dict:
    run = db.get_run(run_id)
    if not run:
        raise ValueError(f"Unknown run: {run_id}")
    rows = db.evaluations_for_run(run_id)
    articles = [
        {
            "title": r["title"], "source": r["source_name"], "category": r["category"],
            "published": r["published_at"], "url": r["url"],
            "relevance": r["relevance"], "credibility": r["credibility"], "freshness": r["freshness"],
            "audience_interest": r["audience_interest"], "overall": r["overall"],
            "decision": r["decision"], "reason": r["reason"], "duplicate": bool(r["duplicate_of"]),
            "llm_scored": bool(r["llm_scored"]),
        }
        for r in rows
    ]
    usage = [dict(u) for u in db.llm_usage_for_run(run_id)]
    headlines = {p["article_id"]: p["headline"] for p in db.posts_for_run(run_id)}
    playbook = db.latest_playbook()
    posts = [
        {
            "headline": p["headline"], "caption": p["caption"], "hashtags": json.loads(p["hashtags"]),
            "attribution": p["attribution"], "image_text": p["image_text"], "status": p["status"],
            "review_note": p["review_note"], "category": p["category"], "article_title": p["title"],
            "model": p["model"], "approval": p["approval"] or "not requested",
            "instagram_text": instagram_caption(p), "x_text": x_text(p), "image": p["image_path"] and {
                "kind": p["image_kind"], "path": p["image_path"], "credit": p["image_credit"],
                "license": p["image_license"], "source_page": p["image_source"], "note": p["image_note"]},
        }
        for p in db.posts_for_run(run_id)
    ]
    return {
        "run_id": run_id,
        "started_at": run["started_at"],
        "discovery": run["discovery"] or {},
        "evaluation": run["evaluation"] or {},
        "content": run.get("content") or {},
        "images": run.get("images") or {},
        "llm_usage": usage,
        "posts": posts,
        "strategy": {
            "stats": run.get("strategy") or {},
            "playbook": json.loads(playbook["playbook"]) if playbook else None,
            "playbook_from": playbook and f"{playbook['model']}, {_date(playbook['created_at'])}",
            "discussion": [{"from": m["sender"], "to": m["recipient"], "post": headlines.get(m["article_id"]),
                            "message": m["body"]} for m in db.agent_messages(run_id)],
        },
        "recommended": [a for a in articles if a["decision"] == "Recommended"],
        "rejected": [a for a in articles if a["decision"] == "Rejected"],
    }


def _article_block(a: dict) -> list[str]:
    lines = [f"### {a['title']}",
             f"- Source: {a['source']}",
             f"- Category: {a['category']}",
             f"- Published: {_date(a['published'])}",
             f"- URL: {a['url']}"]
    if a["llm_scored"]:
        lines += [f"- Relevance: {a['relevance']}/100",
                  f"- Credibility: {a['credibility']}/100",
                  f"- Freshness: {a['freshness']}/100",
                  f"- Audience interest: {a['audience_interest']}/100",
                  f"- Overall score: {a['overall']}/100"]
    else:
        lines.append("- Overall score: n/a (rejected before scoring)")
    lines += [f"- Decision: {a['decision']}", f"- Reason: {a['reason']}", ""]
    return lines


def render_markdown(report: dict) -> str:
    d, e = report["discovery"], report["evaluation"]
    out = [f"# Just News – Run {report['run_id']}", "", f"Started: {_date(report['started_at'])}", "",
           "## Summary", "",
           f"1. Total articles discovered: **{d.get('discovered', 0)}** "
           f"(from {d.get('raw_entries', 0)} raw feed entries, {d.get('sources_ok', 0)}/{d.get('sources_total', 0)} sources reachable)",
           f"2. Total articles evaluated: **{e.get('evaluated', 0)}**"
           + (f" ({e['not_evaluated_llm_error']} left for next run after LLM errors)" if e.get("not_evaluated_llm_error") else ""),
           f"3. Recommended for posting: **{len(report['recommended'])}**",
           *([f"   ⚠ Evaluation error: {e['llm_error']}"] if e.get("llm_error") else []),
           f"4. Rejected: **{len(report['rejected'])}** (of which duplicates: {e.get('duplicates', 0)})", ""]

    if d.get("by_category"):
        out.append("Discovered by category: " + ", ".join(f"{k} {v}" for k, v in d["by_category"].items()))
    if d.get("filtered_out"):
        out.append("Filtered out during discovery: " + ", ".join(f"{k} {v}" for k, v in d["filtered_out"].items()))
    for f in d.get("sources_failed", []):
        out.append(f"- Source unavailable: {f['source']} — {f['error']}")
    out.append("")

    for u in report["llm_usage"]:
        out.append(f"LLM usage ({u['agent']}): model {u['model']}, {u['calls']} calls ({u['failed_calls']} failed), "
                   f"{u['input_tokens']:,} input + {u['output_tokens']:,} output tokens, est. ${u['cost_usd']:.5f}")
    out.append("")

    if report["posts"]:
        out += _posts_section(report)
    out += _strategy_section(report["strategy"])
    out += ["## Recommended for posting", ""]
    for a in report["recommended"]:
        out += _article_block(a)
    if not report["recommended"]:
        out += ["_None this run._", ""]
    out += ["## Rejected", ""]
    for a in report["rejected"]:
        out += _article_block(a)
    return "\n".join(out)


def _strategy_section(st: dict) -> list[str]:
    if not st.get("playbook") and not st.get("discussion"):
        return []
    out = ["## Engagement strategy", ""]
    if st.get("stats"):
        out += [", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in st["stats"].items()), ""]
    if st.get("playbook"):
        pb = st["playbook"]
        out += [f"Playbook ({st['playbook_from']}):", ""]
        for title, key in (("Instagram", "instagram"), ("X", "x"), ("What other news accounts do", "observations"),
                           ("To try next", "experiments")):
            if pb.get(key):
                out += [f"**{title}**", *[f"- {r}" for r in pb[key]], ""]
    if st.get("discussion"):
        out += ["### Discussion between the agents", ""]
        for m in st["discussion"]:
            about = f" about *{m['post']}*" if m["post"] else ""
            out.append(f"- **{m['from']} → {m['to']}**{about}: {m['message']}")
        out.append("")
    return out


def _posts_section(report: dict) -> list[str]:
    c, i = report["content"], report["images"]
    out = ["## Draft posts (not published)", "",
           f"{c.get('created', 0)} drafts written ({c.get('needs_review', 0)} flagged for review, "
           f"{c.get('pending', 0)} left pending); images: {i.get('photos', 0)} licensed photos, "
           f"{i.get('graphics', 0)} text graphics.", ""]
    if c.get("error") and c.get("pending"):
        out += [f"⚠ Content creator: {c['error']}", ""]
    for p in report["posts"]:
        status = "Needs review" if p["status"] == "needs_review" else "Draft"
        out += [f"### {p['headline']}", ""]
        if p["image"]:
            rel = os.path.relpath(p["image"]["path"], REPORTS_DIR)
            out += [f"![{p['image_text']}]({rel})", ""]
        out += ["**Instagram**", "", p["instagram_text"], "", "**X**", "", p["x_text"], "",
                f"- Status: {status}" + (f" ({p['review_note']})" if p["review_note"] else ""),
                f"- Approval: {p['approval']}",
                f"- Image text: {p['image_text']}"]
        if p["image"]:
            img = p["image"]
            out.append(f"- Image: {img['kind']} — {img['note']}")
            if img["kind"] == "photo":
                out.append(f"- Photo credit: {img['credit']} ({img['source_page']})")
        out += [f"- Written by: {p['model']}", ""]
    return out


def render(report: dict, fmt: str) -> str:
    return json.dumps(report, indent=2, ensure_ascii=False) if fmt == "json" else render_markdown(report)
