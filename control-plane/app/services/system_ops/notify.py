# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""System Operations Console NOTIFY publisher.

The control plane fires `pg_notify(channel, payload)` from the System
Console endpoints; every gateway-core / gateway-control replica that
LISTENs picks it up within milliseconds. The architecture choice is
documented in `architecture_cluster_cache_propagation`: we ride the
existing Postgres NOTIFY bus that the cache invalidation layer
already uses — no Redis pub/sub, no separate message bus.

Two contracts this module enforces:

  1. **Channel registry.** Every channel a System endpoint can publish
     to is enumerated as a `CHANNEL_*` constant + has a Pydantic
     payload model declared alongside. Calling `publish_system_notify`
     with a channel string that isn't in the registry raises — there
     is no "publish whatever string the caller wants" path. This is
     the layer that catches typos before they become silent failures
     (a misspelled channel triggers nothing on the listener side, no
     error logged, just a click that did nothing).

  2. **Payload size cap.** Postgres NOTIFY truncates at 8000 bytes
     after channel + overhead. We cap at 4000 bytes after JSON-
     serialisation so a future scope-id growth or extra metadata key
     has headroom before hitting the wire ceiling. Larger payloads
     surface a `PayloadTooLargeError` at publish time; the right
     remedy is to slim the payload, not raise the cap.

Three families of subscriber:

  * gateway-core (every replica) — counter reset, cache invalidate,
    log-level change, maintenance toggle.
  * gateway-control (every replica) — scheduler pause/resume,
    maintenance toggle, log-level change.
  * gateway-control (single scheduler-holder replica) — scheduler
    trigger.

The publisher doesn't care which family will pick up — it just
writes to the channel. The subscriber-side LISTEN registration is
where the dispatch lands.

