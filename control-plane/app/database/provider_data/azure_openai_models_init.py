# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Azure OpenAI Service — Model Definitions for Brutor AI Platform
Model prefix: azure/

Azure OpenAI provides OpenAI models hosted on Microsoft Azure infrastructure.
Model names use azure/<deployment-name> format. The actual deployment name
is configured per-tenant; these entries use canonical model identifiers.
Pricing may differ slightly from OpenAI direct pricing.

NOTE: Requires AZURE_API_KEY, AZURE_API_BASE, AZURE_API_VERSION.
Deployment names are tenant-specific; model_name here uses canonical IDs.
"""

from app.constants.llm import LLModelMode, LLModelProvider


from datetime import date

def get_models():
    return [

        # ---------------------------------------------------------------
        # GPT-5 family on Azure
        # ---------------------------------------------------------------
        {
            "model_name": "azure/gpt-5.2",
            "parameters": 2000.0,
            "name": "Azure GPT-5.2",
            "description": "OpenAI GPT-5.2 on Azure. Latest flagship model with Azure enterprise features.",
            "usage": "Enterprise AI, complex reasoning with Azure compliance and data residency",
            "cost_per_input_token": 1.75,
            "cost_per_output_token": 14.0,
            "capabilities": {
                "context_window": 400000,
                "modalities": ["text", "vision", "audio"],
                "supports_response_schema": True,
                "supports_reasoning": True,
                "max_output_tokens": 128000,
            },
            "supports_vision": True,
            "tags": ["openai", "gpt-5", "flagship"],
            "retirement_notes": "Azure retirement schedule; Microsoft names no replacement yet.",
            "retire_date": date(2027, 6, 8),
        },
        {
            "model_name": "azure/gpt-5.1",
            "parameters": 2000.0,
            "name": "Azure GPT-5.1",
            "description": "OpenAI GPT-5.1 on Azure. Strong general-purpose model.",
            "usage": "Enterprise workloads, coding, analysis with Azure data residency",
            "cost_per_input_token": 1.25,
            "cost_per_output_token": 10.0,
            "capabilities": {
                "context_window": 400000,
                "modalities": ["text", "vision", "audio"],
                "supports_response_schema": True,
                "max_output_tokens": 128000,
            },
            "supports_vision": True,
            "tags": ["openai", "gpt-5"],
            "retirement_notes": "Azure retirement schedule; Microsoft names no replacement yet.",
            "retire_date": date(2027, 7, 13),
        },
        {
            "model_name": "azure/gpt-5-mini",
            "parameters": 500.0,
            "name": "Azure GPT-5 Mini",
            "description": "OpenAI GPT-5 Mini on Azure. Cost-effective for routine tasks.",
            "usage": "High-volume enterprise tasks, Azure-native cost-efficient inference",
            "cost_per_input_token": 0.25,
            "cost_per_output_token": 2.0,
            "capabilities": {
                "context_window": 400000,
                "modalities": ["text", "vision"],
                "supports_response_schema": True,
                "max_output_tokens": 128000,
            },
            "supports_vision": True,
            "tags": ["openai", "gpt-5", "cost-efficient"],
            "retirement_notes": "Azure retirement schedule; Microsoft names no replacement yet.",
            "retire_date": date(2027, 7, 13),
        },

        # ---------------------------------------------------------------
        # GPT-4.1 family on Azure
        # ---------------------------------------------------------------

        # ---------------------------------------------------------------
        # O-series reasoning models on Azure
        # ---------------------------------------------------------------
        {
            "model_name": "azure/gpt-5.6-sol",
            "name": "Azure GPT-5.6 Sol",
            "description": (
                "OpenAI GPT-5.6 Sol on Azure (version 2026-07-09). 1.05M "
                "context (922K input), 128K output. Text and image input, "
                "tools, structured output, reasoning. On Chat Completions "
                "tools work only with reasoning_effort \"none\". Above 272K "
                "input tokens $8 / $30 per 1M. Microsoft's named replacement "
                "for o3."
            ),
            "usage": "Frontier reasoning with Azure compliance and data residency",
            "retire_date": date(2028, 1, 11),
            "retirement_notes": "Azure retirement schedule; Microsoft names no replacement yet.",
            "cost_per_input_token": 4.0,
            "cost_per_output_token": 20.0,
            "supports_vision": True,
            "capabilities": {
                "context_window": 1050000,
                "max_output_tokens": 128000,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                # Chat Completions takes function tools only with
                # reasoning_effort "none"; the gateway reads this.
                "tools_require_reasoning_effort_none": True,
                "supports_response_schema": True,
            },
            "tags": [
                "openai",
                "gpt-5",
                "function-calling",
                "vision",
                "reasoning",
                "long-context",
            ],
        },
        {
            "model_name": "azure/gpt-5.6-terra",
            "name": "Azure GPT-5.6 Terra",
            "description": (
                "OpenAI GPT-5.6 Terra on Azure (version 2026-07-09). 1.05M "
                "context (922K input), 128K output. Text and image input, "
                "tools, structured output, reasoning. On Chat Completions "
                "tools work only with reasoning_effort \"none\". Microsoft's "
                "named replacement for o3-mini and o4-mini."
            ),
            "usage": "Production chat, coding, agents on Azure",
            "retire_date": date(2028, 1, 11),
            "retirement_notes": "Azure retirement schedule; Microsoft names no replacement yet.",
            "cost_per_input_token": 2.0,
            "cost_per_output_token": 12.0,
            "supports_vision": True,
            "capabilities": {
                "context_window": 1050000,
                "max_output_tokens": 128000,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                # Chat Completions takes function tools only with
                # reasoning_effort "none"; the gateway reads this.
                "tools_require_reasoning_effort_none": True,
                "supports_response_schema": True,
            },
            "tags": [
                "openai",
                "gpt-5",
                "function-calling",
                "vision",
                "reasoning",
                "long-context",
            ],
        },
        {
            "model_name": "azure/gpt-5.6-luna",
            "name": "Azure GPT-5.6 Luna",
            "description": (
                "OpenAI GPT-5.6 Luna on Azure (version 2026-07-09). 1.05M "
                "context (922K input), 128K output. Text and image input, "
                "tools, structured output, reasoning. On Chat Completions "
                "tools work only with reasoning_effort \"none\"."
            ),
            "usage": "High-volume, low-cost chat on Azure",
            "retire_date": date(2028, 1, 11),
            "retirement_notes": "Azure retirement schedule; Microsoft names no replacement yet.",
            "cost_per_input_token": 0.2,
            "cost_per_output_token": 1.2,
            "supports_vision": True,
            "capabilities": {
                "context_window": 1050000,
                "max_output_tokens": 128000,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                # Chat Completions takes function tools only with
                # reasoning_effort "none"; the gateway reads this.
                "tools_require_reasoning_effort_none": True,
                "supports_response_schema": True,
            },
            "tags": [
                "openai",
                "gpt-5",
                "function-calling",
                "vision",
                "reasoning",
                "long-context",
            ],
        },

        # ---------------------------------------------------------------
        # Azure OpenAI Embeddings
        # ---------------------------------------------------------------
        {
            "model_name": "azure/text-embedding-3-large",
            "parameters": 350.0,
            "name": "Azure Text Embedding 3 Large",
            "description": "OpenAI text-embedding-3-large on Azure. High-quality embeddings.",
            "usage": "Enterprise semantic search, RAG on Azure",
            "mode": LLModelMode.EMBEDDING.value,
            "cost_per_input_token": 0.13,
            "cost_per_output_token": 0.0,
            "supports_function_calling": False,
            "supports_streaming": False,
            "supports_vision": False,
            "capabilities": {
                "context_window": 8191,
                "dimensions": 3072,
                "modalities": ["text"],
            },
            "tags": ["embedding", "openai"],
            "retirement_notes": "Azure retirement schedule; Microsoft names no replacement yet.",
            "retire_date": date(2028, 2, 9),
        },
        {
            "model_name": "azure/text-embedding-3-small",
            "parameters": 62.0,
            "name": "Azure Text Embedding 3 Small",
            "description": "OpenAI text-embedding-3-small on Azure. Cost-effective embeddings.",
            "usage": "High-volume embedding tasks on Azure",
            "mode": LLModelMode.EMBEDDING.value,
            "cost_per_input_token": 0.02,
            "cost_per_output_token": 0.0,
            "supports_function_calling": False,
            "supports_streaming": False,
            "supports_vision": False,
            "capabilities": {
                "context_window": 8191,
                "dimensions": 1536,
                "modalities": ["text"],
            },
            "tags": ["embedding", "openai", "cost-efficient"],
            "retirement_notes": "Azure retirement schedule; Microsoft names no replacement yet.",
            "retire_date": date(2028, 2, 9),
        },
    ]
