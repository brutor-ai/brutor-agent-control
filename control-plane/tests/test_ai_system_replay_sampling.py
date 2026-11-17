# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Replay suite sampling, scoring and response policies

The property this file exists to defend is one sentence: **a suite built
from real traffic must contain the cases that break, not the cases that
are common.**

Production traffic is overwhelmingly happy path. A proportional sample of
thirty runs from a population that is 92% `completed` yields twenty-seven
happy-path cases, and the resulting suite passes at 100% right up until it
matters. Everything below is about making that specific failure impossible.

The scoring half matters for the same reason from the other end: a pass
rate that counts skipped cases as passes lets a suite where every case was
skipped report 100%, and a gate keyed on that is decoration.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest

from app.database.models.ai_system_replay import (
    AISystemReplayRun,
    AISystemReplaySuite,
    CRITICALITY_CRITICAL,
    DEFAULT_SIDE_EFFECT_POLICY,
    PASSING_VERDICTS,
    REPLAY_COMPLETED,
    REPLAY_FAILED,
    SIDE_EFFECT_MOCK,
    VERDICT_FAILED,
    VERDICT_MATCHED,
    VERDICT_OUTCOME_CHANGED,
    VERDICT_POLICY_CHANGED,
    VERDICT_SHAPE_CHANGED,
    VERDICT_SKIPPED,
    summarize,
)
from app.database.models.ai_system_response_policy import (
    ACTION_NOTIFY,
    ACTION_SET_AUTONOMY,
    AISystemResponsePolicy,
    DEFAULT_RESPONSE_COOLDOWN_SECONDS,
    PolicyValidationError,
    SEVERITY_RANK,
    SIGNAL_DRIFT,
    SIGNAL_LIVENESS,
    actuates,
    validate_actions,
)
from app.services.ai_system_replay_service import (
    MAX_TOOL_STRATA,
    RunRecord,
    build_strata,
    coverage,
    coverage_gaps,
    select_cases,
    action_deciles,
)


