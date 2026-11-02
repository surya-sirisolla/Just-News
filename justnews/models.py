"""Data shapes passed between the agents."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PARAMS = re.compile(r"^(utm_|fbclid|gclid|mc_|cmpid|ocid|taid|smid|src$|ref$)", re.I)
STOPWORDS = set(
    "a an the of to in on for and or but at by with from as is are was were be been it its this that "
    "after over into amid says say said new will can could would should has have had not no up out "
    "about than more most what who why how when where vs".split()
)


def canonical_url(url: str) -> str:
    """Lowercase host, drop fragments, tracking params and trailing slashes."""
    parts = urlsplit(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not _TRACKING_PARAMS.match(k)])
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower().removeprefix("www."), path, query, ""))


def article_id(url: str) -> str:
    return hashlib.sha1(canonical_url(url).encode()).hexdigest()[:16]


def title_tokens(title: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", title.lower())
    return {w for w in words if w not in STOPWORDS and len(w) > 1}


def normalize_title(title: str) -> str:
    return " ".join(sorted(title_tokens(title)))


def title_similarity(a: str, b: str) -> float:
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


@dataclass
class Article:
    title: str
    url: str
    source_name: str
    category: str
    published_at: datetime | None
    description: str = ""
    feed: str = ""
    id: str = field(default="")

    def __post_init__(self) -> None:
        if not self.id:
            self.id = article_id(self.url)


@dataclass
class Evaluation:
    article_id: str
    relevance: int
    credibility: int
    freshness: int
    audience_interest: int
    overall: int
    decision: str  # "Recommended" | "Rejected"
    reason: str
    duplicate_of: str | None = None
    llm_scored: bool = True
