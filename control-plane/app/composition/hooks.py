# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Behaviour hooks.

A behaviour hook is a place where Community Edition code hands over to an
Enterprise Edition behaviour that used to be written inline in it. The
Community code names the point and says what it does when nobody is
registered there; the Enterprise side registers the behaviour. The Community
module then imports nothing of the Enterprise feature.

The points are a closed list, defined here with their contracts:

`MCP_SERVER_DEPLOYMENT_TEARDOWN` — called as `hook(server=..., logger=...)`
by MCP server delete, before the row is deleted. Every registered hook is
called. A hook that raises does not stop the delete (the caller logs it).
With none registered nothing is torn down: the composition has no
deployments.

`MCP_CATALOG_CONTAINER_GATE` and `MCP_CATALOG_CONTAINER_INSTALL` — the
containerised step of a catalog import, asked for with `deploy_container`.
One hook each. The gate is called as `gate(db=..., tenant_id=..., logger=...)`
before anything is written and refuses by raising `HTTPException`. The
install is called as `install(db=..., entry=..., mcp_server=...,
tenant_id=..., env_values=..., logger=...)` after the rows exist and returns
an object with `deployed` (bool), `deployment_id` and `diagnose` (a callable
for the capability probe's `diagnose`, or None); it does not raise. With
either missing, the import refuses a containerised install by name
(`enterprise_edition_feature`) and writes nothing.

`SYSTEM_OPS_DEPENDENCY_PROBES` — called as `await hook()` by the System
Operations overview, beside its own probes; returns a card or None. Every
registered hook is called; with none, only the Community dependencies are
probed.

`SYSTEM_OPS_CONNECTION_POOLS` — called as `hook(db=..., tenant_id=...)` by
the connections page; returns a list of entries, shown after the Community
infrastructure entries and before the model providers.

`LLM_PROVIDER_CATALOG_ROW` — called as `hook(obj=..., key=...)` by the
provider-catalog seed for every row it inserts or refreshes, before the
flush; `obj` is the `llm_provider_catalog` row and `key` its provider key.
Every registered hook is called, and one that raises fails the seed. With
none registered the row keeps the columns the seed itself wrote: no provider
advertises a usage import.

`TENANT_QUOTA_COUNTS` — called as `hook(db=..., tenant_id=...)` by the tenant
quota check (`TenantService.check_quota`); returns a mapping of resource type
to a function of no arguments that counts the tenant's rows of that type.
Every registered hook is called; a type the Community check counts itself is
not replaced. With none registered only the Community resource types are
counted, and a quota on any other type has nothing to count: the composition
has no such resource.

`TENANT_QUOTA_CEILINGS` — called as `hook(db=..., tenant=...,
resource_type=..., count=...)` by the tenant quota check, for a resource type
it can count, before it compares the count with the tenant's own quota;
`count` is the function of no arguments that counts the tenant's rows of that
type. Every registered hook is called; one refuses the creation by raising
`HTTPException`. With none registered the tenant's own quota is the only
bound: the Community Edition has nothing above a tenant.

`TENANT_STATUS_CHECKS` — called as `hook(tenant=...)` by
`TenantService.validate_tenant` for a tenant that exists, is active and is
not suspended. Every registered hook is called; one refuses the tenant by
raising `HTTPException`. With none registered such a tenant is valid: the
Community Edition has one tenant and no trial.

`LLM_MODEL_SAVE_CHECKS` — called as `hook(provider=..., row=...)` by the
model create and update endpoints, before anything is written; `provider` is
the key of the provider the row will have and `row` a mapping of the fields
the hook may ask about as they will be (`supports_batch`). Every registered
hook is called; one refuses the save by raising `HTTPException`. With none
registered nothing more is asked of a row: the Community gateway has no
batch API, and `supports_batch` says only what the provider offers.

`TENANT_SETTINGS_KEPT_KEYS` — called as `hook()` by the tenant update
(`PATCH /admin/tenants/current` and tenant management's update of any
tenant), when the update names `settings`; returns the keys of
`tenants.settings` that have a route of their own. Every registered hook is
called. Those keys are left as they are stored, whatever the update says of
them, for every caller. With none registered every key of `settings` is the
administrator's to write: the Community Edition keeps nothing there.

`PORTAL_ADDRESS` — one hook, called as `hook()` where Community code
writes an address of the User Portal: the OAuth redirect URI of an MCP
server (seeded, or imported from the catalog) and the sign-in link of the
welcome email. Returns the Portal's base URL as a browser reaches it,
without a trailing slash, or None when none is configured. With none
registered there is no Portal: the Community Edition has none, and reads no
setting for one (`portal_public_url()` below is how the three places ask).

`HOOKS` is process-wide, as the other registries read inside a request are
(`composition/extensions.py`). It starts empty: this package defines the
points and no behaviour. The Enterprise Edition's extension registers its
behaviours (`Extension.hooks`); a Community Edition has none, and
`tests/test_behaviour_hooks.py` holds what each point does then.

A behaviour that an extension registered and that fails is an error where
the point's contract says so; none is skipped because its module could not
be imported.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

from app.composition.features import ENTERPRISE_FEATURES

MCP_SERVER_DEPLOYMENT_TEARDOWN = "mcp_server.deployment_teardown"
MCP_CATALOG_CONTAINER_GATE = "mcp_catalog.container_install_gate"
MCP_CATALOG_CONTAINER_INSTALL = "mcp_catalog.container_install"
SYSTEM_OPS_DEPENDENCY_PROBES = "system_ops.dependency_probes"
SYSTEM_OPS_CONNECTION_POOLS = "system_ops.connection_pools"
LLM_PROVIDER_CATALOG_ROW = "llm_provider_catalog.row"
TENANT_QUOTA_COUNTS = "tenant.quota_counts"
TENANT_QUOTA_CEILINGS = "tenant.quota_ceilings"
TENANT_STATUS_CHECKS = "tenant.status_checks"
LLM_MODEL_SAVE_CHECKS = "llm_model.save_checks"
TENANT_SETTINGS_KEPT_KEYS = "tenant.settings_kept_keys"
PORTAL_ADDRESS = "portal.address"

# point -> does it take one hook only?
HOOK_POINTS: Dict[str, bool] = {
    MCP_SERVER_DEPLOYMENT_TEARDOWN: False,
    MCP_CATALOG_CONTAINER_GATE: True,
    MCP_CATALOG_CONTAINER_INSTALL: True,
    SYSTEM_OPS_DEPENDENCY_PROBES: False,
    SYSTEM_OPS_CONNECTION_POOLS: False,
    LLM_PROVIDER_CATALOG_ROW: False,
    TENANT_QUOTA_COUNTS: False,
    TENANT_QUOTA_CEILINGS: False,
    TENANT_STATUS_CHECKS: False,
    LLM_MODEL_SAVE_CHECKS: False,
    TENANT_SETTINGS_KEPT_KEYS: False,
    PORTAL_ADDRESS: True,
}


@dataclass(frozen=True)
class HookSpec:
    """One behaviour registered at a hook point.

    `name` is unique and stable. `point` is one of `HOOK_POINTS`. `call` is
    the behaviour, with the signature the point's contract gives. `feature`
    is the Enterprise Edition feature the behaviour belongs to, or None.
    """

    name: str
    point: str
    call: Callable[..., Any]
    feature: Optional[str] = None


class HookRegistry:
    """The behaviours registered at each hook point, in registration order."""

    def __init__(self, specs: Iterable[HookSpec] = ()) -> None:
        self._specs: Dict[str, HookSpec] = {}
        self.register(specs)

    def register(self, specs: Iterable[HookSpec]) -> None:
        """Add hooks. All of them, or none.

        Refuses a name that is empty or padded, an unknown point, a `call`
        that is not callable, a feature that is not an Enterprise Edition
        feature, a different hook under a name already taken, and a second
        hook at a point that takes one. The same hook registered again is
        skipped (the same extension composed into a second application).
        """
        merged = dict(self._specs)
        for spec in specs:
            if not isinstance(spec.name, str) or not spec.name or spec.name != spec.name.strip():
                raise ValueError(f"hooks: name is empty or padded: {spec.name!r}")
            if spec.point not in HOOK_POINTS:
                raise ValueError(f"hooks: unknown hook point: {spec.point!r}")
            if not callable(spec.call):
                raise ValueError(f"hooks: {spec.name} is not callable")
            if spec.feature is not None and spec.feature not in ENTERPRISE_FEATURES:
                raise ValueError(f"hooks: {spec.name}: not an Enterprise Edition feature: {spec.feature!r}")
            present = merged.get(spec.name)
            if present is not None:
                if present == spec:
                    continue
                raise ValueError(f"hooks: a different hook is already registered as {spec.name}")
            if HOOK_POINTS[spec.point] and any(other.point == spec.point for other in merged.values()):
                raise ValueError(f"hooks: {spec.point} takes one hook, and has one")
            merged[spec.name] = spec
        self._specs = merged

    def specs(self) -> Tuple[HookSpec, ...]:
        return tuple(self._specs.values())

    def all(self, point: str) -> Tuple[Callable[..., Any], ...]:
        """Every behaviour registered at `point`; empty when there is none."""
        if point not in HOOK_POINTS:
            raise ValueError(f"hooks: unknown hook point: {point!r}")
        return tuple(spec.call for spec in self._specs.values() if spec.point == point)

    def one(self, point: str) -> Optional[Callable[..., Any]]:
        """The behaviour registered at a point that takes one, or None."""
        if not HOOK_POINTS.get(point, False):
            raise ValueError(f"hooks: not a point that takes one hook: {point!r}")
        found = self.all(point)
        return found[0] if found else None


# The registry the application reads. It starts empty: every behaviour is
# registered by an extension (`Extension.hooks`).
HOOKS = HookRegistry()


# Where the Portal's OAuth callback is when no Portal address is known: the
# Portal's development address.
DEFAULT_PORTAL_URL = "http://localhost:3001"


def portal_public_url() -> Optional[str]:
    """The User Portal's base URL as a browser reaches it, without a
    trailing slash; None when the composition has no Portal or none is
    configured."""
    hook = HOOKS.one(PORTAL_ADDRESS)
    url = hook() if hook is not None else None
    return url.rstrip("/") if url else None


def portal_oauth_callback_url() -> str:
    """The OAuth redirect URI an MCP server's client is registered with:
    the Portal's `/callback`."""
    return f"{portal_public_url() or DEFAULT_PORTAL_URL}/callback"
