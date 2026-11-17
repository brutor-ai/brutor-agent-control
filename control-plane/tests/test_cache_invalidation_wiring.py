# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Cache-invalidation *wiring* guard.

`test_cache_invalidation.py` covers the publisher itself — the channel name,
the message shape, the serde contract with the Rust proxy. What it cannot
cover is the question that actually bites operators:

    when an admin changes a binding, does the endpoint publish at all?

Its `TestEndpointResourceMapping` table looks like it answers that, but it
calls `publish_invalidation` directly with literal strings and asserts the
JSON echoes them back. It never imports an endpoint, so an endpoint that
publishes nothing passes it. Two real defects lived behind that gap:

  * `PUT /admin/agent-cards/{id}/resource-groups` and its mirror
    `PUT /admin/resource-groups/{id}/agent-cards` — an operator bound a card,
    the row was committed, and A2A kept answering "Agent card is not bound to
    the caller's resource group" until the group cache's 5-minute TTL lapsed
    or the proxy was restarted.
  * `POST|DELETE /admin/end-user-groups/{id}/resource-groups` — the same
    staleness on the access-*removal* direction.

So this module inspects the source. For every mutating admin route whose path
names a binding edge, it asserts the handler either publishes an invalidation
or is listed in ALLOWED_SILENT with a stated reason. Binding edges are the
scope because they are what feeds `EffectiveGroupData` in the proxy; a blanket
"every mutating route must publish" rule would flag ~260 routes that touch no
proxy-visible config and would be turned off within a week.

