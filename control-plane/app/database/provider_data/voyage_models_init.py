# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Voyage AI Models — Premium embedding models.

Model prefix: voyage/ (required)
Cost values: USD per 1M tokens.
"""

from app.constants.llm import LLModelMode


def get_models():
    return [
        {
            "model_name": "voyage/voyage-4",
            "name": "Voyage 4",
            "description": (
                "Voyage 4 embedding model. 32,000-token input. 1,024 "
                "dimensions by default."
            ),
            "usage": "General-purpose retrieval and RAG",
            "mode": "embedding",
            "cost_per_input_token": 0.06,
            "cost_per_output_token": 0.0,
            "capabilities": {
                "context_window": 32000,
                "modalities": ["text"],
                "dimensions": 1024,
            },
            "tags": ["embeddings"],
        },
        {
            "model_name": "voyage/voyage-4-lite",
            "name": "Voyage 4 Lite",
            "description": "Voyage 4 Lite embedding model. 32,000-token input.",
            "usage": "Low-cost, high-volume retrieval",
            "mode": "embedding",
            "cost_per_input_token": 0.02,
            "cost_per_output_token": 0.0,
            "capabilities": {
                "context_window": 32000,
                "modalities": ["text"],
            },
            "tags": ["embeddings"],
        },
        {
            "model_name": "voyage/voyage-code-4",
            "name": "Voyage Code 4",
            "description": "Voyage Code 4 embedding model. 32,000-token input.",
            "usage": "Code search and code RAG",
            "mode": "embedding",
            "cost_per_input_token": 0.12,
            "cost_per_output_token": 0.0,
            "capabilities": {
                "context_window": 32000,
                "modalities": ["text"],
            },
            "tags": ["embeddings"],
        },
    ]
