# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Catalog trust tiers + Ed25519 signatures

Locks down `sign_catalog_entry()` and `verify_catalog_entry_signature()`
— the sign/verify pair that turns a `mcp_catalog_entries` row into
a tenant-attested artefact reusable across tenants who trust each
others' signing keys.

# What's under test

  1. Round-trip: sign + verify on an unmodified entry succeeds.
  2. Tamper detection: change a field that's in the signing body
     → verify fails.
  3. `enabled` toggle does NOT invalidate signatures (deliberate
     escape hatch for operators).
  4. `icon_data` mutation does NOT invalidate (large binary,
     content-addressed via icon_hash).
  5. Revoked entries verify=False even with a valid signature.
  6. Sign mutates trust_tier → tenant_signed; revoke → revoked.
  7. Re-signing a revoked entry is rejected (terminal state).
  8. The signing-key generation reuses the tenant's A2A signing
     key — operators don't need a separate catalog key.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.database.models.mcp_catalog_entry import (
    MCPCatalogEntry,
    TRUST_TIER_REVOKED,
    TRUST_TIER_TENANT_SIGNED,
    TRUST_TIER_UNSIGNED,
)
from app.database.models.tenant import Tenant
from app.database.models.tenant_a2a_key import TenantA2AKey
from app.services.mcp.catalog_signing import (
    CatalogSigningError,
    revoke_catalog_entry,
    sign_catalog_entry,
    verify_catalog_entry_signature,
)


# ---------------------------------------------------------------------------
# Stub EncryptionService — same pattern as the audit-export tests.
# Real Fernet encryption is exercised by the A2A signing test suite;
# here we just want sign/verify to round-trip through a known
# encryption stub.
# ---------------------------------------------------------------------------


class _StubEncryptionService:
    """Stub that base64-encodes the input. The A2A signing path
    decodes via `_b64url_decode` (URL-safe base64), so the stub's
    `encrypt()` MUST produce something that round-trips correctly.
    Easiest: just pass the value through unchanged — the private
    key is already base64'd before reaching here."""

    def encrypt(self, plaintext: str) -> str:
        return plaintext  # no-op; tests don't care about ciphertext

    def decrypt(self, ciphertext: str) -> str:
        return ciphertext


@pytest.fixture
def db():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(
        engine,
        tables=[
            Tenant.__table__,
            TenantA2AKey.__table__,
            MCPCatalogEntry.__table__,
        ],
    )
    Session = sessionmaker(bind=engine, future=True)
    session = Session()
    session.add(Tenant(
        id="t1", name="t1", display_name="t1",
        primary_contact_email="t1@example.invalid",
    ))
    session.flush()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def encryption():
    return _StubEncryptionService()


def _seed_entry(db, **overrides) -> MCPCatalogEntry:
    defaults = {
        "id": "mcp-cat-test-1",
        "tenant_id": "t1",
        "name": "Test Server",
        "slug": "test-server",
        "short_description": "Test",
        "runtime": "remote",
        "auth_pattern": "none",
        "source": "system",
        "enabled": True,
    }
    defaults.update(overrides)
    entry = MCPCatalogEntry(**defaults)
    db.add(entry)
    db.flush()
    return entry


# ---------------------------------------------------------------------------
# 1. Round-trip sign + verify
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_sign_then_verify_returns_true(self, db, encryption):
        entry = _seed_entry(db)
        sign_catalog_entry(
            db=db, entry=entry, signed_by="admin-1",
            encryption_service=encryption,
        )
        db.commit()
        assert verify_catalog_entry_signature(db=db, entry=entry) is True
        assert entry.trust_tier == TRUST_TIER_TENANT_SIGNED
        assert entry.signature is not None
        assert len(entry.signature) > 40  # base64-encoded 64-byte signature
        assert entry.signature_algorithm == "ed25519"
        assert entry.signed_by == "admin-1"
        assert entry.signed_at is not None

    def test_unsigned_entry_verifies_false(self, db, encryption):
        entry = _seed_entry(db)
        assert verify_catalog_entry_signature(db=db, entry=entry) is False


# ---------------------------------------------------------------------------
# 2. Tamper detection
# ---------------------------------------------------------------------------


class TestTamperDetection:
    def test_changing_name_invalidates_signature(self, db, encryption):
        # The whole point of the signature: if an attacker (or
        # accidental edit) changes the name, verify must reject.
        entry = _seed_entry(db, name="Original")
        sign_catalog_entry(
            db=db, entry=entry, signed_by="admin-1",
            encryption_service=encryption,
        )
        db.commit()
        assert verify_catalog_entry_signature(db=db, entry=entry) is True

        entry.name = "Tampered"
        db.flush()
        assert verify_catalog_entry_signature(db=db, entry=entry) is False

    def test_changing_runtime_invalidates_signature(self, db, encryption):
        entry = _seed_entry(db, runtime="remote")
        sign_catalog_entry(
            db=db, entry=entry, signed_by="admin-1",
            encryption_service=encryption,
        )
        db.commit()
        entry.runtime = "containerized"
        assert verify_catalog_entry_signature(db=db, entry=entry) is False

    def test_changing_env_vars_invalidates_signature(self, db, encryption):
        # env_vars is a list of dicts; ordering matters because
        # canonical_json sorts top-level keys but preserves list
        # order. Adding / removing / reordering env_vars must
        # invalidate.
        entry = _seed_entry(db, env_vars=[{"key": "API_KEY", "required": True}])
        sign_catalog_entry(
            db=db, entry=entry, signed_by="admin-1",
            encryption_service=encryption,
        )
        db.commit()
        entry.env_vars = [{"key": "API_KEY", "required": False}]
        assert verify_catalog_entry_signature(db=db, entry=entry) is False


