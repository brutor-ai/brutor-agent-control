# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
End-to-end tests for the contracts + lifecycle API.

What is worth defending at the HTTP boundary:

  * **The gate is readable before it is enforced.** `GET /lifecycle` returns
    the full requirement checklist for every reachable stage, so the UI can
    show what is missing instead of a red toast after the click. The POST
    enforces the same evaluation, because a UI check is a convenience and
    never a control.
  * **A refusal carries the whole list.** One round trip, not one per
    requirement.
  * **Tenant isolation.** A contract is the record of what a customer's
    system was allowed to do.
  * **Contracts only describe AI Systems.** Pointing this at a plain access
    group is a 422, not an empty list.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import uuid
from typing import Dict

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.endpoints.admin.admin_rbac import create_admin_users_router
from app.api.v1.endpoints.admin.ai_system_contracts import (
    create_ai_system_contracts_router,
)
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
from app.database.models.agent_skill import AgentSkill, resource_group_agent_skills
from app.database.models.ai_system_contract import (
    CONTRACT_ACTIVE,
    CONTRACT_DRAFT,
    CONTRACT_REJECTED,
    AISystemContract,
)
from app.database.models.ai_system_evidence import AISystemEvidence
from app.database.models.ai_system_liveness import AISystemLivenessExpectation
from app.database.models.ai_system_run import AISystemRun
from app.database.models.end_user import EndUser
from app.database.models.idp_server import IdPServer
from app.database.models.llm_model import LLModel
from app.database.models.resource_group import (
    GroupType,
    ResourceGroup,
    resource_group_llm_models,
    resource_group_mcp_capabilities,
    resource_group_server_configs,
)
from app.database.models.server_config import ServerConfig
from app.database.models.tenant import Tenant

JWT_SECRET = os.environ["JWT_SECRET"]

_BY_NAME = [
    "llm_catalog",
    "llm_routing_group",
    "llm_providers",
    "llm_provider_catalog",
    "end_user_groups",
    "resource_group_end_user_groups",
    "guardrail_configs",
    "resource_group_guardrails",
    "argument_policies",
    "resource_group_argument_policies",
    "semantic_cache_configs",
    "resource_group_cache_configs",
    "ai_system_response_policies",
    # Part of the closure once set.
    "ai_system_data_scopes",
    "ai_system_reach_rules",
    "mcp_servers",
    "ai_system_lifecycle_events",  # a170_lifecycle_events — transitions write a record
    # The identity floor of every way into `active` reads the calls and the
    # credentials that made them.
    "proxy_logs",
    "api_keys",
]

REQUIRED_TABLES = [Base.metadata.tables[n] for n in _BY_NAME] + [
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
    ServerConfig.__table__,
    AgentSkill.__table__,
    AgentIdentity.__table__,
    ResourceGroupAgentMember.__table__,
    AISystemContract.__table__,
    AISystemEvidence.__table__,
    AISystemLivenessExpectation.__table__,
    AISystemRun.__table__,
    resource_group_llm_models,
    resource_group_mcp_capabilities,
    resource_group_server_configs,
    resource_group_agent_skills,
]


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

    def mint(self, tenant, sid):
        return self.client.post(
            f"/v1/admin/ai-systems/{sid}/contracts", headers=self.login(tenant)
        )

    def list(self, tenant, sid):
        return self.client.get(
            f"/v1/admin/ai-systems/{sid}/contracts", headers=self.login(tenant)
        )

    def current(self, tenant, sid):
        return self.client.get(
            f"/v1/admin/ai-systems/{sid}/contracts/current", headers=self.login(tenant)
        )

    def diff(self, tenant, sid, version):
        return self.client.get(
            f"/v1/admin/ai-systems/{sid}/contracts/{version}/diff",
            headers=self.login(tenant),
        )

    def act(self, tenant, cid, action, **body):
        return self.client.post(
            f"/v1/admin/contracts/{cid}/{action}",
            headers=self.login(tenant),
            json=body or {},
        )

    def lifecycle(self, tenant, sid):
        return self.client.get(
            f"/v1/admin/ai-systems/{sid}/lifecycle", headers=self.login(tenant)
        )

    def transition(self, tenant, sid, **body):
        return self.client.post(
            f"/v1/admin/ai-systems/{sid}/lifecycle",
            headers=self.login(tenant),
            json=body,
        )