The guard is deliberately AST-based rather than a grep for the symbol: a call
inside the handler is what matters, not a mention in a docstring or an import
at the top of the module.
"""

import ast
import pathlib

import pytest
from tests.source_trees import community_sources

ADMIN_ENDPOINTS = (
    pathlib.Path(__file__).resolve().parents[1]
    / "app" / "api" / "v1" / "endpoints" / "admin"
)

MUTATING_METHODS = {"post", "put", "patch", "delete"}

PUBLISHERS = {"invalidate_proxy_cache", "publish_invalidation", "publish_invalidation_sync"}

# Path fragments that mark a route as changing a resource↔group edge, i.e.
# something the proxy materialises into EffectiveGroupData / user_groups.
BINDING_SEGMENTS = (
    "resource-groups",
    "agent-cards",
    "server-configs",
    "mcp-servers",
    "/groups",
    "/skills",
    "/models",
    "assign",
    "unassign",
    "/members",
)

# (module, handler) -> why it is legitimately silent.
#
# Add here only when the proxy does NOT cache what the route changes. Verify
# before adding: find the read path in brutor-gateway-core and confirm it is a
# live query. "Probably fine" is how the agent-card bug shipped.
ALLOWED_SILENT: dict[tuple[str, str], str] = {
    # Assigning an inbox item names an operator, not a resource↔group edge.
    # The proxy never reads assurance_inbox_items; nothing to invalidate.
    ("assurance_inbox.py", "assign_item"):
        "inbox assignee is an operator-facing field; the proxy does not read inbox items",

    # resource_group_agent_members is read with a direct SELECT on every
    # request — services/auth.rs (api-key + workload-token paths) and
    # services/autonomy.rs. Nothing caches the agent→group edge, so a
    # membership change is visible on the next call with no publish.
    ("agent_identities.py", "add_membership"):
        "agent→group edge is read live in auth.rs/autonomy.rs; not cached",
    ("agent_identities.py", "remove_membership"):
        "agent→group edge is read live in auth.rs/autonomy.rs; not cached",

    # Runner machine lifecycle. These drive an inference machine (Ollama /
    # KServe) and do not touch any row the proxy caches. Registering a model
    # DOES create an LLMModel row, but a brand-new model is bound to no group
    # and has no cache entry to go stale — ModelCache does not cache misses.
    # Unregistering deletes a live, possibly group-bound row and therefore is
    # NOT on this list; it publishes.
}


def _route_methods_and_paths(node: ast.AST) -> tuple[set[str], list[str]]:
    """Return the HTTP methods and literal paths this handler is decorated with."""
    methods: set[str] = set()
    paths: list[str] = []
    for dec in getattr(node, "decorator_list", []):
        if not isinstance(dec, ast.Call):
            continue
        func = dec.func
        if isinstance(func, ast.Attribute) and func.attr in MUTATING_METHODS:
            methods.add(func.attr)
            for arg in dec.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    paths.append(arg.value)
    return methods, paths


def _calls_publisher(node: ast.AST) -> bool:
    """True if the handler body calls a cache-invalidation publisher."""
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        name = (
            func.id if isinstance(func, ast.Name)
            else func.attr if isinstance(func, ast.Attribute)
            else None
        )
        if name in PUBLISHERS:
            return True
    return False


def _binding_routes(sources) -> list[tuple[str, str, str, str, bool]]:
    """(module, handler, methods, path, publishes) for every binding route in
    the source files `sources`."""
    found = []
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            methods, paths = _route_methods_and_paths(node)
            if not methods:
                continue
            route = paths[0] if paths else ""
            if not any(seg in route for seg in BINDING_SEGMENTS):
                continue
            found.append((
                path.name,
                node.name,
                ",".join(sorted(methods)),
                route,
                _calls_publisher(node),
            ))
    return found


# The Community package's routes. The Enterprise package's are held by the
# same rule in `enterprise/tests/test_cache_invalidation_wiring.py`: what this
# file asserts does not depend on whether the checkout has `enterprise/`.
BINDING_ROUTES = _binding_routes(community_sources(ADMIN_ENDPOINTS))


def test_discovery_actually_found_routes():
    """Guard the guard.

    Every assertion below is vacuous if the AST walk stops matching — a
    refactor to `APIRouter` subclasses or a directory move would silently
    turn this file into a no-op that reports success.
    """
    # 24 Community routes (the Enterprise half holds the Enterprise ones to
    # a bound of its own).
    assert len(BINDING_ROUTES) >= 24, (
        f"only {len(BINDING_ROUTES)} binding routes discovered — the AST walk has "
        "probably stopped matching the router decorators; fix the walk, do not "
        "lower this bound"
    )
    # And it must be finding publishers, not just routes.
    assert any(r[4] for r in BINDING_ROUTES), "no route detected as publishing"


@pytest.mark.parametrize(
    "module,handler,methods,route,publishes",
    BINDING_ROUTES,
    ids=[f"{r[0]}::{r[1]}" for r in BINDING_ROUTES],
)
def test_binding_route_publishes_invalidation(module, handler, methods, route, publishes):
    """A route that changes a resource↔group edge must tell the proxy."""
    assert_binding_route_publishes(module, handler, methods, route, publishes, ALLOWED_SILENT)


def assert_binding_route_publishes(module, handler, methods, route, publishes, allowed):
    if (module, handler) in allowed:
        pytest.skip(f"allowlisted: {allowed[(module, handler)]}")

    assert publishes, (
        f"{module}::{handler} ({methods.upper()} {route}) changes a resource↔group "
        f"binding but never calls invalidate_proxy_cache.\n\n"
        f"The proxy materialises these edges into EffectiveGroupData with a "
        f"5-minute TTL, so without a publish the change is invisible until the "
        f"TTL lapses or the gateway restarts.\n\n"
        f"Either add:\n"
        f"    await invalidate_proxy_cache(request, \"<type>\", tenant_id, <id>, action=\"update\")\n"
        f"or, if the proxy genuinely does not cache what this route changes, add it "
        f"to ALLOWED_SILENT with the read path that proves it."
    )


def stale_allowlist_entries(routes, allowed) -> list:
    live = {(r[0], r[1]): r[4] for r in routes}
    stale = []
    for key, reason in allowed.items():
        if key not in live:
            stale.append(f"{key[0]}::{key[1]} no longer exists ({reason})")
        elif live[key]:
            stale.append(f"{key[0]}::{key[1]} now publishes — drop the exemption")
    return stale


def test_allowlist_has_no_stale_entries():
    """An allowlisted handler that has since been fixed (or deleted) must not linger.

    A stale entry is worse than none: it silently exempts a route that a later
    refactor may have made cache-relevant again.
    """
    stale = stale_allowlist_entries(BINDING_ROUTES, ALLOWED_SILENT)
    assert not stale, "ALLOWED_SILENT is out of date:\n  " + "\n  ".join(stale)


def _mutating_handlers(module_name: str) -> dict[str, bool]:
    """{handler: publishes} for every mutating route in one admin module."""
    path = ADMIN_ENDPOINTS / module_name
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: dict[str, bool] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        methods, _paths = _route_methods_and_paths(node)
        if methods:
            out[node.name] = _calls_publisher(node)
    return out


# Modules whose rows the proxy holds in a TTL cache and *enforces* on —
# not a resource↔group edge, so BINDING_SEGMENTS never sees them. That
# is exactly how CP-3 hid: `semantic_policies.py` contained zero
# occurrences of `invalidate` while the generic guard above reported
# green. Enforcement controls get their own list because the cost of a
# stale one is asymmetric: a policy the operator just enabled is not
# enforced, and a mis-firing one they just disabled keeps refusing
# calls, for the whole cache TTL.
ENFORCEMENT_CONTROL_MODULES = (
    "semantic_policies.py",
    # Declared dependency edges feed the core's card →
    # shared-service map, which accepted-caller enforcement resolves with.
    "ai_system_composition.py",
)


@pytest.mark.parametrize("module", ENFORCEMENT_CONTROL_MODULES)
def test_enforcement_control_writes_publish_invalidation(module):
    handlers = _mutating_handlers(module)
    assert handlers, f"{module}: no mutating routes found — did the walk break?"
    silent = sorted(h for h, publishes in handlers.items() if not publishes)
    assert not silent, (
        f"{module}: {silent} mutate an enforcement control the proxy caches "
        f"but never call invalidate_proxy_cache. The change does not take "
        f"effect until the proxy's cache TTL lapses."
    )


def test_the_two_regressions_stay_fixed():
    """Pin the specific defects this module was written for.

    Named explicitly so that deleting the generic check above still leaves the
    known bugs covered.
    """
    by_key = {(r[0], r[1]): r for r in BINDING_ROUTES}
    for module, handler in (
        ("agent_cards.py", "set_card_resource_groups"),
        ("resource_groups.py", "set_group_agent_cards"),
        ("end_user_groups.py", "bind_resource_groups"),
        ("end_user_groups.py", "unbind_resource_groups"),
    ):
        route = by_key.get((module, handler))
        assert route is not None, f"{module}::{handler} not found — was it renamed?"
        assert route[4], (
            f"{module}::{handler} stopped publishing cache invalidation. This is the "
            f"regression that made bound agent cards invisible to the A2A "
            f"authorisation check until the gateway was restarted."
        )


# CP-4 / XREPO-8 — provider rows are not a resource↔group edge and are not an
# LLM-judge enforcement control, so neither list above reaches them. They are
# still a dispatch input the proxy caches for ten minutes: the base URL it
# posts to, the auth type it signs with, the residency region it is checked
# against, and the `enabled` flag that is supposed to stop traffic. A silent
# write here means "disable this provider" does nothing for ten minutes.
PROVIDER_CONFIG_HANDLERS = (
    "create_provider",
    "update_provider",
    "rename_provider",
    "delete_provider",
)


def test_llm_provider_config_writes_publish_invalidation():
    handlers = _mutating_handlers("llm_providers.py")
    missing = [h for h in PROVIDER_CONFIG_HANDLERS if h not in handlers]
    assert not missing, f"handlers renamed or removed: {missing} (found {sorted(handlers)})"
    silent = [h for h in PROVIDER_CONFIG_HANDLERS if not handlers[h]]
    assert not silent, (
        f"llm_providers.py::{silent} mutate provider config without publishing a "
        f"'llm_provider' invalidation. The proxy's ProviderCache holds slug + URL + "
        f"auth defaults for 600s and stamps the slug onto every cached model, so the "
        f"edit is invisible to dispatch until the TTL lapses."
    )


def test_llm_provider_icon_routes_stay_silent_on_purpose():
    """The icon routes are the only mutating ones in the module that do not
    publish — ProviderCache selects no icon column, so there is nothing to
    evict. Pinned so the exemption is a decision, not an oversight."""
    handlers = _mutating_handlers("llm_providers.py")
    icon_handlers = {h: p for h, p in handlers.items() if "icon" in h}
    assert icon_handlers, "icon routes not found — was the module restructured?"
    assert set(handlers) - set(icon_handlers) == set(PROVIDER_CONFIG_HANDLERS), (
        "a new mutating route appeared in llm_providers.py — decide whether it "
        "changes proxy-cached provider config and add it to PROVIDER_CONFIG_HANDLERS "
        "or document why it is exempt here."
    )
