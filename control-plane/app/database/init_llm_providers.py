# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Seed providers per tenant — the entry point that
populates `llm_providers` for new tenants.

Why this file exists
--------------------
Before migration `a027_llm_providers_table`, `LLModelProvider` (the Python enum) was the
canonical "list of providers Brutor knows about", and the
`llm_model.provider` string column was the source of truth for
"which one is this model?". That migration made providers DB rows.

For *existing* tenants the migration backfills one row per distinct
`(tenant_id, lower(provider))` pair found in `llm_model`. For
*new* tenants the provisioning pipeline calls
`create_llm_providers(...)` first, then `create_llm_models(...)`;
the latter resolves `provider_id` by looking up
`(tenant_id, key)` in `llm_providers`.

The catalogue below is the source of truth for the new-tenant
seed. Keep it in sync with the migration's `PROVIDER_METADATA` —
both tables should describe the same set of providers and metadata.
The duplication is intentional: the migration runs against
historical schema (no Pydantic, no app context), while this module
runs in the live app and gets to use ORM helpers + logging.

To add a provider
-----------------
1. Add an entry to `PROVIDER_SEED_CATALOG` below.
2. Add the matching key to `LLModelProvider` (in
   `app/constants/llm.py`) so the existing seed loader's registry
   can still reference it by enum.
3. Update `PROVIDER_METADATA` in
   `alembic/versions/a027_llm_providers_table.py` so existing
   tenants pick up the new provider when re-running their
   provisioning seed (or via a follow-up migration).
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session
from ulid import ULID

from app.database.models.llm_provider import LlmProvider
from app.database.models.llm_provider_catalog import (
    LlmProviderCatalog,
    PROVIDER_CATALOG_SOURCE_BUILTIN,
    seeded_provider_catalog_id,
)


# ---------------------------------------------------------------------------
# Icon bundling
# ---------------------------------------------------------------------------
#
# `app/database/provider_icons/{key}.svg` carries the seed icon for
# each catalogue provider. A small alias map handles cases where
# multiple providers share an icon (Bedrock and SageMaker both show
# the AWS mark; Vertex AI and Google Cloud TTS both show the Google
# mark) — keeps us from duplicating the same SVG bytes across files.
#
# Operators can replace any seeded icon via
# `POST /v1/admin/providers/{id}/icon`. Seeded icons are
# overwritten (not skipped) on re-seeding because the catalogue
# may have been updated; if an operator has uploaded a custom
# icon we preserve it (see `_should_seed_icon` below).
ICON_DIR = Path(__file__).resolve().parent / "provider_icons"

# Provider keys that share another provider's icon file. Anything
# not listed here looks for `{key}.svg` directly.
ICON_ALIAS: Dict[str, str] = {
    "vertex_ai": "gemini",
    "google": "gemini",
    "bedrock": "aws",
    "bedrock_converse": "aws",
    "sagemaker": "aws",
    "azure_ai": "azure",
    "together_ai": "together",
    "openai_compatible": "custom",
    # `assemblyai` and `replicate` don't have bundled icons today
    # — operators can upload via the admin UI. Listing them as
    # explicit `None` makes the gap visible if anyone audits.
    "assemblyai": "",
    "replicate": "",
}


def _resolve_icon_path(key: str) -> Optional[Path]:
    """Resolve the bundled SVG path for a provider key.

    Returns None when there's no bundled icon (operator-upload only).
    """
    file_stem = ICON_ALIAS.get(key, key)
    if not file_stem:
        return None
    candidate = ICON_DIR / f"{file_stem}.svg"
    if candidate.is_file():
        return candidate
    return None