def happy_path(n: int = 200) -> list[RunRecord]:
    """The population shape that defeats naive sampling."""
    return [
        RunRecord(f"r{i}", f"t{i}", "completed", 4 + (i % 4), "search|fetch")
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# The property the whole feature rests on
# ---------------------------------------------------------------------------


class TestRareCasesSurvive:
    def test_a_single_exhausted_run_among_two_hundred_is_sampled(self):
        # `exhausted` is the canonical "it is looping and nobody noticed"
        # outcome. A suite that never contains one cannot detect a change
        # that causes looping.
        runs = happy_path() + [
            RunRecord("looper", "tx", "exhausted", 48, "search|fetch|escalate")
        ]
        picked = {c.run.run_id for c in select_cases(runs, 10)}
        assert "looper" in picked

    def test_every_terminal_state_present_is_represented(self):
        runs = happy_path(100)
        for i, state in enumerate(["errored", "exhausted", "blocked_policy", "cancelled"]):
            runs.append(RunRecord(f"rare{i}", f"tx{i}", state, 9 + i, "search"))

        cases = select_cases(runs, 12)
        states = {c.run.terminal_state for c in cases}
        for state in ("errored", "exhausted", "blocked_policy", "cancelled"):
            assert state in states, f"{state} missing — the suite cannot detect breaking it"

    def test_a_rare_tool_path_is_sampled(self):
        # The tool the finance team uses twice a week is exactly the one a
        # permission change breaks.
        runs = happy_path() + [
            RunRecord("refund", "tf", "completed", 6, "search|transfer_funds")
        ]
        picked = {c.run.run_id for c in select_cases(runs, 8)}
        assert "refund" in picked

    def test_the_long_tail_of_step_counts_is_sampled(self):
        # Runs brushing the iteration cap live in the top decile.
        runs = [
            RunRecord(f"r{i}", f"t{i}", "completed", 3, "search") for i in range(150)
        ]
        runs.append(RunRecord("long", "tl", "completed", 90, "search"))
        picked = {c.run.run_id for c in select_cases(runs, 10)}
        assert "long" in picked

    def test_a_proportional_sample_would_have_missed_it(self):
        # Documents the alternative rather than only asserting ours works:
        # 1 rare run in 200 has a ~5% chance of appearing in a proportional
        # sample of 10.
        runs = happy_path() + [RunRecord("looper", "tx", "exhausted", 48, "s")]
        assert len(runs) == 201
        proportional_share = 10 / len(runs)
        assert proportional_share < 0.06
        assert "looper" in {c.run.run_id for c in select_cases(runs, 10)}


# ---------------------------------------------------------------------------
# Sampling mechanics
# ---------------------------------------------------------------------------


class TestSelection:
    def test_the_budget_is_respected(self):
        assert len(select_cases(happy_path(), 7)) == 7

    def test_a_small_population_yields_what_it_has(self):
        runs = happy_path(3)
        assert len(select_cases(runs, 30)) == 3

    def test_no_run_is_selected_twice(self):
        # A run belongs to three strata; picking it once per stratum would
        # silently shrink an effective suite to a third of its size.
        cases = select_cases(happy_path(50), 20)
        ids = [c.run.run_id for c in cases]
        assert len(ids) == len(set(ids))

    def test_an_empty_population_is_not_an_error(self):
        assert select_cases([], 10) == []

    def test_a_zero_budget_selects_nothing(self):
        assert select_cases(happy_path(), 0) == []

    def test_selection_is_deterministic(self):
        # A suite is evidence. An operator who rebuilds one and gets a
        # different set cannot compare the two runs against each other.
        runs = happy_path(80)
        first = [(c.run.run_id, c.stratum) for c in select_cases(runs, 15)]
        second = [(c.run.run_id, c.stratum) for c in select_cases(runs, 15)]
        assert first == second

    def test_every_case_records_the_stratum_it_was_picked_for(self):
        # Without this, "30 cases" and "30 variations on the same happy
        # path" are indistinguishable.
        for case in select_cases(happy_path(40), 10):
            assert case.stratum
            assert ":" in case.stratum


class TestStrata:
    def test_a_run_belongs_to_terminal_step_and_tool_strata(self):
        strata = build_strata([RunRecord("r1", "t1", "completed", 5, "search")])
        kinds = {k.split(":", 1)[0] for k in strata}
        assert kinds == {"terminal", "actions", "tools"}

    def test_tool_strata_are_capped(self):
        # Unbounded cardinality would let one dimension crowd out the others.
        runs = [
            RunRecord(f"r{i}", f"t{i}", "completed", 5, f"tool_{i}") for i in range(60)
        ]
        tool_strata = [k for k in build_strata(runs) if k.startswith("tools:")]
        assert len(tool_strata) <= MAX_TOOL_STRATA

    def test_a_missing_terminal_state_does_not_crash_sampling(self):
        runs = [RunRecord("r1", "t1", None, None, None)]
        cases = select_cases(runs, 5)
        assert len(cases) == 1

    def test_deciles_are_nearest_rank(self):
        # An interpolated boundary can land on a step count no run had,
        # which makes the bucket label unexplainable.
        bounds = action_deciles([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        assert all(b in range(1, 11) for b in bounds)

    def test_deciles_of_an_empty_population(self):
        assert action_deciles([]) == []


class TestCoverageReporting:
    def test_coverage_counts_cases_per_stratum(self):
        cases = select_cases(happy_path(60), 12)
        assert sum(coverage(cases).values()) == len(cases)

    def test_gaps_name_what_the_sample_missed(self):
        # A gate is only as good as what its evidence covers; "30 cases,
        # none of them the exhausted path" is a fact an operator needs.
        runs = happy_path(100)
        runs.append(RunRecord("looper", "tx", "exhausted", 48, "escalate"))
        gaps = coverage_gaps(runs, select_cases(runs, 2))
        assert gaps, "a two-case sample of a rich population must report gaps"

    def test_a_full_sample_has_no_gaps(self):
        runs = happy_path(6)
        assert coverage_gaps(runs, select_cases(runs, 50)) == []


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


class TestSummarize:
    def test_matched_and_shape_changed_both_pass(self):
        # A system that reached the same outcome by a different route has
        # not regressed. Failing it would make every prompt tweak look like
        # a breakage and teach operators to ignore the gate.
        assert PASSING_VERDICTS == {VERDICT_MATCHED, VERDICT_SHAPE_CHANGED}
        s = summarize([VERDICT_MATCHED, VERDICT_SHAPE_CHANGED])
        assert s["pass_rate"] == 1.0

    def test_outcome_and_policy_changes_fail(self):
        s = summarize([VERDICT_MATCHED, VERDICT_OUTCOME_CHANGED, VERDICT_POLICY_CHANGED])
        assert s["pass_rate"] == pytest.approx(1 / 3)

    def test_skipped_cases_are_excluded_from_the_rate(self):
        # Counting them as passes lets an all-skipped suite report 100%;
        # counting them as failures punishes an operator for marking an
        # unsafe case unsafe.
        s = summarize([VERDICT_MATCHED, VERDICT_SKIPPED])
        assert s["pass_rate"] == 1.0
        assert s["skipped"] == 1
        assert s["total_cases"] == 2

    def test_an_entirely_skipped_suite_has_no_rate_rather_than_a_perfect_one(self):
        s = summarize([VERDICT_SKIPPED, VERDICT_SKIPPED])
        assert s["pass_rate"] is None, (
            "a confident 100% derived from nothing is exactly what the "
            "claim-discipline rule exists to prevent"
        )

    def test_a_failed_replay_is_not_a_pass(self):
        s = summarize([VERDICT_MATCHED, VERDICT_FAILED])
        assert s["pass_rate"] == 0.5

    def test_critical_outcome_changes_are_counted_separately(self):
        s = summarize(
            [VERDICT_OUTCOME_CHANGED, VERDICT_OUTCOME_CHANGED],
            [True, False],
        )
        assert s["critical_outcome_changed"] == 1
        assert s["outcome_changed"] == 2

    def test_a_shape_change_on_a_critical_case_is_not_a_critical_failure(self):
        s = summarize([VERDICT_SHAPE_CHANGED], [True])
        assert s["critical_outcome_changed"] == 0


class TestGateArithmetic:
    def _run(self, **kwargs) -> AISystemReplayRun:
        base = dict(
            id="rr-1", tenant_id="t1", suite_id="s1", ai_system_id="sys",
            status=REPLAY_COMPLETED, pass_rate=1.0, critical_outcome_changed=0,
        )
        base.update(kwargs)
        return AISystemReplayRun(**base)

    def test_a_run_above_threshold_passes(self):
        assert self._run(pass_rate=0.95).passes(0.9)

    def test_a_run_below_threshold_fails(self):
        assert not self._run(pass_rate=0.85).passes(0.9)

    def test_one_broken_critical_case_fails_a_high_pass_rate(self):
        # A 200-case suite at 95% has ten failures. If one of them is the
        # refund path, the arithmetic must not wave it through.
        assert not self._run(pass_rate=0.95, critical_outcome_changed=1).passes(0.9)

    def test_an_unfinished_run_never_passes(self):
        assert not self._run(status="running", pass_rate=1.0).passes(0.9)

    def test_a_failed_run_never_passes(self):
        assert not self._run(status=REPLAY_FAILED, pass_rate=1.0).passes(0.9)

    def test_a_null_pass_rate_never_passes(self):
        assert not self._run(pass_rate=None).passes(0.0)


class TestSuiteDefaults:
    def test_side_effects_are_mocked_by_default(self):
        # A replay that actually calls transfer_funds because someone forgot
        # a flag is the worst thing this feature could do.
        assert DEFAULT_SIDE_EFFECT_POLICY == SIDE_EFFECT_MOCK

    def test_an_unreplayable_suite_is_not_usable(self):
        suite = AISystemReplaySuite(
            id="s1", tenant_id="t1", ai_system_id="sys", name="n",
            enabled=True, replayable=False,
        )
        assert not suite.is_usable

    def test_a_disabled_suite_is_not_usable(self):
        suite = AISystemReplaySuite(
            id="s1", tenant_id="t1", ai_system_id="sys", name="n",
            enabled=False, replayable=True,
        )
        assert not suite.is_usable

    def test_a_threshold_outside_zero_to_one_is_refused(self):
        with pytest.raises(ValueError):
            AISystemReplaySuite(
                id="s1", tenant_id="t1", ai_system_id="sys", name="n",
                pass_threshold=1.5,
            )


# ---------------------------------------------------------------------------
# Response policies — the closed loop
# ---------------------------------------------------------------------------


class TestActionValidation:
    def test_an_unknown_action_is_refused_not_dropped(self):
        # Silently dropping it would arm a policy that does less than its
        # author believes — the worst failure for a control somebody reaches
        # for during an incident.
        with pytest.raises(PolicyValidationError):
            validate_actions([{"kind": "delete_everything"}], allow_suspend=False)

    def test_suspend_requires_explicit_opt_in(self):
        actions = [{"kind": ACTION_SET_AUTONOMY, "level": "suspended"}]
        with pytest.raises(PolicyValidationError) as exc:
            validate_actions(actions, allow_suspend=False)
        assert "allow_suspend" in str(exc.value)
        assert validate_actions(actions, allow_suspend=True)

    def test_other_autonomy_levels_need_no_opt_in(self):
        assert validate_actions(
            [{"kind": ACTION_SET_AUTONOMY, "level": "approval_required"}],
            allow_suspend=False,
        )

    def test_an_unknown_autonomy_level_is_refused(self):
        with pytest.raises(PolicyValidationError):
            validate_actions(
                [{"kind": ACTION_SET_AUTONOMY, "level": "mostly_free"}],
                allow_suspend=True,
            )

    def test_notify_needs_somewhere_to_send(self):
        with pytest.raises(PolicyValidationError):
            validate_actions([{"kind": ACTION_NOTIFY, "targets": []}], allow_suspend=False)

    def test_an_empty_action_list_is_refused(self):
        with pytest.raises(PolicyValidationError):
            validate_actions([], allow_suspend=False)

    def test_notify_only_policies_do_not_actuate(self):
        assert not actuates([{"kind": ACTION_NOTIFY, "targets": ["ops"]}])

    def test_autonomy_changes_actuate(self):
        assert actuates([{"kind": ACTION_SET_AUTONOMY, "level": "restricted"}])


class TestPolicyMatching:
    def _policy(self, **kwargs) -> AISystemResponsePolicy:
        base = dict(
            id="p1", tenant_id="t1", ai_system_id="sys", name="p",
            enabled=True, signal_kind=SIGNAL_DRIFT, severity_at_least="high",
            actions=[{"kind": ACTION_NOTIFY, "targets": ["ops"]}],
        )
        base.update(kwargs)
        return AISystemResponsePolicy(**base)

    def test_severity_at_or_above_the_floor_matches(self):
        p = self._policy()
        assert p.matches(signal_kind=SIGNAL_DRIFT, severity="high")
        assert p.matches(signal_kind=SIGNAL_DRIFT, severity="critical")

    def test_severity_below_the_floor_does_not(self):
        p = self._policy()
        assert not p.matches(signal_kind=SIGNAL_DRIFT, severity="medium")

    def test_an_unknown_severity_does_not_match(self):
        # Treating it as maximal would let a future severity name fire every
        # actuating policy in the fleet the moment it shipped.
        assert not self._policy().matches(signal_kind=SIGNAL_DRIFT, severity="apocalyptic")

    def test_a_null_drift_class_matches_any(self):
        p = self._policy(drift_class=None)
        assert p.matches(signal_kind=SIGNAL_DRIFT, severity="high", drift_class="cost")

    def test_a_specific_drift_class_matches_only_itself(self):
        p = self._policy(drift_class="cost")
        assert p.matches(signal_kind=SIGNAL_DRIFT, severity="high", drift_class="cost")
        assert not p.matches(
            signal_kind=SIGNAL_DRIFT, severity="high", drift_class="behavioural"
        )

    def test_signal_kinds_do_not_cross(self):
        p = self._policy(signal_kind=SIGNAL_DRIFT)
        assert not p.matches(signal_kind=SIGNAL_LIVENESS, severity="critical")

    def test_a_disabled_policy_never_matches(self):
        assert not self._policy(enabled=False).matches(
            signal_kind=SIGNAL_DRIFT, severity="critical"
        )

    def test_the_default_floor_is_high(self):
        # medium and below is "look at this when you get a chance"; wiring it
        # to an actuator produces the over-reaction that gets the feature
        # disabled.
        assert self._policy().severity_at_least == "high"


class TestPolicySafetyRails:
    def _policy(self, **kwargs) -> AISystemResponsePolicy:
        base = dict(
            id="p1", tenant_id="t1", ai_system_id="sys", name="p",
            actions=[{"kind": ACTION_SET_AUTONOMY, "level": "restricted"}],
        )
        base.update(kwargs)
        return AISystemResponsePolicy(**base)

    def test_a_policy_that_would_loosen_is_detectable(self):
        # An incident response must never hand a degraded system more
        # freedom than it already had.
        p = self._policy(actions=[{"kind": ACTION_SET_AUTONOMY, "level": "approval_required"}])
        assert p.would_loosen("restricted")
        assert not p.would_loosen("autonomous")

    def test_an_equal_level_does_not_count_as_loosening(self):
        p = self._policy()
        assert not p.would_loosen("restricted")

    def test_a_notify_only_policy_cannot_loosen(self):
        p = self._policy(actions=[{"kind": ACTION_NOTIFY, "targets": ["ops"]}])
        assert not p.would_loosen("suspended")

    def test_restore_is_human_by_default(self):
        # Auto-restore flaps: the signal clears because the system was
        # throttled, the throttle lifts, the signal returns.
        assert AISystemResponsePolicy.__table__.c.require_human_to_restore.default.arg is True

    def test_suspend_is_off_by_default(self):
        assert AISystemResponsePolicy.__table__.c.allow_suspend.default.arg is False

    def test_the_cooldown_defaults_to_an_hour(self):
        # A drift that persists across sweeps must not ratchet a system down
        # four levels before anyone reads the first alert.
        assert DEFAULT_RESPONSE_COOLDOWN_SECONDS == 3600

    def test_severity_ranking_is_ordered(self):
        assert (
            SEVERITY_RANK["low"]
            < SEVERITY_RANK["medium"]
            < SEVERITY_RANK["high"]
            < SEVERITY_RANK["critical"]
        )

    def test_rubric_scores_are_not_a_trigger_vocabulary(self):
        # Structural here, not a convention: a probabilistic judge
        # score wired to an automatic enforcement action is how a system gets
        # throttled because a model had a bad afternoon.
        from app.database.models.ai_system_response_policy import SIGNAL_KINDS

        assert SIGNAL_KINDS == {"drift", "liveness"}
        assert not any("rubric" in s or "score" in s for s in SIGNAL_KINDS)


class TestObservedCompleteEligibility:
    """client_asserted runs are complete observations and must be eligible.

    The regression this pins: every eligibility filter said `= 'intact'`, so
    a system whose agents correlate runs by `x-brutor-run-id` — the documented
    integration for self-driving agents — had 100% of its real traffic
    excluded from baselines and replay suites. Baselines stayed `learning`
    forever and the promotion gate could only ever sample seeded data.
    """

    def test_suite_builder_accepts_asserted_runs(self):
        from app.services import ai_system_suite_builder as b

        sql = b.ELIGIBLE_SQL if hasattr(b, "ELIGIBLE_SQL") else pathlib_read()
        assert "chain_integrity IN ('intact', 'client_asserted')" in sql, (
            "replay eligibility must accept every complete observation; "
            "excluding client_asserted locks real agent traffic out of the gate"
        )

    def test_model_eligibility_matches(self):
        from app.database.models.ai_system_run import (
            CHAIN_CLIENT_ASSERTED,
            CHAIN_INTACT,
            CHAIN_OBSERVED_COMPLETE,
            CHAIN_PARTIAL,
        )

        assert CHAIN_INTACT in CHAIN_OBSERVED_COMPLETE
        assert CHAIN_CLIENT_ASSERTED in CHAIN_OBSERVED_COMPLETE
        assert CHAIN_PARTIAL not in CHAIN_OBSERVED_COMPLETE


def pathlib_read():
    import pathlib as _p

    return _p.Path("app/services/ai_system_suite_builder.py").read_text()

