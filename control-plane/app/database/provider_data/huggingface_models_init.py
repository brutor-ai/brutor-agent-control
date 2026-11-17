# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
HuggingFace Models — HuggingFace Inference API via router.huggingface.co.

Auth: HF_TOKEN (HuggingFace API token)
Cost values: USD per 1M tokens (varies by model and endpoint type).

All models use api_url=https://router.huggingface.co/v1 with
api_url_auto_discovered=False so the correct router
endpoint instead of the legacy api-inference.huggingface.co.

Only models verified as available on HF Inference API (chat/completions)
are included. Models disabled by default.
"""

# HuggingFace Inference API router endpoint
HF_ROUTER_URL = "https://router.huggingface.co/v1"


def get_models():
    return [
        # =====================================================================
        # Serverless Inference API — Chat Models
        # Verified available on router.huggingface.co/v1/chat/completions
        # =====================================================================
        {
            "model_name": "meta-llama/Llama-3.3-70B-Instruct",
            "parameters": 70.0,
            "name": "HuggingFace Llama 3.3 70B",
            "description": "Meta Llama 3.3 70B via HuggingFace Inference API.",
            "usage": "General chat, coding, analysis — serverless HF inference",
            "supports_vision": False,
            "api_url": HF_ROUTER_URL,
            "api_url_auto_discovered": False,
            "cost_per_input_token": 0.36,
            "cost_per_output_token": 0.36,
            "capabilities": {
                "context_window": 128000,
                "modalities": ["text"],
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "huggingface", "open-weight",
                     "long-context", "large"],
        },
        {
            "model_name": "Qwen/Qwen3-32B",
            "parameters": 32.0,
            "name": "HuggingFace Qwen3 32B",
            "description": "Alibaba Qwen3 32B via HuggingFace. Multilingual reasoning.",
            "usage": "Reasoning, multilingual tasks, coding",
            "supports_vision": False,
            "api_url": HF_ROUTER_URL,
            "api_url_auto_discovered": False,
            "cost_per_input_token": 0.20,
            "cost_per_output_token": 0.20,
            "capabilities": {
                "context_window": 40960,
                "modalities": ["text"],
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "huggingface", "open-weight",
                     "multilingual", "medium"],
        },
        {
            "model_name": "deepseek-ai/DeepSeek-R1",
            "parameters": 685.0,
            "name": "HuggingFace DeepSeek R1",
            "description": "DeepSeek R1 via HuggingFace. Advanced reasoning model.",
            "usage": "Reasoning, math, coding — chain-of-thought",
            "supports_vision": False,
            "api_url": HF_ROUTER_URL,
            "api_url_auto_discovered": False,
            "cost_per_input_token": 0.55,
            "cost_per_output_token": 2.19,
            "capabilities": {
                "context_window": 65536,
                "modalities": ["text"],
            },
            "tags": ["reasoning", "huggingface", "open-weight",
                     "chain-of-thought", "large"],
        },
        {
            "model_name": "google/gemma-3-27b-it",
            "parameters": 27.0,
            "name": "HuggingFace Gemma 3 27B",
            "description": "Google Gemma 3 27B IT via HuggingFace. Strong reasoning.",
            "usage": "General chat, reasoning, multilingual tasks",
            "supports_vision": False,
            "api_url": HF_ROUTER_URL,
            "api_url_auto_discovered": False,
            "cost_per_input_token": 0.10,
            "cost_per_output_token": 0.10,
            "capabilities": {
                "context_window": 128000,
                "modalities": ["text"],
            },
            "tags": ["huggingface", "open-weight",
                     "cost-efficient", "medium"],
        },

    ]
