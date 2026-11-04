# Just News – Agentic News Discovery

An agentic social media automation platform. Six independent agents find recent news, decide what
is worth posting, learn what earns attention, and write a separate version for Instagram and for X.
The owner picks on WhatsApp which stories to post; only those get an image and are published.

| Agent | Module | PicoClaw skill | LLM |
|---|---|---|---|
| 1. News Discovery | `justnews/agents/discovery.py` | `news-discovery` | none |
| 2. News Evaluator | `justnews/agents/evaluator.py` | `news-evaluator` | TypeSafe Jev (Claude Haiku 5.5 or Gemini as alternatives) |
| 3. Content Creator | `justnews/agents/content.py` | `news-content-creator` | Gemini free tier writes; Jev reviews |
| 4. Image Designer | `justnews/agents/images.py` | `news-image-designer` | none to draw; Jev picks photos |
| 5. Publisher | `justnews/agents/publisher.py`, `x_publisher.py` | (only after WhatsApp approval) | none |
| 6. Engagement Strategist | `justnews/agents/strategist.py` | (runs inside `create`) | Gemini free tier (lite models first) |

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
  │  exec ./bin/justnews request-approval    ─▶ WhatsApp: numbered shortlist (text only) to the owner
  │                                               "1 3" / "all" / "none" ─▶ WhatsApp bridge ─▶ Agent 4
  │                                               designs the chosen posts only, one text ack back
  │  (Agent 4, on approval)                   ─▶ one 1080×1080 PNG per approved post
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

**WhatsApp approval.** This works the way OpenClaw does: a **spare WhatsApp number** is linked
to the bridge once, as a linked device (scan a QR code). The bot then uses that number to message
your own number, with no Meta account or API keys.
- **The shortlist:** after Agent 3, you get a numbered list (headline, caption, hashtags, source
  and link, plus any review flag) as plain text, never images.
- **Replies:** you answer `1 3`, `1-3`, `all` or `none` (`list` shows it again). Only those posts
  are designed and marked approved; the rest are marked rejected. One short text confirms it.
- **Talking to it:** anything else goes to a Gemini free-tier model, which replies naturally and
  in your language. For example:
  - "send me the templates", "show me 3" or "send the iPhone one" sends those templates as images,
    with caption, hashtags and source;
  - "what's ready?" gets a status update;
  - "go with the Apple story" makes it ask "yes?" before acting.
  Code checks every number against the real list, so the bot can't claim something it didn't do.
  Without Gemini, keyword rules handle the basics.
- **Only you:** the bot reads only your direct chat with it (`WHATSAPP_OWNER_NUMBER`). It ignores
  every other number, groups, broadcast lists, channels and Status updates (including yours),
  without replying, reading or reacting. The bridge also refuses to send to any number but yours.
  Repeats are ignored, a new shortlist replaces the previous one, and shortlists expire after 12
  hours.
- **How messages flow:** the bridge (`python -m justnews whatsapp`) is the only process connected
  to WhatsApp. Other commands queue messages in SQLite (`wa_outbox`), and the bridge sends them,
  so a cycle can run while the bridge is stopped and the shortlist goes out once it starts.
- **Without `WHATSAPP_OWNER_NUMBER`,** the messages are printed instead, and
  `python -m justnews reply "1 3"` applies your answer.

> ⚠️ This uses the WhatsApp Web protocol (whatsmeow, via `neonize`), not an official API.
> WhatsApp may restrict or ban numbers it flags, so use a spare number, message only yourself,
> and keep volume low. The session file (`data/whatsapp-session.sqlite3`) gives full access to
> that WhatsApp account: never commit or share it.

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
.venv/bin/python -m justnews design RUN_ID     # Agent 4 for every draft (no approval step)
.venv/bin/python -m justnews request-approval RUN_ID   # WhatsApp shortlist
.venv/bin/python -m justnews reply "1 3"       # apply an approval from the terminal
.venv/bin/python -m justnews whatsapp          # WhatsApp bridge (keep it running)
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
  agents/publisher.py   Agent 5: shared publishing loop + Instagram (S3 presigned link, container, publish)
  agents/x_publisher.py Agent 5 on X: media upload + post, OAuth 1.0a signing, 280-character text
  approval.py           WhatsApp approval: shortlist, reply parsing, design + post on approval
  assistant.py          natural-language replies on WhatsApp (Gemini free tier, keyword fallback)
  whatsapp.py           linked-device WhatsApp bridge (QR link, outbox sender, owner filter)
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

