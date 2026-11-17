# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# The operating envelope — declared contract terms beyond authorization

The contract has always pinned what a system **may** do (the resolved
authorization closure). The envelope adds what it **should** achieve:
declared, versioned bounds on cost and outcome mix that are signed off
with the rest of the contract and continuously evaluated against the run
ledger.

## Declared, not learned

Baselines learn what normal *is*; the envelope declares what acceptable
*was agreed to be*. The two answer different questions — "has it changed?"
versus "is it still satisfying its contract?" — and the second is the one
an approver signed and an auditor asks about. A system can drift without
breaching its envelope, and breach its envelope without drifting.

## What a term may claim — and what it may not

Every term is computable from ledger facts the gateway actually observed:
outcome mix, cost per completed task, spend. There is deliberately no
"correctness" term — ≥95% correct decisions is not something a proxy can
verify, because there is no ground truth at the proxy, and an envelope
that mixes verifiable terms with unverifiable ones poisons trust in the
verifiable ones.

## Evaluation is honest about evidence

A rate computed over three runs is noise wearing a percentage sign. Below
`MIN_EVIDENCE_RUNS` qualifying runs in the window, rate terms report
`insufficient_evidence` — which is a different verdict from `met`, and
never rolls up as assured. Spend caps are the exception: spend is a sum,
not an estimate, so a cap is evaluable from the first dollar.

## Declared outcomes

Rates read THE run classification (`ai_system_outcomes`): the completion
rate is the success rate (success / (success + deviation + failed)) and the
error rate is failed / the same denominator. A run whose declared outcome
does not count toward the baseline (an empty poll) is never evidence for a
rate: twenty empty polls are not twenty pieces of evidence about the work.
The average-cost term is over `success` runs (cost per processed outcome).

Two parameterised terms name an outcome in their key, `<term>:<outcome id>`
(the envelope stays a flat `{term: bound}` map, so it hashes and diffs like
every other term): `max_outcome_share:no_applications_available` = 0.9 —
"no more than 90% of declared-outcome runs end there", how an upstream feed
that silently stopped shows up — and `avg_cost_per_outcome:<id>`. An id
without a variant covers every variant of that outcome.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.constants.drift_resolution import ENVELOPE_METRIC_PREFIX
from app.constants.outcomes import OUTCOME_ID_RE
from app.database.models.ai_system_run import AISystemRun
from app.database.models.resource_group import ResourceGroup
from app.services.ai_system_outcomes import (
    RATE_CLASSES,
    RUN_FAILED,
    RUN_NO_WORK,
    RUN_SUCCESS,
    class_cond,
    classes_cond,
)
from app.services.ai_system_judgement_exclusion import (
    PURPOSE_RATE,
    epoch_floors,
    judged_run,
    since_with_floor,
)

# ---------------------------------------------------------------------------
# Term vocabulary
# ---------------------------------------------------------------------------

# Graded terms — measured over the reporting window, never enforced.
TERM_MAX_AVG_COST_PER_TASK = "max_avg_cost_per_completed_task_usd"
TERM_MAX_MONTHLY_COST = "max_monthly_cost_usd"
TERM_MIN_COMPLETION_RATE = "min_completion_rate"
TERM_MAX_ERROR_RATE = "max_error_rate"
TERM_MAX_ABANDONED_RATE = "max_abandoned_rate"
TERM_MAX_APPROVAL_RATE = "max_approval_escalation_rate"
# Parameterised by an outcome id: the declared key is
# `<term>:<outcome>[.<variant>]` (`TERM_PARAM_SEPARATOR`).
TERM_MAX_OUTCOME_SHARE = "max_outcome_share"
TERM_AVG_COST_PER_OUTCOME = "avg_cost_per_outcome"
TERM_PARAM_SEPARATOR = ":"

# Enforced per-run caps — evaluated against the WORST single run in the
# window, and enforced at the gate by the core before the next hop.
#
# `max_cost_per_run_usd` finally means what it says: a ceiling for ONE run.
# It used to be graded as the period MEAN, which let a single $50 run hide
# inside three thousand cheap ones while the term still read "met" — the one
# question an auditor uses to stop trusting a report. The averaging behaviour
# now lives under its own honest name, `max_avg_cost_per_completed_task_usd`;
# migration `a153_envelope_per_run` moves existing declarations onto it.
TERM_MAX_COST_PER_RUN = "max_cost_per_run_usd"

