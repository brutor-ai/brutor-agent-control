# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Audit-chain verification.

    GET  /v1/admin/audit/chains                  — chains present in proxy_logs
    POST /v1/admin/audit/chains/{chain_id}/verify — walk a range, return a verdict

## Why this proxies to the Rust core rather than verifying here

Verification recomputes the canonical row bytes that
`gateway-core/src/services/audit_chain.rs` hashes. A second implementation of
that format in Python would eventually drift from the Rust one — and the day
it did, honest rows would start failing verification with no way to tell that
apart from a real tamper. So the check runs where the format is defined, and
this module is a thin, authenticated pass-through over the same
``BRUTOR_GATEWAY_INTERNAL_URL`` path the admin API already uses for model health checks
and cache invalidation.

There is a tempting shortcut worth naming so nobody takes it later: the
control plane *could* check sequence gaps and checkpoint signatures on its own,
because those formats are simple and stable. It could not check row hashes.
A partial check that reported "verified" would be worse than no check at all.

## Why this is system-admin only

A hash chain belongs to a *writer process*, and one chain interleaves rows from
every tenant that process served. A tenant admin verifying a chain would learn
its sequence ranges, and therefore the traffic volume of everyone else on the
same proxy. The report itself carries no row contents — only counts, sequence
numbers and verdicts — but the ranges alone are enough, so the permission gate
is the control.

## Failure to check is not a clean bill of health

A proxy that is unreachable returns 503 here, never an empty or optimistic
report. The one thing this endpoint must never do is let an outage read as
"nothing wrong".
"""

from typing import Optional, Union

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.constants.proxy import proxy_internal_url, gateway_service_api_key
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.database.models.admin import TenantAdmin
from app.core.admin_user.admin_principal import AdminPrincipal
from app.constants.tenant import TenantResourceTypes
from app.core.admin_user.dependencies import (
    require_permission,
    require_tenant_resource_access,
)

#: Verification walks up to 10k rows and hashes each one. The proxy caps the
#: scan itself; this is the HTTP patience for it, and it is deliberately longer
#: than the 3s used for cache invalidation — that call does almost nothing,
#: this one does real work.
VERIFY_TIMEOUT_SECONDS = 30.0
LIST_TIMEOUT_SECONDS = 5.0


def _proxy_base() -> str:
    return proxy_internal_url()


def _proxy_headers(tenant_id: Optional[str]) -> dict:
    headers = {"Content-Type": "application/json"}
    if tenant_id:
        headers["X-Tenant-ID"] = tenant_id
    key = gateway_service_api_key()
    if key:
        # The core accepts either; both are sent to match the existing
        # internal-call convention in llm_models.py.
        headers["Authorization"] = f"Bearer {key}"
        headers["X-API-Key"] = key
    return headers


def create_audit_chain_router(logger: BrutorLogger) -> APIRouter:
    router = APIRouter(prefix="/admin/audit", tags=["Audit Chain"])

    @router.get(
        "/chains",
        summary="List audit hash chains",
        description=(
            "Chains present in `proxy_logs`. A chain corresponds to one proxy "
            "process; a restart begins a new one, which is expected and not a "
            "fault. Requires the `system:read` permission."
        ),
    )
    def list_chains(
        request: Request,
        limit: int = Query(100, ge=1, le=1000),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.SYSTEM_OPS)
        ),
        db: Session = Depends(get_db),
        _perm: AdminPrincipal = Depends(
            require_permission(Permission.SYSTEM_READ)
        ),
    ) -> dict:
        _admin, tenant_id, _admin_type = admin_and_tenant
        url = f"{_proxy_base()}/v1/audit/chains"
        try:
            with httpx.Client(timeout=LIST_TIMEOUT_SECONDS) as client:
                resp = client.get(
                    url, headers=_proxy_headers(tenant_id), params={"limit": limit}
                )
                resp.raise_for_status()
                return resp.json()
        except Exception as exc:
            logger.warning(f"Audit chain listing failed: {exc}")
            raise HTTPException(
                status_code=503,
                detail=(
                    "Could not reach the proxy to list audit chains. This is not "
                    "a statement about the integrity of the records."
                ),
            )

    @router.post(
        "/chains/{chain_id}/verify",
        summary="Verify an audit hash chain",
        description=(
            "Recomputes each row's hash and checks it against its stored value "
            "and its predecessor, reports sequence gaps, and validates any "
            "signed checkpoints covering the range.\n\n"
            "Verdicts: `verified` (hashes link **and** a valid checkpoint "
            "covers them — the only verdict asserting integrity), `unattested` "
            "(hashes link, nothing signed them), `gap` (rows were deleted), "
            "`broken` (a hash does not match), `unchained` (rows predate the "
            "chain — not a pass and not a failure).\n\n"
            "`truncated: true` means the scan stopped at the limit, so the "
            "verdict describes the range examined and not the whole chain. "
            "Requires the `system:read` permission."
        ),
    )
    def verify_chain(
        request: Request,
        chain_id: str,
        from_seq: int = Query(0, ge=0),
        limit: int = Query(10000, ge=1, le=10000),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.SYSTEM_OPS)
        ),
        db: Session = Depends(get_db),
        _perm: AdminPrincipal = Depends(
            require_permission(Permission.SYSTEM_READ)
        ),
    ) -> dict:
        _admin, tenant_id, _admin_type = admin_and_tenant
        url = f"{_proxy_base()}/v1/audit/chains/{chain_id}/verify"
        try:
            with httpx.Client(timeout=VERIFY_TIMEOUT_SECONDS) as client:
                resp = client.post(
                    url,
                    headers=_proxy_headers(tenant_id),
                    params={"from_seq": from_seq, "limit": limit},
                )
                resp.raise_for_status()
                report = resp.json()
        except Exception as exc:
            logger.warning(f"Audit chain verification failed for {chain_id}: {exc}")
            raise HTTPException(
                status_code=503,
                detail=(
                    "Could not reach the proxy to verify this chain. This is not "
                    "a statement about the integrity of the records — no check ran."
                ),
            )

        # A non-clean verdict is an operator event, not a page detail. Log it
        # here too so it lands in the control plane's own audit trail rather
        # than only in the proxy's.
        if report.get("verdict") not in ("verified",):
            logger.warning(
                f"Audit chain {chain_id} verdict={report.get('verdict')} "
                f"rows={report.get('rows_checked')} "
                f"first_bad_seq={report.get('first_bad_seq')} "
                f"gaps={len(report.get('gaps') or [])}"
            )
        return report

    return router
