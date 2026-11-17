# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
NVIDIA NIM Models — NVIDIA Inference Microservices.

Model prefix: nvidia_nim/ (required)
Auth: NVIDIA_NIM_API_KEY + NVIDIA_NIM_API_BASE
Cost values: USD per 1M tokens (NVIDIA API Catalog pricing).

NOTE: NVIDIA NIM can be self-hosted (free) or accessed via NVIDIA API
Catalog (pay-per-token). These entries represent the API Catalog models.
Self-hosted NIM deployments will have api_base set per-deployment.
"""


def get_models():
    return [
        # =====================================================================
        # Llama Models
        # =====================================================================

        # =====================================================================
        # NVIDIA Nemotron Models
        # =====================================================================
        {
            "model_name": "nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b",
            "name": "NIM Nemotron 3 Ultra",
            "description": (
                "NVIDIA Nemotron 3 Ultra, NVIDIA's current Nemotron chat "
                "model. Up to 1M tokens of context; reasoning, tool calling, "
                "text input. NVIDIA publishes no price (the catalogue shows "
                "only a free endpoint): set the cost on import. The maximum "
                "output is not published."
            ),
            "usage": "Enterprise chat and agents on NVIDIA infrastructure",
            "supports_vision": False,
            "cost_per_input_token": None,
            "cost_per_output_token": None,
            "capabilities": {
                "context_window": 1000000,
                "modalities": ["text"],
                "supports_function_calling": True,
                "supports_reasoning": True,
            },
            "tags": ["nvidia", "nemotron", "function-calling", "reasoning"],
        },

        # =====================================================================
        # Other Chat Models
        # =====================================================================
        {
            "model_name": "nvidia_nim/mistralai/mistral-large-4-1t-a52b",
            "name": "NIM Mistral Large 4",
            "description": (
                "Mistral Large 4 on NVIDIA NIM. 1M tokens of combined input "
                "and output, at most 262,144 output; reasoning, function "
                "calling, JSON schema, image input. NVIDIA publishes no "
                "price: set the cost on import."
            ),
            "usage": "Multilingual reasoning and agents on NVIDIA infrastructure",
            "supports_vision": True,
            "cost_per_input_token": None,
            "cost_per_output_token": None,
            "capabilities": {
                "context_window": 1000000,
                "max_output_tokens": 262144,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_response_schema": True,
            },
            "tags": ["mistral", "function-calling", "vision", "reasoning"],
        },

        # =====================================================================
        # Embedding Models
        # =====================================================================
    ]
