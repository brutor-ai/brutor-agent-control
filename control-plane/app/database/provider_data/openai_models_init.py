# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
OpenAI Models — Chat, Responses, and Completion modes.

Cost values: USD per 1M tokens.

Model prefix: openai/ (auto-discovered, no prefix needed for standard models)
"""

from datetime import date

from app.constants.llm import LLModelMode


def get_models():
    return [
        # =================================================================
        # GPT-5 FAMILY — Flagship models
        # =================================================================
        # =================================================================
        # GPT-5.6 LINE — current frontier (refreshed 2026-08-23)
        # Prices and limits re-read 2026-11-11.
        # =================================================================
        {
            "model_name": "gpt-5.6-sol",
            "parameters": 3000.0,
            "name": "OpenAI GPT-5.6 Sol",
            "description": (
                "OpenAI's GPT-5.6 flagship. 1.05M context, 128K output. "
                "Seeded at the regular price ($5 / $30 per 1M), derived from "
                "OpenAI's stated reductions: the promotional $4 / $20 runs "
                "until 2026-11-30."
            ),
            "usage": "Frontier reasoning, hardest coding, long-context analysis",
            "default_max_tokens": 8192,
            "timeout": 300.0,
            "cost_per_input_token": 5.0,
            "cost_per_output_token": 30.0,
            "capabilities": {
                "context_window": 1050000,
                "max_output_tokens": 128000,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_prompt_caching": True,
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "vision", "reasoning", "premium",
                     "long-context", "ultra-large", "latest"],
        },
        {
            "model_name": "gpt-5.6-terra",
            "parameters": 1200.0,
            "name": "OpenAI GPT-5.6 Terra",
            "description": "Mid-tier GPT-5.6. 1.05M context, 128K output.",
            "usage": "Production chat, coding, agents at mid-tier cost",
            "default_max_tokens": 8192,
            "timeout": 120.0,
            "cost_per_input_token": 2.0,
            "cost_per_output_token": 12.0,
            "capabilities": {
                "context_window": 1050000,
                "max_output_tokens": 128000,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_prompt_caching": True,
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "vision", "reasoning",
                     "long-context", "large", "latest"],
        },
        {
            "model_name": "gpt-5.6-luna",
            "parameters": 200.0,
            "name": "OpenAI GPT-5.6 Luna",
            "description": "Small GPT-5.6. 1.05M context, 128K output, reasoning tokens.",
            "usage": "High-volume classification, extraction, cheap chat",
            "default_max_tokens": 8192,
            "timeout": 60.0,
            "cost_per_input_token": 0.20,
            "cost_per_output_token": 1.20,
            "capabilities": {
                "context_window": 1050000,
                "max_output_tokens": 128000,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_prompt_caching": True,
                "supports_response_schema": True,
                "supports_reasoning": True,
            },
            "tags": ["function-calling", "vision", "cheap",
                     "cost-efficient", "long-context", "small", "latest"],
        },
        {
            "model_name": "gpt-6-astra",
            "name": "OpenAI GPT-6 Astra",
            "description": (
                "GPT-6 Astra, OpenAI's flagship and default recommendation. "
                "1.05M context, 128K output. Tool calling requires the "
                "Responses API. Reasoning effort low to max, no \"none\". "
                "Cached input $1.00 per 1M."
            ),
            "usage": "Hardest reasoning, research, long-running agents",
            "mode": LLModelMode.RESPONSES.value,
            "cost_per_input_token": 10.0,
            "cost_per_output_token": 50.0,
            "supports_vision": True,
            "capabilities": {
                "context_window": 1050000,
                "max_output_tokens": 128000,
                "modalities": ["text", "image"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_prompt_caching": True,
                "supports_response_schema": True,
                "supports_responses": True,
                "requires_responses_api": True,
            },
            "tags": [
                "function-calling",
                "vision",
                "reasoning",
                "flagship",
                "premium",
                "long-context",
                "latest",
            ],
        },
        {
            "model_name": "gpt-6.1-sol",
            "name": "OpenAI GPT-6.1 Sol",
            "description": (
                "GPT-6.1 Sol, the newer Sol: OpenAI's recommendation to "
                "balance intelligence and cost. 1.05M context, 128K output. "
                "Tool calling requires the Responses API (Chat Completions is"
                " supported without tools). Reasoning effort low to max, no "
                "\"none\", so temperature is never accepted. Cached input $0.10"
                " per 1M."
            ),
            "usage": "Production agents and coding on the Responses API",
            "mode": LLModelMode.RESPONSES.value,
            "cost_per_input_token": 2.0,
            "cost_per_output_token": 10.0,
            "supports_vision": True,
            "capabilities": {
                "context_window": 1050000,
                "max_output_tokens": 128000,
                "modalities": ["text", "image"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_prompt_caching": True,
                "supports_response_schema": True,
                "supports_responses": True,
                "requires_responses_api": True,
            },
            "tags": [
                "function-calling",
                "vision",
                "reasoning",
                "long-context",
                "latest",
            ],
        },
        {
            "model_name": "gpt-6-sol",
            "name": "OpenAI GPT-6 Sol",
            "description": (
                "GPT-6 Sol. 1.05M context (922K input), 128K output. Chat "
                "Completions, Responses and Batch. Reasoning effort "
                "none/low/medium (default)/high/xhigh/max. On Chat "
                "Completions function calling works only with "
                "reasoning_effort \"none\". OpenAI's named replacement for "
                "gpt-5.3-codex and gpt-5.1. Above 272K input tokens the price"
                " is 2x input / 1.5x output; cached input $0.20 per 1M."
            ),
            "usage": "Production chat, coding, agents",
            "mode": LLModelMode.CHAT.value,
            "cost_per_input_token": 2.0,
            "cost_per_output_token": 10.0,
            "supports_vision": True,
            "capabilities": {
                "context_window": 1050000,
                "max_output_tokens": 128000,
                "modalities": ["text", "image"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                # Chat Completions takes function tools only with
                # reasoning_effort "none"; the gateway reads this.
                "tools_require_reasoning_effort_none": True,
                "supports_prompt_caching": True,
                "supports_response_schema": True,
            },
            "tags": [
                "function-calling",
                "vision",
                "reasoning",
                "long-context",
                "latest",
            ],
        },
        {
            "model_name": "gpt-5.4-mini",
            "parameters": 400.0,
            "name": "OpenAI GPT-5.4 Mini",
            "description": "Mini tier of the 5.4 line.",
            "usage": "Cost-efficient production chat and tooling",
            "default_max_tokens": 8192,
            "timeout": 60.0,
            "cost_per_input_token": 0.75,
            "cost_per_output_token": 4.50,
            "capabilities": {
                "context_window": 400000,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_prompt_caching": True,
                "supports_response_schema": True,
                "supports_reasoning": True,
                "max_output_tokens": 128000,
            },
            "tags": ["function-calling", "vision", "cost-efficient",
                     "long-context", "medium"],
        },
        {
            "model_name": "gpt-6-luna",
            "name": "OpenAI GPT-6 Luna",
            "description": (
                "GPT-6 Luna, the small GPT-6. 1.05M context (922K input), "
                "128K output. Chat Completions, Responses and Batch. "
                "Reasoning default medium. On Chat Completions function "
                "calling works only with reasoning_effort \"none\". OpenAI's "
                "named replacement for gpt-5.4-nano, gpt-4.1, gpt-4.1-mini, "
                "gpt-4o and gpt-4o-mini. Cached input $0.01 per 1M."
            ),
            "usage": "High-volume chat, classification, extraction",
            "mode": LLModelMode.CHAT.value,
            "cost_per_input_token": 0.1,
            "cost_per_output_token": 0.5,
            "supports_vision": True,
            "capabilities": {
                "context_window": 1050000,
                "max_output_tokens": 128000,
                "modalities": ["text", "image"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                # Chat Completions takes function tools only with
                # reasoning_effort "none"; the gateway reads this.
                "tools_require_reasoning_effort_none": True,
                "supports_prompt_caching": True,
                "supports_response_schema": True,
            },
            "tags": [
                "function-calling",
                "vision",
                "reasoning",
                "cheap",
                "cost-efficient",
                "long-context",
                "latest",
            ],
        },
        {
            "model_name": "gpt-5.5",
            "name": "OpenAI GPT-5.5",
            "description": (
                "OpenAI's frontier model for the most complex professional work — "
                "code, research, data analysis, document creation, and long-running "
                "agent workflows. Released April 23, 2026; first fully retrained "
                "base model since GPT-4.5. 1M-token context window.\n\n"
                "Routed via the Responses API (POST /v1/proxy/llm/responses). "
                "Although gpt-5.5 *can* be addressed via /v1/chat/completions for "
                "plain prompts, OpenAI rejects the combination of "
                "`reasoning_effort` + function tools on chat with HTTP 400 — and "
                "since this is a reasoning model with function calling enabled, "
                "the failure surface on chat is large. Forcing Responses removes "
                "the edge case."
            ),
            "usage": "Complex professional work, agentic coding, research, deep analysis, multi-tool workflows",
            "mode": LLModelMode.RESPONSES.value,
            "enabled": True,
            "parameters": 2500.0,
            "supports_vision": True,
            "default_max_tokens": 16384,
            "timeout": 300.0,
            "cost_per_input_token": 5.0,
            "cost_per_output_token": 30.0,
            "capabilities": {
                "context_window": 1050000,
                "modalities": ["text", "image"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_responses": True,
                "supports_response_schema": True,
                "supports_prompt_caching": True,
                "supports_system_messages": True,
                "requires_responses_api": True,
                "max_output_tokens": 128000,
            },
            "tags": ["function-calling", "vision", "reasoning", "flagship",
                     "long-context", "ultra-large", "latest"],
        },
        {
            "model_name": "gpt-5.5-pro",
            "name": "OpenAI GPT-5.5 Pro",
            "description": (
                "Premium GPT-5.5 variant with maximum reasoning depth. Best for "
                "the hardest problems. Reasoning-only — requires the OpenAI "
                "Responses API (POST /v1/proxy/llm/responses); the Chat "
                "Completions endpoint returns 404 'not a chat model'. "
                "Restricted-tier: requires a Pro-tier OpenAI API key."
            ),
            "usage": "Research, deep analysis, complex mathematics, expert-level coding",
            "mode": LLModelMode.RESPONSES.value,
            "parameters": 2500.0,
            "supports_vision": True,
            "default_max_tokens": 16384,
            "timeout": 300.0,
            "cost_per_input_token": 30.0,
            "cost_per_output_token": 180.0,
            "capabilities": {
                "context_window": 1050000,
                "modalities": ["text", "image"],
                "supports_reasoning": True,
                "supports_responses": True,
                "supports_response_schema": True,
                "supports_prompt_caching": True,
                "restricted_tier": True,
                "required_tier": "Pro",
                "requires_responses_api": True,
                "max_output_tokens": 128000,
            },
            "tags": ["function-calling", "vision", "reasoning", "premium",
                     "long-context", "ultra-large", "restricted-tier", "latest"],
        },
        {
            "model_name": "gpt-5.2",
            "name": "OpenAI GPT-5.2 Auto-Routing",
            "description": "Latest flagship GPT model. OpenAI dynamically routes your request to one of the GPT-5.2 family members, such as: a fast general model (gpt-5.2-instant), a deeper reasoning model (gpt-5.2-thinking), a higher-accuracy model (gpt-5.2-pro). Best for complex reasoning, analysis, and creative tasks.",
            "usage": "Complex analysis, creative writing, multi-step reasoning, coding, research",
            "mode": LLModelMode.CHAT.value,
            "enabled": True,
            "parameters": 2000.0,
            "supports_vision": True,
            "default_max_tokens": 16384,
            "cost_per_input_token": 1.75,
            "cost_per_output_token": 14.0,
            "capabilities": {
                "context_window": 400000,
                "modalities": ["text", "image"],
                "supports_reasoning": True,
                "supports_responses": True,
                "supports_response_schema": True,
                "supports_prompt_caching": True,
                "supports_system_messages": True,
                "max_output_tokens": 128000,
            },
            # gpt-5.x is a native-reasoning family — OpenAI rejects
            # `temperature` / `top_p` / `presence_penalty` /
            # `frequency_penalty` on every call. The Rust adapter
            # strips them at runtime (see
            # `services/llm_adapter/openai.rs::transform_request`)
            # but logs a WARN per call when they slip through. Listing
            # them here means the proxy strips them BEFORE the
            # adapter, silences the per-request warning, and matches
            # the convention used by the other gpt-5.2 variants
            # (gpt-5.2-pro / -codex / etc.) further down this file.
            "additional_drop_params": ["temperature", "top_p", "presence_penalty",
                                       "frequency_penalty"],
            "tags": ["function-calling", "vision", "reasoning", "flagship",
                     "long-context", "ultra-large"],
        },
        {
            "model_name": "gpt-5.2-pro",
            "parameters": 2000.0,
            "name": "OpenAI GPT-5.2 Pro",
            "description": (
                "Premium GPT-5.2 variant with maximum reasoning depth. Best for the hardest problems. "
                "Reasoning-only — requires the OpenAI Responses API "
                "(POST /v1/proxy/responses); the Chat Completions "
                "endpoint returns 404 'not a chat model'. "
                "Restricted-tier: requires a Pro-tier OpenAI API key."
            ),
            "usage": "Research, deep analysis, complex mathematics, expert-level coding",
            "mode": LLModelMode.RESPONSES.value,
            "supports_vision": True,
            "default_max_tokens": 16384,
            "timeout": 300.0,
            "cost_per_input_token": 21.0,
            "cost_per_output_token": 168.0,
            "capabilities": {
                "context_window": 400000,
                "modalities": ["text", "image"],
                "supports_reasoning": True,
                "supports_responses": True,
                "supports_response_schema": True,
                "supports_prompt_caching": True,
                "restricted_tier": True,
                "required_tier": "Pro",
                "max_output_tokens": 128000,
            },
            "tags": ["function-calling", "vision", "reasoning", "premium",
                     "long-context", "ultra-large", "restricted-tier"],
        },
        {
            "model_name": "gpt-5.4",
            "parameters": 2000.0,
            "name": "OpenAI GPT-5.4",
            "description": (
                "Latest-generation flagship model. Best quality across all "
                "tasks. Routed via the Responses API — same restriction as "
                "gpt-5.5: chat/completions rejects the combination of "
                "function tools + reasoning_effort with HTTP 400, and this "
                "is a reasoning model with function calling on by default. "
                "Use POST /v1/proxy/llm/responses."
            ),
            "usage": "Complex reasoning, analysis, creative writing, coding, research",
            "mode": LLModelMode.RESPONSES.value,
            "supports_vision": True,
            "default_max_tokens": 16384,
            "timeout": 300.0,
            "cost_per_input_token": 2.5,
            "cost_per_output_token": 15.0,
            "capabilities": {
                "context_window": 1050000,
                "modalities": ["text", "image"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_responses": True,
                "supports_response_schema": True,
                "supports_prompt_caching": True,
                "requires_responses_api": True,
                "max_output_tokens": 128000,
            },
            "tags": ["function-calling", "vision", "reasoning", "flagship",
                     "long-context", "ultra-large"],
        },
        {
            "model_name": "gpt-5.4-pro",
            "parameters": 2000.0,
            "name": "OpenAI GPT-5.4 Pro",
            "description": (
                "Premium GPT-5.4 variant with maximum reasoning depth for the hardest problems. "
                "Reasoning-only — requires the OpenAI Responses API "
                "(POST /v1/proxy/responses); the Chat Completions "
                "endpoint returns 404 'not a chat model'. "
                "Restricted-tier: requires a Pro-tier OpenAI API key."
            ),
            "usage": "Research, deep analysis, complex mathematics, expert-level coding",
            "mode": LLModelMode.RESPONSES.value,
            "supports_vision": True,
            "default_max_tokens": 16384,
            "timeout": 300.0,
            "cost_per_input_token": 30.0,
            "cost_per_output_token": 180.0,
            "capabilities": {
                "context_window": 1050000,
                "modalities": ["text", "image"],
                "supports_reasoning": True,
                "supports_responses": True,
                "supports_response_schema": True,
                "supports_prompt_caching": True,
                "restricted_tier": True,
                "required_tier": "Pro",
                "max_output_tokens": 128000,
            },
            "tags": ["function-calling", "vision", "reasoning", "premium",
                     "long-context", "ultra-large", "restricted-tier"],
        },

        # =================================================================
        # GPT-4 FAMILY — Workhorse models
        # =================================================================

        # =================================================================
        # O-SERIES — Reasoning models
        # =================================================================

        # =================================================================
        # CODEX — Agentic coding models (Responses API native)
        # =================================================================

        # =================================================================
        # LEGACY TEXT COMPLETION
        # =================================================================
    ]
