# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
A row the gateway would drop as a routing-group member or a fallback is
refused as one when it is saved.

The gateway serves a Claude or Gemini row on Amazon Bedrock or Vertex AI
when a request names it, and has no backend for it on the chat forwarder,
where a member of a routing group and a chat fallback are sent
(`providers::cloud_backend`: `ChatForward` is served for the Converse
families only): such a candidate is skipped on every request, with a log
line. On the embeddings route a fallback is sent on the embeddings API,
where the Bedrock embedding rows are served. The rule is read off
`app/services/llm/destination.py`'s copy of `cloud_backend`.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from app.api.v1.endpoints.admin.llm_models import create_llm_models_router
from app.api.v1.endpoints.admin.llm_routing_groups import create_llm_router_admin_router
from app.core.logging_system import BrutorLogger
from app.database.models.llm_model import LLModel
from app.database.models.llm_provider import LlmProvider
from app.database.models.llm_routing_group import LLMRoutingGroup
from app.services.llm import destination as d
from app.services.llm import forward_candidates as fc
from app.services.llm.model.llm_models_manager import LLModelsManager
from app.services.llm.router.llm_router_service import LLMRouterService
from tests.admin_endpoint_harness import build_admin_harness
from tests.core_repo import core_revision, core_source

CLAUDE_BEDROCK = "anthropic.claude-sonnet-5-5-v1:0"
NOVA = "amazon.nova-pro-v1:0"
TITAN = "amazon.titan-embed-text-v2:0"

# id -> (provider, model_name, mode)
ROWS = {
    "llm-claude-bedrock": ("bedrock", CLAUDE_BEDROCK, "chat"),
    "llm-nova": ("bedrock_converse", NOVA, "chat"),
    "llm-titan": ("bedrock", TITAN, "embedding"),
    "llm-claude-vertex": ("vertex_ai", "claude-sonnet-5-5", "chat"),
    "llm-gemini-vertex": ("vertex", "gemini-2.5-pro", "chat"),
    "llm-gpt": ("openai", "gpt-5", "chat"),
    "llm-embed": ("openai", "text-embedding-3-small", "embedding"),
}
DROPPED = ("llm-claude-bedrock", "llm-claude-vertex", "llm-gemini-vertex")
FORWARDED = ("llm-nova", "llm-gpt")


@pytest.fixture()
def h():
    log = BrutorLogger("forward-candidates")
    manager = LLModelsManager(logger=log, encryption_service=None)
    harness = build_admin_harness(
        [
            create_llm_models_router(manager, log, encryption_service=None),
            create_llm_router_admin_router(LLMRouterService(logger=log), log),
        ],
        logger_name="forward-candidates",
    )

    def fn(s):
        for key in ("bedrock", "bedrock_converse", "vertex_ai", "vertex", "openai"):
            s.add(LlmProvider(id=f"prov-{key}", tenant_id="t1", key=key, display_name=key))
        s.flush()
        # Rows that are switched off (they carry no credential here): what
        # they may be a member or a fallback of does not depend on it.
        for mid, (provider, model_name, mode) in ROWS.items():
            s.add(LLModel(id=mid, tenant_id="t1", provider_id=f"prov-{provider}", name=mid,
                          model_name=model_name, mode=mode, enabled=provider == "openai"))
        s.add(LLMRoutingGroup(id="rg-chat", tenant_id="t1", name="chat group", mode="chat"))
        s.add(LLMRoutingGroup(id="rg-embed", tenant_id="t1", name="embed group", mode="embedding"))

    harness.seed(fn)
    yield harness
    harness.close()


def _patch(h, mid, **body):
    return h.client.patch(f"/v1/admin/llms/{mid}", json=body, headers=h.headers())


def _refused(r):
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, str) and "unsupported_provider_api" in detail, detail
    return detail


def _get(h, cls, key, field):
    with h.Factory() as s:
        return getattr(s.get(cls, key), field)


# --- members ------------------------------------------------------------------


