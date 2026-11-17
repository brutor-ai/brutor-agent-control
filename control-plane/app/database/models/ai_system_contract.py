# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# AI System contracts and autonomy

The registry, the run ledger, liveness and drift answer *what is this
system*, *what did it do*, *is it alive* and *has its behaviour changed*.
Contracts answer the question a risk
committee asks first and that none of the others can:

> *"What, exactly, was this system allowed to do — and can you prove it
> was the same thing on the day it ran?"*

## A contract is generated, never authored

The single most important property here is that a contract is a
**serialization of the live resolved configuration**, not a document
someone writes alongside it. A policy document that can disagree with
reality is worse than no document at all: it gives an auditor false
confidence and gives an operator a second place to keep in sync.

Because an AI System *is* a resource group, the contract is the
**resolved closure of that group** — inheritance included. Resources
compose additively gated by `inherit_from_parent`; limits and policies
compose restrictively, min-wins. Both are rules the platform already
implements, so the contract is a rendering of an existing computation
rather than a second opinion about it.

    contract_hash = SHA256(canonical_json(resolved_closure))

Stamped on every run. That is what turns "we have governance" into:

> *"Every one of the 14,203 runs this AI System executed in Q3 was
> governed by contract v4 / sha256:9f2c…; here is the diff to v3, who
> approved it, and when."*

## Append-only, and why

Contracts are never updated in place. A superseded contract is the only
evidence of what the rules were while historical runs were executing, and
a mutable contract row would silently rewrite the past every time someone
changed a limit. `status` moves forward; the row does not change.

## Autonomy is not enforcement mode

`agent_identities.enforcement_mode` (`enforce | dry_run`) is about whether
policy *bites*. Autonomy is a different axis and belongs on the AI System:
how much the system may do without a human. They are orthogonal — a
dry-run system can still be `suspended`, and an enforcing one can still be
fully `autonomous`.

Resolution is **min-wins across the RG hierarchy and the actor**, matching
how limits already resolve. An agent identity may be *more* restricted
than its AI System, never less. Suspending an AI System therefore suspends
every actor inside it, which is exactly what an operator means by "turn it
off".
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import validates

from app.database import Base

# ---------------------------------------------------------------------------
# Contract lifecycle
# ---------------------------------------------------------------------------

CONTRACT_DRAFT = "draft"          # minted, not yet promoted
CONTRACT_ACTIVE = "active"        # governing runs right now
CONTRACT_SUPERSEDED = "superseded"  # was active; kept as evidence
CONTRACT_REJECTED = "rejected"    # reviewed and refused

CONTRACT_STATUSES = frozenset({
    CONTRACT_DRAFT,
    CONTRACT_ACTIVE,
    CONTRACT_SUPERSEDED,
    CONTRACT_REJECTED,
})

# Statuses in which a contract still explains a run that is executing now.
CONTRACT_OPEN_STATUSES = frozenset({CONTRACT_DRAFT, CONTRACT_ACTIVE})

# ---------------------------------------------------------------------------
# Autonomy
# ---------------------------------------------------------------------------

AUTONOMY_AUTONOMOUS = "autonomous"              # runs unattended
AUTONOMY_APPROVAL_REQUIRED = "approval_required"  # side effects need a human
AUTONOMY_RESTRICTED = "restricted"              # read-only; effects denied
AUTONOMY_SUSPENDED = "suspended"                # everything denied

AUTONOMY_LEVELS = frozenset({
    AUTONOMY_AUTONOMOUS,
    AUTONOMY_APPROVAL_REQUIRED,
    AUTONOMY_RESTRICTED,
    AUTONOMY_SUSPENDED,
})

#: Restrictiveness order. **Lower is more restrictive**, so resolution is a
#: plain `min` over the hierarchy — the same shape limits already use, which
#: is why it is expressed as a rank rather than as bespoke comparison code.
#:
#: Mirrored in `brutor-gateway-core/src/services/autonomy.rs`.
AUTONOMY_RANK: Dict[str, int] = {
    AUTONOMY_SUSPENDED: 0,
    AUTONOMY_RESTRICTED: 1,
    AUTONOMY_APPROVAL_REQUIRED: 2,
    AUTONOMY_AUTONOMOUS: 3,
}

