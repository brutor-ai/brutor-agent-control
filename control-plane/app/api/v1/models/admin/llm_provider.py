# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Pydantic models for the `LlmProvider` admin REST surface — the
first-class entity (there is no hardcoded provider enum).

Three shapes per the convention used elsewhere in this directory:

  * `LLProviderCreate` — POST body. Caller must supply `key`,
    `display_name`. Everything else has a sensible default or is
    operator-optional.
  * `LLProviderUpdate` — PATCH body. All fields optional; what's
    sent overrides what's stored.
  * `LLProviderResponse` — what we surface on read. Includes the
    server-managed fields (`id`, `tenant_id`, `is_system`,
    `created_at`, `updated_at`) and an opt-in `model_count` for
    the admin list view.

`LLModelResponse` (in `llm_model.py`) gains a nested
`provider_info: LLProviderResponse` and the slug
`provider: str` (read from `provider_info.key`) so existing
clients keep working.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator
from app.api.v1.models.admin.resource_fields import ResourceVersionField
from app.api.v1.models.admin.resource_fields import ResourceMetaFields


# ---------------------------------------------------------------------------
# Shared fragments
# ---------------------------------------------------------------------------

# Slug constraints — same shape as the existing `LLModelProvider` enum
# values that the migration backfilled from. Must be lowercase
# alphanumeric with optional underscores or hyphens. Prevents
# misclicks like " Openai" or "OPENAI" landing in the `key` column.
_KEY_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"

VALID_AUTH_TYPES = {
    "api_key",
    "oauth2",
    "gcp_service_account",
    "aws_iam",
    "azure_ad",
    "none",
}

VALID_CATEGORIES = {
    "chat",
    "image",
    "audio",
    "video",
    "embeddings",
    "self_hosted",
    "internal",
}


def _validate_url(value: Optional[str]) -> Optional[str]:
    """Reject obviously-bad URLs without depending on `pydantic-extras`."""
    if value is None or value == "":
        return None
    if not (value.startswith("http://") or value.startswith("https://")):
        raise ValueError(f"URL must start with http:// or https://: {value!r}")
    if len(value) > 1024:
        raise ValueError("URL too long (max 1024 chars)")
    return value


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


PROVIDER_ENABLED_DESCRIPTION = (
    "Whether the provider is enabled. Disabling a provider stops its models: the gateway "
    "serves no model of a disabled provider (direct calls, routing-group members and "
    "fallbacks alike), whatever each model's own switch says."
)

