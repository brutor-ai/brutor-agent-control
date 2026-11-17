# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The Python and Rust inheritance merges must agree, field for field.

Two implementations of governance inheritance exist and both are load
bearing. The control plane merges to answer "what does this group
effectively have?" for the console, exports and sealed contracts. The Rust
proxy merges again, from the same rows, to decide what actually happens to
a request. Nothing forces them to agree, and they drifted:

* skill governance — Python merged `discovery_enabled` and
  `approval_timeout_seconds`; Rust merged `max_concurrent_executions` and
  `max_tool_calls_per_execution`. Only `execution_mode` was common, so the
  console and the proxy disagreed about four of the five schema fields.
* limits — the gateway merged every limit family through one
  `usage::restrictive_merge` in which a level's `null` or value of another
  type leaves the ancestor's limit and a number that is not positive is
  unset unless its family reads it as a limit; Python let the child's
  `null`, string or `0` replace the ancestor's limit, so the console showed
  no limit where the gateway enforced one. The limits, skill and media
  governance merges are now held to the gateway's case by case: each table
  row names the gateway's unit test that pins the same case.

That drift was invisible because each side's own tests passed. This suite
reads both source trees and asserts the field sets and orderings match, the
same way `test_compliance_python_rust_parity.py` guards the tag catalogue.

