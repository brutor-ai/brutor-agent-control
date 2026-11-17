# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
KbConnectorIdentityMapping — joins an authenticated Brutor end-user to one
per-source-system principal that the KB Connector Sync service may have stamped
onto a kb_documents.acl array at ingest time.

The search-time ACL filter in gateway-core's services::knowledge_base reads
the set of mappings for the requesting user, unions them with ``["public"]``,
and adds a Qdrant filter that keeps only rows whose ``acl`` overlaps that
union. User-uploaded rows (source='upload') bypass the filter — they're
already gated by the kb_collections → resource_group binding.

Companion migration: alembic a060_kb_identity_mappings.
"""

from typing import Any, Dict

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


# Kept in lockstep with the CHECK constraint in the migrations
# and the Rust connector dispatcher.
CONNECTOR_TYPE_CONFLUENCE = "confluence"
CONNECTOR_TYPE_NOTION = "notion"
CONNECTOR_TYPE_GOOGLE_DRIVE = "google_drive"
CONNECTOR_TYPE_SLACK = "slack"
CONNECTOR_TYPE_GITHUB = "github"
CONNECTOR_TYPE_WEB_CRAWLER = "web_crawler"
CONNECTOR_TYPE_JIRA = "jira"
CONNECTOR_TYPE_SHAREPOINT = "sharepoint"
ALLOWED_IDENTITY_CONNECTOR_TYPES = (
    CONNECTOR_TYPE_CONFLUENCE,
    CONNECTOR_TYPE_NOTION,
    CONNECTOR_TYPE_GOOGLE_DRIVE,
    CONNECTOR_TYPE_SLACK,
    CONNECTOR_TYPE_GITHUB,
    CONNECTOR_TYPE_WEB_CRAWLER,
    CONNECTOR_TYPE_JIRA,
    CONNECTOR_TYPE_SHAREPOINT,
)

MAPPING_SOURCE_SCIM = "scim"
MAPPING_SOURCE_OIDC_CLAIM = "oidc_claim"
MAPPING_SOURCE_MANUAL = "manual"
MAPPING_SOURCE_SELF_LINK = "self_link"
ALLOWED_MAPPING_SOURCES = (
    MAPPING_SOURCE_SCIM,
    MAPPING_SOURCE_OIDC_CLAIM,
    MAPPING_SOURCE_MANUAL,
    MAPPING_SOURCE_SELF_LINK,
)


class KbConnectorIdentityMapping(Base):
    """One (end_user, connector_type, source_principal) link."""

    __tablename__ = "kb_connector_identity_mappings"

    id = Column(String(255), primary_key=True)

    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )

    end_user_id = Column(
        Integer,
        ForeignKey("end_users.id", ondelete="CASCADE"),
        nullable=False,
    )

    connector_type = Column(String(64), nullable=False)

    source_principal = Column(
        String(512),
        nullable=False,
        comment=(
            "Per-source principal ID matched against kb_documents.acl. "
            "Examples: 'slack:user:U123', 'gdrive:group:eng@bar.com'."
        ),
    )

    mapping_source = Column(
        String(32),
        nullable=False,
        default=MAPPING_SOURCE_MANUAL,
        server_default=MAPPING_SOURCE_MANUAL,
    )

    verified_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment=(
            "When the source system last confirmed this principal belongs "
            "to the user — set by self-link OAuth callback or SCIM sync."
        ),
    )

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    created_by = Column(String(255), nullable=True)

    # The end-user side of the relationship is read-only here — the EndUser
    # model owns no back_populates link (kept lazy to avoid changing that
    # model's circular-import surface). Look-ups always go via end_user_id.
    end_user = relationship(
        "EndUser",
        foreign_keys=[end_user_id],
        lazy="select",
    )

    __table_args__ = (
        UniqueConstraint(
            "end_user_id", "connector_type", "source_principal",
            name="uq_kb_identity_user_type_principal",
        ),
        CheckConstraint(
            "connector_type IN ("
            "'confluence','notion','google_drive','slack','github',"
            "'web_crawler','jira','sharepoint'"
            ")",
            name="ck_kb_identity_connector_type",
        ),
        CheckConstraint(
            "mapping_source IN "
            "('scim','oidc_claim','manual','self_link')",
            name="ck_kb_identity_mapping_source",
        ),
        Index(
            "ix_kb_identity_tenant_user",
            "tenant_id", "end_user_id",
        ),
        Index(
            "ix_kb_identity_tenant_type_principal",
            "tenant_id", "connector_type", "source_principal",
        ),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "end_user_id": self.end_user_id,
            "connector_type": self.connector_type,
            "source_principal": self.source_principal,
            "mapping_source": self.mapping_source,
            "verified_at": (
                self.verified_at.isoformat() if self.verified_at else None
            ),
            "created_at": (
                self.created_at.isoformat() if self.created_at else None
            ),
            "updated_at": (
                self.updated_at.isoformat() if self.updated_at else None
            ),
            "created_by": self.created_by,
        }

    def __repr__(self) -> str:
        return (
            f"<KbConnectorIdentityMapping(tenant={self.tenant_id!r}, "
            f"user={self.end_user_id!r}, kind={self.connector_type!r}, "
            f"principal={self.source_principal!r})>"
        )
