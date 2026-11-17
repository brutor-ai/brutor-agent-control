# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Regression tests for the guardrail-config provider-completeness
validator (`_validate_provider_completeness` in
app/api/v1/endpoints/admin/guardrails.py).

The validator is a closure inside `create_guardrails_router`, so it is
exercised through the real `POST /v1/admin/guardrails/configs` endpoint
with a FastAPI TestClient. It rejects a config (HTTP 400,
`error: "guardrail_provider_incomplete"`) when an external provider is
selected but its required configuration is missing:

  - prompt_injection / jailbreak = lakera_guard  → needs a Lakera api_key
  - toxic_content = openai_moderation            → needs an OpenAI api_key
  - pii_provider = presidio                      → needs a Presidio endpoint
  - an enabled Bedrock provider                  → needs config.guardrail_id
  - an enabled Custom Webhook provider           → needs an endpoint

Test isolation: auth dependencies are monkeypatched to no-ops, the
tenant quota check is stubbed out, and `get_db` yields a stub whose
query chain returns no rows — so the request reaches the validator
without a database. A request that *passes* the validator may still
fail later in the handler (the stub DB cannot persist); the tests for
valid configs therefore assert only that the response is **not** a
`guardrail_provider_incomplete` rejection.

Run: pytest tests/test_guardrail_provider_validation.py -v
"""

import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.services.tenant.tenant_service import TenantService


CONFIGS_URL = "/v1/admin/guardrails/configs"


# --- stubs -----------------------------------------------------------------


class _EmptyQuery:
    """SQLAlchemy-query stand-in that matches no rows."""

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def offset(self, *a, **k):
        return self

    def update(self, *a, **k):
        return 0

    def count(self):
        return 0

    def all(self):
        return []

    def first(self):
        return None


class _StubDB:
    def query(self, *a, **k):
        return _EmptyQuery()

    def add(self, *a, **k):
        return None

    def delete(self, *a, **k):
        return None

    def commit(self):
        return None

    def flush(self):
        return None

    def refresh(self, *a, **k):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


class _StubAudit:
    """Permissive audit-logger stand-in — any attribute is a no-op."""

    def __getattr__(self, _name):
        def _noop(*a, **k):
            return None

        return _noop


_FAKE_ADMIN = types.SimpleNamespace(
    id="admin-test", username="tester", email="tester@example.com", tenant_id="t1"
)



@pytest.fixture(autouse=True)
def _feature_gate_open(monkeypatch):
    """The stub DB holds no rows at all, so the tenant feature gate (which
    loads the Tenant row) would 404 before the provider validation under test
    ever ran. These tests are about validation, not entitlement — open it."""
    monkeypatch.setattr(TenantService, "check_feature_access", staticmethod(lambda *a, **k: None))

def _stub_get_db():
    yield _StubDB()


def _fake_require_permission(_permission):
    def _dep():
        return _FAKE_ADMIN

    return _dep


def _fake_require_tenant_resource_access(_resource_type):
    def _dep():
        return (_FAKE_ADMIN, "t1", "tenant")

    return _dep


# --- harness ---------------------------------------------------------------


@pytest.fixture()
def client(monkeypatch):
    """TestClient wired to the guardrails router with auth + quota + DB
    neutralised, so requests reach the provider-completeness validator."""
    import app.api.v1.endpoints.admin.guardrails as gr

    # Auth dependencies are captured by the route at construction time,
    # so patch them in the module before the factory runs.
    monkeypatch.setattr(gr, "require_permission", _fake_require_permission)
    monkeypatch.setattr(
        gr, "require_tenant_resource_access", _fake_require_tenant_resource_access
    )
    monkeypatch.setattr(gr, "get_audit_logger", lambda: _StubAudit())
    monkeypatch.setattr(gr, "get_db", _stub_get_db)
    # The tenant quota check runs before the validator and would hit the
    # DB — stub it to a no-op.
    monkeypatch.setattr(gr.TenantService, "check_quota", lambda *a, **k: None)

    app = FastAPI()
    app.include_router(
        gr.create_guardrails_router(
            types.SimpleNamespace(),
            BrutorLogger("test"),
        ),
        prefix="/v1",
    )
    return TestClient(app)


def _post(client, *, builtin=None, providers=None):
    body = {"name": "test-config"}
    if builtin is not None:
        body["builtin"] = builtin
    if providers is not None:
        body["providers"] = providers
    return client.post(CONFIGS_URL, json=body)


def _is_provider_rejection(resp) -> bool:
    if resp.status_code != 400:
        return False
    try:
        detail = resp.json().get("detail", {})
    except Exception:
        return False
    return isinstance(detail, dict) and detail.get("error") == "guardrail_provider_incomplete"


def _assert_rejected(resp):
    assert _is_provider_rejection(resp), (
        f"expected guardrail_provider_incomplete 400, got {resp.status_code}: {resp.text}"
    )


def _assert_not_rejected(resp):
    assert not _is_provider_rejection(resp), (
        f"validator wrongly rejected a valid config: {resp.text}"
    )


# --- Presidio (pii_provider) -----------------------------------------------


def test_presidio_without_provider_entry_is_rejected(client):
    r = _post(client, builtin={"pii_enabled": True, "pii_provider": "presidio"})
    _assert_rejected(r)


def test_presidio_with_endpoint_is_accepted(client):
    r = _post(
        client,
        builtin={"pii_enabled": True, "pii_provider": "presidio"},
        providers=[{"provider": "presidio", "endpoint": "http://presidio-analyzer:3000"}],
    )
    _assert_not_rejected(r)


def test_presidio_with_empty_endpoint_is_rejected(client):
    r = _post(
        client,
        builtin={"pii_enabled": True, "pii_provider": "presidio"},
        providers=[{"provider": "presidio", "endpoint": ""}],
    )
    _assert_rejected(r)


def test_presidio_with_disabled_provider_entry_is_rejected(client):
    r = _post(
        client,
        builtin={"pii_enabled": True, "pii_provider": "presidio"},
        providers=[
            {"provider": "presidio", "endpoint": "http://p:3000", "enabled": False}
        ],
    )
    _assert_rejected(r)


# --- Bedrock (standalone provider) -----------------------------------------


def test_bedrock_without_guardrail_id_is_rejected(client):
    r = _post(
        client,
        providers=[{"provider": "bedrock", "config": {"region": "us-east-1"}}],
    )
    _assert_rejected(r)


def test_bedrock_with_guardrail_id_is_accepted(client):
    # With the id alone it is no longer accepted: the gateway builds the
    # detector from the entry's own region and credential too
    # (`tests/test_guardrail_bedrock_entry.py`).
    _assert_rejected(_post(client, providers=[{"provider": "bedrock", "config": {"guardrail_id": "gr-abc123"}}]))
    r = _post(
        client,
        providers=[
            {"provider": "bedrock", "config": {
                "guardrail_id": "gr-abc123", "region": "us-east-1",
                "aws_access_key_id": "AKIAEXAMPLE", "aws_secret_access_key": "example-secret",
            }}
        ],
    )
    _assert_not_rejected(r)


def test_disabled_bedrock_without_guardrail_id_is_skipped(client):
    r = _post(
        client,
        providers=[{"provider": "bedrock", "enabled": False, "config": {}}],
    )
    _assert_not_rejected(r)


def test_bedrock_validated_even_when_builtin_absent(client):
    # Regression: the standalone-provider checks must run independently
    # of `builtin` — a config with no `builtin` block but a misconfigured
    # Bedrock provider is still rejected.
    r = _post(client, providers=[{"provider": "bedrock", "config": {}}])
    _assert_rejected(r)


# --- Custom Webhook (standalone provider) ----------------------------------


def test_custom_without_endpoint_is_rejected(client):
    r = _post(client, providers=[{"provider": "custom"}])
    _assert_rejected(r)


def test_custom_with_endpoint_is_accepted(client):
    r = _post(
        client,
        providers=[{"provider": "custom", "endpoint": "https://gr.example.com/check"}],
    )
    _assert_not_rejected(r)


# --- Pre-existing per-check selectors (harness covers them too) -------------


def test_toxic_openai_moderation_without_key_is_rejected(client):
    r = _post(
        client,
        builtin={"toxic_content_enabled": True, "toxic_content_provider": "openai_moderation"},
    )
    _assert_rejected(r)


def test_prompt_injection_lakera_without_key_is_rejected(client):
    r = _post(
        client,
        builtin={"prompt_injection_enabled": True, "prompt_injection_provider": "lakera_guard"},
    )
    _assert_rejected(r)


def test_lakera_with_key_is_accepted(client):
    r = _post(
        client,
        builtin={"prompt_injection_enabled": True, "prompt_injection_provider": "lakera_guard"},
        providers=[{"provider": "lakera", "api_key": "lk-secret"}],
    )
    _assert_not_rejected(r)


# --- Baseline --------------------------------------------------------------


def test_config_without_external_providers_is_accepted(client):
    # All checks on built-in engines, no providers[] — nothing to validate.
    r = _post(client, builtin={"pii_enabled": True})
    _assert_not_rejected(r)
