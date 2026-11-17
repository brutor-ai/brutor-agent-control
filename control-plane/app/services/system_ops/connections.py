# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""System Operations Connections — outbound pool inspector + recycler

The Connections tab answers one question fast: "is any outbound pool
about to starve?" Operators on a 3am page-out need pool saturation,
not pretty graphs. We surface one entry per known pool kind and a
saturation ratio = checked_out / size so the UI can render a bar.

Pool-by-pool implementation notes:

  * **Postgres (control plane).** SQLAlchemy's `engine.pool` exposes
    `.size()`, `.checkedin()`, `.checkedout()`, `.overflow()`. These
    are the truth — pool stats live in-process. We surface them
    verbatim with a derived `saturation`.

  * **Redis.** The control plane uses an `async` redis client opened
    per-call (see `_probe_redis` in overview.py). There is no shared
    pool to introspect. We render an honest 'unknown' entry rather
    than fabricating numbers — the Caches tab is the right place to
    monitor Redis traffic. The pool object would only be meaningful
    if/when we introduce a long-lived shared client; the slot is here
    so the wire shape doesn't change later.

  * **Qdrant.** Same shape: every Qdrant call opens a one-shot
    `httpx.AsyncClient`. We report `client_count=1` (matches the
    per-request model) plus the most-recent latency derived from the
    `system_alerts` table when an alert exists, else null. The Rust
    proxy owns the hot path — control-plane operators are unlikely to
    care about Qdrant pool tuning here.

  * **LLM providers (httpx).** The Python control plane no longer
    proxies LLM requests (the Rust proxy does). We surface one entry
    per *configured* provider name so the UI can list them, but the
    pool counters are 'unknown' — the actual httpx pools live in the
    Rust process and surface through the Rust proxy's own metrics
    endpoint, not here. This keeps the Connections tab honest about
    what the control plane can see.

Recycle:

  * **Postgres.** `engine.dispose(close=True)` drops every checked-in
    connection and the next checkout opens a fresh socket. The
    `close=True` form deliberately closes connections still in use —
    safe because callers in the request path already wrap a session;
    the SQLAlchemy session re-establishes on the next .execute(). Use
    case is "the DB was bounced, some connections are stuck idle, the
    pool isn't recovering on its own".

  * **Redis.** No-op for now — the per-call client doesn't have a
    pool to drop. The endpoint returns `status='noop'` with a
    `detail` line explaining why so the operator UI shows it
    instead of silently doing nothing.

Authorisation is enforced at the endpoint layer; this module trusts
the caller. Every recycle returns `{status, detail}` so the endpoint
can surface that verbatim in its response body and audit row.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.composition.hooks import HOOKS, SYSTEM_OPS_CONNECTION_POOLS
from app.core.logging_system import BrutorLogger
from app.database.models.llm_provider import LlmProvider


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_pools(
    db: Session,
    tenant_id: Optional[str] = None,
    *,
    logger: Optional[BrutorLogger] = None,
) -> List[Dict[str, Any]]:
    """Build the Connections page entries.

    Returns one dict per known pool, in display order. Order is
    deliberate (infra first, then upstreams) — the UI renders without
    extra sorting so the test assertions stay stable across runs.
    """
    entries: List[Dict[str, Any]] = []
    entries.append(_postgres_pool(logger))
    entries.append(_redis_pool())
    # What an Enterprise Edition feature connects to (the vector database)
    # is a behaviour hook (`composition/hooks.py`); none in a composition
    # without the feature.
    for pools in HOOKS.all(SYSTEM_OPS_CONNECTION_POOLS):
        entries.extend(pools(db=db, tenant_id=tenant_id))
    entries.extend(_llm_provider_pools(db, tenant_id))
    return entries