For tests, `publish_system_notify` accepts any object that exposes a
SQLAlchemy-style `.execute(stmt, params)` interface, so the
PG-specific bits can be mocked in pure-Python unit tests. The live
endpoint passes a real `Session`; tests pass a fake one and assert
the SQL shape.
"""

import json
from typing import Any, Dict, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from app.core.logging_system import BrutorLogger

logger = BrutorLogger(__name__)


# ---------------------------------------------------------------------------
# Channel registry — wire-format constants
# ---------------------------------------------------------------------------
#
# RENAMING ANY OF THESE BREAKS THE WIRE between control-plane publishers
# and gateway-core / gateway-control subscribers. Treat as a wire-protocol
# change: bump both sides at the same release.

CHANNEL_COUNTERS_RESET = "brutor:counters:reset"
CHANNEL_CACHE_INVALIDATE = "brutor:cache:invalidate"
CHANNEL_SCHEDULER_TRIGGER = "brutor:scheduler:trigger"
CHANNEL_SCHEDULER_PAUSE = "brutor:scheduler:pause"
CHANNEL_SCHEDULER_RESUME = "brutor:scheduler:resume"
CHANNEL_SESSION_REVOKE = "brutor:session:revoke"
CHANNEL_LOG_LEVEL = "brutor:log_level"
CHANNEL_MAINTENANCE = "brutor:maintenance"

# Cache stats fan-out. The control plane publishes a
# REQUEST with a fresh correlation_id; every gateway-core replica
# snapshots its in-process cache hit/miss/entry counters and publishes
# a REPLY carrying that same correlation_id + its replica_id. The
# control plane LISTENs on the reply channel for a short window
# (default 500ms) and aggregates per-layer across replicas.
#
# Why two channels and not one bidi: Postgres LISTEN/NOTIFY has no
# concept of reply-to. Using a dedicated reply channel keeps the
# subscriber sets disjoint (gateway-core listens only on REQUEST, the
# control plane listens only on REPLY) so we don't have to filter
# messages by direction on either side.
CHANNEL_CACHE_STATS_REQUEST = "brutor:cache:stats:request"
CHANNEL_CACHE_STATS_REPLY = "brutor:cache:stats:reply"


# Postgres NOTIFY message body has an 8000-byte wire cap; reserving
# half of that as headroom keeps a future scope-id or per-tenant
# expansion comfortable without bumping the cap. Anything beyond
# 4000 bytes after JSON-serialisation should be redesigned as a row
# write that the subscriber polls, not a NOTIFY payload.
PAYLOAD_BYTE_LIMIT = 4000


# ---------------------------------------------------------------------------
# Payload models — one per channel, declared inline so the registry can
# auto-validate incoming dicts. Each model is the strict shape the
# subscriber side parses.
# ---------------------------------------------------------------------------


class _StrictPayload(BaseModel):
    """Common config: extras forbidden so a typo in a key name fails
    fast rather than reaching the subscriber as a silently-ignored
    field."""

    model_config = ConfigDict(extra="forbid")


class CountersResetPayload(_StrictPayload):
    """`brutor:counters:reset` — wipe rate-limit / quota counters for
    a scoped subject in every gateway-core replica's in-memory map +
    the matching Redis-backed quota key."""

    scope: str = Field(
        description="What the counter is keyed on. See "
        "`api/v1/models/admin/system_ops.py::CounterScope`.",
    )
    id: str = Field(min_length=1, max_length=255)
    limit_types: Optional[list[str]] = Field(
        default=None,
        description="Subset of buckets to drop. NULL = all buckets for the scope.",
    )
    tenant_id: Optional[str] = Field(
        default=None,
        max_length=255,
        description=(
            "Tenant the action targets. Subscribers filter by this so a "
            "cross-tenant sysadmin reset stays scoped on the receiving side."
        ),
    )


class CacheInvalidatePayload(_StrictPayload):
    """`brutor:cache:invalidate` — extends the existing cluster-cache
    propagation channel with the 5-layer cache dispatch. Subscribers dispatch by `layer`."""

    layer: str = Field(
        description="One of 'semantic', 'policy', 'group', 'tool', 'response'.",
    )
    scope: str = Field(
        description="Scope kind paired with the layer.",
    )
    id: str = Field(min_length=1, max_length=255)
    tenant_id: Optional[str] = Field(default=None, max_length=255)


class SchedulerTriggerPayload(_StrictPayload):
    """`brutor:scheduler:trigger` — wake a scheduler that's currently
    sleeping. The receiving control-plane replica runs the
    scheduler's next iteration immediately."""

    scheduler_name: str = Field(min_length=1, max_length=64)
    requested_by: str = Field(
        min_length=1,
        max_length=255,
        description="Admin id who pressed Trigger.",
    )


class SchedulerPausePayload(_StrictPayload):
    """`brutor:scheduler:pause` — every control-plane replica re-reads
    `scheduler_runtime_state` on receipt so the next tick honours the
    new flag without waiting for its own DB poll."""

    scheduler_name: str = Field(min_length=1, max_length=64)


# `brutor:scheduler:resume` reuses the same payload shape as pause —
# the channel name is what disambiguates intent on the subscriber side.
SchedulerResumePayload = SchedulerPausePayload


class SessionRevokePayload(_StrictPayload):
    """`brutor:session:revoke` — both gateway-control and gateway-core
    drop the session from their respective caches; the next request
    that carries the session token 401s."""

    session_id: str = Field(min_length=1, max_length=255)
    session_type: str = Field(
        description="'admin', 'portal', or 'mcp' — matches the receiver's "
        "session-store partition.",
    )
    tenant_id: Optional[str] = Field(default=None, max_length=255)


class LogLevelPayload(_StrictPayload):
    """`brutor:log_level` — runtime log-level reload for a specific
    module on a specific service. The channel
    is registered so the publisher API is stable from day one."""

    service: str = Field(
        description="'gateway-core', 'gateway-control', 'kb-uploader', etc.",
    )
    module: str = Field(
        description="Module path (Rust: tracing target; Python: logger name).",
    )
    level: str = Field(
        description="'trace', 'debug', 'info', 'warn', 'error'.",
    )
    scope: str = Field(
        default="all",
        description="'all' = every replica, 'self' = only the receiving replica.",
    )


