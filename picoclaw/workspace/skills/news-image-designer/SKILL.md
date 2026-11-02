---
name: news-image-designer
description: Agent 4 of Just News. Make one 1080x1080 image per draft post, using either a licensed Wikimedia Commons photo with a text overlay and credit, or a clean white-background text graphic. Drafts only.
metadata: {"nanobot":{"emoji":"🖼️","requires":{"bins":["sh"]}}}
---

# Image Designer Agent

For each draft post without an image:

- **Licensed photo**, when the content creator suggested one. It searches Wikimedia Commons and keeps
  only CC0, public-domain, CC BY or CC BY-SA images with no extra restrictions. Jev picks the photo
  that best shows the story's subject. The image text, source and photo credit are drawn on it, and
  the licence is recorded.
- **Text graphic** otherwise, or when no fitting licensed photo exists: a white background with
  the image text, headline, category, source and brand.

No AI image generation is used, so the text in every image is exact. Files are saved to
`output/<run_id>/` in the project.

## Run

```bash
./bin/justnews design <run_id>
```

It takes about 5–30 seconds. Stdout is a JSON summary with `photos`, `graphics`, `failed` and `output_dir`.

## Rules

- Never publish, post or send images anywhere.
