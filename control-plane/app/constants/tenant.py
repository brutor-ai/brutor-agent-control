# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

from enum import Enum

# Tenant Resource types
# Canonical tenant feature flags: exactly the
# platform areas a deployment can switch off — for clients using part of the
# solution, or for a mini edition. Enforced in two layers: the console hides
# or FeatureDisables the surface, and each area's create/apply endpoint calls
# TenantService.check_feature_access (403). Keys are wire-stable: `a2a` is
# also enforced by the Rust core, so it keeps its historical key even though
# the label is "Agents (Cards & Identities)". Missing key = enabled.
# Keep in sync with: tenant.py defaults, admin UI lib/tenantFeatureFields.ts,
# the licence library's LicenseFeatures (Rust + trial-service Python mirror).
TENANT_FEATURE_KEYS: tuple = (
    "mcp_servers",
    "mcp_server_deployment",
    "mcp_registry",
    "virtual_mcp_servers",
    "models",
    "model_routing",
    "ai_model_deployment",
    "model_traffic_import",
    "agent_skills",
    "knowledge_base",
    "a2a",
    "guardrails",
    "policies",
    "compliance",
)

# What switching a flag off does, for the API description. The four flags
# below carry no traffic: off, they hide their Admin Console section and
# refuse the create and apply routes of their area for the tenant; what
# exists keeps being enforced, and traffic is not affected.
_ADMIN_ONLY = (
    "Off: the Admin Console hides {section} and the Control Plane refuses to create or apply "
    "{what} for the tenant. It does not stop traffic: what the tenant already has keeps being "
    "enforced by the gateway."
)
TENANT_FEATURE_DESCRIPTIONS: dict = {
    "guardrails": _ADMIN_ONLY.format(section="Guardrails", what="guardrail configurations"),
    "policies": _ADMIN_ONLY.format(section="Policies", what="policies"),
    "compliance": _ADMIN_ONLY.format(section="Compliance", what="compliance records"),
    "model_routing": _ADMIN_ONLY.format(section="Model Routing", what="routing groups"),
}


class TenantResourceTypes(str, Enum):
    ADMIN_USERS = "admin_users"
    END_USERS = "end_users"
    RESOURCE_GROUPS = "resource_groups"
    GUARDRAILS = "guardrails"
    ARGUMENT_POLICIES = "argument_policies"
    SEMANTIC_CACHE = "semantic_cache"
    MCP_SERVERS = "mcp-servers"
    VIRTUAL_SERVERS = "virtual-servers"
    AGENT_SKILLS = "agent_skills"
    LLM_MODELS = "llm_models"
    # A2A quota surfaces: cards + identities are
    # tenant-created resources with their own ceilings.
    AGENT_CARDS = "agent_cards"
    AGENT_IDENTITIES = "agent_identities"
    END_USER_GROUPS = "end_user_groups"
    REGISTRIES = "registries"
    MCP_CATALOG = "mcp_catalog"
    IDENTITY_PROVIDERS = "idps"
    OAUTH_CLIENTS = "oauth_clients"
    SERVER_CONFIGS = "server_configs"
    DOCKER_DEPLOYMENTS = "docker_deployments"
    METRICS = "metrics"
    PROXY = "proxy"
    PERMISSIONS = "permissions"
    KNOWLEDGE_BASE = "knowledge_base"
    KB_CONNECTORS = "kb_connectors"
    GATEWAY_CONFIGS = "gateway_configs" # Would not really have a resource constraint, but using it for consistency
    # System Operations Console — tenant-scoped read +
    # operate surface for runtime controls (counters, caches,
    # sessions, maintenance). Permission gates inside the endpoint
    # (`system:read` / `system:operate` / `system:maintenance:toggle`)
    # are the real authorization layer; this enum entry exists so
    # `require_tenant_resource_access` can attach a tenant id to the
    # request like every other admin endpoint does.
    SYSTEM_OPS = "system_ops"