# The conversation ceiling. A run is one message; a session is the chat it
# belongs to and can last months. `max_cost_per_run_usd` cannot see a user
# who spends $500 across three hundred individually-cheap messages, and a
# daily user quota cannot see which conversation did it. This is the only
# scope that can.
TERM_MAX_COST_PER_SESSION = "max_cost_per_session_usd"
TERM_MAX_CALLS_PER_RUN = "max_llm_calls_per_run"
TERM_MAX_TOKENS_PER_RUN = "max_tokens_per_run"
TERM_MAX_DEPTH_PER_RUN = "max_delegation_depth_per_run"
TERM_MAX_DURATION_PER_RUN = "max_duration_seconds_per_run"
# The governed steps of a run that are not model calls: tool calls, skill
# executions and agent-to-agent hops.
TERM_MAX_ACTIONS_PER_RUN = "max_actions_per_run"
# How many runs one credential may start in a window, and the window's
# length. Not a ceiling of one run: it bounds how many runs there are.
TERM_MAX_NEW_RUNS_PER_WINDOW = "max_new_runs_per_window"
TERM_NEW_RUNS_WINDOW_SECONDS = "new_runs_window_seconds"
#: The window the gateway uses when none is declared, and the longest one
#: it keeps a set of runs for.
DEFAULT_NEW_RUNS_WINDOW_SECONDS = 3_600
MAX_NEW_RUNS_WINDOW_SECONDS = 31 * 86_400

#: Terms the core enforces at the gate. Everything else is graded only.
ENFORCED_TERMS = frozenset({
    TERM_MAX_COST_PER_RUN,
    TERM_MAX_COST_PER_SESSION,
    TERM_MAX_CALLS_PER_RUN,
    TERM_MAX_TOKENS_PER_RUN,
    TERM_MAX_ACTIONS_PER_RUN,
    TERM_MAX_DEPTH_PER_RUN,
    TERM_MAX_DURATION_PER_RUN,
    TERM_MAX_NEW_RUNS_PER_WINDOW,
    TERM_NEW_RUNS_WINDOW_SECONDS,
})

#: Enforced terms the run ledger holds no fact for: the gateway refuses
#: the call that would pass them, and the report states them as enforced
#: without grading them (`STATUS_NOT_GRADED`). A conversation's spend, the
#: steps the gateway admitted to a run (a step a later gate refuses gives
#: its unit back, which the ledger's own counts do not), and the runs a
#: credential started are counted by the gateway and recorded nowhere a
#: report could read a worst case from.
ENFORCED_ONLY_TERMS = frozenset({
    TERM_MAX_COST_PER_SESSION,
    TERM_MAX_ACTIONS_PER_RUN,
    TERM_MAX_NEW_RUNS_PER_WINDOW,
    TERM_NEW_RUNS_WINDOW_SECONDS,
})

