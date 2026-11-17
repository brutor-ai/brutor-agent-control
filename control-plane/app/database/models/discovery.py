# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Discovery plane — out-of-band AI-asset discovery (observability only)

Out-of-band collectors (egress-log, k8s, host-agent, MCP/A2A probe, cloud-api)
emit **Ed25519-signed discovery events** — one AI asset they found plus the
evidence that led there — to the management plane. The platform dedups them
into a tenant-scoped inventory and reconciles each against the gateway's own
config ("is this routed through us?") to surface *discovered but ungoverned*
assets. This plane NEVER touches enforcement: there is no path from a discovery
event to changing gateway behaviour.

Tables:
  * `collector_keys`      — registered Ed25519 public keys; ingest rejects any
                            event whose `key_id` isn't here (or is revoked).
  * `discovered_assets`   — the deduped inventory, one row per (tenant,
                            asset_key); carries the platform-computed confidence
                            and the reconciled governance status.
  * `discovery_evidence`  — the evidence rows that justify each asset.
  * `discovery_events`    — raw signed events, idempotency-keyed on `event_id`.

Vocabularies are module-level constants + `@validates` (mirrors ai_asset.py /
agent_card.py) rather than pg ENUMs, so renames survive without ALTER TYPE.
Contract: discovery-event v1.
"""

from typing import Any, Dict

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import validates
from sqlalchemy.sql import func

from app.database import Base

# ── Vocabularies ─────────────────────────────────────────────────────────

ASSET_TYPE_LLM_ENDPOINT = "llm_endpoint"
ASSET_TYPE_MCP_SERVER = "mcp_server"
ASSET_TYPE_A2A_AGENT = "a2a_agent"
ASSET_TYPE_INFERENCE_RUNTIME = "inference_runtime"
ASSET_TYPE_AGENT_FRAMEWORK = "agent_framework"
ASSET_TYPE_SDK_USAGE = "sdk_usage"
ASSET_TYPE_UNKNOWN_AI = "unknown_ai"
# Endpoint plane (Brutor Scout). A local AI client installed on
# a device, and an MCP server a local client is wired to (the shadow-MCP
# surface). Observe-only, like every other discovered asset type.
ASSET_TYPE_LOCAL_AI_CLIENT = "local_ai_client"
ASSET_TYPE_LOCAL_MCP_SERVER = "local_mcp_server"

ASSET_TYPES = frozenset({
    ASSET_TYPE_LLM_ENDPOINT,
    ASSET_TYPE_MCP_SERVER,
    ASSET_TYPE_A2A_AGENT,
    ASSET_TYPE_INFERENCE_RUNTIME,
    ASSET_TYPE_AGENT_FRAMEWORK,
    ASSET_TYPE_SDK_USAGE,
    ASSET_TYPE_UNKNOWN_AI,
    ASSET_TYPE_LOCAL_AI_CLIENT,
    ASSET_TYPE_LOCAL_MCP_SERVER,
})

EVIDENCE_TYPES = frozenset({
    "dns_resolution", "tls_sni", "netflow", "process", "env_var",
    "package_manifest", "config_file", "well_known_probe",
    "registry_entry", "api_inventory",
})

# Collector trust tiers (precedence order on conflict, highest first).
TRUST_FIRST_PARTY = "first_party"
TRUST_CHANNEL = "channel"
TRUST_CUSTOMER = "customer"
TRUST_COMMUNITY = "community"

TRUST_TIERS = frozenset({
    TRUST_FIRST_PARTY, TRUST_CHANNEL, TRUST_CUSTOMER, TRUST_COMMUNITY,
})
# Numeric rank for precedence arbitration (higher wins).
TRUST_RANK = {
    TRUST_FIRST_PARTY: 3, TRUST_CHANNEL: 2, TRUST_CUSTOMER: 1, TRUST_COMMUNITY: 0,
}

# Reconciled governance status — ALWAYS computed by the platform against the
# config tables, never taken from the collector's advisory governance_hint.
GOV_GOVERNED = "governed"
GOV_UNGOVERNED = "ungoverned"
GOV_UNKNOWN = "unknown"

GOVERNANCE_STATUSES = frozenset({GOV_GOVERNED, GOV_UNGOVERNED, GOV_UNKNOWN})

# Operator review state for a discovered asset.
STATUS_ACTIVE = "active"        # newly discovered, awaiting operator action
STATUS_ONBOARDED = "onboarded"  # promoted into config / ai_assets
STATUS_DISMISSED = "dismissed"  # operator marked not-relevant

DISCOVERED_STATUSES = frozenset({STATUS_ACTIVE, STATUS_ONBOARDED, STATUS_DISMISSED})


class CollectorKey(Base):
    """A registered Ed25519 public key for a discovery collector. Ingest
    verifies each event's signature against the key named by its `key_id`;
    unknown or revoked keys are rejected before any inventory write."""

    __tablename__ = "collector_keys"

    key_id = Column(String(255), primary_key=True)
    tenant_id = Column(
        String(255), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
    )
    collector_id = Column(String(255), nullable=False)
    collector_type = Column(String(64), nullable=True)
    # Base64 (standard) Ed25519 public key, 32 bytes decoded.
    public_key = Column(Text, nullable=False)
    trust_tier = Column(
        String(16), nullable=False,
        default=TRUST_CUSTOMER, server_default=TRUST_CUSTOMER,
    )
    enabled = Column(Boolean, nullable=False, default=True, server_default="true")
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    created_by = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_used_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_collector_keys_tenant", "tenant_id"),
    )

    @validates("trust_tier")
    def _v_trust(self, _k, v):
        if v not in TRUST_TIERS:
            raise ValueError(f"Invalid trust_tier {v!r}; expected {sorted(TRUST_TIERS)}")
        return v

    @property
    def is_active(self) -> bool:
        return bool(self.enabled) and self.revoked_at is None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key_id": self.key_id,
            "tenant_id": self.tenant_id,
            "collector_id": self.collector_id,
            "collector_type": self.collector_type,
            "trust_tier": self.trust_tier,
            "enabled": self.enabled,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            # public_key is intentionally NOT exposed by default in lists; the
            # endpoint may include it explicitly. (Not a secret, but noisy.)
        }


class DiscoveredAsset(Base):
    """One deduped AI asset a collector found, keyed on (tenant, asset_key).
    `governance_status` is reconciled against the gateway config — the honesty
    guarantee — never inherited from the collector."""

    __tablename__ = "discovered_assets"

    id = Column(String(255), primary_key=True)  # dasset-<ULID>
    tenant_id = Column(
        String(255), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
    )
    asset_key = Column(String(512), nullable=False)
    asset_type = Column(String(32), nullable=False)
    provider = Column(String(64), nullable=True)
    display_name = Column(String(512), nullable=True)
    model_hint = Column(String(255), nullable=True)

    # Flattened endpoint (the schema's endpoint object).
    endpoint_scheme = Column(String(16), nullable=True)
    endpoint_host = Column(String(255), nullable=True)
    endpoint_port = Column(Integer, nullable=True)
    endpoint_path = Column(String(512), nullable=True)

    # Platform-computed authoritative confidence (0..1). collector_confidence
    # is advisory and never stored here as authoritative.
    computed_confidence = Column(Float, nullable=False, default=0.0, server_default="0")

    # Reconciled against the config tables.
    governance_status = Column(
        String(16), nullable=False,
        default=GOV_UNKNOWN, server_default=GOV_UNKNOWN,
    )
    governed_kind = Column(String(32), nullable=True)   # llm_provider|mcp_server|agent_card|…
    governed_ref = Column(String(255), nullable=True)   # matched config row id

    # Scout — the device an endpoint-plane asset was seen on
    # (local_ai_client / local_mcp_server). NULL for network/import assets.
    device_id = Column(String(128), nullable=True)
    device_name = Column(String(255), nullable=True)

    status = Column(
        String(16), nullable=False,
        default=STATUS_ACTIVE, server_default=STATUS_ACTIVE,
    )
    evidence_count = Column(Integer, nullable=False, default=0, server_default="0")
    first_seen = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_seen = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "asset_key", name="uix_discovered_assets_tenant_key"),
        Index("ix_discovered_assets_tenant", "tenant_id"),
        Index("ix_discovered_assets_tenant_type", "tenant_id", "asset_type"),
        Index("ix_discovered_assets_tenant_gov", "tenant_id", "governance_status"),
        Index("ix_discovered_assets_tenant_status", "tenant_id", "status"),
        Index("ix_discovered_assets_tenant_device", "tenant_id", "device_id"),
    )

    @validates("asset_type")
    def _v_type(self, _k, v):
        if v not in ASSET_TYPES:
            raise ValueError(f"Invalid asset_type {v!r}; expected {sorted(ASSET_TYPES)}")
        return v

    @validates("governance_status")
    def _v_gov(self, _k, v):
        if v not in GOVERNANCE_STATUSES:
            raise ValueError(f"Invalid governance_status {v!r}; expected {sorted(GOVERNANCE_STATUSES)}")
        return v

    @validates("status")
    def _v_status(self, _k, v):
        if v not in DISCOVERED_STATUSES:
            raise ValueError(f"Invalid status {v!r}; expected {sorted(DISCOVERED_STATUSES)}")
        return v

    def to_dict(self, include_evidence: bool = False) -> Dict[str, Any]:
        d = {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "asset_key": self.asset_key,
            "asset_type": self.asset_type,
            "provider": self.provider,
            "display_name": self.display_name,
            "model_hint": self.model_hint,
            "endpoint": {
                "scheme": self.endpoint_scheme,
                "host": self.endpoint_host,
                "port": self.endpoint_port,
                "path": self.endpoint_path,
            },
            "computed_confidence": self.computed_confidence,
            "governance_status": self.governance_status,
            "governed_kind": self.governed_kind,
            "governed_ref": self.governed_ref,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "status": self.status,
            "evidence_count": self.evidence_count,
            "first_seen": self.first_seen.isoformat() if self.first_seen else None,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_evidence:
            d["evidence"] = [e.to_dict() for e in (self.evidence or [])]
        return d


class DiscoveryEvidence(Base):
    """A single observation supporting a discovered asset. `passive` and
    `host_derived` are kept separate (orthogonal) so the platform can weigh
    each independently when computing confidence."""

    __tablename__ = "discovery_evidence"

    id = Column(String(255), primary_key=True)  # devid-<ULID>
    tenant_id = Column(
        String(255), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
    )
    discovered_asset_id = Column(
        String(255), ForeignKey("discovered_assets.id", ondelete="CASCADE"), nullable=False,
    )
    evidence_type = Column(String(32), nullable=False)
    observed_at = Column(DateTime(timezone=True), nullable=True)
    observation = Column(Text, nullable=True)
    passive = Column(Boolean, nullable=False, default=False, server_default="false")
    host_derived = Column(Boolean, nullable=False, default=False, server_default="false")
    collector_id = Column(String(255), nullable=True)
    trust_tier = Column(String(16), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_discovery_evidence_asset", "discovered_asset_id"),
        Index("ix_discovery_evidence_tenant", "tenant_id"),
    )

    @validates("evidence_type")
    def _v_etype(self, _k, v):
        if v not in EVIDENCE_TYPES:
            raise ValueError(f"Invalid evidence_type {v!r}; expected {sorted(EVIDENCE_TYPES)}")
        return v

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "evidence_type": self.evidence_type,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "observation": self.observation,
            "passive": self.passive,
            "host_derived": self.host_derived,
            "collector_id": self.collector_id,
            "trust_tier": self.trust_tier,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class DiscoveryEvent(Base):
    """Raw signed event audit, idempotency-keyed on the collector's event_id.
    Re-sending the same event_id is a no-op (ON CONFLICT DO NOTHING)."""

    __tablename__ = "discovery_events"

    # Idempotency is PER-TENANT: `event_id` is generated by the customer's own
    # collector and is only guaranteed unique within that tenant. A composite
    # (tenant_id, event_id) primary key lets two tenants legitimately reuse the
    # same event_id — a global PK on event_id alone silently dropped tenant B's
    # distinct event as a "duplicate" of tenant A's (cross-tenant data loss).
    event_id = Column(String(255), primary_key=True)
    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    collector_id = Column(String(255), nullable=True)
    collector_type = Column(String(64), nullable=True)
    collector_version = Column(String(64), nullable=True)
    trust_tier = Column(String(16), nullable=True)
    asset_key = Column(String(512), nullable=True)
    emitted_at = Column(DateTime(timezone=True), nullable=True)
    received_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    accepted = Column(Boolean, nullable=False, default=True, server_default="true")
    reject_reason = Column(String(255), nullable=True)
    payload = Column(JSONB, nullable=True)

    __table_args__ = (
        Index("ix_discovery_events_tenant_received", "tenant_id", "received_at"),
        Index("ix_discovery_events_asset_key", "tenant_id", "asset_key"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "tenant_id": self.tenant_id,
            "collector_id": self.collector_id,
            "collector_type": self.collector_type,
            "collector_version": self.collector_version,
            "trust_tier": self.trust_tier,
            "asset_key": self.asset_key,
            "emitted_at": self.emitted_at.isoformat() if self.emitted_at else None,
            "received_at": self.received_at.isoformat() if self.received_at else None,
            "accepted": self.accepted,
            "reject_reason": self.reject_reason,
        }


# Relationship: asset → its evidence (lazy; loaded on demand for detail views).
from sqlalchemy.orm import relationship  # noqa: E402

DiscoveredAsset.evidence = relationship(
    "DiscoveryEvidence",
    backref="asset",
    cascade="all, delete-orphan",
    lazy="select",
    order_by="DiscoveryEvidence.observed_at",
)