It fails loudly rather than skipping when the core repo is missing: a
skipped parity test reports success while the drift it exists to catch goes
through unnoticed.
"""

from __future__ import annotations

import copy
import json
import re

import pytest

from tests.core_repo import core_revision, core_source

from app.api.v1.endpoints.admin.resource_groups import _GOVERNANCE_RULES
from app.database.models.resource_group import (
    LIMIT_DOCUMENT_FAMILIES,
    MEDIA_GOVERNANCE_FIELD_RULES,
    NON_POSITIVE_IS_A_LIMIT,
    SKILL_GOVERNANCE_CEILINGS,
    SKILL_GOVERNANCE_FIELDS,
    LimitFamily,
    _ACCESS_STRICTNESS,
    _ENFORCE_STRICTNESS,
    _SKILL_EXECUTION_STRICTNESS,
    _governance_restrictive_merge,
    _media_governance_restrictive_merge,
    merge_limit_documents,
    restrictive_merge,
    _skill_governance_restrictive_merge,
    validate_llm_governance,
    validate_mcp_governance,
    validate_skill_governance,
)

# The gateway's files these tests read (`tests/core_repo.py`: read inside a
# test, so that without the core only those tests are skipped).
SKILL_ORCHESTRATOR = ("services", "skill_orchestrator.rs")
MEDIA_GOVERNANCE_RS = ("services", "media_governance.rs")
GOVERNANCE_RS = ("models", "governance.rs")
USAGE_RS = ("services", "usage.rs")
GROUP_CACHE_RS = ("services", "cache", "group_cache.rs")


def _read(parts: tuple) -> str:
    path = core_source(*parts)
    assert path.exists(), f"{path} not found in the gateway's source ({core_revision()})"
    return path.read_text()


@pytest.fixture(scope="module")
def skill_rs() -> str:
    return _read(SKILL_ORCHESTRATOR)


@pytest.fixture(scope="module")
def governance_rs() -> str:
    return _read(GOVERNANCE_RS)


@pytest.fixture(scope="module")
def usage_rs() -> str:
    return _read(USAGE_RS)


def _rust_str_array(text: str, name: str) -> list[str]:
    """Pull the elements of a `const NAME: [&str; N] = [...];` literal."""
    m = re.search(
        rf"const {re.escape(name)}:\s*\[&str;\s*\d+\]\s*=\s*\[(.*?)\];",
        text,
        re.S,
    )
    assert m, f"{name} not found in the Rust source"
    return re.findall(r'"([^"]+)"', m.group(1))


def _rust_match_ranks(text: str, fn_name: str) -> dict[str, int]:
    """Pull `Some("value") => rank,` arms out of a small ranking fn."""
    m = re.search(rf"fn {re.escape(fn_name)}\b.*?\{{(.*?)\n\}}", text, re.S)
    assert m, f"{fn_name} not found in the Rust source"
    return {
        name: int(rank)
        for name, rank in re.findall(r'Some\("([^"]+)"\)\s*=>\s*(\d+)', m.group(1))
    }


class TestSkillGovernanceParity:
    """The drift that actually happened."""

    def test_the_field_lists_match(self, skill_rs):
        rust_fields = _rust_str_array(skill_rs, "SKILL_GOVERNANCE_FIELDS")
        assert set(rust_fields) == set(SKILL_GOVERNANCE_FIELDS), (
            "Python and Rust must restrict the same skill governance fields; "
            f"python={sorted(SKILL_GOVERNANCE_FIELDS)} rust={sorted(rust_fields)}"
        )

    def test_the_field_list_matches_the_schema(self):
        """A field the validator accepts but no merge restricts would be
        stored, shown, and then ignored — so the merge list and the schema's
        accepted keys must be the same set."""
        rejected, msg = validate_skill_governance({"not_a_real_field": 1})
        assert not rejected and "not_a_real_field" in msg
        # Every merged field is accepted by the schema: a document naming
        # only that field validates.
        for field in SKILL_GOVERNANCE_FIELDS:
            sample = {
                "execution_mode": "disabled",
                "discovery_enabled": False,
                "approval_timeout_seconds": 60,
                "max_concurrent_executions": 2,
                "max_tool_calls_per_execution": 5,
            }[field]
            ok, err = validate_skill_governance({field: sample})
            assert ok, f"{field} is merged but rejected by the schema: {err}"

    def test_python_merges_every_field_it_lists(self):
        parent = {
            "execution_mode": "disabled",
            "discovery_enabled": False,
            "approval_timeout_seconds": 60,
            "max_concurrent_executions": 2,
            "max_tool_calls_per_execution": 5,
        }
        child = {
            "execution_mode": "unrestricted",
            "discovery_enabled": True,
            "approval_timeout_seconds": 600,
            "max_concurrent_executions": 99,
            "max_tool_calls_per_execution": 99,
        }
        merged = dict(parent)
        _skill_governance_restrictive_merge(merged, child)
        assert merged == parent, "the parent's value must survive every field"

    def test_rust_merges_every_field_it_lists(self, skill_rs):
        body = re.search(
            r"fn merge_skill_governance\(.*?\n\}", skill_rs, re.S
        )
        assert body, "merge_skill_governance not found"
        for field in SKILL_GOVERNANCE_FIELDS:
            assert f'"{field}"' in body.group(0), (
                f"Rust merge_skill_governance does not handle {field}"
            )

    def test_the_execution_mode_orderings_match(self, skill_rs):
        rust_ranks = _rust_match_ranks(skill_rs, "skill_execution_strictness")
        for mode, rank in rust_ranks.items():
            assert _SKILL_EXECUTION_STRICTNESS[mode] == rank, (
                f"execution_mode '{mode}' ranks {rank} in Rust, "
                f"{_SKILL_EXECUTION_STRICTNESS[mode]} in Python"
            )
        # Python may name a mode Rust treats as the default (rank 0).
        for mode, rank in _SKILL_EXECUTION_STRICTNESS.items():
            if rank > 0:
                assert rust_ranks.get(mode) == rank, f"Rust does not rank '{mode}'"


class TestLlmGovernanceParity:
    def test_the_enforce_orderings_match(self, governance_rs):
        rust = dict(
            re.findall(
                r'"(clamp|reject)"\s*=>\s*(\d+)',
                re.search(r"fn enforce_strictness.*?\n\}", governance_rs, re.S).group(0),
            )
        )
        assert {k: int(v) for k, v in rust.items()} == _ENFORCE_STRICTNESS

    def test_the_access_orderings_match(self, governance_rs):
        rust = dict(
            re.findall(
                r'"(enabled|approval_required|disabled)"\s*=>\s*(\d+)',
                re.search(r"fn access_strictness.*?\n\}", governance_rs, re.S).group(0),
            )
        )
        assert {k: int(v) for k, v in rust.items()} == _ACCESS_STRICTNESS

    def test_python_merges_every_section_the_schema_accepts(self):
        parent = {
            "temperature": {"max": 0.5, "enforce": "reject"},
            "context_window": {"max_input_tokens": 100, "max_output_tokens": 100, "enforce": "reject"},
            "tool_use": {"access": "disabled", "approval_timeout_seconds": 30,
                         "max_tool_calls_per_request": 1, "max_tool_calls_per_hour": 1,
                         "max_tool_calls_per_day": 1, "allowed_tools": ["a"]},
            "system_prompt": {"banned_patterns": ["x"]},
        }
        child = {
            "temperature": {"max": 2.0, "enforce": "clamp"},
            "context_window": {"max_input_tokens": 9999, "max_output_tokens": 9999, "enforce": "clamp"},
            "tool_use": {"access": "enabled", "approval_timeout_seconds": 999,
                         "max_tool_calls_per_request": 99, "max_tool_calls_per_hour": 99,
                         "max_tool_calls_per_day": 99, "allowed_tools": ["a", "b"]},
            "system_prompt": {"banned_patterns": []},
        }
        merged = {k: dict(v) for k, v in parent.items()}
        _governance_restrictive_merge(merged, child)
        assert merged["temperature"] == {"max": 0.5, "enforce": "reject"}
        assert merged["context_window"]["max_input_tokens"] == 100
        assert merged["context_window"]["enforce"] == "reject"
        assert merged["tool_use"]["access"] == "disabled"
        assert merged["tool_use"]["allowed_tools"] == ["a"], "intersection, not union"
        assert merged["system_prompt"]["banned_patterns"] == ["x"], "union keeps the parent's"

    def test_the_rule_table_covers_every_llm_section(self):
        ok, _ = validate_llm_governance({"temperature": {"max": 1.0}})
        assert ok
        paths = {p for p, *_ in _GOVERNANCE_RULES["llm_global_governance"]}
        for section in ("temperature", "context_window", "tool_use"):
            assert any(p.startswith(f"{section}.") for p in paths), (
                f"no relaxation rule covers the {section} section"
            )


class TestMcpGovernanceParity:
    def test_every_access_field_the_schema_accepts_has_a_rule(self):
        fields = [
            "default_tool_access",
            "default_resource_access",
            "default_prompt_access",
            "default_resource_template_access",
            "approval_timeout_seconds",
        ]
        ok, _ = validate_mcp_governance({f: "enabled" for f in fields[:4]})
        assert ok
        paths = {p for p, *_ in _GOVERNANCE_RULES["mcp_global_governance"]}
        assert paths == set(fields)

    def test_rust_maps_the_same_capability_types(self, usage_rs):
        body = re.search(
            r"fn mcp_default_access_from_gov.*?\n\}", usage_rs, re.S
        ).group(0)
        for key in (
            "default_tool_access",
            "default_resource_access",
            "default_prompt_access",
            "default_resource_template_access",
        ):
            assert f'"{key}"' in body, f"Rust does not resolve {key}"

    def test_rust_merges_the_group_default_across_the_chain(self, usage_rs):
        """The fallback used to read a single group's row, so a parent's
        `default_tool_access: disabled` never reached a child with no MCP
        governance of its own."""
        assert "async fn resolve_mcp_global_default_chain" in usage_rs
        body = re.search(
            r"async fn resolve_mcp_global_default_chain.*?\n\}", usage_rs, re.S
        ).group(0)
        assert "id = ANY($1)" in body, "the fallback must query the whole chain"
        assert "strictness()" in body, "the chain must merge most-restrictive"


class TestMediaGovernanceParity:
    """Media is the surface the control plane did not compute at all — the
    proxy merged it across the chain while the console showed nothing. The
    Python rules added to close that must match the Rust ones field for
    field, including which direction each field tightens in."""

    @pytest.fixture(scope="class")
    def media_rs(self) -> str:
        return _read(MEDIA_GOVERNANCE_RS)

    def _rust_rules(self, text: str) -> dict[str, str]:
        body = re.search(r"fn media_field_rule.*?\n\}", text, re.S).group(0)
        rules: dict[str, str] = {}
        # Arms look like:  "a" | "b" => Some(MediaMergeRule::Whitelist),
        arm_re = re.compile(
            r"((?:\s*\|?\s*\"[a-z_]+\")+)\s*=>\s*Some\(MediaMergeRule::(\w+)\)"
        )
        for arm in arm_re.finditer(body):
            kind = arm.group(2).lower()
            for name in re.findall(r'\"([a-z_]+)\"', arm.group(1)):
                rules[name] = {"whitelist": "whitelist", "ceiling": "ceiling",
                               "floor": "floor", "boolor": "bool_or"}[kind]
        return rules

    def test_the_field_rule_tables_match(self, media_rs):
        rust = self._rust_rules(media_rs)
        assert rust, "no media field rules parsed from the Rust source"
        assert rust == MEDIA_GOVERNANCE_FIELD_RULES, (
            "media merge rules differ; "
            f"only-in-python={sorted(set(MEDIA_GOVERNANCE_FIELD_RULES) - set(rust))} "
            f"only-in-rust={sorted(set(rust) - set(MEDIA_GOVERNANCE_FIELD_RULES))}"
        )

    def test_speed_min_is_the_one_field_where_higher_is_stricter(self):
        assert MEDIA_GOVERNANCE_FIELD_RULES["speed_min"] == "floor"
        assert all(
            rule != "floor"
            for field, rule in MEDIA_GOVERNANCE_FIELD_RULES.items()
            if field != "speed_min"
        )


def _as_stated(value) -> str:
    """A JSON value as JSON text: `3` and `3.0`, `1` and `true` differ, as
    they do to the gateway's readers (`as_i64`, `as_bool`)."""
    return json.dumps(value, sort_keys=True)


