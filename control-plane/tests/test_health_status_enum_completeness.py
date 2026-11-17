# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Regression suite for the `HealthStatus` Pydantic enum on the
admin router endpoints.

Background — the bug this exists to lock in:
    A model row carrying `health_status='degraded'` (a value
    the LLM-models manager actively writes) caused the
    `GET /v1/admin/llm-router/deployments` endpoint to 500
    with `ResponseValidationError: Input should be 'healthy',
    'unhealthy' or 'unknown'`. The enum on the Pydantic side
    had drifted out of sync with the actual values written to
    `llm_model.health_status`. Worse — the failure mode looked
    like a CORS/network error in the browser because it was a
    500 with no response body, mirroring the earlier
    `LlmProvider.__repr__` bug pattern.

What this test does:
    Documents the canonical set of `health_status` values the
    rest of the system writes (admin manager, KServe poller,
    routing-group create flow, seed scripts, Rust proxy
    health probes), and asserts:

      1. Every canonical value is present in the
         `HealthStatus` Pydantic enum.
      2. A `ModelDeployment` Pydantic model serializes cleanly
         for each canonical value — the round-trip the failing
         endpoint actually exercised.

    Adding a new health value to the manager without adding
    it here breaks this test before the next admin who hits
    the deployments endpoint sees a 500.

Run: pytest tests/test_health_status_enum_completeness.py -v
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "x")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest

from app.api.v1.models.admin.llm_routing_group import (
    HealthStatus,
    ModelDeployment,
)


# Source-of-truth set. Mirrors the comment block on
# `app/database/models/llm_model.py:health_status` exactly. If
# either side adds a new value, both must change together.
CANONICAL_HEALTH_VALUES = frozenset({
    "healthy",
    "unhealthy",
    "degraded",
    "requires_tier",
    "unknown",
})


def test_pydantic_enum_covers_every_canonical_value():
    """The Pydantic enum must accept every value the manager
    can write. Missing one means an admin endpoint 500s on a
    real row."""
    enum_values = {member.value for member in HealthStatus}
    missing = CANONICAL_HEALTH_VALUES - enum_values
    assert not missing, (
        f"`HealthStatus` Pydantic enum is missing canonical "
        f"value(s) {sorted(missing)}. The bug this test guards "
        f"against: a `health_status='{next(iter(missing))}'` "
        f"row in `llm_model` would 500 the admin "
        f"`/v1/admin/llm-router/deployments` endpoint with "
        f"`ResponseValidationError`. Add the missing value to "
        f"`HealthStatus` in "
        f"`app/api/v1/models/admin/llm_routing_group.py`."
    )


def test_pydantic_enum_has_no_orphan_values():
    """Catches the inverse drift: enum values the rest of the
    system never writes. Not a hard failure — operators may
    add an enum entry ahead of the writer — but it surfaces
    the gap so the next person can decide. Promote this to a
    hard assert if your team prefers strict equality."""
    enum_values = {member.value for member in HealthStatus}
    extra = enum_values - CANONICAL_HEALTH_VALUES
    if extra:
        pytest.skip(
            f"`HealthStatus` enum carries values not in the "
            f"canonical set: {sorted(extra)}. Verify they're "
            f"actually written by the manager / probe / proxy "
            f"or remove them."
        )


@pytest.mark.parametrize("value", sorted(CANONICAL_HEALTH_VALUES))
def test_model_deployment_serializes_with_each_health_value(value):
    """End-to-end check: the very Pydantic model the failing
    endpoint returns must accept the value without a
    ValidationError. We pass a minimal but valid construction
    — only the fields needed to satisfy required validators —
    so the test stays focused on the enum, not on every other
    field on `ModelDeployment`."""
    deployment = ModelDeployment(
        model_id="m-test",
        model_name="test-model",
        provider_model_name="test-model",
        provider="openai",
        enabled=True,
        health_status=value,
        routing_weight=1,
        total_requests=0,
        error_rate=0.0,
    )
    assert deployment.health_status.value == value
    # Round-trip: model_dump must not raise. This is what
    # `serialize_response` calls inside FastAPI before sending
    # the body — failing here is exactly the original 500.
    dumped = deployment.model_dump()
    assert dumped["health_status"] == value
