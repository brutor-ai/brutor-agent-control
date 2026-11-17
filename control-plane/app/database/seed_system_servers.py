# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
System MCP Server Seeding

Creates system-managed virtual MCP servers during gateway initialization.
These servers exist in the database (so they appear in tools/list, admin UI,
user portal) but are handled internally rather than being proxied externally.

Called from seed_initial_data() in app/database/__init__.py

Usage:
    from app.database.seed_system_servers import seed_system_mcp_servers

    # Inside seed_initial_data(), after Default Resource Group creation:
    seed_system_mcp_servers(db, default_tenant.id, default_group_id, logger)
"""

import base64
import hashlib
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.core.logging_system import BrutorLogger
from app.database.models.mcp_server import MCPServer
from app.database.models.server_config import ServerConfig
from app.constants.system_server import (
    AgentSkillServer,
    SearchServer,
    system_server_wire_identity,
)
from app.database.seed_icons import ICON_AGENT_SKILLS, ICON_SEARCH


def _stamp_wire_identity(db: Session, server: MCPServer) -> None:
    """Idempotently stamp protocol_version/dialect; re-runs pick up flag flips."""
    protocol_version, dialect = system_server_wire_identity()
    if server.protocol_version != protocol_version or server.dialect != dialect:
        server.protocol_version = protocol_version
        server.dialect = dialect
        server.updated_at = datetime.now(timezone.utc)
        db.flush()


def seed_system_mcp_servers(
    db: Session,
    tenant_id: str,
    default_group_id: str,
    logger: BrutorLogger,
) -> None:
    """
    Seed all system MCP servers.

    Idempotent — safe to call on every startup. Checks by stable ID.

    Args:
        db: Database session (caller manages commit)
        tenant_id: Default tenant ID
        default_group_id: Default Resource Group ID for initial binding
        logger: Logger instance
    """
    _seed_agent_skill_server(db, tenant_id, default_group_id, logger)
    _seed_search_server(db, tenant_id, default_group_id, logger)


def _seed_agent_skill_server(
    db: Session,
    tenant_id: str,
    default_group_id: str,
    logger: BrutorLogger,
) -> None:
    """
    Seed the Agent Skills Management system MCP server.

    Creates:
        1. MCPServer record (base_url = http://system/agent-skill-server)
        2. ServerConfig record (no OAuth, bound to Default group)
        3. Association to Default Resource Group via resource_group_server_configs

    The capabilities field starts empty and is populated by
    SystemServerCapabilitySync when skills are published.
    """
    # ------------------------------------------------------------------
    # 1. MCPServer
    # ------------------------------------------------------------------
    # Tenant-scoped stable IDs to avoid cross-tenant collisions
    server_id = f"{AgentSkillServer.SERVER_ID}-{tenant_id}"
    config_id = f"{AgentSkillServer.CONFIG_ID}-{tenant_id}"

    existing_server = db.query(MCPServer).filter(
        MCPServer.id == server_id
    ).first()

    if existing_server:
        server = existing_server
        # Keep the server's advertised tools in sync with the
        # four `skills__*` verbs (idempotent refresh so an existing deployment
        # picks them up on the next seed / startup, e.g. after an upgrade that
        # migrated skills off per-skill tools).
        caps = dict(server.capabilities or {})
        if caps.get("tools") != AgentSkillServer.VERB_TOOLS:
            caps["tools"] = list(AgentSkillServer.VERB_TOOLS)
            caps.setdefault("resources", [])
            caps.setdefault("resource_templates", [])
            caps.setdefault("prompts", [])
            server.capabilities = caps
            server.updated_at = datetime.now(timezone.utc)
            db.flush()
    else:
        server = MCPServer(
            id=server_id,
            tenant_id=tenant_id,
            name=AgentSkillServer.NAME,
            description=AgentSkillServer.DESCRIPTION,
            base_url=AgentSkillServer.BASE_URL,
            mcp_endpoint_path=AgentSkillServer.MCP_ENDPOINT_PATH,
            auth_type=AgentSkillServer.AUTH_TYPE,
            timeout=30.0,
            enabled=True,

            # The server's tools are the four static
            # `skills__*` verbs (skills are NOT tools; the skill catalog lives in
            # the initialize `instructions`). Seeded so operators can govern the
            # verbs (argument policies / per-tool rate limits) from the admin UI.
            capabilities={
                "tools": list(AgentSkillServer.VERB_TOOLS),
                "resources": [],
                "resource_templates": [],
                "prompts": [],
            },
            discovered_at=datetime.now(timezone.utc),
            server_info=AgentSkillServer.SERVER_INFO,

            # Health — always healthy (it's us)
            health_status="healthy",
            last_health_check=datetime.now(timezone.utc),
            last_health_success=datetime.now(timezone.utc),
            consecutive_health_failures=0,

            # Embedded PNG icon
            icon_data=base64.b64decode(ICON_AGENT_SKILLS),
            icon_mime_type="image/png",
            icon_size=len(base64.b64decode(ICON_AGENT_SKILLS)),
            icon_hash=hashlib.sha256(base64.b64decode(ICON_AGENT_SKILLS)).hexdigest(),
            icon_uploaded_at=datetime.now(timezone.utc),

            # No IdP (system server, no OAuth)
            idp_id=None,

            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

        db.add(server)
        db.flush()

        logger.info("Created system Agent Skills MCP server", context={
            "server_id": server.id,
            "tenant_id": tenant_id,
            "base_url": server.base_url,
        })

    # Wire identity — system servers are dual-era; stamp the
    # `dual` badge (idempotent).
    _stamp_wire_identity(db, server)

    # ------------------------------------------------------------------
    # 2. ServerConfig (binding to Default group)
    # ------------------------------------------------------------------
    existing_config = db.query(ServerConfig).filter(
        ServerConfig.id == config_id
    ).first()

    if existing_config:
        config = existing_config
    else:
        config = ServerConfig(
            id=config_id,
            tenant_id=tenant_id,
            mcp_server_id=server.id,
            oauth_client_id=None,  # No OAuth for system server
            name=AgentSkillServer.NAME,
            description=AgentSkillServer.DESCRIPTION,
            priority=10,  # High priority (lower = higher)
            is_default=True,
            enabled=True,
            display_name=AgentSkillServer.NAME,
            display_description=AgentSkillServer.DESCRIPTION,
            icon_url=None,
            usage_count=0,
            created_by="system",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

        db.add(config)
        db.flush()

        logger.info("Created system Agent Skills server config", context={
            "config_id": config.id,
            "server_id": server.id,
            "tenant_id": tenant_id,
        })

    # ------------------------------------------------------------------
    # 3. Bind to Default Resource Group
    # ------------------------------------------------------------------
    # Check if already bound via the association table
    from app.database.models.resource_group import ResourceGroup

    default_group = db.query(ResourceGroup).filter(
        ResourceGroup.id == default_group_id
    ).first()

    if default_group and config not in default_group.server_configs:
        default_group.server_configs.append(config)
        db.flush()

        logger.info("Bound Agent Skills server config to Default group", context={
            "config_id": config.id,
            "group_id": default_group_id,
        })
    elif default_group:
        pass  # Already bound


def _seed_search_server(
    db: Session,
    tenant_id: str,
    default_group_id: str,
    logger: BrutorLogger,
) -> None:
    """
    Seed the Web Search system MCP server.

    Creates:
        1. MCPServer record (base_url = http://system/search-server)
        2. ServerConfig record (no OAuth, bound to Default group)
        3. Association to Default Resource Group

    The tools are defined statically in the Rust search_server module.
    """
    # Tenant-scoped stable IDs to avoid cross-tenant collisions
    server_id = f"{SearchServer.SERVER_ID}-{tenant_id}"
    config_id = f"{SearchServer.CONFIG_ID}-{tenant_id}"

    # 1. MCPServer
    existing_server = db.query(MCPServer).filter(
        MCPServer.id == server_id
    ).first()

    if existing_server:
        server = existing_server
    else:
        server = MCPServer(
            id=server_id,
            tenant_id=tenant_id,
            name=SearchServer.NAME,
            description=SearchServer.DESCRIPTION,
            base_url=SearchServer.BASE_URL,
            mcp_endpoint_path=SearchServer.MCP_ENDPOINT_PATH,
            auth_type=SearchServer.AUTH_TYPE,
            timeout=30.0,
            # Seed DISABLED. The Web Search MCP server depends on an
            # upstream provider API key (Brave, Tavily, etc.) that the
            # operator has to set on the provider config row before
            # the server can usefully serve tool calls. With a missing
            # key, the LLM happily picks `search__web` from the tools
            # list, the portal awaits a tool-call response that never
            # comes (or comes back as a slow upstream error), and the
            # whole turn hangs. Operators should:
            #   1. add their search-provider API key under Admin →
            #      Search Providers,
            #   2. flip this server to enabled in Admin → MCP Servers,
            #   3. (optional) bind to a different group than Default.
            # Disabled-by-default makes the misconfigured-then-hang
            # path impossible: the server isn't even visible to the
            # portal until step 2.
            enabled=False,
            capabilities={
                "tools": [
                    {"name": SearchServer.TOOL_WEB, "description": "Search the web",
                     "annotations": SearchServer.ANNOTATIONS},
                    {"name": SearchServer.TOOL_NEWS, "description": "Search news articles",
                     "annotations": SearchServer.ANNOTATIONS},
                    {"name": SearchServer.TOOL_EXTRACT, "description": "Extract content from URL",
                     "annotations": SearchServer.ANNOTATIONS},
                    {"name": SearchServer.TOOL_IMAGES, "description": "Search for images",
                     "annotations": SearchServer.ANNOTATIONS},
                ],
                "resources": [],
                "resource_templates": [],
                "prompts": [],
            },
            discovered_at=datetime.now(timezone.utc),
            server_info=SearchServer.SERVER_INFO,
            health_status="unknown",
            last_health_check=None,
            last_health_success=None,
            consecutive_health_failures=0,
            icon_data=base64.b64decode(ICON_SEARCH),
            icon_mime_type="image/png",
            icon_size=len(base64.b64decode(ICON_SEARCH)),
            icon_hash=hashlib.sha256(base64.b64decode(ICON_SEARCH)).hexdigest(),
            icon_uploaded_at=datetime.now(timezone.utc),
            idp_id=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.add(server)
        db.flush()
        logger.info("Created system Web Search MCP server", context={
            "server_id": server.id,
            "tenant_id": tenant_id,
            "base_url": server.base_url,
        })

    # Wire identity — see _system_server_wire_identity.
    _stamp_wire_identity(db, server)

    # 2. ServerConfig
    existing_config = db.query(ServerConfig).filter(
        ServerConfig.id == config_id
    ).first()

    if existing_config:
        config = existing_config
    else:
        config = ServerConfig(
            id=config_id,
            tenant_id=tenant_id,
            mcp_server_id=server.id,
            oauth_client_id=None,
            name=SearchServer.NAME,
            description=SearchServer.DESCRIPTION,
            priority=10,
            is_default=True,
            # Matches `enabled=False` on the MCPServer above. Both
            # rows must be flipped to True (in either order) once
            # the operator has wired up a search-provider API key.
            enabled=False,
            display_name=SearchServer.NAME,
            display_description=SearchServer.DESCRIPTION,
            icon_url=None,
            usage_count=0,
            created_by="system",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.add(config)
        db.flush()
        logger.info("Created system Web Search server config", context={
            "config_id": config.id,
            "server_id": server.id,
            "tenant_id": tenant_id,
        })

    # 3. Bind to Default Resource Group
    from app.database.models.resource_group import ResourceGroup

    default_group = db.query(ResourceGroup).filter(
        ResourceGroup.id == default_group_id
    ).first()

    if default_group and config not in default_group.server_configs:
        default_group.server_configs.append(config)
        db.flush()
        logger.info("Bound Web Search server config to Default group", context={
            "config_id": config.id,
            "group_id": default_group_id,
        })
