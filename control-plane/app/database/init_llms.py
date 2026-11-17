# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
LLM Model Seeding System — Main Orchestrator

This module provides the template-based model initialization system for the
Brutor AI Platform. It creates curated LLM model configurations for new tenants
using a provider-template architecture where:

1. PROVIDER_TEMPLATES define shared defaults per provider (auth, tags, drop_params, etc.)
2. MODEL_DEFAULTS define field-level defaults for all models
3. Per-provider data files (in provider-data/) define only what differs per model
4. build_model_config() merges everything into complete model configurations

Architecture:
    init_llms.py                    ← This file (orchestrator + templates)
    └── provider-data/
        ├── openai_models_init.py   ← OpenAI models
        ├── anthropic_models_init.py← Anthropic models
        ├── google_models_init.py   ← Google AI Studio models
        ├── mistral_models_init.py  ← Mistral models
        ├── cohere_models_init.py   ← Cohere models
        ├── xai_models_init.py      ← xAI models
        ├── deepseek_models_init.py ← DeepSeek models
        ├── moonshotai_models_init.py ← Moonshot AI (Kimi) models
        ├── minimax_models_init.py    ← MiniMax (M2.x text family) models
        ├── groq_models_init.py     ← Groq models
        ├── together_ai_models_init.py ← Together AI models
        ├── fireworks_ai_models_init.py ← Fireworks AI models
        ├── perplexity_models_init.py  ← Perplexity models
        ├── voyage_models_init.py   ← Voyage AI embedding models
        ├── jina_ai_models_init.py  ← Jina AI embeddings + rerankers
        ├── openai_embedding_models_init.py ← OpenAI embedding models
        ├── elevenlabs_models_init.py ← ElevenLabs TTS/STT models
        ├── deepgram_models_init.py   ← Deepgram STT models
        ├── stability_ai_models_init.py ← Stability AI image gen models
        └── fal_ai_models_init.py     ← Fal AI image gen models

