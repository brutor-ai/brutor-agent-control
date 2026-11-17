# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Groq Models — Ultra-fast inference for open and OSS-licensed models.

Source: https://groq.com/pricing                         (authoritative pricing)
        https://api.groq.com/openai/v1/models            (authoritative catalogue)

Cost values: USD per 1M tokens.

Catalogue scope
---------------
Groq's API exposes models from multiple upstream organizations on the
same endpoint, and the model IDs encode the upstream org name:

  * ``llama-3.x-*``                       (Meta — bare ID)
  * ``meta-llama/llama-4-*``              (Meta — explicit org prefix)
  * ``openai/gpt-oss-*``                  (OpenAI's open-weight models)
  * ``qwen/qwen3.8-*``                    (Alibaba)
  * ``qwen/qwen3-*``                      (Alibaba)
  * ``groq/compound*``                    (Groq's own agentic system)

This seed contains only the **chat-capable** models Groq currently
lists as production (plus clearly-tagged previews). Catalogue
refreshed 2026-08-23 against console.groq.com/docs/models and
/docs/deprecations — the entire Llama line and qwen3-32b were
decommissioned 2026-07/08 in favour of the GPT-OSS pair and the Qwen
preview.

Refreshed again 2026-11-11: ``qwen/qwen3.6-27b`` (shut down 2026-09-14,
replaced by ``qwen/qwen3.8-27b``) and Groq's agentic ``compound`` systems
(shut down 2026-09-21, no replacement) are no longer seeded.

The other things ``/v1/models`` returns are kept out of this file:

  * ``whisper-large-v3`` / ``whisper-large-v3-turbo``  (STT — different mode shape)
  * ``canopylabs/orpheus-*``                            (TTS)
  * ``meta-llama/llama-prompt-guard-2-*``               (safety classifiers)
  * ``allam-2-7b``                                      (Arabic-focused; not priced)

Naming
------
Model IDs are passed to Groq EXACTLY as listed in ``/v1/models``.

* No ``groq/`` Brutor-style prefix on Meta/OpenAI/Qwen models — Groq
  rejects ``groq/llama-3.3-70b-versatile`` with 404 ``model_not_found``.
* Where the upstream org name is *part* of the canonical ID (e.g.
  ``openai/gpt-oss-120b``, ``qwen/qwen3.8-27b``) we keep that prefix because Groq REQUIRES it; it's
  the ID, not a routing decoration.

Provider routing keys off the LlmModel ``provider`` column ("groq").
"""


def get_models():
    return [
        # =================================================================
        # OPENAI GPT-OSS — OpenAI's open-weight reasoning models on Groq
        # =================================================================
        {
            "model_name": "openai/gpt-oss-120b",
            "parameters": 120.0,
            "name": "Groq GPT-OSS 120B",
            "description": "OpenAI's flagship open-weight reasoning model on Groq hardware. ~500 TPS, the most capable Groq-hosted chat model.",
            "usage": "Reasoning, complex Q&A, agentic workflows, low-latency premium tier",
            "default_max_tokens": 8192,
            "timeout": 120.0,
            "cost_per_input_token": 0.15,
            "cost_per_output_token": 0.60,
            "capabilities": {
                "context_window": 131072,
                "modalities": ["text"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_response_schema": True,
                "max_output_tokens": 65536,
            },
            "tags": ["function-calling", "reasoning", "fast-inference",
                     "open-weight", "long-context", "large"],
        },
        {
            "model_name": "openai/gpt-oss-20b",
            "parameters": 20.0,
            "name": "Groq GPT-OSS 20B",
            "description": "OpenAI's smaller open-weight reasoning model on Groq. ~1000 TPS, half the price of 120B.",
            "usage": "Cost-efficient reasoning, classification, mid-complexity Q&A",
            "default_max_tokens": 8192,
            "timeout": 60.0,
            "cost_per_input_token": 0.075,
            "cost_per_output_token": 0.30,
            "capabilities": {
                "context_window": 131072,
                "modalities": ["text"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_response_schema": True,
                "max_output_tokens": 65536,
            },
            "tags": ["function-calling", "reasoning", "fast-inference",
                     "open-weight", "cost-efficient", "long-context", "medium"],
        },
        {
            "model_name": "openai/gpt-oss-safeguard-20b",
            "parameters": 20.0,
            "name": "Groq GPT-OSS Safeguard 20B (Preview)",
            "description": "Safety-reasoning variant of GPT-OSS 20B for content classification against a provided policy. Preview status; a moderation classifier, not a general chat model.",
            "usage": "Moderation pipelines, user-facing chat with stronger refusal behaviour",
            "default_max_tokens": 8192,
            "timeout": 60.0,
            "cost_per_input_token": 0.075,
            "cost_per_output_token": 0.30,
            "capabilities": {
                "context_window": 131072,
                "modalities": ["text"],
                "supports_function_calling": True,
                "supports_response_schema": True,
                "supports_reasoning": True,
                "max_output_tokens": 65536,
            },
            "tags": ["function-calling", "fast-inference", "open-weight",
                     "moderation", "preview", "long-context", "medium"],
        },

        # =================================================================
        # QWEN — Alibaba's models on Groq (PREVIEW)
        # =================================================================
        # Groq decommissioned llama-3.3-70b-versatile + llama-3.1-8b-instant
        # (2026-08-16), qwen/qwen3-32b + llama-4-scout (2026-07-17) and the
        # 3.6 preview (2026-09-14) in favour of qwen3.8-27b.
        # Preview per Groq's docs — may change without the usual
        # deprecation window.
        {
            "model_name": "qwen/qwen3.8-27b",
            "parameters": 27.0,
            "name": "Groq Qwen3.8 27B (Preview)",
            "description": "Alibaba Qwen3.8 27B on Groq; Groq's named replacement for qwen3.6-27b. Multimodal (text+image in, text out), thinking mode by default with reasoning_effort none/low/medium/high. Preview status.",
            "usage": "Multilingual tasks, reasoning, vision, mixed-language code review",
            "default_max_tokens": 8192,
            "timeout": 60.0,
            # console.groq.com/docs/model/qwen/qwen3.8-27b (2026-10-16):
            # $0.80 in / $4.00 out per 1M tokens, 131,072 context,
            # 16,384 max output, tool use + JSON schema mode supported.
            "cost_per_input_token": 0.80,
            "cost_per_output_token": 4.00,
            "capabilities": {
                "context_window": 131072,
                "max_output_tokens": 16384,
                "modalities": ["text", "image"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_vision": True,
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "reasoning", "fast-inference",
                     "open-weight", "multilingual", "vision", "preview",
                     "long-context", "medium"],
        },

        # =================================================================
        # GROQ COMPOUND — Agentic system (built-in tools, billed separately)
        # =================================================================
    ]
