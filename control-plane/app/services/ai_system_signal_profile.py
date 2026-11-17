# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Kind-aware signal profiles — which signals mean something for this shape
of AI System, and which are noise.

`ResourceGroup.system_kind` has always been documented as load-bearing:
"it selects the signal profile the lifecycle engine applies".
Until now nothing read it for that purpose, so every kind was scored as if it
were an `agent`. That is wrong in both directions:

  * **False alarms.** Cadence/silence detection is critical for an
    `integration` and actively wrong for an `assistant` — humans are bursty
    and go on holiday. Paging someone about a quiet Sunday on a chat
    assistant is how an alert category gets muted, and a muted signal
    protects nobody.
  * **False comfort.** Trajectory drift is the headline signal for an
    `agent` and near-meaningless for an `application`, where the call shape
    is fixed by the product code. Scoring it there dilutes the number that
    actually matters.

## The exploit this module must not open

A profile that quietly stops scoring a signal is a way to launder a bad
health score: pick `assistant`, and behaviour drift stops counting. So a
relaxation is never silent. Every downgrade this module applies is returned
in `relaxations` and surfaced in the Assurance Report, and the health verdict
records that the system was scored under a relaxed profile. An operator may
choose a kind; they may not choose it *and* have the choice go unmentioned.

## Three levels, and what each does to the verdict

  * `enforced` — scored, and counts in the worst-of health verdict.
  * `advisory` — computed and displayed, excluded from the verdict. The
    number is real; it just is not evidence of ill health for this shape.
  * `not_applicable` — not scored at all. Renders as "n/a", **never as green
    and never as a zero**. This is the same rule the rest of the platform
    runs on: absence of evidence is never a pass. A signal that does not
    apply has not been satisfied — it was not asked.

`agent` enforces all six, which is why it is `DEFAULT_SYSTEM_KIND`: the
strictest superset, so an unclassified system is never scored more leniently
than it should be.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.database.models.resource_group import (
    DEFAULT_SYSTEM_KIND,
    SYSTEM_KIND_AGENT,
    SYSTEM_KIND_APPLICATION,
    SYSTEM_KIND_ASSISTANT,
    SYSTEM_KIND_INTEGRATION,
    SYSTEM_KIND_SERVICE,
    SYSTEM_KIND_WORKFLOW,
)

ENFORCED = "enforced"
ADVISORY = "advisory"
NOT_APPLICABLE = "not_applicable"

LEVELS = frozenset({ENFORCED, ADVISORY, NOT_APPLICABLE})

# The six signals, in the order the UI reads them.
SIGNALS = (
    "liveness",
    "behaviour",
    "reliability",
    "cost",
    "conformance",
    "oversight",
    "review",
)

# Signals that are never relaxed, whatever the kind. Reliability, cost and
# conformance are shape-independent: an erroring, overspending or
# policy-violating system is unhealthy whether it is an agent or a chat box.
# Keeping them out of the profile table means no future kind can accidentally
# opt out of the three signals that always mean something.
# `review` joins them: unreviewed critical findings are a human-
# process failure regardless of the system's shape — no kind gets to opt
# out of looking at its own flagged runs.
ALWAYS_ENFORCED = frozenset({"reliability", "cost", "conformance", "review"})


def _profile(**overrides: str) -> Dict[str, str]:
    """A profile is `enforced` everywhere except where a kind says otherwise."""
    p = {s: ENFORCED for s in SIGNALS}
    p.update(overrides)
    return p


# Per-kind deviations from "enforce everything". Each entry needs a reason in
# `RELAXATION_REASONS` — a downgrade with no stated justification is exactly
# the silent laundering this module forbids.
PROFILES: Dict[str, Dict[str, str]] = {
    # The strictest superset. Trajectory drift and cadence both mean
    # something for goal-pursuing systems.
    SYSTEM_KIND_AGENT: _profile(),
    # Deterministic control flow: if anything, *more* learnable than an
    # agent, so nothing is relaxed.
    SYSTEM_KIND_WORKFLOW: _profile(),
    # Scheduled / event-driven. Cadence is the headline signal here.
    SYSTEM_KIND_INTEGRATION: _profile(),
    # Human-in-the-loop conversational. The dynamic case: a person can ask
    # anything, so "the shape of the traffic changed" is not evidence of
    # anything going wrong, and silence is a weekend rather than an outage.
    SYSTEM_KIND_ASSISTANT: _profile(
        liveness=NOT_APPLICABLE,
        behaviour=ADVISORY,
    ),
    # A product feature calling models. The call shape is fixed by the
    # product code, so drift in it is a deploy, not a behavioural anomaly;
    # and traffic follows product usage rather than a cadence we can learn.
    SYSTEM_KIND_APPLICATION: _profile(
        liveness=ADVISORY,
        behaviour=ADVISORY,
    ),
    # A shared internal capability. Its callers decide when it runs, so
    # silence is a fact about them, not about it.
    SYSTEM_KIND_SERVICE: _profile(liveness=ADVISORY),
}

