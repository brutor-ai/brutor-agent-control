# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Compliance framework export endpoints.

Six endpoints under `/v1/admin/compliance/`:

  * GET    /profile                      — read the tenant's
                                            compliance_profile
  * PATCH  /profile                      — update the profile
                                            (replaces frameworks)
  * GET    /dashboard                    — per-framework summary
                                            counts for the
                                            5-card admin UI
  * GET    /gdpr/article-30              — RoPA records (CSV/JSON)
  * GET    /eu-ai-act/risk-classification — per-RG tier + counts
  * GET    /soc2/control-coverage        — per-CC counts + sample
  * GET    /hipaa/phi-access             — PHI access log (CSV/JSON)
  * GET    /iso-42001/activity           — AI management system
                                            activity counts

All endpoints are tenant-scoped at the handler level — tenant
admins see only their tenant, system admins can override with
`?tenant_id=`. Read-only — operators edit the per-RG flags
(eu_ai_act_risk_tier, phi_resource) via the existing RG
endpoint.

Storage model: tag rows are populated by the Rust runtime
(`services::compliance::derive_tags` → `apply_compliance_tags`).
This router is pure-read except for the profile PATCH; it
queries the GIN-indexed `compliance_tags` JSONB array with
`@>` containment.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.v1.models.admin.compliance import (
    ComplianceProfile,
    EU_AI_ACT_RISK_TIERS,
    FRAMEWORK_KEYS,
)
from app.constants.admin import AdminType
from app.core.admin_user.permissions import Permission
from app.core.admin_user.dependencies import (
    require_permission,
    get_audit_logger,
    get_current_admin,
    require_system_admin,
)
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.database import get_db
from app.services import iso42001_service
from app.services.tenant.tenant_service import TenantService
from app.database.models.admin import TenantAdmin
from app.database.models.tenant import Tenant


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# One tenant-scoping rule for every admin route — see
# app/core/admin_user/dependencies.py::resolve_tenant_id. This was a
# five-line copy in twenty-five modules and had already drifted into two
# behaviours; the shared one refuses a mismatched ?tenant_id= rather than
# silently answering about a different tenant.
from app.core.admin_user.dependencies import resolve_tenant_id as _resolve_tenant_id
from app.middleware.trusted_proxies import client_ip


def _parse_window(
    from_s: Optional[str],
    to_s: Optional[str],
    default_days: int = 30,
) -> tuple[datetime, datetime]:
    """ISO-8601 window with a sane default of the last N days.
    Reports return a clear error rather than silently shifting if
    the operator passes an unparseable string."""
    try:
        if to_s:
            to_dt = datetime.fromisoformat(to_s.replace("Z", "+00:00"))
        else:
            to_dt = datetime.now(timezone.utc)
        if from_s:
            from_dt = datetime.fromisoformat(from_s.replace("Z", "+00:00"))
        else:
            from_dt = to_dt - timedelta(days=default_days)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Bad date format (expected ISO-8601): {e}",
        ) from e
    if from_dt >= to_dt:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="`from` must be earlier than `to`",
        )
    return from_dt, to_dt


def _count_rows_by_tag(
    db: Session,
    tenant_id: str,
    tag: str,
    from_dt: datetime,
    to_dt: datetime,
) -> int:
    """Containment query over the GIN-indexed compliance_tags
    column. Single tag → wrapped as a single-element JSONB array
    so the `@>` operator hits the index."""
    row = db.execute(
        text(
            "SELECT COUNT(*) FROM proxy_logs "
            "WHERE tenant_id = :tenant_id "
            "AND timestamp >= :from_dt AND timestamp < :to_dt "
            "AND compliance_tags @> CAST(:tag_json AS jsonb)"
        ),
        {
            "tenant_id": tenant_id,
            "from_dt": from_dt,
            "to_dt": to_dt,
            "tag_json": f'["{tag}"]',
        },
    ).first()
    return int(row[0]) if row else 0


