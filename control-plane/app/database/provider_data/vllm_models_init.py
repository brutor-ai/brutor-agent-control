# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
vLLM Models — Self-hosted high-throughput inference via vLLM.

Model prefix: hosted_vllm/ (OpenAI-compatible hosted) or vllm/ (local)
Auth: Optional API key depending on deployment
Cost values: 0.00 (self-hosted, no per-token cost)

NOTE: These are representative models commonly deployed on vLLM servers.
The actual models depend on the user's vLLM deployment. All models are
disabled by default and require a running vLLM endpoint (api_base).
"""



# COST NOTE (2026-08-23): deliberately no per-token cost — vLLM is
# self-hosted; token-level provider cost is genuinely $0. Infra cost
# lives outside the token ledger.

def get_models():
    return [
        # =====================================================================
        # Large Chat Models
        # =====================================================================
        {
            "model_name": "hosted_vllm/meta-llama/Llama-3.3-70B-Instruct",
            "parameters": 70.0,
            "name": "vLLM Llama 3.3 70B",
            "description": "Meta Llama 3.3 70B served via vLLM. High-throughput inference.",
            "usage": "General chat, coding, analysis — self-hosted production",
            "supports_vision": False,
            "cost_per_input_token": 0.00,
            "cost_per_output_token": 0.00,
            "capabilities": {
                "context_window": 128000,
                "modalities": ["text"],
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "self-hosted", "open-weight",
                     "long-context", "large"],
        },
        {
            "model_name": "hosted_vllm/Qwen/Qwen3-32B",
            "parameters": 32.0,
            "name": "vLLM Qwen3 32B",
            "description": "Alibaba Qwen3 32B on vLLM. Strong multilingual reasoning.",
            "usage": "Reasoning, multilingual, coding — self-hosted",
            "supports_vision": False,
            "cost_per_input_token": 0.00,
            "cost_per_output_token": 0.00,
            "capabilities": {
                "context_window": 40960,
                "modalities": ["text"],
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "self-hosted", "open-weight",
                     "multilingual", "medium"],
        },
        {
            "model_name": "hosted_vllm/mistralai/Mistral-Small-3.2-24B-Instruct-2506",
            "parameters": 24.0,
            "name": "vLLM Mistral Small 3.2",
            "description": "Mistral Small 3.2 24B on vLLM. Efficient multilingual model.",
            "usage": "General chat, multilingual, coding — efficient self-hosted",
            "supports_vision": False,
            "cost_per_input_token": 0.00,
            "cost_per_output_token": 0.00,
            "capabilities": {
                "context_window": 128000,
                "modalities": ["text"],
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "self-hosted", "open-weight",
                     "multilingual", "medium"],
        },
        {
            "model_name": "hosted_vllm/deepseek-ai/DeepSeek-R1-Distill-Llama-70B",
            "parameters": 70.0,
            "name": "vLLM DeepSeek R1 Distill 70B",
            "description": "DeepSeek R1 distilled 70B on vLLM. Self-hosted reasoning.",
            "usage": "Complex reasoning, math, analysis — self-hosted",
            "supports_vision": False,
            "cost_per_input_token": 0.00,
            "cost_per_output_token": 0.00,
            "capabilities": {
                "context_window": 128000,
                "modalities": ["text"],
            },
            "tags": ["reasoning", "self-hosted", "open-weight",
                     "long-context", "large"],
        },

        # =====================================================================
        # Small/Medium Chat Models
        # =====================================================================
        {
            "model_name": "hosted_vllm/meta-llama/Llama-3.1-8B-Instruct",
            "parameters": 8.0,
            "name": "vLLM Llama 3.1 8B",
            "description": "Meta Llama 3.1 8B on vLLM. Lightweight, high-throughput.",
            "usage": "Simple tasks, classification, routing — high-volume self-hosted",
            "supports_vision": False,
            "cost_per_input_token": 0.00,
            "cost_per_output_token": 0.00,
            "capabilities": {
                "context_window": 128000,
                "modalities": ["text"],
            },
            "tags": ["self-hosted", "open-weight", "cost-efficient",
                     "small"],
        },
        {
            "model_name": "hosted_vllm/microsoft/Phi-4",
            "parameters": 14.0,
            "name": "vLLM Phi-4 14B",
            "description": "Microsoft Phi-4 14B on vLLM. Compact reasoning model.",
            "usage": "Reasoning, coding, math — efficient self-hosted",
            "supports_vision": False,
            "cost_per_input_token": 0.00,
            "cost_per_output_token": 0.00,
            "capabilities": {
                "context_window": 16384,
                "modalities": ["text"],
            },
            "tags": ["reasoning", "self-hosted", "open-weight", "medium"],
        },

        # =====================================================================
        # Embedding Models
        # =====================================================================
        {
            "model_name": "hosted_vllm/BAAI/bge-large-en-v1.5",
            "parameters": 0.33,
            "name": "vLLM BGE Large EN v1.5",
            "description": "BAAI BGE Large English embeddings served via vLLM.",
            "usage": "Text embeddings, semantic search, RAG — self-hosted",
            "mode": "embedding",
            "supports_vision": False,
            "cost_per_input_token": 0.00,
            "cost_per_output_token": 0.00,
            "capabilities": {
                "context_window": 512,
                "modalities": ["text"],
            },
            "tags": ["embedding", "self-hosted", "open-weight", "small"],
        },
    ]