def recycle_pool(
    db: Session,
    kind: str,
    *,
    logger: Optional[BrutorLogger] = None,
) -> Dict[str, str]:
    """Dispose + re-establish the named pool.

    Returns `{status, detail}`. `status` is one of:
      * `recycled` — pool was disposed; next checkout will open fresh.
      * `noop`     — pool kind has no pool to dispose (Redis today).
      * `error`    — dispose itself raised; detail carries the message.

    `kind` must be `'postgres'` or `'redis'` — anything else raises
    ValueError so the endpoint wrapper can map it to a 422 instead of
    silently no-op'ing.
    """
    if kind == "postgres":
        return _recycle_postgres(logger=logger)
    if kind == "redis":
        # Honest no-op: there's no pool to dispose yet. The status
        # 'noop' (rather than 'recycled') lets the audit row show
        # "operator tried, nothing happened" — useful in a postmortem.
        return {
            "status": "noop",
            "detail": (
                "Redis connections are opened per-call in the control "
                "plane today — there is no shared pool to dispose. This "
                "control will become a real recycle when the persistent "
                "client lands; the audit row still captures the click."
            ),
        }
    raise ValueError(
        f"recycle_pool: unsupported kind {kind!r}. Supported: 'postgres', 'redis'."
    )


# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------


def _postgres_pool(logger: Optional[BrutorLogger]) -> Dict[str, Any]:
    """SQLAlchemy QueuePool stats for the control-plane engine.

    The engine is module-level (`app.database.engine`); we re-import
    inside the function so a future test that swaps the engine via
    `app.database.engine = fake_engine` is honoured without monkey-
    patching this module too. Cost is negligible — Python caches the
    module in sys.modules.
    """
    try:
        from app.database import engine  # local import — see docstring

        pool = engine.pool
        # NullPool / StaticPool (used in SQLite test harness) don't
        # expose every method. We guard with getattr + default 0 so
        # the test harness doesn't 500 the endpoint just because it
        # picked a different pool class.
        size = _safe_int(getattr(pool, "size", lambda: 0)())
        checked_in = _safe_int(getattr(pool, "checkedin", lambda: 0)())
        checked_out = _safe_int(getattr(pool, "checkedout", lambda: 0)())
        overflow = _safe_int(getattr(pool, "overflow", lambda: 0)())

        return pool_entry(
            kind="postgres",
            label="Postgres",
            size=size,
            checked_in=checked_in,
            checked_out=checked_out,
            overflow=overflow,
            recyclable=True,
            client_count=None,
            detail=(
                f"{type(pool).__name__} — size={size}, in={checked_in}, "
                f"out={checked_out}, overflow={overflow}"
            ),
            most_recent_latency_ms=None,
        )
    except Exception as exc:  # pragma: no cover - degraded surface
        if logger:
            logger.warning(
                "Failed to read Postgres pool stats",
                context={"error": str(exc)},
            )
        return pool_entry(
            kind="postgres",
            label="Postgres",
            size=None,
            checked_in=None,
            checked_out=None,
            overflow=None,
            recyclable=True,
            client_count=None,
            detail=f"pool stats unavailable: {type(exc).__name__}",
            most_recent_latency_ms=None,
        )


def _recycle_postgres(logger: Optional[BrutorLogger]) -> Dict[str, str]:
    """Drop the SQLAlchemy pool. `dispose()` closes every connection
    in the pool (`close=True` by default) and resets bookkeeping; the
    next session checkout opens a brand-new socket against Postgres."""
    try:
        from app.database import engine

        engine.dispose()
        if logger:
            logger.warning(
                "Postgres engine pool disposed by operator",
                context={"action": "pool_reconnect", "kind": "postgres"},
            )
        return {
            "status": "recycled",
            "detail": (
                "SQLAlchemy engine pool disposed. Next checkout will "
                "open a fresh connection to Postgres."
            ),
        }
    except Exception as exc:
        if logger:
            logger.error(
                "Failed to dispose Postgres pool",
                context={"error": str(exc)},
                exc_info=True,
            )
        return {
            "status": "error",
            "detail": f"{type(exc).__name__}: {exc}",
        }


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------


