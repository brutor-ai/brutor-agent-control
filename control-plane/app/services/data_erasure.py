# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Data-subject erasure.

One transaction; the line between what goes and what stays is documented in
the data-handling page (https://docs.brutor.ai/governance/data-handling/) and repeated in the code below where each decision is
made. The short version: content and direct identifiers are removed,
pseudonymous keys that the audit hash chain covers are kept, and the
`end_users` record that linked those keys to a person is deleted.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Dict, List, Optional

from sqlalchemy import delete, func, null, or_, select, update
from sqlalchemy.orm import Session
from ulid import ULID

from app.api.v1.models.admin.data_erasure import DataErasureRequest
from app.database.models.ai_system_run_feedback import AISystemRunFeedback
from app.database.models.batch_job import BatchJob
from app.database.models.batch_request import BatchRequest
from app.database.models.data_erasure import DataErasureEvent
from app.database.models.end_user import EndUser
from app.database.models.oauth_token import OAuthToken
from app.database.models.proxy_log import ProxyLog
from app.database.models.tool_approval_request import ToolApprovalRequest
from app.database.models.usage_log import UsageLog


def subject_fingerprint(identifiers: Dict[str, str]) -> str:
    """Stable hash over the identifiers named, so the event row can be matched
    to a later request for the same person without storing who they were."""
    canonical = json.dumps(
        {k: str(v).strip().lower() for k, v in sorted(identifiers.items()) if v},
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class ErasurePlan:
    """The identifiers, resolved. Built once so dry-run and real run agree."""

    def __init__(self, db: Session, tenant_id: str, req: DataErasureRequest):
        self.tenant_id = tenant_id
        self.end_user: Optional[EndUser] = None
        if req.end_user_id is not None:
            self.end_user = (
                db.query(EndUser)
                .filter(EndUser.tenant_id == tenant_id, EndUser.id == req.end_user_id)
                .first()
            )
        elif req.email:
            self.end_user = (
                db.query(EndUser)
                .filter(
                    EndUser.tenant_id == tenant_id,
                    func.lower(EndUser.email) == req.email.strip().lower(),
                )
                .first()
            )

        self.end_user_id: Optional[int] = self.end_user.id if self.end_user else req.end_user_id
        self.idp_user_id = req.idp_user_id or (
            self.end_user.idp_subject if self.end_user and self.end_user.idp_subject else None
        )
        self.email = (req.email or (self.end_user.email if self.end_user else None) or "").strip().lower() or None
        # Delegation subject: the proxy stamps `subject_id` as the api-key user
        # id when an agent acts for a person. Accept an explicit one too.
        self.subject_id = req.subject_id or (str(self.end_user_id) if self.end_user_id is not None else None)

        self.kinds: List[str] = [
            k for k, v in (
                ("end_user_id", req.end_user_id),
                ("idp_user_id", req.idp_user_id),
                ("email", req.email),
                ("subject_id", req.subject_id),
            ) if v
        ]
        self.fingerprint = subject_fingerprint({
            "end_user_id": self.end_user_id,
            "idp_user_id": self.idp_user_id,
            "email": self.email,
            "subject_id": self.subject_id,
        })

    def log_predicate(self):
        """Rows attributed to this subject, by any identifier we hold."""
        clauses = []
        if self.end_user_id is not None:
            clauses.append(ProxyLog.gateway_user_id == str(self.end_user_id))
        if self.idp_user_id:
            clauses.append(ProxyLog.idp_user_id == self.idp_user_id)
        if self.email:
            clauses.append(func.lower(ProxyLog.idp_user_email) == self.email)
        if self.subject_id:
            clauses.append(ProxyLog.subject_id == self.subject_id)
        return or_(*clauses) if clauses else None


def erase(
    db: Session,
    tenant_id: str,
    req: DataErasureRequest,
    *,
    requested_by: str,
) -> DataErasureEvent:
    """Execute (or dry-run) an erasure and record it. Caller commits."""
    plan = ErasurePlan(db, tenant_id, req)
    counts: Dict[str, int] = {}
    pred = plan.log_predicate()

    # --- proxy_logs: content + direct identifiers go; the record stays ---
    if pred is not None:
        base = (ProxyLog.tenant_id == tenant_id, pred, ProxyLog.erased_at.is_(None))
        if req.dry_run:
            counts["proxy_logs"] = int(
                db.execute(select(func.count()).select_from(ProxyLog).where(*base)).scalar() or 0
            )
        else:
            res = db.execute(
                update(ProxyLog)
                .where(*base)
                .values(
                    request_body_preview=None,
                    response_body_preview=None,
                    request_headers=None,
                    response_headers=None,
                    # Direct identifiers. The pseudonymous ids (gateway_user_id,
                    # idp_user_id, subject_id) stay: subject_id is hashed into
                    # the audit chain, and all three stop identifying anyone
                    # once the end_users row below is gone.
                    idp_user_email=None,
                    idp_user_username=None,
                    gateway_user_username=None,
                    client_ip=None,
                    user_agent=None,
                    erased_at=func.now(),
                )
                .execution_options(synchronize_session=False)
            )
            counts["proxy_logs"] = int(res.rowcount or 0)
    else:
        counts["proxy_logs"] = 0

    # --- usage_logs: direct identifiers; user_id nulls via FK on user delete ---
    if plan.end_user_id is not None:
        ubase = (UsageLog.tenant_id == tenant_id, UsageLog.user_id == plan.end_user_id)
        if req.dry_run:
            counts["usage_logs"] = int(
                db.execute(select(func.count()).select_from(UsageLog).where(*ubase)).scalar() or 0
            )
        else:
            res = db.execute(
                update(UsageLog)
                .where(*ubase)
                .values(client_ip=None, user_agent=None)
                .execution_options(synchronize_session=False)
            )
            counts["usage_logs"] = int(res.rowcount or 0)
    else:
        counts["usage_logs"] = 0

    # --- feedback: free text authored by the subject ---
    fb_ids = [v for v in (str(plan.end_user_id) if plan.end_user_id is not None else None,
                          plan.idp_user_id, plan.subject_id) if v]
    if fb_ids:
        fbase = (AISystemRunFeedback.tenant_id == tenant_id, AISystemRunFeedback.user_id.in_(fb_ids))
        if req.dry_run:
            counts["run_feedback"] = int(
                db.execute(select(func.count()).select_from(AISystemRunFeedback).where(*fbase)).scalar() or 0
            )
        else:
            res = db.execute(
                delete(AISystemRunFeedback).where(*fbase).execution_options(synchronize_session=False)
            )
            counts["run_feedback"] = int(res.rowcount or 0)
    else:
        counts["run_feedback"] = 0

    # --- record accesses: who reached which record reference ---
    # The reference is keyed, never the value, but the row says this person
    # (through an agent) reached a record: it goes with the rest.
    from app.database.models.ai_system_data_scope import AISystemRecordAccess

    ra_ids = [v for v in (plan.subject_id, str(plan.end_user_id) if plan.end_user_id is not None else None) if v]
    if ra_ids:
        rbase = (AISystemRecordAccess.tenant_id == tenant_id, AISystemRecordAccess.subject_id.in_(ra_ids))
        if req.dry_run:
            counts["record_access"] = int(
                db.execute(select(func.count()).select_from(AISystemRecordAccess).where(*rbase)).scalar() or 0
            )
        else:
            res = db.execute(
                delete(AISystemRecordAccess).where(*rbase).execution_options(synchronize_session=False)
            )
            counts["record_access"] = int(res.rowcount or 0)
    else:
        counts["record_access"] = 0

    # --- oauth_tokens: a live credential, not a log row ---
    #
    # `oauth_tokens` has no FK to `end_users` (it is keyed by the
    # `gateway_user_id` / `idp_user_id` *strings*, written by the proxy's
    # token manager), so deleting the person below leaves these rows
    # behind. Two reasons they cannot stay:
    #
    #   1. They hold the subject's encrypted third-party access and
    #      refresh tokens. A credential that still works is not erased
    #      data by any reading of Art. 17.
    #   2. `idp_user_id` on a surviving row is a direct link back to the
    #      person at the IdP, which defeats the pseudonymisation argument
    #      the "Keeps" list above rests on.
    #
    # DELETE rather than null-out: unlike proxy_logs there is no record
    # layer worth preserving here — the row *is* the credential, and it is
    # not covered by the audit hash chain.
    ot_clauses = []
    if plan.end_user_id is not None:
        ot_clauses.append(OAuthToken.gateway_user_id == str(plan.end_user_id))
    if plan.idp_user_id:
        ot_clauses.append(OAuthToken.idp_user_id == plan.idp_user_id)
    if ot_clauses:
        obase = (OAuthToken.tenant_id == tenant_id, or_(*ot_clauses))
        if req.dry_run:
            counts["oauth_tokens"] = int(
                db.execute(select(func.count()).select_from(OAuthToken).where(*obase)).scalar() or 0
            )
        else:
            res = db.execute(
                delete(OAuthToken).where(*obase).execution_options(synchronize_session=False)
            )
            counts["oauth_tokens"] = int(res.rowcount or 0)
    else:
        counts["oauth_tokens"] = 0

    # --- tool_approval_requests: the subject's tool-call arguments ---
    #
    # `call_arguments` is "tool call arguments for reviewer inspection" —
    # the subject's own request content, in the clear — and
    # `requester_display` is a human-readable identifier snapshot (name /
    # email at the time of the request), which survives the end_users
    # delete because it is a denormalised copy. `request_metadata` carries
    # the requester's IP — the same direct identifier we null as
    # `client_ip` on proxy_logs, so it goes for the same reason. All three
    # go; the approval *decision* (who approved, when, the resolution
    # note) stays, for the same reason the inbox trail stays: it is the
    # record that a human made a call, not the content they made it about.
    #
    # Matched on `requester_user_id`, which holds a gateway user id — the
    # same namespace as `subject_id` (the api-key user id the proxy
    # stamps when an agent acts for a person). The IdP subject is
    # deliberately NOT matched here: it lives in a different namespace and
    # a cross-namespace string match would erase a different person's row.
    tar_ids = sorted({
        v for v in (
            str(plan.end_user_id) if plan.end_user_id is not None else None,
            plan.subject_id,
        ) if v
    })
    if tar_ids:
        tbase = (
            ToolApprovalRequest.tenant_id == tenant_id,
            ToolApprovalRequest.requester_user_id.in_(tar_ids),
        )
        if req.dry_run:
            counts["tool_approval_requests"] = int(
                db.execute(
                    select(func.count()).select_from(ToolApprovalRequest).where(*tbase)
                ).scalar() or 0
            )
        else:
            res = db.execute(
                update(ToolApprovalRequest)
                .where(*tbase)
                .values(call_arguments=None, request_metadata=None, requester_display=None)
                .execution_options(synchronize_session=False)
            )
            counts["tool_approval_requests"] = int(res.rowcount or 0)
    else:
        counts["tool_approval_requests"] = 0

    # --- batch_requests: full prompts and completions ---
    #
    # `batch_requests.request_body` / `.response_body` are the same class
    # of data as the proxy-log body previews — the subject's prompts and
    # the provider's completions — but stored whole, written by the
    # proxy's batch poller. `batch_jobs.user_id` is `ondelete="SET NULL"`,
    # so deleting the person below would orphan this content rather than
    # remove it: it survives, unattributed and unfindable by a second
    # erasure request. So this MUST run before the end_users delete —
    # the subquery below is the only remaining way to reach these rows.
    if plan.end_user_id is not None:
        job_ids = select(BatchJob.id).where(
            BatchJob.tenant_id == tenant_id,
            BatchJob.user_id == plan.end_user_id,
        )
        bbase = (
            BatchRequest.tenant_id == tenant_id,
            BatchRequest.batch_job_id.in_(job_ids),
        )
        if req.dry_run:
            counts["batch_requests"] = int(
                db.execute(select(func.count()).select_from(BatchRequest).where(*bbase)).scalar() or 0
            )
        else:
            res = db.execute(
                update(BatchRequest)
                .where(*bbase)
                # `null()`, not `None`: SQLAlchemy's JSON type persists a
                # Python None as the JSON value `null`, which is not SQL
                # NULL — an auditor checking `request_body IS NULL` would
                # be told the content is still there.
                .values(request_body=null(), response_body=null())
                .execution_options(synchronize_session=False)
            )
            counts["batch_requests"] = int(res.rowcount or 0)
    else:
        counts["batch_requests"] = 0

    # --- the person: deleting the record severs every pseudonymous key ---
    if plan.end_user is not None:
        counts["end_users"] = 1
        if not req.dry_run:
            db.delete(plan.end_user)
    else:
        counts["end_users"] = 0

    event = DataErasureEvent(
        id=f"erase-{ULID()}",
        tenant_id=tenant_id,
        subject_fingerprint=plan.fingerprint,
        subject_kinds=plan.kinds,
        requested_by=requested_by,
        requested_at=datetime.now(timezone.utc),
        dry_run=req.dry_run,
        counts=counts,
    )
    db.add(event)
    db.flush()
    return event
