# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Which edition this process is, and what in the database belongs to the
Enterprise Edition.

**The edition is read from the composition.** There is no build flag: a
process is the Enterprise Edition when anything it is composed of (a router,
a startup or shutdown step, a step of the install seed or of tenant
provisioning, a permission, a principal, a behaviour hook, an identity
protocol) is tagged with an Enterprise Edition feature, and the Community
Edition when nothing is. The built-in registries hold no tagged entry,
so `app.app.create_app` is the Community Edition; the
Enterprise Edition adds its entries through its `Extension`
(`brutor_enterprise.app.create_app`, or `brutor_enterprise.app.register()`
for a command that builds no application).

**What is Enterprise data** is written down once, here, for the start-up
check (`app/services/edition/guard.py`) and the reset command
(`app/services/edition/reset.py`):

* `ENTERPRISE_ONLY_TABLES`: the tables only Enterprise features write, by
  feature. The same list as `control_plane.enterprise_only_tables` in the
  boundary file (`editions.yaml`); `tests/test_edition_guard.py` holds
  the two equal.
* `ENTERPRISE_TENANT_FEATURE_FLAGS`: the tenant feature flags that switch an
  Enterprise feature on, with the feature each belongs to. The same keys as
  `control_plane.tenant_feature_flags.enterprise` in the boundary file.

The core gateway makes the same check from the same written contract (the
list of checks, their order, their SQL and the refusal text); a change here
is a change there.
"""

from __future__ import annotations

from typing import Dict, FrozenSet, Iterable, Tuple

from app.composition.features import (
    BATCH_JOBS,
    DEPLOYMENT,
    DISCOVERY,
    ENTERPRISE_FEATURES,
    KNOWLEDGE_BASE,
    MCP_REGISTRY,
    MULTI_TENANT,
    POLICY_AS_CODE,
    PROMPT_CACHE,
    RESPONSE_CACHE,
    TRAFFIC_IMPORT,
)

COMMUNITY = "community"
ENTERPRISE = "enterprise"

# Tables only Enterprise Edition features write. The Community Edition leaves
# them empty, so a row in any of them means Enterprise has used the database.
ENTERPRISE_ONLY_TABLES: Dict[str, Tuple[str, ...]] = {
    DEPLOYMENT: (
        "docker_images",
        "docker_deployments",
        "ecs_deployments",
        "kubernetes_deployments",
        "kubernetes_clusters",
        "llm_runner_machines",
        "resource_group_deployment_machines",
    ),
    TRAFFIC_IMPORT: ("usage_import_checkpoints", "usage_import_run_logs"),
    RESPONSE_CACHE: (
        "semantic_cache_configs",
        "resource_group_cache_configs",
        "semantic_cache_tool_rules",
        "router_cache_configs",
    ),
    PROMPT_CACHE: ("prompt_cache_policies", "resource_group_prompt_cache_policies"),
    MCP_REGISTRY: ("mcp_registry_configs",),
    KNOWLEDGE_BASE: (
        "kb_collections",
        "kb_documents",
        "kb_chunks",
        "kb_connectors",
        "resource_group_kb_collections",
        "resource_group_kb_connectors",
        "kb_connector_identity_mappings",
    ),
    POLICY_AS_CODE: ("policy_bundles", "policy_bundle_events", "policy_templates"),
    BATCH_JOBS: ("batch_jobs", "batch_requests"),
    MULTI_TENANT: ("system_admins", "system_admin_sessions", "system_admin_audit_logs", "platform_settings"),
    DISCOVERY: (
        "collector_keys",
        "discovered_assets",
        "discovery_evidence",
        "discovery_events",
        "observed_agent_events",
    ),
}

# Tenant feature flags (keys of `tenants.features`) that switch an Enterprise
# Edition feature on, and the feature each belongs to.
ENTERPRISE_TENANT_FEATURE_FLAGS: Dict[str, str] = {
    "mcp_server_deployment": DEPLOYMENT,
    "ai_model_deployment": DEPLOYMENT,
    "model_traffic_import": TRAFFIC_IMPORT,
    "mcp_registry": MCP_REGISTRY,
    "knowledge_base": KNOWLEDGE_BASE,
}


def enterprise_only_tables() -> Tuple[Tuple[str, str], ...]:
    """Every Enterprise-only table as `(table, feature)`, in a fixed order."""
    return tuple(
        (table, feature) for feature, tables in ENTERPRISE_ONLY_TABLES.items() for table in tables
    )


def _features_of(items: Iterable) -> FrozenSet[str]:
    return frozenset(
        feature for feature in (getattr(item, "feature", None) for item in items) if feature is not None
    )


def composed_enterprise_features(extra: Iterable = ()) -> FrozenSet[str]:
    """The Enterprise Edition features this process is composed of.

    Reads the built-in router and lifecycle lists and the process-wide
    registries when called, so that what an extension registered is counted.
    `extra` is anything else composed into one application: the routers and
    steps of its `Composition`.
    """
    from app.composition import routers, startup
    from app.composition.extensions import PROVISIONING, SEEDING
    from app.composition.hooks import HOOKS
    from app.composition.identity import IDENTITY_PROTOCOLS
    from app.composition.permissions import PERMISSIONS
    from app.composition.principals import PRINCIPALS

    found = set()
    for items in (
        routers.ROUTERS,
        startup.STARTUP,
        startup.SHUTDOWN,
        SEEDING.steps(),
        PROVISIONING.steps(),
        PERMISSIONS.specs(),
        PRINCIPALS.specs(),
        HOOKS.specs(),
        IDENTITY_PROTOCOLS.specs(),
        extra,
    ):
        found |= _features_of(items)
    unknown = found - ENTERPRISE_FEATURES
    if unknown:
        raise ValueError(f"edition: not Enterprise Edition features: {sorted(unknown)}")
    return frozenset(found)


def edition_of(enterprise_features: Iterable[str]) -> str:
    """`enterprise` when any Enterprise feature is composed, else `community`."""
    return ENTERPRISE if frozenset(enterprise_features) else COMMUNITY


def composed_edition(extra: Iterable = ()) -> str:
    return edition_of(composed_enterprise_features(extra))


def default_tenant_feature_flags(keys: Iterable[str]) -> Dict[str, bool]:
    """The feature flags a newly created default tenant starts with.

    Every flag is on, except the flags of Enterprise features this process
    does not have: a Community Edition must not switch on what it cannot
    serve, and its own next start would read the flag as Enterprise data.
    """
    composed = composed_enterprise_features()
    return {
        key: ENTERPRISE_TENANT_FEATURE_FLAGS.get(key) is None
        or ENTERPRISE_TENANT_FEATURE_FLAGS[key] in composed
        for key in keys
    }
