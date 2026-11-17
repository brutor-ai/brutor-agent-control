# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Counter list aggregation for the System Operations Console

Authoritative counter state — current window usage + cooldowns —
lives in the Rust proxy's in-memory `UsageState`. The control plane
can't read that directly, so this module aggregates a best-effort
view from `usage_logs` over the most-recent window per counter kind.

The exact `cooldown_active` field is sourced via a future fanout
NOTIFY/reply round-trip; until that ships, it reads as False and the
admin UI surfaces the limitation. This avoids blocking the rest of
the Counters page on the Rust round-trip work, and the displayed
`used`/`limit` numbers are still accurate enough for operators to
decide whether to reset.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database.models.llm_model import LLModel
from app.database.models.mcp_server import MCPServer
from app.database.models.resource_group import ResourceGroup
from app.database.models.usage_log import UsageLog


# Window per counter kind. Matches the default seeded windows in
# resource_groups.llm_limits / mcp_limits; operators with custom
# windows still get a close-enough estimate (we're showing recent
# usage, not enforcing).
WINDOW_SECONDS: Dict[str, int] = {
    "llm_request_rate": 60,
    "llm_input_tokens": 60,
    "llm_output_tokens": 60,
    "llm_concurrent": 60,
    "llm_spend": 60,
    "mcp_tool_rate": 60,
    "mcp_session_rate": 60,
}


# Map from CounterScope wire token → which counter family (`_llm_*` /
# `_mcp_*` generator) emits rows for that subject type. Used by the
# `scope` filter param to suppress whole row families on a deep link
# whose target subject type doesn't match. Scopes not in this map
# (e.g. tenant / group / user / api_key today) don't trim row
# generation — those are subject types the current `usage_logs`
# schema doesn't carry as `resource_type`, so a row-level match isn't
# possible from this data source. The filter just becomes a no-op for
# those scopes; the UI's `subject_id` + `group_id` filters still apply.
_SCOPE_TO_FAMILIES: Dict[str, set[str]] = {
    "model": {"llm"},
    "mcp_server": {"mcp"},
}


def list_counters(
    db: Session,
    tenant_id: str,
    *,
    group_id: Optional[str] = None,
    kind: Optional[str] = None,
    subject_id: Optional[str] = None,
    scope: Optional[str] = None,
    only_cooling_down: bool = False,
) -> List[Dict[str, Any]]:
    """Build the counter table for the Counters page.

    One row per (group, subject, kind) tuple with non-zero recent
    activity OR a configured limit. The intent is to show the
    operator both 'buckets currently consuming budget' and 'buckets
    that exist even if idle right now' so they can pre-emptively
    reset before a wave lands.

    `scope` is the deep-link counterpart of `subject_id`: when the
    operator arrives via a PagerDuty / Proxy Logs link that pinned
    `scope=mcp_server`, suppress LLM-family rows entirely (and vice
    versa) so the empty-state message correctly says "no rows for
    scope X" instead of falling back to all-tenant.
    """
    allowed_families = _SCOPE_TO_FAMILIES.get(scope) if scope else None

    rows: List[Dict[str, Any]] = []
    if allowed_families is None or "llm" in allowed_families:
        rows.extend(_llm_counters(db, tenant_id, group_id=group_id, kind=kind, subject_id=subject_id))
    if allowed_families is None or "mcp" in allowed_families:
        rows.extend(_mcp_counters(db, tenant_id, group_id=group_id, kind=kind, subject_id=subject_id))
    if only_cooling_down:
        rows = [r for r in rows if r["cooldown_active"]]
    return rows


