# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Vertex AI (Google Cloud) — Model Definitions for Brutor AI Platform
Model prefix: vertex_ai/

Vertex AI provides access to Google's Gemini models plus partner models
(Anthropic Claude, Meta Llama, Mistral) via the Model Garden.
Authentication via GCP service account or application-default credentials.
Pricing is typically the same as direct API pricing for first-party models,
with slight markup for partner models.

NOTE: Vertex AI requires vertex_project and vertex_location configuration.
These are set at deployment time, not in model definitions.
"""

from app.constants.llm import LLModelMode, LLModelProvider


def get_models():
    return [

        # ---------------------------------------------------------------
        # Google Gemini (first-party on Vertex)
        # ---------------------------------------------------------------
        {
            "model_name": "vertex_ai/gemini-3.8-flash",
            "name": "Vertex AI Gemini 3.8 Flash",
            "description": (
                "Gemini 3.8 Flash on Vertex AI (global endpoint). 1M context,"
                " 65K output; thinking, function calling, structured output. "
                "Seeded at the regular price ($1.50 / $7.50 per 1M from "
                "2027-01-01); the promotional $0.75 / $3.75 runs through "
                "2026-12-31. Not retired before 2027-09-02."
            ),
            "usage": "Production chat, multimodal analysis, agents on Google Cloud",
            "cost_per_input_token": 1.5,
            "cost_per_output_token": 7.5,
            "capabilities": {
                "context_window": 1048576,
                "max_output_tokens": 65536,
                "modalities": ["text"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "reasoning", "long-context", "latest"],
        },
        {
            "model_name": "vertex_ai/gemini-3.5-flash-lite",
            "name": "Vertex AI Gemini 3.5 Flash-Lite",
            "description": (
                "Gemini 3.5 Flash-Lite on Vertex AI. 1M context, 65K output; "
                "thinking, function calling, structured output. Not retired "
                "before 2027-07-21."
            ),
            "usage": "High-volume, low-cost chat on Google Cloud",
            "cost_per_input_token": 0.3,
            "cost_per_output_token": 2.5,
            "capabilities": {
                "context_window": 1048576,
                "max_output_tokens": 65536,
                "modalities": ["text"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_response_schema": True,
            },
            "tags": [
                "function-calling",
                "reasoning",
                "cheap",
                "cost-efficient",
                "long-context",
            ],
        },
        {
            "model_name": "vertex_ai/gemini-4-argon",
            "name": "Vertex AI Gemini 4 Argon",
            "description": (
                "Gemini 4 Argon on Vertex AI (GA). 1M unified context; 65K "
                "output by default; thinking, function calling, structured "
                "output. Seeded at the regular price ($4 / $20 per 1M from "
                "2026-12-01); the promotional $2 / $10 runs through "
                "2026-11-30. Not retired before 2027-10-13."
            ),
            "usage": "Frontier reasoning and long-context analysis on Google Cloud",
            "cost_per_input_token": 4.0,
            "cost_per_output_token": 20.0,
            "capabilities": {
                "context_window": 1048576,
                "max_output_tokens": 65536,
                "modalities": ["text"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_response_schema": True,
            },
            "tags": [
                "function-calling",
                "reasoning",
                "flagship",
                "long-context",
                "latest",
            ],
        },
        {
            "model_name": "vertex_ai/gemini-3-flash-preview",
            "parameters": 500.0,
            "name": "Vertex AI Gemini 3 Flash (Preview)",
            "description": (
                "Gemini 3 Flash (Preview) on Vertex AI. 1M context, 65K "
                "output. The output price is the Gemini API price: Vertex's "
                "Standard table has no output row for this model."
            ),
            "usage": "Early access evaluation, fast next-gen inference",
            "cost_per_input_token": 0.5,
            "cost_per_output_token": 3.0,
            "capabilities": {
                "context_window": 1048576,
                "modalities": ["text", "vision", "audio", "video"],
                "supports_reasoning": True,
                "supports_response_schema": True,
                "max_output_tokens": 65536,
            },
            "supports_vision": True,
            "tags": ["gemini", "preview", "fast", "next-gen"],
        },

        # ---------------------------------------------------------------
        # Anthropic Claude on Vertex AI (Partner Model)
        # ---------------------------------------------------------------
        {
            "model_name": "vertex_ai/claude-sonnet-5-5",
            "name": "Vertex AI Claude Sonnet 5.5",
            "description": (
                "Claude Sonnet 5.5 on Vertex AI (global endpoint; regional "
                "endpoints cost 10% more). 1M context, 128K output. Vision, "
                "tool use, adaptive thinking, structured output."
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
            "model_name": "vertex_ai/claude-haiku-5-5",
            "name": "Vertex AI Claude Haiku 5.5",
            "description": (
                "Claude Haiku 5.5 on Vertex AI (global endpoint; regional "
                "endpoints cost 10% more). 1M context, 128K output. Vision, "
                "tool use, adaptive thinking, structured output. $0.10 / "
                "$0.50 per 1M for prompts up to 100,000 tokens (seeded); "
                "$0.50 / $2.50 above."
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
            "model_name": "vertex_ai/claude-opus-5-5",
            "name": "Vertex AI Claude Opus 5.5",
            "description": (
                "Claude Opus 5.5 on Vertex AI (global endpoint; regional "
                "endpoints cost 10% more). 1M context, 128K output. Vision, "
                "tool use, adaptive thinking (always on), structured output."
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
        # Meta Llama on Vertex AI (Model Garden - MaaS)
        # ---------------------------------------------------------------
        {
            "model_name": "vertex_ai/meta/llama-4-maverick-17b-128e-instruct-maas",
            "name": "Vertex AI Llama 4 Maverick",
            "description": (
                "Meta Llama 4 Maverick on Vertex AI (model-as-a-service, GA)."
                " 524K context; text and image input, function calling, "
                "structured output. The maximum output is not published."
            ),
            "usage": "Open-weight chat and vision on Google Cloud",
            "cost_per_input_token": 0.35,
            "cost_per_output_token": 1.15,
            "supports_vision": True,
            "capabilities": {
                "context_window": 524288,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_response_schema": True,
            },
            "tags": ["meta", "llama", "open-weight", "vision"],
        },

        # ---------------------------------------------------------------
        # Mistral on Vertex AI (Partner Model)
        # ---------------------------------------------------------------
        {
            "model_name": "vertex_ai/mistral-medium-3",
            "name": "Vertex AI Mistral Medium 3",
            "description": (
                "Mistral Medium 3 on Vertex AI (GA). 128K context. The "
                "maximum output is not published."
            ),
            "usage": "European-model chat on Google Cloud",
            "supports_vision": False,
            "cost_per_input_token": 0.4,
            "cost_per_output_token": 2.0,
            "capabilities": {
                "context_window": 128000,
                "modalities": ["text"],
            },
            "tags": ["mistral"],
        },

        # ---------------------------------------------------------------
        # Vertex AI Embeddings
        # ---------------------------------------------------------------
        {
            "model_name": "vertex_ai/gemini-embedding-001",
            "name": "Vertex AI Gemini Embedding 001",
            "description": (
                "Gemini Embedding 001 on Vertex AI. 2,048 tokens per text, "
                "3,072 dimensions. Vertex prints the price as $0.00015 per "
                "1,000 without naming the unit; $0.15 per 1M assumes tokens, "
                "as on the Gemini API. Not retired before 2028-05-20."
            ),
            "usage": "Semantic search, RAG, clustering on Google Cloud",
            "mode": "embedding",
            # The gateway builds a Vertex URL from the project and region on
            # the chat, Messages and Gemini routes only: on the embeddings
            # route this row needs its own `api_url`.
            "api_url_auto_discovered": False,
            "supports_streaming": False,
            "supports_function_calling": False,
            "supports_vision": False,
            "cost_per_input_token": 0.15,
            "cost_per_output_token": 0.0,
            "capabilities": {
                "context_window": 2048,
                "modalities": ["text"],
                "dimensions": 3072,
            },
            "tags": ["google", "embeddings"],
        },
        {
            "model_name": "vertex_ai/gemini-embedding-2",
            "name": "Vertex AI Gemini Embedding 2",
            "description": (
                "Gemini Embedding 2 on Vertex AI. 8,192 tokens, 3,072 "
                "dimensions. Text $0.20 per 1M tokens (seeded); image $0.45, "
                "audio $6.50, video $12.00."
            ),
            "usage": "Multimodal semantic search and RAG on Google Cloud",
            "mode": "embedding",
            # The gateway builds a Vertex URL from the project and region on
            # the chat, Messages and Gemini routes only: on the embeddings
            # route this row needs its own `api_url`.
            "api_url_auto_discovered": False,
            "supports_streaming": False,
            "supports_function_calling": False,
            "supports_vision": True,
            "cost_per_input_token": 0.2,
            "cost_per_output_token": 0.0,
            "capabilities": {
                "context_window": 8192,
                "modalities": ["text", "vision", "audio", "video"],
                "dimensions": 3072,
            },
            "tags": ["google", "embeddings", "multimodal"],
        },
    ]
