# Just News – Agentic News Discovery

An agentic social media automation platform. Four independent agents, orchestrated by
[PicoClaw](https://github.com/sipeed/picoclaw), find recent news, decide what is worth posting, and
draft posts with images. Nothing is published, posted or sent: drafts are for human review.
WhatsApp and social publishing are later phases.

| Agent | Module | PicoClaw skill | LLM |
|---|---|---|---|
| 1. News Discovery | `justnews/agents/discovery.py` | `news-discovery` | none |
| 2. News Evaluator | `justnews/agents/evaluator.py` | `news-evaluator` | TypeSafe Jev (Claude Haiku 5.5 or Gemini as alternatives) |
| 3. Content Creator | `justnews/agents/content.py` | `news-content-creator` | Gemini free tier writes; Jev reviews |
| 4. Image Designer | `justnews/agents/images.py` | `news-image-designer` | none to draw; Jev picks photos |

## How it works

```
PicoClaw agent (orchestrator LLM, reads AGENT.md + skills)
  │  exec ./bin/justnews discover            ─▶ Agent 1: 24 free sources in parallel
  │                                               RSS feeds · Google News RSS · website feed autodiscovery
  │                                               filter: stale / promo / off-topic / in-run dup / seen before
  │                                               balance: ≤12 per category, round-robin across feeds
  │                                               └─▶ SQLite: articles (run_id)
  │  exec ./bin/justnews evaluate <run_id>   ─▶ Agent 2: reads the run's articles from SQLite
  │                                               exact repeat of an earlier run → rejected, no model call
  │                                               Jev: per article, Score relevance/interest (+credibility
  │                                                 for unknown outlets) and Noul "reports a new event?";
  │                                                 per candidate pair, Score "same event?"
  │                                               freshness = age decay; credibility = publisher table
  │                                               cluster duplicates → keep the best → decide
  │                                               └─▶ SQLite: evaluations, llm_usage
  │  exec ./bin/justnews create <run_id>     ─▶ Agent 3: top 10 recommended, each written once
  │                                               Gemini (1 request for all posts): headline, caption,
  │                                                 hashtags, image text, photo-or-graphic + photo query
  │                                               code: attribution, hashtag rules, length limits
  │                                               Jev review: unsupported facts / sensational tone /
  │                                                 people or groups described differently → needs_review
  │                                               └─▶ SQLite: posts
  │  exec ./bin/justnews design <run_id>     ─▶ Agent 4: one 1080×1080 PNG per post
  │                                               photo suggested → Wikimedia Commons search, licence
  │                                                 filter, Jev picks best fit (or none) → overlay + credit
  │                                               otherwise / no good fit → white text graphic (Pillow)
  │                                               └─▶ output/<run_id>/*.png, SQLite: images
  └  exec ./bin/justnews report <run_id>     ─▶ Markdown/JSON report with draft posts and images
```

The agents never import each other. They share only the SQLite database, keyed by `run_id`, so
each one can be run, tested and replaced on its own. The same pipeline also runs without PicoClaw:
`python -m justnews run` costs no orchestrator tokens, which suits cron.

**Scoring.** Overall = 0.3·relevance + 0.3·audience interest + 0.2·credibility + 0.2·freshness.
An article is *Recommended* when it reports a new event (not opinion, a review or a how-to),
overall ≥ 70 (`JUSTNEWS_RECOMMEND_THRESHOLD`), and it is not a duplicate. Freshness halves every 24 hours.

**Decision backends** (`LLM_PROVIDER`). Both feed the same decision code:

| | `jev` (default when `TYPESAFE_API_KEY` is set) | `claude` / `gemini` |
|---|---|---|
| How it judges | Typed Score/Noul questions per article and per candidate pair, with calibrated probabilities | Batches of 20 articles, JSON-schema output |
| Measured on 60 articles | ~170 requests, ~87k input tokens, **$0.0037**, ~8 s | 3 calls, ~17k in / 5k out, **$0.0041**, ~23 s |
| Duplicates | Pairwise "same event?" for every candidate pair (code pre-filter), combined in code | Model links each article to one earlier article |
| Reason text | Built from the levels Jev picked ("Significant news, broad audience interest.") | Written by the model |

On the same 60 articles the two backends made the same decision for 56. Jev uses no Claude
credits, so Claude is then needed only for PicoClaw's orchestrator (`PICOCLAW_LLM_PROVIDER`,
default `claude`).

**Content creator and the Gemini free tier.** Free-tier quotas are small and models can be overloaded,
so Agent 3:
- sends all of a run's posts in **one** request;
- spaces requests out (`GEMINI_MIN_INTERVAL_SECONDS`) and waits as long as Google's `retryDelay`
  asks on per-minute 429s;
- moves to the next model in `GEMINI_CONTENT_MODELS` on timeouts, 503 "high demand" or an
  exhausted daily quota;
- stops after `GEMINI_CONTENT_TIME_BUDGET` seconds and leaves posts pending for the next run.

Free-tier prompts may be used by Google to improve its products. They contain only public news.

