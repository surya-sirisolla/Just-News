---
name: news-content-creator
description: Agent 3 of Just News. Write draft social posts for the evaluator's recommended articles (headline, caption, hashtags, source attribution, image text) using the Gemini free tier. Drafts only; nothing is published.
metadata: {"nanobot":{"emoji":"✍️","requires":{"bins":["sh"]}}}
---

# Content Creator Agent

For each recommended article in a run (top 10 by score, each article written only once), creates:

- **Headline** of up to about 70 characters, with no clickbait.
- **Caption** of 1–2 neutral sentences, using only facts from the source headline and summary.
- **3–5 hashtags.**
- **Source attribution** (`Source: <publisher> – <url>`), built by code rather than the model.
- **Image text** of 2–8 words for the image designer, plus a suggestion: a licensed photo (with a
  search query) or a text graphic.

All posts in a run go to Gemini in **one** request, to save free-tier quota. When a model's daily
free quota is used up, the next model in `GEMINI_CONTENT_MODELS` is tried. When a TypeSafe key is
set, Jev checks each caption against its source and marks doubtful posts `needs_review`.

## Run

```bash
./bin/justnews create <run_id>
```

It takes about 10–40 seconds. Stdout is a JSON summary with `created`, `needs_review`, `pending` and `error`.

## Rules

- Exit code 2 means no Gemini model could write the posts (missing key, or quota used up). Report it
  and continue with `news-image-designer` and the report. Pending posts are retried on the next run.
- Do not rewrite or edit the posts yourself.
- Never publish, post or send anything.