#: direction: which way is stricter. `max` terms tighten downward, `min`
#: terms tighten upward. The contract diff and the evaluator both read
#: this — one vocabulary, two consumers, zero drift between them.
#: signal: which health component a breach degrades.
ENVELOPE_TERMS: Dict[str, Dict[str, Any]] = {
    TERM_MAX_AVG_COST_PER_TASK: {
        "direction": "max",
        "unit": "usd",
        "label": "Average cost per completed task",
        "signal": "cost",
        "enforced": False,
        "help": (
            "The mean cost of a processed outcome (a run that completed in "
            "a declared outcome that counts as work) across the reporting "
            "window. A budgeting number: it tells you what the system "
            "typically costs to run, and it is graded in the Assurance "
            "Report — never enforced. One expensive run will not breach "
            "it; use the per-run ceiling for that."
        ),
    },
    TERM_MAX_COST_PER_SESSION: {
        "direction": "max",
        "unit": "usd",
        "label": "Cost ceiling for one conversation",
        "signal": "cost",
        "enforced": True,
        "graded": False,
        "help": (
            "No single conversation may spend more than this, across every "
            "message in it. Enforced at the gateway: once a session's "
            "accumulated spend reaches the ceiling the next call is refused. "
            "Spend is read from the usage ledger, not a cache, so a restart "
            "cannot hand a user a fresh allowance. Raising the ceiling takes "
            "effect immediately — the counter is unchanged, only what it is "
            "compared against. Ending the conversation "
            "(X-Brutor-Session-End) resets it to zero."
        ),
    },
    TERM_MAX_COST_PER_RUN: {
        "direction": "max",
        "unit": "usd",
        "label": "Cost ceiling for one run",
        "signal": "cost",
        "enforced": True,
        "help": (
            "No single run may spend more than this. Enforced at the "
            "gateway: once a run's accumulated spend reaches the ceiling "
            "the next call is refused, so a runaway loop stops at its own "
            "expense instead of consuming the whole group's daily budget. "
            "Dollar bounds move when a vendor changes prices — set a token "
            "ceiling too, as the price-independent safety limit."
        ),
    },
    TERM_MAX_CALLS_PER_RUN: {
        "direction": "max",
        "unit": "count",
        "label": "Model calls per run",
        "signal": "cost",
        "enforced": True,
        "help": (
            "The most LLM calls one run may make — the 'k' in the T(n·k) "
            "workload class, and the classic runaway-loop guard. Enforced "
            "at the gateway, which also means a run that hits the ceiling "
            "is recorded as exhausted on Brutor's own evidence rather than "
            "on the client's word."
        ),
    },
    TERM_MAX_TOKENS_PER_RUN: {
        "direction": "max",
        "unit": "count",
        "label": "Tokens per run",
        "signal": "cost",
        "enforced": True,
        "help": (
            "Total tokens one run may consume across all its calls. This "
            "is the safety limit to set first: it catches the loop whose "
            "call count looks normal but whose context grows every pass, "
            "and unlike a dollar bound it cannot be loosened by a vendor "
            "price change."
        ),
    },
    TERM_MAX_ACTIONS_PER_RUN: {
        "direction": "max",
        "unit": "count",
        "label": "Actions per run",
        "signal": "cost",
        "enforced": True,
        "graded": False,
        "help": (
            "The most actions one run may take. An action is a governed step "
            "that is not a model call: a tool call, a skill execution or an "
            "agent-to-agent hop. A model call is counted by "
            "max_llm_calls_per_run and not here. Enforced at the gateway: the "
            "step that would pass the ceiling is refused, and a step another "
            "check refuses does not count. A whole number of at least 1. "
            "Enforced and not graded: the Assurance Report states the ceiling "
            "and has no recorded worst case to compare it with."
        ),
    },
    TERM_MAX_NEW_RUNS_PER_WINDOW: {
        "direction": "max",
        "unit": "count",
        "label": "New runs per credential in a window",
        "signal": "cost",
        "enforced": True,
        "graded": False,
        "help": (
            "The most runs one credential (an agent identity, otherwise an "
            "API key) may start in a window while it acts in this system. A "
            "new run is the first governed call that carries a run id the "
            "gateway has not seen from that credential in the window; further "
            "calls of the same run do not count again. It bounds how many "
            "runs there are, so a caller cannot stay under every per-run "
            "ceiling by putting each step in a run of its own. The window is "
            "new_runs_window_seconds, an hour (3600) when that is not "
            "declared. A whole number of at least 1. Enforced and not graded."
        ),
    },
    TERM_NEW_RUNS_WINDOW_SECONDS: {
        "direction": "max",
        "unit": "seconds",
        "label": "Window for new runs",
        "signal": "cost",
        "enforced": True,
        "graded": False,
        "maximum": MAX_NEW_RUNS_WINDOW_SECONDS,
        "default": DEFAULT_NEW_RUNS_WINDOW_SECONDS,
        "requires": TERM_MAX_NEW_RUNS_PER_WINDOW,
        "help": (
            "The length, in seconds, of the window max_new_runs_per_window "
            "counts in: a whole number from 1 to 2678400 (31 days); an hour "
            "(3600) when not declared. Declared only together with "
            "max_new_runs_per_window."
        ),
    },
    TERM_MAX_DEPTH_PER_RUN: {
        "direction": "max",
        "unit": "count",
        "label": "Delegation depth per run",
        "signal": "cost",
        "enforced": True,
        "help": (
            "How many agent-to-agent hops a run may chain — the 'a' in "
            "T(n·k·a). Delegation multiplies spend across systems, so a "
            "per-system cost cap is blind to it. The platform enforces an "
            "absolute ceiling of 32 hops regardless; this tightens it for "
            "this system."
        ),
    },
    TERM_MAX_DURATION_PER_RUN: {
        "direction": "max",
        "unit": "seconds",
        "label": "Active time per run",
        "signal": "reliability",
        "enforced": True,
        "help": (
            "How long one run may spend executing before further calls are "
            "refused. Excludes time awaiting a human: a call held for "
            "approval pauses the run's clock until a person decides, so an "
            "approval that takes hours neither breaches this ceiling nor "
            "blocks the approved retry. Catches the stall that burns little "
            "per call but holds a slot open forever. Start generous — set it "
            "from the observed p95 run duration on the Signals tab."
        ),
    },
    TERM_MAX_MONTHLY_COST: {
        "direction": "max",
        "unit": "usd",
        "label": "Spend over trailing 30 days",
        "signal": "cost",
        "enforced": False,
        "help": (
            "Total spend by this system over the trailing 30 days, independent of the window you are viewing. Graded, not enforced — the resource group's own budget is what blocks traffic."
        ),
    },
    TERM_MIN_COMPLETION_RATE: {
        "direction": "min",
        "unit": "rate",
        "label": "Tasks that complete",
        "signal": "reliability",
        "enforced": False,
        "help": (
            "The success rate: runs that completed in a declared outcome that counts as work, out of those plus the runs that failed or ended outside the contract. Declared 'nothing to do' runs are in neither. A floor, not a ceiling: falling below it is the breach. Reported as insufficient evidence until at least 20 such runs finished in the window."
        ),
    },
    TERM_MAX_ERROR_RATE: {
        "direction": "max",
        "unit": "rate",
        "label": "Tasks that error",
        "signal": "reliability",
        "enforced": False,
        "help": (
            "The share of decisive runs (work done, deviated or failed) that ended in an error — a policy block included. Graded over the window — a burst of errors inside one task shows up on the Signals tab, not here."
        ),
    },
    TERM_MAX_ABANDONED_RATE: {
        "direction": "max",
        "unit": "rate",
        "label": "Tasks that go quiet unfinished",
        "signal": "reliability",
        "enforced": False,
        "help": (
            "The share of tasks that went quiet without ever reporting an end. Usually a client that stopped calling rather than a fault in the system itself, which is why it is tracked separately from errors."
        ),
    },
    TERM_MAX_OUTCOME_SHARE: {
        "direction": "max",
        "unit": "rate",
        "label": "Share of runs ending in an outcome",
        "signal": "reliability",
        "enforced": False,
        "parameterized": True,
        "help": (
            "Declared as max_outcome_share:<outcome id>. The share of runs that ended in a declared outcome which ended in this one (an outcome without a variant covers all its variants). A jump in the 'nothing to do' share is how an upstream feed that silently stopped delivering shows up."
        ),
    },
    TERM_AVG_COST_PER_OUTCOME: {
        "direction": "max",
        "unit": "usd",
        "label": "Average cost of an outcome",
        "signal": "cost",
        "enforced": False,
        "parameterized": True,
        "help": (
            "Declared as avg_cost_per_outcome:<outcome id>. The mean cost of runs that ended in this declared outcome — one kind of work, never blended with the others, so a change in the mix cannot hide a change in what one outcome costs."
        ),
    },
    TERM_MAX_APPROVAL_RATE: {
        "direction": "max",
        "unit": "rate",
        "label": "Tasks escalated for human approval",
        "signal": "reliability",
        "enforced": False,
        "help": (
            "The share of tasks that had to be escalated to a human. Rising escalation is a signal the system is operating outside what it was trusted to decide alone."
        ),
    },
}

