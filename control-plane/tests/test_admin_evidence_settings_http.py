# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Evidence settings + key rotation — HTTP behavioural spec (a178_evidence_programme)

    GET  /v1/admin/evidence/settings
    PUT  /v1/admin/evidence/settings
    POST /v1/admin/evidence/keys/rotate

On the shared admin harness (real auth, full-schema SQLite). The core is
never contacted: `core_client.rotate_key` is replaced per test.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest

from app.api.v1.endpoints.admin.evidence import create_evidence_router
from app.core.logging_system import BrutorLogger
from app.database.models.evidence_settings import EvidenceSettings
from app.services.evidence import core_client
from tests.admin_endpoint_harness import build_admin_harness

TENANT = "t1"
OTHER = "t2"
SETTINGS = "/v1/admin/evidence/settings"
ROTATE = "/v1/admin/evidence/keys/rotate"


@pytest.fixture(scope="module")
def _module_harness():
    h = build_admin_harness([create_evidence_router(BrutorLogger("evidence-settings-tests"))])
    yield h
    h.close()


@pytest.fixture()
def harness(_module_harness):
    """One app per module (login is bcrypt-slow); settings rows are cleared
    before each test so tests do not see each other's writes."""
    _module_harness.seed(lambda s: s.query(EvidenceSettings).delete())
    return _module_harness


def _row(harness, tenant):
    s = harness.Factory()
    try:
        return s.get(EvidenceSettings, tenant)
    finally:
        s.close()


class TestSettings:
    def test_defaults_without_a_row(self, harness):
        r = harness.client.get(SETTINGS, headers=harness.headers())
        assert r.status_code == 200, r.text
        assert r.json() == {
            "tenant_id": TENANT,
            "per_tenant_key": False,
            "salt_digests_default": False,
            "updated_at": None,
            "updated_by": None,
        }
        assert _row(harness, TENANT) is None, "GET must not create a row"

    def test_put_creates_then_updates_partially(self, harness):
        r = harness.client.put(SETTINGS, json={"salt_digests_default": True}, headers=harness.headers())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["salt_digests_default"] is True and body["per_tenant_key"] is False
        assert body["updated_at"] and body["updated_by"]

        r = harness.client.put(SETTINGS, json={"per_tenant_key": True}, headers=harness.headers())
        assert r.status_code == 200
        assert r.json()["per_tenant_key"] is True
        assert r.json()["salt_digests_default"] is True, "omitted fields are unchanged"

        assert harness.client.get(SETTINGS, headers=harness.headers()).json()["per_tenant_key"] is True

    def test_tenant_isolation(self, harness):
        harness.client.put(SETTINGS, json={"per_tenant_key": True}, headers=harness.headers())
        other = harness.client.get(SETTINGS, headers=harness.headers(OTHER)).json()
        assert other["tenant_id"] == OTHER and other["per_tenant_key"] is False
        # A tenant admin cannot address another tenant.
        r = harness.client.get(SETTINGS, params={"tenant_id": OTHER}, headers=harness.headers())
        assert r.status_code in (400, 403)


    def test_body_type_is_validated(self, harness):
        r = harness.client.put(SETTINGS, json={"per_tenant_key": "maybe"}, headers=harness.headers())
        assert r.status_code == 422

    def test_unauthenticated(self, harness):
        assert harness.client.get(SETTINGS).status_code in (401, 403)
        assert harness.client.put(SETTINGS, json={}).status_code in (401, 403)
        assert harness.client.post(ROTATE).status_code in (401, 403)


class TestRotate:
    def test_refused_while_per_tenant_key_is_off(self, harness, monkeypatch):
        called = []
        monkeypatch.setattr(core_client, "rotate_key", lambda t: called.append(t) or {})
        r = harness.client.post(ROTATE, headers=harness.headers())
        assert r.status_code == 409
        assert r.json()["detail"]["error"] == "per_tenant_key_disabled"
        assert called == []

    def test_rotates_through_the_core(self, harness, monkeypatch):
        harness.client.put(SETTINGS, json={"per_tenant_key": True}, headers=harness.headers())
        called = []

        def fake(tenant_id):
            called.append(tenant_id)
            return {
                "kid": "ab" * 32,
                "previous_kid": "cd" * 32,
                "tenant_id": tenant_id,
                "rotation_record_id": "ef" * 32,
                "created_at": "2026-09-19T12:00:00Z",
                "status": "active",
            }

        monkeypatch.setattr(core_client, "rotate_key", fake)
        r = harness.client.post(ROTATE, headers=harness.headers())
        assert r.status_code == 200, r.text
        assert called == [TENANT]
        body = r.json()
        assert body["kid"] == "ab" * 32 and body["rotation_record_id"] == "ef" * 32
        assert body["status"] == "active", "core members pass through, not stripped"

    @pytest.mark.parametrize(
        "code,status",
        [
            ("evidence_disabled", 503),
            ("tenant_key_unavailable", 503),
            ("core_unreachable", 502),
            ("core_error", 502),
            ("invalid_record", 422),
        ],
    )
    def test_core_errors_map_to_http(self, harness, monkeypatch, code, status):
        harness.client.put(SETTINGS, json={"per_tenant_key": True}, headers=harness.headers())
        monkeypatch.setattr(core_client, "rotate_key", lambda t: {"error": code, "detail": "why"})
        r = harness.client.post(ROTATE, headers=harness.headers())
        assert r.status_code == status
        assert r.json()["detail"] == {"error": code, "detail": "why"}
