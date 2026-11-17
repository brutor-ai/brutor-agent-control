# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The extension interface: `create_app(extensions=[...])`.

An extension written outside the `app` package contributes a router, startup
and shutdown steps, an install-seed step, a tenant-provisioning step and a
permission; each takes effect where the built-in registries are read. With no
extension the application is the built-in registries and nothing else. A
contribution that would collide with what exists is refused before anything
is mounted or registered, and the refusal leaves every registry as it was.
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.composition.extensions import EXTENSION_INTERFACE_VERSION
from fastapi import APIRouter, Depends
from fastapi.routing import iter_route_contexts

import app.composition.provisioning as provisioning_module
import app.composition.seeding as seeding_module
import app.database as database_module
from app.composition.extensions import PROVISIONING, SEEDING, Extension, StepRegistry, compose
from app.composition.permissions import PERMISSIONS, PermissionSpec
from app.composition.provisioning import TENANT_PROVISIONING, ProvisioningStep
from app.composition.routers import ROUTERS, RouterSpec
from app.composition.seeding import INSTALL_SEED, SeedStep
from app.composition.startup import SHUTDOWN, STARTUP, LifecycleStep
from app.constants.tenant import TenantDefaultRole
from app.core.admin_user.dependencies import require_permission
from app.core.logging_system import BrutorLogger
from app.services.tenant.tenant_provisioning import TenantProvisioner
from tests.app_factory import create_app, edition_under_test
from tests.test_admin_rbac_endpoints import harness  # noqa: F401  (fixture)
from tests.source_trees import python_sources, source_label

APP_DIR = Path(__file__).resolve().parents[1] / "app"
READ_ONLY = TenantDefaultRole.READ_ONLY_USER.value

# ---------------------------------------------------------------------------
# A test-only extension, defined here and nowhere in `app/`.
# ---------------------------------------------------------------------------

CALLS: list = []


def _reports_router(services) -> APIRouter:
    router = APIRouter(prefix="/ext", tags=["ext"])

    @router.get("/reports")
    def reports(_: object = Depends(require_permission("ext:report:read"))):
        return {"reports": []}

    return router


def _start(context) -> None:
    CALLS.append(("start", context.app))
    context.app.state.ext_started = True


def _stop(context) -> None:
    CALLS.append(("stop", context.app))


def _seed(ctx) -> None:
    CALLS.append(("seed", ctx.db))


def _provision(ctx) -> None:
    CALLS.append(("provision", ctx.tenant.id))


REPORTS = Extension(
    name="reports", interface_version=EXTENSION_INTERFACE_VERSION,
    routers=(RouterSpec("ext_reports", _reports_router),),
    startup=(LifecycleStep("ext_reports_start", _start),),
    shutdown=(LifecycleStep("ext_reports_stop", _stop),),
    install_seed=(SeedStep("ext_reports_seed", _seed),),
    tenant_provisioning=(ProvisioningStep("ext_reports_provision", _provision),),
    permissions=(
        PermissionSpec("ext:report:read", "Read reports", default_roles=(READ_ONLY,), read_only=True),
        PermissionSpec("ext:report:write", "Write reports"),
    ),
)


@pytest.fixture(autouse=True)
def restored_registries(monkeypatch):
    """The three process-wide registries, put back after each test."""
    monkeypatch.setattr(SEEDING, "_steps", list(SEEDING._steps))
    monkeypatch.setattr(PROVISIONING, "_steps", list(PROVISIONING._steps))
    monkeypatch.setattr(PERMISSIONS, "_specs", dict(PERMISSIONS._specs))
    CALLS.clear()
    yield


def _names(items) -> list:
    return [item.name for item in items]


def _route_paths(app) -> list:
    """Every route's path in mount order, included routers flattened."""
    return [c.path or c.original_route.path for c in iter_route_contexts(app.router.routes)]


# ---------------------------------------------------------------------------
# With no extensions the application is the built-in registries
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# An extension takes effect everywhere its kind of contribution is read
# ---------------------------------------------------------------------------


def test_an_extensions_startup_runs_after_and_its_shutdown_before_the_builtin_steps():
    composition = compose([REPORTS])
    assert _names(composition.startup) == _names(STARTUP) + ["ext_reports_start"]
    assert composition.startup[-1].run is _start
    assert _names(composition.shutdown) == ["ext_reports_stop"] + _names(SHUTDOWN)
    assert composition.shutdown[0].run is _stop


def test_two_extensions_keep_their_order_on_startup_and_reverse_it_on_shutdown():
    second = Extension(
        name="second", interface_version=EXTENSION_INTERFACE_VERSION,
        startup=(LifecycleStep("second_a", _start), LifecycleStep("second_b", _start)),
        shutdown=(LifecycleStep("second_stop_a", _stop), LifecycleStep("second_stop_b", _stop)),
    )
    composition = compose([REPORTS, second])
    assert _names(composition.startup)[-3:] == ["ext_reports_start", "second_a", "second_b"]
    assert _names(composition.shutdown)[:3] == ["second_stop_b", "second_stop_a", "ext_reports_stop"]
    assert composition.extensions == ("reports", "second")


