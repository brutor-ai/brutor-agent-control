# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Anthropic Models — Claude family.

Source: https://platform.claude.com/docs/en/about-claude/models/overview
        https://platform.claude.com/docs/en/about-claude/pricing
        https://platform.claude.com/docs/en/about-claude/model-deprecations
Cost values: USD per 1M tokens. Catalogue refreshed 2026-08-23.

Retired IDs are NOT seeded — requests to them fail at the provider:
  * claude-opus-4-1-20250805     (retired 2026-08-05 → claude-opus-4-8)
  * claude-sonnet-4-20250514     (retired 2026-06-15 → claude-sonnet-4-6)
  * claude-opus-4-20250514       (retired 2026-06-15 → claude-opus-4-8)

Pricing notes
-------------
* Sonnet 5's $2/$10 introductory pricing was made permanent (the
  announced Sep 2026 increase to $3/$15 was cancelled).
* Batch API is 50% off across the family; cache reads are ~10% of the
  input rate. Neither is modelled here — these are the on-demand rates.
* claude-mythos-5 (invite-only) and the fast-mode Opus rates are
  deliberately not seeded.
"""

from datetime import date


def get_models():
    return [
        # =================================================================
        # CLAUDE 5 FAMILY — current generation
        # =================================================================
        {
            "model_name": "claude-fable-5",
            # Anthropic retired `temperature` / `top_p` on this model (400
            # "`temperature` is deprecated for this model"); the proxy also
            # knows this by version (providers::anthropic_sampling_params_retired),
            # the seed states it so an operator sees it on the model.
            "additional_drop_params": ["temperature", "top_p"],
            "parameters": 3000.0,
            "name": "Anthropic Claude Fable 5",
            "description": "Anthropic's most intelligent generally available model — the first of the Mythos-class tier above Opus. Thinking always on; 1M context.",
            "usage": "Frontier reasoning, long-running agents, expert analysis, hardest coding tasks",
            "default_max_tokens": 8192,
            "timeout": 300.0,
            "cost_per_input_token": 10.0,
            "cost_per_output_token": 50.0,
            "capabilities": {
                "context_window": 1000000,
                "max_output_tokens": 128000,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_prompt_caching": True,
                "supports_pdf_input": True,
                "supports_computer_use": True,
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "vision", "reasoning", "premium",
                     "long-context", "ultra-large", "latest"],
        },
        {
            "model_name": "claude-fable-5-1",
            "name": "Anthropic Claude Fable 5.1",
            "description": (
                "Claude Fable 5.1. 1M context, 128K output. Adaptive thinking"
                " always on. Not retired before 2027-09-01."
            ),
            "usage": "Hardest reasoning, research, long-running agents",
            "default_max_tokens": 8192,
            "timeout": 300.0,
            "additional_drop_params": ["temperature", "top_p"],
            "cost_per_input_token": 10.0,
            "cost_per_output_token": 50.0,
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
                "premium",
                "long-context",
            ],
        },
        {
            "model_name": "claude-fable-5-2",
            "name": "Anthropic Claude Fable 5.2",
            "description": (
                "Claude Fable 5.2. 1M context, 128K output. Adaptive thinking"
                " always on. Needs 30-day data retention: zero-data-retention"
                " organisations get 400. Not retired before 2027-11-09."
            ),
            "usage": "Hardest reasoning, research, long-running agents",
            "default_max_tokens": 8192,
            "timeout": 300.0,
            "additional_drop_params": ["temperature", "top_p"],
            "cost_per_input_token": 8.0,
            "cost_per_output_token": 40.0,
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
                "premium",
                "long-context",
                "latest",
            ],
        },
        {
            "model_name": "claude-opus-5",
            # Anthropic retired `temperature` / `top_p` on this model (400
            # "`temperature` is deprecated for this model"); the proxy also
            # knows this by version (providers::anthropic_sampling_params_retired),
            # the seed states it so an operator sees it on the model.
            "additional_drop_params": ["temperature", "top_p"],
            "parameters": 2000.0,
            "name": "Anthropic Claude Opus 5",
            "description": "Flagship Opus of the Claude 5 family. Deep analysis, nuanced writing, complex agentic work at half Fable's price.",
            "usage": "Research, complex analysis, expert-level writing, advanced coding, agents",
            "default_max_tokens": 8192,
            "timeout": 180.0,
            "cost_per_input_token": 5.0,
            "cost_per_output_token": 25.0,
            "capabilities": {
                "context_window": 1000000,
                "max_output_tokens": 128000,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_prompt_caching": True,
                "supports_pdf_input": True,
                "supports_computer_use": True,
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "vision", "reasoning", "premium",
                     "long-context", "ultra-large", "latest"],
        },
        {
            "model_name": "claude-sonnet-5",
            # Anthropic retired `temperature` / `top_p` on this model (400
            # "`temperature` is deprecated for this model"); the proxy also
            # knows this by version (providers::anthropic_sampling_params_retired),
            # the seed states it so an operator sees it on the model.
            "additional_drop_params": ["temperature", "top_p"],
            "parameters": 800.0,
            "name": "Anthropic Claude Sonnet 5",
            "description": "The Claude 5 workhorse: near-Opus capability at $2/$10 (introductory pricing made permanent). 1M context.",
            "usage": "Production chat, coding, agents, high-volume intelligent workloads",
            "default_max_tokens": 8192,
            "timeout": 120.0,
            "cost_per_input_token": 2.0,
            "cost_per_output_token": 10.0,
            "capabilities": {
                "context_window": 1000000,
                "max_output_tokens": 128000,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_prompt_caching": True,
                "supports_pdf_input": True,
                "supports_computer_use": True,
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "vision", "reasoning",
                     "cost-efficient", "long-context", "large", "latest"],
        },
        {
            "model_name": "claude-haiku-5-5",
            "name": "Anthropic Claude Haiku 5.5",
            "description": (
                "Claude Haiku 5.5. 1M context, 128K output. $0.10 / $0.50 per"
                " 1M for prompts up to 100,000 tokens (seeded); $0.50 / $2.50"
                " above. Adaptive thinking on by default (`disabled` only at "
                "effort high or below); no prefill. Not retired before "
                "2027-10-07."
            ),
            "usage": "High-volume chat, classification, extraction, sub-agents",
            "default_max_tokens": 8192,
            "timeout": 60.0,
            "additional_drop_params": ["temperature", "top_p"],
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
                "long-context",
                "latest",
            ],
        },

        # =================================================================
        # CLAUDE 4.x — legacy but ACTIVE (earliest retirements 2027)
        # Repriced 2026: the 4.6–4.8 Opus line now matches Opus 5 at $5/$25.
        # =================================================================
        {
            "model_name": "claude-opus-4-8",
            # Anthropic retired `temperature` / `top_p` on this model (400
            # "`temperature` is deprecated for this model"); the proxy also
            # knows this by version (providers::anthropic_sampling_params_retired),
            # the seed states it so an operator sees it on the model.
            "additional_drop_params": ["temperature", "top_p"],
            "parameters": 1500.0,
            "name": "Anthropic Claude Opus 4.8",
            "description": "Final Claude 4.x Opus and the official replacement for retired Opus 4.1/4.0. Active until at least 2027-05-28.",
            "usage": "Drop-in replacement for Opus 4.x pins; complex reasoning and coding",
            "default_max_tokens": 8192,
            "timeout": 180.0,
            "cost_per_input_token": 5.0,
            "cost_per_output_token": 25.0,
            "capabilities": {
                "context_window": 1000000,
                "max_output_tokens": 128000,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_prompt_caching": True,
                "supports_pdf_input": True,
                "supports_computer_use": True,
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "vision", "reasoning",
                     "long-context", "ultra-large"],
        },
        {
            "model_name": "claude-opus-4-7",
            "parameters": 1500.0,
            "name": "Anthropic Claude Opus 4.7",
            "description": "Claude 4.7 Opus. Legacy-active (until at least 2027-04-16), repriced to $5/$25.",
            "usage": "Existing 4.7 pins; complex reasoning, long-running agents",
            "default_max_tokens": 8192,
            "timeout": 180.0,
            "cost_per_input_token": 5.0,
            "cost_per_output_token": 25.0,
            "capabilities": {
                "context_window": 1000000,
                "max_output_tokens": 128000,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_prompt_caching": True,
                "supports_pdf_input": True,
                "supports_computer_use": True,
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "vision", "reasoning",
                     "long-context", "ultra-large"],
        },
        {
            "model_name": "claude-opus-4-6",
            "parameters": 1500.0,
            "name": "Anthropic Claude Opus 4.6",
            "description": "Claude 4.6 Opus. Legacy-active (until at least 2027-02-05), repriced to $5/$25.",
            "usage": "Existing 4.6 pins; deep analysis, expert writing",
            "default_max_tokens": 8192,
            "timeout": 180.0,
            "cost_per_input_token": 5.0,
            "cost_per_output_token": 25.0,
            "capabilities": {
                "context_window": 1000000,
                "max_output_tokens": 128000,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_prompt_caching": True,
                "supports_pdf_input": True,
                "supports_computer_use": True,
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "vision", "reasoning",
                     "long-context", "ultra-large"],
        },
        {
            "model_name": "claude-sonnet-4-6",
            "parameters": 600.0,
            "name": "Anthropic Claude Sonnet 4.6",
            "description": "Claude 4.6 Sonnet. Legacy-active (until at least 2027-02-17) and the official replacement for retired Sonnet 4.0.",
            "usage": "Existing Sonnet pins; production chat, coding, agents",
            "default_max_tokens": 8192,
            "timeout": 120.0,
            "cost_per_input_token": 3.0,
            "cost_per_output_token": 15.0,
            "capabilities": {
                "context_window": 1000000,
                "max_output_tokens": 128000,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_prompt_caching": True,
                "supports_pdf_input": True,
                "supports_computer_use": True,
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "vision", "reasoning",
                     "long-context", "large"],
        },
        {
            "model_name": "claude-opus-5-5",
            "name": "Anthropic Claude Opus 5.5",
            "description": (
                "Claude Opus 5.5. 1M context, 128K output. Adaptive thinking "
                "is always on (`disabled` and `budget_tokens` return 400); a "
                "forced `tool_choice` (any / tool) returns 400. Not retired "
                "before 2027-09-22."
            ),
            "usage": "Deep analysis, complex agentic work, coding",
            "default_max_tokens": 8192,
            "timeout": 180.0,
            "additional_drop_params": ["temperature", "top_p"],
            "cost_per_input_token": 4.0,
            "cost_per_output_token": 20.0,
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
                "premium",
                "long-context",
                "latest",
            ],
        },
        {
            "model_name": "claude-sonnet-5-5",
            "name": "Anthropic Claude Sonnet 5.5",
            "description": (
                "Claude Sonnet 5.5, Anthropic's named replacement for Sonnet "
                "4.5. 1M context, 128K output. Adaptive thinking (`disabled` "
                "returns 400). Not retired before 2027-09-28."
            ),
            "usage": "Production chat, coding, agents",
            "default_max_tokens": 8192,
            "timeout": 120.0,
            "additional_drop_params": ["temperature", "top_p"],
            "cost_per_input_token": 2.0,
            "cost_per_output_token": 10.0,
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
                "cost-efficient",
                "long-context",
                "latest",
            ],
        },
    ]
