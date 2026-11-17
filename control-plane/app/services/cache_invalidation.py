# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Cache Invalidation Publisher

Publishes cache invalidation messages to Redis pub/sub so that all
Core proxy pods can evict stale entries from their in-memory caches.

Called from admin API endpoints after database writes (model CRUD,
group changes, membership changes, guardrail updates, etc.).

Why a failed publish is not "just a warning"
--------------------------------------------
The DB write is already committed by the time we publish. If the
publish then fails (Redis down, connection reset), the operator gets
a 200 for a governance change the running proxies have not been told
about — they keep enforcing the previous config until the entry's own
TTL expires, which is up to ten minutes for some caches. On a
platform whose posture is fail-closed by default, that is the one
path where a control change quietly fails *open*.

We cannot roll the commit back, so the answer is to stop reporting
unqualified success: every failure is recorded on the request (the
`CacheInvalidationWarningMiddleware` turns that into a `Warning` and
an `X-Brutor-Cache-Invalidated: false` header on the response) and
raises a `downstream_cache` SystemAlert so the outage is visible in
the console rather than only in a WARNING log line nobody reads.

Publish only after the commit
-----------------------------
Handlers call `invalidate_proxy_cache` inside the handler, but the
request's transaction is committed by the `get_db` dependency only
afterwards. Publishing there opened a window in which the core evicted
on the message, reloaded the still-OLD row and cached it until TTL — a
tightened limit or revoked grant applying late. So when the request's
session (bound by `get_db`, see `bind_request_session`) holds
uncommitted writes, the invalidation is queued on that session instead:
the session's root `after_commit` moves it to a ready list, a rollback
discards it, and `CommitBeforeResponseMiddleware` publishes the ready
list. When there is nothing uncommitted (the handler already
committed, or the request has no bound session) it publishes at once,
as before.

In the FastAPI in use, `get_db`'s teardown commit runs AFTER the
response has been sent — too late for the Warning header, and too late
to turn a failed commit into an error. The middleware therefore commits
any request session with uncommitted writes as the response starts
(`commit_before_response`), publishes, and only then lets the headers
go — so a failed publish still reaches the operator, and a failed
commit becomes a 500 instead of a 200.
"""

import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import event
from sqlalchemy.orm import Session

logger = logging.getLogger("cache-invalidation")

# Key under which failures are recorded on the ASGI scope's `state`
# dict. The middleware reads the same key — see
# `app/middleware/cache_invalidation.py`.
FAILURE_STATE_KEY = "cache_invalidation_failures"

# A Redis outage makes *every* config write fail to publish. The alert
# row is deduped by `dedup_key`, but the lookup+update still costs a
# round trip per write, so throttle the attempt in-process. One alert
# refresh a minute is plenty to keep `last_seen_at` moving.
_ALERT_THROTTLE_SECONDS = 60.0
_alert_lock = threading.Lock()
_last_alert_attempt: float = 0.0

INVALIDATION_CHANNEL = "brutor:cache:invalidate"

#: A declared dependency edge changed. The core's group
#: cache derives the tenant's card → shared-service map from these edges
#: (accepted-caller enforcement resolves the target service through it), so
#: every edge create / update / delete must drop it. Wire contract with the
#: core's `cache/mod.rs` match arm (pinned in tests/test_cache_invalidation.py).
INVALIDATION_AI_SYSTEM_DEPENDENCY = "ai_system_dependency"

# Per-attempt bounds for the publisher's Redis client. Publishes run on the
# admin request path and the pool reconnects lazily inside them, so every
# attempt must fail fast when Redis is away. One retry (no backoff) lets a
# pooled socket that went stale across a Redis restart be replaced by a fresh
# connection without losing that publish; worst case during an outage is two
# connect timeouts.
PUBLISHER_SOCKET_CONNECT_TIMEOUT_SECS = 2.0
PUBLISHER_SOCKET_TIMEOUT_SECS = 2.0
PUBLISHER_RETRIES = 1


class _PublisherHealth:
    """Outage state for the publisher, so an outage is logged once
    (WARNING when it starts, INFO when it ends) rather than per publish.

    Process-wide, like the client it describes. Thread-safe because the
    sync publisher can run from threadpool handlers.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._degraded = False

    def reset(self) -> None:
        with self._lock:
            self._degraded = False

    @property
    def degraded(self) -> bool:
        return self._degraded

    def mark_failed(self) -> bool:
        """Record a failure; True when this starts a new outage."""
        with self._lock:
            started = not self._degraded
            self._degraded = True
            return started

    def mark_ok(self) -> bool:
        """Record a success; True when this ends an outage."""
        with self._lock:
            recovered = self._degraded
            self._degraded = False
            return recovered