def test_the_same_extension_composed_into_a_second_application_registers_once():
    first = create_app(extensions=[REPORTS])
    second = create_app(extensions=[REPORTS])
    assert _route_paths(first) == _route_paths(second)
    assert SEEDING.names().count("ext_reports_seed") == 1
    assert PROVISIONING.names().count("ext_reports_provision") == 1
    assert PERMISSIONS.values().count("ext:report:read") == 1


# ---------------------------------------------------------------------------
# Refusals: all or none, before anything is mounted or registered
# ---------------------------------------------------------------------------


def _snapshot():
    return (SEEDING.steps(), PROVISIONING.steps(), tuple(PERMISSIONS.specs()))


@pytest.mark.parametrize(
    "extensions, message",
    [
        ([REPORTS, Extension(name="reports", interface_version=EXTENSION_INTERFACE_VERSION)], "names are not unique"),
        ([Extension(name="", interface_version=EXTENSION_INTERFACE_VERSION)], "name is empty or padded"),
        ([Extension(name=" reports", interface_version=EXTENSION_INTERFACE_VERSION)], "name is empty or padded"),
        ([Extension(name="x", interface_version=EXTENSION_INTERFACE_VERSION, routers=(RouterSpec("tenant_settings", _reports_router),))], "routers: name already taken: tenant_settings"),
        ([Extension(name="x", interface_version=EXTENSION_INTERFACE_VERSION, routers=(RouterSpec("", _reports_router),))], "routers: name is empty"),
        ([Extension(name="x", interface_version=EXTENSION_INTERFACE_VERSION, startup=(LifecycleStep("database", _start),))], "startup: name already taken: database"),
        ([Extension(name="x", interface_version=EXTENSION_INTERFACE_VERSION, shutdown=(LifecycleStep("cache_invalidation_publisher", _stop),))], "shutdown: name already taken"),
        ([Extension(name="x", interface_version=EXTENSION_INTERFACE_VERSION, install_seed=(SeedStep("default_tenant", _seed),))], "install seed: a different step is already registered as default_tenant"),
        ([Extension(name="x", interface_version=EXTENSION_INTERFACE_VERSION, install_seed=(SeedStep("a", _seed), SeedStep("a", _seed)))], "install seed: step registered twice in one call: a"),
        ([Extension(name="x", interface_version=EXTENSION_INTERFACE_VERSION, tenant_provisioning=(ProvisioningStep("roles", _provision),))], "tenant provisioning: a different step is already registered as roles"),
        ([Extension(name="x", interface_version=EXTENSION_INTERFACE_VERSION, permissions=(PermissionSpec("mcp:server:read", "other words"),))], "permissions: a different permission is already registered as mcp:server:read"),
        ([Extension(name="x", interface_version=EXTENSION_INTERFACE_VERSION, permissions=(PermissionSpec("ext:a:read", feature="not_a_feature"),))], "unknown feature"),
        ([Extension(name="x", interface_version=EXTENSION_INTERFACE_VERSION, permissions=(PermissionSpec("ext:a:read", default_roles=("Nobody",)),))], "not a default role"),
        ([REPORTS, Extension(name="y", interface_version=EXTENSION_INTERFACE_VERSION, routers=(RouterSpec("ext_reports", _reports_router),))], "routers: name already taken: ext_reports"),
        ([REPORTS, Extension(name="y", interface_version=EXTENSION_INTERFACE_VERSION, startup=(LifecycleStep("ext_reports_start", _start),))], "startup: name already taken: ext_reports_start"),
    ],
)
def test_a_colliding_or_malformed_extension_is_refused_and_nothing_is_registered(extensions, message):
    before = _snapshot()
    with pytest.raises(ValueError, match=message):
        compose(extensions)
    assert _snapshot() == before
    # the first extension of a refused pair registered nothing either
    assert "ext_reports_seed" not in SEEDING.names()
    assert not PERMISSIONS.is_registered("ext:report:read")


def test_a_refused_extension_leaves_no_route_and_no_application():
    before = _snapshot()
    with pytest.raises(ValueError, match="routers: name already taken: tenant_settings"):
        create_app(extensions=[Extension(name="x", interface_version=EXTENSION_INTERFACE_VERSION, routers=(RouterSpec("tenant_settings", _reports_router),))])
    assert _snapshot() == before


