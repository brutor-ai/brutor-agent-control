# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Kind-aware signal profiles.

Two properties carry this module, and most tests here defend one or the
other:

  1. **A relaxation is never silent.** If picking `assistant` could quietly
     stop behavioural drift from counting, `system_kind` would be a dropdown
     for laundering a bad health score. Every downgrade must appear in
     `relaxations()` and in the report.
  2. **`not_applicable` is never a pass.** A signal that does not apply has
     not been satisfied — it was not asked. It must never arrive at the
     scorer as 1.0, which would turn "we don't measure this" into "this is
     perfect".
"""

import pytest

from app.database.models.resource_group import (
    DEFAULT_SYSTEM_KIND,
    SYSTEM_KIND_AGENT,
    SYSTEM_KIND_APPLICATION,
    SYSTEM_KIND_ASSISTANT,
    SYSTEM_KIND_INTEGRATION,
    SYSTEM_KIND_SERVICE,
    SYSTEM_KIND_WORKFLOW,
    SYSTEM_KINDS,
)
from app.services.ai_system_signal_profile import (
    ADVISORY,
    ALWAYS_ENFORCED,
    ENFORCED,
    LEVELS,
    NOT_APPLICABLE,
    PROFILES,
    RELAXATION_REASONS,
    SIGNALS,
    apply_profile,
    level_for,
    profile_for,
    profile_gaps,
    relaxations,
)

ALL_SCORES = {s: 0.5 for s in SIGNALS}


# ---------------------------------------------------------------------------
# Structural invariants — cheap, and they catch a whole class of future edits.
# ---------------------------------------------------------------------------


def test_every_system_kind_has_a_profile():
    """A kind with no profile would silently fall back to `agent` and be
    scored under rules nobody wrote for it."""
    assert set(PROFILES) == set(SYSTEM_KINDS)


def test_every_profile_covers_every_signal():
    for kind, prof in PROFILES.items():
        assert set(prof) == set(SIGNALS), f"{kind} profile is missing signals"
        assert set(prof.values()) <= LEVELS, f"{kind} has an unknown level"


def test_always_enforced_signals_are_never_relaxed_by_any_kind():
    """Reliability, cost and conformance are shape-independent: an erroring,
    overspending or policy-violating system is unhealthy whether it is an
    agent or a chat box. No future kind may opt out of these three."""
    for kind, prof in PROFILES.items():
        for signal in ALWAYS_ENFORCED:
            assert prof[signal] == ENFORCED, f"{kind} relaxed {signal}"


def test_every_relaxation_has_a_written_reason():
    """A downgrade with no stated justification is exactly the silent
    laundering this module forbids — so the reason table must be complete."""
    for kind, prof in PROFILES.items():
        for signal, level in prof.items():
            if level != ENFORCED:
                assert (kind, signal) in RELAXATION_REASONS, (
                    f"{kind}/{signal} is {level} with no reason on record"
                )


def test_agent_is_the_strictest_profile():
    """`agent` is DEFAULT_SYSTEM_KIND precisely because it enforces
    everything; an unclassified system must never be scored leniently."""
    assert all(v == ENFORCED for v in PROFILES[SYSTEM_KIND_AGENT].values())
    assert DEFAULT_SYSTEM_KIND == SYSTEM_KIND_AGENT


@pytest.mark.parametrize("kind", sorted(SYSTEM_KINDS))
def test_no_profile_is_stricter_than_agent(kind):
    for signal in SIGNALS:
        if PROFILES[SYSTEM_KIND_AGENT][signal] == ENFORCED:
            assert PROFILES[kind][signal] in LEVELS


# ---------------------------------------------------------------------------
# Fallback — an unknown kind must not be a way to escape scoring.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", [None, "", "not-a-kind", "AGENT", "coding_agent"])
def test_unknown_or_missing_kind_falls_back_to_the_strictest_profile(kind):
    assert profile_for(kind) == PROFILES[DEFAULT_SYSTEM_KIND]
    assert relaxations(kind) == []


def test_profile_for_returns_a_copy_not_the_shared_table():
    """A caller mutating the result must not rewrite the profile for every
    system in the process."""
    p = profile_for(SYSTEM_KIND_AGENT)
    p["cost"] = NOT_APPLICABLE
    assert PROFILES[SYSTEM_KIND_AGENT]["cost"] == ENFORCED


# ---------------------------------------------------------------------------
# The per-kind judgements
# ---------------------------------------------------------------------------


def test_assistant_does_not_score_liveness():
    """Humans are bursty and go on holiday. Paging someone about a quiet
    Sunday on a chat assistant is how an alert category gets muted."""
    assert level_for(SYSTEM_KIND_ASSISTANT, "liveness") == NOT_APPLICABLE


def test_assistant_keeps_behaviour_visible_but_advisory():
    """A person may ask anything, so a change in traffic shape is not
    evidence of misbehaviour — but it is still computed and shown."""
    assert level_for(SYSTEM_KIND_ASSISTANT, "behaviour") == ADVISORY
    out = apply_profile(SYSTEM_KIND_ASSISTANT, ALL_SCORES)
    assert out["display"]["behaviour"]["score"] == 0.5
    assert "behaviour" not in out["enforced"]


def test_application_relaxes_shape_signals_only():
    """The call shape is fixed by product code, so drift there is a deploy."""
    assert level_for(SYSTEM_KIND_APPLICATION, "behaviour") == ADVISORY
    assert level_for(SYSTEM_KIND_APPLICATION, "liveness") == ADVISORY
    assert level_for(SYSTEM_KIND_APPLICATION, "conformance") == ENFORCED


def test_integration_enforces_liveness_because_cadence_is_its_headline(db=None):
    assert level_for(SYSTEM_KIND_INTEGRATION, "liveness") == ENFORCED


def test_workflow_relaxes_nothing():
    assert relaxations(SYSTEM_KIND_WORKFLOW) == []


def test_service_relaxes_only_liveness():
    rel = relaxations(SYSTEM_KIND_SERVICE)
    assert [r["signal"] for r in rel] == ["liveness"]
    assert rel[0]["level"] == ADVISORY


# ---------------------------------------------------------------------------
# `not_applicable` is never a pass.
# ---------------------------------------------------------------------------


def test_not_applicable_never_reaches_the_scorer_as_a_perfect_score():
    """The failure this guards against turns "we don't measure this" into
    "this is perfect" — a silent upgrade of every assistant's health."""
    out = apply_profile(SYSTEM_KIND_ASSISTANT, {**ALL_SCORES, "liveness": 0.0})
    assert "liveness" not in out["enforced"]
    assert 1.0 not in out["enforced"].values()


