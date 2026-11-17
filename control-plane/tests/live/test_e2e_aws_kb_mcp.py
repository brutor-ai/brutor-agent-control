# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Live end-to-end MCP tool-call sweep against the AWS Knowledge Base
server.

The existing ``test_e2e_smoke.py::TestHelloWorldChat`` exercises a
Brutor-internal agent skill MCP — it round-trips inside the gateway
and proves the agent-skill MCP path works. This suite is the
counterpart for EXTERNAL MCP servers: an unauthenticated public
MCP (AWS Knowledge Base) that the trial bundle ships in the
catalog. The session fixture installs it from the MCP catalog when
missing and binds it to the E2ETests resource group.

Flow per model:
  1. Build a tool def for ``aws___search_documentation`` from the
     AWS KB server's live ``tools/list``.
  2. Send a chat completion with that tool exposed + a user prompt
     that obviously wants AWS docs (e.g. "search AWS docs for S3
     bucket versioning").
  3. Assert the model emitted a tool_call naming the AWS tool.
  4. Execute the tool_call via ``/v1/proxy/mcp/{aws-kb-id}``.
  5. Assert non-empty result content.

The suite is opt-in: ``pytest -m live_e2e tests/live/test_e2e_aws_kb_mcp.py -v``.

Pass criterion: at least one model produces a fully-successful
triplet (chat OK → tool_call → MCP execute). Per-model errors are
captured in a rollup table — a single flaky model doesn't starve
the rest.
"""

from __future__ import annotations

from typing import Any, Dict, List

import httpx
import pytest

from ._mcp_helpers import (
    AWS_KB_SEARCH_TOOL,
    aws_kb_server_id,  # noqa: F401  (fixture re-import for collection)
    build_tool_def_from_mcp_tool,
    call_mcp_tool,
    chat_with_tools,
    extract_tool_call,
    mcp_tools_list,
    parse_tool_args,
    render_rollup,
    short,
)
from .test_e2e_smoke import _list_trial_chat_models


# Prompt that any reasonable model should resolve to a single
# ``aws___search_documentation`` tool call. Wording is deliberate:
#   * "search AWS docs for" → cues the tool name.
#   * specific query string → forces a non-empty argument.
#   * "use the tool" → reduces the chance a model decides to
#     answer from prior knowledge instead of calling.
_USER_PROMPT = (
    "Search AWS documentation for: how do I enable versioning on an "
    "Amazon S3 bucket? Use the search tool — do not answer from "
    "memory."
)

_SYSTEM_PROMPT = (
    "You are a helpful assistant. You have an "
    "`aws___search_documentation` tool that queries the official "
    "AWS docs. When the user asks anything about AWS services, "
    "configuration, errors, or best practices, you MUST call that "
    "tool to retrieve current documentation before answering."
)


@pytest.mark.live_e2e
class TestAwsKbMcpToolCall:
    """Per-model AWS KB MCP tool invocation."""

    def test_each_chat_model_invokes_aws_kb_search(
        self,
        proxy_client: httpx.Client,
        trial_headers: Dict[str, str],
        aws_kb_server_id: str,
    ) -> None:
        # Fetch the live tool list from the AWS KB MCP server and
        # locate the search tool. The catalog publishes the tool
        # def with a hefty description + JSON schema; we use those
        # verbatim so the model sees exactly what the portal sees.
        tools = mcp_tools_list(proxy_client, trial_headers, aws_kb_server_id)
        aws_search_tool = next(
            (t for t in tools if t.get("name") == AWS_KB_SEARCH_TOOL),
            None,
        )
        if not aws_search_tool:
            tool_names = [t.get("name") for t in tools]
            pytest.fail(
                f"{AWS_KB_SEARCH_TOOL!r} not present in AWS KB tools/list "
                f"response. Server reported: {tool_names!r}. The AWS KB "
                "MCP server may be in a degraded health state — check "
                "its /v1/proxy/mcp/{id} `initialize` response."
            )
        tool_def = build_tool_def_from_mcp_tool(aws_search_tool)

        # Per-model loop — same harness as the hello-world sweep.
        candidates, preferred_group = _list_trial_chat_models(
            proxy_client, trial_headers,
        )
        if not candidates:
            pytest.fail(
                "trial user has no chat-capable models. Enable + key at "
                "least one provider in the admin UI before running the "
                "live e2e sweep."
            )

        chat_headers = dict(trial_headers)
        if preferred_group:
            chat_headers["X-Resource-Group"] = preferred_group

        # Per-model rollup: chat / tool_call detected / MCP execute.
        rollup: Dict[str, Dict[str, str]] = {}
        any_full_triplet = False

        for entry in candidates:
            name = entry["name"]
            row = rollup.setdefault(
                name, {"chat": "—", "tool_call": "—", "mcp_call": "—"}
            )
            if not entry.get("supports_function_calling"):
                # Deliberate skip, not a failure: the configured model's
                # supports_function_calling is off.
                row["chat"] = "skip: model does not support tool calling"
                continue

            # 1. Chat completion with the AWS search tool.
            attempt = chat_with_tools(
                proxy_client,
                chat_headers,
                entry,
                tool_defs=[tool_def],
                system_prompt=_SYSTEM_PROMPT,
                user_prompt=_USER_PROMPT,
            )
            if not attempt.succeeded:
                row["chat"] = f"FAIL {attempt.status or 'tx'}"
                continue
            row["chat"] = "ok"

            # 2. Did the model actually call the tool? Some small /
            #    heavy-reasoning models will respond in plain text
            #    even with `tool_choice=auto`.
            call = extract_tool_call(attempt.msg or {}, AWS_KB_SEARCH_TOOL)
            if not call:
                row["tool_call"] = "no_call"
                continue
            row["tool_call"] = "ok"

            # 3. Execute the tool the way the portal would.
            args = parse_tool_args(call)
            # AWS KB requires a `search_phrase` — if the model
            # emitted args without it, substitute a sensible default
            # so the test doesn't fail on a model arg-shape quirk.
            if not args.get("search_phrase"):
                args = {**args, "search_phrase": "S3 bucket versioning"}
            ok, detail = call_mcp_tool(
                proxy_client,
                trial_headers,
                aws_kb_server_id,
                tool_name=AWS_KB_SEARCH_TOOL,
                arguments=args,
                proxy_path="mcp",
            )
            if not ok:
                row["mcp_call"] = f"FAIL {short(detail, 30)}"
                continue
            row["mcp_call"] = "ok"
            any_full_triplet = True

        # Always print the rollup — successful runs benefit from the
        # per-model view (which models actually called the tool vs
        # which preferred to monologue).
        print(
            render_rollup(
                rollup,
                title="AWS KB MCP tool-call sweep",
                columns=["chat", "tool_call", "mcp_call"],
            )
        )

        if not any_full_triplet:
            pytest.fail(
                "No model completed the full triplet (chat OK → tool_call "
                "→ MCP execute OK) against the AWS Knowledge Base MCP "
                "server. See the rollup above for per-model status."
            )
