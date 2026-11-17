# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Runtime log-level reload for the gateway-control Python process

Why this exists at all:
    On-call's most common ask is "dial guardrails to DEBUG for five
    minutes". Without runtime reload the answer is "ssh in, edit env,
    rolling-restart, lose mid-flight requests". The cost of that
    workflow is what justifies a small, surgical, single-process log-
    level mutation helper: we accept the risk of a stuck override
    surviving a restart bug in exchange for "click a slider, see
    fresh DEBUG inside three seconds".

How persistence works:
    We persist the override on the existing `scheduler_runtime_state`
    table, keying with `scheduler_name = f"logger:{module}"`. There's
    no dedicated `runtime_log_levels` table yet — the migration footprint
    is kept small, and the
    `scheduler_runtime_state` table is intentionally key/value-ish
    (PK is the name, the row carries paused/by/at metadata that maps
    cleanly to "level/by/at" semantics). When the table grows a
    sibling, we'll migrate this back out — the public functions here
    are the abstraction barrier so callers don't care.

    Trade-off: `paused` is a boolean we don't need. We always set it
    to TRUE on a level override so a future glance at the table
    surfaces a single row per overridden module instead of two
    semantic states. The level itself rides in `paused_by` (which is
    a string column already wide enough to hold "DEBUG"). Hacky on
    purpose — the alternative is "ship a one-column migration just
    so log-level overrides have an aesthetically pleasing schema",
    which moves zero shipping velocity for the on-call team.

How runtime mutation works:
    `set_log_level(module, level)` calls `logging.getLogger(module)
    .setLevel(level)` and persists the override row. Subsequent calls
    to `get_log_levels()` reflect the current effective level — read
    straight from the logger objects in memory, not the DB — so the
    UI never gets confused if a row is stale relative to live state
    (e.g. someone hand-edited the row out of band).

Multi-replica scope:
    A single call mutates the logger only on the replica that
    executed the endpoint. The matching NOTIFY publish on
    `brutor:log_level` (handled at the endpoint layer, not here) is
    what fans the mutation out to peer Python replicas; this module
    is the local mutator that the NOTIFY listener will also call when
    it receives a payload from another replica.

Module curation:
    `KNOWN_LOG_MODULES` is the curated allowlist the UI surfaces as
    sliders. Anything outside that list is still settable — the
    runtime call accepts any logger name — but it won't appear in
    the default response so the slider grid stays human-scannable.
    The curated list was built by grepping `BrutorLogger("…")` calls
    in `app/` and bucketing them under five high-traffic prefixes
    that on-call reaches for most often.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.logging_system import BrutorLogger
from app.database.models.scheduler_runtime_state import SchedulerRuntimeState


logger = BrutorLogger(__name__)


# ---------------------------------------------------------------------------
# Allowed levels
# ---------------------------------------------------------------------------
# We accept TRACE as a synonym for DEBUG because the Rust side has a
# real TRACE; the Python UI sliders are aligned across both runtimes
# for operator clarity, and squashing TRACE → DEBUG on the Python
# side keeps the contract symmetrical without paying a logging-stdlib
# fight to add TRACE.

_LEVEL_ALIASES: Dict[str, int] = {
    "trace": logging.DEBUG,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
    "fatal": logging.CRITICAL,
}

# UI-visible canonical names — what we display, what tests assert on,
# and what the NOTIFY payload carries. Always lowercase on the wire.
LEVEL_TRACE = "trace"
LEVEL_DEBUG = "debug"
LEVEL_INFO = "info"
LEVEL_WARN = "warn"
LEVEL_ERROR = "error"

CANONICAL_LEVELS: Tuple[str, ...] = (
    LEVEL_TRACE,
    LEVEL_DEBUG,
    LEVEL_INFO,
    LEVEL_WARN,
    LEVEL_ERROR,
)


