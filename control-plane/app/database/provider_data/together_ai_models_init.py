# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Together AI Models — Open-source model hosting platform.

Model prefix: together_ai/ (required)
Cost values: USD per 1M tokens.
"""

from app.constants.llm import LLModelMode


def get_models():
    return [
        # Chat models
        {
            "model_name": "together_ai/openai/gpt-oss-120b",
            "name": "Together GPT-OSS 120B",
            "description": (
                "OpenAI's open-weight GPT-OSS 120B on Together's serverless "
                "API. 131K context; function calling, structured output. The "
                "maximum output is not published."
            ),
            "usage": "Open-weight chat and agents",
            "supports_vision": False,
            "cost_per_input_token": 0.15,
            "cost_per_output_token": 0.6,
            "capabilities": {
                "context_window": 131072,
                "modalities": ["text"],
                "supports_function_calling": True,
                "supports_response_schema": True,
            },
            "tags": ["open-weight", "function-calling"],
        },
        {
            "model_name": "together_ai/deepseek-ai/DeepSeek-V4.1-Flash-1017",
            "name": "Together DeepSeek V4.1 Flash",
            "description": (
                "DeepSeek V4.1 Flash (1017) on Together's serverless API. 1M "
                "context; function calling, structured output. The maximum "
                "output is not published."
            ),
            "usage": "Low-cost chat and coding",
            "supports_vision": False,
            "cost_per_input_token": 0.3,
            "cost_per_output_token": 1.2,
            "capabilities": {
                "context_window": 1000000,
                "modalities": ["text"],
                "supports_function_calling": True,
                "supports_response_schema": True,
            },
            "tags": ["deepseek", "function-calling", "long-context"],
        },
        {
            "model_name": "together_ai/Qwen/Qwen3.5-9B",
            "name": "Together Qwen3.5 9B",
            "description": (
                "Qwen3.5 9B on Together's serverless API. 262K context; "
                "function calling, structured output, vision. The maximum "
                "output is not published."
            ),
            "usage": "Cheap, fast chat with image input",
            "supports_vision": True,
            "cost_per_input_token": 0.17,
            "cost_per_output_token": 0.25,
            "capabilities": {
                "context_window": 262144,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_response_schema": True,
            },
            "tags": ["qwen", "function-calling", "vision", "cheap"],
        },
        {
            "model_name": "together_ai/moonshotai/Kimi-K3.1-Pro",
            "name": "Together Kimi K3.1 Pro",
            "description": (
                "Moonshot Kimi K3.1 Pro on Together's serverless API. 1M "
                "context; function calling, structured output, vision. The "
                "maximum output is not published."
            ),
            "usage": "Coding, agents, long-context analysis",
            "supports_vision": True,
            "cost_per_input_token": 2.0,
            "cost_per_output_token": 10.0,
            "capabilities": {
                "context_window": 1048576,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_response_schema": True,
            },
            "tags": ["kimi", "function-calling", "vision", "long-context"],
        },
        {
            "model_name": "together_ai/zai-org/GLM-5.3",
            "name": "Together GLM-5.3",
            "description": (
                "Z.ai GLM-5.3 on Together's serverless API. 1M context; "
                "function calling, structured output. The maximum output is "
                "not published."
            ),
            "usage": "Coding and agents",
            "supports_vision": False,
            "cost_per_input_token": 1.4,
            "cost_per_output_token": 4.4,
            "capabilities": {
                "context_window": 1048575,
                "modalities": ["text"],
                "supports_function_calling": True,
                "supports_response_schema": True,
            },
            "tags": ["glm", "function-calling", "long-context"],
        },
        # Text completion models
    ]
