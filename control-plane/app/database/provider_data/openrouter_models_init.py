# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
OpenRouter Models — one key, every major model, OpenAI-compatible.

Source: https://openrouter.ai/docs/quickstart
        https://openrouter.ai/docs/features/model-routing
        https://openrouter.ai/docs/use-cases/byok
Cost values: USD per 1M tokens. Catalogue refreshed 2026-08-23.

Why this seed exists
--------------------
OpenRouter is the most widely used model aggregator; many teams already
route everything through it. Seeding it PRECONFIGURED AND ENABLED means
a developer can put Brutor in front of OpenRouter with nothing but an
API key — keeping their existing model-routing setup while gaining AI
System assurance, governance and the run ledger on every call.

Wire facts
----------
* Base URL ``https://openrouter.ai/api/v1``; OpenAI-format
  ``/chat/completions``; single required header
  ``Authorization: Bearer <key>``.
* Model IDs are ``vendor/model`` slugs passed through verbatim, plus the
  self-updating ``~vendor/...-latest`` aliases.
* ``openrouter/auto`` adds NO fee — each request bills at the routed
  model's standard rate. Its seeded per-token cost is a clearly-marked
  blended ESTIMATE (approximate spend beats the invisible $0 spend a
  zero seed produced); hard cost ceilings on the auto router belong to
  Brutor budgets/envelopes, not list prices.
* BYOK: 5% fee above $25k/mo list-price inference (waived below).
* No formal deprecations page — providers set ``deprecation_date`` per
  endpoint and OpenRouter surfaces warnings; the ``models`` fallback
  array and ``~latest`` aliases are the mitigation.

Curation
--------
``openrouter/auto`` is the ENABLED default (the whole point of the
aggregator). A handful of stable enterprise pins follow, disabled, for
operators who want fixed slugs with real list prices on the card.
"""


def get_models():
    return [
        # =================================================================
        # AUTO ROUTER — the preconfigured default
        # =================================================================
        {
            "model_name": "openrouter/auto",
            "parameters": 0.0,
            "name": "OpenRouter Auto Router",
            "description": "OpenRouter's task-classifying auto-router. No routing fee — every request bills at the routed model's standard rate; spend is captured by usage accounting rather than list price. Supports allowed/excluded model patterns and cost tiers.",
            "usage": "Drop-in front for teams already routing via OpenRouter; keep their routing, add Brutor assurance",
            "enabled": True,
            "default_max_tokens": 8192,
            "timeout": 180.0,
            # ESTIMATE — the auto-router has no list price of its own
            # (each request bills at the routed model's standard rate).
            # Seeding $0 made OpenRouter traffic invisible to cost
            # accounting, so we seed a traffic-weighted mid-tier blend
            # (between Sonnet-5 $2/$10 and cheap-tier routing) instead:
            # approximate spend beats zero spend. Operators who pin real
            # models get exact list prices; ceilings on the auto-router
            # belong to Brutor budgets/envelopes.
            "cost_per_input_token": 1.50,
            "cost_per_output_token": 8.00,
            "capabilities": {
                "context_window": 2000000,
                "modalities": ["text"],
                "supports_function_calling": True,
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "aggregator", "auto-routing",
                     "long-context", "latest"],
        },

        # =================================================================
        # STABLE ENTERPRISE PINS — fixed slugs, real list prices
        # =================================================================
        {
            "model_name": "anthropic/claude-sonnet-5",
            "parameters": 800.0,
            "name": "Claude Sonnet 5 (via OpenRouter)",
            "description": "Anthropic Claude Sonnet 5 through OpenRouter. Same $2/$10 list price as direct.",
            "usage": "Pinned production workhorse via the aggregator",
            "default_max_tokens": 8192,
            "timeout": 120.0,
            "cost_per_input_token": 2.0,
            "cost_per_output_token": 10.0,
            "capabilities": {
                "context_window": 1000000,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "vision", "reasoning",
                     "aggregator", "long-context", "large"],
        },
        {
            "model_name": "anthropic/claude-opus-5",
            "parameters": 2000.0,
            "name": "Claude Opus 5 (via OpenRouter)",
            "description": "Anthropic Claude Opus 5 through OpenRouter at the direct $5/$25 rate.",
            "usage": "Pinned frontier tier via the aggregator",
            "default_max_tokens": 8192,
            "timeout": 180.0,
            "cost_per_input_token": 5.0,
            "cost_per_output_token": 25.0,
            "capabilities": {
                "context_window": 1000000,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "vision", "reasoning",
                     "aggregator", "premium", "long-context",
                     "ultra-large"],
        },
        {
            "model_name": "openai/gpt-5.2",
            "parameters": 1000.0,
            "name": "GPT-5.2 (via OpenRouter)",
            "description": "OpenAI GPT-5.2 through OpenRouter at the direct $1.75/$14 rate.",
            "usage": "Pinned OpenAI tier via the aggregator",
            "default_max_tokens": 8192,
            "timeout": 120.0,
            "cost_per_input_token": 1.75,
            "cost_per_output_token": 14.0,
            "capabilities": {
                "context_window": 400000,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "vision", "reasoning",
                     "aggregator", "long-context", "large"],
        },
        {
            "model_name": "google/gemini-3.8-flash",
            "name": "Gemini 3.8 Flash (via OpenRouter)",
            "description": (
                "Google Gemini 3.8 Flash through OpenRouter. 1M context, 65K "
                "output; tools, reasoning, structured output; image, audio "
                "and video input. Seeded at the price OpenRouter's API "
                "returns today; it does not say whether that is promotional "
                "(Google's own price doubles on 2027-01-01)."
            ),
            "usage": "Production chat and multimodal analysis via OpenRouter",
            "default_max_tokens": 8192,
            "timeout": 120.0,
            "cost_per_input_token": 0.825,
            "cost_per_output_token": 4.125,
            "supports_vision": True,
            "capabilities": {
                "context_window": 1048576,
                "max_output_tokens": 65536,
                "modalities": ["text", "image", "audio", "video"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_response_schema": True,
            },
            "tags": [
                "function-calling",
                "vision",
                "reasoning",
                "multimodal",
                "long-context",
            ],
        },
        {
            "model_name": "anthropic/claude-haiku-5.5",
            "name": "Claude Haiku 5.5 (via OpenRouter)",
            "description": (
                "Anthropic Claude Haiku 5.5 through OpenRouter. 1M context, "
                "128K output; tools, reasoning, structured output, image "
                "input. The price matches Anthropic's for prompts up to "
                "100,000 tokens."
            ),
            "usage": "High-volume chat, classification, extraction via OpenRouter",
            "default_max_tokens": 8192,
            "timeout": 60.0,
            "cost_per_input_token": 0.1,
            "cost_per_output_token": 0.5,
            "supports_vision": True,
            "capabilities": {
                "context_window": 1000000,
                "max_output_tokens": 128000,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_response_schema": True,
            },
            "tags": [
                "function-calling",
                "vision",
                "reasoning",
                "cheap",
                "cost-efficient",
            ],
        },
    ]
