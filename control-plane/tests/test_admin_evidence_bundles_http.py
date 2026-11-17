# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Evidence bundles, bundle verification, key ids and the record summary's
# effect mode — HTTP behavioural spec

    GET  /v1/admin/evidence/keys
    POST /v1/admin/evidence/bundles
    POST /v1/admin/evidence/bundles/verify
    GET  /v1/admin/evidence/records   (effect_status / effect_mode)

On the shared admin harness (real auth, full-schema SQLite). The core is
never contacted: `core_client.build_bundle` / `verify_bundle` are replaced
per test.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from app.api.v1.endpoints.admin.evidence import create_evidence_router
from app.core.logging_system import BrutorLogger
from app.database.models.action_record import ActionRecord, derive_effect_mode
from app.database.models.evidence_log import EvidenceLog
from app.database.models.evidence_settings import EvidenceSettings
from app.database.models.evidence_signing_key import EvidenceSigningKey
from app.services.evidence import core_client
from tests.admin_endpoint_harness import build_admin_harness

TENANT = "t1"
OTHER = "t2"
LOG_ID = hashlib.sha256(b"deployment|t1").hexdigest()
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
KEYS = "/v1/admin/evidence/keys"
BUNDLES = "/v1/admin/evidence/bundles"
VERIFY = "/v1/admin/evidence/bundles/verify"
RECORDS = "/v1/admin/evidence/records"


def _body(verdict, effect):
    body = {"schema": "ai.brutor/action-record/v1", "verdict": verdict, "action_id": f"mcp/{verdict}"}
    if effect is not None:
        body["effect"] = effect
    return body


# (record id, verdict, effect)
ROWS = [
    ("rec-planned", "planned", {"type": "tools/call", "status": "planned", "attestation": "gate_observed"}),
    ("rec-confirmed", "executed", {"type": "tools/call", "status": "confirmed", "response_digest": "0a" * 32}),
    ("rec-errored", "errored", {"type": "tools/call", "status": "failed"}),
    ("rec-denied", "denied", None),
]


def seed(session):
    session.add(EvidenceLog(log_id=LOG_ID, tenant_id=TENANT, next_seq=len(ROWS) + 1, tree_size=len(ROWS) + 1, root="00" * 32, kid="aa" * 32))
    for i, (rid, verdict, effect) in enumerate(ROWS):
        session.add(ActionRecord(
            record_id=rid, tenant_id=TENANT, log_id=LOG_ID, seq=i, root_task_id="run-1",
            verdict=verdict, kind="decide", surface="mcp", timestamp=T0 + timedelta(minutes=i),
            record=_body(verdict, effect), statement=b"s", kid="aa" * 32,
        ))
    # A pruned record: body gone, mode unknown (never guessed).
    session.add(ActionRecord(
        record_id="rec-pruned", tenant_id=TENANT, log_id=LOG_ID, seq=len(ROWS), root_task_id="run-1",
        verdict="executed", kind="decide", surface="mcp", timestamp=T0 + timedelta(minutes=9),
        record={}, statement=b"s", kid="aa" * 32,
    ))
    session.add(EvidenceSigningKey(kid="d1" * 32, tenant_id=None, encrypted_seed="secret-seed-1", status="retired"))
    session.add(EvidenceSigningKey(kid="e1" * 32, tenant_id=TENANT, encrypted_seed="secret-seed-2", status="active"))
    session.add(EvidenceSigningKey(kid="f1" * 32, tenant_id=OTHER, encrypted_seed="secret-seed-3", status="active"))


@pytest.fixture(scope="module")
def harness():
    h = build_admin_harness([create_evidence_router(BrutorLogger("evidence-bundle-tests"))])
    h.seed(seed)
    yield h
    h.close()