def _rust_fn(text: str, signature: str) -> str:
    m = re.search(rf"{signature}.*?\n\}}", text, re.S)
    assert m, f"`{signature}` not found in the Rust source ({core_revision()})"
    return m.group(0)


# The gateway's one merge of inherited limits (`usage::restrictive_merge`),
# case by case: (family, base, overlay, merged, the Rust unit test that pins
# the same case on the gateway's side). `base` is the levels merged so far,
# `overlay` the next level.
LIMITS_CASES = [
    ("Llm", {"daily_limit": 100}, {"daily_limit": 50}, {"daily_limit": 50},
     "merge_numeric_takes_min_when_child_is_lower"),
    ("Llm", {"daily_limit": 50}, {"daily_limit": 100}, {"daily_limit": 50},
     "merge_numeric_takes_min_when_parent_is_lower"),
    ("Llm", {"limit": 100}, {"limit": 50}, {"limit": 50},
     "merge_numeric_preserves_integer_type"),
    ("Llm", {"hard_stop": False}, {"hard_stop": True}, {"hard_stop": True},
     "merge_bool_true_wins_overlay_true"),
    ("Llm", {"hard_stop": True}, {"hard_stop": False}, {"hard_stop": True},
     "merge_bool_true_wins_base_true"),
    ("Llm", {"existing": 10}, {"new_key": 42}, {"existing": 10, "new_key": 42},
     "merge_new_keys_from_overlay_added"),
    ("Llm", {"field": 100}, {"field": "unlimited"}, {"field": 100},
     "merge_a_childs_non_number_keeps_the_parents_number"),
    ("Llm", {"field": "unlimited"}, {"field": 50}, {"field": 50},
     "merge_mixed_types_string_to_number"),
    ("Llm", 42, 10, 42, "merge_non_object_top_level_ignored"),
    ("Llm", {"limit": 100}, {"limit": 0}, {"limit": 100},
     "merge_a_childs_zero_keeps_the_parents_limit"),
    ("Llm", {"limit": 100}, {"limit": -5}, {"limit": 100},
     "merge_a_childs_zero_keeps_the_parents_limit"),
    ("Llm", {"limit": 0}, {"limit": 40}, {"limit": 40},
     "merge_a_childs_zero_keeps_the_parents_limit"),
    ("Llm",
     {"execution": {"timeout_seconds": 30},
      "budget": {"daily_limit_usd": 10.5, "hard_stop": True},
      "tokens": {"daily_limit": 1000}},
     {"execution": {"timeout_seconds": None},
      "budget": {"daily_limit_usd": None, "hard_stop": None},
      "tokens": None},
     {"execution": {"timeout_seconds": 30},
      "budget": {"daily_limit_usd": 10.5, "hard_stop": True},
      "tokens": {"daily_limit": 1000}},
     "merge_a_childs_null_keeps_the_parents_value"),
    ("Llm",
     {"budget": {"hard_stop": True}, "tokens": {"daily_limit": 9}},
     {"budget": {"hard_stop": "no"}, "tokens": 0},
     {"budget": {"hard_stop": True}, "tokens": {"daily_limit": 9}},
     "merge_a_childs_malformed_bool_or_section_keeps_the_parents"),
    ("Llm", None, {"tokens": {"daily_limit": 7}}, {"tokens": {"daily_limit": 7}},
     "merge_into_a_null_document_adopts_the_overlay"),
    ("Llm", {"limit": 5}, {"limit": 3.0}, {"limit": 3.0},
     "merge_keeps_the_winning_value_as_stated"),
    ("Llm", {"limit": 3.5}, {"limit": 7}, {"limit": 3.5},
     "merge_keeps_the_winning_value_as_stated"),
    ("Media", {"daily_requests": 5}, {"daily_requests": 0}, {"daily_requests": 0},
     "merge_keeps_a_non_positive_limit_where_the_family_reads_it_as_one"),
    ("A2a",
     {"chain": {"max_delegation_depth": 3}, "invocations": {"daily_limit": 100}},
     {"chain": {"max_delegation_depth": 0}, "invocations": {"daily_limit": 0}},
     {"chain": {"max_delegation_depth": 0}, "invocations": {"daily_limit": 100}},
     "merge_keeps_a_non_positive_limit_where_the_family_reads_it_as_one"),
    *[
        (family, {"execution": {"daily_limit": 5}}, {"execution": {"daily_limit": 0}},
         {"execution": {"daily_limit": 5}},
         "merge_keeps_a_non_positive_limit_where_the_family_reads_it_as_one")
        for family in ("Llm", "Mcp", "Skill")
    ],
]


