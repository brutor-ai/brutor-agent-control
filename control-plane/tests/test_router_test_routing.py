# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for `LLMRouterService.test_routing` — the routing-group
exerciser that the admin UI's "Test Router" tab calls.

Coverage:
  - Validates the routing group exists for the tenant (404
    rather than wasted proxy round-trip on a typo'd id).
  - Drives the proxy via a mocked httpx client; verifies the
    POST shape (model = group id, max_tokens, single user
    message).
  - On a 200, surfaces selected_model + response_text +
    latency.
  - On a non-2xx, surfaces the upstream error verbatim instead
    of swallowing — operators must see "your API key is
    expired" rather than a generic "Test failed".
  - On a network error, returns an `error` field with a
    readable message (no exception propagation).
  - Records each result in the per-tenant ring buffer; the
    history endpoint reads from the same buffer.
  - History bound: > 50 results per tenant truncate to the
    last 50 (most-recent first).
  - History tenant-isolated: tenant A's tests never leak into
    tenant B's history.
  - History group filter: surfaces only entries for the
    requested group id.

Run: pytest tests/test_router_test_routing.py -v
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "x")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import asyncio
import uuid
from typing import Optional

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.logging_system import BrutorLogger
from app.database import Base
from app.database.models.llm_model import LLModel
from app.database.models.llm_provider import LlmProvider
from app.database.models.llm_provider_catalog import LlmProviderCatalog
from app.database.models.llm_routing_group import LLMRoutingGroup
from app.database.models.tenant import Tenant
from app.services.llm.router.llm_router_service import LLMRouterService


TENANT_ID = "t-router-test"
GROUP_ID = "rg-test"

REQUIRED_TABLES = [
    Tenant.__table__,
    LlmProviderCatalog.__table__,
    LlmProvider.__table__,
    LLMRoutingGroup.__table__,
    LLModel.__table__,
]


@pytest.fixture()
def db() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine, tables=REQUIRED_TABLES)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = SessionLocal()
    try:
        s.add(
            Tenant(
                id=TENANT_ID,
                name=TENANT_ID,
                display_name="x",
                organization_name="x",
                primary_contact_email="x@x",
                is_active=True,
                is_default=False,
                is_trial=False,
                suspended=False,
                billing_plan="self_hosted",
            )
        )
        s.commit()
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture()
def service() -> LLMRouterService:
    return LLMRouterService(logger=BrutorLogger("test"))


def _seed_provider(db: Session, key: str = "openai") -> LlmProvider:
    p = LlmProvider(
        id=f"prov-{uuid.uuid4().hex[:8]}",
        tenant_id=TENANT_ID,
        key=key,
        display_name=key.title(),
        enabled=True,
        is_system=True,
    )
    db.add(p)
    db.flush()
    return p


def _seed_group(db: Session, group_id: str = GROUP_ID) -> LLMRoutingGroup:
    g = LLMRoutingGroup(
        id=group_id,
        tenant_id=TENANT_ID,
        name=f"Group {group_id}",
        routing_strategy="usage-based-routing-v2",
        enabled=True,
    )
    db.add(g)
    db.flush()
    return g


def _seed_member_model(
    db: Session,
    provider: LlmProvider,
    group: LLMRoutingGroup,
    model_name: str = "gpt-4o-mini",
) -> LLModel:
    m = LLModel(
        id=f"llm-{uuid.uuid4().hex[:8]}",
        tenant_id=TENANT_ID,
        name=model_name,
        provider_id=provider.id,
        model_name=model_name,
        mode="chat",
        routing_group_id=group.id,
        enabled=True,
        api_url_auto_discovered=True,
        auth_type="api_key",
        api_key="encrypted-noise",
    )
    db.add(m)
    db.flush()
    return m


# ────────────────────────────────────────────────────────────────────
# Validation
# ────────────────────────────────────────────────────────────────────

def test_unknown_group_raises_value_error(db, service):
    """A routing-group id that isn't on this tenant must raise
    ValueError before paying the proxy round-trip cost — the
    endpoint converts that to a 404 for the admin UI."""
    with pytest.raises(ValueError, match="not found"):
        asyncio.run(
            service.test_routing(
                db=db,
                tenant_id=TENANT_ID,
                message="hi",
                routing_group_id="rg-nonexistent",
            )
        )


