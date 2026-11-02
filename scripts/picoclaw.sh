#!/bin/sh
# Run PicoClaw with the Just News workspace and skills.
#
# Renders picoclaw/home/config.json from picoclaw/config.template.json and writes the LLM
# key (CLAUDE_API_KEY, or GEMINI_API_KEY when LLM_PROVIDER=gemini; from the environment or
# .env) into picoclaw/home/.security.yml. Both files are gitignored, so keys never land in git.
#
# Usage:
#   scripts/picoclaw.sh agent -m "Run the news cycle"
#   scripts/picoclaw.sh cron list
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOME_DIR="$ROOT/picoclaw/home"

if [ -f "$ROOT/.env" ]; then
  set -a; . "$ROOT/.env"; set +a
fi

# The orchestrator needs a generative LLM. LLM_PROVIDER picks the evaluator backend, which may be
# jev (not a chat model), so the orchestrator has its own PICOCLAW_LLM_PROVIDER setting.
PROVIDER="${PICOCLAW_LLM_PROVIDER:-}"
if [ -z "$PROVIDER" ]; then
  case "${LLM_PROVIDER:-}" in claude|gemini) PROVIDER="$LLM_PROVIDER" ;; esac
fi
if [ -z "$PROVIDER" ]; then
  if [ -n "${CLAUDE_API_KEY:-}" ]; then PROVIDER=claude; else PROVIDER=gemini; fi
fi
case "$PROVIDER" in
  claude)
    API_KEY="${CLAUDE_API_KEY:?CLAUDE_API_KEY is not set (add it to .env)}"
    # PicoClaw 0.3.1 always sends temperature (default 0.7), which current Claude models reject;
    # extra_body is merged last, so a null value removes it. Ignore any exported ANTHROPIC_BASE_URL.
    unset ANTHROPIC_BASE_URL
    export PICOCLAW_PROVIDER=anthropic PICOCLAW_API_BASE="https://api.anthropic.com/v1"
    export PICOCLAW_EXTRA_BODY='{"temperature": null}'
    export PICOCLAW_MODEL="${PICOCLAW_MODEL:-claude-haiku-5-5}" ;;
  gemini)
    API_KEY="${GEMINI_API_KEY:?GEMINI_API_KEY is not set (add it to .env)}"
    export PICOCLAW_PROVIDER=gemini PICOCLAW_API_BASE="https://generativelanguage.googleapis.com/v1beta" PICOCLAW_EXTRA_BODY='{}'
    export PICOCLAW_MODEL="${PICOCLAW_MODEL:-gemini-3.1-flash-lite}" ;;
  *) echo "Unknown orchestrator provider: $PROVIDER (use claude or gemini)" >&2; exit 1 ;;
esac

PICOCLAW_BIN="${PICOCLAW_BIN:-$ROOT/picoclaw/bin/picoclaw}"
[ -x "$PICOCLAW_BIN" ] || PICOCLAW_BIN="$(command -v picoclaw || true)"
[ -n "$PICOCLAW_BIN" ] || { echo "picoclaw binary not found (see README)" >&2; exit 1; }

export JUSTNEWS_PICOCLAW_WORKSPACE="$ROOT/picoclaw/workspace"
export PICOCLAW_HOME="$HOME_DIR"
export PICOCLAW_CONFIG="$HOME_DIR/config.json"

mkdir -p "$HOME_DIR"
umask 077
python3 - "$ROOT/picoclaw/config.template.json" "$PICOCLAW_CONFIG" <<'PY'
import os, sys
src, dst = sys.argv[1:3]
text = open(src).read()
for var in ("JUSTNEWS_PICOCLAW_WORKSPACE", "PICOCLAW_PROVIDER", "PICOCLAW_MODEL", "PICOCLAW_API_BASE",
            "PICOCLAW_EXTRA_BODY"):
    text = text.replace("${%s}" % var, os.environ[var])
open(dst, "w").write(text)
PY
cat > "$HOME_DIR/.security.yml" <<EOF
model_list:
  just-news-orchestrator:0:
    api_keys:
      - "$API_KEY"
EOF

exec "$PICOCLAW_BIN" --no-color "$@"
