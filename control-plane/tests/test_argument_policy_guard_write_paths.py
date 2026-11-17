# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The presence guard on every write path of an argument policy.

A comparison rule reading a MISSING field does not hold, so a banded policy
is only a control when something refuses the call that omits the field. The
validator that insists on that guard ran on create alone: a PATCH could strip
the guard from a saved policy, and a guard that only warns satisfied it.

Each path that writes a policy is exercised here over HTTP, and each test
asserts the stored row, not just the status: a refusal that still saved is
the bug.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from app.api.v1.endpoints.admin.argument_policies import create_argument_policies_router
from app.core.logging_system import BrutorLogger
from app.database.models.argument_policy import ArgumentPolicy
from tests.admin_endpoint_harness import build_admin_harness, throwaway_pg_database

BASE = "/v1/admin/argument-policies"
_n = {"i": 0}

THRESHOLD = {"deny_if": "field_gt", "field": "amount", "value": 1000}
PRESENT = {"allow_only_if": "field_present", "values": ["amount"]}
SCHEMA_INVALID = {"deny_if": "schema_invalid"}
REQUIRED_AMOUNT = {"schema": {"required": ["amount"]}}
BYPASS = "bypassed by omitting"


@pytest.fixture(scope="module")
def h():
    for url in throwaway_pg_database("arg_policy_guard"):
        harness = build_admin_harness(
            [create_argument_policies_router(BrutorLogger("test-arg-policy-guard"))], database_url=url
        )
        yield harness
        harness.close()


def _body(**over) -> dict:
    _n["i"] += 1
    return {
        "name": f"band-{_n['i']}",
        "surface": "mcp_input",
        "target": "transfer_funds",
        "argument_key": "*",
        "analyzer": "json",
        "rules": [PRESENT, THRESHOLD],
        **over,
    }


def _create(h, **over):
    return h.client.post(BASE, json=_body(**over), headers=h.headers("t1"))


def _guarded(h, **over) -> dict:
    r = _create(h, **over)
    assert r.status_code == 201, r.text
    return r.json()


def _patch(h, policy_id: str, body: dict):
    return h.client.patch(f"{BASE}/{policy_id}", json=body, headers=h.headers("t1"))


def _row(h, policy_id: str) -> dict:
    s = h.Factory()
    try:
        p = s.get(ArgumentPolicy, policy_id)
        return {
            "rules": p.rules, "analyzer_config": p.analyzer_config,
            "argument_key": p.argument_key, "severity": p.severity, "enabled": p.enabled,
        }
    finally:
        s.close()


def _by_name(h, name: str):
    s = h.Factory()
    try:
        return s.query(ArgumentPolicy).filter_by(tenant_id="t1", name=name).first()
    finally:
        s.close()


class TestPatchRunsTheGuardOnTheMergedRow:
    """What is checked is the row as it will be after the patch."""

    def test_rules_cannot_be_patched_to_a_bare_threshold(self, h):
        p = _guarded(h)
        before = _row(h, p["id"])
        r = _patch(h, p["id"], {"rules": [THRESHOLD]})
        assert r.status_code == 400, r.text
        assert BYPASS in r.json()["detail"] and "amount" in r.json()["detail"]
        assert _row(h, p["id"]) == before

    def test_the_schema_requirement_cannot_be_patched_away(self, h):
        p = _guarded(h, analyzer_config=REQUIRED_AMOUNT, rules=[SCHEMA_INVALID, THRESHOLD])
        before = _row(h, p["id"])
        r = _patch(h, p["id"], {"analyzer_config": {"schema": {"required": []}}})
        assert r.status_code == 400, r.text
        assert BYPASS in r.json()["detail"]
        assert _row(h, p["id"]) == before

    def test_the_analyzer_config_cannot_be_cleared_from_under_the_guard(self, h):
        p = _guarded(h, analyzer_config=REQUIRED_AMOUNT, rules=[SCHEMA_INVALID, THRESHOLD])
        before = _row(h, p["id"])
        r = _patch(h, p["id"], {"analyzer_config": None})
        assert r.status_code == 400, r.text
        assert _row(h, p["id"]) == before

    def test_the_argument_key_cannot_be_narrowed_under_a_band(self, h):
        """Naming one key analyzes that member alone, so no path resolves and
        neither the band nor its guard reads anything."""
        p = _guarded(h)
        r = _patch(h, p["id"], {"argument_key": "amount"})
        assert r.status_code == 400, r.text
        assert "argument_key must be" in r.json()["detail"]
        assert _row(h, p["id"])["argument_key"] == "*"

    def test_a_threshold_cannot_be_added_to_an_unguarded_policy(self, h):
        p = _guarded(h, rules=[{"deny_if": "field_not_present", "values": ["memo"]}])
        r = _patch(h, p["id"], {"rules": [{"deny_if": "field_not_present", "values": ["memo"]}, THRESHOLD]})
        assert r.status_code == 400, r.text
        assert len(_row(h, p["id"])["rules"]) == 1

    def test_a_patch_that_keeps_the_guard_is_saved(self, h):
        p = _guarded(h)
        tighter = {**THRESHOLD, "value": 500}
        r = _patch(h, p["id"], {"rules": [PRESENT, tighter]})
        assert r.status_code == 200, r.text
        assert _row(h, p["id"])["rules"][1]["value"] == 500

    def test_one_guard_may_be_swapped_for_the_other_in_one_patch(self, h):
        p = _guarded(h)
        r = _patch(h, p["id"], {"analyzer_config": REQUIRED_AMOUNT, "rules": [SCHEMA_INVALID, THRESHOLD]})
        assert r.status_code == 200, r.text

    def test_a_guarded_policy_can_still_be_switched_off(self, h):
        p = _guarded(h)
        r = _patch(h, p["id"], {"enabled": False})
        assert r.status_code == 200, r.text
        assert _row(h, p["id"])["enabled"] is False