@pytest.fixture()
def harness():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    # a184_composite_ai_systems — composition, liveness and conformance read these.
    _extra = [Base.metadata.tables[n] for n in ['ai_system_run_segments', 'ai_system_dependencies', 'agent_cards', 'ai_system_liveness_expectations', 'kb_collections', 'resource_group_kb_collections']
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
    logger = BrutorLogger("test-ai-system-contracts")
    app.include_router(create_admin_users_router(logger), prefix="/v1")
    app.include_router(create_ai_system_contracts_router(logger), prefix="/v1")
    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)

    auth = UnifiedAuthService(jwt_secret=JWT_SECRET)
    s = Factory()
    try:
        for tid in ("t1", "t2"):
            s.add(
                Tenant(
                    id=tid, name=tid, display_name=tid.upper(),
                    organization_name=tid.upper(),
                    primary_contact_email=f"admin@{tid}.example",
                    is_active=True, is_default=False, is_trial=False,
                    suspended=False, billing_plan="self_hosted", features={},
                )
            )
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
                s.add(
                    TenantRolePermission(
                        id=f"perm-{uuid.uuid4().hex[:10]}",
                        role_id=role.id, permission=p.value,
                    )
                )
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


def add_system(session, gid, tenant="t1", group_type=GroupType.AI_SYSTEM, **kwargs):
    session.add(
        ResourceGroup(
            id=gid, tenant_id=tenant, name=gid, hierarchy_level=0,
            inherit_resources=True, is_active=True, is_system=False,
            group_type=group_type,
            system_kind="agent" if group_type == GroupType.AI_SYSTEM else None,
            **kwargs,
        )
    )
    session.flush()


def seed(harness, fn):
    s = harness.Factory()
    try:
        fn(s)
        s.commit()
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Minting
# ---------------------------------------------------------------------------


class TestMinting:
    def test_the_first_mint_creates_version_one(self, harness):
        seed(harness, lambda s: add_system(s, "sys"))
        r = harness.mint("t1", "sys")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["created"] is True
        assert body["contract"]["version"] == 1
        assert body["contract"]["status"] == CONTRACT_DRAFT
        assert body["contract"]["contract_hash"].startswith("sha256:")

    def test_minting_twice_with_no_change_is_idempotent(self, harness):
        seed(harness, lambda s: add_system(s, "sys"))
        harness.mint("t1", "sys")
        second = harness.mint("t1", "sys").json()
        assert second["created"] is False
        assert second["contract"]["version"] == 1
        assert harness.list("t1", "sys").json()["total"] == 1

    def test_the_full_closure_comes_back_on_a_mint(self, harness):
        seed(harness, lambda s: add_system(s, "sys", owner="team"))
        closure = harness.mint("t1", "sys").json()["contract"]["resolved_closure"]
        assert closure["ai_system"]["owner"] == "team"
        assert closure["schema_version"] >= 1

    def test_the_list_view_omits_the_closure(self, harness):
        # A closure is large and nobody reads twenty at once.
        seed(harness, lambda s: add_system(s, "sys"))
        harness.mint("t1", "sys")
        row = harness.list("t1", "sys").json()["contracts"][0]
        assert row.get("resolved_closure") is None

    def test_a_plain_access_group_is_a_422(self, harness):
        # Not a 404 and not an empty list: contracts describe a governed
        # workload, and pointing this at an access group is a mistake worth
        # naming.
        seed(harness, lambda s: add_system(s, "grp", group_type=GroupType.TEAM))
        r = harness.mint("t1", "grp")
        assert r.status_code == 422
        assert "not an AI System" in r.text

    def test_an_unknown_system_is_a_404(self, harness):
        assert harness.mint("t1", "nope").status_code == 404


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    def test_a_tenant_cannot_see_another_tenants_system(self, harness):
        seed(harness, lambda s: add_system(s, "theirs", tenant="t2"))
        assert harness.mint("t1", "theirs").status_code == 404
        assert harness.list("t1", "theirs").status_code == 404

    def test_a_tenant_cannot_promote_another_tenants_contract(self, harness):
        seed(harness, lambda s: add_system(s, "theirs", tenant="t2"))
        cid = harness.mint("t2", "theirs").json()["contract"]["id"]
        assert harness.act("t1", cid, "promote").status_code == 404

        s = harness.Factory()
        try:
            assert (
                s.query(AISystemContract).filter_by(id=cid).one().status
                == CONTRACT_DRAFT
            ), "a cross-tenant 404 must not have mutated the row on the way out"
        finally:
            s.close()


