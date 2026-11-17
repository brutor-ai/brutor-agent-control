# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The retention sweeper.

Four passes per tenant. Each pass works in batches so no single
transaction is long-running, and repeats within the tick until it runs
out of rows older than the cutoff — a pass that ran exactly once per
tick capped drainage at 5 000 rows per tenant per hour, which is below
the ingest rate of any busy tenant and left the declared window
permanently unmet:

1. ``scrub_bodies`` — rows older than ``body_days`` lose bodies and
   headers and gain ``bodies_pruned_at``. The audit hash chain does not
   cover bodies, so the rows stay verifiable. ``scrub_batch_bodies``
   applies the same window to ``batch_requests``, which holds the same
   class of content on a different table.
2. ``prune_log_rows`` — rows older than ``log_days`` are deleted, and the
   deletion is *declared*: before the DELETE, the doomed rows' chain
   positions and hashes are read and written to ``audit_chain_prunes`` as
   contiguous runs, so the verifier can return ``pruned`` instead of
   ``gap``. Declaration and deletion happen in the same transaction; a
   prune record without its deletion, or the reverse, cannot be observed.
3. ``prune_run_rows`` — run-ledger rows older than ``run_days`` and their
   feedback rows are deleted. Runs are not hash-chained (their evidence is
   the proxy_logs beneath them), so no declaration is needed.

Order matters: bodies first, then rows, so a tenant whose log window is
shorter than its body window (rejected by the policy validator, but the
sweeper should not depend on that) never deletes rows the body pass would
have handled.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, delete, func, null, or_, select, update
from sqlalchemy.orm import Session
from ulid import ULID

from app.api.v1.models.admin.retention import DataRetentionPolicy
from app.database.models.ai_system_run import AISystemRun
from app.database.models.ai_system_run_feedback import AISystemRunFeedback
from app.database.models.audit_chain_prune import (
    REASON_RETENTION,
    AuditChainPrune,
)
from app.database.models.batch_request import BatchRequest
from app.database.models.proxy_log import ProxyLog
from app.database.models.tenant import Tenant

log = logging.getLogger("brutor.retention")

# Rows touched per statement. Large enough to drain a day of traffic in a
# handful of statements, small enough that the row locks and the prune
# record stay modest.
BATCH_SIZE = 5000

# How many batches one pass may run in a single tick.
#
# A pass used to run exactly once per tick, which set a hard ceiling of
# BATCH_SIZE rows per tenant per hour — 120k a day. A tenant that writes
# more `proxy_logs` rows than that never reaches its `log_days` window at
# all: the table grows without bound and the retention policy the DPO was
# shown is not the policy in force. The pass now drains until it comes up
# short (nothing left older than the cutoff) or one of these budgets is
# spent, so the steady state is "converged" rather than "always behind".
MAX_BATCHES_PER_PASS = 40

# Wall-clock ceiling per pass, so one enormous tenant cannot starve the
# rest of the estate of its tick. Well under TICK_INTERVAL_SECONDS even
# with all three passes and every tenant hitting the limit.
PASS_BUDGET_SECONDS = 120.0

SWEEPER_ACTOR = "retention-sweeper"


@dataclass
class SweepResult:
    tenant_id: str
    bodies_scrubbed: int = 0
    batch_bodies_scrubbed: int = 0
    logs_deleted: int = 0
    runs_deleted: int = 0
    feedback_deleted: int = 0
    prune_records: int = 0
    errors: list[str] = field(default_factory=list)
    # Passes that spent their budget with work still to do. A pass that
    # never clears is a declared retention policy that is not enforced,
    # which is a compliance fact and not a tuning detail — so it is
    # reported rather than left to be inferred from a flat row count.
    unconverged: list[str] = field(default_factory=list)

    @property
    def touched(self) -> int:
        return (
            self.bodies_scrubbed
            + self.batch_bodies_scrubbed
            + self.logs_deleted
            + self.runs_deleted
        )


