# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Evidence Ledger API

    GET    /admin/evidence/records                    — sealed records, filtered + paged
    GET    /admin/evidence/records/{record_id}        — one record with its statement
    GET    /admin/evidence/records/{record_id}/proof  — RFC 6962 inclusion proof
    GET    /admin/evidence/summary                    — verdict / kind / surface counts + log status
    GET    /admin/evidence/logs                       — tenant logs
    GET    /admin/evidence/logs/{log_id}/tree-heads   — heads with receipts
    GET    /admin/evidence/tree-heads/{id}            — one head with receipts
    GET    /admin/evidence/witnesses                  — platform + this tenant's witnesses
    POST   /admin/evidence/witnesses                  — add one
    PATCH  /admin/evidence/witnesses/{id}             — enable / relabel / pin a key
    DELETE /admin/evidence/witnesses/{id}             — remove one
    GET    /admin/evidence/disclosures                — disclosure records (read)
    POST   /admin/evidence/verify                     — verify a record via the core
    GET    /admin/evidence/settings                   — per-tenant evidence switches
    PUT    /admin/evidence/settings                   — change them
    POST   /admin/evidence/keys/rotate                — create / rotate the tenant's own key (core)
    GET    /admin/evidence/keys                       — key ids (deployment + this tenant), active / retired
    POST   /admin/evidence/bundles                    — build an evidence bundle (core), e.g. one run
    POST   /admin/evidence/bundles/verify             — verify a bundle (core)

Records, logs, heads and receipts are written by the Rust core; this API
only reads them. Witnesses are configuration and are written here.

