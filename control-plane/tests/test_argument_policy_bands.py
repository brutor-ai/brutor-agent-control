# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Banded argument policies — control-plane half.

A band is "allow below the threshold, hold for a human above it, refuse far
above it", expressed as ordered rules with per-rule severity. Two things have
to be true for that to be safe rather than decorative:

1. The third severity exists end to end — Pydantic, the ORM tuple, and the
   CHECK constraint widened in a175_arg_policy_approval must agree, or a policy an operator can
   save is a policy the database rejects.

2. **A comparison rule cannot be bypassed by omitting the field it
   compares.** This is the sharp edge. A predicate reading a missing field
   does not hold, and a rule that does not hold does not deny, so
   `transfer_funds({})` with no `amount` walks straight past a
   `field_gt: amount` threshold. The policy reads as enforced on the page
   and is not — strictly worse than no policy at all.

(2) gets three independent layers because it fails silently. This module is the first: the validator refusal. The form seeding
is layer two and the live 403 assertion is layer three.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.v1.models.admin.argument_policy import (
    PREDICATES_BY_ANALYZER,
    PREDICATES_REQUIRING_FIELD,
    PREDICATES_REQUIRING_LIST_VALUES,
    PREDICATES_REQUIRING_NUMERIC_VALUE,
    ArgumentPolicyCreateRequest,
    ArgumentPolicyRule,
)
from app.database.models.argument_policy import (
    ALLOWED_SEVERITIES,
    ANALYZER_JSON,
    SEVERITY_APPROVAL_REQUIRED,
)

BASE = dict(
    name="payments-threshold",
    surface="mcp_input",
    target="transfer_funds",
    argument_key="*",
    analyzer="json",
)

PRESENCE = {"deny_if": "schema_invalid"}
REQUIRED_AMOUNT = {"schema": {"required": ["amount"]}}


def policy(**over):
    return ArgumentPolicyCreateRequest(**{**BASE, **over})


class TestTheThirdSeverity:
    def test_the_vocabularies_agree(self):
        assert SEVERITY_APPROVAL_REQUIRED in ALLOWED_SEVERITIES
        assert set(ALLOWED_SEVERITIES) == {"deny", "approval_required", "warn"}

    def test_a_policy_may_be_approval_required(self):
        """The gateway reads a policy's own severity as it is stored: a
        tripped rule without a severity of its own holds the call. The
        presence guard still has to deny, so it says so itself."""
        p = policy(severity="approval_required", rules=[{**PRESENCE, "severity": "deny"}])
        assert p.severity == "approval_required"
        assert policy(severity="approval_required", rules=[]).severity == "approval_required"

    def test_a_rule_may_override_the_policy_severity(self):
        p = policy(
            severity="warn",
            analyzer_config=REQUIRED_AMOUNT,
            rules=[
                # Under a `warn` policy the guard has to say `deny` itself:
                # inheriting would make it a logged warning.
                {**PRESENCE, "severity": "deny"},
                {"deny_if": "field_gt", "field": "amount", "value": 10000, "severity": "deny"},
                {
                    "deny_if": "field_gt",
                    "field": "amount",
                    "value": 1000,
                    "severity": "approval_required",
                },
            ],
        )
        assert [r.severity for r in p.rules] == ["deny", "deny", "approval_required"]

    def test_an_unknown_severity_is_refused(self):
        with pytest.raises(ValidationError):
            policy(severity="maybe", rules=[PRESENCE])