@pytest.mark.parametrize("mid", DROPPED)
def test_a_cloud_row_the_forwarder_has_no_backend_for_does_not_join_a_group(h, mid):
    detail = _refused(_patch(h, mid, routing_group_id="rg-chat"))
    assert "member of a routing group" in detail and ROWS[mid][1] in detail
    assert _get(h, LLModel, mid, "routing_group_id") is None


@pytest.mark.parametrize("mid, group", [("llm-nova", "rg-chat"), ("llm-gpt", "rg-chat"), ("llm-titan", "rg-embed")])
def test_a_row_the_forwarder_serves_joins(h, mid, group):
    r = _patch(h, mid, routing_group_id=group)
    assert r.status_code == 200, r.text
    assert _get(h, LLModel, mid, "routing_group_id") == group


def test_it_is_not_created_into_a_group(h):
    body = {"name": "new", "provider_id": "prov-bedrock", "model_name": CLAUDE_BEDROCK, "mode": "chat",
            "enabled": False, "routing_group_id": "rg-chat"}
    _refused(h.client.post("/v1/admin/llms", json=body, headers=h.headers()))
    r = h.client.post("/v1/admin/llms", json={**body, "model_name": NOVA}, headers=h.headers())
    assert r.status_code == 201, r.text


def test_a_member_does_not_become_such_a_row(h):
    assert _patch(h, "llm-nova", routing_group_id="rg-chat").status_code == 200
    _refused(_patch(h, "llm-nova", model_name=CLAUDE_BEDROCK))
    _refused(_patch(h, "llm-nova", provider_id="prov-vertex_ai", model_name="gemini-2.5-pro"))
    assert _get(h, LLModel, "llm-nova", "model_name") == NOVA
    # Leaving the group in the same update, it may.
    r = _patch(h, "llm-nova", model_name=CLAUDE_BEDROCK, routing_group_id=None)
    assert r.status_code == 200, r.text


# --- a model's own chain ------------------------------------------------------


@pytest.mark.parametrize("mid", DROPPED)
@pytest.mark.parametrize("by", ["id", "model_name"])
def test_a_chat_models_chain_does_not_name_a_row_the_forwarder_drops(h, mid, by):
    entry = mid if by == "id" else ROWS[mid][1]
    detail = _refused(_patch(h, "llm-gpt", fallback_models=["llm-nova", entry]))
    assert detail.startswith("fallback_models: ") and "a fallback" in detail
    assert _get(h, LLModel, "llm-gpt", "fallback_models") is None


def test_a_chain_of_rows_the_forwarder_serves_is_saved(h):
    chain = ["llm-nova", "rg-chat", "chat group", "names-nothing"]
    r = _patch(h, "llm-gpt", fallback_models=chain)
    assert r.status_code == 200, r.text
    assert _get(h, LLModel, "llm-gpt", "fallback_models") == chain
    # A Claude row on Bedrock is served when it is the model named: it may
    # have a chain, it may not be in one.
    r = _patch(h, "llm-claude-bedrock", fallback_models=["llm-gpt"])
    assert r.status_code == 200, r.text


def test_an_embedding_chain_is_walked_on_the_embeddings_api(h):
    r = _patch(h, "llm-embed", fallback_models=["llm-titan"])
    assert r.status_code == 200, r.text
    # A chat row of a cloud is not served there, Converse or not.
    _refused(_patch(h, "llm-embed", fallback_models=["llm-nova"]))
    # And an embedding row is not served by the chat forwarder.
    _refused(_patch(h, "llm-gpt", fallback_models=["llm-titan"]))


def test_a_chain_is_checked_on_create(h):
    body = {"name": "new", "provider_id": "prov-openai", "model_name": "gpt-5-mini", "mode": "chat"}
    _refused(h.client.post("/v1/admin/llms", json={**body, "fallback_models": ["llm-claude-vertex"]}, headers=h.headers()))
    r = h.client.post("/v1/admin/llms", json={**body, "fallback_models": ["llm-nova"]}, headers=h.headers())
    assert r.status_code == 201, r.text


# --- a group's lists ----------------------------------------------------------

