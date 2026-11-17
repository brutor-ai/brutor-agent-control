# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
A2A Agent Card signing service.

Three responsibilities, all tenant-scoped:

    1. Generate / fetch the tenant's Ed25519 keypair.
       Private half is Fernet-encrypted with the gateway's
       ENCRYPTION_KEY before being stored. Public half is
       base64url text, ready to hand to an external verifier.

    2. Sign a card's JSON body, writing signature + trust_tier onto
       the AgentCard row. Signing uses canonical JSON (sorted keys,
       no whitespace) so the signature is deterministic across
       pythons, round-trips, and admin UI re-serialisation.

    3. Verify an existing signature against the tenant's public key.
       Called on every card read if/when the admin UI surfaces a
       "signature still valid?" indicator.

Kept deliberately small — no HTTP, no policy logic, no capability
extraction. Capability denormalisation is its own function
`sync_capabilities()` below, which the endpoint layer calls after
persisting the card row. Separation means the signing path stays
auditable on its own.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey
from sqlalchemy.orm import Session
from ulid import ULID

from app.core.logging_system import BrutorLogger
from app.database.models.agent_card import (
    AgentCard,
    TRUST_TIER_REVOKED,
    TRUST_TIER_TENANT_SIGNED,
    TRUST_TIER_UNSIGNED,
)
from app.database.models.agent_card_capability import AgentCardCapability
from app.database.models.tenant_a2a_key import (
    KEY_ALGORITHM_ED25519,
    TenantA2AKey,
)
from app.services.oauth.encryption_service import EncryptionService


_logger = BrutorLogger("a2a_signing")


# =============================================================================
# Exceptions
# =============================================================================


class A2ASigningError(Exception):
    """Base class for A2A signing / verification failures."""


class KeyNotFoundError(A2ASigningError):
    """Raised when the tenant has no signing key yet."""


class InvalidCardError(A2ASigningError):
    """Raised when the card JSON fails structural validation."""


# =============================================================================
# Canonical JSON
# =============================================================================