#: The registry obligations each legacy export is re-pointed
#: at (`statements` block; None = the whole catalog).
EXPORT_OBLIGATIONS: Dict[str, Optional[List[str]]] = {
    "gdpr": ["art30-records-of-processing"],
    "eu-ai-act": ["art12-record-keeping", "art14-human-oversight", "art50-1-ai-interaction-notice"],
    "soc2": None,
    "hipaa": ["164-308-a1-activity-review", "164-312-b-audit-controls"],
    "iso-42001": None,
}


def _registry_statements(
    db: Session, tenant_id: str, framework_id: str, from_dt: datetime, to_dt: datetime, include: bool
) -> Dict[str, Any]:
    """The framework's catalog statements over the export window.
    Additive: the export's own fields are unchanged."""
    if not include:
        return {"available": False, "framework_id": framework_id, "reason": "not requested"}
    from app.services.compliance.framework_statements import statements

    return statements(
        db, tenant_id, framework_id, from_dt, to_dt,
        obligation_keys=EXPORT_OBLIGATIONS.get(framework_id),
    )


def _stream_csv(headers: List[str], rows: List[Dict[str, Any]], filename: str) -> StreamingResponse:
    """Stream a CSV with the given header list. Used by GDPR and
    HIPAA exports — auditors typically want CSV they can sample
    into a spreadsheet."""
    def _gen():
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        yield buf.getvalue()
        for row in rows:
            buf.seek(0)
            buf.truncate(0)
            writer.writerow(row)
            yield buf.getvalue()

    return StreamingResponse(
        _gen(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def create_compliance_admin_router(logger: logging.Logger) -> APIRouter:
    router = APIRouter(prefix="/admin/compliance", tags=["Compliance"])

    # -----------------------------------------------------------------
    # Profile read / write
    # -----------------------------------------------------------------

    @router.get("/profile", dependencies=[Depends(require_permission(Permission.COMPLIANCE_READ))])
    def get_profile(
        request: Request,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
    ):
        """Return the tenant's compliance_profile JSONB.

        `frameworks: []`, the state of a tenant that has declared none,
        switches the tagging of audit rows off.
        """
        admin_obj, admin_type = admin_and_type
        resolved_id = _resolve_tenant_id(admin_obj, admin_type, tenant_id)

        tenant = db.query(Tenant).filter(Tenant.id == resolved_id).first()
        if not tenant:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Tenant '{resolved_id}' not found",
            )
        profile = tenant.compliance_profile or {"frameworks": []}
        return {
            "tenant_id": resolved_id,
            "profile": profile,
            "known_frameworks": sorted(FRAMEWORK_KEYS),
        }

    @router.patch("/profile", dependencies=[Depends(require_permission(Permission.COMPLIANCE_MANAGE))])
    def update_profile(
        body: ComplianceProfile,
        request: Request,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        """Replace the tenant's compliance_profile with the body.

        `frameworks` is the full new list — this is a replace, not
        a merge. Pydantic validates every entry against
        FRAMEWORK_KEYS at deserialize time, so an unknown regime
        fails with 422 before reaching the DB.
        """
        admin_obj, admin_type = admin_and_type
        resolved_id = _resolve_tenant_id(admin_obj, admin_type, tenant_id)

        # Feature gate.
        TenantService.check_feature_access(db, resolved_id, 'compliance')

        tenant = db.query(Tenant).filter(Tenant.id == resolved_id).first()
        if not tenant:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Tenant '{resolved_id}' not found",
            )
        previous_frameworks = sorted(
            (tenant.compliance_profile or {}).get("frameworks", [])
        )
        # Pydantic already validated + deduped + lowercased. Store
        # as a plain dict so the JSONB column round-trips cleanly.
        # Replace `frameworks` only. The settings keep `registry_enabled`
        # (frameworks with no tagging key) alongside it, and a full
        # replace here would silently drop a tenant's DORA/NIS2 enablement
        # every time someone saved the tagging list.
        tenant.compliance_profile = {
            **(tenant.compliance_profile or {}),
            "frameworks": body.frameworks,
        }
        db.commit()
        audit.log(
            action="compliance_profile_update",
            db=db,
            admin=admin_obj,
            admin_type=admin_type,
            success=True,
            resource_type="compliance_profile",
            resource_id=str(resolved_id),
            tenant_id=resolved_id,
            affected_tenant_id=resolved_id,
            details={
                "previous_frameworks": previous_frameworks,
                "new_frameworks": sorted(body.frameworks),
            },
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_method=request.method,
            request_path=str(request.url.path),
        )
        return {
            "tenant_id": resolved_id,
            "profile": tenant.compliance_profile,
            "known_frameworks": sorted(FRAMEWORK_KEYS),
        }

    # -----------------------------------------------------------------
    # Dashboard summary — the 5-card view
    # -----------------------------------------------------------------

    @router.get("/dashboard", dependencies=[Depends(require_permission(Permission.COMPLIANCE_READ))])
    def get_dashboard(
        request: Request,
        tenant_id: Optional[str] = Query(None),
        from_: Optional[str] = Query(None, alias="from"),
        to: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
    ):
        """Per-framework summary counts for the admin UI's 5 cards.

        Each card carries: framework key, declared (is it on the
        tenant's profile?), count of tagged rows in window, and a
        breakdown by sub-tag (SOC 2 control numbers / EU AI Act
        tiers / HIPAA phi vs rg_marked).

        The dashboard is the operator's "is my compliance posture
        what I expect" view — surfacing both declared-but-quiet
        regimes and undeclared-but-tagged anomalies."""
        admin_obj, admin_type = admin_and_type
        resolved_id = _resolve_tenant_id(admin_obj, admin_type, tenant_id)
        from_dt, to_dt = _parse_window(from_, to, default_days=30)

        tenant = db.query(Tenant).filter(Tenant.id == resolved_id).first()
        if not tenant:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Tenant '{resolved_id}' not found",
            )
        declared = set((tenant.compliance_profile or {}).get("frameworks", []))

        # Per-framework top-level tag(s) the engine emits. Keep this
        # in sync with services/compliance/tagging.py — the drift
        # suite cross-checks both.
        framework_tag_map = {
            "gdpr_art_30": ["gdpr_art_30"],
            "soc2": [
                "soc2.cc6.1", "soc2.cc6.6", "soc2.cc6.7",
                "soc2.cc7.1", "soc2.cc7.2",
            ],
            "hipaa": ["hipaa.phi", "hipaa.rg_marked"],
            "eu_ai_act": [
                "eu_ai_act.minimal", "eu_ai_act.limited",
                "eu_ai_act.high", "eu_ai_act.unacceptable",
            ],
            "iso_42001": ["iso_42001"],
        }

        cards = []
        for fw in sorted(FRAMEWORK_KEYS):
            sub = {}
            total = 0
            for tag in framework_tag_map.get(fw, [fw]):
                c = _count_rows_by_tag(db, resolved_id, tag, from_dt, to_dt)
                sub[tag] = c
                total += c
            cards.append({
                "framework": fw,
                "declared": fw in declared,
                "total_tagged_rows": total,
                "by_tag": sub,
            })
        return {
            "tenant_id": resolved_id,
            "from": from_dt.isoformat(),
            "to": to_dt.isoformat(),
            "cards": cards,
        }

    # -----------------------------------------------------------------
    # GDPR Article 30 — Records of Processing Activities
    # -----------------------------------------------------------------

    @router.get("/gdpr/article-30", dependencies=[Depends(require_permission(Permission.COMPLIANCE_READ))])
    def gdpr_article_30(
        request: Request,
        tenant_id: Optional[str] = Query(None),
        from_: Optional[str] = Query(None, alias="from"),
        to: Optional[str] = Query(None),
        format: str = Query("json", pattern="^(json|csv)$"),
        limit: int = Query(10000, ge=1, le=100000),
        include_statements: bool = Query(True, description="Add the registry statements block"),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
    ):
        """GDPR Art 30 Records of Processing.

        Returns one row per data-subject-identifiable, PII-bearing
        call: gateway_user_id, idp_user_email, timestamp,
        processing purpose (LLM inference / MCP tool / Skill
        execution), proxy_type. Auditors typically sample this
        export.

        Both JSON and CSV are supported via `?format=`. CSV
        streams without buffering the whole result set."""
        admin_obj, admin_type = admin_and_type
        resolved_id = _resolve_tenant_id(admin_obj, admin_type, tenant_id)
        from_dt, to_dt = _parse_window(from_, to, default_days=30)

        rows = db.execute(
            text(
                "SELECT id, timestamp, proxy_type, proxy_subtype, "
                "gateway_user_id, idp_user_id, idp_user_email "
                "FROM proxy_logs "
                "WHERE tenant_id = :tenant_id "
                "AND timestamp >= :from_dt AND timestamp < :to_dt "
                "AND compliance_tags @> CAST('[\"gdpr_art_30\"]' AS jsonb) "
                "ORDER BY timestamp DESC LIMIT :limit"
            ),
            {
                "tenant_id": resolved_id,
                "from_dt": from_dt,
                "to_dt": to_dt,
                "limit": limit,
            },
        ).fetchall()

        records = [
            {
                "id": r[0],
                "timestamp": r[1].isoformat() if r[1] else None,
                "proxy_type": r[2],
                "proxy_subtype": r[3],
                "gateway_user_id": r[4],
                "idp_user_id": r[5],
                "idp_user_email": r[6],
            }
            for r in rows
        ]

        if format == "csv":
            return _stream_csv(
                headers=[
                    "id", "timestamp", "proxy_type", "proxy_subtype",
                    "gateway_user_id", "idp_user_id", "idp_user_email",
                ],
                rows=records,
                filename=f"gdpr-art30-{resolved_id}.csv",
            )
        return {
            "tenant_id": resolved_id,
            "from": from_dt.isoformat(),
            "to": to_dt.isoformat(),
            "count": len(records),
            "records": records,
            "statements": _registry_statements(
                db, resolved_id, "gdpr", from_dt, to_dt, include_statements
            ),
        }

    # -----------------------------------------------------------------
    # EU AI Act risk classification
    # -----------------------------------------------------------------

    @router.get("/eu-ai-act/risk-classification", dependencies=[Depends(require_permission(Permission.COMPLIANCE_READ))])
    def eu_ai_act_risk_classification(
        request: Request,
        tenant_id: Optional[str] = Query(None),
        from_: Optional[str] = Query(None, alias="from"),
        to: Optional[str] = Query(None),
        include_statements: bool = Query(True, description="Add the registry statements block"),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
    ):
        """Per-tier call count for the EU AI Act report.

        Lists every tier the engine emitted plus the per-RG
        classification map. The auditor wants both views: "how
        many calls hit each tier" and "which RGs map to which
        tier"."""
        admin_obj, admin_type = admin_and_type
        resolved_id = _resolve_tenant_id(admin_obj, admin_type, tenant_id)
        from_dt, to_dt = _parse_window(from_, to, default_days=30)

        by_tier: Dict[str, int] = {}
        for tier in sorted(EU_AI_ACT_RISK_TIERS):
            by_tier[tier] = _count_rows_by_tag(
                db, resolved_id, f"eu_ai_act.{tier}", from_dt, to_dt,
            )

        # Per-RG tier map — join via resource_group_id (audit
        # column on proxy_logs).
        rg_map_rows = db.execute(
            text(
                "SELECT id, name, eu_ai_act_risk_tier "
                "FROM resource_groups "
                "WHERE tenant_id = :tenant_id "
                "AND eu_ai_act_risk_tier IS NOT NULL"
            ),
            {"tenant_id": resolved_id},
        ).fetchall()
        rg_classifications = [
            {"id": r[0], "name": r[1], "tier": r[2]} for r in rg_map_rows
        ]

        return {
            "tenant_id": resolved_id,
            "from": from_dt.isoformat(),
            "to": to_dt.isoformat(),
            "calls_by_tier": by_tier,
            "rg_classifications": rg_classifications,
            "statements": _registry_statements(
                db, resolved_id, "eu-ai-act", from_dt, to_dt, include_statements
            ),
        }

    # -----------------------------------------------------------------
    # SOC 2 control coverage
    # -----------------------------------------------------------------

    @router.get("/soc2/control-coverage", dependencies=[Depends(require_permission(Permission.COMPLIANCE_READ))])
    def soc2_control_coverage(
        request: Request,
        tenant_id: Optional[str] = Query(None),
        from_: Optional[str] = Query(None, alias="from"),
        to: Optional[str] = Query(None),
        sample_size: int = Query(10, ge=0, le=100),
        include_statements: bool = Query(True, description="Add the registry statements block"),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
    ):
        """Per-CC evidence counts + sample row IDs.

        The auditor samples N row IDs per control and asks for
        full details on each — we return up to `sample_size` IDs
        per CC. The dashboard's "Export for auditor" button hits
        this with sample_size=50.

        Maps evidence to SOC 2 controls: auth events → CC6.1, guardrail blocks
        → CC7.2, etc."""
        admin_obj, admin_type = admin_and_type
        resolved_id = _resolve_tenant_id(admin_obj, admin_type, tenant_id)
        from_dt, to_dt = _parse_window(from_, to, default_days=30)

        controls = [
            "soc2.cc6.1",  # logical access controls
            "soc2.cc6.6",  # authorization
            "soc2.cc6.7",  # data movement (residency block)
            "soc2.cc7.1",  # capacity / rate-limit
            "soc2.cc7.2",  # system monitoring
        ]
        coverage = []
        for cc in controls:
            count = _count_rows_by_tag(db, resolved_id, cc, from_dt, to_dt)
            samples: List[str] = []
            if sample_size > 0 and count > 0:
                sample_rows = db.execute(
                    text(
                        "SELECT id FROM proxy_logs "
                        "WHERE tenant_id = :tenant_id "
                        "AND timestamp >= :from_dt AND timestamp < :to_dt "
                        "AND compliance_tags @> CAST(:tag_json AS jsonb) "
                        "ORDER BY timestamp DESC LIMIT :limit"
                    ),
                    {
                        "tenant_id": resolved_id,
                        "from_dt": from_dt,
                        "to_dt": to_dt,
                        "tag_json": f'["{cc}"]',
                        "limit": sample_size,
                    },
                ).fetchall()
                samples = [r[0] for r in sample_rows]
            coverage.append({
                "control": cc,
                "evidence_count": count,
                "sample_row_ids": samples,
            })
        return {
            "tenant_id": resolved_id,
            "from": from_dt.isoformat(),
            "to": to_dt.isoformat(),
            "controls": coverage,
            "statements": _registry_statements(
                db, resolved_id, "soc2", from_dt, to_dt, include_statements
            ),
        }

    # -----------------------------------------------------------------
    # HIPAA PHI access log
    # -----------------------------------------------------------------

    @router.get("/hipaa/phi-access", dependencies=[Depends(require_permission(Permission.COMPLIANCE_READ))])
    def hipaa_phi_access(
        request: Request,
        tenant_id: Optional[str] = Query(None),
        from_: Optional[str] = Query(None, alias="from"),
        to: Optional[str] = Query(None),
        format: str = Query("json", pattern="^(json|csv)$"),
        limit: int = Query(10000, ge=1, le=100000),
        include_statements: bool = Query(True, description="Add the registry statements block"),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
    ):
        """HIPAA PHI access log.

        Returns every call carrying `hipaa.phi` OR `hipaa.rg_marked`.
        The two sources are kept distinct in the export so the
        auditor sees both "we caught PHI on the wire" and
        "operator pre-declared the RG"."""
        admin_obj, admin_type = admin_and_type
        resolved_id = _resolve_tenant_id(admin_obj, admin_type, tenant_id)
        from_dt, to_dt = _parse_window(from_, to, default_days=30)

        rows = db.execute(
            text(
                "SELECT id, timestamp, proxy_type, gateway_user_id, "
                "idp_user_id, idp_user_email, resource_group_id, "
                "compliance_tags "
                "FROM proxy_logs "
                "WHERE tenant_id = :tenant_id "
                "AND timestamp >= :from_dt AND timestamp < :to_dt "
                "AND (compliance_tags @> CAST('[\"hipaa.phi\"]' AS jsonb) "
                "  OR compliance_tags @> CAST('[\"hipaa.rg_marked\"]' AS jsonb)) "
                "ORDER BY timestamp DESC LIMIT :limit"
            ),
            {
                "tenant_id": resolved_id,
                "from_dt": from_dt,
                "to_dt": to_dt,
                "limit": limit,
            },
        ).fetchall()

        records = []
        for r in rows:
            tags = r[7] or []
            records.append({
                "id": r[0],
                "timestamp": r[1].isoformat() if r[1] else None,
                "proxy_type": r[2],
                "gateway_user_id": r[3],
                "idp_user_id": r[4],
                "idp_user_email": r[5],
                "resource_group_id": r[6],
                "source": (
                    "detected" if "hipaa.phi" in tags else "rg_marked"
                ),
            })

        if format == "csv":
            return _stream_csv(
                headers=[
                    "id", "timestamp", "proxy_type",
                    "gateway_user_id", "idp_user_id", "idp_user_email",
                    "resource_group_id", "source",
                ],
                rows=records,
                filename=f"hipaa-phi-access-{resolved_id}.csv",
            )
        return {
            "tenant_id": resolved_id,
            "from": from_dt.isoformat(),
            "to": to_dt.isoformat(),
            "count": len(records),
            "records": records,
            "statements": _registry_statements(
                db, resolved_id, "hipaa", from_dt, to_dt, include_statements
            ),
        }

    # -----------------------------------------------------------------
    # ISO 42001 — AI management system activity
    # -----------------------------------------------------------------

    @router.get("/iso-42001/activity", dependencies=[Depends(require_permission(Permission.COMPLIANCE_READ))])
    def iso_42001_activity(
        request: Request,
        tenant_id: Optional[str] = Query(None),
        from_: Optional[str] = Query(None, alias="from"),
        to: Optional[str] = Query(None),
        include_statements: bool = Query(True, description="Add the registry statements block"),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
    ):
        """ISO 42001 activity counts.

        The standard requires logging the AI management system's
        activity, not just events. Counts every tagged row in the
        window plus a per-proxy-type breakdown for the auditor.
        """
        admin_obj, admin_type = admin_and_type
        resolved_id = _resolve_tenant_id(admin_obj, admin_type, tenant_id)
        from_dt, to_dt = _parse_window(from_, to, default_days=30)

        total = _count_rows_by_tag(
            db, resolved_id, "iso_42001", from_dt, to_dt,
        )

        breakdown_rows = db.execute(
            text(
                "SELECT proxy_type, COUNT(*) FROM proxy_logs "
                "WHERE tenant_id = :tenant_id "
                "AND timestamp >= :from_dt AND timestamp < :to_dt "
                "AND compliance_tags @> CAST('[\"iso_42001\"]' AS jsonb) "
                "GROUP BY proxy_type"
            ),
            {
                "tenant_id": resolved_id,
                "from_dt": from_dt,
                "to_dt": to_dt,
            },
        ).fetchall()
        by_proxy_type = {row[0]: int(row[1]) for row in breakdown_rows}

        return {
            "tenant_id": resolved_id,
            "from": from_dt.isoformat(),
            "to": to_dt.isoformat(),
            "total_activity_rows": total,
            "by_proxy_type": by_proxy_type,
            # The ISO 42001 Annex A statements.
            "statements": iso42001_service.annex_a_statements(
                db, resolved_id, from_dt, to_dt, include=include_statements
            ),
        }

    return router