_publisher_health = _PublisherHealth()


def _note_publish_failure(error: Exception, *, sync: bool = False) -> None:
    if _publisher_health.mark_failed():
        logger.warning(
            "Cache invalidation publish failed — proxies fall back to TTL "
            "refresh until Redis is reachable again (logged once per outage; "
            "the client reconnects on the next publish)%s: %s",
            " (sync)" if sync else "",
            error,
        )
    else:
        logger.debug("Cache invalidation publish failed (outage ongoing): %s", error)


def _note_publish_success() -> None:
    if _publisher_health.mark_ok():
        logger.info("Redis cache-invalidation publisher recovered — push invalidation restored")


def create_invalidation_publisher(redis_url: str):
    """Build the publisher's async Redis client WITHOUT connecting.

    `redis.asyncio` clients are lazy: the pool opens a connection on the
    first command and replaces a broken one on the next. So the client is
    created once at boot regardless of whether Redis is up yet, and each
    publish reconnects if needed — a Redis that was unreachable at boot no
    longer disables push invalidation for the life of the process.
    """
    import redis.asyncio as redis_async
    from redis.asyncio.retry import Retry
    from redis.backoff import NoBackoff

    return redis_async.from_url(
        redis_url,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=PUBLISHER_SOCKET_CONNECT_TIMEOUT_SECS,
        socket_timeout=PUBLISHER_SOCKET_TIMEOUT_SECS,
        retry=Retry(NoBackoff(), PUBLISHER_RETRIES),
    )


async def probe_invalidation_publisher(redis_client) -> Optional[str]:
    """Boot-time reachability check, for logging only.

    Returns None when Redis answered, else the error text. A failure marks
    the outage as already reported (the caller logs it), so publishes during
    the same outage do not warn again and the first successful publish logs
    the recovery. The client is kept either way.
    """
    try:
        await redis_client.ping()
    except Exception as e:  # noqa: BLE001 — probe must never raise
        _publisher_health.mark_failed()
        return str(e) or type(e).__name__
    _publisher_health.mark_ok()
    return None


async def publish_invalidation(
    redis_client,
    resource_type: str,
    tenant_id: str,
    resource_id: str,
    action: str = "update",
) -> bool:
    """
    Publish a cache invalidation message to Redis pub/sub.

    Args:
        redis_client: Redis async client (from app.state.redis)
        resource_type: "model", "routing_group", "resource_group", "skill",
                       "guardrail", "membership", "assign"
        tenant_id: Tenant ID
        resource_id: Resource ID (model ID, group ID, etc.)
        action: "create", "update", "delete", "assign"

    Returns:
        True if published successfully, False otherwise
    """
    if redis_client is None:
        logger.debug("Redis not available — skipping cache invalidation publish")
        return False

    message = json.dumps({
        "type": resource_type,
        "action": action,
        "tenant_id": tenant_id,
        "id": resource_id,
    })

    try:
        await redis_client.publish(INVALIDATION_CHANNEL, message)
    except Exception as e:  # noqa: BLE001 — a publish never fails the request
        _note_publish_failure(e)
        return False
    _note_publish_success()
    logger.debug(
        f"Cache invalidation published: {resource_type}/{action} "
        f"tenant={tenant_id} id={resource_id}"
    )
    return True


