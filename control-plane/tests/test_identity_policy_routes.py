# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The identity policy over HTTP.

What is worth defending:

  * **A group's effective policy is shown with where each value comes
    from**, and the production floor of an `active` AI System binds the
    system itself.
  * **A write cannot relax what a group inherits**: the refusal names every
    field, the floor and its source.
  * **Every write is audited** with the stored value before and after.
  * **The tenant default needs its own permission** on the general tenant
    update.
  * **Keys the policy would refuse at use are refused at mint**, and a
    personal key always carries an expiry within its ceiling.
  * **The identity summary and the credentials list** count what the core
    recorded, per AI System subtree.
  * **One OIDC IdP per issuer** in a tenant, refused before the database
    would refuse it.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.api.v1.endpoints.admin.identity_policy import create_identity_policy_router
from app.api.v1.endpoints.admin.idp_servers import create_idp_admin_router
from app.api.v1.endpoints.admin.resource_groups import create_resource_groups_router
from app.api.v1.endpoints.admin.tenant_settings import create_tenant_settings_router
from app.core.admin_user.permissions import PERMISSION_READ_ONLY, Permission
from app.core.logging_system import BrutorLogger
from app.database.models.admin import TenantAdminAuditLog
from app.database.models.ai_system_run import AISystemRun
from app.database.models.api_key import APIKey
from app.database.models.end_user import EndUser
from app.database.models.idp_server import IdPServer
from app.database.models.proxy_log import ProxyLog
from app.database.models.resource_group import GroupType, ResourceGroup
from app.services.usage_limit_enforcer import UsageLimitEnforcer
from tests.admin_endpoint_harness import build_admin_harness, throwaway_pg_database

DEV_DEFAULT = {
    "mode": "audit",
    "min_assurance": "key",
    "allow_user_keys": True,
    "allowed_issuers": None,
    "max_token_lifetime_s": 3600,
    "max_key_ttl_days": None,
}