def _cutoff(now: datetime, days: int) -> datetime:
    return now - timedelta(days=days)


# ---------------------------------------------------------------------------
# Pass 1 — bodies
# ---------------------------------------------------------------------------

def scrub_bodies(
    db: Session, tenant_id: str, cutoff: datetime, *, batch: int = BATCH_SIZE
) -> int:
    """Remove bodies/headers from rows older than `cutoff`. Returns rows changed."""
    doomed = (
        select(ProxyLog.id)
        .where(
            ProxyLog.tenant_id == tenant_id,
            ProxyLog.timestamp < cutoff,
            ProxyLog.bodies_pruned_at.is_(None),
            or_(
                ProxyLog.request_body_preview.isnot(None),
                ProxyLog.response_body_preview.isnot(None),
                ProxyLog.request_headers.isnot(None),
                ProxyLog.response_headers.isnot(None),
            ),
        )
        .limit(batch)
    )
    ids = [r[0] for r in db.execute(doomed)]
    if not ids:
        return 0
    res = db.execute(
        update(ProxyLog)
        .where(ProxyLog.id.in_(ids))
        .values(
            request_body_preview=None,
            response_body_preview=None,
            request_headers=None,
            response_headers=None,
            bodies_pruned_at=func.now(),
        )
        .execution_options(synchronize_session=False)
    )
    return int(res.rowcount or 0)


# ---------------------------------------------------------------------------
# Pass 1b — batch bodies
# ---------------------------------------------------------------------------

def scrub_batch_bodies(
    db: Session, tenant_id: str, cutoff: datetime, *, batch: int = BATCH_SIZE
) -> int:
    """Remove batch request/response payloads older than `cutoff`.

    `batch_requests.request_body` / `.response_body` are the same class of
    data as the proxy-log body previews — the subject's full prompt and the
    provider's full completion, one row per line of a batch job — but they
    are written by the proxy's batch poller rather than the audit writer,
    so no `store_bodies` mode and, until now, no retention window reached
    them. The data-handling page (https://docs.brutor.ai/governance/data-handling/) told a DPO that `body_days` governs stored
    content; this is what makes that true for batch traffic.

    There is no `bodies_pruned_at` marker on the table, so the NOT NULL
    check is what makes the pass idempotent.
    """
    doomed = (
        select(BatchRequest.id)
        .where(
            BatchRequest.tenant_id == tenant_id,
            BatchRequest.created_at < cutoff,
            or_(
                BatchRequest.request_body.isnot(None),
                BatchRequest.response_body.isnot(None),
            ),
        )
        .limit(batch)
    )
    ids = [r[0] for r in db.execute(doomed)]
    if not ids:
        return 0
    res = db.execute(
        update(BatchRequest)
        .where(BatchRequest.id.in_(ids))
        # `null()`, not `None`: SQLAlchemy's JSON type persists a Python
        # None as the JSON value `null`, which is NOT SQL NULL. That would
        # leave the row matching `IS NOT NULL` forever, so the pass would
        # rescrub the same rows every tick and never drain.
        .values(request_body=null(), response_body=null())
        .execution_options(synchronize_session=False)
    )
    return int(res.rowcount or 0)


# ---------------------------------------------------------------------------
# Pass 2 — rows, declared
# ---------------------------------------------------------------------------

def _runs_from_seqs(rows: list[tuple[int, str]]) -> list[dict]:
    """Collapse (seq, row_hash) pairs into contiguous runs with tail hashes."""
    runs: list[dict] = []
    for seq, row_hash in sorted(rows, key=lambda r: r[0]):
        if runs and runs[-1]["to"] == seq - 1:
            runs[-1]["to"] = seq
            runs[-1]["tail_hash"] = row_hash
        else:
            runs.append({"from": seq, "to": seq, "tail_hash": row_hash})
    return runs


