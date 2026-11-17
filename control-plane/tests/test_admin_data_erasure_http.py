# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Data-subject erasure (a166_data_retention_erasure) — HTTP behavioural spec

What erasure removes and what it keeps is the whole design, so both halves
are pinned: content and direct identifiers go, the record layer and its
pseudonymous keys stay, and the event row never carries the identifiers.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import uuid
from datetime import datetime, timezone

import pytest

from app.api.v1.endpoints.admin.data_erasure import create_data_erasure_router
from app.core.logging_system import BrutorLogger
from app.database.models.ai_system_data_scope import AISystemRecordAccess
from app.database.models.ai_system_run_feedback import AISystemRunFeedback
from app.database.models.batch_job import BatchJob
from app.database.models.batch_request import BatchRequest
from app.database.models.end_user import EndUser
from app.database.models.oauth_token import OAuthToken
from app.database.models.proxy_log import ProxyLog
from app.database.models.tool_approval_request import ToolApprovalRequest
from tests.admin_endpoint_harness import build_admin_harness

TENANT = "t1"


@pytest.fixture()
def h():
    harness = build_admin_harness(
        [create_data_erasure_router(BrutorLogger("test-erasure"))],
        logger_name="test-erasure",
    )

    def seed(s):
        u = EndUser(tenant_id=TENANT, username="alice", email="Alice@Example.com", is_active=True, password_hash="x")
        s.add(u); s.flush()
        s.add(ProxyLog(
            id="plog-a", tenant_id=TENANT, proxy_type="llm", end_user_session_id="sess-1", http_method="POST",
            endpoint="/v1/chat", success=True, timestamp=datetime.now(timezone.utc),
            gateway_user_id=str(u.id), idp_user_email="alice@example.com",
            idp_user_username="alice", client_ip="10.0.0.9", user_agent="curl",
            request_body_preview="my address is 1 Main St", response_body_preview="ok",
            request_headers='{"x": "y"}', row_hash="deadbeef", subject_id=str(u.id),
        ))
        s.add(ProxyLog(
            id="plog-b", tenant_id=TENANT, proxy_type="llm", end_user_session_id="sess-1", http_method="POST",
            endpoint="/v1/chat", success=True, timestamp=datetime.now(timezone.utc),
            gateway_user_id="someone-else", request_body_preview="not alice",
        ))
        # A record this person reached through an agent, and one
        # someone else did.
        for rid, subject in (("ra-alice", str(u.id)), ("ra-bob", "999")):
            s.add(AISystemRecordAccess(
                id=rid, tenant_id=TENANT, ai_system_id="sys", root_task_id="root-1",
                tool="crm_get_customer", scope_key="customer", record_ref="ref",
                verdict="in_scope", mode="audit", subject_id=subject,
                accessed_at=datetime.now(timezone.utc),
            ))
        s.add(AISystemRunFeedback(
            id="fb-1", tenant_id=TENANT, root_task_id="root-1", user_id=str(u.id), score=1,
            comment="great",
        ))
        # CP-9 — three stores that hold the subject's content or a live
        # credential and are NOT reachable from `end_users` by cascade.
        s.add(OAuthToken(
            tenant_id=TENANT, gateway_user_id=str(u.id), idp_user_id="idp-alice",
            mcp_server_id="mcp-1", idp_id="idp-1",
            encrypted_access_token="enc-access", encrypted_refresh_token="enc-refresh",
            expires_at=datetime.now(timezone.utc),
        ))
        s.add(OAuthToken(
            tenant_id=TENANT, gateway_user_id="999", idp_user_id="idp-bob",
            mcp_server_id="mcp-1", idp_id="idp-1",
            encrypted_access_token="enc-bob",
            expires_at=datetime.now(timezone.utc),
        ))
        s.add(ToolApprovalRequest(
            id="tar-1", tenant_id=TENANT, server_config_id="sc-1", server_name="s",
            capability_type="tool", capability_name="send_email",
            requester_type="portal_user", requester_user_id=str(u.id),
            requester_group_id="rg-1", requester_display="Alice <alice@example.com>",
            call_arguments={"to": "doctor@example.com", "body": "my diagnosis"},
            request_metadata={"ip": "10.0.0.9"},
            status="pending", token_used="N", timeout_seconds=300,
            expires_at=datetime.now(timezone.utc),
        ))
        s.add(ToolApprovalRequest(
            id="tar-2", tenant_id=TENANT, server_config_id="sc-1", server_name="s",
            capability_type="tool", capability_name="send_email",
            requester_type="portal_user", requester_user_id="999",
            requester_group_id="rg-1", requester_display="Bob",
            call_arguments={"to": "bob@example.com"},
            status="pending", token_used="N", timeout_seconds=300,
            expires_at=datetime.now(timezone.utc),
        ))
        s.add(BatchJob(
            id="btj-1", tenant_id=TENANT, user_id=u.id, provider="openai",
            model="gpt-4o", endpoint="/v1/chat/completions",
            completion_window="24h", status="completed",
        ))
        s.add(BatchJob(
            id="btj-2", tenant_id=TENANT, user_id=None, provider="openai",
            model="gpt-4o", endpoint="/v1/chat/completions",
            completion_window="24h", status="completed",
        ))
        s.flush()
        s.add(BatchRequest(
            id="btr-1", tenant_id=TENANT, batch_job_id="btj-1", custom_id="c1",
            status="succeeded",
            request_body={"messages": [{"role": "user", "content": "my address is 1 Main St"}]},
            response_body={"choices": [{"message": {"content": "ok"}}]},
        ))
        s.add(BatchRequest(
            id="btr-2", tenant_id=TENANT, batch_job_id="btj-2", custom_id="c2",
            status="succeeded", request_body={"messages": []}, response_body={"x": 1},
        ))
        harness.alice_id = u.id
    harness.seed(seed)
    yield harness
    harness.close()


