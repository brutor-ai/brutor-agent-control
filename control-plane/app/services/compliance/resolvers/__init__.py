# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# The shared resolver library

    evaluate(db, tenant_id, spec, ai_system_id, window) -> StatementResult

Resolvers are framework-neutral: the same `(resolver, params)` gives the
same result whichever framework cites it — that is what lets one evidence
log serve many audits. `judge.<template>` ids dispatch to the judged
resolver for the four tier-B templates.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional, Tuple

from sqlalchemy.orm import Session

from app.services.compliance.catalogs.base import EvidenceSpec
from app.services.compliance.resolvers._base import (
    DECLARED,
    DEFAULT_WINDOW_DAYS,
    INDICATOR,
    JUDGE_TEMPLATES,
    MET,
    NOT_EVALUABLE,
    NOT_MET,
    RESOLVERS,
    STATEMENT_STATUSES,
    STATUS_RANK,
    UNDETERMINED,
    Outcome,
    ResolverContext,
    StatementResult,
    split_judge,
    utcnow,
    worst,
)

# Registration side effects — every module below decorates its resolvers.
from app.services.compliance.resolvers import (  # noqa: E402,F401  (registration)
    dependencies,
    documents,
    judge as _judge,
    monitoring,
    notice,
    oversight,
    records,
    retention,
)

log = logging.getLogger("brutor.compliance.resolvers")


def resolver_exists(resolver_id: str) -> bool:
    return resolver_id in RESOLVERS or split_judge(resolver_id) is not None


def run(ctx: ResolverContext, spec: EvidenceSpec, *, catalog_version: Optional[str] = None) -> StatementResult:
    """Evaluate one spec in an existing context (shares its memo)."""
    fn = RESOLVERS.get(spec.resolver)
    if fn is None and split_judge(spec.resolver) is not None:
        fn = _judge.judge
    if fn is None:
        outcome = Outcome(NOT_EVALUABLE, detail=f"No resolver named {spec.resolver!r}.")
    else:
        try:
            outcome = fn(ctx, spec)
        except Exception as exc:  # noqa: BLE001 — a resolver failure is not a verdict
            log.exception("resolver %s failed for system %s", spec.resolver, ctx.system_id)
            try:
                ctx.db.rollback()
            except Exception:  # noqa: BLE001
                pass
            outcome = Outcome(NOT_EVALUABLE, detail=f"The statement could not be evaluated ({exc.__class__.__name__}).")
    status = outcome.status if outcome.status in STATEMENT_STATUSES else NOT_EVALUABLE
    # Basis discipline: a declared statement is never `met`, an indicator
    # never passes or fails.
    if spec.basis == "declared" and status == MET:
        status = DECLARED
    if spec.basis == "indicator" and status in (MET, NOT_MET):
        status = INDICATOR
    return StatementResult(
        key=spec.key,
        basis=spec.basis,
        status=status,
        n=outcome.n,
        m=outcome.m,
        detail=outcome.detail,
        limit=spec.limit,
        citations=[c for c in outcome.citations if c],
        evaluated_at=ctx.now,
        statement=spec.statement,
        resolver=spec.resolver,
        params=dict(spec.params),
        catalog_version=catalog_version,
        window_start=ctx.window_start,
        window_end=ctx.window_end,
        facts=outcome.facts,
        refs=outcome.refs,
    )


def evaluate(
    db: Session,
    tenant_id: str,
    spec: EvidenceSpec,
    ai_system_id: str,
    window: Tuple[datetime, datetime],
    *,
    framework_id: Optional[str] = None,
    catalog_version: Optional[str] = None,
    now: Optional[datetime] = None,
) -> StatementResult:
    """Entry point: one spec, one system, one window."""
    from app.database.models.ai_system_compliance_profile import AISystemComplianceProfile
    from app.database.models.resource_group import ResourceGroup

    system = (
        db.query(ResourceGroup)
        .filter(ResourceGroup.id == ai_system_id, ResourceGroup.tenant_id == tenant_id)
        .first()
    )
    if system is None:
        return StatementResult(key=spec.key, basis=spec.basis, status=NOT_EVALUABLE,
                               detail=f"AI System {ai_system_id!r} not found.", statement=spec.statement,
                               resolver=spec.resolver, params=dict(spec.params), limit=spec.limit)
    profile = db.get(AISystemComplianceProfile, ai_system_id)
    if profile is not None and profile.tenant_id != tenant_id:
        profile = None
    ctx = ResolverContext(
        db=db, tenant_id=tenant_id, system=system, window_start=window[0], window_end=window[1],
        framework_id=framework_id, now=now or utcnow(), profile=profile,
    )
    return run(ctx, spec, catalog_version=catalog_version)


__all__ = [
    "DECLARED",
    "DEFAULT_WINDOW_DAYS",
    "INDICATOR",
    "JUDGE_TEMPLATES",
    "MET",
    "NOT_EVALUABLE",
    "NOT_MET",
    "RESOLVERS",
    "ResolverContext",
    "STATEMENT_STATUSES",
    "STATUS_RANK",
    "StatementResult",
    "UNDETERMINED",
    "evaluate",
    "resolver_exists",
    "run",
    "worst",
]
