# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Pydantic models for the saved log views REST endpoints.

These cover both viewer scopes — `proxy_logs` and `admin_audit_logs` —
behind a single CRUD surface. The endpoints accept and return the
`scope` field so the admin UI can keep one client method per verb
instead of two parallel paths.

`filters` is intentionally typed as a free-form dict here. The live
search endpoints re-validate the envelope against their own Pydantic
models on apply, so a malformed saved view can never produce an
invalid search request — it'll just be rejected at apply time with
a clear error. Validating it inside SavedLogView at write time
would mean coupling this CRUD surface to every future change in
`LogQueryRequest` / `AuditLogQueryRequest`, and we'd lose the
ability for an older client to round-trip a view shaped by a newer
search model.
"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, ConfigDict


# Scope literal — mirrors `SavedLogView.scope` exactly. Pydantic will
# reject any other string at request validation time, so we don't
# need a second guard in the endpoint.
SavedViewScope = Literal["proxy_logs", "admin_audit_logs"]


class SavedLogViewCreateRequest(BaseModel):
    """Body for POST /saved-log-views.

    `is_shared` defaults to False: a saved view is its owner's until the
    owner shares it.
    """

    model_config = ConfigDict(extra="forbid")

    scope: SavedViewScope = Field(
        ...,
        description=(
            "Which viewer this view applies to. The same string the "
            "viewer passes when listing its views."
        ),
    )
    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Operator-facing label. Unique within (tenant, scope, owner).",
    )
    description: Optional[str] = Field(
        default=None,
        max_length=4096,
        description="Optional longer description, surfaced in the dropdown tooltip.",
    )
    filters: Dict[str, Any] = Field(
        ...,
        description=(
            "Verbatim request envelope: the same body the viewer "
            "would POST to /search. Stored as-is in JSONB."
        ),
    )
    is_shared: bool = Field(
        default=False,
        description=(
            "When True, every admin on the tenant sees this view "
            "under 'Shared'. Default is private."
        ),
    )


class SavedLogViewUpdateRequest(BaseModel):
    """Body for PATCH /saved-log-views/{id}.

    Every field is optional — only the keys present in the request
    are applied. Operators commonly only flip `is_shared` or rename,
    so requiring the full body would be needlessly chatty.
    """

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=255,
    )
    description: Optional[str] = Field(default=None, max_length=4096)
    filters: Optional[Dict[str, Any]] = None
    is_shared: Optional[bool] = None


class SavedLogViewResponse(BaseModel):
    """API representation of a saved log view."""

    model_config = ConfigDict(extra="forbid")

    id: str
    tenant_id: str
    scope: SavedViewScope
    owner_admin_id: Optional[str]
    name: str
    description: Optional[str]
    filters: Dict[str, Any]
    is_shared: bool
    created_at: Optional[str]
    updated_at: Optional[str]


class SavedLogViewListResponse(BaseModel):
    """List wrapper — `items` carries the views the requester can see
    (their own + any shared) for the requested scope. The admin UI
    splits the list client-side into "Mine" vs "Shared" via
    `owner_admin_id == current_admin_id` and `is_shared`.
    """

    model_config = ConfigDict(extra="forbid")

    items: List[SavedLogViewResponse]
