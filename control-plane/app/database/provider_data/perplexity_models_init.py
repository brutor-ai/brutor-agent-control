# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Perplexity AI Models — Search-augmented LLMs.

Model prefix: perplexity/ (required)
Cost values: USD per 1M tokens.

No model is seeded (2026-11-11). Perplexity ended Sonar support on Chat
Completions on 2026-09-27; its current model is served on the Agent API
(POST /v1/agent) only, which the gateway does not speak. The provider is
kept so an operator can add a model by hand.
"""


def get_models():
    return [
    ]
