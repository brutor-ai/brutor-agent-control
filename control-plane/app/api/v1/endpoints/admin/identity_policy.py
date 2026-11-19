# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Identity policy routes (Community Edition).

* `GET|PUT /admin/resource-groups/{group_id}/identity-policy`: a group's
  explicit identity policy, its effective policy and where each value comes
  from. A write that relaxes what the group inherits is refused with every
  such field named.
* `GET /admin/ai-systems/{ai_system_id}/identity/summary` and
  `/identity/credentials`: the credentials the core recorded on the calls of
  an AI System's subtree, aggregated in SQL.

The resolution itself is `app/services/identity_policy.py`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.v1.models.admin.identity_policy import (
    IdentityCredentialsResponse,
    IdentityPolicyPut,
    IdentityPolicyResponse,
    IdentitySummaryResponse,
)
from app.constants.identity import CREDENTIAL_LEVELS, CREDENTIAL_POLICY_OUTCOMES
from app.core.admin_user.dependencies import (
    acting_tenant_id,
    get_audit_logger,
    get_current_admin,
    require_permission,
)
from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.core.logging_system import BrutorLogger
from app.core.sync_bridge import block_on
from app.database import get_db
from app.database.models.resource_group import ResourceGroup
from app.middleware.trusted_proxies import client_ip
from app.services import identity_policy as ip
from app.services.identity_policy import SUBTREE_CTE, since_clause
from app.services.cache_invalidation import invalidate_proxy_cache

AUDIT_ACTION = "resource_group.identity_policy.update"

def identity_policy_response(group: ResourceGroup, resolution: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "group_id": group.id,
        "group_name": group.name,
        "is_ai_system": ip.is_ai_system(group),
        "lifecycle_stage": group.lifecycle_stage,
        "eu_ai_act_risk_tier": group.eu_ai_act_risk_tier,
        "attributed_to_ai_system": resolution["attributed_to_ai_system"],
        "policy": group.identity_policy,
        "effective": resolution["effective"],
        "inherited_floor": resolution["inherited_floor"],
        "sources": resolution["sources"],
        "max_user_key_ttl_days": resolution["max_user_key_ttl_days"],
    }


def refuse_unknown_issuers(db: Session, tenant_id: str, issuers) -> None:
    unknown = ip.unknown_issuers(db, tenant_id, issuers)
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": "allowed_issuers names identity providers that are not OIDC providers of this tenant",
                "code": "identity_policy_unknown_issuer",
                "issuers": unknown,
            },
        )