class TestLimitsMergeParity:
    """The Control Plane's `restrictive_merge` computes the effective and
    inherited limits the console shows; the gateway's `usage::
    restrictive_merge` (at the core's committed HEAD) computes the ones it
    enforces. They used to differ on a level's `null` or value of another
    type (the Python merge let it replace the ancestor's limit) and on a
    value that is not positive."""

    @pytest.mark.parametrize(
        "family,base,overlay,merged,rust_test",
        LIMITS_CASES,
        ids=[f"{c[0]}-{c[4]}-{i}" for i, c in enumerate(LIMITS_CASES)],
    )
    def test_python_merges_each_case_as_the_gateway_does(self, family, base, overlay, merged, rust_test):
        result = restrictive_merge(copy.deepcopy(base), overlay, LimitFamily(family))
        assert _as_stated(result) == _as_stated(merged)

    def test_every_case_is_pinned_on_the_gateways_side(self, usage_rs):
        tests = usage_rs[usage_rs.index("mod tests"):]
        for name in sorted({case[4] for case in LIMITS_CASES}):
            assert f"fn {name}()" in tests, (
                f"the gateway no longer pins `{name}` ({core_revision()}): "
                "read its restrictive_merge again and update this table"
            )

    def test_the_gateway_has_one_merge_of_inherited_limits(self, usage_rs):
        assert re.search(
            r"pub\(crate\) fn restrictive_merge\(\s*base: &mut serde_json::Value,\s*"
            r"overlay: &serde_json::Value,\s*family: LimitFamily,\s*\)",
            usage_rs,
        ), f"usage::restrictive_merge(base, overlay, family) not found ({core_revision()})"
        cache = _read(GROUP_CACHE_RS)
        assert "fn restrictive_merge_json" not in cache, "the A2A cache has its own merge again"
        assert "restrictive_merge(&mut base, &v, LimitFamily::A2a)" in cache, (
            "the A2A cache must merge with usage::restrictive_merge, family A2a"
        )

    def test_the_gateway_merges_a_field_by_the_same_rules(self, usage_rs):
        body = _rust_fn(usage_rs, r"fn merge_limit_value\(")
        for rule, text in (
            ("a level's null states nothing", "(_, Value::Null) => {}"),
            ("a null takes the other level's value", "(Value::Null, _) => *base = overlay.clone()"),
            ("bool: true wins", "Value::Bool(*b || *o)"),
            ("number: the smallest stated", "(Some(bv), Some(ov)) => ov < bv"),
            ("number: a stated value over an unstated one", "(None, Some(_)) => true"),
            ("number: finite, positive unless the family reads it", "v.is_finite() && (non_positive_is_a_limit || *v > 0.0)"),
            ("two values that are no limit's shape: the overlay's", "shape(b) == 0 && shape(o) == 0"),
            ("a limit's shape over another", "shape(o) > shape(b)"),
        ):
            assert text in body, f"the gateway's rule changed: {rule} ({core_revision()})"
        top = _rust_fn(usage_rs, r"pub\(crate\) fn restrictive_merge\(")
        assert "*base = overlay.clone();" in top, "a JSON-null document takes the overlay"
        assert "None if overlay_val.is_null() => {}" in top, "a new key's null is not added"

    def test_the_families_read_a_non_positive_value_alike(self, usage_rs):
        body = _rust_fn(usage_rs, r"fn non_positive_is_a_limit\(")
        rust: dict[str, object] = {}
        for families in re.findall(r"((?:Self::\w+\s*\|\s*)*Self::\w+)\s*=>\s*false", body):
            for family in re.findall(r"Self::(\w+)", families):
                rust[family] = ()
        for family in re.findall(r"Self::(\w+)\s*=>\s*true", body):
            rust[family] = True
        for family, key in re.findall(r'Self::(\w+)\s*=>\s*key == "(\w+)"', body):
            rust[family] = (key,)
        python = {family.value: keys for family, keys in NON_POSITIVE_IS_A_LIMIT.items()}
        assert rust == python
        variants = set(re.findall(r"^\s{4}(\w+),$", _rust_fn(usage_rs, r"pub\(crate\) enum LimitFamily"), re.M))
        assert variants == {f.value for f in LimitFamily}

    def test_each_limit_document_has_the_gateways_family(self, usage_rs):
        arms = _rust_fn(usage_rs, r"async fn collect_effective_limits\(")
        rust = dict(re.findall(r'"(\w+_global_limits)" => LimitFamily::(\w+)', arms))
        rust["llm_global_limits"] = re.search(r"_ => LimitFamily::(\w+)", arms).group(1)
        media = _rust_fn(usage_rs, r"pub async fn collect_effective_media_limits\(")
        assert "LimitFamily::Media" in media
        rust["media_global_limits"] = "Media"
        assert "LimitFamily::A2a" in _read(GROUP_CACHE_RS)
        rust["a2a_global_limits"] = "A2a"
        assert {a: f.value for a, f in LIMIT_DOCUMENT_FAMILIES.items()} == rust

    def test_media_limits_merge_mode_by_mode_from_the_group_up(self, usage_rs):
        """The gateway merges `media_global_limits[mode]`, the group's own
        first, each ancestor's into it; a mode's `null` is left out."""
        body = _rust_fn(usage_rs, r"pub async fn collect_effective_media_limits\(")
        assert ".and_then(|v| v.get(mode))" in body and ".filter(|v| !v.is_null())" in body
        # Child-first: a mode value of another shape keeps the group's own.
        effective = merge_limit_documents(
            "media_global_limits",
            [{"image_generation": 5}, {"image_generation": {"daily_requests": 2}}],
        )
        assert effective == {"image_generation": {"daily_requests": 2}}
        effective = merge_limit_documents(
            "media_global_limits",
            [{"image_generation": {"daily_requests": 2}}, {"image_generation": None, "audio_speech": {"daily_requests": 0}}],
        )
        assert effective == {"image_generation": {"daily_requests": 2}, "audio_speech": {"daily_requests": 0}}


