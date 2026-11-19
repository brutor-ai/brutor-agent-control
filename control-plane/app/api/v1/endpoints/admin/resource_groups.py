# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Resource Groups API Endpoints 
Manages resource groups for organizing AI resuorces, such as LLMs and MCPs, into hierarchies with limits.
"""

from datetime import datetime, timezone
from ulid import ULID
from typing import Any, Dict, Optional, Union, List
from fastapi import APIRouter, Depends, HTTPException, status, Request, Query
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from sqlalchemy import or_, and_, text, select, func, case

from app.constants.outcomes import starter_catalogue
from app.core.sync_bridge import block_on
from app.constants.tenant import TenantResourceTypes
from app.services.tenant.tenant_service import TenantService
from app.database import get_db
from app.constants.proxy import ProxyType
from app.services.utils.authentication_context import AuthenticationContext
from app.database.models.api_key import APIKey
from app.database.models import MCPServer, UsageLog, UsageAlert
from app.database.models.llm_model import LLModel
from app.database.models.server_config import ServerConfig
from app.database.models.agent_skill import AgentSkill
from app.database.models.ai_asset import LIFECYCLE_PROPOSED
from app.database.models.resource_group import ResourceGroup, GroupType, DEFAULT_SYSTEM_KIND, SYSTEM_KINDS, resource_group_llm_models, resource_group_server_configs, resource_group_mcp_capabilities, resource_group_llm_governance, restrictive_merge, merge_limit_documents, LimitFamily, _merge_governance_child_first, _governance_restrictive_merge, validate_llm_governance, validate_mcp_governance, validate_skill_governance, _skill_governance_restrictive_merge, _ACCESS_STRICTNESS, _MCP_ACCESS_STRICTNESS, _ENFORCE_STRICTNESS, _SKILL_EXECUTION_STRICTNESS, MEDIA_GOVERNANCE_FIELD_RULES, _media_governance_restrictive_merge
from app.database.models.resource_group_membership import ResourceGroupMembership
from app.database.models.end_user import EndUser
from app.database.models.admin import TenantAdmin
from app.core.admin_user.admin_principal import AdminPrincipal
from app.database.models.guardrail_config import GuardrailConfig
from app.core.admin_user.permissions import Permission
from app.core.admin_user.dependencies import (
    acting_tenant_id,
    require_permission,
    get_audit_logger,
    get_current_admin
)
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.services.usage_limit_enforcer import UsageLimitEnforcer
from app.services.cache_invalidation import invalidate_proxy_cache
from app.services import mcp_exec_limits
from app.services.notifications import notify_group_members_about_resource_change
from app.core.logging_system import BrutorLogger
from app.api.v1.models.admin.resource_group import (
    A2A_ALLOW_LISTS_RULE,
    A2AGlobalGovernanceResponse,
    GroupAgentCardsRequest,
    ResourceGroupCreate, ResourceGroupUpdate, ResourceGroupResponse, ResourceGroupListResponse,
    ResourceGroupStatsResponse, GroupMembershipAdd, GroupMemberInfo, GroupMemberListResponse,
    GroupHierarchyTree, GroupAncestorPathResponse, GroupAncestorInfo, GroupDescendantsResponse,
    GroupSiblingsResponse, GroupHierarchyInfo, EffectiveLLModelsResponse, EffectiveServerConfigsResponse,
    EffectiveAgentSkillsResponse, AgentSkillInfo,
    GroupLimitsResponse,
    LLMGlobalLimitsUpdate, LLMGlobalLimitsResponse,
    MCPGlobalLimitsUpdate, MCPGlobalLimitsResponse,
    SkillGlobalLimitsUpdate, SkillGlobalLimitsResponse,
    MediaGlobalLimitsUpdate, MediaGlobalLimitsResponse,
    MediaGlobalGovernanceUpdate, MediaGlobalGovernanceResponse,
    LLMModelLimitsResponse, MCPServerLimitsResponse, SkillItemLimitsResponse, ItemLimitsUpdate,
    PortalVisibilityUpdate,
    LLMModelGovernanceUpdate, LLMModelGovernanceResponse,
    LLMGlobalGovernanceUpdate, MCPGlobalGovernanceUpdate, SkillGovernanceUpdate,
    LLMGlobalGovernanceResponse, MCPGlobalGovernanceResponse,
    SkillGlobalGovernanceResponse, SkillGovernanceResponse,
    LLModelInfo, ServerConfigInfo, GroupAPIKeyUsageResponse,
    APIKeyCreatedResponse, APIKeyCreate, APIKeyUpdate, GroupAPIKeysResponse, APIKeyRevokeRequest,
    GroupUsageResponse, UsageSummary, A2AUsageMetrics,
    AgentCardUsageInfo, AgentCapabilityUsageInfo,
    GroupAlertResponse, TenantAlertsResponse, AlertAcknowledgeResponse, AddLLMModelToGroupRequest,
    AddServerConfigToGroupRequest,
    MCPCapabilityInfo, MCPCapabilityFilterResponse, MCPCapabilityFilterUpdate,
    MCPDiscoveredCapability, CapabilityFilterSummary,
    SiblingLimitInfo, LimitCapacityInfo,
    ModelLimitInfo, PerModelCapacityInfo,
    ServerLimitInfo, PerServerCapacityInfo,
    SkillLimitInfo, PerSkillCapacityInfo,
    ApprovalWindowUpdate, ApprovalWindowResponse,
)
from app.api.v1.models.admin.resource_group import GuardrailConfigInfo, EffectiveGuardrailsResponse

import copy
from app.middleware.trusted_proxies import client_ip


def _compute_inherited_limits(group: ResourceGroup, attr: str) -> Optional[dict]:
    """
    Compute the inherited floor for a limit field by merging all ancestor limits
    (excluding the group itself) by the rule of their family
    (`merge_limit_documents`). Returns None if no ancestors have limits.
    """
    ancestors = group.get_ancestors()  # root → ... → parent (excludes self)
    return merge_limit_documents(attr, [getattr(ancestor, attr, None) for ancestor in ancestors])


def _compute_inherited_governance(group: ResourceGroup, attr_name: str) -> Optional[dict]:
    """
    Compute inherited LLM or MCP governance from ancestors only (excluding
    self). Skill and media governance: `_compute_inherited_skill_governance`,
    `_compute_inherited_media_governance`.
    """
    ancestors = group.get_ancestors()  # root → ... → parent (excludes self)
    if not ancestors:
        return None

    effective = None
    for ancestor in ancestors:
        gov = getattr(ancestor, attr_name, None)
        if gov:
            if effective is None:
                effective = copy.deepcopy(gov)
            else:
                _governance_restrictive_merge(effective, gov)
    return effective


# Fields where a HIGHER value is MORE restrictive (opposite of every other
# limit). The only case today is `cooldown_seconds` — a longer penalty
# lockout means callers must wait longer, which is stricter, not looser.
# Without this set, a parent with cooldown=60 would reject a child with
# cooldown=120 ("exceeds inherited limit") even though 120s is the more
# restrictive policy. Applies to both LLM throughput and MCP frequency
# sections.
_INVERTED_RESTRICTIVE_FIELDS = {"cooldown_seconds"}


def _validate_not_less_restrictive(
    new_limits: dict,
    inherited_floor: Optional[dict],
    path: str = "",
) -> List[str]:
    """
    Validate that new_limits are not less restrictive than the inherited floor.
    Returns a list of violation messages.
    """
    if not inherited_floor:
        return []

    violations = []
    for key, floor_val in inherited_floor.items():
        field_path = f"{path}.{key}" if path else key
        new_val = new_limits.get(key)

        if new_val is None:
            continue  # Not setting this field — OK

        if isinstance(floor_val, dict) and isinstance(new_val, dict):
            violations.extend(_validate_not_less_restrictive(new_val, floor_val, field_path))
        elif isinstance(floor_val, bool) and isinstance(new_val, bool):
            # Bool branch MUST run before the int/float branch because
            # `isinstance(True, int)` is True in Python (bool ⊂ int).
            # Without this ordering, hard_stop=True parent vs hard_stop=False
            # child would silently slip through as a numeric comparison
            # (False=0 > True=1 is false → no violation flagged).
            if floor_val and not new_val:
                violations.append(
                    f"{field_path}: cannot disable — enforced by parent group"
                )
        elif isinstance(floor_val, (int, float)) and isinstance(new_val, (int, float)):
            if key in _INVERTED_RESTRICTIVE_FIELDS:
                # Higher = more restrictive (cooldown_seconds). A child
                # with a SHORTER cooldown is less restrictive than its
                # parent and must be rejected.
                if new_val < floor_val:
                    violations.append(
                        f"{field_path}: value {new_val} is less restrictive than "
                        f"inherited {floor_val} (longer cooldown = stricter)"
                    )
            elif new_val > floor_val:
                violations.append(
                    f"{field_path}: value {new_val} exceeds inherited limit {floor_val} from parent"
                )

    return violations


# ── Governance: which fields can be *relaxed*, and what "looser" means ──
#
# Limits and governance are both restrictive-merge structures, but the
# comparison is not the same. A limit is a number where lower is stricter;
# a governance field can be a number, an enum with its own strictness
# ordering, a boolean that ANDs, or a list that merges by intersection.
# The `*_restrictive_merge` functions in `resource_group.py` are the
# authority on those semantics, and the rule tables below mirror them
# field for field.
#
# Two rules keep this table honest:
#
#   1. Only fields the merge actually restricts appear here — refusing a
#      value the runtime never constrains would be a false alarm. The
#      skill table lists all five schema fields because the merge now
#      restricts all five; it previously covered three, which is how the
#      Python and Rust sides drifted apart.
#   2. Fields that merge by UNION (blocked_tools, banned_patterns,
#      banned_argument_patterns, mandatory_fragments, deny_classifications)
#      are absent too: a child can only ever add to them, so no child
#      value can be a relaxation.

#: How a field is compared against the value inherited from ancestors.
_GOV_MIN_WINS = "min_wins"          # lower is stricter
_GOV_STRICTNESS = "strictness"      # enum with an explicit ordering
_GOV_BOOL_AND = "bool_and"          # False is stricter; child may not re-enable
_GOV_LIST_SUBSET = "list_subset"    # merges by intersection; extras are dropped

#: (path, rule, ordering, operator-facing label). `path` is dotted; a
#: single segment means the governance blob is flat (MCP, skill).
_GOVERNANCE_RULES: Dict[str, tuple] = {
    "llm_global_governance": (
        ("temperature.max", _GOV_MIN_WINS, None, "Max Temperature"),
        ("temperature.enforce", _GOV_STRICTNESS, _ENFORCE_STRICTNESS, "Temperature Enforce Mode"),
        ("context_window.max_input_tokens", _GOV_MIN_WINS, None, "Max Input Tokens"),
        ("context_window.max_output_tokens", _GOV_MIN_WINS, None, "Max Output Tokens"),
        ("context_window.enforce", _GOV_STRICTNESS, _ENFORCE_STRICTNESS, "Context Window Enforce Mode"),
        ("tool_use.access", _GOV_STRICTNESS, _ACCESS_STRICTNESS, "Tool Access"),
        ("tool_use.approval_timeout_seconds", _GOV_MIN_WINS, None, "Approval Timeout"),
        ("tool_use.max_tool_calls_per_request", _GOV_MIN_WINS, None, "Max Tool Calls per Request"),
        ("tool_use.max_tool_calls_per_hour", _GOV_MIN_WINS, None, "Max Tool Calls per Hour"),
        ("tool_use.max_tool_calls_per_day", _GOV_MIN_WINS, None, "Max Tool Calls per Day"),
        ("tool_use.allowed_tools", _GOV_LIST_SUBSET, None, "Allowed Tools"),
    ),
    "mcp_global_governance": (
        ("default_tool_access", _GOV_STRICTNESS, _MCP_ACCESS_STRICTNESS, "Default Tool Access"),
        ("default_resource_access", _GOV_STRICTNESS, _MCP_ACCESS_STRICTNESS, "Default Resource Access"),
        ("default_prompt_access", _GOV_STRICTNESS, _MCP_ACCESS_STRICTNESS, "Default Prompt Access"),
        ("default_resource_template_access", _GOV_STRICTNESS, _MCP_ACCESS_STRICTNESS, "Default Resource Template Access"),
        ("approval_timeout_seconds", _GOV_MIN_WINS, None, "Approval Timeout"),
    ),
    "skill_global_governance": (
        ("execution_mode", _GOV_STRICTNESS, _SKILL_EXECUTION_STRICTNESS, "Execution Mode"),
        ("discovery_enabled", _GOV_BOOL_AND, None, "Skill Discovery"),
        ("approval_timeout_seconds", _GOV_MIN_WINS, None, "Approval Timeout"),
        ("max_concurrent_executions", _GOV_MIN_WINS, None, "Max Concurrent Executions"),
        ("max_tool_calls_per_execution", _GOV_MIN_WINS, None, "Max Tool Calls per Execution"),
    ),
}


def _compute_inherited_media_governance(group: ResourceGroup) -> Optional[dict]:
    """Media governance merged across ancestors only (excluding self), as
    the proxy walks the chain."""
    return _merge_governance_child_first(
        [getattr(a, 'media_global_governance', None) for a in group.get_ancestors()],
        _media_governance_restrictive_merge,
    )


def _compute_inherited_skill_governance(group: ResourceGroup) -> Optional[dict]:
    """Skill governance merged across ancestors only (excluding self), as
    the proxy walks the chain."""
    return _merge_governance_child_first(
        [getattr(a, 'skill_global_governance', None) for a in group.get_ancestors()],
        _skill_governance_restrictive_merge,
    )


def _validate_media_governance_not_less_restrictive(
    new_governance: Optional[dict],
    inherited_governance: Optional[dict],
) -> List[str]:
    """
    Media governance is a ``{mode: {field: value}}`` document with per-field
    rules, so it needs its own comparison rather than the flat rule table.

    A child relaxes when it raises a ceiling, lowers the `speed_min` floor,
    drops a requirement a parent set, or widens a whitelist beyond what the
    parent allows (whitelists intersect, so the extra entries never apply).
    """
    if not new_governance or not inherited_governance:
        return []

    violations: List[str] = []
    for mode, inherited_fields in inherited_governance.items():
        new_fields = new_governance.get(mode)
        if not isinstance(new_fields, dict) or not isinstance(inherited_fields, dict):
            continue
        for field, floor_val in inherited_fields.items():
            rule = MEDIA_GOVERNANCE_FIELD_RULES.get(field)
            if rule is None or field not in new_fields:
                continue
            new_val = new_fields[field]
            if new_val is None or floor_val is None:
                continue
            label = f"{mode}.{field}"

            if rule == 'whitelist':
                if isinstance(new_val, list) and isinstance(floor_val, list) and floor_val:
                    extra = [x for x in new_val if x not in floor_val]
                    if extra:
                        violations.append(
                            f"{label}: {', '.join(str(e) for e in extra)} "
                            f"not permitted by the inherited policy"
                        )
            elif rule == 'bool_or':
                if floor_val is True and new_val is False:
                    violations.append(f"{label}: cannot switch off — required by a parent group")
            elif isinstance(new_val, bool) or isinstance(floor_val, bool):
                continue
            elif isinstance(new_val, (int, float)) and isinstance(floor_val, (int, float)):
                if rule == 'ceiling' and new_val > floor_val:
                    violations.append(
                        f"{label}: {new_val} is looser than the inherited maximum of {floor_val}"
                    )
                elif rule == 'floor' and new_val < floor_val:
                    violations.append(
                        f"{label}: {new_val} is looser than the inherited minimum of {floor_val}"
                    )
    return violations


def _dotted_get(blob: Optional[dict], path: str):
    """Read a dotted path out of a governance blob, or None."""
    node: Any = blob
    for segment in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(segment)
    return node


def _validate_governance_not_less_restrictive(
    new_governance: Optional[dict],
    inherited_governance: Optional[dict],
    attr_name: str,
) -> List[str]:
    """
    Validate that `new_governance` does not relax any value this group
    inherits from its ancestors. Returns operator-readable violations.

    Governance is min-wins at runtime, so a looser child value changes
    nothing about enforcement — which is exactly why it must be refused
    rather than stored. A saved policy the runtime discards is a false
    record: it reads like a decision on the group's page, it travels into
    exports and sealed contracts, and it silently becomes real the day an
    ancestor relaxes its own value. The console shows the inherited
    ceiling on every field, so a refusal here is always actionable.
    """
    if not new_governance or not inherited_governance:
        return []

    violations: List[str] = []
    for path, rule, ordering, label in _GOVERNANCE_RULES.get(attr_name, ()):
        new_val = _dotted_get(new_governance, path)
        floor_val = _dotted_get(inherited_governance, path)
        if new_val is None or floor_val is None:
            continue

        if rule == _GOV_MIN_WINS:
            # bool is a subclass of int in Python — never compare one here.
            if isinstance(new_val, bool) or isinstance(floor_val, bool):
                continue
            if not isinstance(new_val, (int, float)) or not isinstance(floor_val, (int, float)):
                continue
            if new_val > floor_val:
                violations.append(
                    f"{label}: {new_val} is looser than the inherited maximum of {floor_val}"
                )

        elif rule == _GOV_STRICTNESS:
            if ordering.rank(new_val) < ordering.rank(floor_val):
                violations.append(
                    f"{label}: '{new_val}' is looser than the inherited '{floor_val}'"
                )

        elif rule == _GOV_BOOL_AND:
            if floor_val is False and new_val is True:
                violations.append(
                    f"{label}: cannot re-enable — disabled by a parent group"
                )

        elif rule == _GOV_LIST_SUBSET:
            if isinstance(new_val, list) and isinstance(floor_val, list):
                extra = sorted(set(new_val) - set(floor_val))
                if extra:
                    violations.append(
                        f"{label}: {', '.join(str(e) for e in extra)} "
                        f"not allowed by the inherited policy"
                    )

    return violations


# Fields that are policy flags / wait-times, not summable budget quantities.
# `cooldown_seconds` lives here because cooldowns don't *allocate* — each
# scope owns its own lockout; summing siblings' cooldowns against the parent
# is meaningless.
_NON_SUMMABLE_FIELDS = {
    "hard_stop",
    "daily_warning_percent",
    "monthly_warning_percent",
    "cooldown_seconds",
}


def _resolve_budget_holder(
    group: ResourceGroup,
    attr_name: str,
    db: Session,
) -> Optional[tuple]:
    """
    The group whose budget this group draws on, and the other groups drawing
    on it. Returns ``(holder, sharers)`` or ``None`` when no ancestor sets
    limits.

    The holder is the nearest ancestor that actually sets limits, not simply
    the immediate parent: estates commonly set a budget once at the
    organisation and leave the departments between it and an AI system
    unset. Anchoring on the parent meant both the capacity display and the
    sibling-sum check went silent at exactly the depth operators configure.

    The sharers are the holder's direct children minus the one on this
    group's own path — that child *is* this group's branch, so counting its
    allocation as somebody else's claim would double-book the branch
    against itself.

    `_compute_capacity_info` (what the console shows) and
    `_validate_siblings_sum` (what the API refuses) both resolve through
    here, so the warning an operator sees while typing is a faithful
    prediction of the refusal they would get on save.
    """
    ancestors = group.get_ancestors()  # root → ... → parent (excludes self)
    if not ancestors:
        return None

    holder = None
    for candidate in reversed(ancestors):  # nearest first
        if getattr(candidate, attr_name, None):
            holder = candidate
            break
    if holder is None:
        return None

    path_ids = {a.id for a in ancestors} | {group.id}
    children = db.query(ResourceGroup).filter(
        ResourceGroup.parent_group_id == holder.id,
        ResourceGroup.tenant_id == group.tenant_id,
        ResourceGroup.is_active == True,
    ).all()
    sharers = [c for c in children if c.id not in path_ids]
    return holder, sharers


def _validate_siblings_sum(
    group: ResourceGroup,
    new_limits: dict,
    attr_name: str,
    db: Session,
) -> List[str]:
    """
    Validate that this group's proposed limits plus every other group
    drawing on the same budget do not exceed that budget, for any summable
    numeric metric.

    Only summable budget fields are checked (amounts, counts), not policy
    flags (`hard_stop`, warning percents) or per-scope wait times
    (`cooldown_seconds`), which do not allocate.
    """
    resolved = _resolve_budget_holder(group, attr_name, db)
    if resolved is None:
        return []
    holder, sharers = resolved

    holder_limits = getattr(holder, attr_name, None)
    if not holder_limits:
        return []

    sharer_limits_list = [getattr(s, attr_name, None) for s in sharers]
    return _check_sibling_sum_against_parent(
        new_limits, sharer_limits_list, holder_limits
    )


def _check_sibling_sum_against_parent(
    new_limits: dict,
    sibling_limits_list: List[Optional[dict]],
    parent_limits: dict,
    path: str = "",
) -> List[str]:
    """
    Recursively check that sum of new_limits + siblings doesn't exceed parent
    for any numeric budget field.
    """
    violations = []

    for key, parent_val in parent_limits.items():
        if key in _NON_SUMMABLE_FIELDS:
            continue

        field_path = f"{path}.{key}" if path else key
        new_val = new_limits.get(key)

        if isinstance(parent_val, dict):
            # Recurse into nested objects
            if isinstance(new_val, dict):
                sibling_sub = [
                    (s.get(key) if isinstance(s, dict) and s else None)
                    for s in sibling_limits_list
                ]
                violations.extend(_check_sibling_sum_against_parent(
                    new_val, sibling_sub, parent_val, field_path
                ))
        elif isinstance(parent_val, (int, float)) and isinstance(new_val, (int, float)):
            if parent_val <= 0:
                continue  # Parent has no limit for this metric

            # Sum this group's proposed value + all siblings' values
            sibling_sum = 0
            for s_limits in sibling_limits_list:
                if isinstance(s_limits, dict):
                    s_val = s_limits.get(key)
                    if isinstance(s_val, (int, float)):
                        sibling_sum += s_val

            total = new_val + sibling_sum
            if total > parent_val:
                violations.append(
                    f"{field_path}: sum of child limits ({total}) exceeds parent limit ({parent_val})"
                )

    return violations


def _require_readable_mcp_limits(limits: Optional[Dict[str, Any]]) -> None:
    """Refuse (400) an MCP limits document the gateway could not read: it
    would refuse every call of the group to the server."""
    is_valid, error_message = ResourceGroup.validate_mcp_limits_structure(limits)
    if not is_valid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid MCP limits: {error_message}")


def _reject_item_limits_above_group(
    new_limits: Optional[dict],
    effective_group_limits: Optional[dict],
    item_label: str,
) -> None:
    """
    Refuse a per-item limit looser than the group's effective ceiling.

    A per-model, per-server or per-skill limit merges with the group's own
    limits most-restrictive-wins, so a per-item value above the group's
    ceiling never applies. Storing it anyway leaves a number on the item's
    dialog that the runtime discards, and turns real the day the group is
    relaxed — the same failure the group-wide endpoints have refused since
    they shipped, one level further down.

    Over-*allocation* between items is deliberately not refused here: the
    per-item dialogs warn when the sum exceeds the group's budget, but
    sharing one pool between several models is a normal arrangement and the
    group ceiling still binds at request time.
    """
    violations = _validate_not_less_restrictive(new_limits or {}, effective_group_limits)
    if violations:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Cannot set {item_label} limits looser than the group's effective "
                f"limits: {'; '.join(violations)}"
            ),
        )


def _reject_item_governance_above_group(
    new_governance: Optional[dict],
    effective_group_governance: Optional[dict],
    attr_name: str,
    item_label: str,
) -> None:
    """
    Refuse per-item governance looser than the group's effective policy.

    Same reasoning as `_reject_item_limits_above_group`, applied to the
    policy documents, using the same rule table the group-wide endpoints
    use so the two levels cannot disagree about what "looser" means.
    """
    relaxations = _validate_governance_not_less_restrictive(
        new_governance, effective_group_governance, attr_name
    )
    if relaxations:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Cannot set {item_label} governance looser than the group's effective "
                f"policy: {'; '.join(relaxations)}"
            ),
        )


# =========================================================================
# CAPACITY INFO HELPERS (sibling allocation data)
# =========================================================================

_SKIP_SUM_FIELDS = {
    "hard_stop",
    "daily_warning_percent",
    "monthly_warning_percent",
    # Cooldowns aren't allocations — each scope owns its own lockout time.
    # The capacity view sums child *allocations* against the parent's
    # ceiling; summing cooldowns produces a nonsense "available" number.
    "cooldown_seconds",
}


def _compute_capacity_info(
    group: ResourceGroup,
    attr_name: str,
    db: Session,
) -> Optional[LimitCapacityInfo]:
    """
    The budget this group draws on, what the other groups drawing on it have
    claimed, and what is left. Resolves the holder and the sharers through
    `_resolve_budget_holder`, the same way `_validate_siblings_sum` does, so
    the console cannot show one arithmetic while the API enforces another.
    """
    resolved = _resolve_budget_holder(group, attr_name, db)
    if resolved is None:
        return None
    holder, sharers = resolved

    parent_limits = getattr(holder, attr_name, None)

    sibling_infos = [
        SiblingLimitInfo(
            group_id=s.id,
            group_name=s.display_name or s.name,
            limits=getattr(s, attr_name, None),
        )
        for s in sharers
    ]

    sibling_total = _sum_numeric_limits([getattr(s, attr_name, None) for s in sharers])
    available = _subtract_limits(parent_limits, sibling_total) if sibling_total else parent_limits

    return LimitCapacityInfo(
        parent_limit=parent_limits,
        parent_name=holder.display_name or holder.name,
        parent_is_immediate=(holder.id == group.parent_group_id),
        siblings=sibling_infos,
        sibling_total=sibling_total,
        available=available,
    )


def _sum_numeric_limits(limits_list: list) -> Optional[dict]:
    """Sum numeric values across a list of limit dicts, recursively."""
    result = {}
    for limits in limits_list:
        if not isinstance(limits, dict):
            continue
        _add_limits(result, limits)
    return result if result else None


def _add_limits(target: dict, source: dict) -> None:
    """Add numeric values from source into target, recursively."""
    for key, val in source.items():
        if key in _SKIP_SUM_FIELDS:
            continue
        if isinstance(val, dict):
            if key not in target:
                target[key] = {}
            if isinstance(target[key], dict):
                _add_limits(target[key], val)
        elif isinstance(val, (int, float)):
            target[key] = target.get(key, 0) + val


def _subtract_limits(parent: dict, sibling_total: dict) -> dict:
    """Subtract sibling_total from parent to get available capacity."""
    result = {}
    for key, parent_val in parent.items():
        if key in _SKIP_SUM_FIELDS:
            continue
        sib_val = sibling_total.get(key)
        if isinstance(parent_val, dict):
            if isinstance(sib_val, dict):
                result[key] = _subtract_limits(parent_val, sib_val)
            else:
                result[key] = parent_val
        elif isinstance(parent_val, (int, float)):
            if isinstance(sib_val, (int, float)):
                result[key] = max(0, parent_val - sib_val)
            else:
                result[key] = parent_val
    return result


def _names_by_id(db: Session, model, tenant_id: str, ids) -> dict:
    """`{id: name}` for one tenant's rows, in one round trip.

    The three capacity helpers below each walked an association table and
    resolved one display name per row with its own SELECT — a group with 40
    bound models was 41 queries to answer a question one `IN` clause
    answers. Missing ids are simply absent; callers fall back to the id.
    """
    ids = [i for i in dict.fromkeys(ids) if i]
    if not ids:
        return {}
    return {
        row_id: name
        for row_id, name in db.query(model.id, model.name).filter(
            model.id.in_(ids), model.tenant_id == tenant_id
        )
    }


def _compute_per_model_capacity_info(
    group: ResourceGroup,
    model_id: str,
    db: Session,
) -> Optional[PerModelCapacityInfo]:
    """Compute capacity for a per-model limit: group global - other models' allocations."""
    from app.database.models.llm_model import LLModel

    effective_global = group.get_effective_llm_limits()
    if not effective_global:
        return None

    # Get all other models in this group with per-model limits
    rows = db.execute(
        resource_group_llm_models.select().where(
            resource_group_llm_models.c.resource_group_id == group.id,
            resource_group_llm_models.c.llm_model_id != model_id,
        )
    ).fetchall()

    other_models = []
    other_limits_list = []
    names = _names_by_id(
        db, LLModel, group.tenant_id,
        [r.llm_model_id for r in rows if r.limits],
    )
    for row in rows:
        if row.limits:
            model_name = names.get(row.llm_model_id, row.llm_model_id)
            other_models.append(ModelLimitInfo(
                llm_model_id=row.llm_model_id,
                llm_model_name=model_name,
                limits=row.limits,
            ))
            other_limits_list.append(row.limits)

    if not other_limits_list and not effective_global:
        return None

    other_total = _sum_numeric_limits(other_limits_list)
    available = _subtract_limits(effective_global, other_total) if other_total else effective_global

    return PerModelCapacityInfo(
        group_global_limit=effective_global,
        group_name=group.display_name or group.name,
        other_models=other_models,
        other_models_total=other_total,
        available=available,
    )


def _compute_per_server_capacity_info(
    group: ResourceGroup,
    server_config_id: str,
    db: Session,
) -> Optional[PerServerCapacityInfo]:
    """Compute capacity for a per-server limit: group global MCP - other servers' allocations."""
    effective_global = group.get_effective_mcp_limits()
    if not effective_global:
        return None

    rows = db.execute(
        resource_group_server_configs.select().where(
            resource_group_server_configs.c.resource_group_id == group.id,
            resource_group_server_configs.c.server_config_id != server_config_id,
        )
    ).fetchall()

    from app.database.models.server_config import ServerConfig

    other_servers = []
    other_limits_list = []
    names = _names_by_id(
        db, ServerConfig, group.tenant_id,
        [r.server_config_id for r in rows if r.limits],
    )
    for row in rows:
        if row.limits:
            name = names.get(row.server_config_id, row.server_config_id)
            other_servers.append(ServerLimitInfo(
                server_config_id=row.server_config_id,
                server_name=name,
                limits=row.limits,
            ))
            other_limits_list.append(row.limits)

    if not other_limits_list and not effective_global:
        return None

    other_total = _sum_numeric_limits(other_limits_list)
    available = _subtract_limits(effective_global, other_total) if other_total else effective_global

    return PerServerCapacityInfo(
        group_global_limit=effective_global,
        group_name=group.display_name or group.name,
        other_servers=other_servers,
        other_servers_total=other_total,
        available=available,
    )


def _compute_per_skill_capacity_info(
    group: ResourceGroup,
    skill_id: str,
    db: Session,
) -> Optional[PerSkillCapacityInfo]:
    """Compute capacity for a per-skill limit: group global skill - other skills' allocations."""
    effective_global = group.get_effective_skill_limits()
    if not effective_global:
        return None

    from app.database.models.agent_skill import AgentSkill, resource_group_agent_skills

    rows = db.execute(
        resource_group_agent_skills.select().where(
            resource_group_agent_skills.c.resource_group_id == group.id,
            resource_group_agent_skills.c.agent_skill_id != skill_id,
        )
    ).fetchall()

    other_skills = []
    other_limits_list = []
    names = _names_by_id(
        db, AgentSkill, group.tenant_id,
        [r.agent_skill_id for r in rows if r.limits],
    )
    for row in rows:
        if row.limits:
            name = names.get(row.agent_skill_id, row.agent_skill_id)
            other_skills.append(SkillLimitInfo(
                agent_skill_id=row.agent_skill_id,
                skill_name=name,
                limits=row.limits,
            ))
            other_limits_list.append(row.limits)

    if not other_limits_list and not effective_global:
        return None

    other_total = _sum_numeric_limits(other_limits_list)
    available = _subtract_limits(effective_global, other_total) if other_total else effective_global

    return PerSkillCapacityInfo(
        group_global_limit=effective_global,
        group_name=group.display_name or group.name,
        other_skills=other_skills,
        other_skills_total=other_total,
        available=available,
    )