class TenantDefaultTheme(str, Enum):
    PRIMARY_COLOR = "#1BB5E8",
    SECONDARY_COLOR = "#87ceeb",
    BACKGROUND_COLOR = "#ffffff",
    SURFACE_COLOR = "#f8f9fa",
    TEXT_PRIMARY_COLOR = "#000000",
    TEXT_SECONDARY_COLOR = "#666666",
    BORDER_COLOR = "#e0e0e0"


# Theme-variant keys. Order matches the admin-UI tab order
# (admin tabs first, then portal). Adding a new surface = append here +
# extend the variant editor; the rest of the platform reads this list.
THEME_VARIANTS = ("admin_light", "admin_dark", "portal_light", "portal_dark")

# Per-mode default palette. Light borrows from `TenantDefaultTheme`
# above (kept as-is for legacy single-theme callers). Dark uses a
# slate-based palette designed to (a) keep the brand primary visible
# without retuning per tenant, (b) hit WCAG AA contrast on
# text-primary against surface, and (c) match what the admin
# `useTheme` light/dark toggle ships as the default Tailwind dark
# scheme so an unbranded tenant doesn't look broken in dark mode.
DEFAULT_THEME_LIGHT = {
    "primary_color": TenantDefaultTheme.PRIMARY_COLOR.value,
    "secondary_color": TenantDefaultTheme.SECONDARY_COLOR.value,
    "background_color": TenantDefaultTheme.BACKGROUND_COLOR.value,
    "surface_color": TenantDefaultTheme.SURFACE_COLOR.value,
    "text_primary": TenantDefaultTheme.TEXT_PRIMARY_COLOR.value,
    "text_secondary": TenantDefaultTheme.TEXT_SECONDARY_COLOR.value,
    "border_color": TenantDefaultTheme.BORDER_COLOR.value,
    "logo_url": None,
    "favicon_url": None,
}
DEFAULT_THEME_DARK = {
    "primary_color": TenantDefaultTheme.PRIMARY_COLOR.value,  # brand stays
    "secondary_color": TenantDefaultTheme.SECONDARY_COLOR.value,
    "background_color": "#0f172a",  # slate-900
    "surface_color": "#1e293b",     # slate-800
    "text_primary": "#f1f5f9",       # slate-100
    "text_secondary": "#94a3b8",     # slate-400
    "border_color": "#334155",       # slate-700
    "logo_url": None,
    "favicon_url": None,
}


def default_theme_for_variant(variant: str) -> dict:
    """Return the default theme dict for `admin_light` / `admin_dark`
    / `portal_light` / `portal_dark`. Unknown variant raises
    ValueError to flag a coding error early (instead of silently
    returning a default and pretending the variant existed)."""
    if variant not in THEME_VARIANTS:
        raise ValueError(
            f"Unknown theme variant {variant!r}; expected one of {THEME_VARIANTS}"
        )
    return dict(DEFAULT_THEME_DARK if variant.endswith("_dark") else DEFAULT_THEME_LIGHT)

class TenantDefault(str, Enum):
    ID = "default"

class TenantDefaultRole(str, Enum):
    END_USER = "Tenant User"
    ADMIN_USER = "Tenant Admin"
    READ_ONLY_USER = "Tenant Read-Only"
    AUDITOR = "Tenant Auditor"
    # `Tenant Auditor` is intentionally NARROWER than `Tenant Read-Only`:
    # it can see what *happened* (proxy_logs, audit logs, A2A delegation
    # graphs, metrics) but cannot see what *could happen* (config —
    # mcp_servers, models, resource_groups, policies, users). The
    # separation-of-duties rationale is that audit logs can carry the
    # same PII guardrails were configured to block, so the person
    # reviewing them shouldn't also be able to weaken the configuration
    # that determined what got logged. Seeded by tenant_provisioning's
    # `get_default_tenant_roles`; the permission list is the
    # source-of-truth there.


# The default roles that only read: they hold read-only permissions and no
# others (`app.composition.permissions`).
READ_ONLY_ROLES = (TenantDefaultRole.READ_ONLY_USER.value, TenantDefaultRole.AUDITOR.value)

