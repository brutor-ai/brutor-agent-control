# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
KB Connector models — control-plane shape only.

A KB Connector pulls content from an external hosted service
(Confluence, Notion, Google Drive, Slack, GitHub, …) on a
schedule and feeds it into the existing KB ingestion pipeline
(the KB Uploader → Qdrant).

Five tables:

- ``kb_connectors`` — one row per configured connector instance.
- ``kb_connector_credentials`` — encrypted credential blob (1:1).
- ``kb_connector_checkpoints`` — incremental-sync cursor state.
- ``kb_connector_run_logs`` — per-sync-run history.
- ``resource_group_kb_connectors`` — M2M to resource groups, mirrors
  every other governance object's RG binding.

The Rust runtime (the KB Connector Sync service) claims due
connectors with ``SELECT … FOR UPDATE SKIP LOCKED`` against
``kb_connectors`` and writes its progress to
``kb_connector_checkpoints`` / ``kb_connector_run_logs``.
"""

from sqlalchemy import (
    text,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


# Connector types — keep in sync with the CHECK constraint in
# the migrations and the Rust dispatcher of the KB Connector Sync service.
CONNECTOR_TYPE_CONFLUENCE = "confluence"
CONNECTOR_TYPE_NOTION = "notion"
CONNECTOR_TYPE_GOOGLE_DRIVE = "google_drive"
CONNECTOR_TYPE_SLACK = "slack"
CONNECTOR_TYPE_GITHUB = "github"
CONNECTOR_TYPE_WEB_CRAWLER = "web_crawler"
CONNECTOR_TYPE_JIRA = "jira"
CONNECTOR_TYPE_SHAREPOINT = "sharepoint"
ALLOWED_CONNECTOR_TYPES = (
    CONNECTOR_TYPE_CONFLUENCE,
    CONNECTOR_TYPE_NOTION,
    CONNECTOR_TYPE_GOOGLE_DRIVE,
    CONNECTOR_TYPE_SLACK,
    CONNECTOR_TYPE_GITHUB,
    CONNECTOR_TYPE_WEB_CRAWLER,
    CONNECTOR_TYPE_JIRA,
    CONNECTOR_TYPE_SHAREPOINT,
)

# Connector types that legitimately have no credential — the catalog
# entry's `credential_fields` is empty and the admin UI must still
# allow Sync Now. Keep in sync with catalog entries whose
# `credential_fields == []`.
CREDENTIALLESS_CONNECTOR_TYPES = frozenset({
    CONNECTOR_TYPE_WEB_CRAWLER,
})

# Credential kinds — drive which payload shape the runtime expects.
CRED_KIND_API_KEY = "api_key"
CRED_KIND_OAUTH = "oauth"
CRED_KIND_SERVICE_ACCOUNT = "service_account"
ALLOWED_CRED_KINDS = (
    CRED_KIND_API_KEY,
    CRED_KIND_OAUTH,
    CRED_KIND_SERVICE_ACCOUNT,
)

# Run-log statuses.
RUN_STATUS_RUNNING = "running"
RUN_STATUS_SUCCESS = "success"
RUN_STATUS_FAILURE = "failure"
RUN_STATUS_CANCELLED = "cancelled"
ALLOWED_RUN_STATUSES = (
    RUN_STATUS_RUNNING,
    RUN_STATUS_SUCCESS,
    RUN_STATUS_FAILURE,
    RUN_STATUS_CANCELLED,
)

# Run triggers.
RUN_TRIGGER_SCHEDULE = "schedule"
RUN_TRIGGER_MANUAL = "manual"
RUN_TRIGGER_INITIAL = "initial"
ALLOWED_RUN_TRIGGERS = (
    RUN_TRIGGER_SCHEDULE,
    RUN_TRIGGER_MANUAL,
    RUN_TRIGGER_INITIAL,
)


# =============================================================================
# Many-to-many join — resource_group_kb_connectors
# =============================================================================

resource_group_kb_connectors = Table(
    "resource_group_kb_connectors",
    Base.metadata,
    Column(
        "resource_group_id",
        String(255),
        ForeignKey("resource_groups.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "kb_connector_id",
        String(255),
        ForeignKey("kb_connectors.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    # Reverse-lookup by connector, the same index the migration creates.
    Index("ix_rg_kb_connectors_connector", "kb_connector_id"),
    comment="Associates KB connectors with resource groups",
)


# =============================================================================
# Main connector instance
# =============================================================================


class KbConnector(Base):
    """One configured connector instance.

    The connector_type field selects which Rust implementation
    handles polling. The config JSON is connector-specific (which
    spaces / repos / channels / folders to sync, what filters to
    apply). Credentials live in a separate one-to-one table so a
    list query doesn't drag the encrypted blob.
    """

    __tablename__ = "kb_connectors"

    id = Column(String(255), primary_key=True)

    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )

    name = Column(String(255), nullable=False)
    description = Column(String(1024), nullable=True)

    connector_type = Column(String(64), nullable=False)
    config = Column(JSONB, nullable=False, default=dict, server_default="{}")

    collection_id = Column(
        String(255),
        ForeignKey("kb_collections.id", ondelete="SET NULL"),
        nullable=True,
    )

    schedule_cron = Column(String(128), nullable=True)

    enabled = Column(Boolean, nullable=False, default=True, server_default=text("true"))

    # Derivation of identity mappings from a sign-in's email.
    # Nothing reads or writes this column in this release (there is no
    # single sign-on to derive from); it stays in the schema until the
    # migrations are squashed.
    auto_derive_from_oidc = Column(
        Boolean,
        nullable=False,
        server_default="false",
        default=False,
        comment=(
            "Opt in this connector to OIDC-claim auto-derive. Only enable "
            "when the connector stamps email-keyed ACLs (today: Google Drive)."
        ),
    )

    next_run_at = Column(DateTime(timezone=True), nullable=True)
    last_run_started_at = Column(DateTime(timezone=True), nullable=True)
    last_run_finished_at = Column(DateTime(timezone=True), nullable=True)
    last_run_status = Column(String(32), nullable=True)
    last_run_error = Column(Text, nullable=True)

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
    updated_by = Column(String(255), nullable=True)

    # ── Relationships ────────────────────────────────────────────
    credential = relationship(
        "KbConnectorCredential",
        back_populates="connector",
        uselist=False,
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    checkpoint = relationship(
        "KbConnectorCheckpoint",
        back_populates="connector",
        uselist=False,
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    run_logs = relationship(
        "KbConnectorRunLog",
        back_populates="connector",
        cascade="all, delete-orphan",
        order_by="desc(KbConnectorRunLog.started_at)",
        lazy="select",
    )
    groups = relationship(
        "ResourceGroup",
        secondary=resource_group_kb_connectors,
        back_populates="kb_connectors",
        lazy="selectin",
    )
    collection = relationship(
        "KbCollection",
        foreign_keys=[collection_id],
        lazy="selectin",
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_kb_connectors_tenant_name"),
        CheckConstraint(
            "connector_type IN ("
            "'confluence','notion','google_drive','slack','github',"
            "'web_crawler','jira','sharepoint'"
            ")",
            name="ck_kb_connectors_type",
        ),
        CheckConstraint(
            "last_run_status IS NULL OR "
            "last_run_status IN ('success','failure','running')",
            name="ck_kb_connectors_status",
        ),
        # Partial index — sync workers only ever query enabled rows
        # ordered by next_run_at. Mirrors the migration.
        Index(
            "ix_kb_connectors_due",
            "enabled",
            "next_run_at",
            postgresql_where=Column("enabled"),
        ),
        Index("ix_kb_connectors_tenant", "tenant_id"),
    )

    def to_dict(self, include_credential: bool = False) -> dict:
        groups = list(self.groups or [])
        latest_run = None
        run_logs = self.run_logs or []
        if run_logs:
            latest_run = run_logs[0].to_dict()
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "description": self.description,
            "connector_type": self.connector_type,
            "config": self.config or {},
            "collection_id": self.collection_id,
            "schedule_cron": self.schedule_cron,
            "enabled": self.enabled,
            "next_run_at": (
                self.next_run_at.isoformat() if self.next_run_at else None
            ),
            "last_run_started_at": (
                self.last_run_started_at.isoformat()
                if self.last_run_started_at
                else None
            ),
            "last_run_finished_at": (
                self.last_run_finished_at.isoformat()
                if self.last_run_finished_at
                else None
            ),
            "last_run_status": self.last_run_status,
            "last_run_error": self.last_run_error,
            "latest_run": latest_run,
            "credential_kind": (
                self.credential.credential_kind if self.credential else None
            ),
            # The full encrypted payload is never returned via the API
            # — only the *kind* and whether one is configured. If a
            # caller specifically needs decoded creds (the sync
            # runtime, never the admin UI) it calls a dedicated
            # endpoint with explicit auth scoping. Connector types in
            # CREDENTIALLESS_CONNECTOR_TYPES (e.g. the Web Crawler,
            # which only hits unauthenticated HTTP) report configured
            # = true by definition so the admin UI doesn't disable
            # Sync Now on them.
            "credential_configured": (
                self.credential is not None
                or self.connector_type in CREDENTIALLESS_CONNECTOR_TYPES
            ),
            "group_ids": [g.id for g in groups],
            "groups": [
                {
                    "id": g.id,
                    "name": g.name,
                    "display_name": getattr(g, "display_name", None),
                    "parent_group_id": getattr(g, "parent_group_id", None),
                    "is_active": getattr(g, "is_active", None),
                }
                for g in groups
            ],
            "created_at": (
                self.created_at.isoformat() if self.created_at else None
            ),
            "updated_at": (
                self.updated_at.isoformat() if self.updated_at else None
            ),
            "created_by": self.created_by,
            "updated_by": self.updated_by,
        }

    def __repr__(self) -> str:
        return (
            f"<KbConnector(id={self.id!r}, tenant={self.tenant_id!r}, "
            f"type={self.connector_type!r}, name={self.name!r})>"
        )


# =============================================================================
# Credential (encrypted) — 1:1 with KbConnector
# =============================================================================


class KbConnectorCredential(Base):
    """Encrypted credential blob for one connector instance.

    ``encrypted_payload`` is Fernet-encrypted JSON. Shape depends on
    ``credential_kind``:

    - ``api_key``: ``{"token": "..."}``
    - ``oauth``: ``{"access_token": "...", "refresh_token": "...",
      "expires_at": "..."}``
    - ``service_account``: ``{"json": "..."}``

    Decryption is done at point-of-use only — either by the
    ``/v1/admin/kb-connectors/{id}/test-connection`` endpoint or by
    the Rust sync runtime when claiming a job. Never returned to
    the admin UI in plaintext.
    """

    __tablename__ = "kb_connector_credentials"

    id = Column(String(255), primary_key=True)

    connector_id = Column(
        String(255),
        ForeignKey("kb_connectors.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )

    credential_kind = Column(String(32), nullable=False)
    encrypted_payload = Column(Text, nullable=False)

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

    connector = relationship("KbConnector", back_populates="credential")

    __table_args__ = (
        CheckConstraint(
            "credential_kind IN ('api_key','oauth','service_account')",
            name="ck_kb_connector_credentials_kind",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<KbConnectorCredential(connector_id={self.connector_id!r}, "
            f"kind={self.credential_kind!r})>"
        )


# =============================================================================
# Checkpoint — 1:1 with KbConnector, runtime-owned
# =============================================================================


class KbConnectorCheckpoint(Base):
    """Incremental-sync cursor state.

    The Rust runtime writes here after each successful run with
    whatever cursor it needs to resume from (e.g.
    ``{"last_modified": "2026-05-23T10:00:00Z"}`` for Confluence,
    ``{"page_token": "..."}`` for Google Drive). Operators never
    touch this directly; a "force full re-sync" admin action
    clears the row.
    """

    __tablename__ = "kb_connector_checkpoints"

    connector_id = Column(
        String(255),
        ForeignKey("kb_connectors.id", ondelete="CASCADE"),
        primary_key=True,
    )
    cursor = Column(JSON, nullable=False, default=dict, server_default="{}")
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    connector = relationship("KbConnector", back_populates="checkpoint")

    def __repr__(self) -> str:
        return (
            f"<KbConnectorCheckpoint(connector_id={self.connector_id!r})>"
        )


# =============================================================================
# Run log — per-sync history
# =============================================================================


class KbConnectorRunLog(Base):
    """One row per sync run.

    Inserted at run start with ``status='running'``, updated on
    completion with the terminal status, counts, and any error
    message. Drives the admin UI's "last sync" status pill and the
    per-connector run-history view.
    """

    __tablename__ = "kb_connector_run_logs"

    id = Column(String(255), primary_key=True)

    connector_id = Column(
        String(255),
        ForeignKey("kb_connectors.id", ondelete="CASCADE"),
        nullable=False,
    )

    started_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    finished_at = Column(DateTime(timezone=True), nullable=True)

    status = Column(String(32), nullable=False)
    trigger = Column(String(32), nullable=False, default=RUN_TRIGGER_SCHEDULE, server_default="schedule")

    docs_seen = Column(Integer, nullable=False, default=0, server_default="0")
    docs_indexed = Column(Integer, nullable=False, default=0, server_default="0")
    docs_failed = Column(Integer, nullable=False, default=0, server_default="0")

    error_message = Column(Text, nullable=True)

    connector = relationship("KbConnector", back_populates="run_logs")

    __table_args__ = (
        CheckConstraint(
            "status IN ('success','failure','running','cancelled')",
            name="ck_kb_connector_run_logs_status",
        ),
        CheckConstraint(
            "trigger IN ('schedule','manual','initial')",
            name="ck_kb_connector_run_logs_trigger",
        ),
        Index(
            "ix_kb_connector_run_logs_connector_started",
            "connector_id",
            "started_at",
        ),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "connector_id": self.connector_id,
            "started_at": (
                self.started_at.isoformat() if self.started_at else None
            ),
            "finished_at": (
                self.finished_at.isoformat() if self.finished_at else None
            ),
            "status": self.status,
            "trigger": self.trigger,
            "docs_seen": self.docs_seen,
            "docs_indexed": self.docs_indexed,
            "docs_failed": self.docs_failed,
            "error_message": self.error_message,
        }

    def __repr__(self) -> str:
        return (
            f"<KbConnectorRunLog(id={self.id!r}, "
            f"connector_id={self.connector_id!r}, "
            f"status={self.status!r})>"
        )