def canonical_json(payload: Dict[str, Any]) -> bytes:
    """Deterministic JSON encoding used as the signing surface.

    Sorted keys, no whitespace, separator commas, UTF-8 bytes. Matches
    what RFC 8785 calls JCS (JSON Canonicalization Scheme) closely
    enough for our bilateral use — both sides of the signature are
    this code, so full JCS compliance is over-engineering for v1.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


# =============================================================================
# Keypair management
# =============================================================================


@dataclass
class KeyPair:
    """Plaintext view of a tenant's Ed25519 keypair.

    Created only in-process, never persisted in this shape — the
    private key goes through `EncryptionService.encrypt()` before
    touching the DB. Exists so the service layer can hand a caller
    the public key without round-tripping the model.

    `private_key` is a PyNaCl `SigningKey`, libsodium-backed, in use
    since v0.8.0. Signatures and public keys are byte-identical to the
    earlier `cryptography.Ed25519PrivateKey` ones (RFC 8032), so any
    external verifier that worked against the old keys keeps working.
    """
    tenant_id: str
    public_key_b64: str
    private_key: SigningKey


def _b64url(raw: bytes) -> str:
    """URL-safe base64 with padding stripped — matches RFC 4648 §5."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    """Inverse of `_b64url` — re-pad before decoding."""
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _private_key_to_bytes(key: SigningKey) -> bytes:
    """Raw 32-byte Ed25519 seed (RFC 8032).

    PyNaCl's `SigningKey` exposes the seed via `bytes(key)` and via
    `key.encode()` — both return the same 32-byte value. Using
    `key.encode()` makes the intent explicit.
    """
    return key.encode()


def _public_key_to_bytes(key: VerifyKey) -> bytes:
    """Raw 32-byte Ed25519 public key."""
    return bytes(key)


def generate_keypair() -> SigningKey:
    """Fresh Ed25519 keypair. Wrapper exists so tests can monkeypatch."""
    return SigningKey.generate()


def get_or_create_tenant_key(
    db: Session,
    tenant_id: str,
    encryption_service: EncryptionService,
) -> KeyPair:
    """Idempotent — returns the existing key, or creates + persists one.

    Called on tenant provisioning (so every tenant has a key before an
    admin tries to sign a card) and on every sign call as a safety net.
    """
    existing = db.query(TenantA2AKey).filter(
        TenantA2AKey.tenant_id == tenant_id
    ).first()
    if existing is not None:
        try:
            private_pem = encryption_service.decrypt(existing.private_key_encrypted)
        except Exception as e:  # noqa: BLE001 — wrap to a typed error
            raise A2ASigningError(
                f"Tenant {tenant_id}: failed to decrypt signing key — "
                "ENCRYPTION_KEY may have changed"
            ) from e
        # PyNaCl's SigningKey takes the raw 32-byte seed directly;
        # equivalent to cryptography's `Ed25519PrivateKey.from_private_bytes`.
        # Keys generated and stored by the old (`cryptography`-backed)
        # implementation use the same RFC 8032 seed format, so existing
        # rows continue to work unchanged.
        private_key = SigningKey(_b64url_decode(private_pem))
        return KeyPair(
            tenant_id=tenant_id,
            public_key_b64=existing.public_key,
            private_key=private_key,
        )

    # Fresh keypair. PyNaCl exposes the public half as `key.verify_key`;
    # this is the analogue of cryptography's `private_key.public_key()`.
    private_key = generate_keypair()
    private_raw = _private_key_to_bytes(private_key)
    public_raw = _public_key_to_bytes(private_key.verify_key)

    row = TenantA2AKey(
        tenant_id=tenant_id,
        public_key=_b64url(public_raw),
        private_key_encrypted=encryption_service.encrypt(_b64url(private_raw)),
        key_algorithm=KEY_ALGORITHM_ED25519,
    )
    db.add(row)
    db.flush()
    _logger.info(
        "Generated Ed25519 signing key for tenant",
        context={"tenant_id": tenant_id},
    )
    return KeyPair(
        tenant_id=tenant_id,
        public_key_b64=row.public_key,
        private_key=private_key,
    )


# =============================================================================
# Signing / verification
# =============================================================================


def sign_card(
    db: Session,
    card: AgentCard,
    signed_by: str,
    encryption_service: EncryptionService,
) -> AgentCard:
    """Sign `card.card_json` with the tenant's private key.

    Mutates the card row in place: signature, signature_algorithm,
    signed_by, signed_at, trust_tier become populated. Caller owns
    the db.commit().

    A revoked card CANNOT be re-signed — revocation is terminal. The
    operator must create a new card row.
    """
    if card.trust_tier == TRUST_TIER_REVOKED:
        raise A2ASigningError(
            f"Agent card {card.id} is revoked — create a new card to re-sign"
        )

    keypair = get_or_create_tenant_key(db, card.tenant_id, encryption_service)
    body = canonical_json(card.card_json or {})
    # PyNaCl's `SigningKey.sign(body)` returns a `SignedMessage` whose
    # `.signature` is the bare 64-byte RFC 8032 signature — matching what
    # cryptography's `Ed25519PrivateKey.sign(body)` returned directly.
    # Ed25519 is deterministic per RFC 8032 §5.1, so the bytes are
    # identical to what the old implementation produced over the same
    # body — external verifiers see no change.
    signature = keypair.private_key.sign(body).signature

    card.signature = _b64url(signature)
    card.signature_algorithm = KEY_ALGORITHM_ED25519
    card.signed_by = signed_by
    card.signed_at = datetime.now(timezone.utc)
    card.trust_tier = TRUST_TIER_TENANT_SIGNED

    _logger.info(
        "Signed A2A agent card",
        context={
            "card_id": card.id,
            "tenant_id": card.tenant_id,
            "signed_by": signed_by,
            "signature_prefix": card.signature[:16],
        },
    )
    return card


def verify_card_signature(
    db: Session,
    card: AgentCard,
) -> bool:
    """True iff `card.signature` verifies against the tenant's public key.

    Read-only — does NOT mutate the card. Revoked cards always return
    False regardless of signature validity (revocation is terminal).
    Unsigned / never-signed cards return False with no DB access.
    """
    if card.trust_tier == TRUST_TIER_REVOKED:
        return False
    if not card.signature or not card.signature_algorithm:
        return False
    if card.signature_algorithm != KEY_ALGORITHM_ED25519:
        # Only Ed25519 is implemented. Partner-signed (RSA/EC) cards
        # are not verified yet.
        return False

    row = db.query(TenantA2AKey).filter(
        TenantA2AKey.tenant_id == card.tenant_id
    ).first()
    if row is None:
        return False

    try:
        public_raw = _b64url_decode(row.public_key)
        # PyNaCl's `VerifyKey(bytes)` is the analogue of cryptography's
        # `Ed25519PublicKey.from_public_bytes(bytes)` — both expect the
        # raw 32-byte RFC 8032 public key.
        public_key = VerifyKey(public_raw)
        body = canonical_json(card.card_json or {})
        # `verify(message, signature)` raises BadSignatureError on
        # mismatch (analogous to cryptography's InvalidSignature) and
        # returns the message bytes on success. Note the argument order
        # is reversed compared to cryptography's `.verify(sig, msg)`.
        public_key.verify(body, _b64url_decode(card.signature))
        return True
    except (BadSignatureError, ValueError):
        # BadSignatureError is the normal "no, it doesn't match" path;
        # ValueError catches malformed base64 or wrong-length keys.
        return False


# =============================================================================
# Revocation
# =============================================================================


def revoke_card(
    db: Session,
    card: AgentCard,
    reason: str,
    revoked_by: str,
) -> AgentCard:
    """Mark a card permanently revoked. Terminal — new card needed.

    Disables the card so the proxy stops routing to it; keeps the row
    for audit. We intentionally don't null-out the signature — auditors
    want to see what was signed and by whom.
    """
    card.revoke(reason)
    _logger.warning(
        "Revoked A2A agent card",
        context={
            "card_id": card.id,
            "tenant_id": card.tenant_id,
            "revoked_by": revoked_by,
            "reason": reason,
        },
    )
    return card


# =============================================================================
# Capability denormalisation
# =============================================================================


# Fields we pull out of a card's capability object into dedicated
# columns. Anything not listed here stays only in card_json — the
# per-capability table is a *policy-hot path*, not a full mirror.
_KNOWN_CAPABILITY_FIELDS = {
    "name",
    "description",
    "input_schema",
    "output_schema",
    "timeout_secs",
    "rate_limit_per_hour",
    "data_classification",
    # Inbound handler binding.
    "handler_type",
    "handler_ref",
}


def sync_capabilities(db: Session, card: AgentCard) -> List[AgentCardCapability]:
    """Regenerate `agent_card_capabilities` rows for a card.

    Called by the endpoint layer after persisting the card. Strategy
    is delete-then-insert rather than UPSERT — capabilities change
    wholesale when a card is updated (a declared capability going
    away is a revoke-of-sorts) and the table is small, so the
    simplicity wins over the few saved UPDATEs.

    Reads from `card_json.skills[]` (A2A v0.3+) when present, falling
    back to `card_json.capabilities[]` (v0.2.x legacy) so older cards
    keep importing. Each entry must have at least a `name`. Anything
    else is ignored with a log rather than raising — admins importing
    non-conformant cards should see the error in the detail view, not
    a 500 on save.
    """
    # Drop existing rows. relationship is configured with
    # cascade="all, delete-orphan" but we're operating on a fresh
    # query here, so delete explicitly.
    db.query(AgentCardCapability).filter(
        AgentCardCapability.agent_card_id == card.id
    ).delete(synchronize_session=False)

    caps_raw = get_dispatchable_list(card.card_json or {})

    new_rows: List[AgentCardCapability] = []
    seen_names: set[str] = set()
    for idx, cap in enumerate(caps_raw):
        if not isinstance(cap, dict):
            continue
        name = (cap.get("name") or "").strip()
        if not name:
            _logger.warning(
                "Agent card capability without a name — skipping",
                context={"card_id": card.id, "index": idx},
            )
            continue
        if name in seen_names:
            # Duplicate names in the card JSON — keep the first,
            # skip the rest. Policy rules would be ambiguous otherwise.
            _logger.warning(
                "Duplicate capability name in card — keeping first",
                context={"card_id": card.id, "capability": name},
            )
            continue
        seen_names.add(name)

        handler_type_raw = cap.get("handler_type")
        handler_type: Optional[str]
        if handler_type_raw in (None, ""):
            handler_type = None
        elif isinstance(handler_type_raw, str):
            handler_type = handler_type_raw.strip() or None
        else:
            _logger.warning(
                "Agent card capability has non-string handler_type — ignoring",
                context={
                    "card_id": card.id,
                    "capability": name,
                    "handler_type_type": type(handler_type_raw).__name__,
                },
            )
            handler_type = None

        handler_ref_raw = cap.get("handler_ref")
        handler_ref = (
            handler_ref_raw.strip()
            if isinstance(handler_ref_raw, str) and handler_ref_raw.strip()
            else None
        )

        try:
            row = AgentCardCapability(
                id=str(ULID()),
                agent_card_id=card.id,
                capability_name=name,
                description=cap.get("description"),
                input_schema=cap.get("input_schema"),
                output_schema=cap.get("output_schema"),
                timeout_secs=cap.get("timeout_secs"),
                rate_limit_per_hour=cap.get("rate_limit_per_hour"),
                data_classification=cap.get("data_classification"),
                handler_type=handler_type,
                handler_ref=handler_ref,
                enabled=bool(cap.get("enabled", True)),
            )
        except ValueError as e:
            # Validator rejected handler_type — log and drop the row
            # rather than 500 on save. The admin sees the malformed
            # entry next time they open the card.
            _logger.warning(
                "Agent card capability rejected by validator — skipping",
                context={
                    "card_id": card.id,
                    "capability": name,
                    "error": str(e),
                },
            )
            continue
        db.add(row)
        new_rows.append(row)

    db.flush()
    return new_rows


# =============================================================================
# Card validation
# =============================================================================
#
# A2A wire shape changed across versions:
#
#   v0.2.x  →  top-level `capabilities[]` was the array of dispatchable units
#   v0.3+   →  `skills[]` is the array; `capabilities` becomes an OBJECT
#              `{streaming, pushNotifications, extensions, ...}` for feature
#              flags.
#
# We accept either shape so importing real-world cards works regardless of
# generation. The dispatchable list (skills[] preferred over capabilities[])
# is also allowed to be absent — some agents publish a routing-only card
# with no exposed skills, which is still a valid card.

_REQUIRED_CARD_FIELDS = ("name", "description", "version")


def get_dispatchable_list(card_json: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the card's array of dispatchable units, regardless of dialect.

    Prefers v1.0/v0.3 `skills[]`; falls back to v0.2.x `capabilities[]` only
    when `capabilities` is itself a list (i.e. legacy shape). When `skills`
    is missing AND `capabilities` is the v1.0 feature-flag object, returns
    an empty list — the card is valid but advertises no dispatchable skills.
    """
    skills = card_json.get("skills")
    if isinstance(skills, list):
        return [s for s in skills if isinstance(s, dict)]
    legacy = card_json.get("capabilities")
    if isinstance(legacy, list):
        return [c for c in legacy if isinstance(c, dict)]
    return []