LISTS = ("fallback_models", "context_window_fallbacks", "content_policy_fallbacks", "default_fallbacks")


@pytest.mark.parametrize("field", LISTS)
def test_a_groups_list_does_not_name_a_row_the_forwarder_drops(h, field):
    url = "/v1/admin/llm-router/routing-groups"
    for mid in DROPPED:
        r = h.client.post(url, json={"id": "new", "name": "new", "mode": "chat", field: [mid]}, headers=h.headers())
        detail = _refused(r)
        assert detail.startswith(f"{field}: "), detail
        assert _get(h, LLMRoutingGroup, "rg-chat", field) is None
        _refused(h.client.patch(f"{url}/rg-chat", json={field: ["llm-gpt", ROWS[mid][1]]}, headers=h.headers()))
    with h.Factory() as s:
        assert s.get(LLMRoutingGroup, "new") is None
    r = h.client.patch(f"{url}/rg-chat", json={field: ["llm-nova", "rg-embed", "llm-gpt"]}, headers=h.headers())
    assert r.status_code == 200, r.text
    assert _get(h, LLMRoutingGroup, "rg-chat", field) == ["llm-nova", "rg-embed", "llm-gpt"]
    r = h.client.post(url, json={"id": "new", "name": "new", "mode": "chat", field: ["llm-nova"]}, headers=h.headers())
    assert r.status_code in (200, 201), r.text


# --- the rule ------------------------------------------------------------------


def test_the_rule_is_read_off_the_copy_of_cloud_backend():
    for provider, name, mode in ROWS.values():
        api = {"chat": d.API_CHAT_FORWARD, "embedding": d.API_EMBEDDINGS}[mode]
        cloud = provider in d.CLOUD_PROVIDERS
        expected = None if (not cloud or d.cloud_backend(provider, name, api)) else "refused"
        got = d.forward_problem(provider, name, mode, fc.FALLBACK)
        assert (got and "refused") == expected, (provider, name)
    # A cloud row in a mode with no forwarding API of its own is never one.
    assert d.forward_problem("bedrock", NOVA, "image_generation", fc.MEMBER)
    assert d.forward_problem("openai", "gpt-image-1", "image_generation", fc.MEMBER) is None


@pytest.mark.parametrize(
    "provider, name, served",
    [
        ("bedrock", CLAUDE_BEDROCK, {d.API_CHAT, d.API_MESSAGES}),
        ("bedrock_converse", "us.anthropic.claude-opus-5-v1:0", {d.API_CHAT, d.API_MESSAGES}),
        ("bedrock", NOVA, {d.API_CHAT, d.API_CHAT_FORWARD}),
        ("bedrock", "eu.mistral.pixtral-large-2502-v1:0", {d.API_CHAT, d.API_CHAT_FORWARD}),
        ("bedrock", TITAN, {d.API_EMBEDDINGS}),
        ("bedrock", "cohere.embed-multilingual-v3", {d.API_EMBEDDINGS}),
        ("bedrock", "meta.llama4-70b-instruct-v1:0", set()),
        ("bedrock", "amazon.nova-canvas-v1:0", set()),
        ("vertex_ai", "claude-sonnet-5-5", {d.API_CHAT, d.API_MESSAGES}),
        ("vertex", "gemini-2.5-pro", {d.API_CHAT, d.API_GEMINI}),
        ("vertex_ai", "text-bison", {d.API_GEMINI}),
        ("openai", "gpt-5", set()),
    ],
)
def test_the_copy_of_cloud_backend(provider, name, served):
    apis = (d.API_CHAT, d.API_CHAT_FORWARD, d.API_EMBEDDINGS, d.API_MESSAGES, d.API_GEMINI, d.API_OTHER)
    assert {api for api in apis if d.cloud_backend(provider, name, api)} == served


