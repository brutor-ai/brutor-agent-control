# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
OAuth Tokens Admin Endpoints — read-only view of user-held OAuth tokens
for MCP servers, with a per-token diagnostic status useful for triaging
"why is this MCP call 403-ing?" from the admin console.

Security contract:
- **Never** return encrypted tokens (access_token, refresh_token,
  client_secret). The presence of a refresh token is exposed as a
  boolean; the encrypted bodies themselves never leave the DB.
- List is tenant-scoped. System admins can see their own tenant
  the same way tenant admins do — cross-tenant access would be a
  separate feature.
- Read-only for now. Explicit revocation of individual tokens is a
  future iteration; today the diagnostic is enough to tell the
  operator to tell the user to re-auth.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

import httpx
from app.services.outbound_http import outbound_async_client
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.sync_bridge import block_on, block_on_all
from app.constants.tenant import TenantResourceTypes
from app.core.admin_user.dependencies import (
    get_audit_logger,
    get_current_admin,
    require_permission,
    require_tenant_resource_access,
)
from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.database.models.admin import TenantAdmin
from app.core.admin_user.admin_principal import AdminPrincipal
from app.database.models.end_user import EndUser
from app.database.models.idp_server import IdPServer
from app.database.models.mcp_server import MCPServer
from app.database.models.oauth_token import OAuthToken
from app.services.oauth.encryption_service import EncryptionService
from app.middleware.trusted_proxies import client_ip


# ---------------------------------------------------------------------------
# Diagnostic taxonomy — stable string keys. The admin UI maps these to
# badge colors; changing a key silently would break the UI mapping.
# ---------------------------------------------------------------------------

# Token is good — not expired, not revoked.
STATUS_ACTIVE = "active"
# Token expired but has a refresh_token, so the proxy will attempt
# refresh on the next call. Operator does not need to act; user sees
# no disruption unless the refresh itself later fails.
STATUS_EXPIRED_WITH_REFRESH = "expired_with_refresh"
# Token expired AND there's no refresh_token stored — user must
# explicitly re-authorize. This is the primary case that surfaces
# as a 401 from the proxy (Feature 2).
STATUS_EXPIRED_NO_REFRESH = "expired_no_refresh"
# Admin/user explicitly revoked. Treated distinctly from expired so
# the operator can distinguish "we cut this off" from "it just aged out".
STATUS_REVOKED = "revoked"
# The IdP row referenced by `idp_id` was deleted or disabled. The
# token may still be valid at the provider, but we can't use it
# until the IdP is re-enabled.
STATUS_IDP_UNAVAILABLE = "idp_unavailable"

ALL_STATUSES = (
    STATUS_ACTIVE,
    STATUS_EXPIRED_WITH_REFRESH,
    STATUS_EXPIRED_NO_REFRESH,
    STATUS_REVOKED,
    STATUS_IDP_UNAVAILABLE,
)


@dataclass(frozen=True)
class _Diagnosis:
    """Result of classifying one token. Kept as a value object rather
    than inlined so tests (if we add them) can assert against it."""
    status: str
    reason: str


