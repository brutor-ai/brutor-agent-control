# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
End-to-end tests for the A2A delegation-graph admin router.

Coverage:
  * Feature-flag gating — tenants without `features.a2a` see 404 on every
    endpoint (same pattern as agent_cards / outbound_rules).
  * RBAC + tenant isolation — t1 admins can't see t2's graphs.
  * List happy path with pagination + success filter.
  * Detail happy path with depth-ordered steps.
  * Stats aggregates + percentiles over a window.
  * 404 on unknown root_task_id.

Same harness approach as the other A2A endpoint suites — in-memory
SQLite, FastAPI TestClient, seeded system admin + tenants.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from ulid import ULID

from app.api.v1.endpoints.admin.a2a_delegation_graphs import (
    create_a2a_delegation_graphs_router,
)
from app.api.v1.endpoints.admin.admin_rbac import create_admin_users_router
from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.unified_auth_service import UnifiedAuthService
from app.core.logging_system import BrutorLogger
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
from app.database.models.agent_card import AgentCard
from app.database.models.end_user import EndUser
from app.database.models.end_user_group import (
    EndUserGroup,
    end_user_group_members,
    resource_group_end_user_groups,
)
from app.database.models.idp_server import IdPServer
from app.database.models.llm_model import LLModel
from app.database.models.proxy_log import ProxyLog
from app.database.models.ai_system_run import AISystemRun
from app.database.models.agent_identity import AgentIdentity
from app.database.models.ai_system_run_segment import AISystemRunSegment
from app.database.models.resource_group import ResourceGroup
from app.database.models.tenant import Tenant


JWT_SECRET = os.environ["JWT_SECRET"]

REQUIRED_TABLES = [
    LLModel.__table__,
    Tenant.__table__,
    TenantRole.__table__,
    TenantRolePermission.__table__,
    SystemAdmin.__table__,
    TenantAdmin.__table__,
    # TenantAdmin.idp_server_id FKs into idp_servers.
    IdPServer.__table__,
    EndUser.__table__,
    SystemAdminSession.__table__,
    TenantAdminSession.__table__,
    SystemAdminAuditLog.__table__,
    TenantAdminAuditLog.__table__,
    ProxyLog.__table__,
    # Delegation-graph endpoint resolves actor card names from agent_cards.
    AgentCard.__table__,
    # ResourceGroup.end_user_groups selectin needs these.
    ResourceGroup.__table__,
    EndUserGroup.__table__,
    end_user_group_members,
    resource_group_end_user_groups,
    # Graph summaries name the AI Systems their run involved.
    AISystemRun.__table__,
    AISystemRunSegment.__table__,
    AgentIdentity.__table__,  # ai_system_runs.agent_id FK
]


# -----------------------------------------------------------------------------
# Harness
# -----------------------------------------------------------------------------


@pytest.fixture()
def harness():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

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
    logger = BrutorLogger("test-a2a-delegation-graphs")
    app.include_router(create_admin_users_router(logger), prefix="/v1")
    app.include_router(create_a2a_delegation_graphs_router(logger), prefix="/v1")
    app.dependency_overrides[get_db] = override_get_db

    client = TestClient(app)

    auth = UnifiedAuthService(jwt_secret=JWT_SECRET)
    s = Factory()
    try:
        for tid, a2a_on in [("t1", True), ("t2", True), ("t3", False)]:
            features = {"a2a": a2a_on}
            t = Tenant(
                id=tid, name=tid, display_name=tid.upper(),
                organization_name=tid.upper(),
                primary_contact_email=f"admin@{tid}.example",
                is_active=True, is_default=False, is_trial=False,
                suspended=False, billing_plan="self_hosted",
                features=features,
            )
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

    def login(self, tenant_id: str) -> Dict[str, str]:
        r = self.client.post(
            "/v1/admin/users/tenant/login",
            json={
                "username": f"admin_{tenant_id}",
                "password": "Secret123!",
                "tenant_id": tenant_id,
            },
        )
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['access_token']}"}


# -----------------------------------------------------------------------------
# Seed helper — writes fabricated proxy_logs rows for tests
# -----------------------------------------------------------------------------


