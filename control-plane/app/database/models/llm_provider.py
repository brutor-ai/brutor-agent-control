# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
LlmProvider — first-class entity replacing the hardcoded
`LLModelProvider` enum as the source of truth for "what providers
does Brutor talk to".

Why this exists
---------------
The enum approach worked for the first dozen providers but stops
scaling once operators want their own (custom HuggingFace
endpoints, internal vLLM clusters, regional Bedrock variants). It
also duplicated "what providers exist" across:

  * `app/constants/llm.py::LLModelProvider` enum (Python source
    of truth)
  * `init_llms.py::PROVIDER_TEMPLATES` dict (per-provider seed
    defaults)
  * `init_llms.py::PROVIDER_REGISTRY` list (which seed files to
    load)
  * Rust adapter dispatch (`routes/audio_speech.rs`,
    `routes/images.rs`, etc. — string match arms)
  * Admin UI dropdown (hardcoded TS array)

Promoting providers to a DB row collapses all five into a single
list operators can curate via the admin UI + REST API. The Rust
adapter dispatch still matches on a slug, but the slug now comes
from `llm_provider.key` instead of an enum value baked into the
model row.

Migration
---------
`alembic/versions/.../a027_llm_providers_table.py` creates this
table and backfills one row per distinct
`(tenant_id, lower(provider))` pair seen in `llm_model`. The
`llm_model.provider` string column was dropped at the same time;
`llm_model.provider_id` (FK → `llm_providers.id`) is the new
source of truth.

