# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Policy-as-Code Constants and Enums

Defines lifecycle states for policy bundles, event types for the
audit trail, and template categories.
"""

from enum import Enum


class PolicyBundleStatus(str, Enum):
    """
    Policy bundle lifecycle

    Flow: DRAFT → VALIDATED → ACTIVE → SUPERSEDED
    - DRAFT: Created but not yet validated
    - VALIDATED: Passed schema + reference validation
    - ACTIVE: Currently applied — at most one active per (tenant, scope
      group). `apply()` supersedes on `(tenant_id, scope_group_id)`, so each
      resource group can carry its own active bundle; it is NOT one per tenant
    - SUPERSEDED: Was active, replaced by a newer bundle
    - ROLLED_BACK: Was active, rolled back to an earlier version
    """
    DRAFT = "draft"
    VALIDATED = "validated"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    ROLLED_BACK = "rolled_back"


class PolicyEventType(str, Enum):
    """Types of policy bundle events for the audit trail"""
    VALIDATED = "validated"
    APPLIED = "applied"
    ROLLED_BACK = "rolled_back"
    DRY_RUN = "dry_run"
    SUPERSEDED = "superseded"


class PolicyTemplateCategory(str, Enum):
    """Pre-built template categories"""
    COMPLIANCE = "compliance"
    SECURITY = "security"
    COST = "cost"
    DEVELOPMENT = "development"


class PolicySource(str, Enum):
    """How a policy bundle was created"""
    API = "api"
    CI_CD = "ci_cd"
    ADMIN_UI = "admin_ui"
    IMPORT = "import"


# The API version for the YAML schema
POLICY_API_VERSION = "brutor.ai/v1"
POLICY_KIND = "PolicyBundle"

# Allowed guardrail actions in YAML
ALLOWED_GUARDRAIL_ACTIONS = ["block", "redact", "warn", "allow"]

# Allowed skill execution modes in YAML
ALLOWED_SKILL_EXECUTION_MODES = ["unrestricted", "approval_required", "disabled"]

# Allowed MCP capability types in YAML
ALLOWED_CAPABILITY_TYPES = ["tool", "resource", "prompt", "resource_template"]
