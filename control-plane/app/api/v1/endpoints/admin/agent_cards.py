# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
A2A Agent Card admin API.

Endpoints under `/v1/admin/agent-cards/*`:

  GET    /                 — list cards for the caller's tenant
  POST   /                 — create a new card
  GET    /{id}             — detail view (verbatim JSON + signature)
  PATCH  /{id}             — update metadata or body (invalidates signature)
  DELETE /{id}             — hard-delete (capabilities cascade)
  POST   /{id}/sign        — sign with tenant key
  POST   /{id}/revoke      — terminal revocation
  POST   /import           — fetch an external .well-known card

And one tenant-scoped endpoint at `/v1/admin/tenants/{id}/a2a-public-key`
for external verifiers. That one lives on the tenant_admin router.

All endpoints are gated by both:
  * RBAC permission (A2A_CARD_* / A2A_TENANT_KEY_READ), and
  * Tenant feature flag (`features.a2a`) — returns 404 if disabled,
    so the surface is invisible to tenants that haven't opted in.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

import httpx

from app.services.outbound_http import OutboundRefused, outbound_async_client
from app.services.outbound_policy import refusal_message
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session
from ulid import ULID

from app.core.sync_bridge import block_on, block_on_all
from app.api.v1.models.admin.agent_card import (
    AgentCardBulkHealthResponse,
    AgentCardBulkImportRequest,
    AgentCardBulkImportResponse,
    AgentCardCapabilityResponse,
    AgentCardCreateRequest,
    AgentCardDetailResponse,
    AgentCardDiscoverRequest,
    AgentCardDiscoverResponse,
    AgentCardHealthResult,
    AgentCardImportRequest,
    AgentCardListItem,
    AgentCardListResponse,
    AgentCardRevokeRequest,
    AgentCardSignRequest,
    AgentCardGroupsRequest,
    AgentCardUpdateRequest,
    BulkImportResult,
    CatalogAgent,
    CatalogAgentSkill,
    CatalogResponse,
    DiscoveredCapability,
)
from app.core.admin_user.dependencies import (
    get_audit_logger,
    get_current_admin,
    require_permission,
)
from app.core.admin_user.permissions import Permission
from app.constants.tenant import TenantResourceTypes
from app.services.tenant.tenant_service import TenantService
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.database.models.admin import TenantAdmin
from app.core.admin_user.admin_principal import AdminPrincipal
from app.database.models.agent_card import (
    AgentCard,
    HEALTH_HEALTHY,
    HEALTH_UNHEALTHY,
    HEALTH_UNKNOWN,
    TRUST_TIER_REVOKED,
    TRUST_TIER_TENANT_SIGNED,
    TRUST_TIER_UNSIGNED,
)
from app.database.models.tenant import Tenant
from app.services.a2a.card_signing import (
    A2ASigningError,
    InvalidCardError,
    revoke_card as service_revoke_card,
    sign_card as service_sign_card,
    sync_capabilities,
    validate_card_json,
    verify_card_signature,
)
from app.services.cache_invalidation import invalidate_proxy_cache
from app.services.config_manager import ConfigManager
from app.services.oauth.encryption_service import EncryptionService


A2A_FEATURE_FLAG = "a2a"


def _encryption_service(logger: BrutorLogger) -> EncryptionService:
    """Construct a per-request EncryptionService.

    ConfigManager reads ENCRYPTION_KEY from env on each call — cheap,
    no IO. We build it per-request rather than caching on the router
    so tests can swap the env cleanly. The constructor validates the
    key is non-default; production deployments fail fast if that's
    wrong.
    """
    return EncryptionService(
        config_manager=ConfigManager(logger=logger, validate=False),
        logger=logger,
    )


# One tenant-scoping rule for every admin route — see
# app/core/admin_user/dependencies.py::resolve_tenant_id. This was a
# five-line copy in twenty-five modules and had already drifted into two
# behaviours; the shared one refuses a mismatched ?tenant_id= rather than
# silently answering about a different tenant.
from app.core.admin_user.dependencies import resolve_tenant_id as _resolve_tenant_id


def _enforce_a2a_enabled(db: Session, tenant_id: str) -> Tenant:
    """Load tenant, 404 if missing, 404 if A2A disabled for this tenant.

    404 (not 403) so the A2A surface is invisible to tenants who
    haven't opted in — the same pattern the Knowledge Base endpoints
    use when `features.kb` is false.
    """
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if tenant is None:
        raise HTTPException(404, f"Tenant {tenant_id!r} not found")
    if not tenant.has_feature(A2A_FEATURE_FLAG):
        raise HTTPException(
            404,
            "A2A feature is not enabled for this tenant",
        )
    return tenant


def _get_card_or_404(
    db: Session, card_id: str, tenant_id: str
) -> AgentCard:
    card = db.query(AgentCard).filter(
        AgentCard.id == card_id,
        AgentCard.tenant_id == tenant_id,
    ).first()
    if card is None:
        raise HTTPException(404, f"Agent card {card_id!r} not found")
    return card


# =============================================================================
# Well-known agent-card fetch
# =============================================================================
#
# A2A spec 0.3 standardised on /.well-known/agent-card.json. Earlier 0.2.x
# agents (and many still in the wild) use /.well-known/agent.json. The
# official Python and JS SDKs try the new path first then fall back, and
# we mirror that so importing works against either generation.
#
# We accept three URL shapes from the admin:
#   1. Bare base ("https://example.com") — try both well-known suffixes.
#   2. Explicit .json URL — try as-is, fall back from -card to legacy.
#   3. Explicit non-.json URL — treat as base.

_WELL_KNOWN_PRIMARY = ".well-known/agent-card.json"   # spec ≥0.3
_WELL_KNOWN_LEGACY  = ".well-known/agent.json"         # spec ≤0.2


def _candidate_urls(source_url: str) -> List[str]:
    """Return the ordered list of URLs to try for a given input.

    Always returns at least one URL. Duplicates are filtered while
    preserving order (so a user who pastes the legacy path doesn't get
    the primary path tried first against an agent that doesn't speak it).
    """
    src = (source_url or "").strip()
    if not src:
        return []

    # Treat anything ending in `.json` as an explicit endpoint.
    if src.lower().endswith(".json"):
        # If they typed the new path, also try the legacy as a fallback.
        if src.endswith("/" + _WELL_KNOWN_PRIMARY):
            base = src[: -len(_WELL_KNOWN_PRIMARY)]
            return [src, base + _WELL_KNOWN_LEGACY]
        # If they typed the legacy path, no point trying the new one.
        return [src]

    base = src.rstrip("/") + "/"
    return [base + _WELL_KNOWN_PRIMARY, base + _WELL_KNOWN_LEGACY]


