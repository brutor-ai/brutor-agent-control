# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Pydantic request/response models for the A2A Agent Card admin API.

Schema design notes:

  * We keep `card_json` as a free-form dict rather than typing every
    A2A v0.3 field. The spec is v0.x and will evolve; the server-side
    validator in `app.services.a2a.card_signing.validate_card_json`
    enforces the only fields we structurally depend on. Admins can
    round-trip any spec-compliant card without a Pydantic model chase.

  * Responses split into a list shape (no verbatim card JSON — the
    field can be kilobytes and a paginated list rarely needs it) and
    a detail shape (full JSON + signature for audit / re-export).
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, ConfigDict

from app.api.v1.models.admin.governance_documents import A2AGovernance


# =============================================================================
# Request models
# =============================================================================


class AgentCardCreateRequest(BaseModel):
    """Create a new agent card entry.

    `card_json` is the verbatim A2A descriptor — admin UIs paste in
    JSON produced by the agent framework (ADK, LangChain, vendor-
    specific). The gateway does not mutate it; we only read required
    fields for indexing.
    """

    name: str = Field(
        ...,
        description="Admin-friendly name; unique per tenant",
        min_length=1,
        max_length=255,
    )
    version: str = Field(
        default="1.0.0",
        description="Operator-supplied semver of the card",
        max_length=64,
    )
    description: Optional[str] = Field(
        None,
        description="Optional human description for the UI",
    )
    source_url: Optional[str] = Field(
        None,
        description="For imported cards, the .well-known URL the card was fetched from",
        max_length=1024,
    )
    icon_url: Optional[str] = Field(
        None,
        description=(
            "Avatar URL surfaced on list/detail rows. If omitted, the gateway "
            "falls back to card_json.iconUrl when present."
        ),
        max_length=2048,
    )
    card_json: Dict[str, Any] = Field(
        ...,
        description="Verbatim A2A v0.3 Agent Card JSON body",
    )
    extended_card_json: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "Optional richer AgentCard JSON served by the authenticated "
            "GetExtendedAgentCard endpoint of the A2A specification. Must be a valid "
            "AgentCard. Omit/null for cards with no extended variant."
        ),
    )
    enabled: bool = Field(
        default=True,
        description=(
            "Whether the card is enabled: considered by the gateway and the policy engine. "
            "`false` creates it switched off. Omitted: enabled."
        ),
    )


class AgentCardUpdateRequest(BaseModel):
    """Partial update — every field is optional and applied only when set.

    Updating `card_json` triggers capability denormalisation and, if
    the card was previously signed, invalidates the signature (the
    update bumps `trust_tier` back to `unsigned` since the signed
    bytes are no longer the persisted bytes). The admin must re-sign
    after body edits.
    """

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(None, min_length=1, max_length=255)
    version: Optional[str] = Field(None, max_length=64)
    description: Optional[str] = None
    icon_url: Optional[str] = Field(None, max_length=2048)
    card_json: Optional[Dict[str, Any]] = None
    extended_card_json: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "Replace the extended AgentCard JSON (A2A GetExtendedAgentCard). Applied only "
            "when provided; must be a valid AgentCard."
        ),
    )
    enabled: Optional[bool] = None
    tenant_limits: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "Limits of the card for the whole tenant, whichever group calls it: "
            "`invocations.{daily_limit, monthly_limit}` (a count per day and per month), "
            "`frequency.{max_calls_per_minute, max_calls_per_hour, max_calls_per_day, "
            "burst_limit, cooldown_seconds}` and `chain.max_delegation_depth`. They apply in "
            "both directions: to calls this gateway sends to the agent and to calls it "
            "receives for it, `invocations` included. Applied beside the calling group's own "
            "limits: the stricter one refuses. A document the gateway cannot read is refused."
        ),
    )
    tenant_governance: Optional[A2AGovernance] = Field(
        None,
        description=(
            "A2A policy of the card for the whole tenant, in the shape of a group's "
            "`a2a_global_governance`: `capability.default_access` (`enabled`, "
            "`approval_required`, `disabled`), `capability.approval_timeout_seconds`, "
            "`data.deny_classifications`, `remote.allowed_globs` (outbound calls only). It is "
            "judged beside the policy of the calling group and of each of its ancestors, and "
            "the strictest verdict is the call's. The gateway refuses every call of a card "
            "whose policy it cannot read, so such a document is refused here."
        ),
    )


class AgentCardGroupsRequest(BaseModel):
    """The whole set of resource groups an agent card is bound to."""

    model_config = ConfigDict(extra="forbid")

    group_ids: List[str] = Field(..., description="Every resource group the card is bound to after the request")