# ---------------------------------------------------------------------------
# Approval and promotion
# ---------------------------------------------------------------------------


class TestApprovalAndPromotion:
    def _minted(self, harness):
        seed(harness, lambda s: add_system(s, "sys", owner="team", intended_use="x"))
        return harness.mint("t1", "sys").json()["contract"]["id"]

    def test_approval_records_who(self, harness):
        cid = self._minted(harness)
        body = harness.act("t1", cid, "approve").json()
        assert body["approved_by"] == "admin_t1"
        assert body["is_approved"] is True

    def test_promotion_makes_it_active(self, harness):
        cid = self._minted(harness)
        assert harness.act("t1", cid, "approve").status_code == 200
        body = harness.act("t1", cid, "promote").json()
        assert body["status"] == CONTRACT_ACTIVE
        assert body["promoted_at"] is not None
        assert harness.current("t1", "sys").json()["id"] == cid

    def test_promoting_twice_is_a_409(self, harness):
        cid = self._minted(harness)
        harness.act("t1", cid, "approve")
        harness.act("t1", cid, "promote")
        r = harness.act("t1", cid, "promote")
        assert r.status_code == 409
        assert "draft" in r.text

    def test_rejection_needs_a_reason(self, harness):
        cid = self._minted(harness)
        assert harness.act("t1", cid, "reject").status_code == 422
        body = harness.act("t1", cid, "reject", reason="too broad").json()
        assert body["status"] == CONTRACT_REJECTED
        assert body["rejection_reason"] == "too broad"

    def test_a_rejected_contract_cannot_be_promoted(self, harness):
        cid = self._minted(harness)
        harness.act("t1", cid, "reject", reason="no")
        assert harness.act("t1", cid, "promote").status_code == 409

    def test_an_unapproved_contract_cannot_be_promoted(self, harness):
        cid = self._minted(harness)
        r = harness.act("t1", cid, "promote")
        assert r.status_code == 409, r.text
        assert "not been approved" in r.text
        assert "/approve" in r.text, "the refusal must say how to fix it"

        s = harness.Factory()
        try:
            row = s.query(AISystemContract).filter_by(id=cid).one()
            assert row.status == CONTRACT_DRAFT and row.promoted_at is None
        finally:
            s.close()
        assert harness.current("t1", "sys").status_code == 404

    def test_a_system_with_no_active_contract_says_so(self, harness):
        seed(harness, lambda s: add_system(s, "sys"))
        r = harness.current("t1", "sys")
        assert r.status_code == 404
        assert "never been pinned" in r.text


# ---------------------------------------------------------------------------
# Conformance and diff
# ---------------------------------------------------------------------------


