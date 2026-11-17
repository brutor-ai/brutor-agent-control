# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The startup registry (`app/composition/startup.py`): what the control plane
does before it serves requests and on the way out, as ordered lists of named
steps. The lifespan in `app/app.py` runs the lists and holds no step itself.

Why it matters: the lists are the seam another distribution (the Enterprise
Edition) extends. A step written into the lifespan instead would be invisible to it. And
the order is behaviour: the database before anything that reads it, the
"ready" line before the background schedulers.

Every collaborator of a step is replaced here, so nothing touches a database
or Redis and no scheduler loop is started.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import List
from unittest import mock

import pytest

from app.composition import startup
from app.composition.features import DEPLOYMENT, ENTERPRISE_FEATURES, TRAFFIC_IMPORT
from app.composition.startup import SHUTDOWN, STARTUP, LifecycleStep, StartupContext, run_steps
from tests.app_factory import edition_under_test

APP_DIR = Path(__file__).resolve().parent.parent / "app"


# ---------------------------------------------------------------------------
# The lists.
# ---------------------------------------------------------------------------


# The Enterprise Edition's steps, which its extension adds after the built-in
# ones. They used to be entries of the built-in list: `k8s_readiness` after `secret_preflight`,
# `usage_import_scheduler` after `assurance_digest_scheduler`.
ENTERPRISE_STEPS = [
    ("k8s_readiness", DEPLOYMENT),
    ("usage_import_scheduler", TRAFFIC_IMPORT),
]
ENTERPRISE_CALLS = ["check_k8s_deployment_readiness", "start_usage_import_scheduler"]
ENTERPRISE = edition_under_test() == "enterprise"


def test_startup_steps_are_these_in_this_order():
    # Recorded from the lifespan as it was when the steps were written inline.
    # Changing the order is a behaviour change; make it on purpose.
    assert [(step.name, step.feature) for step in STARTUP] == [
        ("database", None),
        ("secret_preflight", None),
        ("ready", None),
        ("audit_export_scheduler", None),
        ("ai_system_response_scheduler", None),
        ("assurance_scheduler", None),
        ("assurance_checks_scheduler", None),
        ("compliance_scheduler", None),
        ("assurance_digest_scheduler", None),
        ("data_retention_scheduler", None),
        ("llm_retirement_warner", None),
        ("skill_source_sync_scheduler", None),
        ("cache_invalidation_publisher", None),
    ]


def test_the_composition_under_test_has_the_steps_of_its_edition():
    from tests.app_factory import composition

    extra = [(step.name, step.feature) for step in composition().startup[len(STARTUP) :]]
    assert extra == (ENTERPRISE_STEPS if ENTERPRISE else [])


def test_shutdown_steps_are_these_in_this_order():
    assert [(step.name, step.feature) for step in SHUTDOWN] == [("cache_invalidation_publisher", None)]


def test_step_names_are_unique_and_features_are_known():
    for steps in (STARTUP, SHUTDOWN):
        names = [step.name for step in steps]
        assert len(names) == len(set(names))
        assert {step.feature for step in steps} <= ENTERPRISE_FEATURES | {None}


# ---------------------------------------------------------------------------
# Running a list.
# ---------------------------------------------------------------------------


def _context(**overrides) -> StartupContext:
    values = dict(app=SimpleNamespace(state=SimpleNamespace()), services=SimpleNamespace(), logger=mock.MagicMock())
    values.update(overrides)
    return StartupContext(**values)  # type: ignore[arg-type]


def test_run_steps_runs_plain_and_async_steps_in_order_with_the_context():
    seen = []

    def plain(context):
        seen.append(("plain", context))

    async def coroutine(context):
        await asyncio.sleep(0)
        seen.append(("async", context))

    context = _context()
    asyncio.run(run_steps(context, (LifecycleStep("a", plain), LifecycleStep("b", coroutine), LifecycleStep("c", plain))))

    assert seen == [("plain", context), ("async", context), ("plain", context)]


def test_a_step_that_raises_stops_the_run():
    seen = []

    def boom(context):
        raise RuntimeError("no database")

    with pytest.raises(RuntimeError, match="no database"):
        asyncio.run(run_steps(_context(), (LifecycleStep("a", boom), LifecycleStep("b", lambda c: seen.append("b")))))

    assert seen == []


# ---------------------------------------------------------------------------
# The real application: its lifespan runs the lists, and nothing else.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_app():
    from tests.app_factory import create_app

    return create_app()


async def _run_lifespan(app, inside=None):
    async with app.router.lifespan_context(app):
        if inside is not None:
            inside()


def test_the_redis_client_is_kept_when_the_probe_fails():
    """A transient startup race (control plane up before Redis) must not
    disable push invalidation until restart: the client is kept and each
    publish reconnects."""
    client = SimpleNamespace()

    async def failing_probe(_client):
        return "timeout"

    context = _context()
    with mock.patch("app.services.cache_invalidation.create_invalidation_publisher", return_value=client), mock.patch(
        "app.services.cache_invalidation.probe_invalidation_publisher", failing_probe
    ), mock.patch.dict("os.environ", {"REDIS_URL": "redis://unused:1/0"}):
        asyncio.run(startup._connect_cache_invalidation_publisher(context))

    assert context.app.state.redis is client
    context.logger.warning.assert_called_once()


# ---------------------------------------------------------------------------
# The lifespan holds no step of its own.
# ---------------------------------------------------------------------------


def _lifespan_calls(source: str) -> List[str]:
    """Names called inside `lifespan`, as written (`a.b.c`)."""
    tree = ast.parse(source)
    lifespan = next(
        node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan"
    )
    return sorted({ast.unparse(node.func) for node in ast.walk(lifespan) if isinstance(node, ast.Call)})


def test_the_lifespan_only_runs_the_two_lists():
    source = (APP_DIR / "app.py").read_text(encoding="utf-8")
    assert _lifespan_calls(source) == ["StartupContext", "logger.error", "logger.info", "run_steps", "str"]
    lifespan_source = source[source.index("async def lifespan") : source.index("# 3. Create app with lifespan")]
    # The lists are the built-in ones composed with the extensions given to
    # create_app; with none they are STARTUP and SHUTDOWN.
    assert "run_steps(context, composition.startup)" in lifespan_source
    assert "run_steps(context, composition.shutdown)" in lifespan_source
    assert source.count("composition = compose(extensions)") == 1
    # A step added to the lifespan would need an import there.
    assert "import " not in lifespan_source


def test_the_guard_above_sees_a_step_written_into_the_lifespan():
    source = (
        "async def lifespan(app):\n"
        "    await run_steps(context, STARTUP)\n"
        "    from app.services.x import start_x\n"
        "    start_x()\n"
        "    yield\n"
    )
    assert _lifespan_calls(source) == ["run_steps", "start_x"]
