---
name: news-approval
description: Ask the owner on WhatsApp which draft posts to turn into templates and post. Sends a numbered shortlist (headline, caption, hashtags, source, link) as text, without images. The owner's reply triggers image design for the chosen posts only.
metadata: {"nanobot":{"emoji":"✅","requires":{"bins":["sh"]}}}
---

# Approval (WhatsApp)

After `news-content-creator` has written draft posts, send the owner the shortlist:

```bash
./bin/justnews request-approval <run_id>
```

Stdout is JSON with `channel`, `requested` and `status`.

- When `channel` is `whatsapp`, the shortlist is queued for the WhatsApp bridge, which sends it
  from the linked spare number to the owner. Their reply is handled by the bridge, which designs
  images only for the chosen posts. You don't need to wait for it or run `design` yourself. If
  `bridge_running` is false, tell the user to start it with `python -m justnews whatsapp`.
- When `channel` is `console`, WhatsApp isn't configured and the shortlist was printed instead.
  Show the user the stories and ask which numbers to approve. When they answer, apply it:

```bash
./bin/justnews reply "1 3"
```

`reply` accepts numbers (`1 3`, `1,3`, `1-3`), `all` or `none`.

## Rules

- Never send images for confirmation, and never approve on the owner's behalf. Only run
  `reply` with the numbers the user actually gave.
- Approved posts are queued. Instagram auto-posting is not connected yet, so never claim that
  anything was posted.
