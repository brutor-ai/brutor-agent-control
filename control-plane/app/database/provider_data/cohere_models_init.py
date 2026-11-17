# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Cohere Models — Command and Embed families.

Model prefix: cohere/ (required)
Cost values: USD per 1M tokens.
"""

from app.constants.llm import LLModelMode


def get_models():
    return [
        {
            "model_name": "cohere/command-r-plus-08-2024",
            "name": "Cohere Command R+ (08-2024)",
            "description": (
                "Command R+ (08-2024): the dated model Cohere names in place "
                "of the deprecated undated alias. 128K context, 4K output, "
                "text only. The price is the one Cohere lists for the model "
                "family."
            ),
            "usage": "RAG, enterprise search, tool use",
            "supports_vision": False,
            "cost_per_input_token": 2.5,
            "cost_per_output_token": 10.0,
            "capabilities": {
                "context_window": 128000,
                "max_output_tokens": 4000,
                "modalities": ["text"],
            },
            "tags": ["rag", "enterprise"],
        },
        {
            "model_name": "cohere/command-r-08-2024",
            "name": "Cohere Command R (08-2024)",
            "description": (
                "Command R (08-2024): the dated model Cohere names in place "
                "of the deprecated undated alias. 128K context, 4K output, "
                "text only. The price is the one Cohere lists for the model "
                "family."
            ),
            "usage": "RAG, enterprise search, tool use",
            "supports_vision": False,
            "cost_per_input_token": 0.15,
            "cost_per_output_token": 0.6,
            "capabilities": {
                "context_window": 128000,
                "max_output_tokens": 4000,
                "modalities": ["text"],
            },
            "tags": ["rag", "enterprise"],
        },
        # Embedding models
        {
            "model_name": "cohere/embed-english-v3.0",
            "parameters": 140.0,
            "name": "Cohere Embed English v3",
            "description": "State-of-the-art English embedding model from Cohere.",
            "usage": "Semantic search, RAG, clustering, classification",
            "mode": LLModelMode.EMBEDDING.value,
            "supports_streaming": False,
            "supports_function_calling": False,
            "supports_vision": False,
            "default_temperature": 0.0,
            "default_max_tokens": 512,
            "cost_per_input_token": 0.1,
            "cost_per_output_token": 0.0,
            "capabilities": {
                "context_window": 512,
                "dimensions": 1024,
                "modalities": ["text"],
            },
            "tags": ["embeddings", "english"],
        },
        {
            "model_name": "cohere/embed-multilingual-v3.0",
            "parameters": 140.0,
            "name": "Cohere Embed Multilingual v3",
            "description": "Multilingual embedding model supporting 100+ languages.",
            "usage": "Multilingual semantic search, cross-language RAG",
            "mode": LLModelMode.EMBEDDING.value,
            "supports_streaming": False,
            "supports_function_calling": False,
            "supports_vision": False,
            "default_temperature": 0.0,
            "default_max_tokens": 512,
            "cost_per_input_token": 0.1,
            "cost_per_output_token": 0.0,
            "capabilities": {
                "context_window": 512,
                "dimensions": 1024,
                "modalities": ["text"],
            },
            "tags": ["embeddings", "multilingual"],
        },
        # Rerank model
        {
            "model_name": "cohere/rerank-english-v3.0",
            "parameters": 140.0,
            "name": "Cohere Rerank English v3",
            "description": "Reranking model to improve search relevance.",
            "usage": "Search relevance, RAG pipeline optimization",
            "mode": LLModelMode.RERANK.value,
            "supports_streaming": False,
            "supports_function_calling": False,
            "supports_vision": False,
            "default_temperature": 0.0,
            "default_max_tokens": 4096,
            "cost_per_input_token": 2.0,
            "cost_per_output_token": 0.0,
            "capabilities": {
                "context_window": 4096,
                "modalities": ["text"],
            },
            "tags": ["rerank", "rag", "search"],
        },
    ]
