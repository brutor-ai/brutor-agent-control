# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# AI Asset Registry

A single inventory row per governed AI resource in a tenant. The registry
is the operator's "what AI do we run, who owns it, what risk tier, what's
its lifecycle" view — the entry point for AI governance (SOC 2 / EU AI Act
asset inventory).

Two kinds of rows live here:

  * `source="discovered"` — auto-synced from the underlying config tables
    (LLM models/providers, routing groups, MCP servers, skills, agent
    cards). The sync refreshes `display_name` only; operator-set
    governance fields (owner, lifecycle_stage, risk_tier, intended_use,
    enforcement_mode, tags, status) are NEVER overwritten by a re-sync.
    When the underlying config row disappears, the asset is flagged
    (status="flagged") rather than deleted — orphans stay visible for
    operator review.

  * `source="manual"` — created by hand for assets that have no config
    row of their own (e.g. an externally-hosted model the tenant governs
    via policy but doesn't proxy).

Vocabularies are kept as module-level string constants + frozensets and
validated with `@validates` (mirrors agent_card.py) rather than as
Postgres ENUMs, so renames survive migrations without an ALTER TYPE.
"""

from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import validates
from sqlalchemy.sql import func

from app.database import Base


# ── Vocabularies ─────────────────────────────────────────────────────────

# The kind of underlying resource an asset describes. The discovery sync
# maps each source config table to exactly one of these.
ASSET_KIND_LLM_MODEL = "llm_model"
ASSET_KIND_LLM_PROVIDER = "llm_provider"
ASSET_KIND_ROUTING_GROUP = "routing_group"
ASSET_KIND_MCP_SERVER = "mcp_server"
ASSET_KIND_SKILL = "skill"
ASSET_KIND_AGENT_CARD = "agent_card"
# Composite / principal kinds. `ai_system` = a resource group of type
# ai_system (an AI System in the ISO 42001 sense — the auditor-legible unit
# that composes the atomic assets above). `agent_identity` = the acting agent
# principal. Both give the inventory a first-class row + fact sheet for the
# agent SYSTEM and the agent, not just the components they use.
ASSET_KIND_AI_SYSTEM = "ai_system"
ASSET_KIND_AGENT_IDENTITY = "agent_identity"

ASSET_KINDS = frozenset({
    ASSET_KIND_LLM_MODEL,
    ASSET_KIND_LLM_PROVIDER,
    ASSET_KIND_ROUTING_GROUP,
    ASSET_KIND_MCP_SERVER,
    ASSET_KIND_SKILL,
    ASSET_KIND_AGENT_CARD,
    ASSET_KIND_AI_SYSTEM,
    ASSET_KIND_AGENT_IDENTITY,
})

# Governance lifecycle of the asset. Operator-owned; the sync seeds new
# discovered rows as "active".
LIFECYCLE_PROPOSED = "proposed"
LIFECYCLE_APPROVED = "approved"
LIFECYCLE_ACTIVE = "active"
LIFECYCLE_DEPRECATED = "deprecated"
LIFECYCLE_RETIRED = "retired"

LIFECYCLE_STAGES = frozenset({
    LIFECYCLE_PROPOSED,
    LIFECYCLE_APPROVED,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_DEPRECATED,
    LIFECYCLE_RETIRED,
})

# EU AI Act-aligned risk tiers. Nullable — most assets start untiered
# until an operator classifies them.
RISK_TIER_MINIMAL = "minimal"
RISK_TIER_LIMITED = "limited"
RISK_TIER_HIGH = "high"
RISK_TIER_UNACCEPTABLE = "unacceptable"

RISK_TIERS = frozenset({
    RISK_TIER_MINIMAL,
    RISK_TIER_LIMITED,
    RISK_TIER_HIGH,
    RISK_TIER_UNACCEPTABLE,
})

#: GPAI track — Art 53–55 obligations attach to the MODEL, on its
#: own track, separate from any system that uses it.
GPAI_NONE = "none"
GPAI = "gpai"
GPAI_SYSTEMIC_RISK = "gpai_systemic_risk"
GPAI_CLASSIFICATIONS = frozenset({GPAI_NONE, GPAI, GPAI_SYSTEMIC_RISK})

#: Who operates the asset. third_party marks constituents you
#: have obligations over but no operational control of — an externally
#: operated MCP server or A2A peer is a supply-chain relationship, not
#: an attribute to leave implicit.
OPERATED_BY_SELF = "self"
OPERATED_BY_THIRD_PARTY = "third_party"
OPERATED_BY = frozenset({OPERATED_BY_SELF, OPERATED_BY_THIRD_PARTY})

# How strongly the registry's governance intent is enforced on this asset.
# Only the operator's intent is stored; nothing enforces it yet.
ENFORCEMENT_OFF = "off"
ENFORCEMENT_WARN = "warn"
ENFORCEMENT_ENFORCE = "enforce"

ENFORCEMENT_MODES = frozenset({
    ENFORCEMENT_OFF,
    ENFORCEMENT_WARN,
    ENFORCEMENT_ENFORCE,
})

# Review status surfaced in the registry UI. The sync flips an orphaned
# discovered asset to "flagged"; the rest are operator-owned.
STATUS_OK = "ok"
STATUS_REVIEW_DUE = "review_due"
STATUS_FLAGGED = "flagged"

STATUSES = frozenset({
    STATUS_OK,
    STATUS_REVIEW_DUE,
    STATUS_FLAGGED,
})

# Provenance of the row.
SOURCE_DISCOVERED = "discovered"
SOURCE_MANUAL = "manual"

SOURCES = frozenset({
    SOURCE_DISCOVERED,
    SOURCE_MANUAL,
})


class AiAsset(Base):
    """One inventory row per governed AI resource in a tenant."""

    __tablename__ = "ai_assets"

    id = Column(String(255), primary_key=True)
    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )

    # What kind of underlying resource this is, and the id of that
    # resource's config row. Together with tenant_id these form the
    # natural key the discovery sync upserts on.
    asset_kind = Column(String(32), nullable=False)
    ref_id = Column(String(255), nullable=False)

    # Denormalised from the source config row's display column on sync so
    # the registry list renders without a per-row join.
    display_name = Column(String(255), nullable=True)

    # Denormalised presentation hints refreshed on every sync (alongside
    # display_name). `icon_url` resolves per-kind (llm_model →
    # catalog/provider icon, llm_provider → provider icon, agent_card →
    # its icon, others → null). `source_enabled` mirrors the underlying
    # config row's enabled flag where it has one (llm_model / llm_provider)
    # so the registry can grey out disabled assets without a join. Both are
    # nullable and, like display_name, are sync-owned (never operator-set).
    icon_url = Column(String(1024), nullable=True)
    source_enabled = Column(Boolean, nullable=True)

    # Observability-only marker (sync-owned, like source_enabled). True when the
    # backing resource is known for cost/usage visibility but is NOT governed
    # inline by the gateway — specifically an LLM model discovered via Traffic
    # Data Import (llm_models.source='import') that hasn't been promoted/enabled
    # yet. These are "Observed", not "Governed": the gateway doesn't proxy them.
    # Flips to False once an operator enables (promotes) the model. NULL/False
    # both mean "not observed" (governed).
    observed = Column(Boolean, nullable=True)

    # ── Operator-owned governance fields ─────────────────────────────────
    # These are NEVER overwritten by a re-sync.
    owner = Column(String(255), nullable=True)
    lifecycle_stage = Column(
        String(32), nullable=False,
        default=LIFECYCLE_ACTIVE, server_default=LIFECYCLE_ACTIVE,
    )
    risk_tier = Column(String(32), nullable=True)
    intended_use = Column(Text, nullable=True)
    gpai_classification = Column(
        String(24),
        nullable=True,
        comment="gpai | gpai_systemic_risk (llm_model assets; Art 53/55 track)",
    )
    operated_by = Column(
        String(16),
        nullable=True,
        comment="self | third_party — who operates this constituent",
    )
    enforcement_mode = Column(
        String(16), nullable=False,
        default=ENFORCEMENT_OFF, server_default=ENFORCEMENT_OFF,
    )
    tags = Column(JSONB, nullable=True)
    status = Column(
        String(32), nullable=False,
        default=STATUS_OK, server_default=STATUS_OK,
    )

    source = Column(
        String(16), nullable=False,
        default=SOURCE_DISCOVERED, server_default=SOURCE_DISCOVERED,
    )

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(), nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(), onupdate=func.now(), nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "asset_kind", "ref_id",
            name="uix_ai_assets_tenant_kind_ref",
        ),
        Index("ix_ai_assets_tenant", "tenant_id"),
        Index("ix_ai_assets_tenant_kind", "tenant_id", "asset_kind"),
        Index("ix_ai_assets_tenant_status", "tenant_id", "status"),
        Index("ix_ai_assets_tenant_lifecycle", "tenant_id", "lifecycle_stage"),
    )

    # ── Validators ───────────────────────────────────────────────────────

    @validates("asset_kind")
    def _validate_asset_kind(self, _key: str, value: str) -> str:
        if value not in ASSET_KINDS:
            raise ValueError(
                f"Invalid asset_kind {value!r}; expected one of "
                f"{sorted(ASSET_KINDS)}"
            )
        return value

    @validates("lifecycle_stage")
    def _validate_lifecycle_stage(self, _key: str, value: str) -> str:
        if value not in LIFECYCLE_STAGES:
            raise ValueError(
                f"Invalid lifecycle_stage {value!r}; expected one of "
                f"{sorted(LIFECYCLE_STAGES)}"
            )
        return value

    @validates("risk_tier")
    def _validate_risk_tier(
        self, _key: str, value: Optional[str]
    ) -> Optional[str]:
        if value is None:
            return value
        if value not in RISK_TIERS:
            raise ValueError(
                f"Invalid risk_tier {value!r}; expected one of "
                f"{sorted(RISK_TIERS)}"
            )
        return value

    @validates("enforcement_mode")
    def _validate_enforcement_mode(self, _key: str, value: str) -> str:
        if value not in ENFORCEMENT_MODES:
            raise ValueError(
                f"Invalid enforcement_mode {value!r}; expected one of "
                f"{sorted(ENFORCEMENT_MODES)}"
            )
        return value

    @validates("status")
    def _validate_status(self, _key: str, value: str) -> str:
        if value not in STATUSES:
            raise ValueError(
                f"Invalid status {value!r}; expected one of "
                f"{sorted(STATUSES)}"
            )
        return value

    @validates("source")
    def _validate_source(self, _key: str, value: str) -> str:
        if value not in SOURCES:
            raise ValueError(
                f"Invalid source {value!r}; expected one of "
                f"{sorted(SOURCES)}"
            )
        return value

    def to_dict(self) -> Dict[str, Any]:
        """Dictionary for API responses. Every column appears here — the
        Pydantic response model declares the same keys (the strip-
        undeclared-fields gotcha)."""
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "asset_kind": self.asset_kind,
            "ref_id": self.ref_id,
            "display_name": self.display_name,
            "icon_url": self.icon_url,
            "source_enabled": self.source_enabled,
            "observed": bool(self.observed),
            "owner": self.owner,
            "lifecycle_stage": self.lifecycle_stage,
            "risk_tier": self.risk_tier,
            "intended_use": self.intended_use,
            "gpai_classification": self.gpai_classification,
            "operated_by": self.operated_by,
            "enforcement_mode": self.enforcement_mode,
            "tags": list(self.tags) if self.tags else [],
            "status": self.status,
            "source": self.source,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


__all__ = [
    "AiAsset",
    "ASSET_KINDS",
    "ASSET_KIND_LLM_MODEL",
    "ASSET_KIND_LLM_PROVIDER",
    "ASSET_KIND_ROUTING_GROUP",
    "ASSET_KIND_MCP_SERVER",
    "ASSET_KIND_SKILL",
    "ASSET_KIND_AGENT_CARD",
    "ASSET_KIND_AI_SYSTEM",
    "ASSET_KIND_AGENT_IDENTITY",
    "LIFECYCLE_STAGES",
    "RISK_TIERS",
    "ENFORCEMENT_MODES",
    "STATUSES",
    "SOURCES",
    "SOURCE_DISCOVERED",
    "SOURCE_MANUAL",
    "STATUS_OK",
    "STATUS_FLAGGED",
    "LIFECYCLE_ACTIVE",
    "ENFORCEMENT_OFF",
]