## Where the templates are

- **Files:** `output/<run_id>/NN-<headline>.png`, one 1080×1080 PNG per approved post. The newest
  run is the newest folder.
- **Report:** `reports/run-<run_id>.md` shows every draft with its image, caption, hashtags,
  source, approval state and photo credit. Run `python -m justnews report` to regenerate it.
- **WhatsApp:** ask the bot ("send me the templates", "send 2").

## WhatsApp setup

1. **Owner number:** in `.env`, set `WHATSAPP_OWNER_NUMBER` to **your own** number (country code,
   no `+`).
2. **libmagic:** install it once (`brew install libmagic` on macOS, `apt install libmagic1` on
   Debian or Ubuntu).
3. **Link the spare number:** run `.venv/bin/python -m justnews whatsapp`. On the **spare** phone,
   open WhatsApp > Settings > Linked devices > Link a device, and scan the QR code from the
   terminal (also saved as `data/whatsapp-qr.png`). Or use
   `--pair-code <spare number>` and enter the 8-character code via "Link with phone number
   instead".
4. **Keep it running:** leave the bridge on. Later starts reconnect from the saved session, with
   no new scan. Each news cycle's shortlist arrives on your phone from the spare number; reply
   there.

## Posting to Instagram and X: step by step

Only posts you approved on WhatsApp will be published. Both platforms need their official APIs
(neither offers a QR-style link for posting). Once the keys below are in `.env`, the publisher
agent is the next piece to build.

### Instagram (built: Agent 5, `justnews/agents/publisher.py`)

Approving a story on WhatsApp now designs its template **and posts it**. The bot replies with the
post links, and `python -m justnews publish [RUN_ID]` posts approved templates by hand.

1. **Token:** in Meta **Business Settings → Users → System users**, create a system user (Admin).
   Use **Assign assets** to give it the **Just News app** (Full control) and the **Instagram
   account** (Full control). Then click **Generate token**: app Just News, expiry **Never**,
   permissions `instagram_basic` and `instagram_content_publish`. The "Generate token" button in
   the app's Unified-login page creates a hidden system user that can't be given the account, so
   use this one.
2. **`.env`:** set `INSTAGRAM_ACCESS_TOKEN` and `INSTAGRAM_USER_ID` (the numeric ID shown in
   Business Settings → Instagram accounts).
3. **S3 bucket:** create a private bucket with "Block all public access" left on. Add a lifecycle
   rule that expires `posts/` after 7 days, and an IAM user (or, on AWS, a role) allowed
   `s3:PutObject`, `s3:GetObject` and `s3:DeleteObject` on `arn:aws:s3:::BUCKET/posts/*`.
   Set `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION` and `JUSTNEWS_S3_BUCKET`.
4. **How each post goes out:** the PNG becomes a JPEG, is uploaded to S3 and gets a 1-hour
   presigned link. Then `POST /<IG_ID>/media` creates the post, the publisher waits for
   `FINISHED`, and `POST /<IG_ID>/media_publish` publishes it. The permalink is saved and the S3
   copy deleted.
5. **Safeguards:** each story is posted once, at most `JUSTNEWS_MAX_POSTS_PER_DAY` (20) per day
   (Instagram allows 100). The caption includes the photo credit for licensed photos.

### X / Twitter (built: `justnews/agents/x_publisher.py`)

1. **Developer account:** sign in to console.x.com as the Just News X account, accept the
   developer agreement, and create an app.
   The app must sit inside a **Project** (console.x.com → Projects). A standalone app answers 403
   `client-not-enrolled` even with credits.
2. **App permissions:** in User authentication settings, set the app permissions to **Read and
   write**, with the type "Web App, Automated App or Bot". The callback and website URLs aren't
   used (the publisher never signs anyone in), but the form needs them: the GitHub repo URL works.
3. **Keys and tokens:** generate the API key and secret (Consumer Keys), then the access token and
   secret for your account. Generate the tokens *after* setting Read and write, or they stay
   read-only. The OAuth 2.0 Client ID/Secret and the bearer token are not used.
   Alternatively, with only the API key and secret, authorise the app through X's PIN flow
   (`oauth/request_token` with `oauth_callback=oob`, open the authorize link, then exchange the
   PIN at `oauth/access_token`).
