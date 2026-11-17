# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
AWS Bedrock (Converse API) — Model Definitions for Brutor AI Platform
Model prefix: bedrock/converse/

AWS Bedrock Converse API provides a UNIFIED request/response format across
all Bedrock models. This is the recommended API for new applications as it:
  - Uses a single JSON schema regardless of provider
  - Supports tool/function calling natively
  - Supports extended thinking (reasoning)
  - Supports guardrails integration
  - Supports document/image inputs consistently

Use bedrock/converse/ prefix for: unified interface, tool calling, guardrails,
extended thinking, and when you want provider-agnostic code.

NOTE: Requires an AWS access key, its secret and a region, in the model's provider configuration.
Models are disabled by default (require AWS credentials to activate).
Models using cross-region inference profiles use us. prefix (e.g., us.anthropic...).
"""

from app.constants.llm import LLModelMode, LLModelProvider


def get_models():
    return [

        # ---------------------------------------------------------------
        # Anthropic Claude on Bedrock (Converse API)
        # ---------------------------------------------------------------
        {
            "model_name": "bedrock/converse/us.anthropic.claude-sonnet-5-5",
            "name": "Bedrock Converse Claude Sonnet 5.5",
            "description": (
                "Claude Sonnet 5.5 on AWS Bedrock (Converse API, US inference"
                " profile: the global price plus 10%). 1M context, 128K "
                "output. Vision, tool use, adaptive thinking, structured "
                "output. `tool_choice` auto / none only."
            ),
            "usage": "Production chat, coding, agents",
            "default_max_tokens": 8192,
            "cost_per_input_token": 2.2,
            "cost_per_output_token": 11.0,
            "supports_vision": True,
            "capabilities": {
                "context_window": 1000000,
                "max_output_tokens": 128000,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "vision", "reasoning", "long-context"],
        },
        {
            "model_name": "bedrock/converse/us.anthropic.claude-haiku-5-5",
            "name": "Bedrock Converse Claude Haiku 5.5",
            "description": (
                "Claude Haiku 5.5 on AWS Bedrock (Converse API, US inference "
                "profile: the global price plus 10%). 1M context, 128K "
                "output. Vision, tool use, adaptive thinking, structured "
                "output. $0.11 / $0.55 per 1M for prompts up to 100,000 "
                "tokens (seeded); $0.55 / $2.75 above."
            ),
            "usage": "High-volume chat, classification, extraction",
            "default_max_tokens": 8192,
            "cost_per_input_token": 0.11,
            "cost_per_output_token": 0.55,
            "supports_vision": True,
            "capabilities": {
                "context_window": 1000000,
                "max_output_tokens": 128000,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "vision", "reasoning", "long-context", "cheap"],
            "timeout": 30.0,
        },
        {
            "model_name": "bedrock/converse/us.anthropic.claude-opus-5-5",
            "name": "Bedrock Converse Claude Opus 5.5",
            "description": (
                "Claude Opus 5.5 on AWS Bedrock (Converse API, US inference "
                "profile: the global price plus 10%). 1M context, 128K "
                "output. Vision, tool use, adaptive thinking (always on), "
                "structured output. `tool_choice` auto / none only."
            ),
            "usage": "Deep analysis, complex agentic work",
            "default_max_tokens": 8192,
            "cost_per_input_token": 4.4,
            "cost_per_output_token": 22.0,
            "supports_vision": True,
            "capabilities": {
                "context_window": 1000000,
                "max_output_tokens": 128000,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "vision", "reasoning", "long-context"],
            "timeout": 300.0,
        },
        {
            "model_name": "bedrock/converse/us.anthropic.claude-opus-4-6-v1",
            "parameters": 1500.0,
            "name": "Bedrock Converse Claude Opus 4.6",
            "description": "Anthropic Claude Opus 4.6 via Bedrock Converse API. Latest, most intelligent.",
            "usage": "Most demanding enterprise reasoning, research on AWS",
            "default_max_tokens": 8192,
            "cost_per_input_token": 5.5,
            "cost_per_output_token": 27.5,
            "capabilities": {
                "context_window": 1000000,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_prompt_caching": True,
                "supports_pdf_input": True,
                "supports_computer_use": True,
                "max_output_tokens": 128000,
            },
            "supports_vision": True,
            "timeout": 300.0,
            "tags": ["anthropic", "claude", "converse-api", "flagship", "latest"],
        },
        {
            "model_name": "bedrock/converse/us.anthropic.claude-sonnet-4-6",
            "parameters": 1500.0,
            "name": "Bedrock Converse Claude Sonnet 4.6",
            "description": "Anthropic Claude Sonnet 4.6 via Bedrock Converse API. Latest Sonnet.",
            "usage": "Latest Claude capabilities with unified Converse interface",
            "default_max_tokens": 8192,
            "cost_per_input_token": 3.3,
            "cost_per_output_token": 16.5,
            "capabilities": {
                "context_window": 1000000,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_prompt_caching": True,
                "supports_pdf_input": True,
                "supports_computer_use": True,
                "max_output_tokens": 64000,
            },
            "supports_vision": True,
            "tags": ["anthropic", "claude", "converse-api", "latest"],
        },

        # ---------------------------------------------------------------
        # Amazon Nova on Bedrock (Converse API)
        # ---------------------------------------------------------------
        {
            "model_name": "bedrock/converse/us.amazon.nova-pro-v1:0",
            "parameters": 500.0,
            "name": "Bedrock Converse Nova Pro",
            "description": "Amazon Nova Pro via Bedrock Converse API. Unified interface for AWS first-party model.",
            "usage": "AWS-native enterprise tasks with unified API and guardrails",
            "default_max_tokens": 4096,
            "cost_per_input_token": 0.80,
            "cost_per_output_token": 3.20,
            "capabilities": {
                "context_window": 300000,
                "modalities": ["text", "vision", "video"],
                "supports_function_calling": True,
                "supports_response_schema": True,
                "max_output_tokens": 10000,
            },
            "supports_vision": True,
            "tags": ["amazon", "nova", "converse-api", "first-party"],
        },
        {
            "model_name": "bedrock/converse/us.amazon.nova-lite-v1:0",
            "parameters": 100.0,
            "name": "Bedrock Converse Nova Lite",
            "description": "Amazon Nova Lite via Bedrock Converse API. Fast, cost-effective.",
            "usage": "High-volume AWS tasks with unified API",
            "default_max_tokens": 4096,
            "cost_per_input_token": 0.06,
            "cost_per_output_token": 0.24,
            "capabilities": {
                "context_window": 300000,
                "modalities": ["text", "vision", "video"],
                "supports_function_calling": True,
                "max_output_tokens": 10000,
            },
            "supports_vision": True,
            "tags": ["amazon", "nova", "converse-api", "first-party", "cost-efficient"],
        },
        {
            "model_name": "bedrock/converse/us.amazon.nova-micro-v1:0",
            "parameters": 25.0,
            "name": "Bedrock Converse Nova Micro",
            "description": "Amazon Nova Micro via Bedrock Converse API. Ultra-low cost text model.",
            "usage": "Ultra-high-volume text tasks on AWS with unified interface",
            "default_max_tokens": 4096,
            "cost_per_input_token": 0.035,
            "cost_per_output_token": 0.14,
            "capabilities": {
                "context_window": 128000,
                "modalities": ["text"],
                "supports_function_calling": True,
                "max_output_tokens": 10000,
            },
            "supports_vision": False,
            "tags": ["amazon", "nova", "converse-api", "first-party", "cheapest"],
        },
        {
            "model_name": "bedrock/converse/us.amazon.nova-2-lite-v1:0",
            "parameters": 200.0,
            "name": "Bedrock Converse Nova 2 Lite",
            "description": "Amazon Nova 2 Lite via Bedrock Converse API. Reasoning-capable second-gen model.",
            "usage": "AWS reasoning tasks with unified interface and guardrails",
            "default_max_tokens": 4096,
            "cost_per_input_token": 0.33,
            "cost_per_output_token": 2.75,
            "capabilities": {
                "context_window": 1000000,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "max_output_tokens": 64000,
            },
            "supports_vision": True,
            "tags": ["amazon", "nova", "converse-api", "first-party", "reasoning"],
        },

        # ---------------------------------------------------------------
        # Meta Llama on Bedrock (Converse API)
        # ---------------------------------------------------------------

        # ---------------------------------------------------------------
        # Mistral on Bedrock (Converse API)
        # ---------------------------------------------------------------
        {
            "model_name": "bedrock/converse/mistral.mistral-large-3-675b-instruct",
            "name": "Bedrock Converse Mistral Large 3",
            "description": (
                "Mistral Large 3 on AWS Bedrock. 256K context, 32K output; "
                "text and image input, tool calling, structured output."
            ),
            "usage": "Multilingual chat, coding, agents on AWS",
            "default_max_tokens": 4096,
            "cost_per_input_token": 0.5,
            "cost_per_output_token": 1.5,
            "supports_vision": True,
            "capabilities": {
                "context_window": 256000,
                "max_output_tokens": 32000,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_response_schema": True,
            },
            "tags": ["mistral", "function-calling", "vision"],
        },

        # ---------------------------------------------------------------
        # DeepSeek on Bedrock (Converse API)
        # ---------------------------------------------------------------
        {
            "model_name": "bedrock/converse/deepseek.v3.2",
            "name": "Bedrock Converse DeepSeek V3.2",
            "description": (
                "DeepSeek V3.2 on AWS Bedrock. 164K context, 8K output; text "
                "only, tool calling, structured output. The model card lists "
                "no reasoning."
            ),
            "usage": "Low-cost chat and coding on AWS",
            "default_max_tokens": 4096,
            "supports_vision": False,
            "cost_per_input_token": 0.62,
            "cost_per_output_token": 1.85,
            "capabilities": {
                "context_window": 164000,
                "max_output_tokens": 8000,
                "modalities": ["text"],
                "supports_function_calling": True,
                "supports_response_schema": True,
            },
            "tags": ["deepseek", "function-calling", "cost-efficient"],
        },
    ]
