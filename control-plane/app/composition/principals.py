# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The principal registry.

A principal is a kind of caller of the admin API: who a token can stand for.
There were two, written as `if admin_type == system ... elif tenant ...` in
the token verifier, in `resolve_tenant_id` and in some twenty route modules,
and the verifier's last word on a type it did not know was the system branch.
Nothing outside this code base could add a kind of caller, and none could be
left out of a distribution.

The registry is the answer in one place. It holds every principal as a
`PrincipalSpec`: the name its tokens carry in `admin_type`, its credential
class, how its token is verified, which tenant a request of its acts on,
where its session is, whether it is platform-wide, and the Enterprise Edition
feature it belongs to (or none, for the Community Edition).

`PRINCIPALS` starts with the one this package defines: `tenant`. The
Enterprise Edition's extension registers `system` (feature `multi_tenant`,
`Extension.principals`); `tests/test_principal_registry.py` holds what a
composition without it answers.

The rule the registry exists for: **a type that is not registered is
refused.** `get` returns None for it, and every caller of `get` treats None
as the most restrictive answer — never as some default principal.

Platform administration. Some things belong to the installation and not to a
tenant (the global model and MCP catalogs). Who may change them:

* a platform-wide principal (the system administrator), where one is
  registered;
* where none is registered — a single-tenant installation — the
  installation's own administrator: a principal pinned to a tenant that holds
  the permission `platform:catalog:manage`.

The second half is decided here and not by the permission alone: a tenant's
administrator can put any registered permission into a role of its tenant, so
in a multi-tenant installation the permission by itself must open nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from app.composition.features import ENTERPRISE_FEATURES
from app.core.admin_user import tenant_principal

# Credential classes: what kind of credential stands behind a
# principal. Every principal today is a person with a session.
HUMAN_SESSION = "human_session"
AUTOMATION = "automation"
RUNTIME = "runtime"
CREDENTIAL_CLASSES = frozenset({HUMAN_SESSION, AUTOMATION, RUNTIME})

# The permission a tenant-pinned principal needs to administer what belongs to
# the installation, where no platform-wide principal is registered. The same
# string as `Permission.PLATFORM_CATALOG_MANAGE`; written out so that this
# module needs nothing of the permission enum.
PLATFORM_CATALOG_MANAGE = "platform:catalog:manage"


# What a principal may leave out (see `PrincipalSpec`).
OPTIONAL_PARTS = (
    "owns",
    "find_admin",
    "audit_entry",
    "complete_sign_in",
    "active_sessions",
    "audit_log_model",
)


@dataclass(frozen=True)
class PrincipalSpec:
    """One kind of caller of the admin API.

    `name` is the value of the `admin_type` claim. `credential_class` is one
    of `CREDENTIAL_CLASSES`.

    `verify(service, payload, db)` returns `(admin, session)` for a token
    that stands for an active administrator with a live session, and None
    otherwise; `service` is the `UnifiedAuthService` asking. Raising is a
    refusal too (the service treats it as None).

    `resolve_tenant(admin, tenant_id_query)` returns the tenant a request
    acts on, or raises `HTTPException`.

    `find_session(db, session_id)` returns the session row, for logout.

    `platform_wide` is True for a principal that belongs to no tenant and may
    act across them. `feature` is the Enterprise Edition feature the
    principal belongs to, or None for the Community Edition.

    The rest is what a principal with administrator rows of its own answers;
    each is optional, and a principal without one is refused where it is
    asked for (never handed another principal's):

    `owns(admin)`: is this administrator object one of mine?

    `find_admin(db, admin_id)`: my administrator row with that id, or None.

    `audit_entry(db, logger, *, admin, admin_id, tenant_id,
    affected_tenant_id, **columns)`: the audit row of an action of mine, for
    my own trail, or None when it cannot be filed.

    `complete_sign_in(service, admin, challenge, db, ip_address, user_agent)`:
    the answer to a sign-in of mine that a second factor completed (it issues
    the session). `challenge` is the decoded challenge token.

    `active_sessions(db, admin_id)`: the live session rows of one of my
    administrators (each has `id` and `deactivate(reason)`), for ending them
    when its password changes.

    `audit_log_model()`: the model class of my own audit trail, for a
    principal whose trail is not a tenant's (the system administrator's):
    the audit export reads it for a caller that names no tenant.
    """

    name: str
    credential_class: str
    verify: Callable[[Any, Dict[str, Any], Any], Optional[Tuple[Any, Any]]]
    resolve_tenant: Callable[[Any, Optional[str]], str]
    find_session: Callable[[Any, str], Any]
    platform_wide: bool = False
    feature: Optional[str] = None
    owns: Optional[Callable[[Any], bool]] = None
    find_admin: Optional[Callable[[Any, Any], Any]] = None
    audit_entry: Optional[Callable[..., Any]] = None
    complete_sign_in: Optional[Callable[..., Dict[str, Any]]] = None
    active_sessions: Optional[Callable[[Any, Any], List[Any]]] = None
    audit_log_model: Optional[Callable[[], Any]] = None


