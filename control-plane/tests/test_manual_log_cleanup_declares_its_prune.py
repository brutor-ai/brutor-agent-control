# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
A one-off cleanup of proxy logs declares what it deleted.

The audit chain numbers every row. Rows that are deleted leave a hole in
the numbering, and the verifier reports a hole nobody declared as a gap:
evidence of tampering. The scheduled retention sweep writes a prune
declaration for what it deletes; the administrator's one-off cleanup
deletes the same rows and writes the same declaration.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.logging_system import BrutorLogger
from app.database import Base
from app.database.models.audit_chain_prune import REASON_MANUAL, AuditChainPrune
from app.database.models.proxy_log import ProxyLog
from app.database.models.tenant import Tenant
from app.services.proxy_log_service import UnifiedProxyLogService


@pytest.fixture
def db():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine, tables=[Tenant.__table__, ProxyLog.__table__, AuditChainPrune.__table__])
    session = sessionmaker(bind=engine, future=True)()
    for tenant in ("tenant-a", "tenant-b"):
        session.add(Tenant(id=tenant, name=tenant, display_name=tenant, primary_contact_email="a@a.example"))
    session.flush()
    yield session
    session.close()
    engine.dispose()


def _log(db, tenant, seq, age_days):
    db.add(ProxyLog(
        id=f"{tenant}-{seq}", tenant_id=tenant, timestamp=datetime.now(timezone.utc) - timedelta(days=age_days),
        proxy_type="mcp", proxy_subtype="mcp_jsonrpc", end_user_session_id="s", http_method="POST", endpoint="/x",
        http_status_code=200, success=True, chain_id=f"chain-{tenant}", chain_seq=seq, row_hash=f"hash-{tenant}-{seq}",
    ))


def _cleanup(db, tenant, days=30, actor="admin-1"):
    service = UnifiedProxyLogService(BrutorLogger("t"))
    return asyncio.run(service.cleanup_old_logs(db=db, tenant_id=tenant, days_to_keep=days, actor=actor))


def test_the_deleted_rows_are_declared(db):
    for seq in (1, 2, 3):
        _log(db, "tenant-a", seq, age_days=100)
    _log(db, "tenant-a", 4, age_days=1)
    _log(db, "tenant-b", 1, age_days=100)
    db.flush()

    assert _cleanup(db, "tenant-a") == 3

    (prune,) = db.query(AuditChainPrune).all()
    assert (prune.chain_id, prune.tenant_id, prune.seq_min, prune.seq_max, prune.row_count) == (
        "chain-tenant-a", "tenant-a", 1, 3, 3,
    )
    assert prune.reason == REASON_MANUAL and prune.actor == "admin-1"
    assert prune.runs == [{"from": 1, "to": 3, "tail_hash": "hash-tenant-a-3"}]
    assert sorted(row.id for row in db.query(ProxyLog).all()) == ["tenant-a-4", "tenant-b-1"]


def test_nothing_deleted_declares_nothing(db):
    _log(db, "tenant-a", 1, age_days=1)
    db.flush()
    assert _cleanup(db, "tenant-a") == 0
    assert db.query(AuditChainPrune).count() == 0


def test_a_cleanup_of_every_tenant_declares_each_tenants_rows_under_that_tenant(db):
    _log(db, "tenant-a", 1, age_days=100)
    _log(db, "tenant-b", 7, age_days=100)
    db.flush()

    assert _cleanup(db, None) == 2

    declared = {(p.tenant_id, p.chain_id, p.seq_min, p.seq_max) for p in db.query(AuditChainPrune).all()}
    assert declared == {("tenant-a", "chain-tenant-a", 1, 1), ("tenant-b", "chain-tenant-b", 7, 7)}
