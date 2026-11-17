# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The control plane sends a model row's credential nowhere.

The gateway used to send a row with no `api_url`, of a provider it knew no
base for, to OpenAI. The control plane has its own outbound calls about a
model: the connection test, the health check and its background sweep. Each
of them asks the gateway (`BRUTOR_GATEWAY_INTERNAL_URL`) to probe the model by id or
name; none of them connects to a provider, and none carries the row's key.
Shown here with a recording transport, for a row with no destination, in
every mode and for both probes; and by reading the sources for a provider
host written as a default.
"""

from __future__ import annotations

import asyncio
import os
import re

import httpx
import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from app.constants.llm import LLModelMode
from app.core.logging_system import BrutorLogger
from app.database.models.llm_model import LLModel
from app.database.models.llm_provider import LlmProvider
from app.services.llm.model import llm_models_manager
from app.services.llm.model.llm_models_manager import PROBE_CATALOG, PROBE_ENDPOINT, LLModelsManager
from tests.source_trees import REPO

GATEWAY = "http://127.0.0.1:9"
ROW_KEY = "row-credential-that-must-stay-here"
# Modes the gateway has no route for: their probe sends nothing at all.
NOT_PROBED = {LLModelMode.RERANK.value, LLModelMode.MODERATION.value}
MODES = sorted(m.value for m in LLModelMode if m is not LLModelMode.ROUTING)


@pytest.fixture()
def sent(monkeypatch):
    """Every request the manager makes, answered 200 by a transport that
    connects to nothing."""
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": [{"embedding": [0.0]}], "choices": []})

    real = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(llm_models_manager.httpx, "AsyncClient", client)
    monkeypatch.setenv("BRUTOR_GATEWAY_INTERNAL_URL", GATEWAY)
    monkeypatch.setenv("BRUTOR_GATEWAY_SERVICE_API_KEY", "brk_platform_service_key")
    return requests


def _row(mode: str) -> LLModel:
    provider = LlmProvider(id="prov-x", tenant_id="t1", key="ollama", display_name="Ollama")
    return LLModel(
        id="llm-no-destination",
        tenant_id="t1",
        name="no destination",
        provider=provider,
        model_name="some-model",
        mode=mode,
        api_url=None,
        auth_type="api_key",
        api_key=ROW_KEY,
        enabled=True,
    )


@pytest.mark.parametrize("probe", [PROBE_ENDPOINT, PROBE_CATALOG])
@pytest.mark.parametrize("mode", MODES)
def test_a_probe_goes_to_the_gateway_and_carries_no_row_credential(sent, mode, probe):
    manager = LLModelsManager(logger=BrutorLogger("test-probe-destinations"), encryption_service=None)
    asyncio.run(manager._test_connection_via_proxy(_row(mode), timeout=1.0, db=None, probe=probe))
    if mode in NOT_PROBED:
        assert sent == []
        return
    assert sent, "the probe sent nothing: this test shows nothing"
    for request in sent:
        assert f"{request.url.scheme}://{request.url.host}:{request.url.port}" == GATEWAY, str(request.url)
        carried = str(request.url) + repr(request.headers.raw) + request.content.decode("utf-8", "replace")
        assert ROW_KEY not in carried, str(request.url)


# A provider's host written where a call is made. Each is that provider's
# own: the OpenAI Moderation guardrail and the OpenAI usage importer talk to
# OpenAI with a credential entered for OpenAI. The seed data states each
# provider's own URLs.
OPENAI_HOST_ALLOWED = {
    "app/database/init_llm_providers.py",
    "app/database/models/llm_provider.py",
    "app/api/v1/models/admin/llm_provider.py",
    "app/services/guardrails/provider_probe.py",
    "app/services/guardrails/guardrails_service.py",
    "enterprise/brutor_enterprise/traffic_import/services/traffic_import/adapters/openai.py",
}


def test_no_other_source_names_the_openai_host():
    found = set()
    for root in ("app", "enterprise/brutor_enterprise"):
        base = REPO / root
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            rel = path.relative_to(REPO).as_posix()
            if rel.startswith("app/database/provider_data/"):
                continue
            if re.search(r"api\.openai\.com", path.read_text(encoding="utf-8")):
                found.add(rel)
    assert found <= OPENAI_HOST_ALLOWED, sorted(found - OPENAI_HOST_ALLOWED)
