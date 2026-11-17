# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
AI21 Labs Models — Jamba SSM-Transformer hybrid architecture.

Source: https://docs.ai21.com/docs/jamba-foundation-models
Model prefix: ai21/ (required)
Cost values: USD per 1M tokens.
"""


def get_models():
    return [
        # =====================================================================
        # Jamba 2 (Latest Generation)
        # =====================================================================
        {
            "model_name": "ai21/jamba-large",
            "parameters": 398.0,
            "name": "AI21 Jamba Large",
            "description": "AI21's most powerful model. SSM-Transformer hybrid, 52B active params.",
            "usage": "Complex reasoning, long-document analysis, enterprise workflows",
            "supports_vision": False,
            "cost_per_input_token": 2.00,
            "cost_per_output_token": 8.00,
            "capabilities": {
                "context_window": 256000,
                "modalities": ["text"],
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "long-context", "enterprise",
                     "open-weight", "large"],
        },
        {
            "model_name": "ai21/jamba-mini",
            "parameters": 52.0,
            "name": "AI21 Jamba Mini",
            "description": "Efficient Jamba model (12B active). Fast processing, 256K context.",
            "usage": "General chat, document analysis, agentic workflows",
            "supports_vision": False,
            "cost_per_input_token": 0.20,
            "cost_per_output_token": 0.40,
            "capabilities": {
                "context_window": 256000,
                "modalities": ["text"],
                "supports_response_schema": True,
            },
            "tags": ["function-calling", "long-context", "cost-efficient",
                     "open-weight", "medium"],
        },

        # =====================================================================
        # Jamba 1.6 (Stable)
        # =====================================================================

        # =====================================================================
        # Jamba 1.5 (Legacy — Still Supported)
        # =====================================================================

        # =====================================================================
        # Jurassic-2 (Legacy Completion Models)
        # =====================================================================
    ]