STRICT = {
    "mode": "enforce",
    "min_assurance": "workload",
    "allow_user_keys": False,
    "allowed_issuers": None,
    "max_token_lifetime_s": 1800,
    "max_key_ttl_days": 30,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _harness(database_url=None):
    log = BrutorLogger("test-identity-policy")
    harness = build_admin_harness(
        [
            create_identity_policy_router(log),
            create_resource_groups_router(UsageLimitEnforcer(logger=log), log),
            create_tenant_settings_router(log),
            create_idp_admin_router(log),
        ],
        logger_name="test-identity-policy",
        database_url=database_url,
    )

    def fn(s):
        s.add(EndUser(id=901, tenant_id="t1", username="alice", email="alice@t1.example",
                      password_hash="x", is_active=True))
        s.add(IdPServer(id="idp-a", tenant_id="t1", name="A", issuer="https://a.example",
                        type="manual", protocol="oidc"))
        s.add(IdPServer(id="idp-b", tenant_id="t1", name="B", issuer="https://b.example",
                        type="manual", protocol="oidc"))
        s.add(IdPServer(id="idp-other", tenant_id="t2", name="O", issuer="https://o.example",
                        type="manual", protocol="oidc"))

    harness.seed(fn)
    return harness


@pytest.fixture()
def h():
    harness = _harness()
    yield harness
    harness.close()


@pytest.fixture(params=["sqlite", "postgres"])
def hs(request):
    """The harness on SQLite and, with BRUTOR_PG_TESTS=1, on a throwaway
    Postgres database: the aggregations read the database clock there."""
    if request.param == "sqlite":
        harness = _harness()
        yield harness
        harness.close()
        return
    if os.environ.get("BRUTOR_PG_TESTS") != "1":
        pytest.skip("needs Postgres; set BRUTOR_PG_TESTS=1 (and BRUTOR_PG_TEST_URL for a non-default server)")
    database = throwaway_pg_database("r32_identity")
    url = next(database)
    harness = _harness(url)
    try:
        yield harness
    finally:
        harness.close()
        next(database, None)


def _group(h, gid, *, tenant="t1", parent=None, ai_system=False, stage=None, tier=None, policy=None):
    def fn(s):
        s.add(
            ResourceGroup(
                id=gid,
                tenant_id=tenant,
                name=f"name-{gid}",
                parent_group_id=parent,
                hierarchy_level=0 if parent is None else 1,
                inherit_resources=True,
                is_active=True,
                is_system=False,
                group_type=GroupType.AI_SYSTEM if ai_system else GroupType.TEAM,
                system_kind="agent" if ai_system else None,
                lifecycle_stage=stage,
                eu_ai_act_risk_tier=tier,
                identity_policy=policy,
            )
        )

    h.seed(fn)
    return gid


def _get(h, gid, headers=None):
    return h.client.get(f"/v1/admin/resource-groups/{gid}/identity-policy", headers=headers or h.headers())


def _put(h, gid, policy, headers=None):
    return h.client.put(
        f"/v1/admin/resource-groups/{gid}/identity-policy",
        json={"policy": policy},
        headers=headers or h.headers(),
    )


def _audit_rows(h, action):
    s = h.Factory()
    try:
        return [r for r in s.query(TenantAdminAuditLog).all() if r.action == action]
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------


class TestPermissions:
    def test_read_is_read_only_and_manage_is_not(self):
        assert Permission.IDENTITY_POLICY_READ.value == "identity_policy:read"
        assert Permission.IDENTITY_POLICY_MANAGE.value == "identity_policy:manage"
        assert PERMISSION_READ_ONLY[Permission.IDENTITY_POLICY_READ] is True
        assert PERMISSION_READ_ONLY[Permission.IDENTITY_POLICY_MANAGE] is False

    def test_both_roles_have_their_defaults(self):
        from app.constants.tenant import READ_ONLY_ROLES, TenantDefaultRole
        from app.services.tenant.tenant_provisioning import TenantProvisioner

        roles = TenantProvisioner.get_default_tenant_roles(None)
        admin = set(roles[TenantDefaultRole.ADMIN_USER.value]["permissions"])
        assert {"identity_policy:read", "identity_policy:manage"} <= admin
        for name in READ_ONLY_ROLES:
            if name not in roles or name == TenantDefaultRole.AUDITOR.value:
                continue
            held = set(roles[name]["permissions"])
            assert "identity_policy:read" in held, name
            assert "identity_policy:manage" not in held, name

    def test_read_needs_read_and_write_needs_manage(self, h):
        gid = _group(h, "rg-perm")
        reader = h.limited_headers("t1", [Permission.IDENTITY_POLICY_READ])
        assert _get(h, gid, reader).status_code == 200
        assert _put(h, gid, STRICT, reader).status_code == 403
        nobody = h.limited_headers("t1", [Permission.RESOURCE_GROUP_READ])
        assert _get(h, gid, nobody).status_code == 403


# ---------------------------------------------------------------------------
# GET
# ---------------------------------------------------------------------------


class TestGet:
    def test_a_plain_group_gets_the_development_default(self, h):
        gid = _group(h, "rg-plain")
        r = _get(h, gid)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["group_id"] == gid and body["group_name"] == "name-rg-plain"
        assert body["is_ai_system"] is False
        assert body["attributed_to_ai_system"] is False
        assert body["policy"] is None
        assert body["effective"] == DEV_DEFAULT
        assert body["inherited_floor"] == DEV_DEFAULT
        assert body["sources"]["mode"] == {"kind": "default"}
        assert body["max_user_key_ttl_days"] == 90

    def test_an_active_system_is_held_by_the_production_floor(self, h):
        sid = _group(h, "sys-live", ai_system=True, stage="active", tier="limited")
        body = _get(h, sid).json()
        assert body["is_ai_system"] is True and body["attributed_to_ai_system"] is True
        assert body["lifecycle_stage"] == "active" and body["eu_ai_act_risk_tier"] == "limited"
        assert body["effective"]["mode"] == "enforce"
        assert body["effective"]["min_assurance"] == "workload"
        assert body["effective"]["allow_user_keys"] is False
        assert body["sources"]["min_assurance"] == {
            "kind": "lifecycle", "group_id": sid, "group_name": "name-sys-live", "stage": "active",
        }

    def test_a_sub_group_inherits_and_is_attributed(self, h):
        sid = _group(h, "sys-p", ai_system=True, stage="active")
        gid = _group(h, "rg-c", parent=sid)
        body = _get(h, gid).json()
        assert body["attributed_to_ai_system"] is True
        assert body["effective"]["min_assurance"] == "workload"
        assert body["sources"]["mode"]["group_id"] == sid
        assert body["sources"]["mode"]["group_name"] == "name-sys-p"

    def test_another_tenants_group_is_not_found(self, h):
        gid = _group(h, "rg-t2", tenant="t2")
        assert _get(h, gid).status_code == 404
        assert _put(h, gid, STRICT).status_code == 404

    def test_a_stored_invalid_policy_reads_as_the_strictest(self, h):
        gid = _group(h, "rg-bad", policy={"mode": "loose"})
        body = _get(h, gid).json()
        assert body["effective"]["min_assurance"] == "workload_pop"
        assert body["effective"]["allowed_issuers"] == []


# ---------------------------------------------------------------------------
# PUT
# ---------------------------------------------------------------------------


class TestPut:
    def test_a_policy_is_stored_audited_and_resolved(self, h):
        gid = _group(h, "rg-w")
        r = _put(h, gid, dict(STRICT, allowed_issuers=["idp-b", "idp-a"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["policy"]["allowed_issuers"] == ["idp-a", "idp-b"]
        assert body["effective"]["mode"] == "enforce"
        assert body["sources"]["mode"] == {"kind": "explicit", "group_id": gid, "group_name": "name-rg-w"}
        rows = _audit_rows(h, "resource_group.identity_policy.update")
        assert len(rows) == 1
        assert rows[0].resource_id == gid
        assert rows[0].details["before"] is None
        assert rows[0].details["after"]["mode"] == "enforce"

        r = _put(h, gid, None)
        assert r.status_code == 200, r.text
        assert r.json()["policy"] is None
        assert r.json()["effective"] == DEV_DEFAULT
        rows = _audit_rows(h, "resource_group.identity_policy.update")
        assert len(rows) == 2
        last = [r for r in rows if r.details["after"] is None]
        assert last and last[0].details["before"]["mode"] == "enforce"

    def test_pydantic_refuses_a_malformed_policy(self, h):
        gid = _group(h, "rg-m")
        missing = {k: v for k, v in STRICT.items() if k != "max_key_ttl_days"}
        assert _put(h, gid, missing).status_code == 422
        assert _put(h, gid, dict(STRICT, extra=1)).status_code == 422
        assert _put(h, gid, dict(STRICT, allow_user_keys="false")).status_code == 422
        assert _put(h, gid, dict(STRICT, max_token_lifetime_s=7200)).status_code == 422
        assert _put(h, gid, dict(STRICT, allowed_issuers=[])).status_code == 422
        r = h.client.put(f"/v1/admin/resource-groups/{gid}/identity-policy", json={}, headers=h.headers())
        assert r.status_code == 422

    def test_an_issuer_must_be_an_oidc_idp_of_the_tenant(self, h):
        gid = _group(h, "rg-i")
        r = _put(h, gid, dict(STRICT, allowed_issuers=["idp-a", "idp-other", "idp-none"]))
        assert r.status_code == 422, r.text
        detail = r.json()["detail"]
        assert detail["code"] == "identity_policy_unknown_issuer"
        assert detail["issuers"] == ["idp-none", "idp-other"]

    def test_a_child_cannot_relax_its_parent(self, h):
        parent = _group(h, "sys-par", ai_system=True, stage="approved",
                        policy=dict(STRICT, allowed_issuers=["idp-a"]))
        child = _group(h, "rg-kid", parent=parent)
        r = _put(h, child, dict(DEV_DEFAULT, max_key_ttl_days=10))
        assert r.status_code == 422, r.text
        detail = r.json()["detail"]
        assert detail["code"] == "identity_policy_below_floor"
        fields = [p["field"] for p in detail["problems"]]
        assert fields == ["mode", "min_assurance", "allow_user_keys", "allowed_issuers", "max_token_lifetime_s"]
        first = detail["problems"][1]
        assert first["requested"] == "key" and first["floor"] == "workload"
        assert first["source"] == {"kind": "explicit", "group_id": parent, "group_name": "name-sys-par"}
        assert _audit_rows(h, "resource_group.identity_policy.update") == []

    def test_an_explicit_policy_cannot_relax_the_production_floor(self, h):
        sid = _group(h, "sys-act", ai_system=True, stage="active")
        r = _put(h, sid, DEV_DEFAULT)
        assert r.status_code == 422
        fields = [p["field"] for p in r.json()["detail"]["problems"]]
        assert fields == ["mode", "min_assurance", "allow_user_keys"]

    def test_a_stricter_policy_than_the_floor_is_accepted(self, h):
        sid = _group(h, "sys-act2", ai_system=True, stage="active")
        r = _put(h, sid, dict(STRICT, min_assurance="workload_pop"))
        assert r.status_code == 200, r.text
        assert r.json()["effective"]["min_assurance"] == "workload_pop"


# ---------------------------------------------------------------------------
# Tenant default
# ---------------------------------------------------------------------------


DEFAULT_BODY = dict(STRICT, mode="audit", min_assurance="key", allow_user_keys=True, max_key_ttl_days=None,
                    max_user_key_ttl_days=30)


class TestTenantDefault:
    def _patch(self, h, value, headers=None):
        return h.client.patch(
            "/v1/admin/tenants/current", json={"identity_policy_default": value}, headers=headers or h.headers()
        )

    def test_it_is_stored_returned_and_resolved(self, h):
        r = self._patch(h, DEFAULT_BODY)
        assert r.status_code == 200, r.text
        assert r.json()["identity_policy_default"]["max_user_key_ttl_days"] == 30
        got = h.client.get("/v1/admin/tenants/current", headers=h.headers()).json()
        assert got["identity_policy_default"]["max_token_lifetime_s"] == 1800
        gid = _group(h, "rg-td")
        body = _get(h, gid).json()
        assert body["max_user_key_ttl_days"] == 30
        assert body["sources"]["max_token_lifetime_s"] == {"kind": "tenant_default"}
        # Recorded by the tenant update's own audit entry.
        rows = _audit_rows(h, "tenant_update")
        assert any("identity_policy_default" in (r.details or {}).get("changes", {}) for r in rows)

        r = self._patch(h, None)
        assert r.status_code == 200, r.text
        assert r.json()["identity_policy_default"] is None

    def test_it_needs_identity_policy_manage(self, h):
        only_config = h.limited_headers("t1", [Permission.GATEWAY_CONFIG_UPDATE])
        r = self._patch(h, DEFAULT_BODY, only_config)
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["code"] == "permission_denied"
        assert r.json()["detail"]["permission"] == "identity_policy:manage"
        # Other fields still need only the route's permission.
        r = h.client.patch("/v1/admin/tenants/current", json={"display_name": "Xy"}, headers=only_config)
        assert r.status_code == 200, r.text

    def test_it_is_validated(self, h):
        assert self._patch(h, {k: v for k, v in DEFAULT_BODY.items() if k != "max_user_key_ttl_days"}).status_code == 422
        r = self._patch(h, dict(DEFAULT_BODY, allowed_issuers=["idp-other"]))
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "identity_policy_unknown_issuer"


# ---------------------------------------------------------------------------
# Key mint
# ---------------------------------------------------------------------------


def _mint(h, gid, **body):
    payload = {"name": f"k-{uuid.uuid4().hex[:6]}"}
    payload.update(body)
    return h.client.post(f"/v1/admin/resource-groups/{gid}/api-keys", json=payload, headers=h.headers())


def _in_days(days: float) -> str:
    return (_now() + timedelta(days=days)).isoformat()


class TestKeyMint:
    def test_a_development_group_mints_a_shared_key_as_before(self, h):
        gid = _group(h, "rg-dev")
        r = _mint(h, gid)
        assert r.status_code == 201, r.text

    def test_an_active_system_refuses_a_key(self, h):
        sid = _group(h, "sys-k", ai_system=True, stage="active")
        r = _mint(h, sid)
        assert r.status_code == 422, r.text
        detail = r.json()["detail"]
        assert detail["code"] == "key_refused_by_identity_policy"
        assert detail["effective_policy"]["min_assurance"] == "workload"
        assert detail["sources"]["min_assurance"]["kind"] == "lifecycle"

    def test_audit_mode_above_key_still_mints(self, h):
        gid = _group(h, "rg-aud", policy=dict(DEV_DEFAULT, min_assurance="workload"))
        assert _mint(h, gid).status_code == 201

    def test_a_personal_key_where_the_policy_refuses_them(self, h):
        gid = _group(h, "rg-nouser", policy=dict(DEV_DEFAULT, allow_user_keys=False))
        r = _mint(h, gid, access_mode="user", user_id=901, expires_at=_in_days(10))
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "user_key_not_allowed"

    def test_a_personal_key_needs_an_expiry_within_its_ceiling(self, h):
        gid = _group(h, "rg-user")
        r = _mint(h, gid, access_mode="user", user_id=901)
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "key_expiry_required"
        r = _mint(h, gid, access_mode="user", user_id=901, expires_at=_in_days(91))
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "key_expiry_exceeds_ceiling"
        r = _mint(h, gid, access_mode="user", user_id=901, expires_at=_in_days(89))
        assert r.status_code == 201, r.text

    def test_the_tenant_default_sets_the_personal_ceiling(self, h):
        h.client.patch("/v1/admin/tenants/current", json={"identity_policy_default": DEFAULT_BODY},
                       headers=h.headers())
        gid = _group(h, "rg-user2")
        r = _mint(h, gid, access_mode="user", user_id=901, expires_at=_in_days(31))
        assert r.json()["detail"]["code"] == "key_expiry_exceeds_ceiling"

    def test_a_key_ttl_ceiling_applies_to_shared_keys(self, h):
        gid = _group(h, "rg-ttl", policy=dict(DEV_DEFAULT, max_key_ttl_days=30))
        assert _mint(h, gid).json()["detail"]["code"] == "key_expiry_required"
        assert _mint(h, gid, expires_at=_in_days(31)).json()["detail"]["code"] == "key_expiry_exceeds_ceiling"
        r = _mint(h, gid, expires_at=_in_days(29))
        assert r.status_code == 201, r.text

    def test_a_patch_may_not_clear_or_stretch_a_ceilinged_expiry(self, h):
        gid = _group(h, "rg-pt", policy=dict(DEV_DEFAULT, max_key_ttl_days=30))
        key_id = _mint(h, gid, expires_at=_in_days(10)).json()["id"]
        url = f"/v1/admin/resource-groups/api-keys/{key_id}"
        r = h.client.patch(url, json={"expires_at": None}, headers=h.headers())
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["code"] == "key_expiry_required"
        r = h.client.patch(url, json={"expires_at": _in_days(40)}, headers=h.headers())
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "key_expiry_exceeds_ceiling"
        r = h.client.patch(url, json={"expires_at": _in_days(20)}, headers=h.headers())
        assert r.status_code == 200, r.text
        r = h.client.patch(url, json={"name": "renamed"}, headers=h.headers())
        assert r.status_code == 200, r.text

    def test_a_patch_without_a_ceiling_clears_the_expiry(self, h):
        gid = _group(h, "rg-clear")
        key_id = _mint(h, gid, expires_at=_in_days(10)).json()["id"]
        r = h.client.patch(f"/v1/admin/resource-groups/api-keys/{key_id}", json={"expires_at": None},
                           headers=h.headers())
        assert r.status_code == 200, r.text
        s = h.Factory()
        try:
            assert s.get(APIKey, key_id).expires_at is None
        finally:
            s.close()


# ---------------------------------------------------------------------------
# Identity summary and credentials
# ---------------------------------------------------------------------------


def _row(tenant, group, *, kind, level, ref, outcome, days_ago=1.0, api_key_id=None, name=None, agent=None):
    return ProxyLog(
        id=str(uuid.uuid4()),
        tenant_id=tenant,
        timestamp=_now() - timedelta(days=days_ago),
        proxy_type="llm",
        request_id=str(uuid.uuid4()),
        end_user_session_id="sess-1",
        http_method="POST",
        endpoint="/v1/chat/completions",
        success=outcome != "refused",
        resource_group_id=group,
        credential_kind=kind,
        credential_assurance=level,
        credential_ref=ref,
        credential_policy_outcome=outcome,
        api_key_id=api_key_id,
        api_key_name=name,
        actor_agent_id=agent,
    )


class TestSummary:
    def _seed(self, h):
        sid = _group(h, "sys-s", ai_system=True, stage="approved")
        _group(h, "rg-s-child", parent=sid)
        other = _group(h, "sys-other", ai_system=True, stage="approved")

        def fn(s):
            s.add_all([
                _row("t1", sid, kind="api_key", level="key", ref="api_key:k1", outcome="met",
                     api_key_id="k1", name="old-name", days_ago=3),
                _row("t1", sid, kind="api_key", level="key", ref="api_key:k1", outcome="below_policy",
                     api_key_id="k1", name="new-name", days_ago=1),
                _row("t1", "rg-s-child", kind="workload_token", level="workload",
                     ref="token:idp-a:spiffe://x/agent", outcome="met", agent="agent-1"),
                _row("t1", sid, kind="human_session", level=None, ref="session:admin:1", outcome="exempt"),
                _row("t1", sid, kind="api_key", level="key", ref="api_key:k2", outcome="refused",
                     api_key_id="k2", name="k2"),
                # Outside the window, another system, another tenant.
                _row("t1", sid, kind="api_key", level="key", ref="api_key:k9", outcome="met", days_ago=20),
                _row("t1", other, kind="api_key", level="key", ref="api_key:k8", outcome="met"),
                _row("t2", sid, kind="api_key", level="key", ref="api_key:k7", outcome="met"),
            ])
            s.add(AISystemRun(
                id="run-1", tenant_id="t1", root_task_id="root-1", ai_system_id=sid,
                started_at=_now() - timedelta(days=1), terminal_state="completed",
                finalized_by="terminal", credential_assurance_min="key",
            ))
            s.add(AISystemRun(
                id="run-2", tenant_id="t1", root_task_id="root-2", ai_system_id=sid,
                started_at=_now() - timedelta(days=1), terminal_state="completed",
                finalized_by="terminal", credential_assurance_min=None,
            ))

        h.seed(fn)
        return sid

    def test_the_summary_counts_the_subtree_in_the_window(self, hs):
        sid = self._seed(hs)
        r = hs.client.get(f"/v1/admin/ai-systems/{sid}/identity/summary", headers=hs.headers())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ai_system_id"] == sid and body["window_days"] == 7
        assert body["effective"]["mode"] == "audit"
        assert body["calls_by_level"] == {"key": 3, "bound_key": 0, "workload": 1, "workload_pop": 0,
                                          "outside_ladder": 1}
        assert body["outcomes"] == {"met": 2, "below_policy": 1, "refused": 1, "exempt": 1}
        assert body["runs_by_min_level"] == {"key": 1, "bound_key": 0, "workload": 0, "workload_pop": 0, "none": 1}
        assert body["credentials"] == 4
        wide = hs.client.get(f"/v1/admin/ai-systems/{sid}/identity/summary?days=30", headers=hs.headers()).json()
        assert wide["calls_by_level"]["key"] == 4

    def test_the_credentials_list(self, hs):
        sid = self._seed(hs)
        r = hs.client.get(f"/v1/admin/ai-systems/{sid}/identity/credentials", headers=hs.headers())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == 5  # 30-day window: k1, token, session, k2, k9
        by_ref = {i["credential_ref"]: i for i in body["items"]}
        k1 = by_ref["api_key:k1"]
        assert k1["calls"] == 2 and k1["below_policy_calls"] == 1 and k1["refused_calls"] == 0
        assert k1["api_key_id"] == "k1" and k1["api_key_name"] == "new-name"
        assert by_ref["api_key:k2"]["refused_calls"] == 1
        assert by_ref["token:idp-a:spiffe://x/agent"]["agent_id"] == "agent-1"
        last_seen = [i["last_seen"] for i in body["items"]]
        assert last_seen == sorted(last_seen, reverse=True)
        page = hs.client.get(f"/v1/admin/ai-systems/{sid}/identity/credentials?limit=2&offset=1",
                            headers=hs.headers()).json()
        assert page["total"] == 5 and len(page["items"]) == 2

    def test_bounds_and_scope(self, hs):
        sid = self._seed(hs)
        gid = _group(hs, "rg-not-a-system")
        assert hs.client.get(f"/v1/admin/ai-systems/{gid}/identity/summary", headers=hs.headers()).status_code == 404
        assert hs.client.get(f"/v1/admin/ai-systems/{sid}/identity/summary?days=91",
                            headers=hs.headers()).status_code == 422
        assert hs.client.get(f"/v1/admin/ai-systems/{sid}/identity/credentials?limit=501",
                            headers=hs.headers()).status_code == 422
        nobody = hs.limited_headers("t1", [Permission.RESOURCE_GROUP_READ])
        assert hs.client.get(f"/v1/admin/ai-systems/{sid}/identity/summary", headers=nobody).status_code == 403
        reader = hs.limited_headers("t1", [Permission.IDENTITY_POLICY_READ])
        assert hs.client.get(f"/v1/admin/ai-systems/{sid}/identity/credentials", headers=reader).status_code == 200


# ---------------------------------------------------------------------------
# One OIDC IdP per issuer
# ---------------------------------------------------------------------------


class TestIdpIssuer:
    def test_a_duplicate_issuer_is_a_conflict(self, h):
        r = h.client.post(
            "/v1/admin/idps",
            json={"name": "dup", "issuer": "https://a.example", "type": "manual", "protocol": "oidc", "enabled": True,
                  "authorization_endpoint": "https://a.example/auth", "token_endpoint": "https://a.example/token"},
            headers=h.headers(),
        )
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert detail["code"] == "idp_issuer_exists"
        assert detail["idp_server_id"] == "idp-a"

    def test_a_patch_onto_another_idps_issuer_is_a_conflict(self, h):
        r = h.client.patch("/v1/admin/idps/idp-b", json={"issuer": "https://a.example"}, headers=h.headers())
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "idp_issuer_exists"
        assert r.json()["detail"]["idp_server_id"] == "idp-a"
