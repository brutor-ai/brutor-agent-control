# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Audit-tab Pydantic models for the System Operations Console.

Kept in a separate module from `system_ops.py` to avoid merging conflicts
across the slices that are being implemented in parallel. The endpoint
module re-exports these alongside the existing system_ops models.

Wire shapes
-----------
* `AuditFacetValue` / `AuditFacetsResponse` — `/v1/admin/system/audit/facets`
  returns the top-N (value, count) for every facetable column, scoped
  to the same filter envelope the table is rendering. This is the
  same Datadog-style shape proxy-logs uses.

The list-row + list envelope wire shape already lives in `system_ops.py`
as `SystemOperationLogResponse` + `SystemOperationLogListResponse` — we
deliberately don't redeclare them here.
"""

from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Facet values
# ---------------------------------------------------------------------------


class AuditFacetValue(BaseModel):
    """One (value, count) entry in the facets response.

    `value` is `Any` because the audit log carries multiple column
    types — `action` and `scope_kind` are strings, but a future
    facetable column (e.g. `tenant_id` for the sysadmin cross-tenant
    view) could be a string ULID. The TypeScript side treats every
    value as a string for sidebar display anyway, so Pydantic
    permissiveness here is appropriate.
    """

    model_config = ConfigDict(extra="forbid")

    value: Union[str, int, bool, None] = Field(
        description=(
            "The distinct column value. Coerced to a string in the UI "
            "for sidebar display, but the wire shape preserves the "
            "underlying type so consumers that POST back the facet "
            "value verbatim hit equality matches."
        ),
    )
    count: int = Field(
        ge=0,
        description="Number of rows with this value under the current filter envelope.",
    )


class AuditFacetsResponse(BaseModel):
    """GET /v1/admin/system/audit/facets body.

    `facets` is a {field: [values]} map. The endpoint always returns
    a row for every facetable field, even if the value list is empty
    — so the sidebar can render an empty section instead of hiding
    it (operators get an explicit "no values match" instead of being
    left wondering whether the API forgot the field).

    `labels` is the human-readable section header per field, e.g.
    `{"action": "Action", "scope_kind": "Scope", ...}`. Kept on the
    wire so the UI doesn't have to maintain a parallel translation
    table.
    """

    model_config = ConfigDict(extra="forbid")

    facets: Dict[str, List[AuditFacetValue]]
    labels: Dict[str, str] = Field(
        description=(
            "Human-readable header per field. Drives the "
            "FacetSidebar section labels — keeps the wire shape "
            "single-source-of-truth for facet UI copy."
        ),
    )
