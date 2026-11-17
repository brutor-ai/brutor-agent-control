# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# AI System run segments — the per-system slice of a run

Root-plus-via keeps one run per task, attributed to the root system, so
cost is never double-counted. The per-system slice comes from segments: a
**segment** is one AI System's share of a run in
which it appears other than as root.

Written by the Rust core's run ledger, in the same aggregation that
finalises the run; deleted and rewritten when the run reopens. Replay runs
write none. Read by the control plane (liveness of on-demand services,
conformance, the Composition tab).

Conservation: for every run, root-own + sum(segments) equals
the run's totals for ``action_count``, ``total_tokens`` and ``cost_usd``.
A root system's OWN share is therefore the run minus its segments.
"""

from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import (
    BigInteger, CheckConstraint, Column, DateTime, ForeignKey, Index, Integer,
    Numeric, SmallInteger, String, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from app.database import Base

SEGMENT_ANSWERED = "answered"
SEGMENT_FAILED = "failed"
SEGMENT_REJECTED = "rejected"
SEGMENT_TIMED_OUT = "timed_out"
#: There is no `blocked` segment outcome: a delegated call the
#: gateway refused is `failed` with segment error_cause `blocked_by_policy`.
SEGMENT_OUTCOMES = (
    SEGMENT_ANSWERED, SEGMENT_FAILED, SEGMENT_REJECTED, SEGMENT_TIMED_OUT,
)
#: Outcomes that mean the service did not answer — the only ones that make
#: an on-demand service *silent*. `rejected` (the service
#: declined the task) is an answer.
SEGMENT_UNANSWERED_OUTCOMES = frozenset({SEGMENT_FAILED, SEGMENT_TIMED_OUT})


class AISystemRunSegment(Base):
    """One AI System's share of a run it did not root."""

    __tablename__ = "ai_system_run_segments"

    id = Column(String(64), primary_key=True)  # seg-<ULID>
    run_id = Column(
        String(64), ForeignKey("ai_system_runs.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id = Column(
        String(255), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    root_task_id = Column(String(255), nullable=False)
    root_system_id = Column(
        String(255), ForeignKey("resource_groups.id", ondelete="SET NULL"), nullable=True
    )
    segment_system_id = Column(
        String(255), ForeignKey("resource_groups.id", ondelete="CASCADE"), nullable=False
    )
    #: The IMMEDIATE calling AI System: the nearest
    #: AI System of the group of the call that entered this segment (in
    #: A → B → C, C's caller is B; `root_system_id` stays A — which run it
    #: belongs to). NULL when that caller is in no AI System. THE definition
    #: of "caller" everywhere: accepted-caller enforcement, used-by,
    #: dependents, the unaccepted-caller finding, per-caller baselines.
    caller_system_id = Column(
        String(255),
        ForeignKey(
            "resource_groups.id",
            name="fk_ai_system_run_segments_caller_system",
            ondelete="SET NULL",
        ),
        nullable=True,
        comment="Immediate calling AI System (nearest AI System of the entering call's group)",
    )
    min_depth = Column(SmallInteger, nullable=False, default=0, server_default="0")
    first_action_at = Column(DateTime(timezone=True), nullable=False)
    last_action_at = Column(DateTime(timezone=True), nullable=False)
    active_ms = Column(Integer, nullable=False, default=0, server_default="0")
    action_count = Column(Integer, nullable=False, default=0, server_default="0")
    llm_call_count = Column(Integer, nullable=False, default=0, server_default="0")
    tool_call_count = Column(Integer, nullable=False, default=0, server_default="0")
    error_count = Column(Integer, nullable=False, default=0, server_default="0")
    tool_error_count = Column(Integer, nullable=False, default=0, server_default="0")
    guardrail_block_count = Column(Integer, nullable=False, default=0, server_default="0")
    policy_denial_count = Column(Integer, nullable=False, default=0, server_default="0")
    approval_request_count = Column(Integer, nullable=False, default=0, server_default="0")
    total_tokens = Column(BigInteger, nullable=False, default=0, server_default="0")
    cost_usd = Column(Numeric(18, 8), nullable=False, default=0, server_default="0")
    models_used = Column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'"),
        comment='{"<model>": <call count>}',
    )
    tool_set_signature = Column(String(64), nullable=True)
    caller_agent_id = Column(
        String(255), nullable=True,
        comment="Actor whose delegation entered the segment",
    )
    a2a_task_state = Column(
        String(32), nullable=True,
        comment="Terminal A2A task state the caller received",
    )
    outcome = Column(String(16), nullable=False)
    #: The delegate's own declared outcome, as it reported it
    #: (response header or final Task metadata), judged against the
    #: DELEGATE's active contract.
    outcome_id = Column(
        String(128), nullable=True,
        comment="The delegate's X-Brutor-Outcome-Id, verbatim",
    )
    outcome_conformance = Column(
        String(16), nullable=True,
        comment="declared | undeclared | unreported (delegate's contract)",
    )
    error_cause = Column(
        String(32), nullable=True,
        comment="Why the delegated call failed (blocked_by_policy, "
        "agent_unreachable, delegate_deviated, …)",
    )
    #: Stopped by a hold, not a refusal: the approval request it waits
    #: on. Set only with `failed` + `blocked_by_policy` (the stored refusal
    #: older readers still read); the connection line judges the hold by it.
    held_approval_request_id = Column(
        String(64), nullable=True,
        comment="The approval request a segment stopped only by a hold waits on "
        "(tool_approval_requests.id); NULL = not held",
    )
    #: The delegate's release, from the segment's entering
    #: calls: a shared service's releases are visible to its callers.
    agent_impl_name = Column(
        String(64), nullable=True,
        comment="The delegate's release name (from the entering calls)",
    )
    agent_impl_version = Column(String(64), nullable=True)
    agent_impl_build = Column(String(128), nullable=True)
    agent_impl_trust = Column(String(16), nullable=True, comment="verified | declared")
    #: The agent identity whose release the segment
    #: carries. The core counts the segment toward that agent's
    #: `agent_implementation_releases` row, idempotently per (run, agent).
    delegate_agent_id = Column(
        String(255), nullable=True,
        comment="Agent identity whose release the segment carries (the delegate)",
    )
    contract_id = Column(String(64), nullable=True)
    # 80, not 64: `sha256:` + 64 hex, the format ai_system_contracts and
    # ai_system_runs store.
    contract_hash = Column(String(80), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "run_id", "segment_system_id",
            name="uq_ai_system_run_segments_run_system",
        ),
        CheckConstraint(
            "outcome IN ('answered', 'failed', 'rejected', 'timed_out')",
            name="ck_ai_system_run_segments_outcome",
        ),
        CheckConstraint(
            "agent_impl_trust IN ('verified', 'declared')",
            name="ck_ai_system_run_segments_agent_impl_trust",
        ),
        Index(
            "ix_ai_system_run_segments_system_first",
            "tenant_id", "segment_system_id", "first_action_at",
        ),
        Index(
            "ix_ai_system_run_segments_root_first",
            "tenant_id", "root_system_id", "first_action_at",
        ),
        Index(
            "ix_ai_system_run_segments_system_caller",
            "tenant_id", "segment_system_id", "caller_system_id",
        ),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "root_task_id": self.root_task_id,
            "root_system_id": self.root_system_id,
            "caller_system_id": self.caller_system_id,
            "segment_system_id": self.segment_system_id,
            "min_depth": self.min_depth,
            "first_action_at": self.first_action_at.isoformat() if self.first_action_at else None,
            "last_action_at": self.last_action_at.isoformat() if self.last_action_at else None,
            "active_ms": self.active_ms,
            "action_count": self.action_count,
            "llm_call_count": self.llm_call_count,
            "tool_call_count": self.tool_call_count,
            "error_count": self.error_count,
            "tool_error_count": self.tool_error_count,
            "guardrail_block_count": self.guardrail_block_count,
            "policy_denial_count": self.policy_denial_count,
            "approval_request_count": self.approval_request_count,
            "total_tokens": int(self.total_tokens or 0),
            "cost_usd": float(self.cost_usd or 0),
            "models_used": self.models_used or {},
            "tool_set_signature": self.tool_set_signature,
            "caller_agent_id": self.caller_agent_id,
            "a2a_task_state": self.a2a_task_state,
            "outcome": self.outcome,
            "outcome_id": self.outcome_id,
            "outcome_conformance": self.outcome_conformance,
            "error_cause": self.error_cause,
            "agent_impl_name": self.agent_impl_name,
            "agent_impl_version": self.agent_impl_version,
            "agent_impl_build": self.agent_impl_build,
            "agent_impl_trust": self.agent_impl_trust,
            "delegate_agent_id": self.delegate_agent_id,
            "contract_id": self.contract_id,
            "contract_hash": self.contract_hash,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


__all__ = [
    "AISystemRunSegment",
    "SEGMENT_OUTCOMES",
    "SEGMENT_UNANSWERED_OUTCOMES",
    "SEGMENT_ANSWERED",
    "SEGMENT_FAILED",
    "SEGMENT_REJECTED",
    "SEGMENT_TIMED_OUT",
]
