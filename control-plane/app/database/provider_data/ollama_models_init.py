# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Ollama Models — Local model serving via Ollama.

Model prefix: ollama_chat/ (chat) or ollama/ (completion/embedding)
Auth: None required (local server at http://localhost:11434)
Cost values: 0.00 (self-hosted, no per-token cost)

NOTE: These are representative models commonly available in the Ollama
library. The actual models available depend on what the user has pulled
locally via `ollama pull <model>`. Chat models seed disabled by default
since they're large pulls and not every install runs Ollama.

The ``nomic-embed-text`` embedding model is the platform's default
embedding for semantic cache and KB RAG, so it seeds **enabled**. The
api_url defaults to ``http://localhost:11434`` and can be overridden at
deploy time by setting ``BRUTOR_OLLAMA_BASE_URL`` (e.g. the Docker Compose
Ollama overlay). If Ollama isn't reachable on this install, the health
check surfaces a red badge — that's the right signal, since the
operator typically *does* want this model enabled and the unreachable
backend is the actual problem to fix.
"""

from app.env_vars import BRUTOR_OLLAMA_BASE_URL


# Resolved once at import time. Used as the api_url for the
# pre-enabled embedding model so deployments that put Ollama at a
# non-default address (e.g. ``ollama:11434`` inside a compose stack)
# don't need to re-point the model after seeding.
_OLLAMA_BASE_URL = BRUTOR_OLLAMA_BASE_URL.require()



# COST NOTE (2026-08-23): all entries deliberately carry no per-token
# cost — Ollama is self-hosted; the marginal API cost of a token is
# genuinely $0. Infrastructure cost lives outside the token ledger.

def get_models():
    return [
        # =====================================================================
        # Large Chat Models (70B+)
        # =====================================================================
        {
            "model_name": "llama3.3:70b",
            "parameters": 70.0,
            "name": "Ollama Llama 3.3 70B",
            "description": "Meta Llama 3.3 70B running locally via Ollama.",
            "usage": "General chat, coding, analysis — high quality, local inference",
            "supports_vision": False,
            "cost_per_input_token": 0.00,
            "cost_per_output_token": 0.00,
            "capabilities": {
                "context_window": 128000,
                "modalities": ["text"],
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "local", "open-weight",
                     "self-hosted", "long-context", "large"],
        },
        {
            "model_name": "qwen3:32b",
            "parameters": 32.0,
            "name": "Ollama Qwen3 32B",
            "description": "Alibaba Qwen3 32B running locally. Strong multilingual reasoning.",
            "usage": "Reasoning, multilingual, coding, local inference",
            "supports_vision": False,
            "cost_per_input_token": 0.00,
            "cost_per_output_token": 0.00,
            "capabilities": {
                "context_window": 40960,
                "modalities": ["text"],
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "local", "open-weight",
                     "self-hosted", "multilingual", "medium"],
        },
        {
            "model_name": "deepseek-r1:70b",
            "parameters": 70.0,
            "name": "Ollama DeepSeek R1 70B",
            "description": "DeepSeek R1 distilled 70B reasoning model, running locally.",
            "usage": "Complex reasoning, math, analysis — local inference",
            "supports_vision": False,
            "cost_per_input_token": 0.00,
            "cost_per_output_token": 0.00,
            "capabilities": {
                "context_window": 128000,
                "modalities": ["text"],
            },
            "tags": ["reasoning", "local", "open-weight",
                     "self-hosted", "long-context", "large"],
        },
        {
            "model_name": "mistral-large",
            "parameters": 123.0,
            "name": "Ollama Mistral Large",
            "description": "Mistral Large 123B running locally via Ollama.",
            "usage": "Complex tasks, multilingual, coding, enterprise reasoning",
            "supports_vision": False,
            "cost_per_input_token": 0.00,
            "cost_per_output_token": 0.00,
            "capabilities": {
                "context_window": 128000,
                "modalities": ["text"],
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "local", "open-weight",
                     "self-hosted", "multilingual", "large"],
        },

        # =====================================================================
        # Medium Chat Models (7B–14B)
        # =====================================================================
        {
            "model_name": "llama3.2:3b",
            "parameters": 3.0,
            "name": "Ollama Llama 3.2 3B",
            "description": "Tiny Llama 3.2 3B. Runs on minimal hardware. Very fast.",
            "usage": "Simple tasks, classification, routing, edge deployment",
            "supports_vision": False,
            "cost_per_input_token": 0.00,
            "cost_per_output_token": 0.00,
            "capabilities": {
                "context_window": 128000,
                "modalities": ["text"],
            },
            "tags": ["local", "open-weight", "self-hosted",
                     "cost-efficient", "tiny"],
        },
        {
            "model_name": "mistral:7b",
            "parameters": 7.0,
            "name": "Ollama Mistral 7B",
            "description": "Mistral 7B running locally. Efficient general-purpose model.",
            "usage": "General chat, coding, lightweight local inference",
            "supports_vision": False,
            "cost_per_input_token": 0.00,
            "cost_per_output_token": 0.00,
            "capabilities": {
                "context_window": 32768,
                "modalities": ["text"],
            },
            "tags": ["local", "open-weight", "self-hosted",
                     "cost-efficient", "small"],
        },
        {
            "model_name": "gemma3:12b",
            "parameters": 12.0,
            "name": "Ollama Gemma 3 12B",
            "description": "Google Gemma 3 12B with multimodal support. Runs locally.",
            "usage": "Vision tasks, general chat, image understanding",
            "supports_vision": True,
            "cost_per_input_token": 0.00,
            "cost_per_output_token": 0.00,
            "capabilities": {
                "context_window": 128000,
                "modalities": ["text", "images"],
            },
            "tags": ["local", "open-weight", "self-hosted",
                     "multimodal", "medium"],
        },
        {
            "model_name": "phi4:14b",
            "parameters": 14.0,
            "name": "Ollama Phi-4 14B",
            "description": "Microsoft Phi-4 14B. Compact but capable reasoning model.",
            "usage": "Reasoning, coding, math — runs on consumer hardware",
            "supports_vision": False,
            "cost_per_input_token": 0.00,
            "cost_per_output_token": 0.00,
            "capabilities": {
                "context_window": 16384,
                "modalities": ["text"],
            },
            "tags": ["reasoning", "local", "open-weight",
                     "self-hosted", "medium"],
        },

        # =====================================================================
        # Code-Specialized Models
        # =====================================================================
        {
            "model_name": "qwen2.5-coder:7b",
            "parameters": 7.0,
            "name": "Ollama Qwen2.5 Coder 7B",
            "description": "Qwen2.5 Coder 7B. Specialized for code generation locally.",
            "usage": "Code generation, debugging, completion, refactoring",
            "supports_vision": False,
            "cost_per_input_token": 0.00,
            "cost_per_output_token": 0.00,
            "capabilities": {
                "context_window": 32768,
                "modalities": ["text"],
            },
            "tags": ["coding", "local", "open-weight",
                     "self-hosted", "small"],
        },

        # =====================================================================
        # Embedding Models
        # =====================================================================
        {
            "model_name": "nomic-embed-text",
            "parameters": 0.14,
            "name": "Ollama Nomic Embed Text",
            "description": "Nomic Embed Text v1.5. High-quality local embeddings. Default embedding model for semantic cache and Knowledge Base RAG.",
            "usage": "Text embeddings, semantic search, RAG — local, no API key needed",
            "mode": "embedding",
            # Pre-enabled because this is the platform's default
            # embedding model — semantic cache and KB RAG won't work
            # without an enabled embedding entry. The api_url honours
            # BRUTOR_OLLAMA_BASE_URL when set so deployments running Ollama at
            # a non-default address pick up the right URL out of the
            # box. If Ollama isn't reachable at all, the periodic
            # health check turns the badge red — that's the operator's
            # cue to either start Ollama or disable this model.
            "enabled": True,
            "api_url_auto_discovered": False,
            "api_url": _OLLAMA_BASE_URL,
            "auth_type": "none",
            "supports_vision": False,
            "supports_streaming": False,
            "supports_function_calling": False,
            "cost_per_input_token": 0.00,
            "cost_per_output_token": 0.00,
            "capabilities": {
                "context_window": 8192,
                "modalities": ["text"],
            },
            "tags": ["embedding", "local", "open-weight",
                     "self-hosted", "tiny"],
        },
        {
            "model_name": "mxbai-embed-large",
            "parameters": 0.33,
            "name": "Ollama MxBAI Embed Large",
            "description": "MxBAI Large embedding model. Strong retrieval performance.",
            "usage": "High-quality embeddings, semantic search, RAG pipelines",
            "mode": "embedding",
            "supports_vision": False,
            "cost_per_input_token": 0.00,
            "cost_per_output_token": 0.00,
            "capabilities": {
                "context_window": 512,
                "modalities": ["text"],
            },
            "tags": ["embedding", "local", "open-weight",
                     "self-hosted", "small"],
        },
    ]
