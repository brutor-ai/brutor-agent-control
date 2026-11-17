# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The permission registry.

A permission is a string an administrator's role holds and a route requires
(`require_permission`). Which strings exist used to be answered by iterating
the `Permission` enum, in five places: the list a role may be given, the
catalogue the Admin Console shows, what a system administrator is created
with, what the Tenant Admin role holds, and the check of a role being saved.
The enum is closed, so nothing outside this code base could add a permission.

The registry is that answer in one place. It holds every permission as a
`PermissionSpec`: the string, its description, the Enterprise Edition feature
it belongs to (or none, for the Community Edition), and the default tenant
roles that are granted it. `PERMISSIONS` starts with the members of the enum;
more are added with `PERMISSIONS.register(...)`.

The enum stays. It is how this code base names a permission at a route, about
a thousand times, and a member's value is the string, so nothing changes
there. What changes is that the enum is no longer asked what exists.

The enum holds the Community Edition's permissions only. The 42 permissions of the Enterprise Edition features are an enum of its
package (`brutor_enterprise.permissions.EnterprisePermission`) and reach the
registry, tagged, through its extension; `default_roles` is how they reach
the default roles.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from app.composition.features import ENTERPRISE_FEATURES
from app.constants.tenant import READ_ONLY_ROLES, TenantDefaultRole
from app.core.admin_user.permissions import PERMISSION_DESCRIPTIONS, PERMISSION_READ_ONLY, Permission


@dataclass(frozen=True)
class PermissionSpec:
    """One permission.

    `value` is the string a role holds. `description` is what the Admin
    Console shows beside it; a permission without one is shown as
    "Permission: <value>". `feature` is the Enterprise Edition feature the
    permission belongs to, or None for the Community Edition.

    `default_roles` names the default tenant roles (other than Tenant Admin,
    which holds every permission but `system:admin`) that are granted the
    permission when a tenant is provisioned and on every start. It is how a
    permission added from outside reaches a role this code base defines. The
    Community permissions do not use it: their grants are written out, with
    the reasons, in `TenantProvisioner.get_default_tenant_roles`.

    `after` is the value of the permission this one is listed after: the role
    editor shows a permission added from outside beside the permissions of its
    kind, not at the end. None, or a value that is not
    registered, lists it at the end, in registration order.

    `read_only` says whether everything the permission opens leaves the
    configuration and the records as they are. A permission is taken to
    change something unless it says otherwise, and only a read-only
    permission can be granted to a role that only reads (`READ_ONLY_ROLES`).
    """

    value: str
    description: Optional[str] = None
    feature: Optional[str] = None
    default_roles: Tuple[str, ...] = ()
    after: Optional[str] = None
    read_only: bool = False

    @property
    def category(self) -> str:
        """The part of the value before the first colon: `mcp`, `admin`, ..."""
        return self.value.split(":")[0]


class PermissionRegistry:
    """The permissions that exist, in the order they are listed: the order
    they were registered in, with each permission that names one to be listed
    after (`PermissionSpec.after`) standing right behind it."""

    def __init__(self, specs: Iterable[PermissionSpec] = ()) -> None:
        self._specs: Dict[str, PermissionSpec] = {}
        self.register(specs)

    def register(self, specs: Iterable[PermissionSpec]) -> None:
        """Add permissions. All of them, or none.

        Refuses a value that is empty or already registered, a feature that is
        not an Enterprise Edition feature, and a default role that is not one
        of the tenant's default roles or is Tenant Admin (which is granted
        everything already). A permission silently registered twice, or
        granted to a role that does not exist, would be an access decision
        nobody made.
        """
        specs = list(specs)
        roles = {role.value for role in TenantDefaultRole} - {TenantDefaultRole.ADMIN_USER.value}
        seen = set(self._specs)
        for spec in specs:
            if not spec.value or spec.value != spec.value.strip():
                raise ValueError(f"permission value is empty or padded: {spec.value!r}")
            if spec.value in seen:
                raise ValueError(f"permission registered twice: {spec.value}")
            if spec.after == spec.value:
                raise ValueError(f"permission {spec.value}: listed after itself")
            if spec.feature is not None and spec.feature not in ENTERPRISE_FEATURES:
                raise ValueError(f"permission {spec.value}: unknown feature {spec.feature!r}")
            unknown = [role for role in spec.default_roles if role not in roles]
            if unknown:
                raise ValueError(f"permission {spec.value}: not a default role that can be granted: {unknown}")
            reading = [role for role in spec.default_roles if role in READ_ONLY_ROLES]
            if reading and not spec.read_only:
                raise ValueError(
                    f"permission {spec.value}: not read-only, and granted to a role that only reads: {reading}"
                )
            seen.add(spec.value)
        for spec in specs:
            self._specs[spec.value] = spec

    def specs(self) -> List[PermissionSpec]:
        """Every permission, in listing order."""
        placed: List[PermissionSpec] = []
        last: List[PermissionSpec] = []
        position: Dict[str, int] = {}
        for spec in self._specs.values():
            if spec.after is None:
                position[spec.value] = len(placed)
                placed.append(spec)
            else:
                last.append(spec)
        for spec in last:
            at = position.get(spec.after)
            if at is None:
                position[spec.value] = len(placed)
                placed.append(spec)
                continue
            placed.insert(at + 1, spec)
            for value, index in position.items():
                if index > at:
                    position[value] = index + 1
            position[spec.value] = at + 1
        return placed

    def values(self) -> List[str]:
        """Every permission string, in listing order."""
        return [spec.value for spec in self.specs()]

    def get(self, value: str) -> Optional[PermissionSpec]:
        return self._specs.get(value)

    def is_registered(self, value: str) -> bool:
        return value in self._specs

    def is_read_only(self, value: str) -> bool:
        """Whether the permission is registered and read-only."""
        spec = self._specs.get(value)
        return spec is not None and spec.read_only

    def default_role_grants(self, role: str) -> List[str]:
        """The permissions granted to a default role through `default_roles`."""
        return [spec.value for spec in self.specs() if role in spec.default_roles]

    def __len__(self) -> int:
        return len(self._specs)


def _builtin_permissions() -> List[PermissionSpec]:
    """Every member of the enum, in the enum's order. All of them Community:
    the permissions of the Enterprise Edition features are registered by its
    extension (`Extension.permissions`)."""
    members = list(Permission)
    unclassified = [permission.value for permission in members if permission not in PERMISSION_READ_ONLY]
    if unclassified:
        raise ValueError(f"permissions that do not say whether they are read-only: {unclassified}")
    return [
        PermissionSpec(
            value=permission.value,
            description=PERMISSION_DESCRIPTIONS.get(permission),
            read_only=PERMISSION_READ_ONLY[permission],
        )
        for permission in members
    ]


BUILTIN_PERMISSIONS: Tuple[PermissionSpec, ...] = tuple(_builtin_permissions())

# The registry the application reads. Extensions add to it before the
# application serves.
PERMISSIONS = PermissionRegistry(BUILTIN_PERMISSIONS)
