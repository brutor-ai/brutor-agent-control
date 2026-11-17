# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Extensions of the control plane application.

An extension is a second distribution's contribution to the assembly: routers
to mount, startup and shutdown steps, steps of the install seed and of tenant
provisioning, permissions, principals (kinds of caller of the admin API) and
behaviour hooks (what Community code hands over to, `composition/hooks.py`). `create_app(extensions=[...])` composes the
built-in registries with the extensions given; with none, the application is
what the built-in registries describe and nothing else.

Two kinds of registry meet here. What the application mounts and runs in its
lifespan — routers, startup, shutdown — is composed per application, because
each `create_app` call is its own application. What runs inside a request or
at install — tenant provisioning, the install seed, the permissions a role may
hold, the principals a token may stand for — is read by code that has no
application in hand, so those registries are process-wide (`SEEDING`,
`PROVISIONING`, `PERMISSIONS`, `PRINCIPALS`, `HOOKS`, `IDENTITY_PROTOCOLS`)
and an extension
is registered into them once; registering the same extension again is a
no-op, and a different item under a name already taken is refused.

Every refusal is all-or-none and happens before anything is mounted or
registered: an extension that is half installed would be an assembly nobody
described.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generic, Iterable, List, Optional, Sequence, Tuple, TypeVar

from app.composition.hooks import HOOKS, HookRegistry, HookSpec
from app.composition.identity import (
    IDENTITY_PROTOCOLS,
    IdentityProtocolRegistry,
    IdentityProtocolSpec,
)
from app.composition.permissions import PERMISSIONS, PermissionRegistry, PermissionSpec
from app.composition.principals import PRINCIPALS, PrincipalRegistry, PrincipalSpec
from app.composition.provisioning import TENANT_PROVISIONING, ProvisioningStep
from app.composition.routers import ROUTERS, RouterSpec
from app.composition.seeding import INSTALL_SEED, SeedStep
from app.composition.startup import SHUTDOWN, STARTUP, LifecycleStep
from app.env_registry import REGISTRY as ENV_REGISTRY, EnvVar


#: The version of the extension interface: of `Extension`, of
#: the specs it carries and of what `compose` does with them. It is separate
#: from the product version and moves when an extension written against the
#: interface before would no longer mean the same. An extension states the
#: version it was written against; `compose` refuses any other.
EXTENSION_INTERFACE_VERSION = 1


@dataclass(frozen=True)
class Extension:
    """What one extension contributes. Every field but the first two may be
    empty. `interface_version` is `EXTENSION_INTERFACE_VERSION` as the
    extension's package imports it from the Community package it is built
    with."""

    name: str
    interface_version: int
    routers: Sequence[RouterSpec] = ()
    startup: Sequence[LifecycleStep] = ()
    shutdown: Sequence[LifecycleStep] = ()
    install_seed: Sequence[SeedStep] = ()
    tenant_provisioning: Sequence[ProvisioningStep] = ()
    permissions: Sequence[PermissionSpec] = ()
    principals: Sequence[PrincipalSpec] = ()
    hooks: Sequence[HookSpec] = ()
    identity_protocols: Sequence[IdentityProtocolSpec] = ()
    # The environment variables only this extension reads
    # (`app/env_registry.py`). They join the registry the process reads its
    # environment through.
    env_vars: Sequence[EnvVar] = ()


T = TypeVar("T")


class StepRegistry(Generic[T]):
    """An ordered, process-wide list of named steps that extensions append to.

    `register` adds steps after the ones present. A step equal to the one
    already registered under its name is skipped (the same extension composed
    into a second application); a different step under a taken name is
    refused, and then nothing of the call is registered.
    """

    def __init__(self, kind: str, builtin: Sequence[T]) -> None:
        self._kind = kind
        self._steps: List[T] = list(builtin)
        names = [step.name for step in self._steps]  # type: ignore[attr-defined]
        if len(set(names)) != len(names):
            raise ValueError(f"{kind}: built-in step names are not unique")

    def steps(self) -> Tuple[T, ...]:
        return tuple(self._steps)

    def names(self) -> List[str]:
        return [step.name for step in self._steps]  # type: ignore[attr-defined]

    def register(self, steps: Iterable[T]) -> None:
        steps = list(steps)
        present = {step.name: step for step in self._steps}  # type: ignore[attr-defined]
        new: List[T] = []
        seen_now = set()
        for step in steps:
            name = step.name  # type: ignore[attr-defined]
            if not name or name != name.strip():
                raise ValueError(f"{self._kind}: step name is empty or padded: {name!r}")
            if name in seen_now:
                raise ValueError(f"{self._kind}: step registered twice in one call: {name}")
            seen_now.add(name)
            if name in present:
                if present[name] == step:
                    continue
                raise ValueError(f"{self._kind}: a different step is already registered as {name}")
            new.append(step)
        self._steps.extend(new)


# The process-wide registries. `app/database/__init__.py` runs `SEEDING`;
# `TenantProvisioner` runs `PROVISIONING`; `PERMISSIONS` is in permissions.py.
SEEDING: StepRegistry[SeedStep] = StepRegistry("install seed", INSTALL_SEED)
PROVISIONING: StepRegistry[ProvisioningStep] = StepRegistry("tenant provisioning", TENANT_PROVISIONING)


@dataclass(frozen=True)
class Composition:
    """The lists one application is built from."""

    routers: Tuple[RouterSpec, ...]
    startup: Tuple[LifecycleStep, ...]
    shutdown: Tuple[LifecycleStep, ...]
    extensions: Tuple[str, ...] = field(default=())


