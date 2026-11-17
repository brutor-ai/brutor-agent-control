# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
LM Studio Models — Local model serving via LM Studio desktop app.

Model prefix: lm_studio/ (required)
Auth: None required (local server, typically http://localhost:1234)
Cost values: 0.00 (self-hosted, no per-token cost)

NOTE: LM Studio is a desktop application for running GGUF models locally.
Models available depend on what the user has downloaded in LM Studio.
The model_name after the prefix should match the model loaded in LM Studio.
All models disabled by default; require LM Studio running locally.
"""



# COST NOTE (2026-08-23): deliberately no per-token cost — LM Studio
# is local; token-level provider cost is genuinely $0.

def get_models():
    return [
        # =====================================================================
        # Large Chat Models
        # =====================================================================
        {
            "model_name": "lm_studio/llama-3.3-70b-instruct",
            "parameters": 70.0,
            "name": "LM Studio Llama 3.3 70B",
            "description": "Meta Llama 3.3 70B (GGUF) in LM Studio. Local desktop inference.",
            "usage": "General chat, coding, analysis — local desktop AI",
            "supports_vision": False,
            "cost_per_input_token": 0.00,
            "cost_per_output_token": 0.00,
            "capabilities": {
                "context_window": 128000,
                "modalities": ["text"],
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "local", "open-weight",
                     "self-hosted", "desktop", "large"],
        },
        {
            "model_name": "lm_studio/qwen3-32b",
            "parameters": 32.0,
            "name": "LM Studio Qwen3 32B",
            "description": "Alibaba Qwen3 32B (GGUF) in LM Studio. Local multilingual.",
            "usage": "Reasoning, multilingual, coding — local desktop",
            "supports_vision": False,
            "cost_per_input_token": 0.00,
            "cost_per_output_token": 0.00,
            "capabilities": {
                "context_window": 40960,
                "modalities": ["text"],
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "local", "open-weight",
                     "self-hosted", "desktop", "medium"],
        },

        # =====================================================================
        # Medium Chat Models
        # =====================================================================
        {
            "model_name": "lm_studio/phi-4",
            "parameters": 14.0,
            "name": "LM Studio Phi-4 14B",
            "description": "Microsoft Phi-4 14B (GGUF) in LM Studio. Compact reasoning.",
            "usage": "Reasoning, coding, math — runs on consumer hardware",
            "supports_vision": False,
            "cost_per_input_token": 0.00,
            "cost_per_output_token": 0.00,
            "capabilities": {
                "context_window": 16384,
                "modalities": ["text"],
            },
            "tags": ["reasoning", "local", "open-weight",
                     "self-hosted", "desktop", "medium"],
        },
        {
            "model_name": "lm_studio/mistral-7b-instruct",
            "parameters": 7.0,
            "name": "LM Studio Mistral 7B",
            "description": "Mistral 7B Instruct (GGUF) in LM Studio. Fast and capable.",
            "usage": "General chat, coding, lightweight local tasks",
            "supports_vision": False,
            "cost_per_input_token": 0.00,
            "cost_per_output_token": 0.00,
            "capabilities": {
                "context_window": 32768,
                "modalities": ["text"],
            },
            "tags": ["local", "open-weight", "self-hosted",
                     "desktop", "cost-efficient", "small"],
        },
        {
            "model_name": "lm_studio/gemma-3-12b-it",
            "parameters": 12.0,
            "name": "LM Studio Gemma 3 12B",
            "description": "Google Gemma 3 12B (GGUF) in LM Studio. Multimodal capable.",
            "usage": "Vision tasks, general chat, image understanding — local",
            "supports_vision": True,
            "cost_per_input_token": 0.00,
            "cost_per_output_token": 0.00,
            "capabilities": {
                "context_window": 128000,
                "modalities": ["text", "images"],
            },
            "tags": ["local", "open-weight", "self-hosted",
                     "desktop", "multimodal", "medium"],
        },

        # =====================================================================
        # Small/Code Models
        # =====================================================================
        {
            "model_name": "lm_studio/deepseek-coder-v2-lite-instruct",
            "parameters": 16.0,
            "name": "LM Studio DeepSeek Coder V2 Lite",
            "description": "DeepSeek Coder V2 Lite (GGUF) in LM Studio. Local coding.",
            "usage": "Code generation, debugging, completion — local development",
            "supports_vision": False,
            "cost_per_input_token": 0.00,
            "cost_per_output_token": 0.00,
            "capabilities": {
                "context_window": 128000,
                "modalities": ["text"],
            },
            "tags": ["coding", "local", "open-weight",
                     "self-hosted", "desktop", "medium"],
        },
    ]