# The gateway's `merge_skill_governance(base, parent)`: `base` is the
# group's own view (merged so far), `parent` the next ancestor's.
SKILL_CASES = [
    ({"execution_mode": "unrestricted"}, {"execution_mode": "disabled"},
     {"execution_mode": "disabled"}, "merge_skill_governance_takes_the_strictest_execution_mode"),
    ({"execution_mode": "disabled"}, {"execution_mode": "unrestricted"},
     {"execution_mode": "disabled"}, "merge_skill_governance_takes_the_strictest_execution_mode"),
    ({}, {"execution_mode": "approval_required"},
     {"execution_mode": "approval_required"}, "merge_skill_governance_adopts_a_mode_the_child_never_set"),
    ({"discovery_enabled": True}, {"discovery_enabled": False},
     {"discovery_enabled": False}, "merge_skill_governance_ands_discovery"),
    ({"discovery_enabled": False}, {"discovery_enabled": True},
     {"discovery_enabled": False}, "merge_skill_governance_ands_discovery"),
    ({}, {"discovery_enabled": False},
     {"discovery_enabled": False}, "merge_skill_governance_ands_discovery"),
    ({"max_concurrent_executions": 1}, {"max_concurrent_executions": 8},
     {"max_concurrent_executions": 1}, "merge_skill_governance_keeps_the_childs_tighter_value"),
    (None, {"execution_mode": "disabled"},
     {"execution_mode": "disabled"}, "merge_skill_governance_a_childs_null_document_inherits_the_parents"),
    ("not an object", {"execution_mode": "disabled"},
     {"execution_mode": "disabled"}, "merge_skill_governance_a_childs_null_document_inherits_the_parents"),
    ({"execution_mode": "approval_required"}, None,
     {"execution_mode": "approval_required"}, "merge_skill_governance_a_childs_null_document_inherits_the_parents"),
    *[
        ({"approval_timeout_seconds": bad, "max_concurrent_executions": bad},
         {"approval_timeout_seconds": 600, "max_concurrent_executions": 3, "max_tool_calls_per_execution": 5},
         {"approval_timeout_seconds": 600, "max_concurrent_executions": 3, "max_tool_calls_per_execution": 5},
         "merge_skill_governance_a_childs_unreadable_ceiling_keeps_the_parents")
        for bad in (0, -1, None, "x")
    ],
    ({"max_tool_calls_per_execution": 0}, {"max_tool_calls_per_execution": 5},
     {"max_tool_calls_per_execution": 0}, "merge_skill_governance_a_childs_unreadable_ceiling_keeps_the_parents"),
    ({"max_concurrent_executions": 2}, {"max_concurrent_executions": 0},
     {"max_concurrent_executions": 2}, "merge_skill_governance_a_childs_unreadable_ceiling_keeps_the_parents"),
]


