# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Audit-export destinations: what a run may never do, and what the API may
never return or lose.

* A run never skips a row: when a window holds more rows than one run
  exports, the position moves to the last row exported and no further.
* A destination's secret is never returned, not even as ciphertext; the
  response says which secrets are stored.
* Editing a destination keeps a secret the request does not mention and
  removes one the request sends empty.
* A column allow-list that names no existing column is refused when saved,
  and exports nothing if it is met at run time.
* Every destination type is validated when saved.
"""

from __future__ import annotations

import gzip
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database import Base
from app.database.models.audit_export_destination import (
    DEST_TYPE_S3,
    DEST_TYPE_SPLUNK_HEC,
    DEST_TYPE_WEBHOOK,
    STATUS_FAILURE,
    AuditExportDestination,
)
from app.database.models.proxy_log import ProxyLog
from app.database.models.tenant import Tenant
from app.services.audit_export import service as service_module
from app.services.audit_export.service import AuditExportService
from app.services.config_manager import ConfigManager
from app.services.oauth.encryption_service import EncryptionService
from tests.admin_endpoint_harness import build_admin_harness


@pytest.fixture(autouse=True)
def _rows_are_exported_as_soon_as_they_are_written(monkeypatch):
    """A run leaves the newest moments for the next one
    (`tests/test_audit_export_service.py`); these tests export what they
    have just written."""
    from app.services.audit_export import service as export_service

    monkeypatch.setattr(export_service, "COMMIT_LAG_SECONDS", 0)

BASE = "/v1/admin/audit-export-destinations"


def _encryption() -> EncryptionService:
    return EncryptionService(config_manager=ConfigManager(logger=BrutorLogger("t"), validate=False))


# --- a run never skips a row -------------------------------------------------


@pytest.fixture
def db():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(
        engine, tables=[Tenant.__table__, ProxyLog.__table__, AuditExportDestination.__table__]
    )
    session = sessionmaker(bind=engine, future=True)()
    session.add(Tenant(id="tenant-a", name="tenant-a", display_name="T", primary_contact_email="t@example.invalid"))
    session.flush()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _row(db, ident: str, timestamp: datetime) -> None:
    db.add(
        ProxyLog(
            id=ident, tenant_id="tenant-a", timestamp=timestamp, created_at=timestamp, proxy_type="mcp",
            proxy_subtype="mcp_jsonrpc",
            end_user_session_id="s", http_method="POST", endpoint="/x", http_status_code=200, success=True,
        )
    )


def _destination(db, **kwargs) -> AuditExportDestination:
    dest = AuditExportDestination(
        id=f"axd-{uuid.uuid4().hex[:12]}", tenant_id="tenant-a", name=uuid.uuid4().hex, enabled=True,
        destination_type=kwargs.pop("destination_type", DEST_TYPE_WEBHOOK),
        config=kwargs.pop("config", {"url": "https://siem.example.invalid/in"}),
        interval_seconds=3600, **kwargs,
    )
    db.add(dest)
    db.flush()
    return dest


def _ids(payload: bytes) -> list:
    lines = [json.loads(line) for line in gzip.decompress(payload).decode().strip().split("\n")]
    return [line.get("id") for line in lines[1:]]


@pytest.fixture
def sent(monkeypatch):
    calls = []

    def send(payload, **kwargs):
        calls.append(payload)
        return True, None

    monkeypatch.setattr("app.services.audit_export.backends.SEND_WEBHOOK", send)
    return calls


def test_rows_beyond_one_run_are_exported_by_the_next_runs(db, sent, monkeypatch):
    monkeypatch.setattr(service_module, "MAX_ROWS_PER_RUN", 3)
    start = datetime.now(timezone.utc) - timedelta(hours=1)
    expected = []
    for i in range(8):
        _row(db, f"row-{i:02d}", start + timedelta(seconds=i))
        expected.append(f"row-{i:02d}")
    db.flush()
    dest = _destination(db)
    service = AuditExportService(db=db, encryption_service=None)

    exported = []
    for _ in range(4):
        service.run_destination(dest.id)
        if sent:
            exported.extend(_ids(sent.pop()))

    assert exported == expected


def test_a_run_reports_the_window_it_exported(db, sent, monkeypatch):
    monkeypatch.setattr(service_module, "MAX_ROWS_PER_RUN", 2)
    start = datetime.now(timezone.utc) - timedelta(hours=1)
    for i in range(5):
        _row(db, f"row-{i}", start + timedelta(seconds=i))
    db.flush()
    dest = _destination(db)

    result = AuditExportService(db=db, encryption_service=None).run_destination(dest.id)

    assert result.rows_exported == 2
    assert result.window_end.replace(tzinfo=None) == (start + timedelta(seconds=1)).replace(tzinfo=None)
    meta = json.loads(gzip.decompress(sent[0]).decode().split("\n")[0])["_meta"]
    assert meta["row_count"] == 2
    assert meta["window_end"] == result.window_end.isoformat()


def test_rows_sharing_the_timestamp_of_the_last_row_go_in_the_same_run(db, sent, monkeypatch):
    monkeypatch.setattr(service_module, "MAX_ROWS_PER_RUN", 2)
    start = datetime.now(timezone.utc) - timedelta(hours=1)
    _row(db, "a", start)
    for ident in ("b", "c", "d"):
        _row(db, ident, start + timedelta(seconds=1))
    _row(db, "e", start + timedelta(seconds=2))
    db.flush()
    dest = _destination(db)
    service = AuditExportService(db=db, encryption_service=None)

    service.run_destination(dest.id)
    first = _ids(sent.pop())
    service.run_destination(dest.id)
    second = _ids(sent.pop())

    assert first == ["a", "b", "c", "d"]
    assert second == ["e"]


def test_an_allow_list_with_no_existing_column_exports_nothing(db, sent):
    _row(db, "a", datetime.now(timezone.utc) - timedelta(minutes=1))
    db.flush()
    dest = _destination(db, field_allowlist=["no_such_column"])

    result = AuditExportService(db=db, encryption_service=None).run_destination(dest.id)

    assert sent == []
    assert result.outcome.value == STATUS_FAILURE
    assert "allow-list" in result.error
    assert dest.last_exported_at is None


# --- the API -----------------------------------------------------------------------


@pytest.fixture(scope="module")
def harness():
    from app.api.v1.endpoints.admin.audit_export_destinations import create_audit_export_router

    h = build_admin_harness([create_audit_export_router(_encryption())])
    yield h
    h.close()


def _create(harness, **body):
    body.setdefault("name", uuid.uuid4().hex)
    return harness.client.post(BASE, json=body, headers=harness.headers())


def _stored(harness, destination_id: str) -> dict:
    s = harness.Factory()
    try:
        return dict(s.get(AuditExportDestination, destination_id).config)
    finally:
        s.close()


WEBHOOK = {"destination_type": DEST_TYPE_WEBHOOK, "config": {"url": "https://siem.example.invalid/in", "bearer_token": "wh-secret"}}
SPLUNK = {"destination_type": DEST_TYPE_SPLUNK_HEC, "config": {"url": "https://splunk.example.invalid:8088/services/collector/event", "token": "hec-secret"}}
S3 = {"destination_type": DEST_TYPE_S3, "config": {"bucket": "b", "access_key_id": "AKIAEXAMPLE", "secret_access_key": "s3-secret"}}


@pytest.mark.parametrize(
    "body, mark",
    [(WEBHOOK, "has_bearer_token"), (SPLUNK, "has_token"), (S3, "has_secret_access_key")],
    ids=["webhook", "splunk_hec", "s3"],
)
def test_no_response_carries_a_secret_or_its_ciphertext(harness, body, mark):
    created = _create(harness, **body)
    assert created.status_code == 201, created.text
    destination_id = created.json()["id"]
    stored = _stored(harness, destination_id)
    ciphertexts = [value for key, value in stored.items() if key.endswith("_encrypted")]
    assert len(ciphertexts) == 1

    reader = harness.limited_headers("t1", [Permission.PROXY_LOGS_READ])
    responses = [
        created,
        harness.client.get(f"{BASE}/{destination_id}", headers=reader),
        harness.client.get(BASE, headers=reader),
        harness.client.patch(f"{BASE}/{destination_id}", json={"enabled": False}, headers=harness.headers()),
    ]
    for response in responses:
        assert response.status_code < 300, response.text
        assert ciphertexts[0] not in response.text
        assert "-secret" not in response.text
        assert "_encrypted" not in response.text
    assert responses[1].json()["config"][mark] is True


def test_editing_a_destination_keeps_the_secret_it_does_not_mention(harness):
    destination_id = _create(harness, **WEBHOOK).json()["id"]
    before = _stored(harness, destination_id)["bearer_token_encrypted"]

    patched = harness.client.patch(
        f"{BASE}/{destination_id}", json={"config": {"url": "https://other.example.invalid/in"}}, headers=harness.headers()
    )

    assert patched.status_code == 200, patched.text
    stored = _stored(harness, destination_id)
    assert stored["url"] == "https://other.example.invalid/in"
    assert stored["bearer_token_encrypted"] == before
    assert patched.json()["config"]["has_bearer_token"] is True


def test_an_empty_secret_removes_it_and_a_new_one_replaces_it(harness):
    destination_id = _create(harness, **WEBHOOK).json()["id"]
    url = WEBHOOK["config"]["url"]

    replaced = harness.client.patch(
        f"{BASE}/{destination_id}", json={"config": {"url": url, "bearer_token": "another"}}, headers=harness.headers()
    )
    assert replaced.status_code == 200, replaced.text
    assert _encryption().decrypt(_stored(harness, destination_id)["bearer_token_encrypted"]) == "another"

    removed = harness.client.patch(
        f"{BASE}/{destination_id}", json={"config": {"url": url, "bearer_token": ""}}, headers=harness.headers()
    )
    assert removed.status_code == 200, removed.text
    assert "bearer_token_encrypted" not in _stored(harness, destination_id)
    assert removed.json()["config"]["has_bearer_token"] is False


def test_a_secret_the_destination_cannot_work_without_is_not_removed(harness):
    destination_id = _create(harness, **SPLUNK).json()["id"]

    removed = harness.client.patch(
        f"{BASE}/{destination_id}",
        json={"config": {"url": SPLUNK["config"]["url"], "token": ""}},
        headers=harness.headers(),
    )

    assert removed.status_code == 422, removed.text
    assert "token" in removed.text
    assert "token_encrypted" in _stored(harness, destination_id)


@pytest.mark.parametrize("key", ["bearer_token_encrypted", "has_bearer_token"])
def test_a_stored_form_of_a_secret_is_not_accepted_from_a_caller(harness, key):
    response = _create(
        harness, destination_type=DEST_TYPE_WEBHOOK, config={"url": "https://siem.example.invalid/in", key: "x"}
    )
    assert response.status_code == 422, response.text
    assert key in response.text


@pytest.mark.parametrize(
    "config, names",
    [
        ({}, "url"),
        ({"url": "https://splunk.example.invalid:8088/x"}, "token"),
        ({"token": "t"}, "url"),
        ({"url": "ftp://splunk.example.invalid/x", "token": "t"}, "url"),
        ({"url": "https://splunk.example.invalid/x", "token": ""}, "token"),
    ],
)
def test_a_splunk_destination_is_validated_when_saved(harness, config, names):
    response = _create(harness, destination_type=DEST_TYPE_SPLUNK_HEC, config=config)
    assert response.status_code == 422, response.text
    assert names in response.text


def test_a_webhook_destination_is_validated_when_saved(harness):
    for config in ({"url": "siem.example.invalid"}, {"url": "https://x.example.invalid", "headers": ["a"]},
                   {"url": "https://x.example.invalid", "headers": {"a": 1}}):
        response = _create(harness, destination_type=DEST_TYPE_WEBHOOK, config=config)
        assert response.status_code == 422, (config, response.text)


def test_an_update_is_validated_as_a_creation_is(harness):
    destination_id = _create(harness, **SPLUNK).json()["id"]
    response = harness.client.patch(
        f"{BASE}/{destination_id}", json={"config": {"token": "t"}}, headers=harness.headers()
    )
    assert response.status_code == 422, response.text
    assert "url" in response.text


def test_an_allow_list_that_names_no_existing_column_is_refused(harness):
    refused = _create(harness, field_allowlist=["no_such_column", "nor_this"], **WEBHOOK)
    assert refused.status_code == 422, refused.text
    assert "no_such_column" in refused.text

    destination_id = _create(harness, field_allowlist=["id", "chain"], **WEBHOOK).json()["id"]
    patched = harness.client.patch(
        f"{BASE}/{destination_id}", json={"field_allowlist": ["no_such_column"]}, headers=harness.headers()
    )
    assert patched.status_code == 422, patched.text


def test_an_allow_list_names_only_existing_columns(harness):
    """One mistyped name among valid ones would export less than the
    administrator meant, silently."""
    refused = _create(harness, field_allowlist=["id", "timestmp"], **WEBHOOK)
    assert refused.status_code == 422, refused.text
    assert "timestmp" in refused.text


def test_an_empty_allow_list_is_refused_and_null_exports_every_column(harness):
    assert _create(harness, field_allowlist=[], **WEBHOOK).status_code == 422
    created = _create(harness, field_allowlist=None, **WEBHOOK)
    assert created.status_code == 201
    assert created.json()["field_allowlist"] is None


# --- a webhook's headers are credentials -----------------------------------------


HEADERS = {"X-Api-Key": "header-secret", "X-Tenant": "acme"}


def test_a_webhooks_headers_are_stored_encrypted_and_never_returned(harness):
    created = _create(
        harness, destination_type=DEST_TYPE_WEBHOOK,
        config={"url": "https://siem.example.invalid/in", "headers": HEADERS},
    )
    assert created.status_code == 201, created.text
    stored = _stored(harness, created.json()["id"])
    assert "headers" not in stored and "header-secret" not in json.dumps(stored)
    assert json.loads(_encryption().decrypt(stored["headers_encrypted"])) == HEADERS

    for response in (created, harness.client.get(f"{BASE}/{created.json()['id']}", headers=harness.headers())):
        assert "header-secret" not in response.text and "acme" not in response.text
        config = response.json()["config"]
        assert config["has_headers"] is True and config["header_names"] == ["X-Api-Key", "X-Tenant"]
        assert "headers" not in config


def test_editing_keeps_the_headers_it_does_not_mention_and_an_empty_object_removes_them(harness):
    destination_id = _create(
        harness, destination_type=DEST_TYPE_WEBHOOK,
        config={"url": "https://siem.example.invalid/in", "headers": HEADERS},
    ).json()["id"]
    before = _stored(harness, destination_id)["headers_encrypted"]

    kept = harness.client.patch(
        f"{BASE}/{destination_id}", json={"config": {"url": "https://other.example.invalid/in"}}, headers=harness.headers()
    )
    assert kept.status_code == 200, kept.text
    assert _stored(harness, destination_id)["headers_encrypted"] == before
    assert kept.json()["config"]["header_names"] == ["X-Api-Key", "X-Tenant"]

    removed = harness.client.patch(
        f"{BASE}/{destination_id}",
        json={"config": {"url": "https://other.example.invalid/in", "headers": {}}}, headers=harness.headers(),
    )
    assert removed.status_code == 200, removed.text
    stored = _stored(harness, destination_id)
    assert "headers_encrypted" not in stored and "header_names" not in stored
    assert removed.json()["config"]["has_headers"] is False


def test_header_names_are_not_accepted_from_a_caller(harness):
    r = _create(harness, destination_type=DEST_TYPE_WEBHOOK,
                config={"url": "https://siem.example.invalid/in", "header_names": ["X"]})
    assert r.status_code == 422, r.text


def test_a_run_sends_the_decrypted_headers(db, monkeypatch):
    sent = {}

    def send(payload, **kwargs):
        sent.update(kwargs)
        return True, None

    monkeypatch.setattr("app.services.audit_export.backends.SEND_WEBHOOK", send)
    encryption = _encryption()
    _row(db, "a", datetime.now(timezone.utc) - timedelta(minutes=1))
    db.flush()
    dest = _destination(db, config={
        "url": "https://siem.example.invalid/in",
        "headers_encrypted": encryption.encrypt(json.dumps(HEADERS)),
        "header_names": sorted(HEADERS),
        # A clear-text object under the old key is not what is sent.
        "headers": {"X-Api-Key": "stale"},
    })

    AuditExportService(db=db, encryption_service=encryption).run_destination(dest.id)

    assert sent["headers"] == HEADERS