class PrincipalRegistry:
    """The principals that exist, in the order they were registered."""

    def __init__(self, specs: Iterable[PrincipalSpec] = ()) -> None:
        self._specs: Dict[str, PrincipalSpec] = {}
        self.register(specs)

    def register(self, specs: Iterable[PrincipalSpec]) -> None:
        """Add principals. All of them, or none.

        Refuses a name that is empty, padded or already registered, an
        unknown credential class, a feature that is not an Enterprise Edition
        feature, and a spec without its three functions. A principal
        registered twice, or half described, would be an access decision
        nobody made.
        """
        specs = list(specs)
        seen = set(self._specs)
        for spec in specs:
            if not isinstance(spec.name, str) or not spec.name or spec.name != spec.name.strip():
                raise ValueError(f"principal name is empty or padded: {spec.name!r}")
            if spec.name in seen:
                raise ValueError(f"principal registered twice: {spec.name}")
            if spec.credential_class not in CREDENTIAL_CLASSES:
                raise ValueError(f"principal {spec.name}: unknown credential class {spec.credential_class!r}")
            if spec.feature is not None and spec.feature not in ENTERPRISE_FEATURES:
                raise ValueError(f"principal {spec.name}: unknown feature {spec.feature!r}")
            for part in ("verify", "resolve_tenant", "find_session"):
                if not callable(getattr(spec, part)):
                    raise ValueError(f"principal {spec.name}: {part} is not callable")
            for part in OPTIONAL_PARTS:
                if getattr(spec, part) is not None and not callable(getattr(spec, part)):
                    raise ValueError(f"principal {spec.name}: {part} is not callable")
            seen.add(spec.name)
        for spec in specs:
            self._specs[spec.name] = spec

    def specs(self) -> List[PrincipalSpec]:
        return list(self._specs.values())

    def names(self) -> List[str]:
        return list(self._specs)

    def get(self, admin_type: Any) -> Optional[PrincipalSpec]:
        """The principal registered under `admin_type`, or None.

        Takes the plain string and an `AdminType` member alike. Anything else
        — None, a missing claim, a number, a name with different case or
        padding — is not a registered principal: None, never a default.
        """
        name = getattr(admin_type, "value", admin_type)
        if not isinstance(name, str):
            return None
        return self._specs.get(name)

    def owner_of(self, admin: Any) -> Optional[PrincipalSpec]:
        """The registered principal whose administrator `admin` is, or None.

        The object decides, not a claim about it: each principal is asked
        whether the row is one of its own. An object no registered principal
        owns — a system administrator's row where none is registered, a
        stand-in, None — has no owner: None, never a default. A principal
        whose `owns` raises does not own it.
        """
        if admin is None:
            return None
        for spec in self._specs.values():
            if spec.owns is None:
                continue
            try:
                if spec.owns(admin) is True:
                    return spec
            except Exception:
                continue
        return None

    def has_platform_wide(self) -> bool:
        """Is a platform-wide principal registered (a multi-tenant installation)?"""
        return any(spec.platform_wide for spec in self._specs.values())

    def is_platform_wide(self, admin_type: Any) -> bool:
        """Is `admin_type` a registered, platform-wide principal?"""
        spec = self.get(admin_type)
        return spec is not None and spec.platform_wide

    def is_platform_admin(self, admin_type: Any, admin: Any) -> bool:
        """May this caller administer what belongs to the installation?

        See the module docstring. An unregistered type never may.
        """
        spec = self.get(admin_type)
        if spec is None or admin is None:
            return False
        if spec.platform_wide:
            return True
        if self.has_platform_wide():
            return False
        has_permission = getattr(admin, "has_permission", None)
        return callable(has_permission) and has_permission(PLATFORM_CATALOG_MANAGE) is True

    def __len__(self) -> int:
        return len(self._specs)


TENANT_ADMIN = PrincipalSpec(
    name=tenant_principal.NAME,
    credential_class=HUMAN_SESSION,
    verify=tenant_principal.verify,
    resolve_tenant=tenant_principal.resolve_tenant,
    find_session=tenant_principal.find_session,
    owns=tenant_principal.owns,
    find_admin=tenant_principal.find_admin,
    audit_entry=tenant_principal.audit_entry,
    complete_sign_in=tenant_principal.complete_sign_in,
    active_sessions=tenant_principal.active_sessions,
)

BUILTIN_PRINCIPALS: Tuple[PrincipalSpec, ...] = (TENANT_ADMIN,)

# The registry the application reads. Extensions add to it before the
# application serves.
PRINCIPALS = PrincipalRegistry(BUILTIN_PRINCIPALS)
