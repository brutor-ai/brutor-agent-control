# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
xAI Models — Grok family.

Source: https://api.x.ai/v1/language-models (authoritative — verified live)
        https://docs.x.ai/docs/models

Cost values: USD per 1M tokens.

Naming
------
Model IDs are the BARE provider IDs (no "xai/" prefix). The Rust proxy
forwards ``model.model_name`` verbatim to ``api.x.ai/v1/chat/completions``;
xAI 404s on prefixed names. Provider routing is keyed off the LlmModel
``provider`` column ("xai"), not the model name.

Reasoning vs non-reasoning split
--------------------------------
xAI ships the "fast" and "4.20" tiers as paired endpoints — same
weights, different inference paths. ``-reasoning`` runs internal
chain-of-thought before answering (slower, higher quality on hard
problems); ``-non-reasoning`` skips it (cheaper latency, same per-token
price). Operators pick the variant per-use-case rather than toggling
``thinking_enabled`` because the model IDs are different on the wire.

Multi-agent
-----------
``grok-4.20-multi-agent-0309`` is rejected by ``/v1/chat/completions``
("Multi Agent requests are not allowed on chat completions"). It MUST be
invoked via xAI's Responses API at ``/v1/responses`` with an ``input``
field (not ``messages``). It's seeded with ``mode=responses`` so the
proxy routes accordingly; this requires the Rust XaiAdapter to support
the Responses path.
"""

from app.constants.llm import LLModelMode


def get_models():
    return [
        # =================================================================
        # GROK 4.20 — Latest flagship (released March 2026)
        # =================================================================
        {
            "model_name": "grok-4.20-0309-reasoning",
            "parameters": 1500.0,
            "name": "xAI Grok 4.20 (Reasoning)",
            "description": "xAI's latest flagship with reasoning. Strongest Grok for complex problem solving, math, and analysis.",
            "usage": "Complex reasoning, expert-level analysis, deep research, advanced coding",
            "default_max_tokens": 8192,
            "timeout": 180.0,
            "cost_per_input_token": 1.25,
            "cost_per_output_token": 2.5,
            "capabilities": {
                "context_window": 1000000,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "vision", "reasoning", "premium",
                     "long-context", "ultra-large", "latest"],
        },
        {
            "model_name": "grok-4.20-0309-non-reasoning",
            "parameters": 1500.0,
            "name": "xAI Grok 4.20 (Non-Reasoning)",
            "description": "Same Grok 4.20 weights, fast non-reasoning inference path. Same price, lower latency.",
            "usage": "High-volume chat, content generation, well-defined tasks",
            "default_max_tokens": 8192,
            "timeout": 60.0,
            "cost_per_input_token": 1.25,
            "cost_per_output_token": 2.5,
            "capabilities": {
                "context_window": 1000000,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "vision", "premium",
                     "long-context", "ultra-large"],
        },
        {
            "model_name": "grok-4.20-multi-agent-0309",
            "parameters": 1500.0,
            "name": "xAI Grok 4.20 Multi-Agent",
            "description": "Grok 4.20 with multi-agent orchestration. Spawns parallel sub-agents for harder tasks. Requires the Responses API (chat completions rejects it with 'Multi Agent requests are not allowed on chat completions').",
            "usage": "Multi-step research, agentic workflows, parallel sub-task decomposition",
            "mode": LLModelMode.RESPONSES.value,
            "default_max_tokens": 8192,
            "timeout": 300.0,
            "cost_per_input_token": 1.25,
            "cost_per_output_token": 2.5,
            "capabilities": {
                "context_window": 1000000,
                "modalities": ["text", "vision"],
                # Multi-agent grok DOES support function calling
                # internally (for its own sub-task orchestration) but
                # xAI gates *client-supplied* tools behind a separate
                # beta. Without that beta, the upstream returns
                # `400 Client-side tools for multi-agent models
                # require beta access` for any request that carries
                # a user-defined `tools[]` array.
                #
                # We mark the capability false so admin UI / portal /
                # router stop offering this model for tool-calling
                # workloads. Operators with xAI beta access can flip
                # this to true via the admin UI; the proxy doesn't
                # cache the flag, so the change takes effect on the
                # next request.
                "supports_function_calling": False,
                "supports_reasoning": True,
                "supports_response_schema": True,
                "supports_responses": True,
                # Hard requirement: this model can only be served via
                # /v1/responses; the proxy must auto-route any chat
                # completion request for it to the Responses path.
                "requires_responses_api": True,
            },
            "tags": ["function-calling", "vision", "reasoning", "agentic",
                     "long-context", "ultra-large", "premium", "responses-api",
                     "responses-only"],
        },

        # =================================================================
        # GROK 4.1 FAST — Cheap, fast, 2M context (most popular per usage)
        # =================================================================
        # =================================================================
        # GROK 4.x CURRENT LINE — post-May-2026 catalog
        # The fast/4-0709/3 era was retired 2026-05-15 (slugs redirect to
        # grok-4.3 and bill at its rates); grok-code-fast-1 became
        # grok-build-0.1. Refreshed 2026-08-23 from docs.x.ai/docs/models.
        # =================================================================
        {
            "model_name": "grok-4.6",
            "parameters": 1800.0,
            "name": "xAI Grok 4.6",
            "description": "Newest Grok flagship. 500K context; tiered pricing doubles above 200K prompt tokens.",
            "usage": "Frontier chat, analysis, coding",
            "default_max_tokens": 8192,
            "timeout": 120.0,
            "cost_per_input_token": 2.0,
            "cost_per_output_token": 6.0,
            "capabilities": {
                "context_window": 500000,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "vision", "premium",
                     "long-context", "ultra-large", "latest"],
        },
        {
            "model_name": "grok-4.5",
            "parameters": 1600.0,
            "name": "xAI Grok 4.5",
            "description": "xAI's recommended general-purpose + coding model. 500K context; same tiered pricing as 4.6.",
            "usage": "General chat, coding, production workloads",
            "default_max_tokens": 8192,
            "timeout": 120.0,
            "cost_per_input_token": 2.0,
            "cost_per_output_token": 6.0,
            "capabilities": {
                "context_window": 500000,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "vision", "long-context",
                     "ultra-large"],
        },
        {
            "model_name": "grok-4.3",
            "parameters": 1200.0,
            "name": "xAI Grok 4.3",
            "description": "Value tier with a reasoning-effort parameter (none→high) and 1M context. The redirect target for every retired pre-4.3 slug.",
            "usage": "Cost-efficient reasoning, high-volume chat, migration target for legacy pins",
            "default_max_tokens": 8192,
            "timeout": 120.0,
            "cost_per_input_token": 1.25,
            "cost_per_output_token": 2.50,
            "capabilities": {
                "context_window": 1000000,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "vision", "reasoning",
                     "cost-efficient", "long-context", "large"],
        },
        {
            "model_name": "grok-build-0.1",
            "parameters": 300.0,
            "name": "xAI Grok Build 0.1",
            "description": "Agentic coding model — the grok-code-fast-1 successor. 256K context.",
            "usage": "Coding agents, IDE integrations, repo-scale edits",
            "default_max_tokens": 8192,
            "timeout": 120.0,
            "cost_per_input_token": 1.0,
            "cost_per_output_token": 2.0,
            "capabilities": {
                "context_window": 256000,
                "modalities": ["text"],
                "supports_function_calling": True,
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "coding", "cost-efficient",
                     "long-context", "medium"],
        },
    ]
