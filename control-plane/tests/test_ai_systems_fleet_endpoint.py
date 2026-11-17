# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
End-to-end tests for the AI System fleet board.

Coverage:
  * Only `group_type='ai_system'` groups appear — the board is a
    projection over resource groups, not a group list.
  * Tenant isolation: a tenant admin never sees another tenant's systems.
  * Governance gaps are computed per system and rolled up into fleet
    stats that span the whole filter, not just the returned page.
  * Filters: kind, lifecycle stage, risk tier, owner, undocumented-only,
    include-inactive.
  * An unknown `system_kind` filter is a 422, not a silent empty list.
  * Pagination returns a page while `total` and `stats` still describe
    the whole fleet.

Same harness approach as the other admin endpoint suites — in-memory
SQLite, FastAPI TestClient, seeded system admin + tenants.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import uuid
from typing import Dict, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.endpoints.admin.admin_rbac import create_admin_users_router
from app.api.v1.endpoints.admin.ai_system_runs import create_ai_system_runs_router
from app.api.v1.endpoints.admin.ai_systems import create_ai_systems_router
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
from app.database.models.agent_identity import AgentIdentity, ResourceGroupAgentMember
from app.database.models.ai_system_health import AISystemHealth
from app.database.models.ai_system_run import AISystemRun
from app.database.models.proxy_log import ProxyLog
from app.database.models.usage_log import UsageLog
from app.database.models.end_user import EndUser
from app.database.models.end_user_group import (
    EndUserGroup,
    end_user_group_members,
    resource_group_end_user_groups,
)
from app.database.models.idp_server import IdPServer
from app.database.models.llm_model import LLModel
from app.database.models.agent_skill import (
    AgentSkill,
    resource_group_agent_skills,
)
from app.database.models.resource_group import (
    GroupType,
    ResourceGroup,
    resource_group_llm_models,
    resource_group_server_configs,
)
from app.database.models.server_config import ServerConfig
from app.database.models.tenant import Tenant

JWT_SECRET = os.environ["JWT_SECRET"]

REQUIRED_TABLES = [
    LLModel.__table__,
    Tenant.__table__,
    TenantRole.__table__,
    TenantRolePermission.__table__,
    SystemAdmin.__table__,
    TenantAdmin.__table__,
    IdPServer.__table__,
    EndUser.__table__,
    SystemAdminSession.__table__,
    TenantAdminSession.__table__,
    SystemAdminAuditLog.__table__,
    TenantAdminAuditLog.__table__,
    ResourceGroup.__table__,
    EndUserGroup.__table__,
    end_user_group_members,
    resource_group_end_user_groups,
    resource_group_llm_models,
    # The board counts composition across all four association tables in
    # one grouped query each; every one of them has to exist.
    ServerConfig.__table__,
    resource_group_server_configs,
    AgentSkill.__table__,
    resource_group_agent_skills,
    AgentIdentity.__table__,
    ResourceGroupAgentMember.__table__,
    # include_health reads the materialised assurance rollup.
    AISystemHealth.__table__,
    # The monthly statement composes cost + runs + savings on top.
    UsageLog.__table__,
    ProxyLog.__table__,
    AISystemRun.__table__,
]


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

    # a184_composite_ai_systems — composition, liveness and conformance read these.
    # The composition index also reads active contracts
    # (stale pins), a2a_call grants (grant/edge checks) and open drift.
    _extra = [Base.metadata.tables[n] for n in ['ai_system_run_segments', 'tool_approval_requests', 'ai_system_dependencies', 'agent_cards', 'ai_system_liveness_expectations', 'kb_collections', 'resource_group_kb_collections', 'ai_system_contracts', 'agent_grants', 'ai_system_drift_events', 'ai_system_incident_windows', 'ai_system_baseline_epochs',
              # include_card — the gallery card's gateway and eval reads.
              'mcp_servers', 'guardrail_configs', 'resource_group_guardrails', 'resource_group_agent_cards',
              'argument_policies', 'resource_group_argument_policies',
              'semantic_policies', 'resource_group_semantic_policies',
              'ai_system_response_policies', 'ai_system_data_scopes', 'ai_system_reach_rules',
              'ai_system_replay_suites', 'ai_system_replay_runs']
              if Base.metadata.tables[n] not in REQUIRED_TABLES]
    Base.metadata.create_all(bind=engine, tables=REQUIRED_TABLES + _extra)
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
    logger = BrutorLogger("test-ai-systems-fleet")
    app.include_router(create_admin_users_router(logger), prefix="/v1")
    app.include_router(create_ai_systems_router(logger), prefix="/v1")
    # Served calls — the Runs tab's view of a shared service.
    app.include_router(create_ai_system_runs_router(logger), prefix="/v1")
    app.dependency_overrides[get_db] = override_get_db

    client = TestClient(app)

    auth = UnifiedAuthService(jwt_secret=JWT_SECRET)
    s = Factory()
    try:
        for tid in ("t1", "t2"):
            t = Tenant(
                id=tid, name=tid, display_name=tid.upper(),
                organization_name=tid.upper(),
                primary_contact_email=f"admin@{tid}.example",
                is_active=True, is_default=False, is_trial=False,
                suspended=False, billing_plan="self_hosted",
                features={},
            )
            s.add(t)
            s.flush()
            role = TenantRole(
                id=f"role-{tid}-admin", tenant_id=tid, name="Tenant Admin",
                description="full-perm admin", is_system=True,
            )
            s.add(role)
            s.flush()
            for p in Permission:
                if p == Permission.SYSTEM_ADMIN:
                    continue
                s.add(TenantRolePermission(
                    id=f"perm-{uuid.uuid4().hex[:10]}",
                    role_id=role.id, permission=p.value,
                ))
            auth.create_tenant_admin(
                username=f"admin_{tid}", email=f"admin@{tid}.example",
                password="Secret123!", tenant_id=tid, db=s, role_id=role.id,
            )
        s.commit()
    finally:
        s.close()

    yield _H(client=client, Factory=Factory)
    app.dependency_overrides.clear()
    engine.dispose()