def record_invalidation_failure(
    request,
    resource_type: str,
    tenant_id: str,
    resource_id: str,
    action: str,
) -> None:
    """Mark this request as having committed a change the proxies were
    not told about.

    Writes onto the ASGI scope's `state` dict rather than a ContextVar:
    the endpoint runs in a task the middleware does not share a context
    with, but both see the same scope. Deliberately never raises — a
    bookkeeping failure must not turn a committed 200 into a 500.
    """
    _record_failure_on_scope(
        getattr(request, "scope", None), resource_type, tenant_id, resource_id, action
    )


def _record_failure_on_scope(
    scope,
    resource_type: str,
    tenant_id: str,
    resource_id: str,
    action: str,
) -> None:
    try:
        if not isinstance(scope, dict):
            return
        state = scope.setdefault("state", {})
        if not isinstance(state, dict):
            return
        failures: List[Dict[str, str]] = state.setdefault(FAILURE_STATE_KEY, [])
        failures.append({
            "type": resource_type,
            "action": action,
            "tenant_id": tenant_id,
            "id": resource_id,
        })
    except Exception:  # pragma: no cover — defensive
        logger.debug("Could not record cache-invalidation failure on request", exc_info=True)


def _raise_publish_failure_alert(resource_type: str, tenant_id: Optional[str]) -> None:
    """Raise/refresh the `downstream_cache` alert for a dead publisher.

    Throttled in-process and wrapped whole: the caller has already
    committed its write, so nothing here may propagate.
    """
    global _last_alert_attempt

    now = time.monotonic()
    with _alert_lock:
        if now - _last_alert_attempt < _ALERT_THROTTLE_SECONDS:
            return
        _last_alert_attempt = now

    try:
        from datetime import datetime, timezone

        from app.database import SessionLocal
        from app.database.models.system_alert import SystemAlert
        from ulid import ULID

        # Platform-wide, not tenant-scoped: the publisher being down
        # affects every tenant's writes at once, so one row says it.
        dedup_key = "platform|downstream_cache|cache.invalidation.publisher|publish_failed"
        db = SessionLocal()
        try:
            existing = (
                db.query(SystemAlert)
                .filter(SystemAlert.dedup_key == dedup_key)
                .filter(SystemAlert.status.in_(("active", "acknowledged")))
                .first()
            )
            message = (
                "Configuration changes are committing to the database but the cache "
                "invalidation message is not reaching the proxies. Until this is "
                "restored, running proxies keep enforcing the previously cached "
                "configuration until each entry's TTL expires (up to 10 minutes). "
                f"Most recent affected resource type: {resource_type}."
            )
            if existing is not None:
                existing.last_seen_at = datetime.now(timezone.utc)
                existing.occurrence_count = (existing.occurrence_count or 0) + 1
                existing.message = message
            else:
                db.add(SystemAlert(
                    id=f"sysalert-{ULID()}",
                    tenant_id=None,
                    severity="high",
                    category="downstream_cache",
                    source="cache.invalidation.publisher",
                    resource_type="cache_invalidation",
                    resource_id=None,
                    resource_name="Proxy cache invalidation channel",
                    error_code="publish_failed",
                    title="Proxy cache invalidation is not being published",
                    message=message,
                    context={"last_resource_type": resource_type, "last_tenant_id": tenant_id},
                    status="active",
                    dedup_key=dedup_key,
                ))
            db.commit()
        finally:
            db.close()
    except Exception:
        logger.warning("Could not raise cache-invalidation publisher alert", exc_info=True)


