# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Live end-to-end VMCP aggregation sweep.

Sets up a Brutor Virtual MCP that fronts two unauth public upstreams:
  * AWS Knowledge Base → exposed as ``brutor_aws_doc_search``
  * Cloudflare Docs    → exposed as ``brutor_cloudflare_doc_search``

Then, for each chat-capable model the trial user can reach, drives
TWO prompts:
  1. "Search AWS docs for X" → expect a ``brutor_aws_doc_search``
     tool call → execute via ``/v1/proxy/vmcp/{vs_id}``.
  2. "Search Cloudflare docs for Y" → expect a
     ``brutor_cloudflare_doc_search`` tool call → execute via the
     same VMCP id.

The two arms exercise two distinct things end-to-end:
  * **Aggregation**: a single VMCP exposes tools sourced from two
    different upstreams.
  * **Tool-name rewrite**: the operator-chosen ``exposed_name``
    (``brutor_*_doc_search``) is what the model sees in
    ``tools/list``; the VMCP's ``reverse_name_mapping`` translates
    back to the original upstream tool at ``tools/call`` time.

Opt-in: ``pytest -m live_e2e tests/live/test_e2e_vmcp_aggregation.py -v``.
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional

import httpx
import pytest

from ._mcp_helpers import (
    AWS_KB_SEARCH_TOOL,
    CLOUDFLARE_DOCS_SEARCH_TOOL,
    VMCP_AWS_EXPOSED_NAME,
    VMCP_CLOUDFLARE_EXPOSED_NAME,
    VMCP_NAME,
    aws_kb_server_id,  # noqa: F401  (fixture re-import)
    build_tool_def_from_mcp_tool,
    call_mcp_tool,
    chat_with_tools,
    cloudflare_server_id,  # noqa: F401  (fixture re-import)
    extract_tool_call,
    mcp_tools_list,
    parse_tool_args,
    render_rollup,
    short,
)
from .test_e2e_smoke import _list_trial_chat_models


# ---------------------------------------------------------------------------
# VMCP setup fixture
# ---------------------------------------------------------------------------


def _find_existing_vmcp_id(
    control_client: httpx.Client,
    admin_headers: Dict[str, str],
    name: str,
) -> Optional[str]:
    """Admin-side discovery of an existing VMCP by name. We can't
    rely on the portal here because the trial user can only see
    VMCPs that have a server_config + group binding — and that's
    exactly what we may need to create."""
    r = control_client.get("/v1/admin/virtual-servers", headers=admin_headers)
    if r.status_code != 200:
        return None
    body = r.json()
    raw = body.get("servers") if isinstance(body, dict) else body
    if isinstance(raw, dict):
        raw = list(raw.values())
    if not isinstance(raw, list):
        return None
    for s in raw:
        if isinstance(s, dict) and s.get("name") == name and s.get("id"):
            return s["id"]
    return None


def _find_existing_server_config_for_vmcp(
    control_client: httpx.Client,
    admin_headers: Dict[str, str],
    vmcp_id: str,
) -> Optional[Dict[str, Any]]:
    """Find a server_config wrapping the given VMCP."""
    r = control_client.get("/v1/admin/server-configs", headers=admin_headers)
    if r.status_code != 200:
        return None
    body = r.json()
    raw = body.get("configs") or body.get("server_configs") or body
    if isinstance(raw, dict):
        raw = list(raw.values())
    if not isinstance(raw, list):
        return None
    for cfg in raw:
        if not isinstance(cfg, dict):
            continue
        if cfg.get("virtual_server_id") == vmcp_id:
            return cfg
    return None


