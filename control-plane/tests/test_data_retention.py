# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Data retention — policy and sweeper

The behaviours worth pinning are the ones that protect evidence:

  * a body window longer than the log window is rejected — rows would be
    deleted before their bodies were due;
  * the body pass removes content and marks the row, and never touches the
    record layer;
  * the row pass DECLARES what it deletes: one prune record per chain with
    the exact sequence runs and tail hashes, written in the same transaction;
  * a malformed policy enforces nothing rather than everything.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.api.v1.models.admin.retention import DataRetentionPolicy
from app.core.logging_system import BrutorLogger
from app.database.models.ai_system_run import AISystemRun
from app.database.models.ai_system_run_feedback import AISystemRunFeedback
from app.database.models.audit_chain_prune import AuditChainPrune, REASON_RETENTION
from app.database.models.proxy_log import ProxyLog
from app.database.models.tenant import Tenant
from app.services.data_retention.policy import effective_policy, retention_disclosure
from app.database.models.batch_job import BatchJob
from app.database.models.batch_request import BatchRequest
from app.services.data_retention.sweeper import (
    _runs_from_seqs,
    prune_log_rows,
    prune_run_rows,
    scrub_batch_bodies,
    scrub_bodies,
    sweep_tenant,
)
from tests.admin_endpoint_harness import build_admin_harness

TENANT = "t1"


@pytest.fixture()
def h():
    harness = build_admin_harness([], logger_name="test-retention")
    yield harness
    harness.close()


def _log(tenant, *, age_days, chain=None, seq=None, body="hello", **over):
    now = datetime.now(timezone.utc)
    v = dict(
        id=f"plog-{uuid.uuid4().hex[:12]}",
        tenant_id=tenant,
        proxy_type="llm",
        end_user_session_id="sess-1",
        http_method="POST",
        endpoint="/v1/chat",
        success=True,
        timestamp=now - timedelta(days=age_days),
        created_at=now - timedelta(days=age_days),
        request_body_preview=body,
        response_body_preview=body and body.upper(),
        request_headers='{"x": "y"}',
        chain_id=chain,
        chain_seq=seq,
        row_hash=f"h{seq}" if seq is not None else None,
    )
    v.update(over)
    return ProxyLog(**v)


# --- policy ---------------------------------------------------------------

def test_body_window_may_not_exceed_log_window():
    with pytest.raises(ValidationError):
        DataRetentionPolicy(body_days=90, log_days=30)
    DataRetentionPolicy(body_days=30, log_days=90)  # fine


def test_malformed_policy_enforces_nothing():
    t = Tenant(id="tenant-x", name="tenant-x")
    t.data_retention = {"store_bodies": "shred", "log_days": -4}
    assert effective_policy(t) is None


def test_empty_policy_is_no_policy():
    t = Tenant(id="tenant-x", name="tenant-x")
    t.data_retention = {"store_bodies": "redacted"}
    assert effective_policy(t) is None  # store_bodies alone is the proxy's business


def test_disclosure_names_the_windows():
    t = Tenant(id="tenant-x", name="tenant-x")
    t.data_retention = {"run_days": 90, "log_days": 180, "body_days": 30}
    text = " ".join(retention_disclosure(t))
    assert "90 days" in text and "180 days" in text and "30 days" in text


# --- runs helper -----------------------------------------------------------

def test_runs_collapse_contiguous_sequences():
    runs = _runs_from_seqs([(5, "h5"), (3, "h3"), (4, "h4"), (9, "h9")])
    assert runs == [
        {"from": 3, "to": 5, "tail_hash": "h5"},
        {"from": 9, "to": 9, "tail_hash": "h9"},
    ]


# --- sweeper ---------------------------------------------------------------

def test_body_pass_removes_content_and_keeps_the_record(h):
    def seed(s):
        s.add(_log(TENANT, age_days=40, chain="c1", seq=1))
        s.add(_log(TENANT, age_days=1, chain="c1", seq=2))
        s.add(_log("t2", age_days=40, chain="c1", seq=3))
    h.seed(seed)
    db = h.Factory()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        assert scrub_bodies(db, TENANT, cutoff) == 1
        db.commit()
        old = db.query(ProxyLog).filter(ProxyLog.chain_seq == 1).one()
        assert old.request_body_preview is None and old.response_body_preview is None
        assert old.request_headers is None
        assert old.bodies_pruned_at is not None
        assert old.row_hash == "h1"  # record layer untouched
        fresh = db.query(ProxyLog).filter(ProxyLog.chain_seq == 2).one()
        assert fresh.request_body_preview == "hello"
        other = db.query(ProxyLog).filter(ProxyLog.chain_seq == 3).one()
        assert other.request_body_preview == "hello"  # other tenant untouched
        # Idempotent: a second pass finds nothing.
        assert scrub_bodies(db, TENANT, cutoff) == 0
    finally:
        db.close()