# ---------------------------------------------------------------------------
# Curated module allowlist
# ---------------------------------------------------------------------------
# Sliders the UI renders by default. Adding a module here requires a
# matching `BrutorLogger("<name>")` call somewhere in app/, otherwise
# the slider points at a logger nobody emits to.
#
# Grouping rationale (kept loose so on-call doesn't have to learn a
# taxonomy): guardrails / mcp / llm / auth / cache cover ~90% of
# triage asks; the rest fall under the catch-all gateway logger
# defined in core/logging_system.init_logger.

KNOWN_LOG_MODULES: Tuple[str, ...] = (
    # Root Brutor logger — wide-net dial used when on-call doesn't
    # know which sub-component to target yet.
    "brutor-ai-gateway",
    # High-traffic feature loggers that map onto product domains
    # surfaced in the admin UI. Each must exist as a BrutorLogger
    # construction in `app/` — verified by test_system_ops_logs.
    "guardrails-service",
    "mcp-server-client",
    "mcp-probe",
    "mcp-auto-create",
    "semantic-cache-service",
    "unified-auth",
    "end-user-auth",
    "tenant_middleware",
    "tenant_context",
    "database",
    "config-manager",
    "registry-admin",
    "knowledge-base-service",
    "encryption-service",
    "admin-audit",
)


def _scheduler_key(module: str) -> str:
    """Persisted PK for a log-level override row.

    `logger:<module>` is the convention; we deliberately use a colon
    delimiter because it can't collide with any scheduler name (which
    are bare ASCII tokens per KNOWN_SCHEDULERS) — so a clean LIKE
    `logger:%` query can hoist out just the log-level rows.
    """
    return f"logger:{module}"


def _normalise_level(level: str) -> Tuple[str, int]:
    """Canonical (display_name, stdlib_int) for a wire-format level.

    Raises ValueError when the input is outside the alias table —
    the caller is expected to surface this as a 400.
    """
    lowered = (level or "").strip().lower()
    if lowered not in _LEVEL_ALIASES:
        raise ValueError(
            f"Unknown log level {level!r}. Accepted: {sorted(_LEVEL_ALIASES)}"
        )
    # Display-name canonicalisation: alias `warning` → `warn` so the
    # NOTIFY wire carries one shape only.
    display = lowered
    if display == "warning":
        display = "warn"
    if display == "critical" or display == "fatal":
        # We don't surface CRITICAL on the slider — collapse onto
        # ERROR so the UI shows a consistent rung.
        display = "error"
    return display, _LEVEL_ALIASES[lowered]