def validate_card_json(card_json: Dict[str, Any]) -> None:
    """Light structural validation across A2A v0.2.x → v1.0.

    This is deliberately permissive — we accept anything a well-formed
    A2A card producer would emit, without enforcing the entire JSON schema.
    The intent is to catch "admin pasted a YAML blob" or "missing name" at
    write time, not to validate against the evolving spec (which would
    also be a moving target). Spec-strict validation is not done
    before the proxy trusts the card for routing.
    """
    if not isinstance(card_json, dict):
        raise InvalidCardError("card_json must be a JSON object")
    missing = [f for f in _REQUIRED_CARD_FIELDS if f not in card_json]
    if missing:
        raise InvalidCardError(
            f"card_json is missing required fields: {', '.join(missing)}"
        )

    # `skills` is the modern dispatchable list — must be a list when present.
    skills = card_json.get("skills")
    if skills is not None and not isinstance(skills, list):
        raise InvalidCardError("card_json.skills must be a list when present")

    # `capabilities` was a list in v0.2.x and is an object in v0.3+. Reject
    # only the truly malformed case (a string, a number, etc.) — list or
    # dict are both fine, and absence is fine too.
    caps = card_json.get("capabilities")
    if caps is not None and not isinstance(caps, (list, dict)):
        raise InvalidCardError(
            "card_json.capabilities must be a list (legacy) or object (v1.0)"
        )

    # A capability's hourly rate is a limit the gateway applies to every
    # call of it: a positive whole number, or absent.
    for entry in get_dispatchable_list(card_json):
        if not isinstance(entry, dict) or entry.get("rate_limit_per_hour") is None:
            continue
        rate = entry["rate_limit_per_hour"]
        if isinstance(rate, bool) or not isinstance(rate, int) or rate < 1:
            raise InvalidCardError(
                f"capability '{entry.get('name') or entry.get('id')}': rate_limit_per_hour must be a "
                "positive whole number of calls per hour"
            )