class TestConformance:
    def test_a_freshly_promoted_system_is_not_drifted(self, harness):
        seed(harness, lambda s: add_system(s, "sys"))
        cid = harness.mint("t1", "sys").json()["contract"]["id"]
        harness.act("t1", cid, "approve")
        harness.act("t1", cid, "promote")
        assert harness.list("t1", "sys").json()["config_drifted"] is False

    def test_a_config_change_after_promotion_shows_as_drift(self, harness):
        seed(harness, lambda s: add_system(s, "sys", owner="a"))
        cid = harness.mint("t1", "sys").json()["contract"]["id"]
        harness.act("t1", cid, "approve")
        harness.act("t1", cid, "promote")

        s = harness.Factory()
        try:
            s.query(ResourceGroup).filter_by(id="sys").one().owner = "b"
            s.commit()
        finally:
            s.close()

        body = harness.list("t1", "sys").json()
        assert body["config_drifted"] is True
        assert body["active_version"] == 1


class TestDiff:
    def test_the_first_version_has_an_empty_diff_rather_than_a_500(self, harness):
        seed(harness, lambda s: add_system(s, "sys"))
        harness.mint("t1", "sys")
        summary = harness.diff("t1", "sys", 1).json()["summary"]
        assert summary["widened_count"] == 0
        assert summary["has_widened_permissions"] is False

    def test_a_widening_is_reported_as_such(self, harness):
        seed(harness, lambda s: add_system(s, "sys", autonomy_level="restricted"))
        harness.mint("t1", "sys")

        s = harness.Factory()
        try:
            s.query(ResourceGroup).filter_by(id="sys").one().autonomy_level = "autonomous"
            s.commit()
        finally:
            s.close()

        harness.mint("t1", "sys")
        body = harness.diff("t1", "sys", 2).json()
        assert body["diff_from_version"] == 1
        assert body["summary"]["has_widened_permissions"] is True
        assert "autonomy" in body["summary"]["widened"][0]["detail"]

    def test_a_narrowing_does_not_set_the_gate_flag(self, harness):
        seed(harness, lambda s: add_system(s, "sys", autonomy_level="autonomous"))
        harness.mint("t1", "sys")

        s = harness.Factory()
        try:
            s.query(ResourceGroup).filter_by(id="sys").one().autonomy_level = "restricted"
            s.commit()
        finally:
            s.close()

        harness.mint("t1", "sys")
        summary = harness.diff("t1", "sys", 2).json()["summary"]
        assert summary["has_widened_permissions"] is False
        assert summary["narrowed_count"] == 1

    def test_an_unknown_version_is_a_404(self, harness):
        seed(harness, lambda s: add_system(s, "sys"))
        assert harness.diff("t1", "sys", 99).status_code == 404


# ---------------------------------------------------------------------------
# The lifecycle gate over HTTP
# ---------------------------------------------------------------------------


class TestLifecycleReadable:
    def test_it_lists_what_each_reachable_stage_requires(self, harness):
        seed(harness, lambda s: add_system(s, "sys", lifecycle_stage="proposed"))
        body = harness.lifecycle("t1", "sys").json()
        assert body["current_stage"] == "proposed"
        stages = {t["to_stage"] for t in body["transitions"]}
        assert "approved" in stages
        # Stage skips are not offered at all.
        assert "retired" not in stages

    def test_requirements_come_back_before_anything_is_clicked(self, harness):
        seed(harness, lambda s: add_system(s, "sys", lifecycle_stage="proposed"))
        approved = next(
            t
            for t in harness.lifecycle("t1", "sys").json()["transitions"]
            if t["to_stage"] == "approved"
        )
        assert approved["allowed"] is False
        assert {r["key"] for r in approved["unmet"]} == {
            "contract_minted",
            "owner_set",
            "intended_use_set",
        }
        assert all(r["detail"] for r in approved["unmet"])

    def test_the_resolved_autonomy_is_reported(self, harness):
        seed(
            harness,
            lambda s: add_system(
                s, "sys", lifecycle_stage="proposed", autonomy_level="restricted"
            ),
        )
        assert harness.lifecycle("t1", "sys").json()["autonomy_level"] == "restricted"


