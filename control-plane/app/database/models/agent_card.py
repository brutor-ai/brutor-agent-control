# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
A2A Agent Card model.

An Agent Card is the A2A-standard descriptor (v0.3 spec) that other
agents read to discover this agent's identity, capabilities, auth
requirements, and endpoint. The gateway stores it verbatim in
`card_json` plus a handful of denormalised columns that the policy
engine and admin UI hit on every list/filter.

Trust tier semantics:

  * unsigned        — card was created/imported without a signature.
                      Default-deny under production policies;
                      present in the registry for operator review.
  * tenant_signed   — signed by this tenant's Ed25519 key (the seed
                      generates one per tenant on provisioning).
                      Trusted within the tenant.
  * partner_signed  — imported with an externally-valid signature
                      from a configured trust anchor. Reserved for
                      cross-tenant use; the column exists so
                      policy code can filter on it today.
  * revoked         — terminal. Kept in the table for audit; policy
                      engine treats as never-trusted.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship, validates
from sqlalchemy.sql import func

from app.database.models.resource_mixins import ResourceAuthorshipMixin, ResourceVersionMixin
from app.database import Base


# Must match what migration `a012_a2a_v1_schema` writes (`postgresql.JSONB()`), and it is
# load-bearing rather than cosmetic: a GIN index needs `jsonb`, because
# Postgres ships no default GIN operator class for `json`. Declaring these
# columns as plain `JSON` made `create_all()` on a FRESH database die with
# "data type json has no default operator class for access method gin" while
# migrated databases were fine — the two schema paths had drifted, and only
# the fresh one was broken. See `ix_agent_cards_extensions_gin` below.
_JSONB_TYPE = JSONB()


# Sentinel values for trust_tier. Kept as module constants rather than
# an Enum because the column is plain VARCHAR — migrations should
# survive string renames without altering an enum type in Postgres.
TRUST_TIER_UNSIGNED = "unsigned"
TRUST_TIER_TENANT_SIGNED = "tenant_signed"
TRUST_TIER_PARTNER_SIGNED = "partner_signed"
TRUST_TIER_REVOKED = "revoked"

VALID_TRUST_TIERS = frozenset({
    TRUST_TIER_UNSIGNED,
    TRUST_TIER_TENANT_SIGNED,
    TRUST_TIER_PARTNER_SIGNED,
    TRUST_TIER_REVOKED,
})

# Health states. Kept loose strings (not an Enum) for the same reason
# trust_tier is — the column is plain VARCHAR so renames don't need a
# Postgres ENUM ALTER. Same vocabulary as llm_model / mcp_server.
HEALTH_UNKNOWN = "unknown"
HEALTH_HEALTHY = "healthy"
HEALTH_UNHEALTHY = "unhealthy"

VALID_HEALTH_STATUSES = frozenset({
    HEALTH_UNKNOWN,
    HEALTH_HEALTHY,
    HEALTH_UNHEALTHY,
})