class TestSkillGovernanceMergeCases:
    @pytest.mark.parametrize(
        "base,parent,merged,rust_test", SKILL_CASES,
        ids=[f"{c[3]}-{i}" for i, c in enumerate(SKILL_CASES)],
    )
    def test_python_merges_each_case_as_the_gateway_does(self, base, parent, merged, rust_test):
        result = _skill_governance_restrictive_merge(copy.deepcopy(base), parent)
        assert _as_stated(result) == _as_stated(merged)

    def test_every_case_is_pinned_on_the_gateways_side(self, skill_rs):
        for name in sorted({case[3] for case in SKILL_CASES}):
            assert f"fn {name}()" in skill_rs, f"the gateway no longer pins `{name}` ({core_revision()})"

    def test_the_ceilings_read_a_non_positive_value_alike(self, skill_rs):
        body = _rust_fn(skill_rs, r"fn merge_skill_governance\(")
        rust = [(k, v == "true") for k, v in re.findall(r'\("(\w+)",\s*(true|false)\)', body)]
        assert rust == list(SKILL_GOVERNANCE_CEILINGS)
        assert ".and_then(|v| v.as_i64())" in body, "a ceiling is a whole number"


# The gateway's `merge_media_governance(base, parent)`, `base` the group's
# own view. The cases without a Rust unit test follow its doc comment ("A
# level's `null` (a document, a mode or a field) states nothing") and its
# `stated` bound (a finite number, 0 or more).
MEDIA_CASES = [
    ({"audio_speech": {"voice_whitelist": ["alloy", "nova", "echo"], "max_input_characters": 5000,
                       "speed_max": 2.0, "speed_min": 0.5},
      "video_generation": {"require_image_input": False}},
     {"audio_speech": {"voice_whitelist": ["alloy", "nova"], "max_input_characters": 1000,
                       "speed_max": 1.5, "speed_min": 0.8},
      "video_generation": {"require_image_input": True},
      "image_generation": {"max_n": 3}},
     {"audio_speech": {"voice_whitelist": ["alloy", "nova"], "max_input_characters": 1000,
                       "speed_max": 1.5, "speed_min": 0.8},
      "video_generation": {"require_image_input": True},
      "image_generation": {"max_n": 3}},
     "merge_media_governance_applies_per_field_rules"),
    (None, {"image_generation": {"max_n": 3}}, {"image_generation": {"max_n": 3}}, None),
    ({"image_generation": {"max_n": 3}}, None, {"image_generation": {"max_n": 3}}, None),
    ({"image_generation": None}, {"image_generation": {"max_n": 3}}, {"image_generation": {"max_n": 3}}, None),
    ({"image_generation": {"max_n": None}}, {"image_generation": {"max_n": 3}}, {"image_generation": {"max_n": 3}}, None),
    ({"image_generation": {"max_n": "x"}}, {"image_generation": {"max_n": 3}}, {"image_generation": {"max_n": 3}}, None),
    ({"image_generation": {"max_n": -1}}, {"image_generation": {"max_n": 3}}, {"image_generation": {"max_n": 3}}, None),
    ({"image_generation": {"max_n": 0}}, {"image_generation": {"max_n": 3}}, {"image_generation": {"max_n": 0}}, None),
    ({"image_generation": {"max_n": 2}}, {"image_generation": {"max_n": None}}, {"image_generation": {"max_n": 2}}, None),
    ({"image_generation": {"max_n": 2}}, {"image_generation": {"max_n": True}}, {"image_generation": {"max_n": 2}}, None),
    ({"image_generation": {"max_n": 5}}, {"image_generation": {"max_n": 3.0}}, {"image_generation": {"max_n": 3.0}}, None),
    ({"image_generation": {"require_image_input": "yes"}}, {"image_generation": {"require_image_input": False}},
     {"image_generation": {"require_image_input": False}}, None),
    ({}, {"image_generation": {"max_n": None, "require_image_input": None}},
     {"image_generation": {"require_image_input": False}}, None),
    ({"embedding": {"dim_whitelist": [1, 2]}}, {"embedding": {"dim_whitelist": [1.0, 2]}},
     {"embedding": {"dim_whitelist": [2]}}, None),
    ({"audio_speech": {"future": None}}, {"audio_speech": {"future": 1}}, {"audio_speech": {"future": 1}}, None),
]