@pytest.fixture(scope="module")
def vmcp_setup(
    control_client: httpx.Client,
    admin_headers: Dict[str, str],
    e2e_group_id: str,
    aws_kb_server_id: str,
    cloudflare_server_id: str,
) -> Iterator[Dict[str, str]]:
    """Idempotent VMCP + server_config + group binding (to E2ETests, removed
    again on teardown — see the note at the end).

    Returns ``{"vmcp_id": "vs-...", "config_id": "srvconf-..."}``.

    Re-runs against an already-set-up environment are no-ops: we
    discover existing rows by name / FK and skip the create. That
    keeps the fixture safe for repeated suite runs against the same
    DB without operator cleanup between them.
    """
    # 1. VMCP — create if missing.
    vmcp_id = _find_existing_vmcp_id(control_client, admin_headers, VMCP_NAME)
    if not vmcp_id:
        create_body = {
            "name": VMCP_NAME,
            "description": (
                "Aggregates one tool each from AWS Knowledge Base "
                "and Cloudflare Docs, with operator-chosen exposed "
                "names. Seeded by the live e2e VMCP test."
            ),
            "capabilities": {
                "tools": [
                    {
                        "server_id": aws_kb_server_id,
                        "capability_name": AWS_KB_SEARCH_TOOL,
                        "exposed_name": VMCP_AWS_EXPOSED_NAME,
                        "description_override": (
                            "Search the official AWS documentation."
                        ),
                    },
                    {
                        "server_id": cloudflare_server_id,
                        "capability_name": CLOUDFLARE_DOCS_SEARCH_TOOL,
                        "exposed_name": VMCP_CLOUDFLARE_EXPOSED_NAME,
                        "description_override": (
                            "Search the official Cloudflare documentation."
                        ),
                    },
                ],
                "resources": [],
                "prompts": [],
            },
        }
        r = control_client.post(
            "/v1/admin/virtual-servers",
            headers=admin_headers,
            json=create_body,
        )
        if r.status_code not in (200, 201):
            pytest.fail(
                f"VMCP create failed: {r.status_code} {short(r.text, 200)}"
            )
        created = r.json()
        vmcp_id = (
            created.get("server", {}).get("id")
            or created.get("id")
        )
        if not vmcp_id:
            pytest.fail(
                f"VMCP create response missing id: {short(r.text, 200)}"
            )

    # 2. server_config — create if missing, bound to E2ETests.
    cfg = _find_existing_server_config_for_vmcp(
        control_client, admin_headers, vmcp_id,
    )
    if not cfg:
        cfg_body = {
            "name": VMCP_NAME,
            "virtual_server_id": vmcp_id,
            "group_ids": [e2e_group_id],
            "display_name": VMCP_NAME,
            "display_description": (
                "Brutor-aggregated docs search across AWS and Cloudflare."
            ),
            "priority": 100,
            "is_default": False,
            "enabled": True,
        }
        r = control_client.post(
            "/v1/admin/server-configs",
            headers=admin_headers,
            json=cfg_body,
        )
        if r.status_code not in (200, 201):
            pytest.fail(
                f"server_config create failed: {r.status_code} "
                f"{short(r.text, 200)}"
            )
        body = r.json()
        cfg = body.get("server_config") or body
    elif e2e_group_id not in (cfg.get("group_ids") or []):
        # Already exists but not bound to E2ETests — patch.
        merged = sorted(set((cfg.get("group_ids") or []) + [e2e_group_id]))
        r = control_client.patch(
            f"/v1/admin/server-configs/{cfg['id']}",
            headers=admin_headers,
            json={"group_ids": merged},
        )
        # Not fatal if the patch fails — the test will surface a
        # clearer "VMCP not visible" error a step later.

    yield {"vmcp_id": vmcp_id, "config_id": cfg["id"]}

    # Unbind on teardown. While a VMCP server config (mcp_server_id NULL)
    # is bound to a group, the core's effective-group loader fails to decode
    # it and every LLM call in that group is refused fail-closed with 429
    # "limits_unavailable" (core services/cache/group_cache.rs, the
    # `SELECT DISTINCT sc.mcp_server_id` query decodes into a non-Option
    # String). Leaving the binding in place would poison E2ETests for every
    # later test and every later run, so it only lives for this module.
    r = control_client.get(f"/v1/admin/server-configs/{cfg['id']}", headers=admin_headers)
    if r.status_code == 200:
        body = r.json()
        row = body.get("server_config") or body
        remaining = [g for g in (row.get("group_ids") or []) if g != e2e_group_id]
        control_client.patch(
            f"/v1/admin/server-configs/{cfg['id']}",
            headers=admin_headers,
            json={"group_ids": remaining},
        )


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


_SYSTEM_PROMPT = (
    "You are a helpful assistant. You have two documentation-search "
    "tools available:\n"
    f"  * `{VMCP_AWS_EXPOSED_NAME}` — searches the official AWS docs.\n"
    f"  * `{VMCP_CLOUDFLARE_EXPOSED_NAME}` — searches the official "
    "Cloudflare docs.\n"
    "When the user asks about AWS, you MUST call the AWS tool. When "
    "the user asks about Cloudflare, you MUST call the Cloudflare "
    "tool. Never answer from memory for cloud-provider documentation "
    "questions."
)

# Each entry: (label, user_prompt, expected_exposed_name, upstream_arg_key,
#              upstream_arg_value, retry_arg_value)
#
# `upstream_arg_value` is what we substitute when the model emits
# an empty / unparseable arg payload — it keeps the test robust
# against models that emit `{}` even when the prompt is clear.
_ARMS = [
    (
        "AWS",
        "Search the AWS documentation for: how do I enable versioning "
        "on an Amazon S3 bucket?",
        VMCP_AWS_EXPOSED_NAME,
        "search_phrase",
        "S3 bucket versioning",
    ),
    (
        "Cloudflare",
        "Search the Cloudflare documentation for: how do I configure "
        "Cloudflare Workers KV bindings?",
        VMCP_CLOUDFLARE_EXPOSED_NAME,
        "query",
        "Workers KV bindings",
    ),
]


