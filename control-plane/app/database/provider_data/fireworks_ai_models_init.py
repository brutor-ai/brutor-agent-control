# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Fireworks AI Models — Fast inference platform.

Model prefix: fireworks_ai/ (required)
Cost values: USD per 1M tokens.
"""


def get_models():
    return [
        {
            "model_name": "fireworks_ai/accounts/fireworks/models/glm-5p3-flash",
            "name": "Fireworks GLM 5.3 Flash",
            "description": (
                "Z.ai GLM 5.3 Flash on Fireworks serverless. 1040K-token "
                "context; function calling, image input. The maximum output "
                "is not published."
            ),
            "usage": "Cheap, fast chat with image input",
            "supports_vision": True,
            "cost_per_input_token": 0.15,
            "cost_per_output_token": 0.5,
            "capabilities": {
                "context_window": 1040000,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
            },
            "tags": ["glm", "function-calling", "vision", "cheap"],
        },
        {
            "model_name": "fireworks_ai/accounts/fireworks/models/glm-5p3",
            "name": "Fireworks GLM-5.3",
            "description": (
                "Z.ai GLM-5.3 on Fireworks serverless. 1040K-token context; "
                "function calling; no image input. The maximum output is not "
                "published."
            ),
            "usage": "Coding and agents",
            "supports_vision": False,
            "cost_per_input_token": 1.4,
            "cost_per_output_token": 4.4,
            "capabilities": {
                "context_window": 1040000,
                "modalities": ["text"],
                "supports_function_calling": True,
            },
            "tags": ["glm", "function-calling", "long-context"],
        },
        {
            "model_name": "fireworks_ai/accounts/fireworks/models/kimi-k3p1-pro",
            "name": "Fireworks Kimi K3.1 Pro",
            "description": (
                "Moonshot Kimi K3.1 Pro on Fireworks serverless. 1040K-token "
                "context; function calling, image input. The maximum output "
                "is not published."
            ),
            "usage": "Coding, agents, long-context analysis",
            "supports_vision": True,
            "cost_per_input_token": 2.0,
            "cost_per_output_token": 10.0,
            "capabilities": {
                "context_window": 1040000,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
            },
            "tags": ["kimi", "function-calling", "vision", "long-context"],
        },
    ]