class TestLifecycleEnforced:
    def _ready(self, harness):
        seed(
            harness,
            lambda s: add_system(
                s, "sys", lifecycle_stage="proposed", owner="team",
                intended_use="does a thing",
            ),
        )
        harness.mint("t1", "sys")

    def test_a_fully_evidenced_transition_succeeds(self, harness):
        self._ready(harness)
        r = harness.transition("t1", "sys", to_stage="approved")
        assert r.status_code == 200, r.text
        assert r.json()["to_stage"] == "approved"

        s = harness.Factory()
        try:
            assert (
                s.query(ResourceGroup).filter_by(id="sys").one().lifecycle_stage
                == "approved"
            )
        finally:
            s.close()

    def test_a_refusal_carries_every_missing_requirement(self, harness):
        # One round trip, not one per requirement.
        seed(harness, lambda s: add_system(s, "sys", lifecycle_stage="proposed"))
        r = harness.transition("t1", "sys", to_stage="approved")
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert len(detail["decision"]["unmet"]) == 3
        assert "owner" in detail["message"].lower()

    def test_a_refused_transition_does_not_change_the_stage(self, harness):
        seed(harness, lambda s: add_system(s, "sys", lifecycle_stage="proposed"))
        harness.transition("t1", "sys", to_stage="approved")
        s = harness.Factory()
        try:
            assert (
                s.query(ResourceGroup).filter_by(id="sys").one().lifecycle_stage
                == "proposed"
            )
        finally:
            s.close()

    def test_skipping_a_stage_is_refused(self, harness):
        # The gate would otherwise be bypassable by jumping two stages.
        self._ready(harness)
        r = harness.transition("t1", "sys", to_stage="retired")
        assert r.status_code == 409
        assert "skips a lifecycle stage" in r.text

    def test_high_risk_needs_a_named_human_and_an_impact_assessment(self, harness):
        seed(
            harness,
            lambda s: add_system(
                s, "sys", lifecycle_stage="proposed", owner="team",
                intended_use="x", eu_ai_act_risk_tier="high",
            ),
        )
        harness.mint("t1", "sys")
        assert harness.transition("t1", "sys", to_stage="approved").status_code == 409
        # A named human alone is not enough at high risk (a146_ai_system_evidence): the
        # refusal names the missing impact assessment.
        r = harness.transition("t1", "sys", to_stage="approved", approved_by="alice")
        assert r.status_code == 409, r.text
        unmet = {u["key"] for u in r.json()["detail"]["decision"]["unmet"]}
        assert "impact_assessment_on_file" in unmet

        seed(
            harness,
            lambda s: s.add(
                AISystemEvidence(
                    id="ev-fria", tenant_id="t1", ai_system_id="sys",
                    kind="impact_assessment", title="FRIA 2026",
                )
            ),
        )
        r = harness.transition("t1", "sys", to_stage="approved", approved_by="alice")
        assert r.status_code == 200, r.text

    def test_an_unknown_stage_is_a_422(self, harness):
        self._ready(harness)
        r = harness.transition("t1", "sys", to_stage="mostly_done")
        assert r.status_code == 422

    def test_going_backwards_needs_no_evidence(self, harness):
        seed(harness, lambda s: add_system(s, "sys", lifecycle_stage="active"))
        assert harness.transition("t1", "sys", to_stage="approved").status_code == 200

    def test_deprecation_needs_a_successor_decision(self, harness):
        seed(harness, lambda s: add_system(s, "sys", lifecycle_stage="active"))
        assert harness.transition("t1", "sys", to_stage="deprecated").status_code == 409
        r = harness.transition("t1", "sys", to_stage="deprecated", no_successor=True)
        assert r.status_code == 200, r.text