class AgentCardImportRequest(BaseModel):
    """Import an A2A card from an external `.well-known/agent-card` URL.

    The gateway fetches the URL server-side (so admins don't need to
    paste the JSON), stores it with `trust_tier="unsigned"` regardless
    of any signature the remote carries, and marks `source_url`.
    Every imported card is untrusted until an operator signs it.
    """

    source_url: str = Field(
        ...,
        description="Full URL to the external agent card JSON",
        max_length=1024,
    )
    name_override: Optional[str] = Field(
        None,
        description="Override the card's own name field with an admin-friendly label",
        max_length=255,
    )
    description_override: Optional[str] = Field(
        None,
        description=(
            "Override the card's own description with an admin-friendly summary. "
            "When omitted, the imported card's `description` is used as-is."
        ),
    )
    timeout_secs: float = Field(
        default=10.0,
        description="Network timeout for the fetch",
        ge=1.0,
        le=60.0,
    )
    enabled: bool = Field(
        default=True,
        description=(
            "Whether the imported card is enabled: considered by the gateway and the policy "
            "engine. `false` imports it switched off, to be enabled once it has been reviewed."
        ),
    )


class AgentCardDiscoverRequest(BaseModel):
    """Fetch + parse a remote agent card WITHOUT persisting it.

    Used by the admin UI's "Add Agent Card" wizard: the operator pastes
    a URL, hits **Connect**, the gateway fetches the well-known card,
    parses skills + metadata, and returns them as a preview. The
    operator can then click **Create** to persist via the existing
    `/import` endpoint. Mirrors the MCP "Add Server" flow, where
    `test-connection` is the discovery step before final create.

    No DB writes happen here. The operator sees a preview only — that
    keeps "create" the single concept that produces an `AgentCard`
    row, and means a failed/cancelled discovery leaves no orphan state.
    """

    source_url: str = Field(..., max_length=2048)
    timeout_secs: float = Field(default=10.0, ge=1.0, le=60.0)


class DiscoveredCapability(BaseModel):
    """One capability/skill from a discovered card.

    Surfaces `id` (when the card carries one — A2A v1.0 skills do) and
    `name` separately because the admin UI shows the human name in the
    preview but the `id` is what the proxy uses at dispatch time.
    """

    id: Optional[str] = None
    name: str
    description: Optional[str] = None
    tags: List[str] = Field(default_factory=list)


class AgentCardDiscoverResponse(BaseModel):
    """Preview of a remote agent card.

    `resolved_url` is the well-known URL that actually answered (which
    may differ from `source_url` because of the v0.3 → v0.2 fallback).
    Surfacing it lets the UI show "we found it at /agent.json" so the
    operator notices when they're talking to a legacy 0.2.x agent.
    """

    source_url: str
    resolved_url: str
    name: Optional[str] = None
    description: Optional[str] = None
    version: Optional[str] = None
    icon_url: Optional[str] = None
    protocol_version: Optional[str] = None
    provider_name: Optional[str] = None
    capabilities: List[DiscoveredCapability] = Field(default_factory=list)
    capability_count: int = 0
    # Verbatim card so the UI can render the "Advanced" view if asked,
    # and so we have everything needed for a follow-up create call.
    card_json: Dict[str, Any]
    # True iff a card with this `name` already exists in the caller's
    # tenant. Drives the UI's "this name is already taken" hint so the
    # operator can pick a name_override before clicking Create.
    name_conflict: bool = False


class AgentCardBulkImportItem(BaseModel):
    """One row in a bulk-import payload."""

    source_url: str = Field(..., max_length=2048)
    name_override: Optional[str] = Field(None, max_length=255)
    enabled: bool = Field(
        default=True,
        description=(
            "Whether the imported card is enabled: considered by the gateway and the policy "
            "engine. `false` imports it switched off, to be enabled once it has been reviewed."
        ),
    )


class AgentCardBulkImportRequest(BaseModel):
    """Bulk-import N cards from external well-known URLs.

    The `import` endpoint takes one URL at a time; this is the
    "tick checkboxes in the catalog modal and submit" shape. We do not
    fail the whole batch if one URL is broken — see `BulkImportResult`.
    """

    imports: List["AgentCardBulkImportItem"] = Field(..., min_length=1, max_length=50)
    timeout_secs: float = Field(default=10.0, ge=1.0, le=60.0)


class BulkImportResult(BaseModel):
    """Per-row outcome of a bulk import. `success=False` rows carry `error`."""

    source_url: str
    success: bool
    card_id: Optional[str] = None
    name: Optional[str] = None
    error: Optional[str] = None


class AgentCardBulkImportResponse(BaseModel):
    results: List["BulkImportResult"]
    total: int
    succeeded: int
    failed: int


# =============================================================================
# Public registry catalog (a2aregistry.org)
# =============================================================================


class CatalogAgentSkill(BaseModel):
    """One skill from a catalog entry — surfaced so the UI can show
    a chip-list without fetching each card individually."""

    id: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None