def _redis_pool() -> Dict[str, Any]:
    """Best-effort Redis pool surface.

    The control plane today opens a fresh `redis.asyncio` client per
    call (see overview.py probe). That means there's no long-lived
    pool to introspect — surfacing fake numbers would mislead. We
    render `client_count='unknown'` and the detail line names the
    reason so the operator knows it's deliberate, not a bug.
    """
    from app.env_vars import REDIS_URL

    redis_url = REDIS_URL.exact()
    if not redis_url:
        return pool_entry(
            kind="redis",
            label="Redis",
            size=None,
            checked_in=None,
            checked_out=None,
            overflow=None,
            recyclable=False,
            client_count=0,
            detail="Redis is not configured for this deployment.",
            most_recent_latency_ms=None,
        )

    return pool_entry(
        kind="redis",
        label="Redis",
        size=None,
        checked_in=None,
        checked_out=None,
        overflow=None,
        recyclable=False,
        client_count=None,
        detail=(
            "Redis client is opened per-call in the control plane — "
            "no shared pool to introspect. Persistent-client tracking "
            "lands with the shared-pool migration."
        ),
        most_recent_latency_ms=None,
    )


# ---------------------------------------------------------------------------
# LLM providers
# ---------------------------------------------------------------------------


def _llm_provider_pools(
    db: Session, tenant_id: Optional[str]
) -> List[Dict[str, Any]]:
    """One entry per configured LLM provider, with 'unknown' counters.

    Rationale: the live httpx pools that talk to OpenAI / Anthropic /
    Bedrock / Vertex live in the Rust proxy. The control plane can
    list which providers EXIST (so the operator sees "there are 6
    providers configured") but doesn't have visibility into their
    pool state from here. The Connections tab points the operator at
    the proxy metrics endpoint via the entry's detail line.
    """
    # Provider rows are platform-wide (no tenant_id column) — every
    # tenant sees the same provider catalogue. We surface all of them
    # regardless of tenant_id so the tab is useful for both tenant
    # and platform admins.
    providers = (
        db.query(LlmProvider)
        .order_by(LlmProvider.display_name.asc().nullslast(), LlmProvider.id.asc())
        .all()
    )
    out: List[Dict[str, Any]] = []
    for p in providers:
        label = getattr(p, "display_name", None) or getattr(p, "key", None) or p.id
        out.append(
            pool_entry(
                kind=f"llm_provider:{p.id}",
                label=f"LLM · {label}",
                size=None,
                checked_in=None,
                checked_out=None,
                overflow=None,
                recyclable=False,
                client_count=None,
                detail=(
                    "Provider pool stats live in the Rust proxy "
                    "(see /metrics)."
                ),
                most_recent_latency_ms=None,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Shared entry shape
# ---------------------------------------------------------------------------


def pool_entry(
    *,
    kind: str,
    label: str,
    size: Optional[int],
    checked_in: Optional[int],
    checked_out: Optional[int],
    overflow: Optional[int],
    recyclable: bool,
    client_count: Optional[int],
    detail: str,
    most_recent_latency_ms: Optional[int],
) -> Dict[str, Any]:
    """One dict per pool. Saturation is derived here so the UI bar
    has a single source of truth.

    `saturation` is None when `size` is None or 0 — the UI then
    renders an indeterminate bar instead of dividing by zero. We
    deliberately don't clamp to [0,1] for the overflow case: an
    overflowing pool returns a value >1, and the UI styles that as
    an angry-red bar at 100% with a separate "overflowed" badge.
    """
    saturation: Optional[float] = None
    if size and size > 0 and checked_out is not None:
        saturation = round(checked_out / size, 4)

    return {
        "kind": kind,
        "label": label,
        "size": size,
        "checked_in": checked_in,
        "checked_out": checked_out,
        "overflow": overflow,
        "saturation": saturation,
        "client_count": client_count,
        "recyclable": recyclable,
        "most_recent_latency_ms": most_recent_latency_ms,
        "detail": detail,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def _safe_int(value: Any) -> Optional[int]:
    """SQLAlchemy pool stats are ints but some pool subclasses return
    the literal string 'NullPool' or raise on .size(). Coerce defensively
    so a weird response doesn't propagate as a 500."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