class TestRetirementCountdown:
    """a159_system_succession — a deprecated system says *when* it can be retired.

    "Has run in the last 30 days" is true and useless: it leaves an
    operator with nothing to do but remember to come back, so deprecated
    systems accumulate forever. The gate now returns the date.
    """

    def _deprecate(self, harness):
        return harness.transition(
            "t1", "old", to_stage="deprecated", no_successor=True
        )

    def test_a_system_that_never_ran_is_retirable_at_once(self, harness):
        seed(harness, lambda s: add_system(s, "old", lifecycle_stage="active"))
        self._deprecate(harness)
        r = harness.lifecycle("t1", "old").json()["retirement"]
        assert r["retirable"] is True
        assert r["last_run_at"] is None
        assert r["days_remaining"] == 0

    def test_a_recent_run_reports_the_date_it_clears(self, harness):
        from datetime import datetime, timedelta, timezone
        from app.database.models.ai_system_run import AISystemRun

        started = datetime.now(timezone.utc) - timedelta(days=4)

        def _fn(s):
            add_system(s, "old", lifecycle_stage="active")
            s.add(AISystemRun(
                id="run-1", tenant_id="t1", ai_system_id="old",
                root_task_id="task-1", started_at=started,
                terminal_state="completed", finalized_by="idle",
            ))
        seed(harness, _fn)
        self._deprecate(harness)

        r = harness.lifecycle("t1", "old").json()["retirement"]
        assert r["retirable"] is False
        # 30-day window, ran 4 days ago -> ~26 days left. The point is that
        # a date exists at all, so allow a day of slack around midnight.
        assert 25 <= r["days_remaining"] <= 27
        assert r["retirable_at"].startswith(
            (started + timedelta(days=30)).date().isoformat()
        )

    def test_the_unmet_requirement_carries_the_date_too(self, harness):
        from datetime import datetime, timedelta, timezone
        from app.database.models.ai_system_run import AISystemRun

        def _fn(s):
            add_system(s, "old", lifecycle_stage="active")
            s.add(AISystemRun(
                id="run-1", tenant_id="t1", ai_system_id="old",
                root_task_id="task-1",
                started_at=datetime.now(timezone.utc) - timedelta(days=1),
                terminal_state="completed", finalized_by="idle",
            ))
        seed(harness, _fn)
        self._deprecate(harness)

        body = harness.lifecycle("t1", "old").json()
        retire = next(t for t in body["transitions"] if t["to_stage"] == "retired")
        unmet = next(r for r in retire["unmet"] if r["key"] == "no_recent_runs")
        # The operator should not have to compute the date themselves.
        assert "Retirable in" in unmet["detail"]

    def test_readiness_is_absent_until_deprecated(self, harness):
        seed(harness, lambda s: add_system(s, "old", lifecycle_stage="active"))
        assert harness.lifecycle("t1", "old").json()["retirement"] is None