def test_dry_run_counts_without_changing_anything(h):
    r = h.client.post(
        "/v1/admin/data-erasure",
        json={"end_user_id": h.alice_id, "dry_run": True},
        headers=h.headers(TENANT),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True
    assert body["counts"] == {
        "proxy_logs": 1,
        "usage_logs": 0,
        "run_feedback": 1,
        "record_access": 1,
        "oauth_tokens": 1,
        "tool_approval_requests": 1,
        "batch_requests": 1,
        "end_users": 1,
    }
    db = h.Factory()
    try:
        row = db.get(ProxyLog, "plog-a")
        assert row.request_body_preview == "my address is 1 Main St"
        assert db.query(EndUser).count() == 1
        # Nothing in the three new stores moved either.
        assert db.query(OAuthToken).count() == 2
        assert db.get(ToolApprovalRequest, "tar-1").call_arguments is not None
        assert db.get(BatchRequest, "btr-1").request_body is not None
    finally:
        db.close()


def test_erasure_removes_content_and_identifiers_and_keeps_the_record(h):
    r = h.client.post(
        "/v1/admin/data-erasure",
        json={"email": "alice@example.com"},
        headers=h.headers(TENANT),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["counts"]["proxy_logs"] == 1
    assert body["counts"]["end_users"] == 1
    assert body["subject_kinds"] == ["email"]
    # The event fingerprints the subject; it never stores the email.
    assert "alice" not in r.text.lower().replace("alice_id", "")
    assert len(body["subject_fingerprint"]) == 64

    db = h.Factory()
    try:
        row = db.get(ProxyLog, "plog-a")
        # content + direct identifiers gone
        assert row.request_body_preview is None and row.response_body_preview is None
        assert row.request_headers is None
        assert row.idp_user_email is None and row.idp_user_username is None
        assert row.client_ip is None and row.user_agent is None
        assert row.erased_at is not None
        # record layer + pseudonymous keys kept (subject_id is hash-chained)
        assert row.row_hash == "deadbeef"
        assert row.subject_id == str(h.alice_id)
        assert row.endpoint == "/v1/chat"
        # the neighbour is untouched
        assert db.get(ProxyLog, "plog-b").request_body_preview == "not alice"
        assert db.query(EndUser).count() == 0
        assert db.query(AISystemRunFeedback).count() == 0
    finally:
        db.close()


def test_erasure_reaches_the_stores_end_users_does_not_cascade_to(h):
    """CP-9 — oauth_tokens, approval arguments and batch bodies.

    Each of these survived the a166_data_retention_erasure erasure for a different reason:
    `oauth_tokens` has no FK to `end_users` at all, the approval columns
    are denormalised snapshots, and `batch_jobs.user_id` is
    `ondelete=SET NULL` — so batch content survived *unattributed*,
    which is worse than surviving attributed because a second erasure
    request can no longer find it.
    """
    r = h.client.post(
        "/v1/admin/data-erasure",
        json={"email": "alice@example.com"},
        headers=h.headers(TENANT),
    )
    assert r.status_code == 200, r.text
    counts = r.json()["counts"]
    assert counts["record_access"] == 1
    assert counts["oauth_tokens"] == 1
    assert counts["tool_approval_requests"] == 1
    assert counts["batch_requests"] == 1

    db = h.Factory()
    try:
        # The person's record accesses go; someone else's stay.
        assert db.get(AISystemRecordAccess, "ra-alice") is None
        assert db.get(AISystemRecordAccess, "ra-bob") is not None

        # The credential is gone outright — the row IS the credential.
        remaining = db.query(OAuthToken).all()
        assert len(remaining) == 1 and remaining[0].idp_user_id == "idp-bob"

        # Approval: content + identifier snapshot gone, decision kept.
        tar = db.get(ToolApprovalRequest, "tar-1")
        assert tar is not None
        assert tar.call_arguments is None
        assert tar.request_metadata is None
        assert tar.requester_display is None
        assert tar.status == "pending" and tar.capability_name == "send_email"
        # Someone else's approval is untouched.
        assert db.get(ToolApprovalRequest, "tar-2").call_arguments == {"to": "bob@example.com"}

        # Batch bodies gone, the row (and its cost/token record) kept.
        btr = db.get(BatchRequest, "btr-1")
        assert btr.request_body is None and btr.response_body is None
        assert btr.custom_id == "c1"
        # A batch job belonging to nobody is not swept up.
        assert db.get(BatchRequest, "btr-2").request_body is not None
    finally:
        db.close()


def test_batch_bodies_are_scrubbed_before_the_user_reference_is_nulled(h):
    """Ordering is load-bearing: `batch_jobs.user_id` is SET NULL on the
    end_users delete, so scrubbing after it would find zero rows."""
    r = h.client.post(
        "/v1/admin/data-erasure",
        json={"end_user_id": h.alice_id},
        headers=h.headers(TENANT),
    )
    assert r.status_code == 200, r.text
    assert r.json()["counts"]["batch_requests"] == 1
    db = h.Factory()
    try:
        assert db.get(BatchRequest, "btr-1").request_body is None
    finally:
        db.close()

    listing = h.client.get("/v1/admin/data-erasure", headers=h.headers(TENANT))
    assert listing.status_code == 200
    assert listing.json()["total"] == 1


def test_needs_an_identifier(h):
    r = h.client.post("/v1/admin/data-erasure", json={}, headers=h.headers(TENANT))
    assert r.status_code == 422


def test_tenant_admin_cannot_erase_in_another_tenant(h):
    r = h.client.post(
        "/v1/admin/data-erasure?tenant_id=t2",
        json={"email": "alice@example.com"},
        headers=h.headers(TENANT),
    )
    assert r.status_code == 403
