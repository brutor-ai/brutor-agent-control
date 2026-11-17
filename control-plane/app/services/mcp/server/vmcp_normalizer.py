# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# VMCP capabilities normalization

The Virtual MCP server's `capabilities` JSON column carries both
the operator's intent (which upstream tools to expose, optional
rename/prefix/description-override) AND the runtime data the Rust
proxy reads on every `tools/list` / `tools/call`. The runtime data
is computed from intent — that computation lives here.

# Why normalize at write time, not at read time

Two reasons:
  1. The Rust proxy stays simple. It reads a fixed JSON shape per
     virtual server; it doesn't re-derive `source_mapping` or look
     up upstream tool defs on every request.
  2. Validation. Operators set rename / prefix; we catch collisions
     ("two tools end up with the same exposed_name") at save time
     instead of at the next client `tools/list` (where the failure
     mode is "one of the two tools silently shadows the other").

# Input shape (what the admin UI POSTs)

    {
      "tools": [
        {
          "server_id": "<upstream_mcp_server_id>",
          "capability_name": "<upstream's tool name>",
          "exposed_name": "<override>"          # optional
          "description_override": "<text>"      # optional
        },
        ...
      ],
      "resources": [ ... ],
      "prompts":   [ ... ],
      "tool_name_prefix": "<optional VMCP-wide prefix>"
    }

# Output shape (what gets stored on `virtual_servers.capabilities`)

Same fields as input, plus per-tool `tool_def` snapshot and the
two server-derived maps:

    {
      "tools": [
        {
          "server_id": "...",
          "capability_name": "...",
          "exposed_name": "<effective name>",   # always present after norm
          "description_override": "...",        # if provided
          "tool_def": { "name", "description", "inputSchema", ... }
                                                # snapshot from upstream
        }, ...
      ],
      "resources": [ ... ],
      "prompts": [ ... ],
      "tool_name_prefix": "...",
      "source_mapping":        { "<exposed_name>": "<server_id>" },
      "reverse_name_mapping":  { "<exposed_name>": "<capability_name>" }
    }

# Resource & prompt parity

Resources are keyed by URI, prompts by name — the same mapping
mechanic applies. For now this module fully normalizes tools and
leaves resources/prompts as basic refs (no rename) — adding the
override mechanic to those is a small extension once tools land.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.database.models.mcp_server import MCPServer


log = logging.getLogger("brutor-vmcp-normalizer")


CAPABILITY_TYPES = ("tools", "resources", "prompts")


class VMCPNormalizationError(ValueError):
    """Raised when the input capabilities JSON can't be normalized —
    duplicate exposed_names, references to non-existent upstream
    servers, etc. The API endpoint translates this to HTTP 400.
    """


