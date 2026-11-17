# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Validation regression tests for the LLM/MCP limits restrictive-merge +
sibling-sum helpers in `app/api/v1/endpoints/admin/resource_groups.py`.

These guards are the enforcement boundary for governance inheritance:
- PATCH that would relax the inherited floor is rejected (HTTP 400).
- PATCH where this group's value + active siblings exceeds the parent
  capacity is rejected.

Both invariants must hold for the new LLM throughput / concurrency /
spend-rate sections AND for the corrected cooldown semantics
(longer cooldown = more restrictive, not less).
"""

from app.api.v1.endpoints.admin.resource_groups import (
    _check_sibling_sum_against_parent,
    _validate_not_less_restrictive,
    _INVERTED_RESTRICTIVE_FIELDS,
    _NON_SUMMABLE_FIELDS,
)


class TestRestrictiveMerge:
    """`_validate_not_less_restrictive` — child PATCH cannot relax parent."""

    def test_child_within_parent_passes(self):
        floor = {"throughput": {"max_requests_per_minute": 100}}
        new = {"throughput": {"max_requests_per_minute": 50}}
        assert _validate_not_less_restrictive(new, floor) == []

    def test_child_above_parent_rejected(self):
        floor = {"throughput": {"max_requests_per_minute": 100}}
        new = {"throughput": {"max_requests_per_minute": 200}}
        violations = _validate_not_less_restrictive(new, floor)
        assert len(violations) == 1
        assert "max_requests_per_minute" in violations[0]
        assert "200" in violations[0]
        assert "100" in violations[0]

    def test_child_unset_is_always_ok(self):
        """Not setting a field means inheriting — never a violation."""
        floor = {"throughput": {"max_requests_per_minute": 100}}
        new = {"throughput": {}}
        assert _validate_not_less_restrictive(new, floor) == []

    def test_new_throughput_fields_recurse(self):
        """All four /min fields and burst capacities recurse through the
        nested check; setting any above the parent ceiling is rejected."""
        floor = {
            "throughput": {
                "max_requests_per_minute": 60,
                "max_input_tokens_per_minute": 100_000,
                "max_output_tokens_per_minute": 20_000,
                "max_usd_per_minute": 0.5,
            }
        }
        new = {
            "throughput": {
                "max_input_tokens_per_minute": 999_999,
                "max_output_tokens_per_minute": 30_000,
            }
        }
        violations = _validate_not_less_restrictive(new, floor)
        assert len(violations) == 2
        paths = " ".join(violations)
        assert "max_input_tokens_per_minute" in paths
        assert "max_output_tokens_per_minute" in paths

    def test_concurrency_field_recurse(self):
        floor = {"concurrency": {"max_concurrent": 8}}
        new = {"concurrency": {"max_concurrent": 16}}
        violations = _validate_not_less_restrictive(new, floor)
        assert any("max_concurrent" in v for v in violations)

    def test_cooldown_inversion_shorter_rejected(self):
        """
        Longer cooldown = MORE restrictive. A child with cooldown=30 vs
        parent cooldown=60 is LESS restrictive and must be rejected.
        Without the inversion, the old code rejected 120>60 (the wrong
        direction).
        """
        assert "cooldown_seconds" in _INVERTED_RESTRICTIVE_FIELDS
        floor = {"throughput": {"cooldown_seconds": 60}}
        new = {"throughput": {"cooldown_seconds": 30}}
        violations = _validate_not_less_restrictive(new, floor)
        assert len(violations) == 1
        assert "less restrictive" in violations[0]

    def test_cooldown_inversion_longer_passes(self):
        """A child cooldown=120 vs parent=60 is stricter — must pass."""
        floor = {"throughput": {"cooldown_seconds": 60}}
        new = {"throughput": {"cooldown_seconds": 120}}
        assert _validate_not_less_restrictive(new, floor) == []

    def test_cooldown_equal_passes(self):
        floor = {"throughput": {"cooldown_seconds": 60}}
        new = {"throughput": {"cooldown_seconds": 60}}
        assert _validate_not_less_restrictive(new, floor) == []

    def test_hard_stop_cannot_be_disabled_by_child(self):
        """hard_stop=True at parent must not be relaxed by child."""
        floor = {"budget": {"hard_stop": True}}
        new = {"budget": {"hard_stop": False}}
        violations = _validate_not_less_restrictive(new, floor)
        assert len(violations) == 1
        assert "hard_stop" in violations[0]


class TestSiblingsSum:
    """
    `_check_sibling_sum_against_parent` — the sum of this group's proposed
    value + active siblings' values for each numeric metric must not exceed
    parent.
    """

    def test_passes_when_sum_under_parent(self):
        new = {"throughput": {"max_requests_per_minute": 30}}
        siblings = [
            {"throughput": {"max_requests_per_minute": 30}},
            {"throughput": {"max_requests_per_minute": 20}},
        ]
        parent = {"throughput": {"max_requests_per_minute": 100}}
        assert _check_sibling_sum_against_parent(new, siblings, parent) == []

    def test_rejects_when_sum_exceeds_parent(self):
        new = {"throughput": {"max_requests_per_minute": 60}}
        siblings = [
            {"throughput": {"max_requests_per_minute": 30}},
            {"throughput": {"max_requests_per_minute": 30}},
        ]
        parent = {"throughput": {"max_requests_per_minute": 100}}
        violations = _check_sibling_sum_against_parent(new, siblings, parent)
        assert len(violations) == 1
        assert "120" in violations[0]
        assert "100" in violations[0]

    def test_itpm_and_otpm_summed_independently(self):
        """
        Input-token and output-token rates are independent allocations;
        each is summed against its parent separately.
        """
        new = {
            "throughput": {
                "max_input_tokens_per_minute": 60_000,
                "max_output_tokens_per_minute": 5_000,
            }
        }
        siblings = [
            {
                "throughput": {
                    "max_input_tokens_per_minute": 60_000,
                    "max_output_tokens_per_minute": 5_000,
                }
            }
        ]
        parent = {
            "throughput": {
                "max_input_tokens_per_minute": 100_000,
                "max_output_tokens_per_minute": 20_000,
            }
        }
        violations = _check_sibling_sum_against_parent(new, siblings, parent)
        # ITPM sums to 120k > 100k → violation.
        # OTPM sums to 10k ≤ 20k → no violation.
        assert len(violations) == 1
        assert "max_input_tokens_per_minute" in violations[0]

    def test_concurrency_summed_against_parent(self):
        """Per-child concurrency caps must not exceed the parent's pool."""
        new = {"concurrency": {"max_concurrent": 5}}
        siblings = [{"concurrency": {"max_concurrent": 4}}]
        parent = {"concurrency": {"max_concurrent": 8}}
        violations = _check_sibling_sum_against_parent(new, siblings, parent)
        assert len(violations) == 1
        assert "max_concurrent" in violations[0]

    def test_cooldown_not_summed(self):
        """
        Cooldowns are wait times, not allocations. Each child owns its
        own cooldown; summing siblings' cooldowns against parent is
        nonsense. Verify cooldown_seconds is in the skip set.
        """
        assert "cooldown_seconds" in _NON_SUMMABLE_FIELDS
        new = {"throughput": {"cooldown_seconds": 60}}
        siblings = [{"throughput": {"cooldown_seconds": 60}}]
        parent = {"throughput": {"cooldown_seconds": 60}}
        # 60 + 60 = 120, but cooldown isn't summed → no violation.
        assert _check_sibling_sum_against_parent(new, siblings, parent) == []

    def test_warning_percent_not_summed(self):
        """Same skip-sum logic for warning %."""
        new = {"budget": {"daily_warning_percent": 80}}
        siblings = [{"budget": {"daily_warning_percent": 80}}]
        parent = {"budget": {"daily_warning_percent": 90}}
        assert _check_sibling_sum_against_parent(new, siblings, parent) == []

    def test_burst_capacity_is_summed(self):
        """
        Burst capacity IS an allocation: parent allows N burst tokens
        across the whole token bucket, children carve up that pool.
        Summing siblings should fire.
        """
        new = {"throughput": {"request_burst": 8}}
        siblings = [{"throughput": {"request_burst": 8}}]
        parent = {"throughput": {"request_burst": 10}}
        violations = _check_sibling_sum_against_parent(new, siblings, parent)
        assert len(violations) == 1
        assert "request_burst" in violations[0]

    def test_missing_sibling_limits_treated_as_zero(self):
        """A sibling with no limits set contributes 0 to the sum."""
        new = {"throughput": {"max_requests_per_minute": 90}}
        siblings = [None, {}, {"throughput": None}]
        parent = {"throughput": {"max_requests_per_minute": 100}}
        assert _check_sibling_sum_against_parent(new, siblings, parent) == []
