---
name: news-discovery
description: Agent 1 of Just News. Discover recent news (Technology, AI, Business, Politics, World) from free RSS feeds, Google News and selected news websites, and store it in SQLite for evaluation.
metadata: {"nanobot":{"emoji":"📰","requires":{"bins":["sh"]}}}
---

# News Discovery Agent

Collects recent articles (headline, description, URL, published date, source name,
category) from the sources listed in `justnews/sources.py`, drops obviously
irrelevant items (deals, shopping guides, quizzes, promos, stale or off-topic
stories, in-run duplicates) and saves the result to the Just News SQLite database.
Unreachable or rate-limited sources are logged and skipped; they never stop the run.

## Run

```bash
./bin/justnews discover
```

Run it from the workspace root. It takes about 5–20 seconds and makes no LLM calls.

## Output

Logs go to stderr. Stdout is a JSON summary. Read the `run_id`, because the evaluator needs it:

```json
{"run_id": "20261102-165519", "sources_total": 24, "sources_ok": 23,
 "sources_failed": [{"source": "VentureBeat [AI]", "error": "HTTP 429"}],
 "raw_entries": 846, "filtered_out": {"stale": 233, "...": 0},
 "discovered": 60, "by_category": {"AI": 12, "...": 12}}
```

## Rules

- If `discovered` is 0, report that and do not run the evaluator.
- A few failed sources is normal. Mention them, but carry on.
- Never edit the source list or code to work around a failure.
