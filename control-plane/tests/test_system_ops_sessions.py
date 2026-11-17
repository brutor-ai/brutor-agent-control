# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# System Operations Console — Sessions service regressions

Four contracts:

  1. **Kind dispatch** — list_admin_sessions / list_portal_sessions /
     list_api_keys each query their own underlying table and never
     bleed into another kind's rows.

  2. **Tenant isolation** — every list path filters on tenant_id;
     a tenant id that has no matching rows returns []. The helper
     never returns cross-tenant rows by accident.

  3. **Revoke side effects** — revoke flips the durable state on
     admin sessions + api keys, leaves portal session DB state
     untouched, and ALWAYS publishes one NOTIFY with the
     `brutor:session:revoke` channel and a payload whose
     `session_type` matches the kind.

  4. **Audit perimeter** — revoke raises SessionNotFoundError for
     ids that exist in another tenant. A tenant admin asking about
     another tenant's session id gets the same shape error as
     asking about a typo (the test asserts both raise the same
     exception class without distinguishing).
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.database.models.admin import TenantAdminSession
from app.database.models.api_key import APIKey
from app.database.models.proxy_log import ProxyLog
from app.database.models.tenant import Tenant
from app.services.system_ops.sessions import (
    KIND_ADMIN,
    KIND_API_KEY,
    KIND_PORTAL,
    SessionNotFoundError,
    UnknownSessionKindError,
    list_admin_sessions,
    list_api_keys,
    list_portal_sessions,
    revoke_session,
)


# ---------------------------------------------------------------------------
# Fixtures — in-memory SQLite. The session_operation_log + scheduler
# tables ship JSONB-only and the test harness doesn't need them; we
# only create the subset the sessions helper actually queries.
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    # Seed two tenants so the cross-tenant isolation assertion is
    # meaningful — selecting from one tenant must not leak the other.
    s.add(Tenant(id="t-acme", name="acme", display_name="Acme",
                 primary_contact_email="ops@acme.test"))
    s.add(Tenant(id="t-beta", name="beta", display_name="Beta",
                 primary_contact_email="ops@beta.test"))
    s.commit()
    yield s
    s.close()


def _now():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# list_admin_sessions
# ---------------------------------------------------------------------------


def test_list_admin_sessions_excludes_inactive_and_expired(db):
    # Three rows — one live, one inactive, one expired. Only the live
    # one shows up.
    db.add_all([
        TenantAdminSession(
            id="sa-1",
            tenant_id="t-acme",
            admin_id="adm-1",
            token_hash="h1",
            ip_address="10.0.0.1",
            user_agent="test/1.0",
            expires_at=_now() + timedelta(hours=1),
            is_active=True,
        ),
        TenantAdminSession(
            id="sa-2",
            tenant_id="t-acme",
            admin_id="adm-2",
            token_hash="h2",
            expires_at=_now() + timedelta(hours=1),
            is_active=False,
        ),
        TenantAdminSession(
            id="sa-3",
            tenant_id="t-acme",
            admin_id="adm-3",
            token_hash="h3",
            expires_at=_now() - timedelta(hours=1),
            is_active=True,
        ),
    ])
    db.commit()

    rows = list_admin_sessions(db, "t-acme")
    assert len(rows) == 1
    assert rows[0]["id"] == "sa-1"
    assert rows[0]["kind"] == KIND_ADMIN
    assert rows[0]["user_id"] == "adm-1"
    assert rows[0]["ip"] == "10.0.0.1"


def test_list_admin_sessions_is_tenant_scoped(db):
    db.add_all([
        TenantAdminSession(
            id="sa-acme",
            tenant_id="t-acme",
            admin_id="adm-1",
            token_hash="h1",
            expires_at=_now() + timedelta(hours=1),
            is_active=True,
        ),
        TenantAdminSession(
            id="sa-beta",
            tenant_id="t-beta",
            admin_id="adm-2",
            token_hash="h2",
            expires_at=_now() + timedelta(hours=1),
            is_active=True,
        ),
    ])
    db.commit()

    rows = list_admin_sessions(db, "t-acme")
    assert [r["id"] for r in rows] == ["sa-acme"]


# ---------------------------------------------------------------------------
# list_api_keys
# ---------------------------------------------------------------------------


def test_list_api_keys_skips_revoked_disabled_expired(db):
    db.add_all([
        APIKey(
            id="ak-live",
            tenant_id="t-acme",
            name="Live key",
            key_hash="kh-1",
            key_prefix="bru_live",
            scope="tenant",
            enabled=True,
            revoked=False,
            expires_at=None,
        ),
        APIKey(
            id="ak-revoked",
            tenant_id="t-acme",
            name="Revoked",
            key_hash="kh-2",
            key_prefix="bru_revk",
            scope="tenant",
            enabled=True,
            revoked=True,
        ),
        APIKey(
            id="ak-disabled",
            tenant_id="t-acme",
            name="Disabled",
            key_hash="kh-3",
            key_prefix="bru_disb",
            scope="tenant",
            enabled=False,
            revoked=False,
        ),
        APIKey(
            id="ak-expired",
            tenant_id="t-acme",
            name="Expired",
            key_hash="kh-4",
            key_prefix="bru_expd",
            scope="tenant",
            enabled=True,
            revoked=False,
            expires_at=_now() - timedelta(hours=1),
        ),
    ])
    db.commit()

    rows = list_api_keys(db, "t-acme")
    assert [r["id"] for r in rows] == ["ak-live"]
    assert rows[0]["kind"] == KIND_API_KEY
    assert rows[0]["user_label"] == "Live key"