async def _fetch_well_known_card(
    source_url: str,
    timeout_secs: float,
) -> tuple[dict, str]:
    """Fetch the first reachable A2A card for a URL.

    Returns `(card_json, resolved_url)` where `resolved_url` is the URL
    that actually worked (so we can record it on `agent_cards.source_url`).
    Raises `HTTPException` directly on failure — the caller is always an
    endpoint, so propagating the right status keeps the call sites tight.

    `follow_redirects=False` is deliberate: an admin typing a redirected
    host should see the redirect, not have us silently fan into wherever
    the remote points (SSRF defence in depth).
    """
    candidates = _candidate_urls(source_url)
    if not candidates:
        raise HTTPException(400, "source_url is required")

    last_status: Optional[int] = None
    last_url: str = candidates[-1]
    try:
        async with outbound_async_client(
            timeout=timeout_secs, follow_redirects=False
        ) as client:
            for url in candidates:
                try:
                    resp = await client.get(url)
                except OutboundRefused as refused:
                    # The outbound policy refused the destination: the
                    # next candidate is the same host, and the admin is told.
                    raise HTTPException(400, refusal_message(url, "Agent card URL", refused.reason))
                except httpx.RequestError:
                    last_url = url
                    continue
                last_status = resp.status_code
                last_url = url
                if resp.status_code == 404 and len(candidates) > 1:
                    # Walk to the next candidate; the spec fallback exists
                    # precisely for this case.
                    continue
                if resp.status_code != 200:
                    raise HTTPException(
                        502,
                        f"Remote returned HTTP {resp.status_code} when fetching {url}",
                    )
                try:
                    return resp.json(), url
                except ValueError as e:
                    raise HTTPException(
                        502,
                        f"Remote returned non-JSON body ({e}); expected an A2A Agent Card",
                    )
    except httpx.TimeoutException:
        raise HTTPException(504, f"Timed out fetching {last_url}")

    # Every candidate 404'd.
    raise HTTPException(
        502,
        (
            f"No agent card found at {source_url} — tried {len(candidates)} "
            f"well-known path(s); last status was {last_status}"
        ),
    )


def _persist_imported_card(
    db: Session,
    *,
    tenant_id: str,
    admin: AdminPrincipal,
    card_json: dict,
    resolved_url: str,
    name_override: Optional[str],
    description_override: Optional[str] = None,
    enabled: bool = True,
) -> AgentCard:
    """Validate + persist a fetched card. Mirrors `create_card` but for
    imports; pulled out so single-import and bulk-import share one path.

    Captures `iconUrl` from the card body onto the denormalised
    `icon_url` column so the list view can render avatars without
    parsing the full body.
    """
    try:
        validate_card_json(card_json)
    except InvalidCardError as e:
        raise HTTPException(400, str(e))

    name = (name_override or card_json.get("name") or "").strip()
    if not name:
        raise HTTPException(
            400,
            "Imported card has no `name` field and no name_override was provided",
        )

    dup = db.query(AgentCard).filter(
        AgentCard.tenant_id == tenant_id,
        AgentCard.name == name,
    ).first()
    if dup is not None:
        raise HTTPException(
            409,
            f"An agent card named {name!r} already exists in this tenant",
        )

    # One more card of the tenant's quota, asked as `create_card` asks it
    # and before the row is written.
    TenantService.check_quota(db, tenant_id, TenantResourceTypes.AGENT_CARDS)

    icon_url = card_json.get("iconUrl") or card_json.get("icon_url")
    if isinstance(icon_url, str):
        icon_url = icon_url.strip()[:2048] or None
    else:
        icon_url = None

    description = (
        description_override.strip() if description_override and description_override.strip()
        else card_json.get("description")
    )

    card = AgentCard(
        id=f"agentcard-{ULID()}",
        tenant_id=tenant_id,
        name=name,
        version=str(card_json.get("version", "1.0.0"))[:64],
        description=description,
        source_url=resolved_url,
        icon_url=icon_url,
        card_json=card_json,
        trust_tier=TRUST_TIER_UNSIGNED,
        enabled=enabled,
        created_by=getattr(admin, "username", None) or getattr(admin, "id", None),
    )
    db.add(card)
    db.flush()
    sync_capabilities(db, card)
    return card


def _extract_discovered_capabilities(card_json: dict) -> List[DiscoveredCapability]:
    """Parse skills/capabilities out of a fetched card body.

    Uses the shared dialect-aware extractor: A2A v0.3+ cards put
    dispatchable units under `skills[]`; v0.2.x put them under
    `capabilities[]`. v1.0 cards keep `capabilities` but as the
    feature-flag *object* — never the dispatchable list.

    Bad/missing entries are skipped silently — discovery is best-effort
    preview, not validation. The caller's `validate_card_json` already
    enforces the structural minimum.
    """
    from app.services.a2a.card_signing import get_dispatchable_list

    out: List[DiscoveredCapability] = []
    for entry in get_dispatchable_list(card_json):
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            # Skill objects without a name aren't dispatchable; the UI
            # has nothing useful to render either.
            continue
        tags_raw = entry.get("tags")
        tags: List[str] = []
        if isinstance(tags_raw, list):
            tags = [t.strip() for t in tags_raw if isinstance(t, str) and t.strip()]
        out.append(DiscoveredCapability(
            id=entry.get("id") if isinstance(entry.get("id"), str) else None,
            name=name.strip(),
            description=entry.get("description") if isinstance(entry.get("description"), str) else None,
            tags=tags,
        ))
    return out


# =============================================================================
# a2aregistry.org catalog cache
# =============================================================================
#
# The registry exposes a public catalog at
# https://a2aregistry.org/api/agents?conformance=standard. We proxy it
# server-side (CORS would block the browser) and cache the result for a
# few minutes so an operator opening the Import modal repeatedly doesn't
# hammer the upstream — the registry's own health checks run on a
# 30-minute cadence so a 5-minute cache is well below the data-freshness
# floor anyway.

_REGISTRY_URL = "https://a2aregistry.org/api/agents?conformance=standard"
_REGISTRY_CACHE: Dict[str, object] = {"at": 0.0, "payload": None}
_REGISTRY_CACHE_TTL_SECS = 300.0