4. **Credits:** X has no free posting tier. Without credits, every call answers 403
   `client-not-enrolled`, which the bot reports as "X needs Pay-Per-Use credits". Posting is
   pay-per-use: **$0.015 per post**, but **$0.20 per post that contains a link**. Buy credits in
   the console (Billing), set a monthly spending limit, and leave auto-recharge off at first.
   Ten posts a day comes to about $5 a month.
5. **`.env`:** set `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN` and `X_ACCESS_TOKEN_SECRET`.
   X publishing switches on when all four are set; approvals then post to Instagram *and* X, and
   the WhatsApp reply lists the links (or the reason) per platform.
6. **How each post goes out:** the JPEG is uploaded with `POST https://api.x.com/2/media/upload`
   (`media_category=tweet_image`), then posted with `POST https://api.x.com/2/tweets` (text plus
   `media.media_ids`). Requests are signed with OAuth 1.0a (HMAC-SHA1).
7. **The text:** headline, caption, up to three hashtags and "Source: BBC News" (plus the photo
   credit for licensed photos), within 280 characters as X counts them. When it's too long, the
   caption is dropped first, then hashtags. Links and bare domains ("Reuters.com") are removed,
   which keeps each post at $0.015.
8. **Safeguards:** the same as Instagram: each story is posted once, at most
   `JUSTNEWS_MAX_POSTS_PER_DAY` per day, and a timed-out post is looked up on the account before
   any retry. X also refuses identical text, which the publisher treats as "already posted".

## Engagement strategist and the agents' discussion

**Off by default for now** (`JUSTNEWS_STRATEGIST=true` turns it on). While it's off, nothing is
read from either platform, and the content creator still writes separate Instagram and X versions
from the latest saved playbook (the research defaults below); Jev's fact-check round still runs.

When on, Agent 6 runs before posts are written (`python -m justnews create`, and every hourly cycle).
- **Our engagement:** reads each live post's numbers at fixed ages: Instagram at 3, 24 and 72
  hours (free), X at 6 and 48 hours (X charges $0.005 per post read, so about $3 a month at 10 posts
  a day). Stored in `post_metrics`.
- **Other news accounts:** every 3 days (`JUSTNEWS_BENCHMARK_EVERY_HOURS`), reads the last 10 posts
  of `JUSTNEWS_BENCHMARK_INSTAGRAM` (bbcnews, reuters, ndtv, cnn) and `JUSTNEWS_BENCHMARK_X`
  (BBCWorld, Reuters, ndtv), about $1.50 a month on X. Stored in `benchmarks`.
- **Playbook:** when there's new evidence (at most every 6 hours, at least daily), Gemini updates the
  rules for each platform, plus "what other accounts do that we don't" and experiments. It starts
  from research-based defaults (`BASE_PLAYBOOK`) and is told how small our sample is. Observations
  about other accounts are kept only when benchmark posts were actually read. Stored in `playbooks`.

Then, for each run's drafts:
```
content creator ──drafts (Instagram + X versions)──▶ strategist
strategist ──notes: code checks (hook length, publisher name, X repeats the hook…) + Gemini critique──▶
content creator ──revises once, or pushes back when a note would need facts the source lacks──▶
fact checker (Jev) ──flags facts not in the source──▶ content creator revises once ──▶ Jev checks again
```
Still-flagged posts are marked ⚠️ in the WhatsApp shortlist. Every message is saved in
`agent_messages` and listed under "Discussion between the agents" in the run report.
`python -m justnews strategy` refreshes the numbers and prints the playbook.

**How the two versions differ** (the starting playbook):

