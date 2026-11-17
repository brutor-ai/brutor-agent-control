# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the routing-group `mode` contract:

  1. `LLMRouterService.assert_model_mode_matches_group` — the
     guard that the LLM-Models endpoints call before assigning
     a model to a routing group. Same-mode assignments pass;
     mismatched-mode assignments raise; detaching (None group)
     always passes; cross-tenant lookups raise "not found".

  2. `LLMRouterService.update_routing_group` mode-immutability
     while members exist — once a routing group has any
     non-virtual member, a mode change is rejected with an
     operator-actionable message that names the member count.
     Empty groups can change modes freely.

  3. `LLMRouterService.test_routing` per-mode endpoint dispatch
     — a chat-mode group POSTs to /v1/proxy/llm/chat/
     completions with `messages`; a responses-mode group POSTs
     to /v1/proxy/llm/responses with `input`; a completion-mode
     group POSTs to /v1/proxy/llm/completions with `prompt`;
     non-text modes (image / audio / video / embedding) return
     a structured error explaining where to test instead.

Run: pytest tests/test_router_mode_validation.py -v
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


TENANT_ID = "t-mode-validation"

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


def _seed_provider(db: Session, tenant_id: str = TENANT_ID, key: str = "openai") -> LlmProvider:
    p = LlmProvider(
        id=f"prov-{uuid.uuid4().hex[:8]}",
        tenant_id=tenant_id,
        key=key,
        display_name=key.title(),
        enabled=True,
        is_system=True,
    )
    db.add(p)
    db.flush()
    return p


def _seed_group(
    db: Session,
    group_id: str,
    mode: str = "chat",
    tenant_id: str = TENANT_ID,
) -> LLMRoutingGroup:
    g = LLMRoutingGroup(
        id=group_id,
        tenant_id=tenant_id,
        name=f"Group {group_id}",
        routing_strategy="usage-based-routing-v2",
        enabled=True,
        mode=mode,
    )
    db.add(g)
    db.flush()
    return g


def _seed_member_model(
    db: Session,
    provider: LlmProvider,
    group: LLMRoutingGroup,
    model_name: str = "gpt-4o-mini",
    mode: str = "chat",
    tenant_id: str = TENANT_ID,
) -> LLModel:
    m = LLModel(
        id=f"llm-{uuid.uuid4().hex[:8]}",
        tenant_id=tenant_id,
        name=model_name,
        provider_id=provider.id,
        model_name=model_name,
        mode=mode,
        routing_group_id=group.id,
        enabled=True,
        api_url_auto_discovered=True,
        auth_type="api_key",
        api_key="encrypted-noise",
    )
    db.add(m)
    db.flush()
    return m


def _patch_httpx(monkeypatch, handler):
    """Install a fake httpx.AsyncClient.post that calls
    `handler(url, json, headers) -> httpx.Response`. Captures
    the last call so the test can assert wire shape."""
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


# ────────────────────────────────────────────────────────────────────
# 1. assert_model_mode_matches_group
# ────────────────────────────────────────────────────────────────────

def test_mode_match_passes(db):
    """A chat model assigned to a chat-mode group is the happy
    path — the helper must return without raising."""
    _seed_provider(db)
    g = _seed_group(db, "rg-chat", mode="chat")
    db.commit()
    # No exception expected.
    LLMRouterService.assert_model_mode_matches_group(
        db=db, tenant_id=TENANT_ID, routing_group_id=g.id, model_mode="chat"
    )


def test_mode_mismatch_raises_with_actionable_message(db):
    """A responses-mode model assigned to a chat-mode group is
    the bug we're guarding against. The message must include
    both modes and the group id so an operator can fix it
    without reading the source."""
    _seed_provider(db)
    g = _seed_group(db, "rg-chat", mode="chat")
    db.commit()

    with pytest.raises(ValueError) as exc_info:
        LLMRouterService.assert_model_mode_matches_group(
            db=db,
            tenant_id=TENANT_ID,
            routing_group_id=g.id,
            model_mode="responses",
        )

    msg = str(exc_info.value)
    assert "responses" in msg
    assert "chat" in msg
    assert g.id in msg


def test_detach_passes_regardless_of_mode(db):
    """Setting routing_group_id=None means "standalone model"
    — no group to validate against, so the helper must no-op
    without touching the DB. We pass model_mode='moderation'
    deliberately to prove the early return doesn't reach the
    group lookup."""
    LLMRouterService.assert_model_mode_matches_group(
        db=db, tenant_id=TENANT_ID, routing_group_id=None, model_mode="moderation"
    )