class TestComparisonRuleShape:
    @pytest.mark.parametrize("predicate", sorted(PREDICATES_REQUIRING_FIELD))
    def test_every_comparison_predicate_requires_a_field(self, predicate):
        with pytest.raises(ValidationError, match="requires a 'field' path"):
            ArgumentPolicyRule(deny_if=predicate, value=1, values=[1])

    @pytest.mark.parametrize("predicate", sorted(PREDICATES_REQUIRING_NUMERIC_VALUE))
    def test_ordering_predicates_require_a_number(self, predicate):
        with pytest.raises(ValidationError, match="requires a numeric 'value'"):
            ArgumentPolicyRule(deny_if=predicate, field="amount", value="8000")

    def test_a_boolean_is_not_a_number(self):
        """`isinstance(True, int)` is True in Python; a boolean threshold is
        a mistake, not a comparison."""
        with pytest.raises(ValidationError, match="requires a numeric 'value'"):
            ArgumentPolicyRule(deny_if="field_gt", field="amount", value=True)

    @pytest.mark.parametrize("predicate", sorted(PREDICATES_REQUIRING_LIST_VALUES))
    def test_membership_predicates_require_a_list(self, predicate):
        with pytest.raises(ValidationError, match="requires a 'values' list"):
            ArgumentPolicyRule(deny_if=predicate, field="currency")
        ArgumentPolicyRule(deny_if=predicate, field="currency", values=["GBP"])

    def test_equality_predicates_take_a_scalar(self):
        r = ArgumentPolicyRule(deny_if="field_eq", field="currency", value="GBP")
        assert r.value == "GBP"
        with pytest.raises(ValidationError, match="requires a 'value'"):
            ArgumentPolicyRule(deny_if="field_eq", field="currency")

    def test_the_eight_are_registered_for_the_json_analyzer(self):
        assert PREDICATES_REQUIRING_FIELD <= PREDICATES_BY_ANALYZER[ANALYZER_JSON]
        assert len(PREDICATES_REQUIRING_FIELD) == 8, "the set is closed at eight"

    def test_a_comparison_predicate_is_refused_on_another_analyzer(self):
        with pytest.raises(ValidationError, match="not valid for analyzer"):
            ArgumentPolicyCreateRequest(
                **{**BASE, "analyzer": "sql"},
                rules=[{"deny_if": "field_gt", "field": "amount", "value": 1}],
            )


class TestTheOmitTheFieldBypass:
    """The layer-one refusal. Each of these policies would look enforced."""

    def test_a_bare_threshold_is_refused(self):
        with pytest.raises(ValidationError, match="bypassed by omitting"):
            policy(rules=[{"deny_if": "field_gt", "field": "amount", "value": 1000}])

    def test_the_message_names_the_unguarded_path(self):
        with pytest.raises(ValidationError, match="amount"):
            policy(rules=[{"deny_if": "field_gt", "field": "amount", "value": 1000}])

    def test_requiring_the_field_without_denying_on_it_is_not_enough(self):
        """`schema.required` only populates an error; something has to deny
        on that error for the requirement to bite."""
        with pytest.raises(ValidationError, match="bypassed by omitting"):
            policy(
                analyzer_config=REQUIRED_AMOUNT,
                rules=[{"deny_if": "field_gt", "field": "amount", "value": 1000}],
            )

    def test_schema_required_plus_schema_invalid_guards_it(self):
        policy(
            analyzer_config=REQUIRED_AMOUNT,
            rules=[PRESENCE, {"deny_if": "field_gt", "field": "amount", "value": 1000}],
        )

    def test_an_explicit_presence_rule_guards_it(self):
        policy(
            rules=[
                {"allow_only_if": "field_present", "values": ["amount"]},
                {"deny_if": "field_gt", "field": "amount", "value": 1000},
            ]
        )

    def test_the_negated_phrasing_guards_it_too(self):
        policy(
            rules=[
                {"deny_if": "field_not_present", "values": ["amount"]},
                {"deny_if": "field_gt", "field": "amount", "value": 1000},
            ]
        )

    def test_guarding_one_path_does_not_guard_another(self):
        with pytest.raises(ValidationError, match="currency"):
            policy(
                analyzer_config=REQUIRED_AMOUNT,
                rules=[
                    PRESENCE,
                    {"deny_if": "field_gt", "field": "amount", "value": 1000},
                    {"deny_if": "field_eq", "field": "currency", "value": "XXX"},
                ],
            )

    def test_a_nested_path_needs_its_own_guard(self):
        """Requiring `transfer` says nothing about `transfer.amount`."""
        with pytest.raises(ValidationError, match="transfer.amount"):
            policy(
                analyzer_config={"schema": {"required": ["transfer"]}},
                rules=[
                    PRESENCE,
                    {"deny_if": "field_gt", "field": "transfer.amount", "value": 1000},
                ],
            )

    def test_a_policy_with_no_comparison_rules_is_unaffected(self):
        policy(rules=[PRESENCE, {"deny_if": "field_not_present", "values": ["x"]}])

    def test_existing_policies_still_validate(self):
        """Nothing about the old shapes changed."""
        ArgumentPolicyCreateRequest(
            **{**BASE, "analyzer": "sql"},
            rules=[{"deny_if": "writes"}, {"deny_if": "touches_any", "values": ["ssn"]}],
        )