def _load_seed_icon(key: str) -> Optional[Tuple[bytes, str]]:
    """Read bundled icon bytes + mime for the given key, or None."""
    path = _resolve_icon_path(key)
    if path is None:
        return None
    try:
        return path.read_bytes(), "image/svg+xml"
    except OSError:
        return None


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provider catalogue — what we seed for every new tenant.
# ---------------------------------------------------------------------------
#
# `key` is the slug; everything else is metadata that surfaces in the
# admin UI. The Rust proxy adapter dispatch matches on `key` and
# nothing else — adding fields here doesn't change runtime behaviour.
#
# `region` (data residency): present ONLY on providers whose
# upstream is honestly globally distributed (single anycast endpoint,
# no per-region selection by the operator). Stamped as `"global"` so
# tenants with `data_residency.block_global=true` correctly filter
# them out without operators having to set the region per-provider.
#
# Entries OMIT `region` when the upstream is operator-picked at
# config time (Azure/AWS Bedrock/Vertex pick a region in the URL;
# vLLM/Ollama/SageMaker self-hosted location varies). For those
# providers, region stays NULL — operators set it via the admin UI
# to match where their deployment actually lives.
PROVIDER_SEED_CATALOG: List[Dict[str, Any]] = [
    # =========================================================================
    # Tier 1 — major direct API providers
    # =========================================================================
    {
        "key": "openai",
        "display_name": "OpenAI",
        "category": "chat",
        "developer_url": "https://platform.openai.com/docs/",
        "api_key_url": "https://platform.openai.com/api-keys",
        "pricing_url": "https://openai.com/api/pricing/",
        "model_catalog_url": "https://platform.openai.com/docs/models",
        "models_api_url": "https://api.openai.com/v1/models",
        "default_api_url": "https://api.openai.com",
        "default_auth_type": "api_key",
        "region": "global",
    },
    {
        "key": "anthropic",
        "display_name": "Anthropic",
        "category": "chat",
        "developer_url": "https://docs.anthropic.com",
        "api_key_url": "https://console.anthropic.com/settings/keys",
        "pricing_url": "https://www.anthropic.com/pricing",
        "model_catalog_url": "https://docs.anthropic.com/en/docs/about-claude/models",
        "models_api_url": "https://api.anthropic.com/v1/models",
        "default_api_url": "https://api.anthropic.com",
        "default_auth_type": "api_key",
        "region": "global",
    },
    {
        "key": "gemini",
        "display_name": "Google AI Studio (Gemini)",
        "category": "chat",
        "developer_url": "https://ai.google.dev/gemini-api/docs",
        "api_key_url": "https://aistudio.google.com/apikey",
        "pricing_url": "https://ai.google.dev/gemini-api/docs/pricing",
        "model_catalog_url": "https://ai.google.dev/gemini-api/docs/models",
        # Gemini's Generative Language API uses ?key= query auth, not
        # bearer. Probe handler in the proxy switches auth style based
        # on the provider key (`gemini` → query param).
        "models_api_url": "https://generativelanguage.googleapis.com/v1beta/models",
        "default_auth_type": "api_key",
        "region": "global",
    },
    {
        "key": "vertex_ai",
        "display_name": "Google Vertex AI",
        "category": "chat",
        "developer_url": "https://cloud.google.com/vertex-ai/docs",
        "model_catalog_url": "https://cloud.google.com/vertex-ai/generative-ai/docs/model-reference",
        "default_auth_type": "gcp_service_account",
    },
    {
        "key": "azure",
        "display_name": "Azure OpenAI Service",
        "category": "chat",
        "developer_url": "https://learn.microsoft.com/azure/ai-services/openai/",
        "default_auth_type": "api_key",
    },
    {
        "key": "azure_ai",
        "display_name": "Azure AI Foundry",
        "category": "chat",
        "developer_url": "https://learn.microsoft.com/azure/ai-foundry/",
        "default_auth_type": "api_key",
    },
    {
        "key": "bedrock",
        "display_name": "AWS Bedrock",
        "category": "chat",
        "developer_url": "https://docs.aws.amazon.com/bedrock/",
        "default_auth_type": "aws_iam",
        "additional_drop_params": [
            "logprobs",
            "top_logprobs",
            "logit_bias",
            "frequency_penalty",
            "presence_penalty",
            "n",
            "parallel_tool_calls",
            "response_format",
            "seed",
            "suffix",
            "user",
            "stream_options",
        ],
    },
    {
        "key": "bedrock_converse",
        "display_name": "AWS Bedrock (Converse API)",
        "category": "chat",
        "developer_url": "https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference.html",
        "default_auth_type": "aws_iam",
        "additional_drop_params": [
            "logprobs",
            "top_logprobs",
            "logit_bias",
            "frequency_penalty",
            "presence_penalty",
            "n",
            "parallel_tool_calls",
            "response_format",
            "seed",
            "suffix",
            "user",
            "stream_options",
        ],
    },
    {
        "key": "mistral",
        "display_name": "Mistral AI",
        "category": "chat",
        "developer_url": "https://docs.mistral.ai/",
        "api_key_url": "https://console.mistral.ai/api-keys/",
        "pricing_url": "https://mistral.ai/pricing",
        "models_api_url": "https://api.mistral.ai/v1/models",
        "default_api_url": "https://api.mistral.ai",
        "default_auth_type": "api_key",
        "additional_drop_params": [
            "logprobs",
            "top_logprobs",
            "logit_bias",
            "n",
            "presence_penalty",
            "frequency_penalty",
            "suffix",
            "user",
            "stream_options",
        ],
        "region": "global",
    },
    {
        "key": "cohere",
        "display_name": "Cohere",
        "category": "chat",
        "developer_url": "https://docs.cohere.com/",
        "api_key_url": "https://dashboard.cohere.com/api-keys",
        "models_api_url": "https://api.cohere.com/v1/models",
        "default_auth_type": "api_key",
        "region": "global",
    },
    {
        "key": "xai",
        "display_name": "xAI",
        "category": "chat",
        "developer_url": "https://docs.x.ai/",
        "api_key_url": "https://console.x.ai/",
        "models_api_url": "https://api.x.ai/v1/models",
        "default_api_url": "https://api.x.ai",
        "default_auth_type": "api_key",
        # xAI's chat API rejects `stop` and `frequency_penalty` across
        # the grok-3-mini / grok-4* / grok-code* lineup.
        "additional_drop_params": ["stop", "frequency_penalty"],
        "region": "global",
    },
    {
        "key": "deepseek",
        "display_name": "DeepSeek",
        "category": "chat",
        "developer_url": "https://api-docs.deepseek.com/",
        "api_key_url": "https://platform.deepseek.com/api_keys",
        "models_api_url": "https://api.deepseek.com/v1/models",
        "default_api_url": "https://api.deepseek.com",
        "default_auth_type": "api_key",
        "additional_drop_params": [
            "logprobs",
            "top_logprobs",
            "logit_bias",
            "parallel_tool_calls",
            "suffix",
            "stream_options",
        ],
        "region": "global",
    },
    {
        "key": "moonshot",
        "display_name": "Moonshot AI (Kimi)",
        "category": "chat",
        "developer_url": "https://platform.moonshot.cn/docs/",
        "api_key_url": "https://platform.moonshot.cn/console/api-keys",
        "models_api_url": "https://api.moonshot.cn/v1/models",
        "default_auth_type": "api_key",
        "additional_drop_params": [
            "logprobs",
            "top_logprobs",
            "logit_bias",
            "parallel_tool_calls",
            "suffix",
            "stream_options",
        ],
        "region": "global",
    },
    {
        "key": "minimax",
        "display_name": "MiniMax",
        "category": "chat",
        "developer_url": "https://intl.minimaxi.com/document",
        "default_auth_type": "api_key",
        "additional_drop_params": [
            "logprobs",
            "top_logprobs",
            "logit_bias",
            "parallel_tool_calls",
            "suffix",
            "stream_options",
        ],
        "region": "global",
    },
    # =========================================================================
    # Tier 2 — inference platforms
    # =========================================================================
    {
        "key": "groq",
        "display_name": "Groq",
        "category": "chat",
        "developer_url": "https://console.groq.com/docs",
        "api_key_url": "https://console.groq.com/keys",
        "models_api_url": "https://api.groq.com/openai/v1/models",
        "default_api_url": "https://api.groq.com/openai",
        "default_auth_type": "api_key",
        "additional_drop_params": [
            "logprobs",
            "top_logprobs",
            "logit_bias",
            "parallel_tool_calls",
        ],
        "region": "global",
    },
    {
        "key": "together_ai",
        "display_name": "Together AI",
        "category": "chat",
        "developer_url": "https://docs.together.ai/",
        "api_key_url": "https://api.together.xyz/settings/api-keys",
        "models_api_url": "https://api.together.xyz/v1/models",
        "default_api_url": "https://api.together.xyz",
        "default_auth_type": "api_key",
        "additional_drop_params": [
            "logit_bias",
            "logprobs",
            "top_logprobs",
            "parallel_tool_calls",
            "suffix",
            "stream_options",
        ],
        "region": "global",
    },
    {
        "key": "fireworks_ai",
        "display_name": "Fireworks AI",
        "category": "chat",
        "developer_url": "https://docs.fireworks.ai/",
        "api_key_url": "https://fireworks.ai/account/api-keys",
        "models_api_url": "https://api.fireworks.ai/inference/v1/models",
        "default_api_url": "https://api.fireworks.ai/inference",
        "default_auth_type": "api_key",
        "region": "global",
    },
    {
        "key": "perplexity",
        "display_name": "Perplexity",
        "category": "chat",
        "developer_url": "https://docs.perplexity.ai/",
        # Perplexity does not expose a public /models listing; the
        # cheapest probe is a real one-token completion. NULL here →
        # health check falls back to the real-inference probe.
        "default_auth_type": "api_key",
        "additional_drop_params": [
            "tools",
            "tool_choice",
            "logprobs",
            "top_logprobs",
            "logit_bias",
            "n",
            "parallel_tool_calls",
            "suffix",
            "stream_options",
        ],
        "region": "global",
    },
    {
        "key": "openrouter",
        "display_name": "OpenRouter",
        "category": "chat",
        "developer_url": "https://openrouter.ai/docs",
        "models_api_url": "https://openrouter.ai/api/v1/models",
        "default_api_url": "https://openrouter.ai/api/v1",
        "default_auth_type": "api_key",
        "region": "global",
    },
    {
        "key": "sambanova",
        "display_name": "SambaNova",
        "category": "chat",
        "developer_url": "https://docs.sambanova.ai/",
        "models_api_url": "https://api.sambanova.ai/v1/models",
        "default_auth_type": "api_key",
        "region": "global",
    },
    {
        "key": "cerebras",
        "display_name": "Cerebras",
        "category": "chat",
        "developer_url": "https://inference-docs.cerebras.ai/",
        "models_api_url": "https://api.cerebras.ai/v1/models",
        "default_auth_type": "api_key",
        "region": "global",
    },
    {
        "key": "ai21",
        "display_name": "AI21 Labs",
        "category": "chat",
        "developer_url": "https://docs.ai21.com/",
        "models_api_url": "https://api.ai21.com/studio/v1/models",
        "default_auth_type": "api_key",
        "additional_drop_params": [
            "logprobs",
            "top_logprobs",
            "logit_bias",
            "parallel_tool_calls",
            "suffix",
            "user",
            "stream_options",
        ],
        "region": "global",
    },
    {
        "key": "deepinfra",
        "display_name": "DeepInfra",
        "category": "chat",
        "developer_url": "https://deepinfra.com/docs/",
        "models_api_url": "https://api.deepinfra.com/v1/openai/models",
        "default_auth_type": "api_key",
        "region": "global",
    },
    # =========================================================================
    # Tier 3 — self-hosted / specialised
    # =========================================================================
    {
        "key": "ollama",
        "display_name": "Ollama (self-hosted)",
        "category": "self_hosted",
        "developer_url": "https://ollama.com/library",
        # Ollama exposes an OpenAI-compatible /v1/models endpoint
        # (and the native /api/tags as an alternative). We use the
        # `<MODEL_API_URL>` sentinel here — the proxy substitutes
        # the model row's `api_url` at probe time. That gives us
        # the right URL in every deployment shape:
        #   - trial bundle: model.api_url = http://ollama:11434
        #     (set by docker-start.sh)
        #   - Kubernetes: model.api_url = the Ollama service's URL
        #     (set via BRUTOR_OLLAMA_BASE_URL)
        #   - native dev: http://localhost:11434
        # A literal `localhost` URL here would break Kubernetes and
        # docker-compose probes — exactly the symptom seen as
        # "Unknown" status on the Ollama row in the trial.
        "models_api_url": "<MODEL_API_URL>/v1/models",
        "default_api_url": "http://localhost:11434",
        "default_auth_type": "none",
        "additional_drop_params": [
            "logit_bias",
            "logprobs",
            "top_logprobs",
            "parallel_tool_calls",
            "suffix",
            "user",
            "stream_options",
        ],
    },
    {
        "key": "vllm",
        "display_name": "vLLM (self-hosted)",
        "category": "self_hosted",
        "developer_url": "https://docs.vllm.ai/",
        # vLLM mounts an OpenAI-compatible API on the operator's
        # chosen base. The probe substitutes the model's api_url for
        # the bracketed prefix (see proxy probe handler) so a model at
        # https://vllm.example/v1 probes
        # https://vllm.example/v1/models. Sentinel literally `<MODEL_API_URL>`
        # tells the probe handler to derive the URL from the model
        # row's api_url.
        "models_api_url": "<MODEL_API_URL>/v1/models",
        "default_auth_type": "none",
    },
    {
        "key": "nvidia_nim",
        "display_name": "NVIDIA NIM",
        "category": "self_hosted",
        "developer_url": "https://build.nvidia.com/",
        "default_auth_type": "api_key",
    },
    {
        "key": "databricks",
        "display_name": "Databricks Model Serving",
        "category": "self_hosted",
        "developer_url": "https://docs.databricks.com/machine-learning/model-serving/",
        "default_auth_type": "api_key",
        "additional_drop_params": [
            "logprobs",
            "top_logprobs",
            "logit_bias",
            "parallel_tool_calls",
            "suffix",
            "user",
            "stream_options",
        ],
    },
    {
        "key": "huggingface",
        "display_name": "HuggingFace Inference Endpoints",
        "category": "self_hosted",
        "developer_url": "https://huggingface.co/docs/inference-endpoints",
        "api_key_url": "https://huggingface.co/settings/tokens",
        "default_auth_type": "api_key",
        "region": "global",
    },
    {
        "key": "sagemaker",
        "display_name": "AWS SageMaker",
        "category": "self_hosted",
        "developer_url": "https://docs.aws.amazon.com/sagemaker/",
        "default_auth_type": "aws_iam",
    },
    {
        "key": "lm_studio",
        "display_name": "LM Studio (self-hosted)",
        "category": "self_hosted",
        "developer_url": "https://lmstudio.ai/docs",
        "models_api_url": "<MODEL_API_URL>/v1/models",
        "default_auth_type": "none",
    },
    # =========================================================================
    # Specialised modalities
    # =========================================================================
    {
        "key": "voyage",
        "display_name": "Voyage AI",
        "category": "embeddings",
        "developer_url": "https://docs.voyageai.com/",
        # Voyage doesn't expose a public /models listing; cheapest
        # honest probe is a real one-token embedding.
        "default_auth_type": "api_key",
        "region": "global",
    },
    {
        "key": "jina_ai",
        "display_name": "Jina AI",
        "category": "embeddings",
        "developer_url": "https://jina.ai/",
        "default_auth_type": "api_key",
        "region": "global",
    },
    {
        "key": "elevenlabs",
        "display_name": "ElevenLabs",
        "category": "audio",
        "developer_url": "https://elevenlabs.io/docs",
        "api_key_url": "https://elevenlabs.io/app/settings/api-keys",
        "pricing_url": "https://elevenlabs.io/pricing",
        "models_api_url": "https://api.elevenlabs.io/v1/models",
        "default_api_url": "https://api.elevenlabs.io",
        "default_auth_type": "api_key",
        "region": "global",
    },
    {
        "key": "deepgram",
        "display_name": "Deepgram",
        "category": "audio",
        "developer_url": "https://developers.deepgram.com/",
        "api_key_url": "https://console.deepgram.com/",
        "default_api_url": "https://api.deepgram.com",
        "default_auth_type": "api_key",
        "region": "global",
    },
    {
        "key": "assemblyai",
        "display_name": "AssemblyAI",
        "category": "audio",
        "developer_url": "https://www.assemblyai.com/docs/",
        "api_key_url": "https://www.assemblyai.com/app/account",
        "default_api_url": "https://api.assemblyai.com",
        "default_auth_type": "api_key",
        "region": "global",
    },
    {
        "key": "google",
        "display_name": "Google Cloud (TTS / STT)",
        "category": "audio",
        "developer_url": "https://cloud.google.com/text-to-speech/docs",
        "default_auth_type": "api_key",
    },
    {
        "key": "stability_ai",
        "display_name": "Stability AI",
        "category": "image",
        "developer_url": "https://platform.stability.ai/docs",
        "api_key_url": "https://platform.stability.ai/account/keys",
        "default_api_url": "https://api.stability.ai",
        "default_auth_type": "api_key",
        "region": "global",
    },
    {
        "key": "fal_ai",
        "display_name": "fal.ai",
        "category": "image",
        "developer_url": "https://fal.ai/docs",
        "api_key_url": "https://fal.ai/dashboard/keys",
        "default_api_url": "https://fal.run",
        "default_auth_type": "api_key",
        "region": "global",
    },
    {
        "key": "replicate",
        "display_name": "Replicate",
        "category": "image",
        "developer_url": "https://replicate.com/docs",
        "api_key_url": "https://replicate.com/account/api-tokens",
        "models_api_url": "https://api.replicate.com/v1/models",
        "default_api_url": "https://api.replicate.com",
        "default_auth_type": "api_key",
        "additional_drop_params": [
            "logprobs",
            "top_logprobs",
            "logit_bias",
            "n",
            "frequency_penalty",
            "presence_penalty",
            "parallel_tool_calls",
            "response_format",
            "suffix",
            "user",
            "stream_options",
        ],
        "region": "global",
    },
    # =========================================================================
    # Generic fallthroughs
    # =========================================================================
    {
        "key": "openai_compatible",
        "display_name": "OpenAI-compatible (generic)",
        "category": "chat",
        "default_auth_type": "api_key",
    },
    {
        "key": "routing_group",
        "display_name": "Brutor virtual routing group",
        "category": "internal",
        "default_auth_type": "none",
    },
]