class TestTheGuardMustDenyOverHttp:
    """The gateway derives a rule's severity as its own, else the
    policy's, and a `warn` is logged and let through."""

    @pytest.mark.parametrize(
        "over",
        [
            {"rules": [{**PRESENT, "severity": "warn"}, THRESHOLD]},
            {"rules": [{**PRESENT, "severity": "approval_required"}, THRESHOLD]},
            {"severity": "warn", "rules": [PRESENT, {**THRESHOLD, "severity": "deny"}]},
            {"severity": "approval_required", "rules": [PRESENT, THRESHOLD]},
            {
                "analyzer_config": REQUIRED_AMOUNT,
                "rules": [{**SCHEMA_INVALID, "severity": "warn"}, THRESHOLD],
            },
        ],
        ids=["rule-warn", "rule-approval", "inherits-warn", "inherits-approval", "schema-rule-warn"],
    )
    def test_create_refuses_a_guard_that_does_not_deny(self, h, over):
        body = _body(**over)
        r = h.client.post(BASE, json=body, headers=h.headers("t1"))
        assert r.status_code == 422, r.text
        assert BYPASS in r.text and "severity" in r.text
        assert _by_name(h, body["name"]) is None

    def test_a_denying_guard_under_a_warn_policy_is_saved(self, h):
        r = _create(h, severity="warn", rules=[{**PRESENT, "severity": "deny"}, THRESHOLD])
        assert r.status_code == 201, r.text

    def test_the_policy_severity_cannot_be_patched_out_from_under_the_guard(self, h):
        """The guard carried no severity of its own, so it denied only because
        the policy did."""
        p = _guarded(h)
        r = _patch(h, p["id"], {"severity": "warn"})
        assert r.status_code == 400, r.text
        assert BYPASS in r.json()["detail"]
        assert _row(h, p["id"])["severity"] == "deny"

    def test_the_guard_cannot_be_patched_down_to_a_warning(self, h):
        p = _guarded(h)
        before = _row(h, p["id"])
        r = _patch(h, p["id"], {"rules": [{**PRESENT, "severity": "warn"}, THRESHOLD]})
        assert r.status_code == 400, r.text
        assert _row(h, p["id"]) == before


class TestImportRunsTheGuard:
    def _import(self, h, entries):
        return h.client.post(
            f"{BASE}/import.yaml", content=yaml.safe_dump({"policies": entries}), headers=h.headers("t1")
        )

    def _entry(self, **over) -> dict:
        return _body(**over)

    def test_a_bare_threshold_is_reported_and_not_written(self, h):
        entry = self._entry(rules=[THRESHOLD])
        r = self._import(h, [entry])
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["created"] == [] and BYPASS in body["errors"][0]["error"]
        assert _by_name(h, entry["name"]) is None

    def test_an_import_cannot_strip_the_guard_from_a_saved_policy(self, h):
        p = _guarded(h)
        before = _row(h, p["id"])
        entry = self._entry(name=p["name"], rules=[THRESHOLD])
        body = self._import(h, [entry]).json()
        assert body["updated"] == [] and BYPASS in body["errors"][0]["error"]
        assert _row(h, p["id"]) == before

    def test_a_guard_that_only_warns_is_reported_and_not_written(self, h):
        entry = self._entry(rules=[{**PRESENT, "severity": "warn"}, THRESHOLD])
        body = self._import(h, [entry]).json()
        assert body["created"] == [] and BYPASS in body["errors"][0]["error"]
        assert _by_name(h, entry["name"]) is None

    def test_an_import_cannot_turn_a_saved_guard_into_a_warning(self, h):
        p = _guarded(h)
        entry = self._entry(name=p["name"], severity="warn")
        body = self._import(h, [entry]).json()
        assert body["updated"] == [] and BYPASS in body["errors"][0]["error"]
        assert _row(h, p["id"])["severity"] == "deny"


class TestNothingSeedsAnArgumentPolicy:
    """No seed module writes an argument policy today, so there is no seeded
    row for the guard to pass or fail. A seed that starts writing them has to
    go through the validator; this fails then, to say so."""

    def test_no_seed_or_catalog_data_mentions_argument_policies(self):
        root = pathlib.Path(__file__).resolve().parents[1] / "app" / "database"
        offenders = []
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix not in {".py", ".json", ".yaml", ".yml", ".sql"}:
                continue
            if "models" in path.relative_to(root).parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "argument_polic" in text or "ArgumentPolicy" in text:
                offenders.append(str(path.relative_to(root)))
        assert offenders == [], (
            "a seed now writes argument policies: validate each seeded row with "
            f"validate_comparison_rules_have_a_presence_guard ({offenders})"
        )
