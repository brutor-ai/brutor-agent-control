# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Claude Desktop config export

Turns a Brutor end-user's resource-group access into a
`claude_desktop_config.json` snippet they can drop into their
Claude Desktop install.

# What gets exported

For each MCP server the user can reach (via their resource-group
memberships → `resource_group_server_configs` → `server_configs`
→ `mcp_servers`), one entry under `mcpServers` keyed by the
server's name. Each entry uses the `mcp-remote` npm package
(https://www.npmjs.com/package/mcp-remote) as a stdio↔HTTP bridge
— Claude Desktop spawns the bridge subprocess, which forwards the
MCP traffic to Brutor's `/v1/proxy/mcp/{server_id}` endpoint over
HTTP.

Virtual MCP servers are exported the same way against the
`/v1/proxy/vmcp/{vserver_id}` route, and follow the *identical*
reachability path: a VMCP-backed `server_config` bound to one of
the user's resource groups. No more tenant-wide
VMCP fanout.

# What's NOT exported (today)

- **Standalone Brutor skills** — they're not yet wrapped as
  MCP servers. Once a wrapper exists, this export picks them up
  automatically.
- **A2A delegation graphs** — Claude Desktop doesn't understand
  A2A. Separate export path if a customer asks.
- **LLM model access** — Claude Desktop talks to its own model
  (Claude); LLM model routing via Brutor isn't relevant for the
  Desktop install. (For routing Claude Desktop's actual LLM
  traffic, see `claude-code-connect-guide.md` — Claude Code has
  the env-var override; Desktop doesn't.)
"""

from __future__ import annotations

from typing import Any, Dict, List

from sqlalchemy.orm import Session

from app.database.models.end_user import EndUser
from app.database.models.mcp_server import MCPServer
from app.database.models.resource_group_membership import ResourceGroupMembership
from app.database.models.server_config import ServerConfig
from app.database.models.virtual_server import VirtualServer


def build_claude_desktop_config(
    *,
    db: Session,
    tenant_id: str,
    user_id: int,
    gateway_url: str,
    api_key: str,
) -> Dict[str, Any]:
    """Generate the Claude Desktop config dict for one end user.

    Args:
        db: SQLAlchemy session
        tenant_id: tenant scoping (cross-tenant always fails the
            membership lookup, but we guard explicitly)
        user_id: which end user the config is for
        gateway_url: public URL of the Brutor gateway (e.g.
            `https://gateway.example.com`). Must NOT include the
            `/v1` suffix; we add path components ourselves.
        api_key: the broker API key the user will authenticate
            with. Passed through unchanged into the
            `Authorization: Bearer ...` header arg.

    Returns:
        The Claude Desktop config shape:
        ```
        {
          "mcpServers": {
            "<friendly-name>": {
              "command": "npx",
              "args": ["-y", "mcp-remote", "<url>", "--header", "Authorization: Bearer <key>"]
            },
            ...
          }
        }
        ```

    Raises:
        ValueError if the user is not in the tenant, or if the
        gateway_url looks malformed.
    """
    if not gateway_url:
        raise ValueError("gateway_url is required")
    base_url = gateway_url.rstrip("/")
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        raise ValueError(
            f"gateway_url must be http(s); got: {gateway_url}"
        )

    user = (
        db.query(EndUser)
        .filter(EndUser.id == user_id)
        .filter(EndUser.tenant_id == tenant_id)
        .first()
    )
    if user is None:
        raise ValueError(
            f"User {user_id} not found in tenant {tenant_id}"
        )

    mcp_servers, virtual_servers = _resolve_reachable_servers(
        db=db, tenant_id=tenant_id, user_id=user_id,
    )

    entries: Dict[str, Dict[str, Any]] = {}
    for server in mcp_servers:
        entry_name = _claude_desktop_name(prefix="brutor-mcp", server_name=server.name)
        entries[entry_name] = _build_mcp_entry(
            url=f"{base_url}/v1/proxy/mcp/{server.id}",
            api_key=api_key,
        )

    for vserver in virtual_servers:
        entry_name = _claude_desktop_name(prefix="brutor-vmcp", server_name=vserver.name)
        entries[entry_name] = _build_mcp_entry(
            url=f"{base_url}/v1/proxy/vmcp/{vserver.id}",
            api_key=api_key,
        )

    return {"mcpServers": entries}


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _resolve_reachable_servers(
    *, db: Session, tenant_id: str, user_id: int,
) -> tuple[List[MCPServer], List[VirtualServer]]:
    """Walk user → groups → server_configs → mcp_servers.

    Returns deduplicated lists. Disabled servers / configs are
    skipped — only what the user could actually call right now.
    """
    # 1. User's group memberships
    membership_rows = (
        db.query(ResourceGroupMembership.group_id)
        .filter(ResourceGroupMembership.tenant_id == tenant_id)
        .filter(ResourceGroupMembership.user_id == user_id)
        .all()
    )
    group_ids = [g_id for (g_id,) in membership_rows]

    if not group_ids:
        return [], []

    # 2. Server configs bound to those groups (via the M2M table).
    # The M2M table is `resource_group_server_configs` per the
    # comment on server_config.py — query through SQL for simplicity.
    from app.database.models.resource_group import resource_group_server_configs

    server_config_rows = (
        db.query(ServerConfig)
        .join(
            resource_group_server_configs,
            ServerConfig.id == resource_group_server_configs.c.server_config_id,
        )
        .filter(resource_group_server_configs.c.resource_group_id.in_(group_ids))
        .filter(ServerConfig.tenant_id == tenant_id)
        .filter(ServerConfig.enabled == True)  # noqa: E712
        .all()
    )

    # server_configs back either an MCP server
    # (`mcp_server_id`) or a Virtual MCP (`virtual_server_id`) under
    # an XOR constraint. Bucket each reachable config by backend
    # and resolve both backends with their respective enabled
    # filters — VMCPs are now per-group bound exactly like MCPs.
    mcp_server_ids = {sc.mcp_server_id for sc in server_config_rows if sc.mcp_server_id}
    vmcp_server_ids = {sc.virtual_server_id for sc in server_config_rows if sc.virtual_server_id}

    if mcp_server_ids:
        mcp_servers = (
            db.query(MCPServer)
            .filter(MCPServer.id.in_(mcp_server_ids))
            .filter(MCPServer.tenant_id == tenant_id)
            .filter(MCPServer.enabled == True)  # noqa: E712
            .all()
        )
    else:
        mcp_servers = []

    if vmcp_server_ids:
        virtual_servers = (
            db.query(VirtualServer)
            .filter(VirtualServer.id.in_(vmcp_server_ids))
            .filter(VirtualServer.tenant_id == tenant_id)
            .filter(VirtualServer.enabled == True)  # noqa: E712
            .all()
        )
    else:
        virtual_servers = []

    return mcp_servers, virtual_servers


def _claude_desktop_name(*, prefix: str, server_name: str) -> str:
    """Claude Desktop config keys are arbitrary strings but stick to
    a safe, namespaced format so multiple Brutor instances exporting
    to the same install don't collide.

    Format: `<prefix>-<slugified-server-name>` — kebab-case,
    alphanumerics + hyphens only.
    """
    slug = "".join(
        c.lower() if c.isalnum() else "-"
        for c in server_name
    ).strip("-")
    # Collapse repeated hyphens.
    while "--" in slug:
        slug = slug.replace("--", "-")
    if not slug:
        slug = "unnamed"
    return f"{prefix}-{slug}"


def _build_mcp_entry(*, url: str, api_key: str) -> Dict[str, Any]:
    """Build one Claude Desktop MCP server entry.

    Uses `mcp-remote` (https://www.npmjs.com/package/mcp-remote) —
    a Node-based stdio↔HTTP bridge that's the standard way to talk
    to remote MCP servers from Claude Desktop today. Future
    Claude Desktop versions may speak HTTP MCP directly; until
    then, the bridge is mandatory.
    """
    return {
        "command": "npx",
        "args": [
            "-y",
            "mcp-remote",
            url,
            "--header",
            f"Authorization: Bearer {api_key}",
        ],
    }
