# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Whether the gateway serves a model: its one availability rule.

Every query of the gateway that loads a model in order to call it, offer
it or pick it uses one predicate (`model_available_sql!` in its `lib.rs`).
A model is available when it is enabled, is not retired (it has no
`retire_date`, or that date is still in the future, UTC: from its retire
date on it is not called), and its provider record is enabled. A routing
group's virtual row (`mode = 'routing'`) stands for no provider of its
own.

`MODEL_AVAILABLE_SQL` is that predicate for an unaliased `llm_model` row,
held to the gateway's text by a test that reads the gateway. The Python
rule below says the same three things, for code that has a row in hand.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.constants.llm import LLModelMode

MODEL_AVAILABLE_SQL = (
    "enabled = true AND ("
    "retire_date IS NULL OR "
    "retire_date > (now() AT TIME ZONE 'UTC')::date) AND ("
    "mode = 'routing' OR EXISTS (SELECT 1 FROM llm_providers available_provider "
    "WHERE available_provider.id = provider_id AND available_provider.enabled = true))"
)


def _today() -> date:
    return datetime.now(timezone.utc).date()


def not_served_reason(
    *,
    retire_date: Optional[date],
    provider_enabled: Optional[bool],
    mode: Optional[str],
    today: Optional[date] = None,
) -> Optional[str]:
    """Why the gateway does not serve a row whatever its own switch says:
    it is retired, or its provider is disabled (or missing). None when
    neither holds."""
    if retire_date is not None and retire_date <= (today or _today()):
        return f"it is retired (retire_date {retire_date.isoformat()}, UTC)"
    if mode != LLModelMode.ROUTING.value and not provider_enabled:
        return "its provider is disabled"
    return None


def is_available(
    *,
    enabled: Optional[bool],
    retire_date: Optional[date],
    provider_enabled: Optional[bool],
    mode: Optional[str],
    today: Optional[date] = None,
) -> bool:
    """The gateway's rule for one row."""
    return bool(enabled) and not_served_reason(
        retire_date=retire_date, provider_enabled=provider_enabled, mode=mode, today=today
    ) is None


def row_not_served_reason(row) -> Optional[str]:
    """`not_served_reason` of an `LLModel` row (its provider loaded)."""
    provider = getattr(row, "provider", None)
    return not_served_reason(
        retire_date=row.retire_date,
        provider_enabled=bool(provider.enabled) if provider is not None else None,
        mode=row.mode,
    )


def provider_enabled(db: Session, provider_id: Optional[str]) -> Optional[bool]:
    """Whether the provider record is enabled; None when there is none."""
    from app.database.models.llm_provider import LlmProvider

    if not provider_id:
        return None
    row = db.query(LlmProvider.enabled).filter(LlmProvider.id == provider_id).first()
    return bool(row[0]) if row is not None else None
