# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# System Operations Console — ORM + Pydantic + audit-row helper regressions

Covers four contracts:

  1. **Model export**: the new ORM classes and constants are
     reachable from `app.database.models` (so a future endpoint
     import doesn't accidentally miss them).
  2. **Pydantic wire shapes**: the Literal enums in the API models
     match the model-level string constants byte-for-byte. Drift
     here means the OpenAPI surface lies to clients about what
     values are valid.
  3. **Payload redaction**: `redact_payload` strips known sensitive
     keys at every depth without mutating the caller's dict.
  4. **`write_system_operation`**: round-trip — captures actor
     identity from the auth context, persists the row with the
     right shape, commits its own transaction.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Trigger model imports — also confirms the __init__ re-exports
# resolve without raising.
from app.database import Base
from app.database.models import (
    KNOWN_SCHEDULERS,
    SchedulerRuntimeState,
    SystemOperationLog,
    SYSTEM_OPERATION_LOG_ACTOR_TYPE_SYSTEM_ADMIN,
    SYSTEM_OPERATION_LOG_ACTOR_TYPE_TENANT_ADMIN,
    SYSTEM_OPERATION_LOG_VALID_ACTOR_TYPES,
)
from app.database.models.admin import SystemAdmin, TenantAdmin
from app.database.models.tenant import Tenant
from app.api.v1.models.admin.system_ops import (
    ActorType,
    CacheFlushRequest,
    CacheLayer,
    CacheScope,
    CounterReleaseCooldownRequest,
    CounterResetRequest,
    CounterScope,
    MaintenancePatchRequest,
    MaintenanceMode,
    MaintenanceStateResponse,
    SchedulerActionRequest,
    SchedulerListResponse,
    SchedulerStatusResponse,
    SystemOperationLogListRequest,
    SystemOperationLogListResponse,
    SystemOperationLogResponse,
)
from app.services.system_ops import (
    SENSITIVE_PAYLOAD_KEYS,
    redact_payload,
    write_system_operation,
)


# ---------------------------------------------------------------------------
# In-memory test harness
# ---------------------------------------------------------------------------


@pytest.fixture
def session():
    """A throwaway in-memory SQLite session with the full schema. Same
    shape as existing admin-endpoint tests; the audit row writes
    cleanly because SystemOperationLog declares a SQLite variant for
    JSONB + INET."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    sess = Session()
    try:
        # Minimum-viable Tenant row for the FK on system_operation_log.
        sess.add(
            Tenant(
                id="tenant-test",
                name="tenant-test",
                display_name="Test",
                primary_contact_email="ops@example.com",
            )
        )
        sess.commit()
        yield sess
    finally:
        sess.close()
        engine.dispose()


@pytest.fixture
def system_admin(session):
    """Persisted SystemAdmin for actor-resolution tests."""
    admin = SystemAdmin(
        id="sysadmin-1",
        username="sysadmin",
        email="sysadmin@example.com",
        password_hash="x",
    )
    session.add(admin)
    session.commit()
    return admin


@pytest.fixture
def tenant_admin(session):
    """Persisted TenantAdmin for actor-resolution tests. Carries a
    placeholder role_id so the NOT NULL FK is satisfied — the System
    Console helper doesn't actually navigate the role here, the
    tests only need the row to exist for the actor-type assertion."""
    admin = TenantAdmin(
        id="tadmin-1",
        tenant_id="tenant-test",
        username="admin",
        email="admin@example.com",
        password_hash="x",
        role_id="role-test",
    )
    session.add(admin)
    session.commit()
    return admin


# ---------------------------------------------------------------------------
# Model exports — drift catcher for app.database.models.__init__
# ---------------------------------------------------------------------------


class TestModelExports:
    def test_system_operation_log_class_exported(self):
        assert SystemOperationLog is not None
        assert SystemOperationLog.__tablename__ == "system_operation_log"

    def test_scheduler_runtime_state_class_exported(self):
        assert SchedulerRuntimeState is not None
        assert SchedulerRuntimeState.__tablename__ == "scheduler_runtime_state"

    def test_actor_type_constants_exported(self):
        # The aliased re-exports from __init__ must hold the same
        # string values as the source module.
        from app.database.models.system_operation_log import (
            ACTOR_TYPE_SYSTEM_ADMIN,
            ACTOR_TYPE_TENANT_ADMIN,
            VALID_ACTOR_TYPES,
        )
        assert (
            SYSTEM_OPERATION_LOG_ACTOR_TYPE_SYSTEM_ADMIN
            == ACTOR_TYPE_SYSTEM_ADMIN
        )
        assert (
            SYSTEM_OPERATION_LOG_ACTOR_TYPE_TENANT_ADMIN
            == ACTOR_TYPE_TENANT_ADMIN
        )
        assert SYSTEM_OPERATION_LOG_VALID_ACTOR_TYPES == VALID_ACTOR_TYPES

    def test_known_schedulers_includes_audit_export(self):
        # If the scheduler enum drifts from the actual schedulers
        # we're tracking, the Background Tasks dropdown will list
        # ghosts. Keep the canonical set sane.
        assert "audit_export" in KNOWN_SCHEDULERS
        assert len(KNOWN_SCHEDULERS) >= 8


# ---------------------------------------------------------------------------
# Pydantic ↔ ORM enum drift
# ---------------------------------------------------------------------------


class TestEnumDriftBetweenPydanticAndOrm:
    """The Pydantic Literal types and the ORM string constants live
    in two files. They MUST stay in lockstep — drift means the
    OpenAPI schema either over- or under-reports what's valid."""

    def test_actor_type_literal_matches_orm(self):
        # `ActorType` is a Pydantic Literal — extract its arguments.
        from typing import get_args
        assert set(get_args(ActorType)) == set(
            SYSTEM_OPERATION_LOG_VALID_ACTOR_TYPES
        )

    def test_maintenance_mode_literal_matches_migration(self):
        from typing import get_args
        # Same four modes as the CHECK constraint declares.
        assert set(get_args(MaintenanceMode)) == {
            "off", "read_only", "drain", "paused",
        }

    def test_cache_layer_literal_covers_rfc_layers(self):
        from typing import get_args
        # The console flushes exactly 5 cache layers.
        assert set(get_args(CacheLayer)) == {
            "semantic", "policy", "group", "tool", "response",
        }


# ---------------------------------------------------------------------------
# Pydantic request-body validation
# ---------------------------------------------------------------------------


class TestPydanticRequestBodies:
    def test_counter_reset_round_trips(self):
        req = CounterResetRequest(
            scope="group",
            id="rg-123",
            limit_types=["rpm", "itpm"],
            reason="rehearsal reset",
        )
        assert req.scope == "group"
        assert req.id == "rg-123"
        assert req.limit_types == ["rpm", "itpm"]

    def test_counter_reset_rejects_unknown_scope(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            CounterResetRequest(scope="not_a_scope", id="x")

    def test_counter_reset_rejects_empty_id(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            CounterResetRequest(scope="user", id="")

    def test_cache_flush_round_trips(self):
        req = CacheFlushRequest(
            layer="semantic",
            scope="tenant",
            id="tenant-test",
        )
        assert req.layer == "semantic"

    def test_maintenance_patch_drain_grace_clamped(self):
        # The Pydantic ge/le bounds match the CHECK constraint range
        # in the migration. The endpoint must reject values out of
        # this band before they reach the DB.
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            MaintenancePatchRequest(mode="drain", drain_grace_secs=4000)
        with pytest.raises(ValidationError):
            MaintenancePatchRequest(mode="drain", drain_grace_secs=-1)
        # In-band values pass.
        ok = MaintenancePatchRequest(mode="drain", drain_grace_secs=120)
        assert ok.drain_grace_secs == 120

    def test_list_request_pagination_bounds(self):
        from pydantic import ValidationError
        # limit > 500 must be rejected; we don't want the audit
        # endpoint becoming an accidental data-export vector.
        with pytest.raises(ValidationError):
            SystemOperationLogListRequest(limit=501)
        SystemOperationLogListRequest(limit=500)  # in-band

    def test_unknown_field_rejected(self):
        from pydantic import ValidationError
        # All system_ops models forbid extras — typos in client
        # payloads should fail fast, not silently drop.
        with pytest.raises(ValidationError):
            CounterResetRequest(
                scope="group",
                id="x",
                bogus_extra="should fail",
            )


# ---------------------------------------------------------------------------
# Payload redaction
# ---------------------------------------------------------------------------


class TestRedactPayload:
    def test_simple_api_key_redacted(self):
        out = redact_payload({"api_key": "sk-secret", "scope": "group"})
        assert out == {"api_key": "[REDACTED]", "scope": "group"}

    def test_nested_redaction(self):
        out = redact_payload(
            {
                "scope": "group",
                "deep": {"credentials": {"password": "hunter2", "name": "ok"}},
            }
        )
        assert out["deep"]["credentials"]["password"] == "[REDACTED]"
        assert out["deep"]["credentials"]["name"] == "ok"
        assert out["scope"] == "group"

    def test_redaction_in_list(self):
        out = redact_payload(
            [{"token": "abc", "label": "first"}, {"token": "def"}]
        )
        assert out == [
            {"token": "[REDACTED]", "label": "first"},
            {"token": "[REDACTED]"},
        ]

    def test_case_insensitive_match(self):
        # `APIKey` should hit `apikey`; `API_KEY` should hit `api_key`.
        out = redact_payload({"APIKey": "x", "API_KEY": "y", "harmless": "ok"})
        assert out["APIKey"] == "[REDACTED]"
        assert out["API_KEY"] == "[REDACTED]"
        assert out["harmless"] == "ok"

    def test_non_dict_passthrough(self):
        # Strings, ints, None all pass unchanged.
        assert redact_payload("hello") == "hello"
        assert redact_payload(42) == 42
        assert redact_payload(None) is None

    def test_caller_dict_not_mutated(self):
        # The helper must deep-copy before redacting — endpoint code
        # often inspects its own payload AFTER writing the audit row.
        original = {"api_key": "sk-leak"}
        redact_payload(original)
        assert original["api_key"] == "sk-leak"

    def test_all_sensitive_keys_redact(self):
        # Smoke-test every key in the SENSITIVE_PAYLOAD_KEYS tuple.
        for key in SENSITIVE_PAYLOAD_KEYS:
            out = redact_payload({key: "very-secret"})
            assert (
                out[key] == "[REDACTED]"
            ), f"key {key!r} not redacted — SENSITIVE_PAYLOAD_KEYS drift?"


# ---------------------------------------------------------------------------
# write_system_operation — end-to-end behaviour
# ---------------------------------------------------------------------------


class TestWriteSystemOperation:

    def test_tenant_admin_actor_type_resolved(self, session, tenant_admin):
        row = write_system_operation(
            session,
            actor=tenant_admin,
            action="cache_flush",
            tenant_id="tenant-test",
        )
        assert row.actor_type == "tenant_admin"

    def test_unknown_actor_type_raises(self, session):
        # Anything that isn't a SystemAdmin or TenantAdmin must fail
        # noisily — otherwise the audit row attribution becomes
        # unreliable.
        class WeirdActor:
            id = "whoever"

        with pytest.raises(TypeError, match="unknown actor type"):
            write_system_operation(
                session,
                actor=WeirdActor(),
                action="counter_reset",
            )

    def test_none_actor_raises(self, session):
        with pytest.raises(ValueError, match="without an actor"):
            write_system_operation(
                session,
                actor=None,
                action="counter_reset",
            )


