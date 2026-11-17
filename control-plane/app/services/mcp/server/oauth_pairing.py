# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Whether an OAuth-protected MCP server has an identity provider paired with
it, as the gateway decides it.

The gateway exchanges an authorization code for a server and an identity
provider only when the tenant configured the two as a pair:

- the server's own `idp_id` names the identity provider, or
- a server configuration of the server attaches an active OAuth client of
  that identity provider (`server_configs.oauth_client_id`).

Otherwise the exchange is answered 403. A server is saved before its
configurations, so a server with no pair is accepted and reported; an
`idp_id` that names no identity provider of the tenant is refused at save,
because it can never pair.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.database.models.idp_server import IdPServer
from app.database.models.mcp_server import MCPServer
from app.database.models.oauth_app_client import OAuthAppClient
from app.database.models.server_config import ServerConfig

# The `auth_type` values of an OAuth-protected server (the set the server
# configurations already use to require an OAuth client).
OAUTH_AUTH_TYPES = frozenset({"oauth", "oauth2"})

UNPAIRED_WARNING = (
    "No identity provider is paired with this OAuth-protected server: the gateway refuses the "
    "sign-in (403 \"Identity Provider is not configured for this MCP server\") until the server "
    "names an identity provider, or a server configuration of it attaches an active OAuth client."
)


def is_oauth_protected(auth_type: Optional[str]) -> bool:
    return (auth_type or "").lower() in OAUTH_AUTH_TYPES


def tenant_has_idp(db: Session, tenant_id: str, idp_id: str) -> bool:
    return db.query(IdPServer.id).filter(IdPServer.id == idp_id, IdPServer.tenant_id == tenant_id).first() is not None


def oauth_pairing(db: Session, server: MCPServer) -> Optional[Dict[str, Any]]:
    """`{"paired", "idp_ids", "warning"}` for an OAuth-protected server:
    the identity providers the gateway accepts a code exchange for, and what
    to tell the administrator when there is none. `None` for a server that
    is not OAuth-protected."""
    if not is_oauth_protected(server.auth_type):
        return None
    idp_ids = set()
    if server.idp_id and tenant_has_idp(db, server.tenant_id, server.idp_id):
        idp_ids.add(server.idp_id)
    attached = (
        db.query(OAuthAppClient.idp_id)
        .join(ServerConfig, ServerConfig.oauth_client_id == OAuthAppClient.id)
        .filter(
            ServerConfig.mcp_server_id == server.id,
            ServerConfig.tenant_id == server.tenant_id,
            OAuthAppClient.tenant_id == server.tenant_id,
            OAuthAppClient.is_active.is_(True),
        )
        .all()
    )
    idp_ids.update(idp_id for (idp_id,) in attached if idp_id)
    return {
        "paired": bool(idp_ids),
        "idp_ids": sorted(idp_ids),
        "warning": None if idp_ids else UNPAIRED_WARNING,
    }
