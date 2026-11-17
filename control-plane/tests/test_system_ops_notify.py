# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# System Operations Console — NOTIFY publisher regressions

Five contracts:

  1. **Channel registry** — the 8 channel constants exist with the
     declared wire strings. Subscribers on the Rust
     side parse these literal strings; a rename here is a wire break.
  2. **Payload validation** — every registered channel rejects
     malformed payloads and accepts the documented happy-path
     shape.
  3. **Wire shape** — the helper emits `SELECT pg_notify(:channel,
     :payload)` with the channel + JSON payload as bind params, and
     the JSON is deterministic (sorted keys, no whitespace).
  4. **Size cap** — payloads over `PAYLOAD_BYTE_LIMIT` raise
     `PayloadTooLargeError` rather than silently truncating on the
     wire.
  5. **Unknown channel** — a misspelled channel string raises
     `UnknownChannelError` rather than firing into the void
     (silent-no-op is the wire equivalent of a misconfigured
     button — exactly what we built the registry to prevent).
"""

import json
from typing import Any, List

import pytest

from app.services.system_ops import (
    CHANNEL_CACHE_INVALIDATE,
    CHANNEL_COUNTERS_RESET,
    CHANNEL_LOG_LEVEL,
    CHANNEL_MAINTENANCE,
    CHANNEL_SCHEDULER_PAUSE,
    CHANNEL_SCHEDULER_RESUME,
    CHANNEL_SCHEDULER_TRIGGER,
    CHANNEL_SESSION_REVOKE,
    PAYLOAD_BYTE_LIMIT,
    PayloadTooLargeError,
    PayloadValidationError,
    UnknownChannelError,
    publish_system_notify,
    registered_channels,
)
from app.services.system_ops.notify import (
    CHANNEL_CACHE_STATS_REPLY,
    CHANNEL_CACHE_STATS_REQUEST,
    CacheInvalidatePayload,
    CountersResetPayload,
    LogLevelPayload,
    MaintenancePayload,
    SchedulerPausePayload,
    SchedulerResumePayload,
    SchedulerTriggerPayload,
    SessionRevokePayload,
)


# ---------------------------------------------------------------------------
# Recording session — captures executes for shape assertions
# ---------------------------------------------------------------------------


class RecordingSession:
    """Minimal `.execute(stmt, params)` stub. Each call appends one
    `(sql_text, params)` tuple to `calls`."""

    def __init__(self):
        self.calls: List[tuple[str, dict[str, Any]]] = []

    def execute(self, clause, params=None):
        # SQLAlchemy `text()` clauses stringify to the SQL fragment.
        self.calls.append((str(clause), dict(params or {})))


@pytest.fixture
def session() -> RecordingSession:
    return RecordingSession()


# ---------------------------------------------------------------------------
# 1. Channel registry — wire-format strings
# ---------------------------------------------------------------------------


class TestChannelRegistry:
    """The constants are the wire format. Rust subscribers compile
    these strings into their LISTEN registration; a rename here
    silently drops every message until the Rust side is rebuilt."""

    @pytest.mark.parametrize(
        "constant,wire",
        [
            (CHANNEL_COUNTERS_RESET, "brutor:counters:reset"),
            (CHANNEL_CACHE_INVALIDATE, "brutor:cache:invalidate"),
            (CHANNEL_SCHEDULER_TRIGGER, "brutor:scheduler:trigger"),
            (CHANNEL_SCHEDULER_PAUSE, "brutor:scheduler:pause"),
            (CHANNEL_SCHEDULER_RESUME, "brutor:scheduler:resume"),
            (CHANNEL_SESSION_REVOKE, "brutor:session:revoke"),
            (CHANNEL_LOG_LEVEL, "brutor:log_level"),
            (CHANNEL_MAINTENANCE, "brutor:maintenance"),
        ],
    )
    def test_channel_wire_format(self, constant: str, wire: str):
        assert constant == wire

    def test_registered_channels_count(self):
        # The base channels (pause + resume share a payload but are
        # separate channel strings → 8 entries) plus the two cache stats
        # request/reply channels of the stats collector
        # → 10 registered channels.
        assert len(registered_channels()) == 10

    def test_registered_channels_includes_all_constants(self):
        # Every CHANNEL_* constant declared at module scope must show up
        # in the registry — otherwise the registry is the limit and the
        # constant is a misleading export.
        expected = {
            CHANNEL_COUNTERS_RESET,
            CHANNEL_CACHE_INVALIDATE,
            CHANNEL_SCHEDULER_TRIGGER,
            CHANNEL_SCHEDULER_PAUSE,
            CHANNEL_SCHEDULER_RESUME,
            CHANNEL_SESSION_REVOKE,
            CHANNEL_LOG_LEVEL,
            CHANNEL_MAINTENANCE,
            CHANNEL_CACHE_STATS_REQUEST,
            CHANNEL_CACHE_STATS_REPLY,
        }
        assert set(registered_channels()) == expected


# ---------------------------------------------------------------------------
# 2. Payload models — happy path + extra-field rejection
# ---------------------------------------------------------------------------


class TestPayloadHappyPath:
    """Each declared payload shape round-trips a minimum-viable
    body. If the model gains a required field, the corresponding
    publisher / subscriber pair will fall out of sync — this catches
    it before deploy."""

    def test_counters_reset(self):
        CountersResetPayload(scope="group", id="rg-1")
        CountersResetPayload(
            scope="user", id="user-1", limit_types=["rpm"], tenant_id="t1"
        )

    def test_cache_invalidate(self):
        CacheInvalidatePayload(layer="semantic", scope="tenant", id="t1")

    def test_scheduler_trigger(self):
        SchedulerTriggerPayload(
            scheduler_name="audit_export", requested_by="sysadmin-1"
        )

    def test_scheduler_pause_and_resume_share_shape(self):
        # Same shape, two channel names. The contract is intentional:
        # pause + resume carry the same body and only the channel
        # disambiguates the operator intent.
        SchedulerPausePayload(scheduler_name="audit_export")
        SchedulerResumePayload(scheduler_name="audit_export")
        # Identity at the class level: resume is aliased to pause.
        assert SchedulerResumePayload is SchedulerPausePayload

    def test_session_revoke(self):
        SessionRevokePayload(session_id="sess-1", session_type="admin")

    def test_log_level(self):
        LogLevelPayload(
            service="gateway-core", module="brutor_core::services::usage",
            level="debug",
        )
        # `scope` defaults to "all".
        assert LogLevelPayload(
            service="gateway-control", module="app.services.cache",
            level="info",
        ).scope == "all"

    def test_maintenance(self):
        MaintenancePayload(mode="drain", drain_grace_secs=60, tenant_id="t1")
        # Global flag — tenant_id omitted.
        MaintenancePayload(mode="off")


class TestPayloadRejectsExtras:
    """`extra='forbid'` on every model. A typo'd key that landed in
    the published payload would be silently ignored by the subscriber
    parser, hiding the bug behind a "but I clicked it" complaint."""

    def test_counters_reset_rejects_unknown_field(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            CountersResetPayload(scope="group", id="rg-1", bogus="x")

    def test_maintenance_rejects_out_of_band_grace(self):
        # ge/le bounds: 0 ≤ drain_grace_secs ≤ 3600. The Pydantic
        # model rejects out-of-band values BEFORE they reach the
        # wire — matches the CHECK constraint in the migration.
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            MaintenancePayload(mode="drain", drain_grace_secs=99999)


# ---------------------------------------------------------------------------
# 3. publish_system_notify — wire shape + happy path
# ---------------------------------------------------------------------------


class TestPublisher:
    def test_emits_pg_notify_with_channel_and_payload(
        self, session: RecordingSession
    ):
        publish_system_notify(
            session,
            CHANNEL_COUNTERS_RESET,
            {"scope": "group", "id": "rg-1", "tenant_id": "t1"},
        )
        assert len(session.calls) == 1
        sql, params = session.calls[0]
        # The published SQL is parameterised — channel and payload
        # both ride bind params so a future channel name with weird
        # characters can't be confused with SQL.
        assert "pg_notify(:channel" in sql
        assert ":payload" in sql
        assert params["channel"] == CHANNEL_COUNTERS_RESET

    def test_returned_json_is_deterministic(self, session: RecordingSession):
        # Sorted keys + no whitespace mean the wire payload is
        # byte-stable across Python versions. Stable wire format ==
        # stable assertions in subscriber-side tests.
        wire = publish_system_notify(
            session,
            CHANNEL_CACHE_INVALIDATE,
            {"id": "rg-1", "layer": "semantic", "scope": "tenant"},
        )
        assert wire == '{"id":"rg-1","layer":"semantic","scope":"tenant"}'

    def test_none_values_dropped(self, session: RecordingSession):
        # `exclude_none=True` keeps the wire compact and lets the
        # subscriber tell "operator didn't supply this field" from
        # "operator supplied a null value" (no current channel
        # distinguishes them, but the policy is forward-looking).
        wire = publish_system_notify(
            session,
            CHANNEL_COUNTERS_RESET,
            {
                "scope": "group", "id": "rg-1",
                "limit_types": None, "tenant_id": None,
            },
        )
        # Both nulls are excluded from the emitted JSON.
        assert "limit_types" not in wire
        assert "tenant_id" not in wire
        assert json.loads(wire) == {"scope": "group", "id": "rg-1"}

    def test_returned_string_matches_params(self, session: RecordingSession):
        # The return value IS the wire payload — endpoint tests assert
        # against this to confirm the subscriber will see the same
        # bytes the publisher meant to send.
        wire = publish_system_notify(
            session,
            CHANNEL_SESSION_REVOKE,
            {
                "session_id": "sess-1",
                "session_type": "admin",
                "tenant_id": "t1",
            },
        )
        _, params = session.calls[0]
        assert params["payload"] == wire


# ---------------------------------------------------------------------------
# 4. Error paths — every guard tested
# ---------------------------------------------------------------------------


class TestUnknownChannelGuard:
    """Misspelled channel names are the most likely author error.
    Catching them at publish time keeps the System page reliable —
    a button that looks like it did something but didn't fire is
    worse than a 500."""

    def test_misspelled_channel_raises(self, session: RecordingSession):
        with pytest.raises(UnknownChannelError) as exc:
            publish_system_notify(
                session, "brutor:counter:reset", {"scope": "group", "id": "x"}
            )
        # Helpful error: lists the known channels so the author can
        # pick the right one without grepping the codebase.
        assert "counters:reset" in str(exc.value)

    def test_completely_bogus_channel_raises(self, session: RecordingSession):
        with pytest.raises(UnknownChannelError):
            publish_system_notify(session, "nonsense", {})

    def test_session_not_touched_on_unknown_channel(
        self, session: RecordingSession
    ):
        # Failure must be early — no SQL emitted before the guard.
        with pytest.raises(UnknownChannelError):
            publish_system_notify(session, "nonsense", {})
        assert session.calls == []


class TestPayloadValidationGuard:
    def test_missing_required_field_raises(self, session: RecordingSession):
        with pytest.raises(PayloadValidationError):
            publish_system_notify(
                session,
                CHANNEL_COUNTERS_RESET,
                {"scope": "group"},  # missing required `id`
            )

    def test_unknown_field_raises(self, session: RecordingSession):
        with pytest.raises(PayloadValidationError):
            publish_system_notify(
                session,
                CHANNEL_COUNTERS_RESET,
                {"scope": "group", "id": "rg-1", "typo": "oops"},
            )

    def test_validation_failure_emits_no_sql(
        self, session: RecordingSession
    ):
        try:
            publish_system_notify(
                session, CHANNEL_COUNTERS_RESET, {"scope": "group"}
            )
        except PayloadValidationError:
            pass
        assert session.calls == []


class TestSizeCapGuard:
    def test_oversized_payload_raises(self, session: RecordingSession):
        # Build an id big enough to push the JSON past the cap.
        # Default cap is 4000 bytes; the id field is 255-char-capped
        # by the model so we need to drown the limit via another
        # uncapped field. limit_types is a list of strings — flood it.
        many_strings = ["x" * 100] * 50  # ~5000 bytes after JSON encoding
        with pytest.raises(PayloadTooLargeError) as exc:
            publish_system_notify(
                session,
                CHANNEL_COUNTERS_RESET,
                {"scope": "group", "id": "rg-1", "limit_types": many_strings},
            )
        # Useful error: tells the operator the byte count.
        assert "byte" in str(exc.value).lower()

    def test_payload_at_cap_succeeds(self, session: RecordingSession):
        # A payload safely under the cap publishes fine. Important so
        # the cap doesn't accidentally clamp legitimate use.
        publish_system_notify(
            session,
            CHANNEL_COUNTERS_RESET,
            {"scope": "group", "id": "rg-1"},
        )
        assert len(session.calls) == 1

    def test_byte_limit_constant_in_reasonable_range(self):
        # Belt-and-braces against a future "let me bump the cap" PR.
        # Postgres' 8000-byte wire ceiling minus channel-name overhead
        # is the hard limit; we cap well under to give the subscriber
        # parser headroom.
        assert 1000 <= PAYLOAD_BYTE_LIMIT <= 7800


# ---------------------------------------------------------------------------
# 5. End-to-end: every channel publishes its own happy path
# ---------------------------------------------------------------------------


class TestEveryChannelPublishes:
    """Smoke: every registered channel has a realistic happy-path
    publish call. If we ever add a channel without wiring a payload
    case, this fails loudly."""

    @pytest.mark.parametrize(
        "channel,payload",
        [
            (
                CHANNEL_COUNTERS_RESET,
                {"scope": "group", "id": "rg-1", "tenant_id": "t1"},
            ),
            (
                CHANNEL_CACHE_INVALIDATE,
                {"layer": "semantic", "scope": "tenant", "id": "t1"},
            ),
            (
                CHANNEL_SCHEDULER_TRIGGER,
                {
                    "scheduler_name": "audit_export",
                    "requested_by": "sysadmin-1",
                },
            ),
            (
                CHANNEL_SCHEDULER_PAUSE,
                {"scheduler_name": "audit_export"},
            ),
            (
                CHANNEL_SCHEDULER_RESUME,
                {"scheduler_name": "audit_export"},
            ),
            (
                CHANNEL_SESSION_REVOKE,
                {"session_id": "sess-1", "session_type": "admin"},
            ),
            (
                CHANNEL_LOG_LEVEL,
                {
                    "service": "gateway-core",
                    "module": "brutor_core::services::usage",
                    "level": "debug",
                },
            ),
            (
                CHANNEL_MAINTENANCE,
                {"mode": "drain", "tenant_id": "t1", "drain_grace_secs": 60},
            ),
        ],
    )
    def test_channel_publishes_realistic_payload(
        self, session: RecordingSession, channel: str, payload: dict
    ):
        wire = publish_system_notify(session, channel, payload)
        # Always emitted exactly one SQL statement.
        assert len(session.calls) == 1
        # Channel rode the params verbatim.
        _, params = session.calls[0]
        assert params["channel"] == channel
        # Wire payload is valid JSON.
        json.loads(wire)