def test_row_pass_declares_every_deleted_sequence(h):
    def seed(s):
        for seq in (10, 11, 12, 14):
            s.add(_log(TENANT, age_days=100, chain="c1", seq=seq))
        s.add(_log("t2", age_days=100, chain="c1", seq=13))   # interleaved tenant
        s.add(_log(TENANT, age_days=100, chain="c2", seq=1))
        s.add(_log(TENANT, age_days=1, chain="c1", seq=15))
        s.add(_log(TENANT, age_days=100, chain=None, seq=None))  # pre-a143 row
    h.seed(seed)
    db = h.Factory()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        deleted, records = prune_log_rows(db, TENANT, cutoff)
        db.commit()
        assert deleted == 6          # 4 on c1 + 1 on c2 + 1 unchained
        assert records == 2          # one declaration per chain touched
        c1 = db.query(AuditChainPrune).filter(AuditChainPrune.chain_id == "c1").one()
        assert c1.reason == REASON_RETENTION
        assert c1.tenant_id == TENANT
        assert c1.row_count == 4
        assert c1.runs == [
            {"from": 10, "to": 12, "tail_hash": "h12"},
            {"from": 14, "to": 14, "tail_hash": "h14"},
        ]
        assert (c1.seq_min, c1.seq_max) == (10, 14)
        # t2's row 13 and the fresh row 15 survive.
        left = {r.chain_seq for r in db.query(ProxyLog).filter(ProxyLog.chain_id == "c1")}
        assert left == {13, 15}
    finally:
        db.close()


def test_run_pass_takes_feedback_with_it(h):
    now = datetime.now(timezone.utc)

    def seed(s):
        for i, age in enumerate((100, 100, 1)):
            s.add(AISystemRun(
                id=f"run-{i}", tenant_id=TENANT, root_task_id=f"root-{i}",
                ai_system_id="group-default-t1", started_at=now - timedelta(days=age),
                terminal_state="completed", finalized_by="explicit", is_replay=False,
            ))
            s.add(AISystemRunFeedback(
                id=f"fb-{i}", tenant_id=TENANT, root_task_id=f"root-{i}",
                user_id="u1", score=1,
            ))
    h.seed(seed)
    db = h.Factory()
    try:
        runs, fb = prune_run_rows(db, TENANT, now - timedelta(days=30))
        db.commit()
        assert (runs, fb) == (2, 2)
        assert db.query(AISystemRun).count() == 1
        assert db.query(AISystemRunFeedback).count() == 1
    finally:
        db.close()


def test_sweep_tenant_runs_every_pass_and_reports(h):
    def seed(s):
        s.add(_log(TENANT, age_days=45, chain="c1", seq=1))   # bodies only
        s.add(_log(TENANT, age_days=400, chain="c1", seq=2))  # whole row
        t = s.query(Tenant).filter(Tenant.id == TENANT).one()
        t.data_retention = {"store_bodies": "full", "body_days": 30, "log_days": 365}
    h.seed(seed)
    db = h.Factory()
    try:
        tenant = db.query(Tenant).filter(Tenant.id == TENANT).one()
        result = sweep_tenant(db, tenant, effective_policy(tenant))
        assert result.errors == []
        assert result.bodies_scrubbed == 2   # both rows are past the body window
        assert result.logs_deleted == 1
        assert result.prune_records == 1
        assert db.query(ProxyLog).filter(ProxyLog.tenant_id == TENANT).count() == 1
    finally:
        db.close()


# --- CP-13: a pass must drain, not tick ------------------------------------

def test_sweep_drains_a_backlog_within_one_tick(h):
    """A pass that ran once per tick capped drainage at BATCH_SIZE rows per
    tenant per hour. With batch=2 and 7 doomed rows, one tick must clear
    all seven, not two."""
    def seed(s):
        for seq in range(1, 8):
            s.add(_log(TENANT, age_days=400, chain="c1", seq=seq))
        t = s.query(Tenant).filter(Tenant.id == TENANT).one()
        t.data_retention = {"store_bodies": "full", "body_days": 30, "log_days": 365}
    h.seed(seed)
    db = h.Factory()
    try:
        tenant = db.query(Tenant).filter(Tenant.id == TENANT).one()
        result = sweep_tenant(db, tenant, effective_policy(tenant), batch=2)
        assert result.logs_deleted == 7
        assert result.unconverged == []
        assert db.query(ProxyLog).filter(ProxyLog.tenant_id == TENANT).count() == 0
    finally:
        db.close()


