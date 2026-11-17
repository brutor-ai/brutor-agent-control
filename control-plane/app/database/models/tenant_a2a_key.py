# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Per-tenant Ed25519 signing key for A2A Agent Cards.

Each tenant owns a single signing keypair. The private half is Fernet-
encrypted at rest with the gateway's ENCRYPTION_KEY; the public half
is base64url-encoded text so the admin API can serve it verbatim at
`GET /v1/admin/tenants/{id}/a2a-public-key` for external verifiers.

One-to-one with `tenants` — tenant_id is both PK and FK. Rotation is
a future capability; today's flow is "revoke all cards,
then call a /rotate-key endpoint to replace the row". We write the
`rotated_at` column now so the rotation code has a place to land
without a migration.
"""

from typing import Any, Dict

from sqlalchemy import Column, DateTime, ForeignKey, String, Text
from sqlalchemy.sql import func

from app.database import Base


KEY_ALGORITHM_ED25519 = "ed25519"


class TenantA2AKey(Base):
    """Tenant-scoped Ed25519 signing keypair for Agent Cards."""

    __tablename__ = "tenant_a2a_keys"

    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )

    public_key = Column(Text, nullable=False,
                        comment="Ed25519 public key, base64url-encoded")
    private_key_encrypted = Column(Text, nullable=False,
                                   comment="Fernet-encrypted Ed25519 private key")
    key_algorithm = Column(String(32), nullable=False,
                           default=KEY_ALGORITHM_ED25519,
                           server_default=KEY_ALGORITHM_ED25519)

    created_at = Column(DateTime(timezone=True),
                        server_default=func.now(), nullable=False)
    rotated_at = Column(DateTime(timezone=True), nullable=True)

    def to_public_dict(self) -> Dict[str, Any]:
        """Safe-to-expose subset — omits the encrypted private key."""
        return {
            "tenant_id": self.tenant_id,
            "public_key": self.public_key,
            "key_algorithm": self.key_algorithm,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "rotated_at": self.rotated_at.isoformat() if self.rotated_at else None,
        }
