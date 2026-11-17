# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
`AdminPrincipal`: the type of an authenticated administrator.

Which kinds of administrator exist is a matter of the principal registry
(`app/composition/principals.py`): the Community Edition has the tenant
administrator, and an Enterprise composition adds the system administrator.
Code that receives "the administrator who made this request" therefore
annotates it with this type and not with the model classes of particular
principals; it used to read `Union[SystemAdmin, TenantAdmin]`, which made
every admin route module name a table only the Enterprise Edition writes.

The type says what every principal's administrator row has. What differs
between principals (which tenant a request acts on, whether the caller is
platform-wide, where its sessions and audit trail are) is asked of the
registry, never read off the object's class.

This module imports nothing of the application, so that anything may import
it.
"""

from __future__ import annotations

from typing import Protocol


class AdminPrincipal(Protocol):
    """An administrator row of any registered principal."""

    id: str
    username: str
    email: str

    def has_permission(self, permission_value: str) -> bool:
        """Does this administrator hold the permission?"""
        ...
