# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Audit-export service

Locks down the load-bearing behaviours of
`AuditExportService.run_destination`:

  1. Window bounds — only `last_exported_at` < timestamp <= window_end
     rows ship; rows arriving mid-run go in the next batch.
  2. High-water mark advances ONLY on success / no-data (at-least-once
     delivery semantics).
  3. Field allowlist projection filters columns before they leave;
     null / empty allowlists mean "all"; one naming no existing column
     exports nothing.
  4. Backend dispatch routes by destination_type; failures roll back
     to last_run_error WITHOUT advancing the high-water mark.
  5. Payload shape: gzipped JSONL with the metadata line at the head,
     one record per line after, matching the interactive admin export.

These tests use stubbed backends (`monkeypatch.setattr` on the
module-level SEND_S3 / SEND_WEBHOOK names — that's why the service
references them through `backends.SEND_S3` rather than importing
the functions directly) and an in-memory SQLite session so we don't
need a live Postgres for the unit suite.
"""

from __future__ import annotations

import gzip
import io
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.database.models.audit_export_destination import (
    AuditExportDestination,
    DEST_TYPE_S3,
    DEST_TYPE_WEBHOOK,
    STATUS_FAILURE,
    STATUS_NO_DATA,
    STATUS_SUCCESS,
)
from app.database.models.proxy_log import ProxyLog
from app.database.models.tenant import Tenant
from app.services.audit_export import RunOutcome, service as service_module
from app.services.audit_export.service import (
    AuditExportService,
)


# ---------------------------------------------------------------------------
# In-memory test DB fixture
# ---------------------------------------------------------------------------


def _strip_tz(dt):
    """SQLite returns naive datetimes; Postgres returns tz-aware.
    Normalise so comparisons in tests don't have to care which
    backend they're running against."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.replace(tzinfo=None)


