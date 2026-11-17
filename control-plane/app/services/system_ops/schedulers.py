# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Background Tasks service — list + control schedulers

The control plane runs N singleton background loops (KB connector
sync, audit export, …). Each loop reads its
own `scheduler_runtime_state` row at tick start and short-circuits
when `paused = TRUE`. The Background Tasks page is the operator
surface for those flags + an out-of-band Trigger-Now button.

Two operational invariants this module enforces:

  1. **Pause/resume = DB write THEN notify.** The runtime-state row
     is the durable source of truth. NOTIFY is a wake-up signal so
     a scheduler that's mid-sleep can react faster than its tick
     interval — but if NOTIFY delivery fails, the next natural tick
     still picks up the new flag from the row. Reverse the order
     and a NOTIFY consumer that re-reads the row would see the OLD
     value, then the row would update silently — making the click
     appear to no-op for one tick.

  2. **`list_schedulers` is read-only.** Operators land on the page,
     poll every few seconds, and shouldn't trigger any state mutation
     just by looking. Schedulers that have never been paused have NO
     row in `scheduler_runtime_state`; we synthesise a default
     `{paused: false, state: 'unknown'}` entry so the table covers
     every code-side known scheduler without an UPSERT side effect.

The last-activity-at column is derived from
`system_operation_log` rows where `scope_kind = 'scheduler'` and
`scope_id = <scheduler_name>`. This is what we have today for "did
this scheduler do anything recently?" — schedulers don't write to
`usage_logs` directly (those are per-request usage rows), but every
manual Trigger/Pause/Resume already writes a system op row, and the
ground-truth scheduler tick logs will land in this same table when
Without the "scheduler reported in" beacon, the
column shows the last manual interaction, which is the signal an
operator most cares about ("did my last Trigger Now actually fire?").
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.logging_system import BrutorLogger
from app.database.models.scheduler_runtime_state import (
    KNOWN_SCHEDULERS,
    SchedulerRuntimeState,
)
from app.database.models.system_operation_log import (
    ACTION_SCHEDULER_PAUSE,
    ACTION_SCHEDULER_RESUME,
    ACTION_SCHEDULER_TRIGGER,
    SCOPE_SCHEDULER,
    SystemOperationLog,
)
from app.services.system_ops.audit_row import write_system_operation
from app.services.system_ops.notify import (
    CHANNEL_SCHEDULER_PAUSE,
    CHANNEL_SCHEDULER_RESUME,
    CHANNEL_SCHEDULER_TRIGGER,
    publish_system_notify,
)

logger = BrutorLogger(__name__)


# Set of trigger/pause/resume actions we look for when computing
# `last_run_at`. Kept as a tuple so the IN-clause SQL the query
# emits stays a constant-length predicate (one index hit per value).
_SCHEDULER_ACTIONS = (
    ACTION_SCHEDULER_TRIGGER,
    ACTION_SCHEDULER_PAUSE,
    ACTION_SCHEDULER_RESUME,
)


# Staleness threshold for the per-tick beacon. A
# beacon row written within this window proves the scheduler is alive;
# anything older flips the badge to 'unknown'.
#
# 5 minutes is the right knob for two reasons:
#   * The shortest tick interval in the fleet today is 10s
#     (batch_submitter). 5 minutes is 30× that — generous enough to
#     ride out a slow DB query without flapping the badge.
#   * The longest interval in the fleet is 60s
#     (audit_export_scheduler, llm_retirement_warner). 5 minutes is
#     5× that, leaving 4 missed ticks before we cry "unknown" — which
#     is the right signal: a scheduler missing four consecutive ticks
#     IS unhealthy and the operator should know.
BEACON_STALE_THRESHOLD_SECS = 300


class UnknownSchedulerError(ValueError):
    """An action named a scheduler that is not in `KNOWN_SCHEDULERS`. A
    notification for a name no scheduler has would be delivered and do
    nothing, so the name is refused before any row is written or any
    message sent. The message is the 404's: it names the schedulers there
    are."""


def _ensure_known(scheduler_name: str) -> None:
    if scheduler_name not in KNOWN_SCHEDULERS:
        raise UnknownSchedulerError(
            f"There is no scheduler named {scheduler_name!r}. The schedulers are: "
            f"{', '.join(sorted(KNOWN_SCHEDULERS))}."
        )


# ---------------------------------------------------------------------------
# Read path
# ---------------------------------------------------------------------------


