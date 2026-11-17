# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Shared helpers for live MCP / VMCP end-to-end tests.

Two distinct suites consume these:
  * ``test_e2e_aws_kb_mcp.py`` — per-model AWS Knowledge Base MCP
    tool invocation. Pure MCP path, no aggregation.
  * ``test_e2e_vmcp_aggregation.py`` — per-model VMCP invocation
    against a Brutor-aggregated server fronting AWS KB + Cloudflare
    Docs. Exercises the ``/v1/proxy/vmcp/{id}`` route + tool-name
    rewrite.

Both pull from the existing ``test_e2e_smoke.py`` framework for the
per-model dispatch loop (``_chat_or_responses``,
``_list_trial_chat_models``, ``_ChatAttempt``, ``short``). Those are
re-exported here so test files don't reach into ``test_e2e_smoke``
internals.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest

from .conftest import ensure_catalog_mcp_server, short  # noqa: F401  (re-export for callers)
from .test_e2e_smoke import (  # noqa: F401  (re-export)
    _ChatAttempt,
    _CHAT_HTTP_TIMEOUT,
    _chat_or_responses,
    _list_trial_chat_models,
)


# MCP server discovery: catalogued MCP servers in the trial bundle
# are named in the seed by their human-readable label. We look the
# server up by name from the trial user's portal-servers list so
# tenant-id and ULID changes don't break the test on every clean DB.
_AWS_KB_DISPLAY_NAME = "AWS Knowledge Base"
_CLOUDFLARE_DISPLAY_NAME = "Cloudflare Docs"

# Tool names as exposed by the upstream MCP servers (the seed
# catalog runs `initialize` + `tools/list` against each upstream and
# stores the discovered tool names verbatim — these are the names a
# direct /v1/proxy/mcp/{id} caller uses).
AWS_KB_SEARCH_TOOL = "aws___search_documentation"
CLOUDFLARE_DOCS_SEARCH_TOOL = "search_cloudflare_documentation"

# VMCP-side exposed names (the operator-chosen rewrite). Used by the
# VMCP aggregation test — these names are what models see in tools/list
# from the VMCP, and what the proxy maps back to the upstream tool
# at tools/call time.
VMCP_NAME = "Brutor Docs Aggregation"
VMCP_AWS_EXPOSED_NAME = "brutor_aws_doc_search"
VMCP_CLOUDFLARE_EXPOSED_NAME = "brutor_cloudflare_doc_search"


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------


def _find_portal_server_by_name(
    proxy_client: httpx.Client,
    headers: Dict[str, str],
    display_name: str,
) -> Optional[Dict[str, Any]]:
    """Return the portal-server config (server_config row) whose
    underlying MCP server display name matches. Returns the WHOLE
    record so callers can pick out either ``mcp_server.id`` for the
    MCP proxy path or ``virtual_server.id`` for the VMCP path.

    The portal lists every server_config visible to the user across
    BOTH regular MCP and Virtual MCP — that's why we iterate the
    union and match by display_name / mcp_server.name."""
    for path in ("/v1/portal/servers", "/v1/portal/servers/"):
        r = proxy_client.get(path, headers=headers)
        if r.status_code != 200:
            continue
        body = r.json()
        raw = body.get("servers") if isinstance(body, dict) else body
        if isinstance(raw, dict):
            raw = list(raw.values())
        if not isinstance(raw, list):
            continue
        for s in raw:
            if not isinstance(s, dict):
                continue
            # The portal surfaces the display_name on the config row
            # plus a nested mcp_server.name when the config wraps an
            # MCP server. Match either.
            candidates = [
                s.get("display_name"),
                s.get("mcp_server_name"),
                (s.get("mcp_server") or {}).get("name"),
                (s.get("virtual_server") or {}).get("name"),
                s.get("name"),
            ]
            if display_name in (c for c in candidates if c):
                return s
    return None


def _resolve_mcp_server_id_by_name(
    proxy_client: httpx.Client,
    headers: Dict[str, str],
    display_name: str,
) -> Optional[str]:
    """Resolve the underlying ``mcp_server.id`` (NOT the
    server_config id) so callers can talk directly to
    ``/v1/proxy/mcp/{id}``.

    The MCP proxy route is keyed on the MCP server id, not the
    server_config id — getting this wrong yields ``404 not found``.
    """
    cfg = _find_portal_server_by_name(proxy_client, headers, display_name)
    if not cfg:
        return None
    mcp = cfg.get("mcp_server") or {}
    if isinstance(mcp, dict) and mcp.get("id"):
        return mcp["id"]
    return cfg.get("mcp_server_id")


def _resolve_vmcp_id_by_name(
    proxy_client: httpx.Client,
    headers: Dict[str, str],
    display_name: str,
) -> Optional[str]:
    """Resolve the ``virtual_server.id`` so callers can talk to
    ``/v1/proxy/vmcp/{id}``. Same display-name match as the MCP path."""
    cfg = _find_portal_server_by_name(proxy_client, headers, display_name)
    if not cfg:
        return None
    vs = cfg.get("virtual_server") or {}
    if isinstance(vs, dict) and vs.get("id"):
        return vs["id"]
    return cfg.get("virtual_server_id")


