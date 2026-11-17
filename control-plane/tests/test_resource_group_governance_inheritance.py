# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Governance inheritance: a child group may tighten what it inherits, never
loosen it.

Limits have refused a relaxed value on PATCH since they shipped; governance
did not — it validated only the *structure* of the blob, so a department
under an organisation capped at temperature 1.0 could save 1.4. Nothing
enforced it (the runtime merge is min-wins, so 1.0 still applied), which is
precisely the problem: the console then showed a policy the platform had
quietly discarded, and the value would have become real the day the
organisation relaxed its own.

`_validate_governance_not_less_restrictive` closes that, driven by a rule
table that must mirror the `*_restrictive_merge` functions field for field.
The tests below pin both halves: every rule kind, and the deliberate
absences (union fields, and fields the merge does not restrict at all).

`_compute_capacity_info` is pinned here too. It used to look only at the
immediate parent, so an estate that sets its budget once at the
organisation reported no capacity at all for the AI systems three levels
down — the depth at which operators actually configure.
"""

import pytest

from app.api.v1.endpoints.admin.resource_groups import (
    _GOVERNANCE_RULES,
    _compute_capacity_info,
    _validate_governance_not_less_restrictive,
)

LLM = "llm_global_governance"
MCP = "mcp_global_governance"
SKILL = "skill_global_governance"


def violations(new, inherited, attr=LLM):
    return _validate_governance_not_less_restrictive(new, inherited, attr)


class TestNumericFields:
    """min-wins fields: a higher child value is looser."""

    def test_tighter_temperature_passes(self):
        assert violations({"temperature": {"max": 0.4}}, {"temperature": {"max": 1.0}}) == []

    def test_equal_temperature_passes(self):
        assert violations({"temperature": {"max": 1.0}}, {"temperature": {"max": 1.0}}) == []

    def test_looser_temperature_is_refused_and_names_both_values(self):
        found = violations({"temperature": {"max": 1.4}}, {"temperature": {"max": 1.0}})
        assert len(found) == 1
        assert "Max Temperature" in found[0]
        assert "1.4" in found[0] and "1.0" in found[0]

    def test_every_context_and_tool_ceiling_is_covered(self):
        found = violations(
            {
                "context_window": {"max_input_tokens": 8000, "max_output_tokens": 8192},
                "tool_use": {
                    "approval_timeout_seconds": 600,
                    "max_tool_calls_per_request": 50,
                    "max_tool_calls_per_hour": 500,
                    "max_tool_calls_per_day": 5000,
                },
            },
            {
                "context_window": {"max_input_tokens": 4000, "max_output_tokens": 4096},
                "tool_use": {
                    "approval_timeout_seconds": 300,
                    "max_tool_calls_per_request": 10,
                    "max_tool_calls_per_hour": 100,
                    "max_tool_calls_per_day": 1000,
                },
            },
        )
        assert len(found) == 6

    def test_a_field_the_child_leaves_unset_is_not_a_violation(self):
        assert violations({"temperature": {"enforce": "reject"}}, {"temperature": {"max": 1.0}}) == []

    def test_no_inherited_policy_means_nothing_to_violate(self):
        assert violations({"temperature": {"max": 2.0}}, None) == []
        assert violations({"temperature": {"max": 2.0}}, {}) == []


class TestEnumFields:
    """Enums compare on their own strictness ordering, not alphabetically."""

    def test_clamp_under_reject_is_refused(self):
        found = violations({"temperature": {"enforce": "clamp"}}, {"temperature": {"enforce": "reject"}})
        assert len(found) == 1
        assert "Temperature Enforce Mode" in found[0]

    def test_reject_under_clamp_is_a_tightening(self):
        assert violations({"temperature": {"enforce": "reject"}}, {"temperature": {"enforce": "clamp"}}) == []

    @pytest.mark.parametrize(
        "child,parent,refused",
        [
            ("enabled", "approval_required", True),
            ("enabled", "disabled", True),
            ("approval_required", "disabled", True),
            ("disabled", "enabled", False),
            ("approval_required", "enabled", False),
            ("approval_required", "approval_required", False),
        ],
    )
    def test_tool_access_strictness_ordering(self, child, parent, refused):
        found = violations({"tool_use": {"access": child}}, {"tool_use": {"access": parent}})
        assert bool(found) is refused

    def test_mcp_access_defaults_are_checked_on_a_flat_blob(self):
        found = violations(
            {"default_tool_access": "enabled", "approval_timeout_seconds": 900},
            {"default_tool_access": "approval_required", "approval_timeout_seconds": 300},
            MCP,
        )
        assert len(found) == 2
        assert any("Default Tool Access" in v for v in found)
        assert any("Approval Timeout" in v for v in found)

    def test_skill_execution_mode_ordering(self):
        assert violations({"execution_mode": "unrestricted"}, {"execution_mode": "disabled"}, SKILL)
        assert violations({"execution_mode": "disabled"}, {"execution_mode": "unrestricted"}, SKILL) == []


class TestBooleanAndListFields:
    def test_a_child_cannot_re_enable_discovery_a_parent_disabled(self):
        found = violations({"discovery_enabled": True}, {"discovery_enabled": False}, SKILL)
        assert len(found) == 1
        assert "Skill Discovery" in found[0]

    def test_a_child_may_disable_discovery_a_parent_allows(self):
        assert violations({"discovery_enabled": False}, {"discovery_enabled": True}, SKILL) == []

    def test_allowed_tools_may_only_narrow(self):
        assert violations(
            {"tool_use": {"allowed_tools": ["read"]}},
            {"tool_use": {"allowed_tools": ["read", "write"]}},
        ) == []

    def test_naming_a_tool_the_ancestors_do_not_allow_is_refused(self):
        found = violations(
            {"tool_use": {"allowed_tools": ["read", "delete"]}},
            {"tool_use": {"allowed_tools": ["read", "write"]}},
        )
        assert len(found) == 1
        assert "delete" in found[0] and "read" not in found[0].split(":")[1]

    def test_union_fields_can_never_be_a_relaxation(self):
        """blocked_tools, banned patterns and fragments merge by union, so a
        child that drops one of the parent's entries still gets it back."""
        assert violations(
            {"tool_use": {"blocked_tools": []}, "system_prompt": {"banned_patterns": []}},
            {
                "tool_use": {"blocked_tools": ["shell"]},
                "system_prompt": {"banned_patterns": ["ssn"], "mandatory_fragments": [{"position": "prepend", "text": "x"}]},
            },
        ) == []


