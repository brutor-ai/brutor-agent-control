# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Pydantic models for the System Operations Console Sessions tab.

Carved out of `system_ops.py` per the slice convention — the parent
file re-exports these so existing callers keep working. The split
keeps the high-traffic parent file readable and lets the slice land
without merge churn on the shared module.

Two request bodies and two response wrappers:

  * `SessionListResponse` — list payload, one envelope per kind.
  * `SessionEntry` — the rendered row. Shape is identical across kinds
    so the UI table component renders unchanged; missing fields land
    as `null`.
  * `SessionRevokeRequest` — body for the revoke endpoint.
  * `SessionRevokeResponse` — the post-revoke envelope, includes the
    NOTIFY payload echoed for operator confirmation.
"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# The wire-format string for which credential family this row
# represents. Kept here (and mirrored as KIND_* constants in the
# service helper) so a typo on either side fails fast.
SessionKindWire = Literal["admin", "portal", "api_key"]


class SessionEntry(BaseModel):
    """One row in the Sessions table.

    The shape is deliberately the same across kinds even when a field
    is meaningful only for one kind (e.g. `expires_at` is `None` for
    portal sessions). The UI renders one component and shows a dash
    for `null`, which is clearer than a per-kind table component.
    """

    model_config = ConfigDict(extra="forbid")

    kind: SessionKindWire = Field(
        description="Which credential family this row represents.",
    )
    id: str = Field(
        description=(
            "Identifier of the session row. Shape depends on kind: "
            "admin session id, portal `end_user_session_id`, or "
            "API key id."
        ),
    )
    tenant_id: str
    user_id: Optional[str] = Field(
        default=None,
        description=(
            "Owner of the session — admin id, end-user id, or the "
            "API key's bound user_id. NULL when the credential is "
            "not bound to a single user (e.g. group-scoped API key)."
        ),
    )
    user_label: Optional[str] = Field(
        default=None,
        description=(
            "Human-readable owner — username / email / API key name. "
            "Renders in the UI's 'Owner' column."
        ),
    )
    created_at: Optional[str] = Field(
        default=None,
        description="ISO-8601 timestamp the credential was first issued.",
    )
    last_seen_at: Optional[str] = Field(
        default=None,
        description=(
            "Most recent activity. For admin sessions, the throttled "
            "`last_activity` column. For portal sessions, the most "
            "recent proxy_log row. For API keys, `last_used_at`."
        ),
    )
    expires_at: Optional[str] = Field(
        default=None,
        description=(
            "Hard expiry. NULL for portal sessions (the expiry lives "
            "on the JWT, not the DB) and NULL for non-expiring API keys."
        ),
    )
    ip: Optional[str] = Field(
        default=None,
        description="Source IP at credential issue (admin sessions) or most-recent IP (portal).",
    )
    user_agent: Optional[str] = Field(default=None)
    extra: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Per-kind extra fields the table renders as supplementary "
            "columns (admin: risk_score; api_key: scope + usage_count)."
        ),
    )


class SessionListResponse(BaseModel):
    """GET /v1/admin/system/sessions response body."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    kind: SessionKindWire
    generated_at: str
    items: List[SessionEntry]


class SessionRevokeRequest(BaseModel):
    """Body for POST /v1/admin/system/sessions/revoke.

    `reason` is optional but the audit row carries it through verbatim
    — operators are encouraged (in the UI confirm modal) to supply
    one so the trail is meaningful at incident-review time.
    """

    model_config = ConfigDict(extra="forbid")

    kind: SessionKindWire = Field(
        description=(
            "Which credential family the `id` belongs to. The endpoint "
            "dispatches to the right service helper from this field."
        ),
    )
    id: str = Field(
        min_length=1,
        max_length=255,
        description=(
            "Identifier of the row to revoke. Shape depends on kind. "
            "Cross-tenant ids are rejected by the helper — the auth "
            "tenant scopes the lookup."
        ),
    )
    reason: Optional[str] = Field(
        default=None,
        max_length=4096,
        description=(
            "Operator-supplied reason. Persisted into the audit row "
            "payload AND, for kinds that carry a DB-side reason "
            "column (api_key.revoked_reason, "
            "admin_session.deactivation_reason), into that column too."
        ),
    )


class SessionRevokeResponse(BaseModel):
    """POST /v1/admin/system/sessions/revoke response.

    Echoes back the exact NOTIFY payload so the UI toast can confirm
    "queued, payload landed on every replica" and so a future
    integration test can assert wire shape without round-tripping
    through the listener.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["queued"] = "queued"
    kind: SessionKindWire
    id: str
    tenant_id: str
    channel: str
    payload: str = Field(
        description="JSON-serialised NOTIFY payload that landed on the wire.",
    )
