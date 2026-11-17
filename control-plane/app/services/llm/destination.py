# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Whether a model row has a destination.

The gateway sends a request for a model to the row's own `api_url`, or to the
one base URL it knows for the row's provider on the API in question, and
nowhere else. A row with neither is refused at dispatch: 501
`provider_api_url_missing` (`brutor-gateway-core/src/services/providers.rs`:
`upstream_base_for`, `known_base`, `dispatch_refusal`), and a row of a cloud provider (Amazon Bedrock, Vertex AI) it has no
backend for is refused whatever its `api_url`: 501
`unsupported_provider_api`.
This module is the control plane's copy of those rules, so that such a row
is refused when it is saved and the catalog can say which entries need an
address.

The tables are the gateway's. `tests/test_model_destination_rule.py` reads
the gateway's source and fails when the two differ: change them there and
here together.
"""

from __future__ import annotations

from typing import Optional

from app.constants.llm import LLModelMode

# `discover_base_url`: the providers with one known base for the LLM routes
# (chat, responses, completions, embeddings, rerank, moderation, Messages,
# Gemini). Matched exactly, as there.
LLM_BASE_PROVIDERS = frozenset({
    "openai", "anthropic", "gemini", "google_ai_studio", "mistral", "cohere",
    "xai", "deepseek", "moonshot", "minimax", "groq", "together_ai",
    "fireworks_ai", "perplexity", "voyage", "jina_ai", "elevenlabs",
    "deepgram", "stability", "fal_ai", "sambanova", "cerebras", "ai21",
    "deepinfra", "huggingface", "openrouter",
})

# `known_base`, the media arms: by model mode. Matched lower-cased, as there.
MEDIA_BASE_PROVIDERS = {
    LLModelMode.IMAGE_GENERATION.value: frozenset({
        "openai", "fal", "fal_ai", "fal-ai", "replicate", "stability",
        "stability_ai", "stability-ai", "gemini", "google", "google_ai",
    }),
    LLModelMode.AUDIO_SPEECH.value: frozenset({
        "openai", "elevenlabs", "eleven_labs", "eleven-labs", "google",
        "google_cloud", "google-cloud",
    }),
    LLModelMode.AUDIO_TRANSCRIPTION.value: frozenset({
        "openai", "deepgram", "assemblyai", "assembly_ai", "assembly-ai",
        "elevenlabs", "eleven_labs", "eleven-labs", "google", "google_cloud",
        "google-cloud",
    }),
    LLModelMode.VIDEO_GENERATION.value: frozenset({"openai", "fal", "fal_ai"}),
}

# The clouds: Amazon Bedrock and Vertex AI (`providers::cloud_backend`,
# `dispatch_refusal`; `bedrock_runtime::family`;
# `anthropic_adapter::should_route_via_openai_surface`). A row of a cloud
# provider is served by that provider's own backend, which builds the URL
# from the row's region or project and authenticates with the row's cloud
# credential, or it is refused, 501 `unsupported_provider_api`. It never
# reaches a forwarder that has only `api_url` and an API key, so an `api_url`
# gives a cloud row no destination. The gateway's table, by the mode of the
# row (the route a row of that mode is called on):
#
# * Claude on Bedrock (the name contains `anthropic` or `claude`): chat (the
#   chat and Messages routes);
# * the Converse chat families `amazon.nova*` (not canvas, reel, sonic,
#   embed), `mistral.*`, `deepseek.*` on Bedrock: chat;
# * `amazon.titan-embed-text*` and `cohere.embed-{english,multilingual}-v3`
#   on Bedrock: embedding;
# * Vertex AI (`vertex_ai`; `vertex` is the same provider): chat. Claude and
#   Gemini on the chat route, Claude on the Messages route, every model of
#   Google's on the Gemini route, which is called with a chat row.
#
# Every other Bedrock model, and every cloud row in any other mode
# (responses, completion, embedding on Vertex, rerank, the media modes), has
# no backend.
BEDROCK_PROVIDERS = frozenset({"bedrock", "bedrock_converse"})
BEDROCK_CLAUDE = "claude"
BEDROCK_CONVERSE_CHAT = "converse_chat"
BEDROCK_TITAN_EMBED = "titan_embed"
BEDROCK_COHERE_EMBED = "cohere_embed"
# `bedrock_runtime::family_name`: an inference profile is `<geography>.<vendor>.<model>`.
_BEDROCK_VENDORS = ("amazon", "mistral", "deepseek", "cohere")
_BEDROCK_GEOGRAPHIES = ("us", "eu", "apac", "ca", "jp", "global")
_NOVA_NOT_CHAT = ("canvas", "reel", "sonic", "embed")

VERTEX_PROVIDERS = frozenset({"vertex_ai", "vertex"})
CLOUD_PROVIDERS = BEDROCK_PROVIDERS | VERTEX_PROVIDERS


def bedrock_family(provider: Optional[str], model_name: Optional[str]) -> Optional[str]:
    """What the gateway serves a Bedrock row as; `None` for a row of another
    provider and for a Bedrock model it does not implement."""
    if provider not in BEDROCK_PROVIDERS:
        return None
    name = model_name or ""
    lowered = name.lower()
    # `should_route_via_openai_surface`.
    if "anthropic" in lowered or "claude" in lowered:
        return BEDROCK_CLAUDE
    # `bedrock_runtime::model_id`: without the catalog's prefixes.
    for prefix in ("bedrock/", "converse/"):
        if name.startswith(prefix):
            name = name[len(prefix):]
    # `family_name`: after the last `/` of an ARN, without the geography.
    base = name.lower().rsplit("/", 1)[-1]
    labels = base.split(".", 2)
    if len(labels) == 3 and labels[0] in _BEDROCK_GEOGRAPHIES and labels[1] in _BEDROCK_VENDORS:
        base = f"{labels[1]}.{labels[2]}"
    if base.startswith("amazon.nova"):
        return None if any(word in base for word in _NOVA_NOT_CHAT) else BEDROCK_CONVERSE_CHAT
    if base.startswith(("mistral.", "deepseek.")):
        return BEDROCK_CONVERSE_CHAT
    if base.startswith("amazon.titan-embed-text"):
        return BEDROCK_TITAN_EMBED
    if base.startswith(("cohere.embed-english-v3", "cohere.embed-multilingual-v3")):
        return BEDROCK_COHERE_EMBED
    return None


# `providers::ProviderApi`, as far as a cloud row is concerned: the APIs
# the gateway has a cloud backend on, and one name for all the others
# (responses, text completions, rerank, the media routes, batch).
API_CHAT = "chat"
API_CHAT_FORWARD = "chat_forward"
API_EMBEDDINGS = "embeddings"
API_MESSAGES = "messages"
API_GEMINI = "gemini"
API_OTHER = "other"


def is_cloud_claude(provider: Optional[str], model_name: Optional[str]) -> bool:
    """`anthropic_adapter::should_route_via_openai_surface` for a cloud row."""
    lowered = (model_name or "").lower()
    if provider in BEDROCK_PROVIDERS:
        return "anthropic" in lowered or "claude" in lowered
    return provider in VERTEX_PROVIDERS and "claude" in lowered


def cloud_backend(provider: Optional[str], model_name: Optional[str], api: str) -> bool:
    """`providers::cloud_backend`: whether the gateway has a backend that
    serves a cloud row on `api`. False for a row of another provider. The
    one copy of that table here: every other rule of this module about a
    cloud row on an API is read off it."""
    claude = is_cloud_claude(provider, model_name)
    if provider in BEDROCK_PROVIDERS:
        if claude:
            return api in (API_CHAT, API_MESSAGES)
        family = bedrock_family(provider, model_name)
        if family == BEDROCK_CONVERSE_CHAT:
            return api in (API_CHAT, API_CHAT_FORWARD)
        if family in (BEDROCK_TITAN_EMBED, BEDROCK_COHERE_EMBED):
            return api == API_EMBEDDINGS
        return False
    if provider in VERTEX_PROVIDERS:
        if claude:
            return api in (API_CHAT, API_MESSAGES)
        if api == API_CHAT:
            return "gemini" in (model_name or "").lower()
        return api == API_GEMINI
    return False


# The API a routing-group member or a fallback of a row in a mode is sent
# on: the chat forwarder (`llm_router::forward_chat_completion{,_streaming}`,
# `try_fallback_list`) for chat, the embeddings route's own chain
# (`resolve_fallback_chain` with `ProviderApi::Embeddings`) for embedding.
# In every other mode a cloud row has no backend at all.
_FORWARD_API = {
    LLModelMode.CHAT.value: API_CHAT_FORWARD,
    LLModelMode.EMBEDDING.value: API_EMBEDDINGS,
}


def forward_problem(
    provider: Optional[str], model_name: Optional[str], mode: Optional[str], role: str
) -> Optional[str]:
    """Why the gateway would drop a row as `role` ("a routing-group member",
    "a fallback") of a model or group in `mode`: a cloud row with no backend
    on the API such a candidate is sent on (a Claude or Gemini cloud row on
    the chat forwarder: it is skipped, with a log line, on every request).
    `None` for a row of another provider and for a cloud row served there."""
    if provider not in CLOUD_PROVIDERS:
        return None
    api = _FORWARD_API.get(mode or LLModelMode.CHAT.value, API_OTHER)
    if cloud_backend(provider, model_name, api):
        return None
    cloud = "Vertex AI" if provider in VERTEX_PROVIDERS else "Amazon Bedrock"
    return (
        f"unsupported_provider_api: model '{model_name}' (provider '{provider}') cannot be {role}: "
        f"the gateway serves it on {cloud} only when it is the model a request names, never as "
        "a candidate it forwards to, so it would skip it on every request. As a member or a fallback "
        "it forwards to Amazon Nova, Mistral and DeepSeek chat models on Bedrock (chat) and to the "
        "Amazon Titan Text and Cohere Embed v3 models on Bedrock (embedding), and to no other cloud model."
    )


def cloud_served_mode(provider: Optional[str], model_name: Optional[str]) -> Optional[str]:
    """The one mode the gateway has a backend for a cloud row in; `None` for
    a cloud model it serves in no mode, and for a row of another provider.
    Read off `cloud_backend`: chat for a row served on the chat, Messages
    or Gemini route (each is called with a chat row), embedding for one
    served on the embeddings route."""
    if any(cloud_backend(provider, model_name, api) for api in (API_CHAT, API_MESSAGES, API_GEMINI)):
        return LLModelMode.CHAT.value
    if cloud_backend(provider, model_name, API_EMBEDDINGS):
        return LLModelMode.EMBEDDING.value
    return None


def unsupported_api_problem(
    provider: Optional[str], mode: Optional[str], model_name: Optional[str]
) -> Optional[str]:
    """Why the gateway refuses a row whatever its `api_url`
    (`unsupported_provider_api`): a cloud row it has no backend for in the
    row's mode. `None` for a row it does not refuse so."""
    if provider not in CLOUD_PROVIDERS:
        return None
    mode = mode or LLModelMode.CHAT.value
    if mode == LLModelMode.ROUTING.value:
        return None
    served = cloud_served_mode(provider, model_name)
    if served == mode:
        return None
    cloud = "Vertex AI" if provider in VERTEX_PROVIDERS else "Amazon Bedrock"
    if served is None:
        return (
            f"unsupported_provider_api: the gateway does not implement the Amazon Bedrock API for model "
            f"'{model_name}' (provider '{provider}'), so it would refuse every request for it. On Bedrock "
            "it serves Anthropic Claude models and the Amazon Nova, Mistral and DeepSeek chat models as "
            "chat models, and the Amazon Titan Text and Cohere Embed v3 embedding models as embedding models."
        )
    return (
        f"unsupported_provider_api: the gateway serves model '{model_name}' (provider '{provider}') on "
        f"{cloud} as a {served} model only, so it would refuse every request for it as a {mode} model, "
        f"with or without an api_url. Set mode to {served}."
    )