class TestEffectMode:
    @pytest.mark.parametrize(
        "effect, mode",
        [
            (None, "not_applicable"),
            ({}, "not_applicable"),
            ({"status": "planned"}, "not_applicable"),
            ({"status": "confirmed", "response_digest": "ab" * 32}, "confirmed"),
            ({"status": "confirmed", "response_digest": "AB" * 32}, "dispatched_unconfirmed"),
            ({"status": "confirmed"}, "dispatched_unconfirmed"),
            ({"status": "failed"}, "dispatched_unconfirmed"),
            ({"status": "dispatched"}, "dispatched_unconfirmed"),
            ({"status": "something-new"}, "dispatched_unconfirmed"),
        ],
    )
    def test_derivation_never_grades_up(self, effect, mode):
        assert derive_effect_mode(effect) == mode

    def test_summary_carries_status_and_mode(self, harness):
        r = harness.client.get(RECORDS, params={"root_task_id": "run-1"}, headers=harness.headers())
        assert r.status_code == 200, r.text
        by_id = {i["record_id"]: i for i in r.json()["items"]}
        assert by_id["rec-planned"]["effect_status"] == "planned"
        assert by_id["rec-planned"]["effect_mode"] == "not_applicable"
        assert by_id["rec-confirmed"]["effect_mode"] == "confirmed"
        assert by_id["rec-errored"]["effect_mode"] == "dispatched_unconfirmed"
        assert by_id["rec-denied"]["effect_status"] is None
        assert by_id["rec-denied"]["effect_mode"] == "not_applicable"
        assert by_id["rec-pruned"]["effect_mode"] is None, "a pruned body has no mode"
        assert r.json()["items"][0]["record_id"] == "rec-pruned"


class TestKeys:
    def test_lists_deployment_and_own_tenant_keys_without_seeds(self, harness):
        r = harness.client.get(KEYS, headers=harness.headers())
        assert r.status_code == 200, r.text
        body = r.json()
        kids = {k["kid"]: k for k in body["items"]}
        assert set(kids) == {"d1" * 32, "e1" * 32}, "another tenant's key never listed"
        assert kids["d1" * 32]["scope"] == "deployment" and kids["d1" * 32]["status"] == "retired"
        assert kids["e1" * 32]["scope"] == "tenant" and kids["e1" * 32]["status"] == "active"
        assert "secret-seed" not in r.text and "encrypted_seed" not in r.text
        assert "tenant_id" not in kids["e1" * 32]
        assert body["log_kid"] == "aa" * 32
        assert body["per_tenant_key"] is False

    def test_reports_per_tenant_key_setting(self, harness):
        harness.seed(lambda s: s.merge(EvidenceSettings(tenant_id=TENANT, per_tenant_key=True, salt_digests_default=False)))
        try:
            assert harness.client.get(KEYS, headers=harness.headers()).json()["per_tenant_key"] is True
        finally:
            harness.seed(lambda s: s.query(EvidenceSettings).delete())

    def test_other_tenant_sees_no_log(self, harness):
        body = harness.client.get(KEYS, headers=harness.headers(OTHER)).json()
        assert body["log_kid"] is None
        assert {k["kid"] for k in body["items"]} == {"d1" * 32, "f1" * 32}

    def test_unauthenticated(self, harness):
        assert harness.client.get(KEYS).status_code in (401, 403)


