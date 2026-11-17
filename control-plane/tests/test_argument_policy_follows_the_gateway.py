# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
What the control plane accepts and says about an argument policy is what the
gateway does with it.

Three places where the two had parted:

- **Operands.** The gateway compares a field with a string, a number or a
  boolean operand, and a boolean field only with a boolean. Any other
  operand (a list, an object) can never be compared: every call that
  carries the field is refused as undecidable. Such an operand is refused at
  save.
- **Policy severity.** The gateway reads a policy's own severity as it is
  stored: `deny`, `warn` or `approval_required`, which holds the call for a
  person when the rule that tripped carries no severity of its own. All
  three are accepted, on every path that writes a policy. A presence guard
  must still deny, by its effective severity.
- **Analysis limits.** The gateway analyzes at most 64 elements of an
  array, 512 leaves and 8 levels of nesting, unless the policy states its
  own in `analyzer_config` (`max_leaves` 1-4096, `max_depth` 1-32,
  `max_array_elements` 1-1024), and refuses a call beyond them. The API's
  descriptions say so with the gateway's own numbers, and a stated limit
  that is not an integer in its range is refused at save: the gateway
  would refuse every call of such a policy.
"""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from app.api.v1.endpoints.admin.argument_policies import create_argument_policies_router
from app.api.v1.models.admin import argument_policy as model
from app.api.v1.models.admin.argument_policy import (
    ArgumentPolicyCreateRequest,
    ArgumentPolicyRule,
    ArgumentPolicyUpdateRequest,
)
from app.core.logging_system import BrutorLogger
from app.database.models.argument_policy import ALLOWED_SEVERITIES, ArgumentPolicy
from tests.admin_endpoint_harness import build_admin_harness
from tests.core_repo import core_revision, core_source

BASE = "/v1/admin/argument-policies"
PRESENT = {"allow_only_if": "field_present", "values": ["flag"]}
THRESHOLD = {"deny_if": "field_gt", "field": "amount", "value": 1000}
AMOUNT_PRESENT = {"allow_only_if": "field_present", "values": ["amount"]}


def _body(**over) -> dict:
    return {
        "name": "p", "surface": "mcp_input", "target": "transfer_funds", "argument_key": "*", "analyzer": "json",
        "rules": [PRESENT], **over,
    }


# --- operands -------------------------------------------------------------------


@pytest.mark.parametrize("predicate", ["field_eq", "field_ne"])
@pytest.mark.parametrize("operand", [True, False, "eu", 0, 3, 2.5])
def test_an_equality_rule_takes_a_string_a_number_or_a_boolean(predicate, operand):
    rule = ArgumentPolicyRule(deny_if=predicate, field="flag", value=operand)
    assert rule.value == operand and type(rule.value) is type(operand)
    # The stored rule keeps the JSON type: `true` is not `1`.
    assert rule.model_dump(exclude_none=True)["value"] is operand or rule.model_dump()["value"] == operand


@pytest.mark.parametrize("predicate", ["field_eq", "field_ne"])
@pytest.mark.parametrize("operand", [[True], {"a": 1}, [], {}])
def test_an_equality_rule_refuses_an_operand_the_gateway_cannot_compare(predicate, operand):
    with pytest.raises(ValidationError, match="a string, a number or a boolean"):
        ArgumentPolicyRule(deny_if=predicate, field="flag", value=operand)


@pytest.mark.parametrize("predicate", ["field_in", "field_not_in"])
def test_a_membership_rule_takes_strings_numbers_and_booleans(predicate):
    rule = ArgumentPolicyRule(deny_if=predicate, field="flag", values=[True, False])
    assert rule.values == [True, False] and all(type(v) is bool for v in rule.values)
    ArgumentPolicyRule(deny_if=predicate, field="flag", values=["eu", "us", 3, 2.5, True])


@pytest.mark.parametrize("predicate", ["field_in", "field_not_in"])
@pytest.mark.parametrize("values", [[["eu"]], [{"a": 1}], ["eu", None]])
def test_a_membership_rule_refuses_an_option_the_gateway_cannot_compare(predicate, values):
    with pytest.raises(ValidationError, match="a string, a number or a boolean"):
        ArgumentPolicyRule(deny_if=predicate, field="flag", values=values)


def test_the_stored_dict_shape_is_judged_the_same_way():
    """A bundle hands the stored dicts, not the parsed rules."""
    with pytest.raises(ValueError, match=r"rules\[1\].*a string, a number or a boolean"):
        model.validate_policy_as_stored(
            [PRESENT, {"deny_if": "field_eq", "field": "flag", "value": [True]}], None, "*", "deny"
        )
    model.validate_policy_as_stored(
        [PRESENT, {"deny_if": "field_eq", "field": "flag", "value": True}], None, "*", "deny"
    )


# --- the policy's own severity -------------------------------------------------------


def test_the_gateway_reads_a_policys_severity_as_it_is_stored():
    """The fact the three accepted values rest on, read from the gateway."""
    cache = " ".join(
        core_source("services", "argument_policy", "cache.rs").read_text(encoding="utf-8").split("#[cfg(test)]")[0].split()
    )
    read = f" [the gateway's source read: {core_revision()}]"
    assert (
        'let severity = match self.severity.as_str() { "warn" => Severity::Warn, '
        '"approval_required" => Severity::ApprovalRequired, "deny" => Severity::Deny, unknown => {'
    ) in cache, "the gateway's loader no longer reads a policy's severity as deny, approval_required or warn" + read
    assert model.POLICY_SEVERITIES == ("deny", "approval_required", "warn")
    assert set(model.POLICY_SEVERITIES) == set(ALLOWED_SEVERITIES)


@pytest.mark.parametrize("severity", ["deny", "approval_required", "warn"])
def test_a_policy_takes_each_severity_the_gateway_reads(severity):
    assert ArgumentPolicyCreateRequest(**_body(severity=severity, rules=[])).severity == severity
    assert ArgumentPolicyUpdateRequest(severity=severity).severity == severity
    for request in (ArgumentPolicyCreateRequest, ArgumentPolicyUpdateRequest):
        with pytest.raises(ValidationError):
            request(**(_body(severity="block") if request is ArgumentPolicyCreateRequest else {"severity": "block"}))


def test_the_openapi_schema_offers_what_is_accepted():
    for request in (ArgumentPolicyCreateRequest, ArgumentPolicyUpdateRequest):
        severity = request.model_json_schema()["properties"]["severity"]
        offered = severity.get("enum") or next(o["enum"] for o in severity["anyOf"] if "enum" in o)
        assert offered == ["deny", "approval_required", "warn"]
        assert "holds it for a person" in severity["description"]
    rule = ArgumentPolicyRule.model_json_schema()["properties"]["severity"]
    assert "approval_required" in str(rule)


@pytest.fixture()
def h():
    harness = build_admin_harness(
        [create_argument_policies_router(BrutorLogger("test-arg-policy-gateway"))], logger_name="arg-policy-gateway"
    )
    yield harness
    harness.close()


def _stored(h) -> list:
    with h.Factory() as s:
        return sorted((p.name, p.severity) for p in s.query(ArgumentPolicy))


def test_a_policy_level_approval_required_is_stored_by_create_and_update(h):
    t1 = h.headers("t1")
    r = h.client.post(BASE, json=_body(name="held", severity="approval_required", rules=[THRESHOLD, {**AMOUNT_PRESENT, "severity": "deny"}]), headers=t1)
    assert r.status_code == 201 and r.json()["severity"] == "approval_required", r.text
    r = h.client.post(BASE, json=_body(), headers=t1)
    assert r.status_code == 201, r.text
    policy_id = r.json()["id"]
    r = h.client.patch(f"{BASE}/{policy_id}", json={"severity": "approval_required"}, headers=t1)
    assert r.status_code == 200 and r.json()["severity"] == "approval_required", r.text
    assert _stored(h) == [("held", "approval_required"), ("p", "approval_required")]


def test_a_presence_guard_does_not_inherit_a_policys_approval_required(h):
    """A held call is not a refused one: the field a comparison reads must
    be guarded by a rule that denies, whatever the policy's severity."""
    t1 = h.headers("t1")
    r = h.client.post(BASE, json=_body(severity="approval_required", rules=[AMOUNT_PRESENT, THRESHOLD]), headers=t1)
    assert r.status_code == 422 and "does not deny" in r.text, r.text
    assert _stored(h) == []
    # Under a denying policy the guard inherits `deny`; raising the policy
    # to `approval_required` would take that away, and is refused.
    r = h.client.post(BASE, json=_body(rules=[AMOUNT_PRESENT, THRESHOLD]), headers=t1)
    assert r.status_code == 201, r.text
    r = h.client.patch(f"{BASE}/{r.json()['id']}", json={"severity": "approval_required"}, headers=t1)
    assert r.status_code == 400 and "does not deny" in r.json()["detail"], r.text
    assert _stored(h) == [("p", "deny")]


