# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Agent Identity — agents as first-class governable principals

Brutor is the decision/enforce/evidence plane for agent identity — NOT an IdP.
Issuance and lifecycle stay with the customer's IdP (Okta / Entra Agent ID /
SPIFFE); we *consume* the identity and decide, action-by-action, what the agent
is allowed to do ("actions, not access"), then prove it.

Three tables:
  * `agent_identities`            — the agent principal (subject = its human/
                                    upstream sponsor; the agent itself is the
                                    actor). Authenticates via an api_key bound
                                    to it, or an IdP workload claim
                                    (idp_server_id + idp_subject).
  * `agent_grants`               — per-agent, per-action allow/deny/approval
                                    rules evaluated by the Rust runtime authz
                                    engine alongside (strictest-wins) the
                                    resource-group ACLs.
  * `resource_group_agent_members` — lets an agent principal be a member of a
                                    resource group so the *entire* existing
                                    per-capability ACL / argument-policy /
                                    approval / limits stack applies unchanged.

Vocabularies are module constants + `@validates` (not pg ENUMs), per house style.
"""

from typing import Any, Dict

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import validates
from sqlalchemy.sql import func, text

from app.database.models.ai_system_contract import AUTONOMY_LEVELS
from app.database.models.resource_mixins import ResourceAuthorshipMixin
from app.database import Base

# ── Vocabularies ─────────────────────────────────────────────────────────

# Where the agent identity came from. 'idp' = mapped from an IdP workload
# claim; 'discovered' = surfaced by the discovery plane, not yet IdP-linked.
SOURCE_MANUAL = "manual"
SOURCE_IDP = "idp"
SOURCE_DISCOVERED = "discovered"
AGENT_SOURCES = frozenset({SOURCE_MANUAL, SOURCE_IDP, SOURCE_DISCOVERED})

AGENT_STATUS_ACTIVE = "active"
AGENT_STATUS_SUSPENDED = "suspended"
AGENT_STATUSES = frozenset({AGENT_STATUS_ACTIVE, AGENT_STATUS_SUSPENDED})

# Enforcement mode for the agent's runtime authz. `enforce` blocks on a
# non-allow decision; `dry_run` records a "would-block" on proxy_logs and lets
# the action proceed (safe rollout — Google's DRY_RUN equivalent).
ENFORCEMENT_ENFORCE = "enforce"
ENFORCEMENT_DRY_RUN = "dry_run"
ENFORCEMENT_MODES = frozenset({ENFORCEMENT_ENFORCE, ENFORCEMENT_DRY_RUN})

# Governed actions an agent grant can target.
ACTION_LLM_CALL = "llm_call"
ACTION_MCP_TOOL = "mcp_tool"
ACTION_A2A_CALL = "a2a_call"
ACTION_SKILL_EXEC = "skill_exec"
GRANT_ACTION_TYPES = frozenset({
    ACTION_LLM_CALL, ACTION_MCP_TOOL, ACTION_A2A_CALL, ACTION_SKILL_EXEC,
})

# Grant effect. 'approval_required' routes through the existing approval flow.
EFFECT_ALLOW = "allow"
EFFECT_DENY = "deny"
EFFECT_APPROVAL = "approval_required"
GRANT_EFFECTS = frozenset({EFFECT_ALLOW, EFFECT_DENY, EFFECT_APPROVAL})

#: Grant effects, least permissive first. Mirrored in
#: `brutor-gateway-core/src/services/agent_authz.rs`.
GRANT_EFFECT_RANK: Dict[str, int] = {EFFECT_DENY: 0, EFFECT_APPROVAL: 1, EFFECT_ALLOW: 2}


def grant_effect_rank(effect: Any) -> int:
    """The rank of a grant's effect. One the gateway does not know (a typo,
    one a newer release added, none at all) it reads as `deny`, so it ranks
    as `deny` here too."""
    if isinstance(effect, str) and effect in GRANT_EFFECT_RANK:
        return GRANT_EFFECT_RANK[effect]
    return GRANT_EFFECT_RANK[EFFECT_DENY]


class AgentIdentity(ResourceAuthorshipMixin, Base):
    """An autonomous agent treated as a governable principal."""

    __tablename__ = "agent_identities"

    id = Column(String(255), primary_key=True)  # agent-<ULID>
    tenant_id = Column(
        String(255), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
    )
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)

    # Subject / sponsor — the human accountable for this agent.
    owner_user_id = Column(
        Integer, ForeignKey("end_users.id", ondelete="SET NULL"), nullable=True,
    )
    owner_label = Column(String(255), nullable=True)  # free-text sponsor when no end_user

    # IdP workload identity (the handoff seam). When set, an inbound caller
    # whose token claim matches (idp_server_id, idp_subject) authenticates as
    # this agent.
    idp_server_id = Column(
        String(255), ForeignKey("idp_servers.id", ondelete="SET NULL"), nullable=True,
    )
    idp_subject = Column(String(512), nullable=True)

    source = Column(String(16), nullable=False, default=SOURCE_MANUAL, server_default=SOURCE_MANUAL)
    enabled = Column(Boolean, nullable=False, default=True, server_default="true")
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)  # TTL
    status = Column(String(16), nullable=False, default=AGENT_STATUS_ACTIVE, server_default=AGENT_STATUS_ACTIVE)

    # When no grant matches and a principal is present: deny (true) or fall
    # through to resource-group scoping only (false). Default-deny is the safe
    # posture for an autonomous principal.
    default_deny = Column(Boolean, nullable=False, default=True, server_default="true")

    # enforce | dry_run — dry_run logs would-block verdicts without blocking.
    enforcement_mode = Column(
        String(16), nullable=False, default="enforce", server_default="enforce",
    )

    # How much this actor may do without a human.
    # Orthogonal to enforcement_mode, which is about whether policy bites at
    # all: a dry_run agent can still be suspended, and an enforcing one can
    # still be fully autonomous.
    #
    # Resolved min-wins against the AI System's level, so this can only ever
    # *tighten*. An agent cannot buy itself more freedom than the system it
    # belongs to — which is what makes "suspend the system" mean something.
    autonomy_level = Column(
        String(24),
        nullable=True,
        comment=(
            "autonomous | approval_required | restricted "
            "| suspended. NULL contributes nothing to the min, so an unset "
            "agent simply follows its AI System."
        ),
    )

    # The implementation this identity's software is declared to be, and
    # the releases an approver accepts. These DESCRIBE; they never decide
    # access. All three are part of the contract closure of every AI System
    # the identity is a member of, so changing what is approved drifts the
    # contract.
    implementation_name = Column(
        String(64),
        nullable=True,
        comment="Name this identity's software must declare; NULL = not declared",
    )
    approved_versions = Column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'"),
        comment="Approved version patterns (exact or trailing .*); empty = observe only",
    )
    require_release = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
        comment="A run with no name/version is a finding",
    )

    # created_by / updated_by come from ResourceAuthorshipMixin — one
    # definition, stamped automatically on flush. (AgentGrant below keeps
    # its own: a grant is not an AI Resource.)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uix_agent_identities_tenant_name"),
        UniqueConstraint("idp_server_id", "idp_subject", name="uix_agent_identities_idp_subject"),
        Index("ix_agent_identities_tenant", "tenant_id"),
        Index("ix_agent_identities_tenant_status", "tenant_id", "status"),
    )

    @validates("source")
    def _v_source(self, _k, v):
        if v not in AGENT_SOURCES:
            raise ValueError(f"Invalid source {v!r}; expected {sorted(AGENT_SOURCES)}")
        return v

    @validates("status")
    def _v_status(self, _k, v):
        if v not in AGENT_STATUSES:
            raise ValueError(f"Invalid status {v!r}; expected {sorted(AGENT_STATUSES)}")
        return v

    @validates("enforcement_mode")
    def _v_enforcement_mode(self, _k, v):
        if v not in ENFORCEMENT_MODES:
            raise ValueError(
                f"Invalid enforcement_mode {v!r}; expected {sorted(ENFORCEMENT_MODES)}"
            )
        return v

    @validates("autonomy_level")
    def _v_autonomy_level(self, _k, v):
        if v is not None and v not in AUTONOMY_LEVELS:
            raise ValueError(
                f"Invalid autonomy_level {v!r}; expected {sorted(AUTONOMY_LEVELS)}"
            )
        return v

    @property
    def is_active(self) -> bool:
        return bool(self.enabled) and self.revoked_at is None and self.status == AGENT_STATUS_ACTIVE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "description": self.description,
            "owner_user_id": self.owner_user_id,
            "owner_label": self.owner_label,
            "idp_server_id": self.idp_server_id,
            "idp_subject": self.idp_subject,
            "source": self.source,
            "enabled": self.enabled,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "status": self.status,
            "default_deny": self.default_deny,
            "enforcement_mode": self.enforcement_mode,
            "autonomy_level": self.autonomy_level,
            "implementation_name": self.implementation_name,
            "approved_versions": list(self.approved_versions or []),
            "require_release": bool(self.require_release),
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class AgentGrant(Base):
    """A per-agent, per-action authorization rule. Evaluated strictest-wins
    against the resource-group ACL by the Rust runtime authz engine."""

    __tablename__ = "agent_grants"

    id = Column(String(255), primary_key=True)  # grant-<ULID>
    tenant_id = Column(
        String(255), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
    )
    agent_id = Column(
        String(255), ForeignKey("agent_identities.id", ondelete="CASCADE"), nullable=False,
    )
    action_type = Column(String(16), nullable=False)
    # Glob over the action target (model name / tool name / agent-card id / skill
    # id). "*" matches anything of that action_type.
    target = Column(String(512), nullable=False, default="*", server_default="*")
    # 'approval_required' is 17 chars — needs > 16.
    effect = Column(String(32), nullable=False)
    constraints = Column(JSONB, nullable=True)  # {time_window, max_depth, rate, …}
    enabled = Column(Boolean, nullable=False, default=True, server_default="true")
    expires_at = Column(DateTime(timezone=True), nullable=True)
    created_by = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )

    __table_args__ = (
        Index("ix_agent_grants_agent", "agent_id"),
        Index("ix_agent_grants_tenant_agent_action", "tenant_id", "agent_id", "action_type"),
    )

    @validates("action_type")
    def _v_action(self, _k, v):
        if v not in GRANT_ACTION_TYPES:
            raise ValueError(f"Invalid action_type {v!r}; expected {sorted(GRANT_ACTION_TYPES)}")
        return v

    @validates("effect")
    def _v_effect(self, _k, v):
        if v not in GRANT_EFFECTS:
            raise ValueError(f"Invalid effect {v!r}; expected {sorted(GRANT_EFFECTS)}")
        return v

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "agent_id": self.agent_id,
            "action_type": self.action_type,
            "target": self.target,
            "effect": self.effect,
            "constraints": self.constraints,
            "enabled": self.enabled,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ResourceGroupAgentMember(Base):
    """Binds an agent principal to a resource group so the existing
    per-capability ACL / argument-policy / approval / limits stack applies to
    it exactly as it does to a human member (parallel to
    resource_group_memberships, which is user-only)."""

    __tablename__ = "resource_group_agent_members"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(
        String(255), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
    )
    agent_id = Column(
        String(255), ForeignKey("agent_identities.id", ondelete="CASCADE"), nullable=False,
    )
    group_id = Column(
        String(255), ForeignKey("resource_groups.id", ondelete="CASCADE"), nullable=False,
    )
    role = Column(String(50), nullable=True)
    added_by = Column(String(255), nullable=True)
    joined_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "agent_id", "group_id", name="uix_rg_agent_member"),
        Index("ix_rg_agent_members_tenant_agent", "tenant_id", "agent_id"),
        Index("ix_rg_agent_members_tenant_group", "tenant_id", "group_id"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "agent_id": self.agent_id,
            "group_id": self.group_id,
            "role": self.role,
            "added_by": self.added_by,
            "joined_at": self.joined_at.isoformat() if self.joined_at else None,
        }