Provider data files export get_models() → List[dict] with model-specific overrides.
"""

import importlib
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ulid import ULID

from app.constants.llm import LLModelMode, LLModelProvider
from app.services.llm import availability


# =========================================================================
# MODEL FIELD DEFAULTS
# =========================================================================
# These are the baseline defaults for ALL models.
# Provider templates and model overrides take precedence.

MODEL_DEFAULTS: Dict[str, Any] = {
    # Basic Information
    "name": None,                       # REQUIRED per model
    "description": None,                # REQUIRED per model
    "usage": None,

    # Provider Information
    "provider": None,                   # Set by provider template
    "model_name": None,                 # REQUIRED per model (e.g., "gpt-5.2")
    "mode": LLModelMode.CHAT.value,

    # Connection Details
    "api_url_auto_discovered": True,
    "api_url": None,

    # Authentication
    "auth_type": "api_key",
    "api_key": None,

    # Model Configuration
    "default_temperature": 0.7,
    "default_max_tokens": 4096,
    "system_prompt": None,
    "parameters": None,                 # Model size in billions

    # Core Generation Parameters
    "top_p": None,
    "n": 1,
    "stop_sequences": None,
    "seed": None,

    # Penalties
    "presence_penalty": None,
    "frequency_penalty": None,
    "logit_bias": None,

    # Response Format & Output Control
    "response_format": None,
    "logprobs": False,
    "top_logprobs": None,

    # Streaming Configuration
    "stream_options": None,

    # Capabilities (boolean flags)
    "supports_streaming": True,
    "supports_function_calling": False,
    "supports_vision": False,
    "supports_batch": False,

    # Tools/Functions Configuration
    "tools": None,
    "tool_choice": None,
    "parallel_tool_calls": True,

    # Safety & Monitoring
    "user_identifier": None,

    # Provider-Specific Configuration
    "api_version": None,
    "extra_headers": None,

    # Advanced configuration
    # (retry/fallback are routing-group concerns)
    "model_metadata": None,
    "drop_params": True,                # Default: drop parameters the provider does not support
    "additional_drop_params": None,
    "thinking_enabled": False,           # Default off — only flipped on for reasoning models

    # Routing configuration
    "routing_group_id": None,
    "routing_weight": 1,
    "rate_limit_rpm": None,
    "rate_limit_tpm": None,

    # Cost Management (USD per 1M tokens)
    "cost_per_input_token": None,
    "cost_per_output_token": None,

    # Connection Settings
    "timeout": 60.0,

    # Status
    "enabled": False,

    # Capabilities JSON
    "capabilities": None,

    # Tags
    "tags": [],
}


# =========================================================================
# PROVIDER TEMPLATES
# =========================================================================
# Each provider template defines defaults that override MODEL_DEFAULTS.
# Model-specific overrides then take precedence over both.

PROVIDER_TEMPLATES: Dict[str, Dict[str, Any]] = {

    # ----- OpenAI -----
    LLModelProvider.OPENAI.value: {
        "provider": LLModelProvider.OPENAI.value,
        "auth_type": "api_key",
        "api_url_auto_discovered": True,
        "api_url": None,
        "drop_params": True,
        "supports_streaming": True,
        "supports_function_calling": True,
        "supports_batch": True,
        "default_temperature": 0.7,
        "default_max_tokens": 4096,
        "timeout": 120.0,
        "tags": ["openai", "production"],
    },

    # ----- Anthropic -----
    LLModelProvider.ANTHROPIC.value: {
        "provider": LLModelProvider.ANTHROPIC.value,
        "auth_type": "api_key",
        "api_url_auto_discovered": True,
        "api_url": None,
        "drop_params": True,
        "supports_streaming": True,
        "supports_function_calling": True,
        "supports_vision": True,
        "supports_batch": True,
        "default_temperature": 0.7,
        "default_max_tokens": 4096,
        "timeout": 120.0,
        "tags": ["anthropic", "production"],
    },

    # ----- Google AI Studio (Gemini) -----
    LLModelProvider.GOOGLE_AI_STUDIO.value: {
        "provider": LLModelProvider.GOOGLE_AI_STUDIO.value,
        "auth_type": "api_key",
        "api_url_auto_discovered": True,
        "api_url": None,
        "drop_params": True,
        "supports_streaming": True,
        "supports_function_calling": True,
        "supports_vision": True,
        "supports_batch": True,
        "default_temperature": 0.7,
        "default_max_tokens": 8192,
        "timeout": 120.0,
        "tags": ["google", "production"],
    },

    # ----- Mistral AI -----
    LLModelProvider.MISTRAL.value: {
        "provider": LLModelProvider.MISTRAL.value,
        "auth_type": "api_key",
        "api_url_auto_discovered": True,
        "api_url": None,
        "drop_params": True,
        "supports_streaming": True,
        "supports_function_calling": True,
        "supports_batch": True,
        "default_temperature": 0.7,
        "default_max_tokens": 4096,
        "timeout": 120.0,
        "tags": ["mistral", "production"],
    },

    # ----- Cohere -----
    LLModelProvider.COHERE.value: {
        "provider": LLModelProvider.COHERE.value,
        "auth_type": "api_key",
        "api_url_auto_discovered": True,
        "api_url": None,
        "drop_params": True,
        "supports_streaming": True,
        "supports_function_calling": True,
        "default_temperature": 0.7,
        "default_max_tokens": 4096,
        "timeout": 120.0,
        "tags": ["cohere", "production"],
    },

    # ----- xAI -----
    LLModelProvider.XAI.value: {
        "provider": LLModelProvider.XAI.value,
        "auth_type": "api_key",
        "api_url_auto_discovered": True,
        "api_url": None,
        "drop_params": True,
        "supports_streaming": True,
        "supports_function_calling": True,
        "supports_vision": True,
        "supports_batch": True,
        "default_temperature": 0.7,
        "default_max_tokens": 4096,
        "timeout": 120.0,
        "tags": ["xai", "production"],
    },

    # ----- DeepSeek -----
    LLModelProvider.DEEPSEEK.value: {
        "provider": LLModelProvider.DEEPSEEK.value,
        "auth_type": "api_key",
        "api_url_auto_discovered": True,
        "api_url": None,
        "drop_params": True,
        "supports_streaming": True,
        "supports_function_calling": True,
        "default_temperature": 0.7,
        "default_max_tokens": 4096,
        "timeout": 180.0,      # DeepSeek reasoning can be slow
        "tags": ["deepseek", "production"],
    },

    # ----- Moonshot AI (Kimi K2.x + Moonshot V1) -----
    LLModelProvider.MOONSHOT.value: {
        "provider": LLModelProvider.MOONSHOT.value,
        "auth_type": "api_key",
        "api_url_auto_discovered": True,
        "api_url": None,
        "drop_params": True,
        "supports_streaming": True,
        "supports_function_calling": True,
        "default_temperature": 0.7,
        "default_max_tokens": 4096,
        # Kimi K2.x reasoning emits long internal CoT before the visible
        # answer, so the 30s default is too tight on cold pings.
        "timeout": 240.0,
        "tags": ["moonshot", "production"],
    },

    # ----- MiniMax (M2.x text family) -----
    LLModelProvider.MINIMAX.value: {
        "provider": LLModelProvider.MINIMAX.value,
        "auth_type": "api_key",
        "api_url_auto_discovered": True,
        "api_url": None,
        "drop_params": True,
        "supports_streaming": True,
        "supports_function_calling": True,
        "default_temperature": 0.7,
        "default_max_tokens": 4096,
        # MiniMax M2.x always emits a <think>…</think> block before the
        # visible answer, so even small completions can take a few
        # seconds. The -highspeed SKUs are faster but the same timeout
        # works for both.
        "timeout": 240.0,
        "tags": ["minimax", "production"],
    },

    # ----- Groq -----
    LLModelProvider.GROQ.value: {
        "provider": LLModelProvider.GROQ.value,
        "auth_type": "api_key",
        "api_url_auto_discovered": True,
        "api_url": None,
        "drop_params": True,
        "supports_streaming": True,
        "supports_function_calling": True,
        "default_temperature": 0.7,
        "default_max_tokens": 4096,
        "timeout": 30.0,       # Groq is ultra-fast
        "tags": ["groq", "production", "fast-inference"],
    },

    # ----- OpenRouter -----
    # The aggregator front: one key, every major model, OpenAI-compatible
    # wire. Seeded PRECONFIGURED so "Brutor in front of OpenRouter" is a
    # key-paste away — the operator keeps their routing, gains assurance.
    LLModelProvider.OPENROUTER.value: {
        "provider": LLModelProvider.OPENROUTER.value,
        "auth_type": "api_key",
        "api_url_auto_discovered": True,
        "api_url": None,
        "drop_params": True,
        "supports_streaming": True,
        "supports_function_calling": True,
        "default_temperature": 0.7,
        "default_max_tokens": 4096,
        "timeout": 180.0,
        "tags": ["openrouter", "production", "aggregator"],
    },

    # ----- Together AI -----
    LLModelProvider.TOGETHER_AI.value: {
        "provider": LLModelProvider.TOGETHER_AI.value,
        "auth_type": "api_key",
        "api_url_auto_discovered": True,
        "api_url": None,
        "drop_params": True,
        "supports_streaming": True,
        "supports_function_calling": True,
        "default_temperature": 0.7,
        "default_max_tokens": 4096,
        "timeout": 120.0,
        "tags": ["together_ai", "production"],
    },

    # ----- Fireworks AI -----
    LLModelProvider.FIREWORKS_AI.value: {
        "provider": LLModelProvider.FIREWORKS_AI.value,
        "auth_type": "api_key",
        "api_url_auto_discovered": True,
        "api_url": None,
        "drop_params": True,
        "supports_streaming": True,
        "supports_function_calling": True,
        "default_temperature": 0.7,
        "default_max_tokens": 4096,
        "timeout": 60.0,
        "tags": ["fireworks_ai", "production"],
    },

    # ----- Perplexity -----
    LLModelProvider.PERPLEXITY.value: {
        "provider": LLModelProvider.PERPLEXITY.value,
        "auth_type": "api_key",
        "api_url_auto_discovered": True,
        "api_url": None,
        "drop_params": True,
        "supports_streaming": True,
        "supports_function_calling": False,  # Perplexity doesn't support tools
        "default_temperature": 0.7,
        "default_max_tokens": 4096,
        "timeout": 60.0,
        "tags": ["perplexity", "production", "search-augmented"],
    },

    # ----- Voyage AI (Embeddings) -----
    LLModelProvider.VOYAGE.value: {
        "provider": LLModelProvider.VOYAGE.value,
        "auth_type": "api_key",
        "api_url_auto_discovered": True,
        "api_url": None,
        "drop_params": True,
        "supports_streaming": False,
        "supports_function_calling": False,
        "supports_vision": False,
        "default_temperature": 0.0,
        "default_max_tokens": 8192,
        "timeout": 30.0,
        "tags": ["voyage", "production", "embeddings"],
    },

    # ----- Jina AI (Embeddings + Rerankers) -----
    LLModelProvider.JINA_AI.value: {
        "provider": LLModelProvider.JINA_AI.value,
        "auth_type": "api_key",
        "api_url_auto_discovered": True,
        "api_url": None,
        "drop_params": True,
        "supports_streaming": False,
        "supports_function_calling": False,
        "supports_vision": False,
        "default_temperature": 0.0,
        "default_max_tokens": 8192,
        "timeout": 30.0,
        "tags": ["jina-ai", "production", "embeddings", "rerankers"],
    },

    # ----- ElevenLabs (TTS / STT) -----
    LLModelProvider.ELEVENLABS.value: {
        "provider": LLModelProvider.ELEVENLABS.value,
        "auth_type": "api_key",
        "api_url_auto_discovered": True,
        "api_url": None,
        "drop_params": True,
        "supports_streaming": True,
        "supports_function_calling": False,
        "supports_vision": False,
        "default_temperature": 0.0,
        "default_max_tokens": 4096,
        "timeout": 120.0,
        "tags": ["elevenlabs", "production", "voice", "audio"],
    },

    # ----- Deepgram (STT) -----
    LLModelProvider.DEEPGRAM.value: {
        "provider": LLModelProvider.DEEPGRAM.value,
        "auth_type": "api_key",
        "api_url_auto_discovered": True,
        "api_url": None,
        "drop_params": True,
        "supports_streaming": True,
        "supports_function_calling": False,
        "supports_vision": False,
        "default_temperature": 0.0,
        "default_max_tokens": 4096,
        "timeout": 120.0,
        "tags": ["deepgram", "production", "speech-to-text"],
    },

    # ----- Stability AI (Image Generation) -----
    LLModelProvider.STABILITY.value: {
        "provider": LLModelProvider.STABILITY.value,
        "auth_type": "api_key",
        "api_url_auto_discovered": True,
        "api_url": None,
        "drop_params": True,
        "supports_streaming": False,
        "supports_function_calling": False,
        "supports_vision": False,
        "default_temperature": 0.0,
        "default_max_tokens": 4096,
        "timeout": 120.0,
        "tags": ["stability-ai", "production", "image-generation"],
    },

    # ----- Fal AI (Image Generation) -----
    LLModelProvider.FAL_AI.value: {
        "provider": LLModelProvider.FAL_AI.value,
        "auth_type": "api_key",
        "api_url_auto_discovered": True,
        "api_url": None,
        "drop_params": True,
        "supports_streaming": False,
        "supports_function_calling": False,
        "supports_vision": False,
        "default_temperature": 0.0,
        "default_max_tokens": 4096,
        "timeout": 120.0,
        "tags": ["fal-ai", "production", "image-generation"],
    },

    # ----- Replicate (Image / Video Generation) -----
    # The Brutor Rust adapter calls Replicate directly with a
    # `Bearer <token>` header and `Prefer: wait=60`. `api_url`
    # defaults to api.replicate.com so seeds don't need to set it.
    LLModelProvider.REPLICATE.value: {
        "provider": LLModelProvider.REPLICATE.value,
        "auth_type": "api_key",
        "api_url_auto_discovered": True,
        "api_url": None,
        "drop_params": True,
        "supports_streaming": False,
        "supports_function_calling": False,
        "supports_vision": False,
        "default_temperature": 0.0,
        "default_max_tokens": 4096,
        "timeout": 240.0,
        "tags": ["replicate", "production", "image-generation"],
    },

    # ----- AssemblyAI (Speech-to-Text) -----
    # Auth header is the raw key (no Bearer prefix). The Brutor
    # Rust adapter handles a 3-step async flow (upload + submit +
    # poll) so individual model timeouts cover the whole journey.
    LLModelProvider.ASSEMBLY_AI.value: {
        "provider": LLModelProvider.ASSEMBLY_AI.value,
        "auth_type": "api_key",
        "api_url_auto_discovered": True,
        "api_url": None,
        "drop_params": True,
        "supports_streaming": False,
        "supports_function_calling": False,
        "supports_vision": False,
        "default_temperature": 0.0,
        "default_max_tokens": 4096,
        "timeout": 600.0,
        "tags": ["assemblyai", "production", "speech-to-text"],
    },

    # ----- Google Cloud TTS / STT -----
    # Distinct from GOOGLE_AI_STUDIO ("gemini") which carries the
    # text/multimodal Gemini models. The TTS / STT products use
    # `provider="google"` so the Rust audio adapters' match arms
    # ("google" | "google_cloud") fire.
    LLModelProvider.GOOGLE_CLOUD.value: {
        "provider": LLModelProvider.GOOGLE_CLOUD.value,
        "auth_type": "api_key",
        "api_url_auto_discovered": True,
        "api_url": None,
        "drop_params": True,
        "supports_streaming": False,
        "supports_function_calling": False,
        "supports_vision": False,
        "default_temperature": 0.0,
        "default_max_tokens": 4096,
        "timeout": 120.0,
        "tags": ["google", "google-cloud", "production"],
    },

    # ----- Vertex AI (Google Cloud) -----
    LLModelProvider.VERTEX_AI.value: {
        "provider": LLModelProvider.VERTEX_AI.value,
        "auth_type": "gcp_service_account",
        "api_url_auto_discovered": True,
        "api_url": None,  # Auto-discovered from GCP project + location (e.g., https://us-central1-aiplatform.googleapis.com)
        "api_key": None,  # Uses GCP Service Account JSON credentials
        "drop_params": True,
        "supports_streaming": True,
        "supports_function_calling": True,
        "supports_vision": True,
        "supports_batch": True,
        "default_temperature": 0.7,
        "default_max_tokens": 8192,
        "timeout": 120.0,
        "tags": ["vertex-ai", "gcp", "production", "enterprise"],
    },

    # ----- Azure OpenAI Service -----
    LLModelProvider.AZURE.value: {
        "provider": LLModelProvider.AZURE.value,
        "auth_type": "api_key",
        "api_url_auto_discovered": False,
        "api_url": None,  # e.g., https://your-resource.openai.azure.com (Azure portal → your resource → Endpoint)
        "drop_params": True,
        "supports_streaming": True,
        "supports_function_calling": True,
        "supports_vision": True,
        "supports_batch": True,
        "default_temperature": 0.7,
        "default_max_tokens": 4096,
        "timeout": 90.0,
        "tags": ["azure", "openai", "production", "enterprise"],
    },

    # ----- Azure AI Foundry (Azure AI Studio) -----
    LLModelProvider.AZURE_AI.value: {
        "provider": LLModelProvider.AZURE_AI.value,
        "auth_type": "api_key",
        "api_url_auto_discovered": False,
        "api_url": None,  # e.g., https://your-endpoint.region.inference.ai.azure.com (Azure AI Studio → Deployments → Endpoint)
        "drop_params": True,
        "supports_streaming": True,
        "supports_function_calling": True,
        "supports_vision": False,  # Varies by model
        "default_temperature": 0.7,
        "default_max_tokens": 4096,
        "timeout": 90.0,
        "tags": ["azure-ai", "azure-foundry", "production", "enterprise"],
    },

    # ----- AWS Bedrock (Invoke API) -----
    LLModelProvider.BEDROCK.value: {
        "provider": LLModelProvider.BEDROCK.value,
        "auth_type": "aws_iam",
        "api_url_auto_discovered": False,
        "api_url": None,  # Auto-derived from AWS region (e.g., https://bedrock-runtime.us-east-1.amazonaws.com)
        "api_key": None,  # Uses IAM credentials, not API key
        "drop_params": False,  # Provider-specific formats, be careful
        "supports_streaming": True,
        "supports_function_calling": True,
        "supports_vision": True,
        "supports_batch": True,
        "default_temperature": 0.5,
        "default_max_tokens": 4096,
        "timeout": 90.0,
        "tags": ["bedrock", "aws", "production", "enterprise"],
    },

    # ----- AWS Bedrock (Converse API) -----
    LLModelProvider.BEDROCK_CONVERSE.value: {
        "provider": LLModelProvider.BEDROCK_CONVERSE.value,
        "auth_type": "aws_iam",
        "api_url_auto_discovered": False,
        "api_url": None,  # Auto-derived from AWS region (e.g., https://bedrock-runtime.us-east-1.amazonaws.com)
        "api_key": None,  # Uses IAM credentials, not API key
        "drop_params": True,  # Converse API has unified format
        "supports_streaming": True,
        "supports_function_calling": True,
        "supports_vision": True,
        "supports_batch": True,
        "default_temperature": 0.5,
        "default_max_tokens": 4096,
        "timeout": 90.0,
        "tags": ["bedrock", "aws", "converse-api", "production", "enterprise"],
    },

    # --- Inference platform providers ---

    # ----- SambaNova -----
    LLModelProvider.SAMBANOVA.value: {
        "provider": LLModelProvider.SAMBANOVA.value,
        "auth_type": "api_key",
        "api_url_auto_discovered": True,
        "api_url": None,  # https://api.sambanova.ai/v1
        "drop_params": True,
        "supports_streaming": True,
        "supports_function_calling": True,
        "default_temperature": 0.7,
        "default_max_tokens": 4096,
        "timeout": 120.0,
        "tags": ["sambanova", "fast-inference", "production"],
    },

    # ----- Cerebras -----
    LLModelProvider.CEREBRAS.value: {
        "provider": LLModelProvider.CEREBRAS.value,
        "auth_type": "api_key",
        "api_url_auto_discovered": True,
        "api_url": None,  # https://api.cerebras.ai/v1
        "drop_params": True,
        "supports_streaming": True,
        "supports_function_calling": True,
        "default_temperature": 0.7,
        "default_max_tokens": 4096,
        "timeout": 60.0,  # Cerebras is ultra-fast, shorter timeout
        "tags": ["cerebras", "fast-inference", "production"],
    },

    # ----- AI21 Labs (Jamba) -----
    LLModelProvider.AI21.value: {
        "provider": LLModelProvider.AI21.value,
        "auth_type": "api_key",
        "api_url_auto_discovered": True,
        "api_url": None,
        "drop_params": True,
        "supports_streaming": True,
        "supports_function_calling": True,
        "default_temperature": 0.7,
        "default_max_tokens": 4096,
        "timeout": 120.0,
        "tags": ["ai21", "jamba", "production"],
    },

    # ----- DeepInfra -----
    LLModelProvider.DEEPINFRA.value: {
        "provider": LLModelProvider.DEEPINFRA.value,
        "auth_type": "api_key",
        "api_url_auto_discovered": True,
        "api_url": None,  # https://api.deepinfra.com/v1/openai
        "drop_params": True,
        "supports_streaming": True,
        "supports_function_calling": True,
        "default_temperature": 0.7,
        "default_max_tokens": 4096,
        "timeout": 120.0,
        "tags": ["deepinfra", "cost-efficient", "production"],
    },

    # --- Self-hosted / serving platform providers ---

    # ----- Ollama (local) -----
    LLModelProvider.OLLAMA.value: {
        "provider": LLModelProvider.OLLAMA.value,
        "auth_type": "none",
        "api_url_auto_discovered": False,
        "api_url": None,  # http://localhost:11434 (user must configure)
        "api_key": None,  # No API key required for local server
        "drop_params": True,
        "supports_streaming": True,
        "supports_function_calling": True,
        "default_temperature": 0.7,
        "default_max_tokens": 4096,
        "timeout": 300.0,  # Longer timeout for local models (may load slowly)
        "tags": ["ollama", "local", "self-hosted"],
    },

    # ----- vLLM -----
    LLModelProvider.VLLM.value: {
        "provider": LLModelProvider.VLLM.value,
        "auth_type": "api_key",  # Optional, depends on deployment
        "api_url_auto_discovered": False,
        "api_url": None,  # User must configure vLLM endpoint URL
        "drop_params": True,
        "supports_streaming": True,
        "supports_function_calling": True,
        "default_temperature": 0.7,
        "default_max_tokens": 4096,
        "timeout": 120.0,
        "tags": ["vllm", "self-hosted"],
    },

    # ----- NVIDIA NIM -----
    LLModelProvider.NVIDIA_NIM.value: {
        "provider": LLModelProvider.NVIDIA_NIM.value,
        "auth_type": "api_key",
        "api_url_auto_discovered": False,
        "api_url": None,  # NVIDIA_NIM_API_BASE (API Catalog or self-hosted)
        "drop_params": True,
        "supports_streaming": True,
        "supports_function_calling": True,
        "default_temperature": 0.7,
        "default_max_tokens": 4096,
        "timeout": 120.0,
        "tags": ["nvidia", "nim", "enterprise"],
    },

    # ----- Databricks -----
    LLModelProvider.DATABRICKS.value: {
        "provider": LLModelProvider.DATABRICKS.value,
        "auth_type": "api_key",
        "api_url_auto_discovered": False,
        "api_url": None,  # DATABRICKS_API_BASE (workspace URL)
        "drop_params": True,
        "supports_streaming": True,
        "supports_function_calling": True,
        "default_temperature": 0.7,
        "default_max_tokens": 4096,
        "timeout": 120.0,
        "tags": ["databricks", "enterprise"],
    },

    # ----- HuggingFace -----
    LLModelProvider.HUGGINGFACE.value: {
        "provider": LLModelProvider.HUGGINGFACE.value,
        "auth_type": "api_key",  # HF_TOKEN
        "api_url_auto_discovered": True,
        "api_url": None,  # Auto for free API; custom for Inference Endpoints
        "drop_params": True,
        "supports_streaming": True,
        "supports_function_calling": True,
        "default_temperature": 0.7,
        "default_max_tokens": 4096,
        "timeout": 120.0,
        "tags": ["huggingface", "open-weight"],
    },

    # ----- AWS SageMaker -----
    LLModelProvider.SAGEMAKER.value: {
        "provider": LLModelProvider.SAGEMAKER.value,
        "auth_type": "aws_iam",
        "api_url_auto_discovered": False,
        "api_url": None,  # Auto-derived from AWS region (e.g., https://runtime.sagemaker.us-east-1.amazonaws.com)
        "api_key": None,  # Uses IAM credentials
        "drop_params": True,
        "supports_streaming": True,
        "supports_function_calling": False,
        "default_temperature": 0.7,
        "default_max_tokens": 4096,
        "timeout": 120.0,
        "tags": ["sagemaker", "aws", "enterprise", "self-hosted"],
    },

    # ----- LM Studio (local) -----
    LLModelProvider.LM_STUDIO.value: {
        "provider": LLModelProvider.LM_STUDIO.value,
        "auth_type": "none",
        "api_url_auto_discovered": False,
        "api_url": None,  # http://localhost:1234/v1 (user must configure)
        "api_key": None,  # No API key for local server
        "drop_params": True,
        "supports_streaming": True,
        "supports_function_calling": True,
        "default_temperature": 0.7,
        "default_max_tokens": 4096,
        "timeout": 300.0,  # Longer timeout for local models
        "tags": ["lm-studio", "local", "self-hosted", "desktop"],
    },
}


# =========================================================================
# MODEL BUILDER
# =========================================================================

# The boolean capability flags that exist both as top-level model columns
# and inside the `capabilities` JSON; kept in step in both directions.
CAPABILITY_FLAGS = (
    "supports_vision",
    "supports_function_calling",
    "supports_streaming",
    "supports_batch",
)


def build_model_config(
    provider_key: str,
    model_overrides: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Build a complete model configuration by merging:
    1. MODEL_DEFAULTS (baseline)
    2. PROVIDER_TEMPLATES[provider_key] (provider-level defaults)
    3. model_overrides (model-specific values)

    The model_overrides dict must contain at minimum:
    - model_name: str (e.g., "gpt-5.2")
    - name: str (display name)
    - description: str

    Tags from all three layers are merged (deduplicated), not replaced.

    Returns:
        Complete model configuration dict ready for LLModel creation.
    """
    # Start with baseline defaults
    config = dict(MODEL_DEFAULTS)

    # Apply provider template (overrides baseline)
    provider_template = PROVIDER_TEMPLATES.get(provider_key, {})
    for key, value in provider_template.items():
        if key == "tags":
            continue  # Tags handled separately
        config[key] = value

    # Apply model-specific overrides
    for key, value in model_overrides.items():
        if key == "tags":
            continue  # Tags handled separately
        config[key] = value

    # An explicit `capabilities` flag on the model entry outranks the
    # provider template's top-level flag of the same name. Without this
    # the template's `supports_function_calling: True` survived onto
    # `groq/compound` (seeded with `capabilities.supports_function_calling:
    # False`), the catalog projected the template value, the proxy then
    # forwarded caller tools, and Groq answered 400 "tool calling is not
    # supported" — a model whose seed said exactly that.
    explicit_caps = model_overrides.get("capabilities")
    if isinstance(explicit_caps, dict):
        for flag in CAPABILITY_FLAGS:
            if flag in explicit_caps and isinstance(explicit_caps[flag], bool):
                config[flag] = explicit_caps[flag]

    # Merge tags from all three layers (deduplicated, ordered)
    base_tags = MODEL_DEFAULTS.get("tags", [])
    provider_tags = provider_template.get("tags", [])
    model_tags = model_overrides.get("tags", [])

    seen = set()
    merged_tags = []
    for tag in provider_tags + model_tags + base_tags:
        if tag not in seen:
            seen.add(tag)
            merged_tags.append(tag)
    config["tags"] = merged_tags

    # A model already PAST its provider retirement date at seed time must not
    # be seeded ENABLED: a fresh install on/after the retire date would both
    # route traffic to a dead upstream and trip the retirement warner on the
    # first scheduler tick.
    # Installs done BEFORE the retire date still seed it enabled, as intended;
    # operators can re-enable via the admin UI if a provider extends a schedule.
    # "Past" is the gateway's: from the retire date in UTC (`availability`).
    retire = config.get("retire_date")
    if retire is not None and config.get("enabled"):
        retire_d = retire.date() if isinstance(retire, datetime) else retire
        if isinstance(retire_d, date) and retire_d <= availability._today():
            config["enabled"] = False

    # Auto-generate ID if not provided
    if "id" not in config or config.get("id") is None:
        config["id"] = f"llm-{ULID()}"

    return config


