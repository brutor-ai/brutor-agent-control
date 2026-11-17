# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Global LLM Catalog

Splits the *catalog* (the browseable list of models Brutor knows how to
talk to) from *configured instances* (`llm_model` rows a tenant has
actually wired up with an API key and assigned to a resource group).

Historically the FULL catalog was seeded PER-TENANT as `llm_model` rows
distinguished only by `enabled` — a design smell: every tenant carried
~300 disabled rows that were really just a static catalog. This table is
the single, **GLOBAL** (no tenant_id) source of truth for "what models
can be browsed and imported". A tenant imports an entry → a real
`llm_model` row is created with `catalog_id` pointing back here for
provenance (so the asset registry can resolve the catalog icon and we
know an instance's origin).

Seeded entries are idempotent on `(provider_key, model_name)` and use a
deterministic id (`llmcat-{provider_key}-{slug(model_name)}`) so a
re-seed updates in place rather than duplicating. Custom entries get a
`llmcat-{ULID}` id and `source="custom"`.

Vocabularies (mode/source) are validated with `@validates` rather than
Postgres ENUMs so renames survive migrations without an ALTER TYPE
(mirrors ai_asset.py / agent_card.py).
"""

from typing import Any, Dict, Optional

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import validates
from sqlalchemy.sql import func

from app.constants.llm import LLModelMode
from app.database import Base


# ── Vocabularies ─────────────────────────────────────────────────────────

# Provenance of a catalog row.
CATALOG_SOURCE_BUILTIN = "builtin"
CATALOG_SOURCE_CUSTOM = "custom"

CATALOG_SOURCES = frozenset({
    CATALOG_SOURCE_BUILTIN,
    CATALOG_SOURCE_CUSTOM,
})

# Valid catalog modes mirror the runtime LLModelMode vocabulary so an
# imported catalog entry maps straight onto an `llm_model.mode` without a
# translation step.
CATALOG_MODES = frozenset(m.value for m in LLModelMode)


class LlmCatalog(Base):
    """One global, browseable model definition. Lean by design — only the
    descriptive metadata needed to render a catalog row and seed a fresh
    `llm_model` import. Connection / auth / governance live on the
    `llm_model` instance, not here."""

    __tablename__ = "llm_catalog"

    id = Column(
        String(255),
        primary_key=True,
        comment=(
            "Deterministic for seeded rows "
            "('llmcat-{provider_key}-{model_name-slug}') so re-seed is "
            "idempotent; 'llmcat-{ULID}' for custom rows."
        ),
    )

    provider_key = Column(
        String(64),
        nullable=False,
        comment="Provider slug, e.g. openai / anthropic / bedrock.",
    )
    model_name = Column(
        String(200),
        nullable=False,
        comment="Provider API model id, e.g. gpt-4o, claude-sonnet-4-5.",
    )
    display_name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)

    mode = Column(
        String(32),
        nullable=False,
        default=LLModelMode.CHAT.value,
        comment=(
            "Model interface mode (mirrors LLModelMode): chat | "
            "embedding | image_generation | audio_speech | "
            "audio_transcription | video_generation | moderation | ..."
        ),
    )

    # Free-form capability flags (vision / function_calling / etc.).
    capabilities = Column(JSONB, nullable=True)

    context_window = Column(Integer, nullable=True)
    max_output_tokens = Column(Integer, nullable=True)

    default_cost_per_input_token = Column(Float, nullable=True)
    default_cost_per_output_token = Column(Float, nullable=True)

    # =====================================================================
    # Per-model import-time DEFAULTS
    #
    # These mirror the model-intrinsic media / behaviour columns on
    # `llm_model`. The lean catalog originally dropped them, which meant
    # `import_to_model` could not copy a model's media whitelists / per-unit
    # costs / reasoning + drop-param behaviour onto the created instance —
    # so an imported audio/image/video model silently lost its validation
    # whitelist and price (the import regression). Types mirror the same
    # columns in `llm_model.py` (JSONB for whitelists/lists/blobs, Float for
    # costs + parameters, Integer for max_* caps, Boolean for the flag).
    # `cost_per_audio_second` is intentionally NOT carried — it is dead and
    # being dropped from `llm_model` in this same migration.
    # =====================================================================

    # Image generation (mode='image_generation')
    cost_per_image = Column(Float, nullable=True)
    supported_image_sizes = Column(JSONB, nullable=True)
    supported_image_qualities = Column(JSONB, nullable=True)
    max_images_per_request = Column(Integer, nullable=True)

    # Audio TTS (mode='audio_speech')
    cost_per_audio_char = Column(Float, nullable=True)
    supported_voices = Column(JSONB, nullable=True)
    supported_audio_formats = Column(JSONB, nullable=True)

    # Audio STT (mode='audio_transcription')
    cost_per_audio_minute = Column(Float, nullable=True)
    max_audio_duration_seconds = Column(Integer, nullable=True)
    supported_input_formats = Column(JSONB, nullable=True)
    supported_response_formats = Column(JSONB, nullable=True)

    # Video generation (mode='video_generation')
    cost_per_video_second = Column(Float, nullable=True)
    supported_video_resolutions = Column(JSONB, nullable=True)
    supported_video_durations = Column(JSONB, nullable=True)
    max_video_duration_seconds = Column(Integer, nullable=True)

    # Reasoning / param behaviour
    thinking_enabled = Column(Boolean, nullable=True)
    additional_drop_params = Column(JSONB, nullable=True)

    # Intrinsic descriptive facts
    parameters = Column(Float, nullable=True)  # model size in billions
    tags = Column(JSONB, nullable=True)
    retire_date = Column(Date, nullable=True)

    icon_url = Column(String(1024), nullable=True)

    recommended = Column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    deprecated = Column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    source = Column(
        String(16),
        nullable=False,
        default=CATALOG_SOURCE_BUILTIN,
        server_default=CATALOG_SOURCE_BUILTIN,
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
        UniqueConstraint(
            "provider_key", "model_name", name="uix_llm_catalog_provider_model"
        ),
        Index("ix_llm_catalog_provider", "provider_key"),
        Index("ix_llm_catalog_mode", "mode"),
        Index("ix_llm_catalog_recommended", "recommended"),
    )

    # ── Validators ───────────────────────────────────────────────────────

    @validates("mode")
    def _validate_mode(self, _key: str, value: str) -> str:
        if value not in CATALOG_MODES:
            raise ValueError(
                f"Invalid mode {value!r}; expected one of {sorted(CATALOG_MODES)}"
            )
        return value

    @validates("source")
    def _validate_source(self, _key: str, value: str) -> str:
        if value not in CATALOG_SOURCES:
            raise ValueError(
                f"Invalid source {value!r}; expected one of "
                f"{sorted(CATALOG_SOURCES)}"
            )
        return value

    @property
    def requires_api_url(self) -> bool:
        """True for a template: a model of this provider and mode has no
        destination until it is given an `api_url` (the customer's own
        Azure resource, Databricks workspace, self-hosted server, ...). The
        gateway knows no base URL for it and refuses a row without one
        (`app/services/llm/destination.py`)."""
        from app.services.llm.destination import requires_api_url

        return requires_api_url((self.provider_key or "").lower(), self.mode, self.model_name)

    def to_dict(self) -> Dict[str, Any]:
        """Dictionary for API responses. Every column appears here — the
        Pydantic response model declares the same keys (the strip-
        undeclared-fields gotcha)."""
        return {
            "id": self.id,
            "provider_key": self.provider_key,
            "model_name": self.model_name,
            "display_name": self.display_name,
            "description": self.description,
            "mode": self.mode,
            "capabilities": self.capabilities,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "default_cost_per_input_token": self.default_cost_per_input_token,
            "default_cost_per_output_token": self.default_cost_per_output_token,
            # Per-model import-time defaults
            "cost_per_image": self.cost_per_image,
            "supported_image_sizes": self.supported_image_sizes,
            "supported_image_qualities": self.supported_image_qualities,
            "max_images_per_request": self.max_images_per_request,
            "cost_per_audio_char": self.cost_per_audio_char,
            "cost_per_audio_minute": self.cost_per_audio_minute,
            "supported_voices": self.supported_voices,
            "supported_audio_formats": self.supported_audio_formats,
            "supported_input_formats": self.supported_input_formats,
            "supported_response_formats": self.supported_response_formats,
            "max_audio_duration_seconds": self.max_audio_duration_seconds,
            "cost_per_video_second": self.cost_per_video_second,
            "supported_video_resolutions": self.supported_video_resolutions,
            "supported_video_durations": self.supported_video_durations,
            "max_video_duration_seconds": self.max_video_duration_seconds,
            "thinking_enabled": self.thinking_enabled,
            "additional_drop_params": self.additional_drop_params,
            "parameters": self.parameters,
            "tags": self.tags,
            "retire_date": (
                self.retire_date.isoformat() if self.retire_date else None
            ),
            "icon_url": self.icon_url,
            # Not a column: the gateway's rule, applied to this entry.
            "requires_api_url": self.requires_api_url,
            "recommended": self.recommended,
            "deprecated": self.deprecated,
            "source": self.source,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:
        return (
            f"<LlmCatalog id={self.id!r} provider={self.provider_key!r} "
            f"model={self.model_name!r} mode={self.mode!r}>"
        )


def slug_model_name(model_name: str) -> str:
    """Filesystem/url-safe slug for a model name, used to build the
    deterministic seeded catalog id. Lowercase, non-alnum → '-',
    collapsed and trimmed."""
    out = []
    prev_dash = False
    for ch in (model_name or "").lower():
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        else:
            if not prev_dash:
                out.append("-")
                prev_dash = True
    return "".join(out).strip("-")


def seeded_catalog_id(provider_key: str, model_name: str) -> str:
    """Deterministic id for a seeded catalog entry so re-seed is
    idempotent (update-in-place, never duplicate)."""
    return f"llmcat-{provider_key}-{slug_model_name(model_name)}"


__all__ = [
    "LlmCatalog",
    "CATALOG_MODES",
    "CATALOG_SOURCES",
    "CATALOG_SOURCE_BUILTIN",
    "CATALOG_SOURCE_CUSTOM",
    "slug_model_name",
    "seeded_catalog_id",
]