The inclusion proof is computed from the stored node set for the log's
current tree size (or an earlier `?tree_size=`), never from re-reading
statements: a proof is a claim about the tree, and the tree is the nodes.
Verification of the record itself — id recomputation, signature, the
semantic rules — is delegated to the core, which holds the key and the
vectors; an unreachable core is reported as a finding, never as a pass.
"""

from __future__ import annotations

import base64
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func
from sqlalchemy.orm import Session
from ulid import ULID

from app.constants.admin import AdminType
from app.core.admin_user.dependencies import (
    get_audit_logger,
    get_current_admin,
    is_platform_wide,
    may_act_on_tenant,
    require_permission,
    tenant_scope,
)
from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.database.models.admin import TenantAdmin
from app.database.models.action_record import RECORD_KINDS, RECORD_VERDICTS, ActionRecord
from app.database.models.disclosure_record import DisclosureRecord
from app.database.models.evidence_settings import EvidenceSettings
from app.database.models.evidence_signing_key import EvidenceSigningKey
from app.database.models.evidence_log import (
    GRADE_STANDALONE,
    WITNESS_KINDS,
    EvidenceLog,
    EvidenceLogNode,
    EvidenceReceipt,
    EvidenceTreeHead,
)
from app.database.models.resource_mixins import actor_name
from app.database.models.witness import (
    SCOPE_PLATFORM,
    SCOPE_TENANT,
    URL_SCHEMES,
    WITNESS_SCOPES,
    Witness,
)
from app.services.evidence import core_client, merkle


# One tenant-scoping rule for every admin route — see
# app/core/admin_user/dependencies.py::resolve_tenant_id. This was a
# five-line copy in twenty-five modules and had already drifted into two
# behaviours; the shared one refuses a mismatched ?tenant_id= rather than
# silently answering about a different tenant.
from app.core.admin_user.dependencies import resolve_tenant_id as _resolve_tenant_id
from app.middleware.trusted_proxies import client_ip


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class RecordSummary(BaseModel):
    record_id: str
    log_id: str
    seq: int
    ai_system_id: Optional[str] = None
    root_task_id: Optional[str] = None
    proxy_log_id: Optional[str] = None
    verdict: str
    kind: str
    surface: str
    epoch: Optional[str] = None
    timestamp: Optional[str] = None
    tags: Optional[Any] = None
    action_id: Optional[str] = None
    effect_type: Optional[str] = None
    effect_status: Optional[str] = None
    effect_mode: Optional[str] = Field(
        None,
        description="Derived effect mode (not_applicable | confirmed | dispatched_unconfirmed); null when the body was pruned",
    )
    human_disposed: Optional[bool] = None
    chain: Optional[Dict[str, Any]] = None


class RecordListResponse(BaseModel):
    items: List[RecordSummary]
    total: int
    limit: int
    offset: int


class RecordLogRef(BaseModel):
    log_id: str
    seq: int
    tree_size_now: int


class RecordDetail(RecordSummary):
    record: Dict[str, Any]
    statement_b64: Optional[str] = None
    kid: str
    created_at: Optional[str] = None
    log: RecordLogRef


class InclusionProof(BaseModel):
    record_id: str
    log_id: str
    leaf_index: int
    tree_size: int
    root: str
    path: List[str]
    leaf_hash: str


class TreeHeadRef(BaseModel):
    id: str
    tree_size: int
    issued_at: Optional[str] = None
    grade: str


class LogStatus(BaseModel):
    log_id: str
    tree_size: int
    root: Optional[str] = None
    last_tree_head: Optional[TreeHeadRef] = None
    unwitnessed_heads: int = 0


class EvidenceSummary(BaseModel):
    records_total: int
    by_verdict: Dict[str, int]
    by_kind: Dict[str, int]
    by_surface: Dict[str, int]
    human_disposed_count: int
    systems_covered: int
    log: Optional[LogStatus] = None


class LogListItem(BaseModel):
    log_id: str
    tenant_id: str
    tree_size: int
    root: Optional[str] = None
    kid: Optional[str] = None
    last_tree_head: Optional[TreeHeadRef] = None
    heads_count: int
    receipts_count: int
    latest_grade: Optional[str] = None


class LogListResponse(BaseModel):
    items: List[LogListItem]
    total: int


class ReceiptOut(BaseModel):
    id: str
    witness_id: Optional[str] = None
    kind: str
    verdict: str
    verified_at: Optional[str] = None
    leaf_index: Optional[int] = None
    tree_size: Optional[int] = None
    witness_iat: Optional[int] = None
    error: Optional[str] = None


class TreeHeadOut(BaseModel):
    id: str
    log_id: str
    tenant_id: str
    tree_size: int
    root: str
    prev_size: int
    prev_root: Optional[str] = None
    issued_at: Optional[str] = None
    stamp_seq: Optional[int] = None
    grade: str
    statement_b64: Optional[str] = None
    transparent_statement_b64: Optional[str] = None
    created_at: Optional[str] = None
    receipts: List[ReceiptOut] = Field(default_factory=list)


class TreeHeadListResponse(BaseModel):
    items: List[TreeHeadOut]
    total: int
    limit: int
    offset: int


#: `witnesses.pinned_key` is TEXT so an rfc3161 pin can be the
#: TSA certificate PEM (a fingerprint or scitt hex key is far shorter). The
#: bound only stops an accidental paste of a whole chain file.
PINNED_KEY_MAX_LEN = 16384


class WitnessOut(BaseModel):
    id: str
    scope: str
    tenant_id: Optional[str] = None
    kind: str
    url: str
    operator_label: str
    pinned_key: Optional[str] = None
    enabled: bool
    same_operator: bool
    created_by: Optional[str] = None
    created_at: Optional[str] = None
    updated_by: Optional[str] = None
    updated_at: Optional[str] = None


class WitnessListResponse(BaseModel):
    items: List[WitnessOut]
    total: int


class WitnessCreateRequest(BaseModel):
    scope: str = Field(SCOPE_TENANT, description="platform | tenant")
    kind: str = Field(..., description="scitt | rfc3161")
    url: str = Field(..., min_length=1, max_length=4000)
    operator_label: str = Field(..., min_length=1, max_length=255)
    pinned_key: Optional[str] = Field(None, max_length=PINNED_KEY_MAX_LEN)
    enabled: bool = True
    same_operator: bool = False


class WitnessUpdateRequest(BaseModel):

    model_config = ConfigDict(extra="forbid")
    url: Optional[str] = Field(None, min_length=1, max_length=4000)
    operator_label: Optional[str] = Field(None, min_length=1, max_length=255)
    pinned_key: Optional[str] = Field(None, max_length=PINNED_KEY_MAX_LEN)
    enabled: Optional[bool] = None
    same_operator: Optional[bool] = None


class DisclosureOut(BaseModel):
    id: str
    tenant_id: str
    log_id: str
    seq: Optional[int] = None
    audience: str
    selector: Dict[str, Any]
    completeness: str
    disclosed_record_ids: List[str]
    disclosed_count: int
    suppressed_fields: List[str]
    bundle_digest: Optional[str] = None
    statement_b64: Optional[str] = None
    created_at: Optional[str] = None
    created_by: Optional[str] = None


class DisclosureListResponse(BaseModel):
    items: List[DisclosureOut]
    total: int
    limit: int
    offset: int


class VerifyRequest(BaseModel):
    record_id: Optional[str] = Field(
        None, description="Verify a stored record by id (tenant-scoped)"
    )
    record: Optional[Dict[str, Any]] = Field(
        None, description="Or verify a record supplied inline"
    )
    statement_b64: Optional[str] = Field(
        None, description="The record's Signed Statement, base64 (inline mode)"
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class EvidenceSettingsOut(BaseModel):
    tenant_id: str
    per_tenant_key: bool
    salt_digests_default: bool
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None


class EvidenceSettingsUpdate(BaseModel):
    """Omitted fields are left unchanged."""

    model_config = ConfigDict(extra="forbid")

    per_tenant_key: Optional[bool] = None
    salt_digests_default: Optional[bool] = None


class KeyRotationOut(BaseModel):
    """The core's rotation answer. Its members are the core's to extend, so
    unknown ones pass through instead of being stripped."""

    model_config = ConfigDict(extra="allow")

    kid: Optional[str] = None
    previous_kid: Optional[str] = None
    tenant_id: Optional[str] = None
    rotation_record_id: Optional[str] = None
    created_at: Optional[str] = None


class SigningKeyOut(BaseModel):
    kid: str
    scope: str = Field(..., description="deployment | tenant")
    status: str = Field(..., description="active | retired")
    created_at: Optional[str] = None
    retired_at: Optional[str] = None
    rotation_record_id: Optional[str] = None


class SigningKeyListResponse(BaseModel):
    """Public key metadata only — the seed never leaves the core. The
    deployment key configured in the core's environment has no row; the
    tenant log's `kid` names the key its records are signed under now."""

    items: List[SigningKeyOut]
    total: int
    log_kid: Optional[str] = None
    per_tenant_key: bool = False