def test_the_candidates_are_sent_where_the_gateway_sends_them():
    read = f" [the gateway's source read: {core_revision()}]"
    router = core_source("services", "llm_router.rs").read_text(encoding="utf-8")
    # A member and a chat fallback: the chat forwarder's check.
    assert router.count("providers::dispatch_refusal(model, providers::ProviderApi::ChatForward)") >= 2, read
    walk = router[router.index("async fn try_fallback_list("):router.index("// Model-level fallback for NON-chat modes")]
    assert walk.count("forward_chat_completion(client, &fb") == 2, "try_fallback_list changed" + read
    # An entry: a routing group by id or name, then a model by name or id.
    assert '"SELECT * FROM llm_routing_group WHERE (id = $1 OR name = $1) AND tenant_id = $2 AND enabled = true"' in walk, read
    # The model must also be one the gateway serves (enabled, not retired,
    # its provider enabled): the gateway's own availability predicate.
    assert "WHERE (model_name = $1 OR id = $1) AND tenant_id = $2 AND \"," in walk, read
    assert 'crate::model_available_sql!("")' in walk, read
    chain = router[router.index("pub async fn resolve_fallback_chain("):router.index("pub async fn send_first_success<F>(")]
    assert "Ok(m) => match providers::dispatch_refusal(&m, api) {" in chain, read
    llm = core_source("routes", "llm.rs").read_text(encoding="utf-8")
    call = llm[llm.index("resolve_fallback_chain("):][:600]
    assert "providers::ProviderApi::Embeddings," in call, "the embeddings chain is walked on another API" + read


# --- a model the gateway does not serve ---------------------------------------
#
# The gateway's one availability rule (`model_available_sql!`): a retired
# model, and a model of a disabled provider, is never called, as a fallback
# or a member either. Naming one where it would be a candidate is refused
# when it is saved. (A row that is merely switched off is not refused: it is
# switched on again without touching what names it.)

from datetime import date, timedelta  # noqa: E402

# The day the availability rule reads as today, pinned. The rule is the
# gateway's, in UTC (`availability._today`); a retire date built from the
# host's local `date.today()` named the wrong day for the hours the local
# date and the UTC date differ (from local midnight east of UTC, until UTC
# midnight), and these tests failed then. Every date here is this one's.
TODAY = date(2026, 6, 15)


@pytest.fixture()
def utc_today(monkeypatch):
    from app.services.llm import availability

    monkeypatch.setattr(availability, "_today", lambda: TODAY)
    return TODAY


def _not_served(r, *words):
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, str) and "does not serve" in detail, detail
    for word in words:
        assert word in detail, detail
    return detail


def _retire(h, mid, when):
    def fn(s):
        s.get(LLModel, mid).retire_date = when

    h.seed(fn)


def _provider(h, key, enabled):
    def fn(s):
        s.get(LlmProvider, f"prov-{key}").enabled = enabled

    h.seed(fn)


@pytest.mark.parametrize("days", [0, -1, -400])
def test_a_retired_model_is_not_named_as_a_fallback(h, utc_today, days):
    _retire(h, "llm-nova", utc_today + timedelta(days=days))
    detail = _not_served(_patch(h, "llm-gpt", fallback_models=["llm-nova"]), "fallback_models", "retire")
    assert NOVA in detail or "llm-nova" in detail
    assert _get(h, LLModel, "llm-gpt", "fallback_models") is None


def test_a_model_that_retires_later_is_a_fallback_until_then(h, utc_today):
    _retire(h, "llm-nova", utc_today + timedelta(days=1))
    assert _patch(h, "llm-gpt", fallback_models=["llm-nova"]).status_code == 200


def test_a_model_of_a_disabled_provider_is_not_named_as_a_fallback(h):
    _provider(h, "bedrock_converse", False)
    _not_served(_patch(h, "llm-gpt", fallback_models=[NOVA]), "fallback_models", "provider")
    _provider(h, "bedrock_converse", True)
    assert _patch(h, "llm-gpt", fallback_models=[NOVA]).status_code == 200


@pytest.mark.parametrize("field", LISTS)
def test_a_groups_list_does_not_name_a_model_the_gateway_does_not_serve(h, utc_today, field):
    _retire(h, "llm-nova", utc_today)
    url = "/v1/admin/llm-router/routing-groups"
    detail = _not_served(h.client.patch(f"{url}/rg-chat", json={field: ["llm-gpt", "llm-nova"]}, headers=h.headers()))
    assert detail.startswith(f"{field}: ")
    assert _get(h, LLMRoutingGroup, "rg-chat", field) is None


