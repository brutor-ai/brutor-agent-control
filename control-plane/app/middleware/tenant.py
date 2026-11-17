# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Tenant Middleware
FastAPI middleware for automatic tenant context injection
"""


import asyncio

from fastapi import Request, HTTPException, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response, JSONResponse
from typing import Callable

from app.constants.tenant import TenantDefault
from app.constants.http import HttpHeaders
from app.database import get_db
from app.services.tenant.tenant_service import TenantContext, TenantService
from app.core.logging_system import BrutorLogger

logger = BrutorLogger("tenant_middleware")


class TenantMiddleware(BaseHTTPMiddleware):
    """
    Middleware to automatically set tenant context for each request

    This middleware:
    1. Extracts tenant_id from the verified credential (JWT claim / API key)
    2. Falls back to the X-Tenant-ID header
    3. Falls back to default tenant if neither available
    4. Validates tenant is active
    5. Sets tenant context for the request
    6. Adds tenant info to request.state for easy access
    """

    # Paths that don't require tenant context (public endpoints)
    EXCLUDED_PATHS = [
        '/health',
        '/healthz',
        '/docs',
        '/redoc',
        '/openapi.json',
        '/favicon.ico',
        # Sys Admin login endpoint (tenant is not used for system admin)
        '/v1/auth/system/login',
        # Tenant Admin login endpoint (before tenant is known)
        '/v1/auth/tenant/login',
        # System Admin endpoints for managing tenants
        '/v1/admin/tenants',
        # Public tenant theme — the user portal calls this BEFORE any
        # user signs in so it can theme the login screen with the
        # tenant's brand. The endpoint itself does the tenant lookup
        # from the path parameter; the middleware mustn't 401 it.
        '/v1/public/tenants',
    ]

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """
        Process each request and inject tenant context

        Priority order for tenant resolution:
        1. Verified credential (tenant-admin JWT claim or API-key row)
        2. X-Tenant-ID header (system admins; header-only contexts)
        3. Default tenant (for backwards compatibility)

        Args:
            request: Incoming request
            call_next: Next middleware/endpoint handler

        Returns:
            Response from next handler
        """
        # Check if path is excluded
        if self._is_excluded_path(request.url.path):
            return await call_next(request)

        db = None
        tenant_id = None
        tenant_source = None

        # The tenant lookup is synchronous SQLAlchemy + (potentially) JWT decode.
        # This middleware runs on EVERY request, so doing the DB work inline would
        # block the single Uvicorn event loop for the duration of each query under
        # load. Offload the blocking resolution to a worker thread; only the
        # contextvar / request.state mutations stay on the event loop so the
        # TenantContext propagates into the downstream handler.
        tenant_id_header = request.headers.get(HttpHeaders.X_TENANT_ID.value)
        try:
            db, tenant_id, tenant_source, tenant = await asyncio.to_thread(
                self._resolve_tenant_blocking, request, tenant_id_header
            )

            # Set tenant context for this request (must run on the event-loop
            # task so the contextvar is visible to call_next downstream).
            TenantContext.set_tenant_id(tenant_id)

            # Store tenant in request.state for easy access
            request.state.tenant_id = tenant_id
            request.state.tenant = tenant
            request.state.tenant_source = tenant_source  # Track where tenant came from

            # Log tenant context with source
            logger.debug(f"Tenant context set for request", context={
                'tenant_id': tenant_id,
                'tenant_name': tenant.name if tenant is not None else None,
                'tenant_source': tenant_source,
                'path': request.url.path,
                'method': request.method
            })

        except HTTPException as http_exc:
            # Clean up before returning error response
            if db:
                db.close()
            TenantContext.clear()
            # Return JSON response directly (HTTPException doesn't work in middleware)
            return JSONResponse(
                status_code=http_exc.status_code,
                content={"detail": http_exc.detail}
            )
        except Exception as e:
            # Clean up before returning error response
            if db:
                db.close()
            TenantContext.clear()
            logger.error(f"Error in tenant middleware during setup: {e}", exc_info=True)
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"detail": "Error processing tenant context"}
            )

        # Process request - this is outside the try block so exceptions from
        # call_next don't get caught by our error handlers
        try:
            response = await call_next(request)

            # Add tenant header to response (for debugging/tracking)
            response.headers[HttpHeaders.X_TENANT_ID.value] = tenant_id
            response.headers[HttpHeaders.X_TENANT_SOURCE.value] = tenant_source

            return response
        finally:
            # Always clean up after request processing
            if db:
                db.close()
            TenantContext.clear()

    def _resolve_tenant_blocking(self, request: Request, tenant_id_header):
        """
        Resolve the tenant for a request using synchronous DB access.

        This runs inside a worker thread (via ``asyncio.to_thread``) so the
        synchronous SQLAlchemy queries and any JWT/API-key decoding never block
        the event loop. Only header reads off ``request`` happen here — the body
        is never touched — so accessing ``request`` from the worker thread is safe.

        Returns a ``(db, tenant_id, tenant_source, tenant)`` tuple. The caller
        owns ``db`` and must close it once the request is done. On any failure the
        session is closed here before the exception propagates, so a thrown
        exception never leaks a connection.
        """
        db = next(get_db())
        try:
            tenant_id = None
            tenant_source = None

            # PRIORITY 1: a verified credential — tenant-admin JWT claim or
            # API-key row. A signed tenant must win over one anyone can type
            # into a header (the core proxy makes the same choice). System
            # admins carry no tenant and fall through to the header, which
            # is how they name the tenant they act on.
            try:
                tenant_id, tenant_source = TenantService.get_tenant_from_request(request, db)
                if tenant_id:
                    logger.debug(f"Tenant ID found via {tenant_source}: {tenant_id}")
            except Exception as e:
                logger.debug(f"Could not extract tenant from request: {e}")

            # PRIORITY 2: X-Tenant-ID header, only when no credential named one
            if not tenant_id and tenant_id_header:
                tenant_id = tenant_id_header
                tenant_source = "header"
                logger.debug(f"Tenant ID found in {HttpHeaders.X_TENANT_ID.value} header: {tenant_id}")

            # PRIORITY 3: Use default tenant if neither header nor JWT available
            if not tenant_id:
                logger.debug("No tenant in header or JWT, using default tenant")
                default_tenant = TenantService.get_or_create_default_tenant(db)
                tenant_id = default_tenant.id
                tenant_source = TenantDefault.ID.value

            # Validate tenant exists and is active. `None` means the read
            # model vouched for it within the last few seconds (no query).
            tenant = TenantService.validate_tenant_cached(db, tenant_id)

            return db, tenant_id, tenant_source, tenant
        except Exception:
            # Don't leak the connection if resolution fails (e.g. validate_tenant
            # raises HTTPException for a suspended/unknown tenant).
            db.close()
            raise

    def _is_excluded_path(self, path: str) -> bool:
        """
        Check if path should be excluded from tenant context

        Args:
            path: Request path

        Returns:
            True if path is excluded and this would be a public endpoint
        """
        # Exact match
        if path in self.EXCLUDED_PATHS:
            return True

        # Prefix match
        for excluded_path in self.EXCLUDED_PATHS:
            if path.startswith(excluded_path):
                return True

        # Icon endpoints are public (icons are non-sensitive, server_id is globally unique)
        if path.endswith('/icon'):
            return True

        return False