def _insert_step(
    session,
    tenant_id: str,
    root: str,
    *,
    current_task_id: str,
    parent_task_id: str | None = None,
    depth: int = 0,
    direction: str = "outbound",
    timestamp: datetime | None = None,
    duration_ms: float = 100.0,
    success: bool = True,
    status: int = 200,
    card_id: str | None = None,
    error: str | None = None,
):
    session.add(ProxyLog(
        id=f"log-{ULID()}",
        tenant_id=tenant_id,
        timestamp=timestamp or datetime.now(timezone.utc),
        proxy_type="a2a",
        proxy_subtype=direction,
        correlation_id=f"corr-{root[:8]}",
        end_user_session_id=f"corr-{root[:8]}",
        server_id=card_id,
        http_method="POST",
        endpoint=f"/v1/proxy/a2a/{direction}",
        http_status_code=status,
        success=success,
        error_message=error,
        duration_ms=duration_ms,
        root_task_id=root,
        delegation_depth=depth,
        current_task_id=current_task_id,
        parent_task_id=parent_task_id,
    ))


# =============================================================================
# Feature-flag gating
# =============================================================================


class TestFeatureFlag:
    def test_list_404_without_a2a(self, harness):
        h = harness.login("t3")
        r = harness.client.get("/v1/admin/a2a/delegation-graphs", headers=h)
        assert r.status_code == 404

    def test_detail_404_without_a2a(self, harness):
        h = harness.login("t3")
        r = harness.client.get(
            "/v1/admin/a2a/delegation-graphs/some-root", headers=h
        )
        assert r.status_code == 404

    def test_stats_404_without_a2a(self, harness):
        h = harness.login("t3")
        r = harness.client.get("/v1/admin/a2a/delegation-graphs/stats", headers=h)
        assert r.status_code == 404


# =============================================================================
# List
# =============================================================================


class TestList:
    def test_list_empty_for_fresh_tenant(self, harness):
        h = harness.login("t1")
        r = harness.client.get("/v1/admin/a2a/delegation-graphs", headers=h)
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 0
        assert body["graphs"] == []

    def test_list_returns_seeded_graphs(self, harness):
        with harness.Factory() as s:
            now = datetime.now(timezone.utc)
            for i in range(3):
                root = str(ULID())
                _insert_step(
                    s, "t1", root,
                    current_task_id=root, depth=0,
                    timestamp=now + timedelta(milliseconds=i),
                    card_id="card-A",
                )
            s.commit()

        h = harness.login("t1")
        r = harness.client.get("/v1/admin/a2a/delegation-graphs", headers=h)
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 3
        assert len(body["graphs"]) == 3
        for g in body["graphs"]:
            assert g["tenant_id"] == "t1"
            assert g["total_steps"] == 1
            assert g["root_card_id"] == "card-A"

    def test_pagination_limit_and_offset(self, harness):
        with harness.Factory() as s:
            now = datetime.now(timezone.utc)
            for i in range(5):
                root = str(ULID())
                _insert_step(
                    s, "t1", root,
                    current_task_id=root, depth=0,
                    timestamp=now + timedelta(seconds=i),
                )
            s.commit()

        h = harness.login("t1")
        r = harness.client.get(
            "/v1/admin/a2a/delegation-graphs?limit=2&offset=0", headers=h
        )
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 5
        assert len(body["graphs"]) == 2
        assert body["limit"] == 2
        assert body["offset"] == 0

    def test_success_filter(self, harness):
        with harness.Factory() as s:
            now = datetime.now(timezone.utc)
            for succ in (True, False, True):
                root = str(ULID())
                _insert_step(
                    s, "t1", root,
                    current_task_id=root, depth=0,
                    timestamp=now,
                    success=succ,
                    status=200 if succ else 502,
                )
            s.commit()

        h = harness.login("t1")
        ok = harness.client.get(
            "/v1/admin/a2a/delegation-graphs?success=true", headers=h
        ).json()
        fail = harness.client.get(
            "/v1/admin/a2a/delegation-graphs?success=false", headers=h
        ).json()
        assert ok["total"] == 2
        assert fail["total"] == 1


# =============================================================================
# Tenant isolation
# =============================================================================


