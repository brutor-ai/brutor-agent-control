# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Shared column builders for the evidence-programme models

Every evidence-programme table carries the same `created_at` / `updated_at` shape and the
same `to_dict()` conversions; these builders keep the ten models from
restating them. Each call returns a NEW `Column` — a Column object cannot
be shared between tables.

`server_default`s mirror `alembic/versions/20260920_1000_a178_evidence_programme.py`
exactly (pinned by `tests/test_orm_server_defaults_match_migrations.py`).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Optional

from sqlalchemy import Column, DateTime, func


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def created_at_column() -> Column:
    return Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=func.now(),
    )


def updated_at_column() -> Column:
    return Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )


def iso(value: Optional[Any]) -> Optional[str]:
    """datetime / date → ISO-8601; None stays None."""
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def number(value: Optional[Any]) -> Optional[float]:
    """NUMERIC (Decimal) → float for JSON; None stays None."""
    return None if value is None else float(value)


def check_vocab(field: str, value: Optional[str], allowed, *, nullable: bool = False):
    """Validator body shared by every vocabulary column."""
    if value is None and nullable:
        return value
    if value not in allowed:
        raise ValueError(f"{field} must be one of {sorted(allowed)}, got {value!r}")
    return value


__all__ = [
    "utcnow",
    "created_at_column",
    "updated_at_column",
    "iso",
    "number",
    "check_vocab",
]