System vs operator-created
--------------------------
Providers seeded by `init_llm_providers.py` get `is_system=True`.
Operators creating providers via the admin UI (or auto-creation
during HuggingFace import) get `is_system=False`. System rows
cannot be deleted via the admin API; they can be edited or
soft-disabled. Non-system rows can be deleted, but only if no
`llm_model` row still references them.
"""
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func, text

from app.database.models.resource_mixins import ResourceAuthorshipMixin, ResourceVersionMixin
from app.database import Base


class LlmProvider(ResourceAuthorshipMixin, ResourceVersionMixin, Base):
    """Per-tenant LLM provider record."""

    __tablename__ = "llm_providers"

    # ---------------------------------------------------------------------
    # Identity
    # ---------------------------------------------------------------------
    id = Column(
        String(64),
        primary_key=True,
        comment=(
            "Stable ULID-based id ('prov-…'). Foreign-keyed from "
            "llm_model.provider_id."
        ),
    )

    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )

    # ---------------------------------------------------------------------
    # Slug + display
    # ---------------------------------------------------------------------
    key = Column(
        String(64),
        nullable=False,
        comment=(
            "Lowercase slug the Rust adapter dispatch matches on "
            "(e.g. 'openai', 'fal_ai', 'replicate'). Unique per "
            "tenant. Edits to this column will break running "
            "models — there's a route-level guard against changing "
            "it once a model references the provider."
        ),
    )
    display_name = Column(
        String(255),
        nullable=False,
        comment="Human-readable name surfaced in the admin UI.",
    )
    category = Column(
        String(32),
        nullable=True,
        comment=(
            "Loose grouping for the admin UI: chat | image | audio | "
            "embeddings | self_hosted | internal."
        ),
    )

    # ---------------------------------------------------------------------
    # Documentation links — human-facing click-out anchors. Surface
    # in the admin UI as the row's "Open developer docs", "Get API
    # key", "Pricing", "Models catalog" links. None of these are
    # ever read by the proxy or any backend service — they exist
    # purely to save operators a Google search.
    # ---------------------------------------------------------------------
    developer_url = Column(String(1024), nullable=True)
    api_key_url = Column(String(1024), nullable=True)
    pricing_url = Column(String(1024), nullable=True)
    # Docs page describing the provider's model line-up
    # (e.g. https://platform.openai.com/docs/models). Operator
    # reads this; proxy never calls it. For the machine-callable
    # /models endpoint the proxy hits during health checks, see
    # `models_api_url` below.
    model_catalog_url = Column(String(1024), nullable=True)
    icon_url = Column(String(1024), nullable=True)

    # Machine-callable model-listing API endpoint
    # (e.g. https://api.openai.com/v1/models). The proxy's
    # catalog-probe health check issues `GET <models_api_url>` with
    # the model's API key, verifying auth + reachability +
    # model-name presence on the provider side at zero cost (most
    # providers' /models endpoints are free). Distinct from
    # `model_catalog_url` above, which is a human-facing docs link.
    # NULL → no catalog probe available; the health check falls
    # back to a real-inference probe for the model's mode.
    # Self-hosted providers (vLLM, LM Studio) use the literal
    # `<MODEL_API_URL>` sentinel as a placeholder; the proxy
    # substitutes the model row's `api_url` at probe time so a
    # vLLM deployment at https://vllm.example/v1 probes
    # https://vllm.example/v1/models.
    models_api_url = Column(
        String(1024),
        nullable=True,
        comment=(
            "Provider's machine-callable /models endpoint "
            "(e.g. https://api.openai.com/v1/models). Used by "
            "the catalog-probe health check; NULL falls back to "
            "a real-inference probe. NOT to be confused with "
            "model_catalog_url which is a human-facing docs link."
        ),
    )

    # ---------------------------------------------------------------------
    # Defaults applied to new models created against this provider.
    # The Rust proxy still reads `api_url` / `auth_type` from the
    # model row (which usually overrides), but the per-provider
    # defaults populate the admin form when an operator creates a
    # new model.
    # ---------------------------------------------------------------------
    default_api_url = Column(String(1024), nullable=True)
    default_auth_type = Column(
        String(32),
        nullable=True,
        comment=(
            "api_key | oauth2 | gcp_service_account | aws_iam | none"
        ),
    )

    # ---------------------------------------------------------------------
    # Provider-wide drop list — UNLIKE the `default_*` columns above
    # (which only pre-fill the admin form when an operator creates a
    # new model), this list is INHERITED at runtime by every model
    # under this provider. The proxy unions provider.additional_drop_params
    # with the model's own additional_drop_params before stripping the
    # request body. Use this for params an entire provider's API
    # rejects (e.g. xAI rejects `stop`/`frequency_penalty`; minimax/
    # moonshot/deepseek/groq reject `logit_bias`/`stream_options`).
    # Use the model row's additional_drop_params for per-model
    # exceptions on top.
    # ---------------------------------------------------------------------
    additional_drop_params = Column(
        JSON,
        nullable=True,
        comment=(
            "JSON array of parameter names every model under this "
            "provider must drop from outgoing request bodies."
        ),
    )

    # ---------------------------------------------------------------------
    # Free-form
    # ---------------------------------------------------------------------
    notes = Column(
        Text,
        nullable=True,
        comment="Operator-facing notes (markdown-friendly).",
    )
    metadata_ = Column(
        "metadata",
        JSON,
        nullable=True,
        comment=(
            "Provider-specific fields that don't deserve their own "
            "column yet (rate-limit hints, regional endpoints, "
            "feature flags). The ORM attribute is `metadata_` "
            "because SQLAlchemy reserves `metadata` on Base."
        ),
    )

    # ---------------------------------------------------------------------
    # Icon storage — mirrors `mcp_servers`. The seed
    # pipeline (`init_llm_providers.create_llm_providers`) populates
    # these from `app/database/provider_icons/{key}.svg` bundles for
    # the catalogue providers. Operators can upload / replace via
    # `POST /v1/admin/providers/{id}/icon` (delegates to the
    # `LlmProviderIconManager` service for validation + dedup).
    # ---------------------------------------------------------------------
    icon_data = Column(LargeBinary, nullable=True)
    icon_mime_type = Column(String(100), nullable=True)
    icon_size = Column(Integer, nullable=True)
    icon_hash = Column(String(64), nullable=True)
    icon_uploaded_at = Column(DateTime(timezone=True), nullable=True)

    # ---------------------------------------------------------------------
    # Status
    # ---------------------------------------------------------------------
    enabled = Column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
        comment=(
            "Soft-disable. When false, the provider's models are "
            "filtered out of the portal's model picker."
        ),
    )
    is_system = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment=(
            "True for providers seeded by `init_llm_providers.py`. "
            "Cannot be deleted via the admin API; can be edited or "
            "soft-disabled."
        ),
    )

    # ---------------------------------------------------------------------
    # Data residency
    # ---------------------------------------------------------------------
    # Region this provider serves traffic from. NULL = unspecified.
    # The runtime residency check uses this to gate which providers
    # a tenant can route to. Values like us-east-1, eu-west-1,
    # ap-southeast-2, global. "global" is a sentinel meaning "no
    # specific region commitment" (e.g. OpenAI's default endpoint);
    # the tenant's `block_global` flag can refuse it.
    region = Column(
        String(32),
        nullable=True,
        comment=(
            "Geographic region this provider serves from (us-east-1, "
            "eu-west-1, etc.). Read by the Rust residency check. "
            "NULL = unspecified, treated as matching only when the "
            "tenant has no profile or block_global is false."
        ),
    )

    # ---------------------------------------------------------------------
    # Catalog provenance — back-ref to the global llm_provider_catalog
    # entry this provider was materialized from (NULL for hand-rolled
    # providers). Lets the asset registry resolve a catalog icon and tells
    # us a provider row's origin. SET NULL on catalog delete so the
    # materialized provider survives a catalog cleanup.
    # ---------------------------------------------------------------------
    catalog_id = Column(
        String(64),
        ForeignKey("llm_provider_catalog.id", ondelete="SET NULL"),
        nullable=True,
        comment=(
            "Origin llm_provider_catalog.id this provider was "
            "materialized from (provenance)."
        ),
    )

    # ---------------------------------------------------------------------
    # Traffic Data Import — per-tenant import settings. The
    # configured provider row IS the import connector. Credentials are the
    # admin/analytics key (distinct from the inference api_key), Fernet-
    # encrypted at rest and never serialized.
    # ---------------------------------------------------------------------
    import_enabled = Column(
        Boolean, nullable=False, server_default="false", default=False,
        comment="Whether scheduled usage import runs for this provider",
    )
    import_credentials_encrypted = Column(
        Text, nullable=True,
        comment="Fernet-encrypted admin/analytics API key (write-only)",
    )
    import_endpoint_override = Column(
        String(512), nullable=True,
        comment="Override the adapter's default base URL (e.g. analytics path)",
    )
    import_scope = Column(
        JSON, nullable=True,
        comment="org/project/workspace/region scope passed to the adapter",
    )
    import_mapping_policy = Column(
        JSON, nullable=True,
        comment="Identity reconciliation policy (auto_create|quarantine, maps)",
    )
    import_schedule_cron = Column(
        String(128), nullable=True,
        comment="Cron expression for scheduled import (NULL = manual only)",
    )
    import_next_run_at = Column(
        DateTime(timezone=True), nullable=True,
        comment="Next due time; the scheduler claims rows where this <= now()",
    )
    import_backfill_through = Column(
        Date, nullable=True,
        comment="Oldest date to pull on first/backfill run",
    )

    # ---------------------------------------------------------------------
    # Timestamps
    # ---------------------------------------------------------------------
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # ---------------------------------------------------------------------
    # Relationships
    # ---------------------------------------------------------------------
    models = relationship(
        "LLModel",
        back_populates="provider",
        cascade="save-update, merge",
        passive_deletes=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "key", name="uq_llm_providers_tenant_key"
        ),
        Index("ix_llm_providers_tenant", "tenant_id"),
        Index("ix_llm_providers_tenant_enabled", "tenant_id", "enabled"),
        Index("ix_llm_providers_icon_hash", "icon_hash"),
        # Import scheduler due-poll — partial: only import-enabled
        # providers. The same index the migration creates.
        Index(
            "ix_llm_providers_import_due",
            "import_next_run_at",
            postgresql_where=text("import_enabled"),
        ),
    )

    def has_icon(self) -> bool:
        """True when an icon has been uploaded / seeded for this row."""
        return self.icon_data is not None

    def to_dict(
        self, include_model_count: bool = False
    ) -> Dict[str, Any]:
        """Convert to a flat dict for the admin REST API.

        `include_model_count` adds a `model_count` field counted
        from the loaded relationship — caller is responsible for
        eager-loading or bouncing through a separate query if the
        relationship isn't loaded.
        """
        data: Dict[str, Any] = {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "key": self.key,
            "display_name": self.display_name,
            "category": self.category,
            "developer_url": self.developer_url,
            "api_key_url": self.api_key_url,
            "pricing_url": self.pricing_url,
            "model_catalog_url": self.model_catalog_url,
            "models_api_url": self.models_api_url,
            "icon_url": self.icon_url,
            # `has_icon` says whether the GET icon endpoint has bytes
            # to serve. `icon_uploaded_at` is the Last-Modified hint
            # for client-side cache busting. The blob itself is never
            # returned in JSON — clients fetch it via
            # `GET /v1/admin/providers/{id}/icon`.
            "has_icon": self.has_icon(),
            "icon_mime_type": self.icon_mime_type,
            "icon_size": self.icon_size,
            "icon_uploaded_at": (
                self.icon_uploaded_at.isoformat()
                if self.icon_uploaded_at
                else None
            ),
            "default_api_url": self.default_api_url,
            "default_auth_type": self.default_auth_type,
            "additional_drop_params": self.additional_drop_params,
            "notes": self.notes,
            "metadata": self.metadata_,
            "enabled": self.enabled,
            "is_system": self.is_system,
            "region": self.region,
            "catalog_id": self.catalog_id,
            # Traffic Data Import. The encrypted key is never
            # serialized; expose only whether one is set so the UI can render
            # "(leave blank to keep)".
            "import_enabled": self.import_enabled,
            "import_credentials_set": bool(self.import_credentials_encrypted),
            "import_endpoint_override": self.import_endpoint_override,
            "import_scope": self.import_scope,
            "import_mapping_policy": self.import_mapping_policy,
            "import_schedule_cron": self.import_schedule_cron,
            "import_next_run_at": (
                self.import_next_run_at.isoformat()
                if self.import_next_run_at
                else None
            ),
            "import_backfill_through": (
                self.import_backfill_through.isoformat()
                if self.import_backfill_through
                else None
            ),
            # AI Resource version + authorship block. Pydantic
            # strips undeclared fields, so a value missing here vanishes
            # from every GET regardless of the response model.
            "version": self.version,
            **self.authorship_dict(),
        }
        if include_model_count:
            data["model_count"] = len(self.models or [])
        return data

    def __repr__(self) -> str:
        # Safe repr — must NOT trigger lazy reload, even on a
        # detached instance.
        #
        # `__repr__` is invoked from inside exception formatting
        # paths (FastAPI's `RequestValidationError.__str__`,
        # SQLAlchemy's own debug logs, the CORS middleware's 500
        # handler at `app/middleware/cors.py:259` in particular).
        # When those error paths fire AFTER the originating
        # request's session was closed, accessing `self.id` /
        # `self.tenant_id` / `self.key` via normal attribute
        # access triggers `_load_expired` → `DetachedInstance
        # Error: Instance is not bound to a Session`. That error
        # then propagates AS the response exception, which the
        # CORS middleware can't format either, so the client
        # sees "no Access-Control-Allow-Origin header" — a
        # totally unrelated symptom.
        #
        # `inspect(self).dict` reads from the in-memory state
        # dict directly, never goes back to the DB. If the value
        # was loaded by the originating query it shows up; if it
        # wasn't, we render `<unloaded>` instead of crashing.
        from sqlalchemy import inspect as sa_inspect
        try:
            state = sa_inspect(self).dict
        except Exception:
            state = {}
        rid = state.get("id", "<unloaded>")
        tid = state.get("tenant_id", "<unloaded>")
        key = state.get("key", "<unloaded>")
        return f"<LlmProvider id={rid!r} tenant={tid!r} key={key!r}>"
