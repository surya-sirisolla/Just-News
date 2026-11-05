"""Runtime settings, loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

CATEGORIES = ["India", "South India", "Business", "Technology", "AI", "World"]


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _list(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    return [x.strip() for x in raw.split(",") if x.strip()] if raw else default


# Cheapest capable model per provider and its list price (USD per 1M tokens, Nov 2026).
PROVIDER_DEFAULTS = {
    "claude": {"model": "claude-haiku-5-5", "in": 0.10, "out": 0.50},
    "gemini": {"model": "gemini-3.1-flash-lite", "in": 0.25, "out": 1.50},
    # Pinned rather than jev-latest so thresholds stay valid when the alias moves.
    "jev": {"model": "jev-1.14.0", "in": 0.042, "out": 0.0},
}


def _default_provider() -> str:
    """Jev for decisions when its key is set (fast, calibrated, no Claude credits), else Claude, else Gemini."""
    if os.getenv("TYPESAFE_API_KEY"):
        return "jev"
    return "claude" if (os.getenv("CLAUDE_API_KEY") or os.getenv("ANTHROPIC_API_KEY")) else "gemini"


def _provider() -> str:
    p = os.getenv("LLM_PROVIDER") or _default_provider()
    return p if p in PROVIDER_DEFAULTS else "claude"


@dataclass(frozen=True)
class Settings:
    db_path: Path = field(default_factory=lambda: Path(os.getenv("JUSTNEWS_DB_PATH", PROJECT_ROOT / "data" / "justnews.db")))
    log_dir: Path = field(default_factory=lambda: Path(os.getenv("JUSTNEWS_LOG_DIR", PROJECT_ROOT / "logs")))
    log_level: str = field(default_factory=lambda: os.getenv("JUSTNEWS_LOG_LEVEL", "INFO"))

    # Discovery
    categories: list[str] = field(default_factory=lambda: _list("JUSTNEWS_CATEGORIES", CATEGORIES))
    max_age_hours: int = field(default_factory=lambda: _int("JUSTNEWS_MAX_AGE_HOURS", 36))
    max_per_category: int = field(default_factory=lambda: _int("JUSTNEWS_MAX_PER_CATEGORY", 12))
    request_timeout: int = field(default_factory=lambda: _int("JUSTNEWS_REQUEST_TIMEOUT", 15))
    request_retries: int = field(default_factory=lambda: _int("JUSTNEWS_REQUEST_RETRIES", 2))

    # Evaluator backend (LLM_PROVIDER=jev|claude|gemini); see _default_provider.
    claude_api_key: str = field(default_factory=lambda: os.getenv("CLAUDE_API_KEY") or os.getenv("ANTHROPIC_API_KEY", ""))
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    typesafe_api_key: str = field(default_factory=lambda: os.getenv("TYPESAFE_API_KEY", ""))
    llm_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER") or _default_provider())
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL") or PROVIDER_DEFAULTS[_provider()]["model"])
    # Claude effort (low|medium|high): "low" suits batch scoring and keeps thinking tokens down.
    claude_effort: str = field(default_factory=lambda: os.getenv("JUSTNEWS_CLAUDE_EFFORT", "low"))
    # Explicit, so an ANTHROPIC_BASE_URL exported by other tools (e.g. a proxy) is never picked up.
    claude_base_url: str = field(default_factory=lambda: os.getenv("JUSTNEWS_ANTHROPIC_BASE_URL", "https://api.anthropic.com"))
    # Gemini 3 thinking level (minimal|low|medium|high).
    gemini_thinking_level: str = field(default_factory=lambda: os.getenv("GEMINI_THINKING_LEVEL", "minimal"))
    # USD per 1M tokens, used only for the cost estimate in the report.
    price_input_per_m: float = field(default_factory=lambda: _float("LLM_PRICE_INPUT_PER_M", PROVIDER_DEFAULTS[_provider()]["in"]))
    price_output_per_m: float = field(default_factory=lambda: _float("LLM_PRICE_OUTPUT_PER_M", PROVIDER_DEFAULTS[_provider()]["out"]))
    eval_batch_size: int = field(default_factory=lambda: _int("JUSTNEWS_EVAL_BATCH_SIZE", 20))
    recommend_threshold: int = field(default_factory=lambda: _int("JUSTNEWS_RECOMMEND_THRESHOLD", 70))
    history_days: int = field(default_factory=lambda: _int("JUSTNEWS_HISTORY_DAYS", 7))

    # Content creator (Gemini free tier). Models are tried in order; the next one is used when a
    # model is unavailable to the key or its daily free quota is used up.
    gemini_content_models: list[str] = field(default_factory=lambda: _list(
        "GEMINI_CONTENT_MODELS", ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]))
    gemini_content_thinking_level: str = field(default_factory=lambda: os.getenv("GEMINI_CONTENT_THINKING_LEVEL", "low"))
    # Minimum gap between Gemini requests, to stay under free-tier requests-per-minute limits.
    gemini_min_interval: float = field(default_factory=lambda: _float("GEMINI_MIN_INTERVAL_SECONDS", 6.0))
    max_posts_per_run: int = field(default_factory=lambda: _int("JUSTNEWS_MAX_POSTS_PER_RUN", 10))
    # The shortlist's mix (regions.py): mostly India and South India; world news only when important.
    international_share: float = field(default_factory=lambda: _float("JUSTNEWS_INTERNATIONAL_SHARE", 0.2))
    international_min_score: int = field(default_factory=lambda: _int("JUSTNEWS_INTERNATIONAL_MIN_SCORE", 85))
    south_india_share: float = field(default_factory=lambda: _float("JUSTNEWS_SOUTH_INDIA_SHARE", 0.4))
    # On-demand research from WhatsApp ("research on AQI today"): a lower bar, a short list.
    research_threshold: int = field(default_factory=lambda: _int("JUSTNEWS_RESEARCH_THRESHOLD", 55))
    research_max_posts: int = field(default_factory=lambda: _int("JUSTNEWS_RESEARCH_MAX_POSTS", 5))
    # Total seconds the content creator may spend across models, so a cycle always finishes.
    gemini_content_time_budget: float = field(default_factory=lambda: _float("GEMINI_CONTENT_TIME_BUDGET", 150.0))

    # Image designer
    output_dir: Path = field(default_factory=lambda: Path(os.getenv("JUSTNEWS_OUTPUT_DIR", PROJECT_ROOT / "output")))
    image_size: tuple[int, int] = field(default_factory=lambda: tuple(
        int(x) for x in os.getenv("JUSTNEWS_IMAGE_SIZE", "1080x1080").lower().split("x")))
    # Licences that allow commercial use and modification (a text overlay is a modification).
    image_licenses: list[str] = field(default_factory=lambda: _list(
        "JUSTNEWS_IMAGE_LICENSES", ["cc0", "pd", "cc-by", "cc-by-sa"]))
    photo_search: bool = field(default_factory=lambda: os.getenv("JUSTNEWS_PHOTO_SEARCH", "true").lower() == "true")
    # Wikimedia asks API clients to identify themselves; put a contact URL or email here.
    contact: str = field(default_factory=lambda: os.getenv("JUSTNEWS_CONTACT", ""))
    brand_name: str = field(default_factory=lambda: os.getenv("JUSTNEWS_BRAND", "JUST NEWS"))
    font_bold: str = field(default_factory=lambda: os.getenv("JUSTNEWS_FONT_BOLD", ""))
    font_regular: str = field(default_factory=lambda: os.getenv("JUSTNEWS_FONT_REGULAR", ""))

    # WhatsApp approval through a linked spare number (see justnews/whatsapp.py). Approval is on
    # when the owner number is set; otherwise `run` designs images for every draft as before.
    # The owner is the only number allowed to approve: digits with country code, no "+".
    whatsapp_owner: str = field(default_factory=lambda: os.getenv("WHATSAPP_OWNER_NUMBER", "").lstrip("+").replace(" ", ""))
    whatsapp_session: Path = field(default_factory=lambda: Path(os.getenv(
        "WHATSAPP_SESSION_PATH", PROJECT_ROOT / "data" / "whatsapp-session.sqlite3")))
    approval_expiry_hours: float = field(default_factory=lambda: _float("JUSTNEWS_APPROVAL_EXPIRY_HOURS", 1.0))
    # `serve` runs a news cycle this often; an unanswered shortlist is replaced by the next one.
    cycle_minutes: int = field(default_factory=lambda: _int("JUSTNEWS_CYCLE_MINUTES", 60))
    timezone: str = field(default_factory=lambda: os.getenv("JUSTNEWS_TIMEZONE", "Asia/Kolkata"))
    # No news cycles, shortlists or retries between these local hours ("23-7"; "off" disables).
    quiet_hours: str = field(default_factory=lambda: os.getenv("JUSTNEWS_QUIET_HOURS", "23-7"))
    # Free-tier Gemini models for chatting with the owner, fastest first (replies should feel instant).
    gemini_chat_models: list[str] = field(default_factory=lambda: _list(
        "GEMINI_CHAT_MODELS", ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.6-flash"]))

    # Instagram publishing (Instagram Graph API via a Meta system-user token) and S3 for the
    # images Instagram downloads. Publishing is on when the token, account ID and bucket are set.
    instagram_token: str = field(default_factory=lambda: os.getenv("INSTAGRAM_ACCESS_TOKEN", "").strip())
    instagram_user_id: str = field(default_factory=lambda: os.getenv("INSTAGRAM_USER_ID", "").strip())
    instagram_api_version: str = field(default_factory=lambda: os.getenv("INSTAGRAM_API_VERSION", "v23.0"))
    s3_bucket: str = field(default_factory=lambda: os.getenv("JUSTNEWS_S3_BUCKET", "").strip())
    aws_region: str = field(default_factory=lambda: os.getenv("AWS_REGION", "ap-south-1"))
    # Our own cap, well under Instagram's 100 API posts per 24 hours. A carousel counts as one post.
    max_posts_per_day: int = field(default_factory=lambda: _int("JUSTNEWS_MAX_POSTS_PER_DAY", 20))
    # Two or more approved stories go to Instagram as one swipeable carousel (cover, a slide per story, close).
    instagram_carousel: bool = field(default_factory=lambda: os.getenv("JUSTNEWS_INSTAGRAM_CAROUSEL", "true").lower() == "true")
    # X gets one post per story, spaced out like a newsroom feed; posts still waiting after
    # JUSTNEWS_X_QUEUE_MAX_HOURS are dropped rather than posted stale.
    x_spacing_minutes: float = field(default_factory=lambda: _float("JUSTNEWS_X_SPACING_MINUTES", 5))
    x_queue_max_hours: float = field(default_factory=lambda: _float("JUSTNEWS_X_QUEUE_MAX_HOURS", 3))
    # Every day at this local time, a Reel recaps the stories posted that day ("off" disables).
    recap_time: str = field(default_factory=lambda: os.getenv("JUSTNEWS_RECAP_TIME", "21:30"))
    # Clarity over count: the day's top stories, about 8 seconds each.
    recap_max_stories: int = field(default_factory=lambda: _int("JUSTNEWS_RECAP_MAX_STORIES", 5))
    # Background music: a track's name (the owner's pick, calm-keys), "rotate" (a different one of our
    # own tracks each day) or "off".
    recap_music: str = field(default_factory=lambda: os.getenv("JUSTNEWS_RECAP_MUSIC", "calm-keys"))

    # X (API v2) with OAuth 1.0a keys for the posting account itself. Posting needs Pay-Per-Use
    # credits on console.x.com. Publishing is on when all four are set.
    x_api_key: str = field(default_factory=lambda: os.getenv("X_API_KEY", "").strip())
    x_api_secret: str = field(default_factory=lambda: os.getenv("X_API_SECRET", "").strip())
    x_access_token: str = field(default_factory=lambda: os.getenv("X_ACCESS_TOKEN", "").strip())
    x_access_token_secret: str = field(default_factory=lambda: os.getenv("X_ACCESS_TOKEN_SECRET", "").strip())

    # Engagement strategist (agents/strategist.py): learns from our posts' engagement and from
    # other news accounts, and reviews drafts with the content creator. Off by default for now;
    # without it, posts follow the latest saved playbook (or the research defaults).
    strategist: bool = field(default_factory=lambda: os.getenv("JUSTNEWS_STRATEGIST", "false").lower() == "true")
    benchmark_instagram: list[str] = field(default_factory=lambda: _list(
        "JUSTNEWS_BENCHMARK_INSTAGRAM", ["bbcnews", "reuters", "ndtv", "cnn"]))
    # X charges per post read ($0.005): 3 accounts x 10 posts every 3 days is about $1.50 a month.
    benchmark_x: list[str] = field(default_factory=lambda: _list("JUSTNEWS_BENCHMARK_X", ["BBCWorld", "Reuters", "ndtv"]))
    benchmark_posts: int = field(default_factory=lambda: _int("JUSTNEWS_BENCHMARK_POSTS", 10))
    benchmark_every_hours: float = field(default_factory=lambda: _float("JUSTNEWS_BENCHMARK_EVERY_HOURS", 72))

    @property
    def instagram_enabled(self) -> bool:
        return bool(self.instagram_token and self.instagram_user_id and self.s3_bucket)

    @property
    def x_enabled(self) -> bool:
        return bool(self.x_api_key and self.x_api_secret and self.x_access_token and self.x_access_token_secret)

    @property
    def whatsapp_enabled(self) -> bool:
        return bool(self.whatsapp_owner)

    @property
    def whatsapp_heartbeat(self) -> Path:
        return self.whatsapp_session.with_suffix(".heartbeat")

    @property
    def whatsapp_qr_png(self) -> Path:
        return self.whatsapp_session.with_name("whatsapp-qr.png")


settings = Settings()
