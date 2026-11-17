# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Global LLM Provider Catalog

Mirrors `llm_catalog` (the global *model* catalog) for *providers*.

Historically the FULL provider catalogue was BULK-SEEDED per tenant as
`llm_providers` rows by `init_llm_providers.create_llm_providers` — every
fresh tenant carried ~41 provider rows the operator never asked for, a
design smell directly analogous to the old per-tenant `llm_model` catalog.

This table is the single, **GLOBAL** (no tenant_id) source of truth for
"what providers Brutor knows how to talk to". A tenant gets a real
`llm_providers` row only when a model that uses the provider is actually
added (seed / catalog-import / admin-create) — `upsert_provider_for_key`
materializes one provider from this catalog on demand, stamping
`llm_providers.catalog_id` back here for provenance (so the asset registry
can resolve the catalog icon and we know a provider row's origin).

Seeded entries are idempotent on `key` (UNIQUE) and use a deterministic
id (`provcat-{key}`) so a re-seed updates in place rather than
duplicating. Custom entries get `source="custom"`.

Vocabulary (`source`) is validated with `@validates` rather than a
Postgres ENUM so renames survive without an ALTER TYPE (mirrors
llm_catalog.py).
"""

from typing import Any, Dict, Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    JSON,
    LargeBinary,
    String,
    Text,
)
from sqlalchemy.orm import validates
from sqlalchemy.sql import func

from app.database import Base


# ── Vocabularies ─────────────────────────────────────────────────────────

PROVIDER_CATALOG_SOURCE_BUILTIN = "builtin"
PROVIDER_CATALOG_SOURCE_CUSTOM = "custom"

PROVIDER_CATALOG_SOURCES = frozenset({
    PROVIDER_CATALOG_SOURCE_BUILTIN,
    PROVIDER_CATALOG_SOURCE_CUSTOM,
})


def seeded_provider_catalog_id(key: str) -> str:
    """Deterministic id for a seeded provider-catalog entry so re-seed is
    idempotent (update-in-place, never duplicate)."""
    return f"provcat-{key}"


class LlmProviderCatalog(Base):
    """One global, browseable provider definition. Carries exactly the
    descriptive + default metadata needed to (a) render a provider row in
    the admin "add a model → pick a provider" dropdown and (b) materialize
    a real per-tenant `llm_providers` row on demand. Connection secrets /
    runtime config live on the materialized `llm_providers` instance, not
    here."""

    __tablename__ = "llm_provider_catalog"

    id = Column(
        String(64),
        primary_key=True,
        comment=(
            "Deterministic for seeded rows ('provcat-{key}') so re-seed "
            "is idempotent; 'provcat-{ULID}' for custom rows."
        ),
    )

    # The provider slug the Rust adapter dispatch matches on, e.g.
    # 'openai' / 'anthropic' / 'fal_ai'. UNIQUE across the global catalog.
    key = Column(String(64), nullable=False, unique=True)
    display_name = Column(String(255), nullable=False)
    category = Column(
        String(32),
        nullable=True,
        comment=(
            "Loose grouping for the admin UI: chat | image | audio | "
            "embeddings | self_hosted | internal."
        ),
    )

    # ── Documentation links (human-facing click-outs) ──
    developer_url = Column(String(1024), nullable=True)
    api_key_url = Column(String(1024), nullable=True)
    pricing_url = Column(String(1024), nullable=True)
    model_catalog_url = Column(String(1024), nullable=True)
    icon_url = Column(String(1024), nullable=True)

    # Machine-callable model-listing endpoint used by the catalog-probe
    # health check (mirrors llm_providers.models_api_url).
    models_api_url = Column(String(1024), nullable=True)

    # ── Defaults copied onto a materialized llm_providers row ──
    default_api_url = Column(String(1024), nullable=True)
    default_auth_type = Column(
        String(32),
        nullable=True,
        comment="api_key | oauth2 | gcp_service_account | aws_iam | none",
    )
    additional_drop_params = Column(
        JSON,
        nullable=True,
        comment=(
            "JSON array of parameter names every model under this provider "
            "must drop from outgoing request bodies."
        ),
    )
    region = Column(
        String(32),
        nullable=True,
        comment=(
            "Geographic region this provider serves from, or 'global'. "
            "NULL = unspecified (operator-picked at config time)."
        ),
    )

    notes = Column(Text, nullable=True)

    # ── Icon storage (bundled SVG bytes) — mirrors llm_providers icon
    # cols so `upsert_provider_for_key` can copy the blob onto the
    # materialized row without re-reading the filesystem. ──
    icon_data = Column(LargeBinary, nullable=True)
    icon_mime_type = Column(String(100), nullable=True)
    icon_size = Column(Integer, nullable=True)
    icon_hash = Column(String(64), nullable=True)

    source = Column(
        String(16),
        nullable=False,
        default=PROVIDER_CATALOG_SOURCE_BUILTIN,
        server_default=PROVIDER_CATALOG_SOURCE_BUILTIN,
    )

    # Traffic Data Import — global capability descriptor. Tells the
    # UI + worker whether this provider supports pulling usage/cost telemetry,
    # which adapter realizes it, and what that vendor API can yield.
    supports_usage_import = Column(
        Boolean, nullable=False, server_default="false", default=False,
        comment="Whether a traffic-data-import adapter exists for this provider",
    )
    import_api = Column(
        String(64), nullable=True,
        comment="Adapter key (registry import_api), e.g. anthropic_enterprise_analytics",
    )
    import_endpoint_tmpl = Column(
        String(512), nullable=True,
        comment="Default base URL / path template for the import API",
    )
    import_capabilities = Column(
        JSON, nullable=True,
        comment="{cost,per_user,per_workspace,grain,surfaces,query_window_days,...}",
    )

    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_llm_provider_catalog_category", "category"),
    )

    @validates("source")
    def _validate_source(self, _key: str, value: str) -> str:
        if value not in PROVIDER_CATALOG_SOURCES:
            raise ValueError(
                f"Invalid source {value!r}; expected one of "
                f"{sorted(PROVIDER_CATALOG_SOURCES)}"
            )
        return value

    def has_icon(self) -> bool:
        """True when a bundled icon has been seeded for this row."""
        return self.icon_data is not None

    def to_dict(self) -> Dict[str, Any]:
        """Dictionary for API responses. Every JSON-serialisable column
        appears here — the Pydantic response model declares the same keys
        (the strip-undeclared-fields gotcha). The icon blob itself is
        never returned in JSON; clients render `icon_url` (or the served
        per-tenant blob once a provider is materialized)."""
        return {
            "id": self.id,
            "key": self.key,
            "display_name": self.display_name,
            "category": self.category,
            "developer_url": self.developer_url,
            "api_key_url": self.api_key_url,
            "pricing_url": self.pricing_url,
            "model_catalog_url": self.model_catalog_url,
            "models_api_url": self.models_api_url,
            "icon_url": self.icon_url,
            "has_icon": self.has_icon(),
            "default_api_url": self.default_api_url,
            "default_auth_type": self.default_auth_type,
            "additional_drop_params": self.additional_drop_params,
            "region": self.region,
            "notes": self.notes,
            "source": self.source,
            "supports_usage_import": self.supports_usage_import,
            "import_api": self.import_api,
            "import_endpoint_tmpl": self.import_endpoint_tmpl,
            "import_capabilities": self.import_capabilities,
            "created_at": (
                self.created_at.isoformat() if self.created_at else None
            ),
            "updated_at": (
                self.updated_at.isoformat() if self.updated_at else None
            ),
        }

    def __repr__(self) -> str:
        return (
            f"<LlmProviderCatalog id={self.id!r} key={self.key!r} "
            f"source={self.source!r}>"
        )


__all__ = [
    "LlmProviderCatalog",
    "PROVIDER_CATALOG_SOURCES",
    "PROVIDER_CATALOG_SOURCE_BUILTIN",
    "PROVIDER_CATALOG_SOURCE_CUSTOM",
    "seeded_provider_catalog_id",
]
