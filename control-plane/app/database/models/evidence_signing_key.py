# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Evidence signing keys — per-tenant and retired keys

The deployment key comes from the environment; rows here are per-tenant
keys (opt-in via `evidence_settings.per_tenant_key`) and retired keys a
verifier must still trust. `kid` is the hex SHA-256 key id published at
`/.well-known/brutor-evidence-keys.json`. `encrypted_seed` is the Ed25519
seed, Fernet-encrypted under `ENCRYPTION_KEY` by the Rust core — the
control plane never decrypts it and `to_dict()` never returns it.

A partial unique index allows at most one `active` key per tenant.
`tenant_id IS NULL` is a deployment key; Postgres treats NULLs as distinct,
so that index does not constrain deployment rows — the core retires the old
row and inserts the new one in one transaction.
"""

from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import Column, DateTime, Index, String, Text, text
from sqlalchemy.orm import validates

from app.database import Base
from app.database.models.evidence_columns import check_vocab, created_at_column, iso

KEY_STATUS_ACTIVE = "active"
KEY_STATUS_RETIRED = "retired"
KEY_STATUSES = frozenset({KEY_STATUS_ACTIVE, KEY_STATUS_RETIRED})


class EvidenceSigningKey(Base):
    """One signing key (core-written; CP reads the public metadata)."""

    __tablename__ = "evidence_signing_keys"

    kid = Column(String(64), primary_key=True, comment="hex SHA-256 key id")
    tenant_id = Column(String(255), nullable=True, comment="NULL = deployment key")
    encrypted_seed = Column(Text, nullable=False, comment="Fernet(ENCRYPTION_KEY) — never exposed")
    status = Column(String(16), nullable=False, default=KEY_STATUS_ACTIVE, server_default="active")
    created_at = created_at_column()
    retired_at = Column(DateTime(timezone=True), nullable=True)
    rotation_record_id = Column(String(64), nullable=True, comment="ai.brutor.key-rotation record")

    __table_args__ = (
        Index(
            "uq_evidence_signing_keys_active",
            "tenant_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )

    @validates("status")
    def _validate_status(self, _key, value):
        return check_vocab("status", value, KEY_STATUSES)

    def to_dict(self) -> Dict[str, Any]:
        # encrypted_seed deliberately omitted.
        return {
            "kid": self.kid,
            "tenant_id": self.tenant_id,
            "scope": "deployment" if self.tenant_id is None else "tenant",
            "status": self.status,
            "created_at": iso(self.created_at),
            "retired_at": iso(self.retired_at),
            "rotation_record_id": self.rotation_record_id,
        }


__all__ = ["EvidenceSigningKey", "KEY_STATUSES", "KEY_STATUS_ACTIVE", "KEY_STATUS_RETIRED"]
