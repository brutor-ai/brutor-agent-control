# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
SambaNova Cloud Models — Ultra-fast inference on custom RDU hardware.

Source: https://docs.sambanova.ai/cloud/docs/models/sambacloud-models
Model prefix: sambanova/ (required)
Cost values: USD per 1M tokens.
"""


def get_models():
    return [
        # =====================================================================
        # Production Chat Models
        # =====================================================================
        {
            "model_name": "sambanova/Meta-Llama-3.3-70B-Instruct",
            "parameters": 70.0,
            "name": "SambaNova Llama 3.3 70B",
            "description": "Meta Llama 3.3 70B on SambaNova RDU. Fast general-purpose model.",
            "usage": "General chat, coding, content generation, analysis",
            "supports_vision": False,
            "cost_per_input_token": 0.6,
            "cost_per_output_token": 1.2,
            "capabilities": {
                "context_window": 131072,
                "modalities": ["text"],
                "supports_response_schema": True,
                "max_output_tokens": 3072,
            },
            "tags": ["function-calling", "fast-inference", "open-weight",
                     "long-context", "large"],
        },

        # =====================================================================
        # Preview Chat Models
        # =====================================================================
        {
            "model_name": "sambanova/gemma-4-31B-it",
            "name": "SambaNova Gemma 4 31B (Preview)",
            "description": (
                "Google Gemma 4 31B on SambaNova (Preview), SambaNova's named"
                " replacement for Llama 4 Maverick. 131K context and output; "
                "text, image and video input. Tool and JSON support are not "
                "stated."
            ),
            "usage": "Open-weight multimodal chat",
            "supports_vision": True,
            "cost_per_input_token": 0.38,
            "cost_per_output_token": 1.15,
            "capabilities": {
                "context_window": 131072,
                "max_output_tokens": 131072,
                "modalities": ["text", "image", "video"],
            },
            "tags": ["gemma", "open-weight", "vision", "preview"],
        },
        {
            "model_name": "sambanova/gpt-oss-120b",
            "parameters": 120.0,
            "name": "SambaNova GPT-OSS 120B",
            "description": "OpenAI's open-source reasoning model. 700+ tok/s on SambaNova.",
            "usage": "Reasoning, coding, tool calling, agentic workflows",
            "supports_vision": False,
            "cost_per_input_token": 0.22,
            "cost_per_output_token": 0.59,
            "capabilities": {
                "context_window": 128000,
                "modalities": ["text"],
                "supports_response_schema": True,
            },
            "tags": ["reasoning", "fast-inference", "open-weight",
                     "cost-efficient", "preview"],
        },
        {
            "model_name": "sambanova/MiniMax-M3.1-Flash",
            "name": "SambaNova MiniMax M3.1 Flash (Preview)",
            "description": (
                "MiniMax M3.1 Flash on SambaNova (Preview). 1M context and "
                "output; text and image input."
            ),
            "usage": "Long-context chat with image input",
            "supports_vision": True,
            "cost_per_input_token": 0.3,
            "cost_per_output_token": 1.2,
            "capabilities": {
                "context_window": 1048576,
                "max_output_tokens": 1048576,
                "modalities": ["text", "image"],
            },
            "tags": ["minimax", "vision", "long-context", "preview"],
        },

        # =====================================================================
        # Embedding Model
        # =====================================================================
    ]
