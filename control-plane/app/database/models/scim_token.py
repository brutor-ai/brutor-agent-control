# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# The SCIM bearer-token table

SCIM provisioning is not in this release: no route writes or reads this
table, and no token in it is accepted anywhere. It stays in the schema until
the migrations are squashed. Its columns: `token_hash` (SHA-256 hex of a
token; the plaintext was never stored), `token_prefix` (the first 8
characters, for display), `idp_server_id` (an optional identity provider),
and the issue and revocation timestamps.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import (
    Column, DateTime, ForeignKey, Index, String,
)
from sqlalchemy.sql import func

from app.database import Base


class ScimToken(Base):
    """A bearer token issued to an external IdP for SCIM provisioning."""

    __tablename__ = "scim_tokens"

    id = Column(String(64), primary_key=True)
    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String(255), nullable=False)
    # SHA-256 hex digest (64 chars). Stored with `sha256:` prefix
    # so future algo upgrades don't require a schema change —
    # callers branch on the prefix.
    token_hash = Column(String(128), nullable=False)
    token_prefix = Column(String(16), nullable=False)

    idp_server_id = Column(
        String(255),
        ForeignKey("idp_servers.id", ondelete="SET NULL"),
        nullable=True,
    )

    expires_at = Column(DateTime(timezone=True), nullable=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    created_by = Column(String(64), nullable=True)

    __table_args__ = (
        Index("ix_scim_tokens_hash", "token_hash"),
        Index("ix_scim_tokens_tenant_active", "tenant_id", "revoked_at"),
    )

    # ---------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------

    @property
    def is_active(self) -> bool:
        """Token is usable for auth right now."""
        if self.revoked_at is not None:
            return False
        if self.expires_at is not None:
            # Compare in UTC. Postgres returns timezone-aware datetimes
            # for `TIMESTAMPTZ` columns; SQLite tests use UTC literals.
            now = datetime.now(timezone.utc)
            expires = self.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if now >= expires:
                return False
        return True

    def to_dict(self, *, include_plaintext: Optional[str] = None) -> Dict[str, Any]:
        """Serialise for the admin API.

        `include_plaintext` is ONLY passed by the create endpoint —
        the only path where the freshly-generated plaintext is in
        scope. Every other call must omit it.
        """
        d: Dict[str, Any] = {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "token_prefix": self.token_prefix,
            "idp_server_id": self.idp_server_id,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "created_by": self.created_by,
            "is_active": self.is_active,
        }
        if include_plaintext is not None:
            d["token"] = include_plaintext
        return d

    def __repr__(self) -> str:
        return (
            f"<ScimToken(id={self.id!r}, tenant={self.tenant_id!r}, "
            f"prefix={self.token_prefix!r}, active={self.is_active})>"
        )


__all__ = ["ScimToken"]