def test_group_belonging_to_another_tenant_is_not_found(db, service):
    """Cross-tenant lookup attempts must NOT return another
    tenant's group. The query filters by tenant_id; this lock
    catches a regression that drops that filter and accidentally
    leaks routing config across tenants — a serious multi-
    tenancy bug."""
    other_tenant_id = "t-other"
    db.add(
        Tenant(
            id=other_tenant_id,
            name=other_tenant_id,
            display_name="other",
            organization_name="x",
            primary_contact_email="x@x",
            is_active=True,
            is_default=False,
            is_trial=False,
            suspended=False,
            billing_plan="self_hosted",
        )
    )
    g = LLMRoutingGroup(
        id="rg-other-tenant",
        tenant_id=other_tenant_id,
        name="other",
        routing_strategy="simple-shuffle",
        enabled=True,
    )
    db.add(g)
    db.commit()

    with pytest.raises(ValueError, match="not found"):
        asyncio.run(
            service.test_routing(
                db=db,
                tenant_id=TENANT_ID,
                message="hi",
                routing_group_id="rg-other-tenant",
            )
        )


# ────────────────────────────────────────────────────────────────────
# Proxy round-trip — happy path + error paths
# ────────────────────────────────────────────────────────────────────

def _patch_httpx(monkeypatch, handler):
    """Install a fake httpx.AsyncClient.post that calls
    `handler(url, json, headers) -> httpx.Response`."""
    captured: dict = {}

    class _FakeAsyncClient:
        def __init__(self, *_a, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return handler(url, json, headers)

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    return captured


def test_happy_path_calls_proxy_with_group_id_as_model(db, service, monkeypatch):
    """Sends `model = <routing_group.id>` to /v1/proxy/llm/chat/
    completions. The proxy resolves the routing group's virtual
    model (`rg-<group_id>`), applies strategy, and dispatches
    to a member model. We assert the wire contract here."""
    provider = _seed_provider(db)
    group = _seed_group(db)
    _seed_member_model(db, provider, group, model_name="gpt-4o-mini")
    db.commit()

    def handler(_url, _json, _headers):
        return httpx.Response(
            status_code=200,
            json={
                "id": "chat-1",
                # Proxy echoes the actual member model in
                # response.model — we read this for the UI's
                # "selected_model" field.
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Hello!",
                        }
                    }
                ],
            },
        )

    captured = _patch_httpx(monkeypatch, handler)

    result = asyncio.run(
        service.test_routing(
            db=db,
            tenant_id=TENANT_ID,
            message="ping",
            routing_group_id=group.id,
        )
    )

    # Wire contract — these must hold for the proxy to dispatch
    # via the routing-group path.
    assert captured["url"].endswith("/v1/proxy/llm/chat/completions")
    assert captured["json"]["model"] == group.id
    assert captured["json"]["messages"] == [{"role": "user", "content": "ping"}]
    assert captured["json"]["stream"] is False
    # Tenant context must propagate so the proxy resolves the
    # right tenant's catalog.
    assert captured["headers"]["X-Tenant-ID"] == TENANT_ID

    # Result shape — what the admin UI renders.
    assert result["selected_model"] == "gpt-4o-mini"
    assert result["selected_provider"] == "openai"
    assert result["response_text"] == "Hello!"
    assert result["status_code"] == 200
    assert result["error"] is None
    assert result["routing_group_id"] == group.id
    assert result["routing_group_name"] == f"Group {group.id}"
    assert result["latency_ms"] is not None and result["latency_ms"] >= 0


def test_upstream_error_surfaces_status_and_body(db, service, monkeypatch):
    """A 401 / 429 / 500 from the proxy must surface its body
    in `error` so operators see "your API key is expired" or
    similar. Swallowing the upstream message is the worst-case
    UX — the admin sees "Test failed" with no path forward."""
    provider = _seed_provider(db)
    group = _seed_group(db)
    _seed_member_model(db, provider, group)
    db.commit()

    def handler(_url, _json, _headers):
        return httpx.Response(
            status_code=401,
            text='{"error":{"message":"Invalid Authentication"}}',
        )

    _patch_httpx(monkeypatch, handler)

    result = asyncio.run(
        service.test_routing(
            db=db,
            tenant_id=TENANT_ID,
            message="hi",
            routing_group_id=group.id,
        )
    )

    assert result["status_code"] == 401
    assert result["error"] is not None and "401" in result["error"]
    assert "Invalid Authentication" in result["error"]
    # Selected_model is None when the proxy errored before
    # dispatching to a member.
    assert result["selected_model"] is None


