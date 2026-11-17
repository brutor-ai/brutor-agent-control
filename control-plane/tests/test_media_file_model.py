# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the ``media_files`` ORM model.

The multimodal Files API keeps a tenant-scoped registry
of media assets stored in object storage. Wire references go via
stable ``brutor_file_id`` and signed-URL issuance through the proxy;
the row drives quota enforcement, retention/GC, and cost rollups.

These tests pin the contract that the rest of the multimodal work
builds on:

  1. The table exists on ``Base.metadata`` and the columns the
     gateway-core proxy expects are wired through with the right
     types (BigInt for size_bytes, NOT NULL on retention deadline,
     soft-delete column nullable).
  2. ``is_alive`` correctly composes the soft-delete and
     retention-deadline gates — this is what signed-URL issuance
     calls; getting it wrong leaks expired files.
  3. ``soft_delete`` flips the marker without touching anything else
     (the GC sweep is the only thing allowed to hard-delete).
  4. ``to_dict`` deliberately omits ``object_key`` /
     ``storage_bucket_override`` — those are server-internal and
     leaking them would let a client probe bucket layouts.

Run: pytest tests/test_media_file_model.py -v
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "x")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import BigInteger, DateTime, Integer, String

from app.database.models.media_file import MediaFile
from app.database.models.tenant import Tenant


# ---------------------------------------------------------------------------
# 1. Table is registered + critical columns wire through
# ---------------------------------------------------------------------------

def test_media_files_table_in_base_metadata():
    """Without the import in models/__init__.py, fresh-DB
    create_all() silently skips the table."""
    from app.database import Base

    assert "media_files" in Base.metadata.tables, (
        "media_files missing from Base.metadata — check the import "
        "in app/database/models/__init__.py"
    )


def test_required_columns_present_with_correct_types():
    """The runtime path (gateway-core) reads these columns directly.
    A type mismatch (e.g. Integer instead of BigInteger for
    size_bytes) silently caps the byte counter at 2 GB and breaks
    quota math the moment a tenant uploads a single video."""
    cols = {c.name: c for c in MediaFile.__table__.columns}
    expected = {
        "id",
        "tenant_id",
        "kind",
        "mime_type",
        "size_bytes",
        "content_hash",
        "object_key",
        "storage_bucket_override",
        "chat_id",
        "model_id",
        "actor_id",
        "display_name",
        "created_at",
        "expires_at",
        "deleted_at",
        "access_count",
        "last_accessed_at",
    }
    missing = expected - cols.keys()
    assert not missing, f"media_files is missing columns: {sorted(missing)}"

    # Type sanity — the columns the gateway-core path *cares* about.
    assert isinstance(cols["size_bytes"].type, BigInteger), (
        "size_bytes must be BigInteger; Integer caps quota maths at 2 GB."
    )
    assert isinstance(cols["access_count"].type, Integer), (
        "access_count is signed-URL issuance count, fits in Integer."
    )
    assert isinstance(cols["expires_at"].type, DateTime), (
        "expires_at must be DateTime; the GC sweep relies on it."
    )
    # NOT NULL invariants the GC sweep depends on.
    assert cols["expires_at"].nullable is False, (
        "expires_at must be NOT NULL — GC sweep ranges on it; a NULL "
        "row would slip through every retention pass."
    )
    assert cols["tenant_id"].nullable is False
    assert cols["size_bytes"].nullable is False
    assert cols["object_key"].nullable is False
    # Soft-delete column has to be nullable (NULL == alive).
    assert cols["deleted_at"].nullable is True


def test_indexes_match_expected_query_paths():
    """The four indexes correspond to the four hot queries:
    quota-sum (tenant_active), GC sweep (lifecycle), 'delete this
    chat' fanout (chat), per-model cost rollup (model). If any
    drops, the corresponding code path goes from O(log n) to seq
    scan on a multi-million-row table."""
    indexes = {idx.name: idx for idx in MediaFile.__table__.indexes}
    assert "ix_media_files_tenant_active" in indexes
    assert "ix_media_files_lifecycle" in indexes
    assert "ix_media_files_chat" in indexes
    assert "ix_media_files_model" in indexes


def test_foreign_keys_are_correct():
    """tenant_id MUST cascade-delete (operator deletes a tenant ->
    every media row goes away). model_id MUST set-null (deleting a
    model definition shouldn't strand audit history)."""
    fks_by_target = {}
    for col in MediaFile.__table__.columns:
        for fk in col.foreign_keys:
            fks_by_target[fk.column.table.name] = fk

    assert "tenants" in fks_by_target
    assert fks_by_target["tenants"].ondelete == "CASCADE"

    assert "llm_model" in fks_by_target
    assert fks_by_target["llm_model"].ondelete == "SET NULL"


