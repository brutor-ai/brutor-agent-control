# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Azure AI Foundry (formerly Azure AI Studio) — Model Definitions for Brutor AI Platform
Model prefix: azure_ai/

Azure AI Foundry provides access to third-party models via Azure's serverless
API infrastructure. Models include Anthropic Claude, Mistral, Cohere, Meta Llama,
and others — all accessible with Azure-native authentication and billing.

NOTE: Requires AZURE_AI_API_KEY and AZURE_AI_API_BASE per-model.
Each model deployment gets its own endpoint URL.
"""

from app.constants.llm import LLModelMode, LLModelProvider


def get_models():
    return [

        # ---------------------------------------------------------------
        # Anthropic Claude on Azure AI Foundry
        # ---------------------------------------------------------------
        {
            "model_name": "azure_ai/claude-sonnet-5-5",
            "name": "Azure AI Claude Sonnet 5.5",
            "description": (
                "Claude Sonnet 5.5 on Azure AI Foundry, billed at Anthropic's"
                " list rates. 1M context, 128K output. Vision, tool use, "
                "adaptive thinking, structured output."
            ),
            "usage": "Production chat, coding, agents",
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
            "tags": ["function-calling", "vision", "reasoning", "long-context"],
        },
        {
            "model_name": "azure_ai/claude-haiku-5-5",
            "name": "Azure AI Claude Haiku 5.5",
            "description": (
                "Claude Haiku 5.5 on Azure AI Foundry, billed at Anthropic's "
                "list rates. 1M context, 128K output. Vision, tool use, "
                "adaptive thinking, structured output. $0.10 / $0.50 per 1M "
                "for prompts up to 100,000 tokens (seeded); $0.50 / $2.50 "
                "above."
            ),
            "usage": "High-volume chat, classification, extraction",
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
            "tags": ["function-calling", "vision", "reasoning", "long-context", "cheap"],
        },
        {
            "model_name": "azure_ai/claude-opus-5-5",
            "name": "Azure AI Claude Opus 5.5",
            "description": (
                "Claude Opus 5.5 on Azure AI Foundry, billed at Anthropic's "
                "list rates. 1M context, 128K output. Vision, tool use, "
                "adaptive thinking (always on), structured output."
            ),
            "usage": "Deep analysis, complex agentic work",
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
            "tags": ["function-calling", "vision", "reasoning", "long-context"],
            "timeout": 300.0,
        },

        # ---------------------------------------------------------------
        # Mistral on Azure AI Foundry
        # ---------------------------------------------------------------

        # ---------------------------------------------------------------
        # Cohere on Azure AI Foundry
        # ---------------------------------------------------------------

        # ---------------------------------------------------------------
        # Meta Llama on Azure AI Foundry
        # ---------------------------------------------------------------

        # ---------------------------------------------------------------
        # Azure AI Foundry Embeddings
        # ---------------------------------------------------------------
        {
            "model_name": "azure_ai/Cohere-embed-v5-Fast",
            "name": "Azure AI Cohere Embed v5 Fast",
            "description": (
                "Cohere Embed v5 Fast on Azure AI Foundry, Microsoft's named "
                "replacement for Cohere Embed v3. Text $0.08 per 1M tokens "
                "(seeded); image $0.40. Dimensions 2048, 1536, 1024, 768, 512"
                " or 256. The input token limit is not published."
            ),
            "usage": "Semantic search and RAG on Azure",
            "mode": "embedding",
            "supports_streaming": False,
            "supports_function_calling": False,
            "supports_vision": False,
            "cost_per_input_token": 0.08,
            "cost_per_output_token": 0.0,
            "capabilities": {
                "modalities": ["text", "image"],
                "dimensions": 2048,
            },
            "tags": ["cohere", "embeddings"],
        },
    ]