class TestMediaGovernanceMergeCases:
    @pytest.mark.parametrize(
        "base,parent,merged,rust_test", MEDIA_CASES,
        ids=[f"{c[3] or 'doc-comment'}-{i}" for i, c in enumerate(MEDIA_CASES)],
    )
    def test_python_merges_each_case_as_the_gateway_does(self, base, parent, merged, rust_test):
        result = _media_governance_restrictive_merge(copy.deepcopy(base), parent)
        assert _as_stated(result) == _as_stated(merged)

    def test_the_rules_the_cases_follow_are_the_gateways(self):
        text = _read(MEDIA_GOVERNANCE_RS)
        assert "fn merge_media_governance_applies_per_field_rules()" in text
        body = _rust_fn(text, r"pub fn merge_media_governance\(")
        assert "A level's `null` (a document, a mode or a field) states" in text
        assert ".filter(|f| f.is_finite() && *f >= 0.0)" in body, "a bound is a finite number, 0 or more"
        assert "*base = parent.clone();" in body, "a null document takes the parent's"
        assert ".and_then(|v| v.as_bool())" in body and "pv.as_bool().unwrap_or(false)" in body, (
            "a requirement is a JSON bool"
        )
        assert "is_none_or(Value::is_null)" in body, "a field with no rule: the parent's over a null"


# ---------------------------------------------------------------------------
# A value the gateway does not know
# ---------------------------------------------------------------------------
#
# The gateway reads an access, mode or effect it does not know (a typo, a
# value a newer release added, a value that is not text) as the most
# restrictive one, and the request path refuses on it. The control plane's
# merges for the console used to rank such a value 0, the most permissive:
# a parent's `default_tool_access: "disbled"` was lost under a child's
# `enabled`, so the console showed `enabled` where the gateway denied. Each
# ranking below is held to the arm of the gateway's source that reads an
# unknown value.

AUTONOMY_RS = ("services", "autonomy.rs")
AGENT_AUTHZ_RS = ("services", "agent_authz.rs")

# Not one of any table's values: a typo, a future value, and values that
# are not text at all.
UNKNOWN_VALUES = ("disbled", "audit_only", "", 1, True, ["disabled"], {"x": 1})


def _rust_wildcard_rank(body: str, arm: str) -> int:
    """The rank of the catch-all arm (`_ => 3,`, `Some(_) => 3,`) of a
    small ranking fn."""
    m = re.search(rf"\n\s*{re.escape(arm)}\s*=>\s*(\d+)", body)
    assert m, f"no `{arm} =>` arm in:\n{body}"
    return int(m.group(1))


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


class TestUnknownValuesRankAsTheGatewayReadsThem:
    def test_llm_access_unknown_ranks_as_the_gateways_catch_all(self, governance_rs):
        body = _rust_fn(governance_rs, r"fn access_strictness\(")
        rust = _rust_wildcard_rank(body, "_")
        assert rust > max(_ACCESS_STRICTNESS.values()), "the gateway ranks it above every known access"
        assert _ACCESS_STRICTNESS.unknown == rust
        for value in UNKNOWN_VALUES:
            assert _ACCESS_STRICTNESS.rank(value) == rust, value

    def test_enforce_unknown_ranks_as_the_gateways_catch_all(self, governance_rs):
        body = _rust_fn(governance_rs, r"fn enforce_strictness\(")
        rust = _rust_wildcard_rank(body, "_")
        assert rust > max(_ENFORCE_STRICTNESS.values())
        assert _ENFORCE_STRICTNESS.unknown == rust
        for value in UNKNOWN_VALUES:
            assert _ENFORCE_STRICTNESS.rank(value) == rust, value

    def test_skill_execution_mode_unknown_ranks_as_the_gateways_catch_all(self, skill_rs):
        body = _rust_fn(skill_rs, r"fn skill_execution_strictness\(")
        rust = _rust_wildcard_rank(body, "Some(_)")
        assert rust > max(_SKILL_EXECUTION_STRICTNESS.values())
        assert _SKILL_EXECUTION_STRICTNESS.unknown == rust
        for value in UNKNOWN_VALUES:
            assert _SKILL_EXECUTION_STRICTNESS.rank(value) == rust, value
        # A mode that is not text ranks as unknown too, on the gateway's side.
        stated = _rust_fn(skill_rs, r"fn stated_execution_strictness\(")
        assert _rust_wildcard_rank(stated, "Some(_)") == rust

    def test_an_unset_value_ranks_as_the_gateway_reads_absent(self, skill_rs):
        """Absent and null state nothing: rank 0, as `None => 0` reads."""
        stated = _rust_fn(skill_rs, r"fn stated_execution_strictness\(")
        assert re.search(r"None \| Some\(serde_json::Value::Null\) => 0", stated)
        for table in (_ACCESS_STRICTNESS, _ENFORCE_STRICTNESS, _SKILL_EXECUTION_STRICTNESS):
            assert table.rank(None) == 0

    def test_mcp_access_unknown_reads_as_denied(self, usage_rs):
        """An MCP `default_*_access` and a capability-filter row's access the
        gateway does not know is `CapabilityAccess::Denied` — the strictness
        of `disabled`."""
        from app.database.models import resource_group as rg_models

        table = rg_models._MCP_ACCESS_STRICTNESS
        strictness = _rust_fn(usage_rs, r"fn strictness\(&self\) -> u8")
        ranks = {
            variant: int(rank)
            for variant, rank in re.findall(r"Self::(\w+)(?: \{ \.\. \})? => (\d+)", strictness)
        }
        assert ranks == {"Allowed": 0, "ApprovalRequired": 1, "Denied": 2}
        assert dict(table) == {"enabled": ranks["Allowed"],
                               "approval_required": ranks["ApprovalRequired"],
                               "disabled": ranks["Denied"]}

        default = _rust_fn(usage_rs, r"fn mcp_default_access_from_gov\(")
        assert re.search(r"Some\(other\) => \{.*?CapabilityAccess::Denied\s*\}", default, re.S), (
            "the gateway no longer reads an unknown default access as Denied"
        )
        rows = re.search(
            r'"enabled" => 0u8,\s*"approval_required" => 1u8,(?:\s*//[^\n]*)*\s*_ => (\d+)u8',
            usage_rs,
        )
        assert rows, "the capability-filter row ranking was not found"
        assert int(rows.group(1)) == ranks["Denied"]

        assert table.unknown == ranks["Denied"]
        for value in UNKNOWN_VALUES:
            assert table.rank(value) == ranks["Denied"], value

    def test_autonomy_unknown_reads_as_suspended(self):
        from app.database.models.ai_system_contract import (
            AUTONOMY_RANK,
            autonomy_rank,
            resolve_autonomy,
        )

        text = _read(AUTONOMY_RS)
        enum = re.search(r"pub enum Autonomy \{(.*?)\}", text, re.S).group(1)
        variants = [_snake(v) for v in re.findall(r"^\s*([A-Z]\w*),", enum, re.M)]
        assert variants == sorted(AUTONOMY_RANK, key=AUTONOMY_RANK.get), (
            "the gateway's derived order (most restrictive first) is not AUTONOMY_RANK's"
        )
        resolve = _rust_fn(text, r"pub fn resolve<")
        unknown = re.search(r"unwrap_or_else\(\|\| \{.*?Autonomy::(\w+)\s*\}\)", resolve, re.S)
        assert unknown, "the gateway's reading of an unknown level was not found"
        reads_as = _snake(unknown.group(1))
        assert reads_as == "suspended"
        for value in UNKNOWN_VALUES:
            assert autonomy_rank(value) == AUTONOMY_RANK[reads_as], value
        assert resolve_autonomy("autonomous", "disbled") == reads_as
        assert resolve_autonomy("autonomous", None) == "autonomous"

    def test_grant_effect_unknown_reads_as_deny(self):
        from app.database.models.agent_identity import (
            GRANT_EFFECT_RANK,
            GRANT_EFFECTS,
            grant_effect_rank,
        )

        text = _read(AGENT_AUTHZ_RS)
        arms = re.search(r"let effect = match eff\.as_str\(\) \{(.*?)\n\s*\};", text, re.S)
        assert arms, "the gateway's reading of a grant's effect was not found"
        known = dict(re.findall(r'"(\w+)" => Decision::(\w+),', arms.group(1)))
        assert {k: _snake(v) for k, v in known.items()} == {e: e for e in GRANT_EFFECTS}
        unknown = re.search(r"\n\s*\w+ => \{.*?Decision::(\w+)\s*\}", arms.group(1), re.S)
        assert unknown, "the gateway's catch-all effect arm was not found"
        reads_as = _snake(unknown.group(1))
        assert reads_as == "deny"
        assert set(GRANT_EFFECT_RANK) == GRANT_EFFECTS
        assert GRANT_EFFECT_RANK[reads_as] == min(GRANT_EFFECT_RANK.values())
        for value in UNKNOWN_VALUES + (None,):
            assert grant_effect_rank(value) == GRANT_EFFECT_RANK[reads_as], value


