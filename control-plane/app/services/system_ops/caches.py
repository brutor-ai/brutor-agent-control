# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Cache layer stats aggregation for the System Operations Console

Why this lives in the control plane at all: the authoritative cache
state — entry counts, hit/miss counters, per-key TTL — lives in the
Rust gateway-core's in-process moka stores plus Redis / Qdrant for the
semantic layer. The control plane can't read moka internals
directly, so this module surfaces a BEST-EFFORT view assembled from
durable sources operators can act on:

  * The `semantic` layer's hit/miss numbers come from `proxy_logs`
    over the last 60 minutes (`llm_cache_hit` boolean column —
    indexed by `ix_upl_llm_cache_hit_timestamp`). This is the only
    layer where we can give an honest hit-rate without round-
    tripping the Rust proxy, because every cache decision is
    already logged for billing.

  * The other 4 layers (`policy`, `group`, `tool`, `response`) live
    purely in-process. Until the gateway-core fan-out reply round-
    trip ships, this module
    surfaces zero-valued counters with the limitation called out
    in the response detail so the UI can render a "stats not
    available" hint instead of misleading numbers.

  * `last_flushed_at` is read from the `system_operation_log` table
    — we filter on `action='cache_flush'` and bucket by
    `scope_kind='cache_layer'`. The wire constant is
    `ACTION_CACHE_FLUSH` from system_operation_log.py.

The flush ACTION itself is endpoint-side (system_ops.py); this
module is read-only.

`scopes_available` is the static per-layer matrix —
which `CacheScope` values pair with each layer. Returned to the UI
so the flush modal scope-picker can disable invalid combinations
without a second round-trip.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database.models.proxy_log import ProxyLog
from app.database.models.system_operation_log import (
    ACTION_CACHE_FLUSH,
    SCOPE_CACHE_LAYER,
    SystemOperationLog,
)


# Hit/miss aggregation window for the semantic layer. 60 minutes is
# long enough that low-traffic tenants still see a non-zero hit-rate
# but short enough that the number "feels live" — operators expect
# stats to react within minutes of a config change, not hours.
SEMANTIC_WINDOW_MINUTES = 60


# Per-layer scope matrix. Treat as the wire
# contract between control plane and UI scope-picker; new layers added
# to the `CacheLayer` Literal must extend this map AND the gateway-core
# dispatcher in src/services/system_notify.rs.
LAYER_SCOPES: Dict[str, List[str]] = {
    # Semantic LLM cache (Qdrant + Redis): tenant-wide nukes the whole
    # collection, per-group lets operators flush only one routing
    # group's slice, per-model evicts entries that targeted a single
    # model id (e.g. after a model retirement).
    "semantic": ["tenant", "group", "model"],
    # Argument-policy cache (POLICY-10): keyed on (tenant, policy_id).
    # The Rust handle only exposes tenant-level invalidate — that's
    # all that's safe to expose here.
    "policy": ["tenant"],
    # Materialised resource-group cache: keyed on (tenant, group_id).
    # Tenant-level drop also clears the user_groups index per the
    # group_cache invariants documented in cache/mod.rs.
    "group": ["tenant"],
    # MCP tool cache: keyed on (tenant, mcp_server_id). The flush UX
    # most operators want is "drop the cached tool list for one
    # server" — the broader tenant variant is for incident recovery.
    "tool": ["mcp_server", "tenant"],
    # Provider-side response cache (routing-group cache config): keyed
    # on (tenant, group_id) with a per-group enable/ttl gate. The
    # 'config' scope matches the cluster-cache propagation channel
    # used by the routing-group cache editor.
    "response": ["config", "tenant"],
}


