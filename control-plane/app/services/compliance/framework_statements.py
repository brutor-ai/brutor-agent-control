# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Registry statements for the legacy per-framework exports

The framework exports (`/compliance/gdpr/article-30`, `/soc2/control-coverage`,
`/hipaa/phi-access`, `/iso-42001/activity`, `/eu-ai-act/risk-classification`)
keep their response shape and gain a `statements` block: the framework's
catalog statements evaluated over the same window through the shared
resolvers, estate-wide — the same rows an Evidence Report would carry.

Read-only and best-effort: an export must never fail because the
statement engine could not run; the block then says `available: false`.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Iterable, Optional

from sqlalchemy.orm import Session

from app.services.compliance import offering
from app.services.compliance.catalogs.base import NOT_LEGAL_ADVICE

log = logging.getLogger("brutor.compliance.statements")


def statements(
    db: Session,
    tenant_id: str,
    framework_id: str,
    start: datetime,
    end: datetime,
    *,
    obligation_keys: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    from app.services.compliance import reports

    known = offering.any_catalog(framework_id)
    if known is None:
        return {"available": False, "framework_id": framework_id, "reason": "no catalog"}
    reason = offering.unavailable_reason(known)
    if reason is not None:
        return {"available": False, "framework_id": framework_id, "reason": reason}
    framework = known
    try:
        version = framework.version_on(end.date())
        catalog_version = version.catalog_version if version else None
        systems = reports.systems_in_scope(db, tenant_id, None)
        rows = reports.evaluate_rows(
            db, tenant_id, framework, systems, start, end, catalog_version=catalog_version,
            obligation_keys=list(obligation_keys) if obligation_keys is not None else None,
        )
        return {
            "available": True,
            "framework_id": framework_id,
            "catalog_version": catalog_version or framework.current_version,
            "verified_against_instrument": framework.verified_against_instrument,
            "provision_note": NOT_LEGAL_ADVICE,
            "summary": reports.summarize_rows(rows),
            "rows": rows,
        }
    except Exception as exc:  # noqa: BLE001 — the export itself must still answer
        log.warning("registry statements unavailable for %s/%s: %s", tenant_id, framework_id, exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"available": False, "framework_id": framework_id, "reason": exc.__class__.__name__}


__all__ = ["statements"]
