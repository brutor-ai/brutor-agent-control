# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Data residency Pydantic shapes.

Two boundary models:

* `DataResidencyProfile` — the JSONB blob stored on
  `tenants.data_residency`. Validates the shape (list of region
  strings + bool) without enforcing a closed enum on regions
  (operators name their own regions; we don't ship a master list).

* `ProviderRegion` — a thin validator for the `region` field on
  llm_providers / mcp_servers. Same lenient open-string approach;
  the residency check compares strings literally.

The validator rejects nonsense input (numbers, control chars,
strings longer than the column's 32-char limit) but does NOT
enforce a region allow-list — operators run vLLM clusters in
self-named regions, on-prem datacentres, etc.
"""

from __future__ import annotations

import re
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


# Region tokens are lowercase ASCII per cloud convention
# (us-east-1, eu-west-1, ap-southeast-2, global, on-prem-london).
# Permit alnum + hyphen + underscore; reject anything else so we
# don't end up with whitespace or control chars in a column the
# runtime literal-compares against.
_REGION_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}[a-z0-9]$|^[a-z0-9]$")


def _validate_region_token(v: Optional[str]) -> Optional[str]:
    if v is None:
        return v
    v = v.strip().lower()
    if not v:
        return None
    if len(v) > 32:
        raise ValueError("region must be ≤32 characters")
    if not _REGION_RE.match(v):
        raise ValueError(
            "region must be lowercase alphanumeric, hyphen, or underscore "
            "(e.g. us-east-1, eu-west-1, global)"
        )
    return v


class DataResidencyProfile(BaseModel):
    """Per-tenant residency profile shape stored at
    `tenants.data_residency`. Read by the Rust runtime to gate
    request routing."""

    allowed_regions: List[str] = Field(
        default_factory=list,
        description=(
            "Regions the tenant's traffic is allowed to route to. "
            "Empty list + block_global=true means 'no provider may "
            "serve this tenant', useful for hard-cordoned tenants. "
            "Region strings must match the format used on provider "
            "rows (lowercase alnum + hyphen/underscore)."
        ),
    )
    block_global: bool = Field(
        False,
        description=(
            "When true, refuse providers whose region is 'global' "
            "or NULL even if allowed_regions is empty. Use this on "
            "tenants under strict data-residency regimes where "
            "no-region-set is considered an unsafe default."
        ),
    )

    @field_validator("allowed_regions")
    @classmethod
    def _validate_regions(cls, v: List[str]) -> List[str]:
        # Validate every entry and de-dupe preserving order. Bad
        # entries fail loudly here so operators see the error at
        # save time rather than a silent runtime mismatch.
        seen = set()
        out: List[str] = []
        for r in v:
            validated = _validate_region_token(r)
            if validated is None:
                # Empty / whitespace input — skip silently rather
                # than error; pasted-from-spreadsheet input is the
                # common case.
                continue
            if validated not in seen:
                seen.add(validated)
                out.append(validated)
        return out


def validate_region(v: Optional[str]) -> Optional[str]:
    """Public validator for the region field on llm_providers and
    mcp_servers. Exported separately so the admin endpoints'
    provider/server schemas can reuse it without importing the
    full DataResidencyProfile."""
    return _validate_region_token(v)
