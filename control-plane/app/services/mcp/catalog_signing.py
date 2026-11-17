# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# MCP catalog entry signing

Reuses the A2A Ed25519 signing primitive (`canonical_json` +
tenant signing key) to give catalog entries the same
cryptographic attestation that agent cards already carry.

# Why share the A2A primitive

A tenant only needs one signing key. Adding a separate
catalog-specific key would mean:
  - Operators rotate twice when they rotate keys
  - Audit logs split across two signing surfaces
  - Two encryption-at-rest stories

Instead we re-use `get_or_create_tenant_key()` from
`app/services/a2a/card_signing.py`. The signature scheme (Ed25519,
RFC 8032) and signing surface (`canonical_json(payload)`) are
identical — only the columns we mutate differ.

# Signing surface

The body that gets signed is `canonical_json({...catalog entry
contents...})`. We DELIBERATELY exclude the signature columns
themselves and the binary `icon_data` (large + already content-
addressed via `icon_hash`). What stays in is the operator-visible
metadata: name, slug, description, runtime, configs, env_vars,
tool_preview, categories, etc. Any tamper with these fields
breaks the signature; tampering with `enabled` (an operator-side
toggle) does not — that's an intentional escape hatch so
operators can disable a verified entry without invalidating its
signature.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey
from sqlalchemy.orm import Session

from app.database.models.mcp_catalog_entry import (
    MCPCatalogEntry,
    TRUST_TIER_PARTNER_SIGNED,
    TRUST_TIER_REVOKED,
    TRUST_TIER_TENANT_SIGNED,
    TRUST_TIER_UNSIGNED,
)
from app.database.models.tenant_a2a_key import (
    KEY_ALGORITHM_ED25519,
    TenantA2AKey,
)
from app.services.a2a.card_signing import (
    _b64url,
    _b64url_decode,
    canonical_json,
    get_or_create_tenant_key,
)
from app.services.oauth.encryption_service import EncryptionService


log = logging.getLogger("brutor-catalog-signing")


class CatalogSigningError(RuntimeError):
    """Surface to HTTP 4xx — bad input, revoked entry, missing key, etc."""


# ---------------------------------------------------------------------------
# Canonical signing body
# ---------------------------------------------------------------------------


def _entry_signing_body(entry: MCPCatalogEntry) -> Dict[str, Any]:
    """Build the dict that gets canonicalised + signed.

    Includes everything that defines the entry's IDENTITY +
    BEHAVIOUR — name, runtime, configs, env_vars, etc. Excludes:

      - `signature`, `signature_algorithm`, `signed_by`, `signed_at`,
        `trust_tier` (the signature can't sign over itself)
      - `enabled` (operator toggle; can flip without re-signing)
      - `icon_data` (large binary, already content-addressed via
        `icon_hash`)
      - `created_at`, `updated_at` (server-managed timestamps)
      - `tenant_id`, `id` (identifiers; the signature is attested
        within its tenant scope by the tenant-bound public key)

    A change to ANY included field invalidates the signature on
    next verify.
    """
    return {
        "name": entry.name,
        "slug": entry.slug,
        "short_description": entry.short_description,
        "description": entry.description,
        "icon_url": entry.icon_url,
        "icon_hash": entry.icon_hash,
        "repo_url": entry.repo_url,
        "runtime": entry.runtime,
        "remote_config": entry.remote_config,
        "containerized_config": entry.containerized_config,
        "env_vars": entry.env_vars or [],
        "tool_preview": entry.tool_preview or [],
        "categories": entry.categories or [],
        "metadata_extra": entry.metadata_extra,
        "auth_pattern": entry.auth_pattern,
        "source": entry.source,
    }


# ---------------------------------------------------------------------------
# Sign / verify
# ---------------------------------------------------------------------------


def sign_catalog_entry(
    db: Session,
    entry: MCPCatalogEntry,
    signed_by: str,
    encryption_service: EncryptionService,
) -> MCPCatalogEntry:
    """Sign `entry` with the tenant's Ed25519 key.

    Mutates the entry in place; caller owns `db.commit()`.

    Returns the same entry (for chaining). Raises CatalogSigningError
    on revoked entries — operators must clone + re-sign instead of
    resurrecting a revoked one (revocation is terminal).
    """
    if entry.trust_tier == TRUST_TIER_REVOKED:
        raise CatalogSigningError(
            f"Catalog entry {entry.id} is revoked. Clone + re-sign "
            "instead of resurrecting a revoked entry."
        )

    keypair = get_or_create_tenant_key(db, entry.tenant_id, encryption_service)
    body = canonical_json(_entry_signing_body(entry))
    signature = keypair.private_key.sign(body).signature

    entry.signature = _b64url(signature)
    entry.signature_algorithm = KEY_ALGORITHM_ED25519
    entry.signed_by = signed_by
    entry.signed_at = datetime.now(timezone.utc)
    entry.trust_tier = TRUST_TIER_TENANT_SIGNED

    log.info(
        "Signed MCP catalog entry",
        extra={
            "entry_id": entry.id,
            "tenant_id": entry.tenant_id,
            "signed_by": signed_by,
            "signature_prefix": entry.signature[:16],
        },
    )
    return entry


def verify_catalog_entry_signature(
    db: Session,
    entry: MCPCatalogEntry,
) -> bool:
    """True iff `entry.signature` verifies against the tenant's
    public key.

    Read-only. Revoked entries always return False regardless of
    signature validity. Unsigned entries return False with no DB
    access.
    """
    if entry.trust_tier == TRUST_TIER_REVOKED:
        return False
    if not entry.signature or not entry.signature_algorithm:
        return False
    if entry.signature_algorithm != KEY_ALGORITHM_ED25519:
        # Partner-signed (RSA/EC) lands in a future sprint with the
        # cross-tenant CA story; today we only verify Ed25519.
        return False

    row = (
        db.query(TenantA2AKey)
        .filter(TenantA2AKey.tenant_id == entry.tenant_id)
        .first()
    )
    if row is None:
        return False

    try:
        public_raw = _b64url_decode(row.public_key)
        public_key = VerifyKey(public_raw)
        body = canonical_json(_entry_signing_body(entry))
        public_key.verify(body, _b64url_decode(entry.signature))
        return True
    except (BadSignatureError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------


def revoke_catalog_entry(
    db: Session,
    entry: MCPCatalogEntry,
    revoked_by: str,
) -> MCPCatalogEntry:
    """Mark an entry as `revoked` — terminal state.

    Sets `trust_tier=revoked` and audits via standard log. The
    signature bytes stay on the row so an operator can forensically
    verify what the entry's body looked like at sign time (the body
    can drift after revocation, but the signature would no longer
    verify even without the trust_tier check).
    """
    entry.trust_tier = TRUST_TIER_REVOKED
    # Repurpose `signed_by` / `signed_at` to capture revocation
    # metadata — same column meaning ("who last attested + when"),
    # different semantics.
    entry.signed_by = revoked_by
    entry.signed_at = datetime.now(timezone.utc)
    log.info(
        "Revoked MCP catalog entry",
        extra={
            "entry_id": entry.id,
            "tenant_id": entry.tenant_id,
            "revoked_by": revoked_by,
        },
    )
    return entry


__all__ = [
    "CatalogSigningError",
    "sign_catalog_entry",
    "verify_catalog_entry_signature",
    "revoke_catalog_entry",
    # Re-export tier constants for convenience.
    "TRUST_TIER_UNSIGNED",
    "TRUST_TIER_TENANT_SIGNED",
    "TRUST_TIER_PARTNER_SIGNED",
    "TRUST_TIER_REVOKED",
]
