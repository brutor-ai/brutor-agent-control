# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
End-to-end tests for the A2A Agent Card admin router.

Coverage:
  * Feature-flag gating — tenants with `features.a2a = False` see 404
    on every A2A endpoint (the surface is invisible, not 403)
  * RBAC — tenant admin in T1 cannot read/create cards in T2 (tenant
    isolation)
  * Full CRUD happy path + signing round-trip
  * Revocation is terminal — revoked card can be re-revoked (idempotent)
    but cannot be re-signed or updated
  * Body update invalidates signature (trust_tier → unsigned)
  * Unique-name-per-tenant guard returns 409
  * Tenant public key endpoint lazy-creates the key on first read

The harness follows the same pattern as test_admin_rbac_endpoints —
in-memory SQLite, FastAPI TestClient, seeded system admin + two
tenants with full-permission admin roles. Keeps the tests decoupled
from Postgres so they run in CI without a DB.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import uuid
from typing import Any, Dict, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.unified_auth_service import UnifiedAuthService
from app.core.logging_system import BrutorLogger
from app.api.v1.endpoints.admin.admin_rbac import create_admin_users_router
from app.api.v1.endpoints.admin.agent_cards import create_agent_cards_router
from app.database import Base, get_db
from app.database.models.admin import (
    SystemAdmin,
    SystemAdminAuditLog,
    SystemAdminSession,
    TenantAdmin,
    TenantAdminAuditLog,
    TenantAdminSession,
    TenantRole,
    TenantRolePermission,
)
from app.database.models.agent_card import (
    AgentCard,
    TRUST_TIER_REVOKED,
    TRUST_TIER_TENANT_SIGNED,
    TRUST_TIER_UNSIGNED,
)
from app.database.models.agent_card_capability import AgentCardCapability
from app.database.models.end_user import EndUser
from app.database.models.end_user_group import (
    EndUserGroup,
    end_user_group_members,
    resource_group_end_user_groups,
)
from app.database.models.idp_server import IdPServer
from app.database.models.llm_catalog import LlmCatalog
from app.database.models.llm_model import LLModel
from app.database.models.resource_group import ResourceGroup, resource_group_agent_cards
from app.database.models.tenant import Tenant
from app.database.models.tenant_a2a_key import TenantA2AKey
from tests.app_factory import harness_extras


JWT_SECRET = os.environ["JWT_SECRET"]


REQUIRED_TABLES = [
    LlmCatalog.__table__,           # LLModel.catalog_id FKs here
    LLModel.__table__,              # Tenant.default_embedding_model_id FKs here
    Tenant.__table__,
    TenantRole.__table__,
    TenantRolePermission.__table__,
    SystemAdmin.__table__,
    TenantAdmin.__table__,
    # TenantAdmin.idp_server_id FKs into idp_servers.
    # SQLite enforces this FK on insert when foreign_keys=ON, so
    # the target table must exist even when no test ever inserts
    # an IdP row.
    IdPServer.__table__,
    EndUser.__table__,
    SystemAdminSession.__table__,
    TenantAdminSession.__table__,
    SystemAdminAuditLog.__table__,
    TenantAdminAuditLog.__table__,
    AgentCard.__table__,
    AgentCardCapability.__table__,
    TenantA2AKey.__table__,
    # AgentCard.to_dict() eagerly loads resource_groups
    # for the `group_ids` / `groups` fields, so the test DB needs the join
    # table (and its parent ResourceGroup table) even if no test binds a
    # group. Missing them breaks every delete/update path.
    ResourceGroup.__table__,
    resource_group_agent_cards,
    # ResourceGroup.end_user_groups is a lazy=selectin
    # M2M; loading any RG triggers a SELECT against these tables.
    # No test seeds them, but they must exist for the query to succeed.
    EndUserGroup.__table__,
    end_user_group_members,
    resource_group_end_user_groups,
]


VALID_CARD_BODY: Dict[str, Any] = {
    "name": "billing-agent",
    "description": "Handles refunds + invoice reads",
    "version": "1.0.0",
    "capabilities": [
        {"name": "billing.refund", "description": "Issue refunds"},
        {"name": "billing.invoice.read", "description": "Read invoices"},
    ],
}


# =============================================================================
# Harness
# =============================================================================


@pytest.fixture()
def harness():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    # SQLite doesn't enforce FK cascades unless you turn them on per
    # connection. Production is Postgres which enforces them natively;
    # this listener bridges the test harness to the same behaviour so
    # our cascade-on-delete assertions actually exercise the FK.
    @event.listens_for(engine, "connect")
    def _enable_sqlite_fk(dbapi_conn, _conn_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine, tables=REQUIRED_TABLES)
    Factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    def override_get_db():
        s = Factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app = FastAPI()
    logger = BrutorLogger("test-a2a")
    # The system administrator's login is a router of its own.
    for _router in harness_extras().routers(logger):
        app.include_router(_router, prefix="/v1")
    app.include_router(create_admin_users_router(logger), prefix="/v1")
    app.include_router(create_agent_cards_router(logger), prefix="/v1")
    app.dependency_overrides[get_db] = override_get_db

    client = TestClient(app)

    # Seed: sysadmin + t1 (a2a on) + t2 (a2a on) + t3 (a2a off) + admin roles
    auth = UnifiedAuthService(jwt_secret=JWT_SECRET)
    s = Factory()
    try:
        harness_extras().seed_principals(auth, s, 'Secret123!')
        for tid, name, a2a_on in [("t1", "Tenant One", True),
                                   ("t2", "Tenant Two", True),
                                   ("t3", "Tenant Three", False)]:
            t = Tenant(
                id=tid, name=tid, display_name=name,
                organization_name=name,
                primary_contact_email=f"admin@{tid}.example",
                is_active=True, is_default=False, is_trial=False,
                suspended=False, billing_plan="self_hosted",
                features={"a2a": a2a_on} if a2a_on is not True else {"a2a": True},
            )
            if not a2a_on:
                # Explicit False to verify the feature-flag 404 path.
                t.features = {"a2a": False}
            s.add(t)
            s.flush()
            role = TenantRole(
                id=f"role-{tid}-admin",
                tenant_id=tid,
                name="Tenant Admin",
                description="full-perm admin",
                is_system=True,
            )
            s.add(role)
            s.flush()
            for p in Permission:
                if p == Permission.SYSTEM_ADMIN:
                    continue
                s.add(TenantRolePermission(
                    id=f"perm-{uuid.uuid4().hex[:10]}",
                    role_id=role.id,
                    permission=p.value,
                ))
            # Tenant admin user
            auth.create_tenant_admin(
                username=f"admin_{tid}",
                email=f"admin@{tid}.example",
                password="Secret123!",
                tenant_id=tid,
                db=s,
                role_id=role.id,
            )
        s.commit()
    finally:
        s.close()

    yield _H(client=client, Factory=Factory, auth=auth)
    app.dependency_overrides.clear()
    engine.dispose()