def declare_prune(
    db: Session,
    *,
    tenant_id: Optional[str],
    doomed: list[tuple[Optional[str], Optional[int], Optional[str]]],
    reason: str,
    actor: str,
) -> int:
    """Write one ``audit_chain_prunes`` row per chain touched. Returns records written.

    `doomed` is (chain_id, chain_seq, row_hash) per row about to be deleted.
    Rows written before row chaining carry no chain position and need no
    declaration — the verifier already reports them as `unchained`.
    """
    by_chain: dict[str, list[tuple[int, str]]] = {}
    for chain_id, seq, row_hash in doomed:
        if chain_id is None or seq is None:
            continue
        by_chain.setdefault(chain_id, []).append((int(seq), row_hash or ""))
    for chain_id, pairs in by_chain.items():
        runs = _runs_from_seqs(pairs)
        db.add(
            AuditChainPrune(
                id=f"prune-{ULID()}",
                chain_id=chain_id,
                tenant_id=tenant_id,
                seq_min=runs[0]["from"],
                seq_max=runs[-1]["to"],
                row_count=len(pairs),
                runs=runs,
                reason=reason,
                actor=actor,
                pruned_at=datetime.now(timezone.utc),
            )
        )
    return len(by_chain)


def prune_log_rows(
    db: Session, tenant_id: str, cutoff: datetime, *, batch: int = BATCH_SIZE
) -> tuple[int, int]:
    """Delete rows older than `cutoff`, declaring them. Returns (rows, prune records)."""
    doomed = db.execute(
        select(ProxyLog.id, ProxyLog.chain_id, ProxyLog.chain_seq, ProxyLog.row_hash)
        .where(ProxyLog.tenant_id == tenant_id, ProxyLog.timestamp < cutoff)
        .order_by(ProxyLog.timestamp)
        .limit(batch)
    ).all()
    if not doomed:
        return 0, 0
    records = declare_prune(
        db,
        tenant_id=tenant_id,
        doomed=[(r[1], r[2], r[3]) for r in doomed],
        reason=REASON_RETENTION,
        actor=SWEEPER_ACTOR,
    )
    res = db.execute(
        delete(ProxyLog)
        .where(ProxyLog.id.in_([r[0] for r in doomed]))
        .execution_options(synchronize_session=False)
    )
    return int(res.rowcount or 0), records


# ---------------------------------------------------------------------------
# Pass 3 — runs
# ---------------------------------------------------------------------------

def prune_run_rows(
    db: Session, tenant_id: str, cutoff: datetime, *, batch: int = BATCH_SIZE
) -> tuple[int, int]:
    """Delete run rows older than `cutoff` and their feedback. Returns (runs, feedback)."""
    doomed = db.execute(
        select(AISystemRun.id, AISystemRun.root_task_id)
        .where(AISystemRun.tenant_id == tenant_id, AISystemRun.started_at < cutoff)
        .order_by(AISystemRun.started_at)
        .limit(batch)
    ).all()
    if not doomed:
        return 0, 0
    roots = [r[1] for r in doomed if r[1]]
    fb = 0
    if roots:
        res = db.execute(
            delete(AISystemRunFeedback)
            .where(
                AISystemRunFeedback.tenant_id == tenant_id,
                AISystemRunFeedback.root_task_id.in_(roots),
            )
            .execution_options(synchronize_session=False)
        )
        fb = int(res.rowcount or 0)
    res = db.execute(
        delete(AISystemRun)
        .where(AISystemRun.id.in_([r[0] for r in doomed]))
        .execution_options(synchronize_session=False)
    )
    return int(res.rowcount or 0), fb


# ---------------------------------------------------------------------------
# Draining a pass
# ---------------------------------------------------------------------------