def test_virtual_routing_group_model_is_exempt(db):
    """The control plane creates a synthetic LLModel per
    routing group (`id=rg-<group_id>`, `mode='routing'`) that
    acts as the dispatch entry-point. That model is
    self-referentially attached to its own group via
    `routing_group_id`, and its `mode='routing'` will always
    mismatch the group's real surface (chat / responses / …).

    Without an exemption, an admin editing *any* field on the
    virtual model (resource-group access, description, enabled
    flag) would see "Cannot assign a 'routing'-mode model to
    routing group X" because the form always re-submits the
    existing routing_group_id. The helper must return cleanly
    when `model_mode == 'routing'`."""
    g = _seed_group(db, "rg-self-ref", mode="chat")
    db.commit()

    # Same group id the virtual model carries, real model_mode
    # = 'routing' (the synthetic value). Must not raise.
    LLMRouterService.assert_model_mode_matches_group(
        db=db,
        tenant_id=TENANT_ID,
        routing_group_id=g.id,
        model_mode="routing",
    )


def test_empty_string_routing_group_id_is_treated_as_detach(db):
    """An admin form that clears the routing-group dropdown may
    serialize the cleared value as `""` instead of `null`. The
    helper must treat both as "detach" — anything else surfaces
    a misleading "routing group '' not found" 400 to the
    operator. Nothing in the catalog is keyed on empty string,
    so this is unambiguous."""
    # No group seeded — empty string must NOT trigger a lookup.
    LLMRouterService.assert_model_mode_matches_group(
        db=db, tenant_id=TENANT_ID, routing_group_id="", model_mode="responses"
    )


def test_unknown_group_raises_not_found(db):
    """If the group id doesn't exist for this tenant, the
    helper raises with "not found" — the LLM-models endpoint
    converts to 400. We don't trust the caller to validate the
    id first, because typos in the admin form would otherwise
    silently 200 and leave a dangling FK."""
    with pytest.raises(ValueError, match="not found"):
        LLMRouterService.assert_model_mode_matches_group(
            db=db,
            tenant_id=TENANT_ID,
            routing_group_id="rg-nonexistent",
            model_mode="chat",
        )


def test_cross_tenant_group_raises_not_found(db):
    """A group that exists for another tenant must be
    invisible — same multi-tenancy rule that test_routing
    enforces. Otherwise tenant A's admin could attach their
    model to tenant B's routing group via a guessable id."""
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
    _seed_group(db, "rg-other", mode="chat", tenant_id=other_tenant_id)
    db.commit()

    with pytest.raises(ValueError, match="not found"):
        LLMRouterService.assert_model_mode_matches_group(
            db=db,
            tenant_id=TENANT_ID,
            routing_group_id="rg-other",
            model_mode="chat",
        )


def test_none_model_mode_defaults_to_chat(db):
    """When `update_llm` is called without specifying mode (a
    PATCH that touches only `routing_group_id`), the helper
    receives `model_mode=None` and must default to 'chat' on
    that side — same back-compat default the rest of the
    codebase uses. So a NULL-mode patch against a chat group
    must pass; against a non-chat group it must raise."""
    _seed_provider(db)
    chat_group = _seed_group(db, "rg-chat-legacy", mode="chat")
    embedding_group = _seed_group(db, "rg-emb-legacy", mode="embedding")
    db.commit()

    # None on the model side → 'chat' default → matches the
    # chat group → no raise.
    LLMRouterService.assert_model_mode_matches_group(
        db=db, tenant_id=TENANT_ID, routing_group_id=chat_group.id, model_mode=None
    )

    # None on the model side → 'chat' default → mismatches an
    # embedding group → raises.
    with pytest.raises(ValueError, match="chat"):
        LLMRouterService.assert_model_mode_matches_group(
            db=db,
            tenant_id=TENANT_ID,
            routing_group_id=embedding_group.id,
            model_mode=None,
        )


# ────────────────────────────────────────────────────────────────────
# 2. update_routing_group mode-immutability
# ────────────────────────────────────────────────────────────────────

def test_mode_change_rejected_when_members_exist(db, service):
    """Once a member is attached, the group's mode is the
    contract the dispatch path relies on. Changing it
    mid-flight would mean existing members serve the wrong
    inbound surface — exactly the bug this column was added
    to prevent. The error must name the count so the operator
    knows how much detachment is needed."""
    provider = _seed_provider(db)
    g = _seed_group(db, "rg-locked", mode="chat")
    _seed_member_model(db, provider, g, mode="chat")
    db.commit()

    with pytest.raises(ValueError) as exc_info:
        asyncio.run(
            service.update_routing_group(
                db=db,
                tenant_id=TENANT_ID,
                group_id=g.id,
                update_data={"mode": "responses"},
            )
        )

    msg = str(exc_info.value)
    assert "Cannot change mode" in msg
    assert "1 member" in msg
    assert "chat" in msg and "responses" in msg