#: The default for a system nobody has classified.
#:
#: Deliberately the *permissive* end, and worth being explicit about why:
#: this column describes how much supervision a system needs, and the
#: platform has no way to infer that. Defaulting to `approval_required`
#: would silently interpose a human in every side-effecting call on every
#: existing system the moment the column landed — a governance control
#: nobody chose, arriving as a migration. Tightening autonomy is an
#: operator's deliberate act; the promotion gate is what makes them face
#: the decision at promotion time.
DEFAULT_AUTONOMY_LEVEL = AUTONOMY_AUTONOMOUS


def autonomy_rank(level: Any) -> int:
    """The rank of an autonomy level. One the gateway does not know (a typo,
    one a newer release added, a value that is not text) it reads as
    `suspended` (`autonomy::resolve`), so it ranks as `suspended` here too."""
    if isinstance(level, str) and level in AUTONOMY_RANK:
        return AUTONOMY_RANK[level]
    return AUTONOMY_RANK[AUTONOMY_SUSPENDED]


def resolve_autonomy(*levels: Optional[str]) -> str:
    """Min-wins across an ordered chain of levels.

    `None` contributes nothing, so an unset group cannot loosen a parent. A
    level the gateway does not know is read as `suspended`, as it reads it.
    """
    ranked = [autonomy_rank(level) for level in levels if level is not None]
    if not ranked:
        return DEFAULT_AUTONOMY_LEVEL
    lowest = min(ranked)
    for name, rank in AUTONOMY_RANK.items():
        if rank == lowest:
            return name
    return DEFAULT_AUTONOMY_LEVEL


def allows_side_effects(level: str) -> bool:
    """Whether a level permits an effecting tool call without a human."""
    return level == AUTONOMY_AUTONOMOUS


