# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Moonshot AI Models — Kimi K3 family.

Source: https://platform.kimi.ai/docs/pricing       (authoritative pricing)
        https://api.moonshot.ai/v1/models           (authoritative catalogue)

Cost values: USD per 1M tokens. Input prices below are the **cache-miss**
rate; Moonshot applies the cache-hit discount automatically server-side
and reports it in the usage log.

Catalogue scope
---------------
Read on platform.kimi.ai on 2026-11-11: ``kimi-k3`` (flagship),
``kimi-k3.1-pro`` and ``kimi-k3.1-pro-highspeed``. Moonshot retires every
K2 model by the end of December 2026 and points to K3.1 Pro; the K2 line
and the older moonshot-v1 series are not seeded.

Naming
------
Bare provider IDs (no ``moonshot/`` prefix). The proxy forwards
``model.model_name`` verbatim and Moonshot 404s on prefixed names.
Provider routing keys off the ``provider`` column ("moonshot").
"""


def get_models():
    return [
        # =================================================================
        # KIMI K2.6 — Latest multimodal flagship (256K, vision + video + reasoning)
        # =================================================================
        # =================================================================
        # KIMI K3 — current flagship (platform.kimi.ai, refreshed 2026-08-23)
        # kimi-k2.5 and the whole moonshot-v1 series (incl. -auto and the
        # vision previews) sunset 2026-08-31 → kimi-k3.
        # =================================================================
        {
            "model_name": "kimi-k3",
            "parameters": 1500.0,
            "name": "Moonshot Kimi K3",
            "description": "Moonshot's flagship. Always-reasoning with effort low/high/max; 1M context. Cache-hit input $0.30/1M.",
            "usage": "Frontier reasoning, agents, long-context analysis",
            "default_max_tokens": 8192,
            "timeout": 180.0,
            "cost_per_input_token": 3.0,
            "cost_per_output_token": 15.0,
            "capabilities": {
                "context_window": 1048576,
                "modalities": ["text"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_prompt_caching": True,
                "supports_response_schema": True,
                "temperature_locked": True,
            },
            "tags": ["function-calling", "reasoning", "premium",
                     "long-context", "ultra-large", "latest"],
        },
        {
            "model_name": "kimi-k3.1-pro",
            "name": "Moonshot Kimi K3.1 Pro",
            "description": (
                "Kimi K3.1 Pro, the model Moonshot points to as it retires "
                "the K2 line by the end of December 2026. 1M context and "
                "output (max_completion_tokens defaults to 131,072); vision, "
                "always-on thinking, tools, structured output. USD price of "
                "the international platform."
            ),
            "usage": "Coding, agents, long-context analysis",
            "default_max_tokens": 8192,
            "timeout": 120.0,
            "cost_per_input_token": 2.0,
            "cost_per_output_token": 10.0,
            "supports_vision": True,
            "capabilities": {
                "context_window": 1048576,
                "max_output_tokens": 1048576,
                "modalities": ["text", "image"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_response_schema": True,
                "temperature_locked": True,
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
            "model_name": "kimi-k3.1-pro-highspeed",
            "name": "Moonshot Kimi K3.1 Pro (High-Speed)",
            "description": (
                "High-speed tier of Kimi K3.1 Pro. 1M context; vision, "
                "always-on thinking, tools, structured output. Its maximum "
                "output is not stated separately."
            ),
            "usage": "Latency-sensitive coding and agents",
            "default_max_tokens": 8192,
            "timeout": 60.0,
            "cost_per_input_token": 4.0,
            "cost_per_output_token": 20.0,
            "supports_vision": True,
            "capabilities": {
                "context_window": 1048576,
                "modalities": ["text", "image"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_response_schema": True,
                "temperature_locked": True,
            },
            "tags": [
                "function-calling",
                "vision",
                "reasoning",
                "fast-inference",
                "long-context",
                "latest",
            ],
        },
    ]