def test_mode_change_allowed_when_group_empty(db, service):
    """An empty routing group — typical right after creation —
    can change modes freely. No members means no dispatch
    contract to honor."""
    g = _seed_group(db, "rg-empty", mode="chat")
    db.commit()

    result = asyncio.run(
        service.update_routing_group(
            db=db,
            tenant_id=TENANT_ID,
            group_id=g.id,
            update_data={"mode": "embedding"},
        )
    )
    assert result["mode"] == "embedding"

    db.refresh(g)
    assert g.mode == "embedding"


def test_same_mode_passthrough_does_not_count_as_change(db, service):
    """Sending the same mode value (which the admin form does
    on every save, since the field is always populated) must
    NOT trip the immutability guard — that would render edits
    impossible once any member is attached."""
    provider = _seed_provider(db)
    g = _seed_group(db, "rg-noop", mode="chat")
    _seed_member_model(db, provider, g, mode="chat")
    db.commit()

    # Updating description while resending the same mode
    # mirrors the admin form's submit payload.
    result = asyncio.run(
        service.update_routing_group(
            db=db,
            tenant_id=TENANT_ID,
            group_id=g.id,
            update_data={"mode": "chat", "description": "renamed"},
        )
    )
    assert result["mode"] == "chat"
    assert result["description"] == "renamed"


# ────────────────────────────────────────────────────────────────────
# 3. Per-mode test_routing dispatch
# ────────────────────────────────────────────────────────────────────

def test_chat_mode_dispatches_to_chat_completions(db, service, monkeypatch):
    """A chat-mode routing group must POST to /v1/proxy/llm/
    chat/completions with the OpenAI chat shape (`messages`).
    This is the legacy default path — proving it still works
    after the per-mode dispatcher landed catches a regression
    that breaks all existing tenants."""
    provider = _seed_provider(db)
    g = _seed_group(db, "rg-chat", mode="chat")
    _seed_member_model(db, provider, g, mode="chat")
    db.commit()

    def handler(_url, _json, _headers):
        return httpx.Response(
            status_code=200,
            json={
                "model": "gpt-4o-mini",
                "choices": [{"message": {"role": "assistant", "content": "hi"}}],
            },
        )

    captured = _patch_httpx(monkeypatch, handler)

    result = asyncio.run(
        service.test_routing(
            db=db, tenant_id=TENANT_ID, message="ping", routing_group_id=g.id
        )
    )

    assert captured["url"].endswith("/v1/proxy/llm/chat/completions")
    assert captured["json"]["messages"] == [{"role": "user", "content": "ping"}]
    assert "input" not in captured["json"]
    assert "prompt" not in captured["json"]
    assert result["response_text"] == "hi"
    assert result["status_code"] == 200


def test_responses_mode_dispatches_to_responses_endpoint(db, service, monkeypatch):
    """A responses-mode group must hit /v1/proxy/llm/responses
    with the OpenAI Responses-API body shape (`input` as the
    short-form string). The body must NOT include `messages`
    or `prompt` — those would be silently dropped by some
    upstreams and rejected by others."""
    provider = _seed_provider(db)
    g = _seed_group(db, "rg-responses", mode="responses")
    _seed_member_model(db, provider, g, mode="responses", model_name="gpt-5.5")
    db.commit()

    def handler(_url, _json, _headers):
        return httpx.Response(
            status_code=200,
            json={"model": "gpt-5.5", "output_text": "structured ok"},
        )

    captured = _patch_httpx(monkeypatch, handler)

    result = asyncio.run(
        service.test_routing(
            db=db, tenant_id=TENANT_ID, message="ping", routing_group_id=g.id
        )
    )

    assert captured["url"].endswith("/v1/proxy/llm/responses")
    assert captured["json"]["input"] == "ping"
    assert captured["json"]["max_output_tokens"] == 64
    assert "messages" not in captured["json"]
    assert "prompt" not in captured["json"]
    assert "max_tokens" not in captured["json"]
    assert result["response_text"] == "structured ok"


def test_completion_mode_dispatches_to_completions_endpoint(db, service, monkeypatch):
    """A completion-mode group must hit /v1/proxy/llm/
    completions with the legacy OpenAI completion shape
    (`prompt`). Body must NOT include `messages` or
    `input`."""
    provider = _seed_provider(db)
    g = _seed_group(db, "rg-completion", mode="completion")
    _seed_member_model(db, provider, g, mode="completion", model_name="text-davinci-003")
    db.commit()

    def handler(_url, _json, _headers):
        return httpx.Response(
            status_code=200,
            json={
                "model": "text-davinci-003",
                "choices": [{"text": "legacy ok"}],
            },
        )

    captured = _patch_httpx(monkeypatch, handler)

    result = asyncio.run(
        service.test_routing(
            db=db, tenant_id=TENANT_ID, message="ping", routing_group_id=g.id
        )
    )

    assert captured["url"].endswith("/v1/proxy/llm/completions")
    assert captured["json"]["prompt"] == "ping"
    assert "messages" not in captured["json"]
    assert "input" not in captured["json"]
    assert result["response_text"] == "legacy ok"


