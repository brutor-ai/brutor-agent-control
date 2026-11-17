# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Pydantic request/response models for the global LLM Catalog admin API.

Response shapes are built from `LlmCatalog.to_dict()` via
`Model(**row.to_dict())`, so the response field names MUST stay in
lockstep with the to_dict keys (the Pydantic strip-undeclared-fields
gotcha).
"""

from datetime import date
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, ConfigDict

from app.database.models.llm_catalog import CATALOG_MODES

# Sorted literal for OpenAPI docs — derived from the model frozenset so
# it cannot drift from the @validates vocabulary.
_CATALOG_MODES = sorted(CATALOG_MODES)
_MODE_DESC = f"Model interface mode. One of: {_CATALOG_MODES}"


# =============================================================================
# Request models
# =============================================================================


class LlmCatalogImportRequest(BaseModel):
    """Optional overrides applied when importing a catalog entry into a
    tenant as a configured `llm_model`.

    Every field is optional — an empty body imports the entry with the
    catalog's descriptive defaults, `enabled=True`, no api_key.
    """

    name: Optional[str] = Field(
        None, max_length=200, description="Override the instance display name"
    )
    enabled: Optional[bool] = Field(
        None, description="Override the imported instance's enabled flag"
    )
    api_key: Optional[str] = Field(
        None, description="API key to store (encrypted at rest) on the instance"
    )
    api_url: Optional[str] = Field(
        None,
        max_length=500,
        description=(
            "The model's own endpoint. Required for an entry whose `requires_api_url` is true; "
            "the import is refused (422) without it."
        ),
    )
    group_ids: Optional[List[str]] = Field(
        None,
        description=(
            "Resource group ids to assign the imported model to. Empty / "
            "null assigns the tenant Default group (accessible to all users)."
        ),
    )


    @field_validator("api_url")
    @classmethod
    def _api_url_passes_the_outbound_policy(cls, v: Optional[str]) -> Optional[str]:
        # The same refusal as on a model created directly (`LLModelCreate`).
        if v and v.strip():
            from app.services.outbound_policy import check_url, refusal_message
            reason = check_url(v)
            if reason:
                raise ValueError(refusal_message(v, "api_url", reason))
        return v


class LlmCatalogCreateRequest(BaseModel):
    """Create a custom global catalog entry (system-admin only).

    `provider_key`, `model_name`, `display_name` and `mode` are required;
    everything else is optional descriptive metadata. `(provider_key,
    model_name)` must be unique across the catalog (409 otherwise)."""

    provider_key: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Provider slug, e.g. openai / anthropic / bedrock.",
    )
    model_name: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Provider API model id, e.g. gpt-5.2.",
    )
    display_name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = Field(None)
    mode: str = Field(..., description=_MODE_DESC)
    capabilities: Optional[Dict[str, Any]] = Field(
        None, description="Free-form capability flags (vision / etc.)."
    )
    context_window: Optional[int] = Field(None, ge=0)
    max_output_tokens: Optional[int] = Field(None, ge=0)
    default_cost_per_input_token: Optional[float] = Field(None, ge=0)
    default_cost_per_output_token: Optional[float] = Field(None, ge=0)
    icon_url: Optional[str] = Field(None, max_length=1024)
    recommended: bool = Field(False)
    deprecated: bool = Field(False)

    # Per-model import-time defaults — copied onto an imported
    # llm_model so media whitelists / per-unit costs / reasoning behaviour
    # survive the catalog → instance import.
    cost_per_image: Optional[float] = Field(None, ge=0)
    supported_image_sizes: Optional[List[str]] = None
    supported_image_qualities: Optional[List[str]] = None
    max_images_per_request: Optional[int] = Field(None, ge=1)
    cost_per_audio_char: Optional[float] = Field(None, ge=0)
    cost_per_audio_minute: Optional[float] = Field(None, ge=0)
    supported_voices: Optional[List[str]] = None
    supported_audio_formats: Optional[List[str]] = None
    supported_input_formats: Optional[List[str]] = None
    supported_response_formats: Optional[List[str]] = None
    max_audio_duration_seconds: Optional[int] = Field(None, ge=1)
    cost_per_video_second: Optional[float] = Field(None, ge=0)
    supported_video_resolutions: Optional[List[str]] = None
    supported_video_durations: Optional[List[int]] = None
    max_video_duration_seconds: Optional[int] = Field(None, ge=1)
    thinking_enabled: Optional[bool] = None
    additional_drop_params: Optional[List[str]] = None
    parameters: Optional[float] = Field(None, ge=0)
    tags: Optional[List[str]] = None
    retire_date: Optional[date] = None


class LlmCatalogUpdateRequest(BaseModel):
    """Partial update of a catalog entry (system-admin only). Every field
    is optional; only the fields actually supplied are written.

    `provider_key` / `model_name` are immutable (changing them would break
    provenance on already-imported models) so they are not exposed here."""

    model_config = ConfigDict(extra="forbid")

    display_name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = Field(None)
    mode: Optional[str] = Field(None, description=_MODE_DESC)
    capabilities: Optional[Dict[str, Any]] = Field(None)
    context_window: Optional[int] = Field(None, ge=0)
    max_output_tokens: Optional[int] = Field(None, ge=0)
    default_cost_per_input_token: Optional[float] = Field(None, ge=0)
    default_cost_per_output_token: Optional[float] = Field(None, ge=0)
    icon_url: Optional[str] = Field(None, max_length=1024)
    recommended: Optional[bool] = Field(None)
    deprecated: Optional[bool] = Field(None)

    # Per-model import-time defaults
    cost_per_image: Optional[float] = Field(None, ge=0)
    supported_image_sizes: Optional[List[str]] = None
    supported_image_qualities: Optional[List[str]] = None
    max_images_per_request: Optional[int] = Field(None, ge=1)
    cost_per_audio_char: Optional[float] = Field(None, ge=0)
    cost_per_audio_minute: Optional[float] = Field(None, ge=0)
    supported_voices: Optional[List[str]] = None
    supported_audio_formats: Optional[List[str]] = None
    supported_input_formats: Optional[List[str]] = None
    supported_response_formats: Optional[List[str]] = None
    max_audio_duration_seconds: Optional[int] = Field(None, ge=1)
    cost_per_video_second: Optional[float] = Field(None, ge=0)
    supported_video_resolutions: Optional[List[str]] = None
    supported_video_durations: Optional[List[int]] = None
    max_video_duration_seconds: Optional[int] = Field(None, ge=1)
    thinking_enabled: Optional[bool] = None
    additional_drop_params: Optional[List[str]] = None
    parameters: Optional[float] = Field(None, ge=0)
    tags: Optional[List[str]] = None
    retire_date: Optional[date] = None


# =============================================================================
# Response models
# =============================================================================


class LlmCatalogEntryResponse(BaseModel):
    """A single global catalog entry. Field names mirror
    `LlmCatalog.to_dict()`."""

    id: str
    provider_key: str
    model_name: str
    display_name: str
    description: Optional[str] = None
    mode: str
    capabilities: Optional[Dict[str, Any]] = None
    context_window: Optional[int] = None
    max_output_tokens: Optional[int] = None
    default_cost_per_input_token: Optional[float] = None
    default_cost_per_output_token: Optional[float] = None
    # Per-model import-time defaults. Mirror LlmCatalog.to_dict().
    cost_per_image: Optional[float] = None
    supported_image_sizes: Optional[List[str]] = None
    supported_image_qualities: Optional[List[str]] = None
    max_images_per_request: Optional[int] = None
    cost_per_audio_char: Optional[float] = None
    cost_per_audio_minute: Optional[float] = None
    supported_voices: Optional[List[str]] = None
    supported_audio_formats: Optional[List[str]] = None
    supported_input_formats: Optional[List[str]] = None
    supported_response_formats: Optional[List[str]] = None
    max_audio_duration_seconds: Optional[int] = None
    cost_per_video_second: Optional[float] = None
    supported_video_resolutions: Optional[List[str]] = None
    supported_video_durations: Optional[List[int]] = None
    max_video_duration_seconds: Optional[int] = None
    thinking_enabled: Optional[bool] = None
    additional_drop_params: Optional[List[str]] = None
    parameters: Optional[float] = None
    tags: Optional[List[str]] = None
    retire_date: Optional[str] = None
    icon_url: Optional[str] = None
    requires_api_url: bool = Field(
        description=(
            "True when a model of this provider and mode has no destination without its own "
            "`api_url`: the gateway knows no base URL for it (an Azure resource, a Databricks "
            "workspace, a self-hosted server). Importing such an entry needs `api_url`."
        ),
    )
    recommended: bool
    deprecated: bool
    source: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class LlmCatalogListResponse(BaseModel):
    catalog: List[LlmCatalogEntryResponse]
    total: int