@pytest.mark.live_e2e
class TestVmcpAggregationToolCall:
    """Per-model VMCP tool invocation, both arms (AWS + Cloudflare)."""

    def test_each_chat_model_invokes_vmcp_both_tools(
        self,
        proxy_client: httpx.Client,
        trial_headers: Dict[str, str],
        vmcp_setup: Dict[str, str],
    ) -> None:
        vmcp_id = vmcp_setup["vmcp_id"]

        # Fetch the live tool list from the VMCP — operator-chosen
        # exposed names (`brutor_*_doc_search`) are what the model
        # sees, not the upstream `aws___...` / `search_cloudflare_*`.
        vmcp_tools = mcp_tools_list(
            proxy_client, trial_headers, vmcp_id, proxy_path="vmcp",
        )
        names = {t.get("name") for t in vmcp_tools}
        expected = {VMCP_AWS_EXPOSED_NAME, VMCP_CLOUDFLARE_EXPOSED_NAME}
        if not expected.issubset(names):
            missing = expected - names
            pytest.fail(
                f"VMCP {vmcp_id} tools/list missing: {sorted(missing)}. "
                f"Got: {sorted(names)}. Check the VMCP's source_mapping "
                "+ reverse_name_mapping in the admin UI."
            )
        tool_defs = [
            build_tool_def_from_mcp_tool(t)
            for t in vmcp_tools
            if t.get("name") in expected
        ]

        # Per-model loop.
        candidates, preferred_group = _list_trial_chat_models(
            proxy_client, trial_headers,
        )
        if not candidates:
            pytest.fail(
                "trial user has no chat-capable models. Enable + key at "
                "least one provider in the admin UI."
            )

        chat_headers = dict(trial_headers)
        if preferred_group:
            chat_headers["X-Resource-Group"] = preferred_group

        # Rollup: one row per model, one column per arm * three stages
        # collapsed into a single concise marker per arm:
        #   `ok`      — chat OK + correct tool_call + tool exec OK
        #   `no_call` — chat OK but model didn't call the expected tool
        #   `FAIL <stage>` — something broke at that stage
        rollup: Dict[str, Dict[str, str]] = {}
        any_full_pass = False

        for entry in candidates:
            name = entry["name"]
            row = rollup.setdefault(name, {arm[0]: "—" for arm in _ARMS})
            if not entry.get("supports_function_calling"):
                # Deliberate skip, not a failure: the configured model's
                # supports_function_calling is off.
                for arm in _ARMS:
                    row[arm[0]] = "skip: no tool calling"
                continue

            model_passed_both = True
            for arm_label, user_prompt, expected_name, arg_key, default_arg in _ARMS:
                # 1. Chat completion via the trial user's portal path.
                attempt = chat_with_tools(
                    proxy_client,
                    chat_headers,
                    entry,
                    tool_defs=tool_defs,
                    system_prompt=_SYSTEM_PROMPT,
                    user_prompt=user_prompt,
                )
                if not attempt.succeeded:
                    row[arm_label] = f"FAIL chat {attempt.status or 'tx'}"
                    model_passed_both = False
                    continue

                # 2. Tool call check.
                call = extract_tool_call(attempt.msg or {}, expected_name)
                if not call:
                    row[arm_label] = "no_call"
                    model_passed_both = False
                    continue

                # 3. Execute via the VMCP proxy.
                args = parse_tool_args(call)
                if not args.get(arg_key):
                    args = {**args, arg_key: default_arg}
                ok, detail = call_mcp_tool(
                    proxy_client,
                    trial_headers,
                    vmcp_id,
                    tool_name=expected_name,
                    arguments=args,
                    proxy_path="vmcp",
                )
                if not ok:
                    row[arm_label] = f"FAIL exec {short(detail, 30)}"
                    model_passed_both = False
                    continue
                row[arm_label] = "ok"

            if model_passed_both:
                any_full_pass = True

        # Always print — the rollup is the test report.
        print(
            render_rollup(
                rollup,
                title=f"VMCP aggregation sweep — {VMCP_NAME}",
                columns=[arm[0] for arm in _ARMS],
            )
        )

        if not any_full_pass:
            pytest.fail(
                "No model successfully invoked BOTH VMCP-exposed tools "
                "(AWS + Cloudflare doc search) end-to-end. See the "
                "rollup above for per-arm status."
            )