class TestRuleTableMirrorsTheMerge:
    def test_the_skill_table_covers_every_field_the_merge_restricts(self):
        """The rule table and `_skill_governance_restrictive_merge` must
        list the same fields. A schema field the merge restricts but the
        table omits is a value the runtime silently discards; a field the
        table names but the merge ignores is a false alarm."""
        from app.database.models.resource_group import SKILL_GOVERNANCE_FIELDS

        skill_paths = {path for path, *_ in _GOVERNANCE_RULES[SKILL]}
        assert skill_paths == set(SKILL_GOVERNANCE_FIELDS)

    def test_the_two_execution_ceilings_are_enforced(self):
        """These were merged by the Rust proxy but not by Python, so the
        console reported an effective value the runtime did not use."""
        assert violations(
            {"max_concurrent_executions": 99}, {"max_concurrent_executions": 1}, SKILL
        )
        assert violations(
            {"max_tool_calls_per_execution": 50}, {"max_tool_calls_per_execution": 5}, SKILL
        )
        assert violations(
            {"max_concurrent_executions": 1}, {"max_concurrent_executions": 4}, SKILL
        ) == []

    def test_every_surface_with_a_write_endpoint_has_rules(self):
        assert set(_GOVERNANCE_RULES) == {LLM, MCP, SKILL}
        for rules in _GOVERNANCE_RULES.values():
            assert rules, "a surface with no rules would silently accept relaxations"

    def test_booleans_never_fall_into_the_numeric_branch(self):
        """`isinstance(True, int)` is True in Python; a bool reaching the
        min-wins branch would compare 1 > 0 and flag nonsense."""
        assert violations({"temperature": {"max": True}}, {"temperature": {"max": 1.0}}) == []


