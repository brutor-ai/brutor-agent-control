# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
End-to-end tests for the response-policy API.

The API is where the closed loop stops being a service and starts being
something an operator arms. Three things carry it:

  * **The preview shows the blast radius before the policy exists.** The
    affected set must be visible before arming, because autonomy
    resolves min-wins down the tree and a policy on a parent silently
    restricts every inheriting system below it.
  * **Refusals are visible.** Silence after a critical finding is
    indistinguishable from a broken feature, so the history returns what
    was refused and why alongside what fired.
  * **Unsafe policies are refused at write time**, not at fire time. An
    operator should discover that `suspended` needs opt-in while writing
    the policy, not during an incident.
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

from tests.run_outcome_defaults import work_outcome
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.endpoints.admin.admin_rbac import create_admin_users_router
from app.api.v1.endpoints.admin.ai_system_contracts import (
    create_ai_system_contracts_router,
)
from app.api.v1.endpoints.admin.ai_system_response import (
    create_ai_system_response_router,
)
from app.api.v1.endpoints.admin.ai_system_data_scope import (
    create_ai_system_data_scope_router,
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
from app.database.models.ai_system_contract import AISystemContract
from app.database.models.ai_system_evidence import AISystemEvidence
from app.database.models.ai_system_health import (
    AISystemAssuranceReport,
    AISystemHealth,
)
from app.database.models.ai_system_drift import AISystemDriftEvent
from app.database.models.agent_identity import AgentIdentity
from app.database.models.ai_system_run import AISystemRun
from app.database.models.ai_system_drift import (
    AISystemBaseline,
    BASELINE_ALL_ACTORS,
    BASELINE_LEARNING,
    BASELINE_READY,
)
from app.database.models.ai_system_liveness import AISystemLivenessExpectation
from app.database.models.ai_system_replay import (
    AISystemReplayCase,
    AISystemReplayRun,
    AISystemReplaySuite,
    REPLAY_COMPLETED, REPLAY_RUNNING,
)
from app.database.models.ai_system_response_policy import AISystemResponsePolicy
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
from app.database.models.agent_skill import AgentSkill, resource_group_agent_skills
from app.database.models.server_config import ServerConfig
from app.database.models.proxy_log import ProxyLog
from app.database.models.system_operation_log import SystemOperationLog
from app.database.models.tenant import Tenant

JWT_SECRET = os.environ["JWT_SECRET"]

_BY_NAME = [
    "llm_catalog",
    "llm_routing_group",
    "llm_providers",
    "llm_provider_catalog",
    "end_user_groups",
    "resource_group_end_user_groups",
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
    AISystemDriftEvent.__table__,
    AISystemBaseline.__table__,
    # Every graded signal reads the exclusion predicate.
    Base.metadata.tables["ai_system_baseline_epochs"],
    Base.metadata.tables["ai_system_incident_windows"],
    AISystemLivenessExpectation.__table__,
    # The run request stamps the active contract, if there is one.
    AISystemContract.__table__,
    AISystemEvidence.__table__,
    AISystemHealth.__table__,
    AISystemAssuranceReport.__table__,
    # ai_system_runs FKs to agent_identities even when agent_id is NULL.
    AgentIdentity.__table__,
    # The report's composition section reads member agents'
    # current releases.
    Base.metadata.tables["agent_implementation_releases"],
    # a193_approval_window_epochs — the agent checks judge each run by the approval then in effect.
    Base.metadata.tables["agent_identity_approval_history"],
    # replay_cases.source_run_id FKs to it, even when the column is NULL.
    AISystemRun.__table__,
    # 2026-11-07 — the preview says which callers approval_required skips.
    Base.metadata.tables["api_keys"],
    AISystemResponsePolicy.__table__,
    AISystemReplaySuite.__table__,
    AISystemReplayCase.__table__,
    AISystemReplayRun.__table__,
    SystemOperationLog.__table__,
    # The suite builder reads the recorded request from here.
    ProxyLog.__table__,
    # The closure walk reads every binding, even an empty one.
    ServerConfig.__table__,
    resource_group_llm_models,
    resource_group_mcp_capabilities,
    resource_group_server_configs,
    AgentSkill.__table__,
    resource_group_agent_skills,
    Base.metadata.tables['guardrail_configs'],
    Base.metadata.tables['resource_group_guardrails'],
    Base.metadata.tables['argument_policies'],
    Base.metadata.tables['resource_group_argument_policies'],
    Base.metadata.tables['semantic_cache_configs'],
    Base.metadata.tables['resource_group_cache_configs'],
    Base.metadata.tables['resource_group_agent_members'],
    # a154_oversight_signal — health scoring reads approvals for the Oversight signal.
    Base.metadata.tables['tool_approval_requests'],
    # Coverage attestation — the report reads the discovered estate to state
    # what fraction of AI activity it actually covers.
    Base.metadata.tables['collector_keys'],
    Base.metadata.tables['discovered_assets'],
    # Data scope, record accesses and reach rules (also in
    # the contract closure when set). `tests/test_ai_system_data_scope.py`
    # reuses this harness.
    Base.metadata.tables['ai_system_data_scopes'],
    Base.metadata.tables['ai_system_run_scopes'],
    Base.metadata.tables['ai_system_record_access'],
    Base.metadata.tables['ai_system_reach_rules'],
]

TIGHTEN = [{"kind": "set_autonomy", "level": "restricted"}]
NOTIFY = [{"kind": "notify", "targets": ["ops-oncall"]}]


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

    def create(self, tenant, sid, **body):
        payload = {"name": "p", "actions": TIGHTEN}
        payload.update(body)
        return self.client.post(
            f"/v1/admin/ai-systems/{sid}/response-policies",
            headers=self.login(tenant),
            json=payload,
        )

    def listp(self, tenant, sid, **params):
        return self.client.get(
            f"/v1/admin/ai-systems/{sid}/response-policies",
            headers=self.login(tenant),
            params=params,
        )

    def preview(self, tenant, sid, **body):
        payload = {"actions": TIGHTEN}
        payload.update(body)
        return self.client.post(
            f"/v1/admin/ai-systems/{sid}/response-policies/preview",
            headers=self.login(tenant),
            json=payload,
        )

    def patch(self, tenant, pid, **body):
        return self.client.patch(
            f"/v1/admin/response-policies/{pid}",
            headers=self.login(tenant),
            json=body,
        )

    def delete(self, tenant, pid):
        return self.client.delete(
            f"/v1/admin/response-policies/{pid}", headers=self.login(tenant)
        )

    def history(self, tenant, sid):
        return self.client.get(
            f"/v1/admin/ai-systems/{sid}/responses", headers=self.login(tenant)
        )

    def restore(self, tenant, sid, **body):
        return self.client.post(
            f"/v1/admin/ai-systems/{sid}/restore",
            headers=self.login(tenant),
            json=body,
        )

    def replay(self, tenant, sid, suite_id=None):
        qs = f"?suite_id={suite_id}" if suite_id else ""
        return self.client.get(
            f"/v1/admin/ai-systems/{sid}/replay{qs}", headers=self.login(tenant)
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
    logger = BrutorLogger("test-ai-system-response")
    app.include_router(create_admin_users_router(logger), prefix="/v1")
    app.include_router(create_ai_system_response_router(logger), prefix="/v1")
    app.include_router(create_ai_system_data_scope_router(logger), prefix="/v1")
    # The 1.3 status vocabulary distinguishes signed from unsigned
    # assurance, so the harness needs the mint/approve/promote flow.
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


def add_system(session, gid, tenant="t1", parent=None, inherits=True,
               autonomy=None, group_type=GroupType.AI_SYSTEM, **kw):
    session.add(
        ResourceGroup(
            id=gid, tenant_id=tenant, name=gid,
            hierarchy_level=0 if parent is None else 1,
            inherit_resources=inherits, is_active=True, is_system=False,
            group_type=group_type,
            system_kind=kw.pop(
                "system_kind",
                "agent" if group_type == GroupType.AI_SYSTEM else None,
            ),
            parent_group_id=parent, autonomy_level=autonomy, **kw
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
# The preview — required before arming
# ---------------------------------------------------------------------------


class TestPreview:
    def test_it_reports_the_blast_radius_before_the_policy_exists(self, harness):
        # The affected set has to be visible while the policy is still being
        # written, not after it is armed.
        def s(db):
            add_system(db, "parent", autonomy="autonomous")
            add_system(db, "child-a", parent="parent")
            add_system(db, "child-b", parent="parent")

        seed(harness, s)
        body = harness.preview("t1", "parent").json()
        assert body["blast_radius"]["total"] == 3
        assert body["blast_radius"]["inherited_by"] == ["child-a", "child-b"]
        assert harness.Factory().query(AISystemResponsePolicy).count() == 0

    def test_a_wide_radius_produces_a_warning(self, harness):
        def s(db):
            add_system(db, "parent", autonomy="autonomous")
            add_system(db, "child", parent="parent")

        seed(harness, s)
        warnings = harness.preview("t1", "parent").json()["warnings"]
        assert any("acts on 2 systems" in w for w in warnings)

    def test_a_leaf_system_gets_no_wide_warning(self, harness):
        seed(harness, lambda db: add_system(db, "sys", autonomy="autonomous"))
        body = harness.preview("t1", "sys").json()
        assert body["blast_radius"]["is_wide"] is False
        assert not any("acts on" in w for w in body["warnings"])

    def test_it_shows_what_would_happen(self, harness):
        seed(harness, lambda db: add_system(db, "sys", autonomy="autonomous"))
        body = harness.preview("t1", "sys").json()
        assert body["current_autonomy"] == "autonomous"
        assert body["would_do"][0]["params"]["level"] == "restricted"

    def test_it_shows_when_nothing_would_happen(self, harness):
        # A policy that cannot fire against the current state is a policy an
        # operator should not arm believing otherwise.
        seed(harness, lambda db: add_system(db, "sys", autonomy="restricted"))
        body = harness.preview("t1", "sys").json()
        assert body["would_do"] == []
        assert body["would_refuse"][0]["code"] == "no_change_needed"

    def test_suspending_warns_that_reads_are_denied_too(self, harness):
        seed(harness, lambda db: add_system(db, "sys", autonomy="autonomous"))
        body = harness.preview(
            "t1", "sys",
            actions=[{"kind": "set_autonomy", "level": "suspended"}],
            allow_suspend=True,
        ).json()
        assert any("including read-only" in w for w in body["warnings"])

    def test_disabling_human_restore_warns_about_flapping(self, harness):
        seed(harness, lambda db: add_system(db, "sys", autonomy="autonomous"))
        body = harness.preview(
            "t1", "sys", require_human_to_restore=False
        ).json()
        assert any("flaps" in w for w in body["warnings"])

    def test_approval_required_warns_about_callers_it_does_not_hold(self, harness):
        from app.database.models.api_key import APIKey

        def _seed(s):
            add_system(s, "solo", autonomy="autonomous")
            s.add(APIKey(
                id="k1", tenant_id="t1", key_hash="h", key_prefix="brt_",
                name="k", scope="global", group_id="solo", enabled=True,
                revoked=False,
            ))

        seed(harness, _seed)
        r = harness.preview("t1", "solo", actions=[
            {"kind": "set_autonomy", "level": "approval_required"}
        ])
        assert r.status_code == 200, r.text
        assert any("holds agent identities only" in w for w in r.json()["warnings"])

    def test_a_notify_only_policy_has_no_blast_radius(self, harness):
        def s(db):
            add_system(db, "parent", autonomy="autonomous")
            add_system(db, "child", parent="parent")

        seed(harness, s)
        body = harness.preview("t1", "parent", actions=NOTIFY).json()
        assert body["blast_radius"] is None

    def test_an_unsafe_policy_is_refused_at_preview_too(self, harness):
        seed(harness, lambda db: add_system(db, "sys"))
        r = harness.preview(
            "t1", "sys", actions=[{"kind": "set_autonomy", "level": "suspended"}]
        )
        assert r.status_code == 422
        assert "allow_suspend" in r.text


# ---------------------------------------------------------------------------
# Arming
# ---------------------------------------------------------------------------


class TestArming:
    def test_a_policy_can_be_created(self, harness):
        seed(harness, lambda db: add_system(db, "sys"))
        r = harness.create("t1", "sys", name="tighten-on-drift")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["name"] == "tighten-on-drift"
        assert body["is_actuating"] is True
        assert body["target_autonomy"] == "restricted"

    def test_defaults_are_the_safe_ones(self, harness):
        seed(harness, lambda db: add_system(db, "sys"))
        body = harness.create("t1", "sys").json()
        assert body["allow_suspend"] is False
        assert body["require_human_to_restore"] is True
        assert body["severity_at_least"] == "high"
        assert body["response_cooldown_seconds"] == 3600

    def test_suspend_without_opt_in_is_refused_at_write_time(self, harness):
        # Not at fire time, during an incident.
        seed(harness, lambda db: add_system(db, "sys"))
        r = harness.create(
            "t1", "sys", actions=[{"kind": "set_autonomy", "level": "suspended"}]
        )
        assert r.status_code == 422

    def test_an_unknown_action_is_refused_rather_than_dropped(self, harness):
        seed(harness, lambda db: add_system(db, "sys"))
        r = harness.create("t1", "sys", actions=[{"kind": "delete_everything"}])
        assert r.status_code == 422
        assert "unknown action" in r.text

    def test_an_unknown_severity_is_refused(self, harness):
        seed(harness, lambda db: add_system(db, "sys"))
        assert harness.create(
            "t1", "sys", severity_at_least="apocalyptic"
        ).status_code == 422

    def test_a_plain_access_group_cannot_have_a_policy(self, harness):
        seed(harness, lambda db: add_system(db, "grp", group_type=GroupType.TEAM))
        r = harness.create("t1", "grp")
        assert r.status_code == 422
        assert "not an AI System" in r.text

    def test_turning_off_suspend_while_a_suspend_action_remains_is_refused(
        self, harness
    ):
        # Otherwise a patch could silently leave an armed suspend policy with
        # its safety flag off.
        seed(harness, lambda db: add_system(db, "sys"))
        pid = harness.create(
            "t1", "sys",
            actions=[{"kind": "set_autonomy", "level": "suspended"}],
            allow_suspend=True,
        ).json()["id"]
        r = harness.patch("t1", pid, allow_suspend=False)
        assert r.status_code == 422

    def test_a_policy_can_be_disarmed(self, harness):
        seed(harness, lambda db: add_system(db, "sys"))
        pid = harness.create("t1", "sys").json()["id"]
        assert harness.delete("t1", pid).status_code == 200
        assert harness.listp("t1", "sys").json()["total"] == 0


class TestListing:
    def test_a_parents_policy_is_listed_as_inherited(self, harness):
        # An operator debugging why a system was throttled needs to find the
        # policy that did it, and it may not be on the system in front of them.
        def s(db):
            add_system(db, "parent")
            add_system(db, "child", parent="parent")

        seed(harness, s)
        harness.create("t1", "parent", name="from-above")

        body = harness.listp("t1", "child").json()
        assert body["total"] == 1
        assert body["inherited_count"] == 1

    def test_inherited_policies_can_be_excluded(self, harness):
        def s(db):
            add_system(db, "parent")
            add_system(db, "child", parent="parent")

        seed(harness, s)
        harness.create("t1", "parent", name="from-above")
        assert harness.listp("t1", "child", include_inherited=False).json()["total"] == 0

    def test_a_non_inheriting_child_does_not_see_it(self, harness):
        def s(db):
            add_system(db, "parent")
            add_system(db, "child", parent="parent", inherits=False)

        seed(harness, s)
        harness.create("t1", "parent", name="from-above")
        assert harness.listp("t1", "child").json()["total"] == 0


class TestTenantIsolation:
    def test_another_tenants_system_is_a_404(self, harness):
        seed(harness, lambda db: add_system(db, "theirs", tenant="t2"))
        assert harness.create("t1", "theirs").status_code == 404
        assert harness.preview("t1", "theirs").status_code == 404

    def test_another_tenants_policy_cannot_be_deleted(self, harness):
        seed(harness, lambda db: add_system(db, "theirs", tenant="t2"))
        pid = harness.create("t2", "theirs").json()["id"]
        assert harness.delete("t1", pid).status_code == 404
        assert harness.Factory().query(AISystemResponsePolicy).count() == 1


# ---------------------------------------------------------------------------
# History and restore
# ---------------------------------------------------------------------------


def add_finding(db, eid, on="sys", tenant="t1", severity="critical", taken=None):
    db.add(
        AISystemDriftEvent(
            id=eid, tenant_id=tenant, ai_system_id=on, drift_class="behavioural",
            metric_key="step_count", severity=severity,
            attributed_cause="model_change",
            detected_at=datetime.now(timezone.utc), status="active",
            response_taken=taken,
        )
    )
    db.flush()


class TestHistory:
    def test_it_reports_what_fired(self, harness):
        def s(db):
            add_system(db, "sys")
            add_finding(
                db, "d1",
                taken={
                    "policy_id": "p1", "policy_name": "tighten",
                    "actions": [{"kind": "set_autonomy", "params": {"level": "restricted"}}],
                    "responded_at": "2026-07-28T00:00:00Z",
                },
            )

        seed(harness, s)
        body = harness.history("t1", "sys").json()
        assert body["acted_count"] == 1
        assert body["responses"][0]["policy_name"] == "tighten"

    def test_it_reports_what_was_refused_and_why(self, harness):
        # Silence after a critical finding is indistinguishable from a broken
        # feature.
        def s(db):
            add_system(db, "sys")
            add_finding(
                db, "d1",
                taken={
                    "policy_id": None, "actions": [],
                    "refusals": [{"code": "no_matching_policy", "detail": "none"}],
                },
            )

        seed(harness, s)
        body = harness.history("t1", "sys").json()
        assert body["acted_count"] == 0
        assert body["responses"][0]["acted"] is False
        assert body["responses"][0]["refusals"][0]["code"] == "no_matching_policy"

    def test_unconsidered_findings_are_counted_as_pending(self, harness):
        # A number that stays high is how an operator notices the loop has
        # stopped running.
        def s(db):
            add_system(db, "sys")
            add_finding(db, "d1", taken=None)

        seed(harness, s)
        assert harness.history("t1", "sys").json()["pending_count"] == 1


class TestRestore:
    def test_a_human_can_restore(self, harness):
        seed(harness, lambda db: add_system(db, "sys", autonomy="restricted"))
        r = harness.restore("t1", "sys", to_level="autonomous")
        assert r.status_code == 200, r.text
        assert r.json()["from"] == "restricted"
        assert r.json()["resolved_autonomy"] == "autonomous"

    def test_restoring_to_null_clears_the_override(self, harness):
        def s(db):
            add_system(db, "parent", autonomy="approval_required")
            add_system(db, "child", parent="parent", autonomy="restricted")

        seed(harness, s)
        body = harness.restore("t1", "child", to_level=None).json()
        # Inherits the parent's level again rather than becoming unbounded.
        assert body["resolved_autonomy"] == "approval_required"

    def test_the_restore_is_audited_against_the_human(self, harness):
        seed(harness, lambda db: add_system(db, "sys", autonomy="restricted"))
        harness.restore("t1", "sys", to_level="autonomous", drift_event_id="d1")

        s = harness.Factory()
        try:
            row = s.query(SystemOperationLog).one()
            assert row.action == "drift_response_restore"
            assert row.actor_type == "tenant_admin"
            assert row.payload["drift_event_id"] == "d1"
        finally:
            s.close()


# ---------------------------------------------------------------------------
# Replay visibility
# ---------------------------------------------------------------------------


class TestReplayStatus:
    def _suite(self, db, **kw):
        base = dict(
            id="suite-1", tenant_id="t1", ai_system_id="sys", name="prod",
            pass_threshold=0.9,
        )
        base.update(kw)
        db.add(AISystemReplaySuite(**base))
        db.flush()

    def test_no_suite_is_reported_plainly(self, harness):
        seed(harness, lambda db: add_system(db, "sys"))
        body = harness.replay("t1", "sys").json()
        assert body["bound"] is False
        assert body["gate_state"] == "no_suite"

    def test_an_unbound_suite_survives_a_reload(self, harness):
        # Building without binding is the default and nothing binds a suite
        # after the fact, so without this fallback a freshly built suite is
        # unreachable the moment the operator reloads — leaving "bind one
        # sight-unseen" as the only way to get a readable suite.
        def s(db):
            add_system(db, "sys")  # nothing bound
            self._suite(db, id="suite-1")
            self._suite(db, id="suite-2", name="newer")

        seed(harness, s)
        body = harness.replay("t1", "sys").json()   # no suite_id given
        assert body["suite_id"] == "suite-2", "should fall back to the newest suite"
        assert body["bound"] is False, "a fallback suite is not gating anything"

    def test_a_bound_suite_that_never_ran(self, harness):
        def s(db):
            add_system(db, "sys", replay_suite_id="suite-1")
            self._suite(db)

        seed(harness, s)
        body = harness.replay("t1", "sys").json()
        assert body["gate_state"] == "never_run"
        assert body["suite_name"] == "prod"

    def test_an_unreplayable_suite_is_its_own_state(self, harness):
        # Not "passing", not "failing" — an un-runnable suite is a third
        # thing, and collapsing it into either would mislead.
        def s(db):
            add_system(db, "sys", replay_suite_id="suite-1")
            self._suite(db, replayable=False, not_replayable_reason="external memory")

        seed(harness, s)
        body = harness.replay("t1", "sys").json()
        assert body["gate_state"] == "not_replayable"
        assert body["not_replayable_reason"] == "external memory"

    def test_a_passing_run_is_reported(self, harness):
        def s(db):
            add_system(db, "sys", replay_suite_id="suite-1")
            self._suite(db)
            db.add(
                AISystemReplayRun(
                    id="rr-1", tenant_id="t1", suite_id="suite-1", ai_system_id="sys",
                    status=REPLAY_COMPLETED, pass_rate=0.95,
                    critical_outcome_changed=0,
                )
            )

        seed(harness, s)
        assert harness.replay("t1", "sys").json()["gate_state"] == "passing"

    def test_a_run_still_in_flight_reads_as_running_not_failing(self, harness):
        def s(db):
            add_system(db, "sys", replay_suite_id="suite-1")
            self._suite(db)
            db.add(
                AISystemReplayRun(
                    id="rr-1", tenant_id="t1", suite_id="suite-1", ai_system_id="sys",
                    status=REPLAY_RUNNING, pass_rate=None,
                    critical_outcome_changed=0,
                )
            )
        seed(harness, s)
        assert harness.replay("t1", "sys").json()["gate_state"] == "running"

    def test_one_broken_critical_case_reads_as_failing(self, harness):
        def s(db):
            add_system(db, "sys", replay_suite_id="suite-1")
            self._suite(db)
            db.add(
                AISystemReplayRun(
                    id="rr-1", tenant_id="t1", suite_id="suite-1", ai_system_id="sys",
                    status=REPLAY_COMPLETED, pass_rate=0.99,
                    critical_outcome_changed=1,
                )
            )

        seed(harness, s)
        assert harness.replay("t1", "sys").json()["gate_state"] == "failing"

    def test_an_unbound_suite_can_be_inspected_by_id(self, harness):
        # Without this, "review the coverage before you bind" is a dead end:
        # a suite can be built and even executed, but its verdict is
        # unreadable until it is already gating releases — which is exactly
        # the order the default is trying to prevent.
        def s(db):
            add_system(db, "sys")  # nothing bound
            self._suite(db)
            db.add(
                AISystemReplayRun(
                    id="rr-1", tenant_id="t1", suite_id="suite-1", ai_system_id="sys",
                    status=REPLAY_COMPLETED, pass_rate=0.95,
                    critical_outcome_changed=0,
                )
            )

        seed(harness, s)
        body = harness.replay("t1", "sys", suite_id="suite-1").json()
        assert body["suite_id"] == "suite-1"
        # Its own verdict is readable…
        assert body["last_run"]["pass_rate"] == 0.95
        # …but it is NOT gating, and must never render as though it were:
        # a green gate over a system nothing is checking is the exact failure
        # this feature exists to prevent.
        assert body["bound"] is False
        assert body["gate_state"] == "no_suite"

    def test_inspecting_the_bound_suite_still_reports_it_as_bound(self, harness):
        def s(db):
            add_system(db, "sys", replay_suite_id="suite-1")
            self._suite(db)

        seed(harness, s)
        assert harness.replay("t1", "sys", suite_id="suite-1").json()["bound"] is True

    def test_coverage_is_reported_per_stratum(self, harness):
        # "30 cases" and "30 variations on the same happy path" must be
        # distinguishable from the API.
        def s(db):
            add_system(db, "sys", replay_suite_id="suite-1")
            self._suite(db)
            for i, stratum in enumerate(
                ["terminal:completed", "terminal:exhausted", "terminal:completed"]
            ):
                db.add(
                    AISystemReplayCase(
                        id=f"case-{i}", tenant_id="t1", suite_id="suite-1",
                        recorded_request={}, recorded_trajectory={}, stratum=stratum,
                    )
                )

        seed(harness, s)
        body = harness.replay("t1", "sys").json()
        assert body["case_count"] == 3
        assert body["coverage"] == {"terminal:completed": 2, "terminal:exhausted": 1}


# ---------------------------------------------------------------------------
# Building a suite from real traffic
# ---------------------------------------------------------------------------


def add_run(db, rid, on="sys", tenant="t1", terminal="completed", steps=5,
            tools="search|fetch", root=None):
    from app.database.models.ai_system_run import AISystemRun

    db.add(
        AISystemRun(
            id=rid, tenant_id=tenant, root_task_id=root or rid, ai_system_id=on,
            started_at=datetime.now(timezone.utc), terminal_state=terminal, **work_outcome(terminal),
            finalized_by="idle", step_count=steps, tool_set_signature=tools,
            chain_integrity="intact", reopened_count=0, is_replay=False,
        )
    )
    db.flush()


class TestBuildSuite:
    def _build(self, harness, **body):
        payload = {"name": "prod"}
        payload.update(body)
        return harness.client.post(
            "/v1/admin/ai-systems/sys/replay/suite",
            headers=harness.login("t1"),
            json=payload,
        )

    def test_a_suite_is_frozen_from_eligible_runs(self, harness):
        def s(db):
            add_system(db, "sys")
            for i in range(12):
                add_run(db, f"r{i}", steps=3 + (i % 4))

        seed(harness, s)
        r = self._build(harness, target_case_count=5)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["eligible_runs"] == 12
        assert body["cases_created"] == 5
        assert sum(body["coverage"].values()) == 5

    def test_coverage_gaps_are_reported_at_build_time(self, harness):
        # "30 cases, none of them the exhausted path" is a fact the operator
        # needs before a green gate lets a regression through — not after.
        def s(db):
            add_system(db, "sys")
            for i in range(20):
                add_run(db, f"r{i}")
            add_run(db, "rare", terminal="exhausted", steps=40, tools="escalate")

        seed(harness, s)
        body = self._build(harness, target_case_count=1).json()
        assert body["coverage_gaps"], "a one-case sample of rich traffic has gaps"
        assert any("not represented" in w for w in body["warnings"])

    def test_an_empty_window_warns_rather_than_pretending(self, harness):
        seed(harness, lambda db: add_system(db, "sys"))
        body = self._build(harness).json()
        assert body["cases_created"] == 0
        assert any("empty" in w for w in body["warnings"])
        assert any("block promotion" in w for w in body["warnings"])

    def test_replays_and_broken_chains_are_not_frozen_into_a_suite(self, harness):
        # A case built from a run the gateway only partly observed encodes a
        # trajectory that never happened.
        def s(db):
            from app.database.models.ai_system_run import AISystemRun

            add_system(db, "sys")
            add_run(db, "good")
            db.add(
                AISystemRun(
                    id="replay-run", tenant_id="t1", root_task_id="replay-run",
                    ai_system_id="sys", started_at=datetime.now(timezone.utc),
                    terminal_state="completed", **work_outcome("completed"), finalized_by="idle",
                    chain_integrity="intact", is_replay=True,
                )
            )
            db.add(
                AISystemRun(
                    id="broken", tenant_id="t1", root_task_id="broken",
                    ai_system_id="sys", started_at=datetime.now(timezone.utc),
                    terminal_state="completed", **work_outcome("completed"), finalized_by="idle",
                    chain_integrity="broken", is_replay=False,
                )
            )

        seed(harness, s)
        assert self._build(harness).json()["eligible_runs"] == 1

    def test_binding_to_the_gate_is_opt_in(self, harness):
        def s(db):
            add_system(db, "sys")
            add_run(db, "r1")

        seed(harness, s)
        assert self._build(harness).json()["bound_to_gate"] is False
        assert harness.replay("t1", "sys").json()["bound"] is False

    def test_binding_wires_it_to_the_promotion_gate(self, harness):
        def s(db):
            add_system(db, "sys")
            add_run(db, "r1")

        seed(harness, s)
        assert self._build(harness, bind_to_gate=True).json()["bound_to_gate"] is True
        status = harness.replay("t1", "sys").json()
        assert status["bound"] is True
        assert status["gate_state"] == "never_run"


class TestRequestRun:
    def test_a_run_can_be_requested_for_a_bound_suite(self, harness):
        from app.database.models.ai_system_replay import REPLAY_RUNNING

        def s(db):
            add_system(db, "sys")
            add_run(db, "r1")

        seed(harness, s)
        harness.client.post(
            "/v1/admin/ai-systems/sys/replay/suite",
            headers=harness.login("t1"),
            json={"name": "prod", "bind_to_gate": True},
        )
        r = harness.client.post(
            "/v1/admin/ai-systems/sys/replay/run", headers=harness.login("t1")
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == REPLAY_RUNNING
        assert r.json()["replay_run_id"].startswith("rpr-")

    def test_the_request_is_queued_not_executed_here(self, harness):
        # The control plane writes the row; the proxy executes it. A run that
        # came back "completed" from this endpoint would mean the control
        # plane had quietly become a second enforcement path.
        def s(db):
            add_system(db, "sys")
            add_run(db, "r1")

        seed(harness, s)
        harness.client.post(
            "/v1/admin/ai-systems/sys/replay/suite",
            headers=harness.login("t1"),
            json={"name": "prod", "bind_to_gate": True},
        )
        body = harness.client.post(
            "/v1/admin/ai-systems/sys/replay/run", headers=harness.login("t1")
        ).json()
        assert "proxy executes" in body["note"]
        # The row is `running` until the proxy evaluates it. That is neither
        # a pass nor a failure yet, and the status says so — it used to read
        # `failing`, which the console rendered as "Last replay did not pass"
        # for a run that had not started.
        assert harness.replay("t1", "sys").json()["gate_state"] == "running"

    def test_requesting_a_run_with_no_suite_is_a_422(self, harness):
        seed(harness, lambda db: add_system(db, "sys"))
        r = harness.client.post(
            "/v1/admin/ai-systems/sys/replay/run", headers=harness.login("t1")
        )
        assert r.status_code == 422
        assert "Build one first" in r.text


# ---------------------------------------------------------------------------
# Health and the Assurance Report
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    def _health(self, harness, **params):
        return harness.client.get(
            "/v1/admin/ai-systems/sys/health",
            headers=harness.login("t1"),
            params=params,
        )

    def test_a_system_with_no_traffic_is_unknown_not_green(self, harness):
        # The first thing an operator sees for a brand-new system must not
        # be a green tick earned by having no evidence.
        seed(harness, lambda db: add_system(db, "sys"))
        body = self._health(harness).json()
        assert body["health_state"] in ("unknown", "silent")
        assert body["health_score"] in (None, 0.0)

    def test_it_is_computed_on_first_read_rather_than_showing_an_empty_panel(
        self, harness
    ):
        seed(harness, lambda db: add_system(db, "sys"))
        assert self._health(harness).status_code == 200
        s = harness.Factory()
        try:
            assert s.query(AISystemHealth).count() == 1
        finally:
            s.close()

    def test_a_suspended_system_reads_as_suspended(self, harness):
        seed(harness, lambda db: add_system(db, "sys", autonomy="suspended"))
        body = self._health(harness).json()
        assert body["health_state"] == "suspended"
        assert body["reasons"], "a state with no explanation is not actionable"

    def test_the_components_come_back_with_the_total(self, harness):
        seed(harness, lambda db: add_system(db, "sys"))
        body = self._health(harness).json()
        for key in (
            "liveness_score", "behaviour_score", "reliability_score",
            "cost_score", "conformance_score",
        ):
            assert key in body

    def test_trustworthiness_is_reported_separately_from_the_score(self, harness):
        seed(harness, lambda db: add_system(db, "sys"))
        assert "is_trustworthy" in self._health(harness).json()


def add_expectation(db, on="sys", tenant="t1", *, mode="scheduled",
                    window=3600, grace=300, enabled=True):
    db.add(
        AISystemLivenessExpectation(
            id=f"live-{on}", tenant_id=tenant, ai_system_id=on, mode=mode,
            expected_min_runs=1, window_seconds=window, grace_seconds=grace,
            enabled=enabled,
        )
    )
    db.flush()


def add_old_run(db, rid, on="sys", tenant="t1", *, hours_ago=2):
    """A finished run older than any 1h liveness window — traffic exists
    in the 30-day judgment window, but the system missed its schedule."""
    db.add(
        AISystemRun(
            id=rid, tenant_id=tenant, root_task_id=rid, ai_system_id=on,
            started_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
            terminal_state="completed", **work_outcome("completed"), finalized_by="idle", step_count=3,
            tool_set_signature="search", chain_integrity="intact",
            reopened_count=0, is_replay=False,
        )
    )
    db.flush()


class TestHealthResponseCarriesEverySignal:
    """Pins the "Pydantic strips undeclared fields" trap.

    `oversight_score` was persisted by the scorer and declared in the UI's
    types, but never added to `HealthResponse` — so it was silently dropped
    on the way out and the Signals panel read "not measured" for every
    system, forever. The UI test could not catch it because it mocks the
    client. This one asserts against the real response.
    """

    def _health(self, harness):
        return harness.client.get(
            "/v1/admin/ai-systems/sys/health", headers=harness.login("t1")
        ).json()

    def test_every_scored_signal_survives_serialisation(self, harness):
        seed(harness, lambda db: add_system(db, "sys"))
        body = self._health(harness)
        for signal in (
            "liveness",
            "behaviour",
            "reliability",
            "cost",
            "conformance",
            "oversight",
        ):
            assert f"{signal}_score" in body, (
                f"{signal}_score was stripped — add it to HealthResponse AND "
                f"the field-by-field builder, not just to to_dict()"
            )

    def test_the_response_carries_the_applied_signal_profile(self, harness):
        seed(harness, lambda db: add_system(db, "sys", system_kind="assistant"))
        body = self._health(harness)
        assert body["system_kind"] == "assistant"
        assert body["signal_levels"]["liveness"]["level"] == "not_applicable"
        assert body["signal_levels"]["behaviour"]["level"] == "advisory"
        assert body["signal_levels"]["cost"]["level"] == "enforced"

    def test_a_not_applicable_signal_reports_no_score(self, harness):
        """Not zero, not 1.0 — nothing. A signal we deliberately do not judge
        must not appear to have been judged."""
        seed(harness, lambda db: add_system(db, "sys", system_kind="assistant"))
        assert self._health(harness)["signal_levels"]["liveness"]["score"] is None

    def test_relaxations_are_disclosed_on_the_health_response(self, harness):
        seed(harness, lambda db: add_system(db, "sys", system_kind="assistant"))
        rel = self._health(harness)["profile_relaxations"]
        assert {r["signal"] for r in rel} == {"liveness", "behaviour"}
        assert all(r["reason"] for r in rel)

    def test_an_agent_discloses_no_relaxations_and_enforces_all_six(self, harness):
        seed(harness, lambda db: add_system(db, "sys", system_kind="agent"))
        body = self._health(harness)
        assert body["profile_relaxations"] == []
        assert all(
            v["level"] == "enforced" for v in body["signal_levels"].values()
        )


class TestHealthReportsNowAndItsWindows:
    """The health response says what it judged over, and the recent-runs
    verdict survives serialisation (three hours of errored runs must not
    read "Healthy 90%" on a 30-day rate)."""

    def _health(self, harness):
        return harness.client.get(
            "/v1/admin/ai-systems/sys/health?refresh=true", headers=harness.login("t1")
        ).json()

    def test_latest_runs_all_errored_reads_failing_with_the_runs_it_read(self, harness):
        def s(db):
            add_system(db, "sys")
            for i in range(20):
                add_old_run(db, f"ok{i}", hours_ago=5 + i / 10)
            for i in range(3):
                add_old_run(db, f"err{i}", hours_ago=1 - i / 10)
            for i in range(3):
                run = db.get(AISystemRun, f"err{i}")
                run.terminal_state = "errored"
                run.error_cause = "model_unavailable"
            db.flush()
        seed(harness, s)
        body = self._health(harness)
        assert body["health_state"] == "failing", body["reasons"]
        recent = body["recent_runs"]
        assert recent["verdict"] == "down"
        assert [r["run_id"] for r in recent["latest"]] == ["err2", "err1", "err0"]
        # The run class and cause reach the client (declared on the response
        # model, which strips undeclared fields).
        latest = recent["latest"][0]
        assert latest["run_class"] == "failed"
        assert latest["error_cause"] == "model_unavailable"
        assert set(latest) >= {"outcome_id", "outcome_conformance"}
        assert recent["window_success_rate"] == pytest.approx(20 / 23, abs=1e-6)

    def test_the_windows_are_stated(self, harness):
        seed(harness, lambda db: add_system(db, "sys"))
        windows = self._health(harness)["windows"]
        assert windows == {
            "health_period_hours": 720,
            "recent_runs": 3,
            "freshness_seconds": 300,
        }


class TestHealthHonoursTheLivenessDetector:
    """Silence beats learning — and the detector's verdict must reach the
    health state, or the Mission Control card reads "Learning" while the
    liveness panel reads "Silent" (the exact bug this class pins)."""

    def _health(self, harness):
        return harness.client.get(
            "/v1/admin/ai-systems/sys/health", headers=harness.login("t1")
        ).json()

    def test_a_system_that_missed_its_schedule_reads_silent_not_learning(self, harness):
        def s(db):
            add_system(db, "sys")
            add_expectation(db)              # scheduled, 1h + 5m grace
            add_old_run(db, "r1", hours_ago=2)
        seed(harness, s)
        body = self._health(harness)
        assert body["health_state"] == "silent"
        assert body["reasons"]

    def test_a_system_inside_its_window_is_not_silent(self, harness):
        def s(db):
            add_system(db, "sys")
            add_expectation(db)
            add_run(db, "r1")                # started now
        seed(harness, s)
        assert self._health(harness)["health_state"] != "silent"

    def test_a_disabled_expectation_does_not_silence(self, harness):
        def s(db):
            add_system(db, "sys")
            add_expectation(db, enabled=False)
            add_old_run(db, "r1", hours_ago=2)
        seed(harness, s)
        assert self._health(harness)["health_state"] != "silent"

    def test_a_quiet_assistant_is_not_an_outage(self, harness):
        """Kind-aware profiles, end to end.

        `assistant` does not enforce liveness — humans are bursty. If the
        relaxation only lowered the score but still forced the SILENT state,
        it would be cosmetic: the operator would still be paged about a
        quiet Sunday, which is the false alarm the profile exists to remove.
        """
        def s(db):
            add_system(db, "sys", system_kind="assistant")
            add_expectation(db)              # scheduled, and missed
            add_old_run(db, "r1", hours_ago=2)
        seed(harness, s)
        assert self._health(harness)["health_state"] != "silent"

    def test_the_same_silence_on_an_agent_still_reads_silent(self, harness):
        """The control for the test above — the relaxation must be specific
        to the kind, not a blanket weakening of silence detection."""
        def s(db):
            add_system(db, "sys", system_kind="agent")
            add_expectation(db)
            add_old_run(db, "r1", hours_ago=2)
        seed(harness, s)
        assert self._health(harness)["health_state"] == "silent"

    def test_a_sporadic_system_is_allowed_to_be_quiet(self, harness):
        # Sporadic systems are supposed to be quiet; alerting on them is
        # how an operator learns to ignore the whole category.
        def s(db):
            add_system(db, "sys")
            add_expectation(db, mode="sporadic")
            add_old_run(db, "r1", hours_ago=2)
        seed(harness, s)
        assert self._health(harness)["health_state"] != "silent"

    def test_without_an_expectation_recent_traffic_still_reads_learning(self, harness):
        # No expectation configured → the coarse fallback only; a system
        # with traffic in the window and unready baselines stays learning.
        def s(db):
            add_system(db, "sys")
            add_old_run(db, "r1", hours_ago=2)
        seed(harness, s)
        assert self._health(harness)["health_state"] == "learning"


class TestAssuranceReport:
    def _report(self, harness, **params):
        return harness.client.get(
            "/v1/admin/ai-systems/sys/assurance",
            headers=harness.login("t1"),
            params=params,
        )

    def test_it_answers_all_four_guarantees(self, harness):
        seed(harness, lambda db: add_system(db, "sys", owner="team"))
        body = self._report(harness).json()
        for section in ("bounded", "non_drifting", "doing_its_job", "alive"):
            assert section in body, f"{section} missing from the report"

    def test_it_states_what_it_does_not_cover(self, harness):
        # The section that makes the rest credible.
        seed(harness, lambda db: add_system(db, "sys"))
        gaps = self._report(harness).json()["not_covered"]
        assert gaps
        assert any("correctness is not evaluated" in g for g in gaps)
        assert any("through the gateway" in g for g in gaps)

    # --- coverage attestation: the population proof ----------------------

    def test_the_report_carries_a_coverage_attestation(self, harness):
        seed(harness, lambda db: add_system(db, "sys"))
        cov = self._report(harness).json()["coverage"]
        assert cov["basis"] == "unmeasured"
        assert cov["coverage_pct"] is None
        assert cov["statement"]

    def test_an_untouched_tenant_is_never_reported_as_fully_covered(self, harness):
        """End-to-end guard on the rule the coverage module exists for: a
        tenant with no discovery installed must not read as 100% covered
        anywhere in the rendered report."""
        seed(harness, lambda db: add_system(db, "sys"))
        body = self._report(harness).json()
        assert body["coverage"]["coverage_pct"] is None
        assert "100%" not in body["coverage"]["statement"]

    def test_coverage_gaps_lead_the_not_covered_section(self, harness):
        """A gap in the *population* outranks any gap in what we measured
        about it — it scopes everything below it, so it reads first."""
        seed(harness, lambda db: add_system(db, "sys"))
        gaps = self._report(harness).json()["not_covered"]
        assert "Coverage is unmeasured" in gaps[0]

    def test_a_relaxed_kind_is_disclosed_in_the_report(self, harness):
        """The anti-laundering guarantee: if `system_kind` could quietly stop
        a signal from counting, it would be a dropdown for making a bad
        health score disappear. The report must say which signals the kind
        relaxed."""
        seed(harness, lambda db: add_system(db, "sys", system_kind="assistant"))
        gaps = self._report(harness).json()["not_covered"]
        body = " ".join(gaps)
        assert "classified as 'assistant'" in body
        assert "Liveness is not scored" in body

    def test_an_agent_report_discloses_no_relaxations(self, harness):
        seed(harness, lambda db: add_system(db, "sys", system_kind="agent"))
        gaps = self._report(harness).json()["not_covered"]
        assert not any("relaxes" in g for g in gaps)

    def test_no_replay_suite_is_named_as_a_gap(self, harness):
        seed(harness, lambda db: add_system(db, "sys"))
        gaps = self._report(harness).json()["not_covered"]
        assert any("No replay suite" in g for g in gaps)

    def test_no_runs_is_named_as_a_gap_rather_than_implied(self, harness):
        seed(harness, lambda db: add_system(db, "sys"))
        gaps = self._report(harness).json()["not_covered"]
        assert any("nothing here is evidence" in g for g in gaps)

    def test_the_identity_section_carries_the_declared_intent(self, harness):
        seed(
            harness,
            lambda db: add_system(
                db, "sys", owner="team", intended_use="answers billing questions",
                eu_ai_act_risk_tier="high",
            ),
        )
        identity = self._report(harness).json()["identity"]
        assert identity["owner"] == "team"
        assert identity["intended_use"] == "answers billing questions"
        assert identity["eu_ai_act_risk_tier"] == "high"

    def test_it_reports_cost_per_processed_outcome(self, harness):
        # The number nobody else can give them — over the work only.
        seed(harness, lambda db: add_system(db, "sys"))
        assert "cost_per_processed_outcome" in self._report(harness).json()[
            "doing_its_job"
        ]["runs"]

    def test_the_window_is_recorded_so_the_report_is_self_describing(self, harness):
        seed(harness, lambda db: add_system(db, "sys"))
        assert self._report(harness, period_hours=168).json()["period_hours"] == 168


def add_baseline(db, bid, on="sys", tenant="t1", *, metric="cost_per_run",
                 bucket="all", actor=BASELINE_ALL_ACTORS,
                 state=BASELINE_READY, samples=240):
    db.add(
        AISystemBaseline(
            id=bid, tenant_id=tenant, ai_system_id=on, metric_key=metric,
            bucket=bucket, actor_id=actor, median=0.04, mad=0.01, p90=0.09,
            sample_count=samples, state=state,
        )
    )
    db.flush()


class TestReportBaselines:
    """Spec 1.1 — the report prints the yardstick, not just the deviations."""

    def _section(self, harness):
        return harness.client.get(
            "/v1/admin/ai-systems/sys/assurance", headers=harness.login("t1")
        ).json()["baselines"]

    def test_system_wide_baselines_are_printed_in_full(self, harness):
        def s(db):
            add_system(db, "sys")
            add_baseline(db, "b1")
        seed(harness, s)
        section = self._section(harness)
        assert section["ready"] == 1 and section["learning"] == 0
        (row,) = section["metrics"]
        assert row["metric_key"] == "cost_per_run"
        assert row["median"] == 0.04 and row["mad"] == 0.01
        assert row["sample_count"] == 240 and row["state"] == "ready"

    def test_sub_baselines_are_counted_not_dumped(self, harness):
        # Seasonal buckets and per-actor rows sharpen detection; a hundred
        # dow:* rows in an auditor-facing report would bury the signal.
        def s(db):
            add_system(db, "sys")
            add_baseline(db, "b1")
            add_baseline(db, "b2", bucket="dow:1")
            add_baseline(db, "b3", actor="agent-worker-2")
        seed(harness, s)
        section = self._section(harness)
        assert len(section["metrics"]) == 1
        assert section["sub_baselines"] == {"seasonal": 1, "per_actor": 1, "per_caller": 0}

    def test_a_learning_baseline_is_reported_as_learning(self, harness):
        def s(db):
            add_system(db, "sys")
            add_baseline(db, "b1", state=BASELINE_LEARNING, samples=8)
        seed(harness, s)
        section = self._section(harness)
        assert section["ready"] == 0 and section["learning"] == 1
        assert section["metrics"][0]["state"] == "learning"

    def test_a_system_with_no_baselines_says_so(self, harness):
        seed(harness, lambda db: add_system(db, "sys"))
        section = self._section(harness)
        assert section["metrics"] == []
        assert section["ready"] == 0
        assert "never alerts" in section["note"]


class TestRecommendedControls:
    """Spec 1.1 — findings become named next controls, worst first."""

    def _recs(self, harness):
        return harness.client.get(
            "/v1/admin/ai-systems/sys/assurance", headers=harness.login("t1")
        ).json()["recommended_controls"]

    def test_a_bare_system_gets_the_bootstrap_controls(self, harness):
        seed(harness, lambda db: add_system(db, "sys"))
        controls = {r["control"] for r in self._recs(harness)}
        assert {"mint_contract", "bind_replay_suite", "configure_liveness"} <= controls

    def test_every_recommendation_carries_its_reason(self, harness):
        seed(harness, lambda db: add_system(db, "sys"))
        recs = self._recs(harness)
        assert recs
        for r in recs:
            assert r["control"] and r["reason"]
            assert r["severity"] in ("high", "medium", "low")

    def test_recommendations_are_sorted_worst_first(self, harness):
        def s(db):
            add_system(db, "sys", operating_envelope={"max_cost_per_run_usd": 1.0})
        seed(harness, s)
        ranks = [
            {"high": 0, "medium": 1, "low": 2}[r["severity"]]
            for r in self._recs(harness)
        ]
        assert ranks == sorted(ranks)

    def test_unexplained_open_drift_recommends_a_response_policy(self, harness):
        def s(db):
            add_system(db, "sys")
            db.add(AISystemDriftEvent(
                id="d1", tenant_id="t1", ai_system_id="sys",
                drift_class="behavioural", metric_key="step_count",
                severity="high", attributed_cause="unexplained",
                detected_at=datetime.now(timezone.utc), status="active",
            ))
        seed(harness, s)
        controls = {r["control"] for r in self._recs(harness)}
        assert "add_response_policy" in controls

    def test_explained_drift_does_not_ask_for_a_response_policy(self, harness):
        # A finding with an attributed cause is being handled by a human
        # reading the cause — the automatic-response nudge is for the ones
        # nobody can explain.
        def s(db):
            add_system(db, "sys")
            add_finding(db, "d1")  # attributed_cause="model_change"
        seed(harness, s)
        controls = {r["control"] for r in self._recs(harness)}
        assert "add_response_policy" not in controls

    def test_an_unsigned_declared_envelope_outranks_a_missing_contract(self, harness):
        def s(db):
            add_system(db, "sys", operating_envelope={"max_cost_per_run_usd": 1.0})
        seed(harness, s)
        recs = self._recs(harness)
        controls = {r["control"] for r in recs}
        assert "approve_contract" in controls
        assert "mint_contract" not in controls
        (rec,) = [r for r in recs if r["control"] == "approve_contract"]
        assert rec["severity"] == "high"


class TestAssuranceCapture:
    def test_a_snapshot_can_be_captured(self, harness):
        seed(harness, lambda db: add_system(db, "sys"))
        r = harness.client.post(
            "/v1/admin/ai-systems/sys/assurance", headers=harness.login("t1"), json={}
        )
        assert r.status_code == 200, r.text
        assert r.json()["trigger"] == "manual"
        assert r.json()["captured_by"] == "admin_t1"

    def test_the_snapshot_is_frozen_not_recomputed(self, harness):
        # Evidence that changes when the system does is not evidence.
        seed(harness, lambda db: add_system(db, "sys", owner="alice"))
        report_id = harness.client.post(
            "/v1/admin/ai-systems/sys/assurance", headers=harness.login("t1"), json={}
        ).json()["id"]

        s = harness.Factory()
        try:
            s.query(ResourceGroup).filter_by(id="sys").one().owner = "bob"
            s.commit()
            stored = s.query(AISystemAssuranceReport).filter_by(id=report_id).one()
            assert stored.report_json["identity"]["owner"] == "alice"
        finally:
            s.close()

    def test_history_lists_snapshots_without_their_payloads(self, harness):
        seed(harness, lambda db: add_system(db, "sys"))
        for _ in range(2):
            harness.client.post(
                "/v1/admin/ai-systems/sys/assurance",
                headers=harness.login("t1"), json={},
            )
        body = harness.client.get(
            "/v1/admin/ai-systems/sys/assurance/history",
            headers=harness.login("t1"),
        ).json()
        assert body["total"] == 2
        assert "report_json" not in body["reports"][0]

    def test_another_tenant_cannot_capture(self, harness):
        seed(harness, lambda db: add_system(db, "theirs", tenant="t2"))
        r = harness.client.post(
            "/v1/admin/ai-systems/theirs/assurance",
            headers=harness.login("t1"), json={},
        )
        assert r.status_code == 404


class TestPolicyListingVisibility:
    """A disabled policy must stay findable.

    Off is the safe default for a control that can restrict production, so the
    listing hiding disabled policies meant the safest choice made the policy
    disappear — with no way back to it in the console.
    """

    def test_a_disabled_policy_is_still_listed(self, harness):
        def s(db):
            add_system(db, "sys")
            db.add(
                AISystemResponsePolicy(
                    id="pol-1", tenant_id="t1", ai_system_id="sys",
                    name="off but real", enabled=False,
                    signal_kind="drift", severity_at_least="high",
                    actions=[{"kind": "notify", "targets": ["a@b.c"]}],
                )
            )

        seed(harness, s)
        body = harness.client.get(
            "/v1/admin/ai-systems/sys/response-policies",
            headers=harness.login("t1"),
        ).json()
        assert body["total"] == 1, "a disabled policy must remain visible to manage"
        assert body["policies"][0]["enabled"] is False

    def test_the_actuator_still_only_sees_enabled_policies(self, harness):
        # The other half of the split: what fires must not change.
        from app.services.ai_system_response.actuator import applicable_policies

        def s(db):
            add_system(db, "sys")
            db.add(
                AISystemResponsePolicy(
                    id="pol-1", tenant_id="t1", ai_system_id="sys",
                    name="off", enabled=False, signal_kind="drift",
                    severity_at_least="high",
                    actions=[{"kind": "notify", "targets": ["a@b.c"]}],
                )
            )

        seed(harness, s)
        db = harness.Factory()
        try:
            system = db.query(ResourceGroup).filter(ResourceGroup.id == "sys").first()
            assert applicable_policies(db, system) == []
        finally:
            db.close()


class TestExecutiveAssuranceLayer:
    """Spec 1.2 — the first twenty seconds of the report.

    The conclusion, level and dimensions are synthesised from facts the
    sections below already state; these tests pin that the synthesis
    never claims more than the facts support — in particular that a
    verdict earned only against observed history says so."""

    def _report(self, harness):
        return harness.client.get(
            "/v1/admin/ai-systems/sys/assurance", headers=harness.login("t1")
        ).json()

    def test_an_unsigned_system_is_never_plainly_assured(self, harness):
        def s(db):
            add_system(db, "sys")
            add_run(db, "r1")
        seed(harness, s)
        body = self._report(harness)
        assert body["verdict"]["basis"] == "observed_history"
        assert "formal assurance is incomplete" in body["verdict"]["conclusion"]
        # The old wording CLAIMED conformance to the approved envelope
        # while nothing was signed; 1.2 claims observed history instead.
        assert "remained within its approved operating envelope" not in body["verdict"]["conclusion"]
        assert "observed historical behaviour" in body["verdict"]["conclusion"]

    def test_the_conclusion_answers_can_i_rely_on_it(self, harness):
        def s(db):
            add_system(db, "sys")
            add_run(db, "r1")
        seed(harness, s)
        summary = self._report(harness)["assurance_summary"]
        c = summary["conclusion"]
        assert "operated within its observed historical behaviour" in c
        assert "operating contract is not approved" in c
        assert "remain under monitored operation" in c.lower()

    def test_dimensions_split_the_verdict_into_named_halves(self, harness):
        def s(db):
            add_system(db, "sys")
            add_run(db, "r1")
        seed(harness, s)
        dims = self._report(harness)["assurance_summary"]["dimensions"]
        assert set(dims) == {
            "operational_evidence", "behavioural_stability",
            "contract_assurance", "execution_evidence",
            "independent_evidence",
        }
        assert dims["operational_evidence"]["status"] == "ok"
        assert dims["contract_assurance"]["status"] == "missing"
        assert dims["independent_evidence"]["status"] == "missing"
        for d in dims.values():
            assert d["detail"], "a status without a reason is not actionable"

    def test_the_level_is_cumulative_not_best_of(self, harness):
        # Signed chains + replay evidence (L3 material) must NOT lift a
        # system past a missing contract (L2): a level is only reached
        # when every rung below it holds.
        def s(db):
            add_system(db, "sys")
            add_baseline(db, "b1")  # L1 material
            add_run(db, "r1")       # chain intact
        seed(harness, s)
        level = self._report(harness)["assurance_summary"]["level"]
        assert level["level"] == 1
        assert level["label"] == "Baseline established"
        assert level["next"]["label"] == "Controlled"

    def test_a_quiet_period_is_below_the_ladder(self, harness):
        seed(harness, lambda db: add_system(db, "sys"))
        level = self._report(harness)["assurance_summary"]["level"]
        assert level["level"] == -1
        assert level["label"] == "Not observable"

    def test_drift_findings_carry_baseline_and_observed_values(self, harness):
        def s(db):
            add_system(db, "sys")
            db.add(AISystemDriftEvent(
                id="d1", tenant_id="t1", ai_system_id="sys",
                drift_class="behavioural", metric_key="tool_call_count",
                severity="high", attributed_cause="unexplained",
                detected_at=datetime.now(timezone.utc), status="active",
                baseline_value=2.0, observed_value=4.7,
                effect_size=3.1, direction="up",
            ))
        seed(harness, s)
        (event,) = self._report(harness)["non_drifting"]["events"]
        assert event["baseline_value"] == 2.0
        assert event["observed_value"] == 4.7
        assert event["direction"] == "up"


class TestAssuranceStatusVocabulary:
    """Spec 1.3 — the word a decision-maker reads first.

    "Level 0" next to "assured" makes the two ideas fight in a buyer's
    head; the STATUS carries the trust claim (Conditionally assured) and
    the LEVEL carries the evidence maturity, explicitly named as such."""

    def _summary(self, harness):
        return harness.client.get(
            "/v1/admin/ai-systems/sys/assurance", headers=harness.login("t1")
        ).json()["assurance_summary"]

    def test_an_unsigned_system_reads_conditionally_assured(self, harness):
        def s(db):
            add_system(db, "sys")
            add_run(db, "r1")
        seed(harness, s)
        summary = self._summary(harness)
        assert summary["status"]["code"] == "conditionally_assured"
        # The label carries its basis since the badge-basis change.
        assert summary["status"]["label"] == "Conditionally assured · observed history"

    def test_a_signed_clean_system_reads_plainly_assured(self, harness):
        def s(db):
            add_system(db, "sys", operating_envelope={"max_cost_per_run_usd": 1.0})
            # Enough completed runs for the envelope term to clear its
            # evidence floor — one run is honestly "insufficient".
            for i in range(30):
                add_run(db, f"r{i}")
        seed(harness, s)
        # Mint + approve + promote the contract so the envelope is signed.
        h = harness.login("t1")
        r = harness.client.post("/v1/admin/ai-systems/sys/contracts", headers=h)
        assert r.status_code == 200, r.text
        cid = r.json()["contract"]["id"]
        assert harness.client.post(
            f"/v1/admin/contracts/{cid}/approve", headers=h, json={}
        ).status_code == 200
        assert harness.client.post(
            f"/v1/admin/contracts/{cid}/promote", headers=h
        ).status_code == 200
        summary = self._summary(harness)
        assert summary["status"]["code"] in ("assured", "assured_with_exceptions")
        assert "conditionally" not in summary["status"]["label"].lower()

    def test_the_executive_block_has_all_three_rows(self, harness):
        def s(db):
            add_system(db, "sys")
            add_run(db, "r1")
        seed(harness, s)
        summary = self._summary(harness)
        assert summary["what_we_know"]
        assert any(
            "operating contract is not approved" in c
            for c in summary["what_prevents_full_assurance"]
        )
        assert summary["recommended_action"].startswith("Remain under monitored")
        # The assembled paragraph still exists for 1.2 consumers.
        assert summary["what_we_know"] in summary["conclusion"]
        assert summary["recommended_action"] in summary["conclusion"]

    def test_nothing_prevents_assurance_reads_as_an_empty_list(self, harness):
        seed(harness, lambda db: add_system(db, "sys"))
        summary = self._summary(harness)
        # Insufficient evidence is its own status, not a list of blockers.
        assert summary["status"]["code"] == "insufficient_evidence"
        assert summary["status"]["label"] == "Evidence insufficient"

    def test_the_ladder_is_named_evidence_maturity(self, harness):
        def s(db):
            add_system(db, "sys")
            add_run(db, "r1")
        seed(harness, s)
        level = self._summary(harness)["level"]
        assert level["scale"] == "Evidence maturity"


class TestWhyItMatters:
    """Spec 1.4 — the last translation step. The report already lists what
    is missing; this row converts it into "so is this system okay or not?"
    so a busy reader never does that conversion themselves."""

    def _summary(self, harness):
        return harness.client.get(
            "/v1/admin/ai-systems/sys/assurance", headers=harness.login("t1")
        ).json()["assurance_summary"]

    def test_conditional_and_quiet_says_normal_but_unapproved(self, harness):
        def s(db):
            add_system(db, "sys")
            add_run(db, "r1")
        seed(harness, s)
        why = self._summary(harness)["why_it_matters"]
        assert "No evidence of abnormal behaviour" in why
        assert "approved operating boundary" in why

    def test_conditional_with_open_drift_does_not_claim_normality(self, harness):
        # "No abnormal behaviour" next to open drift findings would be the
        # summary knowing less than the sections below it.
        def s(db):
            add_system(db, "sys")
            add_run(db, "r1")
            db.add(AISystemDriftEvent(
                id="d1", tenant_id="t1", ai_system_id="sys",
                drift_class="behavioural", metric_key="step_count",
                severity="high", attributed_cause="unexplained",
                detected_at=datetime.now(timezone.utc), status="active",
            ))
        seed(harness, s)
        why = self._summary(harness)["why_it_matters"]
        assert "No evidence of abnormal behaviour" not in why
        assert "deviated from its own history" in why

    def test_no_evidence_says_absence_is_not_safety(self, harness):
        seed(harness, lambda db: add_system(db, "sys"))
        why = self._summary(harness)["why_it_matters"]
        assert "Absence of evidence is not safety" in why