# ---------------------------------------------------------------------------
# MCP tool invocation
# ---------------------------------------------------------------------------


def _parse_mcp_response_body(text: str) -> Optional[Dict[str, Any]]:
    """Decode a JSON-RPC response body that may be either plain JSON
    OR Streamable-HTTP SSE-framed (``event: message\\ndata: {...}``).

    Some upstream MCP servers (Cloudflare Docs at time of writing)
    declare the Streamable HTTP transport and reply to ``tools/call``
    with a one-event SSE stream rather than a JSON document. The
    Brutor VMCP proxy currently passes that framing through
    verbatim. Operators / tests calling the proxy directly therefore
    need to support both shapes.

    Returns the decoded JSON-RPC envelope, or ``None`` if the body
    can't be coerced to one. Callers treat ``None`` as a parse
    failure with a clear surfaced message.
    """
    if not text:
        return None
    stripped = text.lstrip()
    # Fast path: plain JSON object.
    if stripped.startswith("{"):
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            return None
        return decoded if isinstance(decoded, dict) else None
    # SSE path: one or more ``data: <json>`` lines. The MCP spec
    # uses a single message frame per response; if there were
    # multiple we'd want the LAST one (final state). Walk all
    # ``data:`` lines and return the final parseable JSON object.
    last_obj: Optional[Dict[str, Any]] = None
    for line in text.splitlines():
        line = line.rstrip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].lstrip()
        if not payload or payload == "[DONE]":
            continue
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            last_obj = decoded
    return last_obj


def call_mcp_tool(
    proxy_client: httpx.Client,
    headers: Dict[str, str],
    server_id: str,
    tool_name: str,
    arguments: Dict[str, Any],
    *,
    proxy_path: str = "mcp",
) -> Tuple[bool, str]:
    """JSON-RPC ``tools/call`` against an MCP or VMCP server.

    ``proxy_path`` selects the route family: ``"mcp"`` for direct
    upstream MCP servers, ``"vmcp"`` for Brutor virtual aggregators.
    Returns ``(ok, detail)`` — detail is either a short snippet of
    the textual response on success, or the error message on
    failure.
    """
    if proxy_path not in ("mcp", "vmcp"):
        raise ValueError(f"proxy_path must be 'mcp' or 'vmcp', got {proxy_path!r}")

    rpc = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    try:
        r = proxy_client.post(
            f"/v1/proxy/{proxy_path}/{server_id}",
            headers=headers,
            json=rpc,
            timeout=_CHAT_HTTP_TIMEOUT,
        )
    except httpx.HTTPError as e:
        return False, f"transport: {type(e).__name__}: {short(str(e), 120)}"
    if r.status_code != 200:
        return False, f"{r.status_code} {short(r.text, 160)}"
    body = _parse_mcp_response_body(r.text)
    if body is None:
        return False, f"unparseable body {short(r.text, 160)}"
    if isinstance(body.get("error"), dict):
        return False, f"jsonrpc error {short(json.dumps(body['error']), 160)}"
    result = body.get("result") or {}
    if result.get("isError"):
        return False, f"isError=true {short(json.dumps(result), 160)}"
    content = result.get("content") or []
    text = ""
    for chunk in content:
        if isinstance(chunk, dict) and chunk.get("type") == "text":
            text += chunk.get("text") or ""
    if not text:
        # Some tools return non-text content (resources, image refs).
        # Treat a non-empty content array as success for those.
        if content:
            return True, f"(non-text content, {len(content)} chunks)"
        return False, f"empty result content {short(json.dumps(result), 160)}"
    return True, short(text, 80)


def mcp_tools_list(
    proxy_client: httpx.Client,
    headers: Dict[str, str],
    server_id: str,
    *,
    proxy_path: str = "mcp",
) -> List[Dict[str, Any]]:
    """JSON-RPC ``tools/list`` for a server. Returns ``[]`` on
    failure rather than raising — callers handle the empty case."""
    if proxy_path not in ("mcp", "vmcp"):
        raise ValueError(f"proxy_path must be 'mcp' or 'vmcp', got {proxy_path!r}")
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    try:
        r = proxy_client.post(
            f"/v1/proxy/{proxy_path}/{server_id}",
            headers=headers,
            json=rpc,
            timeout=_CHAT_HTTP_TIMEOUT,
        )
    except httpx.HTTPError:
        return []
    if r.status_code != 200:
        return []
    body = _parse_mcp_response_body(r.text)
    if body is None:
        return []
    if isinstance(body.get("error"), dict):
        return []
    return list((body.get("result") or {}).get("tools") or [])


def build_tool_def_from_mcp_tool(tool: Dict[str, Any]) -> Dict[str, Any]:
    """Translate an MCP tool entry into the OpenAI tool-def shape
    that chat completions expects."""
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description")
            or f"MCP tool: {tool['name']}",
            "parameters": tool.get("inputSchema") or {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    }