class TestTheGuardMustDeny:
    """The gateway acts on `rule.severity.unwrap_or(policy.severity)`
    (`evaluator.rs`); a `warn` is logged and the call goes through, an
    `approval_required` is held rather than refused. A guard of either kind
    leaves the omitted field open, so it does not count."""

    THRESHOLD = {"deny_if": "field_gt", "field": "amount", "value": 1000}
    PRESENT = {"allow_only_if": "field_present", "values": ["amount"]}

    @pytest.mark.parametrize("severity", ["warn", "approval_required"])
    @pytest.mark.parametrize(
        "guard",
        [
            {"allow_only_if": "field_present", "values": ["amount"]},
            {"deny_if": "field_not_present", "values": ["amount"]},
            {"deny_if": "schema_invalid"},
        ],
        ids=["field_present", "field_not_present", "schema_invalid"],
    )
    def test_a_guard_with_its_own_weaker_severity_is_refused(self, guard, severity):
        with pytest.raises(ValidationError, match="bypassed by omitting.*does not deny"):
            policy(
                analyzer_config=REQUIRED_AMOUNT,
                rules=[{**guard, "severity": severity}, self.THRESHOLD],
            )

    @pytest.mark.parametrize("severity", ["warn", "approval_required"])
    def test_a_guard_inheriting_a_weaker_policy_severity_is_refused(self, severity):
        with pytest.raises(ValidationError, match="does not deny"):
            policy(severity=severity, rules=[self.PRESENT, {**self.THRESHOLD, "severity": "deny"}])

    def test_an_explicit_null_inherits_like_an_absent_severity(self):
        with pytest.raises(ValidationError, match="does not deny"):
            policy(severity="warn", rules=[{**self.PRESENT, "severity": None}, self.THRESHOLD])

    def test_a_guard_that_says_deny_holds_under_a_weaker_policy(self):
        policy(severity="warn", rules=[{**self.PRESENT, "severity": "deny"}, self.THRESHOLD])

    def test_a_guard_inheriting_deny_holds(self):
        policy(severity="deny", rules=[self.PRESENT, self.THRESHOLD])

    def test_a_denying_guard_beside_a_warning_one_is_enough(self):
        policy(
            rules=[
                {**self.PRESENT, "severity": "warn"},
                {"deny_if": "field_not_present", "values": ["amount"], "severity": "deny"},
                self.THRESHOLD,
            ]
        )

    def test_a_warning_schema_rule_does_not_lend_its_severity_to_the_requirement(self):
        """Two `schema_invalid` rules: only a denying one makes
        `schema.required` bite."""
        with pytest.raises(ValidationError, match="does not deny"):
            policy(
                severity="warn",
                analyzer_config=REQUIRED_AMOUNT,
                rules=[PRESENCE, {**PRESENCE, "severity": "approval_required"}, self.THRESHOLD],
            )

    def test_the_stored_dict_shape_is_judged_the_same_way(self):
        """Policy-as-code and a PATCH hand the guard stored dicts."""
        from app.api.v1.models.admin.argument_policy import (
            validate_comparison_rules_have_a_presence_guard as guard,
        )

        guard([self.PRESENT, self.THRESHOLD], None, "*", "deny")
        with pytest.raises(ValueError, match="does not deny"):
            guard([self.PRESENT, self.THRESHOLD], None, "*", "warn")
        with pytest.raises(ValueError, match="bypassed by omitting"):
            guard([self.THRESHOLD], None, "*", "deny")


