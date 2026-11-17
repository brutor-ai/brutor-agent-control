# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# RFC 7591 inbound Dynamic Client Registration

Public, unauthenticated `POST /v1/auth/dcr/{tenant_id}/
{idp_id}/register` endpoint that creates a Brutor-issued OAuth
client_id + client_secret for an MCP / OAuth client that wants to
self-register against this gateway. Conforms to RFC 7591 §2 (request)
and RFC 7591 §3 (response) on the wire.

# Why a NEW endpoint, not the existing /register proxy

The existing `/v1/proxy/oauth/{mcp_server_id}/{idp_server_id}/register`
in the Rust proxy is a TRANSPARENT FORWARDER to the upstream IdP's
own registration endpoint. That's wrong for inbound DCR because:

  1. It requires `x-tenant-id` header — not RFC 7591 compliant.
     A self-registering client has no way to know what header to
     send.
  2. It bypasses Brutor's broker-token model — the client ends up
     with raw upstream OAuth credentials, defeating the governance
     value Brutor brings.

This endpoint INSTEAD creates a Brutor-side oauth_app_clients
row with auto_registered=true. The client then uses these credentials
in Brutor's normal OAuth flow, where Brutor mediates with the
upstream IdP and the client only sees broker tokens.

# Security posture

The endpoint is PUBLIC (no auth, no tenant header). To defend
against drive-by registrations from a random internet attacker,
we apply:

  - Per-tenant quota: `MAX_AUTO_REGISTERED_CLIENTS_PER_TENANT`. Hard
    cap so a single bot can't fill a tenant's client list.
  - Audit log entry on every registration with the source IP.
  - Validation: redirect_uris must be http(s), no fragments, max 20.
  - The tenant_id + idp_id in the URL must point to a live IdP with
    `auto_register_enabled = true` (planned future setting; for now
    the IdP must just exist + be enabled). This lets a tenant
    explicitly opt-out of inbound DCR by deleting / disabling
    the IdP row.

What we explicitly do NOT defend against:

  - DDoS on the endpoint itself. The audit-export scheduler runs
    on the same process; a flood would
    starve them. Ingress-level rate limiting (the Helm chart
    annotates `proxy_body_size`; `nginx_limit_req` is not set yet)
    is the right defence.