# ---------------------------------------------------------------------------
# Global provider catalog seed
# ---------------------------------------------------------------------------

def _apply_import_descriptor(obj, key: str) -> None:
    """Hand a provider-catalog row to whoever has more to say about it.

    The Traffic Data Import columns (`supports_usage_import`,
    `import_api`, `import_capabilities`) belong to an Enterprise Edition
    feature, which stamps them through the `LLM_PROVIDER_CATALOG_ROW`
    behaviour hook. With no hook registered the row is left as the
    seed wrote it."""
    from app.composition.hooks import HOOKS, LLM_PROVIDER_CATALOG_ROW

    for hook in HOOKS.all(LLM_PROVIDER_CATALOG_ROW):
        hook(obj=obj, key=key)


def seed_llm_provider_catalog(db: Session, logger=None) -> int:
    """Populate the GLOBAL `llm_provider_catalog` table once from
    `PROVIDER_SEED_CATALOG` (+ the bundled `provider_icons/{key}.svg`).

    GLOBAL — no tenant scope. Idempotent: keyed on the deterministic
    seeded id (`provcat-{key}`). An existing row is refreshed in place
    (descriptive fields + icon); a missing row is inserted. Never
    duplicates on re-run, so it is safe to call on every tenant
    provision (it is the provider analogue of `seed_llm_catalog`).

    Returns the number of rows inserted (existing rows are updates).
    """
    log = logger or logging.getLogger(__name__)

    inserted = 0
    updated = 0
    for entry in PROVIDER_SEED_CATALOG:
        key = entry["key"]
        cat_id = seeded_provider_catalog_id(key)
        existing = db.get(LlmProviderCatalog, cat_id)

        seed_icon = _load_seed_icon(key)
        icon_data = seed_icon[0] if seed_icon else None
        icon_mime = seed_icon[1] if seed_icon else None
        icon_size = len(icon_data) if icon_data is not None else None
        icon_hash = (
            hashlib.sha256(icon_data).hexdigest()
            if icon_data is not None
            else None
        )

        if existing is None:
            row = LlmProviderCatalog(
                id=cat_id,
                key=key,
                display_name=entry["display_name"],
                category=entry.get("category"),
                developer_url=entry.get("developer_url"),
                api_key_url=entry.get("api_key_url"),
                pricing_url=entry.get("pricing_url"),
                model_catalog_url=entry.get("model_catalog_url"),
                models_api_url=entry.get("models_api_url"),
                icon_url=entry.get("icon_url"),
                default_api_url=entry.get("default_api_url"),
                default_auth_type=entry.get("default_auth_type"),
                additional_drop_params=entry.get("additional_drop_params"),
                region=entry.get("region"),
                notes=entry.get("notes"),
                icon_data=icon_data,
                icon_mime_type=icon_mime,
                icon_size=icon_size,
                icon_hash=icon_hash,
                source=PROVIDER_CATALOG_SOURCE_BUILTIN,
            )
            _apply_import_descriptor(row, key)
            db.add(row)
            inserted += 1
        else:
            # Refresh descriptive fields in place (builtin rows only —
            # never stomp an operator-authored custom row). Mirrors the
            # llm_catalog re-seed behaviour.
            if existing.source != PROVIDER_CATALOG_SOURCE_BUILTIN:
                continue
            existing.display_name = entry["display_name"]
            existing.category = entry.get("category")
            existing.developer_url = entry.get("developer_url")
            existing.api_key_url = entry.get("api_key_url")
            existing.pricing_url = entry.get("pricing_url")
            existing.model_catalog_url = entry.get("model_catalog_url")
            existing.models_api_url = entry.get("models_api_url")
            existing.icon_url = entry.get("icon_url")
            existing.default_api_url = entry.get("default_api_url")
            existing.default_auth_type = entry.get("default_auth_type")
            existing.additional_drop_params = entry.get(
                "additional_drop_params"
            )
            existing.region = entry.get("region")
            existing.notes = entry.get("notes")
            if icon_data is not None:
                existing.icon_data = icon_data
                existing.icon_mime_type = icon_mime
                existing.icon_size = icon_size
                existing.icon_hash = icon_hash
            _apply_import_descriptor(existing, key)
            updated += 1

    db.flush()
    if inserted or updated:
        log.info(
            f"Seeded global LLM provider catalog: +{inserted} ~{updated} "
            f"(catalogue had {len(PROVIDER_SEED_CATALOG)})"
        )
    return inserted


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def create_llm_providers(
    db: Session,
    tenant_id: str,
    *,
    logger_=logger,
) -> Dict[str, str]:
    """Insert one provider row per `PROVIDER_SEED_CATALOG` entry
    for the given tenant.

    DEPRECATED for new-tenant provisioning. Providers are now
    auto-imported on demand from the global `llm_provider_catalog`
    (`upsert_provider_for_key`) when a model that uses them is added —
    `create_llm_models` no longer calls this, so a fresh tenant does NOT
    receive all ~41 provider rows. Kept for any operator-driven
    "materialize the whole catalogue for this tenant" flows + the
    icon-backfill path; it must never be re-wired into the default
    provisioning seed.

    Idempotent: rows with a `(tenant_id, key)` already present are
    left untouched (no UPDATE — the "seeds are create-once" rule
    means operator edits via the admin UI are preserved across
    restarts).

    Returns a dict mapping `key` → `id` so the caller (typically
    `create_llm_models`) can resolve provider_id without a second
    query.
    """
    existing_rows = (
        db.query(LlmProvider)
        .filter(LlmProvider.tenant_id == tenant_id)
        .all()
    )
    by_key: Dict[str, str] = {row.key: row.id for row in existing_rows}
    existing_by_key: Dict[str, LlmProvider] = {row.key: row for row in existing_rows}

    # Backfill bundled icons onto rows that were seeded BEFORE the
    # icon-storage columns existed (neither the provider backfill nor
    # the icon-column migration populated bytes). We only stamp `is_system` rows whose
    # `icon_data` is still NULL — operator uploads on user-created
    # providers stay untouched. Skipped silently when there's no
    # bundled SVG for the slug.
    icon_backfilled = 0
    for entry in PROVIDER_SEED_CATALOG:
        key = entry["key"]
        existing = existing_by_key.get(key)
        if existing is None or not existing.is_system or existing.icon_data is not None:
            continue
        seed_icon = _load_seed_icon(key)
        if seed_icon is None:
            continue
        data, mime = seed_icon
        existing.icon_data = data
        existing.icon_mime_type = mime
        existing.icon_size = len(data)
        existing.icon_hash = hashlib.sha256(data).hexdigest()
        existing.icon_uploaded_at = datetime.now(timezone.utc)
        icon_backfilled += 1
    if icon_backfilled:
        db.flush()
        logger_.info(
            f"Backfilled bundled icons on {icon_backfilled} system "
            f"provider(s) for tenant {tenant_id}"
        )

    inserted = 0
    for entry in PROVIDER_SEED_CATALOG:
        key = entry["key"]
        if key in by_key:
            continue

        provider = LlmProvider(
            id=f"prov-{ULID()}",
            tenant_id=tenant_id,
            key=key,
            display_name=entry["display_name"],
            category=entry.get("category"),
            developer_url=entry.get("developer_url"),
            api_key_url=entry.get("api_key_url"),
            pricing_url=entry.get("pricing_url"),
            model_catalog_url=entry.get("model_catalog_url"),
            models_api_url=entry.get("models_api_url"),
            icon_url=entry.get("icon_url"),
            default_api_url=entry.get("default_api_url"),
            default_auth_type=entry.get("default_auth_type"),
            additional_drop_params=entry.get("additional_drop_params"),
            metadata_=entry.get("metadata"),
            # Data residency: stamps "global" on the Tier-1
            # direct-API providers and leaves NULL on BYOC ones
            # (Azure / Bedrock / Vertex / vLLM / Ollama). See the
            # catalogue header comment.
            region=entry.get("region"),
            enabled=True,
            is_system=True,
        )
        # Stamp the bundled icon on the row at creation. The bytes
        # live under `app/database/provider_icons/{key}.svg`; an
        # alias map covers shared marks (Bedrock/SageMaker → AWS,
        # Vertex/Google Cloud → Gemini, …). Providers without a
        # bundled icon stay null until the operator uploads one
        # via the admin UI.
        seed_icon = _load_seed_icon(key)
        if seed_icon is not None:
            data, mime = seed_icon
            provider.icon_data = data
            provider.icon_mime_type = mime
            provider.icon_size = len(data)
            provider.icon_hash = hashlib.sha256(data).hexdigest()
            provider.icon_uploaded_at = datetime.now(timezone.utc)
        db.add(provider)
        db.flush()
        by_key[key] = provider.id
        inserted += 1

    if inserted:
        # The caller's logger may be either a stdlib `logging.Logger`
        # (in tests, from `getLogger(__name__)`) or a `BrutorLogger`
        # (in production, where `info(msg, context=…, …)` is the
        # supported call shape). Earlier code passed printf-style
        # positional args that crashed BrutorLogger mid-transaction
        # during tenant provisioning — and rolled back the
        # resource-group inserts, which cascaded into FK violations
        # in the cache / role seeds. Format the string up-front so
        # the call is just `(message,)` and works against both
        # logger flavours.
        logger_.info(
            f"Seeded {inserted} LLM providers for tenant "
            f"{tenant_id} (catalogue had "
            f"{len(PROVIDER_SEED_CATALOG)}, "
            f"{len(PROVIDER_SEED_CATALOG) - inserted} already present)"
        )

    return by_key


