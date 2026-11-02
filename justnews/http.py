"""HTTP fetching with timeouts, retries and back-off for flaky or rate-limited sites."""

from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; JustNewsBot/0.1; +https://github.com/just-news)"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class FetchError(Exception):
    pass


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/html;q=0.9, */*;q=0.8"})
    return s


def fetch(session: requests.Session, url: str, timeout: int, retries: int) -> requests.Response:
    """GET `url`, retrying transient failures. Raises FetchError with a readable reason."""
    last_error = "unknown error"
    for attempt in range(retries + 1):
        try:
            resp = session.get(url, timeout=timeout)
            if resp.status_code < 400:
                return resp
            last_error = f"HTTP {resp.status_code}"
            if resp.status_code not in RETRYABLE_STATUS:
                break
            wait = min(int(resp.headers.get("Retry-After", 0) or 0), 10) or 2 ** attempt
        except requests.Timeout:
            last_error, wait = f"timeout after {timeout}s", 2 ** attempt
        except requests.RequestException as exc:
            last_error, wait = f"{type(exc).__name__}: {exc}".split("(Caused by")[0].strip(), 2 ** attempt
        if attempt < retries:
            log.debug("Retrying %s in %ss (%s)", url, wait, last_error)
            time.sleep(wait)
    raise FetchError(last_error)
