# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the `models_api_url` column on the LLM provider
seed catalog.

This is the URL the proxy hits in the catalog-probe health check —
GET <models_api_url> with the model's API key. It verifies
auth + reachability + model-name presence on the provider side at
zero cost (most providers' /models endpoints are free).

The tests pin two contracts:

  1. **Major providers have a catalog API URL filled in.** Adding
     a new major provider without setting this field would silently
     drop those models back to the cheap-but-dishonest synthetic-400
     probe, undoing the work that motivated the column. The
     pinned-presence test catches that regression.

  2. **The URLs we ship are well-formed.** Every value is either a
     literal `https://…/models` URL or contains the
     `<MODEL_API_URL>` sentinel (used for self-hosted providers
     where the URL is per-model).

We don't hit the actual URLs here — that's a connectivity test, not
a unit test. Per-provider reachability is verified by the proxy's
catalog-probe handler at runtime.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from urllib.parse import urlparse

import pytest

from app.database.init_llm_providers import PROVIDER_SEED_CATALOG


# Provider keys that we expect to ship a catalog API URL with.
# Adding a new major provider without one is almost always a bug —
# the synthetic-400 probe will silently take over and the operator
# will see "Healthy" badges that don't actually verify the upstream.
EXPECTED_PROVIDERS_WITH_CATALOG_API = frozenset({
    "openai",
    "anthropic",
    "gemini",
    "mistral",
    "cohere",
    "xai",
    "deepseek",
    "moonshot",
    "groq",
    "together_ai",
    "fireworks_ai",
    "openrouter",
    "sambanova",
    "cerebras",
    "deepinfra",
    "ai21",
    "ollama",
    "vllm",
    "lm_studio",
    "elevenlabs",
    "replicate",
})


def _entry(key: str) -> dict:
    for e in PROVIDER_SEED_CATALOG:
        if e["key"] == key:
            return e
    raise AssertionError(
        f"Provider key {key!r} not present in PROVIDER_SEED_CATALOG"
    )


class TestCatalogApiUrlPresence:
    @pytest.mark.parametrize("provider_key", sorted(EXPECTED_PROVIDERS_WITH_CATALOG_API))
    def test_provider_has_catalog_api_url(self, provider_key: str):
        entry = _entry(provider_key)
        url = entry.get("models_api_url")
        assert url is not None and url.strip(), (
            f"Provider {provider_key!r} is expected to ship a "
            "`models_api_url` so the catalog-probe health "
            "check works without falling back to synthetic-400 "
            "probes. Either fill in the URL or, if the provider "
            "genuinely has no model-listing endpoint, remove this "
            "key from EXPECTED_PROVIDERS_WITH_CATALOG_API in "
            "test_provider_catalog_api_urls.py."
        )


class TestCatalogApiUrlShape:
    def test_every_url_is_well_formed(self):
        for entry in PROVIDER_SEED_CATALOG:
            url = entry.get("models_api_url")
            if url is None:
                continue
            # Templated URLs use a leading sentinel that's resolved
            # at probe time from the model row's api_url. Validate
            # the suffix only.
            if url.startswith("<MODEL_API_URL>"):
                tail = url[len("<MODEL_API_URL>"):]
                assert tail.startswith("/"), (
                    f"{entry['key']}: templated catalog URL must "
                    f"continue with a path segment after the "
                    f"<MODEL_API_URL> sentinel; got {url!r}"
                )
                continue
            parsed = urlparse(url)
            assert parsed.scheme in {"http", "https"}, (
                f"{entry['key']}: catalog URL must be HTTP(S); got {url!r}"
            )
            assert parsed.netloc, (
                f"{entry['key']}: catalog URL missing host; got {url!r}"
            )
            assert "/models" in parsed.path or "/v1beta/models" in parsed.path, (
                f"{entry['key']}: catalog URL is expected to point at "
                f"a /models listing; got {url!r}. If the provider's "
                "convention differs, update this test."
            )


class TestKnownExceptions:
    """Providers we deliberately ship NULL on, with a documented
    reason. Pinning these keeps a future contributor from
    'helpfully' adding a fabricated URL."""

    NULL_BY_DESIGN = frozenset({
        "fal_ai",        # Per-model URLs; no unified catalog.
        "stability_ai",  # /v2beta/engines/list semantics differ.
        "perplexity",    # No public model-listing endpoint.
        "voyage",        # No public model-listing endpoint.
        "jina_ai",       # Conservative — endpoint shape uncertain.
        "deepgram",      # No simple /models — uses /projects.
        "assemblyai",    # No public /models.
        "google",        # GCP TTS/STT — service-account-only.
        "azure",         # Per-deployment URL pattern.
        "azure_ai",      # Same.
        "bedrock",       # AWS SigV4 + IAM, no /models endpoint.
        "bedrock_converse",
        "minimax",       # Endpoint shape uncertain.
        "vertex_ai",     # GCP service account flow; complex.
        "nvidia_nim",    # On-prem; per-deployment.
        "databricks",    # Per-workspace URLs.
        "huggingface",   # Per-endpoint deployments.
        "sagemaker",     # AWS IAM; per-endpoint.
        "openai_compatible",  # Generic fallthrough.
        "routing_group",      # Internal virtual provider.
    })

    @pytest.mark.parametrize("provider_key", sorted(NULL_BY_DESIGN))
    def test_provider_has_no_catalog_api_url(self, provider_key: str):
        entry = _entry(provider_key)
        assert entry.get("models_api_url") is None, (
            f"{provider_key!r} is listed as NULL-by-design — if you've "
            "verified an endpoint, also remove it from the "
            "NULL_BY_DESIGN set in this test."
        )


class TestSeedDataCoherence:
    def test_no_duplicate_keys(self):
        # Belt-and-braces: a duplicate key in the seed list would
        # cause the second insert to silently win, with surprising
        # `models_api_url` semantics.
        keys = [e["key"] for e in PROVIDER_SEED_CATALOG]
        assert len(keys) == len(set(keys)), (
            "Duplicate keys in PROVIDER_SEED_CATALOG"
        )

    def test_every_provider_is_either_with_or_without_catalog(self):
        # Pin that every provider is either in the "should have" set
        # or the "by design NULL" set — no orphans. Adding a new
        # provider without classifying it would silently fall
        # through to synthetic-400 probes.
        all_keys = {e["key"] for e in PROVIDER_SEED_CATALOG}
        classified = (
            EXPECTED_PROVIDERS_WITH_CATALOG_API
            | TestKnownExceptions.NULL_BY_DESIGN
        )
        unclassified = all_keys - classified
        assert not unclassified, (
            f"New provider(s) {sorted(unclassified)} not classified. "
            "Add to EXPECTED_PROVIDERS_WITH_CATALOG_API or "
            "TestKnownExceptions.NULL_BY_DESIGN."
        )
