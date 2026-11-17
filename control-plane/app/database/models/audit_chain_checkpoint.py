# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Audit chain checkpoints — signed heads of the `proxy_logs` hash chains.

Each `proxy_logs` row carries `row_hash = SHA-256(canonical bytes ||
prev_hash)`, so altering any row breaks every hash after it. That alone
detects *modification* but not wholesale rewriting: an attacker with write
access could recompute the whole chain. Signing fixes that — but signing
every row would cost an Ed25519 signature per record, so the writer signs the
**head** of each flushed batch instead. With batches up to 500 rows that is
one signature per 500 records, and the property is the same: to forge a row
you must forge every subsequent row hash *and* a signed checkpoint, which
requires the private key.

Deliberately **not tenant-scoped.** A chain belongs to a writer process, not
a tenant, and a tenant's rows interleave with every other tenant's in the
same chain. Partitioning checkpoints per tenant would mean a tenant-scoped
deletion could silently renumber another tenant's evidence.

`public_key` is stored per checkpoint rather than looked up globally so that
key rotation does not invalidate history: a key that has been rotated out
must still verify what it signed while it was live.
"""

from sqlalchemy import BigInteger, Column, DateTime, Index, String, func

from app.database import Base


class AuditChainCheckpoint(Base):
    """One signed attestation over a contiguous range of one chain."""

    __tablename__ = "audit_chain_checkpoints"

    id = Column(String(64), primary_key=True)

    chain_id = Column(
        String(64),
        nullable=False,
        comment="Writer chain this checkpoint attests to.",
    )
    seq_start = Column(
        BigInteger,
        nullable=False,
        comment="First chain_seq covered, inclusive.",
    )
    seq_end = Column(
        BigInteger,
        nullable=False,
        comment="Last chain_seq covered, inclusive.",
    )
    head_hash = Column(
        String(64),
        nullable=False,
        comment=(
            "row_hash at seq_end. Because each hash covers its predecessor, "
            "signing this attests to every row in the range."
        ),
    )
    signature = Column(
        String(128),
        nullable=False,
        comment="Ed25519 signature over the canonical checkpoint bytes, hex.",
    )
    public_key = Column(
        String(64),
        nullable=False,
        comment=(
            "Verifying key, hex. Per checkpoint so a rotated-out key still "
            "verifies what it signed."
        ),
    )
    signed_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_audit_chain_checkpoints_chain", "chain_id", "seq_end"),
        {
            "comment": (
                "Signed heads of the proxy_logs hash chains. Not "
                "tenant-scoped: a chain is a property of the writer."
            )
        },
    )

    def to_dict(self):
        return {
            "id": self.id,
            "chain_id": self.chain_id,
            "seq_start": self.seq_start,
            "seq_end": self.seq_end,
            "head_hash": self.head_hash,
            "signature": self.signature,
            "public_key": self.public_key,
            "signed_at": self.signed_at.isoformat() if self.signed_at else None,
        }

    def __repr__(self):
        return (
            f"<AuditChainCheckpoint {self.chain_id} "
            f"[{self.seq_start}..{self.seq_end}]>"
        )