#: Enforced term -> the observed fact it is graded against.
_WORST_RUN_FACT = {
    TERM_MAX_COST_PER_RUN: "worst_cost_per_run",
    TERM_MAX_CALLS_PER_RUN: "worst_calls_per_run",
    TERM_MAX_TOKENS_PER_RUN: "worst_tokens_per_run",
    TERM_MAX_DEPTH_PER_RUN: "worst_depth_per_run",
    TERM_MAX_DURATION_PER_RUN: "worst_duration_seconds",
}

#: The actor that auto-clears a breach that healed.
SYSTEM_ACTOR_ENVELOPE = "system:envelope-evaluator"

STATUS_MET = "met"
STATUS_BREACHED = "breached"
STATUS_INSUFFICIENT = "insufficient_evidence"
#: An enforced term the ledger has no fact for (`ENFORCED_ONLY_TERMS`):
#: stated, enforced at the gateway, and neither met, breached nor
#: unevaluable in the report.
STATUS_NOT_GRADED = "not_graded"

#: Below this many finished runs in the window, a rate is noise wearing a
#: percentage sign and reports `insufficient_evidence`.
MIN_EVIDENCE_RUNS = 20

#: Breach severity is magnitude, not confidence: exceeding a cap by 5% is
#: a different event from exceeding it 2×. The ratio is observed shortfall
#: or overshoot relative to the declared bound.
HIGH_SEVERITY_RATIO = 1.5



def known_terms() -> Dict[str, Dict[str, Any]]:
    """The vocabulary as the API answers it: each term with what a value
    of it must be (`validate_envelope` refuses anything else). `minimum`
    and `maximum` are inclusive; a dollar amount is greater than 0."""
    terms: Dict[str, Dict[str, Any]] = {}
    for term, spec in ENVELOPE_TERMS.items():
        unit = spec["unit"]
        terms[term] = {
            "graded": True,
            "parameterized": False,
            "whole_number": unit in ("count", "seconds"),
            "minimum": {"rate": 0.0, "count": 1.0, "seconds": 1.0}.get(unit),
            "exclusive_minimum": 0.0 if unit == "usd" else None,
            "maximum": 1.0 if unit == "rate" else None,
            "default": None,
            "requires": None,
            **spec,
        }
    return terms


class EnvelopeValidationError(ValueError):
    """A term the vocabulary does not know, or a value outside its domain."""


def split_term(key: str) -> Tuple[str, Optional[str]]:
    """`"max_outcome_share:a.b"` → ("max_outcome_share", "a.b");
    `"max_error_rate"` → ("max_error_rate", None)."""
    base, sep, param = key.partition(TERM_PARAM_SEPARATOR)
    return base, (param if sep else None)


def term_spec(key: str) -> Optional[Dict[str, Any]]:
    """The vocabulary entry of a declared key (a parameterised key resolves
    to its base term), or None for a term this code does not know."""
    return ENVELOPE_TERMS.get(split_term(key)[0])


def term_label(key: str) -> str:
    base, param = split_term(key)
    spec = ENVELOPE_TERMS.get(base) or {}
    label = spec.get("label", base)
    return f"{label}: {param}" if param else label