def _compute_inherited_model_governance(group: ResourceGroup, model_id: str, db: Session) -> Optional[dict]:
    """
    Compute the inherited governance floor for a specific model from ancestors
    (excluding the group itself). Queries resource_group_llm_governance at each level.
    Uses governance-specific restrictive merge.
    """
    ancestors = group.get_ancestors()  # root → ... → parent (excludes self)
    if not ancestors:
        return None

    effective = None
    for ancestor in ancestors:
        row = db.execute(
            resource_group_llm_governance.select().where(
                and_(
                    resource_group_llm_governance.c.resource_group_id == ancestor.id,
                    resource_group_llm_governance.c.llm_model_id == model_id,
                )
            )
        ).fetchone()
        if row and row.governance:
            gov = row.governance if isinstance(row.governance, dict) else {}
            if effective is None:
                effective = copy.deepcopy(gov)
            else:
                _governance_restrictive_merge(effective, gov)
    return effective


def _compute_effective_model_governance(group: ResourceGroup, model_id: str, db: Session) -> Optional[dict]:
    """
    Compute effective governance for a specific model by walking
    the full ancestor chain (root → ... → self) and applying restrictive merge.

    The effective governance is: governance_restrictive_merge(effective_global_from_hierarchy, per_model_from_hierarchy)
    - First, compute the group's effective llm_global_governance (ceiling)
    - Then, merge per-model governance from ancestors (tightens within that ceiling)
    """
    # Step 1: Get effective global LLM governance from group hierarchy (ceiling)
    effective_global = group.get_effective_llm_governance()

    # Step 2: Walk ancestor chain for per-model governance
    chain = group.get_ancestors(include_self=True)  # root → ... → self
    per_model_effective = None
    for g in chain:
        row = db.execute(
            resource_group_llm_governance.select().where(
                and_(
                    resource_group_llm_governance.c.resource_group_id == g.id,
                    resource_group_llm_governance.c.llm_model_id == model_id,
                )
            )
        ).fetchone()
        if row and row.governance:
            gov = row.governance if isinstance(row.governance, dict) else {}
            if per_model_effective is None:
                per_model_effective = copy.deepcopy(gov)
            else:
                _governance_restrictive_merge(per_model_effective, gov)

    # Step 3: Merge global ceiling with per-model governance
    if effective_global and per_model_effective:
        result = copy.deepcopy(effective_global)
        _governance_restrictive_merge(result, per_model_effective)
        return result
    elif effective_global:
        return effective_global
    elif per_model_effective:
        return per_model_effective
    return None


def _validated_client_systems(
    db: Session, tenant_id: str, group_id: Optional[str], ids: Optional[List[str]]
) -> Optional[List[str]]:
    """`intended_client_systems` → 422 unless every id is an AI
    System in the tenant other than this group."""
    from app.services.ai_system_composition import (
        DependencyValidationError,
        validate_intended_client_systems,
    )

    try:
        return validate_intended_client_systems(
            db, tenant_id=tenant_id, group_id=group_id, ids=ids
        )
    except DependencyValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _acting_tenant(request: Request, admin_and_type: tuple = Depends(get_current_admin)) -> str:
    """The tenant the request acts on, as a dependency: answered before a
    route's body is validated."""
    return acting_tenant_id(request, admin_and_type[0])