"""

from __future__ import annotations

import logging
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from sqlalchemy.orm import Session
from ulid import ULID

from app.api.v1.models.auth.dcr import (
    DCRErrorResponse,
    DCRRegistrationRequest,
    DCRRegistrationResponse,
)
from app.constants.admin import AdminType
from app.core.admin_user.dependencies import get_audit_logger
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.database import get_db
from app.composition.identity import IDENTITY_PROTOCOLS
from app.database.models.idp_server import IdPServer
from app.database.models.oauth_app_client import OAuthAppClient
from app.middleware.trusted_proxies import client_ip


log = logging.getLogger("brutor-dcr")


# Per-tenant quota for auto-registered clients. Hard limit, not
# soft — once hit, new DCR requests return 429. Operators clean up
# stale rows via the admin UI's revoke action.
MAX_AUTO_REGISTERED_CLIENTS_PER_TENANT = 1000

# Client_id length matches the rest of Brutor's app_client_id
# pattern: `app-<ulid>` is 30 chars. Keeping the prefix makes it
# easy to grep audit logs and immediately know these came from DCR.
_CLIENT_ID_PREFIX = "app"
_CLIENT_SECRET_BYTES = 32  # 256 bits


def create_dcr_router(
    *,
    encryption_service,
    logger=None,
) -> APIRouter:
    """Build the dynamic client registration router.

    `encryption_service` is injected so the same Fernet primitive
    that encrypts every other OAuth secret at rest is used here too.
    """
    router = APIRouter(prefix="/auth/dcr", tags=["oauth-dcr"])

    @router.post(
        "/{tenant_id}/{idp_id}/register",
        response_model=DCRRegistrationResponse,
        responses={
            400: {"model": DCRErrorResponse, "description": "Invalid client metadata"},
            404: {"model": DCRErrorResponse, "description": "Unknown tenant or IdP"},
            429: {"model": DCRErrorResponse, "description": "Tenant DCR quota exceeded"},
        },
        summary="RFC 7591 Dynamic Client Registration",
        description=(
            "Self-register as an OAuth client. Returns a Brutor-issued "
            "client_id + client_secret usable with the gateway's OAuth "
            "proxy. Public endpoint — protected by per-tenant quota."
        ),
    )
    def register_client(
        tenant_id: str = Path(..., description="Target tenant"),
        idp_id: str = Path(..., description="IdP the new client will authenticate against"),
        body: DCRRegistrationRequest = ...,  # noqa: B008
        request: Request = ...,  # noqa: B008 — FastAPI special
        db: Session = Depends(get_db),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        # 1. Tenant + IdP existence check.
        idp = (
            db.query(IdPServer)
            .filter(IdPServer.id == idp_id)
            .filter(IdPServer.tenant_id == tenant_id)
            .first()
        )
        # An identity provider that is not an OAuth provider (another
        # protocol, one that is not registered, or none) is not a provider
        # clients register against: for this public endpoint it does not
        # exist.
        if idp is not None and not IDENTITY_PROTOCOLS.is_oauth(idp.protocol):
            idp = None
        if idp is None:
            # RFC 7591 doesn't define a 404 error_code, but the spec
            # explicitly allows non-2xx with a free-form error body.
            # We use `invalid_client_metadata` (the closest defined
            # code) so strict clients still parse the body.
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "invalid_client_metadata",
                    "error_description": (
                        f"IdP '{idp_id}' not found in tenant '{tenant_id}', "
                        "or DCR is not enabled for it."
                    ),
                },
            )
        if not idp.enabled:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "invalid_client_metadata",
                    "error_description": "IdP is disabled.",
                },
            )

        # 2. Per-tenant quota.
        existing_count = (
            db.query(OAuthAppClient)
            .filter(OAuthAppClient.tenant_id == tenant_id)
            .filter(OAuthAppClient.auto_registered == True)  # noqa: E712
            .count()
        )
        if existing_count >= MAX_AUTO_REGISTERED_CLIENTS_PER_TENANT:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "invalid_client_metadata",
                    "error_description": (
                        f"Tenant '{tenant_id}' has reached the DCR quota of "
                        f"{MAX_AUTO_REGISTERED_CLIENTS_PER_TENANT} clients. "
                        "Revoke unused clients in the admin console."
                    ),
                },
            )

        # 3. Generate Brutor-issued credentials.
        client_id_value = f"{_CLIENT_ID_PREFIX}-{ULID()}"
        client_secret_plaintext = secrets.token_urlsafe(_CLIENT_SECRET_BYTES)
        encrypted_secret = encryption_service.encrypt(client_secret_plaintext)

        # 4. Resolve app_type heuristic — RFC 7591 has 'web'/'native';
        # our model uses webapp/mobile/desktop/cli. Map sensibly.
        rfc_app_type = (body.application_type or "web").lower()
        app_type_map = {
            "web": "webapp",
            "webapp": "webapp",
            "native": "desktop",
            "mobile": "mobile",
            "desktop": "desktop",
            "cli": "cli",
            "service": "service",
        }
        internal_app_type = app_type_map.get(rfc_app_type, "webapp")

        # 5. Persist.
        client_row = OAuthAppClient(
            id=f"appc-{ULID()}",
            tenant_id=tenant_id,
            name=body.client_name or f"DCR-registered ({client_id_value[:12]})",
            app_type=internal_app_type,
            idp_id=idp_id,
            client_id=client_id_value,
            encrypted_client_secret=encrypted_secret,
            redirect_uri=body.redirect_uris[0],  # primary; rest live in metadata
            scopes=(body.scope or "").split() if body.scope else [],
            client_type="confidential",
            supports_pkce=True,
            allowed_grant_types=body.grant_types or ["authorization_code", "refresh_token"],
            description=body.client_uri,
            registered_by=None,  # no admin user — self-registered
            auto_registered=True,
        )
        db.add(client_row)
        db.commit()

        # Security audit (promised in the module docstring). The actor
        # is anonymous — DCR is a public, unauthenticated endpoint — so
        # we synthesise `dcr:anonymous` and route through the system
        # audit table. `details` carries non-secret client metadata
        # ONLY; the client_secret is NEVER logged.
        audit.log(
            action="oauth_dcr_register",
            db=db,
            admin_id="dcr:anonymous",
            admin_type=AdminType.SYSTEM_ADMIN_TYPE,
            success=True,
            resource_type="oauth_app_client",
            resource_id=client_id_value,
            tenant_id=tenant_id,
            affected_tenant_id=tenant_id,
            details={
                "idp_id": idp_id,
                "client_name": client_row.name,
                "app_type": internal_app_type,
                "redirect_uri_count": len(body.redirect_uris),
                "auto_registered": True,
            },
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_method=request.method,
            request_path=str(request.url.path),
        )

        log.info(
            "DCR client registered",
            extra={
                "tenant_id": tenant_id,
                "idp_id": idp_id,
                "client_id": client_id_value,
                "client_name": client_row.name,
                "source_ip": client_ip(request),
                "redirect_uri_count": len(body.redirect_uris),
            },
        )

        # 6. RFC 7591 response. Echo back the client's request fields
        # (RFC 7591 §3.2.1) so strict clients can verify the registration
        # was accepted as requested.
        issued_at = int(time.time())
        return DCRRegistrationResponse(
            client_id=client_id_value,
            client_secret=client_secret_plaintext,
            client_id_issued_at=issued_at,
            client_secret_expires_at=0,
            redirect_uris=body.redirect_uris,
            client_name=body.client_name,
            client_uri=body.client_uri,
            logo_uri=body.logo_uri,
            scope=body.scope,
            grant_types=body.grant_types or ["authorization_code", "refresh_token"],
            response_types=body.response_types or ["code"],
            token_endpoint_auth_method=body.token_endpoint_auth_method or "client_secret_basic",
            application_type=rfc_app_type,
            # Per RFC 7591 §3.2.2 — informational; future sprint adds
            # the matching GET/DELETE handler for self-service.
            registration_client_uri=None,
        )

    return router