class _H:
    def __init__(self, client, Factory, auth):
        self.client = client
        self.Factory = Factory
        self.auth = auth

    def login(self, tenant_id: str) -> dict:
        r = self.client.post(
            "/v1/admin/users/tenant/login",
            json={"username": f"admin_{tenant_id}", "password": "Secret123!", "tenant_id": tenant_id},
        )
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['access_token']}"}


# =============================================================================
# Feature flag gating
# =============================================================================


class TestFeatureFlag:
    def test_tenant_without_a2a_sees_404_on_list(self, harness):
        h = harness.login("t3")
        r = harness.client.get("/v1/admin/agent-cards", headers=h)
        assert r.status_code == 404
        assert "A2A feature" in r.text

    def test_tenant_without_a2a_sees_404_on_create(self, harness):
        h = harness.login("t3")
        r = harness.client.post(
            "/v1/admin/agent-cards", headers=h,
            json={"name": "x", "card_json": VALID_CARD_BODY},
        )
        assert r.status_code == 404

    def test_tenant_without_a2a_sees_404_on_tenant_key(self, harness):
        h = harness.login("t3")
        r = harness.client.get("/v1/admin/agent-cards/tenant-key", headers=h)
        assert r.status_code == 404

    def test_tenant_with_a2a_reaches_list(self, harness):
        h = harness.login("t1")
        r = harness.client.get("/v1/admin/agent-cards", headers=h)
        assert r.status_code == 200
        assert r.json()["total"] == 0


# =============================================================================
# CRUD happy path
# =============================================================================


class TestCreateList:
    def test_create_then_list_shows_card(self, harness):
        h = harness.login("t1")
        r = harness.client.post(
            "/v1/admin/agent-cards", headers=h,
            json={"name": "billing", "card_json": VALID_CARD_BODY},
        )
        assert r.status_code == 201, r.text
        created = r.json()
        assert created["name"] == "billing"
        assert created["trust_tier"] == TRUST_TIER_UNSIGNED
        assert created["is_trusted"] is False
        assert created["capability_count"] == 2

        r = harness.client.get("/v1/admin/agent-cards", headers=h)
        assert r.status_code == 200
        listing = r.json()
        assert listing["total"] == 1
        # List items omit card_json.
        assert "card_json" not in listing["cards"][0]

    def test_duplicate_name_returns_409(self, harness):
        h = harness.login("t1")
        harness.client.post(
            "/v1/admin/agent-cards", headers=h,
            json={"name": "billing", "card_json": VALID_CARD_BODY},
        )
        r = harness.client.post(
            "/v1/admin/agent-cards", headers=h,
            json={"name": "billing", "card_json": VALID_CARD_BODY},
        )
        assert r.status_code == 409

    def test_invalid_card_json_returns_400(self, harness):
        h = harness.login("t1")
        r = harness.client.post(
            "/v1/admin/agent-cards", headers=h,
            json={"name": "broken", "card_json": {"only_name": "x"}},
        )
        assert r.status_code == 400
        assert "missing" in r.text

    def test_detail_view_includes_capabilities(self, harness):
        h = harness.login("t1")
        created = harness.client.post(
            "/v1/admin/agent-cards", headers=h,
            json={"name": "billing", "card_json": VALID_CARD_BODY},
        ).json()
        r = harness.client.get(f"/v1/admin/agent-cards/{created['id']}", headers=h)
        assert r.status_code == 200
        body = r.json()
        names = sorted(c["capability_name"] for c in body["capabilities"])
        assert names == ["billing.invoice.read", "billing.refund"]


# =============================================================================
# Extended agent card (the A2A specification's GetExtendedAgentCard)
# =============================================================================