# ── Capacity: which budget this group actually draws on ─────────────────


class _FakeGroup:
    def __init__(self, gid, name, parent=None, limits=None):
        self.id = gid
        self.name = name
        self.display_name = name
        self.tenant_id = "t1"
        self.is_active = True
        self.parent_group = parent
        self.parent_group_id = parent.id if parent else None
        self.llm_global_limits = limits

    def get_ancestors(self, include_self=False):
        out, cur = [], self.parent_group
        while cur:
            out.insert(0, cur)
            cur = cur.parent_group
        if include_self:
            out.append(self)
        return out


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *criteria):
        # The production filter is parent_group_id == holder.id plus tenant
        # and is_active; the fake receives the whole estate and applies the
        # same predicate by inspecting the bind value of the first clause.
        target = criteria[0].right.value
        return _FakeQuery([r for r in self._rows if r.parent_group_id == target and r.is_active])

    def all(self):
        return self._rows


class _FakeDb:
    def __init__(self, rows):
        self._rows = rows

    def query(self, _model):
        return _FakeQuery(self._rows)


def _estate():
    """org(limits) → dept(no limits) → team(no limits) → system, plus an
    aunt department that has claimed part of the org budget."""
    org = _FakeGroup("g-org", "Nordica", limits={"budget": {"daily_limit_usd": 200.0}})
    dept = _FakeGroup("g-dept", "Corporate Functions", parent=org)
    aunt = _FakeGroup("g-aunt", "Customer Operations", parent=org,
                      limits={"budget": {"daily_limit_usd": 80.0}})
    team = _FakeGroup("g-team", "HR", parent=dept)
    system = _FakeGroup("g-sys", "CV Screening Agent", parent=team)
    return org, dept, aunt, team, system


class TestCapacityWalksToTheBudgetHolder:
    def test_a_deep_group_reports_the_organisations_budget(self):
        org, dept, aunt, team, system = _estate()
        info = _compute_capacity_info(system, "llm_global_limits", _FakeDb([dept, aunt, team, system]))
        assert info is not None, "an unset parent must not hide the ancestor's budget"
        assert info.parent_name == "Nordica"
        assert info.parent_is_immediate is False
        assert info.parent_limit == {"budget": {"daily_limit_usd": 200.0}}

    def test_the_groups_own_branch_is_not_counted_against_it(self):
        org, dept, aunt, team, system = _estate()
        info = _compute_capacity_info(system, "llm_global_limits", _FakeDb([dept, aunt, team, system]))
        names = [s.group_name for s in info.siblings]
        assert names == ["Customer Operations"], "the department on this group's own path is its branch, not a rival claim"
        assert info.sibling_total == {"budget": {"daily_limit_usd": 80.0}}
        assert info.available == {"budget": {"daily_limit_usd": 120.0}}

    def test_an_immediate_parent_with_limits_still_wins(self):
        org, dept, aunt, team, system = _estate()
        team.llm_global_limits = {"budget": {"daily_limit_usd": 50.0}}
        info = _compute_capacity_info(system, "llm_global_limits", _FakeDb([dept, aunt, team, system]))
        assert info.parent_name == "HR"
        assert info.parent_is_immediate is True

    def test_a_root_group_has_no_budget_above_it(self):
        org, *_ = _estate()
        assert _compute_capacity_info(org, "llm_global_limits", _FakeDb([])) is None

    def test_no_ancestor_sets_limits_means_no_capacity_to_report(self):
        org, dept, aunt, team, system = _estate()
        org.llm_global_limits = None
        aunt.llm_global_limits = None
        assert _compute_capacity_info(system, "llm_global_limits", _FakeDb([dept, aunt, team, system])) is None