# --- the analysis limits ----------------------------------------------------------------


def test_the_stated_analysis_limits_are_the_gateways():
    source = core_source("services", "argument_policy", "mod.rs").read_text(encoding="utf-8")
    read = f" [the gateway's source read: {core_revision()}]"

    def block(name: str) -> dict:
        found = re.search(rf"pub const {name}: Self = Self \{{(.*?)\}};", source, re.S)
        assert found, f"AnalysisLimits::{name} is not where it was" + read
        return {key: int(value) for key, value in re.findall(r"(\w+): (\d+),", found.group(1))}

    assert block("DEFAULT") == model.ANALYSIS_LIMIT_DEFAULTS == {
        "max_leaves": 512, "max_depth": 8, "max_array_elements": 64,
    }, read
    assert block("CEILING") == model.ANALYSIS_LIMIT_CEILINGS == {
        "max_leaves": 4096, "max_depth": 32, "max_array_elements": 1024,
    }, read
    # The gateway refuses what is outside 1..=ceiling, and never clamps.
    assert ".filter(|n| (1..=ceiling).contains(n))" in source, read
    assert '"analyzer_config.{} must be an integer from 1 to {}"' in source, read
    # ... and the name of the refusal.
    evaluator = core_source("services", "argument_policy", "evaluator.rs").read_text(encoding="utf-8")
    assert 'RULE_ARGUMENTS_TRUNCATED: &str = "arguments_truncated"' in evaluator