def validate_envelope(raw: Optional[Dict[str, Any]]) -> Optional[Dict[str, float]]:
    """Normalize and validate a declared envelope.

    Returns a plain {term: float} dict (stable for hashing), or None for
    an empty declaration. Unknown terms are refused rather than stored:
    a term nobody evaluates is a promise nobody keeps, and it would still
    render in the contract an approver signs.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise EnvelopeValidationError("operating_envelope must be an object")

    normalized: Dict[str, float] = {}
    for key, value in raw.items():
        spec = term_spec(str(key))
        if spec is None:
            raise EnvelopeValidationError(
                f"unknown envelope term {key!r}; known terms: "
                f"{sorted(ENVELOPE_TERMS)}"
            )
        base, param = split_term(str(key))
        if spec.get("parameterized"):
            if not param or not OUTCOME_ID_RE.match(param):
                raise EnvelopeValidationError(
                    f"{base} names an outcome: declare it as "
                    f"'{base}{TERM_PARAM_SEPARATOR}<outcome>[.<variant>]', got {key!r}"
                )
        elif param is not None:
            raise EnvelopeValidationError(f"{base} takes no outcome parameter, got {key!r}")
        if isinstance(value, bool):
            raise EnvelopeValidationError(f"{key} must be a number, got {value!r}")
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise EnvelopeValidationError(f"{key} must be a number, got {value!r}")
        if spec["unit"] == "rate" and not 0.0 <= number <= 1.0:
            raise EnvelopeValidationError(f"{key} must be between 0 and 1")
        if spec["unit"] == "usd" and number <= 0:
            raise EnvelopeValidationError(f"{key} must be a positive dollar amount")
        if spec["unit"] in ("count", "seconds"):
            if number < 1:
                raise EnvelopeValidationError(
                    f"{key} must be at least 1"
                )
            if number != int(number):
                raise EnvelopeValidationError(
                    f"{key} must be a whole number, got {value!r}"
                )
            number = float(int(number))
        normalized[key] = number

    for key, number in normalized.items():
        spec = term_spec(key)
        maximum = spec.get("maximum")
        if maximum is not None and number > maximum:
            raise EnvelopeValidationError(f"{key} must be at most {maximum} (31 days)")
        requires = spec.get("requires")
        if requires is not None and requires not in normalized:
            raise EnvelopeValidationError(
                f"{key} is the window of {requires}: declare that too, or neither"
            )

    return normalized or None


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _observed_facts(
    db: Session, tenant_id: str, ai_system_id: str, since: Optional[datetime]
) -> Dict[str, Any]:
    # Every per-run fact is graded over JUDGED runs only (THE
    # exclusion predicate: a run inside an incident window counts for
    # nothing here, and stays in the ledger as evidence). `since` already
    # carries any baseline-epoch floor of the term being graded.
    graded = (
        AISystemRun.tenant_id == tenant_id,
        AISystemRun.ai_system_id == ai_system_id,
        AISystemRun.is_replay.is_(False),
        judged_run(AISystemRun, purpose=PURPOSE_RATE),
        *((AISystemRun.started_at >= since,) if since is not None else ()),
    )
    finished = int(
        db.query(func.count(AISystemRun.id))
        .filter(*graded, AISystemRun.terminal_state.isnot(None))
        .scalar()
        or 0
    )

    def _count(*conds) -> int:
        return int(db.query(func.count(AISystemRun.id)).filter(*graded, *conds).scalar() or 0)

    # THE classification. A no-work run is never evidence for a
    # rate; the completion and error rates are over the success-rate
    # denominator (success + deviation + failed).
    not_no_work = ~class_cond(RUN_NO_WORK)
    decisive = _count(classes_cond(RATE_CLASSES))
    completed = _count(class_cond(RUN_SUCCESS))
    errored = _count(class_cond(RUN_FAILED))
    finished_work = _count(AISystemRun.terminal_state.isnot(None), not_no_work)
    abandoned = _count(AISystemRun.terminal_state == "abandoned")

    escalated = _count(
        AISystemRun.terminal_state.isnot(None),
        not_no_work,
        AISystemRun.approval_request_count > 0,
    )

    completed_cost, completed_for_cost = (
        db.query(func.sum(AISystemRun.total_cost_usd), func.count(AISystemRun.id))
        .filter(*graded, class_cond(RUN_SUCCESS))
        .first()
        or (None, 0)
    )

    # The monthly-spend term is deliberately a trailing 30-day sum,
    # independent of the report window: "under $500/month" is a claim about
    # the last month whether the reader asked for a day or a quarter. It is
    # money actually spent — a sum, not a judgement of behaviour — so an
    # incident window does not remove it (excluded from judgement,
    # never hidden).
    month_ago = datetime.now(timezone.utc) - timedelta(days=30)
    monthly = db.query(func.sum(AISystemRun.total_cost_usd)).filter(
        AISystemRun.tenant_id == tenant_id,
        AISystemRun.ai_system_id == ai_system_id,
        AISystemRun.is_replay.is_(False),
        AISystemRun.started_at >= month_ago,
    ).scalar()

    # Worst single run in the window. The enforced caps are ceilings for
    # ONE run, so they are graded against the maximum, never a mean — an
    # average is exactly what let a runaway hide.
    #
    # Duration is the ACTIVE figure: time a run spent parked on a
    # human approval is governance, not runtime. Grading elapsed time made a
    # decision approved 13 h later breach a 300 s ceiling, and the response
    # policy then demoted the system's autonomy for having asked a person.
    # The gateway enforces the same figure live (run_budget.rs).
    w_cost, w_calls, w_tokens, w_depth, w_ms = db.query(
        func.max(AISystemRun.total_cost_usd),
        func.max(AISystemRun.llm_call_count),
        func.max(AISystemRun.total_tokens),
        func.max(AISystemRun.max_delegation_depth),
        func.max(AISystemRun.active_duration_ms),
    ).filter(*graded, AISystemRun.terminal_state.isnot(None)).first() or (
        None, None, None, None, None,
    )

    # Disclosed, never graded: finished runs the windows excluded.
    excluded_q = db.query(func.count(AISystemRun.id)).filter(
        AISystemRun.tenant_id == tenant_id,
        AISystemRun.ai_system_id == ai_system_id,
        AISystemRun.is_replay.is_(False),
        AISystemRun.terminal_state.isnot(None),
        ~judged_run(AISystemRun, purpose=PURPOSE_RATE),
    )
    if since is not None:
        excluded_q = excluded_q.filter(AISystemRun.started_at >= since)
    excluded = int(excluded_q.scalar() or 0)

    def _f(v) -> Optional[float]:
        return float(v) if v is not None else None

    return {
        "finished": finished,
        "decisive": decisive,
        "finished_work": finished_work,
        "excluded_runs": excluded,
        "worst_cost_per_run": _f(w_cost),
        "worst_calls_per_run": _f(w_calls),
        "worst_tokens_per_run": _f(w_tokens),
        "worst_depth_per_run": _f(w_depth),
        "worst_duration_seconds": (float(w_ms) / 1000.0) if w_ms is not None else None,
        "completed": completed,
        "errored": errored,
        "abandoned": abandoned,
        "escalated": escalated,
        "cost_per_processed_outcome": (
            float(completed_cost) / completed_for_cost
            if completed_cost is not None and completed_for_cost
            else None
        ),
        "completed_for_cost": int(completed_for_cost or 0),
        "monthly_cost": float(monthly) if monthly is not None else 0.0,
    }


def _outcome_match(outcome_id: str):
    """Runs whose reported outcome is `outcome_id` — an id without a variant
    covers every variant of the outcome."""
    if "." in outcome_id:
        return AISystemRun.outcome_id == outcome_id
    return or_(
        AISystemRun.outcome_id == outcome_id,
        AISystemRun.outcome_id.like(f"{outcome_id}.%"),
    )


def _outcome_facts(
    db: Session, tenant_id: str, ai_system_id: str, since: Optional[datetime], outcome_id: str
) -> Dict[str, Any]:
    """The facts a parameterised outcome term grades: of the runs that ended
    in a DECLARED outcome (success + no_work), how many ended in this one,
    and what those cost."""
    graded = (
        AISystemRun.tenant_id == tenant_id,
        AISystemRun.ai_system_id == ai_system_id,
        AISystemRun.is_replay.is_(False),
        judged_run(AISystemRun, purpose=PURPOSE_RATE),
        classes_cond((RUN_SUCCESS, RUN_NO_WORK)),
        *((AISystemRun.started_at >= since,) if since is not None else ()),
    )
    declared = int(db.query(func.count(AISystemRun.id)).filter(*graded).scalar() or 0)
    n, cost = (
        db.query(func.count(AISystemRun.id), func.sum(AISystemRun.total_cost_usd))
        .filter(*graded, _outcome_match(outcome_id))
        .first()
        or (0, None)
    )
    n = int(n or 0)
    return {
        "declared_runs": declared,
        "outcome_runs": n,
        "share": (n / declared) if declared else None,
        "avg_cost": (float(cost or 0) / n) if n else None,
    }


def _fmt(value: float, unit: str, *, precise: bool = False) -> str:
    """Render a bound or an observation in its own unit."""
    if unit == "usd":
        return f"${value:,.4f}" if precise else f"${value:,.2f}"
    if unit == "rate":
        return f"{value:.1%}"
    if unit == "seconds":
        return f"{value:,.0f}s"
    return f"{value:,.0f}"


def _severity(declared: float, observed: float, direction: str) -> str:
    if declared <= 0:
        return "high"
    ratio = (observed / declared) if direction == "max" else (
        (declared - observed) / declared + 1.0
    )
    return "high" if ratio >= HIGH_SEVERITY_RATIO else "medium"


def evaluate_envelope(
    db: Session,
    system: ResourceGroup,
    *,
    period_hours: int,
    envelope: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Score every declared term against the run ledger.

    `envelope` overrides the system's live declaration — the report passes
    the envelope from the **active contract**, because assurance is against
    what was signed off, not against an edit nobody has approved yet.

    Returns None when nothing is declared: no envelope is a different fact
    from an envelope with zero breaches, and the report says which.
    """
    declared = validate_envelope(
        envelope if envelope is not None else system.operating_envelope
    )
    if not declared:
        return None

    since = (
        datetime.now(timezone.utc) - timedelta(hours=period_hours)
        if period_hours > 0
        else None
    )
    base = _observed_facts(db, system.tenant_id, system.id, since)
    # An `accepted_change` / `false_positive` resolution of a
    # term's breach starts a baseline epoch on `envelope:<term>`: that term
    # is graded only on runs started at or after it. One fact query per
    # distinct floor (usually one).
    floors = epoch_floors(
        db, system.tenant_id, system.id, [envelope_metric_key(k) for k in declared]
    )
    facts_at: Dict[Optional[datetime], Dict[str, Any]] = {since: base}

    terms: List[Dict[str, Any]] = []
    for key, bound in declared.items():
        spec = term_spec(key)
        base_term, param = split_term(key)
        floor = floors.get(envelope_metric_key(key))
        effective = since_with_floor(since, floor)
        if effective not in facts_at:
            facts_at[effective] = _observed_facts(db, system.tenant_id, system.id, effective)
        facts = facts_at[effective]
        finished = facts["finished"]
        observed: Optional[float]
        enough = True

        if base_term in (TERM_MAX_OUTCOME_SHARE, TERM_AVG_COST_PER_OUTCOME):
            of = _outcome_facts(db, system.tenant_id, system.id, effective, param or "")
            if base_term == TERM_MAX_OUTCOME_SHARE:
                observed = of["share"]
                enough = of["declared_runs"] >= MIN_EVIDENCE_RUNS
            else:
                observed = of["avg_cost"]
                enough = of["outcome_runs"] >= MIN_EVIDENCE_RUNS
        elif key == TERM_MAX_MONTHLY_COST:
            observed = facts["monthly_cost"]
        elif key == TERM_MAX_AVG_COST_PER_TASK:
            observed = facts["cost_per_processed_outcome"]
            enough = facts["completed_for_cost"] >= MIN_EVIDENCE_RUNS
        elif key in ENFORCED_ONLY_TERMS:
            observed = None
        elif key in _WORST_RUN_FACT:
            # A ceiling for one run needs exactly one run to be tested
            # against: the evidence floor that guards rates would suppress
            # a real breach just because the window was quiet.
            observed = facts[_WORST_RUN_FACT[key]]
            enough = observed is not None
        else:
            # (numerator, denominator): THE success-rate denominator for the
            # completion and error rates; every finished run but the
            # no-work ones for abandonment and escalation.
            ratio = {
                TERM_MIN_COMPLETION_RATE: (facts["completed"], facts["decisive"]),
                TERM_MAX_ERROR_RATE: (facts["errored"], facts["decisive"]),
                TERM_MAX_ABANDONED_RATE: (facts["abandoned"], facts["finished_work"]),
                TERM_MAX_APPROVAL_RATE: (facts["escalated"], facts["finished_work"]),
            }
            numerator, denominator = ratio[key]
            observed = (numerator / denominator) if denominator else None
            enough = denominator >= MIN_EVIDENCE_RUNS

        if key in ENFORCED_ONLY_TERMS:
            status, severity, detail = STATUS_NOT_GRADED, None, (
                "enforced at the gateway on every call; no worst case is recorded "
                "to grade it against"
            )
        elif observed is None or not enough:
            status, severity, detail = STATUS_INSUFFICIENT, None, (
                f"fewer than {MIN_EVIDENCE_RUNS} qualifying runs in the "
                f"window — not evaluated, and not counted as met"
            )
        elif (spec["direction"] == "max" and observed <= bound) or (
            spec["direction"] == "min" and observed >= bound
        ):
            status, severity, detail = STATUS_MET, None, None
        else:
            severity = _severity(bound, observed, spec["direction"])
            status = STATUS_BREACHED
            shown = _fmt(observed, spec["unit"], precise=True)
            bound_shown = _fmt(bound, spec["unit"])
            comparator = "above the declared maximum" if spec["direction"] == "max" \
                else "below the declared minimum"
            detail = f"observed {shown}, {comparator} of {bound_shown}"

        terms.append(
            {
                "term": key,
                "label": term_label(key),
                "unit": spec["unit"],
                "direction": spec["direction"],
                "declared": bound,
                "observed": observed,
                "status": status,
                "severity": severity,
                "detail": detail,
                # The report and the form both need to say which terms
                # stop traffic and which only grade it.
                "enforced": bool(spec.get("enforced")),
                # Graded from this epoch on (None: the period).
                "epoch_starts_at": floor.isoformat() if floor is not None else None,
            }
        )

    return {
        "terms": terms,
        "breached": sum(1 for t in terms if t["status"] == STATUS_BREACHED),
        "insufficient": sum(1 for t in terms if t["status"] == STATUS_INSUFFICIENT),
        "evaluated_runs": base["finished"],
        # Finished runs in the period an incident window excluded.
        "excluded_runs": base["excluded_runs"],
        "period_hours": period_hours,
    }


