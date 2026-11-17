# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Regression test for the cross-process row-lock deadlock between
`LLModelsManager.test_connection` and the Rust proxy's
`/admin/{id}/reset-resilience` endpoint.

Background
----------
`test_connection` writes the model's health status (`UPDATE
llm_model SET health_status='healthy', ...`) and then makes an HTTP
POST to the Rust proxy's reset-resilience endpoint. The proxy's
endpoint runs its own `UPDATE llm_model SET health_status=...` on
the same row.

If the control plane only `flush()`es (does not commit) before the
HTTP call, its open transaction holds a row-level lock on
`llm_model.id`. The proxy's UPDATE blocks waiting for the lock; the
control plane's transaction can't commit because it's awaiting the
HTTP reply. The deadlock resolves only when the 8 s read-timeout
fires, after which the cross-process exception is swallowed as
"non-fatal" — but every test_connection has paid an 8 s tax.

This was the cause of the live e2e suite slowdown: 24 successful
test_connection calls × 8 s timeout = ~3 minutes of pure waiting
on a suite that should run in under a minute.

The fix is to `db.commit()` (not just flush) immediately after the
health-status UPDATE and before the HTTP call. This regression test
pins the ordering: commit MUST happen before `_reset_proxy_resilience`
is awaited. If a future refactor re-introduces the bug (e.g. removes
the commit, or moves the HTTP call earlier), this test fails.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest


@pytest.mark.asyncio
async def test_commit_runs_before_reset_proxy_resilience():
    """`db.commit()` must execute strictly before the HTTP POST to the
    proxy's reset endpoint. Recording the call order on a shared list
    is the simplest way to express the invariant."""
    from app.services.llm.model.llm_models_manager import LLModelsManager
    from app.constants.server import ServerStatusType

    call_order: list[str] = []

    # Mock the DB session — record commit/flush ordering.
    db = MagicMock()
    db.commit.side_effect = lambda: call_order.append("commit")
    db.flush.side_effect = lambda: call_order.append("flush")

    # Mock the LLM model. update_health_status only mutates attributes,
    # so a MagicMock with the relevant fields suffices.
    model = MagicMock()
    model.id = "llm-deadlock-regression-test"
    model.tenant_id = "default"
    model.model_name = "test-model"
    model.is_routing = False
    model.capabilities = {}
    model.provider = MagicMock(key="openai")
    model.update_health_status = MagicMock(
        side_effect=lambda **kwargs: call_order.append("update_health_status")
    )

    manager = LLModelsManager(logger=MagicMock(), encryption_service=MagicMock())

    # Stub the upstream probe — return success+upstream_called=True so
    # the gate at line 962 lets us reach `_reset_proxy_resilience`.
    manager._test_connection_via_proxy = AsyncMock(
        return_value={
            "success": True,
            "response_time_ms": 100.0,
            "upstream_called": True,
        }
    )

    # Stub the reset call — record that it ran AFTER commit.
    async def _fake_reset(_model: Any) -> None:
        call_order.append("reset_proxy_resilience")

    manager._reset_proxy_resilience = AsyncMock(side_effect=_fake_reset)

    result = await manager.test_connection(model, db)

    assert result["status"] == ServerStatusType.HEALTHY.value, (
        f"expected healthy result, got {result}"
    )

    # The DB write must be committed BEFORE the HTTP call.
    assert "commit" in call_order, (
        f"db.commit() was not called — the row lock will be held across "
        f"the HTTP round-trip and deadlock against the proxy. "
        f"call_order={call_order}"
    )
    assert "reset_proxy_resilience" in call_order, (
        f"_reset_proxy_resilience was never called even though the probe "
        f"succeeded with upstream_called=True. call_order={call_order}"
    )

    commit_idx = call_order.index("commit")
    reset_idx = call_order.index("reset_proxy_resilience")
    assert commit_idx < reset_idx, (
        f"db.commit() must run STRICTLY BEFORE _reset_proxy_resilience to "
        f"release the row lock and avoid the deadlock. "
        f"call_order={call_order}"
    )


@pytest.mark.asyncio
async def test_failed_probe_skips_reset_and_does_not_commit_early():
    """When the upstream probe fails, the reset call is skipped (no
    HTTP round-trip needed) so we don't need the early commit either —
    confirms the fix is gated on the success branch and doesn't leak
    transaction semantics on the failure path."""
    from app.services.llm.model.llm_models_manager import LLModelsManager

    call_order: list[str] = []

    db = MagicMock()
    db.commit.side_effect = lambda: call_order.append("commit")
    db.flush.side_effect = lambda: call_order.append("flush")

    model = MagicMock()
    model.id = "llm-failed-probe"
    model.tenant_id = "default"
    model.model_name = "test-model"
    model.is_routing = False
    model.capabilities = {}
    model.provider = MagicMock(key="openai")
    model.update_health_status = MagicMock(
        side_effect=lambda **kwargs: call_order.append("update_health_status")
    )

    manager = LLModelsManager(logger=MagicMock(), encryption_service=MagicMock())
    manager._test_connection_via_proxy = AsyncMock(
        return_value={
            "success": False,
            "response_time_ms": None,
            "error": "Upstream 401 unauthorized",
            "upstream_called": True,
        }
    )

    async def _fake_reset(_model: Any) -> None:
        call_order.append("reset_proxy_resilience")

    manager._reset_proxy_resilience = AsyncMock(side_effect=_fake_reset)

    await manager.test_connection(model, db)

    assert "reset_proxy_resilience" not in call_order, (
        f"reset must be skipped on failure (no breaker to clear). "
        f"call_order={call_order}"
    )


@pytest.mark.asyncio
async def test_upstream_not_called_skips_reset():
    """Synthetic-skip probes (rerank/moderation/routing modes,
    AssemblyAI's async pipeline, etc.) set upstream_called=False —
    the health-check gate should skip the reset entirely so bulk health
    checks aren't penalised by an unnecessary HTTP round-trip."""
    from app.services.llm.model.llm_models_manager import LLModelsManager

    call_order: list[str] = []

    db = MagicMock()
    db.commit.side_effect = lambda: call_order.append("commit")
    db.flush.side_effect = lambda: call_order.append("flush")

    model = MagicMock()
    model.id = "llm-synthetic-probe"
    model.tenant_id = "default"
    model.model_name = "test-model"
    model.is_routing = False
    model.capabilities = {}
    model.provider = MagicMock(key="anthropic")
    model.update_health_status = MagicMock(
        side_effect=lambda **kwargs: call_order.append("update_health_status")
    )

    manager = LLModelsManager(logger=MagicMock(), encryption_service=MagicMock())
    manager._test_connection_via_proxy = AsyncMock(
        return_value={
            "success": True,
            "response_time_ms": 12.0,
            "upstream_called": False,
        }
    )

    async def _fake_reset(_model: Any) -> None:
        call_order.append("reset_proxy_resilience")

    manager._reset_proxy_resilience = AsyncMock(side_effect=_fake_reset)

    await manager.test_connection(model, db)

    assert "reset_proxy_resilience" not in call_order, (
        f"reset must be skipped when upstream_called=False — there is "
        f"no resilience state to clear. call_order={call_order}"
    )
