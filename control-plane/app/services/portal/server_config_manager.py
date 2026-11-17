# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Server Config Manager - Multi-Tenant Aware
Manages server configurations that link MCP servers to OAuth app clients
End users <-> Groups <-> Server Configs <-> OAuth App Clients <-> IdPs
"""

from ulid import ULID
from typing import Optional, List, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import and_, func
from sqlalchemy.orm import joinedload
from datetime import datetime, timezone, timedelta

from app.core.logging_system import BrutorLogger
from app.database.models.server_config import ServerConfig
from app.database.models.mcp_server import MCPServer
from app.database.models.oauth_app_client import OAuthAppClient
from app.database.models.resource_group import ResourceGroup, resource_group_server_configs


class ServerConfigManager:
    """
    Manages server configurations for the user portal with multi-tenant support

    Handles:
    - Creating/updating/deleting server configs
    - Linking MCP servers to OAuth app clients
    - Querying configs for user portal display
    - Managing default configs and priorities
    - Multi-tenant isolation
    """

    def __init__(self, logger: Optional[BrutorLogger] = None):
        self.logger = logger or BrutorLogger("server-config-manager")

    async def create_config(
            self,
            config: Any,  # ServerConfigCreate pydantic model
            db: Session,
            tenant_id: str
    ) -> Dict[str, Any]:
        """
        Create a new server configuration

        Args:
            config: ServerConfigCreate object with configuration details
            db: Database session
            tenant_id: Tenant ID for multi-tenant isolation

        Returns:
            Created server config as dictionary
        """
        if not db:
            raise ValueError("Database session is required")

        if not tenant_id:
            raise ValueError("tenant_id is required for multi-tenant isolation")

        # Extract values from pydantic model or dict
        if hasattr(config, 'dict'):
            config_dict = config.dict()
        else:
            config_dict = config if isinstance(config, dict) else {}

        mcp_server_id = config_dict.get('mcp_server_id')
        virtual_server_id = config_dict.get('virtual_server_id')
        oauth_client_id = config_dict.get('oauth_client_id')
        name = config_dict.get('name')
        admin_id = config_dict.get('admin_id') or config_dict.get('created_by')
        description = config_dict.get('description')
        priority = config_dict.get('priority', 100)
        is_default = config_dict.get('is_default', False)
        enabled = config_dict.get('enabled', True)
        display_name = config_dict.get('display_name')
        display_description = config_dict.get('display_description')
        icon_url = config_dict.get('icon_url')
        group_ids = config_dict.get('group_ids')

        # XOR backend check. Pydantic already enforces
        # this at the request layer, but the manager is also called
        # from seed paths and tests that bypass Pydantic, so we
        # re-check here.
        if bool(mcp_server_id) == bool(virtual_server_id):
            raise ValueError(
                "Server config must reference exactly one backend: "
                "either mcp_server_id or virtual_server_id (not both)."
            )

        # VMCP-backed branch. Verify the VMCP exists,
        # belongs to the tenant, and that no OAuth client was
        # supplied (VMCPs delegate per-call auth to their
        # underlying MCPs).
        if virtual_server_id:
            from app.database.models.virtual_server import VirtualServer
            vserver = db.query(VirtualServer).filter(
                and_(
                    VirtualServer.id == virtual_server_id,
                    VirtualServer.tenant_id == tenant_id,
                )
            ).first()
            if not vserver:
                raise ValueError(
                    f"Virtual MCP server '{virtual_server_id}' not found in tenant '{tenant_id}'"
                )
            if oauth_client_id:
                raise ValueError(
                    "OAuth client is not applicable to VMCP-backed configs. "
                    "VMCPs delegate per-call auth to the underlying MCP "
                    "servers; configure OAuth on each underlying MCP "
                    "server's config instead."
                )

            # Uniqueness for VMCP path: (tenant, virtual_server_id, name)
            existing = db.query(ServerConfig).filter(
                and_(
                    ServerConfig.tenant_id == tenant_id,
                    ServerConfig.virtual_server_id == virtual_server_id,
                    ServerConfig.name == name,
                )
            ).first()
            if existing:
                raise ValueError(
                    f"Server configuration with name '{name}' already exists "
                    f"for VMCP '{vserver.name}'"
                )

            # Skip the MCP-server branch below — set sentinel so the
            # rest of the function knows we're on the VMCP path.
            mcp_server = None
            oauth_client = None

        else:
            # Pre-Sprint-4.1 MCP-backed path. Verify the real MCP
            # exists and belongs to the tenant.
            mcp_server = db.query(MCPServer).filter(
                and_(
                    MCPServer.id == mcp_server_id,
                    MCPServer.tenant_id == tenant_id
                )
            ).first()
            if not mcp_server:
                raise ValueError(f"MCP server '{mcp_server_id}' not found in tenant '{tenant_id}'")

            # Verify OAuth client (only if provided)
            oauth_client = None
            if oauth_client_id:
                # OAuth client specified - verify it exists and belongs to tenant
                oauth_client = db.query(OAuthAppClient).filter(
                    and_(
                        OAuthAppClient.id == oauth_client_id,
                        OAuthAppClient.tenant_id == tenant_id
                    )
                ).first()
                if not oauth_client:
                    raise ValueError(f"OAuth client '{oauth_client_id}' not found in tenant '{tenant_id}'")

                # Verify OAuth client and MCP server use the same IdP
                if mcp_server.idp_id and oauth_client.idp_id:
                    if mcp_server.idp_id != oauth_client.idp_id:
                        raise ValueError(
                            f"OAuth client IdP ({oauth_client.idp_id}) does not match "
                            f"MCP server IdP ({mcp_server.idp_id}). "
                            f"Please select an OAuth client that uses the same Identity Provider."
                        )
            else:
                # No OAuth client - verify server doesn't require OAuth
                if mcp_server.auth_type and mcp_server.auth_type.lower() in ['oauth', 'oauth2']:
                    raise ValueError(
                        f"MCP server '{mcp_server.name}' requires OAuth authentication. "
                        f"Please provide an OAuth client ID for this server."
                    )

                self.logger.debug("Creating non-OAuth server configuration", context={
                    "server_id": mcp_server_id,
                    "server_name": mcp_server.name,
                    "auth_type": mcp_server.auth_type,
                    "tenant_id": tenant_id
                })

                # Check uniqueness (different logic for OAuth vs non-OAuth)
                if oauth_client_id:
                    # For OAuth servers: unique per (tenant, server, oauth_client)
                    existing = db.query(ServerConfig).filter(
                        and_(
                            ServerConfig.tenant_id == tenant_id,
                            ServerConfig.mcp_server_id == mcp_server_id,
                            ServerConfig.oauth_client_id == oauth_client_id
                        )
                    ).first()

                    if existing:
                        raise ValueError(
                            f"Server configuration already exists for server '{mcp_server.name}' "
                            f"with OAuth client '{oauth_client.name}'"
                        )
                else:
                    # For non-OAuth servers: unique per (tenant, server, name)
                    existing = db.query(ServerConfig).filter(
                        and_(
                            ServerConfig.tenant_id == tenant_id,
                            ServerConfig.mcp_server_id == mcp_server_id,
                            ServerConfig.name == name
                        )
                    ).first()

                    if existing:
                        raise ValueError(
                            f"Server configuration with name '{name}' already exists "
                            f"for server '{mcp_server.name}'"
                        )

        # If this should be default, unset other defaults for this
        # backend+tenant. The "default" scope is per-backend
        # (per-MCP-server OR per-VMCP), not per-tenant.
        if is_default:
            default_query = db.query(ServerConfig).filter(
                and_(
                    ServerConfig.tenant_id == tenant_id,
                    ServerConfig.is_default == True
                )
            )
            if mcp_server_id:
                default_query = default_query.filter(
                    ServerConfig.mcp_server_id == mcp_server_id
                )
            else:
                default_query = default_query.filter(
                    ServerConfig.virtual_server_id == virtual_server_id
                )
            default_query.update({"is_default": False})

        # Generate ID
        config_id = f"srvconf-{ULID()}"

        # Create config
        server_config = ServerConfig(
            id=config_id,
            tenant_id=tenant_id,
            mcp_server_id=mcp_server_id,
            virtual_server_id=virtual_server_id,
            oauth_client_id=oauth_client_id,
            name=name,
            description=description,
            priority=priority,
            is_default=is_default,
            enabled=enabled,
            display_name=display_name,
            display_description=display_description,
            icon_url=icon_url,
            created_by=admin_id,
            created_at=datetime.now(timezone.utc)
        )

        db.add(server_config)

        # Handle group assignments
        if group_ids:
            # Validate that all groups exist and belong to tenant
            groups = db.query(ResourceGroup).filter(
                and_(
                    ResourceGroup.id.in_(group_ids),
                    ResourceGroup.tenant_id == tenant_id
                )
            ).all()

            if len(groups) != len(group_ids):
                found_ids = {g.id for g in groups}
                missing_ids = set(group_ids) - found_ids
                raise ValueError(f"Groups not found in tenant '{tenant_id}': {', '.join(missing_ids)}")

            # Associate groups with the config
            server_config.resource_groups = groups

            self.logger.debug("Assigned groups to server config", context={
                "config_id": config_id,
                "tenant_id": tenant_id,
                "group_count": len(groups),
                "group_ids": group_ids
            })

        db.flush()
        db.refresh(server_config)

        self.logger.info("Server config created", context={
            "config_id": config_id,
            "tenant_id": tenant_id,
            "mcp_server_id": mcp_server_id,
            "oauth_client_id": oauth_client_id,
            "admin_id": admin_id,
            "group_count": len(group_ids) if group_ids else 0,
            "is_global": not group_ids or len(group_ids) == 0
        })

        return server_config.to_dict(include_relationships=True)

    async def update_config(
            self,
            config_id: str,
            updates: Any,  # ServerConfigUpdate or dict
            db: Session,
            tenant_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Update a server configuration

        Args:
            config_id: Config ID to update
            updates: ServerConfigUpdate object or dictionary of fields to update
            db: Database session
            tenant_id: Optional tenant ID for filtering (recommended for security)

        Returns:
            Updated server config as dictionary
        """
        # Build query with mandatory tenant filter
        if not tenant_id:
            raise ValueError("tenant_id is required for security")
        query = db.query(ServerConfig).filter(
            ServerConfig.id == config_id,
            ServerConfig.tenant_id == tenant_id,
        )

        config = query.first()
        if not config:
            raise ValueError(f"Server config '{config_id}' not found")

        # Extract updates from pydantic model or dict
        if hasattr(updates, 'dict'):
            update_dict = updates.dict(exclude_unset=True)
        else:
            update_dict = updates if isinstance(updates, dict) else {}

        # Handle is_default update
        if update_dict.get("is_default") is True:
            # Unset other defaults for this server in the same tenant
            db.query(ServerConfig).filter(
                and_(
                    ServerConfig.tenant_id == config.tenant_id,
                    ServerConfig.mcp_server_id == config.mcp_server_id,
                    ServerConfig.id != config_id,
                    ServerConfig.is_default == True
                )
            ).update({"is_default": False})

        # Handle group_ids update separately
        if 'group_ids' in update_dict:
            group_ids = update_dict.pop('group_ids')

            if group_ids is None or len(group_ids) == 0:
                # Remove all group associations (make global)
                config.resource_groups = []
                self.logger.debug("Removed all groups from server config (now global)", context={
                    "config_id": config_id,
                    "tenant_id": config.tenant_id
                })
            else:
                # Validate and set new groups (must be in same tenant)
                groups = db.query(ResourceGroup).filter(
                    and_(
                        ResourceGroup.id.in_(group_ids),
                        ResourceGroup.tenant_id == config.tenant_id
                    )
                ).all()

                if len(groups) != len(group_ids):
                    found_ids = {g.id for g in groups}
                    missing_ids = set(group_ids) - found_ids
                    raise ValueError(f"Groups not found in tenant: {', '.join(missing_ids)}")

                config.resource_groups = groups
                self.logger.debug("Updated groups for server config", context={
                    "config_id": config_id,
                    "tenant_id": config.tenant_id,
                    "group_count": len(groups)
                })

        # Update other fields
        for field, value in update_dict.items():
            if hasattr(config, field):
                setattr(config, field, value)

        db.flush()
        db.refresh(config)

        self.logger.info("Server config updated", context={
            "config_id": config_id,
            "tenant_id": config.tenant_id,
            "updated_fields": list(update_dict.keys())
        })

        return config.to_dict(include_relationships=True)

    async def delete_config(
            self,
            config_id: str,
            db: Session,
            tenant_id: Optional[str] = None
    ) -> bool:
        """
        Delete a server configuration

        Args:
            config_id: Config ID to delete
            db: Database session
            tenant_id: Optional tenant ID for filtering (recommended for security)

        Returns:
            True if deleted, False if not found
        """
        # Build query with mandatory tenant filter
        if not tenant_id:
            raise ValueError("tenant_id is required for security")
        query = db.query(ServerConfig).filter(
            ServerConfig.id == config_id,
            ServerConfig.tenant_id == tenant_id,
        )

        config = query.first()
        if not config:
            return False

        tenant = config.tenant_id
        db.delete(config)
        db.flush()

        self.logger.info("Server config deleted", context={
            "config_id": config_id,
            "tenant_id": tenant
        })

        return True

    async def get_config(
            self,
            config_id: str,
            db: Session,
            tenant_id: Optional[str] = None,
            include_relationships: bool = False
    ) -> Optional[Dict[str, Any]]:
        """
        Get a specific server configuration

        Args:
            config_id: Config ID
            db: Database session
            tenant_id: Optional tenant ID for filtering
            include_relationships: Whether to include full related objects

        Returns:
            Server config dictionary or None if not found
        """
        # Build query with mandatory tenant filter
        if not tenant_id:
            raise ValueError("tenant_id is required for security")
        query = db.query(ServerConfig).filter(
            ServerConfig.id == config_id,
            ServerConfig.tenant_id == tenant_id,
        )

        if include_relationships:
            query = query.options(
                joinedload(ServerConfig.mcp_server),
                joinedload(ServerConfig.oauth_client),
                joinedload(ServerConfig.resource_groups)
            )

        config = query.first()
        if not config:
            return None

        return config.to_dict(include_relationships=include_relationships)

    async def list_configs(
            self,
            db: Session,
            tenant_id: Optional[str] = None,
            mcp_server_id: Optional[str] = None,
            oauth_client_id: Optional[str] = None,
            app_type: Optional[str] = None,
            enabled_only: bool = False,
            include_relationships: bool = False
    ) -> List[Dict[str, Any]]:
        """
        List server configurations with filters

        Args:
            db: Database session
            tenant_id: Filter by tenant ID (required for multi-tenant)
            mcp_server_id: Optional filter by MCP server
            oauth_client_id: Optional filter by OAuth client
            app_type: Optional filter by app type (via OAuth client)
            enabled_only: Only return enabled configs
            include_relationships: Whether to include full related objects

        Returns:
            List of server config dictionaries
        """
        query = db.query(ServerConfig)

        # Tenant filter (most important for multi-tenant)
        if tenant_id:
            query = query.filter(ServerConfig.tenant_id == tenant_id)

        # MCP Server filter
        if mcp_server_id:
            query = query.filter(ServerConfig.mcp_server_id == mcp_server_id)

        # OAuth Client filter
        if oauth_client_id:
            query = query.filter(ServerConfig.oauth_client_id == oauth_client_id)

        # App type filter (requires join with OAuth client)
        if app_type:
            query = query.join(OAuthAppClient).filter(OAuthAppClient.app_type == app_type)

        # Enabled filter
        if enabled_only:
            query = query.filter(ServerConfig.enabled == True)

        # Load relationships if requested
        if include_relationships:
            query = query.options(
                joinedload(ServerConfig.mcp_server),
                joinedload(ServerConfig.oauth_client),
                joinedload(ServerConfig.resource_groups)
            )

        # Order by priority and name
        query = query.order_by(ServerConfig.priority, ServerConfig.name)

        configs = query.all()

        return [config.to_dict(include_relationships=include_relationships) for config in configs]

    async def get_configs_by_server(
            self,
            mcp_server_id: str,
            db: Session,
            tenant_id: Optional[str] = None,
            enabled_only: bool = False
    ) -> List[Dict[str, Any]]:
        """
        Get all configurations for a specific MCP server

        Args:
            mcp_server_id: MCP server ID
            db: Database session
            tenant_id: Optional tenant ID for filtering
            enabled_only: Only return enabled configs

        Returns:
            List of server configs
        """
        return await self.list_configs(
            db=db,
            tenant_id=tenant_id,
            mcp_server_id=mcp_server_id,
            enabled_only=enabled_only,
            include_relationships=True
        )

    async def get_configs_by_oauth_client(
            self,
            oauth_client_id: str,
            db: Session,
            tenant_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Get all configurations using a specific OAuth client

        Args:
            oauth_client_id: OAuth client ID
            db: Database session
            tenant_id: Optional tenant ID for filtering

        Returns:
            List of server configs
        """
        return await self.list_configs(
            db=db,
            tenant_id=tenant_id,
            oauth_client_id=oauth_client_id,
            include_relationships=True
        )

    async def get_configs_by_group(
            self,
            group_id: str,
            db: Session,
            tenant_id: Optional[str] = None,
            enabled_only: bool = False,
            include_inherited: bool = True
    ) -> List[Dict[str, Any]]:
        """
        Get all configurations assigned to a specific user group

        HIERARCHICAL INHERITANCE SUPPORT:
        - If include_inherited=True: Returns configs from this group AND parent groups
        - If include_inherited=False: Returns only direct assignments

        This matches the pattern from ServerConfigAccessService.

        Args:
            group_id: User group ID
            db: Database session
            tenant_id: Optional tenant ID for filtering
            enabled_only: Only return enabled configs
            include_inherited: Include configs inherited from parent groups (default: True)

        Returns:
            List of server configs with relationships
        """
        # Get the group with tenant isolation
        group_query = db.query(ResourceGroup).filter(ResourceGroup.id == group_id)
        if tenant_id:
            group_query = group_query.filter(ResourceGroup.tenant_id == tenant_id)
        group = group_query.first()
        if not group:
            self.logger.warning("Group not found", context={"group_id": group_id})
            return []

        # Verify tenant if provided
        if tenant_id and group.tenant_id != tenant_id:
            self.logger.warning("Group tenant mismatch", context={
                "group_id": group_id,
                "requested_tenant": tenant_id,
                "group_tenant": group.tenant_id
            })
            return []

        # Get configs based on inheritance setting
        if include_inherited:
            # Use hierarchical inheritance (includes parent groups)
            configs = group.get_effective_server_configs()
            self.logger.debug("Retrieved configs with inheritance", context={
                "group_id": group_id,
                "config_count": len(configs),
                "include_inherited": True
            })
        else:
            # Direct assignments only
            configs = list(group.server_configs.all())
            self.logger.debug("Retrieved direct configs only", context={
                "group_id": group_id,
                "config_count": len(configs),
                "include_inherited": False
            })

        # Apply filters
        if enabled_only:
            configs = [c for c in configs if c.enabled]

        # Sort by priority and name
        configs.sort(key=lambda c: (c.priority or 999, c.name or ''))

        # Eager load relationships for to_dict()
        # This is needed because configs from get_effective_server_configs()
        # might not have relationships loaded
        config_ids = [c.id for c in configs]
        if config_ids:
            configs = db.query(ServerConfig).filter(
                ServerConfig.id.in_(config_ids)
            ).options(
                joinedload(ServerConfig.mcp_server),
                joinedload(ServerConfig.oauth_client),
                joinedload(ServerConfig.resource_groups)
            ).all()

            # Re-sort after query (query might change order)
            configs.sort(key=lambda c: (c.priority or 999, c.name or ''))

        return [config.to_dict(include_relationships=True) for config in configs]

    async def get_statistics(
            self,
            db: Session,
            tenant_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get server config statistics

        Args:
            db: Database session
            tenant_id: Optional tenant ID for filtering

        Returns:
            Statistics dictionary
        """
        # Build base queries with optional tenant filter
        base_query = db.query(ServerConfig)
        if tenant_id:
            base_query = base_query.filter(ServerConfig.tenant_id == tenant_id)

        total_configs = base_query.count()
        enabled_configs = base_query.filter(ServerConfig.enabled == True).count()
        default_configs = base_query.filter(ServerConfig.is_default == True).count()

        # Get usage stats
        total_usage = base_query.with_entities(func.sum(ServerConfig.usage_count)).scalar() or 0

        # Count by activity
        thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)

        active_configs = base_query.filter(
            ServerConfig.last_used >= thirty_days_ago
        ).count()

        never_used = base_query.filter(
            ServerConfig.last_used.is_(None)
        ).count()

        # Count configs with groups vs global configs
        configs_with_groups_query = base_query.join(
            resource_group_server_configs,
            ServerConfig.id == resource_group_server_configs.c.server_config_id
        ).distinct()

        configs_with_groups = configs_with_groups_query.count()
        global_configs = total_configs - configs_with_groups

        return {
            "total_configs": total_configs,
            "enabled_configs": enabled_configs,
            "default_configs": default_configs,
            "total_usage": total_usage,
            "active_last_30_days": active_configs,
            "never_used": never_used,
            "configs_with_groups": configs_with_groups,
            "global_configs": global_configs,
            "tenant_id": tenant_id
        }