class _H:
    def __init__(self, client, Factory):
        self.client = client
        self.Factory = Factory

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

    def fleet(self, tenant_id: str, **params):
        return self.client.get(
            "/v1/admin/ai-systems",
            headers=self.login(tenant_id),
            params=params,
        )


def _add_group(
    session,
    gid: str,
    tenant_id: str,
    *,
    group_type: GroupType = GroupType.AI_SYSTEM,
    system_kind: Optional[str] = "agent",
    owner: Optional[str] = None,
    intended_use: Optional[str] = None,
    lifecycle_stage: Optional[str] = None,
    risk_tier: Optional[str] = None,
    is_active: bool = True,
    parent_group_id: Optional[str] = None,
    hierarchy_level: int = 0,
):
    session.add(ResourceGroup(
        id=gid,
        tenant_id=tenant_id,
        name=gid,
        display_name=gid.title(),
        group_type=group_type,
        system_kind=system_kind if group_type == GroupType.AI_SYSTEM else None,
        owner=owner,
        intended_use=intended_use,
        lifecycle_stage=lifecycle_stage,
        eu_ai_act_risk_tier=risk_tier,
        hierarchy_level=hierarchy_level,
        hierarchy_path=f"/{gid}",
        inherit_resources=True,
        is_active=is_active,
        is_system=False,
        parent_group_id=parent_group_id,
    ))


def _seed_fleet(harness):
    s = harness.Factory()
    try:
        # Fully documented AI System.
        _add_group(
            s, "refund", "t1", system_kind="agent", owner="payments",
            intended_use="Issue refunds", lifecycle_stage="active",
            risk_tier="high",
        )
        # Wholly undocumented AI System.
        _add_group(s, "nightly", "t1", system_kind="integration")
        # Partly documented.
        _add_group(
            s, "helpdesk", "t1", system_kind="assistant", owner="support",
            lifecycle_stage="proposed",
        )
        # Not an AI System — must never appear on the board.
        _add_group(s, "engineering", "t1", group_type=GroupType.TEAM, system_kind=None)
        # Inactive AI System — hidden unless asked for.
        _add_group(s, "retired-bot", "t1", system_kind="agent", is_active=False)
        # Another tenant's AI System.
        _add_group(s, "other-tenant-sys", "t2", system_kind="workflow")
        s.commit()
    finally:
        s.close()


class TestProjection:
    def test_only_ai_systems_are_listed(self, harness):
        _seed_fleet(harness)
        body = harness.fleet("t1").json()
        ids = {row["id"] for row in body["systems"]}
        assert ids == {"refund", "nightly", "helpdesk"}
        assert "engineering" not in ids, "a plain team is not an AI System"

    def test_inactive_systems_hidden_by_default(self, harness):
        _seed_fleet(harness)
        ids = {r["id"] for r in harness.fleet("t1").json()["systems"]}
        assert "retired-bot" not in ids

    def test_include_inactive_surfaces_them(self, harness):
        _seed_fleet(harness)
        ids = {
            r["id"]
            for r in harness.fleet("t1", include_inactive=True).json()["systems"]
        }
        assert "retired-bot" in ids

    def test_tenant_isolation(self, harness):
        _seed_fleet(harness)
        ids = {r["id"] for r in harness.fleet("t1").json()["systems"]}
        assert "other-tenant-sys" not in ids

        other = {r["id"] for r in harness.fleet("t2").json()["systems"]}
        assert other == {"other-tenant-sys"}

    def test_kind_and_governance_fields_are_returned(self, harness):
        _seed_fleet(harness)
        row = next(
            r for r in harness.fleet("t1").json()["systems"] if r["id"] == "refund"
        )
        assert row["system_kind"] == "agent"
        assert row["owner"] == "payments"
        assert row["lifecycle_stage"] == "active"
        assert row["eu_ai_act_risk_tier"] == "high"