| | Instagram | X |
|---|---|---|
| Reader | reads | scans |
| Shape | hook line (≤120 chars, the feed cuts there), 1-2 short paragraphs of the source's facts | one wire-style line leading with the news |
| Extras | a question about the reader's own experience (not on tragedies, crime, court cases or political disputes) | country flag; "JUST IN:" only for stories under an hour old |
| Hashtags | 3-5 (Instagram's limit is 5) | 1-2 |
| Links | source link (tracking parameters removed; Google News redirects dropped) | none (a link makes a post cost $0.20 instead of $0.015) |

**Research behind the defaults** (2026; mostly vendor studies, so directional):
- **Instagram ranking:** shares (sends), saves and comments outweigh likes. Carousels lead on saves
  and engagement rate, Reels on reach for small accounts
  ([Later](https://later.com/blog/how-instagram-algorithm-works/),
  [Socialinsider benchmarks](https://www.socialinsider.io/social-media-benchmarks/instagram),
  [Metricool 2026 study](https://metricool.com/press-release-instagram-study-2026/)).
- **X ranking:** X's open-sourced feed code (Jan 2026) predicts about 15 actions, with replies and
  reposts strong and mutes or blocks strongly negative; hashtags matter little, and posts with
  images do better than text-only
  ([summary of the code](https://postory.io/blog/x-algorithm-2026),
  [OpenTweet](https://opentweet.io/blog/how-twitter-x-algorithm-works-2026)).
- **Pricing:** [X API pricing](https://docs.x.com/x-api/getting-started/pricing).

**Free-tier note:** a cycle now makes up to 4 Gemini requests (write, review, revise, fact-check
revision) instead of 1. The strategist uses the lite models first, so the better models' daily
quota goes to writing.

## Running every hour

`python -m justnews serve` is the production process. It holds the WhatsApp connection and runs a
news cycle **every hour** (`JUSTNEWS_CYCLE_MINUTES`): discover, evaluate, write posts, then send
you a numbered shortlist.
- **Reply with numbers:** those stories are designed and posted to Instagram and X (whichever are
  connected), and the bot replies with the links.
- **Don't reply:** the list is ignored. The next hour's list replaces it, and those stories are
  never offered again; discovery only brings news not seen before.
- **Nothing new:** if a cycle finds no new recommended stories, no message is sent.

It runs the cycle directly in Python rather than through PicoClaw's cron, which would add a Claude
orchestrator call every hour (about $15/month). PicoClaw remains available for on-demand runs.

## Deploying to AWS (EC2 + GitHub Actions)

- **The server:** an EC2 instance runs `serve` as a systemd service (`deploy/justnews.service`).
  It only makes outbound connections, so **no inbound ports** are needed. SSH is limited to your
  IP, for setup only.
- **S3:** access comes from the instance's **IAM role**, so the server's `.env` has no AWS keys.
- **CI/CD:** every push to `main` runs the tests on GitHub Actions. If they pass, the workflow
  deploys through **AWS Systems Manager** (`.github/workflows/deploy.yml`, which runs
  `deploy/deploy.sh`). GitHub signs in to AWS with OIDC: no SSH keys or AWS secrets are stored in
  GitHub, and no port is opened for it.
- **First install:** run `deploy/setup-server.sh` on a fresh Ubuntu 24.04 instance, copy `.env`
  and `data/` (the WhatsApp session and history) to `/opt/justnews`, then
  `systemctl start justnews`.
- **One WhatsApp connection:** only one place may run the bridge at a time, so stop it on your Mac
  before starting the server.

### AWS setup notes

- **Server role:** the instance role needs **AmazonSSMManagedInstanceCore** (so GitHub can deploy
  through Systems Manager) plus the S3 policy for `posts/*`. After adding it, run
  `sudo snap restart amazon-ssm-agent` to register at once; otherwise the agent retries every
  ~30 minutes.
- **GitHub sign-in:** GitHub sends an OIDC subject that includes the owner's and repo's numeric
  IDs, e.g. `repo:surya-sirisolla@267958361/Just-News@1443222459:ref:refs/heads/main`. The deploy
  role's trust policy must match that exact subject; the name-only form is rejected.

### Operating the server

```bash
ssh -i ~/.ssh/justnews-key.pem ubuntu@<server-ip> 'sudo journalctl -u justnews -f'      # live logs
ssh -i ~/.ssh/justnews-key.pem ubuntu@<server-ip> 'sudo systemctl restart justnews'     # restart
ssh -i ~/.ssh/justnews-key.pem ubuntu@<server-ip> 'sudo /opt/justnews/deploy/deploy.sh' # deploy by hand
```

Pushes to `main` deploy automatically. A failed test aborts the deploy, and the running version
stays up. Restarts don't send an extra shortlist: the next cycle waits until an hour after the
last one.

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
