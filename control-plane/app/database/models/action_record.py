# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Action records — one sealed record per governed verdict

A gateway that only seals successes cannot prove its gates ever fired, so
every verdict — executed, blocked, denied, awaiting a human, aborted — gets
a record. The record is JSON (`ai.brutor/action-record/v1`), its id is the
SHA-256 of its JCS canonical form, and `statement` is the COSE Signed
Statement (RFC 9943, hash-envelope mode) the core produced over it. The
control plane never writes these rows: sealing lives on the hot path in the
Rust core, which also holds the key. This model exists so the Evidence
Ledger can read them.

`record` (the body) and `tags` are prunable under retention; `record_id`,
`seq` and `statement` are not — a pruned record still verifies against the
tenant log, it just no longer says what it was about.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import Any, Dict

from sqlalchemy import BigInteger, Boolean, Column, Computed, DateTime, Index, LargeBinary, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import validates

from app.database import Base

# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

KIND_DECIDE = "decide"
KIND_FYI = "fyi"
RECORD_KINDS = frozenset({KIND_DECIDE, KIND_FYI})

VERDICT_EXECUTED = "executed"
VERDICT_ERRORED = "errored"
VERDICT_TIMEOUT = "timeout"
VERDICT_BLOCKED = "blocked"
VERDICT_DENIED = "denied"
VERDICT_ENGINE_FAILURE = "engine_failure"
VERDICT_AWAITING_HUMAN = "awaiting_human"
VERDICT_RESOLVED = "resolved"
VERDICT_EXPIRED = "expired"
VERDICT_EPOCH_BOUNDARY = "epoch_boundary"
VERDICT_ASSESSMENT = "assessment"
VERDICT_ADJUDICATION = "adjudication"
#: An `fyi` statement: declarations, notices, rubrics, reports, incident
#: changes, row-chain heads, bundles, key rotations.
VERDICT_RECORDED = "recorded"
#: The first half of a two-phase action:
#: the gates accepted it and it is being dispatched. `effect.status: planned`,
#: no digests; the outcome record chains `confirms` to it. Non-dispatching as a
#: record — it attests the decision, not the effect.
VERDICT_PLANNED = "planned"
RECORD_VERDICTS = frozenset({
    VERDICT_EXECUTED,
    VERDICT_ERRORED,
    VERDICT_TIMEOUT,
    VERDICT_BLOCKED,
    VERDICT_DENIED,
    VERDICT_ENGINE_FAILURE,
    VERDICT_AWAITING_HUMAN,
    VERDICT_RESOLVED,
    VERDICT_EXPIRED,
    VERDICT_EPOCH_BOUNDARY,
    VERDICT_ASSESSMENT,
    VERDICT_ADJUDICATION,
    VERDICT_RECORDED,
    VERDICT_PLANNED,
})

#: Verdicts that never dispatch an effect.
NON_DISPATCHING_VERDICTS = frozenset({
    VERDICT_BLOCKED,
    VERDICT_DENIED,
    VERDICT_ENGINE_FAILURE,
    VERDICT_AWAITING_HUMAN,
    VERDICT_RESOLVED,
    VERDICT_EXPIRED,
    VERDICT_EPOCH_BOUNDARY,
    VERDICT_ASSESSMENT,
    VERDICT_ADJUDICATION,
    VERDICT_RECORDED,
    VERDICT_PLANNED,
})

RECORD_SCHEMA = "ai.brutor/action-record/v1"

#: Derived effect modes (the verifier's rule).
EFFECT_MODE_NOT_APPLICABLE = "not_applicable"
EFFECT_MODE_CONFIRMED = "confirmed"
EFFECT_MODE_DISPATCHED_UNCONFIRMED = "dispatched_unconfirmed"