def create_resource_groups_router(
        usage_limit_enforcer: UsageLimitEnforcer,
        logger: BrutorLogger) -> APIRouter:
    """Create router for resource groups management endpoints"""
    router = APIRouter(prefix="/admin/resource-groups", tags=["Admin - Resource Groups"])

    # =========================================================================
    # BASIC CRUD ENDPOINTS
    # =========================================================================

    @router.get("", response_model=ResourceGroupListResponse)
    def list_resource_groups(
        request: Request,
        include_stats: bool = Query(False, description="Include member and config counts"),
        include_resources: bool = Query(False, description="Include server configs and models"),
        include_hierarchy: bool = Query(False, description="Include parent/children information"),
        include_limits: bool = Query(False, description="Include effective limits with inheritance"),
        is_active: Optional[bool] = Query(None, description="Filter by active status"),
        group_type: Optional[GroupType] = Query(None, description="Filter by group type"),
        parent_group_id: Optional[str] = Query(None, description="Filter by parent (null for root groups)"),
        page: int = Query(1, ge=1, description="Page number"),
        page_size: int = Query(50, ge=1, le=100, description="Page size"),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_READ))
    ):
        """
        List all resource groups for the tenant with optional filtering

        Supports filtering by:
        - is_active: Active/inactive status
        - group_type: Organization, department, team, project, application, agent, etc.
        - parent_group_id: Parent group (use 'null' for root groups)
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = getattr(admin, 'tenant_id', None)
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        if not admin_tenant_id:
            return ResourceGroupListResponse(groups=[], total=0)

        try:
            # Build query with tenant scope
            query = db.query(ResourceGroup).filter(ResourceGroup.tenant_id == admin_tenant_id)

            # Apply filters
            if is_active is not None:
                query = query.filter(ResourceGroup.is_active == is_active)

            if group_type:
                query = query.filter(ResourceGroup.group_type == group_type)

            if parent_group_id is not None:
                if parent_group_id.lower() == 'null':
                    # Filter for root groups
                    query = query.filter(ResourceGroup.parent_group_id.is_(None))
                else:
                    query = query.filter(ResourceGroup.parent_group_id == parent_group_id)

            # Count total before pagination
            total = query.count()

            # Apply pagination
            offset = (page - 1) * page_size
            groups = query.order_by(ResourceGroup.hierarchy_level, ResourceGroup.name).offset(offset).limit(page_size).all()

            logger.debug("Resource groups listed", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": admin_tenant_id,
                "group_count": len(groups),
                "total": total,
                "page": page
            })

            # Convert to response format
            group_responses = []
            for group in groups:
                group_dict = group.to_dict(
                    include_stats=include_stats,
                    include_resources=include_resources,
                    include_hierarchy=include_hierarchy,
                    include_limits=include_limits
                )
                group_responses.append(ResourceGroupResponse(**group_dict))

            return ResourceGroupListResponse(
                groups=group_responses,
                total=total
            )

        except Exception as e:
            logger.error("Failed to list resource groups", context={
                "tenant_id": admin_tenant_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve resource groups"
            )

    @router.get("/hierarchy-tree")
    def get_group_hierarchy_tree(
            request: Request,
            db: Session = Depends(get_db),
            admin_and_type: tuple = Depends(get_current_admin),
            admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_READ))
    ):
        """
        Get the complete group hierarchy as a recursive tree structure

        Returns all root groups (parent_group_id = NULL) with their children
        recursively nested. This is used by the frontend for hierarchical
        group selection in server configs and other features.

        Response format:
        {
            "tree": [
                {
                    "id": "group-xxx",
                    "name": "Engineering",
                    "display_name": "Engineering Team",
                    "group_type": "department",
                    "hierarchy_level": 0,
                    "is_active": true,
                    "member_count": 25,
                    "server_config_count": 5,
                    "llm_model_count": 3,
                    "access_key_count": 2,
                    "children": [
                        {
                            "id": "group-yyy",
                            "name": "Frontend",
                            ...
                            "children": [...]
                        }
                    ]
                }
            ]
        }
        """
        admin, admin_type = admin_and_type
        admin_id = admin.id
        admin_tenant_id = getattr(admin, 'tenant_id', None)
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        # System admins have no tenant_id — return empty tree
        if not admin_tenant_id:
            return {"tree": []}

        try:
            # Get all root groups (no parent)
            root_groups = db.query(ResourceGroup).filter(
                ResourceGroup.tenant_id == admin_tenant_id,
                ResourceGroup.parent_group_id.is_(None)
            ).order_by(ResourceGroup.name).all()

            # Build tree recursively
            tree = []
            for root in root_groups:
                tree_node = _build_group_tree_node(root, db)
                tree.append(tree_node)

            logger.debug("Group hierarchy tree retrieved", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": admin_tenant_id,
                "root_groups_count": len(tree)
            })

            return {"tree": tree}

        except Exception as e:
            logger.error("Failed to retrieve group hierarchy tree", context={
                "tenant_id": admin_tenant_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve group hierarchy tree"
            )

    def _build_group_tree_node(group: ResourceGroup, db: Session) -> dict:
        """
        Recursively build a tree node with children

        Args:
            group: The ResourceGroup database model
            db: Database session

        Returns:
            Dictionary matching GroupHierarchyTree schema with nested children
        """
        # Get direct children of this group
        children = db.query(ResourceGroup).filter(
            ResourceGroup.parent_group_id == group.id,
            ResourceGroup.tenant_id == group.tenant_id
        ).order_by(ResourceGroup.name).all()

        # Count resources — all relationships load as lists, len() works on all
        member_count = group.members.count() if hasattr(group, 'members') else 0
        server_config_count = group.server_configs.count() if hasattr(group, 'server_configs') else 0
        llm_model_count = group.llm_models.count() if hasattr(group, 'llm_models') else 0
        api_key_count = group.api_keys.count() if hasattr(group, 'api_keys') else 0

        # Build child nodes recursively
        child_nodes = [_build_group_tree_node(child, db) for child in children]

        return {
            "id": group.id,
            "name": group.name,
            "display_name": group.display_name,
            "group_type": group.group_type.value if group.group_type else None,
            "hierarchy_level": group.hierarchy_level,
            "parent_group_id": group.parent_group_id,
            "inherit_resources": group.inherit_resources,
            "is_active": group.is_active,
            "icon": group.icon,
            "color": group.color,
            "member_count": member_count,
            "server_config_count": server_config_count,
            "llm_model_count": llm_model_count,
            "api_key_count": api_key_count,
            "children": child_nodes
        }

    @router.post("", response_model=ResourceGroupResponse, status_code=status.HTTP_201_CREATED)
    def create_resource_group(
        group_data: ResourceGroupCreate,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_CREATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """
        Create a new resource group

        Supports flexible group types:
        - Organizational: organization, division, department, business_unit, team
        - Work: project, program, initiative
        - AI/Technical: application, agent, assistant, service
        - Custom: custom (default)
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)
        admin_id = admin.id

        try:
            # Verify parent group exists if specified
            if group_data.parent_group_id:
                parent_group = db.query(ResourceGroup).filter(
                    ResourceGroup.id == group_data.parent_group_id,
                    ResourceGroup.tenant_id == admin_tenant_id
                ).first()

                if not parent_group:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Parent group '{group_data.parent_group_id}' not found"
                    )

                # Calculate hierarchy info
                hierarchy_level = parent_group.hierarchy_level + 1
                if parent_group.hierarchy_path:
                    hierarchy_path = f"{parent_group.hierarchy_path}/{group_data.name}"
                else:
                    hierarchy_path = f"{parent_group.name}/{group_data.name}"
            else:
                hierarchy_level = 0
                hierarchy_path = group_data.name

            # An AI System's lifecycle stage is gated.
            # PATCH already refuses to move it (see update_resource_group);
            # without this, POST was the back door: a system could be born
            # `active` with no contract, no approver and no replay, and the
            # fleet board would report it as governed. Every system starts
            # at `proposed`; the lifecycle endpoint moves it from there.
            # Other group types keep `lifecycle_stage` as documentation.
            if (
                group_data.group_type == GroupType.AI_SYSTEM
                and group_data.lifecycle_stage not in (None, LIFECYCLE_PROPOSED)
            ):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "An AI System's lifecycle stage is gated on evidence. "
                        f"Create it as '{LIFECYCLE_PROPOSED}' (or omit "
                        "lifecycle_stage), then use "
                        "POST /v1/admin/ai-systems/{id}/lifecycle, which "
                        "reports what each transition requires and what is "
                        "missing."
                    ),
                )

            # Validate limits if provided
            if group_data.llm_global_limits:
                # You can add validation logic here
                pass

            _require_readable_mcp_limits(group_data.mcp_global_limits)

            # Intended client systems must be AI Systems in this
            # tenant. Normalised (sorted, de-duplicated) so the contract
            # closure hashes a set, not the order it was ticked in.
            group_data.intended_client_systems = _validated_client_systems(
                db, admin_tenant_id, None, group_data.intended_client_systems
            )

            # Sibling-name uniqueness. There is no DB unique constraint on
            # (tenant, parent, name), so it is enforced here: duplicates
            # would produce identical hierarchy_paths.
            dup = db.query(ResourceGroup.id).filter(
                ResourceGroup.tenant_id == admin_tenant_id,
                ResourceGroup.parent_group_id == group_data.parent_group_id,
                ResourceGroup.name == group_data.name,
            ).first()
            if dup:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="A group with this name already exists in the parent group",
                )

            # Create new group
            new_group = ResourceGroup(
                id=f"group-{ULID()}",
                tenant_id=admin_tenant_id,
                name=group_data.name,
                display_name=group_data.display_name,
                description=group_data.description,
                group_type=group_data.group_type or GroupType.CUSTOM,
                # The request model has already defaulted this
                # to 'agent' for AI Systems and rejected it for anything else.
                system_kind=group_data.system_kind.value if group_data.system_kind else None,
                # A new AI System starts with the editable
                # starter outcome catalogue (task_completed /
                # nothing_to_do), approved with its first contract.
                declared_outcomes=(
                    starter_catalogue()
                    if group_data.group_type == GroupType.AI_SYSTEM
                    else None
                ),
                parent_group_id=group_data.parent_group_id,
                hierarchy_level=hierarchy_level,
                hierarchy_path=hierarchy_path,
                icon=group_data.icon,
                color=group_data.color,
                # Governance / ownership (AI-System attributes on the RG)
                owner=group_data.owner,
                intended_use=group_data.intended_use,
                intended_client_labels=group_data.intended_client_labels,
                intended_client_systems=group_data.intended_client_systems,
                enforce_accepted_callers=bool(group_data.enforce_accepted_callers),
                lifecycle_stage=group_data.lifecycle_stage,
                autonomy_level=group_data.autonomy_level,
                **({'version': group_data.version} if group_data.version else {}),
                llm_global_limits=group_data.llm_global_limits,
                mcp_global_limits=group_data.mcp_global_limits,
                # Compliance posture is set at
                # group creation when the operator already knows
                # this group will host (say) a high-risk EU AI Act
                # application. Otherwise it stays null and is set
                # later via the Governance → Compliance sub-tab.
                eu_ai_act_risk_tier=group_data.eu_ai_act_risk_tier,
                eu_ai_act_role=group_data.eu_ai_act_role,
                phi_resource=group_data.phi_resource,
                sensitive_data=bool(group_data.sensitive_data),
                is_active=group_data.is_active,
                inherit_resources=group_data.inherit_resources,
                created_by=admin_id,
                updated_by=admin_id
            )

            db.add(new_group)
            db.flush()

            block_on(invalidate_proxy_cache(request, "resource_group", admin_tenant_id, new_group.id, action="create"))

            logger.info("Resource group created", context={
                "group_id": new_group.id,
                "group_name": new_group.name,
                "group_type": new_group.group_type.value if new_group.group_type else None,
                "tenant_id": admin_tenant_id,
                "admin_id": admin_id
            })

            # Audit log
            audit.log(
                action="resource_group_create",
                admin=admin,
                admin_type=admin_type,
                tenant_id=admin_tenant_id,
                success=True,
                resource_type=TenantResourceTypes.RESOURCE_GROUPS.value,
                resource_id=new_group.id,
                details={
                    "group_id": new_group.id,
                    "group_name": new_group.name,
                    "group_type": new_group.group_type.value if new_group.group_type else None,
                    "parent_group_id": new_group.parent_group_id
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            return ResourceGroupResponse(**new_group.to_dict())

        except HTTPException:
            raise
        except IntegrityError as e:
            logger.error("Group creation failed - integrity error", context={
                "error": str(e),
                "tenant_id": admin_tenant_id
            })
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A group with this name already exists in the parent group"
            )
        except Exception as e:
            logger.error("Failed to create resource group", context={
                "error": str(e),
                "tenant_id": admin_tenant_id
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create resource group"
            )

    @router.get("/roots", response_model=ResourceGroupListResponse)
    def get_root_groups(
        request: Request,
        include_stats: bool = Query(False),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_READ))
    ):
        """
        Get all root groups (groups with no parent)
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            roots = db.query(ResourceGroup).filter(
                ResourceGroup.tenant_id == admin_tenant_id,
                ResourceGroup.parent_group_id.is_(None)
            ).order_by(ResourceGroup.name).all()

            group_responses = []
            for group in roots:
                group_dict = group.to_dict(include_stats=include_stats)
                group_responses.append(ResourceGroupResponse(**group_dict))

            return ResourceGroupListResponse(
                groups=group_responses,
                total=len(group_responses)
            )

        except Exception as e:
            logger.error("Failed to get root groups", context={
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve root groups"
            )

    @router.get("/tree", response_model=List[GroupHierarchyTree])
    def get_hierarchy_tree(
        request: Request,
        root_group_id: Optional[str] = Query(None, description="Start from specific group"),
        max_depth: Optional[int] = Query(None, ge=1, le=10, description="Maximum depth"),
        include_stats: bool = Query(True, description="Include member/config counts"),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_READ))
    ):
        """
        Get entire group hierarchy as tree structure

        Returns nested tree structure suitable for UI display.
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = getattr(admin, 'tenant_id', None)

        if not admin_tenant_id:
            return []

        def build_tree(
            group: ResourceGroup,
            current_depth: int = 0,
            ancestor_limits: bool = False,
            ancestor_policy: bool = False,
        ) -> GroupHierarchyTree:
            """Recursively build tree structure.

            `ancestor_limits` / `ancestor_policy` are threaded down rather
            than recomputed per node: each child only needs to know whether
            anything above it governs, and the parent already knows.

            When the caller asks for a subtree (`root_group_id`), the seed
            values come from that root's own ancestors, so a subtree view
            does not report a group as ungoverned merely because the group
            that governs it is out of frame.
            """
            own_limits = bool(
                group.llm_global_limits
                or group.mcp_global_limits
                or group.skill_global_limits
                or group.media_global_limits
                or group.a2a_global_limits
            )
            own_policy = bool(
                group.llm_global_governance
                or group.mcp_global_governance
                or group.skill_global_governance
                or group.media_global_governance
                or group.a2a_global_governance
            )

            children_trees = []

            # Check depth limit
            if max_depth is None or current_depth < max_depth:
                for child in list(group.children):
                    children_trees.append(build_tree(
                        child,
                        current_depth + 1,
                        ancestor_limits or own_limits,
                        ancestor_policy or own_policy,
                    ))

            return GroupHierarchyTree(
                sets_own_limits=own_limits,
                sets_own_policy=own_policy,
                inherits_limits=ancestor_limits,
                inherits_policy=ancestor_policy,
                id=group.id,
                name=group.name,
                display_name=group.display_name,
                group_type=group.group_type,
                system_kind=group.system_kind,
                hierarchy_level=group.hierarchy_level,
                parent_group_id=group.parent_group_id,
                inherit_resources=group.inherit_resources,
                is_active=group.is_active,
                icon=group.icon,
                color=group.color,
                member_count=group.get_member_count() if include_stats else 0,
                server_config_count=group.get_server_config_count() if include_stats else 0,
                llm_model_count=group.get_llm_model_count() if include_stats else 0,
                access_key_count=group.get_access_key_count() if include_stats else 0,
                children=children_trees
            )

        try:
            if root_group_id:
                # Start from specific group
                root = db.query(ResourceGroup).filter(
                    ResourceGroup.id == root_group_id,
                    ResourceGroup.tenant_id == admin_tenant_id
                ).first()

                if not root:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Root group '{root_group_id}' not found"
                    )

                # A subtree view must not report a group as ungoverned just
                # because the group that governs it sits above the root.
                seed_limits = any(
                    a.llm_global_limits or a.mcp_global_limits or a.skill_global_limits
                    or a.media_global_limits or a.a2a_global_limits
                    for a in root.get_ancestors()
                )
                seed_policy = any(
                    a.llm_global_governance or a.mcp_global_governance
                    or a.skill_global_governance or a.media_global_governance
                    or a.a2a_global_governance
                    for a in root.get_ancestors()
                )
                return [build_tree(root, 0, seed_limits, seed_policy)]
            else:
                # Get all root groups
                roots = db.query(ResourceGroup).filter(
                    ResourceGroup.tenant_id == admin_tenant_id,
                    ResourceGroup.parent_group_id.is_(None)
                ).order_by(ResourceGroup.name).all()

                return [build_tree(root) for root in roots]

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to build hierarchy tree", context={
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to build hierarchy tree"
            )

    # =========================================================================
    # EFFECTIVE RESOURCES ENDPOINTS (WITH INHERITANCE)
    # =========================================================================

    @router.get("/stats", response_model=ResourceGroupStatsResponse)
    def get_group_stats(
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_READ))
    ):
        """
        Get overall group statistics
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            # Total groups
            total_groups = db.query(ResourceGroup).filter(
                ResourceGroup.tenant_id == admin_tenant_id
            ).count()

            # Active groups
            active_groups = db.query(ResourceGroup).filter(
                ResourceGroup.tenant_id == admin_tenant_id,
                ResourceGroup.is_active == True
            ).count()

            # Groups by type
            groups_by_type = {}
            for group_type in GroupType:
                count = db.query(ResourceGroup).filter(
                    ResourceGroup.tenant_id == admin_tenant_id,
                    ResourceGroup.group_type == group_type
                ).count()
                if count > 0:
                    groups_by_type[group_type.value] = count

            # Total memberships
            total_memberships = db.query(ResourceGroupMembership).filter(
                ResourceGroupMembership.tenant_id == admin_tenant_id
            ).count()

            # Root groups
            root_groups_count = db.query(ResourceGroup).filter(
                ResourceGroup.tenant_id == admin_tenant_id,
                ResourceGroup.parent_group_id.is_(None)
            ).count()

            return ResourceGroupStatsResponse(
                total_groups=total_groups,
                active_groups=active_groups,
                inactive_groups=total_groups - active_groups,
                groups_by_type=groups_by_type,
                total_memberships=total_memberships,
                root_groups_count=root_groups_count
            )

        except Exception as e:
            logger.error("Failed to get group stats", context={
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve group statistics"
            )

    # =========================================================================
    # USAGE ANALYTICS ENDPOINT
    # =========================================================================

    @router.get(
        "/usage-alerts",
        response_model=TenantAlertsResponse,
        summary="Get all usage alerts across all groups",
        description="Returns usage alerts for all resource groups in the tenant. "
                    "Useful for Admin UI dashboard overview."
    )
    def get_all_alerts(
            request: Request,
            alert_status: Optional[str] = Query(
                None,
                alias="status",
                description="Filter by status: 'active', 'acknowledged', 'resolved'. Default: all statuses."
            ),
            resource_type: Optional[str] = Query(
                None,
                description="Filter by resource type: 'llm' or 'mcp'"
            ),
            alert_type: Optional[str] = Query(
                None,
                description="Filter by alert type: 'warning' or 'hard_stop'"
            ),
            group_id: Optional[str] = Query(
                None,
                description="Optional: filter to a specific group ID"
            ),
            limit: int = Query(100, ge=1, le=500, description="Maximum number of alerts to return"),
            db: Session = Depends(get_db),
            admin_and_type: tuple = Depends(get_current_admin),
            admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_READ))
    ):
        """
        Get all usage alerts across the tenant.

        Returns threshold crossing alerts from all resource groups,
        sorted by severity (hard stops first) then utilization.

        **Examples:**
        - `GET /usage-alerts` — All alerts across all groups
        - `GET /usage-alerts?status=active` — Only unacknowledged alerts
        - `GET /usage-alerts?status=active&type=hard_stop` — Active hard stops only
        - `GET /usage-alerts?group_id=group-xxx` — Alerts for a specific group
        """
        try:
            admin_user, admin_type_val = admin_and_type
            tenant_id = acting_tenant_id(request, admin_user)

            # Validate filter values
            if alert_status:
                valid_statuses = {'active', 'acknowledged', 'resolved'}
                if alert_status not in valid_statuses:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Invalid status '{alert_status}'. Must be one of: {', '.join(sorted(valid_statuses))}"
                    )

            if resource_type:
                try:
                    ProxyType(resource_type)
                except ValueError:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Invalid resource_type '{resource_type}'. Must be one of: {', '.join(t.value for t in ProxyType)}"
                    )

            if alert_type:
                valid_alert_types = {'warning', 'hard_stop'}
                if alert_type not in valid_alert_types:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Invalid alert type '{alert_type}'. Must be 'warning' or 'hard_stop'"
                    )

            # Build query
            from app.database.models.usage_alert import UsageAlert

            query = db.query(UsageAlert).filter(
                UsageAlert.tenant_id == tenant_id
            )

            if alert_status:
                query = query.filter(UsageAlert.status == alert_status)
            if resource_type:
                query = query.filter(UsageAlert.resource_type == resource_type)
            if alert_type:
                query = query.filter(UsageAlert.alert_type == alert_type)
            if group_id:
                query = query.filter(UsageAlert.group_id == group_id)

            alerts = query.order_by(
                UsageAlert.alert_type.desc(),
                UsageAlert.utilization_pct.desc(),
                UsageAlert.created_at.desc()
            ).limit(limit).all()

            active_count = sum(1 for a in alerts if a.status == 'active')
            hard_stop_count = sum(1 for a in alerts if a.alert_type == 'hard_stop' and a.status == 'active')
            groups_affected = len(set(a.group_id for a in alerts))

            return TenantAlertsResponse(
                tenant_id=tenant_id,
                alerts=[a.to_dict() for a in alerts],
                total_count=len(alerts),
                active_count=active_count,
                hard_stop_count=hard_stop_count,
                groups_affected=groups_affected
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get tenant alerts", context={
                "error": str(e),
                "error_type": type(e).__name__
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve usage alerts"
            )

    @router.post(
        "/usage-alerts/{alert_id}/acknowledge",
        response_model=AlertAcknowledgeResponse,
        summary="Acknowledge a usage alert",
        description="Mark a usage alert as acknowledged by the current admin user."
    )
    def acknowledge_alert(
            alert_id: str,
            request: Request,
            db: Session = Depends(get_db),
            admin_and_type: tuple = Depends(get_current_admin),
            admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """
        Acknowledge a usage alert.

        Only active alerts can be acknowledged. Once acknowledged, the alert
        remains visible but won't appear in active alert counts.

        Requires RESOURCE_GROUP_UPDATE permission.
        """
        try:
            admin_user, admin_type_val = admin_and_type
            tenant_id = acting_tenant_id(request, admin_user)

            # Fetch alert with tenant isolation
            alert = db.query(UsageAlert).filter(
                UsageAlert.id == alert_id,
                UsageAlert.tenant_id == tenant_id
            ).first()

            if not alert:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Usage alert '{alert_id}' not found"
                )

            if alert.status != 'active':
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Alert is already '{alert.status}' and cannot be acknowledged"
                )

            # Acknowledge
            alert.status = 'acknowledged'
            alert.acknowledged_by = str(admin_user.id)
            alert.acknowledged_at = datetime.now(timezone.utc)

            db.flush()

            logger.info("Usage alert acknowledged", context={
                "alert_id": alert_id,
                "alert_type": alert.alert_type,
                "metric": alert.metric,
                "group_id": alert.group_id,
                "acknowledged_by": str(admin_user.id)
            })

            audit.log(
                action="usage_alert_acknowledge",
                admin=admin_user,
                admin_type=admin_type_val,
                tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.RESOURCE_GROUPS.value,
                resource_id=str(alert.id),
                details={
                    "alert_type": alert.alert_type,
                    "metric": alert.metric,
                    "group_id": alert.group_id,
                    "status": alert.status
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            return AlertAcknowledgeResponse(
                id=alert.id,
                status=alert.status,
                acknowledged_by=alert.acknowledged_by,
                acknowledged_at=alert.acknowledged_at.isoformat() if alert.acknowledged_at else None,
                message=alert.message
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to acknowledge usage alert", context={
                "alert_id": alert_id,
                "error": str(e),
                "error_type": type(e).__name__
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to acknowledge usage alert"
            )

    @router.get("/by-type/{group_type}", response_model=ResourceGroupListResponse)
    def get_groups_by_type(
        group_type: GroupType,
        request: Request,
        include_stats: bool = Query(False),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_READ))
    ):
        """
        Get all groups of a specific type

        Examples:
        - GET /by-type/application → All AI applications
        - GET /by-type/agent → All AI agents
        - GET /by-type/department → All departments
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            groups = db.query(ResourceGroup).filter(
                ResourceGroup.tenant_id == admin_tenant_id,
                ResourceGroup.group_type == group_type
            ).order_by(ResourceGroup.name).all()

            group_responses = []
            for group in groups:
                group_dict = group.to_dict(include_stats=include_stats)
                group_responses.append(ResourceGroupResponse(**group_dict))

            return ResourceGroupListResponse(
                groups=group_responses,
                total=len(group_responses)
            )

        except Exception as e:
            logger.error("Failed to get groups by type", context={
                "group_type": group_type.value,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve groups by type"
            )

    @router.get("/search", response_model=ResourceGroupListResponse)
    def search_groups(
        request: Request,
        q: str = Query(..., min_length=1, description="Search query"),
        group_type: Optional[GroupType] = Query(None, description="Filter by type"),
        has_members: Optional[bool] = Query(None, description="Filter groups with/without members"),
        has_limits: Optional[bool] = Query(None, description="Filter groups with configured limits"),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_READ))
    ):
        """
        Search groups by name or description
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            query = db.query(ResourceGroup).filter(
                ResourceGroup.tenant_id == admin_tenant_id
            )

            # Search in name, display_name, and description
            search_term = f"%{q}%"
            query = query.filter(
                or_(
                    ResourceGroup.name.ilike(search_term),
                    ResourceGroup.display_name.ilike(search_term),
                    ResourceGroup.description.ilike(search_term)
                )
            )

            # Apply filters
            if group_type:
                query = query.filter(ResourceGroup.group_type == group_type)

            # Execute query
            groups = query.order_by(ResourceGroup.name).all()

            # Apply post-query filters
            if has_members is not None:
                if has_members:
                    groups = [g for g in groups if g.members.count() > 0]
                else:
                    groups = [g for g in groups if g.members.count() == 0]

            if has_limits is not None:
                if has_limits:
                    groups = [g for g in groups if g.llm_global_limits or g.mcp_global_limits or g.skill_global_limits]
                else:
                    groups = [g for g in groups if not g.llm_global_limits and not g.mcp_global_limits and not g.skill_global_limits]

            group_responses = []
            for group in groups:
                group_dict = group.to_dict()
                group_responses.append(ResourceGroupResponse(**group_dict))

            return ResourceGroupListResponse(
                groups=group_responses,
                total=len(group_responses)
            )

        except Exception as e:
            logger.error("Failed to search groups", context={
                "search_query": q,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to search groups"
            )

    # =========================================================================
    # GROUP MEMBERSHIP ENDPOINTS
    # =========================================================================

    # Helper functions for member hierarchy management
    def get_ancestor_group_ids(group: ResourceGroup) -> List[str]:
        """
        Get all ancestor group IDs in the hierarchy.

        Args:
            group: The resource group

        Returns:
            List of ancestor group IDs (from immediate parent to root)
        """
        ancestors = group.get_ancestors()
        return [ancestor.id for ancestor in ancestors]

    def get_descendant_group_ids(group: ResourceGroup) -> List[str]:
        """
        Get all descendant group IDs (children, grandchildren, etc.).

        Args:
            group: The resource group

        Returns:
            List of all descendant group IDs
        """
        descendants = group.get_descendants(include_self=False)
        return [descendant.id for descendant in descendants]

    def _update_descendant_paths(group: ResourceGroup, db: Session):
        """
        Recursively update hierarchy_path for all descendants after a rename or re-parent.

        Walks via parent_group_id relationships so it's immune to stale paths.
        """
        children = db.query(ResourceGroup).filter(
            ResourceGroup.parent_group_id == group.id,
            ResourceGroup.tenant_id == group.tenant_id
        ).all()

        for child in children:
            child.hierarchy_path = f"{group.hierarchy_path}/{child.name}"
            child.hierarchy_level = group.hierarchy_level + 1
            _update_descendant_paths(child, db)

    def get_inherited_members(group: ResourceGroup, db: Session) -> List[GroupMemberInfo]:
        """
        Get members inherited from parent groups with source information.

        Args:
            group: The resource group
            db: Database session

        Returns:
            List of GroupMemberInfo with inheritance information
        """
        inherited_members = []

        # Get all ancestors
        ancestors = group.get_ancestors()

        if not ancestors:
            return []

        # Get members from all ancestor groups
        for ancestor in ancestors:
            memberships = db.query(ResourceGroupMembership).filter(
                ResourceGroupMembership.group_id == ancestor.id
            ).all()

            for membership in memberships:
                user = membership.user
                if user:
                    inherited_members.append(GroupMemberInfo(
                        user_id=str(user.id),
                        username=user.username,
                        email=user.email,
                        display_name=user.display_name,
                        avatar_url=getattr(user, 'avatar_url', None),
                        role=membership.role,
                        joined_at=membership.joined_at.isoformat() if membership.joined_at else None,
                        is_inherited=True,
                        source_group_id=ancestor.id,
                        source_group_name=ancestor.name,
                        is_primary=(user.primary_group_id == ancestor.id),
                    ))

        return inherited_members

    @router.patch(
        "/api-keys/{key_id}",
        summary="Update an API key",
        description="Update an API key's name, description, expiry, enabled "
                    "state, or agent-identity binding (pass agent_id=\"\" to unbind)."
    )
    def update_api_key(
            key_id: str,
            key_data: APIKeyUpdate,
            request: Request,
            db: Session = Depends(get_db),
            admin_and_type: tuple = Depends(get_current_admin),
            admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Update an API key (metadata + agent binding; the secret never changes)."""
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            key = db.query(APIKey).filter(
                APIKey.id == key_id,
                APIKey.tenant_id == admin_tenant_id
            ).first()

            if not key:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"API key '{key_id}' not found"
                )
            if key.revoked:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Cannot update a revoked API key"
                )

            # `expires_at` sent as null clears the expiry; not sent leaves it.
            expiry_sent = "expires_at" in key_data.model_fields_set
            if expiry_sent:
                _refuse_key_by_identity_policy(
                    db, admin_tenant_id, key.group_id,
                    personal=key.scope == 'user', expires_at=key_data.expires_at,
                    creating=False, expiry_changed=True,
                )

            changes = {}
            if key_data.name is not None:
                changes["name"] = key_data.name
                key.name = key_data.name
            if key_data.description is not None:
                changes["description"] = key_data.description
                key.description = key_data.description
            if expiry_sent:
                changes["expires_at"] = key_data.expires_at.isoformat() if key_data.expires_at else None
                key.expires_at = key_data.expires_at
            if key_data.enabled is not None:
                changes["enabled"] = key_data.enabled
                key.enabled = key_data.enabled
            if key_data.agent_id is not None:
                # "" clears the binding; anything else must be a live agent.
                key.agent_id = _resolve_agent_binding(db, admin_tenant_id, key_data.agent_id)
                changes["agent_id"] = key.agent_id

            key.updated_by = admin.id
            db.flush()

            block_on(invalidate_proxy_cache(request, "resource_group", admin_tenant_id, key.group_id, action="update"))

            audit.log(
                action="api_key_update",
                admin=admin,
                admin_type=admin_type,
                tenant_id=admin_tenant_id,
                success=True,
                resource_type="api_key",
                resource_id=key_id,
                details={"key_name": key.name, "changes": changes},
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            return {"message": "API key updated successfully", "key_id": key_id,
                    "agent_id": key.agent_id}

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to update API key", context={
                "key_id": key_id, "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to update API key"
            )

    @router.post(
        "/api-keys/{key_id}/enable",
        summary="Enable an API key",
        description="Enable a disabled API key"
    )
    def enable_api_key(
            key_id: str,
            request: Request,
            db: Session = Depends(get_db),
            admin_and_type: tuple = Depends(get_current_admin),
            admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Enable an API key"""
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            key = db.query(APIKey).filter(
                APIKey.id == key_id,
                APIKey.tenant_id == admin_tenant_id
            ).first()

            if not key:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"API key '{key_id}' not found"
                )

            if key.revoked:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Cannot enable a revoked API key"
                )

            key.enable_key()
            key.updated_by = admin.id
            db.flush()

            block_on(invalidate_proxy_cache(request, "resource_group", admin_tenant_id, key.group_id, action="update"))

            logger.info(f"Enabled API key", context={
                "key_id": key_id,
                "key_name": key.name,
                "admin_id": admin.id
            })

            audit.log(
                action="api_key_enable",
                admin=admin,
                admin_type=admin_type,
                tenant_id=admin_tenant_id,
                success=True,
                resource_type="api_key",
                resource_id=key_id,
                details={"key_name": key.name},
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            return {"message": "API key enabled successfully", "key_id": key_id}

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to enable API key", context={
                "key_id": key_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to enable API key"
            )

    @router.post(
        "/api-keys/{key_id}/disable",
        summary="Disable an API key",
        description="Temporarily disable an API key"
    )
    def disable_api_key(
            key_id: str,
            request: Request,
            db: Session = Depends(get_db),
            admin_and_type: tuple = Depends(get_current_admin),
            admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Disable an API key"""
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            key = db.query(APIKey).filter(
                APIKey.id == key_id,
                APIKey.tenant_id == admin_tenant_id
            ).first()

            if not key:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"API key '{key_id}' not found"
                )

            key.disable_key()
            key.updated_by = admin.id
            db.flush()

            block_on(invalidate_proxy_cache(request, "resource_group", admin_tenant_id, key.group_id, action="update"))

            logger.info(f"Disabled API key", context={
                "key_id": key_id,
                "key_name": key.name,
                "admin_id": admin.id
            })

            audit.log(
                action="api_key_disable",
                admin=admin,
                admin_type=admin_type,
                tenant_id=admin_tenant_id,
                success=True,
                resource_type="api_key",
                resource_id=key_id,
                details={"key_name": key.name},
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            return {"message": "API key disabled successfully", "key_id": key_id}

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to disable API key", context={
                "key_id": key_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to disable API key"
            )

    @router.post(
        "/api-keys/{key_id}/revoke",
        summary="Revoke an API key",
        description="Permanently revoke an API key (cannot be undone)"
    )
    def revoke_api_key(
            key_id: str,
            request: Request,
            revoke_request: Optional[APIKeyRevokeRequest] = None,
            db: Session = Depends(get_db),
            admin_and_type: tuple = Depends(get_current_admin),
            admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Permanently revoke an API key"""
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            key = db.query(APIKey).filter(
                APIKey.id == key_id,
                APIKey.tenant_id == admin_tenant_id
            ).first()

            if not key:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"API key '{key_id}' not found"
                )

            if key.revoked:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="API key is already revoked"
                )

            reason = revoke_request.reason if revoke_request else None
            key.revoke(admin_id=admin.id, reason=reason)
            key.updated_by = admin.id
            db.flush()

            block_on(invalidate_proxy_cache(request, "resource_group", admin_tenant_id, key.group_id, action="delete"))

            logger.info(f"Revoked API key", context={
                "key_id": key_id,
                "key_name": key.name,
                "reason": reason,
                "admin_id": admin.id
            })

            audit.log(
                action="api_key_revoke",
                admin=admin,
                admin_type=admin_type,
                tenant_id=admin_tenant_id,
                success=True,
                resource_type="api_key",
                resource_id=key_id,
                details={
                    "key_name": key.name,
                    "reason": reason
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            return {"message": "API key revoked successfully", "key_id": key_id}

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to revoke API key", context={
                "key_id": key_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to revoke API key"
            )


    # =================================================================
    # PARAMETRIC ROUTES - /{group_id}/... (must be AFTER fixed paths)
    # =================================================================

    @router.get("/{group_id}", response_model=ResourceGroupResponse)
    def get_resource_group(
        group_id: str,
        request: Request,
        include_stats: bool = Query(False, description="Include statistics"),
        include_resources: bool = Query(False, description="Include resources"),
        include_hierarchy: bool = Query(False, description="Include hierarchy info"),
        include_limits: bool = Query(False, description="Include effective limits"),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_READ))
    ):
        """
        Get details of a specific resource group

        Query parameters allow fetching:
        - include_stats: Member/config counts
        - include_resources: Server configs and LLM models
        - include_hierarchy: Parent/children information
        - include_limits: Effective limits with inheritance resolved
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)
        admin_id = admin.id

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()

            if not group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User group '{group_id}' not found"
                )

            logger.debug("Resource group details accessed", context={
                "admin_id": admin_id,
                "tenant_id": admin_tenant_id,
                "group_id": group_id
            })

            group_dict = group.to_dict(
                include_stats=include_stats,
                include_resources=include_resources,
                include_hierarchy=include_hierarchy,
                include_limits=include_limits
            )
            return ResourceGroupResponse(**group_dict)

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get resource group", context={
                "group_id": group_id,
                "tenant_id": admin_tenant_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve user group"
            )

    @router.patch("/{group_id}", response_model=ResourceGroupResponse)
    def update_resource_group(
        group_id: str,
        group_data: ResourceGroupUpdate,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Update resource group"""
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)
        admin_id = admin.id

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()

            if not group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User group '{group_id}' not found"
                )

            # Track changes for audit
            changes = {}

            # Update fields if provided
            if group_data.name is not None and group_data.name != group.name:
                changes['name'] = {'old': group.name, 'new': group_data.name}
                group.name = group_data.name

                # Update hierarchy path if name changed
                if group.parent_group_id:
                    parent = db.query(ResourceGroup).get(group.parent_group_id)
                    if parent.hierarchy_path:
                        group.hierarchy_path = f"{parent.hierarchy_path}/{group.name}"
                    else:
                        group.hierarchy_path = f"{parent.name}/{group.name}"
                else:
                    group.hierarchy_path = group.name

                # Cascade: update all descendants' hierarchy_paths
                _update_descendant_paths(group, db)

            if group_data.display_name is not None:
                changes['display_name'] = {'old': group.display_name, 'new': group_data.display_name}
                group.display_name = group_data.display_name

            if group_data.description is not None:
                group.description = group_data.description

            if group_data.group_type is not None:
                changes['group_type'] = {
                    'old': group.group_type.value if group.group_type else None,
                    'new': group_data.group_type.value
                }
                group.group_type = group_data.group_type

            if group_data.icon is not None:
                group.icon = group_data.icon

            if group_data.color is not None:
                group.color = group_data.color

            if group_data.llm_global_limits is not None:
                changes['llm_global_limits_updated'] = True
                group.llm_global_limits = group_data.llm_global_limits

            if group_data.mcp_global_limits is not None:
                _require_readable_mcp_limits(group_data.mcp_global_limits)
                changes['mcp_global_limits_updated'] = True
                group.mcp_global_limits = group_data.mcp_global_limits

            if group_data.is_active is not None:
                changes['is_active'] = {'old': group.is_active, 'new': group_data.is_active}
                group.is_active = group_data.is_active

            if group_data.inherit_resources is not None:
                changes['inherit_resources'] = {'old': group.inherit_resources, 'new': group_data.inherit_resources}
                group.inherit_resources = group_data.inherit_resources

            if group_data.skill_global_governance is not None:
                ok, err = validate_skill_governance(group_data.skill_global_governance)
                if not ok:
                    raise HTTPException(status_code=400, detail=f"Invalid skill_global_governance: {err}")
                changes['skill_global_governance'] = {'old': group.skill_global_governance, 'new': group_data.skill_global_governance}
                group.skill_global_governance = group_data.skill_global_governance

            # Compliance posture. Use
            # `model_fields_set` so an explicit `null` is preserved
            # (the operator clearing the tier or PHI flag must reach
            # the DB, not be filtered out by the not-None check).
            if 'eu_ai_act_risk_tier' in group_data.model_fields_set:
                changes['eu_ai_act_risk_tier'] = {
                    'old': group.eu_ai_act_risk_tier,
                    'new': group_data.eu_ai_act_risk_tier,
                }
                group.eu_ai_act_risk_tier = group_data.eu_ai_act_risk_tier
            if 'eu_ai_act_role' in group_data.model_fields_set:
                changes['eu_ai_act_role'] = {
                    'old': group.eu_ai_act_role,
                    'new': group_data.eu_ai_act_role,
                }
                group.eu_ai_act_role = group_data.eu_ai_act_role
            if 'phi_resource' in group_data.model_fields_set:
                changes['phi_resource'] = {
                    'old': group.phi_resource,
                    'new': group_data.phi_resource,
                }
                group.phi_resource = group_data.phi_resource
            if 'sensitive_data' in group_data.model_fields_set:
                # NOT NULL column: an explicit null clears the flag.
                new_sensitive = bool(group_data.sensitive_data)
                changes['sensitive_data'] = {
                    'old': bool(group.sensitive_data),
                    'new': new_sensitive,
                }
                group.sensitive_data = new_sensitive

            # An AI System's lifecycle stage is gated.
            #
            # Without this, the gate is decorative: the evidence requirements
            # live on POST /v1/admin/ai-systems/{id}/lifecycle, and a plain
            # PATCH here would set the same column with no contract, no
            # approver and no check for widened permissions. An ungated path
            # to a governed field is worse than no gate at all, because the
            # fleet board would still report the system as approved.
            #
            # For every other group type `lifecycle_stage` is documentation
            # and stays freely editable.
            if (
                'lifecycle_stage' in group_data.model_fields_set
                and group.group_type == GroupType.AI_SYSTEM
                and group_data.lifecycle_stage != group.lifecycle_stage
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "An AI System's lifecycle stage is gated on evidence. "
                        f"Use POST /v1/admin/ai-systems/{group.id}/lifecycle, "
                        "which reports what each transition requires and what "
                        "is missing."
                    ),
                )

            # Governance / ownership (AI-System attributes on the RG).
            # Use `model_fields_set` so an explicit null clears the field.
            # Binding the replay suite the promotion gate
            # consults. Validated rather than trusted: a dangling id would
            # make the gate refuse every promotion with "suite no longer
            # exists", which is a confusing way to learn about a typo.
            if 'replay_suite_id' in group_data.model_fields_set:
                _suite_id = group_data.replay_suite_id
                if _suite_id:
                    from app.database.models.ai_system_replay import (
                        AISystemReplaySuite,
                    )
                    _suite = (
                        db.query(AISystemReplaySuite)
                        .filter(
                            AISystemReplaySuite.id == _suite_id,
                            AISystemReplaySuite.tenant_id == group.tenant_id,
                        )
                        .first()
                    )
                    if _suite is None:
                        raise HTTPException(
                            status_code=422,
                            detail=f"Replay suite {_suite_id!r} not found",
                        )
                    if _suite.ai_system_id != group.id:
                        raise HTTPException(
                            status_code=422,
                            detail=(
                                f"Replay suite {_suite.name!r} was built from "
                                f"{_suite.ai_system_id}'s traffic and cannot "
                                f"gate {group.id} — its cases are another "
                                "system's runs."
                            ),
                        )

            # Estate rollup exclusion. Server stamps who and when: an
            # exclusion is an attestation, and an attestation names its author.
            if 'rollup_exclusion' in group_data.model_fields_set:
                _excl = group_data.rollup_exclusion
                _new_excl = None
                if _excl is not None:
                    _new_excl = {
                        "reason": _excl.reason,
                        "note": _excl.note,
                        "expires_at": _excl.expires_at.isoformat() if _excl.expires_at else None,
                        "by": str(admin.id),
                        "at": datetime.now(timezone.utc).isoformat(),
                    }
                changes['rollup_exclusion'] = {'old': group.rollup_exclusion, 'new': _new_excl}
                group.rollup_exclusion = _new_excl

            if 'intended_client_systems' in group_data.model_fields_set:
                group_data.intended_client_systems = _validated_client_systems(
                    db, admin_tenant_id, group.id, group_data.intended_client_systems
                )

            for _gov_field in (
                'owner', 'intended_use', 'intended_client_labels',
                'intended_client_systems', 'lifecycle_stage',
                'autonomy_level', 'version', 'replay_suite_id',
            ):
                if _gov_field in group_data.model_fields_set:
                    changes[_gov_field] = {
                        'old': getattr(group, _gov_field),
                        'new': getattr(group_data, _gov_field),
                    }
                    setattr(group, _gov_field, getattr(group_data, _gov_field))

            # Accepted-caller enforcement is a property of a
            # SERVICE, and only an AI System is called as one. Checked against
            # the MERGED group type (a PATCH may carry only this field).
            if 'enforce_accepted_callers' in group_data.model_fields_set:
                _enforce = bool(group_data.enforce_accepted_callers)
                if _enforce and group.group_type != GroupType.AI_SYSTEM:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            "enforce_accepted_callers is only valid on an AI System "
                            f"(this group is {getattr(group.group_type, 'value', group.group_type)!r})"
                        ),
                    )
                if bool(group.enforce_accepted_callers) != _enforce:
                    changes['enforce_accepted_callers'] = {
                        'old': bool(group.enforce_accepted_callers), 'new': _enforce,
                    }
                    group.enforce_accepted_callers = _enforce

            # AI System kind. Applied after group_type above so
            # the coupling check below sees the merged state.
            if 'system_kind' in group_data.model_fields_set:
                _new_kind = (
                    group_data.system_kind.value if group_data.system_kind else None
                )
                changes['system_kind'] = {'old': group.system_kind, 'new': _new_kind}
                group.system_kind = _new_kind

            # A PATCH can break the group_type/system_kind coupling in ways
            # neither request model can see on its own, because an omitted
            # field means "leave unchanged" and only the merged object knows
            # the result. Reconcile it here so the model's flush hook never
            # has to raise (which would surface as a 500).
            if 'group_type' in group_data.model_fields_set or 'system_kind' in group_data.model_fields_set:
                _is_ai_system = group.group_type == GroupType.AI_SYSTEM
                if _is_ai_system and not group.system_kind:
                    # Promoted to AI System with no kind stated — default it,
                    # consistent with create and the flush hook.
                    group.system_kind = DEFAULT_SYSTEM_KIND
                    changes['system_kind'] = {'old': None, 'new': DEFAULT_SYSTEM_KIND}
                elif not _is_ai_system and group.system_kind:
                    # Demoted away from AI System. The stored kind is now
                    # definitionally meaningless, so clear it rather than 422
                    # the operator for not having sent `system_kind: null`
                    # alongside the demotion. Audited via `changes`, and the
                    # genuinely wrong case — a kind supplied *together with* a
                    # non-AI-System group_type — is already a 422 from the
                    # request model before we reach here.
                    changes['system_kind'] = {'old': group.system_kind, 'new': None}
                    group.system_kind = None
                if not _is_ai_system and group.enforce_accepted_callers:
                    # Demoted away from AI System: nothing calls it as a
                    # service any more, so the enforcement flag goes with it
                    # (audited), like system_kind above.
                    changes['enforce_accepted_callers'] = {'old': True, 'new': False}
                    group.enforce_accepted_callers = False

                # Backstop: any combination the two branches above failed to
                # normalise is a bug, and 422 beats a 500 from the flush hook.
                _ok, _err = group.validate_system_kind()
                if not _ok:
                    raise HTTPException(status_code=422, detail=_err)

            if group_data.a2a_global_limits is not None:
                # Empty dict is a valid "clear the limits" signal; both
                # None (not supplied) and {} need to round-trip cleanly.
                from app.database.models.resource_group import validate_a2a_limits
                ok, err = validate_a2a_limits(group_data.a2a_global_limits)
                if not ok:
                    raise HTTPException(status_code=400, detail=f"Invalid a2a_global_limits: {err}")
                changes['a2a_global_limits_updated'] = True
                group.a2a_global_limits = group_data.a2a_global_limits or None

            if group_data.a2a_global_governance is not None:
                from app.database.models.resource_group import validate_a2a_governance
                ok, err = validate_a2a_governance(group_data.a2a_global_governance)
                if not ok:
                    raise HTTPException(status_code=400, detail=f"Invalid a2a_global_governance: {err}")
                changes['a2a_global_governance_updated'] = True
                group.a2a_global_governance = group_data.a2a_global_governance or None

            # Handle parent change
            if group_data.parent_group_id is not None:
                if group_data.parent_group_id != group.parent_group_id:
                    # Verify new parent exists
                    if group_data.parent_group_id:
                        new_parent = db.query(ResourceGroup).filter(
                            ResourceGroup.id == group_data.parent_group_id,
                            ResourceGroup.tenant_id == admin_tenant_id
                        ).first()

                        if not new_parent:
                            raise HTTPException(
                                status_code=status.HTTP_404_NOT_FOUND,
                                detail=f"Parent group '{group_data.parent_group_id}' not found"
                            )

                        # Check for circular reference
                        if new_parent.id == group.id or group in new_parent.get_ancestors():
                            raise HTTPException(
                                status_code=status.HTTP_400_BAD_REQUEST,
                                detail="Cannot create circular group hierarchy"
                            )

                        group.hierarchy_level = new_parent.hierarchy_level + 1
                        if new_parent.hierarchy_path:
                            group.hierarchy_path = f"{new_parent.hierarchy_path}/{group.name}"
                        else:
                            group.hierarchy_path = f"{new_parent.name}/{group.name}"
                    else:
                        # Moving to root
                        group.hierarchy_level = 0
                        group.hierarchy_path = group.name

                    changes['parent_group_id'] = {
                        'old': group.parent_group_id,
                        'new': group_data.parent_group_id
                    }
                    group.parent_group_id = group_data.parent_group_id

                    # Cascade: update all descendants' hierarchy_paths
                    _update_descendant_paths(group, db)

            group.updated_by = admin_id
            db.flush()

            block_on(invalidate_proxy_cache(request, "resource_group", admin_tenant_id, group_id, action="update"))

            logger.info("Resource group updated", context={
                "group_id": group_id,
                "changes": changes,
                "tenant_id": admin_tenant_id,
                "admin_id": admin_id
            })

            audit.log(
                action="resource_group_update",
                admin=admin,
                admin_type=admin_type,
                tenant_id=admin_tenant_id,
                success=True,
                resource_type=TenantResourceTypes.RESOURCE_GROUPS.value,
                resource_id=group_id,
                details={
                    "group_id": group_id,
                    "group_name": group.name,
                    "changes": changes
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            # Invalidate enforcer cache so new limits take effect immediately
            usage_limit_enforcer.invalidate_group(admin_tenant_id, group_id)

            # include_limits=True so the response echoes the just-saved
            # limits/governance (incl. a2a/media) — otherwise the admin UI's
            # form reload showed null and the edit appeared to vanish.
            return ResourceGroupResponse(**group.to_dict(include_limits=True))

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to update resource group", context={
                "group_id": group_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to update resource group"
            )

    @router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_resource_group(
        group_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_DELETE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """
        Delete resource group (cascades to children and memberships)
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)
        admin_id = admin.id

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()

            if not group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Resource group '{group_id}' not found"
                )

            # Prevent deletion of system groups (e.g. Default)
            if getattr(group, 'is_system', False):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Cannot delete system resource group '{group.name}'. "
                           f"System groups are managed by the platform."
                )

            group_name = group.name
            child_count = len(list(group.children))
            member_count = group.members.count()

            db.delete(group)
            db.flush()

            block_on(invalidate_proxy_cache(request, "resource_group", admin_tenant_id, group_id, action="delete"))

            logger.info("Resource group deleted", context={
                "group_id": group_id,
                "group_name": group_name,
                "child_count": child_count,
                "member_count": member_count,
                "tenant_id": admin_tenant_id,
                "admin_id": admin_id
            })

            audit.log(
                action="resource_group_delete",
                admin=admin,
                admin_type=admin_type,
                tenant_id=admin_tenant_id,
                success=True,
                resource_type=TenantResourceTypes.RESOURCE_GROUPS.value,
                resource_id=group_id,
                details={
                    "group_id": group_id,
                    "group_name": group_name,
                    "child_count": child_count,
                    "member_count": member_count
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            return None

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to delete resource group", context={
                "group_id": group_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to delete resource group"
            )

    # =========================================================================
    # HIERARCHY NAVIGATION ENDPOINTS
    # =========================================================================

    @router.get("/{group_id}/ancestors", response_model=GroupAncestorPathResponse)
    def get_group_ancestors(
        group_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_READ))
    ):
        """
        Get all ancestor groups (from root to parent)

        Returns the full path from root to this group's parent.
        Example: Engineering > Frontend > React Team returns [Engineering, Frontend]
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()

            if not group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User group '{group_id}' not found"
                )

            ancestors = group.get_ancestors()

            ancestor_infos = [
                GroupAncestorInfo(
                    id=ancestor.id,
                    name=ancestor.name,
                    group_type=ancestor.group_type,
                    hierarchy_level=ancestor.hierarchy_level
                )
                for ancestor in ancestors
            ]

            # Build full path string
            full_path = " / ".join([a.name for a in ancestors] + [group.name])

            return GroupAncestorPathResponse(
                group_id=group.id,
                group_name=group.name,
                ancestors=ancestor_infos,
                full_path=full_path
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get group ancestors", context={
                "group_id": group_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve group ancestors"
            )

    @router.get("/{group_id}/descendants", response_model=GroupDescendantsResponse)
    def get_group_descendants(
        group_id: str,
        request: Request,
        include_self: bool = Query(False, description="Include this group in results"),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_READ))
    ):
        """
        Get all descendant groups (recursive)

        Returns all child groups, grandchildren, etc. in a flat list.
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()

            if not group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User group '{group_id}' not found"
                )

            descendants = group.get_descendants(include_self=include_self)

            descendant_infos = [
                GroupHierarchyInfo(
                    id=desc.id,
                    name=desc.name,
                    display_name=desc.display_name,
                    group_type=desc.group_type,
                    hierarchy_level=desc.hierarchy_level,
                    is_active=desc.is_active
                )
                for desc in descendants
            ]

            return GroupDescendantsResponse(
                group_id=group.id,
                group_name=group.name,
                descendants=descendant_infos,
                total_descendants=len(descendant_infos)
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get group descendants", context={
                "group_id": group_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve group descendants"
            )

    @router.get("/{group_id}/siblings", response_model=GroupSiblingsResponse)
    def get_group_siblings(
        group_id: str,
        request: Request,
        include_self: bool = Query(False, description="Include this group in results"),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_READ))
    ):
        """
        Get sibling groups (groups with same parent)
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()

            if not group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User group '{group_id}' not found"
                )

            siblings = group.get_siblings(include_self=include_self, db=db)

            sibling_infos = [
                GroupHierarchyInfo(
                    id=sib.id,
                    name=sib.name,
                    display_name=sib.display_name,
                    group_type=sib.group_type,
                    hierarchy_level=sib.hierarchy_level,
                    is_active=sib.is_active
                )
                for sib in siblings
            ]

            return GroupSiblingsResponse(
                group_id=group.id,
                group_name=group.name,
                parent_group_id=group.parent_group_id,
                siblings=sibling_infos,
                total_siblings=len(sibling_infos)
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get group siblings", context={
                "group_id": group_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve group siblings"
            )

    @router.get("/{group_id}/children", response_model=GroupDescendantsResponse)
    def get_group_children(
        group_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_READ))
    ):
        """
        Get immediate children (non-recursive)
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()

            if not group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User group '{group_id}' not found"
                )

            children = list(group.children)

            child_infos = [
                GroupHierarchyInfo(
                    id=child.id,
                    name=child.name,
                    display_name=child.display_name,
                    group_type=child.group_type,
                    hierarchy_level=child.hierarchy_level,
                    is_active=child.is_active
                )
                for child in children
            ]

            return GroupDescendantsResponse(
                group_id=group.id,
                group_name=group.name,
                descendants=child_infos,
                total_descendants=len(child_infos)
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get group children", context={
                "group_id": group_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve group children"
            )

    @router.get("/{group_id}/effective-llm-models", response_model=EffectiveLLModelsResponse)
    def get_effective_llm_models(
        group_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_READ))
    ):
        """
        Get all LLM models available to this group (including inherited)

        Returns:
        - Direct models: Models assigned directly to this group
        - Inherited models: Models inherited from ancestor groups
        - Effective models: Combined deduplicated list
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()

            if not group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User group '{group_id}' not found"
                )

            # Get direct models (all models directly assigned to this group,
            # regardless of enabled status — enabled filtering happens in UI)
            direct_models = list(group.llm_models.all())
            direct_model_ids = {model.id for model in direct_models}

            # Get all effective models (includes inherited)
            effective_models = group.get_effective_llm_models()

            # Separate inherited
            inherited_models = [m for m in effective_models if m.id not in direct_model_ids]

            # Build set of model IDs that have governance (direct or inherited)
            # Query governance rows for this group and all ancestors
            governance_model_ids: set = set()
            chain = group.get_ancestors(include_self=True)
            for g in chain:
                rows = db.execute(
                    resource_group_llm_governance.select().where(
                        resource_group_llm_governance.c.resource_group_id == g.id,
                    )
                ).fetchall()
                for row in rows:
                    governance_model_ids.add(row.llm_model_id)

            # Build set of model IDs that have per-item limits on this group
            limits_model_ids: set = set()
            limit_rows = db.execute(
                resource_group_llm_models.select().where(
                    resource_group_llm_models.c.resource_group_id == group_id,
                    resource_group_llm_models.c.limits.isnot(None),
                )
            ).fetchall()
            for row in limit_rows:
                limits_model_ids.add(row.llm_model_id)

            # Per-model portal visibility. The flag lives on the binding row,
            # so an inherited model carries the flag from whichever ancestor
            # binds it directly — visibility is inherited along with access.
            # We walk the chain root→…→self (the `chain` built above) and let
            # the closest binding win: self's direct binding overrides an
            # ancestor's, and an inherited model picks up the nearest
            # ancestor that actually binds it. To change an inherited model's
            # visibility, bind it directly to this group.
            # `chain` is ordered root→…→self; deeper index == closer to self.
            portal_visible_map: dict = {}
            chain_ids = [g.id for g in chain]
            depth = {gid: i for i, gid in enumerate(chain_ids)}
            best_depth: dict = {}
            for row in db.execute(
                resource_group_llm_models.select().where(
                    resource_group_llm_models.c.resource_group_id.in_(chain_ids),
                )
            ).fetchall():
                d = depth.get(row.resource_group_id, -1)
                if row.llm_model_id not in best_depth or d >= best_depth[row.llm_model_id]:
                    best_depth[row.llm_model_id] = d
                    portal_visible_map[row.llm_model_id] = bool(row.portal_visible)

            def _model_info(m, is_inherited: bool) -> LLModelInfo:
                return LLModelInfo(
                    id=m.id,
                    name=m.name,
                    description=m.description,
                    # Provider slug from the relationship.
                    provider=m.provider.key if m.provider else None,
                    model_name=m.model_name,
                    health_status=m.get_health_status_dict(),
                    is_inherited=is_inherited,
                    has_governance=m.id in governance_model_ids,
                    has_limits=m.id in limits_model_ids,
                    portal_visible=portal_visible_map.get(m.id),
                )

            return EffectiveLLModelsResponse(
                group_id=group.id,
                group_name=group.name,
                direct_models=[_model_info(m, False) for m in direct_models],
                direct_count=len(direct_models),
                inherited_models=[_model_info(m, True) for m in inherited_models],
                inherited_count=len(inherited_models),
                effective_models=[_model_info(m, m.id not in direct_model_ids) for m in effective_models],
                effective_count=len(effective_models)
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get effective LLM models", context={
                "group_id": group_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve effective LLM models"
            )

    # Helper function to convert MCPServer database model to MCPServerInfo dict
    def convert_mcp_server(mcp_server: MCPServer):
        """Convert MCPServer database model to dict for MCPServerInfo"""
        if not mcp_server:
            return None
        return {
            "id": mcp_server.id,
            "name": mcp_server.name,
            "description": mcp_server.description,
            "base_url": mcp_server.base_url,
            "mcp_endpoint_path": mcp_server.mcp_endpoint_path,
            "auth_type": mcp_server.auth_type,
            "timeout": mcp_server.timeout,
            "enabled": mcp_server.enabled,
            "idp_id": mcp_server.idp_id,
            "icon": mcp_server.get_icon_info(),
            "capabilities": mcp_server.capabilities,
            "discovered_at": mcp_server.discovered_at.isoformat() if mcp_server.discovered_at else None,
            "health_status": mcp_server.get_health_status_dict() if hasattr(mcp_server, 'get_health_status_dict') else None,
        }

    @router.get("/{group_id}/effective-server-configs", response_model=EffectiveServerConfigsResponse)
    def get_effective_server_configs(
            group_id: str,
            request: Request,
            db: Session = Depends(get_db),
            admin_and_type: tuple = Depends(get_current_admin),
            admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_READ))
    ):
        """
        Get all MCP server configs available to this group (including inherited)

        Returns:
        - Direct configs: Configs assigned directly to this group
        - Inherited configs: Configs inherited from ancestor groups
        - Effective configs: Combined deduplicated list
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()

            if not group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User group '{group_id}' not found"
                )

            # Get direct configs (all configs directly assigned to this group,
            # regardless of enabled status — enabled filtering happens in UI)
            direct_configs = list(group.server_configs.all())
            direct_config_ids = {config.id for config in direct_configs}

            # Get all effective configs (includes inherited)
            effective_configs = group.get_effective_server_configs()

            # Separate inherited
            inherited_configs = [c for c in effective_configs if c.id not in direct_config_ids]

            # Load capability filter summaries for all effective configs in one query
            all_config_ids = [c.id for c in effective_configs]
            filter_rows = []
            if all_config_ids:
                filter_rows = db.execute(
                    resource_group_mcp_capabilities.select().where(
                        resource_group_mcp_capabilities.c.resource_group_id == group_id,
                        resource_group_mcp_capabilities.c.server_config_id.in_(all_config_ids)
                    )
                ).fetchall()

            # Build per-config filter summaries
            filter_summaries: dict[str, CapabilityFilterSummary] = {}
            for row in filter_rows:
                sc_id = row.server_config_id
                if sc_id not in filter_summaries:
                    filter_summaries[sc_id] = {"total": 0, "enabled": 0, "disabled": 0, "approval_required": 0}
                filter_summaries[sc_id]["total"] += 1
                access = row.access
                if access == "enabled":
                    filter_summaries[sc_id]["enabled"] += 1
                elif access == "approval_required":
                    filter_summaries[sc_id]["approval_required"] += 1
                else:
                    filter_summaries[sc_id]["disabled"] += 1

            def get_filter_summary(config_id: str) -> CapabilityFilterSummary:
                if config_id in filter_summaries:
                    s = filter_summaries[config_id]
                    return CapabilityFilterSummary(
                        has_filters=True,
                        total=s["total"],
                        enabled=s["enabled"],
                        disabled=s["disabled"],
                        approval_required=s["approval_required"]
                    )
                return CapabilityFilterSummary(has_filters=False)

            # Build set of server IDs that have per-item limits on this group
            limits_server_ids: set = set()
            limit_rows = db.execute(
                resource_group_server_configs.select().where(
                    resource_group_server_configs.c.resource_group_id == group_id,
                    resource_group_server_configs.c.limits.isnot(None),
                )
            ).fetchall()
            for row in limit_rows:
                limits_server_ids.add(row.server_config_id)

            def _config_info(c, is_inherited: bool) -> ServerConfigInfo:
                # Effective enabled: ServerConfig.enabled AND MCPServer.enabled
                effective_enabled = c.enabled and (c.mcp_server.enabled if c.mcp_server else True)
                return ServerConfigInfo(
                    id=c.id,
                    name=c.name,
                    description=c.description,
                    enabled=effective_enabled,
                    icon_url=c.icon_url,
                    mcp_server=convert_mcp_server(c.mcp_server),
                    is_inherited=is_inherited,
                    capability_filters=get_filter_summary(c.id),
                    has_limits=c.id in limits_server_ids,
                )

            return EffectiveServerConfigsResponse(
                group_id=group.id,
                group_name=group.name,
                direct_configs=[_config_info(c, False) for c in direct_configs],
                direct_count=len(direct_configs),
                inherited_configs=[_config_info(c, True) for c in inherited_configs],
                inherited_count=len(inherited_configs),
                effective_configs=[_config_info(c, c.id not in direct_config_ids) for c in effective_configs],
                effective_count=len(effective_configs)
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get effective server configs", context={
                "group_id": group_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve effective server configs"
            )

    # Helper function to convert AgentSkill database model to AgentSkillInfo
    def convert_agent_skill_info(skill: AgentSkill, is_inherited: bool = False, has_limits: bool = False, has_governance: bool = False) -> AgentSkillInfo:
        """Convert AgentSkill database model to AgentSkillInfo response"""
        metadata = skill.skill_metadata or {}
        return AgentSkillInfo(
            id=skill.id,
            name=skill.name,
            description=skill.description,
            version=skill.version,
            status=skill.status.value if hasattr(skill.status, 'value') else str(skill.status),
            enabled=skill.enabled,
            category=metadata.get('category'),
            icon_name=metadata.get('icon_name'),
            execution_count=skill.execution_count or 0,
            is_inherited=is_inherited,
            has_limits=has_limits,
            has_governance=has_governance
        )

    @router.get("/{group_id}/effective-agent-skills", response_model=EffectiveAgentSkillsResponse)
    def get_effective_agent_skills(
            group_id: str,
            request: Request,
            db: Session = Depends(get_db),
            admin_and_type: tuple = Depends(get_current_admin),
            admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_READ))
    ):
        """
        Get all agent skills available to this group (including inherited)

        Returns:
        - Direct skills: Skills assigned directly to this group
        - Inherited skills: Skills inherited from ancestor groups
        - Effective skills: Combined deduplicated list
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()

            if not group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User group '{group_id}' not found"
                )

            # Get direct skills (all skills directly assigned to this group)
            direct_skills = list(group.agent_skills.all())
            direct_skill_ids = {skill.id for skill in direct_skills}

            # Get all effective skills (includes inherited)
            effective_skills = group.get_effective_agent_skills()

            # Separate inherited
            inherited_skills = [s for s in effective_skills if s.id not in direct_skill_ids]

            # Build set of skill IDs that have per-item limits on this group
            from app.database.models.agent_skill import resource_group_agent_skills as rgas
            limits_skill_ids: set = set()
            governance_skill_ids: set = set()
            assoc_rows = db.execute(
                rgas.select().where(
                    rgas.c.resource_group_id == group_id,
                )
            ).fetchall()
            for row in assoc_rows:
                if row.limits is not None:
                    limits_skill_ids.add(row.agent_skill_id)
                if row.governance is not None:
                    governance_skill_ids.add(row.agent_skill_id)

            # Also check if group has skill_global_governance set (applies to all skills)
            has_global_skill_gov = bool(group.skill_global_governance)

            return EffectiveAgentSkillsResponse(
                group_id=group.id,
                group_name=group.name,
                direct_skills=[
                    convert_agent_skill_info(s, is_inherited=False, has_limits=s.id in limits_skill_ids,
                                             has_governance=s.id in governance_skill_ids or has_global_skill_gov)
                    for s in direct_skills
                ],
                direct_count=len(direct_skills),
                inherited_skills=[
                    convert_agent_skill_info(s, is_inherited=True, has_limits=s.id in limits_skill_ids,
                                             has_governance=s.id in governance_skill_ids or has_global_skill_gov)
                    for s in inherited_skills
                ],
                inherited_count=len(inherited_skills),
                effective_skills=[
                    convert_agent_skill_info(s, is_inherited=s.id not in direct_skill_ids, has_limits=s.id in limits_skill_ids,
                                             has_governance=s.id in governance_skill_ids or has_global_skill_gov)
                    for s in effective_skills
                ],
                effective_count=len(effective_skills)
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get effective agent skills", context={
                "group_id": group_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve effective agent skills"
            )

    @router.get("/{group_id}/effective-guardrails", response_model=EffectiveGuardrailsResponse)
    def get_effective_guardrails(
            group_id: str,
            request: Request,
            db: Session = Depends(get_db),
            admin_and_type: tuple = Depends(get_current_admin),
            admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_READ))
    ):
        """
        Get all guardrail configurations that apply to this resource group.

        Returns:
        - Direct configs: configs explicitly associated with this group
        - Inherited configs: configs associated with ancestor groups
        - Effective configs: combined deduplicated list
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()

            if not group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Resource group '{group_id}' not found"
                )

            # Direct configs
            direct_configs = list(group.guardrail_configs.all())
            direct_config_ids = {cfg.id for cfg in direct_configs}

            # All effective (direct + inherited)
            effective_configs = group.get_effective_guardrails()

            # Separate inherited and resolve source group names
            ancestor_map = {a.id: a.name for a in group.get_ancestors()}
            inherited_tuples = []  # (cfg, source_group_id, source_group_name)

            for cfg in effective_configs:
                if cfg.id in direct_config_ids:
                    continue
                source_id = next((gid for gid in cfg.group_ids if gid in ancestor_map), None)
                inherited_tuples.append((cfg, source_id, ancestor_map.get(source_id) if source_id else None))

            # Count surfaces in each `*_per_surface` JSONB map.
            # NULL / non-dict / empty all collapse to 0 so the table
            # never claims overrides that aren't there. Mirrors the
            # admin UI's `countPerSurfaceOverrides` helper.
            def _ps_count(v) -> int:
                return len(v) if isinstance(v, dict) else 0

            # Helper
            def build_info(cfg, is_inherited=False, source_group_id=None, source_group_name=None):
                providers = cfg.providers or []
                active_providers = [p for p in providers if p.get('enabled', False)]
                return GuardrailConfigInfo(
                    id=cfg.id,
                    name=cfg.name,
                    description=cfg.description,
                    enabled=cfg.enabled,
                    is_default=cfg.is_default,
                    priority=cfg.priority or 0,
                    surfaces={
                        # Per-mode LLM surfaces.
                        "chat_input": cfg.chat_input_enabled,
                        "chat_output": cfg.chat_output_enabled,
                        "embeddings_input": cfg.embeddings_input_enabled,
                        "image_gen_input": cfg.image_gen_input_enabled,
                        "image_gen_output": cfg.image_gen_output_enabled,
                        "audio_tts_input": cfg.audio_tts_input_enabled,
                        "audio_tts_output": cfg.audio_tts_output_enabled,
                        "audio_stt_input": cfg.audio_stt_input_enabled,
                        "audio_stt_output": cfg.audio_stt_output_enabled,
                        "video_gen_input": cfg.video_gen_input_enabled,
                        "video_gen_output": cfg.video_gen_output_enabled,
                        "batch_input": cfg.batch_input_enabled,
                        "batch_output": cfg.batch_output_enabled,
                        "moderation_input": cfg.moderation_input_enabled,
                        # Non-LLM kinds + A2A.
                        "mcp_input": cfg.mcp_input_enabled,
                        "mcp_output": cfg.mcp_output_enabled,
                        "skill_input": cfg.skill_input_enabled,
                        "skill_output": cfg.skill_output_enabled,
                        "a2a_inbound": getattr(cfg, "a2a_inbound_enabled", False) or False,
                        "a2a_outbound": getattr(cfg, "a2a_outbound_enabled", False) or False,
                    },
                    pii_enabled=cfg.pii_detection_enabled,
                    prompt_injection_enabled=cfg.prompt_injection_enabled,
                    jailbreak_enabled=cfg.jailbreak_detection_enabled,
                    toxic_content_enabled=cfg.toxic_content_enabled,
                    secrets_enabled=bool(getattr(cfg, "secrets_detection_enabled", False)),
                    banned_word_count=len(cfg.banned_words or []),
                    banned_pattern_count=len(cfg.banned_patterns or []),
                    provider_count=len(active_providers),
                    per_surface_counts={
                        "pii": _ps_count(getattr(cfg, "pii_per_surface", None)),
                        "prompt_injection": _ps_count(getattr(cfg, "prompt_injection_per_surface", None)),
                        "jailbreak": _ps_count(getattr(cfg, "jailbreak_per_surface", None)),
                        "toxic_content": _ps_count(getattr(cfg, "toxic_content_per_surface", None)),
                        "secrets": _ps_count(getattr(cfg, "secrets_per_surface", None)),
                        "banned_words": _ps_count(getattr(cfg, "banned_words_per_surface", None)),
                        "banned_patterns": _ps_count(getattr(cfg, "banned_patterns_per_surface", None)),
                    },
                    fail_open=cfg.fail_open,
                    max_check_timeout_ms=cfg.max_check_timeout_ms or 2000.0,
                    is_inherited=is_inherited,
                    source_group_id=source_group_id,
                    source_group_name=source_group_name,
                    group_ids=cfg.group_ids,
                )

            direct_list = [build_info(cfg) for cfg in direct_configs]
            inherited_list = [
                build_info(cfg, is_inherited=True, source_group_id=sid, source_group_name=sname)
                for cfg, sid, sname in inherited_tuples
            ]

            # Build inherited lookup for effective list
            inherited_lookup = {cfg.id: (sid, sname) for cfg, sid, sname in inherited_tuples}
            effective_list = [
                build_info(
                    cfg,
                    is_inherited=cfg.id not in direct_config_ids,
                    source_group_id=inherited_lookup.get(cfg.id, (None, None))[0],
                    source_group_name=inherited_lookup.get(cfg.id, (None, None))[1],
                )
                for cfg in effective_configs
            ]

            return EffectiveGuardrailsResponse(
                group_id=group.id,
                group_name=group.name,
                direct_configs=direct_list,
                direct_count=len(direct_list),
                inherited_configs=inherited_list,
                inherited_count=len(inherited_list),
                effective_configs=effective_list,
                effective_count=len(effective_list),
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get effective guardrails", context={
                "group_id": group_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve effective guardrails"
            )


    @router.post(
        "/{group_id}/llm-models",
        status_code=status.HTTP_201_CREATED,
        summary="Add LLM model to resource group",
        description="Associate an LLM model with this resource group. "
                    "Members of this group will then have access to this model."
    )
    def add_llm_model_to_group(
            group_id: str,
            body: AddLLMModelToGroupRequest,
            request: Request,
            db: Session = Depends(get_db),
            admin_and_type: tuple = Depends(get_current_admin),
            admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            # Verify group exists
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()

            if not group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Resource group '{group_id}' not found"
                )

            # Verify model exists
            model = db.query(LLModel).filter(
                LLModel.id == body.llm_model_id,
                LLModel.tenant_id == admin_tenant_id
            ).first()

            if not model:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"LLM model '{body.llm_model_id}' not found"
                )

            # Check for existing association
            existing = db.execute(
                resource_group_llm_models.select().where(
                    resource_group_llm_models.c.resource_group_id == group_id,
                    resource_group_llm_models.c.llm_model_id == body.llm_model_id
                )
            ).first()

            if existing:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Model '{model.name}' is already assigned to group '{group.name}'"
                )

            # Portal visibility default. We only auto-hide a routing
            # MEMBER when its routing group is itself associated with THIS
            # resource group (directly or inherited) — i.e. the member is
            # bound here purely so the routing group can resolve to it, and
            # exposing it as a standalone picker entry would break the
            # single-virtual-model story. If the routing group is NOT on
            # this RG (e.g. binding gpt-6-luna directly to Ops, which has
            # no routing group), the model is a directly-addressable model
            # here and stays visible. Routing virtual models (mode='routing')
            # and plain standalone models always default visible. Embedding
            # models default hidden — you can't chat with an embedding model,
            # so it has no business in the end-user model picker. An explicit
            # body.portal_visible overrides all of this.
            if getattr(body, "portal_visible", None) is not None:
                portal_visible = bool(body.portal_visible)
            elif getattr(model, "mode", None) == "embedding":
                portal_visible = False
            else:
                is_routing_member = (
                    bool(getattr(model, "routing_group_id", None))
                    and getattr(model, "mode", None) != "routing"
                )
                if is_routing_member:
                    chain_ids = [group_id] + [a.id for a in group.get_ancestors()]
                    routing_group_on_rg = db.execute(
                        resource_group_llm_models.select().where(
                            resource_group_llm_models.c.resource_group_id.in_(chain_ids),
                            resource_group_llm_models.c.llm_model_id.in_(
                                select(LLModel.id).where(
                                    LLModel.routing_group_id == model.routing_group_id,
                                    LLModel.mode == "routing",
                                )
                            ),
                        )
                    ).first() is not None
                    portal_visible = not routing_group_on_rg
                else:
                    portal_visible = True

            # Insert association
            db.execute(
                resource_group_llm_models.insert().values(
                    resource_group_id=group_id,
                    llm_model_id=body.llm_model_id,
                    created_by=str(admin.id),
                    portal_visible=portal_visible,
                )
            )
            db.flush()

            block_on(invalidate_proxy_cache(request, "model", admin_tenant_id, body.llm_model_id, action="update"))
            block_on(invalidate_proxy_cache(request, "resource_group", admin_tenant_id, group_id, action="update"))

            logger.info("LLM model added to group", context={
                "group_id": group_id,
                "group_name": group.name,
                "model_id": body.llm_model_id,
                "model_name": model.name,
                "tenant_id": admin_tenant_id
            })

            audit.log(
                action="resource_group_add_llm_model",
                admin=admin,
                admin_type=admin_type,
                tenant_id=admin_tenant_id,
                success=True,
                resource_type="resource_group",
                resource_id=group_id,
                details={
                    "group_id": group_id,
                    "group_name": group.name,
                    "model_id": body.llm_model_id,
                    "model_name": model.name
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            return {"status": "added", "group_id": group_id, "llm_model_id": body.llm_model_id}

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to add LLM model to group", context={
                "group_id": group_id,
                "model_id": body.llm_model_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to add LLM model to group"
            )


    @router.delete(
        "/{group_id}/llm-models/{model_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        summary="Remove LLM model from resource group",
        description="Remove the association between an LLM model and this resource group."
    )
    def remove_llm_model_from_group(
            group_id: str,
            model_id: str,
            request: Request,
            db: Session = Depends(get_db),
            admin_and_type: tuple = Depends(get_current_admin),
            admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            # Verify group exists
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()

            if not group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Resource group '{group_id}' not found"
                )

            # Find the association
            existing = db.execute(
                resource_group_llm_models.select().where(
                    resource_group_llm_models.c.resource_group_id == group_id,
                    resource_group_llm_models.c.llm_model_id == model_id
                )
            ).first()

            if not existing:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Model '{model_id}' is not directly assigned to group '{group.name}'"
                )

            # Delete the association
            db.execute(
                resource_group_llm_models.delete().where(
                    resource_group_llm_models.c.resource_group_id == group_id,
                    resource_group_llm_models.c.llm_model_id == model_id
                )
            )
            db.flush()

            block_on(invalidate_proxy_cache(request, "model", admin_tenant_id, model_id, action="update"))
            block_on(invalidate_proxy_cache(request, "resource_group", admin_tenant_id, group_id, action="update"))

            logger.info("LLM model removed from group", context={
                "group_id": group_id,
                "group_name": group.name,
                "model_id": model_id,
                "tenant_id": admin_tenant_id
            })

            audit.log(
                action="resource_group_remove_llm_model",
                admin=admin,
                admin_type=admin_type,
                tenant_id=admin_tenant_id,
                success=True,
                resource_type="resource_group",
                resource_id=group_id,
                details={
                    "group_id": group_id,
                    "group_name": group.name,
                    "model_id": model_id
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            return None

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to remove LLM model from group", context={
                "group_id": group_id,
                "model_id": model_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to remove LLM model from group"
            )


    @router.post(
        "/{group_id}/server-configs",
        status_code=status.HTTP_201_CREATED,
        summary="Add MCP server config to resource group",
        description="Associate an MCP server config with this resource group. "
                    "Members of this group will then have access to this server."
    )
    def add_server_config_to_group(
            group_id: str,
            body: AddServerConfigToGroupRequest,
            request: Request,
            db: Session = Depends(get_db),
            admin_and_type: tuple = Depends(get_current_admin),
            admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            # Verify group exists
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()

            if not group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Resource group '{group_id}' not found"
                )

            # Verify server config exists
            config = db.query(ServerConfig).filter(
                ServerConfig.id == body.server_config_id,
                ServerConfig.tenant_id == admin_tenant_id
            ).first()

            if not config:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Server config '{body.server_config_id}' not found"
                )

            # Check for existing association
            existing = db.execute(
                resource_group_server_configs.select().where(
                    resource_group_server_configs.c.resource_group_id == group_id,
                    resource_group_server_configs.c.server_config_id == body.server_config_id
                )
            ).first()

            if existing:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Server config '{config.name}' is already assigned to group '{group.name}'"
                )

            # Insert association
            db.execute(
                resource_group_server_configs.insert().values(
                    resource_group_id=group_id,
                    server_config_id=body.server_config_id,
                    created_by=str(admin.id)
                )
            )
            db.flush()

            notify_group_members_about_resource_change(
                db=db,
                tenant_id=admin_tenant_id,
                group_ids=[group_id],
                resource_kind="mcp_server",
                resource_id=body.server_config_id,
                resource_name=config.name,
                action="added",
                actor_username=admin.username,
                logger=logger,
            )

            block_on(invalidate_proxy_cache(request, "mcp_server", admin_tenant_id, body.server_config_id, action="update"))
            block_on(invalidate_proxy_cache(request, "resource_group", admin_tenant_id, group_id, action="update"))

            logger.info("Server config added to group", context={
                "group_id": group_id,
                "group_name": group.name,
                "server_config_id": body.server_config_id,
                "server_name": config.name,
                "tenant_id": admin_tenant_id
            })

            audit.log(
                action="resource_group_add_server_config",
                admin=admin,
                admin_type=admin_type,
                tenant_id=admin_tenant_id,
                success=True,
                resource_type="resource_group",
                resource_id=group_id,
                details={
                    "group_id": group_id,
                    "group_name": group.name,
                    "server_config_id": body.server_config_id,
                    "server_name": config.name
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            return {"status": "added", "group_id": group_id, "server_config_id": body.server_config_id}

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to add server config to group", context={
                "group_id": group_id,
                "server_config_id": body.server_config_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to add server config to group"
            )


    @router.delete(
        "/{group_id}/server-configs/{config_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        summary="Remove MCP server config from resource group",
        description="Remove the association between an MCP server config and this resource group."
    )
    def remove_server_config_from_group(
            group_id: str,
            config_id: str,
            request: Request,
            db: Session = Depends(get_db),
            admin_and_type: tuple = Depends(get_current_admin),
            admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            # Verify group exists
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()

            if not group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Resource group '{group_id}' not found"
                )

            # Find the association
            existing = db.execute(
                resource_group_server_configs.select().where(
                    resource_group_server_configs.c.resource_group_id == group_id,
                    resource_group_server_configs.c.server_config_id == config_id
                )
            ).first()

            if not existing:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Server config '{config_id}' is not directly assigned to group '{group.name}'"
                )

            # Resolve the server config name for the notification body
            # before we lose the association.
            removed_config = db.query(ServerConfig).filter(
                ServerConfig.id == config_id,
                ServerConfig.tenant_id == admin_tenant_id,
            ).first()
            removed_config_name = removed_config.name if removed_config else config_id

            # Delete the association
            db.execute(
                resource_group_server_configs.delete().where(
                    resource_group_server_configs.c.resource_group_id == group_id,
                    resource_group_server_configs.c.server_config_id == config_id
                )
            )
            db.flush()

            notify_group_members_about_resource_change(
                db=db,
                tenant_id=admin_tenant_id,
                group_ids=[group_id],
                resource_kind="mcp_server",
                resource_id=config_id,
                resource_name=removed_config_name,
                action="removed",
                actor_username=admin.username,
                logger=logger,
            )

            block_on(invalidate_proxy_cache(request, "mcp_server", admin_tenant_id, config_id, action="update"))
            block_on(invalidate_proxy_cache(request, "resource_group", admin_tenant_id, group_id, action="update"))

            logger.info("Server config removed from group", context={
                "group_id": group_id,
                "group_name": group.name,
                "config_id": config_id,
                "tenant_id": admin_tenant_id
            })

            audit.log(
                action="resource_group_remove_server_config",
                admin=admin,
                admin_type=admin_type,
                tenant_id=admin_tenant_id,
                success=True,
                resource_type="resource_group",
                resource_id=group_id,
                details={
                    "group_id": group_id,
                    "group_name": group.name,
                    "config_id": config_id
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            return None

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to remove server config from group", context={
                "group_id": group_id,
                "config_id": config_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to remove server config from group"
            )

    # =========================================================================
    # MCP CAPABILITY ACCESS CONTROL
    # =========================================================================

    @router.get(
        "/{group_id}/server-configs/{config_id}/capabilities",
        response_model=MCPCapabilityFilterResponse,
        summary="Get MCP capability filters for a group+server",
        description="Returns the capability-level access settings for an MCP server within a resource group. "
                    "If no filters are configured, all capabilities are enabled (default)."
    )
    def get_mcp_capability_filters(
            group_id: str,
            config_id: str,
            request: Request,
            db: Session = Depends(get_db),
            admin_and_type: tuple = Depends(get_current_admin),
            admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_READ))
    ):
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()
            if not group:
                raise HTTPException(status_code=404, detail=f"Resource group '{group_id}' not found")

            config = db.query(ServerConfig).filter(
                ServerConfig.id == config_id,
                ServerConfig.tenant_id == admin_tenant_id
            ).first()
            if not config:
                raise HTTPException(status_code=404, detail=f"Server config '{config_id}' not found")

            # Get direct capability filters for this group
            rows = db.execute(
                resource_group_mcp_capabilities.select().where(
                    resource_group_mcp_capabilities.c.resource_group_id == group_id,
                    resource_group_mcp_capabilities.c.server_config_id == config_id
                )
            ).fetchall()

            # Collect inherited restrictive access from ancestor groups
            # (governance inheritance — always on, restrictive merge: max strictness wins)
            # Strictness order: enabled(0) < approval_required(1) < disabled(2);
            # an access the gateway does not know reads as disabled.
            inherited_restrictive = {}  # (cap_type, cap_name) → (access, timeout, source_group_id, source_group_name)
            for ancestor in group.get_ancestors():
                ancestor_rows = db.execute(
                    resource_group_mcp_capabilities.select().where(
                        resource_group_mcp_capabilities.c.resource_group_id == ancestor.id,
                        resource_group_mcp_capabilities.c.server_config_id == config_id,
                    )
                ).fetchall()
                for arow in ancestor_rows:
                    key = (arow.capability_type, arow.capability_name)
                    access = arow.access
                    timeout = arow.approval_timeout_seconds
                    if key in inherited_restrictive:
                        existing = inherited_restrictive[key]
                        # Only override if this ancestor is more restrictive
                        if _MCP_ACCESS_STRICTNESS.rank(access) > _MCP_ACCESS_STRICTNESS.rank(existing[0]):
                            inherited_restrictive[key] = (access, timeout, ancestor.id, ancestor.name)
                    else:
                        # Only track if not 'enabled' (enabled is the default, no restriction)
                        if access != 'enabled':
                            inherited_restrictive[key] = (access, timeout, ancestor.id, ancestor.name)

            # Build capability list: direct filters + inherited restrictive
            capabilities = []
            seen = set()
            for row in rows:
                key = (row.capability_type, row.capability_name)
                seen.add(key)
                inherited = inherited_restrictive.get(key)
                if inherited:
                    inh_access, inh_timeout, src_id, src_name = inherited
                    # Effective access is the most restrictive
                    if _MCP_ACCESS_STRICTNESS.rank(inh_access) > _MCP_ACCESS_STRICTNESS.rank(row.access):
                        effective_access = inh_access
                    else:
                        effective_access = row.access
                    capabilities.append(
                        MCPCapabilityInfo(
                            capability_type=row.capability_type,
                            capability_name=row.capability_name,
                            access=effective_access,
                            approval_timeout_seconds=row.approval_timeout_seconds or inh_timeout,
                            inherited_access=inh_access,
                            source_group_id=src_id,
                            source_group_name=src_name,
                        )
                    )
                else:
                    capabilities.append(
                        MCPCapabilityInfo(
                            capability_type=row.capability_type,
                            capability_name=row.capability_name,
                            access=row.access,
                            approval_timeout_seconds=row.approval_timeout_seconds,
                        )
                    )

            # Add inherited restrictive capabilities that aren't in the direct list
            for (cap_type, cap_name), (inh_access, inh_timeout, src_id, src_name) in inherited_restrictive.items():
                if (cap_type, cap_name) not in seen:
                    capabilities.append(
                        MCPCapabilityInfo(
                            capability_type=cap_type,
                            capability_name=cap_name,
                            access=inh_access,
                            approval_timeout_seconds=inh_timeout,
                            inherited_access=inh_access,
                            source_group_id=src_id,
                            source_group_name=src_name,
                        )
                    )

            # Extract discovered capabilities from the MCP server
            discovered_capabilities = None
            discovered_at = None
            mcp_server = config.mcp_server
            if mcp_server and mcp_server.capabilities:
                caps = mcp_server.capabilities
                discovered_list = []
                # Map capability type keys to their item name field
                type_map = {
                    "tools": ("tool", "name"),
                    "resources": ("resource", "name"),
                    "prompts": ("prompt", "name"),
                    "resource_templates": ("resource_template", "name"),
                }
                for key, (cap_type, name_field) in type_map.items():
                    items = caps.get(key, [])
                    if isinstance(items, list):
                        for item in items:
                            if isinstance(item, dict) and item.get(name_field):
                                discovered_list.append(MCPDiscoveredCapability(
                                    capability_type=cap_type,
                                    name=item[name_field],
                                    description=item.get("description")
                                ))
                if discovered_list:
                    discovered_capabilities = discovered_list
                if mcp_server.discovered_at:
                    discovered_at = mcp_server.discovered_at.isoformat()

            # Get effective MCP global governance defaults from hierarchy
            effective_mcp_gov = group.get_effective_mcp_governance()

            return MCPCapabilityFilterResponse(
                group_id=group.id,
                group_name=group.name,
                server_config_id=config.id,
                server_config_name=config.name,
                capabilities=capabilities,
                total=len(capabilities),
                has_filters=len(capabilities) > 0,
                discovered_capabilities=discovered_capabilities,
                discovered_at=discovered_at,
                global_governance_defaults=effective_mcp_gov if effective_mcp_gov else None
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get MCP capability filters", context={
                "group_id": group_id, "config_id": config_id, "error": str(e)
            }, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to get capability filters")


    @router.put(
        "/{group_id}/server-configs/{config_id}/capabilities",
        response_model=MCPCapabilityFilterResponse,
        summary="Update MCP capability filters for a group+server",
        description="Replace all capability filters for an MCP server within a resource group. "
                    "Send an empty list to remove all filters (restoring full access)."
    )
    def update_mcp_capability_filters(
            group_id: str,
            config_id: str,
            body: MCPCapabilityFilterUpdate,
            request: Request,
            db: Session = Depends(get_db),
            admin_and_type: tuple = Depends(get_current_admin),
            admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()
            if not group:
                raise HTTPException(status_code=404, detail=f"Resource group '{group_id}' not found")

            config = db.query(ServerConfig).filter(
                ServerConfig.id == config_id,
                ServerConfig.tenant_id == admin_tenant_id
            ).first()
            if not config:
                raise HTTPException(status_code=404, detail=f"Server config '{config_id}' not found")

            # Verify group has this server config assigned (direct or inherited)
            effective_configs = group.get_effective_server_configs()
            if not any(c.id == config_id for c in effective_configs):
                raise HTTPException(
                    status_code=404,
                    detail=f"Server config '{config.name}' is not accessible by group '{group.name}'"
                )

            # Validate capability types
            valid_types = {"tool", "resource", "prompt", "resource_template"}
            for cap in body.capabilities:
                if cap.capability_type not in valid_types:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Invalid capability_type '{cap.capability_type}'. Must be one of: {', '.join(valid_types)}"
                    )

            # Validate access values
            valid_access = {'enabled', 'approval_required', 'disabled'}
            for cap in body.capabilities:
                if cap.access not in valid_access:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Invalid access value '{cap.access}'. Must be one of: {', '.join(valid_access)}"
                    )

            # Check inherited restrictive access — cannot relax parent's access level
            # Strictness: enabled(0) < approval_required(1) < disabled(2);
            # an access the gateway does not know reads as disabled.
            inherited_access_map = {}  # (cap_type, cap_name) → most restrictive access from ancestors
            for ancestor in group.get_ancestors():
                ancestor_rows = db.execute(
                    resource_group_mcp_capabilities.select().where(
                        resource_group_mcp_capabilities.c.resource_group_id == ancestor.id,
                        resource_group_mcp_capabilities.c.server_config_id == config_id,
                    )
                ).fetchall()
                for arow in ancestor_rows:
                    key = (arow.capability_type, arow.capability_name)
                    if key in inherited_access_map:
                        # Keep the most restrictive
                        if _MCP_ACCESS_STRICTNESS.rank(arow.access) > _MCP_ACCESS_STRICTNESS.rank(inherited_access_map[key]):
                            inherited_access_map[key] = arow.access
                    else:
                        inherited_access_map[key] = arow.access

            violations = []
            for cap in body.capabilities:
                key = (cap.capability_type, cap.capability_name)
                if key in inherited_access_map:
                    parent_access = inherited_access_map[key]
                    if _MCP_ACCESS_STRICTNESS.rank(cap.access) < _MCP_ACCESS_STRICTNESS.rank(parent_access):
                        violations.append(
                            f"{cap.capability_type}:{cap.capability_name} "
                            f"(tried '{cap.access}', parent requires '{parent_access}')"
                        )
            if violations:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Cannot set access less restrictive than parent group: {', '.join(violations)}"
                )

            # Delete all existing filters for this group+server
            db.execute(
                resource_group_mcp_capabilities.delete().where(
                    resource_group_mcp_capabilities.c.resource_group_id == group_id,
                    resource_group_mcp_capabilities.c.server_config_id == config_id
                )
            )

            # Insert new filters
            now = datetime.now(timezone.utc)
            for cap in body.capabilities:
                db.execute(
                    resource_group_mcp_capabilities.insert().values(
                        tenant_id=admin_tenant_id,
                        resource_group_id=group_id,
                        server_config_id=config_id,
                        capability_type=cap.capability_type,
                        capability_name=cap.capability_name,
                        access=cap.access,
                        approval_timeout_seconds=cap.approval_timeout_seconds,
                        created_at=now,
                        updated_at=now,
                        created_by=str(admin.id)
                    )
                )

            db.flush()

            block_on(invalidate_proxy_cache(request, "resource_group", admin_tenant_id, group_id, action="update"))

            logger.info("MCP capability filters updated", context={
                "group_id": group_id,
                "group_name": group.name,
                "server_config_id": config_id,
                "server_name": config.name,
                "capability_count": len(body.capabilities),
                "tenant_id": admin_tenant_id
            })

            audit.log(
                action="resource_group_update_mcp_capabilities",
                admin=admin,
                admin_type=admin_type,
                tenant_id=admin_tenant_id,
                success=True,
                resource_type="resource_group",
                resource_id=group_id,
                details={
                    "group_id": group_id,
                    "group_name": group.name,
                    "server_config_id": config_id,
                    "server_name": config.name,
                    "capability_count": len(body.capabilities),
                    "enabled_count": sum(1 for c in body.capabilities if c.access == 'enabled'),
                    "approval_required_count": sum(1 for c in body.capabilities if c.access == 'approval_required'),
                    "disabled_count": sum(1 for c in body.capabilities if c.access == 'disabled')
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            # Return updated state
            capabilities = [
                MCPCapabilityInfo(
                    capability_type=cap.capability_type,
                    capability_name=cap.capability_name,
                    access=cap.access,
                    approval_timeout_seconds=cap.approval_timeout_seconds,
                )
                for cap in body.capabilities
            ]

            return MCPCapabilityFilterResponse(
                group_id=group.id,
                group_name=group.name,
                server_config_id=config.id,
                server_config_name=config.name,
                capabilities=capabilities,
                total=len(capabilities),
                has_filters=len(capabilities) > 0
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to update MCP capability filters", context={
                "group_id": group_id, "config_id": config_id, "error": str(e)
            }, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to update capability filters")


    @router.delete(
        "/{group_id}/server-configs/{config_id}/capabilities",
        status_code=status.HTTP_204_NO_CONTENT,
        summary="Clear all MCP capability filters for a group+server",
        description="Remove all capability filters, restoring full access to all capabilities."
    )
    def clear_mcp_capability_filters(
            group_id: str,
            config_id: str,
            request: Request,
            db: Session = Depends(get_db),
            admin_and_type: tuple = Depends(get_current_admin),
            admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()
            if not group:
                raise HTTPException(status_code=404, detail=f"Resource group '{group_id}' not found")

            result = db.execute(
                resource_group_mcp_capabilities.delete().where(
                    resource_group_mcp_capabilities.c.resource_group_id == group_id,
                    resource_group_mcp_capabilities.c.server_config_id == config_id
                )
            )
            db.flush()

            block_on(invalidate_proxy_cache(request, "resource_group", admin_tenant_id, group_id, action="update"))

            logger.info("MCP capability filters cleared", context={
                "group_id": group_id,
                "group_name": group.name,
                "config_id": config_id,
                "deleted_count": result.rowcount,
                "tenant_id": admin_tenant_id
            })

            audit.log(
                action="resource_group_clear_mcp_capabilities",
                admin=admin,
                admin_type=admin_type,
                tenant_id=admin_tenant_id,
                success=True,
                resource_type="resource_group",
                resource_id=group_id,
                details={
                    "group_id": group_id,
                    "group_name": group.name,
                    "config_id": config_id,
                    "deleted_count": result.rowcount
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            return None

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to clear MCP capability filters", context={
                "group_id": group_id, "config_id": config_id, "error": str(e)
            }, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to clear capability filters")


    @router.get("/{group_id}/limits", response_model=GroupLimitsResponse)
    def get_limits(
        group_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_READ))
    ):
        """
        Get limits for this group with governance inheritance.

        Returns direct limits, inherited floor from ancestors (restrictive merge),
        and the effective limits (what's actually enforced at runtime).
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()

            if not group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User group '{group_id}' not found"
                )

            inherited_llm = _compute_inherited_limits(group, 'llm_global_limits')
            inherited_mcp = _compute_inherited_limits(group, 'mcp_global_limits')
            inherited_skill = _compute_inherited_limits(group, 'skill_global_limits')

            return GroupLimitsResponse(
                group_id=group.id,
                group_name=group.name,
                llm_global_limits=group.llm_global_limits,
                mcp_global_limits=group.mcp_global_limits,
                skill_global_limits=group.skill_global_limits,
                effective_llm_limits=group.get_effective_llm_limits() or None,
                effective_mcp_limits=group.get_effective_mcp_limits() or None,
                effective_skill_limits=group.get_effective_skill_limits() or None,
                inherited_llm_limits=inherited_llm,
                inherited_mcp_limits=inherited_mcp,
                inherited_skill_limits=inherited_skill,
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get limits", context={
                "group_id": group_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve limits"
            )

    # =========================================================================
    # LLM LIMITS ENDPOINTS
    # =========================================================================

    @router.get("/{group_id}/llm-global-limits", response_model=LLMGlobalLimitsResponse)
    def get_llm_global_limits(
        group_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_READ))
    ):
        """
        Get LLM global limits for this group with governance inheritance.

        Returns direct limits, inherited floor from ancestors, and effective limits.
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()

            if not group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User group '{group_id}' not found"
                )

            return LLMGlobalLimitsResponse(
                group_id=group.id,
                group_name=group.name,
                llm_global_limits=group.llm_global_limits,
                effective_llm_limits=group.get_effective_llm_limits() or None,
                inherited_llm_limits=_compute_inherited_limits(group, 'llm_global_limits'),
                capacity=_compute_capacity_info(group, 'llm_global_limits', db),
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get LLM global limits", context={
                "group_id": group_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve LLM global limits"
            )

    @router.patch("/{group_id}/llm-global-limits", response_model=LLMGlobalLimitsResponse)
    def update_llm_global_limits(
        group_id: str,
        limits_data: LLMGlobalLimitsUpdate,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """
        Set LLM global limits for this group.

        Values cannot be less restrictive than the inherited floor from ancestors.
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()

            if not group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User group '{group_id}' not found"
                )

            # Validate limits structure
            group.llm_global_limits = limits_data.llm_global_limits
            is_valid, error_message = group.validate_llm_limits()
            if not is_valid:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid LLM limits: {error_message}"
                )

            # Validate against inherited floor — cannot set less restrictive
            inherited_floor = _compute_inherited_limits(group, 'llm_global_limits')
            violations = _validate_not_less_restrictive(limits_data.llm_global_limits, inherited_floor)
            if violations:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Cannot set less restrictive than inherited limits: {'; '.join(violations)}"
                )

            # Validate sibling sum does not exceed parent
            sibling_violations = _validate_siblings_sum(group, limits_data.llm_global_limits, 'llm_global_limits', db)
            if sibling_violations:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Sibling limits exceed parent capacity: {'; '.join(sibling_violations)}"
                )

            old_limits = group.llm_global_limits
            group.llm_global_limits = limits_data.llm_global_limits
            group.updated_by = admin.id

            db.flush()
            db.refresh(group)

            block_on(invalidate_proxy_cache(request, "resource_group", admin_tenant_id, group_id, action="update"))

            audit.log(
                action="resource_group_update_llm_global_limits",
                admin=admin,
                admin_type=admin_type,
                tenant_id=admin_tenant_id,
                success=True,
                resource_type=TenantResourceTypes.RESOURCE_GROUPS.value,
                resource_id=group.id,
                details={
                    "group_name": group.name,
                    "old_limits": old_limits,
                    "new_limits": group.llm_global_limits
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            usage_limit_enforcer.invalidate_group(admin_tenant_id, group_id)

            return LLMGlobalLimitsResponse(
                group_id=group.id,
                group_name=group.name,
                llm_global_limits=group.llm_global_limits,
                effective_llm_limits=group.get_effective_llm_limits() or None,
                inherited_llm_limits=inherited_floor,
                capacity=_compute_capacity_info(group, 'llm_global_limits', db),
            )

        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.error("Failed to set LLM global limits", context={
                "group_id": group_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to set LLM global limits"
            )

    # =========================================================================
    # MCP LIMITS ENDPOINTS
    # =========================================================================

    @router.get("/{group_id}/mcp-global-limits", response_model=MCPGlobalLimitsResponse)
    def get_mcp_global_limits(
        group_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_READ))
    ):
        """
        Get MCP global limits for this group with governance inheritance.

        Returns direct limits, inherited floor from ancestors, and effective limits.
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()

            if not group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User group '{group_id}' not found"
                )

            return MCPGlobalLimitsResponse(
                group_id=group.id,
                group_name=group.name,
                mcp_global_limits=group.mcp_global_limits,
                effective_mcp_limits=group.get_effective_mcp_limits() or None,
                inherited_mcp_limits=_compute_inherited_limits(group, 'mcp_global_limits'),
                capacity=_compute_capacity_info(group, 'mcp_global_limits', db),
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get MCP global limits", context={
                "group_id": group_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve MCP global limits"
            )

    @router.patch("/{group_id}/mcp-global-limits", response_model=MCPGlobalLimitsResponse)
    def update_mcp_global_limits(
        group_id: str,
        limits_data: MCPGlobalLimitsUpdate,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """
        Set MCP global limits for this group.

        Values cannot be less restrictive than the inherited floor from ancestors.
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()

            if not group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User group '{group_id}' not found"
                )

            # Validate limits structure
            group.mcp_global_limits = limits_data.mcp_global_limits
            is_valid, error_message = group.validate_mcp_limits()
            if not is_valid:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid MCP limits: {error_message}"
                )

            # Validate against inherited floor — cannot set less restrictive
            inherited_floor = _compute_inherited_limits(group, 'mcp_global_limits')
            violations = _validate_not_less_restrictive(limits_data.mcp_global_limits, inherited_floor)
            if violations:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Cannot set less restrictive than inherited limits: {'; '.join(violations)}"
                )

            # Validate sibling sum does not exceed parent
            sibling_violations = _validate_siblings_sum(group, limits_data.mcp_global_limits, 'mcp_global_limits', db)
            if sibling_violations:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Sibling limits exceed parent capacity: {'; '.join(sibling_violations)}"
                )

            old_limits = group.mcp_global_limits
            group.mcp_global_limits = limits_data.mcp_global_limits
            group.updated_by = admin.id

            db.flush()
            db.refresh(group)

            block_on(invalidate_proxy_cache(request, "resource_group", admin_tenant_id, group_id, action="update"))

            audit.log(
                action="resource_group_update_mcp_global_limits",
                admin=admin,
                admin_type=admin_type,
                tenant_id=admin_tenant_id,
                success=True,
                resource_type=TenantResourceTypes.RESOURCE_GROUPS.value,
                resource_id=group.id,
                details={
                    "group_name": group.name,
                    "old_limits": old_limits,
                    "new_limits": group.mcp_global_limits
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            usage_limit_enforcer.invalidate_group(admin_tenant_id, group_id)

            return MCPGlobalLimitsResponse(
                group_id=group.id,
                group_name=group.name,
                mcp_global_limits=group.mcp_global_limits,
                effective_mcp_limits=group.get_effective_mcp_limits() or None,
                inherited_mcp_limits=inherited_floor,
                capacity=_compute_capacity_info(group, 'mcp_global_limits', db),
            )

        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.error("Failed to set MCP global limits", context={
                "group_id": group_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to set MCP global limits"
            )

    # =========================================================================
    # SKILL GLOBAL LIMITS ENDPOINTS
    # =========================================================================

    @router.get("/{group_id}/skill-global-limits", response_model=SkillGlobalLimitsResponse)
    def get_skill_global_limits(
        group_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_READ))
    ):
        """Get skill global limits for this group with governance inheritance."""
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()

            if not group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User group '{group_id}' not found"
                )

            return SkillGlobalLimitsResponse(
                group_id=group.id,
                group_name=group.name,
                skill_global_limits=group.skill_global_limits,
                effective_skill_limits=group.get_effective_skill_limits() or None,
                inherited_skill_limits=_compute_inherited_limits(group, 'skill_global_limits'),
                capacity=_compute_capacity_info(group, 'skill_global_limits', db),
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get skill global limits", context={
                "group_id": group_id, "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve skill global limits"
            )

    @router.patch("/{group_id}/skill-global-limits", response_model=SkillGlobalLimitsResponse)
    def update_skill_global_limits(
        group_id: str,
        limits_data: SkillGlobalLimitsUpdate,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Set skill global limits for this group."""
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()

            if not group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User group '{group_id}' not found"
                )

            # Validate limits structure
            group.skill_global_limits = limits_data.skill_global_limits
            is_valid, error_message = group.validate_skill_limits()
            if not is_valid:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid skill limits: {error_message}"
                )

            inherited_floor = _compute_inherited_limits(group, 'skill_global_limits')
            violations = _validate_not_less_restrictive(limits_data.skill_global_limits, inherited_floor)
            if violations:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Cannot set less restrictive than inherited limits: {'; '.join(violations)}"
                )

            # Validate sibling sum does not exceed parent
            sibling_violations = _validate_siblings_sum(group, limits_data.skill_global_limits, 'skill_global_limits', db)
            if sibling_violations:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Sibling limits exceed parent capacity: {'; '.join(sibling_violations)}"
                )

            old_limits = group.skill_global_limits
            group.skill_global_limits = limits_data.skill_global_limits
            group.updated_by = admin.id

            db.flush()
            db.refresh(group)

            block_on(invalidate_proxy_cache(request, "resource_group", admin_tenant_id, group_id, action="update"))

            audit.log(
                action="resource_group_update_skill_global_limits",
                admin=admin,
                admin_type=admin_type,
                tenant_id=admin_tenant_id,
                success=True,
                resource_type=TenantResourceTypes.RESOURCE_GROUPS.value,
                resource_id=group.id,
                details={
                    "group_name": group.name,
                    "old_limits": old_limits,
                    "new_limits": group.skill_global_limits
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            usage_limit_enforcer.invalidate_group(admin_tenant_id, group_id)

            return SkillGlobalLimitsResponse(
                group_id=group.id,
                group_name=group.name,
                skill_global_limits=group.skill_global_limits,
                effective_skill_limits=group.get_effective_skill_limits() or None,
                inherited_skill_limits=inherited_floor,
                capacity=_compute_capacity_info(group, 'skill_global_limits', db),
            )

        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.error("Failed to set skill global limits", context={
                "group_id": group_id, "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to set skill global limits"
            )

    # =========================================================================
    # LLM GLOBAL GOVERNANCE ENDPOINTS
    # =========================================================================

    @router.get("/{group_id}/llm-global-governance", response_model=LLMGlobalGovernanceResponse)
    def get_llm_global_governance(
        group_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_READ))
    ):
        """
        Get LLM global governance for this group with inheritance.

        Returns direct governance, inherited from ancestors, and effective governance.
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()

            if not group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User group '{group_id}' not found"
                )

            return LLMGlobalGovernanceResponse(
                group_id=group.id,
                group_name=group.name,
                llm_global_governance=group.llm_global_governance,
                effective_llm_governance=group.get_effective_llm_governance() or None,
                inherited_llm_governance=_compute_inherited_governance(group, 'llm_global_governance'),
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get LLM global governance", context={
                "group_id": group_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve LLM global governance"
            )

    @router.patch("/{group_id}/llm-global-governance", response_model=LLMGlobalGovernanceResponse)
    def update_llm_global_governance(
        group_id: str,
        governance_data: LLMGlobalGovernanceUpdate,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """
        Set LLM global governance for this group.

        Governance settings are validated before saving.
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()

            if not group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User group '{group_id}' not found"
                )

            # Validate governance structure
            is_valid, error_message = validate_llm_governance(governance_data.governance)
            if not is_valid:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid LLM governance: {error_message}"
                )

            # A child may only tighten what it inherits. Governance is
            # min-wins at runtime, so a looser value would change nothing
            # today and quietly become real the day an ancestor relaxes —
            # refuse it here, the way the limits endpoints already do.
            relaxations = _validate_governance_not_less_restrictive(
                governance_data.governance,
                _compute_inherited_governance(group, 'llm_global_governance'),
                'llm_global_governance',
            )
            if relaxations:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        "Cannot set governance looser than the policy inherited from "
                        "parent groups: " + "; ".join(relaxations)
                    ),
                )

            old_governance = group.llm_global_governance
            group.llm_global_governance = governance_data.governance
            group.updated_by = admin.id

            db.flush()
            db.refresh(group)

            block_on(invalidate_proxy_cache(request, "resource_group", admin_tenant_id, group_id, action="update"))

            audit.log(
                action="resource_group_update_llm_global_governance",
                admin=admin,
                admin_type=admin_type,
                tenant_id=admin_tenant_id,
                success=True,
                resource_type=TenantResourceTypes.RESOURCE_GROUPS.value,
                resource_id=group.id,
                details={
                    "group_name": group.name,
                    "old_governance": old_governance,
                    "new_governance": group.llm_global_governance
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            return LLMGlobalGovernanceResponse(
                group_id=group.id,
                group_name=group.name,
                llm_global_governance=group.llm_global_governance,
                effective_llm_governance=group.get_effective_llm_governance() or None,
                inherited_llm_governance=_compute_inherited_governance(group, 'llm_global_governance'),
            )

        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.error("Failed to set LLM global governance", context={
                "group_id": group_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to set LLM global governance"
            )

    @router.delete("/{group_id}/llm-global-governance", status_code=status.HTTP_204_NO_CONTENT)
    def delete_llm_global_governance(
        group_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Clear LLM global governance for this group."""
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()

            if not group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User group '{group_id}' not found"
                )

            group.llm_global_governance = None
            group.updated_by = admin.id

            db.flush()

            block_on(invalidate_proxy_cache(request, "resource_group", admin_tenant_id, group_id, action="update"))

            audit.log(
                action="resource_group_delete_llm_global_governance",
                admin=admin,
                admin_type=admin_type,
                tenant_id=admin_tenant_id,
                success=True,
                resource_type=TenantResourceTypes.RESOURCE_GROUPS.value,
                resource_id=group.id,
                details={"group_name": group.name},
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.error("Failed to delete LLM global governance", context={
                "group_id": group_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to delete LLM global governance"
            )

    # =========================================================================
    # MCP GLOBAL GOVERNANCE ENDPOINTS
    # =========================================================================

    @router.get("/{group_id}/mcp-global-governance", response_model=MCPGlobalGovernanceResponse)
    def get_mcp_global_governance(
        group_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_READ))
    ):
        """
        Get MCP global governance for this group with inheritance.

        Returns direct governance, inherited from ancestors, and effective governance.
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()

            if not group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User group '{group_id}' not found"
                )

            return MCPGlobalGovernanceResponse(
                group_id=group.id,
                group_name=group.name,
                mcp_global_governance=group.mcp_global_governance,
                effective_mcp_governance=group.get_effective_mcp_governance() or None,
                inherited_mcp_governance=_compute_inherited_governance(group, 'mcp_global_governance'),
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get MCP global governance", context={
                "group_id": group_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve MCP global governance"
            )

    @router.patch("/{group_id}/mcp-global-governance", response_model=MCPGlobalGovernanceResponse)
    def update_mcp_global_governance(
        group_id: str,
        governance_data: MCPGlobalGovernanceUpdate,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """
        Set MCP global governance for this group.

        Governance settings are validated before saving.
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()

            if not group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User group '{group_id}' not found"
                )

            # Validate governance structure
            is_valid, error_message = validate_mcp_governance(governance_data.governance)
            if not is_valid:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid MCP governance: {error_message}"
                )

            # A child may only tighten what it inherits. Governance is
            # min-wins at runtime, so a looser value would change nothing
            # today and quietly become real the day an ancestor relaxes —
            # refuse it here, the way the limits endpoints already do.
            relaxations = _validate_governance_not_less_restrictive(
                governance_data.governance,
                _compute_inherited_governance(group, 'mcp_global_governance'),
                'mcp_global_governance',
            )
            if relaxations:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        "Cannot set governance looser than the policy inherited from "
                        "parent groups: " + "; ".join(relaxations)
                    ),
                )

            old_governance = group.mcp_global_governance
            group.mcp_global_governance = governance_data.governance
            group.updated_by = admin.id

            db.flush()
            db.refresh(group)

            block_on(invalidate_proxy_cache(request, "resource_group", admin_tenant_id, group_id, action="update"))

            audit.log(
                action="resource_group_update_mcp_global_governance",
                admin=admin,
                admin_type=admin_type,
                tenant_id=admin_tenant_id,
                success=True,
                resource_type=TenantResourceTypes.RESOURCE_GROUPS.value,
                resource_id=group.id,
                details={
                    "group_name": group.name,
                    "old_governance": old_governance,
                    "new_governance": group.mcp_global_governance
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            return MCPGlobalGovernanceResponse(
                group_id=group.id,
                group_name=group.name,
                mcp_global_governance=group.mcp_global_governance,
                effective_mcp_governance=group.get_effective_mcp_governance() or None,
                inherited_mcp_governance=_compute_inherited_governance(group, 'mcp_global_governance'),
            )

        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.error("Failed to set MCP global governance", context={
                "group_id": group_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to set MCP global governance"
            )

    @router.delete("/{group_id}/mcp-global-governance", status_code=status.HTTP_204_NO_CONTENT)
    def delete_mcp_global_governance(
        group_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Clear MCP global governance for this group."""
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()

            if not group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User group '{group_id}' not found"
                )

            group.mcp_global_governance = None
            group.updated_by = admin.id

            db.flush()

            block_on(invalidate_proxy_cache(request, "resource_group", admin_tenant_id, group_id, action="update"))

            audit.log(
                action="resource_group_delete_mcp_global_governance",
                admin=admin,
                admin_type=admin_type,
                tenant_id=admin_tenant_id,
                success=True,
                resource_type=TenantResourceTypes.RESOURCE_GROUPS.value,
                resource_id=group.id,
                details={"group_name": group.name},
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.error("Failed to delete MCP global governance", context={
                "group_id": group_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to delete MCP global governance"
            )

    # =========================================================================
    # APPROVAL WINDOW
    # =========================================================================

    @router.get("/{group_id}/approval-window", response_model=ApprovalWindowResponse)
    def get_approval_window(
        request: Request,
        group_id: str,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_READ))
    ):
        """How long a human-approval hold raised for this group stays open
        when the policy that raised it names no window: stated here,
        inherited (min-wins), the tenant default and the platform default."""
        from app.services.approval_window import describe

        admin, _admin_type = admin_and_type
        group = db.query(ResourceGroup).filter(
            ResourceGroup.id == group_id,
            ResourceGroup.tenant_id == acting_tenant_id(request, admin),
        ).first()
        if not group:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail=f"User group '{group_id}' not found")
        return ApprovalWindowResponse(**describe(db, group))

    @router.put("/{group_id}/approval-window", response_model=ApprovalWindowResponse)
    def update_approval_window(
        group_id: str,
        body: ApprovalWindowUpdate,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Set (or clear with null) this group's default approval window.
        A child may only shorten the window it inherits (400)."""
        from app.services.approval_window import describe, relaxation

        admin, admin_type = admin_and_type
        group = db.query(ResourceGroup).filter(
            ResourceGroup.id == group_id,
            ResourceGroup.tenant_id == acting_tenant_id(request, admin),
        ).first()
        if not group:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail=f"User group '{group_id}' not found")
        reason = relaxation(group, body.approval_window_seconds)
        if reason:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=reason)

        old_value = group.approval_window_seconds
        group.approval_window_seconds = body.approval_window_seconds
        group.updated_by = admin.id
        db.flush()
        db.refresh(group)
        block_on(invalidate_proxy_cache(request, "resource_group", acting_tenant_id(request, admin), group_id, action="update"))
        audit.log(
            action="resource_group_update_approval_window",
            admin=admin,
            admin_type=admin_type,
            tenant_id=acting_tenant_id(request, admin),
            success=True,
            resource_type=TenantResourceTypes.RESOURCE_GROUPS.value,
            resource_id=group.id,
            details={
                "group_name": group.name,
                "old_approval_window_seconds": old_value,
                "new_approval_window_seconds": group.approval_window_seconds,
            },
            ip_address=client_ip(request),
            user_agent=request.headers.get('user-agent'),
            request_method=request.method,
            request_path=str(request.url.path),
            db=db,
        )
        return ApprovalWindowResponse(**describe(db, group))

    # =========================================================================
    # SKILL GLOBAL GOVERNANCE ENDPOINTS
    # =========================================================================

    @router.get("/{group_id}/skill-global-governance", response_model=SkillGlobalGovernanceResponse)
    def get_skill_global_governance(
        group_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_READ))
    ):
        """
        Get skill global governance for this group with inheritance.

        Returns direct governance, inherited from ancestors, and effective governance.
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()

            if not group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User group '{group_id}' not found"
                )

            return SkillGlobalGovernanceResponse(
                group_id=group.id,
                group_name=group.name,
                skill_global_governance=group.skill_global_governance,
                effective_skill_governance=group.get_effective_skill_governance() or None,
                inherited_skill_governance=_compute_inherited_skill_governance(group),
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get skill global governance", context={
                "group_id": group_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve skill global governance"
            )

    @router.patch("/{group_id}/skill-global-governance", response_model=SkillGlobalGovernanceResponse)
    def update_skill_global_governance(
        group_id: str,
        governance_data: SkillGovernanceUpdate,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """
        Set skill global governance for this group.

        Governance settings are validated before saving.
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()

            if not group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User group '{group_id}' not found"
                )

            # Validate governance structure
            is_valid, error_message = validate_skill_governance(governance_data.governance)
            if not is_valid:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid skill governance: {error_message}"
                )

            # A child may only tighten what it inherits. Governance is
            # min-wins at runtime, so a looser value would change nothing
            # today and quietly become real the day an ancestor relaxes —
            # refuse it here, the way the limits endpoints already do.
            relaxations = _validate_governance_not_less_restrictive(
                governance_data.governance,
                _compute_inherited_skill_governance(group),
                'skill_global_governance',
            )
            if relaxations:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        "Cannot set governance looser than the policy inherited from "
                        "parent groups: " + "; ".join(relaxations)
                    ),
                )

            old_governance = group.skill_global_governance
            group.skill_global_governance = governance_data.governance
            group.updated_by = admin.id

            db.flush()
            db.refresh(group)

            block_on(invalidate_proxy_cache(request, "resource_group", admin_tenant_id, group_id, action="update"))

            audit.log(
                action="resource_group_update_skill_global_governance",
                admin=admin,
                admin_type=admin_type,
                tenant_id=admin_tenant_id,
                success=True,
                resource_type=TenantResourceTypes.RESOURCE_GROUPS.value,
                resource_id=group.id,
                details={
                    "group_name": group.name,
                    "old_governance": old_governance,
                    "new_governance": group.skill_global_governance
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            return SkillGlobalGovernanceResponse(
                group_id=group.id,
                group_name=group.name,
                skill_global_governance=group.skill_global_governance,
                effective_skill_governance=group.get_effective_skill_governance() or None,
                inherited_skill_governance=_compute_inherited_skill_governance(group),
            )

        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.error("Failed to set skill global governance", context={
                "group_id": group_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to set skill global governance"
            )

    @router.delete("/{group_id}/skill-global-governance", status_code=status.HTTP_204_NO_CONTENT)
    def delete_skill_global_governance(
        group_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Clear skill global governance for this group."""
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()

            if not group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User group '{group_id}' not found"
                )

            group.skill_global_governance = None
            group.updated_by = admin.id

            db.flush()

            block_on(invalidate_proxy_cache(request, "resource_group", admin_tenant_id, group_id, action="update"))

            audit.log(
                action="resource_group_delete_skill_global_governance",
                admin=admin,
                admin_type=admin_type,
                tenant_id=admin_tenant_id,
                success=True,
                resource_type=TenantResourceTypes.RESOURCE_GROUPS.value,
                resource_id=group.id,
                details={"group_name": group.name},
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.error("Failed to delete skill global governance", context={
                "group_id": group_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to delete skill global governance"
            )

    # =========================================================================
    # PER-ITEM LIMIT ENDPOINTS
    # =========================================================================

    @router.get("/{group_id}/llm-models/{model_id}/limits", response_model=LLMModelLimitsResponse)
    def get_llm_model_limits(
        group_id: str,
        model_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_READ))
    ):
        """Get per-model limits for a specific LLM model in this group."""
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()
            if not group:
                raise HTTPException(status_code=404, detail=f"Group '{group_id}' not found")

            from app.database.models.llm_model import LLModel
            model = db.query(LLModel).filter(LLModel.id == model_id, LLModel.tenant_id == admin_tenant_id).first()
            if not model:
                raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")

            # Check direct association first
            row = db.execute(
                resource_group_llm_models.select().where(
                    resource_group_llm_models.c.resource_group_id == group_id,
                    resource_group_llm_models.c.llm_model_id == model_id,
                )
            ).first()

            per_item = row.limits if row else None
            effective_global = group.get_effective_llm_limits() or None

            # If no direct row, check if model is accessible via inheritance
            if not row:
                effective_models = group.get_effective_llm_models()
                model_ids = {m.id for m in effective_models}
                if model_id not in model_ids:
                    raise HTTPException(status_code=404, detail=f"Model '{model_id}' is not accessible in group '{group_id}'")

            # Compute effective = min(global, per_item)
            effective = None
            if effective_global and per_item:
                effective = copy.deepcopy(effective_global)
                effective = restrictive_merge(effective, per_item, LimitFamily.LLM)
            elif per_item:
                effective = copy.deepcopy(per_item)
            elif effective_global:
                effective = copy.deepcopy(effective_global)

            return LLMModelLimitsResponse(
                group_id=group.id,
                group_name=group.name,
                llm_model_id=model.id,
                llm_model_name=model.name,
                limits=per_item,
                effective_global_limits=effective_global,
                effective_limits=effective,
                portal_visible=bool(row.portal_visible) if row is not None else None,
                capacity=_compute_per_model_capacity_info(group, model_id, db),
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get model limits", context={"group_id": group_id, "model_id": model_id, "error": str(e)}, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to retrieve model limits")

    @router.patch("/{group_id}/llm-models/{model_id}/limits", response_model=LLMModelLimitsResponse)
    def update_llm_model_limits(
        group_id: str,
        model_id: str,
        limits_data: ItemLimitsUpdate,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Set per-model limits for a specific LLM model in this group."""
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id, ResourceGroup.tenant_id == admin_tenant_id
            ).first()
            if not group:
                raise HTTPException(status_code=404, detail=f"Group '{group_id}' not found")

            from app.database.models.llm_model import LLModel
            model = db.query(LLModel).filter(LLModel.id == model_id, LLModel.tenant_id == admin_tenant_id).first()
            if not model:
                raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")

            row = db.execute(
                resource_group_llm_models.select().where(
                    resource_group_llm_models.c.resource_group_id == group_id,
                    resource_group_llm_models.c.llm_model_id == model_id,
                )
            ).first()
            if not row:
                raise HTTPException(status_code=404, detail=f"Model '{model_id}' is not assigned to group '{group_id}'")

            # A per-item value above the group's ceiling is discarded by the
            # restrictive merge, so it must not be stored.
            _reject_item_limits_above_group(
                limits_data.limits, group.get_effective_llm_limits() or None, "per-model"
            )

            db.execute(
                resource_group_llm_models.update().where(
                    resource_group_llm_models.c.resource_group_id == group_id,
                    resource_group_llm_models.c.llm_model_id == model_id,
                ).values(limits=limits_data.limits)
            )
            db.flush()

            block_on(invalidate_proxy_cache(request, "resource_group", admin_tenant_id, group_id, action="update"))

            audit.log(
                action="resource_group_update_llm_model_limits",
                admin=admin, admin_type=admin_type, tenant_id=admin_tenant_id,
                success=True, resource_type=TenantResourceTypes.RESOURCE_GROUPS.value, resource_id=group.id,
                details={"group_name": group.name, "model_id": model_id, "model_name": model.name, "limits": limits_data.limits},
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method, request_path=str(request.url.path), db=db
            )

            usage_limit_enforcer.invalidate_group(admin_tenant_id, group_id)

            effective_global = group.get_effective_llm_limits() or None
            effective = None
            if effective_global and limits_data.limits:
                effective = copy.deepcopy(effective_global)
                effective = restrictive_merge(effective, limits_data.limits, LimitFamily.LLM)
            elif limits_data.limits:
                effective = copy.deepcopy(limits_data.limits)
            elif effective_global:
                effective = copy.deepcopy(effective_global)

            return LLMModelLimitsResponse(
                group_id=group.id, group_name=group.name,
                llm_model_id=model.id, llm_model_name=model.name,
                limits=limits_data.limits,
                effective_global_limits=effective_global, effective_limits=effective,
                portal_visible=bool(row.portal_visible) if row is not None else None,
                capacity=_compute_per_model_capacity_info(group, model_id, db),
            )

        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.error("Failed to set model limits", context={"group_id": group_id, "model_id": model_id, "error": str(e)}, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to set model limits")

    @router.patch("/{group_id}/llm-models/{model_id}/portal-visibility", response_model=LLMModelLimitsResponse)
    def update_llm_model_portal_visibility(
        group_id: str,
        model_id: str,
        body: PortalVisibilityUpdate,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Toggle whether a bound LLM model appears in the end-user portal
        model picker.

        Does NOT affect access: a routing member with portal_visible=false
        stays reachable via its routing group; this only controls the
        standalone picker. The Core proxy's /v1/portal/models filters on it.
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id, ResourceGroup.tenant_id == admin_tenant_id
            ).first()
            if not group:
                raise HTTPException(status_code=404, detail=f"Group '{group_id}' not found")

            from app.database.models.llm_model import LLModel
            model = db.query(LLModel).filter(LLModel.id == model_id, LLModel.tenant_id == admin_tenant_id).first()
            if not model:
                raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")

            row = db.execute(
                resource_group_llm_models.select().where(
                    resource_group_llm_models.c.resource_group_id == group_id,
                    resource_group_llm_models.c.llm_model_id == model_id,
                )
            ).first()
            if not row:
                raise HTTPException(status_code=404, detail=f"Model '{model_id}' is not assigned to group '{group_id}'")

            db.execute(
                resource_group_llm_models.update().where(
                    resource_group_llm_models.c.resource_group_id == group_id,
                    resource_group_llm_models.c.llm_model_id == model_id,
                ).values(portal_visible=body.portal_visible)
            )
            db.flush()

            block_on(invalidate_proxy_cache(request, "resource_group", admin_tenant_id, group_id, action="update"))

            audit.log(
                action="resource_group_update_llm_model_portal_visibility",
                admin=admin, admin_type=admin_type, tenant_id=admin_tenant_id,
                success=True, resource_type=TenantResourceTypes.RESOURCE_GROUPS.value, resource_id=group.id,
                details={"group_name": group.name, "model_id": model_id, "model_name": model.name, "portal_visible": body.portal_visible},
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method, request_path=str(request.url.path), db=db
            )

            return LLMModelLimitsResponse(
                group_id=group.id, group_name=group.name,
                llm_model_id=model.id, llm_model_name=model.name,
                limits=row.limits,
                portal_visible=body.portal_visible,
                capacity=_compute_per_model_capacity_info(group, model_id, db),
            )

        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.error("Failed to set model portal visibility", context={"group_id": group_id, "model_id": model_id, "error": str(e)}, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to set model portal visibility")

    @router.delete("/{group_id}/llm-models/{model_id}/limits", status_code=status.HTTP_204_NO_CONTENT)
    def clear_llm_model_limits(
        group_id: str,
        model_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Clear per-model limits for a specific LLM model in this group."""
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id, ResourceGroup.tenant_id == admin_tenant_id
            ).first()
            if not group:
                raise HTTPException(status_code=404, detail=f"Group '{group_id}' not found")

            db.execute(
                resource_group_llm_models.update().where(
                    resource_group_llm_models.c.resource_group_id == group_id,
                    resource_group_llm_models.c.llm_model_id == model_id,
                ).values(limits=None)
            )
            db.flush()

            block_on(invalidate_proxy_cache(request, "resource_group", admin_tenant_id, group_id, action="update"))

            audit.log(
                action="resource_group_clear_llm_model_limits",
                admin=admin, admin_type=admin_type, tenant_id=admin_tenant_id,
                success=True, resource_type=TenantResourceTypes.RESOURCE_GROUPS.value, resource_id=group.id,
                details={"group_name": group.name, "model_id": model_id},
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method, request_path=str(request.url.path), db=db
            )

            usage_limit_enforcer.invalidate_group(admin_tenant_id, group_id)

        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.error("Failed to clear model limits", context={"group_id": group_id, "model_id": model_id, "error": str(e)}, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to clear model limits")

    # --- Per-MCP-Server Limits ---

    @router.get("/{group_id}/mcp-servers/{server_id}/limits", response_model=MCPServerLimitsResponse)
    def get_mcp_server_limits(
        group_id: str,
        server_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_READ))
    ):
        """Get per-server limits for a specific MCP server in this group."""
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id, ResourceGroup.tenant_id == admin_tenant_id
            ).first()
            if not group:
                raise HTTPException(status_code=404, detail=f"Group '{group_id}' not found")

            from app.database.models.server_config import ServerConfig
            server = db.query(ServerConfig).filter(ServerConfig.id == server_id, ServerConfig.tenant_id == admin_tenant_id).first()
            if not server:
                raise HTTPException(status_code=404, detail=f"Server '{server_id}' not found")

            row = db.execute(
                resource_group_server_configs.select().where(
                    resource_group_server_configs.c.resource_group_id == group_id,
                    resource_group_server_configs.c.server_config_id == server_id,
                )
            ).first()

            per_item = row.limits if row else None
            effective_global = group.get_effective_mcp_limits() or None

            # If no direct row, check if server is accessible via inheritance
            if not row:
                effective_servers = group.get_effective_server_configs()
                server_ids = {s.id for s in effective_servers}
                if server_id not in server_ids:
                    raise HTTPException(status_code=404, detail=f"Server '{server_id}' is not accessible in group '{group_id}'")

            effective = None
            if effective_global and per_item:
                effective = copy.deepcopy(effective_global)
                effective = restrictive_merge(effective, per_item, LimitFamily.MCP)
            elif per_item:
                effective = copy.deepcopy(per_item)
            elif effective_global:
                effective = copy.deepcopy(effective_global)

            return MCPServerLimitsResponse(
                group_id=group.id, group_name=group.name,
                server_config_id=server.id, server_config_name=server.name,
                limits=per_item, effective_global_limits=effective_global, effective_limits=effective,
                capacity=_compute_per_server_capacity_info(group, server_id, db),
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get server limits", context={"group_id": group_id, "server_id": server_id, "error": str(e)}, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to retrieve server limits")

    @router.patch(
        "/{group_id}/mcp-servers/{server_id}/limits",
        response_model=MCPServerLimitsResponse,
        description=(
            "Set the group's limits for one MCP server (the binding's `limits`): an MCP limits "
            "document, `McpLimitsDocument`. " + mcp_exec_limits.DOCUMENT_DESCRIPTION
        ),
    )
    def update_mcp_server_limits(
        group_id: str,
        server_id: str,
        limits_data: ItemLimitsUpdate,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Set per-server limits for a specific MCP server in this group."""
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id, ResourceGroup.tenant_id == admin_tenant_id
            ).first()
            if not group:
                raise HTTPException(status_code=404, detail=f"Group '{group_id}' not found")

            from app.database.models.server_config import ServerConfig
            server = db.query(ServerConfig).filter(ServerConfig.id == server_id, ServerConfig.tenant_id == admin_tenant_id).first()
            if not server:
                raise HTTPException(status_code=404, detail=f"Server '{server_id}' not found")

            row = db.execute(
                resource_group_server_configs.select().where(
                    resource_group_server_configs.c.resource_group_id == group_id,
                    resource_group_server_configs.c.server_config_id == server_id,
                )
            ).first()
            if not row:
                raise HTTPException(status_code=404, detail=f"Server '{server_id}' is not assigned to group '{group_id}'")

            # Validate the per-server limits structure (execution / data /
            # frequency / per_tool). ItemLimitsUpdate.limits is an untyped
            # dict, so this is the only structural gate before persistence.
            _require_readable_mcp_limits(limits_data.limits)

            # A per-item value above the group's ceiling is discarded by the
            # restrictive merge, so it must not be stored.
            _reject_item_limits_above_group(
                limits_data.limits, group.get_effective_mcp_limits() or None, "per-server"
            )

            db.execute(
                resource_group_server_configs.update().where(
                    resource_group_server_configs.c.resource_group_id == group_id,
                    resource_group_server_configs.c.server_config_id == server_id,
                ).values(limits=limits_data.limits)
            )
            db.flush()

            block_on(invalidate_proxy_cache(request, "resource_group", admin_tenant_id, group_id, action="update"))

            audit.log(
                action="resource_group_update_mcp_server_limits",
                admin=admin, admin_type=admin_type, tenant_id=admin_tenant_id,
                success=True, resource_type=TenantResourceTypes.RESOURCE_GROUPS.value, resource_id=group.id,
                details={"group_name": group.name, "server_id": server_id, "server_name": server.name, "limits": limits_data.limits},
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method, request_path=str(request.url.path), db=db
            )

            usage_limit_enforcer.invalidate_group(admin_tenant_id, group_id)

            effective_global = group.get_effective_mcp_limits() or None
            effective = None
            if effective_global and limits_data.limits:
                effective = copy.deepcopy(effective_global)
                effective = restrictive_merge(effective, limits_data.limits, LimitFamily.MCP)
            elif limits_data.limits:
                effective = copy.deepcopy(limits_data.limits)
            elif effective_global:
                effective = copy.deepcopy(effective_global)

            return MCPServerLimitsResponse(
                group_id=group.id, group_name=group.name,
                server_config_id=server.id, server_config_name=server.name,
                limits=limits_data.limits,
                effective_global_limits=effective_global, effective_limits=effective,
                capacity=_compute_per_server_capacity_info(group, server_id, db),
            )

        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.error("Failed to set server limits", context={"group_id": group_id, "server_id": server_id, "error": str(e)}, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to set server limits")

    @router.delete("/{group_id}/mcp-servers/{server_id}/limits", status_code=status.HTTP_204_NO_CONTENT)
    def clear_mcp_server_limits(
        group_id: str,
        server_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Clear per-server limits."""
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id, ResourceGroup.tenant_id == admin_tenant_id
            ).first()
            if not group:
                raise HTTPException(status_code=404, detail=f"Group '{group_id}' not found")

            db.execute(
                resource_group_server_configs.update().where(
                    resource_group_server_configs.c.resource_group_id == group_id,
                    resource_group_server_configs.c.server_config_id == server_id,
                ).values(limits=None)
            )
            db.flush()

            block_on(invalidate_proxy_cache(request, "resource_group", admin_tenant_id, group_id, action="update"))

            audit.log(
                action="resource_group_clear_mcp_server_limits",
                admin=admin, admin_type=admin_type, tenant_id=admin_tenant_id,
                success=True, resource_type=TenantResourceTypes.RESOURCE_GROUPS.value, resource_id=group.id,
                details={"group_name": group.name, "server_id": server_id},
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method, request_path=str(request.url.path), db=db
            )

            usage_limit_enforcer.invalidate_group(admin_tenant_id, group_id)

        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.error("Failed to clear server limits", context={"group_id": group_id, "server_id": server_id, "error": str(e)}, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to clear server limits")

    # --- Per-Agent-Skill Limits ---

    @router.get("/{group_id}/agent-skills/{skill_id}/limits", response_model=SkillItemLimitsResponse)
    def get_agent_skill_limits(
        group_id: str,
        skill_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_READ))
    ):
        """Get per-skill limits for a specific agent skill in this group."""
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id, ResourceGroup.tenant_id == admin_tenant_id
            ).first()
            if not group:
                raise HTTPException(status_code=404, detail=f"Group '{group_id}' not found")

            from app.database.models.agent_skill import AgentSkill, resource_group_agent_skills
            skill = db.query(AgentSkill).filter(AgentSkill.id == skill_id, AgentSkill.tenant_id == admin_tenant_id).first()
            if not skill:
                raise HTTPException(status_code=404, detail=f"Skill '{skill_id}' not found")

            row = db.execute(
                resource_group_agent_skills.select().where(
                    resource_group_agent_skills.c.resource_group_id == group_id,
                    resource_group_agent_skills.c.agent_skill_id == skill_id,
                )
            ).first()

            per_item = row.limits if row else None
            effective_global = group.get_effective_skill_limits() or None

            # If no direct row, check if skill is accessible via inheritance
            if not row:
                effective_skills = group.get_effective_agent_skills()
                skill_ids = {s.id for s in effective_skills}
                if skill_id not in skill_ids:
                    raise HTTPException(status_code=404, detail=f"Skill '{skill_id}' is not accessible in group '{group_id}'")

            effective = None
            if effective_global and per_item:
                effective = copy.deepcopy(effective_global)
                effective = restrictive_merge(effective, per_item, LimitFamily.SKILL)
            elif per_item:
                effective = copy.deepcopy(per_item)
            elif effective_global:
                effective = copy.deepcopy(effective_global)

            return SkillItemLimitsResponse(
                group_id=group.id, group_name=group.name,
                agent_skill_id=skill.id, agent_skill_name=skill.name,
                limits=per_item, effective_global_limits=effective_global, effective_limits=effective,
                capacity=_compute_per_skill_capacity_info(group, skill_id, db),
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get skill limits", context={"group_id": group_id, "skill_id": skill_id, "error": str(e)}, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to retrieve skill limits")

    @router.patch("/{group_id}/agent-skills/{skill_id}/limits", response_model=SkillItemLimitsResponse)
    def update_agent_skill_limits(
        group_id: str,
        skill_id: str,
        limits_data: ItemLimitsUpdate,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Set per-skill limits for a specific agent skill in this group."""
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id, ResourceGroup.tenant_id == admin_tenant_id
            ).first()
            if not group:
                raise HTTPException(status_code=404, detail=f"Group '{group_id}' not found")

            from app.database.models.agent_skill import AgentSkill, resource_group_agent_skills
            skill = db.query(AgentSkill).filter(AgentSkill.id == skill_id, AgentSkill.tenant_id == admin_tenant_id).first()
            if not skill:
                raise HTTPException(status_code=404, detail=f"Skill '{skill_id}' not found")

            row = db.execute(
                resource_group_agent_skills.select().where(
                    resource_group_agent_skills.c.resource_group_id == group_id,
                    resource_group_agent_skills.c.agent_skill_id == skill_id,
                )
            ).first()
            if not row:
                raise HTTPException(status_code=404, detail=f"Skill '{skill_id}' is not assigned to group '{group_id}'")

            # A per-item value above the group's ceiling is discarded by the
            # restrictive merge, so it must not be stored.
            _reject_item_limits_above_group(
                limits_data.limits, group.get_effective_skill_limits() or None, "per-skill"
            )

            db.execute(
                resource_group_agent_skills.update().where(
                    resource_group_agent_skills.c.resource_group_id == group_id,
                    resource_group_agent_skills.c.agent_skill_id == skill_id,
                ).values(limits=limits_data.limits)
            )
            db.flush()

            block_on(invalidate_proxy_cache(request, "resource_group", admin_tenant_id, group_id, action="update"))

            audit.log(
                action="resource_group_update_agent_skill_limits",
                admin=admin, admin_type=admin_type, tenant_id=admin_tenant_id,
                success=True, resource_type=TenantResourceTypes.RESOURCE_GROUPS.value, resource_id=group.id,
                details={"group_name": group.name, "skill_id": skill_id, "skill_name": skill.name, "limits": limits_data.limits},
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method, request_path=str(request.url.path), db=db
            )

            usage_limit_enforcer.invalidate_group(admin_tenant_id, group_id)

            effective_global = group.get_effective_skill_limits() or None
            effective = None
            if effective_global and limits_data.limits:
                effective = copy.deepcopy(effective_global)
                effective = restrictive_merge(effective, limits_data.limits, LimitFamily.SKILL)
            elif limits_data.limits:
                effective = copy.deepcopy(limits_data.limits)
            elif effective_global:
                effective = copy.deepcopy(effective_global)

            return SkillItemLimitsResponse(
                group_id=group.id, group_name=group.name,
                agent_skill_id=skill.id, agent_skill_name=skill.name,
                limits=limits_data.limits,
                effective_global_limits=effective_global, effective_limits=effective,
                capacity=_compute_per_skill_capacity_info(group, skill_id, db),
            )

        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.error("Failed to set skill limits", context={"group_id": group_id, "skill_id": skill_id, "error": str(e)}, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to set skill limits")

    @router.delete("/{group_id}/agent-skills/{skill_id}/limits", status_code=status.HTTP_204_NO_CONTENT)
    def clear_agent_skill_limits(
        group_id: str,
        skill_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Clear per-skill limits."""
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id, ResourceGroup.tenant_id == admin_tenant_id
            ).first()
            if not group:
                raise HTTPException(status_code=404, detail=f"Group '{group_id}' not found")

            from app.database.models.agent_skill import resource_group_agent_skills
            db.execute(
                resource_group_agent_skills.update().where(
                    resource_group_agent_skills.c.resource_group_id == group_id,
                    resource_group_agent_skills.c.agent_skill_id == skill_id,
                ).values(limits=None)
            )
            db.flush()

            block_on(invalidate_proxy_cache(request, "resource_group", admin_tenant_id, group_id, action="update"))

            audit.log(
                action="resource_group_clear_agent_skill_limits",
                admin=admin, admin_type=admin_type, tenant_id=admin_tenant_id,
                success=True, resource_type=TenantResourceTypes.RESOURCE_GROUPS.value, resource_id=group.id,
                details={"group_name": group.name, "skill_id": skill_id},
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method, request_path=str(request.url.path), db=db
            )

            usage_limit_enforcer.invalidate_group(admin_tenant_id, group_id)

        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.error("Failed to clear skill limits", context={"group_id": group_id, "skill_id": skill_id, "error": str(e)}, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to clear skill limits")

    # =========================================================================
    # PER-MODEL LLM GOVERNANCE ENDPOINTS
    # =========================================================================

    @router.get("/{group_id}/llm-models/{model_id}/governance", response_model=LLMModelGovernanceResponse)
    def get_llm_model_governance(
        group_id: str,
        model_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_READ))
    ):
        """
        Get LLM governance for a specific model in this group.

        Returns direct governance, inherited floor from ancestors, and effective governance.
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()
            if not group:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Resource group '{group_id}' not found")

            model = db.query(LLModel).filter(LLModel.id == model_id, LLModel.tenant_id == admin_tenant_id).first()
            if not model:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"LLM model '{model_id}' not found")

            # Get direct governance for this group+model
            row = db.execute(
                resource_group_llm_governance.select().where(
                    and_(
                        resource_group_llm_governance.c.resource_group_id == group_id,
                        resource_group_llm_governance.c.llm_model_id == model_id,
                    )
                )
            ).fetchone()

            direct_gov = row.governance if row else None
            inherited_gov = _compute_inherited_model_governance(group, model_id, db)
            effective_gov = _compute_effective_model_governance(group, model_id, db)

            return LLMModelGovernanceResponse(
                group_id=group.id,
                group_name=group.name,
                llm_model_id=model.id,
                llm_model_name=model.name,
                governance=direct_gov,
                effective_governance=effective_gov,
                inherited_governance=inherited_gov,
                has_governance=bool(direct_gov or inherited_gov),
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get LLM model governance", context={
                "group_id": group_id, "model_id": model_id, "error": str(e)
            }, exc_info=True)
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to retrieve LLM model governance")

    @router.patch("/{group_id}/llm-models/{model_id}/governance", response_model=LLMModelGovernanceResponse)
    def update_llm_model_governance(
        group_id: str,
        model_id: str,
        governance_data: LLMModelGovernanceUpdate,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """
        Set LLM governance for a specific model in this group.

        Governance settings are validated. Upserts (creates or updates) the governance row.
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()
            if not group:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Resource group '{group_id}' not found")

            model = db.query(LLModel).filter(LLModel.id == model_id, LLModel.tenant_id == admin_tenant_id).first()
            if not model:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"LLM model '{model_id}' not found")

            # Validate governance structure
            is_valid, error_message = validate_llm_governance(governance_data.governance)
            if not is_valid:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid LLM governance: {error_message}")

            # A per-item policy looser than the group's effective policy is
            # discarded by the restrictive merge, so it must not be stored.
            _reject_item_governance_above_group(
                governance_data.governance,
                group.get_effective_llm_governance() or None,
                'llm_global_governance',
                'per-model',
            )

            # Upsert: check if row exists
            existing = db.execute(
                resource_group_llm_governance.select().where(
                    and_(
                        resource_group_llm_governance.c.resource_group_id == group_id,
                        resource_group_llm_governance.c.llm_model_id == model_id,
                    )
                )
            ).fetchone()

            now = datetime.now(timezone.utc)
            if existing:
                old_governance = existing.governance
                db.execute(
                    resource_group_llm_governance.update().where(
                        resource_group_llm_governance.c.id == existing.id
                    ).values(
                        governance=governance_data.governance,
                        updated_at=now,
                    )
                )
            else:
                old_governance = None
                db.execute(
                    resource_group_llm_governance.insert().values(
                        id=f"rglg-{ULID()}",
                        tenant_id=admin_tenant_id,
                        resource_group_id=group_id,
                        llm_model_id=model_id,
                        governance=governance_data.governance,
                        created_at=now,
                        updated_at=now,
                        created_by=admin.id,
                    )
                )

            db.flush()

            block_on(invalidate_proxy_cache(request, "resource_group", admin_tenant_id, group_id, action="update"))

            audit.log(
                action="resource_group_update_llm_model_governance",
                admin=admin,
                admin_type=admin_type,
                tenant_id=admin_tenant_id,
                success=True,
                resource_type=TenantResourceTypes.RESOURCE_GROUPS.value,
                resource_id=group.id,
                details={
                    "group_name": group.name,
                    "llm_model_id": model_id,
                    "llm_model_name": model.name,
                    "old_governance": old_governance,
                    "new_governance": governance_data.governance,
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            inherited_gov = _compute_inherited_model_governance(group, model_id, db)
            effective_gov = _compute_effective_model_governance(group, model_id, db)

            return LLMModelGovernanceResponse(
                group_id=group.id,
                group_name=group.name,
                llm_model_id=model.id,
                llm_model_name=model.name,
                governance=governance_data.governance,
                effective_governance=effective_gov,
                inherited_governance=inherited_gov,
                has_governance=True,
            )

        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.error("Failed to set LLM model governance", context={
                "group_id": group_id, "model_id": model_id, "error": str(e)
            }, exc_info=True)
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to set LLM model governance")

    @router.delete("/{group_id}/llm-models/{model_id}/governance", status_code=status.HTTP_204_NO_CONTENT)
    def delete_llm_model_governance(
        group_id: str,
        model_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """
        Clear LLM governance for a specific model in this group.

        Removes the direct governance row. Inherited governance from ancestors still applies.
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()
            if not group:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Resource group '{group_id}' not found")

            result = db.execute(
                resource_group_llm_governance.delete().where(
                    and_(
                        resource_group_llm_governance.c.resource_group_id == group_id,
                        resource_group_llm_governance.c.llm_model_id == model_id,
                    )
                )
            )

            db.flush()

            block_on(invalidate_proxy_cache(request, "resource_group", admin_tenant_id, group_id, action="update"))

            audit.log(
                action="resource_group_delete_llm_model_governance",
                admin=admin,
                admin_type=admin_type,
                tenant_id=admin_tenant_id,
                success=True,
                resource_type=TenantResourceTypes.RESOURCE_GROUPS.value,
                resource_id=group.id,
                details={
                    "group_name": group.name,
                    "llm_model_id": model_id,
                    "rows_deleted": result.rowcount,
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.error("Failed to delete LLM model governance", context={
                "group_id": group_id, "model_id": model_id, "error": str(e)
            }, exc_info=True)
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to delete LLM model governance")

    # =========================================================================
    # PER-SKILL GOVERNANCE ENDPOINTS
    # =========================================================================

    @router.get("/{group_id}/agent-skills/{skill_id}/governance", response_model=SkillGovernanceResponse)
    def get_agent_skill_governance(
        group_id: str,
        skill_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_READ))
    ):
        """
        Get governance for a specific skill in this group.

        Returns direct per-skill governance, effective global skill governance, and final effective governance.
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()
            if not group:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Resource group '{group_id}' not found")

            from app.database.models.agent_skill import AgentSkill, resource_group_agent_skills
            skill = db.query(AgentSkill).filter(AgentSkill.id == skill_id, AgentSkill.tenant_id == admin_tenant_id).first()
            if not skill:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Agent skill '{skill_id}' not found")

            # Check direct association
            row = db.execute(
                resource_group_agent_skills.select().where(
                    resource_group_agent_skills.c.resource_group_id == group_id,
                    resource_group_agent_skills.c.agent_skill_id == skill_id,
                )
            ).first()

            per_skill_gov = row.governance if row else None

            # If no direct row, check if skill is accessible via inheritance
            if not row:
                effective_skills = group.get_effective_agent_skills()
                skill_ids = {s.id for s in effective_skills}
                if skill_id not in skill_ids:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Skill '{skill_id}' is not accessible in group '{group_id}'")

            effective_global = group.get_effective_skill_governance() or None

            # Compute effective = restrictive_merge(effective_global, per_skill)
            effective = None
            if effective_global and per_skill_gov:
                effective = _skill_governance_restrictive_merge(copy.deepcopy(per_skill_gov), effective_global)
            elif per_skill_gov:
                effective = copy.deepcopy(per_skill_gov)
            elif effective_global:
                effective = copy.deepcopy(effective_global)

            return SkillGovernanceResponse(
                group_id=group.id,
                group_name=group.name,
                agent_skill_id=skill.id,
                agent_skill_name=skill.name,
                governance=per_skill_gov,
                effective_global_governance=effective_global,
                effective_governance=effective,
                has_governance=bool(per_skill_gov or effective_global),
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get agent skill governance", context={
                "group_id": group_id, "skill_id": skill_id, "error": str(e)
            }, exc_info=True)
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to retrieve agent skill governance")

    @router.patch("/{group_id}/agent-skills/{skill_id}/governance", response_model=SkillGovernanceResponse)
    def update_agent_skill_governance(
        group_id: str,
        skill_id: str,
        governance_data: SkillGovernanceUpdate,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """
        Set governance for a specific skill in this group.

        Governance settings are validated. The skill must be directly associated with the group (not inherited).
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()
            if not group:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Resource group '{group_id}' not found")

            from app.database.models.agent_skill import AgentSkill, resource_group_agent_skills
            skill = db.query(AgentSkill).filter(AgentSkill.id == skill_id, AgentSkill.tenant_id == admin_tenant_id).first()
            if not skill:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Agent skill '{skill_id}' not found")

            # Validate governance structure
            is_valid, error_message = validate_skill_governance(governance_data.governance)
            if not is_valid:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid skill governance: {error_message}")

            # A per-item policy looser than the group's effective policy is
            # discarded by the restrictive merge, so it must not be stored.
            _reject_item_governance_above_group(
                governance_data.governance,
                group.get_effective_skill_governance() or None,
                'skill_global_governance',
                'per-skill',
            )

            # Must be a direct association (not inherited)
            row = db.execute(
                resource_group_agent_skills.select().where(
                    resource_group_agent_skills.c.resource_group_id == group_id,
                    resource_group_agent_skills.c.agent_skill_id == skill_id,
                )
            ).first()
            if not row:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Skill '{skill_id}' is not directly assigned to group '{group_id}'")

            old_governance = row.governance
            db.execute(
                resource_group_agent_skills.update().where(
                    resource_group_agent_skills.c.resource_group_id == group_id,
                    resource_group_agent_skills.c.agent_skill_id == skill_id,
                ).values(governance=governance_data.governance)
            )
            db.flush()

            block_on(invalidate_proxy_cache(request, "skill", admin_tenant_id, skill_id, action="update"))
            block_on(invalidate_proxy_cache(request, "resource_group", admin_tenant_id, group_id, action="update"))

            audit.log(
                action="resource_group_update_agent_skill_governance",
                admin=admin,
                admin_type=admin_type,
                tenant_id=admin_tenant_id,
                success=True,
                resource_type=TenantResourceTypes.RESOURCE_GROUPS.value,
                resource_id=group.id,
                details={
                    "group_name": group.name,
                    "skill_id": skill_id,
                    "skill_name": skill.name,
                    "old_governance": old_governance,
                    "new_governance": governance_data.governance,
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            effective_global = group.get_effective_skill_governance() or None
            effective = None
            if effective_global and governance_data.governance:
                effective = _skill_governance_restrictive_merge(
                    copy.deepcopy(governance_data.governance), effective_global
                )
            elif governance_data.governance:
                effective = copy.deepcopy(governance_data.governance)
            elif effective_global:
                effective = copy.deepcopy(effective_global)

            return SkillGovernanceResponse(
                group_id=group.id,
                group_name=group.name,
                agent_skill_id=skill.id,
                agent_skill_name=skill.name,
                governance=governance_data.governance,
                effective_global_governance=effective_global,
                effective_governance=effective,
                has_governance=True,
            )

        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.error("Failed to set agent skill governance", context={
                "group_id": group_id, "skill_id": skill_id, "error": str(e)
            }, exc_info=True)
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to set agent skill governance")

    @router.delete("/{group_id}/agent-skills/{skill_id}/governance", status_code=status.HTTP_204_NO_CONTENT)
    def delete_agent_skill_governance(
        group_id: str,
        skill_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """
        Clear governance for a specific skill in this group.

        Removes direct per-skill governance. Global skill governance still applies.
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()
            if not group:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Resource group '{group_id}' not found")

            from app.database.models.agent_skill import resource_group_agent_skills
            db.execute(
                resource_group_agent_skills.update().where(
                    resource_group_agent_skills.c.resource_group_id == group_id,
                    resource_group_agent_skills.c.agent_skill_id == skill_id,
                ).values(governance=None)
            )
            db.flush()

            block_on(invalidate_proxy_cache(request, "skill", admin_tenant_id, skill_id, action="update"))
            block_on(invalidate_proxy_cache(request, "resource_group", admin_tenant_id, group_id, action="update"))

            audit.log(
                action="resource_group_delete_agent_skill_governance",
                admin=admin,
                admin_type=admin_type,
                tenant_id=admin_tenant_id,
                success=True,
                resource_type=TenantResourceTypes.RESOURCE_GROUPS.value,
                resource_id=group.id,
                details={
                    "group_name": group.name,
                    "skill_id": skill_id,
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.error("Failed to delete agent skill governance", context={
                "group_id": group_id, "skill_id": skill_id, "error": str(e)
            }, exc_info=True)
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to delete agent skill governance")

    # =========================================================================
    # STATISTICS & FILTERING ENDPOINTS
    # =========================================================================

    @router.get(
        "/{group_id}/usage",
        response_model=GroupUsageResponse,
        summary="Get usage analytics for a resource group",
        description="Returns LLM and MCP usage metrics for a resource group with optional time range filtering. "
                    "Supports both direct-only and hierarchical (roll-up) usage including all descendant groups."
    )
    def get_group_usage(
            request: Request,
            group_id: str,
            period: str = Query("30d", description="Time period: '24h', '7d', '30d', '90d', or 'all'"),
            include_hierarchy: bool = Query(False, description="Include usage from all descendant groups"),
            resource_type: Optional[str] = Query(None, description="Filter by 'llm' or 'mcp'"),
            user_id: Optional[int] = Query(
                None,
                description=(
                    "Restrict the rollup to a single end-user (the gateway "
                    "end_users.id). Same response shape — every metric "
                    "becomes that user's slice of the group. Used by the "
                    "Members tab's per-row Usage modal."
                ),
            ),
            db: Session = Depends(get_db),
            admin_and_type: tuple = Depends(get_current_admin),
            admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_READ))
    ):
        """
        Get usage analytics for a resource group

        Returns aggregated usage metrics including:
        - LLM token consumption and costs
        - MCP tool invocations and execution times
        - Request counts and error rates
        - Top models and tools by usage
        - Daily usage breakdown for charting
        """
        try:
            admin_user, admin_type = admin_and_type
            tenant_id = acting_tenant_id(request, admin_user)

            # Validate group exists
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == tenant_id
            ).first()

            if not group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Resource group '{group_id}' not found"
                )

            # Calculate time range
            from datetime import datetime, timedelta
            now = datetime.now(timezone.utc)
            period_map = {
                "24h": timedelta(hours=24),
                "7d": timedelta(days=7),
                "30d": timedelta(days=30),
                "90d": timedelta(days=90),
            }

            start_date = None
            if period != "all":
                delta = period_map.get(period)
                if not delta:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Invalid period '{period}'. Use '24h', '7d', '30d', '90d', or 'all'"
                    )
                start_date = now - delta

            # Build base query — use parent_group_id walking (immune to renames)
            base_filter = [UsageLog.tenant_id == tenant_id]

            if include_hierarchy:
                # Walk the group tree via parent_group_id relationships
                descendant_ids = get_descendant_group_ids(group)
                all_group_ids = [group_id] + descendant_ids
                base_filter.append(UsageLog.group_id.in_(all_group_ids))
            else:
                base_filter.append(UsageLog.group_id == group_id)

            if start_date:
                base_filter.append(UsageLog.created_at >= start_date)

            if resource_type:
                base_filter.append(UsageLog.resource_type == resource_type)

            # Per-member drill-down filter. `usage_logs.user_id`
            # is the gateway `end_users.id` (Integer FK), so the
            # query param is typed `int`. When unset the rollup
            # spans every member of the group as before.
            if user_id is not None:
                base_filter.append(UsageLog.user_id == user_id)

            # ---- SQL-side aggregation ----
            #
            # This used to fetch every matching usage_logs row as a full
            # ORM entity (`.all()`) and aggregate in Python — measured at
            # 17s for a 30-day window on a year of seeded data, because a
            # busy group accumulates tens of thousands of rows per month.
            # Every figure below is the same rollup pushed into SQL over
            # the composite (tenant_id, group_id, resource_type,
            # created_at) index. Response shape unchanged; semantics
            # preserved (quirks marked inline).
            #
            # Success/error semantics mirror the old model properties:
            #   is_success:  200 <= status_code < 300   (NULL → False)
            #   is_error:    status_code >= 400          (NULL → False)
            # Note the two DIFFERENT error notions carried over from the
            # loop version: per-block `error_count` is "not 2xx"
            # (count - success_count), while the summary's `total_errors`
            # counts only >= 400. That asymmetry predates this rewrite.
            ok_cond = and_(UsageLog.status_code.isnot(None),
                           UsageLog.status_code >= 200, UsageLog.status_code < 300)
            err_cond = and_(UsageLog.status_code.isnot(None), UsageLog.status_code >= 400)

            def _uq(*cols):
                q = db.query(*cols)
                for c in base_filter:
                    q = q.filter(c)
                return q

            def _one(cond):
                return func.sum(case((cond, 1), else_=0))

            # Day bucket, dialect-aware (`to_char` is PG-only; the unit
            # suite runs SQLite whose spelling is `strftime`).
            if db.get_bind().dialect.name == "postgresql":
                u_day = func.to_char(UsageLog.created_at, 'YYYY-MM-DD')
                p_day_expr = lambda col: func.to_char(col, 'YYYY-MM-DD')
            else:
                u_day = func.strftime('%Y-%m-%d', UsageLog.created_at)
                p_day_expr = lambda col: func.strftime('%Y-%m-%d', col)

            # ---- LLM rollup (includes media rows — see the media note
            # below; they share resource_type='llm') ----
            (llm_count, llm_success_count, total_prompt_tokens, total_completion_tokens,
             total_tokens, total_llm_cost, total_prompt_cost, total_completion_cost,
             llm_success_time_sum) = _uq(
                func.count(UsageLog.id),
                _one(ok_cond),
                func.coalesce(func.sum(UsageLog.prompt_tokens), 0),
                func.coalesce(func.sum(UsageLog.completion_tokens), 0),
                func.coalesce(func.sum(UsageLog.total_tokens), 0),
                func.coalesce(func.sum(UsageLog.estimated_cost), 0.0),
                func.coalesce(func.sum(UsageLog.prompt_cost), 0.0),
                func.coalesce(func.sum(UsageLog.completion_cost), 0.0),
                func.coalesce(func.sum(case((ok_cond, func.coalesce(UsageLog.response_time_ms, 0)), else_=0)), 0),
            ).filter(UsageLog.resource_type == 'llm').one()
            llm_count = int(llm_count or 0)
            llm_success_count = int(llm_success_count or 0)
            total_tokens = int(total_tokens or 0)
            total_llm_cost = float(total_llm_cost or 0.0)
            avg_tokens_per_request = round(total_tokens / llm_count, 1) if llm_count else 0
            avg_response_time_ms = round(
                float(llm_success_time_sum) / llm_success_count, 1
            ) if llm_success_count else 0

            # Top models. `provider` was "first seen" in the loop; MAX()
            # is an equally arbitrary representative.
            model_rows = _uq(
                func.coalesce(UsageLog.model_name, 'unknown'),
                func.max(UsageLog.provider),
                func.count(UsageLog.id),
                func.coalesce(func.sum(UsageLog.total_tokens), 0),
                func.coalesce(func.sum(UsageLog.estimated_cost), 0.0),
            ).filter(UsageLog.resource_type == 'llm'
            ).group_by(func.coalesce(UsageLog.model_name, 'unknown')).all()
            top_models = sorted(
                ({"model_name": name, "provider": prov, "request_count": int(cnt),
                  "total_tokens": int(toks or 0), "total_cost": round(float(cost or 0.0), 6)}
                 for name, prov, cnt, toks, cost in model_rows),
                key=lambda x: (x["request_count"], x["total_tokens"]),
                reverse=True,
            )

            llm_metrics = {
                "request_count": llm_count,
                "success_count": llm_success_count,
                "error_count": llm_count - llm_success_count,
                "total_tokens": total_tokens,
                "prompt_tokens": int(total_prompt_tokens or 0),
                "completion_tokens": int(total_completion_tokens or 0),
                "total_cost": round(total_llm_cost, 6),
                "prompt_cost": round(float(total_prompt_cost or 0.0), 6),
                "completion_cost": round(float(total_completion_cost or 0.0), 6),
                "avg_tokens_per_request": avg_tokens_per_request,
                "avg_response_time_ms": avg_response_time_ms,
                "top_models": top_models
            }

            # ---- MCP rollup ----
            (mcp_count, mcp_success_count, total_mcp_calls, total_mcp_cost,
             total_data_bytes, mcp_success_time_sum) = _uq(
                func.count(UsageLog.id),
                _one(ok_cond),
                func.coalesce(func.sum(UsageLog.mcp_call_count), 0),
                func.coalesce(func.sum(UsageLog.estimated_cost), 0.0),
                func.coalesce(func.sum(UsageLog.mcp_data_size_bytes), 0),
                func.coalesce(func.sum(case((ok_cond, func.coalesce(UsageLog.mcp_execution_time_ms, 0)), else_=0)), 0),
            ).filter(UsageLog.resource_type == 'mcp').one()
            mcp_count = int(mcp_count or 0)
            mcp_success_count = int(mcp_success_count or 0)
            total_mcp_cost = float(total_mcp_cost or 0.0)
            avg_execution_time = round(
                float(mcp_success_time_sum) / mcp_success_count, 1
            ) if mcp_success_count else 0

            # Top tools. The loop counted `mcp_call_count or 1` (NULL → 1)
            # and took the first-seen server name.
            tool_rows = _uq(
                func.coalesce(UsageLog.mcp_tool_name, 'unknown'),
                func.max(UsageLog.resource_name),
                func.coalesce(func.sum(func.coalesce(UsageLog.mcp_call_count, 1)), 0),
                func.coalesce(func.sum(func.coalesce(UsageLog.mcp_execution_time_ms, 0)), 0),
                _one(err_cond),
            ).filter(UsageLog.resource_type == 'mcp'
            ).group_by(func.coalesce(UsageLog.mcp_tool_name, 'unknown')).all()
            top_tools = sorted(
                ({"server_name": sname, "tool_name": tname, "call_count": int(calls or 0),
                  "total_execution_time_ms": int(t or 0), "error_count": int(errs or 0)}
                 for tname, sname, calls, t, errs in tool_rows),
                key=lambda x: x["call_count"], reverse=True,
            )

            mcp_metrics = {
                "request_count": mcp_count,
                "success_count": mcp_success_count,
                "error_count": mcp_count - mcp_success_count,
                "total_tool_calls": int(total_mcp_calls or 0),
                "total_cost": round(total_mcp_cost, 6),
                "total_data_bytes": int(total_data_bytes or 0),
                "avg_execution_time_ms": avg_execution_time,
                "top_tools": top_tools
            }

            # ---- Skill rollup ----
            (skill_count, skill_success_count, total_skill_cost,
             skill_success_time_sum) = _uq(
                func.count(UsageLog.id),
                _one(ok_cond),
                func.coalesce(func.sum(UsageLog.estimated_cost), 0.0),
                func.coalesce(func.sum(case((ok_cond, func.coalesce(UsageLog.response_time_ms, 0)), else_=0)), 0),
            ).filter(UsageLog.resource_type == 'skill').one()
            skill_count = int(skill_count or 0)
            skill_success_count = int(skill_success_count or 0)
            total_skill_cost = float(total_skill_cost or 0.0)
            avg_skill_time = round(
                float(skill_success_time_sum) / skill_success_count, 1
            ) if skill_success_count else 0

            skill_key = func.coalesce(UsageLog.resource_name, UsageLog.skill_id, 'unknown')
            skill_rows = _uq(
                skill_key,
                func.max(UsageLog.skill_id),
                func.count(UsageLog.id),
                func.coalesce(func.sum(func.coalesce(UsageLog.response_time_ms, 0)), 0),
                _one(err_cond),
            ).filter(UsageLog.resource_type == 'skill'
            ).group_by(skill_key).all()
            top_skills = sorted(
                ({"skill_name": name, "skill_id": sid, "call_count": int(calls or 0),
                  "total_execution_time_ms": int(t or 0), "error_count": int(errs or 0)}
                 for name, sid, calls, t, errs in skill_rows),
                key=lambda x: x["call_count"], reverse=True,
            )

            skill_metrics = {
                "request_count": skill_count,
                "success_count": skill_success_count,
                "error_count": skill_count - skill_success_count,
                "total_cost": round(total_skill_cost, 6),
                "avg_execution_time_ms": avg_skill_time,
                "top_skills": top_skills,
            }

            # ---- A2A rollup (proxy_logs — A2A doesn't write usage_logs) ----
            from app.database.models.proxy_log import ProxyLog

            a2a_filter = [
                ProxyLog.tenant_id == tenant_id,
                ProxyLog.proxy_type == "a2a",
            ]
            if include_hierarchy:
                a2a_filter.append(ProxyLog.resource_group_id.in_(all_group_ids))
            else:
                a2a_filter.append(ProxyLog.resource_group_id == group_id)
            if start_date:
                a2a_filter.append(ProxyLog.timestamp >= start_date)
            # gateway_user_id is String(255) on proxy_logs; cast to match
            # the Integer usage_logs.user_id drill-down.
            if user_id is not None:
                a2a_filter.append(ProxyLog.gateway_user_id == str(user_id))

            def _aq(*cols):
                q = db.query(*cols)
                for c in a2a_filter:
                    q = q.filter(c)
                return q

            (a2a_count, a2a_success_count, a2a_total_bytes,
             a2a_success_dur_sum) = _aq(
                func.count(ProxyLog.id),
                _one(ProxyLog.success.is_(True)),
                func.coalesce(func.sum(
                    func.coalesce(ProxyLog.request_size_bytes, 0)
                    + func.coalesce(ProxyLog.response_size_bytes, 0)), 0),
                func.coalesce(func.sum(case(
                    (ProxyLog.success.is_(True), func.coalesce(ProxyLog.duration_ms, 0.0)),
                    else_=0.0)), 0.0),
            ).one()
            a2a_count = int(a2a_count or 0)
            a2a_success_count = int(a2a_success_count or 0)
            a2a_avg_duration = round(
                float(a2a_success_dur_sum) / a2a_success_count, 1
            ) if a2a_success_count else 0.0

            from app.database.models.agent_card import AgentCard

            card_rows = _aq(
                func.coalesce(ProxyLog.server_id, 'unknown'),
                func.count(ProxyLog.id),
                _one(ProxyLog.success.is_(True)),
                func.coalesce(func.sum(func.coalesce(ProxyLog.duration_ms, 0.0)), 0.0),
            ).group_by(func.coalesce(ProxyLog.server_id, 'unknown')).all()

            a2a_card_ids = sorted({r[0] for r in card_rows if r[0] and r[0] != 'unknown'})
            a2a_card_names: Dict[str, str] = {}
            if a2a_card_ids:
                for c in db.query(AgentCard.id, AgentCard.name).filter(
                    AgentCard.id.in_(a2a_card_ids)
                ).all():
                    a2a_card_names[c.id] = c.name

            top_cards = sorted(
                ({"card_id": cid,
                  "card_name": a2a_card_names.get(cid, cid),
                  "request_count": int(cnt),
                  "success_count": int(succ or 0),
                  "error_count": int(cnt) - int(succ or 0),
                  "total_duration_ms": float(dur or 0.0)}
                 for cid, cnt, succ, dur in card_rows),
                key=lambda x: x["request_count"], reverse=True,
            )

            cap_rows = _aq(
                ProxyLog.server_id,
                ProxyLog.mcp_capability_name,
                func.count(ProxyLog.id),
                _one(ProxyLog.success.is_(False)),
            ).filter(ProxyLog.mcp_capability_name.isnot(None)
            ).group_by(ProxyLog.server_id, ProxyLog.mcp_capability_name).all()
            top_capabilities = sorted(
                ({"card_id": cid,
                  "card_name": a2a_card_names.get(cid, cid),
                  "capability_name": cap,
                  "call_count": int(calls),
                  "error_count": int(errs or 0)}
                 for cid, cap, calls, errs in cap_rows),
                key=lambda x: x["call_count"], reverse=True,
            )

            a2a_metrics = {
                "request_count": a2a_count,
                "success_count": a2a_success_count,
                "error_count": a2a_count - a2a_success_count,
                "avg_duration_ms": a2a_avg_duration,
                "total_data_bytes": int(a2a_total_bytes or 0),
                "top_cards": top_cards,
                "top_capabilities": top_capabilities,
            }

            # ---- Media rollup (proxy_logs; same cost dollars as the LLM
            # block — see the double-count note on the response below) ----
            media_filter = [
                ProxyLog.tenant_id == tenant_id,
                ProxyLog.proxy_type == "llm",
                or_(
                    ProxyLog.media_images_generated.isnot(None),
                    ProxyLog.media_audio_chars.isnot(None),
                    ProxyLog.media_audio_seconds_input.isnot(None),
                    ProxyLog.media_video_seconds_output.isnot(None),
                ),
            ]
            if include_hierarchy:
                media_filter.append(ProxyLog.resource_group_id.in_(all_group_ids))
            else:
                media_filter.append(ProxyLog.resource_group_id == group_id)
            if start_date:
                media_filter.append(ProxyLog.timestamp >= start_date)
            if user_id is not None:
                media_filter.append(ProxyLog.gateway_user_id == str(user_id))

            ENDPOINT_TO_MODE = {
                "/v1/proxy/llm/images/generations": "image_generation",
                "/v1/proxy/llm/audio/speech": "audio_speech",
                "/v1/proxy/llm/audio/transcriptions": "audio_transcription",
                "/v1/proxy/llm/video/generations": "video_generation",
            }
            mq = db.query(
                ProxyLog.endpoint,
                func.count(ProxyLog.id),
                func.coalesce(func.sum(func.coalesce(ProxyLog.llm_estimated_cost_usd, 0.0)), 0.0),
                func.coalesce(func.sum(func.coalesce(ProxyLog.media_images_generated, 0)), 0),
                func.coalesce(func.sum(func.coalesce(ProxyLog.media_audio_chars, 0)), 0),
                func.coalesce(func.sum(func.coalesce(ProxyLog.media_audio_seconds_input, 0.0)), 0.0),
                func.coalesce(func.sum(func.coalesce(ProxyLog.media_video_seconds_output, 0.0)), 0.0),
            )
            for c in media_filter:
                mq = mq.filter(c)
            media_rows = mq.group_by(ProxyLog.endpoint).all()

            media_modes: dict = {}
            media_request_count = 0
            media_total_cost = 0.0
            for ep, cnt, cost, imgs, chars, aud_s, vid_s in media_rows:
                media_request_count += int(cnt)
                media_total_cost += float(cost or 0.0)
                mode = ENDPOINT_TO_MODE.get(ep or "")
                if not mode:
                    # Unknown endpoint with a media counter — counted in the
                    # totals (as before) but not bucketed into a wrong mode.
                    continue
                slot = media_modes.setdefault(mode, {
                    "request_count": 0, "cost_usd": 0.0, "images_generated": 0,
                    "audio_chars": 0, "audio_seconds_input": 0.0,
                    "video_seconds_output": 0.0,
                })
                slot["request_count"] += int(cnt)
                slot["cost_usd"] = round(slot["cost_usd"] + float(cost or 0.0), 6)
                slot["images_generated"] += int(imgs or 0)
                slot["audio_chars"] += int(chars or 0)
                slot["audio_seconds_input"] = round(slot["audio_seconds_input"] + float(aud_s or 0.0), 2)
                slot["video_seconds_output"] = round(slot["video_seconds_output"] + float(vid_s or 0.0), 2)

            media_metrics = {
                "request_count": media_request_count,
                "estimated_cost_usd": round(media_total_cost, 6),
                "modes": media_modes,
            }

            # ---- Daily breakdown (usage_logs × resource_type + a2a) ----
            daily_map: dict = {}

            def _ensure_day(day: str) -> dict:
                if day not in daily_map:
                    daily_map[day] = {
                        "date": day,
                        "llm_requests": 0, "mcp_requests": 0, "a2a_requests": 0,
                        "skill_requests": 0,
                        "total_tokens": 0, "total_cost": 0.0,
                        "error_count": 0,
                    }
                return daily_map[day]

            for day, rtype, cnt, toks, cost, errs in _uq(
                func.coalesce(u_day, 'unknown'),
                UsageLog.resource_type,
                func.count(UsageLog.id),
                func.coalesce(func.sum(UsageLog.total_tokens), 0),
                func.coalesce(func.sum(UsageLog.estimated_cost), 0.0),
                _one(err_cond),
            ).group_by(func.coalesce(u_day, 'unknown'), UsageLog.resource_type).all():
                slot = _ensure_day(day)
                if rtype == 'llm':
                    slot["llm_requests"] += int(cnt)
                    slot["total_tokens"] += int(toks or 0)
                elif rtype == 'mcp':
                    slot["mcp_requests"] += int(cnt)
                elif rtype == 'skill':
                    slot["skill_requests"] += int(cnt)
                slot["total_cost"] += float(cost or 0.0)
                slot["error_count"] += int(errs or 0)

            p_day = p_day_expr(ProxyLog.timestamp)
            for day, cnt, fails in _aq(
                func.coalesce(p_day, 'unknown'),
                func.count(ProxyLog.id),
                _one(ProxyLog.success.is_(False)),
            ).group_by(func.coalesce(p_day, 'unknown')).all():
                slot = _ensure_day(day)
                slot["a2a_requests"] += int(cnt)
                slot["error_count"] += int(fails or 0)

            daily_breakdown = sorted(daily_map.values(), key=lambda x: x["date"])
            for d in daily_breakdown:
                d["total_cost"] = round(d["total_cost"], 6)

            # ---- Summary ----
            (usage_total, usage_errors, uniq_users) = _uq(
                func.count(UsageLog.id),
                _one(err_cond),
                func.count(func.distinct(UsageLog.user_id)),
            ).one()
            total_requests = int(usage_total or 0) + a2a_count
            total_errors = int(usage_errors or 0) + (a2a_count - a2a_success_count)
            total_cost = round(total_llm_cost + total_mcp_cost + total_skill_cost, 6)
            unique_users = int(uniq_users or 0)

            if include_hierarchy:
                descendant_groups_with_usage = int(_uq(
                    func.count(func.distinct(UsageLog.group_id)),
                ).filter(UsageLog.group_id.isnot(None),
                         UsageLog.group_id != group_id).one()[0] or 0)
            else:
                descendant_groups_with_usage = 0

            return GroupUsageResponse(
                group_id=group_id,
                group_name=group.display_name or group.name,
                period=period,
                include_hierarchy=include_hierarchy,
                start_date=start_date.isoformat() if start_date else None,
                end_date=now.isoformat(),
                summary=UsageSummary(
                    total_requests=total_requests,
                    total_errors=total_errors,
                    error_rate=round((total_errors / total_requests * 100), 2) if total_requests > 0 else 0,
                    total_cost=total_cost,
                    unique_users=unique_users,
                    descendant_groups=descendant_groups_with_usage
                ),
                llm=llm_metrics,
                mcp=mcp_metrics,
                skill=skill_metrics,
                a2a=a2a_metrics,
                # `media` is informational — the same rows already
                # contribute to ``llm.total_cost`` (and therefore
                # ``summary.total_cost``) because record_multimodal_outcome
                # writes them into ``usage_logs`` with
                # ``resource_type='llm'`` and ``estimated_cost`` set.
                # This panel surfaces the typed billing-unit counters
                # (images, chars, seconds) that the token-based LLM
                # block can't represent. Adding
                # ``media.estimated_cost_usd`` to ``summary.total_cost``
                # again would double-count.
                media=media_metrics,
                daily_breakdown=daily_breakdown
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get group usage", context={
                "group_id": group_id,
                "error": str(e),
                "error_type": type(e).__name__
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve group usage analytics"
            )

    @router.get(
        "/{group_id}/usage-alerts",
        response_model=GroupAlertResponse,
        summary="Get usage alerts for a resource group",
        description="Returns usage alerts for a resource group, filtered by status, type, and resource."
    )
    def get_group_alerts(
            request: Request,
            group_id: str,
            alert_status: Optional[str] = Query(
                None,
                alias="status",
                description="Filter by status: 'active', 'acknowledged', 'resolved'. Default: all statuses."
            ),
            resource_type: Optional[str] = Query(
                None,
                description="Filter by resource type: 'llm' or 'mcp'"
            ),
            alert_type: Optional[str] = Query(
                None,
                description="Filter by alert type: 'warning' or 'hard_stop'"
            ),
            limit: int = Query(50, ge=1, le=200, description="Maximum number of alerts to return"),
            db: Session = Depends(get_db),
            admin_and_type: tuple = Depends(get_current_admin),
            admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_READ))
    ):
        """
        Get usage alerts for a resource group.

        Returns threshold crossing alerts with filtering options.
        Active hard_stop alerts are sorted first, then by utilization percentage.

        **Status values:**
        - `active` — Threshold crossed, not yet acknowledged
        - `acknowledged` — Admin has seen and acknowledged the alert
        - `resolved` — Period expired or limit was increased

        **Examples:**
        - `GET /{group_id}/usage-alerts` — All alerts
        - `GET /{group_id}/usage-alerts?status=active` — Only unacknowledged alerts
        - `GET /{group_id}/usage-alerts?status=active&type=hard_stop` — Active hard stops only
        - `GET /{group_id}/usage-alerts?resource_type=llm` — LLM alerts only
        """
        try:
            admin_user, admin_type_val = admin_and_type
            tenant_id = acting_tenant_id(request, admin_user)

            # Validate group exists and belongs to tenant
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == tenant_id
            ).first()

            if not group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Resource group '{group_id}' not found"
                )

            # Validate filter values
            valid_statuses = {'active', 'acknowledged', 'resolved'}
            if alert_status and alert_status not in valid_statuses:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid status '{alert_status}'. Must be one of: {', '.join(sorted(valid_statuses))}"
                )

            if resource_type:
                try:
                    ProxyType(resource_type)
                except ValueError:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Invalid resource_type '{resource_type}'. Must be one of: {', '.join(t.value for t in ProxyType)}"
                    )

            valid_alert_types = {'warning', 'hard_stop'}
            if alert_type and alert_type not in valid_alert_types:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid alert type '{alert_type}'. Must be 'warning' or 'hard_stop'"
                )

            # Build query
            query = db.query(UsageAlert).filter(
                UsageAlert.tenant_id == tenant_id,
                UsageAlert.group_id == group_id
            )

            if alert_status:
                query = query.filter(UsageAlert.status == alert_status)

            if resource_type:
                query = query.filter(UsageAlert.resource_type == resource_type)

            if alert_type:
                query = query.filter(UsageAlert.alert_type == alert_type)

            alerts = query.order_by(
                UsageAlert.alert_type.desc(),      # hard_stop first
                UsageAlert.utilization_pct.desc(),  # highest utilization first
                UsageAlert.created_at.desc()        # newest first
            ).limit(limit).all()

            # Compute summary counts
            active_count = sum(1 for a in alerts if a.status == 'active')
            hard_stop_count = sum(1 for a in alerts if a.alert_type == 'hard_stop' and a.status == 'active')

            return GroupAlertResponse(
                group_id=group_id,
                group_name=group.display_name or group.name,
                alerts=[a.to_dict() for a in alerts],
                total_count=len(alerts),
                active_count=active_count,
                hard_stop_count=hard_stop_count
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get group usage alerts", context={
                "group_id": group_id,
                "error": str(e),
                "error_type": type(e).__name__
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve group usage alerts"
            )

    @router.get("/{group_id}/members", response_model=GroupMemberListResponse)
    def get_group_members(
        group_id: str,
        request: Request,
        include_descendants: bool = Query(False, description="Include members from descendant groups"),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_READ))
    ):
        """
        Get all members of a resource group (direct + inherited from parents)

        Returns direct members and inherited members from parent groups with source information.
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()

            if not group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User group '{group_id}' not found"
                )

            all_members = []
            seen_user_ids = set()

            # Get direct members (from this group only)
            direct_memberships = db.query(ResourceGroupMembership).filter(
                ResourceGroupMembership.group_id == group_id,
                ResourceGroupMembership.tenant_id == admin_tenant_id
            ).all()

            for membership in direct_memberships:
                user = membership.user
                if user and str(user.id) not in seen_user_ids:
                    all_members.append(GroupMemberInfo(
                        user_id=str(user.id),
                        username=user.username,
                        email=user.email,
                        display_name=user.display_name,
                        avatar_url=getattr(user, 'avatar_url', None),
                        role=membership.role,
                        joined_at=membership.joined_at.isoformat() if membership.joined_at else None,
                        is_inherited=False,
                        is_primary=(user.primary_group_id == group_id),
                    ))
                    seen_user_ids.add(str(user.id))

            # Get inherited members from parent groups (if not include_descendants)
            if not include_descendants:
                inherited_members = get_inherited_members(group, db)
                for member in inherited_members:
                    if member.user_id not in seen_user_ids:
                        all_members.append(member)
                        seen_user_ids.add(member.user_id)

            # If include_descendants, get members from all descendant groups
            if include_descendants:
                descendant_ids = [g.id for g in group.get_descendants(include_self=False)]
                if descendant_ids:
                    desc_memberships = db.query(ResourceGroupMembership).filter(
                        ResourceGroupMembership.tenant_id == admin_tenant_id,
                        ResourceGroupMembership.group_id.in_(descendant_ids)
                    ).all()

                    for membership in desc_memberships:
                        user = membership.user
                        if user and str(user.id) not in seen_user_ids:
                            # Get the descendant group info
                            desc_group = db.query(ResourceGroup).filter(
                                ResourceGroup.id == membership.group_id
                            ).first()

                            all_members.append(GroupMemberInfo(
                                user_id=str(user.id),
                                username=user.username,
                                email=user.email,
                                display_name=user.display_name,
                                avatar_url=getattr(user, 'avatar_url', None),
                                role=membership.role,
                                joined_at=membership.joined_at.isoformat() if membership.joined_at else None,
                                is_inherited=True,
                                source_group_id=desc_group.id if desc_group else None,
                                source_group_name=desc_group.name if desc_group else None,
                                is_primary=(user.primary_group_id == membership.group_id),
                            ))
                            seen_user_ids.add(str(user.id))

            return GroupMemberListResponse(
                group_id=group_id,
                group_name=group.name,
                members=all_members,
                total=len(all_members)
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get group members", context={
                "group_id": group_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve group members"
            )

    @router.post("/{group_id}/members")
    def add_group_members(
        group_id: str,
        member_data: GroupMembershipAdd,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """
        Add members to a resource group with hierarchy validation.

        Enforces the rule: A user can only be a direct member of ONE group in any hierarchy.
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)
        admin_id = admin.id

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()

            if not group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User group '{group_id}' not found"
                )

            # Get ancestor and descendant group IDs for hierarchy validation
            ancestor_ids = get_ancestor_group_ids(group)
            descendant_ids = get_descendant_group_ids(group)
            hierarchy_group_ids = ancestor_ids + descendant_ids + [group_id]

            added = []
            skipped = []

            for user_id in member_data.user_ids:
                user = db.query(EndUser).filter(
                    EndUser.id == user_id,
                    EndUser.tenant_id == admin_tenant_id
                ).first()

                if not user:
                    skipped.append({
                        "user_id": str(user_id),
                        "reason": "User not found"
                    })
                    continue

                # HIERARCHY VALIDATION: Check if user is already a member in this hierarchy
                existing_membership = db.query(ResourceGroupMembership).filter(
                    ResourceGroupMembership.user_id == user_id,
                    ResourceGroupMembership.group_id.in_(hierarchy_group_ids),
                    ResourceGroupMembership.tenant_id == admin_tenant_id
                ).first()

                if existing_membership:
                    # Get the conflicting group for better error message
                    conflict_group = db.query(ResourceGroup).filter(
                        ResourceGroup.id == existing_membership.group_id
                    ).first()

                    if existing_membership.group_id == group_id:
                        skipped.append({
                            "user_id": str(user_id),
                            "username": user.username,
                            "reason": "Already a member of this group"
                        })
                    elif existing_membership.group_id in ancestor_ids:
                        skipped.append({
                            "user_id": str(user_id),
                            "username": user.username,
                            "reason": f"Already a member of parent group '{conflict_group.name}' (hierarchy rule)"
                        })
                    elif existing_membership.group_id in descendant_ids:
                        skipped.append({
                            "user_id": str(user_id),
                            "username": user.username,
                            "reason": f"Already a member of child group '{conflict_group.name}' (hierarchy rule)"
                        })
                    continue

                # Add the member
                membership = ResourceGroupMembership(
                    tenant_id=admin_tenant_id,
                    user_id=user_id,
                    group_id=group_id,
                    role=member_data.role,
                    added_by=admin_id
                )

                db.add(membership)

                # Primary group semantics:
                # - First group a user ever joins → auto-set as primary
                # - Subsequent groups → only set primary if admin opts in
                #   via member_data.make_primary (default False)
                primary_set = False
                if user.primary_group_id is None:
                    user.primary_group_id = group_id
                    primary_set = True
                elif member_data.make_primary and user.primary_group_id != group_id:
                    user.primary_group_id = group_id
                    primary_set = True

                added.append({
                    "user_id": str(user_id),
                    "username": user.username,
                    "primary": primary_set,
                })

            db.flush()

            # Send notification to newly added users about their resources
            from app.database.models.notification import Notification
            from ulid import ULID as NotifULID

            for member_info in added:
                uid = int(member_info["user_id"])
                # Gather resource names for the notification body
                model_names = [m.name for m in group.llm_models][:5]
                server_names = [s.name for s in group.server_configs][:5]
                skill_names = [s.name for s in group.agent_skills][:5]

                body_parts = []
                if model_names:
                    body_parts.append(f"**AI Models:** {', '.join(model_names)}")
                if server_names:
                    body_parts.append(f"**Data Access:** {', '.join(server_names)}")
                if skill_names:
                    body_parts.append(f"**Agent Skills:** {', '.join(skill_names)}")

                notif_body = "You now have access to the following resources:\n\n" + "\n\n".join(body_parts) if body_parts else "You now have access to this group's resources."

                notif = Notification(
                    id=f"notif-{NotifULID()}",
                    tenant_id=admin_tenant_id,
                    user_id=uid,
                    notification_type="group_membership",
                    title=f"You've been added to {group.display_name or group.name}",
                    body=notif_body,
                    notification_metadata={
                        "group_id": group.id,
                        "group_name": group.display_name or group.name,
                        "group_path": group.hierarchy_path,
                        "added_by": admin.username,
                    },
                )
                db.add(notif)

            db.flush()

            block_on(invalidate_proxy_cache(request, "membership", admin_tenant_id, group_id, action="create"))

            logger.info("Group members added", context={
                "group_id": group_id,
                "tenant_id": admin_tenant_id,
                "added_count": len(added),
                "skipped_count": len(skipped)
            })

            audit.log(
                action="resource_group_add_member",
                admin=admin,
                admin_type=admin_type,
                tenant_id=admin_tenant_id,
                success=True,
                resource_type="resource_group_membership",
                resource_id=group_id,
                details={
                    "group_id": group_id,
                    "group_name": group.name,
                    "added": added,
                    "skipped": skipped
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            return {
                "message": f"Added {len(added)} member(s) to group '{group.name}'",
                "added": added,
                "skipped": skipped,
                "total_added": len(added),
                "total_skipped": len(skipped)
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to add group members", context={
                "group_id": group_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to add members to group"
            )

    @router.delete("/{group_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
    def remove_group_member(
        group_id: str,
        user_id: int,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """
        Remove a member from a user group
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)
        admin_id = admin.id

        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()

            if not group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Resource group '{group_id}' not found"
                )

            membership = db.query(ResourceGroupMembership).filter(
                ResourceGroupMembership.group_id == group_id,
                ResourceGroupMembership.user_id == user_id,
                ResourceGroupMembership.tenant_id == admin_tenant_id
            ).first()

            if not membership:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User with ID '{user_id}' is not a member of group '{group.name}'"
                )

            user = db.query(EndUser).filter_by(id=user_id).first()
            username = user.username if user else str(user_id)

            # Gather resource names before removing membership
            model_names = [m.name for m in group.llm_models][:5]
            server_names = [s.name for s in group.server_configs][:5]
            skill_names = [s.name for s in group.agent_skills][:5]

            db.delete(membership)
            db.flush()

            # Primary-group cleanup: if the removed group was this user's
            # primary, promote another remaining membership (oldest first)
            # or clear the field. Keeps the invariant that a user with
            # memberships always has a primary_group_id set.
            if user and user.primary_group_id == group_id:
                next_membership = db.query(ResourceGroupMembership).filter(
                    ResourceGroupMembership.user_id == user_id,
                    ResourceGroupMembership.tenant_id == admin_tenant_id,
                ).order_by(ResourceGroupMembership.joined_at.asc()).first()
                user.primary_group_id = (
                    next_membership.group_id if next_membership else None
                )
                db.flush()

            # Notify user about removed access
            from app.database.models.notification import Notification
            from ulid import ULID as NotifULID

            body_parts = []
            if model_names:
                body_parts.append(f"**AI Models:** {', '.join(model_names)}")
            if server_names:
                body_parts.append(f"**Data Access:** {', '.join(server_names)}")
            if skill_names:
                body_parts.append(f"**Agent Skills:** {', '.join(skill_names)}")

            notif_body = (
                "You no longer have access to the following resources:\n\n" + "\n\n".join(body_parts)
                if body_parts else "You no longer have access to this group's resources."
            )

            notif = Notification(
                id=f"notif-{NotifULID()}",
                tenant_id=admin_tenant_id,
                user_id=user_id,
                notification_type="group_membership",
                title=f"You've been removed from {group.display_name or group.name}",
                body=notif_body,
                notification_metadata={
                    "group_id": group.id,
                    "group_name": group.display_name or group.name,
                    "group_path": group.hierarchy_path,
                    "removed_by": admin.username,
                    "action": "removed",
                },
            )
            db.add(notif)
            db.flush()

            block_on(invalidate_proxy_cache(request, "membership", admin_tenant_id, group_id, action="delete"))

            logger.info("Group member removed", context={
                "group_id": group_id,
                "user_id": user_id,
                "username": username,
                "tenant_id": admin_tenant_id
            })

            audit.log(
                action="resource_group_remove_member",
                admin=admin,
                admin_type=admin_type,
                tenant_id=admin_tenant_id,
                success=True,
                resource_type="resource_group_membership",
                resource_id=group_id,
                details={
                    "group_id": group_id,
                    "group_name": group.name,
                    "user_id": user_id,
                    "username": username
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            return None

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to remove group member", context={
                "group_id": group_id,
                "user_id": user_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to remove member from group"
            )

    # =========================================================================
    # GROUP-SCOPED API KEYS ENDPOINTS
    # =========================================================================

    @router.get(
        "/{group_id}/api-keys",
        response_model=GroupAPIKeysResponse,
        summary="List API keys for a resource group",
        description="Get all API keys scoped to this resource group"
    )
    def get_group_api_keys(
            group_id: str,
            request: Request,
            db: Session = Depends(get_db),
            admin_and_type: tuple = Depends(get_current_admin),
            admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_READ))
    ):
        """
        Get all API keys scoped to this resource group

        Returns both enabled and disabled keys (but not revoked ones by default)
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            # Verify group exists and belongs to tenant
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()

            if not group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Resource group '{group_id}' not found"
                )

            # Get all non-revoked keys for this group
            keys = db.query(APIKey).filter(
                APIKey.group_id == group_id,
                APIKey.tenant_id == admin_tenant_id,
                APIKey.revoked == False
            ).all()

            # Convert to summary dicts and add access_mode
            keys_data = []
            for key in keys:
                key_dict = key.to_summary_dict()
                # Map scope to access_mode for UI
                key_dict['access_mode'] = 'user' if key.scope == 'user' else 'shared'
                # Ensure user_id is int not str
                if key.user_id:
                    key_dict['user_id'] = key.user_id
                keys_data.append(key_dict)

            logger.debug(f"Retrieved {len(keys_data)} keys for group", context={
                "group_id": group_id,
                "total_keys": len(keys_data)
            })

            return GroupAPIKeysResponse(
                group_id=group.id,
                group_name=group.name,
                group_type=group.group_type.value if group.group_type else None,
                keys=keys_data,
                total=len(keys_data)
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get group API keys", context={
                "group_id": group_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve group API keys"
            )

    def _refuse_key_by_identity_policy(db, tenant_id: str, group_id, **check):
        """422 when the effective identity policy of the key's group refuses
        the key (`identity_policy.check_key`)."""
        from app.services import identity_policy

        group = None
        if group_id:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id, ResourceGroup.tenant_id == tenant_id
            ).first()
        resolution = (
            identity_policy.resolve_group(db, group) if group is not None
            else identity_policy.resolve_ungrouped(db, tenant_id)
        )
        try:
            identity_policy.check_key(resolution, now=datetime.now(timezone.utc), **check)
        except identity_policy.KeyRefused as refused:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=refused.detail()
            )

    def _resolve_agent_binding(db, tenant_id: str, agent_id):
        """Validate an agent-identity id for key binding. Returns the
        normalized id (or None). 404s on an unknown/foreign agent so a typo
        can't silently create an unbound key that LOOKS bound."""
        from app.database.models.agent_identity import AgentIdentity
        agent_id = (agent_id or "").strip() or None
        if agent_id is None:
            return None
        agent = db.query(AgentIdentity).filter(
            AgentIdentity.id == agent_id,
            AgentIdentity.tenant_id == tenant_id,
        ).first()
        if not agent:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Agent identity '{agent_id}' not found",
            )
        return agent.id

    @router.post(
        "/{group_id}/api-keys",
        response_model=APIKeyCreatedResponse,
        status_code=status.HTTP_201_CREATED,
        summary="Create a new API key for resource group",
        description="Generate a new API key for accessing this resource group's resources. The full key is only shown once!"
    )
    def create_group_access_key(
            group_id: str,
            key_data: APIKeyCreate,
            request: Request,
            db: Session = Depends(get_db),
            admin_and_type: tuple = Depends(get_current_admin),
            admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ) -> APIKeyCreatedResponse:
        """
        Create a new API key scoped to this resource group

        **Access Modes:**
        - **shared**: Key can be used by anyone (maps to scope='global')
        - **user**: Key is tied to a specific user (maps to scope='user')

        **Important:** The full API key is only shown in this response.
        Store it securely - it cannot be retrieved later!
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)
        admin_id = admin.id

        try:
            # Verify group exists and belongs to tenant
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()

            if not group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Resource group '{group_id}' not found"
                )

            # Check for duplicate name within this group
            existing = db.query(APIKey).filter(
                and_(
                    APIKey.tenant_id == admin_tenant_id,
                    APIKey.group_id == group_id,
                    APIKey.name == key_data.name,
                    APIKey.revoked == False
                )
            ).first()

            if existing:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"API key with name '{key_data.name}' already exists in this group"
                )

            # Map access_mode to scope
            # UI sends: 'shared' or 'user'
            # DB expects: 'global' or 'user'
            scope = 'user' if key_data.access_mode == 'user' else 'global'

            # What the group's identity policy would refuse at use is refused
            # here: a key where the policy needs more than a key, a personal
            # key where it refuses them, an expiry beyond its ceiling.
            _refuse_key_by_identity_policy(
                db, admin_tenant_id, group_id,
                personal=scope == 'user', expires_at=key_data.expires_at,
                creating=True, expiry_changed=True,
            )

            # Optional agent-identity binding (actor attribution for agent authz).
            bound_agent_id = _resolve_agent_binding(db, admin_tenant_id, key_data.agent_id)

            # Validate and fetch user if user-specific key
            user_email = None
            if scope == 'user':
                if not key_data.user_id:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="user_id is required for user-specific keys (access_mode='user')"
                    )

                user = db.query(EndUser).filter(
                    and_(
                        EndUser.id == key_data.user_id,
                        EndUser.tenant_id == admin_tenant_id
                    )
                ).first()

                if not user:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"User with ID {key_data.user_id} not found"
                    )

                user_email = user.email

            # Generate API key (format: sk_brutor_api_xxxxxxxxxxxxx...)
            full_key, key_hash, key_prefix = AuthenticationContext.generate_api_key()

            # Generate API Key ID
            api_key_id = f"apikey-{ULID()}"

            # Create key record
            new_key = APIKey(
                id=api_key_id,
                tenant_id=admin_tenant_id,
                name=key_data.name,
                description=key_data.description,
                key_hash=key_hash,
                key_prefix=key_prefix,
                scope=scope,
                user_id=key_data.user_id if scope == 'user' else None,
                user_email=user_email,
                group_id=group_id,  # Associate with resource group
                agent_id=bound_agent_id,
                expires_at=key_data.expires_at,
                enabled=key_data.enabled if key_data.enabled is not None else True,
                created_by=admin_id
            )

            db.add(new_key)
            db.flush()
            db.refresh(new_key)

            block_on(invalidate_proxy_cache(request, "resource_group", admin_tenant_id, group_id, action="update"))

            logger.info(
                f"Created API key for group: {new_key.name}",
                context={
                    "key_id": new_key.id,
                    "group_id": group_id,
                    "scope": new_key.scope,
                    "access_mode": key_data.access_mode,
                    "user_id": new_key.user_id,
                    "created_by": admin_id
                }
            )

            # Audit log
            audit.log(
                action="api_key_create",
                admin=admin,
                admin_type=admin_type,
                tenant_id=admin_tenant_id,
                success=True,
                resource_type="api_key",
                resource_id=new_key.id,
                details={
                    "key_name": new_key.name,
                    "group_id": group_id,
                    "group_name": group.name,
                    "scope": new_key.scope,
                    "access_mode": key_data.access_mode,
                    "user_id": new_key.user_id,
                    "user_email": user_email,
                    "expires_at": new_key.expires_at.isoformat() if new_key.expires_at else None
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            # Return response with FULL KEY (only time it's shown!)
            return APIKeyCreatedResponse(
                id=new_key.id,
                key_value=full_key,  # ⚠️ ONLY SHOWN ONCE!
                full_key=full_key,  # Alias for compatibility
                key=full_key,  # Another alias
                key_prefix=new_key.key_prefix,
                name=new_key.name,
                description=new_key.description,
                scope=new_key.scope,
                access_mode=key_data.access_mode,  # Return what UI sent
                user_id=new_key.user_id,
                user_email=new_key.user_email,
                group_id=new_key.group_id,
                group_name=group.name,
                agent_id=new_key.agent_id,
                enabled=new_key.enabled,
                expires_at=new_key.expires_at,
                created_at=new_key.created_at
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error(
                "Failed to create API key",
                context={
                    "group_id": group_id,
                    "error": str(e)
                },
                exc_info=True
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create API key"
            )

    @router.get(
        "/{group_id}/api-keys/usage",
        response_model=GroupAPIKeyUsageResponse,
        summary="Get API key usage statistics",
        description="Get usage statistics for all API keys in this resource group"
    )
    def get_group_key_usage(
            group_id: str,
            request: Request,
            db: Session = Depends(get_db),
            admin_and_type: tuple = Depends(get_current_admin),
            admin: AdminPrincipal = Depends(require_permission(Permission.RESOURCE_GROUP_READ))
    ):
        """
        Get usage statistics for API keys in this resource group
        """
        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        try:
            # Verify group exists
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id
            ).first()

            if not group:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Resource group '{group_id}' not found"
                )

            # Get all keys for this group
            keys = db.query(APIKey).filter(
                APIKey.group_id == group_id,
                APIKey.tenant_id == admin_tenant_id
            ).all()

            # Per-key token totals from proxy_logs (`api_key_id`
            # makes attribution exact). Single GROUP BY
            # is cheaper than one query per key. The IN-clause caps
            # the join to the group's keys so we don't aggregate
            # across the whole tenant.
            key_ids = [k.id for k in keys]
            per_key_tokens: dict[str, int] = {}
            if key_ids:
                rows = db.execute(
                    text(
                        "SELECT api_key_id, COALESCE(SUM(llm_total_tokens), 0) "
                        "FROM proxy_logs "
                        "WHERE tenant_id = :tid AND api_key_id = ANY(:ids) "
                        "GROUP BY api_key_id"
                    ),
                    {"tid": admin_tenant_id, "ids": key_ids},
                ).fetchall()
                per_key_tokens = {r[0]: int(r[1] or 0) for r in rows}

            # Calculate stats
            total_keys = len(keys)
            active_keys = sum(1 for k in keys if k.is_valid)
            total_usage = sum(k.usage_count for k in keys)
            total_tokens = sum(per_key_tokens.values())

            # Per-key usage details
            keys_usage = []
            for key in keys:
                keys_usage.append({
                    "key_id": key.id,
                    "key_name": key.name,
                    "key_prefix": key.key_prefix,
                    "is_valid": key.is_valid,
                    "usage_count": key.usage_count,
                    "tokens_consumed": per_key_tokens.get(key.id, 0),
                    "last_used_at": key.last_used_at.isoformat() if key.last_used_at else None,
                    "last_used_by_user_id": key.last_used_by_user_id,
                    "created_at": key.created_at.isoformat()
                })

            return GroupAPIKeyUsageResponse(
                group_id=group.id,
                group_name=group.name,
                total_keys=total_keys,
                active_keys=active_keys,
                total_usage=total_usage,
                total_tokens_consumed=total_tokens,
                keys_usage=keys_usage
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get API key usage", context={
                "group_id": group_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve API key usage statistics"
            )

    # =========================================================================
    # A2A — effective agent cards + per-group bindings + limits + governance
    # =========================================================================

    @router.get("/{group_id}/effective-agent-cards")
    def get_effective_agent_cards(
        group_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ):
        """Direct + inherited + effective agent cards for this group."""
        admin_obj, _admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin_obj)
        try:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == group_id,
                ResourceGroup.tenant_id == admin_tenant_id,
            ).first()
            if not group:
                raise HTTPException(status_code=404, detail="Group not found")

            direct = list(group.agent_cards)
            direct_ids = {c.id for c in direct}
            effective = group.get_effective_agent_cards()
            inherited = [c for c in effective if c.id not in direct_ids]

            ancestors = group.get_ancestors() if group.inherit_resources else []
            ancestor_order = {a.id: i for i, a in enumerate(ancestors)}

            def _find_source(card):
                best = None
                best_idx = len(ancestors) + 1
                # card.resource_groups is a dynamic relationship; iterating
                # executes a query so we only run it for inherited rows.
                try:
                    bound = list(card.resource_groups) if card.resource_groups is not None else []
                except Exception:
                    bound = []
                for g in bound:
                    if g.id in ancestor_order and ancestor_order[g.id] < best_idx:
                        best = g
                        best_idx = ancestor_order[g.id]
                return best

            def _card_info(c, is_inherited):
                source = _find_source(c) if is_inherited else None
                try:
                    group_ids = [g.id for g in (c.resource_groups or [])]
                except Exception:
                    group_ids = []
                return {
                    "id": c.id,
                    "name": c.name,
                    "version": c.version,
                    "description": c.description,
                    "trust_tier": c.trust_tier,
                    "is_trusted": c.is_trusted,
                    "enabled": c.enabled,
                    "capability_count": len(c.capabilities or []),
                    "is_inherited": is_inherited,
                    "source_group_id": source.id if source else None,
                    "source_group_name": source.name if source else None,
                    "group_ids": group_ids,
                }

            return {
                "group_id": group.id,
                "group_name": group.name,
                "direct_cards": [_card_info(c, False) for c in direct],
                "direct_count": len(direct),
                "inherited_cards": [_card_info(c, True) for c in inherited],
                "inherited_count": len(inherited),
                "effective_cards": [_card_info(c, c.id not in direct_ids) for c in effective],
                "effective_count": len(effective),
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error(
                "Failed to get effective agent cards",
                context={"group_id": group_id, "error": str(e)},
                exc_info=True,
            )
            raise HTTPException(status_code=500, detail="Failed to retrieve effective agent cards")

    @router.put("/{group_id}/agent-cards")
    def set_group_agent_cards(
        group_id: str,
        body: GroupAgentCardsRequest,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin=Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
        # Decided before the body is read: a caller that acts on no tenant
        # is refused, whatever it sent.
        admin_tenant_id: str = Depends(_acting_tenant),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        """
        Replace this group's agent-card bindings (PUT semantics).
        Body: {"agent_card_ids": ["id1", "id2", ...]}
        """
        admin_obj, _admin_type = admin_and_type

        card_ids = [str(c) for c in body.agent_card_ids]

        group = db.query(ResourceGroup).filter(
            ResourceGroup.id == group_id,
            ResourceGroup.tenant_id == admin_tenant_id,
        ).first()
        if not group:
            raise HTTPException(status_code=404, detail="Group not found")

        if card_ids:
            from app.database.models.agent_card import AgentCard
            cards = db.query(AgentCard).filter(
                AgentCard.id.in_(card_ids),
                AgentCard.tenant_id == admin_tenant_id,
            ).all()
            found = {c.id for c in cards}
            missing = set(card_ids) - found
            if missing:
                raise HTTPException(
                    400,
                    f"Agent cards not found in tenant: {', '.join(sorted(missing))}",
                )
            group.agent_cards = cards
        else:
            group.agent_cards = []

        db.commit()
        db.refresh(group)

        audit.log(
            action="resource_group_set_agent_cards",
            admin=admin_obj,
            admin_type=_admin_type,
            tenant_id=admin_tenant_id,
            success=True,
            resource_type=TenantResourceTypes.RESOURCE_GROUPS.value,
            resource_id=group.id,
            details={
                "group_name": group.name,
                "agent_card_ids": sorted(card_ids),
                "agent_card_count": len(card_ids),
            },
            ip_address=client_ip(request),
            user_agent=request.headers.get('user-agent'),
            request_method=request.method,
            request_path=str(request.url.path),
            db=db
        )

        # Mirrors the card-side `PUT /agent-cards/{id}/resource-groups`: the
        # binding feeds `EffectiveGroupData.effective_agent_card_ids` in the
        # proxy, so without a publish the A2A authorisation check keeps using
        # the pre-change set until the group cache's TTL lapses.
        block_on(invalidate_proxy_cache(
            request, "resource_group", admin_tenant_id, group.id, action="update"
        ))

        return {"group_id": group.id, "agent_card_ids": sorted(card_ids)}

    @router.get("/{group_id}/a2a-global-limits")
    def get_group_a2a_limits(
        request: Request,
        group_id: str,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ):
        admin_obj, _admin_type = admin_and_type
        group = db.query(ResourceGroup).filter(
            ResourceGroup.id == group_id,
            ResourceGroup.tenant_id == acting_tenant_id(request, admin_obj),
        ).first()
        if not group:
            raise HTTPException(status_code=404, detail="Group not found")

        effective = group.get_effective_a2a_limits()
        inherited = None
        if group.parent_group and group.inherit_resources:
            inherited = group.parent_group.get_effective_a2a_limits() or None

        return {
            "group_id": group.id,
            "group_name": group.name,
            "a2a_global_limits": group.a2a_global_limits,
            "effective_a2a_limits": effective or None,
            "inherited_a2a_limits": inherited,
        }

    @router.get(
        "/{group_id}/a2a-global-governance",
        response_model=A2AGlobalGovernanceResponse,
        summary="The group's A2A policy, and what applies to it from its ancestors",
    )
    def get_group_a2a_governance(
        request: Request,
        group_id: str,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ):
        admin_obj, _admin_type = admin_and_type
        group = db.query(ResourceGroup).filter(
            ResourceGroup.id == group_id,
            ResourceGroup.tenant_id == acting_tenant_id(request, admin_obj),
        ).first()
        if not group:
            raise HTTPException(status_code=404, detail="Group not found")

        effective = group.get_effective_a2a_governance()
        inherited = None
        if group.parent_group and group.inherit_resources:
            inherited = group.parent_group.get_effective_a2a_governance() or None

        return {
            "group_id": group.id,
            "group_name": group.name,
            "a2a_global_governance": group.a2a_global_governance,
            "effective_a2a_governance": effective or None,
            "inherited_a2a_governance": inherited,
            "remote_allow_lists": group.a2a_remote_allow_lists(),
            "remote_allow_lists_rule": A2A_ALLOW_LISTS_RULE,
        }

    # =========================================================================
    # MEDIA (multimodal) GLOBAL LIMITS + GOVERNANCE
    # =========================================================================
    #
    # Per-mode limits + governance for audio_speech, audio_transcription,
    # image_generation, video_generation, embedding. Stored as a single
    # JSONB column per category, keyed by LLModelMode value. The proxy
    # reads these and enforces at request time on each multimodal
    # endpoint. Inheritance up the group tree isn't computed here yet —
    # mirror `_compute_inherited_limits` when multi-tier media policy
    # becomes a real ask.

    @router.get(
        "/{group_id}/media-global-limits",
        response_model=MediaGlobalLimitsResponse,
    )
    def get_media_global_limits(
        group_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.RESOURCE_GROUP_READ)
        ),
    ):
        admin, admin_type = admin_and_type
        group = db.query(ResourceGroup).filter(
            ResourceGroup.id == group_id,
            ResourceGroup.tenant_id == acting_tenant_id(request, admin),
        ).first()
        if not group:
            raise HTTPException(404, f"Resource group '{group_id}' not found")
        return MediaGlobalLimitsResponse(
            group_id=group.id,
            group_name=group.name,
            media_global_limits=group.media_global_limits,
            effective_media_limits=group.get_effective_media_limits() or None,
            inherited_media_limits=_compute_inherited_limits(group, 'media_global_limits'),
            capacity=_compute_capacity_info(group, 'media_global_limits', db),
        )

    @router.patch(
        "/{group_id}/media-global-limits",
        response_model=MediaGlobalLimitsResponse,
    )
    def update_media_global_limits(
        group_id: str,
        update: MediaGlobalLimitsUpdate,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.RESOURCE_GROUP_UPDATE)
        ),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        admin, admin_type = admin_and_type
        group = db.query(ResourceGroup).filter(
            ResourceGroup.id == group_id,
            ResourceGroup.tenant_id == acting_tenant_id(request, admin),
        ).first()
        if not group:
            raise HTTPException(404, f"Resource group '{group_id}' not found")
        # A child may only tighten what it inherits — the proxy merges media
        # limits across the chain, so a looser value here would be discarded.
        inherited_floor = _compute_inherited_limits(group, 'media_global_limits')
        violations = _validate_not_less_restrictive(update.media_global_limits, inherited_floor)
        if violations:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot set less restrictive than inherited limits: {'; '.join(violations)}"
            )
        sibling_violations = _validate_siblings_sum(
            group, update.media_global_limits, 'media_global_limits', db
        )
        if sibling_violations:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Sibling limits exceed parent capacity: {'; '.join(sibling_violations)}"
            )

        old_limits = group.media_global_limits
        group.media_global_limits = update.media_global_limits
        db.commit()
        db.refresh(group)
        audit.log(
            action="resource_group_update_media_global_limits",
            admin=admin,
            admin_type=admin_type,
            tenant_id=acting_tenant_id(request, admin),
            success=True,
            resource_type=TenantResourceTypes.RESOURCE_GROUPS.value,
            resource_id=group.id,
            details={
                "group_name": group.name,
                "old_limits": old_limits,
                "new_limits": group.media_global_limits,
            },
            ip_address=client_ip(request),
            user_agent=request.headers.get('user-agent'),
            request_method=request.method,
            request_path=str(request.url.path),
            db=db
        )
        return MediaGlobalLimitsResponse(
            group_id=group.id,
            group_name=group.name,
            media_global_limits=group.media_global_limits,
            effective_media_limits=group.get_effective_media_limits() or None,
            inherited_media_limits=_compute_inherited_limits(group, 'media_global_limits'),
            capacity=_compute_capacity_info(group, 'media_global_limits', db),
        )

    @router.get(
        "/{group_id}/media-global-governance",
        response_model=MediaGlobalGovernanceResponse,
    )
    def get_media_global_governance(
        group_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.RESOURCE_GROUP_READ)
        ),
    ):
        admin, admin_type = admin_and_type
        group = db.query(ResourceGroup).filter(
            ResourceGroup.id == group_id,
            ResourceGroup.tenant_id == acting_tenant_id(request, admin),
        ).first()
        if not group:
            raise HTTPException(404, f"Resource group '{group_id}' not found")
        return MediaGlobalGovernanceResponse(
            group_id=group.id,
            group_name=group.name,
            media_global_governance=group.media_global_governance,
            effective_media_governance=group.get_effective_media_governance() or None,
            inherited_media_governance=_compute_inherited_media_governance(group),
        )

    @router.patch(
        "/{group_id}/media-global-governance",
        response_model=MediaGlobalGovernanceResponse,
    )
    def update_media_global_governance(
        group_id: str,
        update: MediaGlobalGovernanceUpdate,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.RESOURCE_GROUP_UPDATE)
        ),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        admin, admin_type = admin_and_type
        group = db.query(ResourceGroup).filter(
            ResourceGroup.id == group_id,
            ResourceGroup.tenant_id == acting_tenant_id(request, admin),
        ).first()
        if not group:
            raise HTTPException(404, f"Resource group '{group_id}' not found")
        # Same rule as every other policy surface: tighten, never loosen.
        relaxations = _validate_media_governance_not_less_restrictive(
            update.media_global_governance,
            _compute_inherited_media_governance(group),
        )
        if relaxations:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Cannot set governance looser than the policy inherited from "
                    "parent groups: " + "; ".join(relaxations)
                ),
            )

        old_governance = group.media_global_governance
        group.media_global_governance = update.media_global_governance
        db.commit()
        db.refresh(group)
        audit.log(
            action="resource_group_update_media_global_governance",
            admin=admin,
            admin_type=admin_type,
            tenant_id=acting_tenant_id(request, admin),
            success=True,
            resource_type=TenantResourceTypes.RESOURCE_GROUPS.value,
            resource_id=group.id,
            details={
                "group_name": group.name,
                "old_governance": old_governance,
                "new_governance": group.media_global_governance,
            },
            ip_address=client_ip(request),
            user_agent=request.headers.get('user-agent'),
            request_method=request.method,
            request_path=str(request.url.path),
            db=db
        )
        return MediaGlobalGovernanceResponse(
            group_id=group.id,
            group_name=group.name,
            media_global_governance=group.media_global_governance,
            effective_media_governance=group.get_effective_media_governance() or None,
            inherited_media_governance=_compute_inherited_media_governance(group),
        )

    # =========================================================================
    # GLOBAL API KEY MANAGEMENT (not group-specific)
    # =========================================================================

    return router