class LLProviderCreate(ResourceVersionField):
    """Body of `POST /v1/admin/providers`.

    `key` is the slug the Rust proxy adapter dispatch matches on.
    Once a provider has models attached, the key cannot be changed
    (the API will 409). Pick carefully.
    """

    model_config = ConfigDict(extra="forbid")

    key: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=_KEY_PATTERN,
        description=(
            "Lowercase slug used by the Rust proxy adapter dispatch "
            "(e.g. 'openai', 'fal_ai', 'replicate'). Unique per tenant."
        ),
    )
    display_name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Human-readable name for the admin UI.",
    )
    category: Optional[str] = Field(
        default=None,
        description=(
            "Loose grouping for the admin UI. One of: "
            f"{sorted(VALID_CATEGORIES)}."
        ),
    )
    region: Optional[str] = Field(
        default=None,
        max_length=32,
        description=(
            "Geographic region this provider serves from (us-east-1, "
            "eu-west-1, …), or 'global' for no specific commitment. Read "
            "by the residency check in the Rust proxy to decide which "
            "providers a tenant may route to, so leaving it unset is a "
            "governance decision, not a cosmetic one."
        ),
    )
    developer_url: Optional[str] = None
    api_key_url: Optional[str] = None
    pricing_url: Optional[str] = None
    # Human-facing docs link describing the provider's model
    # line-up (e.g. https://platform.openai.com/docs/models). The
    # admin UI surfaces it as a click-out anchor on the provider
    # row. Never read by the proxy. For the machine-callable
    # endpoint the proxy hits during health checks see
    # `models_api_url` below.
    model_catalog_url: Optional[str] = Field(
        default=None,
        description=(
            "Human-facing docs page about the provider's models "
            "(click-out anchor in the admin UI). Not called by the "
            "proxy. NOT to be confused with models_api_url."
        ),
    )
    # Machine-callable model-listing API endpoint
    # (e.g. https://api.openai.com/v1/models). The proxy's
    # catalog-probe health check issues `GET <models_api_url>` with
    # the model's API key. NULL → falls back to a real-inference
    # probe. NOT a docs link — see `model_catalog_url` for that.
    models_api_url: Optional[str] = Field(
        default=None,
        description=(
            "Provider's machine-callable /models API endpoint "
            "(e.g. https://api.openai.com/v1/models). Used by "
            "the proxy's catalog-probe health check; NULL falls "
            "back to a real-inference probe. NOT a docs link — "
            "see model_catalog_url for that."
        ),
    )
    icon_url: Optional[str] = None
    default_api_url: Optional[str] = None
    default_auth_type: Optional[str] = Field(
        default=None,
        description=(
            "Default auth type the admin UI will pre-select on the "
            f"new-model form. One of: {sorted(VALID_AUTH_TYPES)}."
        ),
    )
    additional_drop_params: Optional[List[str]] = Field(
        default=None,
        description=(
            "Provider-wide drop list applied at runtime to every "
            "model under this provider. Unioned with the model row's "
            "own additional_drop_params before the proxy strips the "
            "request body."
        ),
    )
    notes: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    enabled: bool = Field(True, description=PROVIDER_ENABLED_DESCRIPTION)

    @field_validator(
        "developer_url",
        "api_key_url",
        "pricing_url",
        "model_catalog_url",
        "models_api_url",
        "icon_url",
        "default_api_url",
    )
    @classmethod
    def _check_url(cls, value: Optional[str]) -> Optional[str]:
        return _validate_url(value)

    @field_validator("category")
    @classmethod
    def _check_category(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if value not in VALID_CATEGORIES:
            raise ValueError(
                f"category must be one of {sorted(VALID_CATEGORIES)}; got {value!r}"
            )
        return value

    @field_validator("default_auth_type")
    @classmethod
    def _check_auth_type(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if value not in VALID_AUTH_TYPES:
            raise ValueError(
                f"default_auth_type must be one of {sorted(VALID_AUTH_TYPES)}; got {value!r}"
            )
        return value


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------

class LLProviderUpdate(ResourceVersionField):
    """Body of `PATCH /v1/admin/providers/{id}`.

    Every field is optional. Sending `null` for a nullable field
    clears it; omitting the field leaves it unchanged. `key` is
    intentionally not editable once the provider has models — the
    endpoint will 409 if someone tries.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    region: Optional[str] = Field(
        default=None,
        max_length=32,
        description=(
            "Geographic region this provider serves from (us-east-1, "
            "eu-west-1, …), or 'global' for no specific commitment. Read "
            "by the residency check in the Rust proxy to decide which "
            "providers a tenant may route to, so leaving it unset is a "
            "governance decision, not a cosmetic one."
        ),
    )
    category: Optional[str] = None
    developer_url: Optional[str] = None
    api_key_url: Optional[str] = None
    pricing_url: Optional[str] = None
    model_catalog_url: Optional[str] = None
    models_api_url: Optional[str] = None
    icon_url: Optional[str] = None
    default_api_url: Optional[str] = None
    default_auth_type: Optional[str] = None
    additional_drop_params: Optional[List[str]] = None
    notes: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    enabled: Optional[bool] = Field(None, description=PROVIDER_ENABLED_DESCRIPTION)

    # `key` is editable only via a separate, explicitly-named endpoint
    # (POST /v1/admin/providers/{id}/rename) so a routine PATCH can't
    # silently rewrite the slug that drives proxy dispatch.

    @field_validator(
        "developer_url",
        "api_key_url",
        "pricing_url",
        "model_catalog_url",
        "models_api_url",
        "icon_url",
        "default_api_url",
    )
    @classmethod
    def _check_url(cls, value: Optional[str]) -> Optional[str]:
        return _validate_url(value)

    @field_validator("category")
    @classmethod
    def _check_category(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if value not in VALID_CATEGORIES:
            raise ValueError(
                f"category must be one of {sorted(VALID_CATEGORIES)}; got {value!r}"
            )
        return value

    @field_validator("default_auth_type")
    @classmethod
    def _check_auth_type(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if value not in VALID_AUTH_TYPES:
            raise ValueError(
                f"default_auth_type must be one of {sorted(VALID_AUTH_TYPES)}; got {value!r}"
            )
        return value


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------

class LLProviderResponse(ResourceMetaFields):
    """Single provider row as surfaced by the admin REST API."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    key: str
    display_name: str
    region: Optional[str] = Field(
        default=None,
        max_length=32,
        description=(
            "Geographic region this provider serves from (us-east-1, "
            "eu-west-1, …), or 'global' for no specific commitment. Read "
            "by the residency check in the Rust proxy to decide which "
            "providers a tenant may route to, so leaving it unset is a "
            "governance decision, not a cosmetic one."
        ),
    )
    category: Optional[str] = None
    developer_url: Optional[str] = None
    api_key_url: Optional[str] = None
    pricing_url: Optional[str] = None
    model_catalog_url: Optional[str] = None
    models_api_url: Optional[str] = None
    icon_url: Optional[str] = None
    default_api_url: Optional[str] = None
    default_auth_type: Optional[str] = None
    additional_drop_params: Optional[List[str]] = None
    notes: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    enabled: bool
    is_system: bool
    # Icon storage metadata. The icon bytes are never
    # returned inline; callers fetch them via
    # `GET /v1/admin/providers/{id}/icon`. These flags let the
    # admin UI render an `<img>` against that endpoint only when a
    # row actually has bytes.
    has_icon: bool = False
    icon_mime_type: Optional[str] = None
    icon_size: Optional[int] = None
    icon_uploaded_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    # Optional — only populated when the caller asks for it (e.g.
    # `?include_model_count=true` on the list endpoint). Avoiding
    # the always-on N+1 keeps the list cheap.
    model_count: Optional[int] = None


class LLProviderListResponse(BaseModel):
    """Wraps a list of providers in a stable shape (mirrors
    `AvailableModelsResponse` and the rest of the admin API)."""

    providers: List[LLProviderResponse]
    total: int


__all__ = [
    "LLProviderCreate",
    "LLProviderUpdate",
    "LLProviderResponse",
    "LLProviderListResponse",
    "VALID_AUTH_TYPES",
    "VALID_CATEGORIES",
]
