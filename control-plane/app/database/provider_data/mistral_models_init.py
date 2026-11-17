# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Mistral AI Models.

Model prefix: mistral/ (required)
Cost values: USD per 1M tokens.
"""


def get_models():
    return [
        {
            "model_name": "mistral/mistral-large-latest",
            "parameters": 123.0,
            "name": "Mistral Large",
            "description": (
                "Alias of Mistral's current Large model (today mistral-"
                "large-2512, Mistral Large 3). 256K context; vision, function"
                " calling, structured output. Mistral moves the alias; its "
                "named successor mistral-large-4 is seeded with an explicit "
                "id."
            ),
            "usage": "Complex reasoning, coding, multilingual tasks, enterprise applications",
            "supports_vision": True,
            "cost_per_input_token": 0.5,
            "cost_per_output_token": 1.5,
            "capabilities": {
                "context_window": 256000,
                "modalities": ["text", "vision"],
                "supports_response_schema": True,
                "supports_function_calling": True,
            },
            "tags": ["function-calling", "multilingual", "long-context", "large"],
        },
        {
            "model_name": "mistral/mistral-large-4",
            "name": "Mistral Large 4",
            "description": (
                "Mistral Large 4 (released 2026-11-05), Mistral's named "
                "replacement for Large 3. 1M context; vision, reasoning, "
                "function calling, structured output. The maximum output is "
                "not published."
            ),
            "usage": "Complex reasoning, coding, multilingual tasks",
            "cost_per_input_token": 0.7,
            "cost_per_output_token": 2.1,
            "supports_vision": True,
            "capabilities": {
                "context_window": 1000000,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_response_schema": True,
            },
            "tags": [
                "function-calling",
                "vision",
                "reasoning",
                "multilingual",
                "long-context",
                "latest",
            ],
        },
        {
            "model_name": "mistral/mistral-small-latest",
            "parameters": 22.0,
            "name": "Mistral Small",
            "description": (
                "Alias of mistral-small-2603 (Mistral Small 4). 256K context;"
                " vision, reasoning, function calling, structured output."
            ),
            "usage": "Everyday tasks, chatbots, summarization, classification",
            "supports_vision": True,
            "cost_per_input_token": 0.15,
            "cost_per_output_token": 0.6,
            "capabilities": {
                "context_window": 256000,
                "modalities": ["text", "vision"],
                "supports_response_schema": True,
                "supports_reasoning": True,
                "supports_function_calling": True,
            },
            "tags": ["function-calling", "multilingual", "cost-efficient", "medium"],
        },
        {
            "model_name": "mistral/codestral-latest",
            "parameters": 22.0,
            "name": "Mistral Codestral",
            "description": (
                "Alias of codestral-2508, Mistral's code model. 128K context;"
                " text only, function calling, structured output."
            ),
            "usage": "Code generation, code review, documentation, code completion",
            "supports_vision": False,
            "cost_per_input_token": 0.3,
            "cost_per_output_token": 0.9,
            "capabilities": {
                "context_window": 128000,
                "modalities": ["text"],
                "supports_response_schema": True,
                "supports_function_calling": True,
            },
            "tags": ["coding", "long-context", "large"],
        },
        {
            "model_name": "mistral/ministral-8b-latest",
            "name": "Mistral Ministral 3 8B",
            "description": (
                "Alias of ministral-8b-2512 (Ministral 3 8B). 256K context; "
                "vision, function calling, structured output. The maximum "
                "output is not published."
            ),
            "usage": "Low-cost chat, edge and high-volume tasks",
            "cost_per_input_token": 0.15,
            "cost_per_output_token": 0.15,
            "supports_vision": True,
            "capabilities": {
                "context_window": 256000,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_response_schema": True,
            },
            "tags": [
                "function-calling",
                "vision",
                "cheap",
                "cost-efficient",
                "long-context",
            ],
        },
    ]