def test_the_descriptions_state_the_limits_and_the_operands():
    create = ArgumentPolicyCreateRequest.model_json_schema()
    analyzer = create["properties"]["analyzer"]["description"]
    for number in ("64", "512", "8"):
        assert re.search(rf"\b{number}\b", analyzer), analyzer
    assert "arguments_truncated" in analyzer and "refused" in analyzer
    assert model.ANALYSIS_CAPS in analyzer
    rule = ArgumentPolicyRule.model_json_schema()["properties"]
    assert "boolean" in rule["value"]["description"] and "boolean" in rule["values"]["description"]
    assert "64" in rule["field"]["description"] and "8" in rule["field"]["description"]
    update = ArgumentPolicyUpdateRequest.model_json_schema()["properties"]
    assert model.ANALYSIS_CAPS in str(update["analyzer"])
    for properties in (create["properties"], update):
        config = str(properties["analyzer_config"])
        for key, default in model.ANALYSIS_LIMIT_DEFAULTS.items():
            assert f"'{key}' (an integer from 1 to {model.ANALYSIS_LIMIT_CEILINGS[key]}, default {default})" in config
        assert "refused, never adjusted" in config


LIMITS = [("max_leaves", 4096), ("max_depth", 32), ("max_array_elements", 1024)]
NOT_A_LIMIT = [0, -1, 1.5, 2.0, "8", None, True, False, [8], {"n": 8}]


@pytest.mark.parametrize("key, ceiling", LIMITS)
def test_a_limit_from_one_to_its_ceiling_is_accepted_as_written(key, ceiling):
    for stated in (1, 2, ceiling):
        created = ArgumentPolicyCreateRequest(**_body(analyzer_config={key: stated}))
        assert created.analyzer_config == {key: stated}
        assert ArgumentPolicyUpdateRequest(analyzer_config={key: stated}).analyzer_config == {key: stated}
    assert model.analysis_limit_error({key: ceiling, "schema": {"required": ["a"]}}) is None
    assert model.analysis_limit_error(None) is None and model.analysis_limit_error({}) is None