class AgentCard(ResourceAuthorshipMixin, ResourceVersionMixin, Base):
    """A2A Agent Card — one row per registered agent."""

    __tablename__ = "agent_cards"

    id = Column(String(255), primary_key=True)
    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )

    name = Column(String(255), nullable=False)
    version = Column(String(64), nullable=False, default="1.0.0",
                     server_default="1.0.0")
    description = Column(Text, nullable=True)
    source_url = Column(String(1024), nullable=True)

    # AgentCard.iconUrl — denormalised from card_json on write so the
    # admin UI can render avatars without parsing the full body. Nullable
    # because many cards in the wild omit it.
    icon_url = Column(String(2048), nullable=True)

    # Verbatim A2A card JSON. Originally v0.3; v1.0-native cards
    # write directly here too. `protocol_version` below tells the
    # proxy which dialect to deserialize into.
    card_json = Column(JSON, nullable=False)

    # ── v1.0 wire fields ─────────────────────────────────────────────
    # Denormalised from `card_json` on write so the proxy hot path
    # never parses the whole document. Keep `card_json` + these
    # columns in sync via `sync_v1_denormalisation` at upsert time.

    supported_interfaces = Column(
        _JSONB_TYPE, nullable=True,
        comment="A2A v1.0 AgentInterface[]; replaces v0.3 top-level url + protocolVersion."
    )
    signatures = Column(
        _JSONB_TYPE, nullable=True,
        comment="A2A v1.0 AgentCardSignature[] — ordered JWS flattened-serialization objects."
    )
    extensions = Column(
        _JSONB_TYPE, nullable=True,
        comment="AgentCapabilities.extensions[].uri values, denormalised for policy lookups."
    )
    protocol_version = Column(
        String(8), nullable=False, default="0.3", server_default="0.3",
        comment="Dialect of card_json: '0.3' or '1.0'."
    )

    # Verbatim A2A AgentCard JSON served by the authenticated
    # GetExtendedAgentCard endpoint of the A2A specification. Null = no extended
    # card configured; the proxy then returns ExtendedAgentCardNotConfigured.
    extended_card_json = Column(
        JSONB, nullable=True,
        comment="A2A extended AgentCard JSON (served by GetExtendedAgentCard). Null = none."
    )

    # Tenant-wide default limit/policy posture for this agent card (min-wins
    # with each Resource Group). Same shapes the RG A2A forms use
    # (a2a_global_limits / a2a_global_governance). The Rust proxy folds
    # tenant_limits into the effective A2A limits at delegation/quota time. Set
    # via Resources → Agents → agent card edit → Limits / Policies.
    tenant_limits = Column(JSON(none_as_null=True), nullable=True)
    tenant_governance = Column(JSON(none_as_null=True), nullable=True)

    # Signing / trust
    signature = Column(Text, nullable=True)
    signature_algorithm = Column(String(32), nullable=True)
    signed_by = Column(String(255), nullable=True)
    signed_at = Column(DateTime(timezone=True), nullable=True)
    trust_tier = Column(
        String(32),
        nullable=False,
        default=TRUST_TIER_UNSIGNED,
        server_default=TRUST_TIER_UNSIGNED,
    )
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revoked_reason = Column(Text, nullable=True)

    enabled = Column(Boolean, nullable=False, default=True,
                     server_default="true")
    # created_by / updated_by come from ResourceAuthorshipMixin — one
    # definition, stamped automatically on flush.
    created_at = Column(DateTime(timezone=True),
                        server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True),
                        server_default=func.now(),
                        onupdate=func.now(), nullable=False)

    # Liveness — populated by the admin "Health Check" action. Mirrors
    # the columns on llm_model / mcp_server so the UI can render a
    # consistent badge and the operator can spot dead upstreams.
    health_status = Column(
        String(32), nullable=False,
        default="unknown", server_default="unknown",
    )
    last_health_check_at = Column(DateTime(timezone=True), nullable=True)
    health_check_error = Column(Text, nullable=True)

    # Capabilities — regenerated on every card upsert. `passive_deletes`
    # lets the ON DELETE CASCADE FK handle the child rows without
    # SQLAlchemy issuing per-row deletes.
    capabilities = relationship(
        "AgentCardCapability",
        back_populates="agent_card",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    # Many-to-many: AgentCard <-> ResourceGroup.
    # A card may be bound to any number of groups; the resource group is the
    # unit of governance (limits + governance are defined per-group, with
    # restrictive inheritance along the group hierarchy). Unbound cards are
    # registry-only — discoverable at .well-known but not invocable by any
    # group until an admin binds them.
    resource_groups = relationship(
        "ResourceGroup",
        secondary="resource_group_agent_cards",
        back_populates="agent_cards",
        lazy="dynamic",
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "name",
                         name="uix_agent_cards_tenant_name"),
        Index("ix_agent_cards_tenant", "tenant_id"),
        Index("ix_agent_cards_tenant_enabled", "tenant_id", "enabled"),
        Index("ix_agent_cards_trust_tier", "tenant_id", "trust_tier"),
        # The same indexes the migrations create (GIN on extensions, health
        # status), so create_all builds them too.
        Index("ix_agent_cards_extensions_gin", "extensions", postgresql_using="gin"),
        Index("ix_agent_cards_health_status", "tenant_id", "health_status"),
    )

    @validates("trust_tier")
    def _validate_trust_tier(self, _key: str, value: str) -> str:
        if value not in VALID_TRUST_TIERS:
            raise ValueError(
                f"Invalid trust_tier {value!r}; expected one of "
                f"{sorted(VALID_TRUST_TIERS)}"
            )
        return value

    @validates("health_status")
    def _validate_health_status(self, _key: str, value: str) -> str:
        if value not in VALID_HEALTH_STATUSES:
            raise ValueError(
                f"Invalid health_status {value!r}; expected one of "
                f"{sorted(VALID_HEALTH_STATUSES)}"
            )
        return value

    @validates("name")
    def _validate_name(self, _key: str, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Agent card name is required")
        v = value.strip()
        if len(v) > 255:
            raise ValueError("Agent card name must be ≤255 chars")
        return v

    @property
    def is_trusted(self) -> bool:
        """True iff the card has a non-revoked signature we recognise.

        Policy code should use this rather than reading trust_tier
        directly — keeps the rule at one place if the tier set grows.
        """
        return self.trust_tier in (
            TRUST_TIER_TENANT_SIGNED,
            TRUST_TIER_PARTNER_SIGNED,
        ) and self.revoked_at is None

    def revoke(self, reason: str) -> None:
        """Mark this card permanently revoked. Terminal — cards are not
        un-revoked; the operator creates a new card if needed."""
        self.trust_tier = TRUST_TIER_REVOKED
        self.revoked_at = datetime.now(timezone.utc)
        self.revoked_reason = reason
        # Disable so the proxy doesn't route to it. Keep the row for audit.
        self.enabled = False

    def to_dict(self, include_card_json: bool = True) -> Dict[str, Any]:
        """Dictionary for API responses.

        `include_card_json=False` is what the list endpoint uses —
        the verbatim JSON can be kilobytes and a paginated list
        doesn't need it.
        """
        out: Dict[str, Any] = {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "source_url": self.source_url,
            "icon_url": self.icon_url,
            "signature_algorithm": self.signature_algorithm,
            "signed_by": self.signed_by,
            "signed_at": self.signed_at.isoformat() if self.signed_at else None,
            "trust_tier": self.trust_tier,
            "is_trusted": self.is_trusted,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "revoked_reason": self.revoked_reason,
            "enabled": self.enabled,
            "tenant_limits": self.tenant_limits,
            "tenant_governance": self.tenant_governance,
            "health_status": self.health_status,
            "last_health_check_at": (
                self.last_health_check_at.isoformat()
                if self.last_health_check_at else None
            ),
            "health_check_error": self.health_check_error,
            "created_by": self.created_by,
            # `updated_by` was missing while `created_by` was present, so
            # the Audit Info tab showed a creator and an unknown last
            # editor on the same card — which reads as "nobody has touched
            # it since", the one thing it must never imply.
            "updated_by": self.updated_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "capability_count": len(self.capabilities or []),
        }
        if include_card_json:
            out["card_json"] = self.card_json
            out["extended_card_json"] = self.extended_card_json
            out["signature"] = self.signature
        # Resource-group bindings — surfaces as a flat list of ids on list
        # views plus a detailed {id, name} list on the detail view so the
        # admin UI can render group chips without a second round-trip.
        try:
            groups = list(self.resource_groups) if self.resource_groups is not None else []
        except Exception:
            groups = []
        out["group_ids"] = [g.id for g in groups]
        out["groups"] = [
            {"id": g.id, "name": g.name, "display_name": getattr(g, "display_name", None)}
            for g in groups
        ]
        return out
