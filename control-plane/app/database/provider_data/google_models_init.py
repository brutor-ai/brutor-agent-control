# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Google AI Studio Models — Gemini family.

Source:  https://ai.google.dev/gemini-api/docs/models
         https://openrouter.ai/google for cross-provider price reference
The gateway routes Gemini via the model `provider` field — model_name must
NOT carry a `gemini/` prefix (the prefix is for string-mode IDs;
on the Brutor side `provider="gemini"` does the same job and the doubled
prefix breaks Google's OpenAI-compat endpoint at
https://generativelanguage.googleapis.com/v1beta/openai/chat/completions).

Cost values: USD per 1M tokens (cross-checked against OpenRouter listings).

Why no `additional_drop_params` here:
  Google's OpenAI-compat endpoint rejects a small fixed set of
  OpenAI-only params (`frequency_penalty`, `presence_penalty`,
  `logit_bias`). Those are stripped centrally by the Rust gemini
  adapter (`brutor-gateway-core/src/services/llm_adapter/gemini.rs`)
  for EVERY Gemini model, so this seed doesn't need to repeat them.
  Keeping per-model `additional_drop_params` empty also means
  admin-set knobs like `default_max_tokens` reach Google as
  `max_tokens` (which the OpenAI-compat path accepts natively).
"""


from app.constants.llm import LLModelMode


def get_models():
    return [
        # =================================================================
        # AUTO-TRACKING ALIASES — Google promotes these to whatever it
        # considers the current production-ready Pro / Flash / Flash-Lite.
        # =================================================================
        # Operator caveat: the underlying model behind each alias can
        # change without notice when Google promotes a new release.
        # That means BOTH cost and behaviour can shift mid-deployment —
        # a workload validated at one price/quality point may suddenly
        # be billing at a different rate the next day. Use these
        # aliases for "I always want the latest" tenants, and pin to
        # an explicit version (e.g. `gemini-4-argon`) when stability
        # of cost and output is a hard requirement.
        #
        # Google publishes neither the alias's current target nor a price
        # for it, so no cost is seeded (2026-11-11): operators set it on
        # import. The Brutor proxy logs the resolved upstream model
        # name in `proxy_logs.llm_model_used`, so historical billing
        # is reconstructable even if the alias drifts.
        # =================================================================
        {
            "model_name": "gemini-pro-latest",
            "parameters": 1500.0,
            "name": "Google Gemini Pro (Latest)",
            "description": (
                "Alias: Google's latest release of Gemini Pro. Google moves "
                "the alias to a new model with two weeks' notice and "
                "publishes neither its current target nor a price for it, so "
                "no cost is seeded: set the cost on import, or use the "
                "explicit id gemini-4-argon."
            ),
            "usage": "Tenants who always want the latest Pro tier without manual seed updates",
            "default_max_tokens": 8192,
            "timeout": 180.0,
            "cost_per_input_token": None,
            "cost_per_output_token": None,
            "capabilities": {
                "context_window": 1048576,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_response_schema": True,
                "supports_prompt_caching": True,
                "supports_system_messages": True,
            },
            "tags": ["function-calling", "vision", "reasoning", "alias",
                     "auto-tracking", "long-context", "ultra-large"],
        },
        {
            # Live-verified alias: 1M ctx, 65K output. Currently targets
            # the 2.5-Flash tier; will roll forward to 3.x Flash on GA.
            "model_name": "gemini-flash-latest",
            "parameters": 400.0,
            "name": "Google Gemini Flash (Latest)",
            "description": (
                "Alias: Google's latest release of Gemini Flash. Google moves"
                " the alias to a new model with two weeks' notice and "
                "publishes neither its current target nor a price for it, so "
                "no cost is seeded: set the cost on import, or use the "
                "explicit id gemini-3.8-flash."
            ),
            "usage": "Default chat / coding workloads where staying current is more important than version pinning",
            "default_max_tokens": 8192,
            "cost_per_input_token": None,
            "cost_per_output_token": None,
            "capabilities": {
                "context_window": 1048576,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_response_schema": True,
                "supports_prompt_caching": True,
                "supports_system_messages": True,
            },
            "tags": ["function-calling", "vision", "alias", "auto-tracking",
                     "cost-efficient", "fast", "long-context"],
        },
        {
            # Live-verified alias: 1M ctx, 65K output. Currently targets
            # the 2.5-Flash-Lite tier.
            "model_name": "gemini-flash-lite-latest",
            "parameters": 100.0,
            "name": "Google Gemini Flash Lite (Latest)",
            "description": (
                "Alias: Google's latest release of Gemini Flash-Lite. Google "
                "moves the alias to a new model with two weeks' notice and "
                "publishes neither its current target nor a price for it, so "
                "no cost is seeded: set the cost on import, or use the "
                "explicit id gemini-3.5-flash-lite."
            ),
            "usage": "High-volume routing, classification, content moderation when staying current matters more than version pinning",
            "default_max_tokens": 4096,
            "cost_per_input_token": None,
            "cost_per_output_token": None,
            "capabilities": {
                "context_window": 1048576,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_response_schema": True,
                "supports_system_messages": True,
            },
            "tags": ["function-calling", "vision", "alias", "auto-tracking",
                     "cost-efficient", "cheap", "long-context"],
        },

        # =================================================================
        # GEMINI 3.x FAMILY — current preview / latest
        # =================================================================
        # =================================================================
        # GEMINI 3.8 FLASH / 4 ARGON — current GA (refreshed 2026-11-11).
        # Seeded at the regular prices, not the introductory ones.
        # =================================================================
        {
            "model_name": "gemini-3.8-flash",
            "name": "Google Gemini 3.8 Flash",
            "description": (
                "Gemini 3.8 Flash. 1M context, 65K output; text, image, "
                "video, audio and PDF input; thinking low/medium/high. Seeded"
                " at the regular price ($1.50 / $7.50 per 1M from "
                "2027-01-01); the promotional $0.75 / $3.75 runs through "
                "2026-12-31. Google routes the deprecated gemini-3.7-flash "
                "and the shut-down gemini-2.5-pro here."
            ),
            "usage": "Production chat, multimodal analysis, agents",
            "default_max_tokens": 8192,
            "timeout": 120.0,
            "cost_per_input_token": 1.5,
            "cost_per_output_token": 7.5,
            "supports_vision": True,
            "capabilities": {
                "context_window": 1048576,
                "max_output_tokens": 65536,
                "modalities": ["text", "image", "video", "audio"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_response_schema": True,
                "supports_pdf_input": True,
            },
            "tags": [
                "function-calling",
                "vision",
                "reasoning",
                "multimodal",
                "long-context",
                "latest",
            ],
        },
        {
            "model_name": "gemini-4-argon",
            "name": "Google Gemini 4 Argon",
            "description": (
                "Gemini 4 Argon (GA 2026-10-13), Google's flagship. 1M "
                "unified context, 65K output by default (up to 1,048,576 with"
                " long decode); multimodal input; thinking, function calling,"
                " structured output, computer use. Seeded at the regular "
                "price ($4 / $20 per 1M from 2026-12-01); the promotional $2 "
                "/ $10 runs through 2026-11-30."
            ),
            "usage": "Frontier reasoning, long-context analysis, agents",
            "default_max_tokens": 8192,
            "timeout": 180.0,
            "cost_per_input_token": 4.0,
            "cost_per_output_token": 20.0,
            "supports_vision": True,
            "capabilities": {
                "context_window": 1048576,
                "max_output_tokens": 65536,
                "modalities": ["text", "image", "video", "audio"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_response_schema": True,
                "supports_computer_use": True,
            },
            "tags": [
                "function-calling",
                "vision",
                "reasoning",
                "flagship",
                "multimodal",
                "long-context",
                "latest",
            ],
        },
        {
            "model_name": "gemini-3.5-flash-lite",
            "parameters": 100.0,
            "name": "Google Gemini 3.5 Flash Lite",
            "description": "Cheap stable tier of the 3.5 line.",
            "usage": "High-volume classification, extraction, simple chat",
            "default_max_tokens": 8192,
            "timeout": 60.0,
            "cost_per_input_token": 0.30,
            "cost_per_output_token": 2.50,
            "capabilities": {
                "context_window": 1048576,
                "modalities": ["text", "image"],
                "supports_function_calling": True,
                "supports_vision": True,
                "supports_response_schema": True,
                "max_output_tokens": 65536,
            },
            "tags": ["function-calling", "vision", "cheap",
                     "cost-efficient", "long-context", "small"],
        },
        {
            # Verified live: Google API 2026-04-26 (`models?key=…` returned
            # `gemini-3.1-pro-preview` with inputTokenLimit 1048576).
            # Pricing: OpenRouter `google/gemini-3.1-pro-preview` $2 / $12 per 1M.
            "model_name": "gemini-3.1-pro-preview",
            "parameters": 2000.0,
            "name": "Google Gemini 3.1 Pro (Preview)",
            "description": "Latest Gemini Pro preview — 1M context, improved reasoning + agentic tooling over 3.0.",
            "usage": "Frontier reasoning, large-document analysis, agentic workflows, deep research",
            "default_max_tokens": 8192,
            "timeout": 180.0,
            "cost_per_input_token": 2.0,
            "cost_per_output_token": 12.0,
            "capabilities": {
                "context_window": 1048576,
                "modalities": ["text", "vision", "audio"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_response_schema": True,
                "supports_prompt_caching": True,
                "supports_system_messages": True,
                "restricted_tier": True,
                "required_tier": "Paid Tier 1 (Preview)",
                "max_output_tokens": 65536,
            },
            "tags": ["function-calling", "vision", "reasoning", "preview",
                     "long-context", "ultra-large", "latest"],
        },
        {
            # Verified live: Google API ctx 1,048,576.
            # Pricing: OpenRouter $0.50 / $3.00 per 1M.
            "model_name": "gemini-3-flash-preview",
            "parameters": 500.0,
            "name": "Google Gemini 3 Flash (Preview)",
            "description": "Next-gen fast Gemini with 1M context. Preview release.",
            "usage": "Fast document processing, chatbots, everyday tasks",
            "default_max_tokens": 8192,
            "cost_per_input_token": 0.50,
            "cost_per_output_token": 3.00,
            "capabilities": {
                "context_window": 1048576,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_response_schema": True,
                "supports_system_messages": True,
                "restricted_tier": True,
                "required_tier": "Paid Tier 1 (Preview)",
                "max_output_tokens": 65536,
            },
            "tags": ["function-calling", "vision", "cost-efficient", "preview",
                     "long-context"],
        },

        # =================================================================
        # GEMINI 2.5 FAMILY — stable, the default for production
        # =================================================================

        # =================================================================
        # IMAGE GENERATION
        # =================================================================
        # Google's image model. Wired through the
        # `call_google_images` adapter in routes/images.rs
        # (added in the same release as this seed entry).
        # Default-disabled because of cost-discovery risk on
        # auto-deploy; operators flip `enabled=True` per tenant
        # once they've decided on the spend profile.
        {
            "model_name": "gemini-nano-banana-2.1",
            "name": "Google Gemini Nano Banana 2.1",
            "description": (
                "Gemini Nano Banana 2.1 (GA 2026-10-06), Google's image model"
                " and the replacement for the deprecated gemini-3.1-flash-"
                "image. Token-priced: input $1.50, text output $7.50, image "
                "output $30.00 per 1M tokens, which is $0.0336 per image at "
                "1K (1024x1024), $0.0504 at 2K and $0.113 at 4K. No function "
                "calling, no structured output."
            ),
            "usage": "Image generation",
            "mode": LLModelMode.IMAGE_GENERATION.value,
            "supports_vision": False,
            "supports_function_calling": False,
            "default_max_tokens": 0,
            "enabled": False,
            "timeout": 180.0,
            "cost_per_image": 0.0336,
            "cost_per_input_token": 1.5,
            "cost_per_output_token": 7.5,
            "supported_image_sizes": ["1K", "2K", "4K"],
            # The gateway's own default; Google's per-request limit for this
            # model was not read.
            "max_images_per_request": 1,
            "capabilities": {
                "context_window": 131072,
                "max_output_tokens": 32768,
                "modalities": ["text"],
                "supports_function_calling": False,
                "supports_response_schema": False,
                "cost_per_image_output_token": 30.0,
            },
            "tags": ["image-gen", "google", "gemini", "nano-banana"],
        },
    ]