class TestBuildBundle:
    def _fake(self, monkeypatch, result):
        calls = []

        def fake(tenant_id, selector, **kw):
            calls.append((tenant_id, selector, kw))
            return result

        monkeypatch.setattr(core_client, "build_bundle", fake)
        return calls

    def test_run_selector_goes_to_the_core_for_the_callers_tenant(self, harness, monkeypatch):
        calls = self._fake(monkeypatch, {"bundle_id": "bun-1", "bundle_digest": "ab" * 32, "record_id": "cd" * 32, "bundle": {"manifest": {}}})
        r = harness.client.post(BUNDLES, json={"selector": {"root_task_id": "run-1"}}, headers=harness.headers())
        assert r.status_code == 200, r.text
        assert r.json()["bundle_id"] == "bun-1"
        tenant, selector, kw = calls[0]
        assert tenant == TENANT and selector == {"root_task_id": "run-1"}
        assert kw["audience"] is None and kw["closure_depth"] == 1
        assert kw["requested_by"], "the requesting admin is stored on the bundle row"

    def test_blank_audience_is_no_disclosure(self, harness, monkeypatch):
        calls = self._fake(monkeypatch, {"bundle_id": "b"})
        harness.client.post(BUNDLES, json={"selector": {"root_task_id": "r"}, "audience": "  "}, headers=harness.headers())
        assert calls[0][2]["audience"] is None
        harness.client.post(BUNDLES, json={"selector": {"root_task_id": "r"}, "audience": " Auditor X "}, headers=harness.headers())
        assert calls[1][2]["audience"] == "Auditor X"

    def test_record_ids_are_deduplicated(self, harness, monkeypatch):
        calls = self._fake(monkeypatch, {"bundle_id": "b"})
        r = harness.client.post(BUNDLES, json={"selector": {"record_ids": ["a", "b", "a"]}}, headers=harness.headers())
        assert r.status_code == 200
        assert calls[0][1] == {"record_ids": ["a", "b"]}

    def test_window_selector(self, harness, monkeypatch):
        calls = self._fake(monkeypatch, {"bundle_id": "b"})
        r = harness.client.post(
            BUNDLES,
            json={"selector": {"ai_system_id": "sys-a", "from": "2026-09-01T00:00:00Z", "to": "2026-09-02T00:00:00Z"}},
            headers=harness.headers(),
        )
        assert r.status_code == 200, r.text
        assert calls[0][1]["ai_system_id"] == "sys-a" and calls[0][1]["from"].startswith("2026-09-01")

    @pytest.mark.parametrize(
        "selector",
        [
            {},
            {"root_task_id": "r", "record_ids": ["a"]},
            {"ai_system_id": "sys-a"},
            {"ai_system_id": "sys-a", "from": "2026-09-02T00:00:00Z", "to": "2026-09-01T00:00:00Z"},
        ],
    )
    def test_selector_must_name_exactly_one_well_formed_scope(self, harness, monkeypatch, selector):
        calls = self._fake(monkeypatch, {"bundle_id": "b"})
        r = harness.client.post(BUNDLES, json={"selector": selector}, headers=harness.headers())
        assert r.status_code == 422, r.text
        assert calls == []

    def test_closure_depth_is_bounded(self, harness, monkeypatch):
        self._fake(monkeypatch, {"bundle_id": "b"})
        r = harness.client.post(BUNDLES, json={"selector": {"root_task_id": "r"}, "closure_depth": 9}, headers=harness.headers())
        assert r.status_code == 422

    @pytest.mark.parametrize(
        "result, status, code",
        [
            ({"error": "core_error", "detail": "HTTP 404: no_records"}, 404, "no_records"),
            ({"error": "core_error", "detail": "HTTP 413: too big"}, 413, "bundle_too_large"),
            ({"error": "evidence_disabled", "detail": "HTTP 503"}, 503, "evidence_disabled"),
            ({"error": "core_unreachable", "detail": "refused"}, 502, "core_unreachable"),
        ],
    )
    def test_core_errors_keep_their_meaning(self, harness, monkeypatch, result, status, code):
        self._fake(monkeypatch, result)
        r = harness.client.post(BUNDLES, json={"selector": {"root_task_id": "r"}}, headers=harness.headers())
        assert r.status_code == status, r.text
        assert r.json()["detail"]["error"] == code

    def test_unauthenticated(self, harness):
        assert harness.client.post(BUNDLES, json={"selector": {"root_task_id": "r"}}).status_code in (401, 403)


class TestVerifyBundle:
    def test_relays_the_core_and_compares_the_expected_digest(self, harness, monkeypatch):
        monkeypatch.setattr(core_client, "verify_bundle", lambda b, **kw: {"ok": True, "findings": [], "bundle_digest": "ab" * 32, "missing": []})
        r = harness.client.post(VERIFY, json={"bundle": {"manifest": {}}, "bundle_digest": "AB" * 32}, headers=harness.headers())
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True and r.json()["digest_matches"] is True
        r = harness.client.post(VERIFY, json={"bundle": {"manifest": {}}, "bundle_digest": "cd" * 32}, headers=harness.headers())
        assert r.json()["digest_matches"] is False

    def test_unreachable_verifier_is_never_ok(self, harness, monkeypatch):
        monkeypatch.setattr(core_client, "verify_bundle", lambda b, **kw: {"error": "core_unreachable", "detail": "refused"})
        r = harness.client.post(VERIFY, json={"bundle": {"manifest": {}}}, headers=harness.headers())
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False
        assert body["findings"][0]["code"] == core_client.VERIFIER_UNAVAILABLE