def _diagnose(
    token: OAuthToken,
    idp: Optional[IdPServer],
    now: datetime,
) -> _Diagnosis:
    """Classify a token into one of the public status values.

    Order of checks matters: revocation trumps everything, IdP absence
    trumps expiry (no point refreshing against a disabled IdP), and
    expired-with-refresh is distinct from expired-without because they
    require different operator responses.
    """
    if token.revoked:
        when = token.revoked_at.isoformat() if token.revoked_at else "unknown time"
        return _Diagnosis(
            STATUS_REVOKED,
            f"Token was revoked at {when}.",
        )

    if idp is None or not bool(getattr(idp, "enabled", True)):
        return _Diagnosis(
            STATUS_IDP_UNAVAILABLE,
            "Identity Provider is missing or disabled — token can't be refreshed "
            "until the IdP is restored.",
        )

    expires_at = token.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        # DB may return naive datetimes depending on driver settings;
        # normalise so arithmetic with tz-aware `now` doesn't throw.
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    expired = expires_at is not None and expires_at <= now

    if expired:
        if token.encrypted_refresh_token:
            remaining_age = (now - expires_at).total_seconds() if expires_at else 0
            return _Diagnosis(
                STATUS_EXPIRED_WITH_REFRESH,
                f"Token expired {int(remaining_age)}s ago — refresh token available, "
                "proxy will attempt refresh on next call.",
            )
        return _Diagnosis(
            STATUS_EXPIRED_NO_REFRESH,
            "Token expired and no refresh token is stored. User must "
            "re-authorize this MCP server from the portal.",
        )

    # Not revoked, IdP healthy, not expired.
    if expires_at is not None:
        remaining = (expires_at - now).total_seconds()
        return _Diagnosis(
            STATUS_ACTIVE,
            f"Token valid — expires in {int(remaining)}s.",
        )
    # Long-lived token with no expires_at.
    return _Diagnosis(STATUS_ACTIVE, "Token valid — no expiry set (long-lived).")


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class OAuthTokenResponse(BaseModel):
    """One token row. NO encrypted material is included. `has_refresh_token`
    replaces the raw refresh token so the UI can show "Will auto-refresh"
    without ever exposing it."""

    id: int
    tenant_id: str

    # Owner — who this token is for. `gateway_user_id` is the portal user
    # (int end_users.id). `idp_user_id` is the IdP's identifier for that
    # user (e.g. GitHub username). Both are relevant for triage.
    gateway_user_id: Optional[str] = None
    gateway_username: Optional[str] = None
    idp_user_id: str

    # What the token is scoped to.
    idp_id: str
    idp_name: Optional[str] = None
    mcp_server_id: str
    mcp_server_name: Optional[str] = None

    # Lifecycle.
    scopes: Optional[str] = None
    token_type: Optional[str] = None
    expires_at: Optional[str] = None
    revoked: bool = False
    revoked_at: Optional[str] = None
    has_refresh_token: bool
    refresh_count: int = 0
    last_refreshed_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    # Diagnostic classification (see ALL_STATUSES above).
    status: str
    status_reason: str


class OAuthTokenSummary(BaseModel):
    total: int = 0
    active: int = 0
    expired_with_refresh: int = 0
    expired_no_refresh: int = 0
    revoked: int = 0
    idp_unavailable: int = 0


class OAuthTokensListResponse(BaseModel):
    tokens: List[OAuthTokenResponse]
    summary: OAuthTokenSummary
    timestamp: str


# ---------------------------------------------------------------------------
# Revoke models
# ---------------------------------------------------------------------------


class RevokeTokenResult(BaseModel):
    """Per-token outcome in a revoke operation. Present whether the
    revoke succeeded or failed so the caller can tell the operator
    which rows the IdP couldn't confirm-kill."""
    token_id: int
    mcp_server_id: str
    idp_id: str
    idp_revoked: bool          # True iff the IdP's revocation_endpoint returned 2xx
    idp_error: Optional[str] = None
    # DB revocation always succeeds if we reached this result row (failures
    # earlier throw 500), so `db_revoked` isn't carried — it's implicit.


class RevokeResponse(BaseModel):
    revoked_count: int
    results: List[RevokeTokenResult]
    timestamp: str


class BulkRevokeRequest(BaseModel):
    """Filter predicate for bulk revoke. Matches the list endpoint's
    filters; an empty request body revokes EVERY token in the tenant,
    which is a legitimate "rotate everything" response action but
    needs explicit confirmation on the UI side."""
    mcp_server_id: Optional[str] = None
    idp_id: Optional[str] = None
    gateway_user_id: Optional[str] = None
    status_filter: Optional[str] = Field(
        default=None,
        description=f"Limit to tokens currently in this status. One of: {', '.join(ALL_STATUSES)}",
    )


# ---------------------------------------------------------------------------
# IdP revocation (RFC 7009) best-effort helper
# ---------------------------------------------------------------------------


