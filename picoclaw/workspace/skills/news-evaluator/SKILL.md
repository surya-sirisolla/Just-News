---
name: news-evaluator
description: Agent 2 of Just News. Score the articles found by news-discovery (relevance, credibility, freshness, audience interest), detect duplicate and previously covered stories, and recommend or reject each one. Also produces the run report.
metadata: {"nanobot":{"emoji":"⚖️","requires":{"bins":["sh"]}}}
---

# News Evaluator Agent

Reads the articles a discovery run stored in SQLite and evaluates each one:

- **Relevance** and **audience interest**: TypeSafe Jev answers typed Score questions for each
  article, with calibrated probabilities. When `TYPESAFE_API_KEY` isn't set, Claude Haiku 5.5
  (or Gemini) scores them in batches instead.
- **Credibility** from a curated publisher table, with the model rating unknown publishers.
- **Freshness**, computed from article age (24-hour half-life).
- **Duplicates**: the same URL in an earlier run, or the same story from different outlets
  in this run or a previous one.

Overall = 0.3·relevance + 0.3·interest + 0.2·credibility + 0.2·freshness. An article is
recommended when it reports a new event (rather than opinion, a review or a how-to), the overall
score is at least 70, and it is not a duplicate.

## Run

Pass the `run_id` printed by `news-discovery`. If you omit it, the latest run is used.

```bash
./bin/justnews evaluate <run_id>
```

It takes about 10–30 seconds and costs about $0.004 per run. Stdout is a JSON summary with
`evaluated`, `recommended`, `rejected`, `duplicates` and `llm_usage` (tokens, cost).

## Report

```bash
./bin/justnews report <run_id>
```

This prints the full structured report in Markdown and saves it to `reports/run-<run_id>.md`
in the project. Use `--format json` for machine-readable output.

## Rules

- Exit code 2 means the LLM is unavailable (for example, a missing or invalid API key, or no credits). Report it. Do not retry in a loop.
- Articles left unevaluated after an LLM error are picked up automatically on the next run.
- Never post, publish or send any article anywhere. This agent only recommends.