class CatalogAgent(BaseModel):
    """Normalised catalog row for the Import modal.

    Fields are union of what a2aregistry.org returns + what the admin UI
    needs. Anything missing from the registry response is left null —
    we do not fabricate data.
    """

    # Stable identifier in the catalog (registry-assigned).
    id: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    # Base URL of the agent — what the admin will paste/import.
    base_url: Optional[str] = None
    # Direct URL to the agent-card JSON (the registry sometimes
    # publishes both base + well-known together).
    well_known_url: Optional[str] = None
    icon_url: Optional[str] = None
    protocol_version: Optional[str] = None
    provider_name: Optional[str] = None
    provider_url: Optional[str] = None
    skills: List[CatalogAgentSkill] = Field(default_factory=list)
    skill_count: int = 0
    healthy: Optional[bool] = None
    uptime_pct: Optional[float] = None


class CatalogResponse(BaseModel):
    agents: List[CatalogAgent]
    total: int
    source: str = Field(
        default="a2aregistry.org",
        description="Registry that provided this catalog snapshot",
    )
    fetched_at: str
    cached: bool = Field(
        default=False,
        description="True when the response was served from the in-memory cache",
    )


class AgentCardSignRequest(BaseModel):
    """Sign an existing card with the tenant's Ed25519 key."""
    # No fields needed — the signer is derived from the authenticated
    # admin, and the card ID is in the URL. Keeping the model here so
    # the endpoint presents a documented request body in OpenAPI and
    # so future options (e.g. alternate signing algorithms) have a
    # natural place to land.


class AgentCardRevokeRequest(BaseModel):
    """Revoke a card permanently."""

    reason: str = Field(
        ...,
        description="Human-readable explanation of why the card is being revoked; persisted on the row",
        min_length=1,
        max_length=1024,
    )


# =============================================================================
# Response models
# =============================================================================


class AgentCardCapabilityResponse(BaseModel):
    id: str
    capability_name: str
    description: Optional[str] = None
    timeout_secs: Optional[int] = None
    rate_limit_per_hour: Optional[int] = None
    data_classification: Optional[List[str]] = None
    # Inbound handler binding. Null means the capability is
    # declared on the card but not yet wired up; the proxy treats it
    # as 'noop' and returns a non-executing acknowledgement.
    handler_type: Optional[str] = None
    handler_ref: Optional[str] = None
    enabled: bool = True


class AgentCardListItem(BaseModel):
    """Lightweight shape used by the list endpoint.

    Deliberately omits `card_json` and `signature` — the list view
    doesn't render them, and they can be multi-kilobyte.
    """

    id: str
    tenant_id: str
    name: str
    version: str
    description: Optional[str] = None
    source_url: Optional[str] = None
    icon_url: Optional[str] = None
    trust_tier: str
    is_trusted: bool
    signature_algorithm: Optional[str] = None
    signed_by: Optional[str] = None
    signed_at: Optional[str] = None
    revoked_at: Optional[str] = None
    revoked_reason: Optional[str] = None
    enabled: bool
    tenant_limits: Optional[Dict[str, Any]] = None
    tenant_governance: Optional[A2AGovernance] = None
    health_status: str = "unknown"
    last_health_check_at: Optional[str] = None
    health_check_error: Optional[str] = None
    created_by: Optional[str] = None
    updated_by: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    capability_count: int = 0
    # Resource-group bindings — same {id, name, display_name} shape the
    # other admin list endpoints (LLM models, MCP server configs) use,
    # so the admin UI can render groups via the shared
    # GroupTreeDisplay component.
    group_ids: List[str] = Field(default_factory=list)
    groups: List[Dict[str, Any]] = Field(default_factory=list)


class AgentCardListResponse(BaseModel):
    cards: List[AgentCardListItem]
    total: int


class AgentCardHealthResult(BaseModel):
    """Outcome of a single health probe."""

    card_id: str
    name: str
    status: str
    error: Optional[str] = None
    checked_at: Optional[str] = None
    target_url: Optional[str] = None


class AgentCardBulkHealthResponse(BaseModel):
    total: int
    healthy: int
    unhealthy: int
    unknown: int
    results: List[AgentCardHealthResult]


class AgentCardDetailResponse(AgentCardListItem):
    """Full card body + signature + capability list."""
    card_json: Dict[str, Any]
    extended_card_json: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Extended AgentCard JSON served by GetExtendedAgentCard "
                    "(A2A specification). Null when no extended card is configured.",
    )
    signature: Optional[str] = None
    capabilities: List[AgentCardCapabilityResponse] = Field(default_factory=list)
    signature_valid: Optional[bool] = Field(
        default=None,
        description="True iff the on-record signature verifies against the tenant's public key. "
                    "Null when the card is unsigned or signing algorithm is unknown.",
    )


# =============================================================================
# Tenant A2A key
# =============================================================================


class TenantA2APublicKeyResponse(BaseModel):
    """Public-key view of the tenant's Ed25519 signing keypair.

    Served at `GET /v1/admin/tenants/{id}/a2a-public-key` so external
    verifiers can validate signatures on exported cards. Never
    includes the private key — that stays Fernet-encrypted in the DB.
    """

    tenant_id: str
    public_key: str = Field(..., description="Base64url-encoded Ed25519 public key")
    key_algorithm: str
    created_at: Optional[str] = None
    rotated_at: Optional[str] = None
