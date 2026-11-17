# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Virtual Server Manager
Service for managing virtual MCP servers that aggregate capabilities
"""

import copy
from typing import Optional, List, Dict, Any
from sqlalchemy.orm import Session

from app.database.models.virtual_server import VirtualServer
from app.database.models.mcp_server import MCPServer
from app.core.logging_system import BrutorLogger
from app.services.mcp.server.vmcp_normalizer import normalize_capabilities


class VirtualServerManager:
    """
    Virtual Server Management Service

    Handles CRUD operations for virtual servers that aggregate
    capabilities from multiple real MCP servers.
    """

    def __init__(self, logger: Optional[BrutorLogger] = None):
        self.logger = logger or BrutorLogger("virtual-server-manager")

    # =========================================================================
    # CRUD Operations
    # =========================================================================

    def get_virtual_server(
            self,
            server_id: str,
            db: Session,
            tenant_id: Optional[str] = None
    ) -> Optional[VirtualServer]:
        """Get virtual server by ID with optional tenant filter"""
        query = db.query(VirtualServer).filter_by(id=server_id)

        if tenant_id:
            query = query.filter_by(tenant_id=tenant_id)

        return query.first()

    def list_virtual_servers(
            self,
            owner: Optional[str] = None,
            enabled_only: bool = False,
            tenant_id: Optional[str] = None,
            db: Session = None
    ) -> List[VirtualServer]:
        """
        List virtual servers

        Args:
            owner: Filter by owner ID (admin user)
            enabled_only: Only return enabled servers
            tenant_id: Filter by tenant ID (required for multi-tenant)
            db: Database session

        Returns:
            List of virtual servers
        """
        query = db.query(VirtualServer)

        if tenant_id:
            query = query.filter_by(tenant_id=tenant_id)

        if owner:
            query = query.filter_by(owner=owner)

        if enabled_only:
            query = query.filter_by(enabled=True)

        return query.order_by(VirtualServer.name).all()

    def create_virtual_server(
            self,
            server_id: str,
            name: str,
            owner: str,
            tenant_id: str,
            description: Optional[str] = None,
            capabilities: Optional[Dict[str, Any]] = None,
            enabled: bool = True,
            db: Session = None
    ) -> VirtualServer:
        """
        Create new virtual server

        Args:
            server_id: Unique server ID
            name: Server name
            owner: Owner admin user ID
            tenant_id: Tenant ID (required for multi-tenant isolation)
            description: Optional description
            capabilities: Initial capabilities
            enabled: Whether server is enabled
            db: Database session

        Returns:
            Created virtual server
        """
        # Normalize the caller-supplied capabilities through the
        # vmcp_normalizer: snapshots upstream tool defs, computes
        # source_mapping + reverse_name_mapping, validates
        # exposed_name collisions. See vmcp_normalizer.py for the
        # input/output shape. Raises VMCPNormalizationError on bad
        # input — the API endpoint catches it and returns 400.
        if capabilities:
            normalized = normalize_capabilities(
                db=db, tenant_id=tenant_id,
                capabilities_input=capabilities,
            )
        else:
            normalized = {
                "tools": [],
                "resources": [],
                "prompts": [],
                "source_mapping": {},
                "reverse_name_mapping": {},
            }

        server = VirtualServer(
            id=server_id,
            name=name,
            owner=owner,
            tenant_id=tenant_id,
            description=description,
            capabilities=normalized,
            enabled=enabled
        )

        db.add(server)
        db.flush()
        db.refresh(server)

        self.logger.info("Virtual server created", context={
            "server_id": server_id,
            "name": name,
            "owner": owner,
            "tenant_id": tenant_id
        })

        return server

    def update_virtual_server(
            self,
            server_id: str,
            updates: Dict[str, Any],
            db: Session,
            tenant_id: Optional[str] = None
    ) -> Optional[VirtualServer]:
        """
        Update virtual server

        Args:
            server_id: Server ID
            updates: Fields to update
            db: Database session
            tenant_id: Tenant ID for access control

        Returns:
            Updated server or None if not found
        """
        server = self.get_virtual_server(server_id, db, tenant_id=tenant_id)
        if not server:
            return None

        # Update allowed fields
        if "name" in updates:
            server.name = updates["name"]
        if "description" in updates:
            server.description = updates["description"]
        if "enabled" in updates:
            server.enabled = updates["enabled"]
        if "capabilities" in updates:
            # Re-normalize on every capabilities update so
            # source_mapping / reverse_name_mapping stay in sync
            # with the operator's intent. See create_virtual_server
            # for rationale.
            server.capabilities = normalize_capabilities(
                db=db,
                tenant_id=server.tenant_id,
                capabilities_input=updates["capabilities"] or {},
            )
        if "owner" in updates:
            server.owner = updates["owner"]

        db.flush()
        db.refresh(server)

        self.logger.info("Virtual server updated", context={
            "server_id": server_id,
            "updated_fields": list(updates.keys())
        })

        return server

    def delete_virtual_server(
            self,
            server_id: str,
            db: Session,
            tenant_id: Optional[str] = None
    ) -> bool:
        """
        Delete virtual server

        Args:
            server_id: Server ID
            db: Database session
            tenant_id: Tenant ID for access control

        Returns:
            True if deleted, False if not found
        """
        server = self.get_virtual_server(server_id, db, tenant_id=tenant_id)
        if not server:
            return False

        db.delete(server)
        db.flush()

        self.logger.info("Virtual server deleted", context={
            "server_id": server_id,
            "tenant_id": tenant_id
        })

        return True

    # =========================================================================
    # Capability Management
    # =========================================================================

    def _renormalize(self, virtual_server: VirtualServer, db: Session) -> None:
        """Keep the mappings the core reads in step with an edited capability
        list. `capabilities` is plain JSON, so the ORM only sees an assignment;
        the callers edit a copy, which leaves the committed value intact for
        the comparison at flush."""
        virtual_server.capabilities = normalize_capabilities(
            db=db,
            tenant_id=virtual_server.tenant_id,
            capabilities_input=virtual_server.capabilities or {},
        )

    def add_tool_capability(
            self,
            virtual_server_id: str,
            source_server_id: str,
            tool_name: str,
            db: Session,
            tenant_id: Optional[str] = None
    ) -> bool:
        """
        Add tool capability from an MCP server

        Args:
            virtual_server_id: Virtual server ID
            source_server_id: Source MCP server ID
            tool_name: Name of the tool
            db: Database session
            tenant_id: Tenant ID for access control

        Returns:
            True if added successfully
        """
        virtual_server = self.get_virtual_server(virtual_server_id, db, tenant_id=tenant_id)
        if not virtual_server:
            return False

        # Verify source server exists (with tenant check)
        query = db.query(MCPServer).filter_by(id=source_server_id)
        if tenant_id:
            query = query.filter_by(tenant_id=tenant_id)
        source_server = query.first()

        if not source_server:
            self.logger.warning("Source server not found", context={
                "source_server_id": source_server_id,
                "tenant_id": tenant_id
            })
            return False

        virtual_server.capabilities = copy.deepcopy(virtual_server.capabilities or {})
        virtual_server.add_tool(source_server_id, tool_name)
        self._renormalize(virtual_server, db)
        db.flush()

        self.logger.info("Tool capability added to virtual server", context={
            "virtual_server_id": virtual_server_id,
            "source_server_id": source_server_id,
            "tool_name": tool_name,
            "tenant_id": tenant_id
        })

        return True

    def add_resource_capability(
            self,
            virtual_server_id: str,
            source_server_id: str,
            resource_name: str,
            db: Session,
            tenant_id: Optional[str] = None
    ) -> bool:
        """Add resource capability from an MCP server"""
        virtual_server = self.get_virtual_server(virtual_server_id, db, tenant_id=tenant_id)
        if not virtual_server:
            return False

        query = db.query(MCPServer).filter_by(id=source_server_id)
        if tenant_id:
            query = query.filter_by(tenant_id=tenant_id)
        source_server = query.first()
        if not source_server:
            return False

        virtual_server.capabilities = copy.deepcopy(virtual_server.capabilities or {})
        virtual_server.add_resource(source_server_id, resource_name)
        self._renormalize(virtual_server, db)
        db.flush()

        self.logger.info("Resource capability added to virtual server", context={
            "virtual_server_id": virtual_server_id,
            "source_server_id": source_server_id,
            "resource_name": resource_name,
            "tenant_id": tenant_id
        })

        return True

    def add_prompt_capability(
            self,
            virtual_server_id: str,
            source_server_id: str,
            prompt_name: str,
            db: Session,
            tenant_id: Optional[str] = None
    ) -> bool:
        """Add prompt capability from an MCP server"""
        virtual_server = self.get_virtual_server(virtual_server_id, db, tenant_id=tenant_id)
        if not virtual_server:
            return False

        query = db.query(MCPServer).filter_by(id=source_server_id)
        if tenant_id:
            query = query.filter_by(tenant_id=tenant_id)
        source_server = query.first()
        if not source_server:
            return False

        virtual_server.capabilities = copy.deepcopy(virtual_server.capabilities or {})
        virtual_server.add_prompt(source_server_id, prompt_name)
        self._renormalize(virtual_server, db)
        db.flush()

        self.logger.info("Prompt capability added to virtual server", context={
            "virtual_server_id": virtual_server_id,
            "source_server_id": source_server_id,
            "prompt_name": prompt_name,
            "tenant_id": tenant_id
        })

        return True

    def remove_server_capabilities(
            self,
            virtual_server_id: str,
            source_server_id: str,
            db: Session,
            tenant_id: Optional[str] = None
    ) -> int:
        """
        Remove all capabilities from a specific source server

        Args:
            virtual_server_id: Virtual server ID
            source_server_id: Source server to remove
            db: Database session
            tenant_id: Tenant ID for access control

        Returns:
            Number of capabilities removed
        """
        virtual_server = self.get_virtual_server(virtual_server_id, db, tenant_id=tenant_id)
        if not virtual_server:
            return 0

        removed_count = virtual_server.remove_server_references(source_server_id)
        db.flush()

        self.logger.info("Server capabilities removed from virtual server", context={
            "virtual_server_id": virtual_server_id,
            "source_server_id": source_server_id,
            "removed_count": removed_count
        })

        return removed_count

    # =========================================================================
    # Query Methods
    # =========================================================================

    def get_referenced_servers(
            self,
            virtual_server_id: str,
            db: Session,
            tenant_id: Optional[str] = None
    ) -> List[str]:
        """
        Get list of MCP servers referenced by virtual server

        Args:
            virtual_server_id: Virtual server ID
            db: Database session
            tenant_id: Tenant ID for access control

        Returns:
            List of MCP server IDs
        """
        virtual_server = self.get_virtual_server(virtual_server_id, db, tenant_id=tenant_id)
        if not virtual_server:
            return []

        return virtual_server.get_referenced_servers()

    def get_capability_counts(
            self,
            virtual_server_id: str,
            db: Session,
            tenant_id: Optional[str] = None
    ) -> Dict[str, int]:
        """
        Get counts of each capability type

        Args:
            virtual_server_id: Virtual server ID
            db: Database session
            tenant_id: Tenant ID for access control

        Returns:
            Dictionary with counts
        """
        virtual_server = self.get_virtual_server(virtual_server_id, db, tenant_id=tenant_id)
        if not virtual_server or not virtual_server.capabilities:
            return {
                "tools": 0,
                "resources": 0,
                "prompts": 0
            }

        return {
            "tools": len(virtual_server.capabilities.get("tools", [])),
            "resources": len(virtual_server.capabilities.get("resources", [])),
            "prompts": len(virtual_server.capabilities.get("prompts", []))
        }

    # =========================================================================
    # Authorization Helpers
    # =========================================================================

    # =========================================================================
    # Statistics
    # =========================================================================

    def get_statistics(self, db: Session, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """Get virtual server statistics with optional tenant filter"""
        query = db.query(VirtualServer)
        if tenant_id:
            query = query.filter_by(tenant_id=tenant_id)
        total = query.count()

        enabled_query = db.query(VirtualServer).filter_by(enabled=True)
        if tenant_id:
            enabled_query = enabled_query.filter_by(tenant_id=tenant_id)
        enabled = enabled_query.count()

        # Count by owner
        owner_query = db.query(VirtualServer.owner).distinct()
        if tenant_id:
            owner_query = owner_query.filter(VirtualServer.tenant_id == tenant_id)
        owners = owner_query.all()
        unique_owners = len(owners)

        return {
            "total_virtual_servers": total,
            "enabled_virtual_servers": enabled,
            "unique_owners": unique_owners
        }

    # =========================================================================
    # Response Building
    # =========================================================================

    def to_response(
            self,
            server: VirtualServer,
            include_capabilities: bool = True
    ) -> Dict[str, Any]:
        """
        Convert virtual server to API response format

        Args:
            server: Virtual server instance
            include_capabilities: Include full capability details

        Returns:
            Dictionary for API response
        """
        response = server.to_dict()

        if not include_capabilities:
            # Just include counts
            response["capabilities_summary"] = {
                "tools_count": len(server.capabilities.get("tools", [])),
                "resources_count": len(server.capabilities.get("resources", [])),
                "prompts_count": len(server.capabilities.get("prompts", []))
            }
            response.pop("capabilities", None)

        return response
