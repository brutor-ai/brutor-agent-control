# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Knowledge Base Chunk Database Model

A chunk is a segment of text extracted from a KbDocument. Each chunk
has a corresponding vector in Qdrant (tracked via qdrant_point_id).

Chunks are created during the document ingestion pipeline:
    document → text extraction → chunking → embedding → Qdrant upsert
"""

from sqlalchemy import (
    Column, String, DateTime, Integer, Text, JSON,
    ForeignKey, Index,
)
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from typing import Dict, Any

from app.database import Base


class KbChunk(Base):
    """
    A text chunk from a Knowledge Base document.

    Each chunk corresponds to a vector stored in Qdrant. The chunk
    text is stored here for display in admin search results and for
    re-embedding when the embedding model changes.
    """

    __tablename__ = "kb_chunks"

    # =========================================================================
    # IDENTITY
    # =========================================================================

    id = Column(String(255), primary_key=True)

    document_id = Column(
        String(255),
        ForeignKey("kb_documents.id", ondelete="CASCADE"),
        nullable=False,
        comment="Parent document",
    )

    tenant_id = Column(
        String(255), nullable=False,
        comment="Owning tenant (denormalised for query efficiency)",
    )

    # =========================================================================
    # CHUNK DATA
    # =========================================================================

    chunk_index = Column(
        Integer, nullable=False,
        comment="Zero-based position of this chunk within the document",
    )

    content = Column(
        Text, nullable=False,
        comment="The chunk text",
    )

    token_count = Column(
        Integer, nullable=True,
        comment="Token count of the chunk (using the embedding model's tokeniser)",
    )

    # =========================================================================
    # QDRANT REFERENCE
    # =========================================================================

    qdrant_point_id = Column(
        String(255), nullable=True,
        comment="UUID of the corresponding point in Qdrant (null if not yet embedded)",
    )

    # =========================================================================
    # METADATA
    # =========================================================================

    metadata_json = Column(
        JSON, nullable=True,
        comment="Source metadata: page number, heading, section title, etc.",
    )

    # =========================================================================
    # RELATIONSHIPS
    # =========================================================================

    document = relationship(
        "KbDocument",
        back_populates="chunks",
    )

    # =========================================================================
    # TIMESTAMPS
    # =========================================================================

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_kbch_document", "document_id"),
        Index("ix_kbch_tenant", "tenant_id"),
        Index("ix_kbch_qdrant", "qdrant_point_id"),
        {"comment": "Text chunks from Knowledge Base documents with Qdrant vector references"},
    )

    # =========================================================================
    # HELPERS
    # =========================================================================

    def to_dict(self) -> Dict[str, Any]:
        """Serialise for API responses."""
        return {
            "id": self.id,
            "document_id": self.document_id,
            "chunk_index": self.chunk_index,
            "content": self.content,
            "token_count": self.token_count,
            "qdrant_point_id": self.qdrant_point_id,
            "metadata": self.metadata_json,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:
        return (
            f"<KbChunk(id={self.id!r}, doc={self.document_id!r}, "
            f"idx={self.chunk_index}, tokens={self.token_count})>"
        )
