"""Quiet hours (JUSTNEWS_QUIET_HOURS, owner's local time): no news cycles, shortlists or retries.
The owner can still reply to an open list or chat with the bot at night."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def parse(spec: str) -> tuple[int, int] | None:
    """"23-7" -> (23, 7); "" or "off" -> None (never quiet)."""
    spec = (spec or "").strip().lower()
    if spec in ("", "off", "none"):
        return None
    start, end = (int(x) % 24 for x in spec.split("-"))
    return None if start == end else (start, end)


def quiet_until(settings, now: datetime) -> datetime | None:
    """When the quiet period around `now` ends (UTC), or None when `now` is not in quiet hours."""
    window = parse(settings.quiet_hours)
    if window is None:
        return None
    start, end = window
    local = now.astimezone(ZoneInfo(settings.timezone))
    inside = (local.hour >= start or local.hour < end) if start > end else start <= local.hour < end
    if not inside:
        return None
    ends = local.replace(hour=end, minute=0, second=0, microsecond=0)
    if ends <= local:
        ends += timedelta(days=1)
    return ends.astimezone(timezone.utc)


def local_time(settings, when: datetime) -> str:
    return when.astimezone(ZoneInfo(settings.timezone)).strftime("%-I:%M %p")