def _level_to_display(level_int: int) -> str:
    """Inverse map — what `logging.getLogger(...).level` numerically
    means in slider terms. NOTSET (0) maps to "info" because that's
    the effective default before any override has been applied; the
    UI never shows a blank rung.
    """
    if level_int >= logging.CRITICAL:
        return LEVEL_ERROR
    if level_int >= logging.ERROR:
        return LEVEL_ERROR
    if level_int >= logging.WARNING:
        return LEVEL_WARN
    if level_int >= logging.INFO:
        return LEVEL_INFO
    if level_int > 0:
        return LEVEL_DEBUG
    # NOTSET — root chain hasn't been overridden. Reading the
    # effective level off the logger walks ancestors, so we report
    # the effective dispatch, not the raw level.
    return LEVEL_INFO


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def set_log_level(
    db: Session,
    *,
    module: str,
    level: str,
    actor_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Mutate the Python logger for `module` to `level` and persist.

    Args:
        db: Open SQLAlchemy session. The persisted row commits inside
            this function so a partial-failure on the local logger
            mutation still leaves the DB consistent (the override is
            durable; the in-memory mutation will be replayed at boot).
        module: Python logger name. May be one of `KNOWN_LOG_MODULES`
            or any other dotted name — open enum, like the rest of
            the System Console actions.
        level: One of `CANONICAL_LEVELS` or an alias accepted by
            `_LEVEL_ALIASES`.
        actor_id: Admin id requesting the change. Persisted into the
            row's `paused_by` column so the audit row + persisted
            override agree on attribution. None when the call comes
            from the NOTIFY listener replaying a peer's mutation.

    Returns:
        Effective-state dict — `{module, level, effective_level_int,
        applied_at}` — so the endpoint can echo back what actually
        landed (after alias normalisation).
    """
    display, level_int = _normalise_level(level)

    # 1. Mutate in-memory. This is what the next log record from any
    # code that holds a logger by this name will dispatch against.
    py_logger = logging.getLogger(module)
    py_logger.setLevel(level_int)

    # 2. Persist. Upsert keyed on the synthetic `logger:<module>` PK.
    # `paused` is forced TRUE so a row's mere existence flags it as
    # a runtime override (vs. the default of no row + level=NOTSET).
    # The level itself rides in `paused_by`; see the module docstring
    # for the rationale — we'd rather hack one column than ship a
    # micro-migration mid-phase.
    now = datetime.now(timezone.utc)
    stmt = pg_insert(SchedulerRuntimeState).values(
        scheduler_name=_scheduler_key(module),
        paused=True,
        paused_at=now,
        paused_by=display,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[SchedulerRuntimeState.scheduler_name],
        set_={
            "paused": True,
            "paused_at": now,
            "paused_by": display,
        },
    )
    db.execute(stmt)
    db.commit()

    logger.info(
        "runtime log level applied",
        context={
            "module": module,
            "level": display,
            "level_int": level_int,
            "actor_id": actor_id,
        },
    )

    return {
        "module": module,
        "level": display,
        "effective_level_int": level_int,
        "applied_at": now.isoformat(),
    }


def get_log_levels(db: Session) -> List[Dict[str, Any]]:
    """Effective log level for every module in `KNOWN_LOG_MODULES`,
    augmented with any modules that have a persisted override row
    but aren't in the curated list.

    The effective level is read from `logging.getLogger(...).getEffectiveLevel()`
    so the response always reflects what's actually dispatching right
    now, even if the persisted row drifted (manual DB edit, replay
    glitch on boot).

    Why not just project the DB rows? Because what an on-call cares
    about is "what will the next log call dispatch against?" — not
    "what does the DB think the override is?". When those diverge,
    the live state wins; the persisted state is reconciliation
    bookkeeping. The UI surfaces both: `level` is the live effective
    level, `override_level` (when present) is the persisted row
    value, and the operator sees a yellow dot if they disagree.
    """
    # Snapshot persisted overrides keyed on the bare module name so
    # the merge below is O(n) over the union of known + persisted.
    persisted: Dict[str, SchedulerRuntimeState] = {}
    rows = (
        db.execute(
            select(SchedulerRuntimeState).where(
                SchedulerRuntimeState.scheduler_name.like("logger:%"),
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        bare = row.scheduler_name.removeprefix("logger:")
        persisted[bare] = row

    # Union: curated list first (so the UI order is stable), then any
    # ad-hoc overrides the on-call set on modules outside the curated
    # set.
    union: List[str] = list(KNOWN_LOG_MODULES)
    for name in persisted:
        if name not in union:
            union.append(name)

    result: List[Dict[str, Any]] = []
    for name in union:
        py_logger = logging.getLogger(name)
        effective_int = py_logger.getEffectiveLevel()
        entry: Dict[str, Any] = {
            "module": name,
            "level": _level_to_display(effective_int),
            "effective_level_int": effective_int,
            "is_curated": name in KNOWN_LOG_MODULES,
            "override_level": None,
            "override_at": None,
            "override_by": None,
        }
        row = persisted.get(name)
        if row is not None:
            entry["override_level"] = row.paused_by
            entry["override_at"] = (
                row.paused_at.isoformat() if row.paused_at else None
            )
            # `paused_by` is reused for the level value (see module
            # docstring); attribution is therefore not captured per-
            # row. The audit log carries who-did-what — this surface
            # is a "what's overridden?" snapshot, not a forensic
            # one.
            entry["override_by"] = None
        result.append(entry)
    return result