async def list_cache_stats(
    db: Session,
    tenant_id: str,
) -> List[Dict[str, Any]]:
    """Return one entry per cache layer for the Caches page.

    The order matches `CacheLayer` Literal in system_ops.py so the UI
    can render the 5 cards in a predictable column flow without re-
    sorting client-side.

    The 4 in-process layers (`policy`, `group`, `tool`, `response`)
    have their hit/miss/entry counts collected via the
    NOTIFY fan-out (`cache_stats_collector.collect_cache_stats`). The
    collector returns whatever lands within a 500ms window. When zero
    replicas reply (fresh boot before the first NOTIFY publisher hand-
    shake, dropped connection, partition), the layer rows surface a
    "no live counters" detail rather than misleading zeros.

    The `semantic` layer reads from `proxy_logs` aggregations as
    before — no fan-out needed because the data lives in the same DB
    we're already querying.
    """
    semantic = _semantic_stats(db, tenant_id)
    last_flush_by_layer = _last_flush_per_layer(db, tenant_id)

    # Fire the fan-out RPC for the 4 in-process layers. Wrapped in
    # try/except so a collector blow-up never breaks the whole
    # endpoint — operators still see the semantic card + last-flushed
    # badges even when the live-counter pipeline is wedged.
    try:
        from app.services.system_ops.cache_stats_collector import (
            collect_cache_stats,
        )

        live = await collect_cache_stats()
    except Exception:  # noqa: BLE001 — last-resort guard
        from app.services.system_ops.cache_stats_collector import (
            CacheStatsAggregate,
        )

        live = CacheStatsAggregate(timed_out=True)

    rows: List[Dict[str, Any]] = []

    for layer in ("semantic", "policy", "group", "tool", "response"):
        if layer == "semantic":
            hit = semantic["hit_count"]
            miss = semantic["miss_count"]
            rows.append(
                _row(
                    layer=layer,
                    hit_count=hit,
                    miss_count=miss,
                    item_count_approx=None,
                    last_flushed_at=last_flush_by_layer.get(layer),
                )
            )
            continue

        # In-process layer — pull from collector aggregate if available.
        aggregate = live.layers.get(layer)
        if aggregate is None and live.responding_replicas > 0:
            # The gateway answered and reported nothing for this layer: it
            # has no such counters (a Community gateway has no `response`
            # layer). Not a timeout, and not zero hits.
            rows.append(
                _row(
                    layer=layer,
                    hit_count=0,
                    miss_count=0,
                    item_count_approx=None,
                    last_flushed_at=last_flush_by_layer.get(layer),
                    detail="The gateway reports no counters for this layer.",
                )
            )
        elif aggregate is None or live.responding_replicas == 0:
            # No replicas answered in time. Render an honest "no live
            # data" hint rather than misleading zeros. The operator
            # can refresh; the fan-out is idempotent.
            replica_hint = (
                f"{live.responding_replicas} replica(s) responding "
                f"({', '.join(live.replica_ids) or 'none'})"
            )
            rows.append(
                _row(
                    layer=layer,
                    hit_count=0,
                    miss_count=0,
                    item_count_approx=None,
                    last_flushed_at=last_flush_by_layer.get(layer),
                    detail=(
                        f"No live counters — gateway-core fan-out "
                        f"timed out. {replica_hint}. Flush still works "
                        "via NOTIFY."
                    ),
                )
            )
        else:
            rows.append(
                _row(
                    layer=layer,
                    hit_count=aggregate.hits,
                    miss_count=aggregate.misses,
                    item_count_approx=aggregate.entries,
                    last_flushed_at=last_flush_by_layer.get(layer),
                    # A gateway that is one instance names none: there
                    # is nothing to aggregate and no name to print.
                    detail=(
                        "Live counters of the gateway."
                        if live.single_instance
                        else f"Aggregated across {live.responding_replicas} "
                        f"replica(s): {', '.join(live.replica_ids)}"
                    ),
                )
            )
    return rows


def _semantic_stats(db: Session, tenant_id: str) -> Dict[str, int]:
    """Count semantic-cache hit vs miss rows over the look-back window.

    `llm_cache_hit` is NULL on rows that aren't LLM cache decisions
    (MCP, OAuth, plus older rows pre-migration), so the WHERE clause
    filters those out explicitly. The composite index
    `ix_upl_llm_cache_hit_timestamp` keeps this query
    sub-50-ms even on the highest-traffic tenants we've measured.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=SEMANTIC_WINDOW_MINUTES)
    hits, misses = (
        db.query(
            func.coalesce(
                func.sum(func.cast(ProxyLog.llm_cache_hit == True, _bool_int())), 0
            ).label("hits"),
            func.coalesce(
                func.sum(func.cast(ProxyLog.llm_cache_hit == False, _bool_int())), 0
            ).label("misses"),
        )
        .filter(
            ProxyLog.tenant_id == tenant_id,
            ProxyLog.timestamp >= cutoff,
            ProxyLog.llm_cache_hit.isnot(None),
        )
        .one()
    )
    return {"hit_count": int(hits or 0), "miss_count": int(misses or 0)}


def _bool_int():
    """Cast target for `bool -> 0/1` in the SUM expression.

    SQLAlchemy needs an explicit cast type because we want a portable
    SUM over a boolean predicate. Lazy-imported to keep the module's
    top-level imports lean.
    """
    from sqlalchemy import Integer

    return Integer()


def _last_flush_per_layer(
    db: Session, tenant_id: str
) -> Dict[str, Optional[str]]:
    """Most recent `cache_flush` audit row per layer for this tenant.

    The Audit tab and the Caches page would otherwise diverge: this
    keeps the "Last flushed" badge on each card consistent with the
    audit trail. We read `scope_id` (the layer name we wrote into the
    flush audit row) rather than re-parsing payload JSON — faster, and
    the column is indexed via `ix_system_operation_log_tenant_created`.
    """
    rows = (
        db.query(
            SystemOperationLog.scope_id,
            func.max(SystemOperationLog.created_at).label("max_created_at"),
        )
        .filter(
            SystemOperationLog.tenant_id == tenant_id,
            SystemOperationLog.action == ACTION_CACHE_FLUSH,
            SystemOperationLog.scope_kind == SCOPE_CACHE_LAYER,
        )
        .group_by(SystemOperationLog.scope_id)
        .all()
    )
    out: Dict[str, Optional[str]] = {}
    for scope_id, max_at in rows:
        if scope_id is None or max_at is None:
            continue
        out[scope_id] = max_at.isoformat()
    return out


def _row(
    *,
    layer: str,
    hit_count: int,
    miss_count: int,
    item_count_approx: Optional[int],
    last_flushed_at: Optional[str],
    detail: Optional[str] = None,
) -> Dict[str, Any]:
    """Shape one CacheLayerStats wire entry.

    `hit_rate` is float in [0, 1]; we send a single decimal in the
    wire shape but compute with full precision so the UI can choose
    its own formatting.
    """
    total = hit_count + miss_count
    hit_rate = (hit_count / total) if total > 0 else 0.0
    return {
        "layer": layer,
        "hit_count": hit_count,
        "miss_count": miss_count,
        "hit_rate": hit_rate,
        # None signals "stats not available" rather than "zero items" —
        # different meanings the UI surfaces with different copy.
        "item_count_approx": item_count_approx,
        "last_flushed_at": last_flushed_at,
        "scopes_available": LAYER_SCOPES.get(layer, []),
        "detail": detail,
    }
