# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Semantic Governance Policy (SGP) — natural-language, LLM-judged policy

Brutor's other policies are deterministic (agent-grant globs, regex argument
policies, resource-group ACLs). SGP adds an *intent-based* layer: an operator
writes a constraint in natural language ("agents may not send customer PII to
external peers") and an LLM judge evaluates each governed request's content +
context against it at runtime.

Binding mirrors guardrail configs: a policy with NO resource-group association
is the tenant default; group-scoped policies apply only to those groups.

Mode follows how the big products ship intent policy (Google SGP DRY_RUN vs
ENFORCE): `shadow` records a would-block verdict on proxy_logs without blocking;
`enforce` blocks (or warns). The judge runs on the tenant's default model unless
`judge_model_id` pins a (cheaper/faster) model per policy.

Vocabularies are module constants + `@validates` (house style — not pg ENUMs).

Association table:
    resource_group_semantic_policies  (resource_group_id, semantic_policy_id)
"""

from typing import Any, Dict

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship, validates
from sqlalchemy.sql import func

from app.database import Base

# ── Vocabularies ─────────────────────────────────────────────────────────

# What happens when the judge finds a violation.
POLICY_ACTION_BLOCK = "block"
POLICY_ACTION_WARN = "warn"
POLICY_ACTIONS = frozenset({POLICY_ACTION_BLOCK, POLICY_ACTION_WARN})

# Rollout mode. shadow = log a would-block verdict, never block; enforce = act.
POLICY_MODE_SHADOW = "shadow"
POLICY_MODE_ENFORCE = "enforce"
POLICY_MODES = frozenset({POLICY_MODE_SHADOW, POLICY_MODE_ENFORCE})

# Surface tokens a policy may target — the agent action ingress/egress points
# where the SGP runtime hook is wired. Output surfaces are intentionally
# excluded: an LLM-judge needs the full content (awkward on streaming) and
# overlaps with the existing output guardrails + redaction; intent policy is
# enforced on what an agent SENDS/INVOKES, not on generated responses.
POLICY_SURFACES = frozenset({
    "chat_input",
    "mcp_input",
    "a2a_inbound",
    "a2a_outbound",
    "skill_input",
})


resource_group_semantic_policies = Table(
    "resource_group_semantic_policies",
    Base.metadata,
    Column(
        "resource_group_id",
        String(255),
        ForeignKey("resource_groups.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "semantic_policy_id",
        String(255),
        ForeignKey("semantic_policies.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    comment="Associates semantic governance policies with resource groups",
)


class SemanticPolicy(Base):
    """A natural-language governance policy, evaluated by an LLM judge."""

    __tablename__ = "semantic_policies"

    id = Column(String(255), primary_key=True)  # sgp-<ULID>
    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)

    # The natural-language constraint the judge evaluates against.
    constraint_text = Column(Text, nullable=False)

    # block | warn — what enforce mode does on a violation.
    action = Column(
        String(16), nullable=False, default=POLICY_ACTION_BLOCK,
        server_default=POLICY_ACTION_BLOCK,
    )
    # shadow | enforce — rollout mode.
    mode = Column(
        String(16), nullable=False, default=POLICY_MODE_SHADOW,
        server_default=POLICY_MODE_SHADOW,
    )

    # Surface tokens this policy applies to (JSON array of POLICY_SURFACES).
    # SQL NULL = every surface. The core's `applies_to` reads an empty array as
    # "matches no surface", a policy that silently never runs, so `_v_surfaces`
    # never lets one be written.
    surfaces = Column(JSONB(none_as_null=True), nullable=True)

    # Optional per-policy judge model (an llm_models.id). NULL → tenant default.
    judge_model_id = Column(String(255), nullable=True)

    # What happens when the judge cannot render a verdict — model error,
    # timeout, unparseable reply, or no resolvable judge model at all.
    #
    # FALSE (default) = fail closed: the call is refused. A policy that
    # silently stopped evaluating is indistinguishable, from the outside,
    # from a policy that approved everything.
    #
    # TRUE = log-and-allow: the operator has decided that a flaky judge must
    # not block traffic for this policy. Still alerts either way — a fail-open
    # that is invisible is the failure mode this design exists to avoid.
    #
    # Note `mode = shadow` is a *different* control: shadow never blocks on a
    # real violation. This one is only about the judge being unavailable.
    fail_open = Column(
        Boolean, nullable=False, default=False, server_default="false",
    )

    enabled = Column(Boolean, nullable=False, default=True, server_default="true")
    created_by = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )

    groups = relationship(
        "ResourceGroup",
        secondary=resource_group_semantic_policies,
        backref="semantic_policies",
        lazy="selectin",
    )

    __table_args__ = (
        Index("ix_semantic_policies_tenant", "tenant_id"),
        Index("ix_semantic_policies_tenant_enabled", "tenant_id", "enabled"),
    )

    @validates("action")
    def _v_action(self, _k, v):
        if v not in POLICY_ACTIONS:
            raise ValueError(f"Invalid action {v!r}; expected {sorted(POLICY_ACTIONS)}")
        return v

    @validates("mode")
    def _v_mode(self, _k, v):
        if v not in POLICY_MODES:
            raise ValueError(f"Invalid mode {v!r}; expected {sorted(POLICY_MODES)}")
        return v

    @validates("surfaces")
    def _v_surfaces(self, _k, v):
        if v is None or v == []:
            return None
        if not isinstance(v, list):
            raise ValueError("surfaces must be a list of surface tokens")
        bad = [s for s in v if s not in POLICY_SURFACES]
        if bad:
            raise ValueError(f"Invalid surfaces {bad}; expected subset of {sorted(POLICY_SURFACES)}")
        return v

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "description": self.description,
            "constraint_text": self.constraint_text,
            "action": self.action,
            "mode": self.mode,
            "surfaces": self.surfaces or [],
            "judge_model_id": self.judge_model_id,
            "fail_open": self.fail_open,
            "enabled": self.enabled,
            "resource_group_ids": [g.id for g in self.groups] if self.groups else [],
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