def list_schedulers(db: Session, tenant_id: Optional[str]) -> List[Dict[str, Any]]:
    """Build the Background Tasks table.

    One entry per `KNOWN_SCHEDULERS` constant. Rows that have never
    been paused don't exist in `scheduler_runtime_state` — we still
    emit them as defaults so the UI shows the full inventory.

    `last_run_at` is derived from `system_operation_log`: the most
    recent trigger/pause/resume action whose scope matches. Returns
    one MAX(created_at) per scheduler in a single GROUP BY query so
    we don't N+1 the audit table.

    `tenant_id` is currently unused at the query level — schedulers
    are SINGLETONS at the platform level, not per-tenant — but kept
    as a parameter so a future per-tenant scheduler split (e.g. a
    per-tenant KB connector poller) doesn't break the public API.

    Returns the entries sorted by scheduler_name for stable table
    rendering across polls.
    """

    # Single batched read of every runtime row. `KNOWN_SCHEDULERS` is
    # small enough (10s of entries) that `IN (...)` is cheap; we
    # filter rather than `SELECT *` so a stale row for a removed
    # scheduler doesn't leak into the table.
    runtime_rows = (
        db.query(SchedulerRuntimeState)
        .filter(SchedulerRuntimeState.scheduler_name.in_(KNOWN_SCHEDULERS))
        .all()
    )
    runtime_by_name: Dict[str, SchedulerRuntimeState] = {
        r.scheduler_name: r for r in runtime_rows
    }

    # Last-activity-at per scheduler — one GROUP BY query against the
    # audit log. The index `ix_system_operation_log_action_created`
    # makes this scan-friendly even at high audit volume.
    last_activity_rows = (
        db.query(
            SystemOperationLog.scope_id.label("scheduler_name"),
            func.max(SystemOperationLog.created_at).label("last_at"),
        )
        .filter(
            SystemOperationLog.scope_kind == SCOPE_SCHEDULER,
            SystemOperationLog.action.in_(_SCHEDULER_ACTIONS),
            SystemOperationLog.scope_id.in_(KNOWN_SCHEDULERS),
        )
        .group_by(SystemOperationLog.scope_id)
        .all()
    )
    def _utc(moment: Optional[datetime]) -> Optional[datetime]:
        # A database that keeps no time zone answers a naive UTC time.
        if moment is not None and moment.tzinfo is None:
            return moment.replace(tzinfo=timezone.utc)
        return moment

    last_activity_by_name: Dict[str, datetime] = {
        r.scheduler_name: _utc(r.last_at) for r in last_activity_rows if r.scheduler_name
    }

    # Beacon staleness threshold lookup. Computed once per call rather
    # than per row so a slow lookup wouldn't matter even if it could.
    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(seconds=BEACON_STALE_THRESHOLD_SECS)

    def _derive_state(
        paused: bool,
        beacon: Optional[datetime],
        audit_last: Optional[datetime],
    ) -> str:
        # The single state-derivation truth table for the page. Order
        # matters: paused wins over running, beacon proves running,
        # audit row alone is insufficient (a Trigger from 10 minutes
        # ago doesn't prove the loop is alive NOW).
        if paused:
            return "paused"
        if beacon is not None and beacon >= stale_cutoff:
            return "running"
        # Pre-beacon-rollout compatibility: if a scheduler hasn't
        # written a beacon yet but we have a recent audit row, treat
        # it as 'running' so an upgrade window where some schedulers
        # have the new write_beacon call and some don't doesn't flash
        # every row to 'unknown' at once.
        if audit_last is not None and audit_last >= stale_cutoff:
            return "running"
        return "unknown"

    def _pick_last_run(
        beacon: Optional[datetime], audit_last: Optional[datetime]
    ) -> Optional[datetime]:
        # The page's `last_run_at` column should show the most-recent
        # signal of any kind — a fresh beacon AND a fresh audit row
        # both qualify. Returning the later of the two keeps the
        # column meaningful when only one source is wired.
        candidates = [t for t in (beacon, audit_last) if t is not None]
        return max(candidates) if candidates else None

    out: List[Dict[str, Any]] = []
    for name in sorted(KNOWN_SCHEDULERS):
        rt = runtime_by_name.get(name)
        last_at = last_activity_by_name.get(name)

        if rt is None:
            # No row yet — scheduler has never been paused, resumed,
            # or written a beacon. The first beacon UPSERTs the row,
            # so 'no row' is the genuine 'never seen' state.
            out.append(
                {
                    "scheduler_name": name,
                    "paused": False,
                    "paused_at": None,
                    "paused_by": None,
                    "pause_reason": None,
                    "resumed_at": None,
                    "resumed_by": None,
                    "last_run_at": last_at.isoformat() if last_at else None,
                    "last_run_result": None,
                    "next_run_eta": None,
                    "state": _derive_state(False, None, last_at),
                }
            )
            continue

        beacon_at = _utc(rt.last_beacon_at)
        last_run = _pick_last_run(beacon_at, last_at)
        out.append(
            {
                "scheduler_name": name,
                "paused": bool(rt.paused),
                "paused_at": rt.paused_at.isoformat() if rt.paused_at else None,
                "paused_by": rt.paused_by,
                "pause_reason": rt.pause_reason if rt.paused else None,
                "resumed_at": rt.resumed_at.isoformat() if rt.resumed_at else None,
                "resumed_by": rt.resumed_by,
                "last_run_at": last_run.isoformat() if last_run else None,
                "last_run_result": None,
                "next_run_eta": None,
                "state": _derive_state(bool(rt.paused), beacon_at, last_at),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Per-tick beacon
# ---------------------------------------------------------------------------


def write_beacon(db: Session, scheduler_name: str) -> None:
    """UPSERT this scheduler's `last_beacon_at` to NOW().

    Called from inside the scheduler's tick loop, ideally as the very
    first statement after the tick wakes. The write is a single
    `INSERT ... ON CONFLICT (scheduler_name) DO UPDATE` — Postgres
    handles the race between a fresh scheduler that has no row and a
    concurrent operator Pause click that creates one for the same
    name.

    Failure handling is the caller's choice — but the recommended
    posture is "best-effort": a scheduler that can't reach the DB
    has bigger problems than a stale badge, and surfacing the error
    inside the tick loop is the right place to log it without
    crashing the loop.

    `scheduler_name` is NOT validated against KNOWN_SCHEDULERS here:
    callers ARE the schedulers, and forcing them to register their
    own name to write their own beacon would be ceremony for no
    safety win. The `list_schedulers` query filters by
    KNOWN_SCHEDULERS so a typo would silently never show up in the
    UI — a self-defeating bug for the scheduler author.

    This is a control-plane helper. The Rust schedulers in
    gateway-core / kb-uploader / kb-connector-sync mirror this with
    a similar one-statement helper in their own crates — see
    `services::system_ops::beacon` on the Rust side.
    """
    now = datetime.now(timezone.utc)
    stmt = (
        pg_insert(SchedulerRuntimeState)
        .values(
            scheduler_name=scheduler_name,
            paused=False,
            last_beacon_at=now,
        )
        .on_conflict_do_update(
            index_elements=["scheduler_name"],
            set_={"last_beacon_at": now},
        )
    )
    db.execute(stmt)
    db.commit()


# ---------------------------------------------------------------------------
# Write paths — every action writes the audit row AND publishes NOTIFY
# ---------------------------------------------------------------------------


def trigger_scheduler(
    db: Session,
    *,
    scheduler_name: str,
    actor,
    tenant_id: Optional[str],
    reason: Optional[str],
    request,
) -> Dict[str, Any]:
    """Fire one Trigger-Now request.

    No DB mutation on `scheduler_runtime_state` — a trigger doesn't
    change the paused/resumed flag. It's a one-shot signal to whichever
    control-plane replica holds the scheduler singleton: "skip your
    next sleep, run a tick right now". The receiving replica picks the
    NOTIFY up on its `PgListener` and dispatches to the registered
    scheduler handle.

    The audit row is written FIRST so a NOTIFY delivery failure still
    leaves the forensic trail. The audit-row helper commits its own
    transaction (see `write_system_operation` docstring), and the
    NOTIFY publisher operates on the same session — a subsequent
    publisher commit is unnecessary because `pg_notify` is sent on
    statement execution, not at commit time. (Postgres NOTIFY-from-
    function semantics: messages are queued and delivered at commit
    of the surrounding transaction; the audit-row commit closes that
    transaction, so the NOTIFY is delivered with the same write.)
    """
    _ensure_known(scheduler_name)

    write_system_operation(
        db=db,
        actor=actor,
        action=ACTION_SCHEDULER_TRIGGER,
        tenant_id=tenant_id,
        scope_kind=SCOPE_SCHEDULER,
        scope_id=scheduler_name,
        payload={"reason": reason} if reason else None,
        request=request,
    )

    actor_id = getattr(actor, "id", "unknown")
    wire = publish_system_notify(
        db=db,
        channel=CHANNEL_SCHEDULER_TRIGGER,
        payload={
            "scheduler_name": scheduler_name,
            "requested_by": actor_id,
        },
    )
    # Flush the NOTIFY immediately so callers without an explicit
    # subsequent commit (e.g. test paths) still see the wire message
    # delivered. In live request handling FastAPI's dependency teardown
    # will commit this session at request end, but `pg_notify` queues
    # only until the enclosing transaction commits — we don't want a
    # readback test to silently miss the publish.
    db.commit()

    logger.info(
        "scheduler triggered",
        context={
            "scheduler_name": scheduler_name,
            "actor_id": actor_id,
            "tenant_id": tenant_id,
        },
    )
    return {"channel": CHANNEL_SCHEDULER_TRIGGER, "payload": wire}


def pause_scheduler(
    db: Session,
    *,
    scheduler_name: str,
    actor,
    tenant_id: Optional[str],
    reason: Optional[str],
    request,
) -> Dict[str, Any]:
    """Mark the scheduler paused, then notify.

    Idempotent: pausing an already-paused scheduler updates the
    `paused_at` + `paused_by` to the latest actor (the operator's
    intent is "this is paused as of now, by me") but still re-fires
    NOTIFY so a previously-missed wake-up signal isn't permanently
    lost.
    """
    return _set_pause_state(
        db=db,
        scheduler_name=scheduler_name,
        actor=actor,
        tenant_id=tenant_id,
        reason=reason,
        request=request,
        paused=True,
    )


def resume_scheduler(
    db: Session,
    *,
    scheduler_name: str,
    actor,
    tenant_id: Optional[str],
    reason: Optional[str],
    request,
) -> Dict[str, Any]:
    """Mark the scheduler resumed, then notify.

    Symmetric to `pause_scheduler` — same idempotency rule, same
    audit-then-notify ordering.
    """
    return _set_pause_state(
        db=db,
        scheduler_name=scheduler_name,
        actor=actor,
        tenant_id=tenant_id,
        reason=reason,
        request=request,
        paused=False,
    )


def _set_pause_state(
    db: Session,
    *,
    scheduler_name: str,
    actor,
    tenant_id: Optional[str],
    reason: Optional[str],
    request,
    paused: bool,
) -> Dict[str, Any]:
    """Shared pause/resume implementation.

    Strict ordering:

      1. Upsert the `scheduler_runtime_state` row with the new flag.
      2. Write the audit row (also commits, sealing step 1's mutation
         into a single transaction-level NOTIFY queue).
      3. Publish NOTIFY on the matching channel.
      4. Commit the NOTIFY's transaction.

    Why upsert before audit: a parallel `list_schedulers` reader must
    see the new state coherently. If the audit row were written first
    and committed, there would be a tiny window where the audit log
    says "paused" but the runtime row still says "running" — an
    on-call dashboard would render conflicting state for one round
    trip.
    """
    _ensure_known(scheduler_name)
    now = datetime.now(timezone.utc)
    actor_id = getattr(actor, "id", None)

    # Step 1: upsert. SQLAlchemy 2.x doesn't expose `ON CONFLICT` in
    # the abstract `Query` API, so we do the read-modify-write within
    # the same session. The audit row's commit (step 2) flushes both
    # mutations atomically, so a concurrent reader sees either pre-
    # or post-state, never a half-applied update.
    row = (
        db.query(SchedulerRuntimeState)
        .filter(SchedulerRuntimeState.scheduler_name == scheduler_name)
        .one_or_none()
    )
    if row is None:
        row = SchedulerRuntimeState(
            scheduler_name=scheduler_name,
            paused=paused,
        )
        db.add(row)
    else:
        row.paused = paused

    if paused:
        row.paused_at = now
        row.paused_by = actor_id
        # This pause's reason: one given no reason has none.
        row.pause_reason = reason or None
    else:
        row.resumed_at = now
        row.resumed_by = actor_id
        row.pause_reason = None

    # Flush so the audit row's commit captures the runtime-row write
    # in the same transaction. (`write_system_operation` commits.)
    db.flush()

    action = ACTION_SCHEDULER_PAUSE if paused else ACTION_SCHEDULER_RESUME
    write_system_operation(
        db=db,
        actor=actor,
        action=action,
        tenant_id=tenant_id,
        scope_kind=SCOPE_SCHEDULER,
        scope_id=scheduler_name,
        payload={"reason": reason} if reason else None,
        before_state=None,
        after_state={
            "paused": paused,
            "paused_at": row.paused_at.isoformat() if row.paused_at else None,
            "paused_by": row.paused_by,
            "pause_reason": row.pause_reason,
            "resumed_at": row.resumed_at.isoformat() if row.resumed_at else None,
            "resumed_by": row.resumed_by,
        },
        request=request,
    )

    channel = CHANNEL_SCHEDULER_PAUSE if paused else CHANNEL_SCHEDULER_RESUME
    wire = publish_system_notify(
        db=db,
        channel=channel,
        payload={"scheduler_name": scheduler_name},
    )
    # See trigger_scheduler — explicit commit so the NOTIFY is queued
    # for delivery immediately rather than waiting for FastAPI's
    # session teardown.
    db.commit()

    logger.info(
        "scheduler state changed",
        context={
            "scheduler_name": scheduler_name,
            "paused": paused,
            "actor_id": actor_id,
            "tenant_id": tenant_id,
        },
    )
    return {"channel": channel, "payload": wire}