class TestMergesKeepAnUnknownValue:
    """What the console shows: an ancestor's value the gateway does not
    know is not lost under a known one, and a group's own is not replaced
    by an ancestor's known one."""

    @pytest.mark.parametrize("unknown", ["disbled", 7])
    def test_llm_tool_access_and_enforce(self, unknown):
        for parent, child in (
            ({"tool_use": {"access": unknown}, "temperature": {"enforce": unknown}},
             {"tool_use": {"access": "disabled"}, "temperature": {"enforce": "reject"}}),
            ({"tool_use": {"access": "disabled"}, "temperature": {"enforce": "reject"}},
             {"tool_use": {"access": unknown}, "temperature": {"enforce": unknown}}),
        ):
            merged = copy.deepcopy(parent)
            _governance_restrictive_merge(merged, child)
            assert merged["tool_use"]["access"] == unknown
            assert merged["temperature"]["enforce"] == unknown

    @pytest.mark.parametrize("unknown", ["disbled", ["x"]])
    def test_skill_execution_mode(self, unknown):
        merged = _skill_governance_restrictive_merge({"execution_mode": "disabled"},
                                                     {"execution_mode": unknown})
        assert merged["execution_mode"] == unknown
        merged = _skill_governance_restrictive_merge({"execution_mode": unknown},
                                                     {"execution_mode": "disabled"})
        assert merged["execution_mode"] == unknown

    def test_mcp_default_access_is_denied_under_a_known_child(self):
        from app.database.models.resource_group import _mcp_governance_restrictive_merge

        merged = {"default_tool_access": "disbled"}
        _mcp_governance_restrictive_merge(merged, {"default_tool_access": "enabled"})
        assert merged["default_tool_access"] == "disbled"
        merged = {"default_tool_access": "enabled"}
        _mcp_governance_restrictive_merge(merged, {"default_tool_access": "disbled"})
        assert merged["default_tool_access"] == "disbled"

    def test_an_unhashable_value_does_not_break_a_merge(self):
        from app.database.models.resource_group import _mcp_governance_restrictive_merge

        merged = {"default_tool_access": "enabled"}
        _mcp_governance_restrictive_merge(merged, {"default_tool_access": ["disabled"]})
        assert merged["default_tool_access"] == ["disabled"]
