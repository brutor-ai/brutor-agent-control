# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
OpenAI Embedding Models.

No prefix needed for standard OpenAI embedding models.
Cost values: USD per 1M tokens.

Gemini embeddings: `gemini-embedding-2` and `gemini-embedding-001` are
seeded with the values on their Gemini API model pages; both answer under
the plain id (no `gemini/` prefix) on the native `:embedContent` and the
OpenAI-compat `/v1beta/openai/embeddings` surfaces. Google shut the older
text-embedding 004 model down on 2026-01-14; it is no longer seeded.
"""

from datetime import date

from app.constants.llm import LLModelMode


def get_models():
    return [
        {
            "model_name": "text-embedding-3-large",
            "parameters": 350.0,
            "name": "OpenAI Embedding 3 Large",
            "description": "OpenAI's most capable embedding model. 3072 dimensions.",
            "usage": "Semantic search, RAG, document retrieval, clustering, classification",
            "mode": LLModelMode.EMBEDDING.value,
            "supports_streaming": False,
            "supports_function_calling": False,
            "supports_vision": False,
            "default_temperature": 0.0,
            "default_max_tokens": 8191,
            "cost_per_input_token": 0.13,
            "cost_per_output_token": 0.0,
            "capabilities": {
                "context_window": 8191,
                "dimensions": 3072,
                "modalities": ["text"],
            },
            "tags": ["embeddings", "premium"],
        },
        {
            "model_name": "text-embedding-3-small",
            "parameters": 62.0,
            "name": "OpenAI Embedding 3 Small",
            "description": "Efficient OpenAI embedding model. 1536 dimensions at low cost. Alternative to the default Ollama nomic-embed-text.",
            "usage": "Semantic search, RAG, lightweight retrieval, semantic cache",
            "mode": LLModelMode.EMBEDDING.value,
            "enabled": True,
            "supports_streaming": False,
            "supports_function_calling": False,
            "supports_vision": False,
            "default_temperature": 0.0,
            "default_max_tokens": 8191,
            "cost_per_input_token": 0.02,
            "cost_per_output_token": 0.0,
            "capabilities": {
                "context_window": 8191,
                "dimensions": 1536,
                "modalities": ["text"],
            },
            "tags": ["embeddings", "cost-efficient", "semantic-cache"],
        },
        {
            "model_name": "text-embedding-ada-002",
            "parameters": 175.0,
            "name": "OpenAI Embedding Ada 002",
            "description": "Legacy OpenAI embedding model. Still widely deployed.",
            "usage": "Semantic search, RAG, legacy integrations",
            "mode": LLModelMode.EMBEDDING.value,
            "supports_streaming": False,
            "supports_function_calling": False,
            "supports_vision": False,
            "default_temperature": 0.0,
            "default_max_tokens": 8191,
            "cost_per_input_token": 0.1,
            "cost_per_output_token": 0.0,
            "capabilities": {
                "context_window": 8191,
                "dimensions": 1536,
                "modalities": ["text"],
            },
            "tags": ["embeddings", "legacy"],
            "enabled": False,
        },
        # Gemini embeddings (registered in this file with a provider override).
        #
        # gemini-embedding-2 — ai.google.dev/gemini-api/docs/models/gemini-embedding-2
        # (2026-10-16): "Stable: gemini-embedding-2", GA 2026-04-22 per the
        # changelog; input text/image/video/audio/PDF; 8,192 input tokens;
        # output 128-3072 (embeddings page: "By default, both models output a
        # 3072-dimensional embedding"). Pricing page, paid tier standard:
        # text input $0.20 / 1M tokens (image/audio/video priced separately).
        {
            "model_name": "gemini-embedding-2",
            "name": "Google Gemini Embedding 2",
            "description": "Google's current Gemini API embedding model (GA 2026-04-22) and the named replacement for gemini-embedding-001. Multimodal input (text, image, video, audio, PDF) into one embedding space; 3072 dimensions by default (flexible 128-3072).",
            "usage": "Semantic search, RAG, clustering, multimodal retrieval",
            "mode": LLModelMode.EMBEDDING.value,
            "provider": "gemini",  # Override provider from openai template
            "supports_streaming": False,
            "supports_function_calling": False,
            "supports_vision": False,
            "default_temperature": 0.0,
            "default_max_tokens": 8192,
            "cost_per_input_token": 0.20,
            "cost_per_output_token": 0.0,
            "capabilities": {
                "context_window": 8192,
                "dimensions": 3072,
                "modalities": ["text", "image", "video", "audio", "document"],
            },
            "tags": ["google", "embeddings", "multimodal"],
        },
        # gemini-embedding-001 — ai.google.dev/gemini-api/docs/models/gemini-embedding-001
        # (2026-10-16): "Stable: gemini-embedding-001", text only; 2,048 input
        # tokens; output 128-3072, default 3072. Deprecations page: shutdown
        # 2028-05-14, replacement gemini-embedding-2: recorded in the
        # retirement fields. A date that far off does not mark the catalog
        # entry deprecated (`catalog_deprecated`). Pricing page: $0.15 / 1M.
        {
            "model_name": "gemini-embedding-001",
            "name": "Google Gemini Embedding 001",
            "description": (
                "Google's text-only Gemini API embedding model (stable since "
                "2025-07-14). 3072 dimensions by default (flexible 128-3072)."
                " $0.15 per 1M input tokens."
            ),
            "usage": "Semantic search, RAG, clustering, classification",
            "mode": LLModelMode.EMBEDDING.value,
            "provider": "gemini",  # Override provider from openai template
            "supports_streaming": False,
            "supports_function_calling": False,
            "supports_vision": False,
            "default_temperature": 0.0,
            "default_max_tokens": 2048,
            "cost_per_input_token": 0.15,
            "cost_per_output_token": 0.0,
            "capabilities": {
                "context_window": 2048,
                "dimensions": 3072,
                "modalities": ["text"],
            },
            "tags": ["google", "embeddings"],
            "retirement_notes": "Google calls its shutdown dates the earliest possible dates.",
            "retirement_replacement_provider_key": "gemini",
            "retirement_replacement_model_name": "gemini-embedding-2",
            "retire_date": date(2028, 5, 14),
        },
    ]