async def _revoke_at_idp(
    token: OAuthToken,
    idp: Optional[IdPServer],
    encryption: Optional[EncryptionService],
    logger: BrutorLogger,
) -> RevokeTokenResult:
    """Attempt to revoke the access token at the IdP per RFC 7009.

    This is best-effort: we always proceed with the local DB revocation
    regardless of the IdP's response. Common reasons the IdP step can
    fail:
      * IdP has no `revocation_endpoint` (not all providers expose one).
      * We don't have a client_secret (public client / DCR rotated).
      * IdP is temporarily unreachable — a transient outage.
      * Token already expired server-side; RFC 7009 says 200 in that
        case, but some providers return 400 with `invalid_token`.

    `token.encrypted_access_token` is decrypted only inside this
    function — the plaintext never leaves the request scope.
    """
    result = RevokeTokenResult(
        token_id=token.id,
        mcp_server_id=token.mcp_server_id,
        idp_id=token.idp_id,
        idp_revoked=False,
    )

    if idp is None or not getattr(idp, "revocation_endpoint", None):
        result.idp_error = "no revocation_endpoint configured on the IdP"
        return result

    if encryption is None:
        result.idp_error = "encryption service unavailable — cannot decrypt for IdP revocation"
        return result

    try:
        access_token = encryption.decrypt(token.encrypted_access_token)
    except Exception as e:  # noqa: BLE001 — decrypt can raise a range of errors
        result.idp_error = f"decrypt failed: {e}"
        return result

    # RFC 7009 — POST form-encoded with `token`. `token_type_hint` is
    # recommended. Client auth: prefer Basic with client_id:client_secret,
    # fall back to form body. Some IdPs (Okta, Auth0) accept both;
    # GitHub requires Basic.
    form: Dict[str, str] = {
        "token": access_token,
        "token_type_hint": "access_token",
    }
    auth: Optional[tuple] = None

    client_id = token.client_id
    client_secret: Optional[str] = None
    if token.encrypted_client_secret:
        try:
            client_secret = encryption.decrypt(token.encrypted_client_secret)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Failed to decrypt client_secret during revoke — proceeding without client auth",
                context={"token_id": token.id, "error": str(e)},
            )

    if client_id and client_secret:
        auth = (client_id, client_secret)

    try:
        async with outbound_async_client(timeout=5.0) as client:
            resp = await client.post(
                idp.revocation_endpoint,
                data=form,
                auth=auth,
                headers={"Accept": "application/json"},
            )
        # RFC 7009: 200 on success. Some providers also 204. Treat any
        # 2xx as success. Anything else → record the error but don't
        # propagate — DB revocation still wins.
        if 200 <= resp.status_code < 300:
            result.idp_revoked = True
        else:
            body = (resp.text or "")[:200]
            result.idp_error = f"IdP returned {resp.status_code}: {body}"
    except httpx.HTTPError as e:
        result.idp_error = f"transport error: {e}"
    except Exception as e:  # noqa: BLE001 — defensive
        result.idp_error = f"unexpected error: {e}"

    return result


