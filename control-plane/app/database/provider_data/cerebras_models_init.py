# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Cerebras Inference Models — World's fastest AI inference on Wafer-Scale Engines.

Source: https://inference-docs.cerebras.ai/models/overview
Model prefix: cerebras/ (required)
Cost values: USD per 1M tokens.
"""


def get_models():
    return [
        # =====================================================================
        # Production Chat Models
        # =====================================================================
        {
            "model_name": "cerebras/gpt-oss-120b",
            "parameters": 120.0,
            "name": "Cerebras GPT-OSS 120B",
            "description": "OpenAI's open-source reasoning model. 3,000+ tok/s on Cerebras WSE.",
            "usage": "Reasoning, coding, tool calling, agentic workflows",
            "supports_vision": False,
            "cost_per_input_token": 0.35,
            "cost_per_output_token": 0.75,
            "capabilities": {
                "context_window": 131072,
                "modalities": ["text"],
                "supports_response_schema": True,
                "supports_reasoning": True,
                "supports_function_calling": True,
                "max_output_tokens": 40960,
            },
            "tags": ["reasoning", "fast-inference", "open-weight",
                     "long-context", "production"],
        },

        # =====================================================================
        # Preview Chat Models
        # =====================================================================
        {
            "model_name": "cerebras/qwen-3.8-27b",
            "name": "Cerebras Qwen 3.8 27B",
            "description": (
                "Qwen 3.8 27B on Cerebras. 131K context, 40,960 output on the"
                " paid tier (free tier 65,536 / 32,768); tools, structured "
                "output, reasoning, vision."
            ),
            "usage": "Ultra-fast chat and agents with image input",
            "supports_vision": True,
            "cost_per_input_token": 0.99,
            "cost_per_output_token": 1.49,
            "capabilities": {
                "context_window": 131072,
                "max_output_tokens": 40960,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_response_schema": True,
            },
            "tags": ["qwen", "function-calling", "vision", "reasoning"],
        },
    ]