def test_a_model_the_gateway_does_not_serve_does_not_join_a_group(h, utc_today):
    _retire(h, "llm-nova", utc_today)
    _not_served(_patch(h, "llm-nova", routing_group_id="rg-chat"), "routing group", "retire")
    assert _get(h, LLModel, "llm-nova", "routing_group_id") is None

    _provider(h, "openai", False)
    _not_served(_patch(h, "llm-gpt", routing_group_id="rg-chat"), "provider")
    _provider(h, "openai", True)
    assert _patch(h, "llm-gpt", routing_group_id="rg-chat").status_code == 200


def test_a_row_that_is_switched_off_is_still_named(h):
    with h.Factory() as s:
        assert s.get(LLModel, "llm-nova").enabled is False
    assert _patch(h, "llm-gpt", fallback_models=["llm-nova"]).status_code == 200


def test_the_availability_rule_is_the_gateways():
    """The rule this module mirrors, read from the gateway at its committed
    HEAD: the predicate every query that picks a model uses."""
    import re

    from app.services.llm import availability

    lib = core_source("lib.rs").read_text(encoding="utf-8")
    body = lib[lib.index("macro_rules! model_available_sql"):]
    body = body[body.index("concat!(") + len("concat!("):]
    body = body[: body.index("\n        )\n")]
    pieces = re.findall(r'\$alias|"((?:[^"\\]|\\.)*)"', body, flags=re.S)
    # `$alias` matches with an empty group: the rule for an unaliased row.
    text = "".join(re.sub(r"\\\n\s*", "", piece) for piece in pieces)
    normal = lambda sql: " ".join(sql.split())
    assert normal(text) == normal(availability.MODEL_AVAILABLE_SQL), core_revision()
    # The three things it says, each of which the Python rule states.
    for part in ("enabled = true", "retire_date > (now() AT TIME ZONE 'UTC')::date", "available_provider.enabled = true", "mode = 'routing'"):
        assert part in availability.MODEL_AVAILABLE_SQL


@pytest.mark.parametrize(
    "enabled, retire, provider_enabled, mode, served",
    [
        (True, None, True, "chat", True),
        (False, None, True, "chat", False),
        (True, 0, True, "chat", False),
        (True, -1, True, "chat", False),
        (True, 1, True, "chat", True),
        (True, None, False, "chat", False),
        (True, None, None, "chat", False),
        (True, None, None, "routing", True),
        (True, None, False, "routing", True),
    ],
)
def test_the_rule(enabled, retire, provider_enabled, mode, served):
    from datetime import datetime, timezone

    from app.services.llm import availability

    today = datetime.now(timezone.utc).date()
    retire_date = None if retire is None else today + timedelta(days=retire)
    assert availability.is_available(
        enabled=enabled, retire_date=retire_date, provider_enabled=provider_enabled, mode=mode, today=today
    ) is served


def test_the_rule_reads_the_utc_date_whatever_the_hosts_local_date(monkeypatch):
    """At 00:30 in Central Europe on 15 June it is still 14 June in UTC, and
    the gateway calls a model that retires on the 15th until UTC midnight."""
    from datetime import datetime as real_datetime, timezone

    from app.services.llm import availability

    class Clock(real_datetime):
        @classmethod
        def now(cls, tz=None):
            utc = real_datetime(2026, 6, 14, 22, 30, tzinfo=timezone.utc)
            return utc if tz is not None else utc.replace(tzinfo=None) + timedelta(hours=2)

    monkeypatch.setattr(availability, "datetime", Clock)
    assert availability._today() == date(2026, 6, 14)
    assert availability.is_available(
        enabled=True, retire_date=date(2026, 6, 15), provider_enabled=True, mode="chat"
    ) is True
    assert availability.is_available(
        enabled=True, retire_date=date(2026, 6, 14), provider_enabled=True, mode="chat"
    ) is False