class TestGovernanceGaps:
    def test_documented_system_has_no_gaps(self, harness):
        _seed_fleet(harness)
        row = next(
            r for r in harness.fleet("t1").json()["systems"] if r["id"] == "refund"
        )
        assert row["governance_gaps"] == []

    def test_undocumented_system_reports_every_gap(self, harness):
        _seed_fleet(harness)
        row = next(
            r for r in harness.fleet("t1").json()["systems"] if r["id"] == "nightly"
        )
        assert set(row["governance_gaps"]) == {
            "owner",
            "intended_use",
            "lifecycle_stage",
            "eu_ai_act_risk_tier",
        }

    def test_partial_documentation_reports_only_the_missing(self, harness):
        _seed_fleet(harness)
        row = next(
            r for r in harness.fleet("t1").json()["systems"] if r["id"] == "helpdesk"
        )
        assert set(row["governance_gaps"]) == {"intended_use", "eu_ai_act_risk_tier"}

    def test_undocumented_only_filter(self, harness):
        _seed_fleet(harness)
        ids = {
            r["id"]
            for r in harness.fleet("t1", undocumented_only=True).json()["systems"]
        }
        assert ids == {"nightly", "helpdesk"}


class TestStats:
    def test_stats_describe_the_whole_fleet(self, harness):
        _seed_fleet(harness)
        stats = harness.fleet("t1").json()["stats"]
        assert stats["total"] == 3
        assert stats["fully_documented"] == 1
        assert stats["without_owner"] == 1          # nightly
        assert stats["without_intended_use"] == 2   # nightly + helpdesk

    def test_stats_break_down_by_kind_and_stage(self, harness):
        _seed_fleet(harness)
        stats = harness.fleet("t1").json()["stats"]
        assert stats["by_kind"] == {"agent": 1, "integration": 1, "assistant": 1}
        assert stats["by_lifecycle_stage"]["active"] == 1
        assert stats["by_lifecycle_stage"]["proposed"] == 1
        # Systems with nothing set are counted under an explicit "unset"
        # bucket rather than being dropped from the breakdown.
        assert stats["by_lifecycle_stage"]["unset"] == 1

    def test_stats_span_the_filter_not_the_page(self, harness):
        _seed_fleet(harness)
        body = harness.fleet("t1", limit=1).json()
        assert len(body["systems"]) == 1
        assert body["total"] == 3
        assert body["stats"]["total"] == 3, (
            "an operator paging through 1 of 3 still needs the fleet-wide count"
        )


class TestFilters:
    def test_filter_by_kind(self, harness):
        _seed_fleet(harness)
        ids = {
            r["id"]
            for r in harness.fleet("t1", system_kind="integration").json()["systems"]
        }
        assert ids == {"nightly"}

    def test_filter_by_lifecycle_stage(self, harness):
        _seed_fleet(harness)
        ids = {
            r["id"]
            for r in harness.fleet("t1", lifecycle_stage="active").json()["systems"]
        }
        assert ids == {"refund"}

    def test_filter_by_risk_tier(self, harness):
        _seed_fleet(harness)
        ids = {
            r["id"] for r in harness.fleet("t1", risk_tier="high").json()["systems"]
        }
        assert ids == {"refund"}

    def test_filter_by_owner(self, harness):
        _seed_fleet(harness)
        ids = {
            r["id"] for r in harness.fleet("t1", owner="support").json()["systems"]
        }
        assert ids == {"helpdesk"}

    def test_unknown_kind_is_422_not_an_empty_list(self, harness):
        _seed_fleet(harness)
        r = harness.fleet("t1", system_kind="not-a-kind")
        assert r.status_code == 422, (
            "a typo'd filter must not read as 'you have no such systems'"
        )

    def test_pagination_offsets(self, harness):
        _seed_fleet(harness)
        first = harness.fleet("t1", limit=2, offset=0).json()
        second = harness.fleet("t1", limit=2, offset=2).json()
        assert len(first["systems"]) == 2
        assert len(second["systems"]) == 1
        overlap = {r["id"] for r in first["systems"]} & {
            r["id"] for r in second["systems"]
        }
        assert not overlap