RELAXATION_REASONS: Dict[tuple, str] = {
    (SYSTEM_KIND_ASSISTANT, "liveness"): (
        "Liveness is not scored for an assistant: usage is human and bursty, "
        "so a quiet period is not evidence of an outage."
    ),
    (SYSTEM_KIND_ASSISTANT, "behaviour"): (
        "Behavioural drift is advisory for an assistant: a person may ask "
        "anything, so a change in traffic shape is not evidence of "
        "misbehaviour. It is still computed and shown."
    ),
    (SYSTEM_KIND_APPLICATION, "liveness"): (
        "Liveness is advisory for an application: traffic follows product "
        "usage rather than a learnable cadence."
    ),
    (SYSTEM_KIND_APPLICATION, "behaviour"): (
        "Behavioural drift is advisory for an application: the call shape is "
        "fixed by the product code, so a change is a deployment rather than "
        "an anomaly."
    ),
    (SYSTEM_KIND_SERVICE, "liveness"): (
        "Liveness is advisory for a shared service: its callers decide when "
        "it runs, so silence describes them rather than it."
    ),
}


def profile_for(system_kind: Optional[str]) -> Dict[str, str]:
    """The signal profile for a kind.

    An unknown or missing kind falls back to `DEFAULT_SYSTEM_KIND` (`agent`),
    the strictest profile — a system we cannot classify must never be scored
    more leniently than one we can.
    """
    if not system_kind or system_kind not in PROFILES:
        return dict(PROFILES[DEFAULT_SYSTEM_KIND])
    return dict(PROFILES[system_kind])


def level_for(system_kind: Optional[str], signal: str) -> str:
    return profile_for(system_kind).get(signal, ENFORCED)


def relaxations(system_kind: Optional[str]) -> List[Dict[str, str]]:
    """Every signal this kind does not fully enforce, with the reason.

    The disclosure half of the feature. If this returns entries, the report
    and the health verdict must say so — otherwise choosing a kind becomes a
    way to make a bad score disappear.
    """
    kind = system_kind if system_kind in PROFILES else DEFAULT_SYSTEM_KIND
    out: List[Dict[str, str]] = []
    for signal in SIGNALS:
        level = PROFILES[kind][signal]
        if level == ENFORCED:
            continue
        out.append(
            {
                "signal": signal,
                "level": level,
                "reason": RELAXATION_REASONS.get(
                    (kind, signal),
                    f"{signal} is {level} for a {kind}.",
                ),
            }
        )
    return out


def apply_profile(
    system_kind: Optional[str],
    scores: Dict[str, Optional[float]],
) -> Dict[str, Any]:
    """Split scored signals into what counts and what is merely reported.

    Returns the `enforced` subset for the worst-of health verdict, the full
    set for display, and the disclosure. The caller passes `enforced` into
    `score_health` and keeps `display` for the UI, so a relaxed signal stays
    visible while no longer dragging the verdict down.
    """
    prof = profile_for(system_kind)
    enforced: Dict[str, Optional[float]] = {}
    display: Dict[str, Any] = {}
    for signal in SIGNALS:
        level = prof.get(signal, ENFORCED)
        value = scores.get(signal)
        if level == NOT_APPLICABLE:
            # Dropped entirely — and explicitly NOT recorded as 1.0. A signal
            # that does not apply has not been satisfied; it was not asked.
            display[signal] = {"score": None, "level": level}
            continue
        display[signal] = {"score": value, "level": level}
        if level == ENFORCED:
            enforced[signal] = value
    return {
        "enforced": enforced,
        "display": display,
        "profile": prof,
        "relaxations": relaxations(system_kind),
        "system_kind": system_kind or DEFAULT_SYSTEM_KIND,
    }


def profile_gaps(system_kind: Optional[str]) -> List[str]:
    """Report lines disclosing what this kind's profile does not evidence."""
    rel = relaxations(system_kind)
    if not rel:
        return []
    kind = system_kind or DEFAULT_SYSTEM_KIND
    gaps = [
        f"This system is classified as '{kind}', which relaxes "
        f"{len(rel)} of the {len(SIGNALS)} assurance signals:"
    ]
    gaps.extend(f"— {r['reason']}" for r in rel)
    return gaps
