# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
One window resolver for every Mission Control metrics surface.

Two ways to ask for a window, one precedence rule:

  * ``start`` / ``end`` — an explicit calendar range (month-to-date, last
    month, a custom range). FinOps lives in calendar months; a rolling
    "last 720 hours" is not "March".
  * ``period_hours`` — the rolling presets (24h/7d/30d/90d; 0 = all time).

An explicit ``start`` always wins over ``period_hours``: the caller that
bothered to name dates meant them. ``end`` may be open (None = now) so
"since March 1st" works without the caller inventing a fake end.

Kept dependency-free on purpose — cost, governance, proxy-metrics and
run-signal services all import it, and each converting timestamps its own
way is exactly how three tabs end up disagreeing about "last month".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple


def resolve_window(
    period_hours: Optional[int],
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> Tuple[Optional[datetime], Optional[datetime]]:
    """Return ``(window_start, window_end)``; either side may be None
    (no lower bound / open-ended "until now")."""
    start = _ensure_utc(start)
    end = _ensure_utc(end)
    if start is not None:
        return start, end
    if period_hours and period_hours > 0:
        return datetime.now(timezone.utc) - timedelta(hours=period_hours), end
    return None, end


def previous_window(
    window_start: Optional[datetime],
    window_end: Optional[datetime],
) -> Tuple[Optional[datetime], Optional[datetime]]:
    """The equal-length window immediately preceding the given one.

    Returns ``(None, None)`` for an unbounded window — "all time" has no
    previous period, and a delta against one would be fiction.
    """
    if window_start is None:
        return None, None
    effective_end = window_end or datetime.now(timezone.utc)
    length = effective_end - window_start
    if length.total_seconds() <= 0:
        return None, None
    return window_start - length, window_start


def _ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Naive datetimes (FastAPI parses `2026-03-01` as naive midnight) are
    taken as UTC rather than rejected — the admin API is UTC end to end."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def group_id_filter(column, group_id: Optional[str]):
    """SQLAlchemy condition for a Resource-Group scope that may be a single
    id OR a comma-separated set ("id1,id2") — the Mission Control selector
    supports scoping to several AI Systems at once (a team's portfolio is
    a FinOps unit too). Returns None when there is nothing to filter."""
    if not group_id:
        return None
    ids = [g.strip() for g in str(group_id).split(",") if g.strip()]
    if not ids:
        return None
    if len(ids) == 1:
        return column == ids[0]
    return column.in_(ids)
