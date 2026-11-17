# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Databricks Models — Databricks Model Serving (DBRX, hosted open models).

Model prefix: databricks/ (required)
Auth: DATABRICKS_API_KEY + DATABRICKS_API_BASE (workspace URL)
Cost values: USD per 1M tokens (Databricks Foundation Model API pricing).

NOTE: Models available depend on the Databricks workspace configuration.
Custom fine-tuned models and provisioned throughput endpoints will have
different names. All models disabled by default; require Databricks workspace.
"""


def get_models():
    return [
        # =====================================================================
        # DBRX (Databricks' Own Models)
        # =====================================================================
        {
            "model_name": "databricks/databricks-llama-4-maverick",
            "name": "Databricks Llama 4 Maverick",
            "description": (
                "Meta Llama 4 Maverick on Databricks Foundation Model APIs, "
                "Databricks' named replacement for the retired DBRX and "
                "Mixtral endpoints. 8,192 output tokens; text and image "
                "input. The context window is not published. Databricks "
                "prices in DBU per 1M tokens (7.143 / 21.429); the USD figure"
                " uses the pricing page's $0.07 per DBU and varies by cloud, "
                "region and tier."
            ),
            "usage": "Chat and vision inside a Databricks workspace",
            "supports_vision": True,
            "cost_per_input_token": 0.5,
            "cost_per_output_token": 1.5,
            "capabilities": {
                "max_output_tokens": 8192,
                "modalities": ["text", "image"],
            },
            "tags": ["meta", "llama", "vision"],
        },

        # =====================================================================
        # Foundation Model API — Meta Llama
        # =====================================================================
        {
            "model_name": "databricks/databricks-meta-llama-3-3-70b-instruct",
            "parameters": 70.0,
            "name": "Databricks Llama 3.3 70B",
            "description": "Meta Llama 3.3 70B via Databricks Foundation Model API.",
            "usage": "General chat, coding, analysis — Databricks-managed",
            "supports_vision": False,
            "cost_per_input_token": 0.5,
            "cost_per_output_token": 1.5,
            "capabilities": {
                "context_window": 128000,
                "modalities": ["text"],
                "supports_response_schema": True,
                "max_output_tokens": 8192,
            },
            "tags": ["function-calling", "databricks", "open-weight",
                     "long-context", "large"],
        },
        {
            "model_name": "databricks/databricks-meta-llama-3-1-8b-instruct",
            "parameters": 8.0,
            "name": "Databricks Llama 3.1 8B",
            "description": "Meta Llama 3.1 8B via Databricks. Cost-effective inference.",
            "usage": "Simple tasks, classification, routing, data processing",
            "supports_vision": False,
            "cost_per_input_token": 0.15,
            "cost_per_output_token": 0.45,
            "capabilities": {
                "context_window": 128000,
                "modalities": ["text"],
                "max_output_tokens": 8192,
            },
            "tags": ["databricks", "open-weight", "cost-efficient", "small"],
        },

        # =====================================================================
        # Foundation Model API — Other Models
        # =====================================================================

        # =====================================================================
        # Embedding Models
        # =====================================================================
        {
            "model_name": "databricks/databricks-bge-large-en",
            "parameters": 0.33,
            "name": "Databricks BGE Large EN",
            "description": "BAAI BGE Large English embeddings via Databricks.",
            "usage": "Text embeddings, vector search, RAG on Databricks",
            "mode": "embedding",
            "supports_vision": False,
            "cost_per_input_token": 0.10,
            "cost_per_output_token": 0.00,
            "capabilities": {
                "context_window": 512,
                "modalities": ["text"],
            },
            "tags": ["embedding", "databricks", "enterprise", "small"],
        },
        {
            "model_name": "databricks/databricks-gte-large-en",
            "parameters": 0.33,
            "name": "Databricks GTE Large EN",
            "description": "Alibaba GTE Large English embeddings via Databricks.",
            "usage": "Text embeddings, semantic search, Databricks-native RAG",
            "mode": "embedding",
            "supports_vision": False,
            "cost_per_input_token": 0.13,
            "cost_per_output_token": 0.00,
            "capabilities": {
                "context_window": 8192,
                "modalities": ["text"],
            },
            "tags": ["embedding", "databricks", "enterprise", "small"],
        },
    ]