def envelope_metric_key(term: str) -> str:
    """The metric key a term's drift events and epochs carry."""
    return f"{ENVELOPE_METRIC_PREFIX}{term}"


# ---------------------------------------------------------------------------
# Violation events — the bridge into the closed loop
# ---------------------------------------------------------------------------

#: Re-exported from the drift model, which owns the cause vocabulary.
from app.services.assurance_inbox import AssuranceInboxService
from app.database.models.ai_system_drift import (  # noqa: E402
    CAUSE_CONTRACT_BREACH,
)


def emit_envelope_violations(
    db: Session, system: ResourceGroup, evaluation: Optional[Dict[str, Any]]
) -> int:
    """Record one open `contract` drift event per breached term.

    Deduped against open events (active/acknowledged) on the same
    `envelope:<term>` metric key, because a standing breach is re-observed
    on every tick and a feed with one event per tick per breach is a feed
    nobody reads. When the term is back under its bound the open event is
    auto-resolved with a note — a breach that healed is a different record
    from a breach someone dismissed.

    Returns how many events were newly created. Response policies pick
    these up through the ordinary findings loop; nothing here actuates.
    """
    from ulid import ULID

    from app.database.models.ai_system_drift import (
        DRIFT_CONTRACT,
        AISystemDriftEvent,
    )

    if evaluation is None:
        return 0

    open_events = {
        e.metric_key: e
        for e in db.query(AISystemDriftEvent)
        .filter(
            AISystemDriftEvent.tenant_id == system.tenant_id,
            AISystemDriftEvent.ai_system_id == system.id,
            AISystemDriftEvent.drift_class == DRIFT_CONTRACT,
            AISystemDriftEvent.status.in_(("active", "acknowledged")),
        )
        .all()
    }

    created = 0
    for term in evaluation["terms"]:
        metric_key = envelope_metric_key(term["term"])
        existing = open_events.get(metric_key)

        if term["status"] == STATUS_BREACHED:
            if existing is not None:
                continue  # standing breach, already on record
            db.add(
                AISystemDriftEvent(
                    id=f"drift-{ULID()}",
                    tenant_id=system.tenant_id,
                    ai_system_id=system.id,
                    drift_class=DRIFT_CONTRACT,
                    metric_key=metric_key,
                    baseline_value=float(term["declared"]),
                    observed_value=(
                        float(term["observed"])
                        if term["observed"] is not None
                        else None
                    ),
                    direction="up" if term["direction"] == "max" else "down",
                    severity=term["severity"] or "medium",
                    attributed_cause=CAUSE_CONTRACT_BREACH,
                    cause_evidence={
                        "term": term["term"],
                        "label": term["label"],
                        "declared": term["declared"],
                        "observed": term["observed"],
                        "detail": term["detail"],
                        "evaluated_runs": evaluation["evaluated_runs"],
                        "period_hours": evaluation["period_hours"],
                    },
                )
            )
            created += 1
            # A breached contract term is a human-review finding.
            # Deduped by term fingerprint; recurrence bumps the open item.
            AssuranceInboxService().raise_item(
                db,
                tenant_id=system.tenant_id,
                ai_system_id=system.id,
                category="contract_violation",
                severity="critical",
                fingerprint=f"contract_violation:{system.id}:{term['term']}",
                title=(
                    f"Contract term breached: {term['label']} "
                    f"(declared {term['declared']}, observed {term['observed']})"
                ),
                evidence={
                    "term": term["term"],
                    "declared": term["declared"],
                    "observed": term["observed"],
                    "period_hours": evaluation["period_hours"],
                },
            )
        elif term["status"] == STATUS_MET and existing is not None:
            # Closed by the control plane because it cleared:
            # `auto_cleared`, no effect on detection.
            from app.services.drift_resolution import auto_clear

            auto_clear(
                existing,
                by=SYSTEM_ACTOR_ENVELOPE,
                note="Term back within its declared bound on a later evaluation.",
            )
        # insufficient_evidence leaves any open event standing: losing
        # the evidence to judge a breach is not the same as healing it.

    if created:
        db.flush()
    return created


__all__ = [
    "CAUSE_CONTRACT_BREACH",
    "ENVELOPE_TERMS",
    "known_terms",
    "TERM_AVG_COST_PER_OUTCOME",
    "TERM_MAX_OUTCOME_SHARE",
    "split_term",
    "term_label",
    "term_spec",
    "EnvelopeValidationError",
    "MIN_EVIDENCE_RUNS",
    "STATUS_BREACHED",
    "STATUS_INSUFFICIENT",
    "STATUS_MET",
    "emit_envelope_violations",
    "envelope_metric_key",
    "evaluate_envelope",
    "validate_envelope",
]
