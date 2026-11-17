# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
A model row has a destination, or it is not saved.

The gateway sends a request to the row's own `api_url` or to the one base URL
it knows for the row's provider, and nowhere else: a row with neither is
refused at dispatch, 501 `provider_api_url_missing`
(`brutor-gateway-core/src/services/providers.rs`, `upstream_base_for`,
`dispatch_refusal`). The control plane used to save such a row. It refuses
it now, with a 422 that names the provider and the field, and the catalog
says which entries need an `api_url` (`requires_api_url`).

The same for an Amazon Bedrock row the gateway has no implementation for,
whatever its `api_url` (501 `unsupported_provider_api`; the
control plane used to save a Claude row on Bedrock in embedding mode, a Nova row in
any mode, a model family the gateway cannot call at all).

Held four ways: the rule's table (`app/services/llm/destination.py`) against
the gateway's, read from its source; the create, update and catalog-import
routes; the catalog's field; the seed (no default-enabled row without a
destination, and the list of catalog entries that need an address).
"""

from __future__ import annotations

import os
import re

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from app.api.v1.endpoints.admin.llm_catalog import create_llm_catalog_router
from app.api.v1.endpoints.admin.llm_models import create_llm_models_router
from app.core.logging_system import BrutorLogger
from app.database.init_llms import get_default_llm_configurations
from app.database.models.llm_catalog import LlmCatalog
from app.database.models.llm_model import LLModel
from app.database.models.llm_provider import LlmProvider
from app.services.llm import destination
from app.services.llm.model.llm_models_manager import LLModelsManager
from tests.admin_endpoint_harness import build_admin_harness
from tests.core_repo import core_revision, core_source


# --- the rule ---------------------------------------------------------------


@pytest.mark.parametrize(
    "provider, mode, model_name",
    [
        ("azure", "chat", "azure/gpt-5.6-sol"),
        ("azure", "embedding", "text-embedding-3-large"),
        ("azure_ai", "chat", "Phi-4"),
        ("ollama", "chat", "llama3.3"),
        ("lm_studio", "chat", "qwen3"),
        ("vllm", "chat", "meta-llama/Llama-3.3-70B-Instruct"),
        ("codestral", "chat", "codestral-latest"),
        ("databricks", "chat", "databricks-claude-sonnet-4"),
        ("cloudflare", "chat", "@cf/meta/llama-3.3-70b-instruct"),
        ("nvidia_nim", "chat", "meta/llama-3.3-70b-instruct"),
        ("sagemaker", "chat", "jumpstart-dft-meta-textgeneration-llama-3"),
        ("openai_compatible", "chat", "anything"),
        ("my-own-provider", "chat", "anything"),
        # Replicate has a base for images and none for the LLM routes.
        ("replicate", "chat", "meta/llama-3"),
        # A provider with an LLM base and none for this API.
        ("anthropic", "image_generation", "x"),
        ("deepgram", "audio_speech", "x"),
    ],
)
def test_a_row_with_no_known_base_needs_an_api_url(provider, mode, model_name):
    assert destination.requires_api_url(provider, mode) is True
    problem = destination.missing_api_url_problem(provider, mode, model_name, None)
    assert problem is not None
    assert "api_url" in problem and f"'{provider}'" in problem
    # Blank is missing, as in the gateway (`upstream_base_for` trims).
    assert destination.missing_api_url_problem(provider, mode, model_name, "   ") is not None
    assert destination.missing_api_url_problem(provider, mode, model_name, "https://h.example/v1") is None


@pytest.mark.parametrize(
    "provider, mode",
    [
        ("openai", "chat"),
        ("openai", "responses"),
        ("openai", "embedding"),
        ("openai", "image_generation"),
        ("openai", "audio_speech"),
        ("openai", "audio_transcription"),
        ("openai", "video_generation"),
        ("anthropic", "chat"),
        ("gemini", "chat"),
        ("gemini", "image_generation"),
        ("openrouter", "chat"),
        ("huggingface", "chat"),
        ("jina_ai", "rerank"),
        ("fal_ai", "image_generation"),
        ("fal_ai", "video_generation"),
        ("replicate", "image_generation"),
        ("stability_ai", "image_generation"),
        ("elevenlabs", "audio_speech"),
        ("elevenlabs", "audio_transcription"),
        ("deepgram", "audio_transcription"),
        ("assemblyai", "audio_transcription"),
        # (The clouds, Amazon Bedrock and Vertex AI, are below: a cloud row
        # is served by its provider's own backend or not at all.)
        # A routing group's virtual model is never dispatched.
        ("routing_group", "routing"),
    ],
)
def test_a_row_whose_destination_the_gateway_knows_needs_none(provider, mode):
    assert destination.requires_api_url(provider, mode) is False
    assert destination.missing_api_url_problem(provider, mode, "m", None) is None


# --- the clouds: what the gateway serves, in which mode ---------------------
#
# A row of Amazon Bedrock or Vertex AI is served by that provider's own
# backend, which builds the URL from the row's region or project and
# authenticates with the row's cloud credential, or it is refused: it never
# reaches a forwarder that has only `api_url` and an API key
# (`providers::cloud_backend`, `dispatch_refusal`).

CLAUDE = "anthropic.claude-sonnet-5-5-v1:0"
NOVA = "amazon.nova-pro-v1:0"
TITAN = "amazon.titan-embed-text-v2:0"
URL = "https://bedrock-runtime.eu-west-1.amazonaws.com"


@pytest.mark.parametrize(
    "provider, mode, model_name",
    [
        ("bedrock", "chat", CLAUDE),
        ("bedrock_converse", "chat", "bedrock/converse/us.anthropic.claude-haiku-5-5"),
        ("bedrock", "chat", "bedrock/global.anthropic.claude-opus-5-5"),
        ("bedrock", "chat", "claude-sonnet-5-5"),
        ("bedrock", "chat", NOVA),
        ("bedrock", "chat", "bedrock/amazon.nova-lite-v1:0"),
        ("bedrock_converse", "chat", "bedrock/converse/us.amazon.nova-micro-v1:0"),
        ("bedrock", "chat", "global.amazon.nova-2-lite-v1:0"),
        ("bedrock", "chat", "mistral.pixtral-large-2502-v1:0"),
        ("bedrock", "chat", "deepseek.r1-v1:0"),
        ("bedrock", "chat", "arn:aws:bedrock:eu-west-1:123456789012:inference-profile/eu.mistral.pixtral-large-2502-v1:0"),
        ("bedrock", "chat", "Amazon.Nova-Pro-v1:0"),
        ("bedrock", "embedding", TITAN),
        ("bedrock", "embedding", "bedrock/cohere.embed-english-v3"),
        ("bedrock_converse", "embedding", "cohere.embed-multilingual-v3"),
        # A routing group's virtual model is never dispatched.
        ("bedrock", "routing", "anything"),
        # Vertex AI: Claude and Gemini on the chat route; any other model of
        # Google's on the Gemini route, which is called with a chat row.
        ("vertex_ai", "chat", "claude-sonnet-5-5"),
        ("vertex", "chat", "claude-sonnet-4-5@20250929"),
        ("vertex_ai", "chat", "gemini-3-pro"),
        ("vertex", "chat", "gemini-2.5-flash"),
        ("vertex_ai", "chat", "some-model"),
        ("vertex", "routing", "anything"),
    ],
)
def test_a_cloud_row_the_gateway_serves_needs_no_api_url(provider, mode, model_name):
    assert destination.destination_problem(provider, mode, model_name, None) is None
    assert destination.requires_api_url(provider, mode, model_name) is False
    assert destination.destination_problem(provider, mode, model_name, URL) is None


@pytest.mark.parametrize(
    "provider, mode, model_name",
    [
        # A model family the gateway has no request body for.
        ("bedrock", "chat", "meta.llama4-70b-instruct-v1:0"),
        ("bedrock", "chat", "ai21.jamba-1-5-large-v1:0"),
        ("bedrock", "chat", "some-model"),
        ("bedrock_converse", "chat", "bedrock/converse/us.meta.llama4-70b-instruct-v1:0"),
        ("bedrock", "embedding", "amazon.titan-embed-image-v1"),
        ("bedrock", "embedding", "cohere.embed-v4:0"),
        # Nova's image, video, speech and embedding models are not chat models.
        ("bedrock", "chat", "amazon.nova-canvas-v1:0"),
        ("bedrock", "image_generation", "amazon.nova-canvas-v1:0"),
        ("bedrock", "chat", "amazon.nova-sonic-v1:0"),
        # A family it serves, in a mode it does not serve it in.
        ("bedrock", "embedding", NOVA),
        ("bedrock", "responses", NOVA),
        ("bedrock", "completion", "mistral.mistral-large-2407-v1:0"),
        ("bedrock", "chat", TITAN),
        ("bedrock_converse", "chat", "cohere.embed-english-v3"),
        ("bedrock", "rerank", "cohere.embed-english-v3"),
        # A geography that is not one, a vendor that is not one.
        ("bedrock", "chat", "moon.amazon.nova-pro-v1:0"),
        # Claude on Bedrock: the chat and Messages routes, and no other.
        ("bedrock", "embedding", CLAUDE),
        ("bedrock_converse", "responses", CLAUDE),
        ("bedrock", "completion", CLAUDE),
        ("bedrock", "image_generation", CLAUDE),
        # Vertex AI: nothing but a chat row has a backend.
        ("vertex_ai", "embedding", "gemini-embedding-001"),
        ("vertex", "responses", "gemini-3-pro"),
        ("vertex_ai", "completion", "claude-sonnet-5-5"),
        ("vertex", "embedding", "text-embedding-005"),
        ("vertex_ai", "image_generation", "imagen-4"),
        ("vertex_ai", "rerank", "semantic-ranker"),
    ],
)
def test_a_cloud_row_the_gateway_has_no_backend_for_is_refused_with_or_without_an_api_url(
    provider, mode, model_name
):
    for api_url in (None, URL):
        problem = destination.destination_problem(provider, mode, model_name, api_url)
        assert problem is not None and problem.startswith("unsupported_provider_api:"), problem
        assert f"'{model_name}'" in problem and f"'{provider}'" in problem
    # It is not an address that is missing.
    assert destination.requires_api_url(provider, mode, model_name) is False
    assert destination.missing_api_url_problem(provider, mode, model_name, None) is None


@pytest.mark.parametrize("provider", ["bedrock", "bedrock_converse", "vertex_ai", "vertex"])
def test_an_api_url_never_gives_a_cloud_row_a_destination(provider):
    for mode in ("chat", "embedding", "responses", "completion", "rerank", "image_generation", "audio_speech"):
        for name in (CLAUDE, NOVA, "gemini-3-pro", "x"):
            assert destination.requires_api_url(provider, mode, name) is False, (mode, name)
            assert destination.destination_problem(provider, mode, name, None) == destination.destination_problem(
                provider, mode, name, URL
            )


def test_vertex_is_vertex_ai():
    assert set(destination.VERTEX_PROVIDERS) == {"vertex_ai", "vertex"}
    for mode in ("chat", "embedding", "responses"):
        for name in ("claude-sonnet-5-5", "gemini-3-pro", "x"):
            one = destination.destination_problem("vertex_ai", mode, name, None)
            other = destination.destination_problem("vertex", mode, name, None)
            assert (one is None) == (other is None), (mode, name)


def test_the_cloud_rule_is_for_cloud_rows_only():
    assert destination.bedrock_family("openai", CLAUDE) is None
    assert destination.unsupported_api_problem("anthropic", "embedding", CLAUDE) is None
    assert destination.unsupported_api_problem("azure", "embedding", "meta.llama") is None
    assert set(destination.CLOUD_PROVIDERS) == {"bedrock", "bedrock_converse", "vertex_ai", "vertex"}
    assert destination.destination_problem("openai", "chat", "amazon.nova-canvas-v1:0", None) is None


def test_the_provider_is_matched_as_the_gateway_matches_it():
    # The LLM routes match the slug exactly; the media routes lower-case it.
    assert destination.requires_api_url("OpenAI", "image_generation") is False
    assert destination.requires_api_url("", "chat") is True


# --- the table against the gateway's, read from its source -----------------


def _read() -> str:
    """What was compared against, for a failure message: the gateway's
    source is read at its committed HEAD (`tests/core_repo.py`)."""
    return f" [the gateway's source read: {core_revision()}]"


def _function_body(source: str, signature: str) -> str:
    start = source.index(signature)
    end = source.index("\n}\n", start)
    return source[start:end]


def _arm_providers(text: str) -> set:
    """The provider names of the `"a" | "b" => Some(` arms in `text`."""
    names = set()
    for arm in re.finditer(r'((?:"[^"]+"\s*\|?\s*)+)=>\s*\{?\s*Some\(', text):
        names.update(re.findall(r'"([^"]+)"', arm.group(1)))
    return names


def test_the_llm_bases_are_the_gateways():
    source = core_source("services", "providers.rs").read_text(encoding="utf-8")
    body = _function_body(source, "pub fn discover_base_url(provider: &str)")
    assert _arm_providers(body) == set(destination.LLM_BASE_PROVIDERS), (
        "discover_base_url changed in the gateway: bring "
        "app/services/llm/destination.py LLM_BASE_PROVIDERS into line with it" + _read()
    )


def test_the_media_bases_are_the_gateways():
    source = core_source("services", "providers.rs").read_text(encoding="utf-8")
    body = _function_body(source, "fn known_base(provider: &str, api: ProviderApi)")
    # The LLM APIs all read `discover_base_url`.
    assert re.search(
        r"Llm \| Chat \| ChatForward \| Embeddings \| AnthropicNative \| GeminiNative => \{\s*"
        r"discover_base_url\(provider\)",
        body,
    ), "known_base no longer reads discover_base_url for the LLM APIs" + _read()
    sections = {}
    marks = [(m.start(), m.group(1)) for m in re.finditer(r"\n        (\w+) => match provider", body)]
    for (start, name), (end, _next) in zip(marks, marks[1:] + [(len(body), None)]):
        sections[name] = body[start:end]
    assert set(sections) == {"Images", "Speech", "Transcription", "Video", "Batch"}, sorted(sections)
    for api, mode in (
        ("Images", "image_generation"),
        ("Speech", "audio_speech"),
        ("Transcription", "audio_transcription"),
        ("Video", "video_generation"),
    ):
        assert _arm_providers(sections[api]) == set(destination.MEDIA_BASE_PROVIDERS[mode]), (
            f"known_base's {api} arm changed in the gateway: bring "
            f"MEDIA_BASE_PROVIDERS[{mode!r}] into line with it" + _read()
        )
        # Matched without regard to case there, so here.
        assert "match provider.to_lowercase().as_str()" in sections[api], api


def test_the_rows_whose_url_the_gateway_builds_are_the_gateways():
    source = core_source("services", "providers.rs").read_text(encoding="utf-8")
    read = _read()
    squeeze = lambda text: re.sub(r"\s+", " ", text)  # noqa: E731
    # The clouds, and that `vertex` is `vertex_ai`.
    assert 'pub fn is_bedrock(provider: &str) -> bool {\n    matches!(provider, "bedrock" | "bedrock_converse")\n}' in source, read
    assert 'pub fn is_vertex(provider: &str) -> bool {\n    matches!(provider, "vertex_ai" | "vertex")\n}' in source, read
    assert "pub fn is_cloud_provider(provider: &str) -> bool {\n    is_bedrock(provider) || is_vertex(provider)\n}" in source, read
    assert set(destination.BEDROCK_PROVIDERS) == {"bedrock", "bedrock_converse"}
    assert set(destination.VERTEX_PROVIDERS) == {"vertex_ai", "vertex"}
    # The one table of which API a cloud row is served on.
    table = squeeze(_function_body(source, "fn cloud_backend(model: &LlmModel, api: ProviderApi) -> Option<CloudBackend>"))
    assert table.endswith(squeeze("""
        let claude = crate::services::anthropic_adapter::should_route_via_openai_surface(model);
        if is_bedrock(&model.provider) {
            use crate::services::bedrock_runtime::Family;
            if claude {
                return matches!(api, Chat | AnthropicNative).then_some(CloudBackend::BedrockClaude);
            }
            return match (crate::services::bedrock_runtime::family(model), api) {
                (Some(Family::ConverseChat), Chat | ChatForward)
                | (Some(Family::TitanEmbed | Family::CohereEmbed), Embeddings) => {
                    Some(CloudBackend::BedrockRuntime)
                }
                _ => None,
            };
        }
        if is_vertex(&model.provider) {
            let served = if claude {
                matches!(api, Chat | AnthropicNative)
            } else {
                match api {
                    Chat => model.model_name.to_ascii_lowercase().contains("gemini"),
                    GeminiNative => true,
                    _ => false,
                }
            };
            return served.then_some(CloudBackend::Vertex);
        }
        None
    """).strip()), (
        "cloud_backend changed in the gateway: bring app/services/llm/destination.py "
        "(CLOUD_PROVIDERS, cloud_served_mode) into line with it" + read
    )
    # A cloud row is decided by that table and nowhere below it: no backend,
    # no dispatch, whatever its `api_url`.
    dispatch = squeeze(_function_body(source, "pub fn dispatch_refusal(model: &LlmModel, api: ProviderApi)"))
    # (After the one check that is about no kind of row: a stored
    # credential the gateway could not decrypt.)
    assert dispatch.split("{", 1)[1].lstrip().startswith(
        "if let Some(refusal) = unreadable_credential_refusal(model) { return Some(refusal); } "
        "if is_cloud_provider(&model.provider) { "
        "use crate::services::anthropic_adapter::backend::{bedrock, vertex}; "
        "let problem = match cloud_backend(model, api) { None => { return Some(DispatchRefusal { "
        'error_type: "unsupported_provider_api", reason: unsupported_cloud_api(model), }); }'
    ), "dispatch_refusal no longer decides a cloud row first" + read
    assert "return problem.map(|(error_type, reason)| DispatchRefusal { error_type, reason }); }" in dispatch, read
    refusal = squeeze(_function_body(source, "pub fn unsupported_provider_api(model: &LlmModel, api: ProviderApi)"))
    assert "(is_cloud_provider(&model.provider) && cloud_backend(model, api).is_none())" in refusal, read
    # Which rows are Claude rows.
    adapter = core_source("services", "anthropic_adapter", "mod.rs").read_text(encoding="utf-8")
    surface = _function_body(adapter, "pub fn should_route_via_openai_surface(model: &LlmModel) -> bool")
    assert re.search(
        r'"bedrock" \| "bedrock_converse" => \{\s*model_lc\.contains\("anthropic"\) \|\| model_lc\.contains\("claude"\)\s*\}',
        surface,
    ), "should_route_via_openai_surface changed in the gateway" + read
    assert '"vertex_ai" | "vertex" => model_lc.contains("claude"),' in surface, read
    # The families, as `bedrock_runtime::family` and `family_name` have them.
    runtime = core_source("services", "bedrock_runtime", "mod.rs").read_text(encoding="utf-8")
    family = re.sub(r"\s+", " ", _function_body(runtime, "pub fn family(model: &LlmModel) -> Option<Family>"))
    for piece in (
        'if name.starts_with("amazon.nova") {',
        'let other = ["canvas", "reel", "sonic", "embed"] .iter() .any(|k| name.contains(k)); '
        "return (!other).then_some(Family::ConverseChat);",
        'if name.starts_with("mistral.") || name.starts_with("deepseek.") { return Some(Family::ConverseChat); }',
        'if name.starts_with("amazon.titan-embed-text") { return Some(Family::TitanEmbed); }',
        'if name.starts_with("cohere.embed-english-v3") || name.starts_with("cohere.embed-multilingual-v3") '
        "{ return Some(Family::CohereEmbed); }",
    ):
        assert piece in family, f"bedrock_runtime::family changed in the gateway ({piece!r})" + read
    assert family.count("return ") == 5 and family.rstrip().endswith("None"), read
    assert 'const VENDORS: &[&str] = &["amazon", "mistral", "deepseek", "cohere"];' in runtime, read
    assert 'const GEOGRAPHIES: &[&str] = &["us", "eu", "apac", "ca", "jp", "global"];' in runtime, read
    assert list(destination._BEDROCK_VENDORS) == ["amazon", "mistral", "deepseek", "cohere"]
    assert list(destination._BEDROCK_GEOGRAPHIES) == ["us", "eu", "apac", "ca", "jp", "global"]
    assert list(destination._NOVA_NOT_CHAT) == ["canvas", "reel", "sonic", "embed"]
    model_id = re.sub(r"\s+", " ", _function_body(runtime, "pub fn model_id(model: &LlmModel) -> &str"))
    assert 'let name = name.strip_prefix("bedrock/").unwrap_or(name); name.strip_prefix("converse/").unwrap_or(name)' in model_id, read
    # The refusal the control plane pre-empts is this one.
    assert 'pub const PROVIDER_API_URL_MISSING: &str = "provider_api_url_missing";' in source, read


# --- the routes --------------------------------------------------------------

PROVIDERS = ("openai", "azure", "ollama", "vertex_ai", "bedrock", "replicate")


@pytest.fixture()
def h():
    log = BrutorLogger("test-model-destination")
    manager = LLModelsManager(logger=log, encryption_service=None)
    harness = build_admin_harness(
        [
            create_llm_models_router(manager, log, encryption_service=None),
            create_llm_catalog_router(manager, log, encryption_service=None),
        ],
        logger_name="test-model-destination",
    )

    def fn(s):
        for key in PROVIDERS:
            s.add(LlmProvider(id=f"prov-{key}", tenant_id="t1", key=key, display_name=key))
        s.add(LlmCatalog(id="llmcat-azure-gpt", provider_key="azure", model_name="azure/gpt-5.6-sol",
                         display_name="GPT on Azure", mode="chat", source="builtin"))
        s.add(LlmCatalog(id="llmcat-openai-gpt", provider_key="openai", model_name="gpt-6-sol",
                         display_name="GPT", mode="chat", source="builtin"))
        s.add(LlmCatalog(id="llmcat-vertex-embed", provider_key="vertex_ai", model_name="gemini-embedding-001",
                         display_name="Gemini Embedding on Vertex", mode="embedding", source="builtin"))
        s.add(LlmCatalog(id="llmcat-bedrock-nova", provider_key="bedrock", model_name="amazon.nova-pro-v1:0",
                         display_name="Nova on Bedrock", mode="chat", source="builtin"))

    harness.seed(fn)
    yield harness
    harness.close()


def _body(provider, name="m", **over):
    body = {
        "name": name,
        "provider_id": f"prov-{provider}",
        # A Bedrock row is served by what its model is.
        "model_name": "amazon.nova-pro-v1:0" if provider == "bedrock" else "some-model",
        "mode": "chat",
        # An enabled cloud row needs its own credential, which this harness
        # (no encryption service) cannot store: the destination rule is the
        # same for a row that is switched off.
        "enabled": provider not in ("bedrock", "vertex_ai"),
    }
    body.update(over)
    return body


def _assert_refused(r, provider):
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, str), detail
    assert "api_url" in detail, detail
    assert f"'{provider}'" in detail, detail


def _models(h):
    with h.Factory() as s:
        return [(m.name, m.api_url) for m in s.query(LLModel).all()]


class TestCreate:
    @pytest.mark.parametrize("provider", ["azure", "ollama"])
    def test_a_row_with_no_destination_is_refused(self, h, provider):
        r = h.client.post("/v1/admin/llms", json=_body(provider), headers=h.headers())
        _assert_refused(r, provider)
        assert _models(h) == []

    def test_a_blank_api_url_is_no_destination(self, h):
        # Refused by the request model itself: the outbound policy takes no
        # empty URL.
        r = h.client.post("/v1/admin/llms", json=_body("azure", api_url="   "), headers=h.headers())
        assert r.status_code == 422, r.text
        assert r.json()["detail"][0]["loc"] == ["body", "api_url"], r.text
        assert _models(h) == []

    def test_the_legacy_slug_of_an_unknown_provider_is_refused_too(self, h):
        body = _body("azure")
        del body["provider_id"]
        body["provider"] = "openai_compatible"
        r = h.client.post("/v1/admin/llms", json=body, headers=h.headers())
        _assert_refused(r, "openai_compatible")
        assert _models(h) == []

    def test_the_mode_decides(self, h):
        # Replicate: a base for images, none for chat.
        r = h.client.post("/v1/admin/llms", json=_body("replicate"), headers=h.headers())
        _assert_refused(r, "replicate")
        r = h.client.post(
            "/v1/admin/llms", json=_body("replicate", mode="image_generation"), headers=h.headers()
        )
        assert r.status_code == 201, r.text
        # Vertex: served on the chat route; an embedding row has no backend,
        # with an address or without.
        for extra in ({}, {"api_url": "https://europe-west4-aiplatform.googleapis.com"}):
            r = h.client.post(
                "/v1/admin/llms", json=_body("vertex_ai", name="v-embed", mode="embedding", **extra), headers=h.headers()
            )
            assert r.status_code == 422, r.text
            assert r.json()["detail"].startswith("unsupported_provider_api:"), r.text
        r = h.client.post("/v1/admin/llms", json=_body("vertex_ai", name="v-chat"), headers=h.headers())
        assert r.status_code == 201, r.text

    @pytest.mark.parametrize("provider", ["azure", "ollama"])
    def test_with_an_api_url_it_is_saved(self, h, provider):
        r = h.client.post(
            "/v1/admin/llms",
            json=_body(provider, api_url="https://models.example.com/v1"),
            headers=h.headers(),
        )
        assert r.status_code == 201, r.text
        assert _models(h) == [("m", "https://models.example.com/v1")]

    @pytest.mark.parametrize("provider", ["openai", "bedrock"])
    def test_a_provider_the_gateway_knows_needs_none(self, h, provider):
        r = h.client.post("/v1/admin/llms", json=_body(provider), headers=h.headers())
        assert r.status_code == 201, r.text


    def test_a_bedrock_model_the_gateway_does_not_implement_is_refused(self, h):
        for body in (
            _body("bedrock", model_name="meta.llama4-70b-instruct-v1:0"),
            _body("bedrock", model_name="meta.llama4-70b-instruct-v1:0", api_url="https://bedrock.example.com"),
            _body("bedrock", mode="embedding"),
            _body("bedrock", model_name="amazon.titan-embed-text-v2:0"),
        ):
            r = h.client.post("/v1/admin/llms", json=body, headers=h.headers())
            assert r.status_code == 422, r.text
            assert r.json()["detail"].startswith("unsupported_provider_api:"), r.text
        # A Claude row in a mode its backend does not serve, with an address too.
        r = h.client.post(
            "/v1/admin/llms",
            json=_body("bedrock", model_name="anthropic.claude-sonnet-5-5-v1:0", mode="embedding",
                       api_url="https://bedrock.example.com"),
            headers=h.headers(),
        )
        assert r.status_code == 422 and r.json()["detail"].startswith("unsupported_provider_api:"), r.text
        assert _models(h) == []
        for n, body in enumerate((
            _body("bedrock", model_name="anthropic.claude-sonnet-5-5-v1:0"),
            _body("bedrock", model_name="amazon.titan-embed-text-v2:0", mode="embedding"),
        )):
            r = h.client.post("/v1/admin/llms", json={**body, "name": f"ok-{n}"}, headers=h.headers())
            assert r.status_code == 201, r.text


class TestUpdate:
    def _saved(self, h, provider="azure", **over):
        over.setdefault("api_url", "https://models.example.com/v1")
        r = h.client.post("/v1/admin/llms", json=_body(provider, **over), headers=h.headers())
        assert r.status_code == 201, r.text
        return r.json()["model"]["id"]

    @pytest.mark.parametrize("blank", [None, "", "  "])
    def test_taking_the_api_url_away_is_refused(self, h, blank):
        mid = self._saved(h)
        r = h.client.patch(f"/v1/admin/llms/{mid}", json={"api_url": blank}, headers=h.headers())
        _assert_refused(r, "azure")
        assert _models(h) == [("m", "https://models.example.com/v1")]

    def test_moving_to_a_provider_with_no_base_is_refused(self, h):
        r = h.client.post("/v1/admin/llms", json=_body("openai"), headers=h.headers())
        assert r.status_code == 201, r.text
        mid = r.json()["model"]["id"]
        r = h.client.patch(f"/v1/admin/llms/{mid}", json={"provider_id": "prov-ollama"}, headers=h.headers())
        _assert_refused(r, "ollama")
        with h.Factory() as s:
            assert s.get(LLModel, mid).provider_id == "prov-openai"
        # With the address in the same request it is saved.
        r = h.client.patch(
            f"/v1/admin/llms/{mid}",
            json={"provider_id": "prov-ollama", "api_url": "http://ollama.example.com:11434"},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text

    def test_changing_the_mode_to_one_with_no_base_is_refused(self, h):
        r = h.client.post(
            "/v1/admin/llms", json=_body("replicate", mode="image_generation"), headers=h.headers()
        )
        assert r.status_code == 201, r.text
        mid = r.json()["model"]["id"]
        r = h.client.patch(f"/v1/admin/llms/{mid}", json={"mode": "chat"}, headers=h.headers())
        _assert_refused(r, "replicate")

    def test_changing_a_bedrock_rows_mode_or_model_to_one_the_gateway_refuses_is_refused(self, h):
        r = h.client.post("/v1/admin/llms", json=_body("bedrock"), headers=h.headers())
        assert r.status_code == 201, r.text
        mid = r.json()["model"]["id"]
        for change in ({"mode": "embedding"}, {"model_name": "meta.llama4-70b-instruct-v1:0"}):
            r = h.client.patch(f"/v1/admin/llms/{mid}", json=change, headers=h.headers())
            assert r.status_code == 422, r.text
            assert r.json()["detail"].startswith("unsupported_provider_api:"), r.text
        with h.Factory() as s:
            assert (s.get(LLModel, mid).mode, s.get(LLModel, mid).model_name) == ("chat", "amazon.nova-pro-v1:0")
        r = h.client.patch(f"/v1/admin/llms/{mid}", json={"model_name": "mistral.pixtral-large-2502-v1:0"}, headers=h.headers())
        assert r.status_code == 200, r.text

    def test_another_field_of_a_row_with_an_api_url_is_updated(self, h):
        mid = self._saved(h)
        r = h.client.patch(f"/v1/admin/llms/{mid}", json={"description": "d"}, headers=h.headers())
        assert r.status_code == 200, r.text


class TestCatalog:
    def test_the_catalog_says_which_entries_need_an_api_url(self, h):
        r = h.client.get("/v1/admin/llm-catalog", headers=h.headers())
        assert r.status_code == 200, r.text
        flags = {e["id"]: e["requires_api_url"] for e in r.json()["catalog"]}
        assert flags == {
            "llmcat-azure-gpt": True,
            "llmcat-openai-gpt": False,
            # Not an address that is missing: the gateway has no backend
            # for a Vertex AI embedding model.
            "llmcat-vertex-embed": False,
            "llmcat-bedrock-nova": False,
        }
        r = h.client.get("/v1/admin/llm-catalog/llmcat-azure-gpt", headers=h.headers())
        assert r.json()["requires_api_url"] is True

    def test_importing_an_entry_that_needs_one_without_it_is_refused(self, h):
        r = h.client.post("/v1/admin/llm-catalog/llmcat-azure-gpt/import", headers=h.headers())
        _assert_refused(r, "azure")
        r = h.client.post(
            "/v1/admin/llm-catalog/llmcat-azure-gpt/import", json={"api_url": " "}, headers=h.headers()
        )
        _assert_refused(r, "azure")
        assert _models(h) == []

    def test_importing_it_with_an_api_url_saves_the_row_with_it(self, h):
        r = h.client.post(
            "/v1/admin/llm-catalog/llmcat-azure-gpt/import",
            json={"api_url": "https://my-resource.openai.azure.com"},
            headers=h.headers(),
        )
        assert r.status_code == 201, r.text
        model = r.json()["model"]
        assert model["api_url"] == "https://my-resource.openai.azure.com"
        assert _models(h) == [("GPT on Azure", "https://my-resource.openai.azure.com")]

    def test_an_api_url_the_outbound_policy_refuses_is_refused_on_import(self, h):
        r = h.client.post(
            "/v1/admin/llm-catalog/llmcat-azure-gpt/import",
            json={"api_url": "http://169.254.169.254/latest"},
            headers=h.headers(),
        )
        assert r.status_code == 422, r.text
        assert _models(h) == []

    def test_an_entry_the_gateway_has_no_backend_for_is_not_imported(self, h):
        for body in (None, {"api_url": "https://europe-west4-aiplatform.googleapis.com"}):
            r = h.client.post("/v1/admin/llm-catalog/llmcat-vertex-embed/import", json=body, headers=h.headers())
            assert r.status_code == 422, r.text
            assert "unsupported_provider_api" in r.text, r.text
        assert _models(h) == []

    def test_a_cloud_entry_is_imported_switched_off_and_without_an_api_key(self, h):
        # The import carries no cloud credential, and the gateway refuses an
        # enabled cloud row without one (501 provider_credential_missing):
        # the row is created disabled, to be given its credential and
        # enabled on the model's own page.
        url = "/v1/admin/llm-catalog/llmcat-bedrock-nova/import"
        r = h.client.post(url, json={"enabled": True}, headers=h.headers())
        assert r.status_code == 422, r.text
        assert "provider_credential_missing" in r.json()["detail"], r.text
        r = h.client.post(url, json={"api_key": "AKIAEXAMPLE:secret"}, headers=h.headers())
        assert r.status_code == 422, r.text
        assert "api_key" in r.json()["detail"] and "AKIAEXAMPLE" not in r.text, r.text
        assert _models(h) == []
        r = h.client.post(url, headers=h.headers())
        assert r.status_code == 201, r.text
        assert r.json()["model"]["enabled"] is False
        with h.Factory() as s:
            (row,) = s.query(LLModel).all()
            assert row.enabled is False and row.api_key is None and row.provider_config is None

    def test_an_entry_of_a_known_provider_imports_without(self, h):
        r = h.client.post("/v1/admin/llm-catalog/llmcat-openai-gpt/import", headers=h.headers())
        assert r.status_code == 201, r.text


# --- the seed ---------------------------------------------------------------


def _seed():
    return get_default_llm_configurations()


def _needs(row) -> bool:
    return (
        destination.destination_problem(row["provider"], row["mode"], row["model_name"], row.get("api_url"))
        is not None
    )


def test_no_default_enabled_seed_row_is_without_a_destination():
    # These are the rows a new tenant gets as model rows.
    without = [(m["provider"], m["model_name"]) for m in _seed() if m["enabled"] and _needs(m)]
    assert without == []


# provider -> the number of catalog entries of it that are templates: no row
# of them can be dispatched until it is given the customer's own address.
SEED_ENTRIES_THAT_NEED_AN_API_URL = {
    "azure": 8,
    "azure_ai": 4,
    "databricks": 5,
    "lm_studio": 6,
    "nvidia_nim": 2,
    "ollama": 10,
    "sagemaker": 6,
    "vllm": 7,
}


def test_the_seed_entries_that_need_an_api_url_are_these():
    counted = {}
    for m in _seed():
        if destination.requires_api_url(m["provider"], m["mode"], m["model_name"]) and not (m.get("api_url") or "").strip():
            counted[m["provider"]] = counted.get(m["provider"], 0) + 1
    assert counted == SEED_ENTRIES_THAT_NEED_AN_API_URL


# The catalog entries of a cloud provider the gateway has no backend for:
# they cannot be imported, with an address or without. (Until the gateway's
# `cloud_backend` table they were counted as needing an `api_url`.)
SEED_CLOUD_ENTRIES_THE_GATEWAY_DOES_NOT_SERVE = {
    ("vertex_ai", "embedding"): 2,
}


def test_the_cloud_entries_of_the_seed_are_served_but_for_these():
    cloud = [m for m in _seed() if m["provider"] in destination.CLOUD_PROVIDERS]
    assert len([m for m in cloud if m["provider"] in destination.BEDROCK_PROVIDERS]) == 24
    refused = {}
    for m in cloud:
        if destination.unsupported_api_problem(m["provider"], m["mode"], m["model_name"]):
            key = (m["provider"], m["mode"])
            refused[key] = refused.get(key, 0) + 1
            assert not m["enabled"], m["model_name"]
    assert refused == SEED_CLOUD_ENTRIES_THE_GATEWAY_DOES_NOT_SERVE


def test_no_cloud_entry_of_the_seed_carries_a_credential_or_an_address():
    # The gateway reads a cloud row's credential from `provider_config` and
    # nowhere else (F811: the legacy shapes in `api_key` are not read), and
    # builds its URL itself. The catalog's cloud entries are templates: the
    # customer's own credential goes in when one is imported.
    cloud = [m for m in _seed() if m["provider"] in destination.CLOUD_PROVIDERS]
    assert len(cloud) == 35
    for m in cloud:
        assert not m.get("api_key") and not m.get("provider_config") and not m.get("api_url"), m["model_name"]
        assert not (m.get("model_metadata") or {}), m["model_name"]
        assert not m["enabled"], m["model_name"]
    assert {m["auth_type"] for m in cloud} == {"aws_iam", "gcp_service_account"}


def test_the_seed_does_not_claim_a_discovered_url_where_there_is_none():
    # `api_url_auto_discovered: True` on a row whose provider has no base
    # for its mode said the gateway would find the address. It will not.
    wrong = [
        (m["provider"], m["model_name"])
        for m in _seed()
        if m["api_url_auto_discovered"] and destination.requires_api_url(m["provider"], m["mode"], m["model_name"])
    ]
    assert wrong == []
