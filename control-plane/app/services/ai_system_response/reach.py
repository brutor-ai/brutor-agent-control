# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Who an autonomy level actually binds

`approval_required` holds an agent identity's side-effecting calls for a human.
A plain API key or a person has no agent identity, so the proxy deliberately
lets their calls through (`gateway-core/src/services/autonomy.rs`,
`enforce_for_group`): there is no approval flow for them to wait in.

That is a sound rule and a dangerous record. A response policy once
set `approval_required` on a system whose traffic came from a plain key; the
drift event said the action was taken, and the next exfiltration call went
through. The tightening was real for nobody.

So whenever this level is planned or applied, the platform says how much of the
system it does not cover: active keys with no agent identity, and recent runs
whose actor was not one. `restricted` and `suspended` bind every caller and
need no such note.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.database.models.api_key import APIKey
from app.database.models.ai_system_run import AISystemRun

LEVEL_APPROVAL_REQUIRED = "approval_required"
AGENT_IDENTITY_ACTOR = "agent_identity"
#: The same lookback the drift baseline uses.
RECENT_DAYS = 14

LIMITATION_NON_AGENT_CALLERS = "approval_required_skips_non_agent_callers"


def approval_required_reach(
    db: Session,
    tenant_id: str,
    system_ids: Iterable[str],
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """How much of these systems' traffic `approval_required` would not hold."""
    ids: List[str] = list(system_ids)
    now = now or datetime.now(timezone.utc)
    unbound_keys = (
        db.query(func.count(APIKey.id))
        .filter(
            APIKey.tenant_id == tenant_id,
            APIKey.group_id.in_(ids),
            APIKey.agent_id.is_(None),
            APIKey.revoked.is_(False),
            APIKey.enabled.is_(True),
            or_(APIKey.expires_at.is_(None), APIKey.expires_at > now),
        )
        .scalar()
        or 0
    )
    since = now - timedelta(days=RECENT_DAYS)
    runs = db.query(AISystemRun.actor_type, func.count(AISystemRun.id)).filter(
        AISystemRun.tenant_id == tenant_id,
        AISystemRun.ai_system_id.in_(ids),
        AISystemRun.started_at >= since,
        AISystemRun.is_replay.is_(False),
    ).group_by(AISystemRun.actor_type)
    recent = 0
    unbound_runs = 0
    for actor_type, n in runs.all():
        recent += n
        if actor_type != AGENT_IDENTITY_ACTOR:
            unbound_runs += n
    return {
        "unbound_active_keys": int(unbound_keys),
        "recent_runs": int(recent),
        "unbound_recent_runs": int(unbound_runs),
        "recent_days": RECENT_DAYS,
    }


def describe(reach: Dict[str, Any]) -> str:
    keys = reach["unbound_active_keys"]
    runs = reach["unbound_recent_runs"]
    return (
        "Approval required holds agent identities only. "
        f"{keys} active key(s) without an agent identity and "
        f"{runs} of {reach['recent_runs']} run(s) in the last {reach['recent_days']} days "
        "come from callers it does not hold; their calls continue. "
        "Use restricted or suspended to stop every caller."
    )


def limitation_for(
    db: Session,
    tenant_id: str,
    level: Optional[str],
    system_ids: Iterable[str],
    *,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """The note to record next to an autonomy action, or None when the level
    binds every caller or no caller escapes it."""
    if level != LEVEL_APPROVAL_REQUIRED:
        return None
    reach = approval_required_reach(db, tenant_id, system_ids, now=now)
    if not reach["unbound_active_keys"] and not reach["unbound_recent_runs"]:
        return None
    return {"code": LIMITATION_NON_AGENT_CALLERS, "detail": describe(reach), **reach}


__all__ = [
    "LIMITATION_NON_AGENT_CALLERS",
    "approval_required_reach",
    "describe",
    "limitation_for",
]
