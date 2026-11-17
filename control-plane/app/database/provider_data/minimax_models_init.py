# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
MiniMax Models — M2.x text family (and M2-her roleplay).

Source: https://platform.minimax.io/docs/guides/pricing-paygo   (authoritative pricing)
        https://platform.minimax.io/docs/guides/models-intro    (model descriptions)

Cost values: USD per 1M tokens.

Catalogue scope
---------------
This seed covers MiniMax's *text* models exposed at
``https://api.minimax.io/v1/chat/completions`` (OpenAI-compatible).

Each generation has two SKUs that share the same weights:
  * ``MiniMax-M2.x``           — base latency, base price
  * ``MiniMax-M2.x-highspeed`` — same weights, lower latency, 2× price

The M2-her variant is a separate role-play / multi-turn dialogue model
priced like the base M2.x line. Legacy MiniMax-M2.1 / MiniMax-M2 entries are included
because they're still callable and admins migrating from earlier
deployments will want to pin them.

Out of scope (covered by other surfaces if/when wired):
  * Speech models  (T2A — speech-2.8/2.6/02 hd|turbo)
  * Video models   (MiniMax Hailuo 2.3 / 02)
  * Music models   (Music-2.6 / Music-2.5+ / Music-2.0)
  * Image model    (image-01)

Naming
------
Bare provider IDs, case-sensitive — MiniMax's API matches case
exactly (``MiniMax-M3`` works, ``minimax-m3`` does not). The proxy
forwards ``model.model_name`` verbatim. Provider routing keys off the
LlmModel ``provider`` column ("minimax").

Pricing notes
-------------
Input prices below are the **uncached** rate. MiniMax bills cached
prompt reads at $0.06/M (M2.7) or $0.03/M (M2.5/M2.1/M2) — applied
automatically server-side; no seed change needed.

Context window
--------------
The pricing page doesn't list per-model context. The legacy MiniMax-M2
card on the docs site says "Context Length: 200k tokens, Maximum
Output: 128k tokens (including CoT)". The M2.1 / M2.5 / M2.7 family
inherits the same window (M2.7 actually larger per news posts but no
authoritative number on platform.minimax.io). Seeding 200k as a
conservative shared value; bump per-tenant if a workload needs more.
"""


def get_models():
    return [
        # =================================================================
        # M2.7 — Latest flagship (recursive self-improvement)
        # =================================================================
        # =================================================================
        # MINIMAX M3 — current flagship (refreshed 2026-08-23)
        # M2.5/M2.1/M2 are marked legacy/deprecated on the pay-as-you-go
        # page; M2-her never appeared in the official catalog. All removed.
        # =================================================================
        {
            "model_name": "MiniMax-M3",
            "parameters": 500.0,
            "name": "MiniMax M3",
            "description": "MiniMax flagship. 1M combined context; $0.30/$1.20 up to 512K input (doubles beyond); cache read $0.06.",
            "usage": "Long-context analysis, agents, high-volume intelligent chat",
            "default_max_tokens": 8192,
            "timeout": 120.0,
            "cost_per_input_token": 0.30,
            "cost_per_output_token": 1.20,
            "capabilities": {
                "context_window": 1000000,
                "modalities": ["text"],
                "supports_function_calling": True,
                "supports_prompt_caching": True,
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "cost-efficient", "long-context",
                     "large", "latest"],
        },
        {
            "model_name": "MiniMax-M3.1-Flash",
            "name": "MiniMax M3.1 Flash",
            "description": (
                "MiniMax M3.1 Flash. 1M context, 524K output; text, image and"
                " video input, tool use, thinking. USD price of the "
                "international platform."
            ),
            "usage": "Low-cost chat, agents, multimodal input",
            "default_max_tokens": 8192,
            "timeout": 120.0,
            "cost_per_input_token": 0.15,
            "cost_per_output_token": 0.6,
            "supports_vision": True,
            "capabilities": {
                "context_window": 1000000,
                "max_output_tokens": 524288,
                "modalities": ["text", "image", "video"],
                "supports_function_calling": True,
                "supports_reasoning": True,
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

    ]