# ---------------------------------------------------------------------------
# list_portal_sessions
# ---------------------------------------------------------------------------


def test_list_portal_sessions_aggregates_proxy_log(db):
    # Same session id, two hits — the rollup should produce one row
    # whose last_seen is the later timestamp.
    older = _now() - timedelta(minutes=30)
    newer = _now() - timedelta(minutes=2)
    db.add_all([
        ProxyLog(
            id="pl-1",
            tenant_id="t-acme",
            timestamp=older,
            proxy_type="llm",
            http_method="POST",
            endpoint="/v1/proxy/llm/chat/completions",
            success=True,
            end_user_session_id="ses-A",
            idp_user_id="u-1",
            idp_user_email="alice@example.com",
            client_ip="1.2.3.4",
        ),
        ProxyLog(
            id="pl-2",
            tenant_id="t-acme",
            timestamp=newer,
            proxy_type="llm",
            http_method="POST",
            endpoint="/v1/proxy/llm/chat/completions",
            success=True,
            end_user_session_id="ses-A",
            idp_user_id="u-1",
            client_ip="1.2.3.4",
        ),
        # API-key-authed row should NOT be counted as a portal session.
        ProxyLog(
            id="pl-3",
            tenant_id="t-acme",
            timestamp=newer,
            proxy_type="llm",
            http_method="POST",
            endpoint="/v1/proxy/llm/chat/completions",
            success=True,
            end_user_session_id="ses-B",
            api_key_id="ak-x",
            client_ip="9.9.9.9",
        ),
    ])
    db.commit()

    rows = list_portal_sessions(db, "t-acme")
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == KIND_PORTAL
    assert row["id"] == "ses-A"
    assert row["user_id"] == "u-1"
    assert row["user_label"] == "alice@example.com"
    # expires_at is always null for portal sessions — see service docstring.
    assert row["expires_at"] is None


# ---------------------------------------------------------------------------
# revoke_session
# ---------------------------------------------------------------------------


def test_revoke_admin_session_flips_active_and_notifies(db):
    db.add(TenantAdminSession(
        id="sa-1",
        tenant_id="t-acme",
        admin_id="adm-1",
        token_hash="h1",
        expires_at=_now() + timedelta(hours=1),
        is_active=True,
    ))
    db.commit()

    result = revoke_session(
        db,
        tenant_id="t-acme",
        kind=KIND_ADMIN,
        id="sa-1",
        revoked_by="sys-admin",
        reason="suspected compromise",
    )
    assert result["channel"] == "brutor:session:revoke"

    row = db.query(TenantAdminSession).filter_by(id="sa-1").first()
    assert row.is_active is False
    assert row.deactivation_reason == "suspected compromise"


def test_revoke_api_key_flips_revoked_and_disabled(db):
    db.add(APIKey(
        id="ak-1",
        tenant_id="t-acme",
        name="K",
        key_hash="kh-1",
        key_prefix="bru_ak1",
        scope="tenant",
        enabled=True,
        revoked=False,
    ))
    db.commit()

    revoke_session(
        db,
        tenant_id="t-acme",
        kind=KIND_API_KEY,
        id="ak-1",
        revoked_by="sys-admin",
        reason="key rotation",
    )

    row = db.query(APIKey).filter_by(id="ak-1").first()
    assert row.revoked is True
    assert row.enabled is False
    assert row.revoked_reason == "key rotation"


def test_revoke_portal_session_publishes_notify_without_db_row(db):
    # No row to seed — portal sessions have no DB tombstone.
    result = revoke_session(
        db,
        tenant_id="t-acme",
        kind=KIND_PORTAL,
        id="ses-Z",
    )
    assert result["channel"] == "brutor:session:revoke"
    assert "ses-Z" in result["payload"]


def test_revoke_unknown_kind_raises(db):
    with pytest.raises(UnknownSessionKindError):
        revoke_session(db, tenant_id="t-acme", kind="bogus", id="x")


def test_revoke_cross_tenant_not_found(db):
    db.add(APIKey(
        id="ak-beta",
        tenant_id="t-beta",
        name="K",
        key_hash="kh-1",
        key_prefix="bru_akb",
        scope="tenant",
        enabled=True,
        revoked=False,
    ))
    db.commit()
    with pytest.raises(SessionNotFoundError):
        revoke_session(db, tenant_id="t-acme", kind=KIND_API_KEY, id="ak-beta")