def _is_hex64(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


def derive_effect_mode(effect: Any) -> str:
    """The verifier's derived effect mode: no `effect` or a
    `planned` one ⇒ `not_applicable`; `confirmed` with a 64-hex
    `response_digest` ⇒ `confirmed`; anything else ⇒ `dispatched_unconfirmed`.
    Unknown never grades up. A display projection — the core's verifier is
    the authority and flags a mode that contradicts the verdict."""
    if not isinstance(effect, dict) or not effect:
        return EFFECT_MODE_NOT_APPLICABLE
    status = effect.get("status")
    if status == "planned":
        return EFFECT_MODE_NOT_APPLICABLE
    if status == "confirmed" and _is_hex64(effect.get("response_digest")):
        return EFFECT_MODE_CONFIRMED
    return EFFECT_MODE_DISPATCHED_UNCONFIRMED


# ---------------------------------------------------------------------------
# Generated projections of the record body
# ---------------------------------------------------------------------------
#
# Four facts live only inside the JSONB body and are asked for by the
# compliance pages on every load. Asking the body means reading (and
# de-toasting) every record of the tenant: 3 s for "which records confirm these
# planned records" on 33,000 records, 0.8 s for the record lag, 2.5 s for the
# ledger summary. They are stored generated columns: the database computes them
# from the body on every write, so they cannot disagree with it and no writer
# (the core's INSERT, a prune's UPDATE) has to know they exist.
#
# The expressions use only operators Postgres and SQLite share (`->`, `->>`),
# because the test harness builds this table on SQLite. `-> … = 'true'`
# compares the JSON value itself (jsonb `true` on Postgres, the JSON text
# `true` on SQLite); `->>` would give SQLite the integer 1.
CHAIN_PARENT_EXPR = "(record -> 'chain' ->> 'parent_record_id')"
CHAIN_RELATION_EXPR = "(record -> 'chain' ->> 'relation')"
HUMAN_DISPOSED_EXPR = "COALESCE((record -> 'disposition' -> 'human_disposed') = 'true', false)"
BACKFILLED_EXPR = "COALESCE((record -> 'context' -> 'backfilled') = 'true', false)"


class ActionRecord(Base):
    """One sealed record for one governed verdict. Written by the core."""

    __tablename__ = "action_records"

    record_id = Column(
        String(64),
        primary_key=True,
        comment="Lowercase-hex SHA-256 of the JCS form of the record minus this member",
    )
    tenant_id = Column(String(255), nullable=False)
    log_id = Column(String(64), nullable=False, comment="The tenant log this record is a leaf of")
    seq = Column(BigInteger, nullable=False, comment="Leaf index in the tenant log")

    ai_system_id = Column(String(255), nullable=True)
    root_task_id = Column(String(255), nullable=True)
    proxy_log_id = Column(String(255), nullable=True, comment="The ledger row this record seals")

    verdict = Column(String(32), nullable=False)
    kind = Column(String(8), nullable=False, comment="decide | fyi")
    surface = Column(String(32), nullable=False, comment="mcp | a2a | skill | llm | approval | …")
    epoch = Column(String(96), nullable=True, comment="contract:<contract_hash>")
    timestamp = Column(DateTime(timezone=True), nullable=False)

    tags = Column(JSONB, nullable=True, comment="Compliance tags for the row (prunable)")
    record = Column(JSONB, nullable=False, comment="The ai.brutor/action-record/v1 body (prunable)")
    statement = Column(
        LargeBinary,
        nullable=False,
        comment="COSE_Sign1 Signed Statement over the record (RFC 9943, hash envelope)",
    )
    kid = Column(String(64), nullable=False, comment="Key id the statement was signed with")

    # Generated from the body; never written (see the note above).
    chain_parent_record_id = Column(
        String(64), Computed(CHAIN_PARENT_EXPR, persisted=True),
        comment="The record this one confirms / supersedes (body: chain.parent_record_id)",
    )
    chain_relation = Column(
        String(32), Computed(CHAIN_RELATION_EXPR, persisted=True),
        comment="How this record relates to its parent (body: chain.relation)",
    )
    human_disposed = Column(
        Boolean, Computed(HUMAN_DISPOSED_EXPR, persisted=True),
        comment="A person disposed of the action (body: disposition.human_disposed)",
    )
    backfilled = Column(
        Boolean, Computed(BACKFILLED_EXPR, persisted=True),
        comment="Sealed late by the backfill, not at verdict time (body: context.backfilled)",
    )

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_action_records_tenant_ts", "tenant_id", "timestamp"),
        Index("uq_action_records_log_seq", "log_id", "seq", unique=True),
        Index("ix_action_records_system", "tenant_id", "ai_system_id", "timestamp"),
        Index("ix_action_records_run", "tenant_id", "root_task_id"),
        Index("ix_action_records_verdict", "tenant_id", "verdict"),
        # The parent lookup, without touching the heap's bodies, and the
        # human-disposed count.
        Index(
            "ix_action_records_chain_parent",
            "chain_parent_record_id",
            postgresql_include=["record_id", "chain_relation", "tenant_id", "timestamp"],
            postgresql_where=text("chain_parent_record_id IS NOT NULL"),
        ),
        Index(
            "ix_action_records_human_disposed",
            "tenant_id",
            "timestamp",
            postgresql_where=text("human_disposed"),
        ),
    )

    @validates("kind")
    def _validate_kind(self, _key, value):
        if value not in RECORD_KINDS:
            raise ValueError(f"kind must be one of {sorted(RECORD_KINDS)}, got {value!r}")
        return value

    @validates("verdict")
    def _validate_verdict(self, _key, value):
        if value not in RECORD_VERDICTS:
            raise ValueError(
                f"verdict must be one of {sorted(RECORD_VERDICTS)}, got {value!r}"
            )
        return value

    # -- projections ------------------------------------------------------

    def summary_dict(self) -> Dict[str, Any]:
        """The list-row projection: index columns plus the few record members
        an explorer filters on, without the body or the statement."""
        body = self.record if isinstance(self.record, dict) else {}
        effect = body.get("effect") if isinstance(body.get("effect"), dict) else {}
        disposition = (
            body.get("disposition") if isinstance(body.get("disposition"), dict) else {}
        )
        chain = body.get("chain") if isinstance(body.get("chain"), dict) else None
        return {
            "record_id": self.record_id,
            "log_id": self.log_id,
            "seq": self.seq,
            "ai_system_id": self.ai_system_id,
            "root_task_id": self.root_task_id,
            "proxy_log_id": self.proxy_log_id,
            "verdict": self.verdict,
            "kind": self.kind,
            "surface": self.surface,
            "epoch": self.epoch,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "tags": self.tags,
            "action_id": body.get("action_id"),
            "effect_type": effect.get("type"),
            "effect_status": effect.get("status"),
            # A pruned body says nothing about the effect: no mode, not a guess.
            "effect_mode": derive_effect_mode(body.get("effect")) if body else None,
            "human_disposed": disposition.get("human_disposed"),
            "chain": chain,
        }

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe: the statement travels as base64, never raw bytes."""
        data = self.summary_dict()
        data["record"] = self.record
        data["statement_b64"] = (
            base64.b64encode(self.statement).decode("ascii") if self.statement else None
        )
        data["kid"] = self.kid
        data["created_at"] = self.created_at.isoformat() if self.created_at else None
        return data


__all__ = [
    "ActionRecord",
    "derive_effect_mode",
    "RECORD_SCHEMA",
    "KIND_DECIDE",
    "KIND_FYI",
    "RECORD_KINDS",
    "RECORD_VERDICTS",
    "NON_DISPATCHING_VERDICTS",
    "VERDICT_EXECUTED",
    "VERDICT_ERRORED",
    "VERDICT_TIMEOUT",
    "VERDICT_BLOCKED",
    "VERDICT_DENIED",
    "VERDICT_ENGINE_FAILURE",
    "VERDICT_AWAITING_HUMAN",
    "VERDICT_RESOLVED",
    "VERDICT_EXPIRED",
    "VERDICT_EPOCH_BOUNDARY",
    "VERDICT_ASSESSMENT",
    "VERDICT_ADJUDICATION",
]