def denies_everything(level: str) -> bool:
    return autonomy_rank(level) == AUTONOMY_RANK[AUTONOMY_SUSPENDED]


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def canonical_json(payload: Any) -> str:
    """The exact bytes the contract hash is taken over.

    Sorted keys, no incidental whitespace, non-ASCII preserved rather than
    escaped. The hash is a *claim* — "these were the rules" — so two
    resolutions of the same configuration must produce the same bytes
    regardless of dict ordering, and any real difference must produce
    different bytes.

    Mirrored byte-for-byte in `brutor-gateway-core`; a divergence here means
    the proxy stamps runs with a hash the control plane cannot reproduce,
    and the audit sentence stops being true.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"{type(value).__name__} is not contract-serializable")


def hash_closure(closure: Any) -> str:
    """`sha256:<hex>` over the canonical form."""
    digest = hashlib.sha256(canonical_json(closure).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


class AISystemContract(Base):
    """A hash-pinned, versioned snapshot of everything governing one system."""

    __tablename__ = "ai_system_contracts"

    id = Column(String(64), primary_key=True)

    tenant_id = Column(String(255), nullable=False, index=True)
    ai_system_id = Column(
        String(255),
        nullable=False,
        index=True,
        comment="The resource group whose closure this contract renders.",
    )

    version = Column(
        Integer,
        nullable=False,
        comment=(
            "Monotonic per system, starting at 1. Not a semantic version: "
            "it counts config states, and a revert produces a new number "
            "rather than reusing the old one."
        ),
    )

    contract_hash = Column(
        String(80),
        nullable=False,
        comment=(
            "sha256:<hex> over canonical_json(resolved_closure). Stamped on "
            "every run so a historical run can be tied to the exact rules "
            "that governed it. Deliberately not unique: reverting to a "
            "previous configuration legitimately reproduces an earlier hash, "
            "and that is a fact worth being able to see."
        ),
    )

    resolved_closure = Column(
        JSONB,
        nullable=False,
        comment=(
            "The full resolved closure — composition, limits, policies, "
            "guardrails, residency, autonomy. Generated from live config, "
            "never hand-authored."
        ),
    )

    status = Column(
        String(16),
        nullable=False,
        default=CONTRACT_DRAFT,
        server_default=CONTRACT_DRAFT,
    )

    diff_from_version = Column(
        Integer,
        nullable=True,
        comment="The version this one was diffed against when minted.",
    )
    diff_summary = Column(
        JSONB,
        nullable=True,
        comment=(
            "Precomputed change list, with widened permissions called out "
            "separately from narrowed ones. Stored rather than recomputed "
            "so the diff an approver saw is the diff on record — "
            "recomputing it later against today's config would show them "
            "something they never approved."
        ),
    )

    lifecycle_stage_at_promotion = Column(String(32), nullable=True)

    dry_run_summary = Column(
        JSONB,
        nullable=True,
        comment=(
            "What this contract would have changed about recent real "
            "traffic: calls newly denied, calls newly allowed."
        ),
    )

    replay_run_id = Column(
        String(64),
        nullable=True,
        comment=(
            "The replay suite run that gated promotion (no foreign key)."
        ),
    )

    created_by = Column(String(255), nullable=True)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    approved_by = Column(String(255), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    promoted_at = Column(DateTime(timezone=True), nullable=True)
    rejection_reason = Column(Text, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        # CASCADE, unlike ai_system_runs' SET NULL: a contract describes a
        # system's rules and means nothing once the system is gone, whereas
        # a run records something that actually happened.
        ForeignKeyConstraint(
            ["ai_system_id"], ["resource_groups.id"], ondelete="CASCADE"
        ),
        UniqueConstraint(
            "tenant_id", "ai_system_id", "version",
            name="uq_ai_system_contracts_version",
        ),
        Index(
            "ix_ai_system_contracts_system_status",
            "tenant_id", "ai_system_id", "status",
        ),
        # One active contract per system, enforced by the database rather
        # than by the promotion code path. Two "active" contracts would make
        # the question "which rules governed this run" unanswerable, and the
        # promotion path is exactly the kind of read-then-write that races.
        Index(
            "uq_ai_system_contracts_one_active",
            "tenant_id", "ai_system_id",
            unique=True,
            postgresql_where=Column("status") == CONTRACT_ACTIVE,
            sqlite_where=Column("status") == CONTRACT_ACTIVE,
        ),
        {
            "comment": (
                "Hash-pinned, versioned snapshot of every "
                "control applying to an AI System. Append-only."
            )
        },
    )

    @validates("status")
    def _validate_status(self, _key: str, value: str) -> str:
        if value not in CONTRACT_STATUSES:
            raise ValueError(
                f"status must be one of {sorted(CONTRACT_STATUSES)}, got {value!r}"
            )
        return value

    @validates("contract_hash")
    def _validate_hash(self, _key: str, value: str) -> str:
        if not value or not value.startswith("sha256:"):
            raise ValueError(
                f"contract_hash must be 'sha256:<hex>', got {value!r}"
            )
        return value

    @property
    def is_active(self) -> bool:
        return self.status == CONTRACT_ACTIVE

    @property
    def is_approved(self) -> bool:
        return self.approved_by is not None

    @property
    def short_hash(self) -> str:
        """The form an operator reads in a table cell."""
        return self.contract_hash.split(":", 1)[-1][:12]

    def matches(self, closure: Any) -> bool:
        """Whether live config still resolves to this contract."""
        return hash_closure(closure) == self.contract_hash

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "ai_system_id": self.ai_system_id,
            "version": self.version,
            "contract_hash": self.contract_hash,
            "short_hash": self.short_hash,
            "resolved_closure": self.resolved_closure,
            "status": self.status,
            "diff_from_version": self.diff_from_version,
            "diff_summary": self.diff_summary,
            "lifecycle_stage_at_promotion": self.lifecycle_stage_at_promotion,
            "dry_run_summary": self.dry_run_summary,
            "replay_run_id": self.replay_run_id,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at.isoformat() if self.approved_at else None,
            "promoted_at": self.promoted_at.isoformat() if self.promoted_at else None,
            "rejection_reason": self.rejection_reason,
            "is_active": self.is_active,
            "is_approved": self.is_approved,
        }

    def __repr__(self) -> str:
        return (
            f"<AISystemContract(system={self.ai_system_id!r}, v{self.version}, "
            f"{self.status}, {self.short_hash})>"
        )
