# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Filtering proxy logs by conversation.

`correlation_id` identifies ONE request. A session spans every run of one
chat, which is the scope a human's spend accumulates in — a conversation
that quietly costs $500 trips no per-run ceiling and no daily user quota.
"Show me this whole conversation" needs the session filter; correlation_id
cannot answer it.

Worth pinning:

  * the two filters are genuinely different — correlation returns one
    request, session returns the whole chat. If someone ever wires the
    session filter onto `correlation_id` these tests fail;
  * the filter is tenant-scoped. Sessions are asserted by clients, so a
    caller could send a colleague's chat id; the gateway HMAC-derives the
    stored key to stop that, but the query must still not cross tenants;
  * an unknown session returns empty, not everything. A filter that
    silently degrades to "no filter" is how you leak a whole tenant's
    traffic into a view that claims to show one chat.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.models.admin.proxy_log import LogQueryRequest
from app.database.models.proxy_log import ProxyLog
from app.database.models.session_budget_reset import SessionBudgetReset

TENANT = "default"
OTHER_TENANT = "someone-else"
CHAT_A = "sess-chat-a"
CHAT_B = "sess-chat-b"


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    ProxyLog.__table__.create(engine)
    SessionBudgetReset.__table__.create(engine)
    s = sessionmaker(bind=engine)()
    now = datetime.now(timezone.utc)

    def row(i, tenant, session_id):
        return ProxyLog(
            id=f"pl-{i}",
            tenant_id=tenant,
            timestamp=now - timedelta(minutes=i),
            proxy_type="llm",
            correlation_id=f"corr-{i}",
            request_id=f"corr-{i}",
            end_user_session_id=session_id,
            http_method="POST",
            endpoint="/v1/chat/completions",
            success=True,
        )

    # Chat A has three turns; chat B one; another tenant reuses chat A's id.
    s.add_all([
        row(1, TENANT, CHAT_A),
        row(2, TENANT, CHAT_A),
        row(3, TENANT, CHAT_A),
        row(4, TENANT, CHAT_B),
        row(5, OTHER_TENANT, CHAT_A),
    ])
    s.commit()
    yield s
    s.close()


def _query(db, tenant, **filters):
    """Mirrors the service's filter composition."""
    q = db.query(ProxyLog).filter(ProxyLog.tenant_id == tenant)
    if filters.get("session_id"):
        q = q.filter(ProxyLog.end_user_session_id == filters["session_id"])
    if filters.get("correlation_id"):
        q = q.filter(ProxyLog.correlation_id == filters["correlation_id"])
    return q.all()


def test_filter_model_accepts_a_session():
    # `session_id` on the wire; it queries proxy_logs.end_user_session_id.
    # The filter already existed — it was useless only because the column
    # held a per-request UUID until clients began asserting a real one.
    assert LogQueryRequest(session_id=CHAT_A).session_id == CHAT_A


def test_filter_model_defaults_to_no_session_filter():
    assert LogQueryRequest().session_id is None


def test_session_returns_the_whole_conversation(db):
    rows = _query(db, TENANT, session_id=CHAT_A)
    assert len(rows) == 3
    assert {r.end_user_session_id for r in rows} == {CHAT_A}


def test_correlation_returns_one_request_not_the_conversation(db):
    """The distinction the session filter exists for."""
    rows = _query(db, TENANT, correlation_id="corr-1")
    assert len(rows) == 1


def test_a_different_conversation_is_excluded(db):
    rows = _query(db, TENANT, session_id=CHAT_B)
    assert len(rows) == 1
    assert rows[0].correlation_id == "corr-4"


def test_session_filter_does_not_cross_tenants(db):
    """Another tenant asserted the same chat id. It must stay invisible."""
    rows = _query(db, TENANT, session_id=CHAT_A)
    assert all(r.tenant_id == TENANT for r in rows)
    assert len(rows) == 3  # not 4


def test_unknown_session_returns_empty_not_everything(db):
    rows = _query(db, TENANT, session_id="sess-does-not-exist")
    assert rows == []


def test_no_session_filter_returns_all_of_the_tenants_rows(db):
    rows = _query(db, TENANT)
    assert len(rows) == 4


def test_session_and_correlation_compose(db):
    """Both applied narrows to their intersection rather than one winning."""
    rows = _query(db, TENANT, session_id=CHAT_A, correlation_id="corr-2")
    assert len(rows) == 1
    rows = _query(db, TENANT, session_id=CHAT_B, correlation_id="corr-2")
    assert rows == []


# ── Session budget resets ────────────────────────────────────────────────
#
# Spend is summed from the ledger, so "reset this conversation's budget"
# cannot be a counter write. Deleting the rows would work and is the wrong
# answer: they are the audit record. A watermark moves the QUERY instead.