def _retirement_reminder_default(llm_data: Dict[str, Any]) -> Optional[date]:
    """Default `retirement_reminder_date` to `retire_date - 14d` when
    a retire_date is set but the seed dict doesn't carry an explicit
    reminder. Two weeks is a defensible operator-warning floor:
    enough lead time to swap a model in the admin UI and notify
    downstream users without being so far ahead that the alert
    feels like noise. Operators can override per-model via the
    Retirement tab in the admin UI."""
    explicit = llm_data.get("retirement_reminder_date")
    if explicit is not None:
        return explicit
    retire = llm_data.get("retire_date")
    if retire is None:
        return None
    return retire - timedelta(days=14)


def _load_provider_models(provider_module_name: str, provider_key: str) -> List[Dict[str, Any]]:
    """
    Load models from a provider data module and build complete configs.

    Args:
        provider_module_name: Module name (e.g., "openai_models_init")
        provider_key: Provider key matching PROVIDER_TEMPLATES

    Returns:
        List of complete model configuration dicts.
    """
    try:
        # Import from the provider-data package
        module = importlib.import_module(
            f"app.database.provider_data.{provider_module_name}"
        )
        raw_models = module.get_models()
        return [build_model_config(provider_key, m) for m in raw_models]
    except ImportError as e:
        # Provider module not found — log warning so missing files are visible
        import logging
        logging.getLogger("init_llms").warning(
            f"Provider module not found: {provider_module_name} "
            f"(expected at app.database.seed.provider_data.{provider_module_name}) — "
            f"skipping. Error: {e}"
        )
        return []
    except Exception as e:
        raise RuntimeError(
            f"Failed to load provider models from {provider_module_name}: {e}"
        ) from e