# ---------------------------------------------------------------------------
# 3. Deliberate escape hatches
# ---------------------------------------------------------------------------


class TestEscapeHatches:
    def test_enabled_toggle_does_not_invalidate(self, db, encryption):
        # Operators must be able to disable a verified entry
        # WITHOUT re-signing — the signature attests to the
        # entry's definition, not its activation state.
        entry = _seed_entry(db, enabled=True)
        sign_catalog_entry(
            db=db, entry=entry, signed_by="admin-1",
            encryption_service=encryption,
        )
        db.commit()
        entry.enabled = False
        assert verify_catalog_entry_signature(db=db, entry=entry) is True

    def test_icon_data_change_does_not_invalidate(self, db, encryption):
        # `icon_data` is a large binary; we content-address it via
        # `icon_hash` (which IS in the signing body). Operators can
        # re-upload an icon without breaking the signature as long
        # as the hash matches.
        entry = _seed_entry(db, icon_data=b"old", icon_hash="abc123")
        sign_catalog_entry(
            db=db, entry=entry, signed_by="admin-1",
            encryption_service=encryption,
        )
        db.commit()
        entry.icon_data = b"new"  # binary changes but hash stays
        assert verify_catalog_entry_signature(db=db, entry=entry) is True

    def test_icon_hash_change_DOES_invalidate(self, db, encryption):
        # ...but if the hash changes (i.e. the icon content actually
        # changed), THAT must invalidate. The hash IS part of the
        # signing body.
        entry = _seed_entry(db, icon_data=b"old", icon_hash="abc123")
        sign_catalog_entry(
            db=db, entry=entry, signed_by="admin-1",
            encryption_service=encryption,
        )
        db.commit()
        entry.icon_hash = "def456"
        assert verify_catalog_entry_signature(db=db, entry=entry) is False


# ---------------------------------------------------------------------------
# 4. Revocation
# ---------------------------------------------------------------------------


class TestRevocation:
    def test_revoke_terminates_trust(self, db, encryption):
        entry = _seed_entry(db)
        sign_catalog_entry(
            db=db, entry=entry, signed_by="admin-1",
            encryption_service=encryption,
        )
        db.commit()
        revoke_catalog_entry(db=db, entry=entry, revoked_by="admin-2")
        db.commit()

        assert entry.trust_tier == TRUST_TIER_REVOKED
        assert verify_catalog_entry_signature(db=db, entry=entry) is False
        # signature itself is still present (forensic value), but
        # the trust_tier gates verify.
        assert entry.signature is not None

    def test_revoked_entries_cannot_be_re_signed(self, db, encryption):
        entry = _seed_entry(db)
        sign_catalog_entry(
            db=db, entry=entry, signed_by="admin-1",
            encryption_service=encryption,
        )
        db.commit()
        revoke_catalog_entry(db=db, entry=entry, revoked_by="admin-2")
        db.commit()

        with pytest.raises(CatalogSigningError, match="revoked"):
            sign_catalog_entry(
                db=db, entry=entry, signed_by="admin-3",
                encryption_service=encryption,
            )


# ---------------------------------------------------------------------------
# 5. Tenant signing key is shared with A2A
# ---------------------------------------------------------------------------


class TestSharedSigningKey:
    def test_first_sign_creates_tenant_signing_key(self, db, encryption):
        # No TenantA2AKey row exists yet.
        assert db.query(TenantA2AKey).count() == 0
        entry = _seed_entry(db)
        sign_catalog_entry(
            db=db, entry=entry, signed_by="admin-1",
            encryption_service=encryption,
        )
        db.commit()
        # Signing created the tenant's keypair.
        assert db.query(TenantA2AKey).filter_by(tenant_id="t1").count() == 1

    def test_subsequent_signs_reuse_existing_key(self, db, encryption):
        entry_a = _seed_entry(db, id="ent-a", slug="ent-a")
        entry_b = _seed_entry(db, id="ent-b", slug="ent-b")

        sign_catalog_entry(
            db=db, entry=entry_a, signed_by="admin-1",
            encryption_service=encryption,
        )
        sign_catalog_entry(
            db=db, entry=entry_b, signed_by="admin-1",
            encryption_service=encryption,
        )
        db.commit()
        # Still exactly one signing key per tenant — both entries
        # signed by the same key.
        assert db.query(TenantA2AKey).filter_by(tenant_id="t1").count() == 1
        assert verify_catalog_entry_signature(db=db, entry=entry_a) is True
        assert verify_catalog_entry_signature(db=db, entry=entry_b) is True