async def invalidate_proxy_cache(
    request,
    resource_type: str,
    tenant_id: str,
    resource_id: str,
    action: str = "update",
) -> bool:
    """
    Convenience wrapper that extracts Redis from the FastAPI request.

    Usage in endpoints:
        from app.services.cache_invalidation import invalidate_proxy_cache
        await invalidate_proxy_cache(request, "model", tenant_id, model.id)

    When the request's session holds uncommitted writes the message is
    queued and published after the commit (dropped on rollback) — see
    the module docstring; the call then returns True ("accepted"), and a
    failure of the later publish is surfaced the same way as below.

    Otherwise returns True only when the proxies have actually been
    told. On False the failure is recorded on the request so the
    response carries a warning header, and a SystemAlert is raised —
    callers do not need to bind the return value for that to happen.
    """
    entry = (resource_type, tenant_id, resource_id, action)
    if _defer_until_commit(request, entry):
        return True
    return await _publish_and_surface(
        _redis_of(getattr(request, "app", None)), getattr(request, "scope", None), entry
    )


def _redis_of(app) -> Any:
    state = getattr(app, "state", None)
    return getattr(state, "redis", None) if state is not None else None


async def _publish_and_surface(redis_client, scope, entry: "_Entry") -> bool:
    """Publish one message; on failure mark the response and raise the alert."""
    resource_type, tenant_id, resource_id, action = entry
    ok = await publish_invalidation(redis_client, resource_type, tenant_id, resource_id, action)
    if not ok:
        _record_failure_on_scope(scope, resource_type, tenant_id, resource_id, action)
        _raise_publish_failure_alert(resource_type, tenant_id)
    return ok


# =============================================================================
# Commit-aligned deferral
# =============================================================================

#: (resource_type, tenant_id, resource_id, action)
_Entry = Tuple[str, str, str, str]

#: scope["state"] key holding the request's `get_db` session.
SESSION_STATE_KEY = "db_session"
#: scope["state"] key set by `CommitBeforeResponseMiddleware`. Deferral is
#: only allowed when something will commit and publish the queue.
DEFERRAL_STATE_KEY = "commit_before_response"

# Session.info keys.
_TRACKED = "brutor_ci_tracked"   # a get_db request session
_WROTE = "brutor_ci_wrote"       # current root txn flushed / executed DML
_PENDING = "brutor_ci_pending"   # queued, awaiting the root commit
_READY = "brutor_ci_ready"       # committed, awaiting publish


def _scope_state(request) -> Optional[dict]:
    scope = getattr(request, "scope", None)
    if not isinstance(scope, dict):
        return None
    state = scope.get("state")
    return state if isinstance(state, dict) else None


def bind_request_session(request, session: Session) -> None:
    """Called by `get_db`: expose the request's session to
    `invalidate_proxy_cache` and the deferral middleware."""
    session.info[_TRACKED] = True
    if request is None:
        return
    scope = getattr(request, "scope", None)
    if isinstance(scope, dict):
        state = scope.setdefault("state", {})
        if isinstance(state, dict):
            state[SESSION_STATE_KEY] = session


def _has_uncommitted_writes(session: Session) -> bool:
    if session.info.get(_WROTE):
        return True
    return bool(session.new or session.deleted or session.dirty)


def _defer_until_commit(request, entry: _Entry) -> bool:
    """Queue `entry` on the request's session if its transaction holds
    uncommitted writes; False means "publish now".

    Nothing uncommitted means the change this announces is already
    durable (the handler committed explicitly) or lives outside this
    session — publishing at once is what it always did.
    """
    state = _scope_state(request)
    if not state or not state.get(DEFERRAL_STATE_KEY):
        return False
    session = state.get(SESSION_STATE_KEY)
    if not isinstance(session, Session) or not session.info.get(_TRACKED):
        return False
    try:
        if not session.in_transaction() or not _has_uncommitted_writes(session):
            return False
    except Exception:  # noqa: BLE001 — when unsure, publish (today's behaviour)
        logger.debug("Could not inspect request session; publishing now", exc_info=True)
        return False
    # dict as an ordered set: identical entries collapse.
    session.info.setdefault(_PENDING, {})[entry] = None
    return True


