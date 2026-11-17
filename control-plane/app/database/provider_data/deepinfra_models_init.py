# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
DeepInfra Models — Cost-effective inference for open-source and frontier models.

Source: https://deepinfra.com/pricing
Model prefix: deepinfra/ (required)
Cost values: USD per 1M tokens.
"""


def get_models():
    return [
        # =====================================================================
        # DeepSeek Models
        # =====================================================================
        {
            "model_name": "deepinfra/deepseek-ai/DeepSeek-V3.2",
            "parameters": 671.0,
            "name": "DeepInfra DeepSeek V3.2",
            "description": "DeepSeek V3.2 MoE on DeepInfra. Best price-to-performance ratio.",
            "usage": "General chat, coding, analysis, content generation",
            "supports_vision": False,
            "cost_per_input_token": 0.26,
            "cost_per_output_token": 0.38,
            "capabilities": {
                "context_window": 160000,
                "modalities": ["text"],
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "open-weight", "cost-efficient",
                     "long-context", "frontier"],
        },
        {
            "model_name": "deepinfra/deepseek-ai/DeepSeek-V3.1",
            "parameters": 671.0,
            "name": "DeepInfra DeepSeek V3.1",
            "description": "DeepSeek V3.1 on DeepInfra. Strong general-purpose MoE model.",
            "usage": "General chat, coding, content generation, analysis",
            "supports_vision": False,
            "cost_per_input_token": 0.25,
            "cost_per_output_token": 0.95,
            "capabilities": {
                "context_window": 163840,
                "modalities": ["text"],
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "open-weight", "cost-efficient",
                     "long-context", "large"],
        },
        {
            "model_name": "deepinfra/deepseek-ai/DeepSeek-R1-0528",
            "parameters": 671.0,
            "name": "DeepInfra DeepSeek R1 0528",
            "description": "DeepSeek R1 reasoning model on DeepInfra. Chain-of-thought.",
            "usage": "Complex reasoning, math, coding, analysis",
            "supports_vision": False,
            "cost_per_input_token": 0.50,
            "cost_per_output_token": 2.15,
            "capabilities": {
                "context_window": 160000,
                "modalities": ["text"],
            },
            "tags": ["reasoning", "open-weight", "cost-efficient",
                     "long-context", "frontier"],
        },

        # =====================================================================
        # Qwen Models
        # =====================================================================
        {
            "model_name": "deepinfra/Qwen/Qwen3-Coder-480B-A35B-Instruct-Turbo",
            "name": "DeepInfra Qwen3 Coder 480B Turbo",
            "description": (
                "Qwen3 Coder 480B Turbo on DeepInfra, the replacement "
                "DeepInfra's API names for the deprecated non-Turbo id. 262K "
                "context; tools, JSON, structured output. The maximum output "
                "is not published."
            ),
            "usage": "Coding agents, repo-scale edits",
            "supports_vision": False,
            "cost_per_input_token": 0.3,
            "cost_per_output_token": 1.0,
            "capabilities": {
                "context_window": 262144,
                "modalities": ["text"],
                "supports_function_calling": True,
                "supports_response_schema": True,
            },
            "tags": ["qwen", "coding", "function-calling"],
        },
        {
            "model_name": "deepinfra/Qwen/Qwen3-235B-A22B-Instruct-2507",
            "parameters": 235.0,
            "name": "DeepInfra Qwen3 235B",
            "description": "Qwen3 235B MoE (22B active). Multilingual reasoning model.",
            "usage": "Multilingual tasks, reasoning, coding, general chat",
            "supports_vision": False,
            "cost_per_input_token": 0.09,
            "cost_per_output_token": 0.55,
            "capabilities": {
                "context_window": 262144,
                "modalities": ["text"],
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "open-weight", "multilingual",
                     "cost-efficient", "long-context", "large"],
        },
        {
            "model_name": "deepinfra/Qwen/Qwen3-32B",
            "parameters": 32.0,
            "name": "DeepInfra Qwen3 32B",
            "description": "Qwen3 32B dense model. Fast, multilingual, strong reasoning.",
            "usage": "General tasks, reasoning, multilingual, cost-efficient",
            "supports_vision": False,
            "cost_per_input_token": 0.08,
            "cost_per_output_token": 0.28,
            "capabilities": {
                "context_window": 40000,
                "modalities": ["text"],
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "open-weight", "multilingual",
                     "cost-efficient", "medium"],
        },

        # =====================================================================
        # Llama Models
        # =====================================================================
        {
            "model_name": "deepinfra/meta-llama/Llama-4-Scout-17B-16E-Instruct",
            "parameters": 109.0,
            "name": "DeepInfra Llama 4 Scout",
            "description": "Meta Llama 4 Scout MoE (17B active / 16 experts). Efficient multimodal.",
            "usage": "General chat, coding, image analysis, cost-effective tasks",
            "supports_vision": True,
            "cost_per_input_token": 0.1,
            "cost_per_output_token": 0.3,
            "capabilities": {
                "context_window": 327680,
                "modalities": ["text", "images"],
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "open-weight", "multimodal",
                     "cost-efficient", "cheap", "long-context"],
        },
        {
            "model_name": "deepinfra/meta-llama/Llama-3.3-70B-Instruct-Turbo",
            "name": "DeepInfra Llama 3.3 70B Turbo",
            "description": (
                "Llama 3.3 70B Turbo on DeepInfra, the replacement "
                "DeepInfra's API names for the deprecated non-Turbo id. 131K "
                "context; tools, JSON, structured output. The maximum output "
                "is not published."
            ),
            "usage": "Open-weight chat at low cost",
            "supports_vision": False,
            "cost_per_input_token": 0.1,
            "cost_per_output_token": 0.32,
            "capabilities": {
                "context_window": 131072,
                "modalities": ["text"],
                "supports_function_calling": True,
                "supports_response_schema": True,
            },
            "tags": ["meta", "llama", "open-weight", "function-calling"],
        },

        # =====================================================================
        # Other Chat Models
        # =====================================================================
        {
            "model_name": "deepinfra/openai/gpt-oss-120b",
            "parameters": 120.0,
            "name": "DeepInfra GPT-OSS 120B",
            "description": "OpenAI's open-source reasoning model on DeepInfra infrastructure.",
            "usage": "Reasoning, coding, tool calling, agentic workflows",
            "supports_vision": False,
            "cost_per_input_token": 0.037,
            "cost_per_output_token": 0.17,
            "capabilities": {
                "context_window": 131072,
                "modalities": ["text"],
                "supports_response_schema": True,
            },
            "tags": ["reasoning", "open-weight", "function-calling",
                     "long-context"],
        },
        {
            "model_name": "deepinfra/mistralai/Mistral-Small-3.2-24B-Instruct-2506",
            "parameters": 24.0,
            "name": "DeepInfra Mistral Small 3.2",
            "description": "Mistral Small 3.2 24B. Efficient, fast, multilingual.",
            "usage": "General chat, multilingual, coding, cost-efficient tasks",
            "supports_vision": False,
            "cost_per_input_token": 0.075,
            "cost_per_output_token": 0.2,
            "capabilities": {
                "context_window": 128000,
                "modalities": ["text"],
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "open-weight", "multilingual",
                     "cost-efficient", "cheap", "medium"],
        },

        # =====================================================================
        # Embedding Models
        # =====================================================================
        {
            "model_name": "deepinfra/BAAI/bge-en-icl",
            "parameters": 7.0,
            "name": "DeepInfra BGE EN ICL",
            "description": "BAAI BGE English embedding model. In-context learning support.",
            "usage": "Text embeddings, semantic search, RAG applications",
            "mode": "embedding",
            "supports_vision": False,
            "cost_per_input_token": 0.01,
            "cost_per_output_token": 0.00,
            "capabilities": {
                "context_window": 8192,
                "modalities": ["text"],
            },
            "tags": ["embedding", "open-weight", "cost-efficient", "small"],
        },
        {
            "model_name": "deepinfra/Qwen/Qwen3-Embedding-8B",
            "name": "DeepInfra Qwen3 Embedding 8B",
            "description": (
                "Qwen3 Embedding 8B on DeepInfra, the platform's current "
                "large embedding model. 32,768-token input."
            ),
            "usage": "Semantic search and RAG",
            "mode": "embedding",
            "supports_vision": False,
            "cost_per_input_token": 0.01,
            "cost_per_output_token": 0.0,
            "capabilities": {
                "context_window": 32768,
                "modalities": ["text"],
            },
            "tags": ["embeddings", "qwen"],
        },

        # =====================================================================
        # Reranker Model
        # =====================================================================
        {
            "model_name": "deepinfra/Qwen/Qwen3-Reranker-0.6B",
            "parameters": 0.6,
            "name": "DeepInfra Qwen3 Reranker",
            "description": "Qwen3 Reranker 0.6B. Lightweight, fast document reranking.",
            "usage": "Search result reranking, RAG pipeline optimization",
            "mode": "rerank",
            "supports_vision": False,
            "cost_per_input_token": 0.01,
            "cost_per_output_token": 0.00,
            "capabilities": {
                "context_window": 32768,
                "modalities": ["text"],
            },
            "tags": ["reranker", "open-weight", "cost-efficient",
                     "cheap", "tiny"],
        },
    ]
