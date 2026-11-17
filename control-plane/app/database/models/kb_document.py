# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Knowledge Base Document Database Model

Represents an uploaded document within a KB collection. The original
text is extracted on upload and stored for potential re-chunking.
Actual vector embeddings live in Qdrant — this model holds metadata
and the extracted plain text.

Status lifecycle:
    pending → processing → ready
                        → error
    any     → removed

`removed` is set by the connector sync for a document whose source file was
deleted: the text columns are cleared and the row is kept until the KB
Uploader has deleted the document's vectors, and then the row. A removed
row is not a document of its collection any more, and nothing here puts it
back in the queue.
"""

from sqlalchemy import (
    CheckConstraint, Column, String, Boolean, DateTime, Integer, Text,
    LargeBinary, ForeignKey, Index,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from typing import Dict, Any

from app.database import Base


class KbDocument(Base):
    """
    A document uploaded to a Knowledge Base collection.

    Stores metadata (name, version, author, filename, content type)
    and the extracted plain text. Chunks and their Qdrant vectors
    are tracked via the KbChunk model.
    """

    __tablename__ = "kb_documents"

    # =========================================================================
    # IDENTITY
    # =========================================================================

    id = Column(String(255), primary_key=True)

    collection_id = Column(
        String(255),
        ForeignKey("kb_collections.id", ondelete="CASCADE"),
        nullable=False,
        comment="Parent KB collection",
    )

    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning tenant (denormalised for query efficiency)",
    )

    # =========================================================================
    # DOCUMENT METADATA
    # =========================================================================

    name = Column(
        String(512), nullable=False,
        comment="Display name (e.g. 'Employee Handbook Q2 2026')",
    )

    filename = Column(
        String(512), nullable=False,
        comment="Original upload filename",
    )

    version = Column(
        String(50), nullable=True,
        comment="Document version (e.g. '1.0', '2.3') — set by admin",
    )

    author = Column(
        String(255), nullable=True,
        comment="Document author (not the uploader)",
    )

    content_type = Column(
        String(100), nullable=False,
        comment="MIME type (application/pdf, text/markdown, text/plain, text/html)",
    )

    file_size_bytes = Column(
        Integer, nullable=True,
        comment="Original file size in bytes",
    )

    # =========================================================================
    # RAW FILE + EXTRACTED CONTENT
    # =========================================================================

    raw_content = Column(
        LargeBinary, nullable=True,
        comment="Original uploaded file bytes (PDF, DOCX, etc.). Processed by KB Uploader.",
    )

    original_content = Column(
        Text, nullable=True,
        comment="Extracted plain text (written by KB Uploader after extraction, used for re-chunking)",
    )

    # =========================================================================
    # PROCESSING STATUS
    # =========================================================================

    status = Column(
        String(20), default="pending", nullable=False,
        comment=(
            "Processing status: pending, processing, ready, error, removed "
            "(removed at the source; being purged)"
        ),
    )

    error_message = Column(
        Text, nullable=True,
        comment="Error details when status is 'error'",
    )

    # server_default as in the migration: raw-SQL writers (the connector sync
    # emitter) bypass the ORM default, and a NULL here would fail the document list.
    chunk_count = Column(
        Integer, default=0, server_default="0", nullable=False,
        comment="Number of chunks generated from this document",
    )

    embedding_model = Column(
        String(255), nullable=True,
        comment="Embedding model used when this document was last embedded",
    )

    # =========================================================================
    # CONNECTOR-SOURCED METADATA
    #
    # These columns let the same kb_documents table carry both
    # user-uploaded files ("source='upload'", legacy default) and rows
    # emitted by the KB Connector Sync service ("source='connector'"). The
    # SQLAlchemy model must declare them or `Base.metadata.create_all()`
    # on a fresh DB will build the table without them, and the
    # kb-uploader's RETURNING clause will fail with
    # "column 'source' does not exist".
    # =========================================================================

    source = Column(
        String(32), nullable=False, server_default="upload",
        comment="'upload' (user file) | 'connector' (KB connector sync)",
    )

    source_url = Column(
        String(2048), nullable=True,
        comment="Canonical URL back to the source — surfaced in chat citations",
    )

    source_updated_at = Column(
        DateTime(timezone=True), nullable=True,
        comment="When the source last mutated — drives time-windowed filters",
    )

    connector_doc_key = Column(
        String(512), nullable=True,
        comment=(
            "'{connector_id}::{source_doc_id}' — upsert key for "
            "connector-emitted docs; NULL for user uploads."
        ),
    )

    acl = Column(
        JSONB(), nullable=True,
        comment="Principal IDs that may see this doc (e.g. ['user:42', 'group:eng'])",
    )

    metadata_json = Column(
        JSONB(), nullable=True,
        comment="Free-form per-source metadata (space, channel, repo, …)",
    )

    # =========================================================================
    # RELATIONSHIPS
    # =========================================================================

    collection = relationship(
        "KbCollection",
        back_populates="documents",
    )

    chunks = relationship(
        "KbChunk",
        back_populates="document",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )

    # =========================================================================
    # TIMESTAMPS
    # =========================================================================

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    created_by = Column(String(255), nullable=True, comment="Admin who uploaded this document")
    updated_by = Column(String(255), nullable=True, comment="Admin who last updated metadata")

    __table_args__ = (
        Index("ix_kbd_collection", "collection_id"),
        Index("ix_kbd_tenant", "tenant_id"),
        Index("ix_kbd_status", "tenant_id", "status"),
        # Mirrors the migration's partial unique index — connector-emitted rows
        # upsert on this key; user uploads have NULL and are excluded.
        Index(
            "uq_kb_documents_connector_doc_key",
            "connector_doc_key",
            unique=True,
            postgresql_where=Column("connector_doc_key").isnot(None),
        ),
        CheckConstraint(
            "source IN ('upload','connector')",
            name="ck_kb_documents_source",
        ),
        {"comment": "Documents uploaded to Knowledge Base collections"},
    )

    # =========================================================================
    # HELPERS
    # =========================================================================

    STATUS_REMOVED = "removed"
    VALID_STATUSES = {"pending", "processing", "ready", "error", STATUS_REMOVED}
    # The statuses a document can be queued again from (a re-embed sets
    # `pending`). A removed document is being purged: it is never queued.
    REQUEUEABLE_STATUSES = VALID_STATUSES - {STATUS_REMOVED}
    # What a status says beyond its name, where it needs saying.
    STATUS_NOTES = {STATUS_REMOVED: "removed at the source; being purged"}
    SUPPORTED_CONTENT_TYPES = {
        "application/pdf",
        "text/plain",
        "text/markdown",
        "text/html",
        "text/csv",                                                                   # .csv
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",   # .docx
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",          # .xlsx
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",  # .pptx
        "application/msword",                                                         # .doc
        "application/vnd.ms-excel",                                                   # .xls
        "application/vnd.ms-powerpoint",                                              # .ppt
    }

    def to_dict(self) -> Dict[str, Any]:
        """Serialise for API responses."""
        return {
            "id": self.id,
            "collection_id": self.collection_id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "filename": self.filename,
            "version": self.version,
            "author": self.author,
            "content_type": self.content_type,
            "file_size_bytes": self.file_size_bytes,
            "status": self.status,
            "error_message": self.error_message,
            "chunk_count": self.chunk_count,
            "embedding_model": self.embedding_model,
            "source": self.source,
            "source_url": self.source_url,
            "source_updated_at": (
                self.source_updated_at.isoformat()
                if self.source_updated_at else None
            ),
            "connector_doc_key": self.connector_doc_key,
            "acl": self.acl,
            "metadata_json": self.metadata_json,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "created_by": self.created_by,
            "updated_by": self.updated_by,
        }

    def __repr__(self) -> str:
        return (
            f"<KbDocument(id={self.id!r}, name={self.name!r}, "
            f"collection={self.collection_id!r}, status={self.status!r})>"
        )
