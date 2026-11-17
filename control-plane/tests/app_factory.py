# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The application under test, and its edition.

"Community test harnesses take an app factory. Enterprise CI re-runs the
Community suites against the Enterprise composition, then adds its own." This
module is the one place that says which factory a test run uses:

* `edition_under_test()`: `enterprise` or `community`, from the environment
  variable `BRUTOR_TEST_EDITION`. The default is `enterprise`: the full
  composition, which is what this suite has always tested.
* `app_factory()`: the `create_app` of that edition.
* `create_app(...)`: shorthand for `app_factory()(...)`. Test files call this
  and never `app.app.create_app` (`tests/test_app_factory.py` holds that).
* `harness_extras()`: what the edition adds to the bare application of
  `tests/admin_endpoint_harness.py`: routers to mount beside the admin-users
  router, and principals to create. The harness itself imports nothing of
  the Enterprise Edition.
* `community_factory()`: the Community Edition's factory whatever the
  edition under test, for a process that is to *be* a Community Edition (the
  edition install tests).
* `create_community_app(...)`: a Community application built in a process
  that stays what it is, for the tests that hold the two compositions side
  by side.

The composition was turned round in L3: `app.app.create_app` is the Community
Edition, and the Enterprise Edition is `brutor_enterprise.app.create_app`
(`enterprise/brutor_enterprise/`), the same application composed with the
Enterprise extension.

* `become_enterprise_process()`: registers the extension in the process-wide
  registries, as an Enterprise server or command does at start. A test run of
  the Enterprise edition does it once (`tests/conftest.py`), so that code a
  test calls without building an application (a permission check, the install
  seed) sees the Enterprise composition.

`enterprise/` is not installed: `pyproject.toml` puts it on the path for
pytest (`pythonpath`), and `enterprise_path()` does for a subprocess.

This module imports nothing of the application or of the test suite at import
time, on purpose (as `tests/community_process.py`): a subprocess that installs
into a scratch database must keep the `DATABASE_URL` it was given, which
`tests/conftest.py` would replace.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Iterable

ENTERPRISE = "enterprise"
COMMUNITY = "community"
EDITIONS = (ENTERPRISE, COMMUNITY)
EDITION_ENV = "BRUTOR_TEST_EDITION"
ENTERPRISE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "enterprise")

ENTERPRISE_TESTS_DIR = os.path.join(ENTERPRISE_DIR, "tests")

_became_community = False


def edition_under_test() -> str:
    """The edition this test run is for. Unset means the full composition."""
    value = os.environ.get(EDITION_ENV, ENTERPRISE)
    if value not in EDITIONS:
        raise ValueError(f"{EDITION_ENV} must be one of {EDITIONS}, not {value!r}")
    return value


def enterprise_path() -> None:
    """Put `enterprise/` on `sys.path` (pytest does it from `pyproject.toml`;
    a subprocess started by a test does not read that), and `enterprise/tests`
    behind it: the Enterprise suite's helper modules (`enterprise_harness`)
    are plain modules of that directory, which is not a package."""
    import sys

    if ENTERPRISE_DIR not in sys.path:
        sys.path.insert(0, ENTERPRISE_DIR)
    if ENTERPRISE_TESTS_DIR not in sys.path:
        sys.path.append(ENTERPRISE_TESTS_DIR)


def become_enterprise_process() -> None:
    """Register the Enterprise extension in this process, as an Enterprise
    server does when it builds its application. Registering twice is a no-op."""
    enterprise_path()
    from brutor_enterprise.app import register

    register()
    # An Enterprise process has a licence. The run's
    # is minted by the suite: every feature, no limit
    # (`enterprise/tests/licence_harness.py`).
    import licence_harness

    licence_harness.install_session_licence_once()


def _enterprise_factory() -> Callable[..., Any]:
    enterprise_path()
    from brutor_enterprise.app import create_app

    return create_app


def community_factory() -> Callable[..., Any]:
    """The Community Edition's `create_app`. Makes this process a Community
    Edition, for good: the registries a request reads are process-wide, and
    in a process that registered the Enterprise extension its entries are
    taken out (in a process that never did, there is nothing to take out)."""
    global _became_community
    if not _became_community:
        from tests.community_process import become_community_process

        become_community_process()
        _became_community = True
    from app.app import create_app

    return create_app


def create_community_app(*args: Any, **kwargs: Any) -> Any:
    """A Community application, built in a process that stays what it is:
    the Enterprise entries are out of the composition while it is built and
    back afterwards. What a request then reads from the process-wide
    registries is the caller's to arrange (`community_composition()`)."""
    from app.app import create_app
    from tests.community_process import community_composition

    with community_composition():
        return create_app(*args, **kwargs)


def app_factory() -> Callable[..., Any]:
    """`create_app` of the edition under test."""
    if edition_under_test() == COMMUNITY:
        return community_factory()
    return _enterprise_factory()


def create_app(*args: Any, **kwargs: Any) -> Any:
    """The application of the edition under test."""
    return app_factory()(*args, **kwargs)


def every_permission() -> list:
    """Every permission of the edition under test, as enum members (each has
    `.value`): the Community `Permission` enum, and in an Enterprise run the
    Enterprise Edition's `EnterprisePermission` after it. What a test gives an
    administrator that is to hold everything; `for p in Permission` gives the
    Community permissions only (since L3)."""
    from app.core.admin_user.permissions import Permission

    members = list(Permission)
    if edition_under_test() == ENTERPRISE:
        enterprise_path()
        from brutor_enterprise.permissions import EnterprisePermission

        members += list(EnterprisePermission)
    return members


def routers_of_both_editions() -> tuple:
    """Every `RouterSpec` this tree defines: the Community registry's, then
    the Enterprise extension's (whatever the edition under test). For a test
    about which side a router is on."""
    from app.composition.routers import ROUTERS

    return tuple(ROUTERS) + tuple(enterprise_extension().routers)


def enterprise_extension() -> Any:
    """The Enterprise Edition's one `Extension`."""
    enterprise_path()
    from brutor_enterprise.extension import EXTENSION

    return EXTENSION


def composition() -> Any:
    """The `Composition` (routers, start-up and shutdown steps) of the
    edition under test: the built-in lists, composed with the Enterprise
    extension in an Enterprise run."""
    from app.composition.extensions import compose

    if edition_under_test() == COMMUNITY:
        community_factory()
        return compose()
    return compose([enterprise_extension()])


# ---------------------------------------------------------------------------
# The admin endpoint harness
# ---------------------------------------------------------------------------


def _no_routers(logger: Any) -> Iterable[Any]:
    return ()


def _no_principals(auth: Any, db: Any, password: str) -> None:
    return None


@dataclass(frozen=True)
class HarnessExtras:
    """What an edition adds to the harness's bare application.

    `routers(logger)` returns routers mounted (under `/v1`) before the
    admin-users router. `seed_principals(auth, db, password)` creates the
    edition's own administrators in the harness database, before the tenants
    are made; `auth` is the `UnifiedAuthService`, and the session is the
    harness's (it commits).
    """

    routers: Callable[[Any], Iterable[Any]] = _no_routers
    seed_principals: Callable[[Any, Any, str], None] = _no_principals


NO_EXTRAS = HarnessExtras()


def harness_extras() -> HarnessExtras:
    """The harness additions of the edition under test: none for Community;
    the system administrator's login route and account for the full
    composition (`enterprise/tests/enterprise_harness.py`)."""
    if edition_under_test() == COMMUNITY:
        community_factory()  # the harness's process is a Community Edition
        return NO_EXTRAS
    enterprise_path()
    from enterprise_harness import EXTRAS

    return EXTRAS