class TestSuccession:
    """a159_system_succession — the deprecate answer is recorded, not just used and dropped.

    The gate always asked "successor, or explicitly none?", but nothing
    persisted the answer, so a deprecated system could not say where its
    capability had gone. Worse, the console sent `no_successor: true`
    unconditionally — asserting withdrawal on the operator's behalf.
    """

    def _deprecate(self, harness, **kw):
        return harness.transition("t1", "old", to_stage="deprecated", **kw)

    def _seed_pair(self, harness):
        def _fn(s):
            add_system(s, "old", lifecycle_stage="active")
            add_system(s, "new", lifecycle_stage="active")
        seed(harness, _fn)

    def test_a_named_successor_is_recorded(self, harness):
        self._seed_pair(harness)
        assert self._deprecate(harness, successor_id="new").status_code == 200

        s = harness.Factory()
        try:
            old = s.query(ResourceGroup).filter_by(id="old").one()
            assert old.successor_system_id == "new"
            assert old.no_successor_declared is False
        finally:
            s.close()

    def test_explicit_withdrawal_is_recorded_as_such(self, harness):
        # "No successor" is a real statement — the capability is going away,
        # not moving — so it has to be distinguishable from never asking.
        seed(harness, lambda s: add_system(s, "old", lifecycle_stage="active"))
        assert self._deprecate(harness, no_successor=True).status_code == 200

        s = harness.Factory()
        try:
            old = s.query(ResourceGroup).filter_by(id="old").one()
            assert old.successor_system_id is None
            assert old.no_successor_declared is True
        finally:
            s.close()

    def test_a_running_system_has_been_asked_nothing(self, harness):
        seed(harness, lambda s: add_system(s, "old", lifecycle_stage="active"))
        s = harness.Factory()
        try:
            old = s.query(ResourceGroup).filter_by(id="old").one()
            assert old.successor_system_id is None
            assert old.no_successor_declared is None
        finally:
            s.close()

    def test_a_system_cannot_succeed_itself(self, harness):
        seed(harness, lambda s: add_system(s, "old", lifecycle_stage="active"))
        r = self._deprecate(harness, successor_id="old")
        assert r.status_code == 422, r.text

    def test_an_unknown_successor_is_refused(self, harness):
        # A dangling pointer reads as a migration path and isn't one.
        seed(harness, lambda s: add_system(s, "old", lifecycle_stage="active"))
        r = self._deprecate(harness, successor_id="does-not-exist")
        assert r.status_code == 422, r.text

    def test_a_plain_group_cannot_be_a_successor(self, harness):
        def _fn(s):
            add_system(s, "old", lifecycle_stage="active")
            add_system(s, "team", group_type=GroupType.TEAM)
        seed(harness, _fn)
        r = self._deprecate(harness, successor_id="team")
        assert r.status_code == 422, r.text

    def test_a_successor_from_another_tenant_is_refused(self, harness):
        def _fn(s):
            add_system(s, "old", lifecycle_stage="active")
            add_system(s, "theirs", tenant="t2", lifecycle_stage="active")
        seed(harness, _fn)
        r = self._deprecate(harness, successor_id="theirs")
        assert r.status_code == 422, r.text

    def test_the_lifecycle_view_offers_candidates_and_reports_succession(self, harness):
        self._seed_pair(harness)
        body = harness.lifecycle("t1", "old").json()
        ids = [c["id"] for c in body["candidate_successors"]]
        assert "new" in ids and "old" not in ids, "must not offer itself"

        self._deprecate(harness, successor_id="new")
        after = harness.lifecycle("t1", "old").json()
        assert after["succession"]["successor_system_id"] == "new"
        assert after["succession"]["successor_name"] == "new"

    def test_the_gate_accepts_an_already_recorded_answer(self, harness):
        # Having answered once, a system that bounces back to active and is
        # deprecated again should not have to answer twice.
        self._seed_pair(harness)
        assert self._deprecate(harness, successor_id="new").status_code == 200
        assert harness.transition("t1", "old", to_stage="active").status_code == 200
        assert self._deprecate(harness).status_code == 200


# ---------------------------------------------------------------------------
# The gate has no back door
# ---------------------------------------------------------------------------


class TestNoUngatedPath:
    """A governed field with an ungated writer is worse than no gate.

    The evidence requirements live on the lifecycle endpoint. If a plain
    `PATCH /v1/admin/resource-groups/{id}` could set the same column, an
    operator could move a system to `active` with no contract, no approver
    and no check for widened permissions — and the fleet board would still
    report it as governed.
    """

    def test_the_resource_group_patch_refuses_to_move_an_ai_system(self, harness):
        from app.api.v1.endpoints.admin.resource_groups import (
            create_resource_groups_router,
        )

        # Mounted lazily: this suite exists to test the contracts router, and
        # the RG router is only needed to prove it defers to it.
        app = harness.client.app
        if not any(
            '/admin/resource-groups/' in getattr(r, 'path', '') for r in app.routes
        ):
            from unittest.mock import MagicMock

            app.include_router(
                create_resource_groups_router(MagicMock(), BrutorLogger('test-rg')),
                prefix='/v1',
            )

        seed(harness, lambda s: add_system(s, 'sys', lifecycle_stage='proposed'))
        r = harness.client.patch(
            '/v1/admin/resource-groups/sys',
            headers=harness.login('t1'),
            json={'lifecycle_stage': 'active'},
        )
        assert r.status_code == 409, r.text
        assert '/lifecycle' in r.text, 'the refusal must point at the gated endpoint'

        s = harness.Factory()
        try:
            assert (
                s.query(ResourceGroup).filter_by(id='sys').one().lifecycle_stage
                == 'proposed'
            )
        finally:
            s.close()
