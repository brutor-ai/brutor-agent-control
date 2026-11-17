# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Defensive seed-default tests for data residency

Pins two seed-layer invariants documented in the a069_data_residency migration:

  1. Tier-1 global SaaS providers (OpenAI / Anthropic / Gemini /
     Mistral / Groq / …) carry `region="global"` so a tenant with
     `data_residency.block_global=true` filters them out without
     the operator having to set region per-provider.

  2. BYOC providers (Azure / AWS Bedrock / Vertex / vLLM / Ollama /
     self-hosted) OMIT `region` from the catalogue. Their region is
     operator-picked at config time; stamping a default would be a
     lie and would interact incorrectly with block_global.

  3. The Tenant model's `data_residency` column carries no default
     (Python or server side). NULL is the documented "no
     enforcement" sentinel — a default would silently switch
     enforcement on for every fresh tenant.

A drift here is silent: a future PR that adds `"region": "us-east-1"`
to the Azure entry, or a Tenant constructor default like
`data_residency={"block_global": True}`, would not break any
runtime test — but would change platform semantics for every new
install. These assertions surface that drift at PR time.

No live DB needed — both invariants live in metadata.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test_brutor")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")


from app.database.init_llm_providers import PROVIDER_SEED_CATALOG
from app.database.models.tenant import Tenant


# Tier-1 SaaS APIs that are honestly globally distributed (anycast,
# no per-region operator pick). Keep this list narrow — when a
# provider's regional posture is ambiguous, leave it off and let
# operators stamp region via the admin UI.
EXPECTED_GLOBAL_KEYS = {
    "openai", "anthropic", "gemini", "mistral", "cohere", "xai",
    "deepseek", "moonshot", "minimax", "groq", "together_ai",
    "fireworks_ai", "perplexity", "openrouter", "sambanova",
    "cerebras", "ai21", "deepinfra", "voyage", "jina_ai",
    "elevenlabs", "deepgram", "assemblyai", "stability_ai",
    "fal_ai", "replicate", "huggingface",
}

# BYOC providers whose region is operator-picked at provider config
# time (Azure subscription, AWS account region, GCP project region,
# self-hosted location). Stamping a default would lie about where
# the upstream lives.
EXPECTED_BYOC_KEYS = {
    "azure", "azure_ai", "bedrock", "bedrock_converse", "vertex_ai",
    "sagemaker", "databricks", "vllm", "ollama", "lm_studio",
    "nvidia_nim", "google", "openai_compatible", "routing_group",
}


class TestProviderCatalogRegionSplit:
    def test_known_global_providers_stamped_global(self):
        """Every Tier-1 SaaS provider on the well-known list must
        carry `region="global"` so the runtime + admin UI agree
        with the upstream's actual posture."""
        catalog_by_key = {e["key"]: e for e in PROVIDER_SEED_CATALOG}
        missing = []
        wrong = []
        for key in EXPECTED_GLOBAL_KEYS:
            entry = catalog_by_key.get(key)
            assert entry is not None, (
                f"expected {key!r} in PROVIDER_SEED_CATALOG"
            )
            region = entry.get("region")
            if region is None:
                missing.append(key)
            elif region != "global":
                wrong.append((key, region))
        assert not missing, (
            f"Tier-1 global providers missing region='global': {missing}. "
            f"If one of these was reclassified as regional, update both "
            f"EXPECTED_GLOBAL_KEYS here and the catalogue header comment."
        )
        assert not wrong, (
            f"Tier-1 providers with non-global region stamp: {wrong}"
        )

    def test_byoc_providers_omit_region(self):
        """BYOC entries (Azure / Bedrock / Vertex / self-hosted)
        must NOT pin a region in the catalogue. Their region lives
        on each operator's provider config (per-tenant), not on the
        seed catalogue."""
        catalog_by_key = {e["key"]: e for e in PROVIDER_SEED_CATALOG}
        offenders = []
        for key in EXPECTED_BYOC_KEYS:
            entry = catalog_by_key.get(key)
            if entry is None:
                continue
            if "region" in entry:
                offenders.append((key, entry["region"]))
        assert not offenders, (
            f"BYOC providers must NOT carry a default region — found "
            f"{offenders}. These regions are operator-picked at config "
            f"time; a hardcoded default lies about where the upstream "
            f"actually lives and breaks `block_global` filtering."
        )

    def test_every_catalogue_entry_is_classified(self):
        """Sanity check: every provider in PROVIDER_SEED_CATALOG
        belongs to either the global or the BYOC bucket. A new
        catalogue entry must come with a deliberate classification
        — failing this test surfaces the gap."""
        classified = EXPECTED_GLOBAL_KEYS | EXPECTED_BYOC_KEYS
        unclassified = [
            e["key"] for e in PROVIDER_SEED_CATALOG if e["key"] not in classified
        ]
        assert not unclassified, (
            f"New providers in PROVIDER_SEED_CATALOG without residency "
            f"classification: {unclassified}. Add each to either "
            f"EXPECTED_GLOBAL_KEYS (honestly anycast) or "
            f"EXPECTED_BYOC_KEYS (operator-picked region) before merging."
        )


class TestTenantResidencyColumnDefault:
    def test_data_residency_is_nullable_with_no_default(self):
        """NULL = "no enforcement" is the documented sentinel. A
        SQLAlchemy default or server_default would silently turn
        enforcement on for every fresh tenant — invisible to the
        operator until they get a 403 on a model call.

        Pin this at the column-metadata level so a refactor that
        moves the default into Python-side __init__ logic, or into
        a server-side trigger, still trips the assertion."""
        col = Tenant.__table__.c.data_residency
        assert col.nullable is True, (
            "data_residency must remain nullable — NULL is the "
            "documented 'no enforcement' sentinel"
        )
        assert col.default is None, (
            f"data_residency must have NO Python-side default; "
            f"got {col.default!r}. A default would silently switch "
            f"residency enforcement on for every fresh tenant."
        )
        assert col.server_default is None, (
            f"data_residency must have NO server-side default; "
            f"got {col.server_default!r}. Same reasoning — defaults "
            f"shift the 'NULL = open' invariant."
        )