# ---------------------------------------------------------------------------
# 2. is_alive composes soft-delete + retention deadline
# ---------------------------------------------------------------------------

def _make_row(*, expires_at=None, deleted_at=None) -> MediaFile:
    return MediaFile(
        id="file_test",
        tenant_id="t-test",
        kind="image",
        mime_type="image/png",
        size_bytes=12345,
        object_key="t-test/image/2026/04/file_test.png",
        created_at=datetime.now(timezone.utc),
        expires_at=expires_at or (datetime.now(timezone.utc) + timedelta(days=30)),
        deleted_at=deleted_at,
    )


def test_is_alive_true_for_fresh_undeleted_row():
    row = _make_row()
    assert row.is_alive is True


def test_is_alive_false_when_soft_deleted():
    row = _make_row(deleted_at=datetime.now(timezone.utc))
    assert row.is_alive is False, (
        "Soft-deleted rows must report not-alive even before retention "
        "deadline — signed-URL issuance gates on this. A leak here "
        "means a 'delete this chat' button doesn't actually stop the "
        "image from being re-fetched."
    )


def test_is_alive_false_after_retention_deadline():
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    row = _make_row(expires_at=past)
    assert row.is_alive is False


def test_is_alive_handles_naive_datetimes():
    """Postgres returns timezone-aware datetimes, but tests sometimes
    construct naive ones — and an unguarded comparison would raise
    TypeError. The model coerces to UTC."""
    naive_future = datetime.utcnow() + timedelta(days=1)
    row = _make_row(expires_at=naive_future)
    assert row.is_alive is True


# ---------------------------------------------------------------------------
# 3. soft_delete flips the marker, doesn't touch anything else
# ---------------------------------------------------------------------------

def test_soft_delete_sets_marker_only():
    row = _make_row()
    original_expires = row.expires_at
    original_size = row.size_bytes
    row.soft_delete()
    assert row.deleted_at is not None
    # GC sweep needs both columns intact to know when to hard-delete.
    assert row.expires_at == original_expires
    assert row.size_bytes == original_size


# ---------------------------------------------------------------------------
# 4. to_dict omits server-internal fields
# ---------------------------------------------------------------------------

def test_to_dict_excludes_object_key_and_bucket():
    """object_key and storage_bucket_override are server-internal —
    leaking them lets a client probe bucket layouts and try direct
    fetches that bypass tenant isolation. The proxy's signed-URL
    issuance path is the only legitimate way to access bytes."""
    row = MediaFile(
        id="file_x",
        tenant_id="t-x",
        kind="image",
        mime_type="image/png",
        size_bytes=1024,
        object_key="t-x/image/2026/04/file_x.png",
        storage_bucket_override="brutor-media-east",
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        created_at=datetime.now(timezone.utc),
        access_count=0,
    )
    d = row.to_dict()
    assert "object_key" not in d
    assert "storage_bucket_override" not in d
    # Sanity — the things we DO expose:
    assert d["id"] == "file_x"
    assert d["tenant_id"] == "t-x"
    assert d["kind"] == "image"
    assert d["size_bytes"] == 1024
    assert d["access_count"] == 0


# ---------------------------------------------------------------------------
# 5. Tenant.media_storage_quota_bytes
# ---------------------------------------------------------------------------

def test_tenant_has_media_storage_quota_column():
    """Quota lives on Tenant so the upload path can SELECT it once
    per request alongside the existing tenant load — no second
    join."""
    cols = {c.name: c for c in Tenant.__table__.columns}
    assert "media_storage_quota_bytes" in cols
    col = cols["media_storage_quota_bytes"]
    assert isinstance(col.type, BigInteger)
    assert col.nullable is True, "NULL = unlimited (self-hosted default)"


def test_tenant_get_quotas_surfaces_media_storage():
    """The admin UI's quota panel iterates this dict — leaving the
    field out is a silent UI-side miss (the field is in the DB but
    invisible to operators)."""
    t = Tenant(
        id="t-q",
        name="t-q",
        display_name="Test",
        primary_contact_email="ops@example.com",
        media_storage_quota_bytes=10 * 1024 * 1024 * 1024,  # 10 GB
    )
    quotas = t.get_quotas()
    assert "media_storage_quota_bytes" in quotas
    assert quotas["media_storage_quota_bytes"] == 10 * 1024 * 1024 * 1024