def _normalise_catalog_entry(raw: Dict[str, object]) -> CatalogAgent:
    """Translate one registry row into our `CatalogAgent` shape.

    The registry's payload is loosely-typed and has shifted shape across
    minor releases; we read only the fields we display, leave the rest
    untouched, and tolerate misses with `None`. Anything missing surfaces
    as a blank cell in the UI rather than throwing — the operator can
    still tick the row to import.
    """
    def _s(*keys: str) -> Optional[str]:
        for k in keys:
            v = raw.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return None

    skills_raw = raw.get("skills")
    skills: List[CatalogAgentSkill] = []
    if isinstance(skills_raw, list):
        for s in skills_raw:
            if isinstance(s, dict):
                skills.append(
                    CatalogAgentSkill(
                        id=s.get("id") if isinstance(s.get("id"), str) else None,
                        name=s.get("name") if isinstance(s.get("name"), str) else None,
                        description=s.get("description")
                        if isinstance(s.get("description"), str) else None,
                    )
                )

    provider = raw.get("provider")
    provider_name = None
    provider_url = None
    if isinstance(provider, dict):
        if isinstance(provider.get("organization"), str):
            provider_name = provider["organization"]
        elif isinstance(provider.get("name"), str):
            provider_name = provider["name"]
        if isinstance(provider.get("url"), str):
            provider_url = provider["url"]

    healthy: Optional[bool] = None
    h = raw.get("health") or raw.get("status")
    if isinstance(h, dict):
        s = h.get("status")
        if isinstance(s, str):
            healthy = s.lower() in {"healthy", "alive", "ok", "up"}
    elif isinstance(h, str):
        healthy = h.lower() in {"healthy", "alive", "ok", "up"}
    elif isinstance(raw.get("healthy"), bool):
        healthy = raw["healthy"]

    uptime_pct: Optional[float] = None
    u = raw.get("uptime") or (h.get("uptime") if isinstance(h, dict) else None)
    if isinstance(u, (int, float)):
        uptime_pct = float(u)
    elif isinstance(u, dict) and isinstance(u.get("percentage"), (int, float)):
        uptime_pct = float(u["percentage"])

    return CatalogAgent(
        id=_s("id", "_id", "slug"),
        name=_s("name", "title"),
        description=_s("description", "summary"),
        base_url=_s("url", "baseUrl", "base_url", "endpoint"),
        well_known_url=_s("agentCardUrl", "agent_card_url", "wellKnownUrl"),
        icon_url=_s("iconUrl", "icon_url", "icon", "logoUrl"),
        protocol_version=_s("protocolVersion", "protocol_version"),
        provider_name=provider_name,
        provider_url=provider_url,
        skills=skills,
        skill_count=len(skills),
        healthy=healthy,
        uptime_pct=uptime_pct,
    )


async def _fetch_registry_catalog(
    timeout_secs: float = 10.0,
    force_refresh: bool = False,
) -> tuple[List[CatalogAgent], bool]:
    """Return `(agents, cached)` from the public A2A registry.

    `cached=True` when served from the in-memory cache. Network errors
    surface as HTTP 502/504 — the catalog is read-only and the operator
    can retry without side effects.
    """
    import time as _time
    now = _time.time()
    if not force_refresh:
        cached_at = float(_REGISTRY_CACHE.get("at") or 0)
        cached_payload = _REGISTRY_CACHE.get("payload")
        if cached_payload is not None and (now - cached_at) < _REGISTRY_CACHE_TTL_SECS:
            return list(cached_payload), True  # type: ignore[arg-type]

    try:
        async with httpx.AsyncClient(timeout=timeout_secs, follow_redirects=True) as client:
            resp = await client.get(_REGISTRY_URL)
    except httpx.TimeoutException:
        raise HTTPException(504, f"Timed out fetching A2A registry at {_REGISTRY_URL}")
    except httpx.RequestError as e:
        raise HTTPException(502, f"Network error fetching A2A registry: {e}")

    if resp.status_code != 200:
        raise HTTPException(
            502, f"A2A registry returned HTTP {resp.status_code}"
        )
    try:
        payload = resp.json()
    except ValueError as e:
        raise HTTPException(502, f"A2A registry returned non-JSON body: {e}")

    # Registry shape has varied: sometimes `{"agents": [...]}`,
    # sometimes a bare array. Handle both.
    if isinstance(payload, dict):
        rows = payload.get("agents") or payload.get("data") or payload.get("results")
        if rows is None:
            rows = []
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []

    agents = [_normalise_catalog_entry(r) for r in rows if isinstance(r, dict)]

    _REGISTRY_CACHE["at"] = now
    _REGISTRY_CACHE["payload"] = agents
    return agents, False


def _extract_card_target_url(card: AgentCard) -> Optional[str]:
    """Pull the first usable interface URL from a card.

    A2A v1.0 puts interfaces under `supportedInterfaces[]`; v0.3 has
    a top-level `url`. Tries v1.0 first, then v0.3, then nothing —
    in which case the probe returns `unknown` rather than failing.
    """
    body = card.card_json or {}

    interfaces = body.get("supportedInterfaces")
    if isinstance(interfaces, list):
        for iface in interfaces:
            if isinstance(iface, dict):
                url = iface.get("url")
                if isinstance(url, str) and url.strip():
                    return url.strip()

    # v0.3 fallback
    url = body.get("url")
    if isinstance(url, str) and url.strip():
        return url.strip()

    return None


def _docker_host_fallback_urls(discovery_url: str) -> List[str]:
    """Build a list of URLs to try, accommodating mixed network topology.

    Cards typically advertise `host.docker.internal:9100` so the
    *gateway core* (running inside Docker) can reach an agent on the
    host. The *control plane* health probe, however, may itself be
    running on the host where `host.docker.internal` doesn't resolve
    — in which case we transparently retry against `localhost` /
    `127.0.0.1` so the operator gets a meaningful health result either
    way. The advertised URL is left untouched so outbound proxy
    routing keeps working from inside the gateway container.
    """
    urls = [discovery_url]
    fallbacks = ("localhost", "127.0.0.1")
    if "host.docker.internal" in discovery_url:
        for replacement in fallbacks:
            urls.append(
                discovery_url.replace("host.docker.internal", replacement)
            )
    return urls


async def _probe_agent_card(
    card: AgentCard,
    timeout_seconds: float,
) -> Tuple[str, Optional[str], Optional[str]]:
    """Probe a single card's interface URL.

    Returns ``(status, error, target_url)``:
      * ``healthy``    — `/.well-known/agent-card.json` returned 2xx,
                         and (when JSON-parseable) at least claims to be
                         an A2A card.
      * ``unhealthy``  — connection/timeout/non-2xx/non-JSON.
      * ``unknown``    — card has no advertised URL to probe.

    A 200 with malformed JSON is still treated as healthy — some
    minimal A2A servers respond on root with non-JSON; the goal here
    is liveness, not strict spec validation.
    """
    target_url = _extract_card_target_url(card)
    if not target_url:
        return HEALTH_UNKNOWN, "Card has no advertised interface URL", None

    advertised_url = target_url.rstrip("/") + "/.well-known/agent-card.json"
    candidates = _docker_host_fallback_urls(advertised_url)

    last_error: Optional[str] = None
    last_url = advertised_url
    async with outbound_async_client(
        timeout=timeout_seconds, follow_redirects=True
    ) as client:
        for candidate in candidates:
            last_url = candidate
            try:
                resp = await client.get(candidate)
            except httpx.TimeoutException:
                last_error = "Timed out contacting agent"
                continue
            except httpx.HTTPError as exc:
                last_error = f"Connection error: {exc!s}"
                # DNS failure on `host.docker.internal` from the host —
                # try the next candidate.
                continue
            except Exception as exc:
                last_error = f"Probe failed: {exc!s}"
                continue

            if 200 <= resp.status_code < 300:
                # Report the URL that actually succeeded so the operator
                # can tell whether the docker-host fallback fired.
                return HEALTH_HEALTHY, None, candidate
            last_error = f"Discovery returned HTTP {resp.status_code}"

    return HEALTH_UNHEALTHY, last_error, last_url