# =========================================================================
# PROVIDER REGISTRY
# =========================================================================
# Maps provider data file names to their provider template keys.
# Order determines seeding order (cosmetic only).

PROVIDER_REGISTRY: List[tuple] = [
    # (module_name, provider_key)
    # --- Direct API providers ---
    ("openai_models_init", LLModelProvider.OPENAI.value),
    ("anthropic_models_init", LLModelProvider.ANTHROPIC.value),
    ("google_models_init", LLModelProvider.GOOGLE_AI_STUDIO.value),
    ("mistral_models_init", LLModelProvider.MISTRAL.value),
    ("cohere_models_init", LLModelProvider.COHERE.value),
    ("xai_models_init", LLModelProvider.XAI.value),
    ("deepseek_models_init", LLModelProvider.DEEPSEEK.value),
    ("moonshotai_models_init", LLModelProvider.MOONSHOT.value),
    ("minimax_models_init", LLModelProvider.MINIMAX.value),
    ("groq_models_init", LLModelProvider.GROQ.value),
    ("openrouter_models_init", LLModelProvider.OPENROUTER.value),
    ("together_ai_models_init", LLModelProvider.TOGETHER_AI.value),
    ("fireworks_ai_models_init", LLModelProvider.FIREWORKS_AI.value),
    ("perplexity_models_init", LLModelProvider.PERPLEXITY.value),
    ("voyage_models_init", LLModelProvider.VOYAGE.value),
    ("jina_ai_models_init", LLModelProvider.JINA_AI.value),
    ("openai_embedding_models_init", LLModelProvider.OPENAI.value),
    # --- OpenAI multimodal (image / TTS / STT) — default-enabled
    #     test baseline, shares the tenant's existing OpenAI key with
    #     chat / responses / embedding. ---
    ("openai_multimodal_models_init", LLModelProvider.OPENAI.value),
    # --- Audio / Voice providers ---
    ("elevenlabs_models_init", LLModelProvider.ELEVENLABS.value),
    ("deepgram_models_init", LLModelProvider.DEEPGRAM.value),
    ("assemblyai_models_init", LLModelProvider.ASSEMBLY_AI.value),
    # --- Image generation providers ---
    ("stability_ai_models_init", LLModelProvider.STABILITY.value),
    ("fal_ai_models_init", LLModelProvider.FAL_AI.value),
    ("replicate_models_init", LLModelProvider.REPLICATE.value),
    # --- Cloud platform providers ---
    ("vertex_ai_models_init", LLModelProvider.VERTEX_AI.value),
    ("azure_openai_models_init", LLModelProvider.AZURE.value),
    ("azure_ai_models_init", LLModelProvider.AZURE_AI.value),
    ("bedrock_models_init", LLModelProvider.BEDROCK.value),
    ("bedrock_converse_models_init", LLModelProvider.BEDROCK_CONVERSE.value),
    # --- Inference platform providers ---
    ("sambanova_models_init", LLModelProvider.SAMBANOVA.value),
    ("cerebras_models_init", LLModelProvider.CEREBRAS.value),
    ("ai21_models_init", LLModelProvider.AI21.value),
    ("deepinfra_models_init", LLModelProvider.DEEPINFRA.value),
    # --- Self-hosted / serving platform providers ---
    ("ollama_models_init", LLModelProvider.OLLAMA.value),
    ("vllm_models_init", LLModelProvider.VLLM.value),
    ("nvidia_nim_models_init", LLModelProvider.NVIDIA_NIM.value),
    ("databricks_models_init", LLModelProvider.DATABRICKS.value),
    ("huggingface_models_init", LLModelProvider.HUGGINGFACE.value),
    ("sagemaker_models_init", LLModelProvider.SAGEMAKER.value),
    ("lm_studio_models_init", LLModelProvider.LM_STUDIO.value),
]


