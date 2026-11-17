# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Knowledge Base Collection Database Model

A collection groups related documents for RAG (Retrieval-Augmented
Generation). Documents in a collection are chunked, embedded, and
stored in Qdrant for semantic retrieval at inference time.

Collections are associated with Resource Groups — exactly like
SemanticCacheConfig and GuardrailConfig. A collection assigned to
"HR" is only searchable by users in the HR group.

Association table:
    resource_group_kb_collections  (resource_group_id, kb_collection_id)
"""

from sqlalchemy import (
    Column, String, Boolean, DateTime, Float, Table,
    ForeignKey, Index, Integer, Text,
)
from sqlalchemy.sql import func
from sqlalchemy.orm import validates, relationship
from typing import Dict, Any, List

from app.database import Base

# Bounds of a collection's chunk size, in tokens; the overlap is 0 to size - 1.
CHUNK_SIZE_MIN = 50
CHUNK_SIZE_MAX = 8000
DEFAULT_CHUNK_SIZE_TOKENS = 500
DEFAULT_CHUNK_OVERLAP_TOKENS = 50
from app.database.models.resource_mixins import (
    ResourceAuthorshipMixin,
    ResourceVersionMixin,
)


# =============================================================================
# Many-to-many association table (matches resource_group_cache_configs pattern)
# =============================================================================

resource_group_kb_collections = Table(
    "resource_group_kb_collections",
    Base.metadata,
    Column(
        "resource_group_id",
        String(255),
        ForeignKey("resource_groups.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "kb_collection_id",
        String(255),
        ForeignKey("kb_collections.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    comment="Associates KB collections with resource groups for RAG scoping",
)


# See ResourceGroup: the mixins, not a local copy, so the flush listener
# recognises this model and stamps it.
class KbCollection(ResourceAuthorshipMixin, ResourceVersionMixin, Base):
    """
    Knowledge Base collection — assignable to Resource Groups.

    Each collection contains documents that are chunked, embedded, and
    stored in Qdrant for semantic retrieval. RAG settings (top_k,
    similarity threshold, context budget) are per-collection.

    Isolation: a collection is only searchable by users in resource
    groups associated with it. Multiple groups can share a collection
    (e.g., "Company FAQ" shared by HR and Finance).
    """

    __tablename__ = "kb_collections"

    # =========================================================================
    # IDENTITY
    # =========================================================================

    id = Column(String(255), primary_key=True)

    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning tenant",
    )

    name = Column(
        String(255), nullable=False,
        comment="Human-readable name (unique within tenant)",
    )

    description = Column(
        String(1024), nullable=True,
        comment="Admin-facing description of this collection's purpose",
    )

    # =========================================================================
    # RESOURCE GROUP ASSOCIATION
    # =========================================================================

    groups = relationship(
        "ResourceGroup",
        secondary=resource_group_kb_collections,
        back_populates="kb_collections",
        lazy="selectin",
    )

    # =========================================================================
    # GLOBAL TOGGLE
    # =========================================================================

    enabled = Column(
        Boolean, default=True,
        comment="Master switch — False disables RAG retrieval for this collection",
    )

    # =========================================================================
    # RAG RETRIEVAL SETTINGS
    # =========================================================================

    top_k = Column(
        Integer, default=5,
        comment="Number of top matching chunks to retrieve per query",
    )

    similarity_threshold = Column(
        Float, default=0.75,
        comment="Minimum cosine similarity score to include a chunk (0.0-1.0)",
    )

    max_context_tokens = Column(
        Integer, default=2000,
        comment="Maximum total tokens of retrieved context injected into the prompt",
    )

    # =========================================================================
    # CHUNKING (read by the KB Uploader for each document it claims)
    # =========================================================================
    # Tokens are estimated as characters / 4 by the uploader. A change
    # queues the collection's documents again (`update_collection`).

    chunk_size_tokens = Column(
        Integer, nullable=False, default=DEFAULT_CHUNK_SIZE_TOKENS, server_default=str(DEFAULT_CHUNK_SIZE_TOKENS),
        comment="Size of a chunk the KB Uploader cuts a document of this collection into, in tokens (50-8000)",
    )

    chunk_overlap_tokens = Column(
        Integer, nullable=False, default=DEFAULT_CHUNK_OVERLAP_TOKENS, server_default=str(DEFAULT_CHUNK_OVERLAP_TOKENS),
        comment="Tokens two consecutive chunks share (0 to chunk_size_tokens - 1)",
    )

    system_prompt_template = Column(
        Text, nullable=True,
        comment=(
            "Template for injecting context into the system message. "
            "Use {context} as placeholder. Default: "
            "'Use the following context to answer the user\\'s question:\\n\\n{context}'"
        ),
    )

    # =========================================================================
    # EMBEDDING MODEL
    # =========================================================================
    # NOTE: embedding_model_id was removed in migration
    # a001_primary_group_tenant_embedding. The tenant-wide default stored on
    # Tenant.default_embedding_model_id applies to every collection and
    # cache config in the tenant. This guarantees a single vector space
    # per tenant and avoids cross-model query-time fan-out.

    # =========================================================================
    # DOCUMENTS RELATIONSHIP
    # =========================================================================

    documents = relationship(
        "KbDocument",
        back_populates="collection",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )

    # =========================================================================
    # TIMESTAMPS
    # =========================================================================

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_kbc_tenant", "tenant_id"),
        Index("ix_kbc_tenant_name", "tenant_id", "name", unique=True),
        {"comment": "Knowledge Base collections for RAG document retrieval"},
    )

    # =========================================================================
    # VALIDATION
    # =========================================================================

    @validates("similarity_threshold")
    def validate_similarity_threshold(self, key, value):
        if value is not None and not (0.0 <= value <= 1.0):
            raise ValueError(f"{key} must be between 0.0 and 1.0")
        return value

    @validates("top_k")
    def validate_top_k(self, key, value):
        if value is not None and not (1 <= value <= 50):
            raise ValueError(f"{key} must be between 1 and 50")
        return value

    @validates("max_context_tokens")
    def validate_max_context_tokens(self, key, value):
        if value is not None and not (100 <= value <= 32000):
            raise ValueError(f"{key} must be between 100 and 32000")
        return value

    @validates("chunk_size_tokens")
    def validate_chunk_size_tokens(self, key, value):
        if value is not None and not (CHUNK_SIZE_MIN <= value <= CHUNK_SIZE_MAX):
            raise ValueError(f"{key} must be between {CHUNK_SIZE_MIN} and {CHUNK_SIZE_MAX}")
        return value

    @validates("chunk_overlap_tokens")
    def validate_chunk_overlap_tokens(self, key, value):
        if value is not None and value < 0:
            raise ValueError(f"{key} must be 0 or more")
        return value

    # =========================================================================
    # HELPERS
    # =========================================================================

    @property
    def group_ids(self) -> List[str]:
        """Return list of associated resource group IDs."""
        return [g.id for g in (self.groups or [])]

    @property
    def document_count(self) -> int:
        """The number of documents in this collection. A document that was
        removed at its source and is being purged is not one of them."""
        if self.documents is not None:
            try:
                from app.database.models.kb_document import KbDocument

                return self.documents.filter(KbDocument.status != KbDocument.STATUS_REMOVED).count()
            except Exception:
                return 0
        return 0

    def to_dict(self) -> Dict[str, Any]:
        """Serialise for API responses."""
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "description": self.description,
            "enabled": self.enabled,
            "top_k": self.top_k,
            "similarity_threshold": self.similarity_threshold,
            "max_context_tokens": self.max_context_tokens,
            "chunk_size_tokens": self.chunk_size_tokens,
            "chunk_overlap_tokens": self.chunk_overlap_tokens,
            "system_prompt_template": self.system_prompt_template,
            "group_ids": self.group_ids,
            "groups": [
                {"id": g.id, "name": g.name, "display_name": getattr(g, "display_name", None)}
                for g in (self.groups or [])
            ],
            "document_count": self.document_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "version": self.version,
            "created_by": self.created_by,
            "updated_by": self.updated_by,
        }

    def __repr__(self) -> str:
        return (
            f"<KbCollection(id={self.id!r}, name={self.name!r}, "
            f"tenant={self.tenant_id!r}, enabled={self.enabled})>"
        )