class MaintenancePayload(_StrictPayload):
    """`brutor:maintenance` — invalidate the cached
    `tenants.maintenance_mode` flag on every replica so the request
    middleware reads the new value at next request entry."""

    tenant_id: Optional[str] = Field(
        default=None,
        description="NULL = global maintenance flag change.",
    )
    mode: str = Field(description="'off', 'read_only', 'drain', or 'paused'.")
    drain_grace_secs: Optional[int] = Field(default=None, ge=0, le=3600)


class CacheLayerStats(_StrictPayload):
    """Per-layer counters embedded in a CacheStatsReplyPayload.

    `hits` / `misses` are monotonically-incrementing process counters
    since the replica booted (or since the matching counter wrapped at
    2^64 — 584 years at 1B ops/sec, so not a real concern). The
    control plane treats these as raw cumulative values; the UI
    renders the delta against the previous poll or the raw cumulative,
    whichever the operator picks.
    """

    hits: int = Field(ge=0)
    misses: int = Field(ge=0)
    entries: int = Field(
        ge=0,
        description="Live entry count from moka's entry_count() — what's "
        "resident in the cache right now.",
    )


class CacheStatsRequestPayload(_StrictPayload):
    """`brutor:cache:stats:request` — operator opened the Caches page.

    The control plane stamps a fresh correlation_id and LISTENs on
    `brutor:cache:stats:reply`; each gateway-core replica fans out a
    reply tagged with the same correlation_id so concurrent operator
    requests don't cross-contaminate.
    """

    correlation_id: str = Field(
        min_length=8,
        max_length=64,
        description="Opaque token round-tripped from request to reply.",
    )


class CacheStatsReplyPayload(_StrictPayload):
    """`brutor:cache:stats:reply` — gateway-core replica answers.

    Carries that replica's per-layer counter snapshot. The control
    plane aggregates across all replies that arrive within the
    collection window (default 500ms). Replicas that don't respond
    in time are tallied as 'partial=true' on the response — better
    than failing the whole request when one replica is stuck.
    """

    correlation_id: str = Field(min_length=8, max_length=64)
    # Absent in the reply of a gateway that is one instance (the Community
    # gateway): it has nothing to name. A gateway of several
    # replicas (the Enterprise Clustering extension) names each.
    replica_id: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=128,
        description=(
            "The replying replica (BRUTOR_REPLICA_ID, else the hostname). "
            "Absent when the gateway is a single instance."
        ),
    )
    layers: Dict[str, CacheLayerStats] = Field(
        description="layer name (policy/group/tool/response) → counters.",
    )


# The single source of truth: channel name → payload model. Anything
# not in this dict can't be published.
_CHANNEL_REGISTRY: Dict[str, type[BaseModel]] = {
    CHANNEL_COUNTERS_RESET: CountersResetPayload,
    CHANNEL_CACHE_INVALIDATE: CacheInvalidatePayload,
    CHANNEL_SCHEDULER_TRIGGER: SchedulerTriggerPayload,
    CHANNEL_SCHEDULER_PAUSE: SchedulerPausePayload,
    CHANNEL_SCHEDULER_RESUME: SchedulerResumePayload,
    CHANNEL_SESSION_REVOKE: SessionRevokePayload,
    CHANNEL_LOG_LEVEL: LogLevelPayload,
    CHANNEL_MAINTENANCE: MaintenancePayload,
    CHANNEL_CACHE_STATS_REQUEST: CacheStatsRequestPayload,
    CHANNEL_CACHE_STATS_REPLY: CacheStatsReplyPayload,
}


def registered_channels() -> tuple[str, ...]:
    """Snapshot of the channel-registry keys — read by tests and by
    the admin-UI dropdown that lets a sysadmin pick a channel to dry-
    run NOTIFY traffic against."""
    return tuple(_CHANNEL_REGISTRY.keys())


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class NotifyError(Exception):
    """Base class for NOTIFY publisher failures."""


