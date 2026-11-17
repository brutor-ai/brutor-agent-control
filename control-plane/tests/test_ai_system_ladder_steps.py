# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""The evidence-maturity ladder as a workflow, not a readout.

The ladder used to say "you are at L1, next is L2" and stop. Working out
*which* of five dimensions was blocking L2, and which screen fixes it, was
left to the operator — a workflow the product knows and the user does not.

The property most of these defend: the blocker text is read from the
evaluated dimension rather than written twice. A hand-maintained blocker
string is wrong the first time the dimension rules change, and a ladder that
confidently tells you to fix the wrong thing is worse than one that says
nothing.
"""

import pytest

from app.services.ai_system_assurance import (
    LADDER_STEPS,
    _assurance_level,
    _next_step,
)

DIMENSIONS = (
    "operational_evidence",
    "behavioural_stability",
    "contract_assurance",
    "execution_evidence",
    "independent_evidence",
)


def dims(**overrides):
    """All dimensions ok unless told otherwise."""
    base = {d: {"status": "ok", "detail": f"{d} is fine."} for d in DIMENSIONS}
    for k, v in overrides.items():
        base[k] = v
    return base


def missing(detail="not there"):
    return {"status": "missing", "detail": detail}


# ---------------------------------------------------------------------------
# Structural — the step table must stay aligned with the ladder itself.
# ---------------------------------------------------------------------------


def test_every_rung_has_a_step_with_a_requirement_and_an_action():
    for level in range(5):
        step = LADDER_STEPS[level]
        assert step["dimension"] in DIMENSIONS
        assert len(step["requires"]) > 20
        assert len(step["action"]) > 20
        assert step["surface"]


def test_step_dimensions_are_distinct_and_cover_every_dimension():
    """Each rung is gated by its own dimension; a duplicate would mean one
    dimension silently gates two rungs and another gates none."""
    used = [LADDER_STEPS[i]["dimension"] for i in range(5)]
    assert sorted(used) == sorted(DIMENSIONS)


def test_there_is_no_step_beyond_the_top_rung():
    assert _next_step(5, dims()) is None


# ---------------------------------------------------------------------------
# The blocker comes from the evaluated dimension, never from a second copy.
# ---------------------------------------------------------------------------


def test_the_blocker_is_the_dimensions_own_detail():
    d = dims(contract_assurance=missing("No approved operating envelope is in force."))
    step = _next_step(2, d)
    assert step["blocker"] == "No approved operating envelope is in force."
    assert step["status"] == "missing"


def test_a_changed_dimension_detail_flows_through_without_touching_the_table():
    """The anti-drift property: nothing in LADDER_STEPS restates the reason."""
    for text in ("reason A", "a completely different reason B"):
        step = _next_step(2, dims(contract_assurance=missing(text)))
        assert step["blocker"] == text


def test_step_carries_the_action_and_the_surface_to_act_on():
    step = _next_step(2, dims(contract_assurance=missing()))
    assert "envelope" in step["action"]
    assert step["surface"] == "contract"


# ---------------------------------------------------------------------------
# Integration with the level computation.
# ---------------------------------------------------------------------------


def test_next_names_the_dimension_actually_blocking_it():
    """L2 unmet because the contract is missing — the next step must point at
    the contract, not at whatever rung happens to come next numerically."""
    out = _assurance_level(dims(contract_assurance=missing()))
    assert out["level"] == 1
    assert out["next"]["level"] == 2
    assert out["next"]["dimension"] == "contract_assurance"
    assert out["next"]["action"]


def test_a_fully_assured_system_has_no_next_step():
    out = _assurance_level(dims())
    assert out["level"] == 4
    assert out["next"] is None


def test_a_system_with_no_traffic_is_pointed_at_routing_it():
    out = _assurance_level(dims(operational_evidence=missing()))
    assert out["level"] == -1
    assert out["next"]["level"] == 0
    assert "gateway" in out["next"]["action"]


# ---------------------------------------------------------------------------
# The checklist — every rung, held or not.
# ---------------------------------------------------------------------------


def test_rungs_render_the_whole_ladder_not_just_the_current_level():
    out = _assurance_level(dims(contract_assurance=missing()))
    assert [r["level"] for r in out["rungs"]] == [0, 1, 2, 3, 4]
    assert [r["held"] for r in out["rungs"]] == [True, True, False, True, True]


def test_the_checklist_cannot_disagree_with_the_headline_level():
    """`rungs` and `level` are computed from the same list, so the highest
    contiguous held rung must always equal the reported level."""
    cases = [
        dims(),
        dims(contract_assurance=missing()),
        dims(operational_evidence=missing()),
        dims(independent_evidence=missing()),
        dims(execution_evidence={"status": "partial", "detail": "some gaps"}),
    ]
    for d in cases:
        out = _assurance_level(d)
        contiguous = -1
        for r in out["rungs"]:
            if not r["held"]:
                break
            contiguous = r["level"]
        assert contiguous == out["level"], out["rungs"]


def test_a_held_rung_above_a_broken_one_is_shown_held_but_does_not_raise_the_level():
    """Cumulative means L3 evidence does not buy you L3 while L2 is unmet —
    but the operator should still see that the work is done, or they will
    redo it."""
    out = _assurance_level(dims(contract_assurance=missing()))
    assert out["level"] == 1
    assert out["rungs"][3]["held"] is True
    assert out["rungs"][4]["held"] is True


def test_each_rung_carries_its_requirement_for_the_checklist():
    for r in _assurance_level(dims())["rungs"]:
        assert r["requires"]
        assert r["status"] == "ok"
