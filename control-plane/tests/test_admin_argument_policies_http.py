# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Argument policies over HTTP: CRUD, group binding, YAML policy-as-code, tenant
isolation and permission denial. The core enforces these at runtime, so a row that
is wrong here is a control that is wrong everywhere.
"""

from __future__ import annotations

import pytest
import yaml

from app.api.v1.endpoints.admin.argument_policies import create_argument_policies_router
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database.models.admin import TenantAdminAuditLog
from app.database.models.argument_policy import ArgumentPolicy
from app.database.models.resource_group import ResourceGroup
from app.database.models.tenant import Tenant
from tests.admin_endpoint_harness import build_admin_harness, throwaway_pg_database

BASE = "/v1/admin/argument-policies"
_n = {"i": 0}


@pytest.fixture(scope="module")
def h():
    for url in throwaway_pg_database("arg_policies"):
        harness = build_admin_harness(
            [create_argument_policies_router(BrutorLogger("test-arg-policies"))], database_url=url
        )
        yield harness
        harness.close()


def _body(**over) -> dict:
    _n["i"] += 1
    return {
        "name": f"policy-{_n['i']}",
        "surface": "mcp_input",
        "target": "server:db/query",
        "argument_key": "sql",
        "analyzer": "sql",
        "rules": [{"deny_if": "writes", "message": "no writes"}],
        **over,
    }


def _policy(h, tenant: str = "t1", **over) -> dict:
    r = h.client.post(BASE, json=_body(**over), headers=h.headers(tenant))
    assert r.status_code == 201, r.text
    return r.json()


def _row(h, policy_id: str):
    s = h.Factory()
    try:
        p = s.get(ArgumentPolicy, policy_id)
        if p is None:
            return None
        return {
            "name": p.name, "enabled": p.enabled, "analyzer": p.analyzer, "rules": p.rules,
            "severity": p.severity, "tenant_id": p.tenant_id, "description": p.description,
            "groups": sorted(g.id for g in p.groups),
        }
    finally:
        s.close()


def _by_name(h, tenant: str, name: str):
    s = h.Factory()
    try:
        return s.query(ArgumentPolicy).filter_by(tenant_id=tenant, name=name).first()
    finally:
        s.close()


def _audited(h, tenant: str, action: str) -> int:
    s = h.Factory()
    try:
        return s.query(TenantAdminAuditLog).filter_by(tenant_id=tenant, action=action).count()
    finally:
        s.close()


class TestIsolation:
    def test_another_tenants_policy_looks_absent_on_every_route(self, h):
        p = _policy(h, "t2")
        t1 = h.headers("t1")
        assert h.client.get(f"{BASE}/{p['id']}", headers=t1).status_code == 404
        assert h.client.patch(f"{BASE}/{p['id']}", json={"enabled": False}, headers=t1).status_code == 404
        assert h.client.delete(f"{BASE}/{p['id']}", headers=t1).status_code == 404
        assert h.client.patch(f"{BASE}/{p['id']}/groups", json={"group_ids": []}, headers=t1).status_code == 404
        row = _row(h, p["id"])
        assert row["enabled"] is True and row["tenant_id"] == "t2"

    def test_listing_shows_only_the_callers_tenant(self, h):
        mine, theirs = _policy(h, "t1"), _policy(h, "t2")
        ids = {x["id"] for x in h.client.get(BASE, headers=h.headers("t1")).json()["policies"]}
        assert mine["id"] in ids and theirs["id"] not in ids

    def test_export_shows_only_the_callers_tenant(self, h):
        mine, theirs = _policy(h, "t1"), _policy(h, "t2")
        r = h.client.get(f"{BASE}/export.yaml", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        names = {x["name"] for x in yaml.safe_load(r.text)["policies"]}
        assert mine["name"] in names and theirs["name"] not in names

    def test_binding_another_tenants_group_is_refused(self, h):
        p = _policy(h, "t1")
        r = h.client.patch(f"{BASE}/{p['id']}/groups", json={"group_ids": ["group-default-t2"]}, headers=h.headers("t1"))
        assert r.status_code == 404
        assert _row(h, p["id"])["groups"] == []

    def test_create_with_another_tenants_group_is_refused_not_silently_unbound(self, h):
        """Same rule as the group-binding route: a binding that did not land must
        not look like one that did."""
        body = _body(group_ids=["group-default-t2"])
        r = h.client.post(BASE, json=body, headers=h.headers("t1"))
        assert r.status_code == 404
        assert _by_name(h, "t1", body["name"]) is None

    def test_import_cannot_reach_another_tenants_group_or_policy(self, h):
        foreign = _policy(h, "t2")
        doc = {"policies": [{**_body(name=foreign["name"], rules=[{"deny_if": "writes"}]), "groups": ["Default"]}]}
        r = h.client.post(f"{BASE}/import.yaml", content=yaml.safe_dump(doc), headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert r.json()["created"] == [foreign["name"]]
        assert _row(h, foreign["id"])["groups"] == []
        mine = _by_name(h, "t1", foreign["name"])
        assert _row(h, mine.id)["groups"] == ["group-default-t1"]

    def test_the_same_name_may_exist_in_two_tenants(self, h):
        body = _body()
        assert h.client.post(BASE, json=body, headers=h.headers("t1")).status_code == 201
        assert h.client.post(BASE, json=body, headers=h.headers("t2")).status_code == 201


class TestPermissions:
    @pytest.mark.parametrize(
        "method,path,kw,held",
        [
            ("get", "", {}, Permission.ARGUMENT_POLICY_CREATE),
            ("get", "/x", {}, Permission.ARGUMENT_POLICY_CREATE),
            ("post", "", {"json": "body"}, Permission.ARGUMENT_POLICY_READ),
            ("patch", "/x", {"json": {"enabled": False}}, Permission.ARGUMENT_POLICY_READ),
            ("delete", "/x", {}, Permission.ARGUMENT_POLICY_UPDATE),
            ("patch", "/x/groups", {"json": {"group_ids": []}}, Permission.ARGUMENT_POLICY_READ),
            ("get", "/export.yaml", {}, Permission.ARGUMENT_POLICY_READ),
            ("post", "/import.yaml", {"content": "policies: []"}, Permission.ARGUMENT_POLICY_READ),
        ],
    )
    def test_the_wrong_permission_is_403(self, h, method, path, kw, held):
        if kw.get("json") == "body":
            kw = {"json": _body()}
        limited = h.limited_headers("t1", [held])
        r = getattr(h.client, method)(f"{BASE}{path}", headers=limited, **kw)
        assert r.status_code == 403, r.text

    def test_a_read_only_admin_changes_nothing(self, h):
        p = _policy(h)
        limited = h.limited_headers("t1", [Permission.ARGUMENT_POLICY_READ])
        h.client.patch(f"{BASE}/{p['id']}", json={"enabled": False}, headers=limited)
        h.client.delete(f"{BASE}/{p['id']}", headers=limited)
        assert _row(h, p["id"])["enabled"] is True

    def test_anonymous_is_refused(self, h):
        assert h.client.get(BASE).status_code in (401, 403)


class TestCreate:
    def test_stores_the_policy_and_audits(self, h):
        before = _audited(h, "t1", "argument_policy_create")
        p = _policy(h, group_ids=["group-default-t1"], severity="warn", description="d")
        row = _row(h, p["id"])
        assert (row["severity"], row["description"], row["groups"]) == ("warn", "d", ["group-default-t1"])
        assert row["rules"] == [{"deny_if": "writes", "message": "no writes"}]
        assert _audited(h, "t1", "argument_policy_create") == before + 1

    def test_a_duplicate_name_is_a_conflict(self, h):
        body = _body()
        assert h.client.post(BASE, json=body, headers=h.headers("t1")).status_code == 201
        assert h.client.post(BASE, json=body, headers=h.headers("t1")).status_code == 409

    def test_a_predicate_from_another_analyzer_is_refused(self, h):
        r = h.client.post(BASE, json=_body(rules=[{"deny_if": "host_in_private_range"}]), headers=h.headers("t1"))
        assert r.status_code == 422

    def test_no_tenant_feature_flag_switches_the_control_off(self, h):
        """An argument policy is a governance control: it is behind no tenant
        feature flag. `policies` gates Policy-as-Code only."""
        def off(s):
            s.get(Tenant, "t2").features = {"policies": False}

        def on(s):
            s.get(Tenant, "t2").features = {}

        h.seed(off)
        try:
            body = _body()
            r = h.client.post(BASE, json=body, headers=h.headers("t2"))
            assert r.status_code == 201, r.text
            assert _by_name(h, "t2", body["name"]) is not None
        finally:
            h.seed(on)


class TestList:
    def test_filters(self, h):
        a = _policy(h, surface="skill_input", analyzer="url", rules=[{"deny_if": "host_in_private_range"}])
        b = _policy(h, surface="mcp_input", analyzer="sql")
        ids = lambda q: {x["id"] for x in h.client.get(f"{BASE}?{q}", headers=h.headers("t1")).json()["policies"]}
        assert a["id"] in ids("surface=skill_input") and b["id"] not in ids("surface=skill_input")
        assert b["id"] in ids("analyzer=sql") and a["id"] not in ids("analyzer=sql")

    def test_get_one(self, h):
        p = _policy(h)
        got = h.client.get(f"{BASE}/{p['id']}", headers=h.headers("t1"))
        assert got.status_code == 200 and got.json()["name"] == p["name"]
        assert h.client.get(f"{BASE}/nope", headers=h.headers("t1")).status_code == 404


class TestUpdate:
    def test_changes_what_was_sent_and_audits_the_old_value(self, h):
        p = _policy(h, description="old")
        before = _audited(h, "t1", "argument_policy_update")
        r = h.client.patch(f"{BASE}/{p['id']}", json={"severity": "warn", "enabled": False}, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        row = _row(h, p["id"])
        assert (row["severity"], row["enabled"], row["description"]) == ("warn", False, "old")
        assert _audited(h, "t1", "argument_policy_update") == before + 1

    def test_a_taken_name_is_a_conflict_and_changes_nothing(self, h):
        a, b = _policy(h), _policy(h)
        r = h.client.patch(f"{BASE}/{a['id']}", json={"name": b["name"]}, headers=h.headers("t1"))
        assert r.status_code == 409
        assert _row(h, a["id"])["name"] == a["name"]

    def test_renaming_to_its_own_name_is_not_a_conflict(self, h):
        a = _policy(h)
        assert h.client.patch(f"{BASE}/{a['id']}", json={"name": a["name"]}, headers=h.headers("t1")).status_code == 200

    def test_new_rules_are_checked_against_the_existing_analyzer(self, h):
        a = _policy(h)
        r = h.client.patch(f"{BASE}/{a['id']}", json={"rules": [{"deny_if": "host_in_private_range"}]}, headers=h.headers("t1"))
        assert r.status_code == 400
        assert _row(h, a["id"])["rules"][0]["deny_if"] == "writes"

    def test_a_new_analyzer_is_checked_against_the_existing_rules(self, h):
        a = _policy(h)
        r = h.client.patch(f"{BASE}/{a['id']}", json={"analyzer": "url"}, headers=h.headers("t1"))
        assert r.status_code == 400
        assert _row(h, a["id"])["analyzer"] == "sql"

    def test_analyzer_and_rules_together_are_accepted_when_they_agree(self, h):
        a = _policy(h)
        r = h.client.patch(
            f"{BASE}/{a['id']}",
            json={"analyzer": "url", "rules": [{"deny_if": "host_in_private_range"}]},
            headers=h.headers("t1"),
        )
        assert r.status_code == 200, r.text
        assert _row(h, a["id"])["analyzer"] == "url"

    def test_a_values_predicate_without_values_is_refused(self, h):
        a = _policy(h)
        r = h.client.patch(f"{BASE}/{a['id']}", json={"rules": [{"deny_if": "touches_any"}]}, headers=h.headers("t1"))
        assert r.status_code == 400

    def test_a_nullable_field_can_be_cleared(self, h):
        a = _policy(h, description="old")
        r = h.client.patch(f"{BASE}/{a['id']}", json={"description": None}, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert _row(h, a["id"])["description"] is None

    @pytest.mark.parametrize("field", ["name", "surface", "target", "argument_key", "analyzer", "rules", "enabled", "severity"])
    def test_null_on_a_required_field_changes_nothing(self, h, field):
        """No state to move to, so it is dropped like `version`, not a 500 from
        the NOT NULL constraint, nor a policy with its rules gone."""
        a = _policy(h)
        before = _row(h, a["id"])
        r = h.client.patch(f"{BASE}/{a['id']}", json={field: None}, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert _row(h, a["id"]) == before


class TestDelete:
    def test_removes_and_audits(self, h):
        p = _policy(h)
        before = _audited(h, "t1", "argument_policy_delete")
        assert h.client.delete(f"{BASE}/{p['id']}", headers=h.headers("t1")).status_code == 204
        assert _row(h, p["id"]) is None
        assert _audited(h, "t1", "argument_policy_delete") == before + 1

    def test_unknown_is_404(self, h):
        assert h.client.delete(f"{BASE}/nope", headers=h.headers("t1")).status_code == 404


class TestGroups:
    def test_replaces_the_binding_and_audits_old_and_new(self, h):
        p = _policy(h)
        r = h.client.patch(f"{BASE}/{p['id']}/groups", json={"group_ids": ["group-default-t1"]}, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert _row(h, p["id"])["groups"] == ["group-default-t1"]
        r = h.client.patch(f"{BASE}/{p['id']}/groups", json={"group_ids": []}, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert _row(h, p["id"])["groups"] == []
        assert _audited(h, "t1", "argument_policy_groups_update") >= 2

    def test_an_unknown_group_is_refused_and_keeps_the_binding(self, h):
        p = _policy(h, group_ids=["group-default-t1"])
        r = h.client.patch(f"{BASE}/{p['id']}/groups", json={"group_ids": ["group-default-t1", "nope"]}, headers=h.headers("t1"))
        assert r.status_code == 404
        assert _row(h, p["id"])["groups"] == ["group-default-t1"]


class TestPolicyAsCode:
    def _import(self, h, doc, tenant="t1"):
        text = doc if isinstance(doc, (str, bytes)) else yaml.safe_dump(doc)
        return h.client.post(f"{BASE}/import.yaml", content=text, headers=h.headers(tenant))

    def _entry(self, **over) -> dict:
        body = _body(**over)
        return {k: v for k, v in body.items() if k != "group_ids"}

    def test_export_then_import_round_trips_to_unchanged(self, h):
        _policy(h, group_ids=["group-default-t1"], analyzer_config={"dialect": "postgres"})
        exported = h.client.get(f"{BASE}/export.yaml", headers=h.headers("t1"))
        assert exported.headers["content-type"].startswith("application/x-yaml")
        r = self._import(h, exported.text)
        assert r.status_code == 200, r.text
        body = r.json()
        assert (body["created"], body["updated"], body["errors"]) == ([], [], [])
        assert len(body["unchanged"]) == len(yaml.safe_load(exported.text)["policies"])

    def test_create_then_update_then_unchanged(self, h):
        entry = self._entry()
        assert self._import(h, {"policies": [entry]}).json()["created"] == [entry["name"]]
        assert self._import(h, {"policies": [entry]}).json()["unchanged"] == [entry["name"]]
        changed = {**entry, "severity": "warn", "enabled": False, "groups": ["Default"]}
        assert self._import(h, {"policies": [changed]}).json()["updated"] == [entry["name"]]
        row = _row(h, _by_name(h, "t1", entry["name"]).id)
        assert (row["severity"], row["enabled"], row["groups"]) == ("warn", False, ["group-default-t1"])

    def test_one_bad_entry_does_not_stop_the_others(self, h):
        good = self._entry()
        bad = {**self._entry(), "rules": [{"deny_if": "host_in_private_range"}]}
        r = self._import(h, {"policies": ["not a mapping", {"name": "no-surface"}, bad, good]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["created"] == [good["name"]]
        assert [e["index"] for e in body["errors"]] == [0, 1, 2]
        assert _by_name(h, "t1", bad["name"]) is None

    def test_an_unknown_group_is_reported_and_the_policy_still_lands(self, h):
        entry = {**self._entry(), "groups": ["No Such Group"]}
        r = self._import(h, {"policies": [entry]})
        assert r.json()["unresolved_groups"] == ["No Such Group"]
        assert _by_name(h, "t1", entry["name"]) is not None

    @pytest.mark.parametrize(
        "text", [": : not yaml [", "- a\n- list", "policies: nope", b"\xff\xfe"],
    )
    def test_a_malformed_document_is_400(self, h, text):
        assert self._import(h, text).status_code == 400

    def test_import_is_audited(self, h):
        before = _audited(h, "t1", "argument_policy_import")
        self._import(h, {"policies": []})
        assert _audited(h, "t1", "argument_policy_import") == before + 1

    def test_the_same_name_twice_in_one_document_does_not_500(self, h):
        entry = self._entry()
        r = self._import(h, {"policies": [entry, {**entry, "severity": "warn"}]})
        assert r.status_code == 200, r.text
        s = h.Factory()
        try:
            assert s.query(ArgumentPolicy).filter_by(tenant_id="t1", name=entry["name"]).count() == 1
        finally:
            s.close()

    def test_a_group_import_never_links_a_group_by_name_across_tenants(self, h):
        def add(s):
            s.add(ResourceGroup(id="only-in-t2", tenant_id="t2", name="OnlyInT2", hierarchy_level=0,
                                inherit_resources=True, is_active=True))

        h.seed(add)
        entry = {**self._entry(), "groups": ["OnlyInT2"]}
        r = self._import(h, {"policies": [entry]})
        assert r.json()["unresolved_groups"] == ["OnlyInT2"]
        assert _row(h, _by_name(h, "t1", entry["name"]).id)["groups"] == []