def _on_after_flush(session: Session, _flush_context) -> None:
    if session.info.get(_TRACKED):
        session.info[_WROTE] = True


def _on_do_orm_execute(orm_execute_state) -> None:
    session = orm_execute_state.session
    if session.info.get(_TRACKED) and not orm_execute_state.is_select:
        session.info[_WROTE] = True


def _on_after_commit(session: Session) -> None:
    # Savepoint releases fire this too; only the root commit is durable.
    if not session.info.get(_TRACKED) or session.in_nested_transaction():
        return
    pending = session.info.pop(_PENDING, None)
    if pending:
        session.info.setdefault(_READY, {}).update(pending)


def _on_after_transaction_end(session: Session, transaction) -> None:
    if transaction.parent is not None or not session.info.get(_TRACKED):
        return
    session.info.pop(_WROTE, None)
    # A root commit already moved its queue to READY; anything still
    # pending here belongs to a transaction that rolled back or was closed.
    dropped = session.info.pop(_PENDING, None)
    if dropped:
        logger.debug("Dropped %d cache invalidation(s): transaction rolled back", len(dropped))


_SESSION_HOOKS = (
    ("after_flush", _on_after_flush),
    ("do_orm_execute", _on_do_orm_execute),
    ("after_commit", _on_after_commit),
    ("after_transaction_end", _on_after_transaction_end),
)


def install_session_hooks() -> None:
    """Idempotent. The hooks are no-ops for sessions `get_db` did not bind."""
    for name, fn in _SESSION_HOOKS:
        if not event.contains(Session, name, fn):
            event.listen(Session, name, fn)


install_session_hooks()


def _bound_session(scope) -> Optional[Session]:
    state = scope.get("state") if isinstance(scope, dict) else None
    session = state.get(SESSION_STATE_KEY) if isinstance(state, dict) else None
    return session if isinstance(session, Session) else None


async def commit_before_response(scope) -> None:
    """At `http.response.start`: commit the request's session if its
    transaction holds uncommitted writes, then publish every invalidation
    that has committed.

    Applies to every request, not only those that invalidate: `get_db`'s
    own commit runs after the response is sent, so a commit failure there
    reached the client as a 200 for a change that was never saved. Here a
    failed commit propagates before any header is sent — `get_db` rolls
    back and the request ends in a 500 — and nothing is published.
    Read-only requests are not committed here (their transaction is
    closed by `get_db` as before).
    """
    session = _bound_session(scope)
    if session is None:
        return
    if session.info.get(_PENDING) or (
        session.in_transaction() and _has_uncommitted_writes(session)
    ):
        from starlette.concurrency import run_in_threadpool

        await run_in_threadpool(session.commit)
    await publish_committed(scope)


async def publish_committed(scope) -> None:
    """Publish invalidations whose transaction has committed. Never raises."""
    session = _bound_session(scope)
    if session is None:
        return
    ready = session.info.pop(_READY, None)
    if not ready:
        return
    redis_client = _redis_of(scope.get("app"))
    for entry in ready:
        try:
            await _publish_and_surface(redis_client, scope, entry)
        except Exception:  # pragma: no cover — _publish_and_surface is non-raising
            logger.warning("Deferred cache invalidation publish raised", exc_info=True)


def publish_invalidation_sync(
    redis_client,
    resource_type: str,
    tenant_id: str,
    resource_id: str,
    action: str = "update",
) -> bool:
    """
    Synchronous wrapper for environments where async is not available.
    Uses fire-and-forget Redis publish.
    """
    if redis_client is None:
        return False

    message = json.dumps({
        "type": resource_type,
        "action": action,
        "tenant_id": tenant_id,
        "id": resource_id,
    })

    try:
        redis_client.publish(INVALIDATION_CHANNEL, message)
    except Exception as e:  # noqa: BLE001 — a publish never fails the request
        _note_publish_failure(e, sync=True)
        return False
    _note_publish_success()
    return True
