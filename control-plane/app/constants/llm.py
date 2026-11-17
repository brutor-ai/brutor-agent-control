# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
LLM Constants - Mode and Provider Enumerations

These enums define the valid values for model mode and provider fields
across the Brutor AI Platform. They are used for validation, seeding,
and routing decisions.

"""

from enum import Enum


# =========================================================================
# MODEL MODE
# =========================================================================

class LLModelMode(str, Enum):
    """
    Defines how a model is accessed and what endpoint it serves.

    Modes map to the endpoint a request is routed to:
    - CHAT           → /chat/completions
    - RESPONSES       → /responses - Structured output
    - COMPLETION      → /completions - Legacy
    - EMBEDDING       → /embeddings
    - IMAGE_GENERATION → /images/generations
    - AUDIO_TRANSCRIPTION → /audio/transcriptions
    - AUDIO_SPEECH    → /audio/speech
    - VIDEO_GENERATION → /videos/generations (async; provider-specific
                         polling. Brutor's video route exposes a job-id
                         submit + status-poll + cancel surface to the
                         portal.)
    - RERANK          → /rerank
    - MODERATION      → /moderations
    - ROUTING         → Internal virtual model (routing group)
    """
    CHAT = "chat"
    RESPONSES = "responses"
    COMPLETION = "completion"
    EMBEDDING = "embedding"
    IMAGE_GENERATION = "image_generation"
    AUDIO_TRANSCRIPTION = "audio_transcription"
    AUDIO_SPEECH = "audio_speech"
    VIDEO_GENERATION = "video_generation"
    RERANK = "rerank"
    MODERATION = "moderation"
    ROUTING = "routing"


def default_health_check_timeout_secs(mode: str | None) -> int:
    """
    Per-mode fallback for the health-check probe timeout, used when
    ``LLModel.timeout`` is null. The previous flat 30 s fallback was
    fine for chat / embedding probes (a single upstream round-trip)
    but aborted image and video probes mid-call — DALL-E 3 and
    Imagen routinely take 30-90 s, video gen can take minutes.

    Numbers track the seeded ``timeout`` field for each mode in
    ``provider_data/*.py`` so a manually-created model row with a
    null timeout behaves the same as its seeded peer.
    """
    if mode == LLModelMode.IMAGE_GENERATION:
        return 300
    if mode == LLModelMode.VIDEO_GENERATION:
        return 900
    if mode in (LLModelMode.AUDIO_TRANSCRIPTION, LLModelMode.AUDIO_SPEECH):
        return 120
    # chat / completion / responses / embedding / rerank / moderation /
    # routing — single round-trip, 60 s is plenty of headroom over
    # the typical 5-15 s upstream latency.
    return 60


# =========================================================================
# PROVIDER
# =========================================================================

class LLModelProvider(str, Enum):
    """
    LLM provider identifiers.

    These values are used as the model prefix (e.g., "openai/gpt-5.2")
    and stored in the provider column of the LLModel table.

    Organized into tiers:
    - Tier 1: Major direct-API providers (OpenAI, Anthropic, Google, Azure, AWS)
    - Tier 2: Important inference platforms (Groq, Together, Fireworks, etc.)
    - Tier 3: Specialized / self-hosted (Ollama, vLLM, NVIDIA NIM, etc.)
    - Internal: Brutor routing constructs
    """

    # ----- Tier 1: Major Direct API Providers -----
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GOOGLE_AI_STUDIO = "gemini"             # Google AI Studio (Gemini API)
    VERTEX_AI = "vertex_ai"                 # Google Cloud Vertex AI
    AZURE = "azure"                         # Azure OpenAI Service
    AZURE_AI = "azure_ai"                   # Azure AI Foundry (non-OpenAI models)
    BEDROCK = "bedrock"                     # AWS Bedrock (invoke API)
    BEDROCK_CONVERSE = "bedrock_converse"   # AWS Bedrock (converse API)
    MISTRAL = "mistral"                     # Mistral AI API
    COHERE = "cohere"                       # Cohere API
    XAI = "xai"                             # xAI (Grok models)
    DEEPSEEK = "deepseek"                   # DeepSeek API
    MOONSHOT = "moonshot"                   # Moonshot AI (Kimi K2.x + Moonshot V1)
    MINIMAX = "minimax"                     # MiniMax (M2.x text + Hailuo video + speech)

    # ----- Tier 2: Inference Platforms -----
    GROQ = "groq"                           # Groq (ultra-fast inference)
    TOGETHER_AI = "together_ai"             # Together AI
    FIREWORKS_AI = "fireworks_ai"           # Fireworks AI
    PERPLEXITY = "perplexity"               # Perplexity AI (search-augmented)
    OPENROUTER = "openrouter"               # OpenRouter (multi-provider)
    SAMBANOVA = "sambanova"                 # SambaNova
    CEREBRAS = "cerebras"                   # Cerebras
    AI21 = "ai21"                           # AI21 Labs (Jamba)
    DEEPINFRA = "deepinfra"                 # DeepInfra

    # ----- Tier 3: Self-Hosted / Specialized -----
    OLLAMA = "ollama"                       # Ollama (local models)
    VLLM = "vllm"                           # vLLM serving
    NVIDIA_NIM = "nvidia_nim"               # NVIDIA NIM
    DATABRICKS = "databricks"               # Databricks serving
    HUGGINGFACE = "huggingface"             # HuggingFace Inference Endpoints
    SAGEMAKER = "sagemaker"                 # AWS SageMaker
    LM_STUDIO = "lm_studio"                # LM Studio (local)

    # ----- Specialized Services -----
    VOYAGE = "voyage"                       # Voyage AI (embeddings)
    JINA_AI = "jina_ai"                     # Jina AI (embeddings)
    ELEVENLABS = "elevenlabs"               # ElevenLabs (TTS/STT)
    DEEPGRAM = "deepgram"                   # Deepgram (STT)
    ASSEMBLY_AI = "assemblyai"              # AssemblyAI (STT)
    STABILITY = "stability_ai"              # Stability AI (image gen)
    FAL_AI = "fal_ai"                       # Fal AI (image gen)
    REPLICATE = "replicate"                 # Replicate (image / video gen)
    GOOGLE_CLOUD = "google"                 # Google Cloud TTS / STT
                                            # (separate from GOOGLE_AI_STUDIO
                                            # which uses provider="gemini"
                                            # for chat/responses)

    # ----- OpenAI-Compatible Generic -----
    OPENAI_COMPATIBLE = "openai_compatible"  # Generic OpenAI-compatible endpoint

    # ----- Internal -----
    ROUTING_GROUP = "routing_group"         # Brutor virtual routing group
