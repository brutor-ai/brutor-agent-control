# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
MCP Server Seeding (External OAuth-protected servers)

Creates default MCP Server configurations with OAuth identity providers
during tenant provisioning. Each MCP server requires four records:
    1. IdPServer   -- OAuth/OIDC identity provider endpoints
    2. MCPServer   -- MCP server connection details (with icon)
    3. OAuthAppClient -- OAuth client credentials (placeholders)
    4. ServerConfig   -- Binding that ties server + OAuth client + resource group

OAuth clients are created with placeholder client_id / client_secret values
and is_active=True. An administrator must update the client_id and
client_secret before they can be used.

Called from TenantProvisioner.provision_tenant() after agent skills seeding.

Usage:
    from app.database.seed_mcp_servers import seed_mcp_servers

    # Inside provision_tenant(), after seed_agent_skills():
    seed_mcp_servers(db, tenant_id, default_group_id, logger)
"""

import base64
import hashlib
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.logging_system import BrutorLogger
from app.database.models.idp_server import IdPServer
from app.database.models.mcp_server import MCPServer
from app.database.models.oauth_app_client import OAuthAppClient
from app.database.models.server_config import ServerConfig
from app.database.models.resource_group import ResourceGroup
from app.database.seed_icons import ICON_GITHUB, ICON_HUBSPOT, ICON_SLACK
from app.composition.hooks import portal_oauth_callback_url


def _get_oauth_redirect_uri(config_id: str, idp_id: str) -> str:
    """Build the OAuth redirect URI based on environment.

    The portal handles OAuth callbacks directly at {portal_url}/callback,
    then exchanges the auth code via the proxy. The redirect_uri must match
    what the portal sends to the IdP in the authorization request.
    """
    return portal_oauth_callback_url()


def _decode_icon(icon_b64: str) -> tuple:
    """Decode a base64 icon and return (bytes, mime_type, size, sha256_hash)."""
    icon_bytes = base64.b64decode(icon_b64)
    icon_hash = hashlib.sha256(icon_bytes).hexdigest()
    return icon_bytes, "image/png", len(icon_bytes), icon_hash


def _find_existing_oauth_client(
    db: Session,
    oauth_id: str,
    tenant_id: str,
    idp_id: str,
    client_id: str,
) -> "OAuthAppClient | None":
    """
    Find an existing OAuth client by stable ID or by the unique constraint
    (tenant_id, idp_id, client_id).

    This handles the case where an admin has already created the client
    via the Admin Console with a different (ULID-based) ID.
    """
    existing = db.query(OAuthAppClient).filter(OAuthAppClient.id == oauth_id).first()
    if existing:
        return existing
    return db.query(OAuthAppClient).filter(
        OAuthAppClient.tenant_id == tenant_id,
        OAuthAppClient.idp_id == idp_id,
        OAuthAppClient.client_id == client_id,
    ).first()


def seed_mcp_servers(
    db: Session,
    tenant_id: str,
    default_group_id: str,
    logger: BrutorLogger,
) -> None:
    """
    Seed all default external MCP Servers with OAuth.

    Idempotent -- safe to call on every startup. Checks by stable ID.

    Args:
        db: Database session (caller manages commit)
        tenant_id: Tenant ID
        default_group_id: Default Resource Group ID for initial binding
        logger: Logger instance
    """
    _seed_github(db, tenant_id, default_group_id, logger)
    _seed_hubspot(db, tenant_id, default_group_id, logger)
    _seed_slack(db, tenant_id, default_group_id, logger)


# ======================================================================
# GitHub MCP Server
# ======================================================================

def _seed_github(
    db: Session,
    tenant_id: str,
    default_group_id: str,
    logger: BrutorLogger,
) -> None:
    """
    Seed GitHub MCP Server with OAuth.

    Creates:
        1. IdPServer (GitHub OAuth endpoints)
        2. MCPServer (GitHub MCP connection with icon)
        3. OAuthAppClient (placeholder credentials, inactive)
        4. ServerConfig (binding to Default Resource Group)
    """
    now = datetime.now(timezone.utc)

    # Tenant-scoped stable IDs to avoid cross-tenant collisions
    _t = tenant_id
    idp_id = f"idp-system-github-{_t}"
    server_id = f"mcp-system-github-{_t}"
    oauth_id = f"oauth-system-github-{_t}"
    config_id = f"sc-system-github-{_t}"

    # ------------------------------------------------------------------
    # 1. IdPServer
    # ------------------------------------------------------------------
    idp_id = f"idp-system-github-{_t}"
    existing_idp = db.query(IdPServer).filter(IdPServer.id == idp_id).first()

    if existing_idp:
        pass
    else:
        idp = IdPServer(
            id=idp_id,
            tenant_id=tenant_id,
            name="GitHub",
            issuer="https://github.com",
            type="github",
            well_known_url=None,  # GitHub doesn't support OIDC discovery
            authorization_endpoint="https://github.com/login/oauth/authorize",
            token_endpoint="https://github.com/login/oauth/access_token",
            revocation_endpoint=None,
            introspection_endpoint=None,
            jwks_uri=None,
            userinfo_endpoint="https://api.github.com/user",
            supports_pkce=False,
            supports_refresh_tokens=True,
            supports_client_credentials=False,
            token_endpoint_auth_methods=["client_secret_post"],
            scopes_supported=["openid", "profile", "email", "repo"],
            response_types_supported=["code"],
            grant_types_supported=["authorization_code", "refresh_token"],
            enabled=True,
            verified=True,
            description="GitHub OAuth for MCP server access",
            created_at=now,
            updated_at=now,
        )
        db.add(idp)
        db.flush()
        logger.info("Created GitHub IdP", context={"idp_id": idp_id})

    # ------------------------------------------------------------------
    # 2. MCPServer
    # ------------------------------------------------------------------
    server_id = f"mcp-system-github-{_t}"
    existing_server = db.query(MCPServer).filter(MCPServer.id == server_id).first()

    if existing_server:
        pass
    else:
        icon_data, icon_mime, icon_size, icon_hash = _decode_icon(ICON_GITHUB)
        server = MCPServer(
            id=server_id,
            tenant_id=tenant_id,
            name="GitHub",
            description="GitHub MCP Server \u2014 access repositories, issues, pull requests, and more",
            base_url="https://api.githubcopilot.com/mcp",
            mcp_endpoint_path="",
            auth_type="oauth",
            timeout=30.0,
            enabled=True,
            capabilities={
                "tools": [],
                "resources": [],
                "resource_templates": [],
                "prompts": [],
            },
            health_status="unknown",
            idp_id=idp_id,
            icon_data=icon_data,
            icon_mime_type=icon_mime,
            icon_size=icon_size,
            icon_hash=icon_hash,
            icon_uploaded_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(server)
        db.flush()
        logger.info("Created GitHub MCP server", context={"server_id": server_id})

    # ------------------------------------------------------------------
    # 3. OAuthAppClient
    # ------------------------------------------------------------------
    oauth_id = f"oauth-system-github-{_t}"
    github_client_id = "YOUR_GITHUB_OAUTH_CLIENT_ID"
    existing_oauth = _find_existing_oauth_client(db, oauth_id, tenant_id, idp_id, github_client_id)

    if existing_oauth:
        pass
    else:
        oauth_client = OAuthAppClient(
            id=oauth_id,
            tenant_id=tenant_id,
            name="Brutor User Portal - GitHub Client",
            app_type="webapp",
            idp_id=idp_id,
            client_id=github_client_id,  # Placeholder -- update via Admin Console
            encrypted_client_secret=None,  # Cannot encrypt without encryption service; update via Admin Console
            redirect_uri=_get_oauth_redirect_uri(config_id, idp_id),
            scopes=["openid", "profile", "email", "repo"],
            client_type="confidential",
            supports_pkce=False,
            allowed_grant_types=["authorization_code", "refresh_token"],
            is_active=True,
            description=(
                "GitHub OAuth client \u2014 update client_id and client_secret to activate. "
                "And, depending on the client accessing, update Redirect URL, "
                "currently configured for Brutor User Portal running locally."
            ),
            created_at=now,
            updated_at=now,
        )
        db.add(oauth_client)
        db.flush()
        logger.info("Created GitHub OAuth client", context={"oauth_id": oauth_id})

    # ------------------------------------------------------------------
    # 4. ServerConfig
    # ------------------------------------------------------------------
    config_id = f"sc-system-github-{_t}"
    existing_config = db.query(ServerConfig).filter(ServerConfig.id == config_id).first()

    if existing_config:
        config = existing_config
    else:
        config = ServerConfig(
            id=config_id,
            tenant_id=tenant_id,
            mcp_server_id=server_id,
            oauth_client_id=oauth_id,
            name="GitHub",
            description="GitHub MCP Server with OAuth authentication",
            priority=100,
            is_default=True,
            enabled=False,
            display_name="GitHub",
            display_description="Access GitHub repositories, issues, and pull requests",
            created_by="system",
            created_at=now,
            updated_at=now,
        )
        db.add(config)
        db.flush()
        logger.info("Created GitHub server config", context={"config_id": config_id})

    # ------------------------------------------------------------------
    # 5. Bind to Default Resource Group
    # ------------------------------------------------------------------
    _bind_config_to_group(db, config, default_group_id, logger, "GitHub")


# ======================================================================
# HubSpot MCP Server
# ======================================================================

def _seed_hubspot(
    db: Session,
    tenant_id: str,
    default_group_id: str,
    logger: BrutorLogger,
) -> None:
    """
    Seed HubSpot MCP Server with OAuth.

    Creates:
        1. IdPServer (HubSpot OAuth endpoints)
        2. MCPServer (HubSpot MCP connection with icon)
        3. OAuthAppClient (placeholder credentials, inactive, PKCE-enabled)
        4. ServerConfig (binding to Default Resource Group)
    """
    now = datetime.now(timezone.utc)
    _t = tenant_id
    idp_id = f"idp-system-hubspot-{_t}"
    server_id = f"mcp-system-hubspot-{_t}"
    oauth_id = f"oauth-system-hubspot-{_t}"
    config_id = f"sc-system-hubspot-{_t}"

    # ------------------------------------------------------------------
    # 1. IdPServer
    # ------------------------------------------------------------------
    existing_idp = db.query(IdPServer).filter(IdPServer.id == idp_id).first()

    if existing_idp:
        pass
    else:
        idp = IdPServer(
            id=idp_id,
            tenant_id=tenant_id,
            name="HubSpot",
            issuer="https://mcp.hubspot.com",
            type="manual",
            well_known_url=None,
            authorization_endpoint="https://mcp.hubspot.com/oauth/authorize",
            token_endpoint="https://mcp.hubspot.com/oauth/v1/token",
            revocation_endpoint=None,
            introspection_endpoint=None,
            jwks_uri=None,
            userinfo_endpoint="https://api.hubapi.com/oauth/v1/access-tokens/{token}",
            supports_pkce=False,
            supports_refresh_tokens=True,
            supports_client_credentials=False,
            token_endpoint_auth_methods=["client_secret_post"],
            scopes_supported=[
                "oauth",
                "crm.objects.tickets.read",
                "crm.objects.contacts.read",
                "crm.objects.companies.read",
            ],
            response_types_supported=["code"],
            grant_types_supported=["authorization_code", "refresh_token"],
            enabled=True,
            verified=True,
            description="HubSpot OAuth for CRM MCP server access",
            created_at=now,
            updated_at=now,
        )
        db.add(idp)
        db.flush()
        logger.info("Created HubSpot IdP", context={"idp_id": idp_id})

    # ------------------------------------------------------------------
    # 2. MCPServer
    # ------------------------------------------------------------------
    server_id = f"mcp-system-hubspot-{_t}"
    existing_server = db.query(MCPServer).filter(MCPServer.id == server_id).first()

    if existing_server:
        pass
    else:
        icon_data, icon_mime, icon_size, icon_hash = _decode_icon(ICON_HUBSPOT)
        server = MCPServer(
            id=server_id,
            tenant_id=tenant_id,
            name="HubSpot",
            description="HubSpot MCP Server \u2014 access CRM contacts, companies, tickets, and deals",
            base_url="https://mcp.hubspot.com",
            mcp_endpoint_path="",
            auth_type="oauth",
            timeout=30.0,
            enabled=True,
            capabilities={
                "tools": [],
                "resources": [],
                "resource_templates": [],
                "prompts": [],
            },
            health_status="unknown",
            idp_id=idp_id,
            icon_data=icon_data,
            icon_mime_type=icon_mime,
            icon_size=icon_size,
            icon_hash=icon_hash,
            icon_uploaded_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(server)
        db.flush()
        logger.info("Created HubSpot MCP server", context={"server_id": server_id})

    # ------------------------------------------------------------------
    # 3. OAuthAppClient
    # ------------------------------------------------------------------
    oauth_id = f"oauth-system-hubspot-{_t}"
    hubspot_client_id = "YOUR_HUBSPOT_OAUTH_CLIENT_ID"
    existing_oauth = _find_existing_oauth_client(db, oauth_id, tenant_id, idp_id, hubspot_client_id)

    if existing_oauth:
        pass
    else:
        oauth_client = OAuthAppClient(
            id=oauth_id,
            tenant_id=tenant_id,
            name="Brutor User Portal - HubSpot Client",
            app_type="webapp",
            idp_id=idp_id,
            client_id=hubspot_client_id,  # Placeholder -- update via Admin Console
            encrypted_client_secret=None,  # Cannot encrypt without encryption service; update via Admin Console
            redirect_uri=_get_oauth_redirect_uri(config_id, idp_id),
            scopes=[
                "oauth crm.objects.tickets.read crm.objects.contacts.read "
                "crm.objects.companies.read crm.objects.deals.read "
                "crm.objects.users.read crm.objects.owners.read "
                "crm.objects.products.read crm.objects.line_items.read"
            ],
            client_type="public_with_secret",
            supports_pkce=True,
            allowed_grant_types=["authorization_code"],
            is_active=True,
            description=(
                "HubSpot OAuth client \u2014 update client_id and client_secret to activate. "
                "And, depending on the client accessing, update Redirect URL, "
                "currently configured for Brutor User Portal running locally."
            ),
            created_at=now,
            updated_at=now,
        )
        db.add(oauth_client)
        db.flush()
        logger.info("Created HubSpot OAuth client", context={"oauth_id": oauth_id})

    # ------------------------------------------------------------------
    # 4. ServerConfig
    # ------------------------------------------------------------------
    config_id = f"sc-system-hubspot-{_t}"
    existing_config = db.query(ServerConfig).filter(ServerConfig.id == config_id).first()

    if existing_config:
        config = existing_config
    else:
        config = ServerConfig(
            id=config_id,
            tenant_id=tenant_id,
            mcp_server_id=server_id,
            oauth_client_id=oauth_id,
            name="HubSpot CRM",
            description="HubSpot CRM gives access to contacts, customer data, deals etc",
            priority=100,
            is_default=False,
            enabled=False,
            display_name="HubSpot CRM",
            display_description="Access HubSpot CRM \u2014 contacts, companies, tickets, and deals",
            created_by="system",
            created_at=now,
            updated_at=now,
        )
        db.add(config)
        db.flush()
        logger.info("Created HubSpot server config", context={"config_id": config_id})

    # ------------------------------------------------------------------
    # 5. Bind to Default Resource Group
    # ------------------------------------------------------------------
    _bind_config_to_group(db, config, default_group_id, logger, "HubSpot")


# ======================================================================
# Slack MCP Server
# ======================================================================

def _seed_slack(
    db: Session,
    tenant_id: str,
    default_group_id: str,
    logger: BrutorLogger,
) -> None:
    """
    Seed Slack MCP Server with OAuth.

    Creates:
        1. IdPServer (Slack OAuth endpoints, PKCE-enabled)
        2. MCPServer (Slack MCP connection with icon)
        3. OAuthAppClient (placeholder credentials, inactive, PKCE-enabled)
        4. ServerConfig (binding to Default Resource Group)
    """
    now = datetime.now(timezone.utc)
    _t = tenant_id
    idp_id = f"idp-system-slack-{_t}"
    server_id = f"mcp-system-slack-{_t}"
    oauth_id = f"oauth-system-slack-{_t}"
    config_id = f"sc-system-slack-{_t}"

    # ------------------------------------------------------------------
    # 1. IdPServer
    # ------------------------------------------------------------------
    existing_idp = db.query(IdPServer).filter(IdPServer.id == idp_id).first()

    if existing_idp:
        pass
    else:
        idp = IdPServer(
            id=idp_id,
            tenant_id=tenant_id,
            name="Slack",
            issuer="https://mcp.slack.com",
            type="manual",
            well_known_url=None,
            authorization_endpoint="https://slack.com/oauth/v2_user/authorize",
            token_endpoint="https://slack.com/api/oauth.v2.user.access",
            revocation_endpoint=None,
            introspection_endpoint=None,
            jwks_uri=None,
            userinfo_endpoint=None,
            supports_pkce=True,
            supports_refresh_tokens=True,
            supports_client_credentials=False,
            token_endpoint_auth_methods=["client_secret_post"],
            scopes_supported=[
                "search:read.public",
                "search:read.private",
                "search:read.mpim",
                "search:read.im",
                "search:read.files",
                "search:read.users",
                "chat:write",
                "channels:history",
                "groups:history",
                "mpim:history",
                "im:history",
                "canvases:read",
                "canvases:write",
                "users:read",
                "users:read.email",
            ],
            response_types_supported=["code"],
            grant_types_supported=["authorization_code"],
            enabled=True,
            verified=False,
            description="Slack OAuth for messaging MCP server access",
            created_at=now,
            updated_at=now,
        )
        db.add(idp)
        db.flush()
        logger.info("Created Slack IdP", context={"idp_id": idp_id})

    # ------------------------------------------------------------------
    # 2. MCPServer
    # ------------------------------------------------------------------
    server_id = f"mcp-system-slack-{_t}"
    existing_server = db.query(MCPServer).filter(MCPServer.id == server_id).first()

    if existing_server:
        pass
    else:
        icon_data, icon_mime, icon_size, icon_hash = _decode_icon(ICON_SLACK)
        server = MCPServer(
            id=server_id,
            tenant_id=tenant_id,
            name="Slack",
            description="Slack messaging system",
            base_url="https://mcp.slack.com/mcp",
            mcp_endpoint_path="",
            auth_type="oauth",
            timeout=30.0,
            enabled=True,
            capabilities={
                "tools": [],
                "resources": [],
                "resource_templates": [],
                "prompts": [],
            },
            health_status="unknown",
            idp_id=idp_id,
            icon_data=icon_data,
            icon_mime_type=icon_mime,
            icon_size=icon_size,
            icon_hash=icon_hash,
            icon_uploaded_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(server)
        db.flush()
        logger.info("Created Slack MCP server", context={"server_id": server_id})

    # ------------------------------------------------------------------
    # 3. OAuthAppClient
    # ------------------------------------------------------------------
    oauth_id = f"oauth-system-slack-{_t}"
    slack_client_id = "YOUR_SLACK_OAUTH_CLIENT_ID"
    existing_oauth = _find_existing_oauth_client(db, oauth_id, tenant_id, idp_id, slack_client_id)

    if existing_oauth:
        pass
    else:
        oauth_client = OAuthAppClient(
            id=oauth_id,
            tenant_id=tenant_id,
            name="Brutor User Portal - Slack Client",
            app_type="webapp",
            idp_id=idp_id,
            client_id=slack_client_id,  # Placeholder -- update via Admin Console
            encrypted_client_secret=None,  # Cannot encrypt without encryption service; update via Admin Console
            redirect_uri=_get_oauth_redirect_uri(config_id, idp_id),
            scopes=[
                "search:read.public search:read.private search:read.mpim "
                "search:read.im search:read.files search:read.users "
                "chat:write channels:history groups:history mpim:history "
                "im:history canvases:read canvases:write users:read "
                "users:read.email"
            ],
            client_type="confidential",
            supports_pkce=True,
            allowed_grant_types=["authorization_code"],
            is_active=True,
            description=(
                "Slack OAuth client \u2014 update client_id and client_secret to activate. "
                "And, depending on the client accessing, update Redirect URL, "
                "currently configured for Brutor User Portal running locally."
            ),
            created_at=now,
            updated_at=now,
        )
        db.add(oauth_client)
        db.flush()
        logger.info("Created Slack OAuth client", context={"oauth_id": oauth_id})

    # ------------------------------------------------------------------
    # 4. ServerConfig
    # ------------------------------------------------------------------
    config_id = f"sc-system-slack-{_t}"
    existing_config = db.query(ServerConfig).filter(ServerConfig.id == config_id).first()

    if existing_config:
        config = existing_config
    else:
        config = ServerConfig(
            id=config_id,
            tenant_id=tenant_id,
            mcp_server_id=server_id,
            oauth_client_id=oauth_id,
            name="Slack Messenger",
            description="Access to messages in Slack",
            priority=100,
            is_default=False,
            enabled=False,
            display_name="Slack Messenger",
            display_description="Access Slack channels, messages, and canvases",
            created_by="system",
            created_at=now,
            updated_at=now,
        )
        db.add(config)
        db.flush()
        logger.info("Created Slack server config", context={"config_id": config_id})

    # ------------------------------------------------------------------
    # 5. Bind to Default Resource Group
    # ------------------------------------------------------------------
    _bind_config_to_group(db, config, default_group_id, logger, "Slack")


# ======================================================================
# Helpers
# ======================================================================

def _bind_config_to_group(
    db: Session,
    config: ServerConfig,
    default_group_id: str,
    logger: BrutorLogger,
    server_label: str,
) -> None:
    """Bind a ServerConfig to the Default Resource Group (idempotent)."""
    default_group = db.query(ResourceGroup).filter(
        ResourceGroup.id == default_group_id
    ).first()

    if default_group and config not in default_group.server_configs:
        default_group.server_configs.append(config)
        db.flush()

        logger.info(f"Bound {server_label} server config to Default group", context={
            "config_id": config.id, "group_id": default_group_id,
        })
