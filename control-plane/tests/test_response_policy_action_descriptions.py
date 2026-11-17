# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""A response policy's actions are described from the shape they are stored in.

The bug class: a view reads a stored action under a key it does not have. The
Composition list printed "on drift → None" for the demo's response policy
because it read `type`; `validate_actions` stores actions keyed by `kind`.

Guards:
  * `describe_action` has a phrase for every member of `ACTION_KINDS`, built
    from an action that `validate_actions` accepted (so the fixture cannot
    drift from the stored shape), and never prints a missing value;
  * the Composition list's response-policy row is built through it.
"""
from __future__ import annotations

import pytest

from app.database.models.ai_system_response_policy import (
    ACTION_KINDS,
    ACTION_NOTIFY,
    ACTION_OPEN_INCIDENT,
    ACTION_PIN_MODEL,
    ACTION_SET_AUTONOMY,
    ACTION_TIGHTEN_GRANTS,
    describe_action,
    validate_actions,
)
from app.services.ai_system_composition import _gateway

#: One valid action per kind, as the API stores it.
STORED = validate_actions(
    [
        {"kind": ACTION_SET_AUTONOMY, "level": "approval_required"},
        {"kind": ACTION_NOTIFY, "targets": ["ops-oncall", "system-owner"]},
        {"kind": ACTION_PIN_MODEL},
        {"kind": ACTION_TIGHTEN_GRANTS, "classes": ["side_effecting"]},
        {"kind": ACTION_OPEN_INCIDENT},
    ],
    allow_suspend=False,
)


def test_the_fixture_covers_every_action_kind():
    assert {a["kind"] for a in STORED} == set(ACTION_KINDS)


@pytest.mark.parametrize("action", STORED, ids=lambda a: a["kind"])
def test_every_kind_has_a_phrase_that_names_what_it_does(action):
    phrase = describe_action(action)
    assert "None" not in phrase and "unrecognised" not in phrase
    assert phrase.strip() == phrase and phrase


def test_the_phrases_carry_the_parameters():
    by_kind = {a["kind"]: describe_action(a) for a in STORED}
    assert by_kind[ACTION_SET_AUTONOMY] == "set autonomy to approval_required"
    assert by_kind[ACTION_NOTIFY] == "notify ops-oncall, system-owner"
    assert by_kind[ACTION_TIGHTEN_GRANTS] == "tighten grants (side_effecting)"


@pytest.mark.parametrize("bad", [None, "set_autonomy", {}, {"type": "set_autonomy"}, {"kind": "reboot"}])
def test_an_action_it_cannot_read_is_named_not_hidden(bad):
    assert describe_action(bad).startswith("unrecognised action")


def _policy_row(actions, enabled=True):
    closure = {"response_policies": [{
        "id": "rp-1", "name": "Drift downgrades autonomy", "enabled": enabled,
        "attached_to": "sys-1", "inherited": False, "signal_kind": "drift", "actions": actions,
    }]}
    rows = _gateway(closure, [], {}, "sys-1")["policies"]
    return next(r for r in rows if r["kind"] == "response_policy")


def test_the_composition_row_says_what_the_policy_does():
    row = _policy_row([STORED[0]])
    assert row["summary"] == "on drift → set autonomy to approval_required"


def test_the_composition_row_lists_every_action_and_never_a_missing_value():
    summary = _policy_row(STORED, enabled=False)["summary"]
    for action in STORED:
        assert describe_action(action) in summary
    assert "None" not in summary
    assert summary.endswith("(disabled)")
