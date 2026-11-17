# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Pydantic response models for the global LLM Provider Catalog admin API.

Response shapes are built from `LlmProviderCatalog.to_dict()` via
`Model(**row.to_dict())`, so the response field names MUST stay in
lockstep with the to_dict keys (the Pydantic strip-undeclared-fields
gotcha).
"""

from typing import Any, List, Optional

from pydantic import BaseModel


class LlmProviderCatalogEntryResponse(BaseModel):
    """A single global provider-catalog entry. Field names mirror
    `LlmProviderCatalog.to_dict()`. This is what the admin UI provider
    dropdown reads when manually adding a model."""

    id: str
    key: str
    display_name: str
    category: Optional[str] = None
    developer_url: Optional[str] = None
    api_key_url: Optional[str] = None
    pricing_url: Optional[str] = None
    model_catalog_url: Optional[str] = None
    models_api_url: Optional[str] = None
    icon_url: Optional[str] = None
    has_icon: bool
    default_api_url: Optional[str] = None
    default_auth_type: Optional[str] = None
    additional_drop_params: Optional[List[Any]] = None
    region: Optional[str] = None
    notes: Optional[str] = None
    source: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class LlmProviderCatalogListResponse(BaseModel):
    providers: List[LlmProviderCatalogEntryResponse]
    total: int