def _record_health(
    card: AgentCard,
    status: str,
    error: Optional[str],
) -> None:
    """Persist probe result on the card row. Caller commits."""
    card.health_status = status
    card.last_health_check_at = datetime.now(timezone.utc)
    card.health_check_error = error if status == HEALTH_UNHEALTHY else None


def _apply_version_to_card_json(
    card_json: Optional[Dict[str, Any]], version: Optional[str]
) -> Optional[Dict[str, Any]]:
    """Return `card_json` with `version` applied, or None if nothing changed.

    `card_json` is the source of truth and the denormalised columns are
    derived from it, so a version set through the Audit Info tab has to
    land in the document too. Without this the row would say 2.0 while
    every client fetching the card still reads 1.0 — and the card is the
    thing clients actually trust.

    Returns None rather than the unchanged document so the caller can tell
    "no edit" from "edited", which is what decides whether an existing
    signature is invalidated.
    """
    if version is None or not isinstance(card_json, dict):
        return None
    if card_json.get("version") == version:
        return None
    return {**card_json, "version": version}


def _card_to_detail(
    db: Session, card: AgentCard
) -> AgentCardDetailResponse:
    """Assemble the detail response including signature validity check.

    `signature_valid` semantics:
      * None  — the card is not currently claiming trust (unsigned or
                revoked). The UI should render "not verified" not
                "INVALID". Treating body-edited-after-sign cards as
                `null` rather than `False` matters because the stored
                signature string is kept for audit ("X signed version
                Y at time Z") but is no longer the trust claim.
      * True  — signature verifies against the tenant's public key.
      * False — signed card whose signature does not verify. This is
                the "tampered / key-rotated / algorithm-mismatch"
                red-flag state.
    """
    signature_valid: Optional[bool]
    if (card.trust_tier == TRUST_TIER_TENANT_SIGNED
            and card.signature and card.signature_algorithm):
        signature_valid = verify_card_signature(db, card)
    else:
        signature_valid = None

    caps = [
        AgentCardCapabilityResponse(
            id=c.id,
            capability_name=c.capability_name,
            description=c.description,
            timeout_secs=c.timeout_secs,
            rate_limit_per_hour=c.rate_limit_per_hour,
            data_classification=c.data_classification,
            handler_type=c.handler_type,
            handler_ref=c.handler_ref,
            enabled=c.enabled,
        )
        for c in card.capabilities
    ]
    return AgentCardDetailResponse(
        **card.to_dict(include_card_json=True),
        capabilities=caps,
        signature_valid=signature_valid,
    )


def _request_tenant(
    tenant_id: Optional[str] = Query(None),
    admin_and_type: tuple = Depends(get_current_admin),
) -> str:
    """The tenant the request acts on, as a dependency: answered before a
    route's body is validated."""
    admin, admin_type = admin_and_type
    return _resolve_tenant_id(admin, admin_type, tenant_id)