**Image designer.** Gemini image generation is not available on the free tier, and AI images are a
poor fit for news anyway, so Agent 4 uses none:
- **Photos** come from Wikimedia Commons. Only CC0, public-domain, CC BY or CC BY-SA images
  (`JUSTNEWS_IMAGE_LICENSES`) with no extra restrictions (personality rights, trademarks) are
  used. Jev must rate the photo as clearly showing the story's subject.
- **Credits:** each photo is credited on the image ("File photo: creator / Wikimedia Commons,
  licence"), and the licence and file page are stored.
- **No photos for some stories:** deaths, crime, disasters and private individuals always get a
  text graphic.
- **Text** is drawn by code, so it is always spelled exactly as written.

**Duplicates** are caught at four levels:

1. Same canonical URL within a run (discovery).
2. URL already evaluated in an earlier run (skipped in discovery, and rejected by the evaluator as a safety net).
3. Same story from different outlets in this run (Jev pairwise checks, or the LLM's `duplicate_of`, plus title similarity; the highest-scoring version is kept).
4. Same story as one *recommended* in the last 7 days.

## Setup

```bash
uv venv .venv --python 3.12 && uv pip install --python .venv/bin/python -r requirements.txt
```

```bash
cp .env.example .env   # set TYPESAFE_API_KEY (evaluator, reviews), GEMINI_API_KEY (free tier, posts), CLAUDE_API_KEY (PicoClaw)
```

PicoClaw is a single Go binary. Download the release for your platform from
https://github.com/sipeed/picoclaw/releases (v0.3.1 tested) into `picoclaw/bin/picoclaw`, or put
`picoclaw` on your `PATH`.

## Run

Full cycle orchestrated by PicoClaw:

```bash
scripts/picoclaw.sh agent -m "Run the Just News news cycle now."
```

Same pipeline without the orchestrator LLM:

```bash
.venv/bin/python -m justnews run
```

Individual agents and reports:

```bash
.venv/bin/python -m justnews discover          # Agent 1, prints run_id + stats
.venv/bin/python -m justnews evaluate RUN_ID   # Agent 2 (default: latest run)
.venv/bin/python -m justnews create RUN_ID     # Agent 3: draft posts
.venv/bin/python -m justnews design RUN_ID     # Agent 4: images in output/RUN_ID/
.venv/bin/python -m justnews report RUN_ID --format json
```

Tests (offline, no API key):

```bash
.venv/bin/python -m unittest discover tests
```

Exit codes: 0 means success, 1 means an unexpected error, and 2 means the LLM is unavailable (bad key, no credits or unknown model).
Logs go to stderr and `logs/justnews.log`.

## Layout

```
justnews/
  agents/discovery.py   Agent 1
  agents/evaluator.py   Agent 2
  sources.py            feeds, publisher credibility table, irrelevance patterns  ← edit to add sources
  agents/content.py     Agent 3
  agents/images.py      Agent 4 (Wikimedia Commons search, licence filter)
  graphics.py           Pillow layouts: white text graphic, photo with overlay
  jev.py                TypeSafe Jev: scoring, duplicate checks, post review, photo choice
  llm.py                Claude (Anthropic SDK) and Gemini clients, token/cost accounting
  db.py                 SQLite schema and queries
  report.py, cli.py, config.py, models.py, http.py, logging_setup.py
picoclaw/
  config.template.json  PicoClaw config (rendered to picoclaw/home/ at launch)
  workspace/AGENT.md    orchestrator instructions
  workspace/skills/     news-discovery, news-evaluator (SKILL.md)
  workspace/bin/justnews  entry point inside the PicoClaw sandbox
scripts/picoclaw.sh     renders config + .security.yml from .env, then runs PicoClaw
tests/                offline tests (fake model clients, no network)
```

Secrets only ever live in `.env` and the generated `picoclaw/home/.security.yml`. Both are gitignored.

## Scheduling (later)

PicoClaw has a built-in scheduler. For example, to run every 4 hours:

```bash
scripts/picoclaw.sh cron add -n news-cycle -c '0 */4 * * *' -m "Run the Just News news cycle now."
```

You could also add a system cron entry for `.venv/bin/python -m justnews run`. Neither is set up yet.

## Known limitations

- Google News links are `news.google.com` redirect URLs, and those items have no summary.
- Paywalled publishers (FT, WSJ) appear through Google News. Only the headline is evaluated.
- Duplicate detection depends on the pre-filter (shared names or words) to choose which pairs Jev checks.
  A same-story pair with no words in common is never checked. The Claude backend can also split one
  big story into two clusters.
- Jev reasons are short and built from level descriptions. Claude writes more specific prose.
- Gemini free-tier models were often overloaded in testing (timeouts, 503s). The fallback chain
  handles this, but posts written by the lighter fallback models are vaguer (e.g. "Anthropic
  reports financial figures for 2026").
- Wikimedia Commons has few photos of specific current events, so most posts get text graphics.
  CC BY-SA photos with an overlay must themselves be shared under CC BY-SA.
- Jev's review catches many problems but not all. Every draft still needs a human check before
  publishing.
- PicoClaw 0.3.1 always sends `temperature`, which current Claude models reject. `scripts/picoclaw.sh`
  works around this with `extra_body: {"temperature": null}`.