def normalize_capabilities(
    *,
    db: Session,
    tenant_id: str,
    capabilities_input: Dict[str, Any],
) -> Dict[str, Any]:
    """Normalize a capabilities JSON for storage on a VirtualServer.

    Args:
        db: SQLAlchemy session (used to look up upstream tool defs).
        tenant_id: tenant scoping for the upstream lookup — never
            cross-tenant.
        capabilities_input: the operator's intent, shape above.

    Returns the normalized capabilities ready to be written to
    `virtual_servers.capabilities`. Raises VMCPNormalizationError
    on validation failure.

    Idempotent — running the normalizer on an already-normalized
    JSON returns the same result (modulo refreshed `tool_def`
    snapshots).
    """
    capabilities_input = capabilities_input or {}
    prefix = (capabilities_input.get("tool_name_prefix") or "").strip()

    # Cache of upstream MCPServer rows we've looked up so a VMCP with
    # 50 tools across 5 upstreams hits the DB only 5 times.
    upstream_cache: Dict[str, Optional[MCPServer]] = {}

    def _get_upstream(server_id: str) -> Optional[MCPServer]:
        if server_id in upstream_cache:
            return upstream_cache[server_id]
        server = (
            db.query(MCPServer)
            .filter(MCPServer.id == server_id)
            .filter(MCPServer.tenant_id == tenant_id)
            .first()
        )
        upstream_cache[server_id] = server
        return server

    # Walk tools first — they get the full override mechanic.
    normalized_tools, source_mapping, reverse_name_mapping = _normalize_tools(
        tools_input=capabilities_input.get("tools") or [],
        prefix=prefix,
        get_upstream=_get_upstream,
    )

    # Resources + prompts — basic refs, no rename in this sprint.
    # Snapshot upstream defs the same way so the Rust list handler
    # returns proper MCP entities rather than refs.
    normalized_resources = _normalize_basic(
        items=capabilities_input.get("resources") or [],
        capability_type="resources",
        get_upstream=_get_upstream,
    )
    normalized_prompts = _normalize_basic(
        items=capabilities_input.get("prompts") or [],
        capability_type="prompts",
        get_upstream=_get_upstream,
    )

    out: Dict[str, Any] = {
        "tools": normalized_tools,
        "resources": normalized_resources,
        "prompts": normalized_prompts,
        "source_mapping": source_mapping,
        "reverse_name_mapping": reverse_name_mapping,
    }
    if prefix:
        out["tool_name_prefix"] = prefix
    return out


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _normalize_tools(
    *,
    tools_input: List[Any],
    prefix: str,
    get_upstream,
) -> Tuple[List[Dict[str, Any]], Dict[str, str], Dict[str, str]]:
    """Process tools: apply prefix / rename overrides, snapshot
    upstream tool defs, validate collisions, build maps."""
    normalized: List[Dict[str, Any]] = []
    source_mapping: Dict[str, str] = {}
    reverse_name_mapping: Dict[str, str] = {}

    for raw in tools_input:
        if not isinstance(raw, dict):
            raise VMCPNormalizationError(
                f"Tool entry must be an object, got {type(raw).__name__}"
            )
        server_id = raw.get("server_id")
        capability_name = raw.get("capability_name")
        if not server_id or not capability_name:
            raise VMCPNormalizationError(
                f"Tool entry missing server_id or capability_name: {raw}"
            )

        explicit_exposed = (raw.get("exposed_name") or "").strip()
        if explicit_exposed:
            exposed_name = explicit_exposed
        elif prefix:
            exposed_name = f"{prefix}{capability_name}"
        else:
            exposed_name = capability_name

        # Collision: two tool entries producing the same exposed
        # name. Without this guard, one would silently shadow the
        # other inside source_mapping (last write wins) and the
        # operator would never see a failure.
        if exposed_name in source_mapping:
            existing_server = source_mapping[exposed_name]
            raise VMCPNormalizationError(
                f"Exposed tool name '{exposed_name}' is claimed by two "
                f"entries: server '{existing_server}' and server "
                f"'{server_id}'. Set explicit `exposed_name` on one "
                "of them to disambiguate."
            )

        upstream = get_upstream(server_id)
        if upstream is None:
            raise VMCPNormalizationError(
                f"Upstream MCP server '{server_id}' not found in tenant. "
                "Remove the orphan tool entry or re-add the server."
            )

        # Snapshot the upstream's tool def so the Rust list handler
        # can return a proper MCP tool object (name, description,
        # inputSchema) rather than a {server_id, capability_name}
        # ref. The snapshot is refreshed on every VMCP save — out-
        # of-band changes to the upstream propagate next time the
        # operator opens + saves the VMCP.
        upstream_tool_def = _find_in_upstream(
            upstream_capabilities=upstream.capabilities or {},
            capability_type="tools",
            capability_name=capability_name,
        )
        if upstream_tool_def is None:
            log.warning(
                "Upstream tool def not in capability snapshot — VMCP "
                "tools/list will use the ref-shaped fallback",
                extra={
                    "server_id": server_id,
                    "capability_name": capability_name,
                },
            )

        entry: Dict[str, Any] = {
            "server_id": server_id,
            "capability_name": capability_name,
            "exposed_name": exposed_name,
        }
        description_override = (raw.get("description_override") or "").strip()
        if description_override:
            entry["description_override"] = description_override
        if upstream_tool_def is not None:
            entry["tool_def"] = upstream_tool_def

        normalized.append(entry)
        source_mapping[exposed_name] = server_id
        reverse_name_mapping[exposed_name] = capability_name

    return normalized, source_mapping, reverse_name_mapping


def _normalize_basic(
    *,
    items: List[Any],
    capability_type: str,
    get_upstream,
) -> List[Dict[str, Any]]:
    """Resources/prompts — snapshot upstream def but no rename
    mechanic yet. Same validation around upstream existence."""
    normalized: List[Dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, dict):
            raise VMCPNormalizationError(
                f"{capability_type} entry must be an object, "
                f"got {type(raw).__name__}"
            )
        server_id = raw.get("server_id")
        capability_name = raw.get("capability_name")
        if not server_id or not capability_name:
            raise VMCPNormalizationError(
                f"{capability_type} entry missing server_id or "
                f"capability_name: {raw}"
            )
        upstream = get_upstream(server_id)
        if upstream is None:
            raise VMCPNormalizationError(
                f"Upstream MCP server '{server_id}' not found in tenant."
            )
        upstream_def = _find_in_upstream(
            upstream_capabilities=upstream.capabilities or {},
            capability_type=capability_type,
            capability_name=capability_name,
        )
        entry: Dict[str, Any] = {
            "server_id": server_id,
            "capability_name": capability_name,
        }
        if upstream_def is not None:
            entry["def"] = upstream_def
        normalized.append(entry)
    return normalized


def _find_in_upstream(
    *,
    upstream_capabilities: Dict[str, Any],
    capability_type: str,
    capability_name: str,
) -> Optional[Dict[str, Any]]:
    """Look up the upstream MCP's capability snapshot for the named
    tool/resource/prompt. Returns the raw entry dict from the
    upstream's `capabilities` JSON or None if not found.

    The upstream's capabilities are the raw `initialize` response —
    `{"tools": [{"name": ..., "description": ..., "inputSchema": ...}]}`
    — so we match by `name` (tools/prompts) or `uri` (resources).
    """
    items = upstream_capabilities.get(capability_type)
    if not isinstance(items, list):
        return None
    if capability_type == "resources":
        # Resources are keyed by URI in the MCP spec.
        for item in items:
            if isinstance(item, dict) and item.get("uri") == capability_name:
                return item
    else:
        for item in items:
            if isinstance(item, dict) and item.get("name") == capability_name:
                return item
    return None