def test_network_error_returns_error_not_exception(db, service, monkeypatch):
    """A connection refused / timeout must not propagate as a
    bare exception that 500s the endpoint — surface it as a
    well-formed result with `error` populated, so the admin UI
    can render the message instead of bouncing off a generic
    500. Same UX rule as the previous test."""
    provider = _seed_provider(db)
    group = _seed_group(db)
    _seed_member_model(db, provider, group)
    db.commit()

    def handler(_url, _json, _headers):
        raise httpx.ConnectError("connection refused")

    _patch_httpx(monkeypatch, handler)

    result = asyncio.run(
        service.test_routing(
            db=db,
            tenant_id=TENANT_ID,
            message="hi",
            routing_group_id=group.id,
        )
    )

    assert result["status_code"] is None
    assert result["error"] is not None
    assert "connection refused" in result["error"].lower() or \
           "could not reach proxy" in result["error"].lower()
    assert result["selected_model"] is None


# ────────────────────────────────────────────────────────────────────
# Test-history ring buffer
# ────────────────────────────────────────────────────────────────────

def test_history_records_each_test(db, service, monkeypatch):
    provider = _seed_provider(db)
    group = _seed_group(db)
    _seed_member_model(db, provider, group)
    db.commit()

    def handler(_url, _json, _headers):
        return httpx.Response(
            status_code=200,
            json={
                "model": "gpt-4o-mini",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            },
        )

    _patch_httpx(monkeypatch, handler)

    asyncio.run(service.test_routing(db=db, tenant_id=TENANT_ID, message="a", routing_group_id=group.id))
    asyncio.run(service.test_routing(db=db, tenant_id=TENANT_ID, message="b", routing_group_id=group.id))
    asyncio.run(service.test_routing(db=db, tenant_id=TENANT_ID, message="c", routing_group_id=group.id))

    history = service.get_test_history(tenant_id=TENANT_ID, limit=10)
    assert [e["message"] for e in history] == ["c", "b", "a"], (
        "Most-recent-first ordering matters for the UI: operators "
        "expect their last test at the top of the list."
    )


def test_history_bounded_per_tenant(db, service, monkeypatch):
    """Ring buffer must cap memory. We cap at
    `_TEST_HISTORY_MAX_PER_TENANT` (50). A long-running control
    plane under heavy admin testing would otherwise grow
    unbounded. We assert the cap in two ways: count, and that
    the oldest entries fall off (FIFO eviction)."""
    provider = _seed_provider(db)
    group = _seed_group(db)
    _seed_member_model(db, provider, group)
    db.commit()

    def handler(_url, _json, _headers):
        return httpx.Response(
            status_code=200,
            json={"model": "gpt-4o-mini", "choices": [{"message": {"content": ""}}]},
        )

    _patch_httpx(monkeypatch, handler)

    cap = service._TEST_HISTORY_MAX_PER_TENANT
    for i in range(cap + 5):
        asyncio.run(
            service.test_routing(
                db=db, tenant_id=TENANT_ID, message=f"msg-{i}", routing_group_id=group.id
            )
        )

    history = service.get_test_history(tenant_id=TENANT_ID, limit=cap + 100)
    assert len(history) == cap
    # The five oldest (msg-0..msg-4) must be gone; the newest
    # (msg-{cap+4}) must be at the top.
    messages = [e["message"] for e in history]
    assert messages[0] == f"msg-{cap + 4}"
    assert "msg-0" not in messages
    assert "msg-4" not in messages


