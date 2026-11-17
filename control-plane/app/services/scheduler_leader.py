# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Per-tick leader election for the control plane's background loops.

# The problem this closes

Every scheduler in `app/services/**` is started from the FastAPI
lifespan with `asyncio.create_task`. That is once per *process*, and
the documented production launcher is Gunicorn with
`workers = int(os.getenv('GUNICORN_WORKERS', '2'))` — each worker runs
its own lifespan. So a single default container already ticks every
scheduler twice, and a second container doubles it again. The
observable damage is not theoretical: the usage importer claims the
same provider window twice, the weekly assurance digest is delivered
twice to every subscriber (its dedupe is check-then-act, with no
unique constraint behind it), and the retention sweeper's per-tenant
batches interleave.

The module docstrings used to say "run on the lead instance only" and
leave leader election to the operator. This is that election.

# Why a per-tick advisory lock, and not a held lease

`init_database()` already coordinates concurrent Gunicorn workers with
`pg_advisory_lock(8742)`; reusing the same primitive means no new
moving part, no new table, and no clock assumptions. The choice here
is `pg_try_advisory_lock` **around one tick** rather than a lock held
for the process lifetime:

  * A held lease needs a keepalive and a takeover timeout, and it has
    a stuck-leader failure mode — the holder wedges, the lock stays
    taken, and the scheduler silently stops for everyone.
  * A per-tick lock has no leader at all between ticks. Whichever
    worker reaches the next tick first does the work; if it dies
    mid-tick its connection drops and Postgres releases the lock
    immediately. The worst case is one skipped tick, which every one
    of these loops already tolerates by design.

The cost is one `SELECT pg_try_advisory_lock(...)` round trip per
scheduler per tick — at tick intervals of 60s and up, nothing.

# Lock key space

We use the **two-argument** form `pg_try_advisory_lock(classid, objid)`,
which Postgres keeps in a different space from the one-argument form
that `init_database()` uses. So there is no way for a scheduler lease
to collide with the migration lock, whatever the hash produces.
`classid` is a fixed Brutor namespace; `objid` is a stable CRC32 of the
scheduler name folded into signed int32 (Postgres' advisory-lock args
are `integer`, so an unsigned CRC32 overflows and must be folded).

# Non-Postgres engines

Unit tests run against in-memory SQLite, which has no advisory locks
and — being one process, one connection — needs none. On any
non-Postgres dialect the lease grants unconditionally rather than
failing the tick closed: refusing to run would turn every scheduler
test into a no-op that still passes, which is the worse failure.
"""

import logging
import zlib
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional

from sqlalchemy import text

log = logging.getLogger("brutor.scheduler-leader")


# Namespace for every scheduler lease. Arbitrary but fixed: changing it
# in a rolling deploy would let an old and a new worker both think they
# hold the lease for the same scheduler, so treat it as a wire constant.
LEASE_CLASSID = 8743


def lock_key(scheduler_name: str) -> int:
    """Stable per-scheduler `objid` for the advisory lock.

    CRC32 is not a security hash — it is picked because it is stable
    across Python versions and platforms (unlike `hash()`, which is
    salted per process and would give every worker a different key,
    defeating the whole mechanism).
    """
    raw = zlib.crc32(scheduler_name.encode("utf-8")) & 0xFFFFFFFF
    # Postgres advisory-lock args are signed `integer`. Fold the top
    # half of the unsigned range into the negative half rather than
    # masking it off, so distinct names keep distinct keys.
    return raw - 0x100000000 if raw >= 0x80000000 else raw


@contextmanager
def scheduler_lease(
    scheduler_name: str, *, engine: Optional[Any] = None
) -> Iterator[bool]:
    """Yield True iff this process won the tick for `scheduler_name`.

    Always releases the lock on the way out, including when the tick
    body raises — an unreleased advisory lock would pin the scheduler
    to a single worker until that worker's connection closed.
    """
    if engine is None:
        from app.database import engine as _engine

        engine = _engine

    if engine.dialect.name != "postgresql":
        # Single-process engine (SQLite in tests) — see module docstring.
        yield True
        return

    conn = None
    acquired = False
    try:
        conn = engine.connect()
        acquired = bool(
            conn.execute(
                text("SELECT pg_try_advisory_lock(:cls, :obj)"),
                {"cls": LEASE_CLASSID, "obj": lock_key(scheduler_name)},
            ).scalar()
        )
    except Exception:  # noqa: BLE001
        # A DB blip must not silently stop the scheduler estate. Fail
        # OPEN (run the tick): the duplicate-work risk of a lost lease
        # is bounded and already the status quo ante, whereas failing
        # closed here would stop retention, export and digests for as
        # long as the blip lasts, with no alert.
        log.warning(
            "scheduler lease probe failed for %s — running the tick "
            "unguarded", scheduler_name, exc_info=True,
        )
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            conn = None
        yield True
        return

    try:
        if not acquired:
            log.debug(
                "scheduler %s: another worker holds this tick — skipping",
                scheduler_name,
            )
        yield acquired
    finally:
        try:
            if acquired:
                conn.execute(
                    text("SELECT pg_advisory_unlock(:cls, :obj)"),
                    {"cls": LEASE_CLASSID, "obj": lock_key(scheduler_name)},
                )
        except Exception:  # noqa: BLE001
            # Closing the connection below releases the lock anyway;
            # the explicit unlock is only to return a clean connection
            # to the pool.
            log.warning(
                "scheduler lease release failed for %s", scheduler_name,
                exc_info=True,
            )
        finally:
            conn.close()


def run_as_leader(
    scheduler_name: str, fn: Callable[..., Any], *args: Any, **kwargs: Any
) -> Any:
    """Run one scheduler tick, but only on the worker that wins the lease.

    Returns whatever `fn` returns, or None when the tick was skipped
    because a sibling worker is already running it. Callers treat the
    skip like an empty tick — none of them distinguish "nothing due"
    from "someone else did it", and neither should they.

    Synchronous by design: every loop already funnels its tick through
    `asyncio.to_thread(...)`, so wrapping there keeps the lock probe
    off the event loop with no extra machinery.
    """
    with scheduler_lease(scheduler_name) as leader:
        if not leader:
            return None
        return fn(*args, **kwargs)


__all__ = ["LEASE_CLASSID", "lock_key", "scheduler_lease", "run_as_leader"]