@pytest.mark.parametrize(
    "mode",
    [
        "embedding",
        "image_generation",
        "audio_speech",
        "audio_transcription",
        "video_generation",
        "rerank",
        "moderation",
    ],
)
def test_non_text_modes_return_structured_error(db, service, monkeypatch, mode):
    """The Test Router covers chat / responses / completion;
    other modes (embedding, image, audio, video, rerank,
    moderation) take very different bodies that this surface
    intentionally does not synthesize. The contract: return a
    structured error with status_code=None and an `error`
    string that points the operator to the right place to
    test, instead of guessing the body shape and 400ing
    upstream."""
    provider = _seed_provider(db)
    g = _seed_group(db, f"rg-{mode}", mode=mode)
    _seed_member_model(db, provider, g, mode=mode, model_name=f"model-{mode}")
    db.commit()

    fired: dict = {"called": False}

    def handler(_url, _json, _headers):
        fired["called"] = True
        return httpx.Response(status_code=200, json={})

    _patch_httpx(monkeypatch, handler)

    result = asyncio.run(
        service.test_routing(
            db=db, tenant_id=TENANT_ID, message="ping", routing_group_id=g.id
        )
    )

    # No proxy round-trip — we short-circuit before posting.
    assert fired["called"] is False
    assert result["status_code"] is None
    assert result["selected_model"] is None
    assert result["error"] is not None
    assert mode in result["error"]
    # The error must direct the operator somewhere actionable.
    assert "LLM Models" in result["error"] or "Test Connection" in result["error"]


def test_unsupported_mode_result_lands_in_history(db, service, monkeypatch):
    """Even the structured-error path for an unsupported mode
    must record a history entry — the Status tab's expansion
    surfaces it so the operator can see *why* their last test
    didn't appear to do anything. Otherwise the Test Router
    feels broken."""
    provider = _seed_provider(db)
    g = _seed_group(db, "rg-img", mode="image_generation")
    _seed_member_model(db, provider, g, mode="image_generation", model_name="dall-e-3")
    db.commit()

    _patch_httpx(monkeypatch, lambda *_: httpx.Response(200, json={}))

    asyncio.run(
        service.test_routing(
            db=db, tenant_id=TENANT_ID, message="a cat", routing_group_id=g.id
        )
    )

    history = service.get_test_history(tenant_id=TENANT_ID, routing_group_id=g.id)
    assert len(history) == 1
    assert history[0]["error"] is not None
    assert "image_generation" in history[0]["error"]


# ────────────────────────────────────────────────────────────────────
# 4. _extract_response_text per surface
# ────────────────────────────────────────────────────────────────────

def test_extract_response_text_responses_structured_output():
    """The responses-API canonical shape — output[].content[].
    text — must yield a joined string. This is the common path
    when the upstream emits structured items (no convenience
    `output_text` field)."""
    body = {
        "output": [
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": "first chunk"},
                    {"type": "output_text", "text": "second chunk"},
                ],
            }
        ]
    }
    assert (
        LLMRouterService._extract_response_text("responses", body)
        == "first chunk\nsecond chunk"
    )


def test_extract_response_text_responses_convenience_field():
    """When the upstream populates `output_text`, prefer it —
    it's already the joined string we'd otherwise compute, and
    skipping the walk costs nothing."""
    body = {"output_text": "convenient ok"}
    assert (
        LLMRouterService._extract_response_text("responses", body)
        == "convenient ok"
    )


def test_extract_response_text_chat_message_content():
    """Chat completions: choices[0].message.content. The
    canonical happy path."""
    body = {"choices": [{"message": {"content": "answer"}}]}
    assert LLMRouterService._extract_response_text("chat", body) == "answer"


def test_extract_response_text_completion_text():
    """Legacy completions: choices[0].text."""
    body = {"choices": [{"text": "answer"}]}
    assert LLMRouterService._extract_response_text("completion", body) == "answer"


def test_extract_response_text_unknown_shape_returns_none():
    """Defensive: an unexpected shape collapses to None
    rather than raising. The status_code + selected_model
    fields still surface in the UI; the Response panel
    simply hides itself when the text is empty."""
    assert (
        LLMRouterService._extract_response_text("chat", {"unexpected": "shape"})
        is None
    )
    # Mode we don't have an extractor for — return None (the
    # structured-error path handles those before this method
    # is called, but the helper must still be safe).
    assert LLMRouterService._extract_response_text("image_generation", {}) is None
