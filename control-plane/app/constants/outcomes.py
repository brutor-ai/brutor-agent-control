# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Declared outcomes — the wire contract

Every value here crosses a repository boundary: the Rust core WRITES
`outcome_conformance`, `error_cause` and the run class at finalize and reads
the header; the control plane validates the catalogue, reads the columns and
grades. The literals are pinned by `tests/test_wire_contract.py` and by the
core's own pin, so neither side can change one alone.

Three questions are answered separately for every finished run:

  1. *Did it complete without errors?* — `terminal_state` (+ `error_cause`),
     the gateway's word;
  2. *Was the outcome one the contract declares?* — `outcome_conformance`,
     the gateway checking the agent's `X-Brutor-Outcome-Id`;
  3. *Does the outcome count toward the operating baseline?* —
     `outcome_counts_toward_baseline`, the contract's word.

How those three answers combine into ONE class is defined once, in
`app/services/ai_system_outcomes.py`.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

# ---------------------------------------------------------------------------
# Wire names
# ---------------------------------------------------------------------------

#: Request header on the run-closing call; response header on an A2A
#: delegate's response.
OUTCOME_ID_HEADER = "X-Brutor-Outcome-Id"
#: Response header the gateway adds to the response it returns to an A2A caller.
OUTCOME_CONFORMANCE_HEADER = "X-Brutor-Outcome-Conformance"
#: Body field of `POST /v1/runs/{root}/end`.
OUTCOME_ID_BODY_FIELD = "outcome_id"
#: Final A2A `Task` metadata keys (streamed / async delegation).
TASK_METADATA_OUTCOME_ID = "brutor.outcome.id"
TASK_METADATA_OUTCOME_CONFORMANCE = "brutor.outcome.conformance"
#: OTel root-span attribute read by OTel ingest.
OTEL_OUTCOME_ID_ATTR = "brutor.outcome.id"

#: One part of an outcome id: an outcome or a variant.
OUTCOME_PART_PATTERN = r"[a-z0-9_-]{1,48}"
#: `<outcome>` or `<outcome>.<variant>`.
OUTCOME_ID_PATTERN = rf"^{OUTCOME_PART_PATTERN}(\.{OUTCOME_PART_PATTERN})?$"
OUTCOME_ID_RE = re.compile(OUTCOME_ID_PATTERN)
OUTCOME_PART_RE = re.compile(rf"^{OUTCOME_PART_PATTERN}$")
#: A malformed value on a proxy call is stored verbatim, truncated to this.
OUTCOME_ID_MAX_STORED = 128

# ---------------------------------------------------------------------------
# Question 2 — conformance (`outcome_conformance`)
# ---------------------------------------------------------------------------

CONFORMANCE_DECLARED = "declared"
CONFORMANCE_UNDECLARED = "undeclared"
CONFORMANCE_UNREPORTED = "unreported"
OUTCOME_CONFORMANCES = (CONFORMANCE_DECLARED, CONFORMANCE_UNDECLARED, CONFORMANCE_UNREPORTED)

# ---------------------------------------------------------------------------
# Question 1 — why an execution failed (`error_cause`)
# ---------------------------------------------------------------------------
#
# Set iff terminal_state ∈ {errored, exhausted}. In the order of precedence
# the core applies when deriving the cause from the failing call (the last
# unsuccessful action before close).

CAUSE_BLOCKED_BY_POLICY = "blocked_by_policy"
CAUSE_RATE_LIMITED = "rate_limited"
CAUSE_MODEL_UNAVAILABLE = "model_unavailable"
CAUSE_MODEL_REJECTED = "model_rejected"
CAUSE_TOOL_FAILED = "tool_failed"
CAUSE_SKILL_FAILED = "skill_failed"
CAUSE_AGENT_UNREACHABLE = "agent_unreachable"
CAUSE_DELEGATE_DEVIATED = "delegate_deviated"
CAUSE_ITERATION_LIMIT = "iteration_limit"
CAUSE_CLIENT_REPORTED = "client_reported"

ERROR_CAUSES = (
    CAUSE_BLOCKED_BY_POLICY,
    CAUSE_RATE_LIMITED,
    CAUSE_MODEL_UNAVAILABLE,
    CAUSE_MODEL_REJECTED,
    CAUSE_TOOL_FAILED,
    CAUSE_SKILL_FAILED,
    CAUSE_AGENT_UNREACHABLE,
    CAUSE_DELEGATE_DEVIATED,
    CAUSE_ITERATION_LIMIT,
    CAUSE_CLIENT_REPORTED,
)

