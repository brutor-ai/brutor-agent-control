# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# The one canonical shape of a stored MCP tool

`mcp_servers.capabilities` is read by the runtime as the authority on what a
tool IS: the core derives a tool's risk attributes from its MCP
`annotations` (autonomy gating, `deny_tool_risk`, evidence attribution), and
an unannotated tool fails closed as side-effecting. So a writer that drops a
field is not a cosmetic bug — the legacy handshake discovery path used to
whitelist `name`/`description`/`inputSchema`, every `readOnlyHint` was lost,
and read-only tools were blocked as writes under `restricted` autonomy.

Every Python writer of MCP server capabilities goes through
`normalize_mcp_capabilities` — enforced twice:

  * `MCPServer.capabilities` has a SQLAlchemy `@validates` hook that calls it,
    so ORM construction and attribute assignment cannot skip it;
  * `tests/test_mcp_capability_writers_guard.py` scans `app/` for the paths
    that bypass the ORM (raw SQL, Core `update().values()`, bulk mappings,
    `setattr`, in-place mutation) and fails on any of them.

# What is kept

The MCP spec's Tool fields: `name`, `title`, `description`, `inputSchema`,
`outputSchema`, `annotations`, `icons`, `_meta`. Top-level `None` and empty
values (and unset annotation hints) are dropped — an empty `description` is
"not declared", not a declaration. Schemas and `_meta` are kept verbatim, and
`inputSchema` is always present (the spec requires it; legacy rows always
carried it). A tool without a `name` is uncallable and is dropped with a
warning.

# Content hash

`content_hash` is the sha256 of the canonical contract (lists sorted by
identity key, volatile `_brutor` metadata excluded). The capability diff
uses it to append a snapshot row only when the contract actually changed.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, Iterable, List, Mapping, Optional

log = logging.getLogger("brutor-tool-snapshot")

#: MCP spec Tool fields persisted verbatim, in a stable order.
TOOL_FIELDS = (
    "name",
    "title",
    "description",
    "inputSchema",
    "outputSchema",
    "annotations",
    "icons",
    "_meta",
)

#: The capability lists (and their identity key) that make up the contract.
CAPABILITY_LISTS = {
    "tools": "name",
    "resources": "uri",
    "resource_templates": "uriTemplate",
    "prompts": "name",
}


def _jsonable(value: Any) -> Any:
    """Deep-copy `value` into plain JSON types (AnyUrl etc. → str)."""
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", by_alias=True, exclude_none=True)
    return json.loads(json.dumps(value, default=str))


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, (str, list, dict)) and not value)




def normalize_tool(tool: Any) -> Dict[str, Any]:
    """Canonical stored form of one MCP tool.

    Accepts an MCP SDK `Tool` (or any pydantic model) or a plain dict as
    returned by `tools/list`. Raises `ValueError` when the tool has no name.
    """
    if hasattr(tool, "model_dump"):
        raw = tool.model_dump(mode="json", by_alias=True, exclude_none=True)
    elif isinstance(tool, Mapping):
        raw = _jsonable(dict(tool))
    else:
        raise TypeError(f"cannot normalise MCP tool of type {type(tool).__name__}")

    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("MCP tool has no name")

    out: Dict[str, Any] = {}
    for field in TOOL_FIELDS:
        value = raw.get(field)
        if field == "annotations" and isinstance(value, dict):
            # An unset hint is "not declared" (the SDK's exclude_none
            # view); schemas and _meta stay verbatim — a JSON-Schema
            # `"default": null` is a declaration.
            value = {k: v for k, v in value.items() if v is not None}
        if field == "inputSchema":
            out[field] = value if isinstance(value, dict) else {}
        elif not _is_empty(value):
            out[field] = value
    return out