def create_identity_policy_router(logger: BrutorLogger) -> APIRouter:
    router = APIRouter(tags=["Identity Policy"])

    def _managing_tenant(
        request: Request, admin=Depends(require_permission(Permission.IDENTITY_POLICY_MANAGE))
    ) -> str:
        """The tenant a write acts on, resolved before the body is read: a
        caller that acts on no tenant is refused before anything else."""
        return acting_tenant_id(request, admin)

    def _group(db: Session, tenant_id: str, group_id: str) -> ResourceGroup:
        group = (
            db.query(ResourceGroup)
            .filter(ResourceGroup.id == group_id, ResourceGroup.tenant_id == tenant_id)
            .first()
        )
        if group is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Resource group '{group_id}' not found")
        return group

    def _system(db: Session, tenant_id: str, ai_system_id: str) -> ResourceGroup:
        group = _group(db, tenant_id, ai_system_id)
        if not ip.is_ai_system(group):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"AI System '{ai_system_id}' not found")
        return group

    # ------------------------------------------------------------------
    # The policy of a group
    # ------------------------------------------------------------------

    @router.get(
        "/admin/resource-groups/{group_id}/identity-policy",
        response_model=IdentityPolicyResponse,
        summary="Get a group's identity policy",
        description=(
            "The group's explicit identity policy (or null), its effective policy, the floor it "
            "inherits (the policy without its own), and where each effective value comes from."
        ),
    )
    def get_identity_policy(
        group_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin=Depends(require_permission(Permission.IDENTITY_POLICY_READ)),
    ):
        group = _group(db, acting_tenant_id(request, admin), group_id)
        return identity_policy_response(group, ip.resolve_group(db, group))

    @router.put(
        "/admin/resource-groups/{group_id}/identity-policy",
        response_model=IdentityPolicyResponse,
        summary="Set a group's identity policy",
        description=(
            "Stores the group's explicit identity policy (`{\"policy\": null}` clears it). A policy "
            "looser than what the group inherits in any field is refused with 422 "
            "`identity_policy_below_floor`, naming every such field, the floor and its source."
        ),
    )
    def put_identity_policy(
        group_id: str,
        body: IdentityPolicyPut,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin=Depends(require_permission(Permission.IDENTITY_POLICY_MANAGE)),
        tenant_id: str = Depends(_managing_tenant),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        _, admin_type = admin_and_type
        group = _group(db, tenant_id, group_id)
        new = body.policy.model_dump() if body.policy is not None else None
        if new is not None:
            refuse_unknown_issuers(db, tenant_id, new["allowed_issuers"])

            groups = ip.group_chain(group)
            names = {g.id: g.name for g in groups}
            nodes = ip.chain_nodes(groups)
            nodes[-1]["identity_policy"] = None
            floor = ip.resolve(tenant_default=ip.tenant_default_of(db, tenant_id), chain=nodes)
            refused = ip.looser_fields(new, floor["effective"])
            if refused:
                sources = ip.named_sources(floor["sources"], names)
                problems = [
                    {
                        "field": field,
                        "requested": new[field],
                        "floor": floor["effective"][field],
                        "source": sources[field],
                    }
                    for field in refused
                ]
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={
                        "message": "The policy relaxes what this group inherits: " + ", ".join(refused),
                        "code": "identity_policy_below_floor",
                        "problems": problems,
                    },
                )

        before = group.identity_policy
        group.identity_policy = new
        db.flush()

        block_on(invalidate_proxy_cache(request, "resource_group", tenant_id, group.id, action="update"))
        audit.log(
            action=AUDIT_ACTION,
            admin=admin,
            admin_type=admin_type,
            tenant_id=tenant_id,
            success=True,
            resource_type="resource_group",
            resource_id=group.id,
            details={"group_name": group.name, "before": before, "after": new},
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_method=request.method,
            request_path=str(request.url.path),
            db=db,
        )
        logger.info("Identity policy updated", context={"group_id": group.id, "cleared": new is None})
        return identity_policy_response(group, ip.resolve_group(db, group))

    # ------------------------------------------------------------------
    # What reached an AI System
    # ------------------------------------------------------------------

    @router.get(
        "/admin/ai-systems/{ai_system_id}/identity/summary",
        response_model=IdentitySummaryResponse,
        summary="Credential assurance of an AI System's calls",
        description=(
            "Calls of the system's subtree in the last `days` days by credential level and by the "
            "identity policy's verdict, its runs by their lowest credential level, and the number "
            "of distinct credentials."
        ),
    )
    def identity_summary(
        ai_system_id: str,
        request: Request,
        days: int = Query(7, ge=1, le=90),
        db: Session = Depends(get_db),
        admin=Depends(require_permission(Permission.IDENTITY_POLICY_READ)),
    ):
        tenant_id = acting_tenant_id(request, admin)
        system = _system(db, tenant_id, ai_system_id)
        params: Dict[str, Any] = {"tenant_id": tenant_id, "ai_system_id": system.id}
        window, extra = since_clause(db, "pl.timestamp", days)
        params.update(extra)
        calls = db.execute(
            text(
                SUBTREE_CTE
                + f"""
                SELECT pl.credential_assurance, pl.credential_policy_outcome,
                       COUNT(*) AS calls
                  FROM proxy_logs pl
                 WHERE pl.tenant_id = :tenant_id
                   AND pl.resource_group_id IN (SELECT id FROM subtree)
                   AND {window}
                   AND pl.credential_kind IS NOT NULL
                 GROUP BY pl.credential_assurance, pl.credential_policy_outcome
                """
            ),
            params,
        ).fetchall()
        by_level = {level: 0 for level in CREDENTIAL_LEVELS}
        by_level["outside_ladder"] = 0
        outcomes = {o: 0 for o in CREDENTIAL_POLICY_OUTCOMES}
        for level, outcome, n in calls:
            by_level[level if level in by_level else "outside_ladder"] += n
            if outcome in outcomes:
                outcomes[outcome] += n

        distinct = db.execute(
            text(
                SUBTREE_CTE
                + f"""
                SELECT COUNT(DISTINCT pl.credential_ref)
                  FROM proxy_logs pl
                 WHERE pl.tenant_id = :tenant_id
                   AND pl.resource_group_id IN (SELECT id FROM subtree)
                   AND {window}
                """
            ),
            params,
        ).scalar()

        run_window, run_extra = since_clause(db, "started_at", days)
        runs = db.execute(
            text(
                f"""
                SELECT credential_assurance_min, COUNT(*)
                  FROM ai_system_runs
                 WHERE tenant_id = :tenant_id AND ai_system_id = :ai_system_id AND {run_window}
                 GROUP BY credential_assurance_min
                """
            ),
            {"tenant_id": tenant_id, "ai_system_id": system.id, **run_extra},
        ).fetchall()
        by_run = {level: 0 for level in CREDENTIAL_LEVELS}
        by_run["none"] = 0
        for level, n in runs:
            by_run[level if level in by_run else "none"] += n

        return {
            "ai_system_id": system.id,
            "window_days": days,
            "effective": ip.resolve_group(db, system)["effective"],
            "calls_by_level": by_level,
            "outcomes": outcomes,
            "runs_by_min_level": by_run,
            "credentials": int(distinct or 0),
        }

    @router.get(
        "/admin/ai-systems/{ai_system_id}/identity/credentials",
        response_model=IdentityCredentialsResponse,
        summary="Credentials that reached an AI System",
        description=(
            "One item per credential that made calls in the system's subtree in the last `days` "
            "days, newest first: calls, calls below the policy and refused, first and last seen, "
            "and the key or agent behind it."
        ),
    )
    def identity_credentials(
        ai_system_id: str,
        request: Request,
        days: int = Query(30, ge=1, le=90),
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
        db: Session = Depends(get_db),
        admin=Depends(require_permission(Permission.IDENTITY_POLICY_READ)),
    ):
        tenant_id = acting_tenant_id(request, admin)
        system = _system(db, tenant_id, ai_system_id)
        window, extra = since_clause(db, "pl.timestamp", days)
        params: Dict[str, Any] = {
            "tenant_id": tenant_id,
            "ai_system_id": system.id,
            "limit": limit,
            "offset": offset,
            **extra,
        }
        scoped = f"""
            SELECT pl.credential_ref, pl.credential_kind, pl.credential_assurance,
                   pl.credential_policy_outcome, pl.timestamp, pl.api_key_id, pl.api_key_name,
                   pl.actor_agent_id,
                   ROW_NUMBER() OVER (
                       PARTITION BY pl.credential_ref, pl.credential_kind, pl.credential_assurance
                       ORDER BY pl.timestamp DESC
                   ) AS rn
              FROM proxy_logs pl
             WHERE pl.tenant_id = :tenant_id
               AND pl.resource_group_id IN (SELECT id FROM subtree)
               AND {window}
               AND pl.credential_kind IS NOT NULL
        """
        total = db.execute(
            text(
                SUBTREE_CTE
                + f""", scoped AS ({scoped})
                SELECT COUNT(*) FROM (
                    SELECT 1 FROM scoped
                     GROUP BY credential_ref, credential_kind, credential_assurance
                ) AS credentials
                """
            ),
            params,
        ).scalar()
        rows = db.execute(
            text(
                SUBTREE_CTE
                + f""", scoped AS ({scoped})
                SELECT credential_ref, credential_kind, credential_assurance,
                       COUNT(*) AS calls,
                       SUM(CASE WHEN credential_policy_outcome = 'below_policy' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN credential_policy_outcome = 'refused' THEN 1 ELSE 0 END),
                       MIN(timestamp) AS first_seen,
                       MAX(timestamp) AS last_seen,
                       MAX(CASE WHEN rn = 1 THEN api_key_id END),
                       MAX(CASE WHEN rn = 1 THEN api_key_name END),
                       MAX(CASE WHEN rn = 1 THEN actor_agent_id END)
                  FROM scoped
                 GROUP BY credential_ref, credential_kind, credential_assurance
                 ORDER BY last_seen DESC, credential_ref
                 LIMIT :limit OFFSET :offset
                """
            ),
            params,
        ).fetchall()
        items: List[Dict[str, Any]] = [
            {
                "credential_ref": r[0],
                "credential_kind": r[1],
                "credential_assurance": r[2],
                "calls": int(r[3]),
                "below_policy_calls": int(r[4] or 0),
                "refused_calls": int(r[5] or 0),
                "first_seen": r[6],
                "last_seen": r[7],
                "api_key_id": r[8],
                "api_key_name": r[9],
                "agent_id": r[10],
            }
            for r in rows
        ]
        return {"items": items, "total": int(total or 0)}

    return router


__all__ = ["create_identity_policy_router", "refuse_unknown_issuers"]