class TestAPresenceRuleNamingSeveralPaths:
    """The gateway reads the list as ANY (`targets.iter().any` in
    `evaluator.rs`): `allow_only_if: field_present [amount, currency]` holds
    when either is there, and `deny_if: field_not_present` on the same list
    fires only when both are missing. Neither refuses a call that omits just
    one of them, so such a rule guards no single path."""

    THRESHOLD = {"deny_if": "field_gt", "field": "amount", "value": 1000}

    @pytest.mark.parametrize(
        "guard",
        [
            {"allow_only_if": "field_present", "values": ["amount", "currency"]},
            {"deny_if": "field_not_present", "values": ["amount", "currency"]},
        ],
        ids=["field_present", "field_not_present"],
    )
    def test_it_does_not_guard_any_one_of_them(self, guard):
        with pytest.raises(ValidationError, match="bypassed by omitting.*amount"):
            policy(rules=[guard, self.THRESHOLD])

    def test_one_rule_per_path_guards_both(self):
        policy(
            rules=[
                {"allow_only_if": "field_present", "values": ["amount"]},
                {"allow_only_if": "field_present", "values": ["currency"]},
                self.THRESHOLD,
                {"deny_if": "field_eq", "field": "currency", "value": "XXX"},
            ]
        )

    def test_the_same_path_twice_is_still_one_path(self):
        policy(rules=[{"allow_only_if": "field_present", "values": ["amount", "amount"]}, self.THRESHOLD])


class TestTheWholeObjectConvention:
    """Comparison rules address dotted paths INSIDE the argument object, so
    the policy has to analyze the object rather than one extracted member.
    `argument_key: "*"` says so, mirroring `target: "*"`."""

    def test_a_named_key_is_refused_for_a_banded_policy(self):
        with pytest.raises(ValidationError, match="argument_key must be"):
            ArgumentPolicyCreateRequest(
                **{**BASE, "argument_key": "amount"},
                analyzer_config=REQUIRED_AMOUNT,
                rules=[PRESENCE, {"deny_if": "field_gt", "field": "amount", "value": 1000}],
            )

    def test_a_named_key_is_still_fine_without_comparison_rules(self):
        """SQL, URL, shell and path analyzers all want one named argument."""
        ArgumentPolicyCreateRequest(
            **{**BASE, "analyzer": "sql", "argument_key": "query"},
            rules=[{"deny_if": "writes"}],
        )


class TestTheCanonicalBand:
    """The demo the RFC exists to make expressible."""

    def test_the_transfer_threshold_policy_validates(self):
        p = policy(
            severity="warn",
            analyzer_config=REQUIRED_AMOUNT,
            rules=[
                {
                    "deny_if": "schema_invalid",
                    "severity": "deny",
                    "message": "transfer_funds called without an amount",
                },
                {
                    "deny_if": "field_gt",
                    "field": "amount",
                    "value": 10000,
                    "severity": "deny",
                    "message": "Transfers over £10,000 are not permitted from this system",
                },
                {
                    "deny_if": "field_gt",
                    "field": "amount",
                    "value": 1000,
                    "severity": "approval_required",
                    "message": "Transfers over £1,000 require approval",
                },
            ],
        )
        assert len(p.rules) == 3
        # Ordering IS the band: the tighter deny must precede the looser
        # approval, or the deny is unreachable.
        thresholds = [r.value for r in p.rules if r.value is not None]
        assert thresholds == [10000, 1000], "tighter band first"