class BundleSelector(BaseModel):
    """Exactly one of: `record_ids`, `root_task_id`, or `ai_system_id`
    with a `from`/`to` window."""

    model_config = ConfigDict(populate_by_name=True)

    record_ids: Optional[List[str]] = Field(None, max_length=10_000)
    root_task_id: Optional[str] = Field(None, min_length=1, max_length=255)
    ai_system_id: Optional[str] = Field(None, min_length=1, max_length=255)
    from_: Optional[datetime] = Field(None, alias="from")
    to: Optional[datetime] = None


class BundleBuildRequest(BaseModel):
    selector: BundleSelector
    audience: Optional[str] = Field(
        None, max_length=255, description="Non-null seals a disclosure record naming the audience (as a digest)"
    )
    closure_depth: int = Field(1, ge=0, le=8)
    framework_id: Optional[str] = Field(None, max_length=64)


class BundleVerifyRequest(BaseModel):
    bundle: Dict[str, Any]
    bundle_digest: Optional[str] = Field(None, description="Expected digest; compared when given")


# Core answers the CP does not name a code for: recover
# the status from the relayed detail so the UI can say what happened.
_BUNDLE_CORE_STATUS = {"HTTP 404": 404, "HTTP 413": 413}


def _raise_bundle_error(result: Dict[str, Any]) -> None:
    detail = str(result.get("detail") or "")
    if result.get("error") == core_client.ERROR_CORE:
        for prefix, status in _BUNDLE_CORE_STATUS.items():
            if detail.startswith(prefix):
                raise HTTPException(
                    status_code=status,
                    detail={
                        "error": "no_records" if status == 404 else "bundle_too_large",
                        "detail": detail,
                    },
                )
    _raise_core_error(result)


# core_client error code → HTTP status for pass-through admin calls.
_CORE_ERROR_STATUS = {
    core_client.ERROR_EVIDENCE_DISABLED: 503,
    core_client.ERROR_TENANT_KEY_UNAVAILABLE: 503,
    core_client.ERROR_INVALID_RECORD: 422,
    core_client.ERROR_PAYLOADS_UNAVAILABLE: 409,
    core_client.ERROR_CORE: 502,
    core_client.ERROR_CORE_UNREACHABLE: 502,
}


def _raise_core_error(result: Dict[str, Any]) -> None:
    raise HTTPException(
        status_code=_CORE_ERROR_STATUS.get(result.get("error"), 502),
        detail={"error": result.get("error"), "detail": result.get("detail")},
    )


def _b64(data: Optional[bytes]) -> Optional[str]:
    return base64.b64encode(data).decode("ascii") if data else None


def _head_ref(head: Optional[EvidenceTreeHead]) -> Optional[TreeHeadRef]:
    if head is None:
        return None
    return TreeHeadRef(
        id=head.id,
        tree_size=head.tree_size,
        issued_at=head.issued_at.isoformat() if head.issued_at else None,
        grade=head.grade,
    )


def _client(request: Request) -> Dict[str, Any]:
    return {
        "ip_address": client_ip(request),
        "user_agent": request.headers.get("user-agent"),
        "request_method": request.method,
        "request_path": str(request.url.path),
    }