@pytest.mark.parametrize("key, ceiling", LIMITS)
@pytest.mark.parametrize("stated", NOT_A_LIMIT, ids=repr)
def test_anything_else_is_refused_and_never_clamped(key, ceiling, stated):
    message = rf"analyzer_config\.{key} must be an integer from 1 to {ceiling}"
    for bad in (stated, ceiling + 1):
        with pytest.raises(ValidationError, match=message):
            ArgumentPolicyCreateRequest(**_body(analyzer_config={key: bad}))
        with pytest.raises(ValidationError, match=message):
            ArgumentPolicyUpdateRequest(analyzer_config={key: bad})
        with pytest.raises(ValueError, match=message):
            model.validate_policy_as_stored([PRESENT], {key: bad}, "*", "deny")


def test_no_write_path_stores_a_limit_the_gateway_does_not_apply(h):
    t1 = h.headers("t1")
    # create
    r = h.client.post(BASE, json=_body(analyzer_config={"max_depth": 33}), headers=t1)
    assert r.status_code == 422 and "analyzer_config.max_depth must be an integer from 1 to 32" in r.text, r.text
    r = h.client.post(BASE, json=_body(analyzer_config={"max_depth": 32, "max_leaves": 2000}), headers=t1)
    assert r.status_code == 201 and r.json()["analyzer_config"] == {"max_depth": 32, "max_leaves": 2000}, r.text
    policy_id = r.json()["id"]
    # update: the patch itself, and a row whose stored limit is out of range
    r = h.client.patch(f"{BASE}/{policy_id}", json={"analyzer_config": {"max_leaves": 0}}, headers=t1)
    assert r.status_code == 422 and "analyzer_config.max_leaves must be an integer from 1 to 4096" in r.text, r.text
    r = h.client.patch(f"{BASE}/{policy_id}", json={"analyzer_config": {"max_array_elements": 1024}}, headers=t1)
    assert r.status_code == 200 and r.json()["analyzer_config"] == {"max_array_elements": 1024}, r.text

    def stale(s):
        s.get(ArgumentPolicy, policy_id).analyzer_config = {"max_array_elements": 5000}

    h.seed(stale)
    r = h.client.patch(f"{BASE}/{policy_id}", json={"rules": [PRESENT]}, headers=t1)
    assert r.status_code == 400 and "analyzer_config.max_array_elements must be an integer from 1 to 1024" in r.text
    # import
    document = (
        "policies:\n  - name: imported\n    surface: mcp_input\n    target: t\n    argument_key: '*'\n"
        "    analyzer: json\n    analyzer_config:\n      max_leaves: {limit}\n    rules:\n"
        "      - allow_only_if: field_present\n        values: [flag]\n"
    )
    r = h.client.post(f"{BASE}/import.yaml", content=document.format(limit=5000), headers=t1)
    assert "analyzer_config.max_leaves must be an integer from 1 to 4096" in r.text, r.text
    with h.Factory() as s:
        assert s.query(ArgumentPolicy).filter(ArgumentPolicy.name == "imported").count() == 0
    r = h.client.post(f"{BASE}/import.yaml", content=document.format(limit=4096), headers=t1)
    assert r.status_code == 200, r.text
    with h.Factory() as s:
        (row,) = s.query(ArgumentPolicy).filter(ArgumentPolicy.name == "imported").all()
        assert row.analyzer_config == {"max_leaves": 4096}


# --- approvals and model tool calls ---------------------------------------------
#
# The gateway holds nothing for a person on a model's tool calls: there is
# no approval there. An `approval_required` on that surface would read as a
# hold and be none, so it is refused when it is saved, wherever it is said.

LLM = "llm_output_tool_calls"
NO_APPROVALS = "approvals are not available for model tool calls"
RULE_HOLDS = {"deny_if": "field_gt", "field": "amount", "value": 1000, "severity": "approval_required"}


@pytest.mark.parametrize(
    "over",
    [
        {"severity": "approval_required"},
        {"rules": [AMOUNT_PRESENT, RULE_HOLDS]},
        {"severity": "warn", "rules": [AMOUNT_PRESENT, RULE_HOLDS]},
    ],
    ids=["policy", "rule", "rule-under-warn"],
)
def test_a_policy_on_model_tool_calls_holds_nothing_for_approval(over):
    with pytest.raises(ValidationError) as refused:
        ArgumentPolicyCreateRequest(**_body(surface=LLM, **over))
    assert NO_APPROVALS in str(refused.value).lower()


