# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The admin API for run data scope and record reach.

The core enforces and sweeps (`gateway-core` integration tests
`data_scope.rs`, `record_reach.rs`); this file pins what an operator can
configure, that the config is part of the contract only once it exists, the
reach preview's numbers. Erasure of record accesses is in
`test_admin_data_erasure_http.py`.
"""

from datetime import datetime, timedelta, timezone

import pytest

from tests.test_ai_system_response_endpoints import (  # noqa: F401 — fixtures
    add_system,
    harness,
    seed,
)

from app.database.models.ai_system_data_scope import (
    AISystemRecordAccess,
    AISystemRunScope,
    validate_config,
)

CONFIG = {
    "mode": "audit",
    "keys": [{"key": "customer"}, {"key": "requester"}],
    "rules": [{"tool": "crm_get_customer", "argument": "customer_id", "key": "customer"}],
    "unscoped_tools": ["crm_list_customers"],
}


def _put(h, sid, body, tenant="t1"):
    return h.client.put(
        f"/v1/admin/ai-systems/{sid}/data-scope", headers=h.login(tenant), json=body
    )


def _get(h, sid, path="data-scope", tenant="t1"):
    return h.client.get(f"/v1/admin/ai-systems/{sid}/{path}", headers=h.login(tenant))


class TestConfig:
    def test_a_system_without_a_config_says_so(self, harness):
        seed(harness, lambda s: add_system(s, "sys"))
        r = _get(harness, "sys")
        assert r.status_code == 200, r.text
        assert r.json()["configured"] is False

    def test_a_config_round_trips_and_versions(self, harness):
        seed(harness, lambda s: add_system(s, "sys"))
        r = _put(harness, "sys", CONFIG)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["configured"] and body["version"] == 1
        assert body["keys"] == [
            {"key": "customer", "source": "declared"},
            {"key": "requester", "source": "declared"},
        ]
        assert _put(harness, "sys", {**CONFIG, "mode": "enforce"}).json()["version"] == 2
        got = _get(harness, "sys").json()
        assert got["mode"] == "enforce"

    def test_enforce_warns_about_what_it_will_refuse(self, harness):
        seed(harness, lambda s: add_system(s, "sys"))
        warnings = _put(harness, "sys", {**CONFIG, "mode": "enforce"}).json()["warnings"]
        assert any("crm_list_customers" in w for w in warnings)
        assert any("X-Brutor-Run-Scope" in w for w in warnings)

    @pytest.mark.parametrize(
        "bad, says",
        [
            ({**CONFIG, "mode": "block"}, "mode"),
            ({**CONFIG, "rules": [{"tool": "t", "argument": "a", "key": "nope"}]}, "not one of the declared keys"),
            ({**CONFIG, "rules": [{"tool": "t", "argument": "a..b", "key": "customer"}]}, "dotted path"),
            ({**CONFIG, "keys": [{"key": "Customer"}]}, "lower-case"),
            ({**CONFIG, "unscoped_tools": ["crm_get_customer"]}, "cannot both"),
        ],
    )
    def test_a_bad_config_is_refused_with_a_reason(self, harness, bad, says):
        seed(harness, lambda s: add_system(s, "sys"))
        r = _put(harness, "sys", bad)
        assert r.status_code == 422
        assert says in r.text

    def test_a_plain_group_has_no_data_scope(self, harness):
        from app.database.models.resource_group import GroupType

        seed(harness, lambda s: add_system(s, "plain", group_type=GroupType.TEAM))
        assert _put(harness, "plain", CONFIG).status_code == 422

    def test_another_tenants_system_is_not_found(self, harness):
        seed(harness, lambda s: add_system(s, "sys"))
        assert _put(harness, "sys", CONFIG, tenant="t2").status_code == 404


class TestContract:
    def test_the_config_enters_the_closure_only_once_it_exists(self, harness):
        from app.database.models.resource_group import ResourceGroup
        from app.services.ai_system_closure import resolve_and_hash

        seed(harness, lambda s: add_system(s, "sys"))
        s = harness.Factory()
        before, h1 = resolve_and_hash(s, s.get(ResourceGroup, "sys"))
        s.close()
        assert "data_scope" not in before, "an unconfigured system keeps its hash"

        _put(harness, "sys", CONFIG)
        s = harness.Factory()
        after, h2 = resolve_and_hash(s, s.get(ResourceGroup, "sys"))
        s.close()
        assert after["data_scope"]["rules"][0]["tool"] == "crm_get_customer"
        assert h1 != h2, "changing what a run may touch is a contract change"


def _access(s, run, customer_ref, at, requester=None, subject=None):
    s.add(AISystemRecordAccess(
        id=f"ra-{run}-{customer_ref}", tenant_id="t1", ai_system_id="sys", root_task_id=run,
        tool="crm_get_customer", scope_key="customer", record_ref=customer_ref,
        verdict="in_scope", mode="audit", subject_id=subject, accessed_at=at,
    ))
    if requester and not s.get(AISystemRunScope, f"rs-{run}"):
        s.add(AISystemRunScope(
            id=f"rs-{run}", tenant_id="t1", root_task_id=run, scope_key="requester",
            scope_values=[requester], declared_at=at,
        ))


class TestReach:
    def _rule(self, h, **over):
        body = {"scope_key": "customer", "group_by": "scope:requester",
                "window_hours": 24, "max_distinct": 20}
        body.update(over)
        return h.client.post(
            "/v1/admin/ai-systems/sys/reach-rules", headers=h.login("t1"), json=body
        )

    def test_a_rule_needs_a_key_of_the_data_scope(self, harness):
        seed(harness, lambda s: add_system(s, "sys"))
        assert self._rule(harness).status_code == 422, "no data scope yet"
        _put(harness, "sys", CONFIG)
        r = self._rule(harness)
        assert r.status_code == 201, r.text
        assert r.json()["group_by"] == "scope:requester"
        assert self._rule(harness, group_by="ip").status_code == 422
        assert self._rule(harness, window_hours=1000).status_code == 422
        assert self._rule(harness, max_distinct=0).status_code == 422
        rules = _get(harness, "sys", "reach-rules").json()["rules"]
        assert len(rules) == 1

    def test_a_rule_can_be_changed_and_removed(self, harness):
        seed(harness, lambda s: add_system(s, "sys"))
        _put(harness, "sys", CONFIG)
        rid = self._rule(harness).json()["id"]
        h = harness.login("t1")
        r = harness.client.patch(
            f"/v1/admin/ai-systems/sys/reach-rules/{rid}", headers=h, json={"max_distinct": 40}
        )
        assert r.status_code == 200 and r.json()["max_distinct"] == 40
        assert harness.client.delete(
            f"/v1/admin/ai-systems/sys/reach-rules/{rid}", headers=h
        ).status_code == 204
        assert _get(harness, "sys", "reach-rules").json()["rules"] == []

    def test_the_preview_shows_who_would_have_breached(self, harness):
        now = datetime.now(timezone.utc)

        def _seed(s):
            add_system(s, "sys")
            # Requester 'S7': 25 different customers over an afternoon; 'S8': 5.
            for i in range(25):
                _access(s, f"s7-{i}", f"ref-{i}", now - timedelta(minutes=10 * i), requester="S7")
            for i in range(5):
                _access(s, f"s8-{i}", f"ref-{i}", now - timedelta(minutes=i), requester="S8")
            # A run that did not say who asked.
            _access(s, "anon", "ref-x", now - timedelta(minutes=3))

        seed(harness, _seed)
        _put(harness, "sys", CONFIG)
        r = harness.client.post(
            "/v1/admin/ai-systems/sys/reach-rules/preview",
            headers=harness.login("t1"),
            json={"scope_key": "customer", "group_by": "scope:requester",
                  "window_hours": 24, "max_distinct": 20},
        )
        assert r.status_code == 200, r.text
        p = r.json()
        assert p["groups_seen"] == 3
        assert p["groups_over_limit"] == 1
        assert p["top"][0] == {"group": "S7", "max_distinct_records": 25}
        assert {"group": "(undeclared)", "max_distinct_records": 1} in p["top"]


def test_validation_is_pure_and_normalises():
    config, warnings = validate_config(CONFIG)
    assert config["on_unbound"] == "deny"
    assert config["keys"][0] == {"key": "customer", "source": "declared"}
    assert warnings == []


def test_skill_and_a2a_targets_are_named_by_prefix():
    """2026-11-08: skills and A2A messages are judged too, named
    `skill:<name>` and `a2a:<card name>`. Any other ':' name could never
    match a call (MCP tool names hold no ':'), so it is refused, not saved."""
    body = {
        **CONFIG,
        "rules": [
            {"tool": "skill:report-builder", "argument": "customer_id", "key": "customer"},
            {"tool": "a2a:billing-agent", "argument": "account.owner", "key": "customer"},
        ],
        "unscoped_tools": ["a2a:directory-agent"],
    }
    config, _ = validate_config(body)
    assert [r["tool"] for r in config["rules"]] == ["skill:report-builder", "a2a:billing-agent"]
    for bad in ("skills:report-builder", "skill:", "mcp:crm_get_customer"):
        with pytest.raises(ValueError, match="skill:<skill name>"):
            validate_config({**CONFIG, "rules": [{"tool": bad, "argument": "x", "key": "customer"}]})
    with pytest.raises(ValueError, match="a2a:<agent card name>"):
        validate_config({**CONFIG, "unscoped_tools": ["a2a:"]})


def test_model_tool_calls_are_named_by_prefix():
    """A tool call a model asks for, which the agent may run
    itself outside the gateway, is judged as `model:<function name>`."""
    config, _ = validate_config({
        **CONFIG,
        "rules": [{"tool": "model:get_customer", "argument": "customer_id", "key": "customer"}],
        "unscoped_tools": ["model:list_customers"],
    })
    assert [r["tool"] for r in config["rules"]] == ["model:get_customer"]
    assert config["unscoped_tools"] == ["model:list_customers"]
    with pytest.raises(ValueError, match="model:<function name>"):
        validate_config({**CONFIG, "rules": [{"tool": "model:", "argument": "x", "key": "customer"}]})
