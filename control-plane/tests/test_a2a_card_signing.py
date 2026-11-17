# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Service-layer tests for the A2A Agent Card signing service
(app/services/a2a/card_signing.py).

Covered:
  * canonical_json determinism — same logical payload, same bytes,
    regardless of key order at input
  * keypair generation + idempotency (get_or_create returns the same
    pair on the second call)
  * sign → verify round-trip (tenant_signed trust tier, signature
    base64url, timestamp, signed_by)
  * tampering detection (body mutation invalidates signature)
  * revocation is terminal — revoked card cannot be re-signed, and
    verify always returns False for revoked
  * capability denormalisation (duplicate names kept-first, nameless
    rows skipped, non-list capabilities tolerated with a warning)
  * validate_card_json rejects missing required fields

No DB fixture — we use an in-memory SQLite via StaticPool so the
signing service can actually flush rows. Keeps the tests realistic
without the full FastAPI harness.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from ulid import ULID

from app.core.logging_system import BrutorLogger
from app.database import Base
from app.database.models.agent_card import (
    AgentCard,
    TRUST_TIER_REVOKED,
    TRUST_TIER_TENANT_SIGNED,
    TRUST_TIER_UNSIGNED,
)
from app.database.models.agent_card_capability import AgentCardCapability
from app.database.models.tenant import Tenant
from app.database.models.tenant_a2a_key import TenantA2AKey
from app.services.a2a.card_signing import (
    A2ASigningError,
    InvalidCardError,
    canonical_json,
    get_or_create_tenant_key,
    revoke_card,
    sign_card,
    sync_capabilities,
    validate_card_json,
    verify_card_signature,
)
from app.services.oauth.encryption_service import EncryptionService


REQUIRED_TABLES = [
    Tenant.__table__,
    AgentCard.__table__,
    AgentCardCapability.__table__,
    TenantA2AKey.__table__,
]


