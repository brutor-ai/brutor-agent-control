# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The run envelope's enforced terms are the gateway's.

The gateway reads a system's live operating envelope and enforces the
terms it knows (`src/services/run_budget.rs`); the control plane is where
an envelope is declared, and it refuses a term it does not know. A term
the gateway enforces that could not be declared here would be a control
nobody can switch on, and one declared here under another name would be a
control that looks armed and is not. These read the gateway at its
committed HEAD.
"""

from __future__ import annotations

import re

import pytest

from app.services import ai_system_envelope as env
from tests.core_repo import core_source

# Not a ceiling: the reason of a refusal when the run's counters cannot be read.
NOT_A_TERM = {"run_usage_unavailable"}
# Enforced by the gateway outside the per-run budget module.
ENFORCED_ELSEWHERE = {env.TERM_MAX_COST_PER_SESSION}


def _gateway() -> str:
    return core_source("services", "run_budget.rs").read_text(encoding="utf-8")


def _gateway_terms() -> set:
    return set(re.findall(r'pub const TERM_[A-Z_]+: &str = "([a-z_]+)";', _gateway())) - NOT_A_TERM


def test_every_term_the_gateway_enforces_is_declarable_and_marked_enforced():
    terms = _gateway_terms()
    assert {"max_actions_per_run", "max_new_runs_per_window", "new_runs_window_seconds"} <= terms
    assert terms | ENFORCED_ELSEWHERE == set(env.ENFORCED_TERMS)
    for term in terms:
        assert env.ENVELOPE_TERMS[term]["enforced"] is True


def test_the_longest_window_is_the_gateways():
    (days,) = re.findall(r"pub const MAX_NEW_RUNS_WINDOW_SECS: u64 = (\d+) \* 86_400;", _gateway())
    assert env.MAX_NEW_RUNS_WINDOW_SECONDS == int(days) * 86_400 == 2_678_400
    (default,) = re.findall(r"pub const DEFAULT_NEW_RUNS_WINDOW_SECS: u64 = ([\d_]+);", _gateway())
    assert env.DEFAULT_NEW_RUNS_WINDOW_SECONDS == int(default.replace("_", ""))


@pytest.mark.parametrize("term", ["max_actions_per_run", "max_new_runs_per_window"])
@pytest.mark.parametrize("value", [0, -1, 1.5, "many", None, [3], True])
def test_a_count_is_a_whole_number_of_at_least_one(term, value):
    with pytest.raises(env.EnvelopeValidationError):
        env.validate_envelope({term: value})


@pytest.mark.parametrize("term", ["max_actions_per_run", "max_new_runs_per_window"])
def test_a_count_is_accepted(term):
    assert env.validate_envelope({term: 1}) == {term: 1.0}
    assert env.validate_envelope({term: 40}) == {term: 40.0}


@pytest.mark.parametrize("value", [0, -5, 2_678_401, 1.5, "hour", None])
def test_the_window_is_a_whole_number_of_seconds_within_the_gateways_range(value):
    with pytest.raises(env.EnvelopeValidationError):
        env.validate_envelope({"max_new_runs_per_window": 5, "new_runs_window_seconds": value})


@pytest.mark.parametrize("value", [1, 600, 2_678_400])
def test_a_window_with_its_maximum_is_accepted(value):
    assert env.validate_envelope({"max_new_runs_per_window": 5, "new_runs_window_seconds": value}) == {
        "max_new_runs_per_window": 5.0, "new_runs_window_seconds": float(value),
    }


def test_a_window_without_a_maximum_is_refused():
    with pytest.raises(env.EnvelopeValidationError, match="max_new_runs_per_window"):
        env.validate_envelope({"new_runs_window_seconds": 600})


def test_the_help_says_what_is_counted():
    actions = env.ENVELOPE_TERMS["max_actions_per_run"]["help"]
    for counted in ("tool call", "skill", "agent-to-agent"):
        assert counted in actions
    assert "model call" in actions and "max_llm_calls_per_run" in actions
    runs = env.ENVELOPE_TERMS["max_new_runs_per_window"]["help"]
    assert "credential" in runs and "start" in runs
    window = env.ENVELOPE_TERMS["new_runs_window_seconds"]["help"]
    assert "3600" in window or "an hour" in window


def test_a_term_with_no_recorded_fact_is_enforced_only():
    """The report grades a term against a fact of the run ledger. A term
    without one says so, and is neither met nor breached nor unevaluable."""
    for term in ("max_actions_per_run", "max_new_runs_per_window", "new_runs_window_seconds", env.TERM_MAX_COST_PER_SESSION):
        assert term in env.ENFORCED_ONLY_TERMS
    assert env.ENFORCED_ONLY_TERMS <= env.ENFORCED_TERMS
    graded = set(env.ENFORCED_TERMS) - set(env.ENFORCED_ONLY_TERMS)
    assert graded == set(env._WORST_RUN_FACT)


# --- what the API answers about each term is what the validator holds --------


def _accepts(envelope) -> bool:
    try:
        env.validate_envelope(envelope)
        return True
    except env.EnvelopeValidationError:
        return False


def test_the_answered_bounds_are_the_validators():
    from app.api.v1.endpoints.admin.ai_system_contracts import EnvelopeTerm

    terms = {term: EnvelopeTerm.model_validate(spec) for term, spec in env.known_terms().items()}
    assert set(terms) == set(env.ENVELOPE_TERMS)
    for term, spec in terms.items():
        key = f"{term}:refund" if spec.parameterized else term
        with_ = {spec.requires: 5} if spec.requires else {}

        def accepts(value):
            return _accepts({key: value, **with_})

        good = spec.minimum if spec.minimum is not None else 2.0
        if spec.exclusive_minimum is not None:
            assert not accepts(spec.exclusive_minimum), term
            good = spec.exclusive_minimum + 2
        assert accepts(good), term
        if spec.minimum is not None:
            assert accepts(spec.minimum) and not accepts(spec.minimum - 1), term
        if spec.maximum is not None:
            assert accepts(spec.maximum) and not accepts(spec.maximum + 1), term
        elif spec.unit != "rate":
            assert accepts(10**9), term
        assert accepts(good + 0.5) is not spec.whole_number or spec.unit == "rate", term
        if spec.requires:
            assert not _accepts({key: good}), term
            assert spec.requires in terms
        assert spec.graded is (term not in env.ENFORCED_ONLY_TERMS)
        assert spec.enforced is (term in env.ENFORCED_TERMS)


def test_the_window_says_its_maximum_its_default_and_what_it_needs():
    window = env.known_terms()[env.TERM_NEW_RUNS_WINDOW_SECONDS]
    assert window["maximum"] == env.MAX_NEW_RUNS_WINDOW_SECONDS == 2_678_400
    assert window["default"] == env.DEFAULT_NEW_RUNS_WINDOW_SECONDS == 3_600
    assert window["requires"] == env.TERM_MAX_NEW_RUNS_PER_WINDOW
    others = {t: s for t, s in env.known_terms().items() if t != env.TERM_NEW_RUNS_WINDOW_SECONDS}
    assert all(s["requires"] is None and s["default"] is None for s in others.values())