def _check_names(kind: str, items: Sequence, taken: Iterable[str]) -> None:
    taken = set(taken)
    for item in items:
        if not item.name or item.name != item.name.strip():
            raise ValueError(f"{kind}: name is empty or padded: {item.name!r}")
        if item.name in taken:
            raise ValueError(f"{kind}: name already taken: {item.name}")
        taken.add(item.name)


def _new_permissions(specs: Sequence[PermissionSpec]) -> List[PermissionSpec]:
    """The permissions of `specs` that are new; an identical one is skipped."""
    new = []
    for spec in specs:
        present = PERMISSIONS.get(spec.value)
        if present is None:
            new.append(spec)
        elif present != spec:
            raise ValueError(f"permissions: a different permission is already registered as {spec.value}")
    return new


def _new_principals(specs: Sequence[PrincipalSpec]) -> List[PrincipalSpec]:
    """The principals of `specs` that are new; an identical one is skipped.

    A different principal under a name already taken is refused: an extension
    does not replace how the system administrator, or anyone, is verified."""
    new = []
    for spec in specs:
        present = PRINCIPALS.get(spec.name) if isinstance(spec.name, str) else None
        if present is None:
            new.append(spec)
        elif present != spec:
            raise ValueError(f"principals: a different principal is already registered as {spec.name}")
    return new


def _new_identity_protocols(specs: Sequence[IdentityProtocolSpec]) -> List[IdentityProtocolSpec]:
    """The identity protocols of `specs` that are new; an identical one is
    skipped. A different protocol under a name already taken is refused: an
    extension does not replace how OIDC, or any protocol, is handled."""
    new = []
    for spec in specs:
        present = IDENTITY_PROTOCOLS.get(spec.name)
        if present is None:
            new.append(spec)
        elif present != spec:
            raise ValueError(f"identity protocols: a different protocol is already registered as {spec.name}")
    return new


def compose(extensions: Optional[Sequence[Extension]] = None) -> Composition:
    """Compose the built-in registries with `extensions`.

    Routers, startup steps, install-seed steps and provisioning steps of the
    extensions come after the built-in ones, extension by extension in the
    order given; shutdown steps come before the built-in ones, in reverse,
    so an extension stops before what it depends on. Permissions join
    `PERMISSIONS`, principals join `PRINCIPALS`, behaviour hooks join `HOOKS`,
    identity protocols join `IDENTITY_PROTOCOLS`, environment variables
    join the registry the process reads its environment through.

    Everything is checked first; the first problem raises and nothing is
    registered. With no extensions the result is the built-in lists.
    """
    extensions = list(extensions or ())
    names = [ext.name for ext in extensions]
    if len(set(names)) != len(names):
        raise ValueError(f"extensions: names are not unique: {names}")
    for ext in extensions:
        if not ext.name or ext.name != ext.name.strip():
            raise ValueError(f"extensions: name is empty or padded: {ext.name!r}")
    for ext in extensions:
        if type(ext.interface_version) is not int or ext.interface_version != EXTENSION_INTERFACE_VERSION:
            raise ValueError(
                f"extension {ext.name}: written against version {ext.interface_version} of the extension "
                f"interface; this control plane has version {EXTENSION_INTERFACE_VERSION}. Use the extension "
                "package that was built for this Community package."
            )

    routers = list(ROUTERS)
    startup = list(STARTUP)
    shutdown: List[LifecycleStep] = []

    # Check everything before touching anything.
    for ext in extensions:
        _check_names("routers", ext.routers, [r.name for r in routers])
        routers.extend(ext.routers)
        _check_names("startup", ext.startup, [s.name for s in startup])
        startup.extend(ext.startup)
        _check_names("shutdown", ext.shutdown, [s.name for s in SHUTDOWN] + [s.name for s in shutdown])
        shutdown = list(reversed(ext.shutdown)) + shutdown
    seed_steps = [step for ext in extensions for step in ext.install_seed]
    provisioning_steps = [step for ext in extensions for step in ext.tenant_provisioning]
    permissions = _new_permissions([spec for ext in extensions for spec in ext.permissions])
    principals = _new_principals([spec for ext in extensions for spec in ext.principals])
    hooks = [spec for ext in extensions for spec in ext.hooks]
    identity_protocols = _new_identity_protocols(
        [spec for ext in extensions for spec in ext.identity_protocols]
    )
    # The process-wide registries check their own names; each is tried on a
    # copy first, so that a refusal in one leaves the others untouched.
    StepRegistry("install seed", SEEDING.steps()).register(seed_steps)
    StepRegistry("tenant provisioning", PROVISIONING.steps()).register(provisioning_steps)
    PermissionRegistry(PERMISSIONS.specs()).register(permissions)
    PrincipalRegistry(PRINCIPALS.specs()).register(principals)
    HookRegistry(HOOKS.specs()).register(hooks)
    IdentityProtocolRegistry(IDENTITY_PROTOCOLS.specs()).register(identity_protocols)

    # The last check: a variable under a name another definition holds.
    ENV_REGISTRY.register([var for ext in extensions for var in ext.env_vars])

    SEEDING.register(seed_steps)
    PROVISIONING.register(provisioning_steps)
    PERMISSIONS.register(permissions)
    PRINCIPALS.register(principals)
    HOOKS.register(hooks)
    IDENTITY_PROTOCOLS.register(identity_protocols)

    return Composition(
        routers=tuple(routers),
        startup=tuple(startup),
        shutdown=tuple(shutdown) + tuple(SHUTDOWN),
        extensions=tuple(names),
    )