def _llm_counters(
    db: Session,
    tenant_id: str,
    *,
    group_id: Optional[str],
    kind: Optional[str],
    subject_id: Optional[str],
) -> List[Dict[str, Any]]:
    """Aggregate LLM request rate + token usage from usage_logs.

    One bucket per (group_id, resource_id) for the last 60s. We don't
    show every limit kind here — the Rust limits config is multi-
    dimensional and growing — but we show the dimensions the UI cares
    about: requests-per-minute, input tokens per minute, output
    tokens per minute, concurrent (approximated as in-flight = rows
    in the last 5s without a completion). Spend is a separate model.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=WINDOW_SECONDS["llm_request_rate"])
    q = (
        db.query(
            UsageLog.group_id,
            UsageLog.resource_id,
            UsageLog.resource_name,
            func.count(UsageLog.id).label("request_count"),
            func.coalesce(func.sum(UsageLog.prompt_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(UsageLog.completion_tokens), 0).label("output_tokens"),
        )
        .filter(
            UsageLog.tenant_id == tenant_id,
            UsageLog.resource_type == "llm",
            UsageLog.created_at >= cutoff,
        )
        .group_by(UsageLog.group_id, UsageLog.resource_id, UsageLog.resource_name)
    )
    if group_id:
        q = q.filter(UsageLog.group_id == group_id)
    if subject_id:
        q = q.filter(UsageLog.resource_id == subject_id)

    out: List[Dict[str, Any]] = []
    for row in q.all():
        if not kind or kind == "llm_request_rate":
            out.append(
                _row(
                    tenant_id=tenant_id,
                    group_id=row.group_id,
                    subject_id=row.resource_id,
                    subject_label=row.resource_name or row.resource_id,
                    kind="llm_request_rate",
                    window=f"{WINDOW_SECONDS['llm_request_rate']}s",
                    used=int(row.request_count),
                    limit=_limit_for(db, row.group_id, "llm_request_rate", row.resource_id),
                )
            )
        if not kind or kind == "llm_input_tokens":
            out.append(
                _row(
                    tenant_id=tenant_id,
                    group_id=row.group_id,
                    subject_id=row.resource_id,
                    subject_label=row.resource_name or row.resource_id,
                    kind="llm_input_tokens",
                    window=f"{WINDOW_SECONDS['llm_input_tokens']}s",
                    used=int(row.input_tokens),
                    limit=_limit_for(db, row.group_id, "llm_input_tokens", row.resource_id),
                )
            )
        if not kind or kind == "llm_output_tokens":
            out.append(
                _row(
                    tenant_id=tenant_id,
                    group_id=row.group_id,
                    subject_id=row.resource_id,
                    subject_label=row.resource_name or row.resource_id,
                    kind="llm_output_tokens",
                    window=f"{WINDOW_SECONDS['llm_output_tokens']}s",
                    used=int(row.output_tokens),
                    limit=_limit_for(db, row.group_id, "llm_output_tokens", row.resource_id),
                )
            )
    return out


def _mcp_counters(
    db: Session,
    tenant_id: str,
    *,
    group_id: Optional[str],
    kind: Optional[str],
    subject_id: Optional[str],
) -> List[Dict[str, Any]]:
    """One row per (group, mcp_server_id) for last-60s tool calls."""
    if kind and kind not in ("mcp_tool_rate", "mcp_session_rate"):
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=WINDOW_SECONDS["mcp_tool_rate"])
    q = (
        db.query(
            UsageLog.group_id,
            UsageLog.resource_id,
            UsageLog.resource_name,
            func.count(UsageLog.id).label("request_count"),
        )
        .filter(
            UsageLog.tenant_id == tenant_id,
            UsageLog.resource_type == "mcp",
            UsageLog.created_at >= cutoff,
        )
        .group_by(UsageLog.group_id, UsageLog.resource_id, UsageLog.resource_name)
    )
    if group_id:
        q = q.filter(UsageLog.group_id == group_id)
    if subject_id:
        q = q.filter(UsageLog.resource_id == subject_id)

    out: List[Dict[str, Any]] = []
    for row in q.all():
        out.append(
            _row(
                tenant_id=tenant_id,
                group_id=row.group_id,
                subject_id=row.resource_id,
                subject_label=row.resource_name or row.resource_id,
                kind="mcp_tool_rate",
                window=f"{WINDOW_SECONDS['mcp_tool_rate']}s",
                used=int(row.request_count),
                limit=_limit_for(db, row.group_id, "mcp_tool_rate", row.resource_id),
            )
        )
    return out


def _limit_for(
    db: Session,
    group_id: Optional[str],
    kind: str,
    subject_id: Optional[str],
) -> int:
    """Read the configured limit for (group, kind, subject).

    Best-effort — the multi-dimensional Rust limits config has
    several override layers (group / per-model / per-tool). We
    surface the group-level number; per-subject overrides will
    appear in a future drawer rather than collapsing them into
    a single cell. Returns 0 if no limit configured (unlimited).
    """
    if not group_id:
        return 0
    rg = db.query(ResourceGroup).filter(ResourceGroup.id == group_id).first()
    if not rg:
        return 0
    limits = getattr(rg, "llm_limits", None) if kind.startswith("llm_") else getattr(rg, "mcp_limits", None)
    if not isinstance(limits, dict):
        return 0
    mapping = {
        "llm_request_rate": ("rpm", 0),
        "llm_input_tokens": ("itpm", 0),
        "llm_output_tokens": ("otpm", 0),
        "llm_concurrent": ("concurrent", 0),
        "llm_spend": ("spend_rate", 0),
        "mcp_tool_rate": ("rpm", 0),
        "mcp_session_rate": ("rpm", 0),
    }
    key, default = mapping.get(kind, ("", 0))
    val = limits.get(key, default)
    return int(val) if isinstance(val, (int, float)) else default


def _row(
    *,
    tenant_id: str,
    group_id: Optional[str],
    subject_id: Optional[str],
    subject_label: Optional[str],
    kind: str,
    window: str,
    used: int,
    limit: int,
) -> Dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "group_id": group_id,
        "subject_id": subject_id,
        "subject_label": subject_label,
        "kind": kind,
        "window": window,
        "used": used,
        "limit": limit,
        # See module docstring: authoritative cooldown lives in the
        # Rust runtime; the endpoint surfaces False until
        # the round-trip is wired (tracked separately). Reset always
        # works regardless — it publishes NOTIFY, the proxy handles
        # whatever state it has locally.
        "cooldown_active": False,
        "cooldown_until": None,
        "last_updated_at": datetime.now(timezone.utc).isoformat(),
    }