@pytest.fixture()
def db_session() -> Session:
    """In-memory SQLite DB with just the tables the signing service touches."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine, tables=REQUIRED_TABLES)
    Factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    s = Factory()
    try:
        # Seed one tenant so FKs resolve.
        s.add(
            Tenant(
                id="t1",
                name="t1",
                display_name="Tenant One",
                primary_contact_email="admin@t1.example",
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
def encryption_service() -> EncryptionService:
    from app.services.config_manager import ConfigManager
    logger = BrutorLogger("test-enc")
    return EncryptionService(
        config_manager=ConfigManager(logger=logger, validate=False),
        logger=logger,
    )


def _make_card(db: Session, *, name: str = "billing-agent", capabilities: list = None) -> AgentCard:
    """Persist a minimal valid A2A card row for signing tests."""
    caps = capabilities if capabilities is not None else [
        {"name": "billing.refund", "description": "Issue refunds"},
        {"name": "billing.invoice.read", "description": "Read invoices"},
    ]
    card = AgentCard(
        id=f"agentcard-{ULID()}",
        tenant_id="t1",
        name=name,
        version="1.0.0",
        card_json={
            "name": name,
            "description": "Test agent",
            "version": "1.0.0",
            "capabilities": caps,
        },
        trust_tier=TRUST_TIER_UNSIGNED,
        enabled=True,
    )
    db.add(card)
    db.flush()
    return card


# =============================================================================
# canonical_json
# =============================================================================


class TestCanonicalJSON:
    def test_sorted_keys_produce_identical_bytes(self):
        a = canonical_json({"z": 1, "a": 2, "m": 3})
        b = canonical_json({"a": 2, "m": 3, "z": 1})
        assert a == b

    def test_nested_dict_keys_also_sorted(self):
        a = canonical_json({"outer": {"z": 1, "a": 2}})
        b = canonical_json({"outer": {"a": 2, "z": 1}})
        assert a == b

    def test_no_whitespace(self):
        out = canonical_json({"a": 1, "b": 2})
        assert b" " not in out
        assert out == b'{"a":1,"b":2}'

    def test_unicode_preserved(self):
        out = canonical_json({"name": "café"})
        assert "café".encode("utf-8") in out


# =============================================================================
# get_or_create_tenant_key
# =============================================================================


class TestKeyGeneration:
    def test_creates_key_on_first_call(self, db_session, encryption_service):
        kp = get_or_create_tenant_key(db_session, "t1", encryption_service)
        assert kp.tenant_id == "t1"
        assert kp.public_key_b64
        # Row persisted
        row = db_session.query(TenantA2AKey).filter_by(tenant_id="t1").first()
        assert row is not None
        assert row.key_algorithm == "ed25519"

    def test_idempotent_returns_same_key(self, db_session, encryption_service):
        kp1 = get_or_create_tenant_key(db_session, "t1", encryption_service)
        db_session.commit()
        kp2 = get_or_create_tenant_key(db_session, "t1", encryption_service)
        assert kp1.public_key_b64 == kp2.public_key_b64

    def test_private_key_encrypted_at_rest(self, db_session, encryption_service):
        get_or_create_tenant_key(db_session, "t1", encryption_service)
        db_session.commit()
        row = db_session.query(TenantA2AKey).filter_by(tenant_id="t1").first()
        # Stored value should not equal the plaintext base64url of a raw
        # Ed25519 key (32 bytes → 43 chars base64url). Fernet ciphertext
        # is longer and starts with "gAAAAA".
        assert row.private_key_encrypted.startswith("gAAAAA")


# =============================================================================
# sign_card + verify_card_signature
# =============================================================================


class TestSignVerify:
    def test_sign_sets_trust_tier_and_metadata(self, db_session, encryption_service):
        card = _make_card(db_session)
        sign_card(db_session, card, signed_by="alice",
                  encryption_service=encryption_service)

        assert card.trust_tier == TRUST_TIER_TENANT_SIGNED
        assert card.signature_algorithm == "ed25519"
        assert card.signed_by == "alice"
        assert card.signed_at is not None
        assert card.signature is not None

    def test_verify_round_trip(self, db_session, encryption_service):
        card = _make_card(db_session)
        sign_card(db_session, card, signed_by="alice",
                  encryption_service=encryption_service)
        assert verify_card_signature(db_session, card) is True

    def test_tampered_body_fails_verification(self, db_session, encryption_service):
        card = _make_card(db_session)
        sign_card(db_session, card, signed_by="alice",
                  encryption_service=encryption_service)
        # Mutate the card body without re-signing — signature must no
        # longer verify.
        card.card_json = {**card.card_json, "description": "TAMPERED"}
        assert verify_card_signature(db_session, card) is False

    def test_unsigned_card_returns_false(self, db_session, encryption_service):
        card = _make_card(db_session)
        assert verify_card_signature(db_session, card) is False

    def test_unknown_algorithm_returns_false(self, db_session, encryption_service):
        card = _make_card(db_session)
        sign_card(db_session, card, signed_by="alice",
                  encryption_service=encryption_service)
        card.signature_algorithm = "rsa-2048"   # simulate partner-signed
        assert verify_card_signature(db_session, card) is False


# =============================================================================
# Revocation
# =============================================================================


class TestRevocation:
    def test_revoke_marks_terminal_and_disables(self, db_session, encryption_service):
        card = _make_card(db_session)
        sign_card(db_session, card, signed_by="alice",
                  encryption_service=encryption_service)
        revoke_card(db_session, card, reason="Key compromise", revoked_by="bob")

        assert card.trust_tier == TRUST_TIER_REVOKED
        assert card.enabled is False
        assert card.revoked_at is not None
        assert card.revoked_reason == "Key compromise"

    def test_revoked_card_cannot_be_resigned(self, db_session, encryption_service):
        card = _make_card(db_session)
        sign_card(db_session, card, signed_by="alice",
                  encryption_service=encryption_service)
        revoke_card(db_session, card, reason="Key compromise", revoked_by="bob")

        with pytest.raises(A2ASigningError, match="revoked"):
            sign_card(db_session, card, signed_by="alice",
                      encryption_service=encryption_service)

    def test_revoked_card_verify_returns_false_even_with_valid_signature(
        self, db_session, encryption_service,
    ):
        card = _make_card(db_session)
        sign_card(db_session, card, signed_by="alice",
                  encryption_service=encryption_service)
        # At this point verify would be True. Revoke, and then verify
        # must be False — revocation overrides cryptographic validity.
        assert verify_card_signature(db_session, card) is True
        revoke_card(db_session, card, reason="rotation", revoked_by="bob")
        assert verify_card_signature(db_session, card) is False

    def test_is_trusted_property_honours_revocation(self, db_session, encryption_service):
        card = _make_card(db_session)
        sign_card(db_session, card, signed_by="alice",
                  encryption_service=encryption_service)
        assert card.is_trusted is True
        revoke_card(db_session, card, reason="rotation", revoked_by="bob")
        assert card.is_trusted is False


# =============================================================================
# Capability denormalisation
# =============================================================================


class TestSyncCapabilities:
    def test_creates_rows_from_card_json(self, db_session):
        card = _make_card(db_session)
        rows = sync_capabilities(db_session, card)
        assert len(rows) == 2
        names = sorted(r.capability_name for r in rows)
        assert names == ["billing.invoice.read", "billing.refund"]

    def test_regenerates_on_second_call(self, db_session):
        card = _make_card(db_session)
        sync_capabilities(db_session, card)
        # Mutate: drop one, add a new one.
        card.card_json = {
            **card.card_json,
            "capabilities": [{"name": "billing.refund"}, {"name": "audit.log.read"}],
        }
        rows = sync_capabilities(db_session, card)
        names = sorted(r.capability_name for r in rows)
        assert names == ["audit.log.read", "billing.refund"]

    def test_skips_nameless_capability(self, db_session):
        card = _make_card(
            db_session,
            capabilities=[{"name": "valid.one"}, {"description": "no name here"}],
        )
        rows = sync_capabilities(db_session, card)
        assert len(rows) == 1
        assert rows[0].capability_name == "valid.one"

    def test_duplicate_names_keep_first(self, db_session):
        card = _make_card(
            db_session,
            capabilities=[
                {"name": "billing.refund", "description": "first"},
                {"name": "billing.refund", "description": "duplicate"},
            ],
        )
        rows = sync_capabilities(db_session, card)
        assert len(rows) == 1
        assert rows[0].description == "first"

    def test_non_list_capabilities_tolerated(self, db_session):
        card = _make_card(db_session)
        # Simulate a malformed import.
        card.card_json = {**card.card_json, "capabilities": "not-a-list"}
        rows = sync_capabilities(db_session, card)
        assert rows == []

    # ------------------------------------------------------------------
    # handler_type / handler_ref denormalisation
    # ------------------------------------------------------------------

    def test_handler_fields_persisted_from_card_json(self, db_session):
        card = _make_card(
            db_session,
            capabilities=[
                {
                    "name": "billing.refund",
                    "handler_type": "skill",
                    "handler_ref": "billing-refund-skill",
                },
                {
                    "name": "billing.invoice.read",
                    "handler_type": "noop",
                },
            ],
        )
        rows = sync_capabilities(db_session, card)
        by_name = {r.capability_name: r for r in rows}
        assert by_name["billing.refund"].handler_type == "skill"
        assert by_name["billing.refund"].handler_ref == "billing-refund-skill"
        assert by_name["billing.invoice.read"].handler_type == "noop"
        assert by_name["billing.invoice.read"].handler_ref is None

    def test_handler_fields_null_when_absent(self, db_session):
        card = _make_card(db_session)  # Default caps have no handler fields.
        rows = sync_capabilities(db_session, card)
        for r in rows:
            assert r.handler_type is None
            assert r.handler_ref is None

    def test_whitespace_handler_ref_normalized_to_null(self, db_session):
        card = _make_card(
            db_session,
            capabilities=[
                {
                    "name": "billing.refund",
                    "handler_type": "skill",
                    "handler_ref": "   ",
                },
            ],
        )
        rows = sync_capabilities(db_session, card)
        assert rows[0].handler_type == "skill"
        assert rows[0].handler_ref is None

    def test_invalid_handler_type_skipped_not_500(self, db_session):
        # Validator on AgentCardCapability rejects unknown handler_type
        # with ValueError; sync_capabilities catches and drops the row
        # rather than bubbling a 500 up from the admin API.
        card = _make_card(
            db_session,
            capabilities=[
                {"name": "billing.refund", "handler_type": "bogus"},
                {"name": "billing.invoice.read", "handler_type": "skill", "handler_ref": "s"},
            ],
        )
        rows = sync_capabilities(db_session, card)
        assert len(rows) == 1
        assert rows[0].capability_name == "billing.invoice.read"

    def test_non_string_handler_type_ignored(self, db_session):
        # A card imported from a loose JSON source might have
        # handler_type=null or a dict. Non-string input is silently
        # dropped to null — fail-open on the dispatch side (handler
        # runs as noop) is safer than rejecting the whole card.
        card = _make_card(
            db_session,
            capabilities=[
                {"name": "billing.refund", "handler_type": {"kind": "skill"}},
            ],
        )
        rows = sync_capabilities(db_session, card)
        assert len(rows) == 1
        assert rows[0].handler_type is None


# =============================================================================
# validate_card_json
# =============================================================================


class TestValidateCardJSON:
    def test_valid_card_passes(self):
        validate_card_json({
            "name": "x",
            "description": "y",
            "version": "1.0.0",
            "capabilities": [],
        })

    def test_missing_name_rejected(self):
        with pytest.raises(InvalidCardError, match="missing"):
            validate_card_json({
                "description": "y", "version": "1", "capabilities": [],
            })

    def test_non_dict_rejected(self):
        with pytest.raises(InvalidCardError, match="JSON object"):
            validate_card_json(["not", "a", "dict"])

    def test_non_list_capabilities_rejected(self):
        with pytest.raises(InvalidCardError, match="capabilities"):
            validate_card_json({
                "name": "x",
                "description": "y",
                "version": "1",
                "capabilities": "oops",
            })