@pytest.fixture
def db():
    """Build an in-memory SQLite engine with just the tables this
    service touches. Avoids spinning up Postgres for unit tests.

    Note: SQLite's datetime handling is timezone-naive — we coerce
    on insert so the service sees timezone-aware datetimes either
    way. The production code path is identical.
    """
    engine = create_engine("sqlite://", future=True)
    # Create just the tables the service needs. SQLAlchemy's metadata
    # pulls in everything imported via app.database.models; we filter
    # to the three tables this test cares about so the DDL is fast
    # and doesn't depend on every adjacent model compiling cleanly.
    tables = [
        Tenant.__table__,
        ProxyLog.__table__,
        AuditExportDestination.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)

    Session = sessionmaker(bind=engine, future=True)
    session = Session()

    # Minimal tenant row so the FK on proxy_logs / audit_export_destinations resolves.
    session.add(Tenant(
        id="test-tenant",
        name="test-tenant",
        display_name="Test Tenant",
        primary_contact_email="test@example.invalid",
    ))
    session.flush()

    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _rows_are_exported_as_soon_as_they_are_written(monkeypatch):
    """A run leaves the newest moments for the next one (the class at the
    end of this file); the other tests export what they have just written."""
    from app.services.audit_export import service as module

    monkeypatch.setattr(module, "COMMIT_LAG_SECONDS", 0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_proxy_log(
    db, tenant_id: str = "test-tenant", *,
    timestamp: datetime,
    written_at: Optional[datetime] = None,
    proxy_type: str = "mcp",
    mcp_capability_name: Optional[str] = "read_record",
) -> ProxyLog:
    """Insert a minimal ProxyLog row. Only the columns the service
    actually serializes need to be set; everything else is fine as
    NULL or default. `timestamp` is when the call happened; `written_at`
    is when the gateway wrote the row (`created_at`), the same instant
    unless a test says otherwise."""
    row = ProxyLog(
        id=f"plog-{uuid.uuid4().hex[:12]}",
        tenant_id=tenant_id,
        timestamp=timestamp,
        created_at=written_at or timestamp,
        proxy_type=proxy_type,
        proxy_subtype="mcp_jsonrpc",
        end_user_session_id=f"sess-{uuid.uuid4().hex[:8]}",
        http_method="POST",
        endpoint="/v1/mcp/server/test",
        mcp_capability_name=mcp_capability_name,
        http_status_code=200,
        success=True,
    )
    db.add(row)
    db.flush()
    return row


def _make_destination(
    db,
    *,
    destination_type: str = DEST_TYPE_S3,
    config: Optional[Dict[str, Any]] = None,
    field_allowlist: Optional[List[str]] = None,
    last_exported_at: Optional[datetime] = None,
    interval_seconds: int = 3600,
) -> AuditExportDestination:
    """Insert a destination row with sensible defaults for testing.

    `config` defaults are PRE-ENCRYPTED — they look like what the API
    layer's `_encrypt_secrets_in_config` would have produced. The
    encryption service fixture decrypts them transparently in tests.
    """
    if config is None:
        if destination_type == DEST_TYPE_S3:
            config = {
                "bucket": "test-bucket",
                "prefix": "audit/proxy-logs",
                "region": "us-east-1",
                "access_key_id": "AKIATESTKEY",
                "secret_access_key_encrypted": "ENCRYPTED:secretkey",
            }
        else:
            config = {
                "url": "https://example.invalid/collector",
                "bearer_token_encrypted": "ENCRYPTED:bearer",
            }
    dest = AuditExportDestination(
        id=f"axd-{uuid.uuid4().hex[:12]}",
        tenant_id="test-tenant",
        name=f"dest-{uuid.uuid4().hex[:6]}",
        destination_type=destination_type,
        enabled=True,
        config=config,
        field_allowlist=field_allowlist,
        interval_seconds=interval_seconds,
        last_exported_at=last_exported_at,
    )
    db.add(dest)
    db.flush()
    return dest


class _StubEncryptionService:
    """Inverse of the API layer's `_encrypt_secrets_in_config` —
    accepts our test fixture's `ENCRYPTED:foo` markers and returns
    `foo`. Production code uses the real Fernet primitive; the unit
    tests don't need the actual crypto to round-trip, they just need
    the service to know what to call when a secret is needed.
    """

    def encrypt(self, plaintext: str) -> str:
        return f"ENCRYPTED:{plaintext}"

    def decrypt(self, ciphertext: str) -> str:
        if ciphertext.startswith("ENCRYPTED:"):
            return ciphertext[len("ENCRYPTED:"):]
        raise ValueError(f"Stub encryption can't decrypt: {ciphertext!r}")


@pytest.fixture
def encryption():
    return _StubEncryptionService()


@pytest.fixture
def captured_s3():
    """Mutable container for capturing S3 backend calls. Each test
    that needs to inspect the upload contents reads from this list.

    Patched onto `backends.SEND_S3` via the `service_with_stubs`
    fixture below.
    """
    return {"calls": [], "next_result": (True, None)}


@pytest.fixture
def captured_webhook():
    return {"calls": [], "next_result": (True, None)}


@pytest.fixture
def service_with_stubs(monkeypatch, encryption, captured_s3, captured_webhook):
    """Build a service whose backend calls land in `captured_s3` /
    `captured_webhook` instead of touching real S3 / HTTP."""

    def fake_s3(payload, object_key, **kwargs):
        captured_s3["calls"].append({
            "payload": payload, "object_key": object_key, "kwargs": kwargs,
        })
        return captured_s3["next_result"]

    def fake_webhook(payload, **kwargs):
        captured_webhook["calls"].append({"payload": payload, "kwargs": kwargs})
        return captured_webhook["next_result"]

    # Patch the module-level pointers the service indirects through.
    # See backends.py — `SEND_S3` and `SEND_WEBHOOK` exist precisely
    # so tests don't need to monkey-patch boto3 or httpx underneath.
    monkeypatch.setattr(
        "app.services.audit_export.backends.SEND_S3", fake_s3,
    )
    monkeypatch.setattr(
        "app.services.audit_export.backends.SEND_WEBHOOK", fake_webhook,
    )
    return encryption


# ---------------------------------------------------------------------------
# 1. Window bounds + high-water-mark advancement
# ---------------------------------------------------------------------------


class TestWindowAndHighWaterMark:
    def test_first_run_picks_up_all_existing_rows(
        self, db, service_with_stubs, captured_s3
    ):
        # last_exported_at IS NULL — should grab every row.
        now = datetime.now(timezone.utc)
        for i in range(5):
            _make_proxy_log(db, timestamp=now - timedelta(minutes=10 - i))
        dest = _make_destination(db, last_exported_at=None)

        svc = AuditExportService(db=db, encryption_service=service_with_stubs)
        result = svc.run_destination(dest.id)

        assert result.outcome is RunOutcome.SUCCESS
        assert result.rows_exported == 5
        # High-water mark advanced to the window end. (SQLite strips
        # tzinfo on read-back; Postgres preserves it. Compare against
        # the same wall-clock by stripping the result's tzinfo.)
        db.refresh(dest)
        assert dest.last_exported_at is not None
        assert _strip_tz(dest.last_exported_at) == _strip_tz(result.window_end)
        assert dest.last_run_status == STATUS_SUCCESS
        # And the S3 backend received exactly one payload.
        # Artifact + manifest sidecar (a144_pre_action_state).
        assert len(captured_s3["calls"]) == 2

    def test_subsequent_run_picks_up_only_new_rows(
        self, db, service_with_stubs, captured_s3
    ):
        # Half the rows are pre-existing; the other half are "new"
        # since `last_exported_at`. Only the new ones should ship.
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
        for i in range(3):
            _make_proxy_log(db, timestamp=cutoff - timedelta(minutes=i))
        for i in range(2):
            _make_proxy_log(db, timestamp=cutoff + timedelta(minutes=i + 1))
        dest = _make_destination(db, last_exported_at=cutoff)

        svc = AuditExportService(db=db, encryption_service=service_with_stubs)
        result = svc.run_destination(dest.id)

        assert result.outcome is RunOutcome.SUCCESS
        assert result.rows_exported == 2

    def test_no_data_advances_high_water_mark(self, db, service_with_stubs):
        # An empty window still moves the cutoff forward — otherwise
        # we'd re-scan the same empty range forever. Status reflects
        # "no_data" (distinct from "success" which implies bytes
        # were shipped) so the admin UI can show the right thing.
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
        dest = _make_destination(db, last_exported_at=cutoff)

        svc = AuditExportService(db=db, encryption_service=service_with_stubs)
        result = svc.run_destination(dest.id)

        assert result.outcome is RunOutcome.NO_DATA
        assert result.rows_exported == 0
        db.refresh(dest)
        assert _strip_tz(dest.last_exported_at) > _strip_tz(cutoff)
        assert dest.last_run_status == STATUS_NO_DATA

    def test_failure_does_not_advance_high_water_mark(
        self, db, service_with_stubs, captured_s3
    ):
        # At-least-once semantics — the next tick MUST retry the same
        # window. If we ever advance on failure, audit rows go missing
        # on the first network blip.
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
        for i in range(3):
            _make_proxy_log(db, timestamp=cutoff + timedelta(minutes=i + 1))
        dest = _make_destination(db, last_exported_at=cutoff)

        captured_s3["next_result"] = (False, "S3 Bucket does not exist")
        svc = AuditExportService(db=db, encryption_service=service_with_stubs)
        result = svc.run_destination(dest.id)

        assert result.outcome is RunOutcome.FAILURE
        assert result.error is not None and "Bucket does not exist" in result.error
        db.refresh(dest)
        assert _strip_tz(dest.last_exported_at) == _strip_tz(cutoff), (
            "Failure must leave last_exported_at unchanged so the "
            "next run retries the same window (at-least-once)."
        )
        assert dest.last_run_status == STATUS_FAILURE
        assert dest.last_run_error is not None


# ---------------------------------------------------------------------------
# 2. Field allowlist
# ---------------------------------------------------------------------------


class TestFieldAllowlist:
    def _decode_payload(self, payload: bytes) -> Tuple[Dict, List[Dict]]:
        """Decompress + parse the gzipped JSONL into (meta, records)."""
        raw = gzip.decompress(payload).decode("utf-8")
        lines = [json.loads(line) for line in raw.strip().split("\n")]
        return lines[0]["_meta"], lines[1:]

    def test_no_allowlist_ships_all_canonical_columns(
        self, db, service_with_stubs, captured_s3
    ):
        _make_proxy_log(db, timestamp=datetime.now(timezone.utc))
        dest = _make_destination(db, field_allowlist=None)

        svc = AuditExportService(db=db, encryption_service=service_with_stubs)
        svc.run_destination(dest.id)

        _meta, records = self._decode_payload(captured_s3["calls"][0]["payload"])
        # Every record carries the full PROXY_LOG_CSV_HEADERS set —
        # not just the columns we populated, but the full schema with
        # null values for the rest. That's the invariant the SIEM
        # side relies on (consistent schema, nullable fields).
        # a188_audit_chain_v3 — plus the `chain` block that makes the row verifiable.
        from app.services.admin_export import PROXY_LOG_CSV_HEADERS
        assert set(records[0].keys()) == set(PROXY_LOG_CSV_HEADERS) | {
            AuditExportService.CHAIN_BLOCK
        }

    def test_allowlist_strips_columns(self, db, service_with_stubs, captured_s3):
        _make_proxy_log(db, timestamp=datetime.now(timezone.utc))
        # Pretend an operator wants to scrub anything user-identifying.
        dest = _make_destination(
            db, field_allowlist=["id", "timestamp", "proxy_type", "status_code"],
        )

        svc = AuditExportService(db=db, encryption_service=service_with_stubs)
        svc.run_destination(dest.id)

        _meta, records = self._decode_payload(captured_s3["calls"][0]["payload"])
        assert set(records[0].keys()) == {"id", "timestamp", "proxy_type", "status_code"}

    def test_empty_allowlist_falls_back_to_no_filter(
        self, db, service_with_stubs, captured_s3
    ):
        # Explicit operator intent — empty list means "no filter",
        # not "drop everything". Belt-and-braces against the
        # operator who set `field_allowlist: []` thinking it would
        # strip all PII.
        _make_proxy_log(db, timestamp=datetime.now(timezone.utc))
        dest = _make_destination(db, field_allowlist=[])

        svc = AuditExportService(db=db, encryption_service=service_with_stubs)
        svc.run_destination(dest.id)

        _meta, records = self._decode_payload(captured_s3["calls"][0]["payload"])
        # Records have content (more than zero keys).
        assert len(records[0]) > 4

    def test_allowlist_with_only_unknown_columns_exports_nothing(
        self, db, service_with_stubs, captured_s3
    ):
        # An allow-list is there to keep columns from leaving. One that
        # names no existing column must not turn into "every column".
        _make_proxy_log(db, timestamp=datetime.now(timezone.utc))
        dest = _make_destination(db, field_allowlist=["this_field_doesnt_exist"])

        svc = AuditExportService(db=db, encryption_service=service_with_stubs)
        result = svc.run_destination(dest.id)

        assert captured_s3["calls"] == []
        assert result.outcome == RunOutcome.FAILURE
        assert dest.last_exported_at is None


# ---------------------------------------------------------------------------
# 3. Backend dispatch + payload shape
# ---------------------------------------------------------------------------


class TestBackendDispatch:
    def test_s3_destination_calls_s3_backend(
        self, db, service_with_stubs, captured_s3, captured_webhook, monkeypatch
    ):
        # Pin an unreachable proxy: sign_manifest defaults to
        # http://127.0.0.1:8100, and a developer running a local core
        # would otherwise sign the manifest and turn the "honestly
        # unsigned" assertion into a test of their laptop.
        monkeypatch.setenv("BRUTOR_GATEWAY_INTERNAL_URL", "http://127.0.0.1:1")
        _make_proxy_log(db, timestamp=datetime.now(timezone.utc))
        dest = _make_destination(db, destination_type=DEST_TYPE_S3)

        AuditExportService(
            db=db, encryption_service=service_with_stubs,
        ).run_destination(dest.id)

        # Two objects per S3 export since a144_pre_action_state: the gzipped artifact, then
        # its manifest sidecar. The manifest goes second on purpose — it
        # attests to an object that has already landed.
        assert len(captured_s3["calls"]) == 2
        assert len(captured_webhook["calls"]) == 0
        # S3 backend received the decrypted secret, NOT the ENCRYPTED:
        # marker. Service must round-trip through the encryption stub.
        kwargs = captured_s3["calls"][0]["kwargs"]
        assert kwargs["secret_access_key"] == "secretkey"
        assert kwargs["access_key_id"] == "AKIATESTKEY"

        # The manifest describes the artifact that preceded it.
        artifact = captured_s3["calls"][0]
        manifest_call = captured_s3["calls"][1]
        assert manifest_call["object_key"] == f"{artifact['object_key']}.manifest.json"

        manifest = json.loads(manifest_call["payload"])
        assert manifest["object_key"] == artifact["object_key"]
        assert manifest["byte_size"] == len(artifact["payload"])
        assert (
            manifest["sha256"]
            == hashlib.sha256(artifact["payload"]).hexdigest()
        ), "digest must be over the delivered bytes, so `sha256sum` verifies it"
        assert manifest["row_count"] == 1
        # No allowlist configured, so every row carries its chain block
        # (a188_audit_chain_v3) and a recipient can verify the rows independently. Before
        # a188_audit_chain_v3 this flag said True while the flattened rows carried no chain
        # column at all — the claim is now checked against the payload.
        assert manifest["chain_columns_included"] is True
        exported = json.loads(
            gzip.decompress(artifact["payload"]).decode().splitlines()[1]
        )
        assert {"chain_id", "chain_seq", "prev_hash", "row_hash", "chain_version"} <= set(
            exported["chain"]
        )
        # No proxy is reachable in the test process, so the manifest is
        # honestly unsigned rather than silently claiming to be signed.
        assert manifest["signed"] is False
        assert "unsigned_reason" in manifest
        assert len(manifest["unsigned_reason"]) < 300, "reason must stay bounded"

    def test_manifest_flags_an_allowlist_that_strips_the_chain_columns(
        self, db, service_with_stubs, captured_s3, captured_webhook
    ):
        # An operator can configure a field allowlist that removes row_hash
        # and friends, which silently makes the export unverifiable. The
        # manifest has to say so — a recipient cannot tell from the file.
        _make_proxy_log(db, timestamp=datetime.now(timezone.utc))
        dest = _make_destination(db, destination_type=DEST_TYPE_S3)
        dest.field_allowlist = ["id", "tenant_id", "timestamp"]
        db.commit()

        AuditExportService(
            db=db, encryption_service=service_with_stubs,
        ).run_destination(dest.id)

        manifest = json.loads(captured_s3["calls"][1]["payload"])
        assert manifest["chain_columns_included"] is False

    def test_webhook_destination_calls_webhook_backend(
        self, db, service_with_stubs, captured_s3, captured_webhook
    ):
        _make_proxy_log(db, timestamp=datetime.now(timezone.utc))
        dest = _make_destination(db, destination_type=DEST_TYPE_WEBHOOK)

        AuditExportService(
            db=db, encryption_service=service_with_stubs,
        ).run_destination(dest.id)

        assert len(captured_webhook["calls"]) == 1
        assert len(captured_s3["calls"]) == 0
        kwargs = captured_webhook["calls"][0]["kwargs"]
        assert kwargs["bearer_token"] == "bearer"

    def test_s3_object_key_follows_documented_format(
        self, db, service_with_stubs, captured_s3
    ):
        # Tests pin the S3 key shape because SIEM ingest pipelines
        # often build on the date partitioning to set retention /
        # Glacier transitions. Drift here would silently break
        # lifecycle policies that operators wrote against the v0.8.1
        # shape.
        _make_proxy_log(db, timestamp=datetime.now(timezone.utc))
        dest = _make_destination(db, destination_type=DEST_TYPE_S3)

        AuditExportService(
            db=db, encryption_service=service_with_stubs,
        ).run_destination(dest.id)

        key = captured_s3["calls"][0]["object_key"]
        # Format: `<prefix>/<tenant_id>/<YYYY>/<MM>/<DD>/<HHMMSS>.jsonl.gz`
        assert key.startswith("audit/proxy-logs/test-tenant/")
        assert key.endswith(".jsonl.gz")
        # Year/month/day partitioning present
        import re
        assert re.search(
            r"test-tenant/\d{4}/\d{2}/\d{2}/\d{6}\.jsonl\.gz$", key,
        )


class TestPayloadShape:
    def test_payload_is_gzipped_jsonl(
        self, db, service_with_stubs, captured_s3
    ):
        _make_proxy_log(db, timestamp=datetime.now(timezone.utc))
        dest = _make_destination(db)

        AuditExportService(
            db=db, encryption_service=service_with_stubs,
        ).run_destination(dest.id)

        payload = captured_s3["calls"][0]["payload"]
        # Gzip magic bytes
        assert payload[:2] == b"\x1f\x8b"
        # Decompresses to text starting with the metadata JSON line
        text = gzip.decompress(payload).decode("utf-8")
        first_line = text.split("\n", 1)[0]
        meta_obj = json.loads(first_line)
        assert "_meta" in meta_obj
        meta = meta_obj["_meta"]
        # Every key the SIEM pipeline relies on is present
        assert meta["resource"] == "proxy_logs"
        assert meta["tenant_id"] == "test-tenant"
        assert meta["destination_id"] == dest.id
        assert meta["row_count"] == 1
        assert "window_end" in meta

    def test_meta_line_carries_field_allowlist_for_audit_trail(
        self, db, service_with_stubs, captured_s3
    ):
        # When SIEM pipelines parse the payload, they sometimes need
        # to know what was redacted in this batch. Recording the
        # allowlist (or null) in the metadata is the cheap way.
        _make_proxy_log(db, timestamp=datetime.now(timezone.utc))
        allowlist = ["id", "timestamp", "proxy_type"]
        dest = _make_destination(db, field_allowlist=allowlist)

        AuditExportService(
            db=db, encryption_service=service_with_stubs,
        ).run_destination(dest.id)

        payload = captured_s3["calls"][0]["payload"]
        text = gzip.decompress(payload).decode("utf-8")
        meta = json.loads(text.split("\n", 1)[0])["_meta"]
        assert meta["field_allowlist"] == allowlist


# ---------------------------------------------------------------------------
# 4. Idempotency / edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_missing_destination_raises(self, db, service_with_stubs):
        # Programmer error (calling with the wrong id) should surface
        # loudly, not silently no-op.
        svc = AuditExportService(db=db, encryption_service=service_with_stubs)
        with pytest.raises(ValueError, match="not found"):
            svc.run_destination("axd-nonexistent")

    def test_s3_config_missing_bucket_returns_failure(
        self, db, service_with_stubs
    ):
        _make_proxy_log(db, timestamp=datetime.now(timezone.utc))
        dest = _make_destination(
            db,
            destination_type=DEST_TYPE_S3,
            config={
                "access_key_id": "AKIATESTKEY",
                "secret_access_key_encrypted": "ENCRYPTED:secretkey",
            },
        )
        svc = AuditExportService(db=db, encryption_service=service_with_stubs)
        result = svc.run_destination(dest.id)
        assert result.outcome is RunOutcome.FAILURE
        assert "bucket" in (result.error or "").lower()

    def test_unsupported_destination_type_returns_failure(
        self, db, service_with_stubs
    ):
        _make_proxy_log(db, timestamp=datetime.now(timezone.utc))
        dest = _make_destination(db)
        dest.destination_type = "future_sink_we_dont_support"
        db.flush()

        svc = AuditExportService(db=db, encryption_service=service_with_stubs)
        result = svc.run_destination(dest.id)
        assert result.outcome is RunOutcome.FAILURE
        assert "unsupported destination_type" in (result.error or "")


# ---------------------------------------------------------------------------
# The position is where the gateway had written to, not when the calls happened
# ---------------------------------------------------------------------------


class TestARowWrittenLateIsStillExported:
    """A row's `timestamp` is when the call started. The gateway writes the
    row afterwards: at once, after a long stream, after an approval that
    took hours, after a database outage. The export's position is therefore
    the time rows were written (`created_at`), and a run leaves the newest
    moments alone until the transactions that wrote them have ended."""

    @pytest.fixture(autouse=True)
    def _lag(self, monkeypatch):
        from app.services.audit_export import service as module

        monkeypatch.setattr(module, "COMMIT_LAG_SECONDS", 120)

    def test_a_call_that_started_before_the_position_and_was_written_after_it(
        self, db, service_with_stubs, captured_s3
    ):
        now = datetime.now(timezone.utc)
        position = now - timedelta(minutes=30)
        dest = _make_destination(db, last_exported_at=position)
        # The call started an hour before the last run; its row was written ten minutes ago.
        late = _make_proxy_log(db, timestamp=position - timedelta(hours=1), written_at=now - timedelta(minutes=10))

        result = AuditExportService(db=db, encryption_service=service_with_stubs).run_destination(dest.id)

        assert result.outcome is RunOutcome.SUCCESS and result.rows_exported == 1
        assert late.id in _exported_ids(captured_s3)

    def test_a_row_written_before_the_position_is_not_exported_again(self, db, service_with_stubs, captured_s3):
        now = datetime.now(timezone.utc)
        position = now - timedelta(minutes=30)
        dest = _make_destination(db, last_exported_at=position)
        # Whenever the call happened: the row was there at the last run.
        _make_proxy_log(db, timestamp=now - timedelta(minutes=5), written_at=position - timedelta(minutes=1))

        result = AuditExportService(db=db, encryption_service=service_with_stubs).run_destination(dest.id)
        assert result.outcome is RunOutcome.NO_DATA

    def test_the_newest_moments_wait_for_the_next_run(self, db, service_with_stubs, captured_s3):
        now = datetime.now(timezone.utc)
        dest = _make_destination(db, last_exported_at=now - timedelta(minutes=30))
        settled = _make_proxy_log(db, timestamp=now - timedelta(minutes=5))
        # Written within the last two minutes: a transaction that started
        # then may not have committed, so nothing of that moment is taken yet.
        fresh = _make_proxy_log(db, timestamp=now - timedelta(seconds=20))

        svc = AuditExportService(db=db, encryption_service=service_with_stubs)
        first = svc.run_destination(dest.id)
        assert first.rows_exported == 1 and _exported_ids(captured_s3) == {settled.id}
        db.refresh(dest)
        # The position stopped short of the newest moments.
        assert _strip_tz(dest.last_exported_at) < _strip_tz(now - timedelta(seconds=100))
        assert _strip_tz(dest.last_exported_at) >= _strip_tz(now - timedelta(seconds=140))

        # Later (here: the same row, seen from a run for which it is old enough).
        from app.services.audit_export import service as module

        module.COMMIT_LAG_SECONDS = 0
        captured_s3["calls"].clear()
        second = svc.run_destination(dest.id)
        assert second.rows_exported == 1 and _exported_ids(captured_s3) == {fresh.id}


def _exported_ids(captured_s3) -> set:
    import gzip as _gzip
    import json as _json

    ids = set()
    for call in captured_s3["calls"]:
        if call["object_key"].endswith(".manifest.json"):
            continue
        for line in _gzip.decompress(call["payload"]).decode("utf-8").splitlines():
            record = _json.loads(line)
            if "_meta" not in record:
                ids.add(record["id"])
    return ids