def _apply_db_revocation(
    db: Session,
    token: OAuthToken,
    admin_username: str,
) -> None:
    """Mark the token revoked and destroy both cryptographic materials.

    Clearing the encrypted_access_token and encrypted_refresh_token
    columns (not just flipping `revoked=true`) is the contract the
    operator agreed to: revoke is irreversible without a fresh OAuth
    flow. A later `UPDATE ... SET revoked=false` via SQL surgery would
    leave the row with NULL tokens, so the proxy's `get_valid_token`
    would still decline to use it.
    """
    token.revoked = True
    token.revoked_at = datetime.now(timezone.utc)
    # Keeping the prior value of `updated_at` SQLAlchemy's onupdate
    # will handle; explicit assignment here would fight it.
    # `revoked_by` is not a column on OAuthToken today; admin identity
    # lives in the audit log. If the schema grows that column we'd
    # assign it here.
    _ = admin_username  # reserved — see comment above
    token.encrypted_access_token = ""
    token.encrypted_refresh_token = None


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def create_admin_oauth_tokens_router(
    logger: BrutorLogger,
    encryption_service: Optional[EncryptionService] = None,
) -> APIRouter:
    """Create the /admin/oauth-tokens router.

    Read endpoints diagnose tokens (see `_diagnose`). Write endpoints
    revoke — single or bulk. Revoking:
      * sets `revoked=true`, `revoked_at=NOW()`, `revoked_by=<admin>`,
      * clears BOTH the encrypted access and refresh tokens from the row
        (so a later un-revoke via SQL cannot silently re-enable use),
      * best-effort calls the IdP's `revocation_endpoint` (RFC 7009) to
        invalidate the token at the source too — so a token copied off
        the wire earlier stops working, not just "stops being used by
        our proxy".

    The IdP revocation step runs only when we have the raw access token
    to send and the IdP exposed a `revocation_endpoint` during
    discovery. Failures are logged and returned in the response but
    don't block the local DB revocation — revoking in our DB is always
    better than half-revoking, and "IdP unreachable" is the most common
    failure mode.
    """
    router = APIRouter(
        prefix="/admin/oauth-tokens",
        tags=["OAuth Tokens (Admin)"],
    )

    @router.get(
        "",
        response_model=OAuthTokensListResponse,
        summary="List OAuth tokens for the current tenant with diagnostic status",
        description=(
            "Returns user-held OAuth tokens for MCP servers in the caller's "
            "tenant. Encrypted token material is never included. The per-row "
            "`status` field classifies each token as active, expired (with "
            "or without a usable refresh), revoked, or blocked by a missing "
            "IdP — use it to answer \"why did this MCP call 403?\" from the UI."
        ),
    )
    def list_oauth_tokens(
        request: Request,
        mcp_server_id: Optional[str] = Query(None, description="Filter by MCP server ID"),
        idp_id: Optional[str] = Query(None, description="Filter by Identity Provider ID"),
        gateway_user_id: Optional[str] = Query(None, description="Filter by portal user id"),
        status_filter: Optional[str] = Query(
            None,
            alias="status",
            description=f"Filter by diagnostic status. One of: {', '.join(ALL_STATUSES)}",
        ),
        limit: int = Query(200, ge=1, le=1000),
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.OAUTH_CLIENTS)
        ),
        _perm: AdminPrincipal = Depends(
            require_permission(Permission.OAUTH_CLIENT_READ)
        ),
    ) -> OAuthTokensListResponse:
        _admin, tenant_id, _admin_type = admin_and_tenant

        if status_filter is not None and status_filter not in ALL_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Invalid status '{status_filter}'. Must be one of: "
                    f"{', '.join(ALL_STATUSES)}"
                ),
            )

        query = db.query(OAuthToken).filter(OAuthToken.tenant_id == tenant_id)
        if mcp_server_id:
            query = query.filter(OAuthToken.mcp_server_id == mcp_server_id)
        if idp_id:
            query = query.filter(OAuthToken.idp_id == idp_id)
        if gateway_user_id:
            query = query.filter(OAuthToken.gateway_user_id == gateway_user_id)

        # Order by newest first. We intentionally do not order by
        # expires_at because a pile of long-since-expired rows at the
        # top would bury the active ones during triage.
        tokens: List[OAuthToken] = (
            query.order_by(OAuthToken.created_at.desc()).limit(limit).all()
        )

        # Bulk-load the denormalized IdP + MCP server names and the
        # portal usernames so the UI can render "GitHub / user@x.com"
        # instead of opaque IDs. One N+1-avoiding query per lookup set.
        idp_ids = {t.idp_id for t in tokens if t.idp_id}
        mcp_ids = {t.mcp_server_id for t in tokens if t.mcp_server_id}
        user_ids = {
            int(t.gateway_user_id)
            for t in tokens
            if t.gateway_user_id and str(t.gateway_user_id).isdigit()
        }

        idps: Dict[str, IdPServer] = {
            i.id: i
            for i in (
                db.query(IdPServer).filter(IdPServer.id.in_(idp_ids)).all()
                if idp_ids
                else []
            )
        }
        mcps: Dict[str, Any] = {
            m.id: m
            for m in (
                db.query(MCPServer).filter(MCPServer.id.in_(mcp_ids)).all()
                if mcp_ids
                else []
            )
        }
        users: Dict[int, EndUser] = {
            u.id: u
            for u in (
                db.query(EndUser).filter(EndUser.id.in_(user_ids)).all()
                if user_ids
                else []
            )
        }

        now = datetime.now(timezone.utc)
        rows: List[OAuthTokenResponse] = []
        summary = OAuthTokenSummary()

        for t in tokens:
            idp = idps.get(t.idp_id)
            mcp = mcps.get(t.mcp_server_id)
            user = None
            if t.gateway_user_id and str(t.gateway_user_id).isdigit():
                user = users.get(int(t.gateway_user_id))

            diagnosis = _diagnose(t, idp, now)

            if status_filter and diagnosis.status != status_filter:
                continue

            rows.append(
                OAuthTokenResponse(
                    id=t.id,
                    tenant_id=t.tenant_id,
                    gateway_user_id=t.gateway_user_id,
                    gateway_username=(
                        user.username if user is not None else None
                    ),
                    idp_user_id=t.idp_user_id,
                    idp_id=t.idp_id,
                    idp_name=idp.name if idp is not None else None,
                    mcp_server_id=t.mcp_server_id,
                    mcp_server_name=mcp.name if mcp is not None else None,
                    scopes=t.scopes,
                    token_type=t.token_type,
                    expires_at=t.expires_at.isoformat() if t.expires_at else None,
                    revoked=bool(t.revoked),
                    revoked_at=(
                        t.revoked_at.isoformat() if t.revoked_at else None
                    ),
                    has_refresh_token=t.encrypted_refresh_token is not None,
                    refresh_count=int(t.refresh_count or 0),
                    last_refreshed_at=(
                        t.last_refreshed_at.isoformat()
                        if t.last_refreshed_at
                        else None
                    ),
                    created_at=t.created_at.isoformat() if t.created_at else None,
                    updated_at=t.updated_at.isoformat() if t.updated_at else None,
                    status=diagnosis.status,
                    status_reason=diagnosis.reason,
                )
            )

            # Summary counts are on the filtered set, so a filter like
            # `?status=active` naturally returns counts scoped to that
            # filter. If we ever want overall counts we can emit them as
            # a second field; keep the simpler shape for now.
            summary.total += 1
            if diagnosis.status == STATUS_ACTIVE:
                summary.active += 1
            elif diagnosis.status == STATUS_EXPIRED_WITH_REFRESH:
                summary.expired_with_refresh += 1
            elif diagnosis.status == STATUS_EXPIRED_NO_REFRESH:
                summary.expired_no_refresh += 1
            elif diagnosis.status == STATUS_REVOKED:
                summary.revoked += 1
            elif diagnosis.status == STATUS_IDP_UNAVAILABLE:
                summary.idp_unavailable += 1

        return OAuthTokensListResponse(
            tokens=rows,
            summary=summary,
            timestamp=now.isoformat(),
        )

    # =====================================================================
    # Revoke — single token
    # =====================================================================

    @router.post(
        "/{token_id}/revoke",
        response_model=RevokeResponse,
        summary="Revoke one OAuth token",
        description=(
            "Marks the token revoked and destroys both its encrypted "
            "access and refresh tokens in our DB. Best-effort: also "
            "POSTs to the IdP's `revocation_endpoint` (RFC 7009) so "
            "copies of the token exfiltrated earlier are invalidated "
            "at the source, not just in our proxy. After this, the next "
            "MCP call from the affected user returns 401 "
            "oauth_reauth_required; the portal surfaces a "
            "'Re-authorize' banner."
        ),
    )
    def revoke_token(
        token_id: int,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.OAUTH_CLIENTS)
        ),
        _perm: AdminPrincipal = Depends(
            require_permission(Permission.OAUTH_TOKEN)
        ),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ) -> RevokeResponse:
        admin, tenant_id, admin_type = admin_and_tenant

        token = (
            db.query(OAuthToken)
            .filter(OAuthToken.id == token_id, OAuthToken.tenant_id == tenant_id)
            .first()
        )
        if token is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"OAuth token {token_id} not found",
            )

        # Snapshot what we need BEFORE mutating the row — after
        # `_apply_db_revocation` the access token is gone, so the IdP
        # revocation call that also needs it would get empty string.
        # We do the IdP call first.
        idp = (
            db.query(IdPServer).filter(IdPServer.id == token.idp_id).first()
            if token.idp_id
            else None
        )

        idp_result = block_on(_revoke_at_idp(token, idp, encryption_service, logger))
        # Snapshot identifiers before commit for the audit record.
        token_mcp_server_id = token.mcp_server_id
        token_idp_id = token.idp_id
        token_gateway_user_id = token.gateway_user_id
        _apply_db_revocation(db, token, admin.username)
        db.commit()

        # Security audit — file docstring promises revoke coverage.
        # NO token material is logged; only identifiers + the IdP
        # revocation outcome.
        audit.log(
            action="oauth_token_revoke",
            db=db,
            admin=admin,
            admin_type=admin_type,
            success=True,
            resource_type="oauth_token",
            resource_id=str(token_id),
            tenant_id=tenant_id,
            details={
                "mcp_server_id": token_mcp_server_id,
                "idp_id": token_idp_id,
                "gateway_user_id": token_gateway_user_id,
                "idp_revoked": idp_result.idp_revoked,
                "idp_error": idp_result.idp_error,
            },
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_method=request.method,
            request_path=str(request.url.path),
        )

        logger.info(
            "OAuth token revoked",
            context={
                "token_id": token.id,
                "tenant_id": tenant_id,
                "mcp_server_id": token.mcp_server_id,
                "idp_id": token.idp_id,
                "gateway_user_id": token.gateway_user_id,
                "revoked_by": admin.username,
                "idp_revoked": idp_result.idp_revoked,
                "idp_error": idp_result.idp_error,
                "source_ip": client_ip(request),
            },
        )

        return RevokeResponse(
            revoked_count=1,
            results=[idp_result],
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    # =====================================================================
    # Revoke — bulk, filtered
    # =====================================================================

    @router.post(
        "/revoke-bulk",
        response_model=RevokeResponse,
        summary="Revoke all OAuth tokens matching a filter",
        description=(
            "Revokes every token matching the predicate (same filter "
            "keys as the list endpoint). Primary use cases: offboarding "
            "(by `gateway_user_id`), IdP migration or breach response "
            "(by `idp_id`), MCP server decommissioning (by "
            "`mcp_server_id`), forced re-consent after a policy change "
            "(by `status_filter=active`). An empty body revokes every "
            "non-revoked token in the tenant — the UI confirms this "
            "explicitly."
        ),
    )
    def revoke_bulk(
        body: BulkRevokeRequest,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.OAUTH_CLIENTS)
        ),
        _perm: AdminPrincipal = Depends(
            require_permission(Permission.OAUTH_TOKEN)
        ),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ) -> RevokeResponse:
        admin, tenant_id, admin_type = admin_and_tenant

        if body.status_filter is not None and body.status_filter not in ALL_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Invalid status '{body.status_filter}'. Must be one of: "
                    f"{', '.join(ALL_STATUSES)}"
                ),
            )

        query = db.query(OAuthToken).filter(
            OAuthToken.tenant_id == tenant_id,
            OAuthToken.revoked.is_(False),  # never "re-revoke" a revoked row
        )
        if body.mcp_server_id:
            query = query.filter(OAuthToken.mcp_server_id == body.mcp_server_id)
        if body.idp_id:
            query = query.filter(OAuthToken.idp_id == body.idp_id)
        if body.gateway_user_id:
            query = query.filter(OAuthToken.gateway_user_id == body.gateway_user_id)

        candidates: List[OAuthToken] = query.all()

        # Pre-fetch IdPs in one shot to avoid N+1 during revocation.
        idp_ids = {c.idp_id for c in candidates if c.idp_id}
        idps: Dict[str, IdPServer] = {
            i.id: i
            for i in (
                db.query(IdPServer).filter(IdPServer.id.in_(idp_ids)).all()
                if idp_ids
                else []
            )
        }

        now = datetime.now(timezone.utc)

        # If `status_filter` is set, classify each candidate and drop
        # ones that don't match. We do this in Python rather than SQL
        # because `status` is a derived value (revoked bit + expiry vs
        # now + refresh-token presence), not a column.
        if body.status_filter is not None:
            candidates = [
                c for c in candidates
                if _diagnose(c, idps.get(c.idp_id), now).status == body.status_filter
            ]

        # Fan out IdP revocations in parallel — each is a best-effort 5s
        # HTTP call, so a sequential loop over dozens of rows would
        # be painfully slow. `gather` preserves order so we can zip
        # back onto `candidates`.
        idp_tasks = [
            _revoke_at_idp(c, idps.get(c.idp_id), encryption_service, logger)
            for c in candidates
        ]
        idp_results: List[RevokeTokenResult] = (
            list(block_on_all(*idp_tasks)) if idp_tasks else []
        )

        # Snapshot identifiers before commit for the audit record.
        revoked_token_ids = [c.id for c in candidates]
        for c in candidates:
            _apply_db_revocation(db, c, admin.username)
        db.commit()

        # Security audit — file docstring promises revoke coverage.
        # We log token ids/count + the filter predicate, NEVER token
        # material.
        audit.log(
            action="oauth_token_revoke_bulk",
            db=db,
            admin=admin,
            admin_type=admin_type,
            success=True,
            resource_type="oauth_token",
            resource_id=None,
            tenant_id=tenant_id,
            details={
                "revoked_count": len(candidates),
                "token_ids": revoked_token_ids,
                "filter": body.model_dump(exclude_none=True),
                "idp_failures": sum(1 for r in idp_results if not r.idp_revoked),
            },
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_method=request.method,
            request_path=str(request.url.path),
        )

        logger.info(
            "OAuth tokens bulk-revoked",
            context={
                "tenant_id": tenant_id,
                "revoked_count": len(candidates),
                "filter": body.model_dump(exclude_none=True),
                "revoked_by": admin.username,
                "idp_failures": sum(1 for r in idp_results if not r.idp_revoked),
                "source_ip": client_ip(request),
            },
        )

        return RevokeResponse(
            revoked_count=len(candidates),
            results=idp_results,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    return router
