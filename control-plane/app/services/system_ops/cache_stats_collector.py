# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Cache stats fan-out — control-plane collector.

Mechanism:
    1. The admin UI hits GET /v1/admin/system/caches.
    2. `list_cache_stats` (caches.py) calls `collect_cache_stats(...)`
       below.
    3. We open a dedicated psycopg2 connection (NOT from the
       SQLAlchemy pool — LISTEN takes one connection forever and we
       don't want to starve the request pipeline), LISTEN on
       `brutor:cache:stats:reply`, NOTIFY the request channel with a
       fresh correlation_id, then `select()` on the connection's
       socket for replies until a 500ms (default) wall-clock budget
       expires.
    4. Each reply carrying our correlation_id contributes its layer
       counts to a per-layer aggregate. Replies from prior or
       concurrent operator clicks (different correlation_id) are
       discarded — Postgres NOTIFY delivers them all to every
       LISTENer, so we must filter ourselves.
    5. UNLISTEN + close — the connection is single-use. Holding it
       across operator clicks would be marginally cheaper but
       complicates lifecycle on a long-running Python process.

Why not asyncpg:
    The control plane is mostly SQLAlchemy-sync and doesn't pull in
    asyncpg as a runtime dep. Adding it just for this collector is
    weight we don't need — psycopg2 is already in the wheel set
    (`psycopg2-binary` in pyproject.toml). We bridge to async by
    running the blocking `select()` in `asyncio.to_thread`, which
    is exactly how the control plane already handles other blocking
    IO from FastAPI handlers.

Failure modes (all observable, none fatal):
    * Connection fails to open: log + return EmptyAggregate. The
      caller renders "no live counters available" rather than
      misleading zeros — see Phase F's detail-string copy.
    * No replicas reply within the timeout window: the aggregate's
      `responding_replicas` is 0 and the per-layer counts stay at
      None. Caller renders a "0 replicas responding" hint and the
      operator knows it's a fan-out problem, not a "cache is empty"
      conclusion.
    * A reply arrives late (after timeout): discarded. The Postgres
      NOTIFY queue empties on connection close so we don't leak.
    * A reply has a corrupted JSON payload or wrong shape: logged at
      WARN, dropped. Other replicas' valid replies still count.

We deliberately don't retry on partial responses. Re-firing the
request would duplicate the in-flight replies — Postgres NOTIFY has
no de-dup. The operator clicks Refresh; that's the retry.
"""

from __future__ import annotations

import asyncio
import json
import select as select_module
import secrets
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import psycopg2
import psycopg2.extensions

from app.core.logging_system import BrutorLogger
from app.services.system_ops.notify import (
    CHANNEL_CACHE_STATS_REPLY,
    CHANNEL_CACHE_STATS_REQUEST,
    CacheStatsReplyPayload,
    CacheStatsRequestPayload,
    PAYLOAD_BYTE_LIMIT,
)
from app.env_vars import DATABASE_URL

logger = BrutorLogger("cache-stats-collector")


# Default collection window. Long enough that a healthy local
# multi-replica fan-out lands every reply (~5-20ms per replica), short
# enough that the operator doesn't watch a spinner. Operators can pass
# a larger budget on the GET endpoint if they're chasing a slow
# replica in production.
DEFAULT_TIMEOUT_MS = 500

# Wire constants for the 4 in-process cache layers reported by
# gateway-core. Kept in sync with `system_notify.rs::handle_cache_stats_request`
# — the semantic layer is intentionally absent (it reads from
# `proxy_logs`, not in-process counters, so the control plane reads
# it directly without fan-out).
EXPECTED_LAYERS = ("policy", "group", "tool", "response")


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class LayerAggregate:
    """Aggregated counter values for one cache layer across replicas."""

    hits: int = 0
    misses: int = 0
    entries: int = 0


@dataclass
class CacheStatsAggregate:
    """Full collector result handed back to `list_cache_stats`.

    `responding_replicas` is the count of distinct replica_ids whose
    replies we accepted. `replica_ids` is the actual list so the UI
    can render them in a tooltip ("3 of 5 replicas reporting:
    gateway-core-1, gateway-core-2, gateway-core-4").

    `layers` may be empty when no replicas responded — callers should
    treat empty-layers + responding_replicas=0 as the "fan-out
    timed out" case and render copy accordingly.
    """

    responding_replicas: int = 0
    replica_ids: List[str] = field(default_factory=list)
    layers: Dict[str, LayerAggregate] = field(default_factory=dict)
    timed_out: bool = False
    #: The answer came from a gateway that is one instance and names none
    #: (`responding_replicas` is 1 and `replica_ids` is empty).
    single_instance: bool = False


#: How the one reply of a gateway that names no instance is kept in the set
#: of replicas seen. Not a possible `replica_id` (those have a length).
SINGLE_INSTANCE = ""


def finish_aggregate(
    aggregate: CacheStatsAggregate,
    layer_totals: Dict[str, LayerAggregate],
    seen_replicas: set,
    reported_layers: Optional[set] = None,
) -> CacheStatsAggregate:
    """Fill `aggregate` from what a collection accepted."""
    aggregate.single_instance = SINGLE_INSTANCE in seen_replicas
    aggregate.responding_replicas = len(seen_replicas)
    aggregate.replica_ids = sorted(seen_replicas - {SINGLE_INSTANCE})
    # Only the layers a reply reported: a layer nobody reported is "no
    # data", not zero hits (a Community gateway has no `response` layer).
    # With nothing accepted there are no layers at all.
    if aggregate.responding_replicas > 0:
        aggregate.layers = {
            name: layer_totals[name]
            for name in EXPECTED_LAYERS
            if reported_layers is None or name in reported_layers
        }
    return aggregate


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------


async def collect_cache_stats(
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> CacheStatsAggregate:
    """Fan-out RPC for per-layer cache counters.

    Async-safe: the blocking psycopg2 + select() loop runs in a
    worker thread via `asyncio.to_thread`, so the FastAPI event loop
    stays free for other requests during the (~500ms) collection
    window.

    Args:
        timeout_ms: wall-clock budget for collecting replies. Callers
            can dial this up to chase a slow replica; the only cost
            is the operator waiting longer for the page to render.

    Returns:
        A [`CacheStatsAggregate`] — never raises on infra failures.
        Returns an empty aggregate (responding_replicas=0,
        timed_out=True) on any operational failure so the caller
        can render an honest "no live counters available" state.
    """
    if timeout_ms <= 0:
        timeout_ms = DEFAULT_TIMEOUT_MS

    # Run the blocking work off the event loop. `to_thread` is the
    # Python 3.9+ idiom — the worker is a daemon thread that doesn't
    # block process shutdown if a collector somehow hangs (which it
    # shouldn't — the loop has its own timeout).
    return await asyncio.to_thread(_collect_sync, timeout_ms)


def _collect_sync(timeout_ms: int) -> CacheStatsAggregate:
    """Synchronous collection loop — called from `asyncio.to_thread`."""
    # The DSN is read from the env so the collector picks up the same
    # DB the SQLAlchemy engine targets. We deliberately do NOT reuse
    # an engine connection — every existing engine connection is in
    # the pool, and LISTEN locks one forever.
    database_url = DATABASE_URL.exact()
    if not database_url:
        logger.warning(
            "Cache stats collector skipped — DATABASE_URL not set",
            context={"timeout_ms": timeout_ms},
        )
        return CacheStatsAggregate(timed_out=True)

    # Fresh correlation_id per call. 16 hex chars = 64 bits of entropy
    # — well above the min_length=8 enforced by CacheStatsRequestPayload,
    # and small enough to stay under PAYLOAD_BYTE_LIMIT comfortably.
    correlation_id = secrets.token_hex(8)

    conn: Optional[psycopg2.extensions.connection] = None
    try:
        # `psycopg2.connect` accepts the same libpq DSN format as
        # `DATABASE_URL`. We set AUTOCOMMIT so LISTEN / NOTIFY fire
        # immediately — they're not transactional state.
        conn = psycopg2.connect(database_url)
        conn.set_isolation_level(
            psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT
        )

        # Quote the channel string with `format`-style binding to dodge
        # SQL injection — the channel constant is a static literal but
        # using the binding keeps the call site safe if a future channel
        # name ever ends up dynamic.
        with conn.cursor() as cur:
            cur.execute(f'LISTEN "{CHANNEL_CACHE_STATS_REPLY}"')

        # Validate + publish the request. Same Pydantic model the
        # subscriber expects — strict, extras-forbidden.
        request_payload = CacheStatsRequestPayload(
            correlation_id=correlation_id,
        )
        payload_json = json.dumps(
            request_payload.model_dump(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        if len(payload_json.encode("utf-8")) > PAYLOAD_BYTE_LIMIT:
            # Defensive only — the request payload is tiny.
            logger.error(
                "Cache stats request payload over byte cap",
                context={
                    "bytes": len(payload_json.encode("utf-8")),
                    "limit": PAYLOAD_BYTE_LIMIT,
                },
            )
            return CacheStatsAggregate(timed_out=True)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_notify(%s, %s)",
                (CHANNEL_CACHE_STATS_REQUEST, payload_json),
            )

        # Collection loop. `select()` blocks on the socket for at
        # most the remaining budget. `conn.poll()` then drains the
        # notification queue without an extra syscall round-trip.
        aggregate = CacheStatsAggregate()
        seen_replicas: set[str] = set()
        reported_layers: set[str] = set()
        layer_totals: Dict[str, LayerAggregate] = {
            layer: LayerAggregate() for layer in EXPECTED_LAYERS
        }
        deadline = time.monotonic() + (timeout_ms / 1000.0)

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                aggregate.timed_out = True
                break
            ready = select_module.select([conn], [], [], remaining)
            if ready == ([], [], []):
                # Timeout with no data — done.
                aggregate.timed_out = True
                break
            # Drain everything that's arrived.
            conn.poll()
            while conn.notifies:
                notify = conn.notifies.pop(0)
                if notify.channel != CHANNEL_CACHE_STATS_REPLY:
                    # Defensive — we only LISTEN'd on one channel,
                    # but other LISTENers on the same connection
                    # (none today) could land here.
                    continue
                merged = _merge_one_reply(
                    payload=notify.payload,
                    correlation_id=correlation_id,
                    layer_totals=layer_totals,
                    seen_replicas=seen_replicas,
                    reported_layers=reported_layers,
                )
                if not merged:
                    # Either wrong correlation_id (a concurrent
                    # operator click) or malformed — already logged
                    # inside the merge helper.
                    continue

        finish_aggregate(aggregate, layer_totals, seen_replicas, reported_layers)
        return aggregate

    except psycopg2.Error as exc:
        logger.warning(
            "Cache stats collector psycopg2 error — returning empty aggregate",
            context={
                "error": str(exc),
                "correlation_id": correlation_id,
            },
        )
        return CacheStatsAggregate(timed_out=True)
    except Exception as exc:  # noqa: BLE001 — last-resort guard
        logger.error(
            "Cache stats collector unexpected error",
            context={
                "error": str(exc),
                "type": type(exc).__name__,
                "correlation_id": correlation_id,
            },
            exc_info=True,
        )
        return CacheStatsAggregate(timed_out=True)
    finally:
        if conn is not None:
            try:
                with conn.cursor() as cur:
                    cur.execute(f'UNLISTEN "{CHANNEL_CACHE_STATS_REPLY}"')
            except Exception:  # noqa: BLE001
                # Best-effort — connection is about to close anyway.
                pass
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def _merge_one_reply(
    *,
    payload: str,
    correlation_id: str,
    layer_totals: Dict[str, LayerAggregate],
    seen_replicas: set[str],
    reported_layers: Optional[set] = None,
) -> bool:
    """Decode one reply, validate, merge into the running totals.

    One collection is EITHER the single reply of a gateway that names no
    instance (no `replica_id`: the Community gateway), OR any number of
    replies from distinct named replicas (the Enterprise Clustering
    extension). The first reply accepted decides which. A reply of the other
    kind, a second unnamed reply and a repeated `replica_id` are dropped:
    each could count one instance twice, and nothing in a reply can show
    that it did not.

    Returns True when the reply was accepted, False when it was
    discarded (wrong correlation_id, dup replica, malformed). Logs
    drops at debug/warn — never at error, since an unexpected wire
    payload is "weird" but not "broken".
    """
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as exc:
        logger.warning(
            "Cache stats reply: JSON decode failed",
            context={"error": str(exc), "bytes": len(payload)},
        )
        return False

    try:
        reply = CacheStatsReplyPayload.model_validate(decoded)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Cache stats reply: shape mismatch",
            context={"error": str(exc)},
        )
        return False

    if reply.correlation_id != correlation_id:
        # Concurrent operator click; not for us.
        return False

    replica = SINGLE_INSTANCE if reply.replica_id is None else reply.replica_id
    if replica in seen_replicas:
        # Replica double-reported (rare — a retry on a flaky
        # publish path could cause it), or a second reply that names no
        # instance. Drop the duplicate so we don't double-count.
        logger.debug(
            "Cache stats reply: duplicate replica_id",
            context={"replica_id": reply.replica_id},
        )
        return False
    if seen_replicas and (replica == SINGLE_INSTANCE) != (SINGLE_INSTANCE in seen_replicas):
        # A named reply after the single instance's, or the other way round.
        logger.warning(
            "Cache stats reply: named and unnamed replies in one collection; dropped",
            context={"replica_id": reply.replica_id},
        )
        return False
    seen_replicas.add(replica)

    for layer_name, layer in reply.layers.items():
        if layer_name not in layer_totals:
            # Replica reported a layer the control plane doesn't
            # know about — version skew. Log + ignore; the operator
            # will see the missing layer in the UI and know to look
            # at deployed versions.
            logger.debug(
                "Cache stats reply: unknown layer",
                context={
                    "layer": layer_name,
                    "replica_id": reply.replica_id,
                },
            )
            continue
        if reported_layers is not None:
            reported_layers.add(layer_name)
        total = layer_totals[layer_name]
        total.hits += layer.hits
        total.misses += layer.misses
        # Entries SUM across replicas matches what the operator wants
        # to see — "total cache footprint across the fleet". Per-
        # replica detail is in the replica_ids tooltip if they need
        # to drill in.
        total.entries += layer.entries

    return True
