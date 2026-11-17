# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Approval windows — how long a human-approval hold stays open

The Rust core resolves the window of EVERY hold through one function
(`brutor-gateway-core/src/services/approval_window.rs`); the control plane
stores the defaults and shows the effective value. Precedence, first that
states a window wins:

1. the policy that demanded the human, when it names a window for that action
   (a capability filter's `approval_timeout_seconds`, the group-wide MCP
   default's, the skill execution mode's) — the more specific statement wins
   in both directions;
2. the Resource Group's `approval_window_seconds`, **min-wins across the
   group and every ancestor** (always inherited, not gated by
   `inherit_from_parent` — the house rule for governance: a parent's window
   is a ceiling a child cannot lengthen; the same merge every other approval
   timeout uses). A child may not store a longer window than it inherits
   (400), so the console never shows a value the runtime discards;
3. the tenant's `default_approval_window_seconds`;
4. the platform default, `PLATFORM_DEFAULT_SECONDS`.

WIRE CONTRACT: `PLATFORM_DEFAULT_SECONDS`, `MAX_SECONDS` and `SOURCES` mirror
the core's `approval_window::{PLATFORM_DEFAULT_SECONDS, MAX_SECONDS,
WindowSource::as_str}` (pinned by `tests/test_wire_contract.py`); `MIN/MAX`
are the CHECK constraints of migration `a193_approval_window_epochs`.
"""

from __future__ import annotations

from typing import Iterable, Optional, Tuple

#: The window when no policy, group or tenant states one.
PLATFORM_DEFAULT_SECONDS = 300
#: Smallest group / tenant default an operator may set (a CHECK constraint).
MIN_SECONDS = 60
#: Largest window anywhere (7 days; a CHECK constraint and the core's clamp).
MAX_SECONDS = 7 * 24 * 3600

SOURCE_POLICY = "policy"
SOURCE_RESOURCE_GROUP = "resource_group"
SOURCE_TENANT = "tenant"
SOURCE_PLATFORM = "platform"
#: `tool_approval_requests.request_metadata.approval_window_source`.
SOURCES = (SOURCE_POLICY, SOURCE_RESOURCE_GROUP, SOURCE_TENANT, SOURCE_PLATFORM)


def validate_window(value: Optional[int], field: str) -> Optional[str]:
    """None when `value` is acceptable (NULL clears it), else the reason."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return f"{field} must be an integer number of seconds"
    if value < MIN_SECONDS or value > MAX_SECONDS:
        return f"{field} must be between {MIN_SECONDS} and {MAX_SECONDS} seconds (7 days)"
    return None


def chain_window(chain: Iterable[Tuple[str, Optional[int]]]) -> Tuple[Optional[int], Optional[str]]:
    """Min-wins over `(group_id, approval_window_seconds)` pairs, nearest
    first. Returns (window, the group that states it) — the nearest group on
    a tie."""
    best: Optional[int] = None
    holder: Optional[str] = None
    for group_id, value in chain:
        if value is None or value <= 0:
            continue
        if best is None or value < best:
            best, holder = value, group_id
    return best, holder


def effective_window(
    chain: Iterable[Tuple[str, Optional[int]]],
    tenant_default: Optional[int],
) -> Tuple[int, str, Optional[str]]:
    """The window a hold that names no policy window gets for the first
    group of `chain` (nearest first): (seconds, source, stating group)."""
    group_min, holder = chain_window(chain)
    if group_min is not None:
        return min(group_min, MAX_SECONDS), SOURCE_RESOURCE_GROUP, holder
    if tenant_default is not None and tenant_default > 0:
        return min(tenant_default, MAX_SECONDS), SOURCE_TENANT, None
    return PLATFORM_DEFAULT_SECONDS, SOURCE_PLATFORM, None


__all__ = [
    "MAX_SECONDS",
    "MIN_SECONDS",
    "PLATFORM_DEFAULT_SECONDS",
    "SOURCES",
    "SOURCE_PLATFORM",
    "SOURCE_POLICY",
    "SOURCE_RESOURCE_GROUP",
    "SOURCE_TENANT",
    "chain_window",
    "effective_window",
    "validate_window",
]