class TestExtendedCardJson:
    def test_create_with_extended_card_round_trips(self, harness):
        h = harness.login("t1")
        extended = {**VALID_CARD_BODY, "description": "Extended billing card"}
        created = harness.client.post(
            "/v1/admin/agent-cards", headers=h,
            json={"name": "billing", "card_json": VALID_CARD_BODY,
                  "extended_card_json": extended},
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["extended_card_json"] == extended

        # Detail view round-trips it; list view omits the heavy body.
        detail = harness.client.get(
            f"/v1/admin/agent-cards/{body['id']}", headers=h
        ).json()
        assert detail["extended_card_json"] == extended
        listing = harness.client.get("/v1/admin/agent-cards", headers=h).json()
        assert "extended_card_json" not in listing["cards"][0]

    def test_extended_card_null_when_unset(self, harness):
        h = harness.login("t1")
        created = harness.client.post(
            "/v1/admin/agent-cards", headers=h,
            json={"name": "billing", "card_json": VALID_CARD_BODY},
        ).json()
        assert created["extended_card_json"] is None

    def test_create_with_invalid_extended_card_returns_400(self, harness):
        h = harness.login("t1")
        r = harness.client.post(
            "/v1/admin/agent-cards", headers=h,
            json={"name": "billing", "card_json": VALID_CARD_BODY,
                  "extended_card_json": {"only_name": "x"}},
        )
        assert r.status_code == 400
        assert "extended_card_json" in r.text

    def test_update_sets_extended_card(self, harness):
        h = harness.login("t1")
        created = harness.client.post(
            "/v1/admin/agent-cards", headers=h,
            json={"name": "billing", "card_json": VALID_CARD_BODY},
        ).json()
        assert created["extended_card_json"] is None

        extended = {**VALID_CARD_BODY, "version": "2.0.0"}
        r = harness.client.patch(
            f"/v1/admin/agent-cards/{created['id']}", headers=h,
            json={"extended_card_json": extended},
        )
        assert r.status_code == 200, r.text
        assert r.json()["extended_card_json"] == extended

    def test_update_with_invalid_extended_card_returns_400(self, harness):
        h = harness.login("t1")
        created = harness.client.post(
            "/v1/admin/agent-cards", headers=h,
            json={"name": "billing", "card_json": VALID_CARD_BODY},
        ).json()
        r = harness.client.patch(
            f"/v1/admin/agent-cards/{created['id']}", headers=h,
            json={"extended_card_json": {"only_name": "x"}},
        )
        assert r.status_code == 400
        assert "extended_card_json" in r.text


# =============================================================================
# Tenant isolation
# =============================================================================


class TestTenantIsolation:
    def test_t1_admin_cannot_read_t2_card(self, harness):
        h1 = harness.login("t1")
        h2 = harness.login("t2")
        created = harness.client.post(
            "/v1/admin/agent-cards", headers=h2,
            json={"name": "t2-only", "card_json": VALID_CARD_BODY},
        ).json()

        r = harness.client.get(f"/v1/admin/agent-cards/{created['id']}", headers=h1)
        assert r.status_code == 404

    def test_t1_list_does_not_include_t2_cards(self, harness):
        h1 = harness.login("t1")
        h2 = harness.login("t2")
        harness.client.post(
            "/v1/admin/agent-cards", headers=h2,
            json={"name": "t2-only", "card_json": VALID_CARD_BODY},
        )
        r = harness.client.get("/v1/admin/agent-cards", headers=h1)
        assert r.status_code == 200
        assert r.json()["total"] == 0


# =============================================================================
# Sign + verify
# =============================================================================


class TestSignEndpoint:
    def test_sign_flips_trust_tier_and_validates(self, harness):
        h = harness.login("t1")
        created = harness.client.post(
            "/v1/admin/agent-cards", headers=h,
            json={"name": "billing", "card_json": VALID_CARD_BODY},
        ).json()

        r = harness.client.post(
            f"/v1/admin/agent-cards/{created['id']}/sign",
            headers=h, json={},
        )
        assert r.status_code == 200, r.text
        signed = r.json()
        assert signed["trust_tier"] == TRUST_TIER_TENANT_SIGNED
        assert signed["signature"]
        assert signed["signature_algorithm"] == "ed25519"
        assert signed["signed_by"] == "admin_t1"
        assert signed["is_trusted"] is True
        assert signed["signature_valid"] is True

    def test_body_update_invalidates_signature(self, harness):
        h = harness.login("t1")
        created = harness.client.post(
            "/v1/admin/agent-cards", headers=h,
            json={"name": "billing", "card_json": VALID_CARD_BODY},
        ).json()
        harness.client.post(
            f"/v1/admin/agent-cards/{created['id']}/sign",
            headers=h, json={},
        )

        # Mutate capabilities — body change flips trust tier back to unsigned.
        updated_body = {**VALID_CARD_BODY,
                        "capabilities": [{"name": "billing.refund"}]}
        r = harness.client.patch(
            f"/v1/admin/agent-cards/{created['id']}",
            headers=h, json={"card_json": updated_body},
        )
        assert r.status_code == 200
        assert r.json()["trust_tier"] == TRUST_TIER_UNSIGNED
        # The signature string stays on the row for audit — we just
        # don't trust it anymore. Signature_valid reports null
        # because the tier is now unsigned.
        assert r.json()["signature_valid"] is None

    def test_metadata_only_update_preserves_signature(self, harness):
        h = harness.login("t1")
        created = harness.client.post(
            "/v1/admin/agent-cards", headers=h,
            json={"name": "billing", "card_json": VALID_CARD_BODY,
                  "description": "old"},
        ).json()
        harness.client.post(
            f"/v1/admin/agent-cards/{created['id']}/sign",
            headers=h, json={},
        )
        # Only `description` changes — body stays the same.
        r = harness.client.patch(
            f"/v1/admin/agent-cards/{created['id']}",
            headers=h, json={"description": "new"},
        )
        assert r.status_code == 200
        assert r.json()["trust_tier"] == TRUST_TIER_TENANT_SIGNED


# =============================================================================
# Revocation
# =============================================================================


class TestRevoke:
    def test_revoke_marks_terminal_and_disables(self, harness):
        h = harness.login("t1")
        created = harness.client.post(
            "/v1/admin/agent-cards", headers=h,
            json={"name": "billing", "card_json": VALID_CARD_BODY},
        ).json()
        r = harness.client.post(
            f"/v1/admin/agent-cards/{created['id']}/revoke",
            headers=h, json={"reason": "rotation"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["trust_tier"] == TRUST_TIER_REVOKED
        assert body["enabled"] is False
        assert body["revoked_reason"] == "rotation"

    def test_revoke_is_idempotent(self, harness):
        h = harness.login("t1")
        created = harness.client.post(
            "/v1/admin/agent-cards", headers=h,
            json={"name": "billing", "card_json": VALID_CARD_BODY},
        ).json()
        harness.client.post(
            f"/v1/admin/agent-cards/{created['id']}/revoke",
            headers=h, json={"reason": "rotation"},
        )
        # Second revoke is a no-op success, not a 409.
        r = harness.client.post(
            f"/v1/admin/agent-cards/{created['id']}/revoke",
            headers=h, json={"reason": "still gone"},
        )
        assert r.status_code == 200

    def test_revoked_card_cannot_be_resigned(self, harness):
        h = harness.login("t1")
        created = harness.client.post(
            "/v1/admin/agent-cards", headers=h,
            json={"name": "billing", "card_json": VALID_CARD_BODY},
        ).json()
        harness.client.post(
            f"/v1/admin/agent-cards/{created['id']}/revoke",
            headers=h, json={"reason": "rotation"},
        )
        r = harness.client.post(
            f"/v1/admin/agent-cards/{created['id']}/sign",
            headers=h, json={},
        )
        assert r.status_code == 409

    def test_revoked_card_cannot_be_updated(self, harness):
        h = harness.login("t1")
        created = harness.client.post(
            "/v1/admin/agent-cards", headers=h,
            json={"name": "billing", "card_json": VALID_CARD_BODY},
        ).json()
        harness.client.post(
            f"/v1/admin/agent-cards/{created['id']}/revoke",
            headers=h, json={"reason": "rotation"},
        )
        r = harness.client.patch(
            f"/v1/admin/agent-cards/{created['id']}",
            headers=h, json={"description": "still editable?"},
        )
        assert r.status_code == 409


# =============================================================================
# Tenant public key
# =============================================================================


class TestTenantKey:
    def test_public_key_lazy_creates_on_first_read(self, harness):
        h = harness.login("t1")
        # Sanity — no key row before the call.
        s = harness.Factory()
        try:
            assert s.query(TenantA2AKey).filter_by(tenant_id="t1").first() is None
        finally:
            s.close()

        r = harness.client.get("/v1/admin/agent-cards/tenant-key", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["tenant_id"] == "t1"
        assert body["key_algorithm"] == "ed25519"
        assert body["public_key"]
        # Key now persisted.
        s = harness.Factory()
        try:
            row = s.query(TenantA2AKey).filter_by(tenant_id="t1").first()
            assert row is not None
            assert row.public_key == body["public_key"]
        finally:
            s.close()

    def test_public_key_never_includes_private(self, harness):
        h = harness.login("t1")
        r = harness.client.get("/v1/admin/agent-cards/tenant-key", headers=h)
        body = r.json()
        assert "private_key" not in body
        assert "private_key_encrypted" not in body


# =============================================================================
# Delete
# =============================================================================


# =============================================================================
# URL helpers + catalog normaliser (pure functions, no HTTP)
# =============================================================================


class TestCandidateUrls:
    """`_candidate_urls` decides which well-known suffix(es) to try.

    The fallback rule: spec ≥0.3 standardised on `agent-card.json`,
    spec ≤0.2 used `agent.json`. We try the new path first when the
    operator pastes a base or the new explicit URL, but skip the new
    path when they explicitly pasted the legacy URL — no point making
    the legacy host serve a 404 first.
    """

    def test_bare_base_returns_both_paths_in_order(self):
        from app.api.v1.endpoints.admin.agent_cards import _candidate_urls
        out = _candidate_urls("https://example.com")
        assert out == [
            "https://example.com/.well-known/agent-card.json",
            "https://example.com/.well-known/agent.json",
        ]

    def test_trailing_slash_collapsed(self):
        from app.api.v1.endpoints.admin.agent_cards import _candidate_urls
        a = _candidate_urls("https://example.com/")
        b = _candidate_urls("https://example.com")
        assert a == b

    def test_explicit_new_path_falls_back_to_legacy(self):
        from app.api.v1.endpoints.admin.agent_cards import _candidate_urls
        out = _candidate_urls("https://x.test/.well-known/agent-card.json")
        assert out == [
            "https://x.test/.well-known/agent-card.json",
            "https://x.test/.well-known/agent.json",
        ]

    def test_explicit_legacy_path_does_not_try_new_path(self):
        # The host is openly running 0.2.x; pinging the new path
        # would just generate a guaranteed 404 round-trip.
        from app.api.v1.endpoints.admin.agent_cards import _candidate_urls
        out = _candidate_urls("https://x.test/.well-known/agent.json")
        assert out == ["https://x.test/.well-known/agent.json"]

    def test_explicit_custom_json_path_used_verbatim(self):
        from app.api.v1.endpoints.admin.agent_cards import _candidate_urls
        out = _candidate_urls("https://x.test/custom/somewhere.json")
        assert out == ["https://x.test/custom/somewhere.json"]


class TestNormaliseCatalogEntry:
    """Translate the loosely-typed a2aregistry payload into our shape.

    Coverage is around the registry's known-shaped fields (name, url,
    iconUrl, skills, health) plus a few alternate keys we've seen in
    different snapshots.
    """

    def test_full_record_maps_through(self):
        from app.api.v1.endpoints.admin.agent_cards import _normalise_catalog_entry
        norm = _normalise_catalog_entry({
            "id": "foo",
            "name": "Foo",
            "description": "desc",
            "url": "https://foo.example",
            "iconUrl": "https://foo.example/icon.png",
            "protocolVersion": "0.3.0",
            "skills": [{"id": "s1", "name": "skill1", "description": "d"}],
            "health": {"status": "healthy", "uptime": {"percentage": 99.5}},
            "provider": {"organization": "Acme", "url": "https://acme.com"},
        })
        assert norm.name == "Foo"
        assert norm.base_url == "https://foo.example"
        assert norm.icon_url == "https://foo.example/icon.png"
        assert norm.protocol_version == "0.3.0"
        assert norm.healthy is True
        assert norm.uptime_pct == 99.5
        assert norm.skill_count == 1
        assert norm.skills[0].name == "skill1"
        assert norm.provider_name == "Acme"

    def test_dead_health_maps_to_false(self):
        from app.api.v1.endpoints.admin.agent_cards import _normalise_catalog_entry
        # OpenClaw probes intentionally advertise non-healthy status.
        norm = _normalise_catalog_entry({"name": "dead", "status": "dead"})
        assert norm.healthy is False

    def test_unknown_health_stays_null_not_false(self):
        from app.api.v1.endpoints.admin.agent_cards import _normalise_catalog_entry
        norm = _normalise_catalog_entry({"name": "unknown"})
        assert norm.healthy is None

    def test_alternate_url_keys(self):
        from app.api.v1.endpoints.admin.agent_cards import _normalise_catalog_entry
        norm = _normalise_catalog_entry({
            "name": "x",
            "baseUrl": "https://b.example",
            "agentCardUrl": "https://b.example/.well-known/agent-card.json",
        })
        assert norm.base_url == "https://b.example"
        assert norm.well_known_url == "https://b.example/.well-known/agent-card.json"


# =============================================================================
# Single-card import (mocked httpx)
# =============================================================================


class _FakeResponse:
    """Drop-in replacement for httpx.Response covering only the bits the
    import endpoint reads (status_code + .json())."""

    def __init__(self, status_code: int, payload: Any = None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no payload")
        return self._payload


class _FakeClient:
    """Async-context-manager stand-in for httpx.AsyncClient.

    Records every URL fetched so tests can assert the well-known
    fallback was actually attempted in the right order.
    """

    def __init__(self, *, responses_by_url: Optional[Dict[str, _FakeResponse]] = None,
                 default: Optional[_FakeResponse] = None,
                 raise_on: Optional[Dict[str, Exception]] = None):
        self._responses = responses_by_url or {}
        self._default = default
        self._raise_on = raise_on or {}
        self.calls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def get(self, url: str):
        self.calls.append(url)
        if url in self._raise_on:
            raise self._raise_on[url]
        if url in self._responses:
            return self._responses[url]
        if self._default is not None:
            return self._default
        raise AssertionError(f"unexpected fetch of {url}")


def _install_fake_client(monkeypatch, fake: _FakeClient):
    """Patch httpx.AsyncClient inside the agent_cards module."""
    from app.api.v1.endpoints.admin import agent_cards as ac_mod

    def factory(*_a, **_kw):
        return fake

    monkeypatch.setattr(ac_mod.httpx, "AsyncClient", factory)


class TestImportCard:
    """Single-URL import — covers well-known fallback, validation,
    duplicate-name 409, and iconUrl denormalisation."""

    def test_import_fetches_new_well_known_first(self, harness, monkeypatch):
        h = harness.login("t1")
        fake = _FakeClient(default=_FakeResponse(
            200,
            {
                "name": "imported-1",
                "version": "0.1.0",
                "description": "imported test agent",
                "iconUrl": "https://cdn.test/icon.png",
                "capabilities": [{"name": "x.y"}],
            },
        ))
        _install_fake_client(monkeypatch, fake)

        r = harness.client.post(
            "/v1/admin/agent-cards/import", headers=h,
            json={"source_url": "https://imp.test"},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["name"] == "imported-1"
        # iconUrl was denormalised from card_json onto the row.
        assert body["icon_url"] == "https://cdn.test/icon.png"
        # New path was tried first.
        assert fake.calls[0].endswith("/.well-known/agent-card.json")

    def test_import_falls_back_to_legacy_on_404(self, harness, monkeypatch):
        h = harness.login("t1")
        new_path = "https://imp.test/.well-known/agent-card.json"
        legacy_path = "https://imp.test/.well-known/agent.json"
        fake = _FakeClient(responses_by_url={
            new_path: _FakeResponse(404),
            legacy_path: _FakeResponse(200, {
                "name": "legacy-only",
                "description": "from legacy path",
                "version": "0.2.0",
                "capabilities": [{"name": "noop"}],
            }),
        })
        _install_fake_client(monkeypatch, fake)

        r = harness.client.post(
            "/v1/admin/agent-cards/import", headers=h,
            json={"source_url": "https://imp.test"},
        )
        assert r.status_code == 201, r.text
        # Confirm both paths were attempted in the right order.
        assert fake.calls == [new_path, legacy_path]
        body = r.json()
        # source_url should point at the path that actually resolved.
        assert body["source_url"] == legacy_path

    def test_import_returns_502_when_both_paths_404(self, harness, monkeypatch):
        h = harness.login("t1")
        fake = _FakeClient(default=_FakeResponse(404))
        _install_fake_client(monkeypatch, fake)

        r = harness.client.post(
            "/v1/admin/agent-cards/import", headers=h,
            json={"source_url": "https://nothing.test"},
        )
        assert r.status_code == 502
        # Both attempts recorded.
        assert len(fake.calls) == 2

    def test_import_invalid_card_returns_400(self, harness, monkeypatch):
        h = harness.login("t1")
        # Card without required `name` field — `validate_card_json` rejects.
        fake = _FakeClient(default=_FakeResponse(200, {"capabilities": []}))
        _install_fake_client(monkeypatch, fake)
        r = harness.client.post(
            "/v1/admin/agent-cards/import", headers=h,
            json={"source_url": "https://imp.test"},
        )
        assert r.status_code == 400

    def test_import_duplicate_name_returns_409(self, harness, monkeypatch):
        h = harness.login("t1")
        # First card via direct create.
        harness.client.post(
            "/v1/admin/agent-cards", headers=h,
            json={"name": "dup-name", "card_json": VALID_CARD_BODY},
        )
        # Import a card whose body name collides.
        fake = _FakeClient(default=_FakeResponse(200, {
            "name": "dup-name",
            "description": "duplicate target",
            "version": "1.0.0",
            "capabilities": [{"name": "x"}],
        }))
        _install_fake_client(monkeypatch, fake)
        r = harness.client.post(
            "/v1/admin/agent-cards/import", headers=h,
            json={"source_url": "https://imp.test"},
        )
        assert r.status_code == 409


# =============================================================================
# Bulk import (mocked httpx)
# =============================================================================


class TestBulkImport:
    """Bulk import — per-row outcomes, partial success, ordering."""

    def test_three_rows_two_succeed_one_fails(self, harness, monkeypatch):
        h = harness.login("t1")

        # Row 1: succeeds, Row 2: invalid card body (400), Row 3: succeeds.
        # Round-robin response by URL host so each row gets a different body.
        responses_by_url: Dict[str, _FakeResponse] = {
            "https://a.test/.well-known/agent-card.json": _FakeResponse(200, {
                "name": "alpha", "description": "row a", "version": "1.0.0",
                "capabilities": [{"name": "x"}],
            }),
            "https://b.test/.well-known/agent-card.json": _FakeResponse(200, {
                # Missing name → 400 from validate_card_json.
                "description": "row b", "version": "1.0.0",
                "capabilities": [],
            }),
            "https://c.test/.well-known/agent-card.json": _FakeResponse(200, {
                "name": "gamma",
                "description": "row c",
                "version": "1.0.0",
                "iconUrl": "https://cdn.test/g.png",
                "capabilities": [{"name": "y"}],
            }),
        }
        fake = _FakeClient(responses_by_url=responses_by_url)
        _install_fake_client(monkeypatch, fake)

        r = harness.client.post(
            "/v1/admin/agent-cards/import-bulk", headers=h,
            json={"imports": [
                {"source_url": "https://a.test"},
                {"source_url": "https://b.test"},
                {"source_url": "https://c.test"},
            ]},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == 3
        assert body["succeeded"] == 2
        assert body["failed"] == 1
        # Row order preserved in results.
        urls = [row["source_url"] for row in body["results"]]
        assert urls == ["https://a.test", "https://b.test", "https://c.test"]
        assert body["results"][0]["success"] is True
        assert body["results"][1]["success"] is False
        assert body["results"][2]["success"] is True

        # The icon on row 3 made it to the row.
        listing = harness.client.get("/v1/admin/agent-cards", headers=h).json()
        gamma = next(c for c in listing["cards"] if c["name"] == "gamma")
        assert gamma["icon_url"] == "https://cdn.test/g.png"

    def test_failed_row_does_not_roll_back_earlier_success(self, harness, monkeypatch):
        """A 409 on row N must leave rows 1..N-1 committed."""
        h = harness.login("t1")
        # Pre-populate "duplicate" so row 2 collides on name.
        harness.client.post(
            "/v1/admin/agent-cards", headers=h,
            json={"name": "duplicate", "card_json": VALID_CARD_BODY},
        )
        fake = _FakeClient(responses_by_url={
            "https://a.test/.well-known/agent-card.json": _FakeResponse(200, {
                "name": "row-a", "description": "first", "version": "1.0.0",
                "capabilities": [{"name": "x"}],
            }),
            "https://b.test/.well-known/agent-card.json": _FakeResponse(200, {
                "name": "duplicate", "description": "collides", "version": "1.0.0",
                "capabilities": [{"name": "x"}],
            }),
        })
        _install_fake_client(monkeypatch, fake)

        r = harness.client.post(
            "/v1/admin/agent-cards/import-bulk", headers=h,
            json={"imports": [
                {"source_url": "https://a.test"},
                {"source_url": "https://b.test"},
            ]},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["succeeded"] == 1
        assert body["failed"] == 1

        # row-a really is in the DB.
        listing = harness.client.get("/v1/admin/agent-cards", headers=h).json()
        names = {c["name"] for c in listing["cards"]}
        assert "row-a" in names
        assert "duplicate" in names  # the pre-existing one


# =============================================================================
# Discover (URL → preview, no DB write)
# =============================================================================


class TestDiscover:
    """`/discover` is the Connect step of the Add-Card wizard.

    Critically: it must NEVER persist. A failed discover, a cancelled
    Add dialog, or even a successful preview where the operator backs
    out, must leave zero `agent_cards` rows behind.
    """

    def test_discover_returns_preview_without_persisting(self, harness, monkeypatch):
        h = harness.login("t1")
        fake = _FakeClient(default=_FakeResponse(200, {
            "name": "preview-agent",
            "description": "an agent that helps",
            "version": "1.0.0",
            "iconUrl": "https://cdn.test/p.png",
            "protocolVersion": "0.3.0",
            "provider": {"organization": "Acme"},
            "capabilities": [
                {"name": "skill.a", "description": "first"},
                {"name": "skill.b", "description": "second"},
            ],
        }))
        _install_fake_client(monkeypatch, fake)

        r = harness.client.post(
            "/v1/admin/agent-cards/discover", headers=h,
            json={"source_url": "https://preview.test"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["name"] == "preview-agent"
        assert body["description"] == "an agent that helps"
        assert body["icon_url"] == "https://cdn.test/p.png"
        assert body["provider_name"] == "Acme"
        assert body["capability_count"] == 2
        assert [c["name"] for c in body["capabilities"]] == ["skill.a", "skill.b"]
        # card_json passed through verbatim for the UI to use.
        assert body["card_json"]["version"] == "1.0.0"
        # Crucially: nothing was written.
        listing = harness.client.get("/v1/admin/agent-cards", headers=h).json()
        assert listing["total"] == 0

    def test_discover_falls_back_to_legacy_well_known(self, harness, monkeypatch):
        h = harness.login("t1")
        new_path = "https://preview.test/.well-known/agent-card.json"
        legacy_path = "https://preview.test/.well-known/agent.json"
        fake = _FakeClient(responses_by_url={
            new_path: _FakeResponse(404),
            legacy_path: _FakeResponse(200, {
                "name": "legacy-preview",
                "description": "old spec",
                "version": "0.2.0",
                "capabilities": [{"name": "x"}],
            }),
        })
        _install_fake_client(monkeypatch, fake)

        r = harness.client.post(
            "/v1/admin/agent-cards/discover", headers=h,
            json={"source_url": "https://preview.test"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["resolved_url"] == legacy_path
        assert body["name"] == "legacy-preview"

    def test_discover_502_when_remote_unreachable(self, harness, monkeypatch):
        h = harness.login("t1")
        fake = _FakeClient(default=_FakeResponse(404))
        _install_fake_client(monkeypatch, fake)
        r = harness.client.post(
            "/v1/admin/agent-cards/discover", headers=h,
            json={"source_url": "https://gone.test"},
        )
        assert r.status_code == 502

    def test_discover_400_on_invalid_card(self, harness, monkeypatch):
        h = harness.login("t1")
        # Missing required fields per validate_card_json.
        fake = _FakeClient(default=_FakeResponse(200, {"foo": "bar"}))
        _install_fake_client(monkeypatch, fake)
        r = harness.client.post(
            "/v1/admin/agent-cards/discover", headers=h,
            json={"source_url": "https://broken.test"},
        )
        assert r.status_code == 400

    def test_discover_flags_name_conflict(self, harness, monkeypatch):
        h = harness.login("t1")
        # First create a card with this name.
        harness.client.post(
            "/v1/admin/agent-cards", headers=h,
            json={"name": "taken", "card_json": VALID_CARD_BODY},
        )
        # Discover one with the same name → name_conflict = True.
        fake = _FakeClient(default=_FakeResponse(200, {
            "name": "taken",
            "description": "another",
            "version": "1.0.0",
            "capabilities": [{"name": "x"}],
        }))
        _install_fake_client(monkeypatch, fake)
        r = harness.client.post(
            "/v1/admin/agent-cards/discover", headers=h,
            json={"source_url": "https://other.test"},
        )
        assert r.status_code == 200
        assert r.json()["name_conflict"] is True
        # Counter-test: a unique name returns False.
        fake = _FakeClient(default=_FakeResponse(200, {
            "name": "unique-name",
            "description": "fresh",
            "version": "1.0.0",
            "capabilities": [{"name": "x"}],
        }))
        _install_fake_client(monkeypatch, fake)
        r = harness.client.post(
            "/v1/admin/agent-cards/discover", headers=h,
            json={"source_url": "https://fresh.test"},
        )
        assert r.json()["name_conflict"] is False

    def test_discover_extracts_skills_array_when_present(self, harness, monkeypatch):
        # A2A 1.0 cards put their dispatchable units under `skills`.
        # Older 0.3 cards used `capabilities`. Discover should prefer
        # the modern key, fall back when only legacy is set.
        h = harness.login("t1")
        fake = _FakeClient(default=_FakeResponse(200, {
            "name": "v1",
            "description": "v1.0 card",
            "version": "1.0.0",
            "skills": [
                {"id": "s1", "name": "search", "description": "do a search",
                 "tags": ["search", "web"]},
            ],
            "capabilities": [{"name": "should-be-ignored"}],
        }))
        _install_fake_client(monkeypatch, fake)
        r = harness.client.post(
            "/v1/admin/agent-cards/discover", headers=h,
            json={"source_url": "https://v1.test"},
        )
        # The validator still requires `capabilities` to be present, so
        # this card body validates; preview chooses skills[] over it.
        assert r.status_code == 200
        body = r.json()
        assert body["capability_count"] == 1
        cap = body["capabilities"][0]
        assert cap["id"] == "s1"
        assert cap["name"] == "search"
        assert cap["tags"] == ["search", "web"]

    def test_discover_requires_a2a_feature_flag(self, harness, monkeypatch):
        # t3 has features.a2a = False; discover must 404 too.
        h = harness.login("t3")
        # No httpx mock needed — the feature gate runs before the fetch.
        r = harness.client.post(
            "/v1/admin/agent-cards/discover", headers=h,
            json={"source_url": "https://anything.test"},
        )
        assert r.status_code == 404


class TestImportV1WireShapes:
    """A2A v1.0 cards put dispatch under `skills[]` and use the
    `capabilities` key for a feature-flag *object*. Make sure both the
    discover and import paths handle the v1.0 shape — that's what
    every well-known card on a2aregistry.org looks like today."""

    V1_CARD = {
        "name": "v1-agent",
        "description": "an agent that speaks A2A v1.0",
        "version": "1.0.0",
        "protocolVersion": "1.0",
        "capabilities": {
            "streaming": True,
            "pushNotifications": False,
            "extensions": ["urn:example:ext"],
        },
        "skills": [
            {"id": "s1", "name": "search", "description": "do a search",
             "tags": ["search", "web"]},
            {"id": "s2", "name": "summarise"},
        ],
    }

    def test_v1_card_imports(self, harness, monkeypatch):
        h = harness.login("t1")
        fake = _FakeClient(default=_FakeResponse(200, self.V1_CARD))
        _install_fake_client(monkeypatch, fake)
        r = harness.client.post(
            "/v1/admin/agent-cards/import", headers=h,
            json={"source_url": "https://v1.test"},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        # Skills made it into agent_card_capabilities (denormalised).
        names = sorted(c["capability_name"] for c in body["capabilities"])
        assert names == ["search", "summarise"]

    def test_v1_card_discovers(self, harness, monkeypatch):
        h = harness.login("t1")
        fake = _FakeClient(default=_FakeResponse(200, self.V1_CARD))
        _install_fake_client(monkeypatch, fake)
        r = harness.client.post(
            "/v1/admin/agent-cards/discover", headers=h,
            json={"source_url": "https://v1.test"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["capability_count"] == 2
        ids = sorted(c["id"] for c in body["capabilities"])
        assert ids == ["s1", "s2"]

    def test_v1_card_no_skills_still_valid(self, harness, monkeypatch):
        # A routing-only v1.0 card with the capabilities object but no
        # skills[] is structurally valid and should import (zero
        # denormalised rows).
        h = harness.login("t1")
        body = dict(self.V1_CARD)
        body.pop("skills")
        body["name"] = "router-only"
        fake = _FakeClient(default=_FakeResponse(200, body))
        _install_fake_client(monkeypatch, fake)
        r = harness.client.post(
            "/v1/admin/agent-cards/import", headers=h,
            json={"source_url": "https://router.test"},
        )
        assert r.status_code == 201, r.text
        assert r.json()["capabilities"] == []

    def test_malformed_capabilities_type_rejected(self, harness, monkeypatch):
        # capabilities is neither list nor dict → must reject.
        h = harness.login("t1")
        broken = {
            "name": "broken", "description": "x", "version": "1.0",
            "capabilities": "definitely-not-an-object",
        }
        fake = _FakeClient(default=_FakeResponse(200, broken))
        _install_fake_client(monkeypatch, fake)
        r = harness.client.post(
            "/v1/admin/agent-cards/import", headers=h,
            json={"source_url": "https://broken.test"},
        )
        assert r.status_code == 400


class TestImportWithDescriptionOverride:
    """Final Create from the Add-Card wizard passes both name + description
    overrides. Covered separately so a regression on either knob is loud."""

    def test_description_override_wins_over_card_body(self, harness, monkeypatch):
        h = harness.login("t1")
        fake = _FakeClient(default=_FakeResponse(200, {
            "name": "imported-2",
            "description": "from-body",
            "version": "1.0.0",
            "capabilities": [{"name": "x"}],
        }))
        _install_fake_client(monkeypatch, fake)
        r = harness.client.post(
            "/v1/admin/agent-cards/import", headers=h,
            json={
                "source_url": "https://imp2.test",
                "description_override": "operator-friendly summary",
            },
        )
        assert r.status_code == 201, r.text
        assert r.json()["description"] == "operator-friendly summary"


# =============================================================================
# Catalog (mocked httpx)
# =============================================================================


class TestCatalog:
    def _patch_registry_response(self, monkeypatch, payload: Any,
                                 status_code: int = 200):
        from app.api.v1.endpoints.admin import agent_cards as ac_mod
        # Wipe any previous test's cache to force a network round.
        ac_mod._REGISTRY_CACHE["at"] = 0
        ac_mod._REGISTRY_CACHE["payload"] = None
        fake = _FakeClient(default=_FakeResponse(status_code, payload))
        _install_fake_client(monkeypatch, fake)
        return fake

    def test_catalog_proxies_registry(self, harness, monkeypatch):
        h = harness.login("t1")
        self._patch_registry_response(monkeypatch, {
            "agents": [
                {"name": "Foo", "url": "https://foo.example",
                 "iconUrl": "https://foo.example/icon.png",
                 "skills": [{"id": "s", "name": "skill1"}],
                 "health": {"status": "healthy"}},
                {"name": "Bar", "url": "https://bar.example"},
            ],
        })
        r = harness.client.get("/v1/admin/agent-cards/catalog", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == 2
        assert body["cached"] is False
        names = [a["name"] for a in body["agents"]]
        assert names == ["Foo", "Bar"]
        foo = body["agents"][0]
        assert foo["icon_url"] == "https://foo.example/icon.png"
        assert foo["healthy"] is True
        assert foo["skill_count"] == 1

    def test_catalog_handles_bare_array_payload(self, harness, monkeypatch):
        # Some registry snapshots return a top-level array.
        h = harness.login("t1")
        self._patch_registry_response(monkeypatch, [
            {"name": "Solo", "url": "https://solo.example"},
        ])
        r = harness.client.get("/v1/admin/agent-cards/catalog", headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["total"] == 1

    def test_catalog_502_when_registry_errors(self, harness, monkeypatch):
        h = harness.login("t1")
        self._patch_registry_response(monkeypatch, None, status_code=503)
        r = harness.client.get("/v1/admin/agent-cards/catalog", headers=h)
        assert r.status_code == 502

    def test_catalog_serves_cache_within_ttl(self, harness, monkeypatch):
        h = harness.login("t1")
        fake = self._patch_registry_response(monkeypatch, {
            "agents": [{"name": "Cached"}],
        })
        # First call — populates cache.
        r1 = harness.client.get("/v1/admin/agent-cards/catalog", headers=h)
        assert r1.status_code == 200
        assert r1.json()["cached"] is False
        # Second call within TTL — served from cache, no extra network round.
        first_call_count = len(fake.calls)
        r2 = harness.client.get("/v1/admin/agent-cards/catalog", headers=h)
        assert r2.json()["cached"] is True
        assert len(fake.calls) == first_call_count


# =============================================================================
# iconUrl denormalisation on direct create + body update
# =============================================================================


class TestIconUrl:
    def test_create_extracts_iconurl_from_card_json(self, harness):
        h = harness.login("t1")
        body = dict(VALID_CARD_BODY)
        body["iconUrl"] = "https://cdn.test/from-body.png"
        r = harness.client.post(
            "/v1/admin/agent-cards", headers=h,
            json={"name": "icon-from-body", "card_json": body},
        )
        assert r.status_code == 201
        assert r.json()["icon_url"] == "https://cdn.test/from-body.png"

    def test_explicit_icon_url_wins_over_card_json(self, harness):
        h = harness.login("t1")
        body = dict(VALID_CARD_BODY)
        body["iconUrl"] = "https://cdn.test/from-body.png"
        r = harness.client.post(
            "/v1/admin/agent-cards", headers=h,
            json={
                "name": "icon-override",
                "icon_url": "https://cdn.test/explicit.png",
                "card_json": body,
            },
        )
        assert r.status_code == 201
        assert r.json()["icon_url"] == "https://cdn.test/explicit.png"

    def test_body_update_re_derives_icon(self, harness):
        h = harness.login("t1")
        body = dict(VALID_CARD_BODY)
        body["iconUrl"] = "https://cdn.test/v1.png"
        created = harness.client.post(
            "/v1/admin/agent-cards", headers=h,
            json={"name": "icon-update", "card_json": body},
        ).json()
        assert created["icon_url"] == "https://cdn.test/v1.png"
        # New body without iconUrl → row's icon_url clears.
        updated_body = dict(VALID_CARD_BODY)
        # No iconUrl on this body intentionally.
        r = harness.client.patch(
            f"/v1/admin/agent-cards/{created['id']}", headers=h,
            json={"card_json": updated_body},
        )
        assert r.status_code == 200
        assert r.json()["icon_url"] is None


class TestDelete:
    def test_delete_removes_card_and_capabilities(self, harness):
        h = harness.login("t1")
        created = harness.client.post(
            "/v1/admin/agent-cards", headers=h,
            json={"name": "billing", "card_json": VALID_CARD_BODY},
        ).json()

        r = harness.client.delete(
            f"/v1/admin/agent-cards/{created['id']}", headers=h,
        )
        assert r.status_code == 204

        r = harness.client.get(
            f"/v1/admin/agent-cards/{created['id']}", headers=h,
        )
        assert r.status_code == 404

        # Capabilities cascaded.
        s = harness.Factory()
        try:
            assert s.query(AgentCardCapability).filter_by(
                agent_card_id=created["id"]
            ).count() == 0
        finally:
            s.close()


class TestACardIsCreatedEnabledOrNot:
    """`enabled` is the caller's to say when a card is made, by any of the
    routes that make one; a card made without it is enabled."""

    def _stored(self, harness, card_id):
        from app.database.models.agent_card import AgentCard

        with harness.Factory() as s:
            return s.get(AgentCard, card_id).enabled

    @pytest.mark.parametrize("sent, stored", [({}, True), ({"enabled": True}, True), ({"enabled": False}, False)])
    def test_create(self, harness, sent, stored):
        h = harness.login("t1")
        r = harness.client.post(
            "/v1/admin/agent-cards", headers=h, json={"name": "c", "card_json": VALID_CARD_BODY, **sent}
        )
        assert r.status_code == 201, r.text
        assert r.json()["enabled"] is stored
        assert self._stored(harness, r.json()["id"]) is stored

    @pytest.mark.parametrize("sent, stored", [({}, True), ({"enabled": False}, False)])
    def test_import(self, harness, monkeypatch, sent, stored):
        h = harness.login("t1")
        _install_fake_client(monkeypatch, _FakeClient(default=_FakeResponse(
            200, {"name": "imported-e", "version": "0.1.0", "description": "d", "capabilities": [{"name": "x.y"}]},
        )))
        r = harness.client.post(
            "/v1/admin/agent-cards/import", headers=h, json={"source_url": "https://imp.test", **sent}
        )
        assert r.status_code == 201, r.text
        assert r.json()["enabled"] is stored
        assert self._stored(harness, r.json()["id"]) is stored

    def test_bulk_import(self, harness, monkeypatch):
        h = harness.login("t1")
        _install_fake_client(monkeypatch, _FakeClient(default=_FakeResponse(
            200, {"name": "ignored", "version": "0.1.0", "description": "d", "capabilities": [{"name": "x.y"}]},
        )))
        r = harness.client.post(
            "/v1/admin/agent-cards/import-bulk", headers=h,
            json={"imports": [
                {"source_url": "https://a.test", "name_override": "bulk-on"},
                {"source_url": "https://b.test", "name_override": "bulk-off", "enabled": False},
            ]},
        )
        assert r.status_code in (200, 201, 207), r.text
        listed = {c["name"]: c["enabled"] for c in harness.client.get("/v1/admin/agent-cards", headers=h).json()["cards"]}
        assert (listed["bulk-on"], listed["bulk-off"]) == (True, False)
