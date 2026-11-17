# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
DeepSeek Models — DeepSeek V4 family.

Source: https://api-docs.deepseek.com/quick_start/pricing/
        https://api.deepseek.com/v1/models  (authoritative — verified live)

Cost values: USD per 1M tokens.

Catalogue scope
---------------
DeepSeek's *direct* API (pricing page and changelog, read 2026-11-11):

  * ``deepseek-flash``   — DeepSeek V4.1 Flash: 1M context, vision, thinking
                           on by default
  * ``deepseek-v4-pro``  — premium tier, 1M context, thinking-mode capable

The V4 Flash model was retired on 2026-09-10 (its name is routed to V4.1
Flash for now, with no cut-off); it and its experimental vision variant
are no longer seeded. Older variants (DeepSeek V3, R1 etc.) are not
addressable via api.deepseek.com; they live on third-party platforms and
are seeded in those provider files.

Naming
------
Model IDs are bare provider IDs (no "deepseek/" prefix). The proxy
forwards ``model.model_name`` verbatim to ``api.deepseek.com``;
prefixed names 400 with "Model Not Exist". Provider routing keys off
the LlmModel ``provider`` column ("deepseek").

Pricing notes
-------------
* Input prices below are the **cache-miss** rate (the worst-case rate
  charged when the prompt prefix isn't in DeepSeek's KV cache). DeepSeek
  applies the cache-hit discount automatically server-side; admins see
  the saving in usage logs without any seed change.

* ``deepseek-v4-pro`` is in a 75% promotional discount window until
  2026-05-05 15:59 UTC (effective $0.435 / $0.87). We seed the
  *post-discount regular* prices ($1.74 / $3.48) so usage tracking is
  correct from May 5 onwards without anyone editing the seed. Tenants
  who want exact discount-period costs can override the prices on the
  per-tenant model row.
"""


def get_models():
    return [
        # =================================================================
        # CANONICAL V4 — released 2026 (current generation)
        # =================================================================
        {
            "model_name": "deepseek-flash",
            "name": "DeepSeek V4.1 Flash",
            "description": (
                "DeepSeek V4.1 Flash. 1M context, 384K output; vision, "
                "thinking (on by default), tool calls, JSON output. Seeded at"
                " the list (peak) price; off-peak is $0.15 / $0.60 per 1M."
            ),
            "usage": "Low-cost chat, coding, agents",
            "default_max_tokens": 8192,
            "timeout": 120.0,
            "cost_per_input_token": 0.3,
            "cost_per_output_token": 1.2,
            "supports_vision": True,
            "capabilities": {
                "context_window": 1000000,
                "max_output_tokens": 384000,
                "modalities": ["text", "image"],
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
                "long-context",
                "latest",
            ],
        },
        {
            "model_name": "deepseek-v4-pro",
            "parameters": 671.0,
            "name": "DeepSeek V4 Pro",
            "description": (
                "DeepSeek's V4 Pro reasoning model. 1M context, 384K output. "
                "Seeded at the list (peak) price."
            ),
            "usage": "Complex reasoning, expert-level analysis, long-running coding agents, deep research",
            "default_max_tokens": 8192,
            "timeout": 300.0,
            "cost_per_input_token": 1.32,
            "cost_per_output_token": 3.96,
            "capabilities": {
                "context_window": 1000000,
                "modalities": ["text"],
                "supports_function_calling": True,
                "supports_response_schema": True,
                "supports_prompt_caching": True,
                "supports_reasoning": True,
                "max_output_tokens": 384000,
            },
            "tags": ["function-calling", "reasoning", "premium",
                     "long-context", "ultra-large", "latest"],
        },

        # =================================================================
        # DeepSeek V4.1 Flash and V4 Pro.
        # Prices are the PEAK-window rates; off-peak is half (peak =
        # 01:00-04:00 & 06:00-10:00 UTC Mon-Fri).
        # =================================================================
    ]