def test_not_applicable_displays_as_none_not_zero():
    """Rendered as "n/a" — a zero would read as a failing signal, which is
    just as wrong as a green tick."""
    out = apply_profile(SYSTEM_KIND_ASSISTANT, {**ALL_SCORES, "liveness": 0.0})
    assert out["display"]["liveness"] == {"score": None, "level": NOT_APPLICABLE}


def test_a_dead_assistant_is_not_dragged_down_by_liveness():
    out = apply_profile(
        SYSTEM_KIND_ASSISTANT,
        {**ALL_SCORES, "liveness": 0.0, "behaviour": 0.0},
    )
    # Only the enforced four remain, none of them the relaxed ones.
    # `review` is a governance signal like oversight: a dead assistant
    # still has findings someone must decide on.
    assert set(out["enforced"]) == {"reliability", "cost", "conformance", "oversight", "review"}
    assert min(out["enforced"].values()) == 0.5


def test_an_enforced_zero_still_counts_for_every_kind():
    """Relaxing shape signals must not make a kind unfailable."""
    for kind in SYSTEM_KINDS:
        out = apply_profile(kind, {**ALL_SCORES, "conformance": 0.0})
        assert out["enforced"]["conformance"] == 0.0


def test_none_scores_pass_through_without_becoming_zero():
    """`None` means "not enough data", which is not the same as a bad score
    and must survive the profile untouched."""
    out = apply_profile(SYSTEM_KIND_AGENT, {s: None for s in SIGNALS})
    assert all(v is None for v in out["enforced"].values())


# ---------------------------------------------------------------------------
# Disclosure — the anti-laundering half.
# ---------------------------------------------------------------------------


def test_relaxations_are_disclosed_with_signal_level_and_reason():
    rel = relaxations(SYSTEM_KIND_ASSISTANT)
    assert {r["signal"] for r in rel} == {"liveness", "behaviour"}
    for r in rel:
        assert r["level"] in {ADVISORY, NOT_APPLICABLE}
        assert len(r["reason"]) > 20, "a reason must actually explain"


def test_apply_profile_carries_the_disclosure_alongside_the_scores():
    out = apply_profile(SYSTEM_KIND_ASSISTANT, ALL_SCORES)
    assert out["relaxations"]
    assert out["system_kind"] == SYSTEM_KIND_ASSISTANT


def test_profile_gaps_name_the_kind_and_every_relaxed_signal():
    gaps = profile_gaps(SYSTEM_KIND_ASSISTANT)
    assert any("assistant" in g for g in gaps)
    body = " ".join(gaps)
    assert "Liveness is not scored" in body
    assert "advisory for an assistant" in body


def test_a_fully_enforced_kind_adds_no_gap_lines():
    """The section must shrink to nothing when there is nothing to disclose,
    or operators learn to skip it."""
    assert profile_gaps(SYSTEM_KIND_AGENT) == []
    assert profile_gaps(SYSTEM_KIND_WORKFLOW) == []


def test_unknown_kind_discloses_nothing_because_it_relaxes_nothing():
    assert profile_gaps("not-a-kind") == []