#: How health reasons and the Errors tab name a cause.
ERROR_CAUSE_LABELS: Dict[str, str] = {
    CAUSE_BLOCKED_BY_POLICY: "blocked by policy",
    CAUSE_RATE_LIMITED: "rate limited",
    CAUSE_MODEL_UNAVAILABLE: "model unavailable",
    CAUSE_MODEL_REJECTED: "model rejected the request",
    CAUSE_TOOL_FAILED: "tool failed",
    CAUSE_SKILL_FAILED: "skill failed",
    CAUSE_AGENT_UNREACHABLE: "delegate agent unreachable",
    CAUSE_DELEGATE_DEVIATED: "delegate answered outside its contract",
    CAUSE_ITERATION_LIMIT: "iteration limit reached",
    CAUSE_CLIENT_REPORTED: "client reported an error",
}

# ---------------------------------------------------------------------------
# The run class — the ONE classification (`ai_system_outcomes.run_class`)
# ---------------------------------------------------------------------------

RUN_SUCCESS = "success"
RUN_NO_WORK = "no_work"
RUN_DEVIATION = "deviation"
RUN_FAILED = "failed"
RUN_NOT_DECISIVE = "not_decisive"
RUN_CLASSES = (RUN_SUCCESS, RUN_NO_WORK, RUN_DEVIATION, RUN_FAILED, RUN_NOT_DECISIVE)

# ---------------------------------------------------------------------------
# The catalogue document (`resource_groups.declared_outcomes`, closure
# section `outcomes`)
# ---------------------------------------------------------------------------

CLOSURE_SECTION_OUTCOMES = "outcomes"
MAX_OUTCOMES = 32
MAX_VARIANTS = 32
LABEL_MAX = 128
DESCRIPTION_MAX = 2000

EXPECT_MAX_ACTIONS = "max_actions"
EXPECT_MAX_MODEL_CALLS = "max_model_calls"
EXPECT_MAX_COST_USD = "max_cost_usd"
EXPECT_REQUIRES_CAPABILITIES = "requires_capabilities"
EXPECT_FORBIDS_CAPABILITIES = "forbids_capabilities"
#: The `expect` vocabulary — anything else is a 422.
EXPECT_KEYS = (
    EXPECT_MAX_ACTIONS,
    EXPECT_MAX_MODEL_CALLS,
    EXPECT_MAX_COST_USD,
    EXPECT_REQUIRES_CAPABILITIES,
    EXPECT_FORBIDS_CAPABILITIES,
)
#: Upper bounds (`max_*`): raising one loosens what the run may do.
EXPECT_MAX_KEYS = (EXPECT_MAX_ACTIONS, EXPECT_MAX_MODEL_CALLS, EXPECT_MAX_COST_USD)
EXPECT_INT_KEYS = (EXPECT_MAX_ACTIONS, EXPECT_MAX_MODEL_CALLS)
EXPECT_LIST_KEYS = (EXPECT_REQUIRES_CAPABILITIES, EXPECT_FORBIDS_CAPABILITIES)

STARTER_TASK_COMPLETED = "task_completed"
STARTER_NOTHING_TO_DO = "nothing_to_do"


def starter_catalogue() -> List[Dict[str, Any]]:
    """The editable draft every new AI System gets: one outcome
    that is the work, one that is "nothing to do" with the no-work evidence.
    A fresh list on every call — callers store it."""
    return [
        {
            "id": STARTER_NOTHING_TO_DO,
            "label": "Nothing to do",
            "counts_toward_baseline": False,
            "expect": {
                EXPECT_MAX_ACTIONS: 2,
                EXPECT_MAX_MODEL_CALLS: 0,
                EXPECT_MAX_COST_USD: 0,
            },
        },
        {
            "id": STARTER_TASK_COMPLETED,
            "label": "Task completed",
            "counts_toward_baseline": True,
        },
    ]


__all__ = [
    "CAUSE_AGENT_UNREACHABLE",
    "CAUSE_BLOCKED_BY_POLICY",
    "CAUSE_CLIENT_REPORTED",
    "CAUSE_DELEGATE_DEVIATED",
    "CAUSE_ITERATION_LIMIT",
    "CAUSE_MODEL_REJECTED",
    "CAUSE_MODEL_UNAVAILABLE",
    "CAUSE_RATE_LIMITED",
    "CAUSE_SKILL_FAILED",
    "CAUSE_TOOL_FAILED",
    "CLOSURE_SECTION_OUTCOMES",
    "CONFORMANCE_DECLARED",
    "CONFORMANCE_UNDECLARED",
    "CONFORMANCE_UNREPORTED",
    "ERROR_CAUSES",
    "ERROR_CAUSE_LABELS",
    "EXPECT_KEYS",
    "OUTCOME_CONFORMANCES",
    "OUTCOME_CONFORMANCE_HEADER",
    "OUTCOME_ID_HEADER",
    "OUTCOME_ID_PATTERN",
    "OUTCOME_ID_RE",
    "RUN_CLASSES",
    "RUN_DEVIATION",
    "RUN_FAILED",
    "RUN_NOT_DECISIVE",
    "RUN_NO_WORK",
    "RUN_SUCCESS",
    "starter_catalogue",
]