def upsert_provider_for_key(
    db: Session,
    tenant_id: str,
    key: str,
    *,
    display_name: Optional[str] = None,
    category: Optional[str] = None,
    is_system: bool = False,
) -> LlmProvider:
    """Look up a provider by `(tenant_id, key)`; create a stub if
    it doesn't exist.

    Used by:
      * `init_llms.create_llm_models` to resolve `provider_id`
        for each seed model.
      * The HuggingFace import flow to auto-create a provider
        when a model card mentions a slug we haven't seen.

    The stub is intentionally minimal — operators are expected to
    fill in `display_name`, doc URLs, etc. via the admin UI. We
    pass `is_system=True` only when the seed pipeline calls us; the
    HF import flow uses `is_system=False` so operators can delete
    the auto-created row if they want.
    """
    existing = (
        db.query(LlmProvider)
        .filter(
            LlmProvider.tenant_id == tenant_id,
            LlmProvider.key == key.lower(),
        )
        .first()
    )
    if existing:
        return existing

    # Source the provider metadata from the GLOBAL `llm_provider_catalog`
    # table (seeded once by `seed_llm_provider_catalog`) rather than the
    # in-code PROVIDER_SEED_CATALOG list. This is the on-demand
    # materialization path that replaced the per-tenant bulk seed: a
    # tenant gets a real `llm_providers` row only for providers it
    # actually uses. The bundled icon blob is copied straight off the
    # catalog row (no filesystem re-read). When the slug isn't in the
    # catalog at all we fall back to the minimal stub (unchanged
    # behaviour) so an operator-added custom seed module / HF import for
    # an unknown slug still lands.
    catalog_row = (
        db.query(LlmProviderCatalog)
        .filter(LlmProviderCatalog.key == key.lower())
        .first()
    )
    if catalog_row is not None:
        # The is_system flag still respects the caller's preference (so
        # an operator-driven "import this slug from HF" doesn't
        # accidentally upgrade the provider to un-deletable system
        # status).
        provider = LlmProvider(
            id=f"prov-{ULID()}",
            tenant_id=tenant_id,
            key=catalog_row.key,
            display_name=catalog_row.display_name,
            category=catalog_row.category,
            developer_url=catalog_row.developer_url,
            api_key_url=catalog_row.api_key_url,
            pricing_url=catalog_row.pricing_url,
            model_catalog_url=catalog_row.model_catalog_url,
            models_api_url=catalog_row.models_api_url,
            icon_url=catalog_row.icon_url,
            default_api_url=catalog_row.default_api_url,
            default_auth_type=catalog_row.default_auth_type,
            additional_drop_params=catalog_row.additional_drop_params,
            # Same residency stamp as the catalogue claim.
            region=catalog_row.region,
            notes=catalog_row.notes,
            enabled=True,
            is_system=is_system,
            catalog_id=catalog_row.id,
        )
        # Copy the bundled icon blob off the catalog row.
        if catalog_row.icon_data is not None:
            provider.icon_data = catalog_row.icon_data
            provider.icon_mime_type = catalog_row.icon_mime_type
            provider.icon_size = catalog_row.icon_size
            provider.icon_hash = catalog_row.icon_hash
            provider.icon_uploaded_at = datetime.now(timezone.utc)
    else:
        provider = LlmProvider(
            id=f"prov-{ULID()}",
            tenant_id=tenant_id,
            key=key.lower(),
            display_name=display_name or key.replace("_", " ").title(),
            category=category,
            enabled=True,
            is_system=is_system,
        )
    db.add(provider)
    db.flush()
    logger.info(
        "Auto-created LLM provider %r (id=%s) for tenant %s",
        provider.key,
        provider.id,
        tenant_id,
    )
    return provider


__all__ = [
    "PROVIDER_SEED_CATALOG",
    "seed_llm_provider_catalog",
    "create_llm_providers",
    "upsert_provider_for_key",
]