def requires_api_url(provider: Optional[str], mode: Optional[str], model_name: Optional[str] = None) -> bool:
    """True when a row of `provider` in `mode` has no destination without
    its own `api_url`."""
    provider = provider or ""
    mode = mode or LLModelMode.CHAT.value
    # A routing group's virtual model is resolved to a member, never sent.
    if mode == LLModelMode.ROUTING.value:
        return False
    # A cloud row is served from its region or project, or refused whatever
    # its `api_url` (`unsupported_api_problem`): an address is never what
    # it lacks.
    if provider in CLOUD_PROVIDERS:
        return False
    media = MEDIA_BASE_PROVIDERS.get(mode)
    if media is not None:
        return provider.lower() not in media
    return provider not in LLM_BASE_PROVIDERS


def missing_api_url_problem(
    provider: Optional[str],
    mode: Optional[str],
    model_name: Optional[str],
    api_url: Optional[str],
) -> Optional[str]:
    """Why a row cannot be saved for want of an address; `None` for a row
    with a destination. Blank is missing, as in the gateway."""
    if (api_url or "").strip():
        return None
    if not requires_api_url(provider, mode, model_name):
        return None
    return (
        f"api_url is required: model '{model_name}' is of provider '{provider}', for which the "
        f"gateway knows no base URL for a {mode or LLModelMode.CHAT.value} model, so it would "
        "refuse every request for it (provider_api_url_missing). Set api_url to the address of "
        "your own endpoint."
    )


def destination_problem(
    provider: Optional[str],
    mode: Optional[str],
    model_name: Optional[str],
    api_url: Optional[str],
) -> Optional[str]:
    """Why the gateway would refuse every request for a row as it is: an API
    it does not implement for the row's provider, or no destination. `None`
    for a row it can dispatch."""
    return unsupported_api_problem(provider, mode, model_name) or missing_api_url_problem(
        provider, mode, model_name, api_url
    )