def test_history_tenant_isolation(db, service, monkeypatch):
    """Tenant A's history must never leak into tenant B's
    list. Catastrophic multi-tenancy bug if it does — operators
    would see another customer's prompt content + chosen models."""
    other_tenant_id = "t-other"
    db.add(
        Tenant(
            id=other_tenant_id,
            name=other_tenant_id,
            display_name="other",
            organization_name="x",
            primary_contact_email="x@x",
            is_active=True,
            is_default=False,
            is_trial=False,
            suspended=False,
            billing_plan="self_hosted",
        )
    )
    provider_a = _seed_provider(db)
    group_a = _seed_group(db, group_id="rg-a")
    _seed_member_model(db, provider_a, group_a)
    # Provider+group on the OTHER tenant.
    p_b = LlmProvider(
        id=f"prov-{uuid.uuid4().hex[:8]}",
        tenant_id=other_tenant_id,
        key="openai",
        display_name="OpenAI",
        enabled=True,
        is_system=True,
    )
    db.add(p_b)
    g_b = LLMRoutingGroup(
        id="rg-b",
        tenant_id=other_tenant_id,
        name="b",
        routing_strategy="simple-shuffle",
        enabled=True,
    )
    db.add(g_b)
    db.add(
        LLModel(
            id=f"llm-{uuid.uuid4().hex[:8]}",
            tenant_id=other_tenant_id,
            name="gpt-4o-mini",
            provider_id=p_b.id,
            model_name="gpt-4o-mini",
            mode="chat",
            routing_group_id=g_b.id,
            enabled=True,
            api_url_auto_discovered=True,
            auth_type="api_key",
            api_key="encrypted",
        )
    )
    db.commit()

    def handler(_url, _json, _headers):
        return httpx.Response(
            status_code=200,
            json={"model": "gpt-4o-mini", "choices": [{"message": {"content": ""}}]},
        )

    _patch_httpx(monkeypatch, handler)

    asyncio.run(service.test_routing(db=db, tenant_id=TENANT_ID, message="from-a", routing_group_id="rg-a"))
    asyncio.run(service.test_routing(db=db, tenant_id=other_tenant_id, message="from-b", routing_group_id="rg-b"))

    a_history = service.get_test_history(tenant_id=TENANT_ID)
    b_history = service.get_test_history(tenant_id=other_tenant_id)

    assert [e["message"] for e in a_history] == ["from-a"]
    assert [e["message"] for e in b_history] == ["from-b"]


def test_history_group_filter(db, service, monkeypatch):
    """The Status tab calls history with a `routing_group_id`
    filter so each group's expansion shows only its own
    tests. Without the filter the operator would see
    cross-group activity in every expansion, which is
    confusing."""
    provider = _seed_provider(db)
    group_x = _seed_group(db, group_id="rg-x")
    group_y = _seed_group(db, group_id="rg-y")
    _seed_member_model(db, provider, group_x, model_name="gpt-4o-mini-x")
    _seed_member_model(db, provider, group_y, model_name="gpt-4o-mini-y")
    db.commit()

    def handler(_url, json, _headers):
        # Echo back the model the proxy "would" pick — for
        # this test we just emit the requested model id.
        return httpx.Response(
            status_code=200,
            json={
                "model": json["model"],
                "choices": [{"message": {"content": ""}}],
            },
        )

    _patch_httpx(monkeypatch, handler)

    asyncio.run(service.test_routing(db=db, tenant_id=TENANT_ID, message="hit-x", routing_group_id="rg-x"))
    asyncio.run(service.test_routing(db=db, tenant_id=TENANT_ID, message="hit-y", routing_group_id="rg-y"))
    asyncio.run(service.test_routing(db=db, tenant_id=TENANT_ID, message="hit-x-2", routing_group_id="rg-x"))

    x_only = service.get_test_history(tenant_id=TENANT_ID, routing_group_id="rg-x")
    y_only = service.get_test_history(tenant_id=TENANT_ID, routing_group_id="rg-y")

    assert [e["message"] for e in x_only] == ["hit-x-2", "hit-x"]
    assert [e["message"] for e in y_only] == ["hit-y"]


def test_history_limit_argument(db, service, monkeypatch):
    """`limit` argument trims the returned slice. The Status
    tab passes ~10 to keep the UI compact; misuse should clamp,
    not crash."""
    provider = _seed_provider(db)
    group = _seed_group(db)
    _seed_member_model(db, provider, group)
    db.commit()

    def handler(_url, _json, _headers):
        return httpx.Response(
            status_code=200,
            json={"model": "gpt-4o-mini", "choices": [{"message": {"content": ""}}]},
        )

    _patch_httpx(monkeypatch, handler)

    for i in range(7):
        asyncio.run(service.test_routing(db=db, tenant_id=TENANT_ID, message=f"m-{i}", routing_group_id=group.id))

    assert len(service.get_test_history(tenant_id=TENANT_ID, limit=3)) == 3
    assert len(service.get_test_history(tenant_id=TENANT_ID, limit=100)) == 7
    # Negative / zero — clamp to 0, don't blow up.
    assert service.get_test_history(tenant_id=TENANT_ID, limit=0) == []
    assert service.get_test_history(tenant_id=TENANT_ID, limit=-5) == []


def test_history_for_unknown_tenant_returns_empty(service):
    """A tenant that never tested anything must return [], not
    raise. Avoids a 500 on the very first GET /test-history."""
    assert service.get_test_history(tenant_id="t-never-tested") == []