# ---------------------------------------------------------------------------
# Per-model sweep
# ---------------------------------------------------------------------------


def extract_tool_call(
    msg: Dict[str, Any], tool_name: str,
) -> Optional[Dict[str, Any]]:
    """Find the first tool_call whose function name matches.
    Returns the call dict or None."""
    for tc in (msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        if fn.get("name") == tool_name:
            return tc
    return None


def parse_tool_args(call: Dict[str, Any]) -> Dict[str, Any]:
    """LLMs emit arguments as a JSON-encoded STRING. Decode it
    defensively — fall through to an empty dict on garbage so the
    test can still attempt the call (some MCP tools accept defaults)."""
    raw = (call.get("function") or {}).get("arguments") or "{}"
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def chat_with_tools(
    proxy_client: httpx.Client,
    chat_headers: Dict[str, str],
    model: Dict[str, Any],
    tool_defs: List[Dict[str, Any]],
    *,
    system_prompt: str,
    user_prompt: str,
    stream: bool = False,
) -> _ChatAttempt:
    """Wrapper around the smoke-test dispatch helper. Pulls the
    Responses-vs-chat decision off the model row the same way the
    smoke test does, so reasoning models that require /v1/responses
    get the right endpoint."""
    use_responses = (model.get("mode") or "").lower() == "responses" or bool(
        (model.get("capabilities") or {}).get("requires_responses_api")
        or model.get("requires_responses_api")
    )
    body: Dict[str, Any] = {
        "model": model["name"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": 512,
        "temperature": 0,
    }
    if tool_defs and model.get("supports_function_calling", False):
        body["tools"] = tool_defs
        body["tool_choice"] = "auto"
    return _chat_or_responses(
        proxy_client,
        chat_headers,
        model["name"],
        body,
        stream=stream,
        use_responses=use_responses,
    )


def render_rollup(
    rollup: Dict[str, Dict[str, str]],
    *,
    title: str,
    columns: List[str],
) -> str:
    """Render a per-model rollup table for the suite's final report.
    Mirrors the smoke test's tabular output so operators get a
    consistent feel."""
    lines = [f"\n{title}\n" + "=" * len(title)]
    if not rollup:
        lines.append("(no models exercised)")
        return "\n".join(lines)
    col_w = max(20, max(len(name) for name in rollup) + 2)
    header = "MODEL".ljust(col_w) + "".join(c.ljust(18) for c in columns)
    lines.append(header)
    lines.append("-" * len(header))
    for name in sorted(rollup):
        row = rollup[name]
        lines.append(
            name.ljust(col_w)
            + "".join((row.get(c) or "—").ljust(18) for c in columns)
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Shared fixtures used by both new test files
# ---------------------------------------------------------------------------


# Catalog entry ids (global MCP catalog, `mcp-cat-system-<slug>`). The
# session fixtures below install them from the catalog when missing and
# bind them to E2ETests — no manual Admin-UI step is needed.
_AWS_KB_CATALOG_ID = "mcp-cat-system-aws_knowledge"
_CLOUDFLARE_CATALOG_ID = "mcp-cat-system-cloudflare"


def _ensure_and_resolve(
    control_client: httpx.Client,
    admin_headers: Dict[str, str],
    proxy_client: httpx.Client,
    trial_headers: Dict[str, str],
    e2e_group_id: str,
    catalog_id: str,
    display_name: str,
) -> str:
    ensure_catalog_mcp_server(
        control_client, admin_headers,
        catalog_entry_id=catalog_id, display_name=display_name,
        group_id=e2e_group_id,
    )
    sid = _resolve_mcp_server_id_by_name(proxy_client, trial_headers, display_name)
    if not sid:
        pytest.fail(
            f"{display_name!r} was installed from the MCP catalog and bound "
            f"to E2ETests, but is not visible in the trial user's "
            f"portal-server list (GET /v1/portal/servers)."
        )
    return sid


@pytest.fixture(scope="session")
def aws_kb_server_id(
    control_client: httpx.Client,
    admin_headers: Dict[str, str],
    proxy_client: httpx.Client,
    trial_headers: Dict[str, str],
    e2e_group_id: str,
) -> str:
    """AWS Knowledge Base MCP server id (installed + bound if missing)."""
    return _ensure_and_resolve(
        control_client, admin_headers, proxy_client, trial_headers,
        e2e_group_id, _AWS_KB_CATALOG_ID, _AWS_KB_DISPLAY_NAME,
    )


@pytest.fixture(scope="session")
def cloudflare_server_id(
    control_client: httpx.Client,
    admin_headers: Dict[str, str],
    proxy_client: httpx.Client,
    trial_headers: Dict[str, str],
    e2e_group_id: str,
) -> str:
    """Cloudflare Docs MCP server id (installed + bound if missing)."""
    return _ensure_and_resolve(
        control_client, admin_headers, proxy_client, trial_headers,
        e2e_group_id, _CLOUDFLARE_CATALOG_ID, _CLOUDFLARE_DISPLAY_NAME,
    )