class UnknownChannelError(NotifyError):
    """Raised when `publish_system_notify` receives a channel string
    that isn't in `_CHANNEL_REGISTRY`. Catches the most common author
    error: a misspelled channel name that would silently no-op on the
    subscriber side."""


class PayloadValidationError(NotifyError):
    """Raised when the supplied payload doesn't match the channel's
    declared model. Surfaces the underlying Pydantic validation error
    so the endpoint test catches the bad shape with a clear message."""


class PayloadTooLargeError(NotifyError):
    """Raised when the serialised payload exceeds `PAYLOAD_BYTE_LIMIT`.
    Hitting this means the payload was designed wrong: NOTIFY is for
    keys, not bulk data. The subscriber should re-read from the DB
    using the carried id."""


# ---------------------------------------------------------------------------
# Session protocol — keeps the helper testable without SQLAlchemy types
# ---------------------------------------------------------------------------


class _SupportsExecute(Protocol):
    """Anything with SQLAlchemy's `.execute(clause, params)` signature.
    Real callers pass a `Session`; tests pass a recording mock."""

    def execute(self, clause: Any, params: Any = ...) -> Any: ...


# ---------------------------------------------------------------------------
# The publisher
# ---------------------------------------------------------------------------


def publish_system_notify(
    db: _SupportsExecute,
    channel: str,
    payload: Dict[str, Any],
) -> str:
    """Validate and publish one NOTIFY message.

    Args:
        db: A SQLAlchemy Session (or any object with the same
            `.execute(stmt, params)` shape — used by tests).
        channel: Must be one of the `CHANNEL_*` constants. Free-text
            channel strings are rejected — see UnknownChannelError.
        payload: Dict that validates against the channel's declared
            Pydantic model. Pass values straight from the endpoint's
            request body; the model rejects unknown keys.

    Returns:
        The exact JSON-serialised payload string that landed on the
        wire. Useful for endpoint tests that want to assert "this is
        what every replica received".

    Raises:
        UnknownChannelError: `channel` not in _CHANNEL_REGISTRY.
        PayloadValidationError: `payload` doesn't fit the channel's
            declared shape.
        PayloadTooLargeError: serialised payload > PAYLOAD_BYTE_LIMIT.
    """
    if channel not in _CHANNEL_REGISTRY:
        raise UnknownChannelError(
            f"NOTIFY channel {channel!r} is not in the registry. "
            f"Known channels: {sorted(_CHANNEL_REGISTRY.keys())}. "
            "If you're adding a new sub-section, register its channel "
            "in app/services/system_ops/notify.py before publishing."
        )

    model_cls = _CHANNEL_REGISTRY[channel]
    try:
        validated = model_cls.model_validate(payload)
    except Exception as e:
        raise PayloadValidationError(
            f"Payload for {channel!r} failed validation: {e}"
        ) from e

    # Deterministic JSON — sorted keys, no whitespace, no NaNs. The
    # subscriber side parses with `serde_json::from_str` (Rust) or
    # `json.loads` (Python); both tolerate whitespace, but consistency
    # makes test assertions stable across Python versions.
    payload_json = json.dumps(
        validated.model_dump(exclude_none=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )

    if len(payload_json.encode("utf-8")) > PAYLOAD_BYTE_LIMIT:
        raise PayloadTooLargeError(
            f"NOTIFY payload for {channel!r} is "
            f"{len(payload_json.encode('utf-8'))} bytes — over the "
            f"{PAYLOAD_BYTE_LIMIT}-byte cap. Trim the payload and have "
            "the subscriber re-read details from the DB by id."
        )

    # `pg_notify(text, text)` is the SQL function form — both args are
    # parameterised so a channel name that ever contains a colon-rich
    # string can't be confused with literal SQL. (All current channels
    # are static, but the helper still uses bind params for forward-
    # compatibility.)
    db.execute(
        text("SELECT pg_notify(:channel, :payload)"),
        {"channel": channel, "payload": payload_json},
    )

    logger.debug(
        "system NOTIFY published",
        context={
            "channel": channel,
            "payload_bytes": len(payload_json.encode("utf-8")),
        },
    )
    return payload_json