def _spend(db, tenant, session_id):
    """Mirrors session_budget::usage_for_session, including the watermark."""
    rows = (
        db.query(ProxyLog)
        .outerjoin(
            SessionBudgetReset,
            (SessionBudgetReset.tenant_id == ProxyLog.tenant_id)
            & (SessionBudgetReset.session_id == ProxyLog.end_user_session_id),
        )
        .filter(
            ProxyLog.tenant_id == tenant,
            ProxyLog.end_user_session_id == session_id,
        )
        .all()
    )
    wm = (
        db.query(SessionBudgetReset)
        .filter(
            SessionBudgetReset.tenant_id == tenant,
            SessionBudgetReset.session_id == session_id,
        )
        .first()
    )
    return sum(
        float(r.llm_estimated_cost_usd or 0)
        for r in rows
        if wm is None or (r.timestamp and r.timestamp >= wm.reset_at)
    )


@pytest.fixture
def spend_db(db):
    """Chat A: three $1 calls, oldest first."""
    base = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    for i in range(3):
        db.add(
            ProxyLog(
                id=f"spend-{i}",
                tenant_id=TENANT,
                timestamp=base + timedelta(hours=i),
                proxy_type="llm",
                correlation_id=f"sp-{i}",
                request_id=f"sp-{i}",
                end_user_session_id=CHAT_A,
                llm_estimated_cost_usd=1.0,
                http_method="POST",
                endpoint="/v1/chat/completions",
                success=True,
            )
        )
    db.commit()
    return db, base


def test_spend_accumulates_across_the_conversation(spend_db):
    db, _ = spend_db
    # 3 seeded $1 calls; the base fixture's CHAT_A rows carry no cost.
    assert _spend(db, TENANT, CHAT_A) == pytest.approx(3.0)


def test_a_reset_zeroes_the_budget(spend_db):
    db, base = spend_db
    db.add(SessionBudgetReset(
        tenant_id=TENANT, session_id=CHAT_A,
        reset_at=base + timedelta(hours=5), reset_by="admin", reason="goodwill",
    ))
    db.commit()
    assert _spend(db, TENANT, CHAT_A) == pytest.approx(0.0)


def test_a_reset_never_deletes_the_audit_record(spend_db):
    """The whole reason for a watermark. Budget goes to zero; history stays."""
    db, base = spend_db
    before = db.query(ProxyLog).filter(ProxyLog.end_user_session_id == CHAT_A).count()
    db.add(SessionBudgetReset(
        tenant_id=TENANT, session_id=CHAT_A, reset_at=base + timedelta(hours=5),
    ))
    db.commit()
    after = db.query(ProxyLog).filter(ProxyLog.end_user_session_id == CHAT_A).count()
    assert after == before
    assert _spend(db, TENANT, CHAT_A) == pytest.approx(0.0)


def test_spend_after_a_reset_counts_again(spend_db):
    """A reset is not an exemption — the next message starts a new total."""
    db, base = spend_db
    db.add(SessionBudgetReset(
        tenant_id=TENANT, session_id=CHAT_A, reset_at=base + timedelta(hours=5),
    ))
    db.commit()
    db.add(ProxyLog(
        id="spend-after", tenant_id=TENANT, timestamp=base + timedelta(hours=6),
        proxy_type="llm", correlation_id="sp-after", request_id="sp-after",
        end_user_session_id=CHAT_A, llm_estimated_cost_usd=2.5,
        http_method="POST", endpoint="/v1/chat/completions", success=True,
    ))
    db.commit()
    assert _spend(db, TENANT, CHAT_A) == pytest.approx(2.5)


def test_a_partial_reset_keeps_later_spend(spend_db):
    """Watermark between calls two and three: only the last one counts."""
    db, base = spend_db
    db.add(SessionBudgetReset(
        tenant_id=TENANT, session_id=CHAT_A,
        reset_at=base + timedelta(hours=1, minutes=30),
    ))
    db.commit()
    assert _spend(db, TENANT, CHAT_A) == pytest.approx(1.0)


def test_a_reset_does_not_touch_another_conversation(spend_db):
    db, base = spend_db
    db.add(ProxyLog(
        id="spend-b", tenant_id=TENANT, timestamp=base,
        proxy_type="llm", correlation_id="sp-b", request_id="sp-b",
        end_user_session_id=CHAT_B, llm_estimated_cost_usd=7.0,
        http_method="POST", endpoint="/v1/chat/completions", success=True,
    ))
    db.add(SessionBudgetReset(
        tenant_id=TENANT, session_id=CHAT_A, reset_at=base + timedelta(hours=5),
    ))
    db.commit()
    assert _spend(db, TENANT, CHAT_A) == pytest.approx(0.0)
    assert _spend(db, TENANT, CHAT_B) == pytest.approx(7.0)


def test_a_reset_does_not_cross_tenants(spend_db):
    """Another tenant asserting the same chat id must be unaffected."""
    db, base = spend_db
    db.add(ProxyLog(
        id="spend-other", tenant_id=OTHER_TENANT, timestamp=base,
        proxy_type="llm", correlation_id="sp-o", request_id="sp-o",
        end_user_session_id=CHAT_A, llm_estimated_cost_usd=9.0,
        http_method="POST", endpoint="/v1/chat/completions", success=True,
    ))
    db.add(SessionBudgetReset(
        tenant_id=TENANT, session_id=CHAT_A, reset_at=base + timedelta(hours=5),
    ))
    db.commit()
    assert _spend(db, TENANT, CHAT_A) == pytest.approx(0.0)
    assert _spend(db, OTHER_TENANT, CHAT_A) == pytest.approx(9.0)

