# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Continuous Assurance Checks — the run-facts context contract.

WIRE CONTRACT: the Rust core builds the ``run`` object tier-A check
expressions evaluate against
(`brutor-gateway-core/src/services/run_ledger.rs::RUN_FACT_KEYS`), and
this is its pinned mirror — used by the NL compile assist (so the model
proposes expressions over real fields) and the authoring UI's reference
panel. Adding a key is a minor version; removing or retyping one is
breaking. Keep BOTH lists sorted and identical.
"""

from app.constants.outcomes import ERROR_CAUSES, OUTCOME_CONFORMANCES, RUN_CLASSES
from app.database.models.ai_system_run import (
    CHAIN_BROKEN,
    CHAIN_CLIENT_ASSERTED,
    CHAIN_INTACT,
    CHAIN_OBSERVED_EXTERNAL,
    CHAIN_PARTIAL,
    TERMINAL_STATES,
)

# 2 — `client_outcome` removed (breaking), outcome_id / outcome_conformance /
# error_cause / run_class added. Matches the core.
RUN_FACTS_VERSION = 2

# Sorted, exactly as in run_ledger.rs.
RUN_FACT_KEYS = (
    "a2a_call_count",
    "action_count",
    "active_duration_ms",
    "actor_type",
    "agent_id",
    "agent_impl_build",
    "agent_impl_name",
    "agent_impl_trust",
    "agent_impl_version",
    "ai_system_id",
    "approval_request_count",
    "awaiting_human_ms",
    "cache_hit_count",
    "chain_integrity",
    "client_label",
    "completion_tokens",
    "contract_present",
    "distinct_tools",
    "duration_ms",
    "error_cause",
    "error_count",
    "fallback_count",
    "feedback_score",
    "finalized_by",
    "guardrail_block_count",
    "guardrail_trip_count",
    "implementation_fingerprint",
    "is_replay",
    "llm_call_count",
    "max_delegation_depth",
    "models",
    "outcome_conformance",
    "outcome_id",
    "policy_denial_count",
    "prompt_fingerprint",
    "prompt_tokens",
    "rag_chunks",
    "rag_collections",
    "rag_used",
    "records_touched",
    "reopened",
    "run_class",
    "run_id",
    "scope_declared",
    "scope_unbound_count",
    "scope_violation_count",
    "skill_call_count",
    "source",
    "step_count",
    "subject_id",
    "subject_kind",
    "system_kind",
    "terminal_state",
    "tokens_saved",
    "tool_call_count",
    "tool_error_count",
    "tools",
    "total_cost_usd",
    "total_tokens",
    "turn_count",
    "via_system_ids",
)

# The value list for `chain_integrity` is derived from the model's
# own vocabulary rather than restated here. The restated version had drifted:
# it named `dropped` and `sig_failed`, which are `proxy_logs.trace_continuity`
# values (and even there the value is `client_dropped`), while omitting the
# two — `observed_external` and `partial` — that evidence-provenance
# checks are actually written against. A check compiled from a wrong guide
# (`run.chain_integrity == 'sig_failed'`) compiles, evaluates false on every
# run forever, and reads to an operator as "nothing flagged" — the silent
# no-op the `check_stalled` category exists to prevent but which no
# emitter can detect here. The guide is what the NL assist and the authoring
# UI put in front of an author, so it must not be a second source of truth.
# Ordered strongest-evidence first rather than alphabetically — an author
# choosing a threshold is choosing a point on that ladder, and the order is
# the only place the ladder is visible to them. Membership is pinned against
# `CHAIN_INTEGRITIES` by tests/test_wire_contract.py, so adding a value to
# the model without adding it here fails there rather than silently.
_CHAIN_INTEGRITY_ORDER = (
    CHAIN_INTACT,
    CHAIN_CLIENT_ASSERTED,
    CHAIN_OBSERVED_EXTERNAL,
    CHAIN_PARTIAL,
    CHAIN_BROKEN,
)
_CHAIN_INTEGRITY_VALUES = " | ".join(f"'{v}'" for v in _CHAIN_INTEGRITY_ORDER)

# Derived from the one vocabulary (`app/constants/outcomes.py`,
# the run model), never restated; pinned by tests/test_wire_contract.py.
_CONFORMANCE_VALUES = " | ".join(f"'{v}'" for v in OUTCOME_CONFORMANCES)
_RUN_CLASS_VALUES = " | ".join(f"'{v}'" for v in RUN_CLASSES)
_ERROR_CAUSE_VALUES = " | ".join(f"'{v}'" for v in ERROR_CAUSES)
_TERMINAL_STATE_VALUES = " | ".join(f"'{v}'" for v in sorted(TERMINAL_STATES))

# Human-readable field guide, embedded in the NL compile-assist prompt and
# the authoring UI. Types: str | int | float | bool | list[str].
RUN_FACT_GUIDE = {
    "run_id": "str — ledger run id",
    "ai_system_id": "str — owning AI System ('' when unattributed)",
    "system_kind": "str — e.g. 'agent', 'integration' ('' when unattributed)",
    "via_system_ids": "list[str] — other AI Systems this run touched",
    "actor_type": "str — 'agent_identity' | 'end_user' | 'api_key' | 'scheduler' | ''",
    "agent_id": "str — agent identity when actor_type='agent_identity'",
    "subject_id": "str — who the actor acted FOR (delegation subject); '' when none",
    "subject_kind": "str — 'user' | 'agent' | 'service' | ''",
    "prompt_fingerprint": "str — SHA-256 of the instructions the run was given; '' when none",
    "outcome_id": "str — the agent's X-Brutor-Outcome-Id as reported ('<outcome>[.<variant>]'); '' when none",
    "outcome_conformance": f"str — the outcome judged against the active contract: {_CONFORMANCE_VALUES}",
    "run_class": f"str — THE run classification: {_RUN_CLASS_VALUES}",
    "error_cause": f"str — why the run failed (errored / exhausted only): {_ERROR_CAUSE_VALUES} | ''",
    "rag_collections": "list[str] — KB collection ids the run retrieved from",
    "source": "str — where the steps were witnessed: 'gateway' | 'otel' (ingested telemetry) | 'mixed'",
    "terminal_state": f"str — {_TERMINAL_STATE_VALUES}",
    "finalized_by": "str — 'explicit' | 'terminal' | 'idle' | 'sweeper'",
    "chain_integrity": f"str — {_CHAIN_INTEGRITY_VALUES}",
    "contract_present": "bool — an active contract governed this run",
    "reopened": "bool — the run was reopened by late actions",
    "is_replay": "bool — replay runs are excluded from live checks",
    "duration_ms": "int — elapsed ms, first action to last (includes time awaiting a human)",
    "active_duration_ms": "int — duration_ms minus awaiting_human_ms: the system's own execution time",
    "awaiting_human_ms": "int — ms parked awaiting a human approval decision (overlapping holds counted once)",
    "action_count": "int — leaf actions (proxied calls)",
    "step_count": "int — client-asserted phases (0 = none asserted)",
    "turn_count": "int — agent-loop passes",
    "llm_call_count": "int",
    "tool_call_count": "int",
    "skill_call_count": "int",
    "a2a_call_count": "int",
    "max_delegation_depth": "int",
    "distinct_tools": "int",
    "total_tokens": "int",
    "prompt_tokens": "int",
    "completion_tokens": "int",
    "total_cost_usd": "float",
    "cache_hit_count": "int",
    "tokens_saved": "int",
    "error_count": "int",
    "fallback_count": "int",
    "tool_error_count": "int",
    "guardrail_trip_count": "int",
    "guardrail_block_count": "int",
    "policy_denial_count": "int",
    "approval_request_count": "int",
    "rag_used": "bool",
    "rag_chunks": "int",
    "client_label": "str — the normalised client label of the run's first step; '' when none",
    # The release the run's actor declared. Descriptive facts only:
    # a check may flag a release, nothing may authorise on one.
    "agent_impl_name": "str — implementation name the run's actor declared; '' when none",
    "agent_impl_version": "str — implementation version the run's actor declared, e.g. '1.4.2'; '' when none",
    "agent_impl_build": "str — build / digest the run's actor declared; '' when none",
    "agent_impl_trust": "str — how much the declaration is worth: 'verified' | 'declared' | '' (none)",
    "implementation_fingerprint": "str — 'sha256:…' over the actor's first software surface; '' when none",
    "models": "list[str] — model names this run used",
    "scope_declared": "bool — the run declared a data scope",
    "records_touched": "int — distinct records the run's tool calls named under a data-scope rule",
    "scope_violation_count": "int — calls outside the run's scope: out of scope, unscoped tool, missing argument or a changed declaration",
    "scope_unbound_count": "int — calls a rule covers in a run that declared no value for its key",
    "tools": "list[str] — tools this run called or tried to call, sorted; [] on runs finalized before tools were recorded",
    "feedback_score": "int — summed portal thumbs (0 = none)",
}
