# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Shared HTTP plumbing for the compliance routers

Incidents, notice configs, reports, oversight, documentation and blind
reviews all answer the same three questions the same way: which tenant
(the shared `resolve_tenant_id` rule), which AI System (must be one of the
tenant's `ai_system` groups), and did the record seal (`SealStatus`, the
block every write response carries so the console can say "not sealed").
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from fastapi import HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database.models.resource_group import GroupType, ResourceGroup
from app.services.compliance.sealing import SealOutcome
from app.middleware.trusted_proxies import client_ip


class SealStatus(BaseModel):
    """Whether the write was sealed into the tenant log. `sealed: false`
    with `seal_error` (`evidence_disabled`, `core_unreachable`, …) means
    the change is saved but no record exists for it."""

    sealed: bool = False
    record_id: Optional[str] = None
    seal_error: Optional[str] = None
    seal_detail: Optional[str] = None


def seal_status(outcome: Optional[SealOutcome], *, record_id: Optional[str] = None) -> SealStatus:
    if outcome is None:
        # Nothing was sealed on this request (e.g. an idempotent re-read):
        # report the stored record.
        return SealStatus(sealed=record_id is not None, record_id=record_id)
    return SealStatus(**outcome.status())


def client_info(request: Request) -> Dict[str, Any]:
    return {
        "ip_address": client_ip(request),
        "user_agent": request.headers.get("user-agent"),
        "request_method": request.method,
        "request_path": str(request.url.path),
    }


def load_ai_system(db: Session, tenant_id: str, ai_system_id: str) -> ResourceGroup:
    system = (
        db.query(ResourceGroup)
        .filter(ResourceGroup.id == ai_system_id, ResourceGroup.tenant_id == tenant_id)
        .first()
    )
    if system is None or system.group_type != GroupType.AI_SYSTEM:
        raise HTTPException(status_code=404, detail=f"AI System {ai_system_id!r} not found")
    return system


def parse_instant(value: Optional[str], name: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{name}: not an ISO-8601 timestamp") from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_window(
    from_: Optional[str], to: Optional[str], *, default_days: int = 30
) -> Tuple[datetime, datetime]:
    end = parse_instant(to, "to") or datetime.now(timezone.utc)
    start = parse_instant(from_, "from") or end - timedelta(days=default_days)
    if start >= end:
        raise HTTPException(status_code=422, detail="`from` must be earlier than `to`")
    return start, end


def parse_day(value: Optional[str], name: str) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{name}: not an ISO-8601 date") from exc


__all__ = [
    "SealStatus",
    "client_info",
    "load_ai_system",
    "parse_day",
    "parse_instant",
    "parse_window",
    "seal_status",
]
