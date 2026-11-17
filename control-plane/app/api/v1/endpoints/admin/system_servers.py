# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
System Server Configuration API Endpoints

Manages configuration for built-in system MCP servers:
- Search provider settings (Brave, Google, SearXNG)
"""

from typing import Optional, Union, List
from fastapi import APIRouter, Depends, HTTPException, status, Request
from pydantic import BaseModel, Field, ConfigDict, field_validator
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.core.sync_bridge import block_on
from app.database.models import TenantAdmin
from app.database.models.search_provider_config import SearchProviderConfig as SearchProviderConfigRow
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.services.oauth.encryption_service import EncryptionService
from app.services.config_manager import ConfigManager
from app.services import release_scope
from app.core.admin_user.dependencies import (
    require_permission,
    get_audit_logger
)
from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.core.admin_user.admin_principal import AdminPrincipal
from app.middleware.trusted_proxies import client_ip


# =============================================================================
# Pydantic Models
# =============================================================================

class SearchProviderConfig(BaseModel):
    """Search provider configuration response."""
    # DB column is an autoincrement Integer (see
    # `app/database/models/search_provider_config.py`). Pydantic v2
    # is strict about int↔str coercion, so keep this typed as int.
    id: Optional[int] = None
    tenant_id: Optional[str] = None
    provider: str = "brave"
    api_key_configured: bool = False  # Never expose actual key
    google_cx_id: Optional[str] = None
    searxng_base_url: Optional[str] = None
    max_results_per_query: Optional[int] = Field(
        None, description="Not available in this release: always null (the gateway does not cap the results of a search)."
    )
    safe_search: bool = True
    allowed_domains: List[str] = []
    blocked_domains: List[str] = []
    daily_query_limit: Optional[int] = Field(
        None, description="Not available in this release: always null (the gateway does not count searches per day)."
    )
    enabled: bool = True


class UpdateSearchProviderRequest(BaseModel):
    """Update search provider configuration."""

    model_config = ConfigDict(extra="forbid")
    provider: Optional[str] = Field(None, pattern="^(brave|google|searxng)$")
    # Plain text on the wire; encrypted at rest via EncryptionService
    # before the UPDATE (see update_search_provider below).
    api_key: Optional[str] = None
    google_cx_id: Optional[str] = None
    searxng_base_url: Optional[str] = None
    max_results_per_query: Optional[int] = Field(
        None, description="Not available in this release: a value is refused (the gateway does not cap the results of a search)."
    )
    safe_search: Optional[bool] = None
    allowed_domains: Optional[List[str]] = None
    blocked_domains: Optional[List[str]] = None
    daily_query_limit: Optional[int] = Field(
        None, description="Not available in this release: a value is refused (the gateway does not count searches per day). A per-tool frequency limit on the search tool is enforced."
    )
    enabled: Optional[bool] = None

    @field_validator("max_results_per_query")
    @classmethod
    def _results_in_this_release(cls, value: Optional[int]) -> Optional[int]:
        if value is not None:
            raise ValueError(
                release_scope.not_available("max_results_per_query", why="the gateway does not cap the results of a search")
            )
        return value

    @field_validator("daily_query_limit")
    @classmethod
    def _daily_limit_in_this_release(cls, value: Optional[int]) -> Optional[int]:
        if value is not None:
            raise ValueError(
                release_scope.not_available(
                    "daily_query_limit",
                    why="the gateway does not count searches per day; a per-tool frequency limit on the search tool is enforced",
                )
            )
        return value


# =============================================================================
# Router Factory
# =============================================================================

def create_system_servers_admin_router(logger: Optional[BrutorLogger] = None) -> APIRouter:
    """Create router for System Server admin endpoints."""
    if logger is None:
        logger = BrutorLogger("system-servers")

    router = APIRouter(prefix="/admin/system-servers", tags=["System Servers"])

    # =========================================================================
    # Search Provider Config
    # =========================================================================

    @router.get(
        "/search-config",
        response_model=SearchProviderConfig,
        summary="Get search provider configuration",
    )
    def get_search_config(
        request: Request,
        db: Session = Depends(get_db),
        admin: AdminPrincipal = Depends(require_permission(Permission.GATEWAY_CONFIG_READ)),
    ):
        """Get the current search provider configuration for this tenant."""
        tenant_id = getattr(request.state, 'tenant_id', 'default')

        row = db.execute(
            text("""
                SELECT id, tenant_id, provider, api_key_encrypted, google_cx_id,
                       searxng_base_url, max_results_per_query, safe_search,
                       allowed_domains, blocked_domains, daily_query_limit, enabled
                FROM search_provider_configs
                WHERE tenant_id = :tid
                LIMIT 1
            """),
            {"tid": tenant_id}
        ).fetchone()

        if not row:
            return SearchProviderConfig(tenant_id=tenant_id)

        return SearchProviderConfig(
            id=row[0],
            tenant_id=row[1],
            provider=row[2],
            api_key_configured=bool(row[3]),
            google_cx_id=row[4],
            searxng_base_url=row[5],
            safe_search=row[7],
            allowed_domains=row[8] or [],
            blocked_domains=row[9] or [],
            enabled=row[11],
        )

    @router.put(
        "/search-config",
        response_model=SearchProviderConfig,
        summary="Update search provider configuration",
    )
    def update_search_config(
        request: Request,
        body: UpdateSearchProviderRequest,
        db: Session = Depends(get_db),
        admin: AdminPrincipal = Depends(require_permission(Permission.GATEWAY_CONFIG_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        """Update or create the search provider configuration for this tenant.

        Uses the ORM rather than raw SQL so `JSON`-typed columns
        (`allowed_domains`, `blocked_domains`) get serialised correctly
        from Python lists — the earlier hand-written SQL failed with
        `can't adapt type 'list'` on every save because psycopg2 has
        no default adapter for list→JSON on a raw bind parameter.
        """
        tenant_id = getattr(request.state, 'tenant_id', 'default')

        row: Optional[SearchProviderConfigRow] = (
            db.query(SearchProviderConfigRow)
            .filter(SearchProviderConfigRow.tenant_id == tenant_id)
            .first()
        )

        updates = body.model_dump(exclude_unset=True)
        # `api_key` in the request → `api_key_encrypted` on the row.
        # The column name has always promised Fernet-at-rest, but
        # the previous version of this handler stored plaintext —
        # the matching Rust read path in
        # `services/search_server.rs::load_provider_config` ALSO
        # didn't decrypt, so the bug was symmetrical and invisible
        # to operators (Brave / Google search worked because both
        # ends agreed on plaintext). The proxy now decrypts on
        # read; if we don't encrypt here too, every saved key
        # 401s upstream. Wrap the value through EncryptionService
        # so write and read are finally honest about the column
        # name.
        api_key_value = updates.pop("api_key", None)
        if api_key_value:
            try:
                _encryption = EncryptionService(
                    config_manager=ConfigManager(logger=logger, validate=False),
                    logger=logger,
                )
                api_key_value = _encryption.encrypt(api_key_value)
            except Exception:
                # Not stored in clear: the gateway decrypts it.
                logger.exception("Failed to encrypt search-provider api_key; nothing saved")
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "api_key cannot be stored: this control plane could not encrypt it "
                        "(ENCRYPTION_KEY), and a credential is never stored in clear. Nothing was saved."
                    ),
                ) from None

        if row is None:
            # Create with defaults for anything the admin didn't send.
            row = SearchProviderConfigRow(
                tenant_id=tenant_id,
                provider=updates.get("provider", "brave"),
                enabled=updates.get("enabled", True),
                api_key_encrypted=api_key_value,
                google_cx_id=updates.get("google_cx_id"),
                searxng_base_url=updates.get("searxng_base_url"),
                max_results_per_query=updates.get("max_results_per_query", 10),
                safe_search=updates.get("safe_search", True),
                daily_query_limit=updates.get("daily_query_limit"),
                allowed_domains=updates.get("allowed_domains"),
                blocked_domains=updates.get("blocked_domains"),
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            audit_action = "search_provider_config_create"
            audit_resource_id = str(row.id)
            audit_details = {"provider": row.provider}
        else:
            if api_key_value is not None:
                row.api_key_encrypted = api_key_value
            for key, value in updates.items():
                # Only touch attributes the ORM knows about to avoid
                # silently creating typos as runtime attributes.
                if hasattr(row, key):
                    setattr(row, key, value)
            db.commit()
            db.refresh(row)
            audit_action = "search_provider_config_update"
            audit_resource_id = str(row.id)
            audit_details = {"updated_fields": list(updates.keys()) + (
                ["api_key"] if api_key_value is not None else []
            )}

        admin_obj, admin_type = (admin, None) if not isinstance(admin, tuple) else admin
        audit.log(
            action=audit_action,
            db=db,
            admin=admin_obj,
            admin_type=admin_type,
            success=True,
            resource_type="search_provider_config",
            resource_id=audit_resource_id,
            tenant_id=tenant_id,
            details=audit_details,
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_method=request.method,
            request_path=str(request.url.path),
        )

        # Return updated config
        return block_on(get_search_config(request, db, admin))

    @router.delete(
        "/search-config/api-key",
        summary="Clear the search provider API key",
    )
    def clear_search_api_key(
        request: Request,
        db: Session = Depends(get_db),
        admin: AdminPrincipal = Depends(require_permission(Permission.GATEWAY_CONFIG_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        """Remove the API key from the search provider configuration."""
        tenant_id = getattr(request.state, 'tenant_id', 'default')
        db.execute(
            text("UPDATE search_provider_configs SET api_key_encrypted = NULL, updated_at = NOW() WHERE tenant_id = :tid"),
            {"tid": tenant_id}
        )
        db.commit()

        row: Optional[SearchProviderConfigRow] = (
            db.query(SearchProviderConfigRow)
            .filter(SearchProviderConfigRow.tenant_id == tenant_id)
            .first()
        )

        admin_obj, admin_type = (admin, None) if not isinstance(admin, tuple) else admin
        audit.log(
            action="search_provider_api_key_clear",
            db=db,
            admin=admin_obj,
            admin_type=admin_type,
            success=True,
            resource_type="search_provider_config",
            resource_id=str(row.id) if row else None,
            tenant_id=tenant_id,
            details={"api_key_cleared": True},
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_method=request.method,
            request_path=str(request.url.path),
        )

        return {"message": "API key cleared"}

    return router
