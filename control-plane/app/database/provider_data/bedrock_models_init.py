# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
AWS Bedrock (Invoke API) — Model Definitions for Brutor AI Platform
Model prefix: bedrock/

AWS Bedrock Invoke API uses provider-native request/response formats.
Each provider (Anthropic, Amazon, Meta, Mistral, Cohere) has its own
JSON schema for InvokeModel calls. The gateway's adapter handles the translation.

Use bedrock/ prefix for: Anthropic Claude (native format), Amazon Nova/Titan,
and cases where you need provider-specific features not available via Converse.

NOTE: Requires an AWS access key, its secret and a region, in the model's provider configuration.
No API key needed — uses IAM-based authentication.
Bedrock models are disabled by default (require AWS credentials to activate).
"""

from app.constants.llm import LLModelMode, LLModelProvider


# Parameters that Bedrock's Anthropic provider doesn't support
BEDROCK_ANTHROPIC_DROPS = ["presence_penalty", "frequency_penalty", "logprobs",
                            "logit_bias", "top_logprobs"]


def get_models():
    return [

        # ---------------------------------------------------------------
        # Anthropic Claude on Bedrock (Invoke API)
        # ---------------------------------------------------------------
        {
            "model_name": "bedrock/global.anthropic.claude-sonnet-5-5",
            "name": "Bedrock Claude Sonnet 5.5",
            "description": (
                "Claude Sonnet 5.5 on AWS Bedrock (Invoke API, global "
                "inference profile: the bare model id is not invocable on "
                "bedrock-runtime). 1M context, 128K output. Vision, tool use,"
                " adaptive thinking, structured output."
            ),
            "usage": "Production chat, coding, agents",
            "default_max_tokens": 8192,
            "additional_drop_params": BEDROCK_ANTHROPIC_DROPS,
            "cost_per_input_token": 2.0,
            "cost_per_output_token": 10.0,
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
            "model_name": "bedrock/global.anthropic.claude-haiku-5-5",
            "name": "Bedrock Claude Haiku 5.5",
            "description": (
                "Claude Haiku 5.5 on AWS Bedrock (Invoke API, global "
                "inference profile: the bare model id is not invocable on "
                "bedrock-runtime). 1M context, 128K output. Vision, tool use,"
                " adaptive thinking, structured output. $0.10 / $0.50 per 1M "
                "for prompts up to 100,000 tokens (seeded); $0.50 / $2.50 "
                "above."
            ),
            "usage": "High-volume chat, classification, extraction",
            "default_max_tokens": 8192,
            "additional_drop_params": BEDROCK_ANTHROPIC_DROPS,
            "cost_per_input_token": 0.1,
            "cost_per_output_token": 0.5,
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
            "model_name": "bedrock/global.anthropic.claude-opus-5-5",
            "name": "Bedrock Claude Opus 5.5",
            "description": (
                "Claude Opus 5.5 on AWS Bedrock (Invoke API, global inference"
                " profile: the bare model id is not invocable on bedrock-"
                "runtime). 1M context, 128K output. Vision, tool use, "
                "adaptive thinking (always on), structured output. "
                "`tool_choice` auto / none only."
            ),
            "usage": "Deep analysis, complex agentic work",
            "default_max_tokens": 8192,
            "additional_drop_params": BEDROCK_ANTHROPIC_DROPS,
            "cost_per_input_token": 4.0,
            "cost_per_output_token": 20.0,
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
            "model_name": "bedrock/global.anthropic.claude-opus-4-6-v1",
            "parameters": 1500.0,
            "name": "Bedrock Claude Opus 4.6",
            "description": "Anthropic Claude Opus 4.6 on AWS Bedrock (Invoke API). Latest most intelligent Claude.",
            "usage": "Most demanding enterprise tasks, research, complex reasoning on AWS",
            "default_max_tokens": 8192,
            "cost_per_input_token": 5.0,
            "cost_per_output_token": 25.0,
            "additional_drop_params": BEDROCK_ANTHROPIC_DROPS,
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
            "tags": ["anthropic", "claude", "aws-native", "flagship", "latest"],
        },
        {
            "model_name": "bedrock/global.anthropic.claude-sonnet-4-6",
            "parameters": 1500.0,
            "name": "Bedrock Claude Sonnet 4.6",
            "description": "Anthropic Claude Sonnet 4.6 on AWS Bedrock (Invoke API). Latest Sonnet on AWS.",
            "usage": "Latest Claude capabilities on AWS, enterprise workloads",
            "default_max_tokens": 8192,
            "cost_per_input_token": 3.0,
            "cost_per_output_token": 15.0,
            "additional_drop_params": BEDROCK_ANTHROPIC_DROPS,
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
            "tags": ["anthropic", "claude", "aws-native", "latest"],
        },

        # ---------------------------------------------------------------
        # Amazon Nova on Bedrock
        # ---------------------------------------------------------------
        {
            "model_name": "bedrock/amazon.nova-pro-v1:0",
            "parameters": 500.0,
            "name": "Bedrock Amazon Nova Pro",
            "description": "Amazon Nova Pro on Bedrock. AWS first-party model optimized for enterprise tasks.",
            "usage": "AWS-native enterprise workloads, multimodal tasks, document processing",
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
            "tags": ["amazon", "nova", "aws-native", "first-party"],
        },
        {
            "model_name": "bedrock/amazon.nova-lite-v1:0",
            "parameters": 100.0,
            "name": "Bedrock Amazon Nova Lite",
            "description": "Amazon Nova Lite on Bedrock. Fast, cost-effective AWS first-party model.",
            "usage": "High-volume AWS tasks, cost-sensitive multimodal processing",
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
            "tags": ["amazon", "nova", "aws-native", "first-party", "cost-efficient"],
        },
        {
            "model_name": "bedrock/amazon.nova-micro-v1:0",
            "parameters": 25.0,
            "name": "Bedrock Amazon Nova Micro",
            "description": "Amazon Nova Micro on Bedrock. Smallest AWS model, text-only, ultra-low cost.",
            "usage": "Ultra-high-volume text tasks, classification, routing on AWS",
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
            "tags": ["amazon", "nova", "aws-native", "first-party", "cheapest", "ultra-fast"],
        },
        {
            "model_name": "bedrock/global.amazon.nova-2-lite-v1:0",
            "parameters": 200.0,
            "name": "Bedrock Amazon Nova 2 Lite",
            "description": "Amazon Nova 2 Lite on Bedrock. Second-gen AWS model with reasoning support.",
            "usage": "AWS-native reasoning tasks, improved enterprise capabilities",
            "default_max_tokens": 4096,
            "cost_per_input_token": 0.3,
            "cost_per_output_token": 2.5,
            "capabilities": {
                "context_window": 1000000,
                "modalities": ["text", "vision"],
                "supports_function_calling": True,
                "supports_reasoning": True,
                "supports_response_schema": False,
                "max_output_tokens": 64000,
            },
            "supports_vision": True,
            "tags": ["amazon", "nova", "aws-native", "first-party", "reasoning"],
        },

        # ---------------------------------------------------------------
        # Meta Llama on Bedrock (Invoke API)
        # ---------------------------------------------------------------

        # ---------------------------------------------------------------
        # Mistral on Bedrock (Invoke API)
        # ---------------------------------------------------------------
        {
            "model_name": "bedrock/mistral.mistral-large-3-675b-instruct",
            "name": "Bedrock Mistral Large 3",
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
        # DeepSeek on Bedrock (Invoke API)
        # ---------------------------------------------------------------
        {
            "model_name": "bedrock/deepseek.v3.2",
            "name": "Bedrock DeepSeek V3.2",
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

        # ---------------------------------------------------------------
        # Bedrock Embeddings (Invoke API)
        # ---------------------------------------------------------------
        {
            "model_name": "bedrock/amazon.titan-embed-text-v2:0",
            "parameters": 150.0,
            "name": "Bedrock Titan Embed v2",
            "description": "Amazon Titan Text Embedding v2 on Bedrock. AWS first-party embeddings.",
            "usage": "Enterprise semantic search on AWS, RAG with AWS-native models",
            "mode": LLModelMode.EMBEDDING.value,
            "cost_per_input_token": 0.02,
            "cost_per_output_token": 0.0,
            "supports_function_calling": False,
            "supports_streaming": False,
            "supports_vision": False,
            "capabilities": {
                "context_window": 8192,
                "dimensions": 1024,
                "modalities": ["text"],
            },
            "tags": ["embedding", "amazon", "aws-native", "first-party"],
        },
        {
            "model_name": "bedrock/cohere.embed-english-v3",
            "parameters": 140.0,
            "name": "Bedrock Cohere Embed v3 English",
            "description": "Cohere Embed v3 English on AWS Bedrock.",
            "usage": "English-language semantic search on AWS",
            "mode": LLModelMode.EMBEDDING.value,
            "cost_per_input_token": 0.10,
            "cost_per_output_token": 0.0,
            "supports_function_calling": False,
            "supports_streaming": False,
            "supports_vision": False,
            "capabilities": {
                "context_window": 512,
                "dimensions": 1024,
                "modalities": ["text"],
            },
            "tags": ["embedding", "cohere", "aws-native"],
        },
    ]