class TestComposition:
    def test_composition_counts_members(self, harness):
        _seed_fleet(harness)
        s = harness.Factory()
        try:
            # Models count what the system can reach, joined to each model's
            # `enabled` flag, so a real `llm_model` row is needed. Its
            # catalogue/provider FK web is never read by this endpoint, so
            # FKs are suspended — and SQLite ignores the PRAGMA inside an
            # open transaction, so it must be the session's first statement.
            s.execute(text("PRAGMA foreign_keys=OFF"))
            s.execute(LLModel.__table__.insert().values(
                id="model-1", tenant_id="t1", name="m", provider_id="p", model_name="m",
                mode="chat", api_url_auto_discovered=False, enabled=True,
            ))
            s.execute(resource_group_llm_models.insert().values(
                id="bind-1", resource_group_id="refund", llm_model_id="model-1",
            ))
            s.execute(text("PRAGMA foreign_keys=ON"))

            # The agent count uses a different query (a mapped model, not an
            # association Table), so it is exercised with real rows.
            s.add(AgentIdentity(id="agent-1", tenant_id="t1", name="worker"))
            s.flush()
            s.add(ResourceGroupAgentMember(
                tenant_id="t1", agent_id="agent-1", group_id="refund",
            ))
            s.commit()
        finally:
            s.close()

        row = next(
            r for r in harness.fleet("t1").json()["systems"] if r["id"] == "refund"
        )
        # Member counts live under composition.members.
        assert row["composition"]["members"]["models"] == 1
        assert row["composition"]["members"]["agents"] == 1
        assert row["composition"]["members"]["mcp_servers"] == 0

    def test_child_groups_are_counted(self, harness):
        _seed_fleet(harness)
        s = harness.Factory()
        try:
            _add_group(
                s, "refund-child", "t1", group_type=GroupType.TEAM,
                system_kind=None, parent_group_id="refund", hierarchy_level=1,
            )
            s.commit()
        finally:
            s.close()

        row = next(
            r for r in harness.fleet("t1").json()["systems"] if r["id"] == "refund"
        )
        assert row["composition"]["child_groups"] == 1


class TestEmptyFleet:
    def test_empty_tenant_returns_zeroed_stats_not_an_error(self, harness):
        r = harness.fleet("t1")
        assert r.status_code == 200
        body = r.json()
        assert body["systems"] == []
        assert body["total"] == 0
        assert body["stats"]["total"] == 0
        assert body["stats"]["fully_documented"] == 0


class TestIncludeHealth:
    """`include_health=true` attaches the materialised assurance rollup.

    The board must read the rollup in one grouped query (no per-system
    recompute), keep nulls for systems whose health was never computed
    (render as unknown, never healthy), and never leak another tenant's
    rollup row.
    """

    @staticmethod
    def _add_health(harness, gid: str, tenant_id: str, **overrides):
        s = harness.Factory()
        try:
            s.add(AISystemHealth(
                id=f"health-{gid}",
                tenant_id=tenant_id,
                ai_system_id=gid,
                health_state=overrides.get("health_state", "degraded"),
                health_score=overrides.get("health_score", 0.6),
                open_drift_events=overrides.get("open_drift_events", 2),
                verdict_status=overrides.get("verdict_status", "assured_with_exceptions"),
            ))
            s.commit()
        finally:
            s.close()

    def test_health_attached_when_requested(self, harness):
        _seed_fleet(harness)
        self._add_health(harness, "refund", "t1")
        body = harness.fleet("t1", include_health=True).json()
        rows = {r["id"]: r for r in body["systems"]}

        assert rows["refund"]["health_state"] == "degraded"
        assert rows["refund"]["health_score"] == 0.6
        assert rows["refund"]["open_drift_events"] == 2
        assert rows["refund"]["health_computed_at"] is not None
        # The verdict rides the same row — the fleet stripe needs BOTH
        # judgments (green = healthy AND assured).
        assert rows["refund"]["verdict_status"] == "assured_with_exceptions"

    def test_never_computed_stays_null_not_healthy(self, harness):
        _seed_fleet(harness)
        self._add_health(harness, "refund", "t1")
        body = harness.fleet("t1", include_health=True).json()
        rows = {r["id"]: r for r in body["systems"]}

        # `nightly` has no rollup row: every health field must be null —
        # the UI renders that as `unknown`. Defaulting it to any state
        # here would be the optimistic-dashboard bug the engine forbids.
        assert rows["nightly"]["health_state"] is None
        assert rows["nightly"]["health_score"] is None
        assert rows["nightly"]["open_drift_events"] is None
        assert rows["nightly"]["health_computed_at"] is None
        assert rows["nightly"]["verdict_status"] is None

    def test_omitted_without_the_flag(self, harness):
        _seed_fleet(harness)
        self._add_health(harness, "refund", "t1")
        body = harness.fleet("t1").json()
        rows = {r["id"]: r for r in body["systems"]}

        # Row exists in the DB, but the flag was not passed — the summary
        # must not pay for (or expose) the join it wasn't asked for.
        assert rows["refund"]["health_state"] is None
        assert rows["refund"]["health_score"] is None

    def test_health_is_tenant_scoped(self, harness):
        _seed_fleet(harness)
        # A rollup row for the SAME system id under another tenant must
        # never be attached to t1's row.
        self._add_health(harness, "refund", "t2", health_state="healthy")
        body = harness.fleet("t1", include_health=True).json()
        rows = {r["id"]: r for r in body["systems"]}
        assert rows["refund"]["health_state"] is None


