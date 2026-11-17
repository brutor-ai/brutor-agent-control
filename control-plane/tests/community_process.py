# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The Community composition of this build, for tests.

The Community composition is the built-in one: the `create_app` of `app/app.py` with no
extension, and a process that never registered the
Enterprise extension is a Community Edition with nothing to take out. Being
Community is "do not compose the extension".

A test run of the Enterprise edition has registered it, though
(`tests/conftest.py`), and the registries a request reads are process-wide.
So for a test that holds the two compositions side by side, the entries
tagged with an Enterprise Edition feature are taken out of every process-wide
registry (and of the built-in lists, which hold none since L3):

* `community_composition()`: for a `with` block; everything is put back on
  exit.
* `become_community_process()`: for good, for a subprocess that is to be a
  Community Edition (`tests/test_edition_install_pg.py`).

This module imports nothing of the test suite, on purpose: `tests/conftest.py`
points `DATABASE_URL` at nothing, and a subprocess that installs into a
scratch database must keep the URL it was given.
"""

from __future__ import annotations

from contextlib import contextmanager

_LISTS = (
    ("app.composition.routers", "ROUTERS"),
    ("app.composition.startup", "STARTUP"),
    ("app.composition.startup", "SHUTDOWN"),
    ("app.composition.extensions", "ROUTERS"),
    ("app.composition.extensions", "STARTUP"),
    ("app.composition.extensions", "SHUTDOWN"),
)


def _community_only(items):
    return [item for item in items if getattr(item, "feature", None) is None]


_DELETE = object()


def keep_enterprise_out(registry) -> None:
    """While a registry is held as the Community composition, an Enterprise
    application built in the same process (another test's fixture, a module
    imported late) composes its extension again and would put the tagged
    entries back: `compose` registers whatever is missing. Until `let_enterprise_in`,
    the registry takes untagged entries only. (A real process is one edition
    from start to end; only a test holds two side by side.)"""
    original = type(registry).register

    def register(items):
        return original(registry, _community_only(list(items)))

    registry.register = register


def let_enterprise_in(registry) -> None:
    registry.__dict__.pop("register", None)


def become_community_process() -> list:
    """Take every Enterprise-tagged entry out of the composition. Returns what
    is needed to put them back (`_restore`)."""
    import importlib

    from app.composition.extensions import PROVISIONING, SEEDING
    from app.composition.hooks import HOOKS
    from app.composition.identity import IDENTITY_PROTOCOLS
    from app.composition.permissions import PERMISSIONS
    from app.composition.principals import PRINCIPALS

    saved = []
    for module_name, attr in _LISTS:
        module = importlib.import_module(module_name)
        saved.append((module, attr, getattr(module, attr)))
        setattr(module, attr, tuple(_community_only(getattr(module, attr))))
    for registry in (SEEDING, PROVISIONING):
        saved.append((registry, "_steps", registry._steps))
        registry._steps = _community_only(registry._steps)
        saved.append((registry, "register", _DELETE))
        keep_enterprise_out(registry)
    for registry in (PERMISSIONS, PRINCIPALS, HOOKS, IDENTITY_PROTOCOLS):
        saved.append((registry, "register", _DELETE))
        keep_enterprise_out(registry)
        saved.append((registry, "_specs", registry._specs))
        registry._specs = {
            name: spec for name, spec in registry._specs.items() if spec.feature is None
        }
    return saved


def _restore(saved: list) -> None:
    for owner, attr, value in reversed(saved):
        if value is _DELETE:
            let_enterprise_in(owner)
        else:
            setattr(owner, attr, value)


@contextmanager
def community_composition():
    saved = become_community_process()
    try:
        yield
    finally:
        _restore(saved)


@contextmanager
def fresh_registries():
    """The process-wide registries as a fresh process has them (the built-in
    entries only), open to an extension; put back afterwards. For a test that
    composes the Enterprise extension itself, whatever the edition under
    test: without this, a Community run would be an Enterprise process from
    that test on. Unlike `community_composition()`, which also keeps the
    Enterprise extension from registering again."""
    from app.composition.extensions import PROVISIONING, SEEDING
    from app.composition.hooks import HOOKS
    from app.composition.identity import IDENTITY_PROTOCOLS
    from app.composition.permissions import PERMISSIONS
    from app.composition.principals import PRINCIPALS

    saved = []
    for registry, attr in (
        (SEEDING, "_steps"),
        (PROVISIONING, "_steps"),
        (PERMISSIONS, "_specs"),
        (PRINCIPALS, "_specs"),
        (HOOKS, "_specs"),
        (IDENTITY_PROTOCOLS, "_specs"),
    ):
        held = getattr(registry, attr)
        saved.append((registry, attr, held))
        # A process that became a Community Edition for good keeps tagged
        # entries out of `register` (`keep_enterprise_out`); "open to an
        # extension" means that is lifted here too, and put back afterwards.
        if "register" in registry.__dict__:
            saved.append((registry, "register", registry.__dict__["register"]))
            let_enterprise_in(registry)
        if attr == "_steps":
            setattr(registry, attr, [step for step in held if step.feature is None])
        else:
            setattr(registry, attr, {name: spec for name, spec in held.items() if spec.feature is None})
    try:
        yield
    finally:
        for registry, attr, held in reversed(saved):
            setattr(registry, attr, held)
