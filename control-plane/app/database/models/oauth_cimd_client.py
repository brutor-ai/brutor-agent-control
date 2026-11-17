# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Gateway-owned OAuth clients minted for CIMD callers.

A CIMD client presents `client_id = https://…` pointing at a metadata
document it hosts. A stock OIDC provider has no idea what that means, so
the gateway bridges: it lazily registers ONE client of its own at the IdP
per (tenant, idp, document URL) and drives the upstream leg with that.
This table is the record of those minted clients, plus a cache of the
fetched document.

**Written only by the Rust core** (`routes/oauth.rs`), which owns the
authorize/callback/token bridge. The model exists here because the
control plane owns the schema — and, critically, because a fresh install
builds its schema with `create_all()` from these models and then *stamps*
the Alembic head rather than replaying migrations. A table that lives
only in `alembic/versions/` is therefore created on upgrades but silently
missing on new installs; declaring it here is what makes migration
`a126_oauth_cimd_clients` real for both paths.
"""

from sqlalchemy import Column, DateTime, JSON, String, Text, UniqueConstraint
from sqlalchemy.sql import func

from app.database import Base


class OAuthCimdClient(Base):
    """One gateway-owned IdP client per (tenant, idp, CIMD document URL)."""

    __tablename__ = "oauth_cimd_clients"

    # No FKs to tenants/idp_servers here — migration `a126_oauth_cimd_clients` declares none, and a
    # fresh install builds this table from the model while an upgrade builds it
    # from the migration. The two paths must produce identical schemas, so this
    # deliberately mirrors that migration rather than improving on it.
    id = Column(String(255), primary_key=True)  # cimd-<random>
    tenant_id = Column(String(255), nullable=False, index=True)
    idp_id = Column(String(255), nullable=False)
    # The client_id the caller presented — an https URL that is also the
    # location of its metadata document.
    cimd_url = Column(String(2048), nullable=False)

    # The client the gateway registered at the IdP on that caller's behalf.
    idp_client_id = Column(String(512), nullable=False)
    idp_client_secret_encrypted = Column(Text, nullable=True)

    # Last fetched metadata document + when, so the bridge can refresh on
    # a TTL instead of re-fetching on every authorize.
    doc = Column(JSON, nullable=True)
    doc_fetched_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )

    # `index=True` on tenant_id already emits ix_oauth_cimd_clients_tenant_id;
    # naming it again here would try to create the same index twice.
    __table_args__ = (
        UniqueConstraint("tenant_id", "idp_id", "cimd_url", name="uq_oauth_cimd_clients_scope"),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "idp_id": self.idp_id,
            "cimd_url": self.cimd_url,
            "idp_client_id": self.idp_client_id,
            # Never surface the encrypted secret.
            "has_client_secret": self.idp_client_secret_encrypted is not None,
            "doc_fetched_at": self.doc_fetched_at.isoformat() if self.doc_fetched_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