class TestMonthlyStatement:
    """`GET /admin/ai-systems/{id}/statement?month=` — the owner's artifact."""

    def _seed_statement_world(self, harness):
        _seed_fleet(harness)
        from datetime import datetime, timezone
        s = harness.Factory()
        try:
            when = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
            outside = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
            s.add(UsageLog(
                tenant_id="t1", resource_type="llm", resource_id="m",
                request_id="st-1", created_at=when, source="gateway",
                currency="USD", provider="openai", model_name="gpt-5.2",
                group_id="refund", total_tokens=100, estimated_cost=5.0,
            ))
            s.add(UsageLog(
                tenant_id="t1", resource_type="llm", resource_id="m",
                request_id="st-2", created_at=outside, source="gateway",
                currency="USD", provider="openai", model_name="gpt-5.2",
                group_id="refund", total_tokens=50, estimated_cost=2.0,
            ))
            s.add(AISystemRun(
                id="run-1", tenant_id="t1", root_task_id="run-1",
                ai_system_id="refund", started_at=when,
                terminal_state="completed", finalized_by="explicit",
                step_count=4, total_cost_usd=5.0, total_tokens=100,
            ))
            s.add(AISystemHealth(
                id="h-refund", tenant_id="t1", ai_system_id="refund",
                health_state="healthy", health_score=1.0,
                open_drift_events=0,
            ))
            s.commit()
        finally:
            s.close()

    def test_statement_composes_the_month(self, harness):
        self._seed_statement_world(harness)
        r = harness.client.get(
            "/v1/admin/ai-systems/refund/statement",
            headers=harness.login("t1"),
            params={"month": "2026-07"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["month"] == "2026-07"
        assert body["system"]["id"] == "refund"
        assert body["system"]["owner"] == "payments"
        assert body["system"]["lifecycle_stage"] == "active"
        # Only July's spend — June's $2 must not leak in.
        assert body["cost"]["totals"]["cost"] == pytest.approx(5.0)
        # Compare window (June) is attached for the delta narrative.
        assert body["cost"]["previous"]["totals"]["cost"] == pytest.approx(2.0)
        assert body["runs"]["terminal_states"]["total_runs"] == 1
        assert body["health"]["state"] == "healthy"

    def test_unknown_system_is_404(self, harness):
        _seed_fleet(harness)
        r = harness.client.get(
            "/v1/admin/ai-systems/nope/statement",
            headers=harness.login("t1"), params={"month": "2026-07"},
        )
        assert r.status_code == 404

    def test_malformed_month_is_422(self, harness):
        _seed_fleet(harness)
        r = harness.client.get(
            "/v1/admin/ai-systems/refund/statement",
            headers=harness.login("t1"), params={"month": "July-26"},
        )
        assert r.status_code == 422

    def test_never_computed_health_is_null_not_green(self, harness):
        self._seed_statement_world(harness)
        # helpdesk has no health row → statement carries null, not a guess.
        r = harness.client.get(
            "/v1/admin/ai-systems/helpdesk/statement",
            headers=harness.login("t1"), params={"month": "2026-07"},
        )
        assert r.status_code == 200
        assert r.json()["health"] is None


class TestGalleryCardFacts:
    """`include_card=true` — the gallery card's execution path and KPIs.

    Pins the honesty rules: gateway counts are effective over the ancestor
    chain (guardrails additive, limits counted once per path), disabled
    guardrails enforce nothing, replays never move a live KPI, and a system
    with no runs reports None — never 0 — for every per-run figure.
    """

    def _seed(self, harness):
        from datetime import datetime, timedelta, timezone

        from app.database.models.guardrail_config import GuardrailConfig, resource_group_guardrails

        s = harness.Factory()
        try:
            _add_group(s, "org", "t1", group_type=GroupType.ORGANIZATION, system_kind=None)
            _add_group(s, "refund", "t1", owner="payments", parent_group_id="org", hierarchy_level=1)
            _add_group(s, "idle", "t1")
            s.flush()
            org = s.get(ResourceGroup, "org")
            org.llm_global_limits = {"requests_per_minute": 10, "hard_stop": True}
            refund = s.get(ResourceGroup, "refund")
            # rpm is set at both levels: restrictive merge => ONE limit.
            refund.llm_global_limits = {"requests_per_minute": 5, "tokens_per_day": 1000}
            refund.declared_outcomes = [{"id": "approved"}, {"id": "declined"}]

            for gid, enabled in (("gr-org", True), ("gr-sys", True), ("gr-off", False)):
                s.add(GuardrailConfig(id=gid, tenant_id="t1", name=gid, enabled=enabled))
            s.flush()
            s.execute(resource_group_guardrails.insert(), [
                {"resource_group_id": "org", "guardrail_config_id": "gr-org"},
                {"resource_group_id": "refund", "guardrail_config_id": "gr-sys"},
                {"resource_group_id": "refund", "guardrail_config_id": "gr-off"},
            ])

            s.add(AgentIdentity(id="agent-1", tenant_id="t1", name="refund-worker"))
            s.flush()
            s.add(ResourceGroupAgentMember(tenant_id="t1", agent_id="agent-1", group_id="refund"))

            now = datetime.now(timezone.utc)
            for i, (ms, version) in enumerate(((100, "1.0"), (300, "1.1"), (200, "1.2"))):
                s.add(AISystemRun(
                    id=f"run-{i}", tenant_id="t1", root_task_id=f"task-{i}",
                    ai_system_id="refund", started_at=now - timedelta(hours=3 - i),
                    terminal_state="completed", finalized_by="explicit",
                    duration_ms=ms, action_count=4, total_cost_usd=0.5, total_tokens=100,
                    prompt_tokens=70, completion_tokens=30,
                    guardrail_block_count=1 if i == 0 else 0,
                    agent_impl_name="refund-agent", agent_impl_version=version,
                ))
            # A replay — a simulation, never a live KPI.
            s.add(AISystemRun(
                id="run-replay", tenant_id="t1", root_task_id="task-replay",
                ai_system_id="refund", started_at=now, is_replay=True,
                terminal_state="completed", finalized_by="explicit",
                duration_ms=99999, action_count=100, total_cost_usd=50.0,
                agent_impl_name="refund-agent", agent_impl_version="9.9",
            ))
            s.commit()
        finally:
            s.close()

    def _rows(self, harness, **params):
        r = harness.fleet("t1", include_card=True, **params)
        assert r.status_code == 200, r.text
        return {row["id"]: row for row in r.json()["systems"]}

    def test_card_is_opt_in(self, harness):
        self._seed(harness)
        rows = {row["id"]: row for row in harness.fleet("t1").json()["systems"]}
        assert rows["refund"]["card"] is None

    def test_gateway_counts_are_effective_over_the_chain(self, harness):
        self._seed(harness)
        gw = self._rows(harness)["refund"]["card"]["gateway"]
        assert gw["guardrails"] == 2, "parent's binding governs the child; a disabled config does not"
        assert gw["limits"] == 2, "rpm at two levels is one limit; the hard_stop flag is not a limit"
        assert gw["blocks"] == 1

    def test_agent_reports_latest_live_implementation(self, harness):
        self._seed(harness)
        agent = self._rows(harness)["refund"]["card"]["agent"]
        assert agent["identity_id"] == "agent-1"
        assert agent["identity_name"] == "refund-worker"
        assert agent["impl_version"] == "1.2", "the replay's 9.9 must not win"
        assert agent["declared_outcomes"] == 2

    def test_kpis_exclude_replays_and_use_nearest_rank(self, harness):
        self._seed(harness)
        k = self._rows(harness)["refund"]["card"]["kpis"]
        assert k["traces"] == 3
        assert k["spans"] == 12
        assert k["cost_usd"] == pytest.approx(1.5)
        assert k["p50_latency_ms"] == 200
        assert k["p95_latency_ms"] == 300
        assert (k["tokens"], k["input_tokens"], k["output_tokens"]) == (300, 210, 90)
        assert "feedback_up" not in k

    def test_resources_are_what_the_system_can_reach(self, harness):
        """Regression: the card counted only DIRECT bindings, so a system
        running on a model bound to its org unit showed "0 models", and the
        auto-bound Agent Skills VMCP inflated the MCP count by one.

        Resources are additive and inherited while `inherit_from_parent`
        holds (the proxy's `model_in_group_chain` rule); platform system
        servers (`http://system/…`) are plumbing, not the system's tools.
        """
        from app.database.models.agent_card import AgentCard
        from app.database.models.mcp_server import MCPServer
        from app.database.models.resource_group import resource_group_agent_cards

        self._seed(harness)
        s = harness.Factory()
        try:
            # Catalogue rows drag in provider/routing-group FK webs this
            # endpoint never reads; the counts are over the bindings joined
            # to each row's `enabled` flag, so FKs are suspended for seeding.
            # SQLite ignores this PRAGMA inside an open transaction, so it
            # has to be the session's first statement.
            s.execute(text("PRAGMA foreign_keys=OFF"))
            _add_group(s, "walled", "t1", parent_group_id="org", hierarchy_level=1)
            s.flush()
            s.get(ResourceGroup, "walled").inherit_resources = False
            s.execute(LLModel.__table__.insert(), [
                {"id": m, "tenant_id": "t1", "name": m, "provider_id": "p", "model_name": m,
                 "mode": "chat", "api_url_auto_discovered": False, "enabled": on}
                for m, on in (("m-org", True), ("m-sys", True), ("m-off", False))
            ])
            s.execute(MCPServer.__table__.insert(), [
                {"id": "crm", "tenant_id": "t1", "name": "crm", "base_url": "https://crm.example", "enabled": True},
                {"id": "docs", "tenant_id": "t1", "name": "docs", "base_url": "https://docs.example", "enabled": True},
                {"id": "skills-vmcp", "tenant_id": "t1", "name": "Agent Skills",
                 "base_url": "http://system/agent-skill-server", "enabled": True},
            ])
            s.execute(ServerConfig.__table__.insert(), [
                {"id": "cfg-crm", "tenant_id": "t1", "name": "crm", "mcp_server_id": "crm", "enabled": True},
                {"id": "cfg-docs", "tenant_id": "t1", "name": "docs", "mcp_server_id": "docs", "enabled": True},
                {"id": "cfg-skills", "tenant_id": "t1", "name": "skills", "mcp_server_id": "skills-vmcp", "enabled": True},
            ])
            s.execute(AgentSkill.__table__.insert(), [
                {"id": "sk-1", "tenant_id": "t1", "name": "sk", "description": "d",
                 "skill_md_content": "x", "skill_md_hash": "h", "enabled": True},
            ])
            s.execute(AgentCard.__table__.insert(), [
                {"id": "card-1", "tenant_id": "t1", "name": "fraud", "card_json": {}, "enabled": True},
            ])
            s.execute(resource_group_llm_models.insert(), [
                {"id": "b1", "resource_group_id": "org", "llm_model_id": "m-org"},
                {"id": "b2", "resource_group_id": "refund", "llm_model_id": "m-sys"},
                {"id": "b3", "resource_group_id": "org", "llm_model_id": "m-off"},
                {"id": "b4", "resource_group_id": "walled", "llm_model_id": "m-sys"},
            ])
            s.execute(resource_group_server_configs.insert(), [
                {"id": "c1", "resource_group_id": "org", "server_config_id": "cfg-crm"},
                {"id": "c3", "resource_group_id": "org", "server_config_id": "cfg-docs"},
                {"id": "c2", "resource_group_id": "refund", "server_config_id": "cfg-skills"},
            ])
            s.execute(resource_group_agent_skills.insert(), [
                {"id": "k1", "resource_group_id": "refund", "agent_skill_id": "sk-1"},
            ])
            s.execute(resource_group_agent_cards.insert(), [
                {"resource_group_id": "org", "agent_card_id": "card-1"},
            ])
            s.execute(text("PRAGMA foreign_keys=ON"))
            s.commit()
        finally:
            s.close()

        rows = self._rows(harness)
        res = rows["refund"]["card"]["resources"]
        assert res["models"] == 2, "own + inherited from the org unit; a disabled model is not reachable"
        assert res["mcp_servers"] == 2, "the org's two servers count; the Agent Skills VMCP does not"
        assert res["skills"] == 1
        assert res["delegate_agents"] == 1, "agent cards inherit like any resource"

        walled = rows["walled"]["card"]["resources"]
        assert (walled["models"], walled["mcp_servers"], walled["delegate_agents"]) == (1, 0, 0), (
            "inherit_from_parent=false stops the walk: only its own binding counts"
        )

        # The table's composition columns read the same function: the two
        # views of one system can never disagree.
        for sid in ("refund", "walled"):
            members = rows[sid]["composition"]["members"]
            card = rows[sid]["card"]["resources"]
            assert (members["models"], members["mcp_servers"], members["skills"]) == (
                card["models"], card["mcp_servers"], card["skills"]
            )
        table_only = {r["id"]: r for r in harness.fleet("t1").json()["systems"]}
        assert table_only["refund"]["composition"]["members"]["models"] == 2, (
            "the table counts reachable resources even without include_card"
        )

    def _seed_service(self, harness):
        """`screener` — a shared service with no runs of its own, called five
        times inside `refund`'s runs: 2 answered, 1 deviation (undeclared
        outcome), 1 failed, 1 refused by the gateway."""
        from datetime import datetime, timedelta, timezone

        from app.database.models.ai_system_run_segment import AISystemRunSegment

        self._seed(harness)
        s = harness.Factory()
        try:
            _add_group(s, "screener", "t1")
            s.flush()
            now = datetime.now(timezone.utc)
            # (run, outcome, conformance, cause, active_ms, cost, tokens, prompt, completion)
            calls = [
                ("run-0", "answered", "declared", None, 400, 0.01, 300, 200, 100),
                ("run-1", "answered", "declared", None, 600, 0.01, 300, 220, 80),
                ("run-2", "answered", "undeclared", None, 500, 0.01, 300, 250, 50),
                ("run-c", "failed", None, "upstream_error", 900, 0.0, 0, 0, 0),
                ("run-d", "failed", None, "blocked_by_policy", 0, 0.0, 0, 0, 0),
            ]
            for rid in ("run-c", "run-d"):
                s.add(AISystemRun(
                    id=rid, tenant_id="t1", root_task_id=f"task-{rid}", ai_system_id="refund",
                    started_at=now - timedelta(hours=1), terminal_state="completed",
                    finalized_by="explicit",
                ))
            s.flush()
            for i, (rid, outcome, conf, cause, ms, cost, tok, pin, pout) in enumerate(calls):
                task = f"task-{rid[4:]}" if rid in ("run-0", "run-1", "run-2") else f"task-{rid}"
                s.add(AISystemRunSegment(
                    id=f"seg-{i}", run_id=rid, tenant_id="t1", root_task_id=task,
                    root_system_id="refund", segment_system_id="screener", caller_system_id="refund",
                    first_action_at=now - timedelta(minutes=30), last_action_at=now - timedelta(minutes=29),
                    active_ms=0, action_count=2, total_tokens=tok, cost_usd=cost,
                    outcome=outcome, outcome_conformance=conf, error_cause=cause,
                    guardrail_block_count=1 if cause == "blocked_by_policy" else 0,
                    agent_impl_name="fraud-screener", agent_impl_version=f"0.{i}.0",
                ))
                if tok or cause == "upstream_error":
                    s.add(ProxyLog(
                        id=f"pl-{i}", tenant_id="t1", proxy_type="llm", end_user_session_id="x",
                        http_method="POST", endpoint="/v1/chat", success=True,
                        resource_group_id="screener", root_task_id=task,
                        llm_prompt_tokens=pin, llm_completion_tokens=pout, duration_ms=ms,
                    ))
            # The CALLER's own call in the same run must not leak into the split.
            s.add(ProxyLog(
                id="pl-caller", tenant_id="t1", proxy_type="llm", end_user_session_id="x",
                http_method="POST", endpoint="/v1/chat", success=True,
                resource_group_id="refund", root_task_id="task-0",
                llm_prompt_tokens=9999, llm_completion_tokens=9999, duration_ms=99999,
            ))
            s.commit()
        finally:
            s.close()

    def test_shared_service_reports_the_calls_it_served(self, harness):
        """Regression: a shared service (called over A2A inside another
        system's runs) roots no runs, so the card read 0 traces, $0 and no
        tokens although it did real work. With no own run in the window the
        KPIs come from its segments, using THE served-call
        classification; a system with own runs keeps the runs basis."""
        self._seed_service(harness)
        rows = self._rows(harness)
        k = rows["screener"]["card"]["kpis"]
        assert k["basis"] == "served_calls"
        assert k["traces"] == 5 and k["spans"] == 10
        # answered 2 (declared) / judged 4 (blocked excluded; the deviation counts against)
        assert k["success_rate"] == pytest.approx(0.5)
        assert k["failed"] == 1
        # The service's own call time per served call (400/500/600/900 ms);
        # the refused call made no call, and the caller's 99 s call is not its.
        assert (k["p50_latency_ms"], k["p95_latency_ms"]) == (500, 900)
        assert k["cost_usd"] == pytest.approx(0.03)
        assert (k["tokens"], k["input_tokens"], k["output_tokens"]) == (900, 670, 230)
        assert rows["screener"]["card"]["gateway"]["blocks"] == 1
        agent = rows["screener"]["card"]["agent"]
        # All five calls share a timestamp; the newest by (time, id) is seg-4.
        assert (agent["impl_name"], agent["impl_version"]) == ("fraud-screener", "0.4.0"), (
            "a shared service's release comes from the calls it served"
        )
        assert rows["refund"]["card"]["agent"]["impl_version"] == "1.2", "a system's own runs still win"
        # The caller keeps its own runs basis.
        assert rows["refund"]["card"]["kpis"]["basis"] == "runs"

    def test_served_calls_list_the_services_slice_of_each_run(self, harness):
        """The Runs tab of a shared service: its segments, newest first, with
        the SERVICE's figures — not the caller run's totals — and the caller
        named so the operator can open the run it served."""
        self._seed_service(harness)
        h = harness.login("t1")
        r = harness.client.get(
            "/v1/admin/ai-systems/screener/served-calls", headers=h, params={"period_hours": 24}
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == 5
        by_run = {c["run_id"]: c for c in body["calls"]}
        first = by_run["run-0"]
        assert first["caller_system_id"] == "refund"
        assert first["caller_system_name"] == "Refund"
        assert first["outcome"] == "answered"
        assert (first["input_tokens"], first["output_tokens"], first["duration_ms"]) == (200, 100, 400), (
            "the caller's own 9999-token call in the same run is not the service's"
        )
        assert by_run["run-d"]["outcome"] == "blocked", "a gateway refusal reads as blocked, not failed"
        assert by_run["run-c"]["outcome"] == "failed"

        # The caller is not a service of itself, and another tenant sees nothing.
        own = harness.client.get("/v1/admin/ai-systems/refund/served-calls", headers=h)
        assert own.json()["total"] == 0
        other = harness.client.get(
            "/v1/admin/ai-systems/screener/served-calls", headers=harness.login("t2")
        )
        assert other.json()["total"] == 0

    def test_no_runs_is_none_not_zero(self, harness):
        self._seed(harness)
        k = self._rows(harness)["idle"]["card"]["kpis"]
        assert k["traces"] == 0
        assert k["success_rate"] is None
        assert k["p50_latency_ms"] is None and k["p95_latency_ms"] is None
