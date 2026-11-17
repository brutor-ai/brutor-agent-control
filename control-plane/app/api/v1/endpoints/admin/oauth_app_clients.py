# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
OAuth App Clients Endpoints (Admin) - Multi-Tenant Version with Unified Audit Logging
Admin endpoints for managing OAuth application clients with tenant isolation
"""

from fastapi import APIRouter, Depends, HTTPException, status, Query, Request
from sqlalchemy.orm import Session
from typing import Union, Optional
from datetime import datetime, timezone
from ulid import ULID

from app.core.sync_bridge import block_on
from app.constants.tenant import TenantResourceTypes
from app.services.tenant.tenant_service import TenantService
from app.constants.oauth import ClientType
from app.database import get_db
from app.database.models.admin import TenantAdmin
from app.core.admin_user.admin_principal import AdminPrincipal
from app.database.models.oauth_app_client import OAuthAppClient
from app.composition.identity import IDENTITY_PROTOCOLS
from app.database.models.idp_server import IdPServer
from app.database.models.server_config import ServerConfig
from app.services.oauth.encryption_service import EncryptionService
from app.services.oauth.client_registration_service import DynamicClientRegistrationService
from app.core.admin_user.dependencies import (
    require_permission,
    require_tenant_resource_access,
    get_audit_logger
)
from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.api.v1.models.admin.server_config import (
    OAuthAppClientCreate,
    OAuthAppClientUpdate,
    OAuthAppClientResponse,
    OAuthAppClientListResponse,
    AutoRegisterOAuthAppClientRequest
)
from app.api.v1.models.admin.in_use import IN_USE_RESPONSE, in_use
from app.core.logging_system import BrutorLogger
from app.middleware.trusted_proxies import client_ip


def _require_oauth_provider(idp: IdPServer) -> None:
    """Refuse (400) an identity provider that is not an OAuth provider.

    An OAuth client belongs to an OAuth 2.0 / OIDC provider. A row of another
    identity protocol (or of one that is not registered, or of none) has no
    token endpoint to speak of, and must never be handled as if it had."""
    if IDENTITY_PROTOCOLS.is_oauth(idp.protocol):
        return
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=(
            f"Identity Provider '{idp.name}' is not an OAuth provider "
            f"(protocol {idp.protocol!r}); an OAuth client cannot be attached to it"
        ),
    )


def create_admin_oauth_app_clients_router(
        encryption_service: EncryptionService,
        logger: BrutorLogger,
        dcr_service: Optional[DynamicClientRegistrationService] = None
) -> APIRouter:
    """
    Create admin OAuth app clients management router with multi-tenant support

    Tenant isolation:
    - All endpoints are tenant-scoped
    - System admins use tenant management screens (not these endpoints)
    """

    router = APIRouter(
        prefix="/admin/oauth-app-clients",
        tags=["OAuth App Clients (Admin)"]
    )

    @router.get(
        "",
        response_model=OAuthAppClientListResponse,
        summary="List OAuth app clients",
        description="List OAuth application clients for current tenant"
    )
    def list_oauth_app_clients(
            request: Request,
            app_type: Optional[str] = Query(None, description="Filter by app type"),
            idp_id: Optional[str] = Query(None, description="Filter by Identity Provider"),
            active_only: bool = Query(False, description="Only return active clients"),
            db: Session = Depends(get_db),
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.OAUTH_CLIENTS)),
            admin: AdminPrincipal = Depends(require_permission(Permission.OAUTH_CLIENT_READ))
    ):
        """
        List all OAuth application clients with tenant isolation.

        These represent OAuth clients for different application types
        (webapp, mobile, desktop, etc.) that can access MCP servers.
        """
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_username = admin.username
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            logger.debug("OAuth app clients list requested", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "app_type": app_type,
                "idp_id": idp_id
            })

            # Query with tenant filter
            query = db.query(OAuthAppClient).filter(
                OAuthAppClient.tenant_id == tenant_id
            )

            if app_type:
                query = query.filter(OAuthAppClient.app_type == app_type)

            if idp_id:
                query = query.filter(OAuthAppClient.idp_id == idp_id)

            if active_only:
                query = query.filter(OAuthAppClient.is_active == True)

            # Execute query
            clients = query.order_by(OAuthAppClient.name).all()

            # Convert to response models
            client_responses = [
                OAuthAppClientResponse(**client.to_dict())
                for client in clients
            ]

            logger.debug("OAuth app clients listed", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "count": len(client_responses)
            })

            return OAuthAppClientListResponse(
                clients=client_responses,
                total=len(client_responses)
            )

        except Exception as e:
            logger.error("Failed to list OAuth app clients", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to list OAuth app clients: {str(e)}"
            )

    @router.get(
        "/{client_id}",
        response_model=OAuthAppClientResponse,
        summary="Get OAuth app client details",
        description="Get OAuth app client details (tenant isolation enforced)"
    )
    def get_oauth_app_client(
            client_id: str,
            request: Request,
            db: Session = Depends(get_db),
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.OAUTH_CLIENTS)),
            admin: AdminPrincipal = Depends(require_permission(Permission.OAUTH_CLIENT_READ))
    ):
        """Get details of a specific OAuth app client (without secret) with tenant verification"""
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_username = admin.username
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # Query with tenant filter
            client = db.query(OAuthAppClient).filter(
                OAuthAppClient.id == client_id,
                OAuthAppClient.tenant_id == tenant_id
            ).first()

            if not client:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"OAuth app client '{client_id}' not found"
                )

            logger.debug("OAuth app client details accessed", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "client_id": client_id
            })

            return OAuthAppClientResponse(**client.to_dict())

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to retrieve OAuth app client", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "client_id": client_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to retrieve OAuth app client: {str(e)}"
            )

    @router.get(
        "/{client_id}/usage",
        summary="Get OAuth app client usage",
        description="Get OAuth app client usage (tenant isolation enforced)"
    )
    def get_oauth_app_client_usage(
            client_id: str,
            request: Request,
            db: Session = Depends(get_db),
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.OAUTH_CLIENTS)),
            admin: AdminPrincipal = Depends(require_permission(Permission.OAUTH_CLIENT_READ))
    ):
        """
        Get usage information for an OAuth app client.

        Shows which server configs are using this OAuth client (within tenant).
        """
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_username = admin.username
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # Query with tenant filter
            client = db.query(OAuthAppClient).filter(
                OAuthAppClient.id == client_id,
                OAuthAppClient.tenant_id == tenant_id
            ).first()

            if not client:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"OAuth app client '{client_id}' not found"
                )

            # Find all server configs using this client (scoped to tenant)
            configs = db.query(ServerConfig).filter(
                ServerConfig.tenant_id == tenant_id,
                ServerConfig.oauth_client_id == client_id
            ).all()

            usage_details = [
                {
                    "config_id": config.id,
                    "config_name": config.name,
                    "mcp_server_id": config.mcp_server_id,
                    "mcp_server_name": config.mcp_server.name if config.mcp_server else None,
                    "is_default": config.is_default,
                    "enabled": config.enabled,
                    "usage_count": config.usage_count,
                    "last_used": config.last_used.isoformat() + "Z" if config.last_used else None
                }
                for config in configs
            ]

            logger.debug("OAuth app client usage retrieved", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "client_id": client_id,
                "configs_using": len(usage_details)
            })

            return {
                "client_id": client_id,
                "client_name": client.name,
                "app_type": client.app_type,
                "total_configs_using": len(usage_details),
                "usage_details": usage_details,
                "last_used": client.last_used.isoformat() + "Z" if client.last_used else None,
                "queried_at": datetime.now(timezone.utc).isoformat() + "Z"
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get OAuth client usage", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "client_id": client_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get OAuth client usage: {str(e)}"
            )

    @router.post(
        "",
        response_model=OAuthAppClientResponse,
        status_code=status.HTTP_201_CREATED,
        summary="Create OAuth app client",
        description="Create a new OAuth application client for current tenant"
    )
    def create_oauth_app_client(
            client_data: OAuthAppClientCreate,
            request: Request,
            db: Session = Depends(get_db),
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.OAUTH_CLIENTS)),
            admin: AdminPrincipal = Depends(require_permission(Permission.OAUTH_CLIENT_CREATE)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """
        Create a new OAuth application client.

        This registers OAuth client credentials for an application
        (webapp, mobile, desktop, etc.) that will access MCP servers.
        """
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_username = admin.username
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            logger.debug("OAuth app client creation requested", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "client_name": client_data.name,
                "app_type": client_data.app_type,
                "idp_id": client_data.idp_id
            })

            #Validate IdP exists and belongs to same tenant
            idp = db.query(IdPServer).filter(
                IdPServer.id == client_data.idp_id,
                IdPServer.tenant_id == tenant_id
            ).first()

            if not idp:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Identity Provider '{client_data.idp_id}' not found in this tenant"
                )

            _require_oauth_provider(idp)

            # Check for duplicate client_id within the same IdP and tenant
            existing = db.query(OAuthAppClient).filter(
                OAuthAppClient.tenant_id == tenant_id,
                OAuthAppClient.idp_id == client_data.idp_id,
                OAuthAppClient.client_id == client_data.client_id
            ).first()

            if existing:
                # Log failed attempt
                audit.log(
                    action="oauth_app_client_create",
                    admin=admin,
                    admin_type=admin_type,
                    tenant_id=tenant_id,
                    success=False,
                    resource_type=TenantResourceTypes.OAUTH_CLIENTS,
                    details={
                        "error": "Duplicate client_id",
                        "client_id": client_data.client_id,
                        "client_name": client_data.name,
                        "idp_id": client_data.idp_id
                    },
                    ip_address=client_ip(request),
                    user_agent=request.headers.get('user-agent'),
                    request_method=request.method,
                    request_path=str(request.url.path),
                    db=db
                )

                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"OAuth client with client_id '{client_data.client_id}' already exists for this IdP in this tenant"
                )

            # Validate client type and secret
            if client_data.client_type == ClientType.CONFIDENTIAL.value and not client_data.client_secret:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Client secret is required for confidential clients"
                )

            if client_data.client_type == ClientType.PUBLIC_WITH_SECRET.value and not client_data.client_secret:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Client secret is required for public clients (PKCE + Secret)"
                )

            if client_data.client_type == ClientType.PUBLIC.value and client_data.client_secret:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Public (pure PKCE) clients cannot have a client secret"
                )

            # Generate oauth app ID, Result: 'oauthapp-01ARZ3NDEKTSV4RRFFQ69G5FAV'
            new_client_id = f"oauthapp-{ULID()}"

            # Encrypt client secret if provided
            encrypted_secret = None
            if client_data.client_secret:
                encrypted_secret = encryption_service.encrypt(client_data.client_secret)

            # Create OAuth app client
            new_client = OAuthAppClient(
                id=new_client_id,
                tenant_id=tenant_id,
                name=client_data.name,
                app_type=client_data.app_type,
                idp_id=client_data.idp_id,
                client_id=client_data.client_id,
                client_type=client_data.client_type,
                encrypted_client_secret=encrypted_secret,
                redirect_uri=client_data.redirect_uri,
                scopes=client_data.scopes,
                supports_pkce=client_data.supports_pkce,
                allowed_grant_types=client_data.allowed_grant_types,
                description=client_data.description,
                is_active=True
            )

            db.add(new_client)
            db.flush()
            db.refresh(new_client)

            # Log successful creation
            audit.log(
                action="oauth_app_client_create",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.OAUTH_CLIENTS,
                resource_id=new_client.id,
                details={
                    "client_name": new_client.name,
                    "app_type": new_client.app_type,
                    "client_type": new_client.client_type,
                    "idp_id": new_client.idp_id,
                    "supports_pkce": new_client.supports_pkce,
                    "has_secret": encrypted_secret is not None
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            logger.info("OAuth app client created", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "client_id": new_client.id,
                "client_name": new_client.name
            })

            return OAuthAppClientResponse(**new_client.to_dict())

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to create OAuth app client", context={
                "admin_id": admin_id if 'admin' in locals() else None,
                "admin_type": admin_type_value if 'admin_type' in locals() else None,
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create OAuth app client: {str(e)}"
            )

    @router.patch(
        "/{client_id}",
        response_model=OAuthAppClientResponse,
        summary="Update OAuth app client",
        description="Update OAuth app client (tenant isolation enforced)"
    )
    def update_oauth_app_client(
            client_id: str,
            updates: OAuthAppClientUpdate,
            request: Request,
            db: Session = Depends(get_db),
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.OAUTH_CLIENTS)),
            admin: AdminPrincipal = Depends(require_permission(Permission.OAUTH_CLIENT_UPDATE)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Update an existing OAuth app client with tenant verification"""
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_username = admin.username
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            logger.debug("OAuth app client update requested", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "client_id": client_id
            })

            # Query with tenant filter
            client = db.query(OAuthAppClient).filter(
                OAuthAppClient.id == client_id,
                OAuthAppClient.tenant_id == tenant_id
            ).first()

            if not client:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"OAuth app client '{client_id}' not found"
                )

            # Track changes for audit
            changes = {}

            # Check if client is being used by any server configs (scoped to tenant)
            configs_using_client = db.query(ServerConfig).filter(
                ServerConfig.tenant_id == tenant_id,
                ServerConfig.oauth_client_id == client_id
            ).count()

            # Update fields
            if updates.name is not None and updates.name != client.name:
                changes['name'] = {'old': client.name, 'new': updates.name}
                client.name = updates.name

            if updates.client_id is not None and updates.client_id != client.client_id:
                # Check for duplicate within tenant
                existing = db.query(OAuthAppClient).filter(
                    OAuthAppClient.tenant_id == tenant_id,
                    OAuthAppClient.idp_id == client.idp_id,
                    OAuthAppClient.client_id == updates.client_id,
                    OAuthAppClient.id != client_id
                ).first()

                if existing:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"OAuth client with client_id '{updates.client_id}' already exists for this IdP in this tenant"
                    )

                changes['client_id'] = {'old': client.client_id, 'new': updates.client_id}
                client.client_id = updates.client_id

            if updates.client_secret is not None:
                # Logic: Allow secret if it's explicitly Confidential OR
                # if it's a Public client that requires a secret (like HubSpot/MCP)
                is_confidential = client.client_type == ClientType.CONFIDENTIAL.value
                is_public_with_secret = client.client_type == ClientType.PUBLIC_WITH_SECRET.value

                if is_confidential or is_public_with_secret:
                    changes['client_secret'] = {'old': '***', 'new': '***'}
                    client.encrypted_client_secret = encryption_service.encrypt(updates.client_secret)
                else:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Client type does not support a client secret. Change client type first."
                    )

            if updates.redirect_uri is not None and updates.redirect_uri != client.redirect_uri:
                changes['redirect_uri'] = {'old': client.redirect_uri, 'new': updates.redirect_uri}
                client.redirect_uri = updates.redirect_uri

            if updates.scopes is not None and updates.scopes != client.scopes:
                changes['scopes'] = {'old': client.scopes, 'new': updates.scopes}
                client.scopes = updates.scopes

            if updates.supports_pkce is not None and updates.supports_pkce != client.supports_pkce:
                changes['supports_pkce'] = {'old': client.supports_pkce, 'new': updates.supports_pkce}
                client.supports_pkce = updates.supports_pkce

            if updates.allowed_grant_types is not None and updates.allowed_grant_types != client.allowed_grant_types:
                changes['allowed_grant_types'] = {'old': client.allowed_grant_types, 'new': updates.allowed_grant_types}
                client.allowed_grant_types = updates.allowed_grant_types

            if updates.description is not None and updates.description != client.description:
                changes['description'] = {'old': client.description, 'new': updates.description}
                client.description = updates.description

            if updates.is_active is not None and updates.is_active != client.is_active:
                if not updates.is_active and configs_using_client > 0:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Cannot deactivate OAuth client used by {configs_using_client} server config(s)"
                    )
                changes['is_active'] = {'old': client.is_active, 'new': updates.is_active}
                client.is_active = updates.is_active

            # If no changes, return current state
            if not changes:
                logger.debug("No changes to OAuth app client", context={
                    "client_id": client_id,
                    "tenant_id": tenant_id
                })
                return OAuthAppClientResponse(**client.to_dict())

            client.updated_at = datetime.now(timezone.utc)

            db.flush()
            db.refresh(client)

            # Log successful update
            audit.log(
                action="oauth_app_client_update",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.OAUTH_CLIENTS,
                resource_id=client.id,
                details={
                    "client_name": client.name,
                    "changes": changes
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            logger.info("OAuth app client updated", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "client_id": client_id,
                "changes": list(changes.keys())
            })

            return OAuthAppClientResponse(**client.to_dict())

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to update OAuth app client", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "client_id": client_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to update OAuth app client: {str(e)}"
            )

    @router.delete(
        "/{client_id}",
        status_code=status.HTTP_200_OK,
        summary="Delete OAuth app client",
        description=(
            "Delete OAuth app client (tenant isolation enforced). Refused with 409 while a "
            "server configuration uses it, unless `force=true`, which deletes those too."
        ),
        responses={
            409: {
                **IN_USE_RESPONSE,
                "description": "A server configuration uses the client; `detail.used_by.server_configs` lists them.",
            }
        },
    )
    def delete_oauth_app_client(
            client_id: str,
            request: Request,
            force: bool = Query(False, description="Force deletion even if in use"),
            db: Session = Depends(get_db),
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.OAUTH_CLIENTS)),
            admin: AdminPrincipal = Depends(require_permission(Permission.OAUTH_CLIENT_DELETE)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """
        Delete an OAuth app client with tenant verification.

        Warning: This will affect all server configs using this client.
        """
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_username = admin.username
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            logger.debug("OAuth app client deletion requested", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "client_id": client_id,
                "force": force
            })

            # Query with tenant filter
            client = db.query(OAuthAppClient).filter(
                OAuthAppClient.id == client_id,
                OAuthAppClient.tenant_id == tenant_id
            ).first()

            if not client:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"OAuth app client '{client_id}' not found"
                )

            # Check usage in server configs (scoped to tenant)
            configs_using_client = db.query(ServerConfig).filter(
                ServerConfig.tenant_id == tenant_id,
                ServerConfig.oauth_client_id == client_id
            ).all()

            affected_configs = [config.name for config in configs_using_client]

            if affected_configs and not force:
                raise in_use(
                    f"Cannot delete OAuth client used by {len(affected_configs)} server config(s). "
                    "Use force=true to delete it and those server configs.",
                    server_configs=configs_using_client,
                )

            client_name = client.name

            # Delete the client (will cascade to server_configs)
            db.delete(client)
            db.flush()

            # Log successful deletion
            audit.log(
                action="oauth_app_client_delete",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.OAUTH_CLIENTS,
                resource_id=client_id,
                details={
                    "client_name": client_name,
                    "affected_configs_count": len(affected_configs),
                    "affected_configs": affected_configs,
                    "forced": force
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            logger.info("OAuth app client deleted", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "client_id": client_id,
                "client_name": client_name,
                "affected_configs_count": len(affected_configs)
            })

            return {
                "message": f"OAuth app client '{client_name}' deleted successfully",
                "client_id": client_id,
                "affected_configs": affected_configs,
                "deleted_by": admin_username,
                "timestamp": datetime.now(timezone.utc).isoformat() + "Z"
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to delete OAuth app client", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "client_id": client_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete OAuth app client: {str(e)}"
            )

    @router.post(
        "/auto-register",
        response_model=OAuthAppClientResponse,
        status_code=status.HTTP_201_CREATED,
        summary="Auto-register OAuth client via RFC 7591 Dynamic Client Registration",
        description="Dynamically register an OAuth client with an IdP that supports RFC 7591"
    )
    def auto_register_oauth_client(
            body: AutoRegisterOAuthAppClientRequest,
            request: Request,
            db: Session = Depends(get_db),
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.OAUTH_CLIENTS)),
            admin: AdminPrincipal = Depends(require_permission(Permission.OAUTH_CLIENT_CREATE)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """
        Auto-register an OAuth client via RFC 7591 Dynamic Client Registration.

        The IdP must have a registration_endpoint configured (typically discovered via OIDC).
        The endpoint will:
        1. Validate the IdP exists and supports DCR
        2. Call the IdP's registration endpoint with client metadata
        3. Store the returned credentials as a new OAuthAppClient
        """
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_username = admin.username
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        if dcr_service is None:
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail="Dynamic Client Registration service is not configured"
            )

        try:
            logger.debug("Auto-register OAuth client requested", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "idp_id": body.idp_id,
                "client_name": body.client_name
            })

            # Validate IdP exists and belongs to same tenant
            idp = db.query(IdPServer).filter(
                IdPServer.id == body.idp_id,
                IdPServer.tenant_id == tenant_id
            ).first()

            if not idp:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Identity Provider '{body.idp_id}' not found in this tenant"
                )

            _require_oauth_provider(idp)

            if not idp.registration_endpoint:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Identity Provider '{idp.name}' does not support Dynamic Client Registration (no registration_endpoint)"
                )

            # Build scope string from list
            scope_str = " ".join(body.scopes) if body.scopes else None

            # Call RFC 7591 DCR
            dcr_result = block_on(dcr_service.register_client(
                idp=idp,
                redirect_uris=[body.redirect_uri],
                client_name=body.client_name,
                token_endpoint_auth_method=body.token_endpoint_auth_method,
                grant_types=body.grant_types,
                response_types=body.response_types,
                scope=scope_str,
            ))

            # Determine client type based on whether a secret was returned
            if dcr_result.client_secret:
                client_type = ClientType.CONFIDENTIAL.value
                encrypted_secret = encryption_service.encrypt(dcr_result.client_secret)
            else:
                client_type = ClientType.PUBLIC.value
                encrypted_secret = None

            # Generate oauth app ID
            new_client_id = f"oauthapp-{ULID()}"

            # Create OAuthAppClient record
            new_client = OAuthAppClient(
                id=new_client_id,
                tenant_id=tenant_id,
                name=body.client_name,
                app_type=body.app_type,
                idp_id=body.idp_id,
                client_id=dcr_result.client_id,
                client_type=client_type,
                encrypted_client_secret=encrypted_secret,
                redirect_uri=body.redirect_uri,
                scopes=body.scopes,
                supports_pkce=True,
                allowed_grant_types=body.grant_types,
                description=body.description,
                is_active=True,
                auto_registered=True,
                registration_client_uri=dcr_result.registration_client_uri,
            )

            db.add(new_client)
            db.flush()
            db.refresh(new_client)

            # Log successful creation
            audit.log(
                action="oauth_app_client_auto_register",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.OAUTH_CLIENTS,
                resource_id=new_client.id,
                details={
                    "client_name": new_client.name,
                    "app_type": new_client.app_type,
                    "client_type": client_type,
                    "idp_id": body.idp_id,
                    "idp_name": idp.name,
                    "auto_registered": True,
                    "has_secret": encrypted_secret is not None,
                    "has_registration_uri": dcr_result.registration_client_uri is not None,
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            logger.info("OAuth app client auto-registered via DCR", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "client_id": new_client.id,
                "client_name": new_client.name,
                "idp_id": body.idp_id,
                "dcr_client_id": dcr_result.client_id
            })

            return OAuthAppClientResponse(**new_client.to_dict())

        except HTTPException:
            raise
        except ValueError as e:
            logger.warning("DCR validation error", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "idp_id": body.idp_id,
                "error": str(e)
            })
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e)
            )
        except Exception as e:
            logger.error("Failed to auto-register OAuth app client", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "idp_id": body.idp_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to auto-register OAuth app client: {str(e)}"
            )

    return router