def _drain(
    step,
    *,
    batch: int,
    commit,
    max_batches: Optional[int] = None,
    budget_seconds: Optional[float] = None,
) -> tuple[list[int], bool]:
    """Run `step()` until it comes up short or a budget is spent.

    `step` returns a tuple of counters whose FIRST element is the number of
    rows the batch actually touched; a value below `batch` means the pass
    has nothing left to do. Each batch is committed on its own, so a budget
    cut-off leaves the work done so far durable and the next tick resumes
    from there.

    Returns (summed counters, converged).
    """
    # Read the module constants at call time, not as default arguments:
    # defaults bind at definition, which makes the budgets untunable from
    # a test or a settings override.
    max_batches = MAX_BATCHES_PER_PASS if max_batches is None else max_batches
    budget = PASS_BUDGET_SECONDS if budget_seconds is None else budget_seconds
    deadline = time.monotonic() + budget
    totals: list[int] = []
    for _ in range(max_batches):
        counters = step()
        if not isinstance(counters, tuple):
            counters = (counters,)
        if not totals:
            totals = [0] * len(counters)
        for i, value in enumerate(counters):
            totals[i] += int(value or 0)
        commit()
        if int(counters[0] or 0) < batch:
            return totals, True
        if time.monotonic() >= deadline:
            return totals, False
    return totals, False


# ---------------------------------------------------------------------------
# One tenant, one tick
# ---------------------------------------------------------------------------

def sweep_tenant(
    db: Session,
    tenant: Tenant,
    policy: DataRetentionPolicy,
    *,
    now: Optional[datetime] = None,
    batch: int = BATCH_SIZE,
) -> SweepResult:
    """Drain every pass for one tenant, committing per batch.

    Each batch touches at most `batch` rows and each pass repeats until it
    comes up short or spends its budget (`MAX_BATCHES_PER_PASS` /
    `PASS_BUDGET_SECONDS`). A pass that stops on a budget is recorded in
    `SweepResult.unconverged`: the tenant is producing rows faster than the
    sweeper removes them, so its declared window is not being honoured.
    """
    now = now or datetime.now(timezone.utc)
    out = SweepResult(tenant_id=tenant.id)

    if policy.body_days is not None:
        try:
            cutoff = _cutoff(now, policy.body_days)
            totals, converged = _drain(
                lambda: scrub_bodies(db, tenant.id, cutoff, batch=batch),
                batch=batch,
                commit=db.commit,
            )
            out.bodies_scrubbed = totals[0] if totals else 0
            if not converged:
                out.unconverged.append("bodies")
        except Exception as exc:  # noqa: BLE001 — one pass must not stop the others
            db.rollback()
            out.errors.append(f"bodies: {exc}")
            log.exception("retention: body scrub failed for tenant %s", tenant.id)

        try:
            cutoff = _cutoff(now, policy.body_days)
            totals, converged = _drain(
                lambda: scrub_batch_bodies(db, tenant.id, cutoff, batch=batch),
                batch=batch,
                commit=db.commit,
            )
            out.batch_bodies_scrubbed = totals[0] if totals else 0
            if not converged:
                out.unconverged.append("batch_bodies")
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            out.errors.append(f"batch_bodies: {exc}")
            log.exception("retention: batch body scrub failed for tenant %s", tenant.id)

    if policy.log_days is not None:
        try:
            cutoff = _cutoff(now, policy.log_days)
            totals, converged = _drain(
                lambda: prune_log_rows(db, tenant.id, cutoff, batch=batch),
                batch=batch,
                commit=db.commit,
            )
            out.logs_deleted, out.prune_records = (totals + [0, 0])[:2]
            if not converged:
                out.unconverged.append("logs")
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            out.errors.append(f"logs: {exc}")
            log.exception("retention: log prune failed for tenant %s", tenant.id)

    if policy.run_days is not None:
        try:
            cutoff = _cutoff(now, policy.run_days)
            totals, converged = _drain(
                lambda: prune_run_rows(db, tenant.id, cutoff, batch=batch),
                batch=batch,
                commit=db.commit,
            )
            out.runs_deleted, out.feedback_deleted = (totals + [0, 0])[:2]
            if not converged:
                out.unconverged.append("runs")
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            out.errors.append(f"runs: {exc}")
            log.exception("retention: run prune failed for tenant %s", tenant.id)

    return out
