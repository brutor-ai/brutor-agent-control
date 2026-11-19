# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Permission definitions for RBAC system
All roles are now database-driven, defined in TenantRole/TenantRolePermission tables
"""

import enum
from typing import List


class Permission(enum.Enum):
    """Granular permissions mapped to API endpoints"""

    # Guardrail filter) Management
    GUARDRAIL_READ = "guardrail:read"
    GUARDRAIL_UPDATE = "guardrail:update"
    GUARDRAIL_TEST = "guardrail:test"

    # Argument Policy (POLICY-10 AST-aware argument checks) Management
    ARGUMENT_POLICY_READ = "argument_policy:read"
    ARGUMENT_POLICY_CREATE = "argument_policy:create"
    ARGUMENT_POLICY_UPDATE = "argument_policy:update"
    ARGUMENT_POLICY_DELETE = "argument_policy:delete"
    ARGUMENT_POLICY_IMPORT = "argument_policy:import"
    ARGUMENT_POLICY_EXPORT = "argument_policy:export"

    # MCP Server Management
    MCP_SERVER_CREATE = "mcp:server:create"
    MCP_SERVER_READ = "mcp:server:read"
    MCP_SERVER_UPDATE = "mcp:server:update"
    MCP_SERVER_DELETE = "mcp:server:delete"
    MCP_SERVER_TEST = "mcp:server:test"
    MCP_SERVER_HEALTH = "mcp:server:health"

    # Virtual Server Management
    VIRTUAL_SERVER_CREATE = "virtual:server:create"
    VIRTUAL_SERVER_READ = "virtual:server:read"
    VIRTUAL_SERVER_UPDATE = "virtual:server:update"
    VIRTUAL_SERVER_DELETE = "virtual:server:delete"

    # MCP Proxy Administration
    PROXY_LOGS_READ = "proxy:logs:read"
    # Bodies are a separate grant from the row. A log row says who
    # called what and whether it was allowed; the body is the prompt, the
    # tool arguments and the model's answer — the tenant's most sensitive
    # data. Reading the ledger should not imply reading its contents.
    PROXY_LOGS_READ_BODIES = "proxy:logs:read_bodies"
    PROXY_STATS_READ = "proxy:stats:read"
    PROXY_MANAGEMENT = "proxy:management"
    # Execute a data-subject erasure across the ledger.
    DATA_ERASURE = "data:erasure"

    # MCP Catalog Management
    MCP_CATALOG_READ = "mcp:catalog:read"
    MCP_CATALOG_CREATE = "mcp:catalog:create"
    MCP_CATALOG_UPDATE = "mcp:catalog:update"
    MCP_CATALOG_DELETE = "mcp:catalog:delete"
    MCP_CATALOG_IMPORT = "mcp:catalog:import"

    # OAuth App Client Management
    OAUTH_CLIENT_CREATE = "oauth:client:create"
    OAUTH_CLIENT_READ = "oauth:client:read"
    OAUTH_CLIENT_UPDATE = "oauth:client:update"
    OAUTH_CLIENT_DELETE = "oauth:client:delete"

    # Server Config Management
    SERVER_CONFIG_CREATE = "server:config:create"
    SERVER_CONFIG_READ = "server:config:read"
    SERVER_CONFIG_UPDATE = "server:config:update"
    SERVER_CONFIG_DELETE = "server:config:delete"

    # IdP Server Management
    IDP_SERVER_CREATE = "idp:server:create"
    IDP_SERVER_READ = "idp:server:read"
    IDP_SERVER_UPDATE = "idp:server:update"
    IDP_SERVER_DELETE = "idp:server:delete"
    IDP_SERVER_TEST = "idp:server:test"

    # OAuth Flow Management
    OAUTH_TOKEN = "oauth:token"

    # Admin User Management
    ADMIN_USER_CREATE = "admin:user:create"
    ADMIN_USER_READ = "admin:user:read"
    ADMIN_USER_UPDATE = "admin:user:update"
    ADMIN_USER_DELETE = "admin:user:delete"
    ADMIN_AUDIT_READ = "admin:audit:read"

    # End User Management
    END_USER_READ = "enduser:read"
    END_USER_UPDATE = "enduser:update"
    END_USER_DELETE = "enduser:delete"

    # End User Group Management. Separate scope from
    # END_USER_* so an operator can grant a "user-group admin" role
    # without exposing direct end-user mutation.
    END_USER_GROUP_CREATE = "endusergroup:create"
    END_USER_GROUP_READ = "endusergroup:read"
    END_USER_GROUP_UPDATE = "endusergroup:update"
    END_USER_GROUP_DELETE = "endusergroup:delete"
    END_USER_GROUP_MEMBERS_ADD = "endusergroup:members:add"
    END_USER_GROUP_MEMBERS_REMOVE = "endusergroup:members:remove"

    # Resource Group Management
    RESOURCE_GROUP_CREATE = "resourcegroup:create"
    RESOURCE_GROUP_READ = "resourcegroup:read"
    RESOURCE_GROUP_UPDATE = "resourcegroup:update"
    RESOURCE_GROUP_DELETE = "resourcegroup:delete"
    RESOURCE_GROUP_MEMBERS_READ = "resourcegroup:members:read"
    RESOURCE_GROUP_MEMBERS_ADD = "resourcegroup:members:add"
    RESOURCE_GROUP_MEMBERS_REMOVE = "resourcegroup:members:remove"

    # Runtime intervention.
    #
    # Separate from RESOURCE_GROUP_UPDATE on purpose: "can see runs" and "can
    # stop runs" are different jobs. A SOC operator watching the fleet should
    # be able to pull the handle without also being able to reconfigure the
    # groups they are watching.
    RUN_ABORT = "run:abort"

    # Role Management (Database-Driven)
    ROLES_CREATE = "roles:create"
    ROLES_READ = "roles:read"
    ROLES_UPDATE = "roles:update"
    ROLES_DELETE = "roles:delete"
    ROLES_ASSIGN = "roles:assign"
    PERMISSIONS_READ = "permissions:read"

    # Gateway Configuration
    GATEWAY_CONFIG_READ = "gateway:config:read"
    GATEWAY_CONFIG_UPDATE = "gateway:config:update"
    GATEWAY_STATUS_READ = "gateway:status:read"

    # Agent Skill Management
    AGENT_SKILL_READ = "agent_skill:read"
    AGENT_SKILL_CREATE  = "agent_skill:create"
    AGENT_SKILL_UPDATE  = "agent_skill:update"
    AGENT_SKILL_DELETE  = "agent_skill:delete"

    # LLM/AI Model Management
    AI_MODEL_CREATE = "ai:model:create"
    AI_MODEL_READ = "ai:model:read"
    AI_MODEL_UPDATE = "ai:model:update"
    AI_MODEL_DELETE = "ai:model:delete"
    AI_MODEL_TEST = "ai:model:test"
    AI_MODEL_HEALTH = "ai:model:health"

    # LLM Router Management
    LLM_ROUTER_READ = "llm:router:read"
    LLM_ROUTER_UPDATE = "llm:router:update"
    LLM_ROUTER_DELETE = "llm:router:delete"
    LLM_ROUTER_TEST = "llm:router:test"

    # A2A (Agent-to-Agent) — registry permissions.
    A2A_CARD_READ = "a2a:card:read"
    A2A_CARD_CREATE = "a2a:card:create"
    A2A_CARD_UPDATE = "a2a:card:update"
    A2A_CARD_DELETE = "a2a:card:delete"
    A2A_CARD_SIGN = "a2a:card:sign"
    A2A_CARD_REVOKE = "a2a:card:revoke"
    A2A_CARD_IMPORT = "a2a:card:import"
    A2A_TENANT_KEY_READ = "a2a:tenant_key:read"
    # Delegation-graph (Mission Control) observability.
    A2A_DELEGATION_READ = "a2a:delegation:read"
    # Stateful task registry (read-only; proxy writes rows).
    A2A_TASK_READ = "a2a:task:read"

    # AI Asset Registry — governed AI inventory.
    AI_ASSET_READ = "ai_asset:read"
    AI_ASSET_WRITE = "ai_asset:write"

    # Agent Identity — agents as governable principals + per-agent grants.
    AGENT_IDENTITY_READ = "agent_identity:read"
    AGENT_IDENTITY_WRITE = "agent_identity:write"

    # Identity policy — what credentials a group's traffic must present
    # (per group, and the tenant's identity default).
    IDENTITY_POLICY_READ = "identity_policy:read"
    IDENTITY_POLICY_MANAGE = "identity_policy:manage"

    # Semantic Governance Policy — NL/LLM-judged policy.
    SEMANTIC_POLICY_READ = "semantic_policy:read"
    SEMANTIC_POLICY_WRITE = "semantic_policy:write"

    # Assurance Inbox — human review of assurance findings.
    ASSURANCE_INBOX_READ = "assurance_inbox:read"
    ASSURANCE_INBOX_MANAGE = "assurance_inbox:manage"

    # Assurance notifications — channels + alerts.
    NOTIFICATION_READ = "notification:read"
    NOTIFICATION_MANAGE = "notification:manage"

    # Continuous Assurance Checks — spec authoring,
    # backtests, results, weekly digest.
    ASSURANCE_CHECK_READ = "assurance_check:read"
    ASSURANCE_CHECK_MANAGE = "assurance_check:manage"

    # Tool Approval Management
    TOOL_APPROVAL_READ = "tool_approval:read"
    TOOL_APPROVAL_MANAGE = "tool_approval:manage"

    # Compliance evidence programme. `evidence:*` is the sealed
    # record ledger — records, tenant logs, tree heads, receipts, witnesses,
    # disclosures. `compliance:*` is the framework registry and the
    # per-system compliance profiles. Split because an auditor reads the
    # ledger but never configures witnesses.
    EVIDENCE_READ = "evidence:read"
    EVIDENCE_MANAGE = "evidence:manage"
    COMPLIANCE_READ = "compliance:read"
    COMPLIANCE_MANAGE = "compliance:manage"

    # System Administration
    SYSTEM_ADMIN = "system:admin"

    # Platform administration: changing what belongs to the
    # installation and not to a tenant — the global model catalog and the
    # global MCP catalog. It opens those writes only in an installation with
    # no system administrator (a single-tenant installation), where the
    # Tenant Admin role is granted it; where a system administrator exists
    # the writes are the system administrator's and this permission opens
    # nothing (`PrincipalRegistry.is_platform_admin`).
    PLATFORM_CATALOG_MANAGE = "platform:catalog:manage"

    # System Operations Console
    #
    # Three-tier split mirroring the operator workflow:
    #   * SYSTEM_READ — view Overview / Counters / Caches / Audit /
    #     Connections / Sessions / Background Tasks read panes. Safe
    #     for an Auditor; required by every other write permission
    #     since the action buttons live on the same pages as the
    #     status they mutate.
    #   * SYSTEM_OPERATE — perform destructive runtime mutations:
    #     counter reset, cache flush, force-logout, scheduler
    #     trigger / pause / resume, pool dispose, log-level change.
    #     Implies SYSTEM_READ — the dependency is enforced at the
    #     endpoint layer, not the permission seed.
    #   * SYSTEM_MAINTENANCE_TOGGLE — gate the per-tenant
    #     `maintenance_mode` flag (off / read_only / drain / paused).
    #     Carved out separately because draining your own tenant is a
    #     blast-radius decision distinct from "I can flush a cache".
    #     Default-EXCLUDED from Tenant Admin (see
    #     get_default_tenant_roles): customers opt in per-role.
    SYSTEM_READ = "system:read"
    SYSTEM_OPERATE = "system:operate"
    SYSTEM_MAINTENANCE_TOGGLE = "system:maintenance:toggle"


# =============================================================================
# PERMISSION DESCRIPTIONS - Complete documentation for all permissions
# =============================================================================

PERMISSION_DESCRIPTIONS = {
    # Guardrail filter) Management
    Permission.GUARDRAIL_READ: "View guardrail configurations and filters",
    Permission.GUARDRAIL_UPDATE: "Update guardrail configurations and filters",
    Permission.GUARDRAIL_TEST:"Test guardrail configurations and filters",

    # MCP Server
    Permission.MCP_SERVER_CREATE: "Create new MCP server configurations",
    Permission.MCP_SERVER_READ: "View MCP server configurations",
    Permission.MCP_SERVER_UPDATE: "Update MCP server configurations",
    Permission.MCP_SERVER_DELETE: "Delete MCP server configurations",
    Permission.MCP_SERVER_TEST: "Test MCP server connections",
    Permission.MCP_SERVER_HEALTH: "Check MCP server health status",

    # Virtual Server
    Permission.VIRTUAL_SERVER_CREATE: "Create virtual server configurations",
    Permission.VIRTUAL_SERVER_READ: "View virtual server configurations",
    Permission.VIRTUAL_SERVER_UPDATE: "Update virtual server configurations",
    Permission.VIRTUAL_SERVER_DELETE: "Delete virtual server configurations",

    Permission.PROXY_LOGS_READ: "View proxy request logs",
    Permission.PROXY_LOGS_READ_BODIES: "View stored request and response bodies in proxy logs",
    Permission.PROXY_STATS_READ: "Access proxy statistics and performance metrics",
    Permission.PROXY_MANAGEMENT: "Manage proxy configuration and settings",
    Permission.DATA_ERASURE: "Erase a data subject's personal data from the ledger",

    # Registry
    Permission.MCP_CATALOG_READ: "View MCP server catalog entries",
    Permission.MCP_CATALOG_CREATE: "Create custom MCP catalog entries",
    Permission.MCP_CATALOG_UPDATE: "Update MCP catalog entries",
    Permission.MCP_CATALOG_DELETE: "Delete MCP catalog entries",
    Permission.MCP_CATALOG_IMPORT: "Import MCP servers from catalog",

    # OAuth App Clients
    Permission.OAUTH_CLIENT_CREATE: "Register new OAuth application clients",
    Permission.OAUTH_CLIENT_READ: "View OAuth application client configurations",
    Permission.OAUTH_CLIENT_UPDATE: "Update OAuth application client configurations",
    Permission.OAUTH_CLIENT_DELETE: "Delete OAuth application clients",

    # Server Configs
    Permission.SERVER_CONFIG_CREATE: "Create server configuration mappings",
    Permission.SERVER_CONFIG_READ: "View server configuration mappings",
    Permission.SERVER_CONFIG_UPDATE: "Update server configuration mappings",
    Permission.SERVER_CONFIG_DELETE: "Delete server configuration mappings",

    # IdP Servers
    Permission.IDP_SERVER_CREATE: "Register new Identity Provider servers",
    Permission.IDP_SERVER_READ: "View Identity Provider configurations",
    Permission.IDP_SERVER_UPDATE: "Update Identity Provider configurations",
    Permission.IDP_SERVER_DELETE: "Delete Identity Provider servers",
    Permission.IDP_SERVER_TEST: "Test Identity Provider connections",

    # OAuth Flows
    Permission.OAUTH_TOKEN: "Manage OAuth access and refresh tokens",

    # Admin Users
    Permission.ADMIN_USER_CREATE: "Create new admin user accounts",
    Permission.ADMIN_USER_READ: "View admin user information and profiles",
    Permission.ADMIN_USER_UPDATE: "Modify admin user accounts and settings",
    Permission.ADMIN_USER_DELETE: "Delete or deactivate admin user accounts",
    Permission.ADMIN_AUDIT_READ: "Access audit logs and compliance data",

    # End Users
    Permission.END_USER_READ: "View end user accounts and profiles",
    Permission.END_USER_UPDATE: "Modify end user accounts and settings",
    Permission.END_USER_DELETE: "Delete or deactivate end user accounts",

    # End User Group Management
    Permission.END_USER_GROUP_CREATE: "Create end-user groups",
    Permission.END_USER_GROUP_READ: "View end-user groups and their members",
    Permission.END_USER_GROUP_UPDATE: "Modify end-user group metadata",
    Permission.END_USER_GROUP_DELETE: "Delete end-user groups",
    Permission.END_USER_GROUP_MEMBERS_ADD: "Add users to end-user groups",
    Permission.END_USER_GROUP_MEMBERS_REMOVE: "Remove users from end-user groups",

    # Resource Groups
    Permission.RESOURCE_GROUP_CREATE: "Create new resource groups",
    Permission.RESOURCE_GROUP_READ: "View resource groups and their configurations",
    Permission.RESOURCE_GROUP_UPDATE: "Update resource group configurations",
    Permission.RESOURCE_GROUP_DELETE: "Delete resource groups",
    Permission.RESOURCE_GROUP_MEMBERS_READ: "View resource group membership",
    Permission.RESOURCE_GROUP_MEMBERS_ADD: "Add users to resource groups",
    Permission.RESOURCE_GROUP_MEMBERS_REMOVE: "Remove users from resource groups",
    Permission.RUN_ABORT: "Stop an in-flight run",

    # Role Management
    Permission.ROLES_CREATE: "Create new custom roles",
    Permission.ROLES_READ: "View roles and their permissions",
    Permission.ROLES_UPDATE: "Modify existing roles and permissions",
    Permission.ROLES_DELETE: "Delete custom roles",
    Permission.ROLES_ASSIGN: "Assign roles to admin users",
    Permission.PERMISSIONS_READ: "View available permissions",

    # Gateway
    Permission.GATEWAY_CONFIG_READ: "View gateway configuration settings",
    Permission.GATEWAY_CONFIG_UPDATE: "Modify gateway configuration",
    Permission.GATEWAY_STATUS_READ: "Access gateway system status and health",

    # AI/LLM Models
    Permission.AI_MODEL_CREATE: "Register new AI/LLM models",
    Permission.AI_MODEL_READ: "View AI model configurations and details",
    Permission.AI_MODEL_UPDATE: "Update AI model configurations",
    Permission.AI_MODEL_DELETE: "Delete AI model registrations",
    Permission.AI_MODEL_TEST: "Test AI model connections and responses",
    Permission.AI_MODEL_HEALTH: "Check AI model health and availability",

    # LLM Router
    Permission.LLM_ROUTER_READ: "View LLM router configurations",
    Permission.LLM_ROUTER_UPDATE: "Update LLM router settings",
    Permission.LLM_ROUTER_DELETE: "Delete LLM router configurations",
    Permission.LLM_ROUTER_TEST: "Send a real, billed test request through a routing group",

    # A2A (Agent-to-Agent)
    Permission.A2A_CARD_READ: "View A2A agent card registry entries",
    Permission.A2A_CARD_CREATE: "Create new A2A agent card entries",
    Permission.A2A_CARD_UPDATE: "Update A2A agent card metadata or body",
    Permission.A2A_CARD_DELETE: "Delete A2A agent card entries (hard delete)",
    Permission.A2A_CARD_SIGN: "Sign A2A agent cards with the tenant's Ed25519 key",
    Permission.A2A_CARD_REVOKE: "Revoke A2A agent cards (terminal)",
    Permission.A2A_CARD_IMPORT: "Import A2A agent cards from external .well-known URLs",
    Permission.A2A_TENANT_KEY_READ: "View the tenant's A2A public signing key",
    Permission.A2A_DELEGATION_READ: "View A2A delegation graphs in Mission Control",
    Permission.A2A_TASK_READ: "View A2A v1.0 stateful tasks",

    # AI Asset Registry
    Permission.AI_ASSET_READ: "View the AI asset registry (governed AI inventory)",
    Permission.AI_ASSET_WRITE: (
        "Create, update, delete, and sync AI asset registry entries"
    ),

    # Agent Identity
    Permission.AGENT_IDENTITY_READ: "View agent identities, grants, and memberships",
    Permission.AGENT_IDENTITY_WRITE: (
        "Create/update/revoke agent identities, grants, and resource-group memberships"
    ),

    # Identity policy
    Permission.IDENTITY_POLICY_READ: (
        "View identity policies, effective credential requirements and the "
        "credentials that reached each AI System"
    ),
    Permission.IDENTITY_POLICY_MANAGE: (
        "Set a group's identity policy and the tenant's identity default"
    ),

    # Semantic Governance Policy
    Permission.SEMANTIC_POLICY_READ: "View semantic governance policies",
    Permission.SEMANTIC_POLICY_WRITE: (
        "Create/update/delete semantic governance policies and their bindings"
    ),

    # Tool Approvals
    # Assurance Inbox
    Permission.ASSURANCE_INBOX_READ: (
        "View the assurance inbox (flagged runs and findings awaiting review)"
    ),
    Permission.ASSURANCE_INBOX_MANAGE: (
        "Acknowledge, assign, and record dispositions on assurance inbox items"
    ),
    Permission.NOTIFICATION_READ: (
        "View notification channels, subscriptions, and delivery history"
    ),
    Permission.NOTIFICATION_MANAGE: (
        "Create and manage notification channels and alert subscriptions"
    ),
    Permission.ASSURANCE_CHECK_READ: (
        "View continuous assurance checks, their results, and weekly digests"
    ),
    Permission.ASSURANCE_CHECK_MANAGE: (
        "Author, backtest, enable, and delete continuous assurance checks"
    ),
    Permission.TOOL_APPROVAL_READ: "View tool approval requests and history",
    Permission.TOOL_APPROVAL_MANAGE: "Approve or reject tool approval requests",

    # Compliance evidence programme
    Permission.EVIDENCE_READ: (
        "View sealed action records, tenant evidence logs, tree heads, "
        "witness receipts and disclosure records"
    ),
    Permission.EVIDENCE_MANAGE: (
        "Configure witnesses (transparency services and timestamp authorities)"
    ),
    Permission.COMPLIANCE_READ: (
        "View the compliance framework registry and AI System compliance profiles"
    ),
    Permission.COMPLIANCE_MANAGE: (
        "Enable frameworks for the tenant and declare AI System compliance profiles"
    ),

    # System
    Permission.SYSTEM_ADMIN: "Complete system administration access (super admin only)",
    Permission.PLATFORM_CATALOG_MANAGE: "Manage the installation's global model and MCP catalogs (single-tenant installations)",

    # System Operations Console
    Permission.SYSTEM_READ: (
        "View System page: license, health grid, counter status, cache "
        "stats, scheduler state, sessions, and the operator audit trail."
    ),
    Permission.SYSTEM_OPERATE: (
        "Take destructive runtime actions from the System page: reset "
        "rate-limit counters, flush caches, force-logout sessions, "
        "trigger / pause / resume schedulers, dispose pools, change "
        "log levels. Implies SYSTEM_READ at the endpoint layer."
    ),
    Permission.SYSTEM_MAINTENANCE_TOGGLE: (
        "Toggle per-tenant maintenance mode (off / read_only / drain / "
        "paused). Carved out from SYSTEM_OPERATE because draining a "
        "tenant has a tenant-wide blast radius."
    ),
}


# =============================================================================
# READ-ONLY CLASSIFICATION
# =============================================================================
#
# Every permission says whether it is read-only: whether everything it opens
# leaves the configuration and the records as they are. The Tenant Read-Only
# and Tenant Auditor roles hold read-only permissions and no others, and the
# permission registry refuses to grant them anything else. A permission that
# is added states which it is here, or the permission registry does not build.

PERMISSION_READ_ONLY = {
    Permission.GUARDRAIL_READ: True,
    Permission.GUARDRAIL_UPDATE: False,
    Permission.GUARDRAIL_TEST: False,
    Permission.ARGUMENT_POLICY_READ: True,
    Permission.ARGUMENT_POLICY_CREATE: False,
    Permission.ARGUMENT_POLICY_UPDATE: False,
    Permission.ARGUMENT_POLICY_DELETE: False,
    Permission.ARGUMENT_POLICY_IMPORT: False,
    Permission.ARGUMENT_POLICY_EXPORT: True,
    Permission.MCP_SERVER_CREATE: False,
    Permission.MCP_SERVER_READ: True,
    Permission.MCP_SERVER_UPDATE: False,
    Permission.MCP_SERVER_DELETE: False,
    Permission.MCP_SERVER_TEST: False,
    Permission.MCP_SERVER_HEALTH: True,
    Permission.VIRTUAL_SERVER_CREATE: False,
    Permission.VIRTUAL_SERVER_READ: True,
    Permission.VIRTUAL_SERVER_UPDATE: False,
    Permission.VIRTUAL_SERVER_DELETE: False,
    Permission.PROXY_LOGS_READ: True,
    Permission.PROXY_LOGS_READ_BODIES: True,
    Permission.PROXY_STATS_READ: True,
    Permission.PROXY_MANAGEMENT: False,
    Permission.DATA_ERASURE: False,
    Permission.MCP_CATALOG_READ: True,
    Permission.MCP_CATALOG_CREATE: False,
    Permission.MCP_CATALOG_UPDATE: False,
    Permission.MCP_CATALOG_DELETE: False,
    Permission.MCP_CATALOG_IMPORT: False,
    Permission.OAUTH_CLIENT_CREATE: False,
    Permission.OAUTH_CLIENT_READ: True,
    Permission.OAUTH_CLIENT_UPDATE: False,
    Permission.OAUTH_CLIENT_DELETE: False,
    Permission.SERVER_CONFIG_CREATE: False,
    Permission.SERVER_CONFIG_READ: True,
    Permission.SERVER_CONFIG_UPDATE: False,
    Permission.SERVER_CONFIG_DELETE: False,
    Permission.IDP_SERVER_CREATE: False,
    Permission.IDP_SERVER_READ: True,
    Permission.IDP_SERVER_UPDATE: False,
    Permission.IDP_SERVER_DELETE: False,
    Permission.IDP_SERVER_TEST: False,
    Permission.OAUTH_TOKEN: False,
    Permission.ADMIN_USER_CREATE: False,
    Permission.ADMIN_USER_READ: True,
    Permission.ADMIN_USER_UPDATE: False,
    Permission.ADMIN_USER_DELETE: False,
    Permission.ADMIN_AUDIT_READ: True,
    Permission.END_USER_READ: True,
    Permission.END_USER_UPDATE: False,
    Permission.END_USER_DELETE: False,
    Permission.END_USER_GROUP_CREATE: False,
    Permission.END_USER_GROUP_READ: True,
    Permission.END_USER_GROUP_UPDATE: False,
    Permission.END_USER_GROUP_DELETE: False,
    Permission.END_USER_GROUP_MEMBERS_ADD: False,
    Permission.END_USER_GROUP_MEMBERS_REMOVE: False,
    Permission.RESOURCE_GROUP_CREATE: False,
    Permission.RESOURCE_GROUP_READ: True,
    Permission.RESOURCE_GROUP_UPDATE: False,
    Permission.RESOURCE_GROUP_DELETE: False,
    Permission.RESOURCE_GROUP_MEMBERS_READ: True,
    Permission.RESOURCE_GROUP_MEMBERS_ADD: False,
    Permission.RESOURCE_GROUP_MEMBERS_REMOVE: False,
    Permission.RUN_ABORT: False,
    Permission.ROLES_CREATE: False,
    Permission.ROLES_READ: True,
    Permission.ROLES_UPDATE: False,
    Permission.ROLES_DELETE: False,
    Permission.ROLES_ASSIGN: False,
    Permission.PERMISSIONS_READ: True,
    Permission.GATEWAY_CONFIG_READ: True,
    Permission.GATEWAY_CONFIG_UPDATE: False,
    Permission.GATEWAY_STATUS_READ: True,
    Permission.AGENT_SKILL_READ: True,
    Permission.AGENT_SKILL_CREATE: False,
    Permission.AGENT_SKILL_UPDATE: False,
    Permission.AGENT_SKILL_DELETE: False,
    Permission.AI_MODEL_CREATE: False,
    Permission.AI_MODEL_READ: True,
    Permission.AI_MODEL_UPDATE: False,
    Permission.AI_MODEL_DELETE: False,
    Permission.AI_MODEL_TEST: False,
    Permission.AI_MODEL_HEALTH: False,
    Permission.LLM_ROUTER_READ: True,
    Permission.LLM_ROUTER_UPDATE: False,
    Permission.LLM_ROUTER_DELETE: False,
    Permission.LLM_ROUTER_TEST: False,
    Permission.A2A_CARD_READ: True,
    Permission.A2A_CARD_CREATE: False,
    Permission.A2A_CARD_UPDATE: False,
    Permission.A2A_CARD_DELETE: False,
    Permission.A2A_CARD_SIGN: False,
    Permission.A2A_CARD_REVOKE: False,
    Permission.A2A_CARD_IMPORT: False,
    Permission.A2A_TENANT_KEY_READ: True,
    Permission.A2A_DELEGATION_READ: True,
    Permission.A2A_TASK_READ: True,
    Permission.AI_ASSET_READ: True,
    Permission.AI_ASSET_WRITE: False,
    Permission.AGENT_IDENTITY_READ: True,
    Permission.AGENT_IDENTITY_WRITE: False,
    Permission.IDENTITY_POLICY_READ: True,
    Permission.IDENTITY_POLICY_MANAGE: False,
    Permission.SEMANTIC_POLICY_READ: True,
    Permission.SEMANTIC_POLICY_WRITE: False,
    Permission.ASSURANCE_INBOX_READ: True,
    Permission.ASSURANCE_INBOX_MANAGE: False,
    Permission.NOTIFICATION_READ: True,
    Permission.NOTIFICATION_MANAGE: False,
    Permission.ASSURANCE_CHECK_READ: True,
    Permission.ASSURANCE_CHECK_MANAGE: False,
    Permission.TOOL_APPROVAL_READ: True,
    Permission.TOOL_APPROVAL_MANAGE: False,
    Permission.EVIDENCE_READ: True,
    Permission.EVIDENCE_MANAGE: False,
    Permission.COMPLIANCE_READ: True,
    Permission.COMPLIANCE_MANAGE: False,
    Permission.SYSTEM_ADMIN: False,
    Permission.PLATFORM_CATALOG_MANAGE: False,
    Permission.SYSTEM_READ: True,
    Permission.SYSTEM_OPERATE: False,
    Permission.SYSTEM_MAINTENANCE_TOGGLE: False,
}


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def get_all_permission_strings() -> List[str]:
    """
    Every permission string that exists (e.g., ["mcp:server:read", ...]).

    That is the permission registry's answer (`app.composition.permissions`),
    not this enum's: the enum names the permissions this code base requires at
    its routes, and the registry may hold more.
    """
    from app.composition.permissions import PERMISSIONS

    return PERMISSIONS.values()


def validate_permission(permission_str: str) -> bool:
    """
    Check if a permission string is valid, which is to say registered.

    Args:
        permission_str: Permission string to validate

    Returns:
        True if valid, False otherwise
    """
    from app.composition.permissions import PERMISSIONS

    return PERMISSIONS.is_registered(permission_str)
