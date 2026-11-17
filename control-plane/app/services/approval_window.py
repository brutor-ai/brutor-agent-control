# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Approval windows — what a group states, inherits and gets

The rule lives in `app/constants/approval_window.py`; the runtime resolver is
the core's `services/approval_window.rs`. This module answers the console's
questions about one group, and refuses a window a child may not store.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.constants.approval_window import (
    MAX_SECONDS,
    MIN_SECONDS,
    PLATFORM_DEFAULT_SECONDS,
    chain_window,
    effective_window,
)


def _ancestors_nearest_first(group: Any) -> List[Any]:
    """Every ancestor, nearest first. Always inherited (governance is not
    gated by `inherit_from_parent`), matching the core's chain walk."""
    return list(reversed(group.get_ancestors()))


def inherited_window(group: Any) -> Tuple[Optional[int], Optional[Any]]:
    """The smallest window an ancestor states, and that ancestor."""
    ancestors = _ancestors_nearest_first(group)
    value, holder_id = chain_window((g.id, g.approval_window_seconds) for g in ancestors)
    holder = next((g for g in ancestors if g.id == holder_id), None)
    return value, holder


def chain_min(group: Any) -> Optional[int]:
    """Min-wins over the group and every ancestor — what the core reads."""
    chain = [group, *_ancestors_nearest_first(group)]
    return chain_window((g.id, g.approval_window_seconds) for g in chain)[0]


def relaxation(group: Any, new_value: Optional[int]) -> Optional[str]:
    """Why `new_value` may not be stored on `group`, or None.

    Min-wins: a window longer than the inherited one changes nothing at
    runtime today and would silently become real the day the ancestor
    relaxes — refused like every other governance relaxation."""
    if new_value is None:
        return None
    inherited, holder = inherited_window(group)
    if inherited is not None and new_value > inherited:
        name = getattr(holder, "name", None) or getattr(holder, "id", "an ancestor")
        return (
            f"approval_window_seconds {new_value} is longer than the {inherited} s "
            f"inherited from '{name}'; a group may only shorten the window it inherits"
        )
    return None


def describe(db: Session, group: Any) -> Dict[str, Any]:
    """The `ApprovalWindowResponse` body for `group`."""
    from app.database.models.tenant import Tenant

    tenant_default = (
        db.query(Tenant.default_approval_window_seconds)
        .filter(Tenant.id == group.tenant_id)
        .scalar()
    )
    inherited, holder = inherited_window(group)
    chain = [group, *_ancestors_nearest_first(group)]
    seconds, source, _ = effective_window(
        ((g.id, g.approval_window_seconds) for g in chain), tenant_default
    )
    return {
        "group_id": group.id,
        "group_name": group.name,
        "approval_window_seconds": group.approval_window_seconds,
        "inherited_window_seconds": inherited,
        "inherited_from_group_id": getattr(holder, "id", None),
        "inherited_from_group_name": getattr(holder, "name", None),
        "tenant_default_seconds": tenant_default,
        "platform_default_seconds": PLATFORM_DEFAULT_SECONDS,
        "effective_seconds": seconds,
        "effective_source": source,
        "min_seconds": MIN_SECONDS,
        "max_seconds": MAX_SECONDS,
    }


__all__ = ["chain_min", "describe", "inherited_window", "relaxation"]