def create_evidence_router(logger: BrutorLogger) -> APIRouter:
    router = APIRouter(prefix="/admin/evidence", tags=["Evidence"])

    # -- helpers ----------------------------------------------------------

    def _tenant_log(db: Session, tenant_id: str) -> Optional[EvidenceLog]:
        return db.query(EvidenceLog).filter(EvidenceLog.tenant_id == tenant_id).first()

    def _load_record(db: Session, tenant_id: str, record_id: str) -> ActionRecord:
        row = (
            db.query(ActionRecord)
            .filter(ActionRecord.record_id == record_id, ActionRecord.tenant_id == tenant_id)
            .first()
        )
        if row is None:
            raise HTTPException(status_code=404, detail=f"Record {record_id!r} not found")
        return row

    def _latest_head(db: Session, log_id: str) -> Optional[EvidenceTreeHead]:
        return (
            db.query(EvidenceTreeHead)
            .filter(EvidenceTreeHead.log_id == log_id)
            .order_by(EvidenceTreeHead.tree_size.desc())
            .first()
        )

    def _receipts_for(db: Session, head_ids: List[str]) -> Dict[str, List[EvidenceReceipt]]:
        out: Dict[str, List[EvidenceReceipt]] = {h: [] for h in head_ids}
        if not head_ids:
            return out
        rows = (
            db.query(EvidenceReceipt)
            .filter(EvidenceReceipt.tree_head_id.in_(head_ids))
            .order_by(EvidenceReceipt.created_at, EvidenceReceipt.id)
            .all()
        )
        for r in rows:
            out.setdefault(r.tree_head_id, []).append(r)
        return out

    def _head_out(head: EvidenceTreeHead, receipts: List[EvidenceReceipt]) -> TreeHeadOut:
        return TreeHeadOut(
            **head.to_dict(),
            receipts=[ReceiptOut(**r.summary_dict()) for r in receipts],
        )

    def _records_query(
        db: Session,
        tenant_id: str,
        *,
        ai_system_id: Optional[str],
        verdict: Optional[str],
        kind: Optional[str],
        surface: Optional[str],
        root_task_id: Optional[str],
        since: Optional[datetime],
        until: Optional[datetime],
    ):
        if verdict is not None and verdict not in RECORD_VERDICTS:
            raise HTTPException(
                status_code=422, detail=f"verdict must be one of {sorted(RECORD_VERDICTS)}"
            )
        if kind is not None and kind not in RECORD_KINDS:
            raise HTTPException(
                status_code=422, detail=f"kind must be one of {sorted(RECORD_KINDS)}"
            )
        if since and until and until < since:
            raise HTTPException(status_code=422, detail="until must not be before since")
        q = db.query(ActionRecord).filter(ActionRecord.tenant_id == tenant_id)
        if ai_system_id is not None:
            q = q.filter(ActionRecord.ai_system_id == ai_system_id)
        if verdict is not None:
            q = q.filter(ActionRecord.verdict == verdict)
        if kind is not None:
            q = q.filter(ActionRecord.kind == kind)
        if surface is not None:
            q = q.filter(ActionRecord.surface == surface)
        if root_task_id is not None:
            q = q.filter(ActionRecord.root_task_id == root_task_id)
        if since is not None:
            q = q.filter(ActionRecord.timestamp >= since)
        if until is not None:
            q = q.filter(ActionRecord.timestamp <= until)
        return q

    # -- records ----------------------------------------------------------

    @router.get(
        "/records",
        response_model=RecordListResponse,
        summary="Sealed action records for the tenant",
    )
    def list_records(
        ai_system_id: Optional[str] = Query(None),
        verdict: Optional[str] = Query(None),
        kind: Optional[str] = Query(None, description="decide | fyi"),
        surface: Optional[str] = Query(None),
        root_task_id: Optional[str] = Query(None),
        since: Optional[datetime] = Query(None, description="ISO 8601 — records at or after"),
        until: Optional[datetime] = Query(None, description="ISO 8601 — records at or before"),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.EVIDENCE_READ)),
    ) -> RecordListResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        q = _records_query(
            db,
            resolved,
            ai_system_id=ai_system_id,
            verdict=verdict,
            kind=kind,
            surface=surface,
            root_task_id=root_task_id,
            since=since,
            until=until,
        )
        total = q.count()
        rows = (
            q.order_by(ActionRecord.timestamp.desc(), ActionRecord.seq.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        return RecordListResponse(
            items=[RecordSummary(**r.summary_dict()) for r in rows],
            total=total,
            limit=limit,
            offset=offset,
        )

    @router.get(
        "/records/{record_id}",
        response_model=RecordDetail,
        summary="One sealed record with its statement",
    )
    def get_record(
        record_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.EVIDENCE_READ)),
    ) -> RecordDetail:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        row = _load_record(db, resolved, record_id)
        log = db.get(EvidenceLog, row.log_id)
        return RecordDetail(
            **row.to_dict(),
            log=RecordLogRef(
                log_id=row.log_id,
                seq=row.seq,
                tree_size_now=(log.tree_size or 0) if log else 0,
            ),
        )

    @router.get(
        "/records/{record_id}/proof",
        response_model=InclusionProof,
        summary="RFC 6962 inclusion proof for a record in the tenant log",
    )
    def get_record_proof(
        record_id: str,
        tree_size: Optional[int] = Query(
            None,
            ge=1,
            description="Prove against an earlier tree size (defaults to the current one)",
        ),
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.EVIDENCE_READ)),
    ) -> InclusionProof:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        row = _load_record(db, resolved, record_id)
        log = db.get(EvidenceLog, row.log_id)
        current = (log.tree_size or 0) if log else 0
        if current <= 0:
            raise HTTPException(
                status_code=409, detail="The tenant log has no entries to prove against"
            )
        size = current if tree_size is None else tree_size
        if size > current:
            raise HTTPException(
                status_code=422,
                detail=f"tree_size {size} exceeds the log's current size {current}",
            )
        if row.seq >= size:
            raise HTTPException(
                status_code=422,
                detail=f"record is leaf {row.seq}, not in a tree of size {size}",
            )

        cache: Dict[tuple, Optional[str]] = {}

        def get_node(level: int, index: int) -> Optional[str]:
            key = (level, index)
            if key not in cache:
                node = db.get(EvidenceLogNode, (row.log_id, level, index))
                cache[key] = node.hash if node else None
            return cache[key]

        try:
            path = merkle.inclusion_path(get_node, row.seq, size)
            root = merkle.root_hash(get_node, size)
        except merkle.MerkleError as exc:
            # A missing node is a broken store, not a bad request: say so
            # loudly rather than serving a proof that cannot verify.
            logger.error(
                "evidence log node store cannot serve a proof",
                context={"log_id": row.log_id, "seq": row.seq, "tree_size": size, "error": str(exc)},
            )
            raise HTTPException(
                status_code=500, detail=f"inclusion proof unavailable: {exc}"
            )

        return InclusionProof(
            record_id=row.record_id,
            log_id=row.log_id,
            leaf_index=row.seq,
            tree_size=size,
            root=root,
            path=path,
            leaf_hash=merkle.leaf_hash(row.statement or b""),
        )

    # -- summary ----------------------------------------------------------

    @router.get(
        "/summary",
        response_model=EvidenceSummary,
        summary="Record counts by verdict / kind / surface, and the tenant log's status",
    )
    def evidence_summary(
        since: Optional[datetime] = Query(None),
        until: Optional[datetime] = Query(None),
        ai_system_id: Optional[str] = Query(None),
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.EVIDENCE_READ)),
    ) -> EvidenceSummary:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        base = _records_query(
            db,
            resolved,
            ai_system_id=ai_system_id,
            verdict=None,
            kind=None,
            surface=None,
            root_task_id=None,
            since=since,
            until=until,
        )

        def _counts(column) -> Dict[str, int]:
            rows = (
                base.with_entities(column, func.count(ActionRecord.record_id))
                .group_by(column)
                .all()
            )
            return {str(k): int(n) for k, n in rows if k is not None}

        by_verdict = _counts(ActionRecord.verdict)
        by_kind = _counts(ActionRecord.kind)
        by_surface = _counts(ActionRecord.surface)
        records_total = sum(by_verdict.values())
        systems_covered = (
            base.filter(ActionRecord.ai_system_id.isnot(None))
            .with_entities(func.count(func.distinct(ActionRecord.ai_system_id)))
            .scalar()
            or 0
        )
        # `disposition.human_disposed` lives inside the record body. Counted
        # in the database, on the generated column and its partial index:
        # reading every record body into the control plane to count one flag
        # took 2.5 s on 29,000 records and grew with the ledger.
        human_disposed = (
            base.filter(ActionRecord.human_disposed)  # bare, so the partial index applies
            .with_entities(func.count(ActionRecord.record_id))
            .scalar()
            or 0
        )

        log = _tenant_log(db, resolved)
        log_status = None
        if log is not None:
            unwitnessed = (
                db.query(func.count(EvidenceTreeHead.id))
                .filter(
                    EvidenceTreeHead.log_id == log.log_id,
                    EvidenceTreeHead.grade == GRADE_STANDALONE,
                )
                .scalar()
                or 0
            )
            log_status = LogStatus(
                log_id=log.log_id,
                tree_size=log.tree_size or 0,
                root=log.root,
                last_tree_head=_head_ref(_latest_head(db, log.log_id)),
                unwitnessed_heads=int(unwitnessed),
            )

        return EvidenceSummary(
            records_total=records_total,
            by_verdict=by_verdict,
            by_kind=by_kind,
            by_surface=by_surface,
            human_disposed_count=human_disposed,
            systems_covered=int(systems_covered),
            log=log_status,
        )

    # -- logs + heads -----------------------------------------------------

    @router.get(
        "/logs",
        response_model=LogListResponse,
        summary="Tenant evidence logs (one per tenant; all for a system admin without tenant_id)",
    )
    def list_logs(
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.EVIDENCE_READ)),
    ) -> LogListResponse:
        admin, admin_type = admin_and_type
        q = db.query(EvidenceLog)
        # Every tenant's logs are the system administrator's to list; a
        # type nobody registered lists none (403).
        scope_tenant_id = tenant_scope(admin, admin_type, tenant_id)
        if scope_tenant_id:
            q = q.filter(EvidenceLog.tenant_id == scope_tenant_id)
        logs = q.order_by(EvidenceLog.tenant_id).all()

        items: List[LogListItem] = []
        for log in logs:
            heads_count = (
                db.query(func.count(EvidenceTreeHead.id))
                .filter(EvidenceTreeHead.log_id == log.log_id)
                .scalar()
                or 0
            )
            receipts_count = (
                db.query(func.count(EvidenceReceipt.id))
                .join(EvidenceTreeHead, EvidenceTreeHead.id == EvidenceReceipt.tree_head_id)
                .filter(EvidenceTreeHead.log_id == log.log_id)
                .scalar()
                or 0
            )
            latest = _latest_head(db, log.log_id)
            items.append(
                LogListItem(
                    log_id=log.log_id,
                    tenant_id=log.tenant_id,
                    tree_size=log.tree_size or 0,
                    root=log.root,
                    kid=log.kid,
                    last_tree_head=_head_ref(latest),
                    heads_count=int(heads_count),
                    receipts_count=int(receipts_count),
                    latest_grade=latest.grade if latest else None,
                )
            )
        return LogListResponse(items=items, total=len(items))

    @router.get(
        "/logs/{log_id}/tree-heads",
        response_model=TreeHeadListResponse,
        summary="Tree heads of a log, newest first, with their receipts",
    )
    def list_tree_heads(
        log_id: str,
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.EVIDENCE_READ)),
    ) -> TreeHeadListResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        log = db.get(EvidenceLog, log_id)
        if log is None or log.tenant_id != resolved:
            raise HTTPException(status_code=404, detail=f"Log {log_id!r} not found")
        q = db.query(EvidenceTreeHead).filter(EvidenceTreeHead.log_id == log_id)
        total = q.count()
        heads = q.order_by(EvidenceTreeHead.tree_size.desc()).offset(offset).limit(limit).all()
        receipts = _receipts_for(db, [h.id for h in heads])
        return TreeHeadListResponse(
            items=[_head_out(h, receipts.get(h.id, [])) for h in heads],
            total=total,
            limit=limit,
            offset=offset,
        )

    @router.get(
        "/tree-heads/{head_id}",
        response_model=TreeHeadOut,
        summary="One tree head with its receipts",
    )
    def get_tree_head(
        head_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.EVIDENCE_READ)),
    ) -> TreeHeadOut:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        head = db.get(EvidenceTreeHead, head_id)
        if head is None or head.tenant_id != resolved:
            raise HTTPException(status_code=404, detail=f"Tree head {head_id!r} not found")
        return _head_out(head, _receipts_for(db, [head.id]).get(head.id, []))

    # -- witnesses --------------------------------------------------------

    def _validate_witness_fields(kind: Optional[str], url: Optional[str]) -> None:
        if kind is not None and kind not in WITNESS_KINDS:
            raise HTTPException(
                status_code=422, detail=f"kind must be one of {sorted(WITNESS_KINDS)}"
            )
        if url is not None and not url.strip().lower().startswith(URL_SCHEMES):
            raise HTTPException(status_code=422, detail="url must be an http:// or https:// URL")

    def _load_witness_for_write(
        db: Session, admin, admin_type: str, tenant_id: Optional[str], witness_id: str
    ) -> Witness:
        row = db.get(Witness, witness_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"Witness {witness_id!r} not found")
        if not is_platform_wide(admin_type):
            # Anyone but the system administrator: a tenant administrator,
            # and a type nobody registered (which is of no tenant).
            if row.scope == SCOPE_PLATFORM:
                raise HTTPException(
                    status_code=403,
                    detail="Platform witnesses are managed by a system admin",
                )
            if not may_act_on_tenant(admin, admin_type, row.tenant_id):
                raise HTTPException(status_code=404, detail=f"Witness {witness_id!r} not found")
        elif row.scope == SCOPE_TENANT:
            # A system admin editing a tenant witness must name the tenant,
            # and it must be the one the row belongs to.
            resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
            if row.tenant_id != resolved:
                raise HTTPException(status_code=404, detail=f"Witness {witness_id!r} not found")
        return row

    @router.get(
        "/witnesses",
        response_model=WitnessListResponse,
        summary="Platform witnesses plus this tenant's own",
    )
    def list_witnesses(
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.EVIDENCE_READ)),
    ) -> WitnessListResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        rows = (
            db.query(Witness)
            .filter(
                (Witness.scope == SCOPE_PLATFORM)
                | ((Witness.scope == SCOPE_TENANT) & (Witness.tenant_id == resolved))
            )
            .order_by(Witness.scope, Witness.operator_label, Witness.id)
            .all()
        )
        return WitnessListResponse(
            items=[WitnessOut(**w.to_dict()) for w in rows], total=len(rows)
        )

    @router.post(
        "/witnesses",
        response_model=WitnessOut,
        status_code=201,
        summary="Add a witness (tenant admins: scope=tenant only)",
    )
    def create_witness(
        request: Request,
        body: WitnessCreateRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.EVIDENCE_MANAGE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ) -> WitnessOut:
        admin, admin_type = admin_and_type
        if body.scope not in WITNESS_SCOPES:
            raise HTTPException(
                status_code=422, detail=f"scope must be one of {sorted(WITNESS_SCOPES)}"
            )
        _validate_witness_fields(body.kind, body.url)

        if body.scope == SCOPE_PLATFORM:
            if not is_platform_wide(admin_type):
                raise HTTPException(
                    status_code=403,
                    detail="Only a system admin can add a platform-scope witness",
                )
            owner: Optional[str] = None
            audit_tenant = tenant_id
        else:
            owner = _resolve_tenant_id(admin, admin_type, tenant_id)
            audit_tenant = owner

        row = Witness(
            id=f"wit-{ULID()}".lower(),
            scope=body.scope,
            tenant_id=owner,
            kind=body.kind,
            url=body.url.strip(),
            operator_label=body.operator_label.strip(),
            pinned_key=(body.pinned_key or None),
            enabled=body.enabled,
            same_operator=body.same_operator,
        )
        db.add(row)
        db.commit()
        db.refresh(row)

        audit.log(
            action="evidence_witness_create",
            admin=admin,
            admin_type=admin_type,
            tenant_id=audit_tenant,
            success=True,
            resource_type="witness",
            resource_id=row.id,
            details={
                "scope": row.scope,
                "kind": row.kind,
                "url": row.url,
                "operator_label": row.operator_label,
                "same_operator": row.same_operator,
            },
            db=db,
            **_client(request),
        )
        return WitnessOut(**row.to_dict())

    @router.patch(
        "/witnesses/{witness_id}",
        response_model=WitnessOut,
        summary="Update a witness",
    )
    def update_witness(
        request: Request,
        witness_id: str,
        body: WitnessUpdateRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.EVIDENCE_MANAGE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ) -> WitnessOut:
        admin, admin_type = admin_and_type
        row = _load_witness_for_write(db, admin, admin_type, tenant_id, witness_id)
        _validate_witness_fields(None, body.url)

        changes: Dict[str, Any] = {}
        for name in ("url", "operator_label", "pinned_key", "enabled", "same_operator"):
            value = getattr(body, name)
            if value is None:
                continue
            if isinstance(value, str):
                value = value.strip()
            if getattr(row, name) != value:
                changes[name] = {"from": getattr(row, name), "to": value}
                setattr(row, name, value)
        if changes:
            db.commit()
            db.refresh(row)
            audit.log(
                action="evidence_witness_update",
                admin=admin,
                admin_type=admin_type,
                tenant_id=row.tenant_id if row.scope == SCOPE_TENANT else tenant_id,
                success=True,
                resource_type="witness",
                resource_id=row.id,
                details={"changes": changes},
                db=db,
                **_client(request),
            )
        return WitnessOut(**row.to_dict())

    @router.delete(
        "/witnesses/{witness_id}",
        summary="Remove a witness",
    )
    def delete_witness(
        request: Request,
        witness_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.EVIDENCE_MANAGE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ) -> Dict[str, Any]:
        admin, admin_type = admin_and_type
        row = _load_witness_for_write(db, admin, admin_type, tenant_id, witness_id)
        snapshot = row.to_dict()
        db.delete(row)
        db.commit()
        audit.log(
            action="evidence_witness_delete",
            admin=admin,
            admin_type=admin_type,
            tenant_id=snapshot["tenant_id"] if snapshot["scope"] == SCOPE_TENANT else tenant_id,
            success=True,
            resource_type="witness",
            resource_id=witness_id,
            details={"deleted": snapshot},
            db=db,
            **_client(request),
        )
        return {"deleted": True, "id": witness_id}

    # -- disclosures ------------------------------------------------------

    @router.get(
        "/disclosures",
        response_model=DisclosureListResponse,
        summary="Disclosure records for the tenant (newest first)",
    )
    def list_disclosures(
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.EVIDENCE_READ)),
    ) -> DisclosureListResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        q = db.query(DisclosureRecord).filter(DisclosureRecord.tenant_id == resolved)
        total = q.count()
        rows = (
            q.order_by(DisclosureRecord.created_at.desc(), DisclosureRecord.id.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        return DisclosureListResponse(
            items=[DisclosureOut(**r.to_dict()) for r in rows],
            total=total,
            limit=limit,
            offset=offset,
        )

    # -- verify -----------------------------------------------------------

    @router.post(
        "/verify",
        summary="Verify a record (by id or inline) through the core's verifier",
    )
    def verify(
        body: VerifyRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.EVIDENCE_READ)),
    ) -> Dict[str, Any]:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        if body.record_id:
            row = _load_record(db, resolved, body.record_id)
            record = row.record if isinstance(row.record, dict) else {}
            statement_b64 = _b64(row.statement)
        elif body.record is not None:
            record = body.record
            statement_b64 = body.statement_b64
        else:
            raise HTTPException(
                status_code=422, detail="Provide record_id or an inline record"
            )
        return core_client.verify_record(record, statement_b64)

    # -- settings & keys -------------------------------------------------

    def _settings_out(db: Session, tenant: str) -> EvidenceSettingsOut:
        row = db.get(EvidenceSettings, tenant)
        data = row.to_dict() if row is not None else EvidenceSettings.defaults_dict(tenant)
        return EvidenceSettingsOut(**data)

    @router.get(
        "/settings",
        response_model=EvidenceSettingsOut,
        summary="Per-tenant evidence settings (no row = defaults)",
    )
    def get_settings(
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.EVIDENCE_READ)),
    ) -> EvidenceSettingsOut:
        admin, admin_type = admin_and_type
        return _settings_out(db, _resolve_tenant_id(admin, admin_type, tenant_id))

    @router.put(
        "/settings",
        response_model=EvidenceSettingsOut,
        summary="Change per-tenant evidence settings",
    )
    def put_settings(
        request: Request,
        body: EvidenceSettingsUpdate,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.EVIDENCE_MANAGE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ) -> EvidenceSettingsOut:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        row = db.get(EvidenceSettings, resolved)
        created = row is None
        if created:
            row = EvidenceSettings(
                tenant_id=resolved, per_tenant_key=False, salt_digests_default=False
            )
            db.add(row)

        changes: Dict[str, Any] = {}
        for name in ("per_tenant_key", "salt_digests_default"):
            value = getattr(body, name)
            if value is None or bool(getattr(row, name)) == value:
                continue
            changes[name] = {"from": bool(getattr(row, name)), "to": value}
            setattr(row, name, value)
        if changes or created:
            row.updated_by = actor_name(admin)
        db.commit()
        db.refresh(row)
        if changes:
            audit.log(
                action="evidence_settings_update",
                admin=admin,
                admin_type=admin_type,
                tenant_id=resolved,
                success=True,
                resource_type="evidence_settings",
                resource_id=resolved,
                details={"changes": changes},
                db=db,
                **_client(request),
            )
        return EvidenceSettingsOut(**row.to_dict())

    @router.post(
        "/keys/rotate",
        response_model=KeyRotationOut,
        summary="Create or rotate the tenant's own signing key (requires per_tenant_key)",
    )
    def rotate_key(
        request: Request,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.EVIDENCE_MANAGE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ) -> KeyRotationOut:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        settings = db.get(EvidenceSettings, resolved)
        if settings is None or not settings.per_tenant_key:
            # A tenant key the settings say is not in use would silently
            # change which key signs this tenant's records.
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "per_tenant_key_disabled",
                    "detail": "Enable per_tenant_key in evidence settings before rotating",
                },
            )
        result = core_client.rotate_key(resolved)
        audit.log(
            action="evidence_key_rotate",
            admin=admin,
            admin_type=admin_type,
            tenant_id=resolved,
            success=not core_client.is_error(result),
            resource_type="evidence_signing_key",
            resource_id=result.get("kid") or resolved,
            details=(
                {"error": result.get("error"), "detail": result.get("detail")}
                if core_client.is_error(result)
                else {
                    "kid": result.get("kid"),
                    "previous_kid": result.get("previous_kid"),
                    "rotation_record_id": result.get("rotation_record_id"),
                }
            ),
            db=db,
            **_client(request),
        )
        if core_client.is_error(result):
            _raise_core_error(result)
        return KeyRotationOut(**result)

    @router.get(
        "/keys",
        response_model=SigningKeyListResponse,
        summary="Signing key ids for this tenant's records (deployment + tenant), active and retired",
    )
    def list_keys(
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.EVIDENCE_READ)),
    ) -> SigningKeyListResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        rows = (
            db.query(EvidenceSigningKey)
            .filter(
                EvidenceSigningKey.tenant_id.is_(None)
                | (EvidenceSigningKey.tenant_id == resolved)
            )
            .order_by(EvidenceSigningKey.created_at.desc(), EvidenceSigningKey.kid)
            .all()
        )
        log = _tenant_log(db, resolved)
        settings = db.get(EvidenceSettings, resolved)
        items = [
            SigningKeyOut(**{k: v for k, v in r.to_dict().items() if k != "tenant_id"})
            for r in rows
        ]
        return SigningKeyListResponse(
            items=items,
            total=len(items),
            log_kid=log.kid if log is not None else None,
            per_tenant_key=bool(settings.per_tenant_key) if settings is not None else False,
        )

    @router.post(
        "/bundles",
        summary="Build an evidence bundle through the core (records, proofs, heads, receipts)",
    )
    def build_bundle(
        request: Request,
        body: BundleBuildRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.EVIDENCE_READ)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ) -> Dict[str, Any]:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        sel = body.selector
        chosen = [
            name
            for name, present in (
                ("record_ids", bool(sel.record_ids)),
                ("root_task_id", sel.root_task_id is not None),
                ("ai_system_id", sel.ai_system_id is not None),
            )
            if present
        ]
        if len(chosen) != 1:
            raise HTTPException(
                status_code=422,
                detail="selector must name exactly one of record_ids, root_task_id, ai_system_id",
            )
        if chosen[0] == "record_ids":
            selector: Dict[str, Any] = {"record_ids": list(dict.fromkeys(sel.record_ids or []))}
        elif chosen[0] == "root_task_id":
            selector = {"root_task_id": sel.root_task_id}
        else:
            if sel.from_ is None or sel.to is None:
                raise HTTPException(
                    status_code=422, detail="an ai_system_id selector needs from and to"
                )
            if sel.to < sel.from_:
                raise HTTPException(status_code=422, detail="to must not be before from")
            selector = {
                "ai_system_id": sel.ai_system_id,
                "from": sel.from_.isoformat(),
                "to": sel.to.isoformat(),
            }
        audience = (body.audience or "").strip() or None
        result = core_client.build_bundle(
            resolved,
            selector,
            audience=audience,
            framework_id=body.framework_id,
            closure_depth=body.closure_depth,
            requested_by=actor_name(admin),
        )
        failed = core_client.is_error(result)
        audit.log(
            action="evidence_bundle_build",
            admin=admin,
            admin_type=admin_type,
            tenant_id=resolved,
            success=not failed,
            resource_type="evidence_bundle",
            resource_id=(None if failed else result.get("bundle_id")) or resolved,
            details=(
                {"selector": list(selector), "error": result.get("error")}
                if failed
                else {
                    "selector": list(selector),
                    "bundle_digest": result.get("bundle_digest"),
                    "record_id": result.get("record_id"),
                    "disclosed": audience is not None,
                }
            ),
            db=db,
            **_client(request),
        )
        if failed:
            _raise_bundle_error(result)
        return result

    @router.post(
        "/bundles/verify",
        summary="Verify an evidence bundle through the core's verifier",
    )
    def verify_bundle(
        body: BundleVerifyRequest,
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.EVIDENCE_READ)),
    ) -> Dict[str, Any]:
        result = core_client.verify_bundle(body.bundle)
        if core_client.is_error(result):
            # A verifier that could not run is never a clean result.
            return {
                "ok": False,
                "findings": [
                    {
                        "check": "verifier",
                        "code": core_client.VERIFIER_UNAVAILABLE,
                        "severity": "error",
                        "detail": str(result.get("detail") or result.get("error")),
                    }
                ],
                "bundle_digest": None,
                "missing": [],
            }
        if body.bundle_digest:
            computed = result.get("bundle_digest")
            result["digest_matches"] = bool(computed) and computed == body.bundle_digest.lower()
        return result

    return router


__all__ = ["create_evidence_router"]
