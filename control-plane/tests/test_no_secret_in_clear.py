# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
No credential is stored in clear.

A model's `api_key` was encrypted "if there is an encryption service", and
stored as it came where there was none; the search provider's key was stored
in clear when encrypting it failed. The answer is the one `provider_config`
has: 503, naming `ENCRYPTION_KEY`, and nothing saved. (The composed
application always has an encryption service: it refuses to start without
`ENCRYPTION_KEY`. The rule holds where a router is built without one.)
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from app.api.v1.endpoints.admin.llm_catalog import create_llm_catalog_router
from app.api.v1.endpoints.admin.llm_models import create_llm_models_router
from app.core.logging_system import BrutorLogger
from app.database.models.llm_catalog import LlmCatalog
from app.database.models.llm_model import LLModel
from app.database.models.llm_provider import LlmProvider
from app.services.llm.model.llm_models_manager import LLModelsManager
from tests.admin_endpoint_harness import build_admin_harness

KEY = "sk-clear-SENTINEL"
SEARCH_CONFIG = "/v1/admin/system-servers/search-config"


@pytest.fixture()
def h():
    log = BrutorLogger("no-secret-in-clear")
    manager = LLModelsManager(logger=log, encryption_service=None)
    harness = build_admin_harness(
        [
            create_llm_models_router(manager, log, encryption_service=None),
            create_llm_catalog_router(manager, log, encryption_service=None),
        ],
        logger_name="no-secret-in-clear",
    )

    def fn(s):
        s.add(LlmProvider(id="prov-openai", tenant_id="t1", key="openai", display_name="OpenAI"))
        s.add(LlmCatalog(id="llmcat-gpt", provider_key="openai", model_name="gpt-6-sol",
                         display_name="GPT", mode="chat", source="builtin"))

    harness.seed(fn)
    yield harness
    harness.close()


def _refused(r):
    assert r.status_code == 503, r.text
    detail = r.json()["detail"]
    assert "ENCRYPTION_KEY" in detail and "api_key" in detail, detail
    assert KEY not in r.text


def _keys(h):
    with h.Factory() as s:
        return [m.api_key for m in s.query(LLModel).all()]


BODY = {"name": "m", "provider_id": "prov-openai", "model_name": "gpt-5", "mode": "chat"}


def test_a_models_api_key_is_not_stored_in_clear_on_create(h):
    _refused(h.client.post("/v1/admin/llms", json={**BODY, "api_key": KEY}, headers=h.headers()))
    assert _keys(h) == []
    # A model without a key is saved.
    assert h.client.post("/v1/admin/llms", json=BODY, headers=h.headers()).status_code == 201


def test_nor_on_update(h):
    mid = h.client.post("/v1/admin/llms", json=BODY, headers=h.headers()).json()["model"]["id"]
    _refused(h.client.patch(f"/v1/admin/llms/{mid}", json={"api_key": KEY, "description": "d"}, headers=h.headers()))
    assert _keys(h) == [None]
    with h.Factory() as s:
        assert s.get(LLModel, mid).description is None
    # Clearing one needs nothing to encrypt with.
    assert h.client.patch(f"/v1/admin/llms/{mid}", json={"api_key": None}, headers=h.headers()).status_code == 200


def test_nor_on_a_catalog_import(h):
    _refused(h.client.post("/v1/admin/llm-catalog/llmcat-gpt/import", json={"api_key": KEY}, headers=h.headers()))
    assert _keys(h) == []
    assert h.client.post("/v1/admin/llm-catalog/llmcat-gpt/import", headers=h.headers()).status_code == 201


def test_the_search_providers_key_is_not_stored_in_clear_when_it_cannot_be_encrypted(monkeypatch):
    from app.api.v1.endpoints.admin import system_servers
    from app.database.models.search_provider_config import SearchProviderConfig

    class Broken:
        def __init__(self, *_, **__):
            raise ValueError("ENCRYPTION_KEY is not configured!")

    monkeypatch.setattr(system_servers, "EncryptionService", Broken)
    log = BrutorLogger("no-secret-in-clear")
    harness = build_admin_harness([system_servers.create_system_servers_admin_router(log)], logger_name="no-secret-in-clear")
    try:
        r = harness.client.put(
            SEARCH_CONFIG, json={"provider": "brave", "api_key": KEY},
            headers=harness.headers(),
        )
        assert r.status_code == 503, r.text
        assert "ENCRYPTION_KEY" in r.json()["detail"] and KEY not in r.text
        with harness.Factory() as s:
            assert s.query(SearchProviderConfig).count() == 0
    finally:
        harness.close()