class TestTenantIsolation:
    """A tenant admin naming someone else's tenant is refused, not re-aimed.

    These endpoints scope through the shared
    ``app.core.admin_user.dependencies.resolve_tenant_id``, which answers a
    cross-tenant ``?tenant_id=`` with 403 rather than silently substituting
    the caller's own tenant. The silent-substitution alternative answers a
    question nobody asked — the operator requests tenant B, gets tenant A's
    rows, and reads them under a heading that says B — so these assert the
    refusal, and that the caller's own tenant still works.
    """

    def test_t1_admin_cannot_see_t2_graphs(self, harness):
        with harness.Factory() as s:
            root = str(ULID())
            _insert_step(s, "t2", root, current_task_id=root, depth=0)
            s.commit()

        h = harness.login("t1")
        r = harness.client.get(
            "/v1/admin/a2a/delegation-graphs?tenant_id=t2", headers=h
        )
        assert r.status_code == 403, r.text

    def test_t1_admin_cannot_fetch_t2_detail(self, harness):
        with harness.Factory() as s:
            root = str(ULID())
            _insert_step(s, "t2", root, current_task_id=root, depth=0)
            s.commit()

        h = harness.login("t1")
        r = harness.client.get(
            f"/v1/admin/a2a/delegation-graphs/{root}?tenant_id=t2", headers=h
        )
        assert r.status_code == 403, r.text

    def test_t1_admin_naming_its_own_tenant_is_allowed(self, harness):
        """The refusal is about the mismatch, not about passing the param.

        Without this, a resolver that rejected every ``?tenant_id=`` from a
        tenant admin would pass the two tests above while breaking the
        Admin UI, which sends the parameter on every request.
        """
        with harness.Factory() as s:
            root = str(ULID())
            _insert_step(s, "t1", root, current_task_id=root, depth=0)
            s.commit()

        h = harness.login("t1")
        r = harness.client.get(
            "/v1/admin/a2a/delegation-graphs?tenant_id=t1", headers=h
        )
        assert r.status_code == 200, r.text
        assert r.json()["total"] == 1

    def test_t1_admin_cannot_fetch_a_t2_graph_without_naming_t2(self, harness):
        """Omitting the param must not become the way around the refusal.

        The caller is pinned to t1, so a root task id that exists only in
        t2 is simply not found — the 403 above is a second gate, not the
        only one holding tenancy.
        """
        with harness.Factory() as s:
            root = str(ULID())
            _insert_step(s, "t2", root, current_task_id=root, depth=0)
            s.commit()

        h = harness.login("t1")
        r = harness.client.get(
            f"/v1/admin/a2a/delegation-graphs/{root}", headers=h
        )
        assert r.status_code == 404, r.text


# =============================================================================
# Detail
# =============================================================================


class TestDetail:
    def test_404_on_unknown_root(self, harness):
        h = harness.login("t1")
        r = harness.client.get(
            "/v1/admin/a2a/delegation-graphs/01JZZUNKNOWNUNKNOWNUNKNOWN", headers=h
        )
        assert r.status_code == 404

    def test_returns_tree_ordered_by_depth(self, harness):
        root = str(ULID())
        parent_id = str(ULID())
        child1 = str(ULID())
        child2 = str(ULID())
        now = datetime.now(timezone.utc)

        with harness.Factory() as s:
            _insert_step(
                s, "t1", root,
                current_task_id=child2, parent_task_id=parent_id,
                depth=1, timestamp=now + timedelta(milliseconds=200),
            )
            _insert_step(
                s, "t1", root,
                current_task_id=parent_id, depth=0,
                timestamp=now,
            )
            _insert_step(
                s, "t1", root,
                current_task_id=child1, parent_task_id=parent_id,
                depth=1, timestamp=now + timedelta(milliseconds=100),
            )
            s.commit()

        h = harness.login("t1")
        r = harness.client.get(
            f"/v1/admin/a2a/delegation-graphs/{root}", headers=h
        )
        assert r.status_code == 200
        body = r.json()
        assert body["total_steps"] == 3
        assert body["max_depth"] == 1
        depths = [h["depth"] for h in body["steps"]]
        assert depths == [0, 1, 1]
        cts = [h["current_task_id"] for h in body["steps"]]
        assert cts == [parent_id, child1, child2]


# =============================================================================
# Stats
# =============================================================================


class TestStats:
    def test_empty_tenant_returns_zeros(self, harness):
        h = harness.login("t1")
        r = harness.client.get("/v1/admin/a2a/delegation-graphs/stats", headers=h)
        assert r.status_code == 200
        body = r.json()
        assert body["total_steps"] == 0
        assert body["total_graphs"] == 0
        assert body["success_rate"] is None
        assert body["top_cards"] == []

    def test_stats_counts_and_top_cards(self, harness):
        now = datetime.now(timezone.utc)
        with harness.Factory() as s:
            # 3 steps on card-A, 2 on card-B, 1 failure.
            for _ in range(3):
                root = str(ULID())
                _insert_step(
                    s, "t1", root,
                    current_task_id=root, depth=0,
                    timestamp=now, card_id="card-A",
                )
            for i in range(2):
                root = str(ULID())
                _insert_step(
                    s, "t1", root,
                    current_task_id=root, depth=0,
                    timestamp=now, card_id="card-B",
                    success=(i == 0),
                    status=200 if i == 0 else 502,
                )
            s.commit()

        h = harness.login("t1")
        r = harness.client.get("/v1/admin/a2a/delegation-graphs/stats", headers=h)
        assert r.status_code == 200
        body = r.json()
        assert body["total_steps"] == 5
        assert body["total_graphs"] == 5
        assert body["success_steps"] == 4
        assert body["failure_steps"] == 1
        assert round(body["success_rate"], 2) == 0.8
        top = {c["card_id"]: c["count"] for c in body["top_cards"]}
        assert top == {"card-A": 3, "card-B": 2}

    def test_window_filter_excludes_old_rows(self, harness):
        now = datetime.now(timezone.utc)
        with harness.Factory() as s:
            _insert_step(
                s, "t1", str(ULID()),
                current_task_id=str(ULID()), depth=0,
                timestamp=now - timedelta(days=30),
            )
            _insert_step(
                s, "t1", str(ULID()),
                current_task_id=str(ULID()), depth=0,
                timestamp=now,
            )
            s.commit()

        from urllib.parse import quote

        since = quote((now - timedelta(days=1)).isoformat())
        h = harness.login("t1")
        r = harness.client.get(
            f"/v1/admin/a2a/delegation-graphs/stats?since={since}", headers=h
        )
        assert r.status_code == 200
        assert r.json()["total_steps"] == 1