@pytest.mark.parametrize("surface", ["mcp_input", "skill_input"])
def test_the_other_surfaces_hold(surface):
    ArgumentPolicyCreateRequest(**_body(surface=surface, severity="approval_required"))
    ArgumentPolicyCreateRequest(**_body(surface=surface, rules=[AMOUNT_PRESENT, RULE_HOLDS]))


@pytest.mark.parametrize("severity", ["deny", "warn"])
def test_a_policy_on_model_tool_calls_denies_or_warns(severity):
    ArgumentPolicyCreateRequest(**_body(surface=LLM, severity=severity))


def test_the_api_refuses_it_on_create_and_on_every_update_that_makes_it_so(h):
    created = h.client.post(BASE, json=_body(surface=LLM, severity="approval_required"), headers=h.headers())
    assert created.status_code == 422, created.text
    assert NO_APPROVALS in created.text.lower()

    # A policy that holds on another surface is not moved to this one...
    holding = h.client.post(BASE, json=_body(name="holding", severity="approval_required"), headers=h.headers())
    assert holding.status_code == 201, holding.text
    moved = h.client.patch(f"{BASE}/{holding.json()['id']}", json={"surface": LLM}, headers=h.headers())
    assert moved.status_code in (400, 422), moved.text
    assert NO_APPROVALS in moved.text.lower()

    # ...and a policy on this one is not given a hold, by its severity or by a rule.
    on_llm = h.client.post(BASE, json=_body(name="on-llm", surface=LLM), headers=h.headers())
    assert on_llm.status_code == 201, on_llm.text
    for patch in ({"severity": "approval_required"}, {"rules": [AMOUNT_PRESENT, RULE_HOLDS]}):
        r = h.client.patch(f"{BASE}/{on_llm.json()['id']}", json=patch, headers=h.headers())
        assert r.status_code in (400, 422), (patch, r.text)
        assert NO_APPROVALS in r.text.lower()
    with h.Factory() as s:
        stored = s.get(ArgumentPolicy, on_llm.json()["id"])
        assert stored.severity == "deny" and stored.surface == LLM


def test_tool_use_governance_does_not_ask_for_approval():
    from app.database.models.resource_group import validate_llm_governance

    ok, error = validate_llm_governance({"tool_use": {"access": "approval_required"}})
    assert ok is False
    assert NO_APPROVALS in error.lower()
    for access in ("enabled", "disabled"):
        assert validate_llm_governance({"tool_use": {"access": access}}) == (True, None)


@pytest.mark.parametrize("value", [300, 1, None, "soon"])
def test_tool_use_governance_takes_no_approval_window(value):
    """There is nothing to wait for: no call is held on a model's tool use."""
    from app.database.models.resource_group import validate_llm_governance

    ok, error = validate_llm_governance({"tool_use": {"access": "enabled", "approval_timeout_seconds": value}})
    assert ok is False
    assert "tool_use.approval_timeout_seconds" in error and NO_APPROVALS in error.lower()


def test_the_list_says_which_severities_each_surface_takes(h):
    """What the form offers per surface is what a save accepts there, for
    the policy and for a rule."""
    from app.api.v1.models.admin import argument_policy as schema
    from app.database.models.argument_policy import ALLOWED_SURFACES

    r = h.client.get(BASE, headers=h.headers())
    assert r.status_code == 200, r.text
    answered = r.json()["severities_by_surface"]
    assert set(answered) == set(ALLOWED_SURFACES)
    assert answered[LLM] == ["deny", "warn"]
    assert answered["mcp_input"] == answered["skill_input"] == ["deny", "approval_required", "warn"]
    for surface in ALLOWED_SURFACES:
        for severity in schema.POLICY_SEVERITIES:
            accepted = severity in answered[surface]
            assert (schema.approval_on_model_tool_calls_error(surface, [], severity) is None) is accepted
            rule = [{"deny_if": "field_gt", "field": "amount", "value": 1, "severity": severity}]
            assert (schema.approval_on_model_tool_calls_error(surface, rule, "deny") is None) is accepted

    document = h.client.app.openapi()["components"]["schemas"]
    listed = document["ArgumentPolicyListResponse"]["properties"]["severities_by_surface"]
    assert "llm_output_tool_calls" in listed["description"]
