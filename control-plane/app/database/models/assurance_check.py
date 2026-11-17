# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Continuous Assurance Checks.

The trust boundary: natural language is authoring UX ONLY. The runtime
artifact is the *spec* — versioned, hash-pinned, operator-approved.
Changing the NL description does nothing; publishing a new spec version
does. Prior versions are immutable snapshots (``assurance_check_versions``)
so a result row's ``check_version`` always resolves to exactly the spec
that produced it.

Two evaluator tiers, declared and never blended:

* ``deterministic`` — a bounded-CEL expression over run-ledger *facts*,
  evaluated in the Rust core as a follower of run finalization (and by
  the core's backtest endpoint). The control plane never evaluates CEL —
  one engine, no divergence.
* ``semantic`` — an LLM judge over run *content*, executed by the CP
  batch worker THROUGH the gateway proxy so the judge call is itself
  governed, ledgered, and billed. Sampling is declared in the spec and
  disclosed; every result carries an evaluator fingerprint
  (spec_hash + model + prompt_hash) so re-judging is reproducible.

``assurance_check_results`` rows are written by the Rust core
(on-finalize + backtest) and by the CP judge worker. Threshold breaches
land in the Assurance Inbox as ``check_breach`` items — the inbox is the
sink, checks are a source.
"""

import hashlib
import json

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.database import Base

#: Predicate of the `uq_ckr_check_version_run` partial unique index — the
#: arbiter the Rust core's result upsert infers against. WIRE CONTRACT: this
#: string must stay byte-identical here, in migration `a164_assurance_checks`, and in
#: `brutor-gateway-core/src/services/assurance_checks.rs`. See the comment on
#: the Index below for what breaks when it drifts.
CKR_UNIQUE_WHERE = "run_id IS NOT NULL AND NOT backtest"

CHECK_TIER_DETERMINISTIC = "deterministic"
CHECK_TIER_SEMANTIC = "semantic"
CHECK_TIERS = (CHECK_TIER_DETERMINISTIC, CHECK_TIER_SEMANTIC)

CHECK_OUTPUT_BOOLEAN = "boolean"
CHECK_OUTPUT_CLASSIFICATION = "classification"
CHECK_OUTPUT_SCORE = "score"
CHECK_OUTPUT_COUNT = "count"
CHECK_OUTPUT_EXTRACTION = "extraction"
CHECK_OUTPUTS = (
    CHECK_OUTPUT_BOOLEAN,
    CHECK_OUTPUT_CLASSIFICATION,
    CHECK_OUTPUT_SCORE,
    CHECK_OUTPUT_COUNT,
    CHECK_OUTPUT_EXTRACTION,
)

CHECK_TRIGGER_ON_FINALIZE = "on_finalize"
CHECK_TRIGGER_BATCH = "batch"
CHECK_TRIGGERS = (CHECK_TRIGGER_ON_FINALIZE, CHECK_TRIGGER_BATCH)

THRESHOLD_KIND_VALUE = "value"
THRESHOLD_KIND_RATE_OVER = "rate_over"
THRESHOLD_KINDS = (THRESHOLD_KIND_VALUE, THRESHOLD_KIND_RATE_OVER)
THRESHOLD_OPS = ("eq", "ne", "gt", "gte", "lt", "lte")

# Fields that participate in the spec hash — the runtime contract. Name and
# NL description are context, not contract, and deliberately excluded.
SPEC_FIELDS = ("scope", "tier", "evaluator", "output", "trigger", "threshold")


def compute_spec_hash(spec: dict) -> str:
    """Canonical hash of the runtime-relevant spec fields.

    Sorted-key JSON so semantically identical specs hash identically
    regardless of dict ordering. The Rust core recomputes nothing — it
    trusts this hash as the version's identity.
    """
    canonical = {field: spec.get(field) for field in SPEC_FIELDS}
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AssuranceCheck(Base):
    """The ACTIVE spec. History lives in ``assurance_check_versions``."""

    __tablename__ = "assurance_checks"

    id = Column(String(64), primary_key=True, comment="ULID, prefixed 'chk-'")
    tenant_id = Column(String(255), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    # The operator's original question — kept for context, never executed.
    description_nl = Column(Text, nullable=True)

    version = Column(Integer, nullable=False, default=1, server_default="1")
    spec_hash = Column(String(64), nullable=False)

    # {ai_system_ids: [..]} — empty/absent = whole estate.
    scope = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'"))
    tier = Column(String(16), nullable=False)
    # deterministic: {"expression": "<bounded CEL over run facts>"}
    # semantic:      {"rubric": str, "model_ref": str|null,
    #                 "sample_rate": 0..1, "max_tokens": int,
    #                 "daily_token_budget": int|null,
    #                 "labels": [..] (classification only)}
    evaluator = Column(JSONB, nullable=False)
    output = Column(String(24), nullable=False)
    trigger = Column(String(16), nullable=False)
    # {"kind": "value", "op": "eq", "value": true, "severity": "critical"}
    # {"kind": "rate_over", "window_hours": 24, "op": "gt", "value": 0.05,
    #  "severity": "critical"}
    threshold = Column(JSONB, nullable=True)
    route_to_inbox = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    enabled = Column(Boolean, nullable=False, default=False, server_default=text("false"))

    created_by = Column(String(255), nullable=True)
    # Set when an admin enables the check — enabling IS the approval act.
    approved_by = Column(String(255), nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "tier IN ('deterministic','semantic')", name="ck_achk_tier"
        ),
        CheckConstraint(
            "output IN ('boolean','classification','score','count',"
            "'extraction')",
            name="ck_achk_output",
        ),
        CheckConstraint(
            "trigger IN ('on_finalize','batch')", name="ck_achk_trigger"
        ),
        Index("uq_achk_tenant_name", "tenant_id", "name", unique=True),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "description_nl": self.description_nl,
            "version": self.version,
            "spec_hash": self.spec_hash,
            "scope": self.scope,
            "tier": self.tier,
            "evaluator": self.evaluator,
            "output": self.output,
            "trigger": self.trigger,
            "threshold": self.threshold,
            "route_to_inbox": self.route_to_inbox,
            "enabled": self.enabled,
            "created_by": self.created_by,
            "approved_by": self.approved_by,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


class AssuranceCheckVersion(Base):
    """Immutable snapshot of a published spec version.

    Written once at publish time, never updated (except
    ``backtest_summary``, which is evidence *about* the version, not part
    of it). What history judges.
    """

    __tablename__ = "assurance_check_versions"

    id = Column(String(64), primary_key=True, comment="ULID, prefixed 'ckv-'")
    tenant_id = Column(String(255), nullable=False, index=True)
    check_id = Column(
        String(64),
        ForeignKey("assurance_checks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version = Column(Integer, nullable=False)
    spec_hash = Column(String(64), nullable=False)
    # Full spec snapshot incl. name/description at publish time.
    spec = Column(JSONB, nullable=False)
    # {evaluated, breaches, window_days, samples: [...], ran_at} — the
    # evidence a reviewer checks the compiled check against, kept auditable rather than transient.
    backtest_summary = Column(JSONB, nullable=True)
    created_by = Column(String(255), nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("uq_ckv_check_version", "check_id", "version", unique=True),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "check_id": self.check_id,
            "version": self.version,
            "spec_hash": self.spec_hash,
            "spec": self.spec,
            "backtest_summary": self.backtest_summary,
            "created_by": self.created_by,
            "created_at": _iso(self.created_at),
        }


class AssuranceCheckResult(Base):
    """One evaluation outcome. Written by the Rust core (deterministic
    on-finalize + backtest samples) and the CP judge worker (semantic)."""

    __tablename__ = "assurance_check_results"

    id = Column(String(64), primary_key=True, comment="ULID, prefixed 'ckr-'")
    tenant_id = Column(String(255), nullable=False, index=True)
    check_id = Column(
        String(64),
        ForeignKey("assurance_checks.id", ondelete="CASCADE"),
        nullable=False,
    )
    check_version = Column(Integer, nullable=False)
    run_id = Column(String(64), nullable=True)
    window_start = Column(DateTime(timezone=True), nullable=True)
    window_end = Column(DateTime(timezone=True), nullable=True)

    # Typed per the spec's output: {"value": <bool|str|float|int|...>}.
    value = Column(JSONB, nullable=False)
    # Pointers into the ledger — never copied content.
    evidence = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'"))
    # deterministic: spec_hash. semantic: sha256(spec_hash+model+prompt_hash).
    evaluator_fingerprint = Column(String(64), nullable=False)
    backtest = Column(Boolean, nullable=False, default=False, server_default="false")
    # Judge tokens actually spent (semantic only) — the budget ledger.
    tokens_used = Column(Integer, nullable=True)
    evaluated_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # The on-finalize follower and the judge worker are both idempotent
        # per (check version, run): a reopened run re-evaluates in place.
        Index(
            "uq_ckr_check_version_run",
            "check_id",
            "check_version",
            "run_id",
            unique=True,
            # BOTH dialects or the SQLite test harness silently degrades the
            # partial unique to a full one.
            #
            # The predicate is spelled as literal SQL, character for
            # character identical to migration `a164_assurance_checks` and to the core's
            # `ON CONFLICT ... WHERE` in services/assurance_checks.rs.
            # That is not stylistic. Postgres matches an arbiter index by
            # PROVING the statement's predicate implies the index's, and its
            # prover does not equate `NOT backtest` with `backtest IS FALSE`.
            # This model used to render the latter, so on a fresh install
            # (create_all, not migrations) every on-finalize result insert
            # failed with "no unique or exclusion constraint matching the ON
            # CONFLICT specification" — and because check evaluation is
            # fail-open, the checks stayed green while recording nothing.
            # `test_check_result_index_predicate_matches_migration` pins it.
            postgresql_where=text(CKR_UNIQUE_WHERE),
            sqlite_where=text(CKR_UNIQUE_WHERE),
        ),
        Index("ix_ckr_tenant_check_at", "tenant_id", "check_id", "evaluated_at"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "check_id": self.check_id,
            "check_version": self.check_version,
            "run_id": self.run_id,
            "window_start": _iso(self.window_start),
            "window_end": _iso(self.window_end),
            "value": self.value,
            "evidence": self.evidence,
            "evaluator_fingerprint": self.evaluator_fingerprint,
            "backtest": self.backtest,
            "tokens_used": self.tokens_used,
            "evaluated_at": _iso(self.evaluated_at),
        }


class AssuranceDigest(Base):
    """One generated weekly digest.

    Strictly a projection of the assurance report services — stored so the
    console shows exactly what was pushed to channels, and so generation is
    idempotent per (tenant, week).
    """

    __tablename__ = "assurance_digests"

    id = Column(String(64), primary_key=True, comment="ULID, prefixed 'dig-'")
    tenant_id = Column(String(255), nullable=False, index=True)
    week_start = Column(Date, nullable=False)
    content = Column(JSONB, nullable=False)
    generated_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("uq_dig_tenant_week", "tenant_id", "week_start", unique=True),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "week_start": self.week_start.isoformat() if self.week_start else None,
            "content": self.content,
            "generated_at": _iso(self.generated_at),
        }


def _iso(dt) -> str | None:
    return dt.isoformat() if dt is not None else None