# =============================================================================
# The systems each graph involved
# =============================================================================


class TestGraphSystems:
    """Each summary names its run's root system and every segment system,
    from the run ledger, in a fixed number of queries for the whole page."""

    def _seed_graph(self, s, root_task, *, root_sys, segs):
        from app.database.models.resource_group import GroupType

        now = datetime.now(timezone.utc)
        for gid in (root_sys, *segs):
            if s.get(ResourceGroup, gid) is None:
                s.add(ResourceGroup(
                    id=gid, tenant_id="t1", name=gid, display_name=f"{gid} display",
                    description=gid, hierarchy_level=0, inherit_resources=True,
                    is_active=True, is_system=False, group_type=GroupType.AI_SYSTEM,
                ))
        s.flush()
        _insert_step(s, "t1", root_task, current_task_id=root_task, depth=0, timestamp=now)
        s.add(AISystemRun(
            id=f"run-{root_task}", tenant_id="t1", root_task_id=root_task,
            ai_system_id=root_sys, started_at=now, terminal_state="completed",
            finalized_by="explicit",
        ))
        s.flush()
        for depth, seg in enumerate(segs, start=1):
            s.add(AISystemRunSegment(
                id=f"seg-{root_task}-{seg}", run_id=f"run-{root_task}", tenant_id="t1",
                root_task_id=root_task, root_system_id=root_sys, segment_system_id=seg,
                caller_system_id=([root_sys, *segs][depth - 1]),
                min_depth=depth, first_action_at=now, last_action_at=now, outcome="answered",
            ))

    def test_summary_lists_root_then_segments(self, harness):
        with harness.Factory() as s:
            self._seed_graph(s, "g-root-1", root_sys="sys-a", segs=["sys-b", "sys-c"])
            _insert_step(s, "t1", "g-unfinalised", current_task_id="g-unfinalised", depth=0)
            s.commit()
        r = harness.client.get("/v1/admin/a2a/delegation-graphs", headers=harness.login("t1"))
        assert r.status_code == 200, r.text
        by_root = {g["root_task_id"]: g for g in r.json()["graphs"]}
        assert by_root["g-root-1"]["systems"] == [
            {"id": "sys-a", "name": "sys-a display", "role": "root"},
            {"id": "sys-b", "name": "sys-b display", "role": "segment"},
            {"id": "sys-c", "name": "sys-c display", "role": "segment"},
        ]
        # Not yet in the ledger: an empty list, never a missing key.
        assert by_root["g-unfinalised"]["systems"] == []

    def test_lookup_is_batched_not_per_graph(self, harness):
        from sqlalchemy import event as sa_event

        with harness.Factory() as s:
            for i in range(6):
                self._seed_graph(s, f"g-batch-{i}", root_sys="sys-a", segs=["sys-b"])
            s.commit()
            engine = s.get_bind()

        statements = []

        def _count(conn, cursor, statement, *_):
            if "ai_system_run" in statement or "FROM resource_groups" in statement:
                statements.append(statement)

        sa_event.listen(engine, "before_cursor_execute", _count)
        try:
            r = harness.client.get("/v1/admin/a2a/delegation-graphs",
                                   headers=harness.login("t1"))
        finally:
            sa_event.remove(engine, "before_cursor_execute", _count)
        assert r.status_code == 200, r.text
        assert all(len(g["systems"]) == 2 for g in r.json()["graphs"])
        # runs + segments + names — three statements for six graphs.
        assert len(statements) <= 3, statements