@pytest.mark.parametrize(
    "later",
    [
        dict(tenant_provisioning=(ProvisioningStep("roles", _provision),)),
        dict(permissions=(PermissionSpec("mcp:server:read", "other words"),)),
        dict(permissions=(PermissionSpec("ext:a:read", default_roles=("Nobody",)),)),
    ],
)
def test_valid_contributions_are_not_registered_when_a_later_one_is_refused(later):
    """The seed step is valid and checked first; what fails comes after it in
    the composition. Nothing of the extension may remain registered."""
    before = _snapshot()
    extension = Extension(name="x", interface_version=EXTENSION_INTERFACE_VERSION, install_seed=(SeedStep("ext_x_seed", _seed),), **later)
    with pytest.raises(ValueError):
        compose([extension])
    assert "ext_x_seed" not in SEEDING.names()
    assert _snapshot() == before
    # and the refusal of a provisioning step leaves a valid permission out too
    with pytest.raises(ValueError):
        compose(
            [
                Extension(
                    name="y", interface_version=EXTENSION_INTERFACE_VERSION,
                    install_seed=(SeedStep("ext_y_seed", _seed),),
                    tenant_provisioning=(ProvisioningStep("ext_y_prov", _provision), ProvisioningStep("roles", _provision)),
                    permissions=(PermissionSpec("ext:y:read"),),
                )
            ]
        )
    assert _snapshot() == before


def test_a_registry_refuses_a_different_step_under_a_taken_name_and_skips_an_equal_one():
    registry = StepRegistry("test", [SeedStep("a", _seed)])
    registry.register([SeedStep("a", _seed)])  # equal: skipped
    assert registry.names() == ["a"]
    with pytest.raises(ValueError, match="a different step is already registered as a"):
        registry.register([SeedStep("b", _seed), SeedStep("a", _provision)])
    assert registry.names() == ["a"]  # all or none: b was not added
    registry.register([SeedStep("b", _seed)])
    assert registry.names() == ["a", "b"]
    with pytest.raises(ValueError, match="built-in step names are not unique"):
        StepRegistry("test", [SeedStep("a", _seed), SeedStep("a", _seed)])


# ---------------------------------------------------------------------------
# Nothing reads the built-in lists past the composition
# ---------------------------------------------------------------------------


def test_nothing_outside_the_composition_package_reads_a_builtin_list_directly():
    """The lists an extension adds to are read through `compose`, `SEEDING`
    and `PROVISIONING`; a reader of `ROUTERS`, `STARTUP`, `SHUTDOWN`,
    `INSTALL_SEED` or `TENANT_PROVISIONING` elsewhere would not see the
    extensions."""
    names = {"ROUTERS", "STARTUP", "SHUTDOWN", "INSTALL_SEED", "TENANT_PROVISIONING"}
    readers = []
    for path in python_sources(APP_DIR):
        if path.parent == APP_DIR / "composition":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app.composition"):
                for alias in node.names:
                    if alias.name in names:
                        readers.append((source_label(path), alias.name))
            if isinstance(node, ast.Attribute) and node.attr in names:
                readers.append((source_label(path), node.attr))
    assert readers == []


# ---------------------------------------------------------------------------
# The extension interface has a version of its own
# ---------------------------------------------------------------------------


def test_the_interface_version_is_one():
    from app.composition.extensions import EXTENSION_INTERFACE_VERSION

    assert EXTENSION_INTERFACE_VERSION == 1


def test_an_extension_must_state_the_interface_version_it_was_written_against():
    with pytest.raises(TypeError, match="interface_version"):
        Extension(name="silent")


@pytest.mark.parametrize("offset", [-1, 1], ids=["one_before", "one_after"])
def test_an_extension_written_against_another_version_is_refused(offset):
    from app.composition.extensions import EXTENSION_INTERFACE_VERSION

    written_against = EXTENSION_INTERFACE_VERSION + offset
    assert written_against in (0, EXTENSION_INTERFACE_VERSION + 1)
    before = (list(ROUTERS), SEEDING.names(), PROVISIONING.names())
    with pytest.raises(ValueError) as refused:
        compose([Extension(name="other-times", interface_version=written_against)])
    message = str(refused.value)
    assert "other-times" in message
    assert f"version {written_against} of the extension interface" in message
    assert f"has version {EXTENSION_INTERFACE_VERSION}" in message
    # ... and nothing of it was registered.
    assert (list(ROUTERS), SEEDING.names(), PROVISIONING.names()) == before


@pytest.mark.parametrize("stated", [None, "1", 1.0, True])
def test_a_version_that_is_not_the_number_is_refused(stated):
    with pytest.raises(ValueError, match="extension interface"):
        compose([Extension(name="vague", interface_version=stated)])


def test_an_extension_written_against_this_version_is_admitted():
    from app.composition.extensions import EXTENSION_INTERFACE_VERSION

    composition = compose([Extension(name="in-step", interface_version=EXTENSION_INTERFACE_VERSION)])
    assert composition.extensions == ("in-step",)