# =========================================================================
# PUBLIC API
# =========================================================================

def get_default_llm_configurations() -> List[Dict[str, Any]]:
    """
    Load and return all default LLM model configurations.

    Loads per-provider model data files from the provider-data directory,
    applies provider templates and model defaults, and returns a complete
    list of model configurations ready for database insertion.

    Returns:
        List of complete model configuration dictionaries.
    """
    all_models = []
    for module_name, provider_key in PROVIDER_REGISTRY:
        models = _load_provider_models(module_name, provider_key)
        all_models.extend(models)

    return all_models


# =========================================================================
# GLOBAL CATALOG SEEDING
# =========================================================================
# The catalog (`llm_catalog`) is GLOBAL — one shared, browseable list of
# every model Brutor knows how to talk to, independent of any tenant. It
# replaces the old design where the FULL catalog was seeded PER-TENANT as
# disabled `llm_model` rows. A tenant imports a catalog entry → a real
# `llm_model` instance is created (see `create_llm_models` /
# `LlmCatalogService.import_to_model`).


def _catalog_capabilities_from_seed(llm_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Project the seed dict's boolean capability flags (plus any explicit
    `capabilities` JSON) into the catalog's lean `capabilities` blob.

    Keeps the browse view useful (vision / function_calling badges) without
    dragging every per-model tuning column onto the catalog row."""
    caps: Dict[str, Any] = {}
    for flag in CAPABILITY_FLAGS:
        val = llm_data.get(flag)
        if val:
            caps[flag] = True
    explicit = llm_data.get("capabilities")
    if isinstance(explicit, dict):
        # Explicit wins: a seed that says `supports_function_calling: False`
        # must not be overridden by the projected template flag.
        for k, v in explicit.items():
            caps[k] = v
    return caps or None


# A catalog row shows as deprecated this many days before its retirement
# date (about six months), when the provider has not announced it earlier.
DEPRECATION_HORIZON_DAYS = 180


def catalog_deprecated(llm_data: Dict[str, Any], today: Optional[date] = None) -> bool:
    """Whether a seed row is surfaced as deprecated in the browse view.

    Deprecated means the provider has announced the retirement
    (`retirement_announced_at`), or the published `retire_date` is within
    `DEPRECATION_HORIZON_DAYS` (or already past). A `retire_date` alone is
    not a deprecation: Azure and Google publish an end-of-life date for a
    model on the day it is released, a year and more ahead. Days count
    from the UTC day, the day the gateway retires a model on
    (`availability`).
    """
    if llm_data.get("retirement_announced_at") is not None:
        return True
    retire = llm_data.get("retire_date")
    if retire is None:
        return False
    retire_d = retire.date() if isinstance(retire, datetime) else retire
    return retire_d <= (today or availability._today()) + timedelta(days=DEPRECATION_HORIZON_DAYS)


def seed_llm_catalog(db, logger=None) -> int:
    """Populate the GLOBAL `llm_catalog` table once from the existing
    catalog source (`get_default_llm_configurations()`).

    Idempotent: keyed on the deterministic seeded id
    (`llmcat-{provider_key}-{slug(model_name)}`) which encodes
    (provider_key, model_name). An existing row is refreshed in place
    (descriptive fields only); a missing row is inserted. Never
    duplicates on re-run.

    Returns the number of rows inserted (existing rows are updates).
    """
    from app.database.models.llm_catalog import (
        CATALOG_SOURCE_BUILTIN,
        LlmCatalog,
        seeded_catalog_id,
    )

    default_llms = get_default_llm_configurations()

    inserted = 0
    updated = 0
    for llm_data in default_llms:
        provider_key = (llm_data.get("provider") or "").lower()
        model_name = llm_data.get("model_name")
        if not provider_key or not model_name:
            continue

        cat_id = seeded_catalog_id(provider_key, model_name)
        existing = db.get(LlmCatalog, cat_id)

        display_name = llm_data.get("name") or model_name
        description = llm_data.get("description")
        mode = llm_data.get("mode", "chat")
        capabilities = _catalog_capabilities_from_seed(llm_data)
        context_window = llm_data.get("context_window")
        max_output_tokens = (
            llm_data.get("max_output_tokens")
            or llm_data.get("default_max_tokens")
        )
        cost_in = llm_data.get("cost_per_input_token")
        cost_out = llm_data.get("cost_per_output_token")
        # Pull the icon from the provider seed catalogue when the model
        # seed dict carries none of its own.
        icon_url = llm_data.get("icon_url") or _provider_icon_url(provider_key)
        deprecated = catalog_deprecated(llm_data)

        # Per-model import-default columns — sourced from the same seed
        # dict that create_llm_models used to put directly on the llm_model
        # instance. Carried on the catalog so import_to_model can copy them
        # onto a freshly imported audio/image/video/reasoning model.
        import_defaults = {
            "cost_per_image": llm_data.get("cost_per_image"),
            "supported_image_sizes": llm_data.get("supported_image_sizes"),
            "supported_image_qualities": llm_data.get("supported_image_qualities"),
            "max_images_per_request": llm_data.get("max_images_per_request"),
            "cost_per_audio_char": llm_data.get("cost_per_audio_char"),
            "cost_per_audio_minute": llm_data.get("cost_per_audio_minute"),
            "supported_voices": llm_data.get("supported_voices"),
            "supported_audio_formats": llm_data.get("supported_audio_formats"),
            "supported_input_formats": llm_data.get("supported_input_formats"),
            "supported_response_formats": llm_data.get("supported_response_formats"),
            "max_audio_duration_seconds": llm_data.get("max_audio_duration_seconds"),
            "cost_per_video_second": llm_data.get("cost_per_video_second"),
            "supported_video_resolutions": llm_data.get("supported_video_resolutions"),
            "supported_video_durations": llm_data.get("supported_video_durations"),
            "max_video_duration_seconds": llm_data.get("max_video_duration_seconds"),
            "thinking_enabled": llm_data.get("thinking_enabled"),
            "additional_drop_params": llm_data.get("additional_drop_params"),
            "parameters": llm_data.get("parameters"),
            "tags": llm_data.get("tags"),
            "retire_date": llm_data.get("retire_date"),
        }

        if existing is None:
            row = LlmCatalog(
                id=cat_id,
                provider_key=provider_key,
                model_name=model_name,
                display_name=display_name,
                description=description,
                mode=mode,
                capabilities=capabilities,
                context_window=context_window,
                max_output_tokens=max_output_tokens,
                default_cost_per_input_token=cost_in,
                default_cost_per_output_token=cost_out,
                icon_url=icon_url,
                recommended=bool(llm_data.get("enabled")),
                deprecated=deprecated,
                source=CATALOG_SOURCE_BUILTIN,
                **import_defaults,
            )
            db.add(row)
            inserted += 1
        else:
            # Refresh descriptive fields in place. Don't stomp an
            # operator's `recommended`/`deprecated` toggles unnecessarily,
            # but the builtin seed is authoritative for the descriptive
            # metadata.
            existing.provider_key = provider_key
            existing.model_name = model_name
            existing.display_name = display_name
            existing.description = description
            existing.mode = mode
            existing.capabilities = capabilities
            existing.context_window = context_window
            existing.max_output_tokens = max_output_tokens
            existing.default_cost_per_input_token = cost_in
            existing.default_cost_per_output_token = cost_out
            if icon_url is not None:
                existing.icon_url = icon_url
            existing.deprecated = deprecated
            for _k, _v in import_defaults.items():
                setattr(existing, _k, _v)
            updated += 1

    db.commit()
    if logger is not None:
        logger.info(
            f"Seeded global LLM catalog: {inserted} inserted, "
            f"{updated} refreshed (total source configs={len(default_llms)})"
        )
    return inserted


def _provider_icon_url(provider_key: str) -> Optional[str]:
    """Best-effort icon URL for a provider slug, read from the global
    provider seed catalogue. Returns None when unavailable."""
    try:
        from app.database.init_llm_providers import PROVIDER_SEED_CATALOG
    except Exception:  # pragma: no cover - import guard
        return None
    for entry in PROVIDER_SEED_CATALOG:
        if entry.get("key") == provider_key:
            return entry.get("icon_url")
    return None


def create_llm_models(
    db,
    tenant_id: str,
    default_group_id: str,
    logger,
) -> Dict[str, str]:
    """
    Import the default-enabled catalog models into a new tenant as
    configured `llm_model` instances.

    Previously this seeded the FULL catalog per-tenant as `llm_model`
    rows distinguished only by `enabled` — a design smell that left every
    tenant carrying ~300 disabled rows that were really a static catalog.
    The catalog now lives globally in `llm_catalog` (seeded once by
    `seed_llm_catalog`). This function imports ONLY the default-enabled
    seeds as real instances (`catalog_id` set, `enabled=True`) and assigns
    them to the default resource group. Disabled catalog models are NOT
    materialised here — operators import them on demand via
    `POST /v1/admin/llm-catalog/{id}/import`.

    The full template-based seed list is still the source of truth for
    which models are default-enabled; we filter it to the enabled subset.

    Args:
        db: Database session
        tenant_id: Tenant ID to associate models with
        default_group_id: Resource group ID (must be a valid ULID-based ID
                          like "group-01JXXXX..." — NOT a bare name like "Default")
        logger: Logger instance

    Returns:
        Dictionary mapping model_name -> model_id for all created models
    """
    from app.database.models.llm_catalog import seeded_catalog_id
    from app.database.models.llm_model import LLModel
    from app.database.models.resource_group import resource_group_llm_models

    # Ensure the GLOBAL catalogs (models + providers) are seeded once
    # before importing instances. Both are idempotent — safe to call on
    # every tenant provision; they no-op (update-in-place) when already
    # present.
    seed_llm_catalog(db, logger=logger)
    from app.database.init_llm_providers import seed_llm_provider_catalog
    seed_llm_provider_catalog(db, logger=logger)

    # Import only the DEFAULT-ENABLED catalog models as configured
    # instances. The disabled remainder stays in the global catalog and is
    # imported on demand via the catalog API.
    default_llms = [
        m for m in get_default_llm_configurations() if m.get("enabled")
    ]

    # Create LLM model entries
    models_created = 0
    llm_ids = {}

    # Providers are now AUTO-IMPORTED on demand from the global
    # `llm_provider_catalog` (seeded above) — we no longer bulk-seed all
    # ~41 `llm_providers` rows per tenant. A tenant gets a real provider
    # row only for the providers its seeded models actually use, via
    # `upsert_provider_for_key` (which materializes one catalog row into
    # `llm_providers`, stamping `catalog_id`). The cache below memoises
    # the per-key resolution so we materialize each used provider once.
    from app.database.init_llm_providers import upsert_provider_for_key
    provider_id_by_key: Dict[str, str] = {}

    for llm_data in default_llms:
        # Resolve `provider_id`. Every seed dict has
        # `provider` (slug) injected by the PROVIDER_TEMPLATES
        # merge in `_load_provider_models`. We look that slug up
        # against `provider_id_by_key`; if it's a slug we don't
        # have a catalogue entry for (e.g. an operator-added
        # custom seed module) we lazy-create the provider via
        # `upsert_provider_for_key` so the model still lands.
        provider_slug = (llm_data.get("provider") or "").lower()
        if not provider_slug:
            raise ValueError(
                f"Seed model {llm_data.get('model_name')!r} has no "
                "`provider` slug — cannot resolve provider_id."
            )
        provider_id = provider_id_by_key.get(provider_slug)
        if provider_id is None:
            row = upsert_provider_for_key(
                db, tenant_id=tenant_id, key=provider_slug,
                is_system=True,
            )
            provider_id_by_key[provider_slug] = row.id
            provider_id = row.id

        llm = LLModel(
            # Basic Information
            id=llm_data["id"],
            tenant_id=tenant_id,
            name=llm_data["name"],
            description=llm_data["description"],
            usage=llm_data.get("usage"),

            # Provider Information — `provider_id` resolved above
            # from the slug carried by each seed dict.
            provider_id=provider_id,
            model_name=llm_data["model_name"],
            mode=llm_data.get("mode", "chat"),
            parameters=llm_data.get("parameters"),
            tags=llm_data.get("tags"),

            # Connection Details
            api_url_auto_discovered=llm_data.get("api_url_auto_discovered", True),
            api_url=llm_data.get("api_url"),

            # Authentication
            auth_type=llm_data.get("auth_type", "api_key"),
            api_key=llm_data.get("api_key"),

            # Model Configuration
            default_temperature=llm_data["default_temperature"],
            default_max_tokens=llm_data["default_max_tokens"],
            system_prompt=llm_data.get("system_prompt"),

            # Core Generation Parameters
            top_p=llm_data.get("top_p"),
            n=llm_data.get("n"),
            stop_sequences=llm_data.get("stop_sequences"),
            seed=llm_data.get("seed"),

            # Penalties
            presence_penalty=llm_data.get("presence_penalty"),
            frequency_penalty=llm_data.get("frequency_penalty"),
            logit_bias=llm_data.get("logit_bias"),

            # Response Format & Output Control
            response_format=llm_data.get("response_format"),
            logprobs=llm_data.get("logprobs", False),
            top_logprobs=llm_data.get("top_logprobs"),

            # Streaming Configuration
            stream_options=llm_data.get("stream_options"),

            # Capabilities
            supports_streaming=llm_data["supports_streaming"],
            supports_function_calling=llm_data["supports_function_calling"],
            supports_vision=llm_data["supports_vision"],
            supports_batch=llm_data.get("supports_batch", False),

            # Tools/Functions Configuration
            tools=llm_data.get("tools"),
            tool_choice=llm_data.get("tool_choice"),
            parallel_tool_calls=llm_data.get("parallel_tool_calls"),

            # Safety & Monitoring
            user_identifier=llm_data.get("user_identifier"),

            # Provider-Specific Configuration
            api_version=llm_data.get("api_version"),
            extra_headers=llm_data.get("extra_headers"),

            # Advanced configuration (retry/fallback = routing-group)
            model_metadata=llm_data.get("model_metadata"),
            drop_params=llm_data.get("drop_params", True),
            additional_drop_params=llm_data.get("additional_drop_params"),
            thinking_enabled=llm_data.get("thinking_enabled", False),

            # Routing configuration
            routing_group_id=llm_data.get("routing_group_id"),
            routing_weight=llm_data.get("routing_weight", 1),
            rate_limit_rpm=llm_data.get("rate_limit_rpm"),
            rate_limit_tpm=llm_data.get("rate_limit_tpm"),

            # Cost Management
            cost_per_input_token=llm_data.get("cost_per_input_token"),
            cost_per_output_token=llm_data.get("cost_per_output_token"),

            # Connection Settings
            timeout=llm_data.get("timeout", 60.0),

            # Status
            enabled=llm_data["enabled"],

            # Retirement metadata. Optional — most models aren't on a
            # published retirement schedule. When a provider data
            # module sets `retire_date` we pass it through verbatim;
            # the `retirement_reminder_date` falls back to
            # `retire_date - 14d` if not explicitly set so operators
            # get a default 2-week lead time. Date objects are
            # accepted as-is by SQLAlchemy's Date column.
            retire_date=llm_data.get("retire_date"),
            retirement_announced_at=llm_data.get("retirement_announced_at"),
            retirement_reminder_date=_retirement_reminder_default(llm_data),
            retirement_replacement_model_name=llm_data.get(
                "retirement_replacement_model_name"
            ),
            retirement_replacement_provider_key=llm_data.get(
                "retirement_replacement_provider_key"
            ),
            retirement_notes=llm_data.get("retirement_notes"),

            # Capabilities JSON
            capabilities=llm_data.get("capabilities"),

            # Catalog provenance — back-ref to the global llm_catalog entry
            # this instance was imported from. Deterministic seeded id so
            # it matches `seed_llm_catalog` regardless of insertion order.
            catalog_id=seeded_catalog_id(provider_slug, llm_data["model_name"]),

            # =================================================================
            # Multimodal columns (Phases 2.1 / 3.1 / 4.1 / 5.1)
            # ─────────────────────────────────────────────────────────────────
            # The seed dicts in `provider_data/*_models_init.py` set these
            # for image / audio / video models. Without this passthrough
            # the columns stay NULL on a freshly provisioned tenant and
            # the proxy validators in `routes/images.rs`,
            # `routes/audio_speech.rs`, `routes/audio_transcribe.rs`,
            # `routes/video.rs` all fall through to "no whitelist" —
            # silently. Catch this regression by asserting that a seeded
            # image model has e.g. supported_image_sizes set after
            # `create_llm_models` runs.
            # =================================================================

            # Image generation (mode='image_generation')
            cost_per_image=llm_data.get("cost_per_image"),
            supported_image_sizes=llm_data.get("supported_image_sizes"),
            supported_image_qualities=llm_data.get("supported_image_qualities"),
            max_images_per_request=llm_data.get("max_images_per_request", 1),

            # Audio TTS (mode='audio_speech')
            cost_per_audio_char=llm_data.get("cost_per_audio_char"),
            supported_voices=llm_data.get("supported_voices"),
            supported_audio_formats=llm_data.get("supported_audio_formats"),

            # Audio STT (mode='audio_transcription')
            cost_per_audio_minute=llm_data.get("cost_per_audio_minute"),
            max_audio_duration_seconds=llm_data.get("max_audio_duration_seconds"),
            supported_input_formats=llm_data.get("supported_input_formats"),
            supported_response_formats=llm_data.get("supported_response_formats"),

            # Video generation (mode='video_generation')
            cost_per_video_second=llm_data.get("cost_per_video_second"),
            supported_video_resolutions=llm_data.get("supported_video_resolutions"),
            supported_video_durations=llm_data.get("supported_video_durations"),
            max_video_duration_seconds=llm_data.get("max_video_duration_seconds"),

            # Timestamps
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

        db.add(llm)
        db.flush()

        # Assign the imported (enabled) model to the default resource
        # group. A model that build_model_config() force-disabled because
        # it's already past its provider retire_date is skipped here — it
        # still lives in the global catalog for browse/import.
        if llm.enabled:
            db.execute(
                resource_group_llm_models.insert().values(
                    resource_group_id=default_group_id,
                    llm_model_id=llm.id,
                )
            )

        models_created += 1
        llm_ids[llm.model_name] = llm.id

        enabled_status = "ENABLED" if llm.enabled else "disabled"
        logger.debug(f"Created LLM: {llm.name} [{enabled_status}]", context={
            "llm_id": llm.id,
            "tenant_id": llm.tenant_id,
            # `provider` is a relationship; use the
            # slug we just resolved upstream (carried via
            # `provider_slug` in scope) so we don't accidentally
            # log the ORM repr.
            "provider": provider_slug,
            "model_name": llm.model_name,
            "mode": llm.mode,
            "enabled": llm.enabled,
            "default_group": default_group_id,
        })

    db.commit()
    logger.info(f"Created {models_created} LLM model configurations for tenant {tenant_id}")
    logger.info(f"Enabled models assigned to resource group '{default_group_id}'")

    return llm_ids