def test_pass_that_spends_its_budget_is_reported_unconverged(h, monkeypatch):
    """When the budget runs out with rows still older than the cutoff, the
    declared window is not being honoured — say so rather than reporting a
    healthy-looking row count."""
    from app.services.data_retention import sweeper as sweeper_mod

    monkeypatch.setattr(sweeper_mod, "MAX_BATCHES_PER_PASS", 2)

    def seed(s):
        for seq in range(1, 8):
            s.add(_log(TENANT, age_days=400, chain="c1", seq=seq))
        t = s.query(Tenant).filter(Tenant.id == TENANT).one()
        t.data_retention = {"store_bodies": "full", "body_days": 30, "log_days": 365}
    h.seed(seed)
    db = h.Factory()
    try:
        tenant = db.query(Tenant).filter(Tenant.id == TENANT).one()
        result = sweep_tenant(db, tenant, effective_policy(tenant), batch=2)
        assert result.logs_deleted == 4          # 2 batches of 2, then stop
        assert "logs" in result.unconverged
        assert db.query(ProxyLog).filter(ProxyLog.tenant_id == TENANT).count() == 3
    finally:
        db.close()


def test_drain_commits_each_batch(h):
    """A budget cut-off must leave the work already done durable, so the
    next tick resumes rather than repeats."""
    from app.services.data_retention.sweeper import _drain

    calls = {"steps": 0, "commits": 0}

    def step():
        calls["steps"] += 1
        return (2, 1) if calls["steps"] < 3 else (1, 0)

    def commit():
        calls["commits"] += 1

    totals, converged = _drain(step, batch=2, commit=commit)
    assert totals == [5, 2]
    assert converged is True
    assert calls["commits"] == 3


# --- CP-16: batch content is under body_days too ---------------------------

def _batch(session, tenant, *, age_days, n=1):
    """One batch job with `n` request rows carrying content."""
    now = datetime.now(timezone.utc)
    job = BatchJob(
        id=f"btj-{uuid.uuid4().hex[:12]}", tenant_id=tenant,
        provider="openai", model="gpt-5", endpoint="/v1/chat/completions",
        completion_window="24h", status="completed",
        created_at=now - timedelta(days=age_days),
    )
    session.add(job)
    for i in range(n):
        session.add(BatchRequest(
            id=f"btr-{uuid.uuid4().hex[:12]}", tenant_id=tenant,
            batch_job_id=job.id, custom_id=f"c{i}", status="succeeded",
            request_body={"messages": [{"role": "user", "content": "secret"}]},
            response_body={"choices": [{"message": {"content": "answer"}}]},
            created_at=now - timedelta(days=age_days),
        ))
    return job


def test_batch_bodies_are_scrubbed_by_the_body_window(h):
    """The data-handling page (https://docs.brutor.ai/governance/data-handling/) tells a DPO that `body_days` governs stored
    content. `batch_requests` holds the same class of content on another
    table and used to be reached by no retention pass at all."""
    def seed(s):
        _batch(s, TENANT, age_days=40, n=2)
        _batch(s, TENANT, age_days=1)
        _batch(s, "t2", age_days=40)
    h.seed(seed)
    db = h.Factory()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        assert scrub_batch_bodies(db, TENANT, cutoff) == 2
        db.commit()
        scrubbed = db.query(BatchRequest).filter(
            BatchRequest.tenant_id == TENANT,
            BatchRequest.request_body.is_(None),
        ).count()
        assert scrubbed == 2
        # Fresh rows and the other tenant keep their content.
        assert db.query(BatchRequest).filter(
            BatchRequest.request_body.isnot(None)
        ).count() == 2
        # Idempotent.
        assert scrub_batch_bodies(db, TENANT, cutoff) == 0
    finally:
        db.close()


def test_sweep_tenant_covers_batch_bodies(h):
    def seed(s):
        _batch(s, TENANT, age_days=40)
        t = s.query(Tenant).filter(Tenant.id == TENANT).one()
        t.data_retention = {"store_bodies": "full", "body_days": 30, "log_days": 365}
    h.seed(seed)
    db = h.Factory()
    try:
        tenant = db.query(Tenant).filter(Tenant.id == TENANT).one()
        result = sweep_tenant(db, tenant, effective_policy(tenant))
        assert result.errors == []
        assert result.batch_bodies_scrubbed == 1
        row = db.query(BatchRequest).one()
        assert row.request_body is None and row.response_body is None
        # The record layer survives — this is a content pass, not a delete.
        assert row.custom_id == "c0" and row.status == "succeeded"
    finally:
        db.close()