def create_agent_cards_router(logger: BrutorLogger) -> APIRouter:
    """Build the `/admin/agent-cards/*` router."""

    router = APIRouter(prefix="/admin/agent-cards", tags=["A2A Agent Cards"])

    # ────────────────────────────────────────────────────────────────────
    # List
    # ────────────────────────────────────────────────────────────────────
    @router.get(
        "",
        response_model=AgentCardListResponse,
        summary="List A2A agent cards",
    )
    def list_cards(
        tenant_id: Optional[str] = Query(None),
        enabled_only: bool = Query(False),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.A2A_CARD_READ)),
    ) -> AgentCardListResponse:
        admin, admin_type = admin_and_type
        resolved_tenant = _resolve_tenant_id(admin, admin_type, tenant_id)
        _enforce_a2a_enabled(db, resolved_tenant)

        q = db.query(AgentCard).filter(AgentCard.tenant_id == resolved_tenant)
        if enabled_only:
            q = q.filter(AgentCard.enabled.is_(True))
        rows = q.order_by(AgentCard.created_at.desc()).all()

        return AgentCardListResponse(
            cards=[AgentCardListItem(**r.to_dict(include_card_json=False)) for r in rows],
            total=len(rows),
        )

    # ────────────────────────────────────────────────────────────────────
    # Tenant public key — for external verifiers.
    # Declared BEFORE the /{card_id} GET so FastAPI's first-match
    # routing doesn't try to resolve 'tenant-key' as a card_id.
    # ────────────────────────────────────────────────────────────────────
    @router.get(
        "/tenant-key",
        summary="Get the tenant's A2A Ed25519 public signing key",
    )
    def get_tenant_public_key(
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.A2A_TENANT_KEY_READ)),
    ):
        from app.api.v1.models.admin.agent_card import TenantA2APublicKeyResponse
        from app.database.models.tenant_a2a_key import TenantA2AKey
        from app.services.a2a.card_signing import get_or_create_tenant_key

        admin, admin_type = admin_and_type
        resolved_tenant = _resolve_tenant_id(admin, admin_type, tenant_id)
        _enforce_a2a_enabled(db, resolved_tenant)

        # Lazy create — every read on this endpoint guarantees a
        # keypair exists afterwards. Matters for tenants that existed
        # before the A2A migration ran.
        encryption_service = _encryption_service(logger)
        get_or_create_tenant_key(db, resolved_tenant, encryption_service)
        db.commit()

        row = db.query(TenantA2AKey).filter(
            TenantA2AKey.tenant_id == resolved_tenant
        ).first()
        return TenantA2APublicKeyResponse(**row.to_public_dict())

    # ────────────────────────────────────────────────────────────────────
    # Catalog (a2aregistry.org)
    #
    # Declared BEFORE /{card_id} so FastAPI's first-match routing
    # doesn't try to resolve 'catalog' as a card_id.
    # ────────────────────────────────────────────────────────────────────
    @router.get(
        "/catalog",
        response_model=CatalogResponse,
        summary="List public A2A agents from a2aregistry.org",
    )
    def get_registry_catalog(
        tenant_id: Optional[str] = Query(None),
        force_refresh: bool = Query(
            False,
            description="Bypass the in-memory cache and re-fetch from the registry",
        ),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.A2A_CARD_IMPORT)),
    ) -> CatalogResponse:
        admin, admin_type = admin_and_type
        resolved_tenant = _resolve_tenant_id(admin, admin_type, tenant_id)
        _enforce_a2a_enabled(db, resolved_tenant)

        agents, cached = block_on(_fetch_registry_catalog(force_refresh=force_refresh))
        return CatalogResponse(
            agents=agents,
            total=len(agents),
            fetched_at=datetime.now(timezone.utc).isoformat(),
            cached=cached,
        )

    # ────────────────────────────────────────────────────────────────────
    # Detail
    # ────────────────────────────────────────────────────────────────────
    @router.get(
        "/{card_id}",
        response_model=AgentCardDetailResponse,
        summary="Get an A2A agent card by id",
    )
    def get_card(
        card_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.A2A_CARD_READ)),
    ) -> AgentCardDetailResponse:
        admin, admin_type = admin_and_type
        resolved_tenant = _resolve_tenant_id(admin, admin_type, tenant_id)
        _enforce_a2a_enabled(db, resolved_tenant)
        card = _get_card_or_404(db, card_id, resolved_tenant)
        return _card_to_detail(db, card)

    # ────────────────────────────────────────────────────────────────────
    # Create
    # ────────────────────────────────────────────────────────────────────
    @router.post(
        "",
        response_model=AgentCardDetailResponse,
        status_code=201,
        summary="Create an A2A agent card",
    )
    def create_card(
        body: AgentCardCreateRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _perm=Depends(require_permission(Permission.A2A_CARD_CREATE)),
    ) -> AgentCardDetailResponse:
        admin, admin_type = admin_and_type
        resolved_tenant = _resolve_tenant_id(admin, admin_type, tenant_id)
        _enforce_a2a_enabled(db, resolved_tenant)

        # Tenant quota — Max Agent Cards.
        TenantService.check_quota(db, resolved_tenant, TenantResourceTypes.AGENT_CARDS)

        try:
            validate_card_json(body.card_json)
        except InvalidCardError as e:
            raise HTTPException(400, str(e))

        # The extended card (if supplied) is itself a full AgentCard the proxy
        # round-trips through its typed model — validate it up front so we never
        # persist a body that GetExtendedAgentCard would reject at read time.
        if body.extended_card_json is not None:
            try:
                validate_card_json(body.extended_card_json)
            except InvalidCardError as e:
                raise HTTPException(400, f"extended_card_json: {e}")

        # Uniqueness — name must be unique per tenant. Rely on the
        # UNIQUE constraint to be the source of truth, but pre-check
        # for a friendlier error.
        dup = db.query(AgentCard).filter(
            AgentCard.tenant_id == resolved_tenant,
            AgentCard.name == body.name,
        ).first()
        if dup is not None:
            raise HTTPException(
                409,
                f"An agent card named {body.name!r} already exists in this tenant",
            )

        # Body iconUrl wins over explicit icon_url only when the operator
        # didn't provide one — the explicit field is the override path
        # (admin re-skinning a card whose source has no/poor icon).
        body_icon = body.icon_url
        if not body_icon:
            raw = body.card_json.get("iconUrl") or body.card_json.get("icon_url")
            if isinstance(raw, str) and raw.strip():
                body_icon = raw.strip()[:2048]

        card = AgentCard(
            id=f"agentcard-{ULID()}",
            tenant_id=resolved_tenant,
            name=body.name,
            version=body.version,
            description=body.description,
            source_url=body.source_url,
            icon_url=body_icon,
            card_json=body.card_json,
            extended_card_json=body.extended_card_json,
            trust_tier=TRUST_TIER_UNSIGNED,
            enabled=body.enabled,
            created_by=getattr(admin, "username", None) or getattr(admin, "id", None),
        )
        db.add(card)
        db.flush()
        sync_capabilities(db, card)
        db.commit()
        db.refresh(card)

        logger.info(
            "A2A agent card created",
            context={
                "card_id": card.id,
                "tenant_id": resolved_tenant,
                "name": card.name,
                "capability_count": len(card.capabilities),
            },
        )
        audit.log(
            action="a2a.card.create",
            db=db,
            admin=admin,
            resource_type="agent_card",
            resource_id=card.id,
            tenant_id=resolved_tenant,
            details={"name": card.name, "trust_tier": card.trust_tier},
        )
        return _card_to_detail(db, card)

    # ────────────────────────────────────────────────────────────────────
    # Update
    # ────────────────────────────────────────────────────────────────────
    @router.patch(
        "/{card_id}",
        response_model=AgentCardDetailResponse,
        summary="Update an A2A agent card",
    )
    def update_card(
        card_id: str,
        body: AgentCardUpdateRequest,
        request: Request,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _perm=Depends(require_permission(Permission.A2A_CARD_UPDATE)),
    ) -> AgentCardDetailResponse:
        admin, admin_type = admin_and_type
        resolved_tenant = _resolve_tenant_id(admin, admin_type, tenant_id)
        _enforce_a2a_enabled(db, resolved_tenant)
        card = _get_card_or_404(db, card_id, resolved_tenant)

        if card.trust_tier == TRUST_TIER_REVOKED:
            raise HTTPException(409, "Cannot update a revoked card — create a new one")

        changed_body = False
        if body.name is not None:
            card.name = body.name
        if body.version is not None:
            card.version = body.version
        if body.description is not None:
            card.description = body.description
        if body.icon_url is not None:
            card.icon_url = body.icon_url.strip()[:2048] or None
        if body.enabled is not None:
            card.enabled = body.enabled
        # The gateway refuses every call of a card whose policy document
        # it cannot read: such a document is not saved.
        if body.tenant_limits is not None:
            from app.database.models.resource_group import validate_a2a_limits

            ok, problem = validate_a2a_limits(body.tenant_limits)
            if not ok:
                raise HTTPException(422, f"tenant_limits: {problem}")
            card.tenant_limits = body.tenant_limits
        if body.tenant_governance is not None:
            from app.database.models.resource_group import validate_a2a_governance

            ok, problem = validate_a2a_governance(body.tenant_governance)
            if not ok:
                raise HTTPException(422, f"tenant_governance: {problem}")
            card.tenant_governance = body.tenant_governance
        if body.extended_card_json is not None:
            try:
                validate_card_json(body.extended_card_json)
            except InvalidCardError as e:
                raise HTTPException(400, f"extended_card_json: {e}")
            card.extended_card_json = body.extended_card_json
        if body.card_json is not None:
            try:
                validate_card_json(body.card_json)
            except InvalidCardError as e:
                raise HTTPException(400, str(e))
            card.card_json = body.card_json
            changed_body = True
            # Re-derive icon from the new body unless the same PATCH
            # also pinned an explicit override.
            if body.icon_url is None:
                raw_icon = body.card_json.get("iconUrl") or body.card_json.get("icon_url")
                if isinstance(raw_icon, str) and raw_icon.strip():
                    card.icon_url = raw_icon.strip()[:2048]
                else:
                    card.icon_url = None

        # `card_json` is the source of truth and the columns are
        # denormalised from it, so a version set through the Audit Info
        # tab has to land in the document too. Without this the row would
        # say 2.0 while every client fetching the card still reads 1.0 —
        # and the card is the thing clients actually trust.
        #
        # Applied after the `card_json` branch above so an explicit
        # `version` wins over one carried in a body replaced in the same
        # PATCH: the operator typed the former.
        synced = _apply_version_to_card_json(card.card_json, body.version)
        if synced is not None:
            card.card_json = synced
            changed_body = True

        # Any body change invalidates an existing signature. We keep
        # the historical signature + signed_by / signed_at on the row
        # for audit so we can see "X signed version Y at time Z, then
        # A edited the body" — trust_tier flipping back to unsigned is
        # what tells the policy engine to stop honouring the old sig.
        if changed_body and card.signature is not None:
            card.trust_tier = TRUST_TIER_UNSIGNED
            sync_capabilities(db, card)

        db.commit()
        db.refresh(card)

        audit.log(
            action="a2a.card.update",
            db=db,
            admin=admin,
            resource_type="agent_card",
            resource_id=card.id,
            tenant_id=resolved_tenant,
            details={
                "body_changed": changed_body,
                "trust_tier_after": card.trust_tier,
            },
        )
        # Evict the gateway's AgentCardCache so limits/trust changes
        # take effect now, not at TTL expiry.
        block_on(invalidate_proxy_cache(request, "agent_card", resolved_tenant, card.id, "update"))
        return _card_to_detail(db, card)

    # ────────────────────────────────────────────────────────────────────
    # Delete
    # ────────────────────────────────────────────────────────────────────
    @router.delete(
        "/{card_id}",
        status_code=204,
        summary="Delete an A2A agent card",
    )
    def delete_card(
        card_id: str,
        request: Request,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _perm=Depends(require_permission(Permission.A2A_CARD_DELETE)),
    ):
        admin, admin_type = admin_and_type
        resolved_tenant = _resolve_tenant_id(admin, admin_type, tenant_id)
        _enforce_a2a_enabled(db, resolved_tenant)
        card = _get_card_or_404(db, card_id, resolved_tenant)

        name_for_audit = card.name
        db.delete(card)
        db.commit()

        audit.log(
            action="a2a.card.delete",
            db=db,
            admin=admin,
            resource_type="agent_card",
            resource_id=card_id,
            tenant_id=resolved_tenant,
            details={"name": name_for_audit},
        )
        block_on(invalidate_proxy_cache(request, "agent_card", resolved_tenant, card_id, "delete"))

    # ────────────────────────────────────────────────────────────────────
    # Sign
    # ────────────────────────────────────────────────────────────────────
    @router.post(
        "/{card_id}/sign",
        response_model=AgentCardDetailResponse,
        summary="Sign an A2A agent card with the tenant's Ed25519 key",
    )
    def sign_card_endpoint(
        card_id: str,
        request: Request,
        _body: AgentCardSignRequest = AgentCardSignRequest(),
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _perm=Depends(require_permission(Permission.A2A_CARD_SIGN)),
    ) -> AgentCardDetailResponse:
        admin, admin_type = admin_and_type
        resolved_tenant = _resolve_tenant_id(admin, admin_type, tenant_id)
        _enforce_a2a_enabled(db, resolved_tenant)
        card = _get_card_or_404(db, card_id, resolved_tenant)

        encryption_service = _encryption_service(logger)
        signer_id = getattr(admin, "username", None) or getattr(admin, "id", "unknown")
        try:
            service_sign_card(
                db, card,
                signed_by=signer_id,
                encryption_service=encryption_service,
            )
        except A2ASigningError as e:
            raise HTTPException(409, str(e))

        db.commit()
        db.refresh(card)

        audit.log(
            action="a2a.card.sign",
            db=db,
            admin=admin,
            resource_type="agent_card",
            resource_id=card.id,
            tenant_id=resolved_tenant,
            details={"signed_by": signer_id, "trust_tier": card.trust_tier},
        )
        block_on(invalidate_proxy_cache(request, "agent_card", resolved_tenant, card.id, "update"))
        return _card_to_detail(db, card)

    # ────────────────────────────────────────────────────────────────────
    # Revoke
    # ────────────────────────────────────────────────────────────────────
    @router.post(
        "/{card_id}/revoke",
        response_model=AgentCardDetailResponse,
        summary="Revoke an A2A agent card (terminal)",
    )
    def revoke_card_endpoint(
        card_id: str,
        body: AgentCardRevokeRequest,
        request: Request,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _perm=Depends(require_permission(Permission.A2A_CARD_REVOKE)),
    ) -> AgentCardDetailResponse:
        admin, admin_type = admin_and_type
        resolved_tenant = _resolve_tenant_id(admin, admin_type, tenant_id)
        _enforce_a2a_enabled(db, resolved_tenant)
        card = _get_card_or_404(db, card_id, resolved_tenant)

        if card.trust_tier == TRUST_TIER_REVOKED:
            # Idempotent — a second revoke is a no-op success, not a 409.
            return _card_to_detail(db, card)

        revoker_id = getattr(admin, "username", None) or getattr(admin, "id", "unknown")
        service_revoke_card(db, card, reason=body.reason, revoked_by=revoker_id)
        db.commit()
        db.refresh(card)

        audit.log(
            action="a2a.card.revoke",
            db=db,
            admin=admin,
            resource_type="agent_card",
            resource_id=card.id,
            tenant_id=resolved_tenant,
            details={"reason": body.reason, "revoked_by": revoker_id},
        )
        # Revocation must reach the gateway immediately — do not wait
        # out the card cache TTL.
        block_on(invalidate_proxy_cache(request, "agent_card", resolved_tenant, card.id, "update"))
        return _card_to_detail(db, card)

    # ────────────────────────────────────────────────────────────────────
    # Health check (single + bulk)
    #
    # Mirrors LLM model + MCP server health-check endpoints. We
    # GET each card's `/.well-known/agent-card.json` discovery URL
    # and write `healthy` / `unhealthy` / `unknown` back to the card
    # row. Permission: A2A_CARD_UPDATE — non-destructive but mutates
    # state (health_status + last_health_check_at).
    # ────────────────────────────────────────────────────────────────────
    @router.post(
        "/{card_id}/health-check",
        response_model=AgentCardHealthResult,
        summary="Probe a single agent card and record liveness",
    )
    def health_check_card(
        card_id: str,
        timeout: float = Query(
            5.0, ge=1.0, le=30.0,
            description="Per-card probe timeout in seconds",
        ),
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.A2A_CARD_UPDATE)),
    ) -> AgentCardHealthResult:
        admin, admin_type = admin_and_type
        resolved_tenant = _resolve_tenant_id(admin, admin_type, tenant_id)
        _enforce_a2a_enabled(db, resolved_tenant)
        card = _get_card_or_404(db, card_id, resolved_tenant)

        status, error, target_url = block_on(_probe_agent_card(card, timeout))
        _record_health(card, status, error)
        db.commit()
        db.refresh(card)

        return AgentCardHealthResult(
            card_id=card.id,
            name=card.name,
            status=status,
            error=error,
            checked_at=card.last_health_check_at.isoformat()
            if card.last_health_check_at else None,
            target_url=target_url,
        )

    @router.post(
        "/health-check/bulk",
        response_model=AgentCardBulkHealthResponse,
        summary="Probe every registered agent card in parallel",
    )
    def bulk_health_check_cards(
        enabled_only: bool = Query(
            False,
            description="Limit probing to cards with `enabled=true`.",
        ),
        timeout: float = Query(
            5.0, ge=1.0, le=30.0,
            description="Per-card probe timeout in seconds",
        ),
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.A2A_CARD_UPDATE)),
    ) -> AgentCardBulkHealthResponse:
        admin, admin_type = admin_and_type
        resolved_tenant = _resolve_tenant_id(admin, admin_type, tenant_id)
        _enforce_a2a_enabled(db, resolved_tenant)

        query = db.query(AgentCard).filter(
            AgentCard.tenant_id == resolved_tenant,
            AgentCard.trust_tier != TRUST_TIER_REVOKED,
        )
        if enabled_only:
            query = query.filter(AgentCard.enabled == True)  # noqa: E712
        cards = query.all()

        async def _probe(card: AgentCard) -> AgentCardHealthResult:
            status, error, target_url = await _probe_agent_card(card, timeout)
            return AgentCardHealthResult(
                card_id=card.id,
                name=card.name,
                status=status,
                error=error,
                target_url=target_url,
            )

        if cards:
            probe_results = block_on_all(
                *(_probe(c) for c in cards), return_exceptions=False,
            )
        else:
            probe_results = []

        # Persist outcomes — sequential SQLAlchemy mutations are fine
        # since the network probes already ran in parallel above.
        now_iso: Optional[str] = None
        for card, result in zip(cards, probe_results):
            _record_health(card, result.status, result.error)
            if card.last_health_check_at and now_iso is None:
                now_iso = card.last_health_check_at.isoformat()
            result.checked_at = (
                card.last_health_check_at.isoformat()
                if card.last_health_check_at else None
            )
        db.commit()

        healthy = sum(1 for r in probe_results if r.status == HEALTH_HEALTHY)
        unhealthy = sum(1 for r in probe_results if r.status == HEALTH_UNHEALTHY)
        unknown = sum(1 for r in probe_results if r.status == HEALTH_UNKNOWN)

        logger.info(
            "Agent card bulk health check complete",
            context={
                "tenant_id": resolved_tenant,
                "total": len(probe_results),
                "healthy": healthy,
                "unhealthy": unhealthy,
                "unknown": unknown,
            },
        )

        return AgentCardBulkHealthResponse(
            total=len(probe_results),
            healthy=healthy,
            unhealthy=unhealthy,
            unknown=unknown,
            results=probe_results,
        )

    # ────────────────────────────────────────────────────────────────────
    # Discover (URL → preview, no DB write)
    #
    # The "Add Agent Card" dialog mirrors MCP's "Add Server" flow:
    # operator pastes a URL, hits **Connect**, the gateway fetches the
    # card and parses skills, and the UI renders a preview before the
    # operator clicks **Create**. This endpoint is the Connect step.
    #
    # Permission: A2A_CARD_IMPORT (same as /import, since the network
    # round-trip leaves the same trail and the same SSRF surface as
    # final create).
    # ────────────────────────────────────────────────────────────────────
    @router.post(
        "/discover",
        response_model=AgentCardDiscoverResponse,
        summary="Fetch + parse a remote A2A agent card without persisting it",
    )
    def discover_card(
        body: AgentCardDiscoverRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.A2A_CARD_IMPORT)),
    ) -> AgentCardDiscoverResponse:
        admin, admin_type = admin_and_type
        resolved_tenant = _resolve_tenant_id(admin, admin_type, tenant_id)
        _enforce_a2a_enabled(db, resolved_tenant)

        card_json, resolved_url = block_on(_fetch_well_known_card(
            body.source_url, body.timeout_secs
        ))
        # validate so the operator gets the same 400 they'd get on
        # final create — preview lying about validity would be worse
        # than failing fast.
        try:
            validate_card_json(card_json)
        except InvalidCardError as e:
            raise HTTPException(400, str(e))

        # Provider name lives at card_json.provider.organization in
        # v0.3 / v1.0; tolerate either shape and a few alternates we've
        # seen in the wild.
        provider_name = None
        provider = card_json.get("provider")
        if isinstance(provider, dict):
            provider_name = (
                provider.get("organization")
                or provider.get("name")
                or None
            )

        capabilities = _extract_discovered_capabilities(card_json)

        # Surface name conflict so the UI can pre-populate a
        # name_override field — saves a round-trip 409 on Create.
        candidate_name = (card_json.get("name") or "").strip()
        name_conflict = False
        if candidate_name:
            existing = db.query(AgentCard).filter(
                AgentCard.tenant_id == resolved_tenant,
                AgentCard.name == candidate_name,
            ).first()
            name_conflict = existing is not None

        icon_url = card_json.get("iconUrl") or card_json.get("icon_url")
        if isinstance(icon_url, str):
            icon_url = icon_url.strip()[:2048] or None
        else:
            icon_url = None

        return AgentCardDiscoverResponse(
            source_url=body.source_url,
            resolved_url=resolved_url,
            name=candidate_name or None,
            description=card_json.get("description"),
            version=str(card_json.get("version", "")) or None,
            icon_url=icon_url,
            protocol_version=str(card_json.get("protocolVersion", "")) or None,
            provider_name=provider_name,
            capabilities=capabilities,
            capability_count=len(capabilities),
            card_json=card_json,
            name_conflict=name_conflict,
        )

    # ────────────────────────────────────────────────────────────────────
    # Import from URL — single
    # ────────────────────────────────────────────────────────────────────
    @router.post(
        "/import",
        response_model=AgentCardDetailResponse,
        status_code=201,
        summary="Import an A2A agent card from a .well-known URL",
    )
    def import_card(
        body: AgentCardImportRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _perm=Depends(require_permission(Permission.A2A_CARD_IMPORT)),
    ) -> AgentCardDetailResponse:
        admin, admin_type = admin_and_type
        resolved_tenant = _resolve_tenant_id(admin, admin_type, tenant_id)
        _enforce_a2a_enabled(db, resolved_tenant)

        card_json, resolved_url = block_on(_fetch_well_known_card(
            body.source_url, body.timeout_secs
        ))
        card = _persist_imported_card(
            db,
            tenant_id=resolved_tenant,
            admin=admin,
            card_json=card_json,
            resolved_url=resolved_url,
            name_override=body.name_override,
            description_override=body.description_override,
            enabled=body.enabled,
        )
        db.commit()
        db.refresh(card)

        audit.log(
            action="a2a.card.import",
            db=db,
            admin=admin,
            resource_type="agent_card",
            resource_id=card.id,
            tenant_id=resolved_tenant,
            details={
                "source_url": body.source_url,
                "resolved_url": resolved_url,
                "name": card.name,
            },
        )
        return _card_to_detail(db, card)

    # ────────────────────────────────────────────────────────────────────
    # Import from URL — bulk
    #
    # The catalog modal uses this. One bad URL doesn't fail the whole
    # batch — every row gets its own outcome so the UI can render
    # partial success. Each row is committed in its own transaction so
    # a duplicate-name 409 on row 7 doesn't roll back the first six
    # successful imports.
    # ────────────────────────────────────────────────────────────────────
    @router.post(
        "/import-bulk",
        response_model=AgentCardBulkImportResponse,
        summary="Import multiple A2A agent cards from external URLs",
    )
    def import_cards_bulk(
        body: AgentCardBulkImportRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _perm=Depends(require_permission(Permission.A2A_CARD_IMPORT)),
    ) -> AgentCardBulkImportResponse:
        admin, admin_type = admin_and_type
        resolved_tenant = _resolve_tenant_id(admin, admin_type, tenant_id)
        _enforce_a2a_enabled(db, resolved_tenant)

        results: List[BulkImportResult] = []
        succeeded = 0
        failed = 0

        for item in body.imports:
            try:
                card_json, resolved_url = block_on(_fetch_well_known_card(
                    item.source_url, body.timeout_secs
                ))
                card = _persist_imported_card(
                    db,
                    tenant_id=resolved_tenant,
                    admin=admin,
                    card_json=card_json,
                    resolved_url=resolved_url,
                    name_override=item.name_override,
                    description_override=None,
                    enabled=item.enabled,
                )
                db.commit()
                db.refresh(card)
                audit.log(
                    action="a2a.card.import",
                    db=db,
                    admin=admin,
                    resource_type="agent_card",
                    resource_id=card.id,
                    tenant_id=resolved_tenant,
                    details={
                        "source_url": item.source_url,
                        "resolved_url": resolved_url,
                        "name": card.name,
                        "bulk": True,
                    },
                )
                results.append(BulkImportResult(
                    source_url=item.source_url,
                    success=True,
                    card_id=card.id,
                    name=card.name,
                ))
                succeeded += 1
            except HTTPException as e:
                db.rollback()
                results.append(BulkImportResult(
                    source_url=item.source_url,
                    success=False,
                    # A refusal whose detail is structured (a licence
                    # limit) is reported by its sentence.
                    error=TenantService.quota_refusal_reason(e),
                ))
                failed += 1
            except Exception as e:  # last-ditch — never let one row kill the batch
                db.rollback()
                logger.error(
                    "Bulk import row failed unexpectedly",
                    context={"source_url": item.source_url, "error": str(e)},
                )
                results.append(BulkImportResult(
                    source_url=item.source_url,
                    success=False,
                    error=f"Unexpected error: {e}",
                ))
                failed += 1

        return AgentCardBulkImportResponse(
            results=results,
            total=len(results),
            succeeded=succeeded,
            failed=failed,
        )

    # ────────────────────────────────────────────────────────────────────
    # Resource-group bindings
    # ────────────────────────────────────────────────────────────────────
    @router.put(
        "/{card_id}/resource-groups",
        summary="Replace the set of resource groups bound to this agent card",
    )
    def set_card_resource_groups(
        card_id: str,
        body: AgentCardGroupsRequest,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        # Decided before the body is read: a caller that may not act on the
        # tenant is refused, whatever it sent.
        resolved_tenant: str = Depends(_request_tenant),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _perm=Depends(require_permission(Permission.A2A_CARD_UPDATE)),
    ):
        admin, admin_type = admin_and_type
        _enforce_a2a_enabled(db, resolved_tenant)
        card = _get_card_or_404(db, card_id, resolved_tenant)

        group_ids = [str(g) for g in body.group_ids]

        # Validate groups belong to the same tenant — prevents a sysadmin
        # from accidentally binding one tenant's card to another tenant's
        # group via a malformed body.
        if group_ids:
            from app.database.models.resource_group import ResourceGroup
            valid_groups = (
                db.query(ResourceGroup)
                .filter(
                    ResourceGroup.id.in_(group_ids),
                    ResourceGroup.tenant_id == resolved_tenant,
                )
                .all()
            )
            found_ids = {g.id for g in valid_groups}
            missing = set(group_ids) - found_ids
            if missing:
                raise HTTPException(
                    400,
                    f"Groups not found in tenant: {', '.join(sorted(missing))}",
                )
            card.resource_groups = valid_groups
        else:
            card.resource_groups = []

        db.commit()
        db.refresh(card)

        audit.log(
            action="a2a.card.set_groups",
            db=db,
            admin=admin,
            resource_type="agent_card",
            resource_id=card.id,
            tenant_id=resolved_tenant,
            details={"group_ids": sorted(group_ids)},
        )

        # Which cards a group may invoke is materialised into the gateway's
        # `EffectiveGroupData.effective_agent_card_ids`, so a binding change
        # that isn't published stays invisible to the proxy until the 5-minute
        # group-cache TTL lapses — the operator binds a card in the Admin UI,
        # the row is there, and A2A still answers "not bound to the caller's
        # resource group". The "agent_card" message drops both the card entry
        # and the tenant's materialised groups.
        block_on(invalidate_proxy_cache(
            request, "agent_card", resolved_tenant, card.id, "assign"
        ))

        return _card_to_detail(db, card)

    return router