def normalize_tools(tools: Optional[Iterable[Any]]) -> List[Dict[str, Any]]:
    """Normalise a tool list, dropping (and logging) nameless entries."""
    out: List[Dict[str, Any]] = []
    for tool in tools or []:
        try:
            out.append(normalize_tool(tool))
        except (ValueError, TypeError) as e:
            log.warning("Dropping malformed MCP tool from capabilities: %s", e)
    return out


def normalize_mcp_capabilities(
    capabilities: Optional[Mapping[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Canonical stored form of an MCP server's `capabilities` column.

    Tools are normalised with `normalize_tool`; the other lists and any extra
    keys (`extensions`, `_brutor`, …) are kept as JSON. Idempotent. `None`
    passes through (the column is nullable).
    """
    if capabilities is None:
        return None
    if not isinstance(capabilities, Mapping):
        raise TypeError(
            f"MCP capabilities must be a mapping, got {type(capabilities).__name__}"
        )
    out: Dict[str, Any] = {}
    for key, value in capabilities.items():
        if key == "tools":
            out["tools"] = normalize_tools(value if isinstance(value, list) else [])
        else:
            out[key] = _jsonable(value)
    return out


def _sorted_by(items: Any, key: str) -> List[Any]:
    if not isinstance(items, list):
        return []
    return sorted(
        items,
        key=lambda i: (
            str(i.get(key) or i.get("name") or "") if isinstance(i, dict) else "",
            json.dumps(i, sort_keys=True, default=str),
        ),
    )


def canonical_contract(capabilities: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """The comparable content of a capabilities blob.

    Order-insensitive (lists sorted by identity key) and free of volatile
    Brutor metadata (`_brutor.fetched_epoch` changes on every fetch).
    """
    caps = normalize_mcp_capabilities(capabilities or {}) or {}
    contract: Dict[str, Any] = {
        key: _sorted_by(caps.get(key), ident) for key, ident in CAPABILITY_LISTS.items()
    }
    if caps.get("extensions") is not None:
        contract["extensions"] = caps["extensions"]
    return contract


def content_hash(capabilities: Optional[Mapping[str, Any]]) -> str:
    """sha256 of `canonical_contract` — equal iff the declared contract is."""
    blob = json.dumps(
        canonical_contract(capabilities),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def tool_risk_attributes(annotations: Optional[Mapping[str, Any]]) -> List[str]:
    """Risk attributes of a tool from its MCP annotation hints.

    Mirror of the core's `services::agent_authz::tool_risk_attributes` (the
    enforcing implementation) with the MCP spec defaults: no annotations =
    `destructive` + `open_world`, i.e. side-effecting — fail closed. Used
    here to rank annotation drift (a flip that makes a tool look safer is a
    governance bypass if the server is lying). Both planes are pinned to the
    shared contract fixture so they cannot drift apart.
    """
    ann = annotations if isinstance(annotations, Mapping) else {}

    def hint(key: str, default: bool) -> bool:
        v = ann.get(key)
        return v if isinstance(v, bool) else default

    read_only = hint("readOnlyHint", False)
    destructive = False if read_only else hint("destructiveHint", True)
    idempotent = True if read_only else hint("idempotentHint", False)
    open_world = hint("openWorldHint", True)
    attrs: List[str] = []
    if read_only:
        attrs.append("read_only")
    if destructive:
        attrs.append("destructive")
    if idempotent:
        attrs.append("idempotent")
    if open_world:
        attrs.append("open_world")
    return attrs


#: Attributes whose disappearance makes a tool look less risky.
_RISK_ATTRIBUTES = ("destructive", "open_world")


def annotation_change_relaxes(
    before: Optional[Mapping[str, Any]], after: Optional[Mapping[str, Any]]
) -> bool:
    """True when the new annotations make the tool look SAFER to the gate:
    it became read-only, or lost `destructive` / `open_world`."""
    b = set(tool_risk_attributes(before))
    a = set(tool_risk_attributes(after))
    if "read_only" in a and "read_only" not in b:
        return True
    return any(attr in b and attr not in a for attr in _RISK_ATTRIBUTES)
