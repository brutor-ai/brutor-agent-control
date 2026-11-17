# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Resource Group Model
Represents groups that organizes AI resources, such as AI Models and MCP Servers,
into hierarchies with limits. Users are associated with groups via membership and role (optional).

Rules:
* Resource Inheritance (LLMs, MCPs, Skills) — controlled by `inherit_resources`, can be disabled per group
* Governance Inheritance (Guardrails, Limits, MCP Capabilities, LLM Governance) — always on, cannot be disabled:
  - Guardrails: additive (parent's guardrails + child's guardrails)
  - Limits: restrictive (min of parent and child values)
  - MCP Capabilities: restrictive (parent disables = stays disabled)
  - LLM Governance: per-model restrictive merge via resource_group_llm_governance table
* Target Group identification is done via API Keys that are generated per group and not inherited
* Target group identification can also be done via X-Gateway-User header and the username is linked to a group
* A user can only be a member of one group in a tree branch in the hierarchy
"""

import copy
import json
import math
import uuid
from datetime import datetime, timezone
from sqlalchemy import Table, Column, String, Boolean, DateTime, Text, Index, UniqueConstraint, ForeignKey, JSON, Integer, Enum as SQLEnum, event, text, func, CheckConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import validates
from app.database.models.ai_asset import LIFECYCLE_STAGES, RISK_TIERS

#: EU AI Act regulated roles. Obligations attach to the pair
#: (system, role) — the same system carries different duties for a
#: provider than for a deployer, and Art 25 can flip a deployer into a
#: provider on rebranding or substantial modification.
EU_AI_ACT_ROLES = frozenset({
    "provider",
    "deployer",
    "provider_and_deployer",
    "importer",
    "distributor",
    "product_manufacturer",
    "authorised_representative",
})
from app.database.models.ai_system_contract import AUTONOMY_LEVELS
from app.database.models.resource_mixins import (
    ResourceAuthorshipMixin,
    ResourceVersionMixin,
)
from sqlalchemy.orm import relationship, Mapped
from sqlalchemy.sql import func
from typing import Dict, Any, Optional, List, Tuple, TYPE_CHECKING
from enum import Enum

from app.database import Base
from app.constants.agent_skill import SkillExecutionMode
from app.services import mcp_exec_limits, release_scope

if TYPE_CHECKING:
    # Relationship annotations only (`Mapped[List["X"]]`): SQLAlchemy resolves
    # the names through its registry, and importing them here at run time would
    # be a cycle.
    from app.database.models.llm_model import LLModel
    from app.database.models.server_config import ServerConfig
    from app.database.models.api_key import APIKey
    from app.database.models.resource_group_membership import ResourceGroupMembership
    from app.database.models.end_user_group import EndUserGroup
    from app.database.models.agent_skill import AgentSkill
    from app.database.models.guardrail_config import GuardrailConfig
    from app.database.models.argument_policy import ArgumentPolicy
    from app.database.models.semantic_cache_config import SemanticCacheConfig
    from app.database.models.prompt_cache_policy import PromptCachePolicy
    from app.database.models.kb_collection import KbCollection
    from app.database.models.kb_connector import KbConnector
    from app.database.models.agent_card import AgentCard


class GroupType(str, Enum):
    """
    Group type enumeration for different organizational structures

    This flexible system allows creating any hierarchy needed:
    - DEPARTMENT > TEAM > PROJECT
    - DIVISION > DEPARTMENT > AI_SYSTEM
    - ORGANIZATION > BUSINESS_UNIT > AI_SYSTEM

    There are no AI/technical member values (APPLICATION / AGENT /
    ASSISTANT / SERVICE): they would express as a *group type* what is
    really a *kind of AI System*, giving the platform two ways to say
    "this is an agent" and forcing the lifecycle engine to guess which one
    an operator meant. That meaning lives on `ResourceGroup.system_kind` (see SYSTEM_KINDS below),
    which is only meaningful when `group_type == AI_SYSTEM`.

    Migration `a130_ai_system_kind` builds the Postgres `grouptype` enum
    without those four members.
    """
    # Organizational units
    ORGANIZATION = "organization"
    DIVISION = "division"
    DEPARTMENT = "department"
    BUSINESS_UNIT = "business_unit"
    TEAM = "team"

    # Work structures
    PROJECT = "project"
    PROGRAM = "program"
    INITIATIVE = "initiative"

    # AI/Technical units
    AI_SYSTEM = "ai_system"      # An AI System — a governed composition RG

    # Special
    CUSTOM = "custom"            # For custom group types


# ============================================================================
# AI System kind
# ============================================================================
#
# `system_kind` discriminates the *shape* of an AI System. It selects the
# signal profile the lifecycle engine applies: trajectory
# drift is the headline signal for `agent` and `workflow` and near
# meaningless for `application`; cadence/silence detection is critical for
# `integration` and wrong for `assistant`. Applying agent-shaped detectors
# to an application generates noise and gets the feature switched off, so
# this column is load-bearing rather than decorative.
#
# Only meaningful when `group_type == GroupType.AI_SYSTEM`; enforced by
# `_validate_system_kind` below and by the API layer.

SYSTEM_KIND_AGENT = "agent"              # Autonomous / semi-autonomous goal pursuit
SYSTEM_KIND_ASSISTANT = "assistant"      # Human-in-the-loop conversational
SYSTEM_KIND_APPLICATION = "application"  # Product feature calling models
SYSTEM_KIND_INTEGRATION = "integration"  # Scheduled / event-driven pipeline
SYSTEM_KIND_WORKFLOW = "workflow"        # Orchestrated, deterministic control flow
SYSTEM_KIND_SERVICE = "service"          # Internal shared AI capability

SYSTEM_KINDS = frozenset({
    SYSTEM_KIND_AGENT,
    SYSTEM_KIND_ASSISTANT,
    SYSTEM_KIND_APPLICATION,
    SYSTEM_KIND_INTEGRATION,
    SYSTEM_KIND_WORKFLOW,
    SYSTEM_KIND_SERVICE,
})

# Default applied when an AI System is created without an explicit kind.
# `agent` is the dominant kind
# today and the one whose signal profile is the strictest superset.
DEFAULT_SYSTEM_KIND = SYSTEM_KIND_AGENT

# The former GroupType members that are system kinds. Consumed by
# migration `a130_ai_system_kind` and by its regression test, so the mapping lives in exactly
# one place rather than being restated in the migration.
LEGACY_GROUP_TYPE_TO_SYSTEM_KIND = {
    "APPLICATION": SYSTEM_KIND_APPLICATION,
    "AGENT": SYSTEM_KIND_AGENT,
    "ASSISTANT": SYSTEM_KIND_ASSISTANT,
    "SERVICE": SYSTEM_KIND_SERVICE,
}


# ============================================
# Association Tables
# ============================================

# Association table for ResourceGroup <-> LLModel
resource_group_llm_models = Table(
    'resource_group_llm_models',
    Base.metadata,
    Column('id', String(255), primary_key=True, default=lambda: str(uuid.uuid4())),
    Column('resource_group_id', String(255), ForeignKey('resource_groups.id', ondelete='CASCADE'), nullable=False),
    Column('llm_model_id', String(255), ForeignKey('llm_model.id', ondelete='CASCADE'), nullable=False),  # ← llm_model NOT llm_model_info
    Column('created_at', DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)),
    Column('created_by', String(255)),
    Column('limits', JSON(none_as_null=True), nullable=True, comment="Per-model limits (budget/tokens/requests). Effective = min(global, per_item)"),
    # Show this bound model in the end-user portal model picker. False
    # for routing members that are bound only so the routing group can
    # resolve to them — it does NOT affect access (the Core proxy's
    # /v1/portal/models filters on this; runtime access checks ignore it).
    Column('portal_visible', Boolean, nullable=False, server_default=text('true'),
           comment="Show in the end-user portal model picker. False hides routing members; does not affect access."),
    # This binding was created automatically because a system
    # feature bound to this group needs the model at runtime (semantic
    # policy judge, semantic cache embedding). Provenance matters on
    # teardown: only auto-bound rows may be removed when the feature stops
    # needing them; a binding an admin created by hand is never touched.
    Column('auto_bound', Boolean, nullable=False, server_default=text('false'),
           comment="Created automatically for a system feature (judge/embedding model). "
                   "Only these rows are removed when the feature no longer needs them."),
    UniqueConstraint('resource_group_id', 'llm_model_id', name='uq_group_llm_model'),
    # selectinload from the LLModel side ("which groups does this
    # model belong to?") issues `WHERE llm_model_id IN (...)`. The
    # unique constraint above is left-anchored on resource_group_id,
    # so it can't accelerate that lookup. Add a single-column index
    # on llm_model_id so the admin LLModel list endpoint stays fast
    # as the model catalog grows.
    Index('ix_rg_llm_models_llm_model_id', 'llm_model_id'),
)

# Association table for ResourceGroup <-> ServerConfig
resource_group_server_configs = Table(
    'resource_group_server_configs',
    Base.metadata,
    Column('id', String(255), primary_key=True, default=lambda: str(uuid.uuid4())),
    Column('resource_group_id', String(255), ForeignKey('resource_groups.id', ondelete='CASCADE'), nullable=False),
    Column('server_config_id', String(255), ForeignKey('server_configs.id', ondelete='CASCADE'), nullable=False),
    Column('created_at', DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)),
    Column('created_by', String(255)),
    Column('limits', JSON(none_as_null=True), nullable=True, comment="Per-server limits (execution/data/frequency). Effective = min(global, per_item)"),
    UniqueConstraint('resource_group_id', 'server_config_id', name='uq_group_server_config')
)

# Capability-level access control for MCP servers per resource group.
# No rows = all capabilities enabled (backward compatible).
# Once rows exist for a group+server, only access='enabled' capabilities are freely accessible.
# Three-state access model: enabled → approval_required → disabled (strictness order).
# Governance inheritance uses restrictive merge: max strictness wins.
resource_group_mcp_capabilities = Table(
    'resource_group_mcp_capabilities',
    Base.metadata,
    Column('id', String(255), primary_key=True, default=lambda: str(uuid.uuid4())),
    Column('tenant_id', String(255), ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False),
    Column('resource_group_id', String(255), ForeignKey('resource_groups.id', ondelete='CASCADE'), nullable=False),
    Column('server_config_id', String(255), ForeignKey('server_configs.id', ondelete='CASCADE'), nullable=False),
    Column('capability_type', String(50), nullable=False),   # tool, resource, prompt, resource_template
    Column('capability_name', String(500), nullable=False),  # tool name, resource URI, prompt name, template URI
    Column('access', String(20), nullable=False, default='enabled'),  # enabled, approval_required, disabled
    Column('approval_timeout_seconds', Integer, nullable=True),  # per-capability timeout for approval_required
    Column('created_at', DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)),
    Column('updated_at', DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
           onupdate=lambda: datetime.now(timezone.utc)),
    Column('created_by', String(255)),
    UniqueConstraint('resource_group_id', 'server_config_id', 'capability_type', 'capability_name',
                     name='uq_group_server_capability'),
    Index('ix_rgmc_group_server', 'resource_group_id', 'server_config_id'),
    Index('ix_rgmc_tenant', 'tenant_id'),
)

# Per-model LLM governance for resource groups.
# Each row stores governance policy for a specific (group, model) pair.
# No rows = no governance restrictions for that model in that group.
# Governance inheritance walks the ancestor chain, applying restrictive merge per model.
resource_group_llm_governance = Table(
    'resource_group_llm_governance',
    Base.metadata,
    Column('id', String(255), primary_key=True, default=lambda: str(uuid.uuid4())),
    Column('tenant_id', String(255), ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False),
    Column('resource_group_id', String(255), ForeignKey('resource_groups.id', ondelete='CASCADE'), nullable=False),
    Column('llm_model_id', String(255), ForeignKey('llm_model.id', ondelete='CASCADE'), nullable=False),
    Column('governance', JSON, nullable=False),
    Column('created_at', DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)),
    Column('updated_at', DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
           onupdate=lambda: datetime.now(timezone.utc)),
    Column('created_by', String(255)),
    UniqueConstraint('resource_group_id', 'llm_model_id', name='uq_group_llm_governance'),
    Index('ix_rglg_group_model', 'resource_group_id', 'llm_model_id'),
    Index('ix_rglg_tenant', 'tenant_id'),
)

# Association table for ResourceGroup <-> AgentCard.
# Mirrors resource_group_kb_collections; the optional `limits` column
# reserves space for a per-(group, card) override without requiring
# another migration down the line.
resource_group_agent_cards = Table(
    'resource_group_agent_cards',
    Base.metadata,
    Column('id', String(255), primary_key=True, default=lambda: str(uuid.uuid4())),
    Column('resource_group_id', String(255), ForeignKey('resource_groups.id', ondelete='CASCADE'), nullable=False),
    Column('agent_card_id', String(255), ForeignKey('agent_cards.id', ondelete='CASCADE'), nullable=False),
    Column('limits', JSON(none_as_null=True), nullable=True,
           comment="Per-(group, card) A2A limit overrides; merged restrictively with group a2a_global_limits"),
    Column('created_at', DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now()),
    Column('created_by', String(255)),
    UniqueConstraint('resource_group_id', 'agent_card_id', name='uq_group_agent_card'),
    Index('ix_rgac_group', 'resource_group_id'),
    Index('ix_rgac_card', 'agent_card_id'),
)

# ============================================
# Restrictive Merge Helper
# ============================================

class LimitFamily(str, Enum):
    """The family of a limit document, which says how its readers take a
    stated number that is not positive. The values are the gateway's names
    (`usage::LimitFamily`)."""

    LLM = "Llm"
    MCP = "Mcp"
    SKILL = "Skill"
    MEDIA = "Media"
    A2A = "A2a"


#: Where a family's reader takes a number that is not positive as a limit
#: (the strictest: it admits nothing) rather than as unset: `True` at every
#: key, else the keys where it does. A media quota of 0 admits no request;
#: `chain.max_delegation_depth: 0` forbids delegation. The LLM, skill, MCP
#: frequency and A2A invocation and frequency readers take it as unset; the
#: MCP execution limits refuse it (and are refused at save).
#: `usage::LimitFamily::non_positive_is_a_limit` in the gateway.
NON_POSITIVE_IS_A_LIMIT: Dict[LimitFamily, Any] = {
    LimitFamily.LLM: (),
    LimitFamily.MCP: (),
    LimitFamily.SKILL: (),
    LimitFamily.MEDIA: True,
    LimitFamily.A2A: ('max_delegation_depth',),
}

#: The family of each limit document of a resource group (and of the
#: per-item and tenant-wide documents of the same kind).
LIMIT_DOCUMENT_FAMILIES: Dict[str, LimitFamily] = {
    'llm_global_limits': LimitFamily.LLM,
    'mcp_global_limits': LimitFamily.MCP,
    'skill_global_limits': LimitFamily.SKILL,
    'media_global_limits': LimitFamily.MEDIA,
    'a2a_global_limits': LimitFamily.A2A,
}


#: The keys of an MCP limits document (`mcp_global_limits`, a server
#: binding's `limits`, an MCP server's `tenant_limits`): the ones the gateway
#: reads, and in `execution` and `data` the five this release refuses with
#: their own sentence (`release_scope.MCP_UNAVAILABLE`). Any other key would
#: be stored and never read, so it is refused at save. `per_tool` is keyed by
#: tool name, each holding the keys of `frequency`.
MCP_FREQUENCY_KEYS: Tuple[str, ...] = (
    'max_calls_per_minute', 'max_calls_per_hour', 'max_calls_per_day',
    'burst_limit', 'cooldown_seconds', 'daily_warning_percent',
)
MCP_LIMITS_KEYS: Dict[str, Optional[Tuple[str, ...]]] = {
    section: tuple(f for s, f, _, _ in mcp_exec_limits.FIELDS if s == section)
    + tuple(k for s, k, _ in release_scope.MCP_UNAVAILABLE if s == section)
    for section in ('execution', 'data')
}
MCP_LIMITS_KEYS['frequency'] = MCP_FREQUENCY_KEYS
MCP_LIMITS_KEYS['per_tool'] = None


def _unknown_mcp_limits_key(block: Dict[str, Any], accepted: Tuple[str, ...], where: str) -> Optional[str]:
    """The sentence refusing the first key of `block` an MCP limits document
    does not have, or None."""
    unknown = sorted(k for k in block if k not in accepted)
    if not unknown:
        return None
    name = f"{where}.{unknown[0]}" if where else unknown[0]
    return (
        f"MCP limits: '{name}' is not a key of an MCP limits document; the gateway does not "
        f"read it. Accepted{' in ' + where if where else ''}: {', '.join(accepted)}."
    )


def _non_positive_is_a_limit(family: LimitFamily, key: str) -> bool:
    keys = NON_POSITIVE_IS_A_LIMIT[family]
    return keys is True or key in keys


def _is_json_number(value: Any) -> bool:
    # bool is a subclass of int in Python; to JSON it is not a number.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _as_finite_float(value: Any) -> Optional[float]:
    """A JSON number as the gateway reads it with `as_f64`; None when it is
    not a finite number."""
    if not _is_json_number(value):
        return None
    try:
        as_float = float(value)
    except OverflowError:
        return None
    return as_float if math.isfinite(as_float) else None


def _limit_shape(value: Any) -> int:
    """How far a value can be read as a limit: a number or a section (2), a
    bool (1), anything else (0)."""
    if isinstance(value, bool):
        return 1
    if _is_json_number(value) or isinstance(value, dict):
        return 2
    return 0


def restrictive_merge(base: Any, overlay: Any, family: LimitFamily) -> Any:
    """
    Merge the limit document `overlay` (one level of a chain) into `base`
    (the levels merged so far) and return the result; `base` is changed in
    place when it is a JSON object. A limit can be tightened along a chain,
    never removed. The gateway's `usage::restrictive_merge`, rule for rule
    (`tests/test_python_rust_merge_parity.py` holds the two together):

    - Numeric: the smallest of the stated numbers, kept as stated (an
      integer stays an integer). A number that is not positive is stated
      only where the family reads it as a limit (`NON_POSITIVE_IS_A_LIMIT`);
      elsewhere it is no limit at its level and the other level's stays.
    - `null` (or an absent field) states nothing: the other level's value
      stays.
    - A value of another type than the other level's (a string where a
      number is, `"no"` where a bool is) does not replace a number, a bool
      or a section.
    - Bool (e.g. hard_stop): true wins.
    - Object: recurse.
    - A base that is not an object (a JSON-null document) takes the overlay;
      an overlay that is not an object changes nothing.

    New values are deep-copied so the result shares nothing with a model's
    JSON column.
    """
    if not isinstance(overlay, dict):
        return base
    if not isinstance(base, dict):
        return copy.deepcopy(overlay)
    for key, overlay_val in overlay.items():
        if key in base:
            base[key] = _merge_limit_value(
                base[key], overlay_val, _non_positive_is_a_limit(family, key), family
            )
        elif overlay_val is not None:
            base[key] = copy.deepcopy(overlay_val)
    return base


def _merge_limit_value(base: Any, overlay: Any, non_positive_is_a_limit: bool, family: LimitFamily) -> Any:
    """One field of `restrictive_merge`: the merged value."""
    if overlay is None:
        return base
    if base is None:
        return copy.deepcopy(overlay)
    if isinstance(base, dict) and isinstance(overlay, dict):
        return restrictive_merge(base, overlay, family)
    if isinstance(base, bool) and isinstance(overlay, bool):
        return base or overlay
    if _is_json_number(base) and _is_json_number(overlay):
        def stated(value: Any) -> Optional[float]:
            number = _as_finite_float(value)
            if number is None or (not non_positive_is_a_limit and number <= 0):
                return None
            return number

        base_num, overlay_num = stated(base), stated(overlay)
        if overlay_num is not None and (base_num is None or overlay_num < base_num):
            return overlay
        return base
    # Two values that are not a limit's shape (strings, lists): the overlay's.
    if _limit_shape(base) == 0 and _limit_shape(overlay) == 0:
        return copy.deepcopy(overlay)
    if _limit_shape(overlay) > _limit_shape(base):
        return copy.deepcopy(overlay)
    return base


def _merge_limit_chain(documents: List[Any], family: LimitFamily) -> Any:
    """The limit documents of a chain, root first, merged; None when no
    level states one (a level's None is left out). `usage::merge_chain`."""
    effective = None
    for document in documents:
        if document is None:
            continue
        effective = copy.deepcopy(document) if effective is None else restrictive_merge(effective, document, family)
    return effective


def _merge_media_limit_chain(documents: List[Any]) -> Any:
    """`media_global_limits` of a chain, root first, merged as the gateway
    does it (`usage::collect_effective_media_limits`): mode by mode, the
    group's own first and each ancestor's merged into it; a mode's `null` is
    left out. None when no level states a mode."""
    levels = [document for document in documents if isinstance(document, dict)]
    modes: List[str] = []
    for level in levels:
        modes.extend(mode for mode in level if mode not in modes)
    effective: Dict[str, Any] = {}
    for mode in modes:
        merged = None
        for level in reversed(levels):
            value = level.get(mode)
            if value is None:
                continue
            merged = copy.deepcopy(value) if merged is None else restrictive_merge(merged, value, LimitFamily.MEDIA)
        if merged is not None:
            effective[mode] = merged
    return effective or None


def merge_limit_documents(attr: str, documents: List[Any]) -> Any:
    """The limit documents `attr` (`llm_global_limits`, ...) of a chain,
    root first, merged by the rule of their family; None when no level
    states one."""
    if attr == 'media_global_limits':
        return _merge_media_limit_chain(documents)
    return _merge_limit_chain(documents, LIMIT_DOCUMENT_FAMILIES[attr])


class StrictnessOrder(dict):
    """The values a setting may take, ranked: a higher rank is stricter.

    A value outside the table (a typo, a value a newer release added, a
    value that is not text) ranks `unknown`: the gateway reads such a value
    as the most restrictive one and refuses on it, so a merge for the
    console must neither lose it under a known value nor let a known one
    replace it. `None` (absent, or JSON `null`) states nothing and ranks 0.
    The tables are held to the gateway's source by
    `tests/test_python_rust_merge_parity.py`.
    """

    def __init__(self, known: Dict[str, int], *, unknown: int) -> None:
        super().__init__(known)
        self.unknown = unknown

    def rank(self, value: Any) -> int:
        if value is None:
            return 0
        if isinstance(value, str) and value in self:
            return self[value]
        return self.unknown


# The gateway's `governance::access_strictness` (LLM `tool_use.access`): an
# access it does not know outranks every known one.
_ACCESS_STRICTNESS = StrictnessOrder({'enabled': 0, 'approval_required': 1, 'disabled': 2}, unknown=3)
# MCP `default_*_access`, an MCP capability filter's access and an A2A
# `capability.default_access`: an access the gateway does not know reads as
# denied (`usage::mcp_default_access_from_gov`, the capability-filter
# ranking), the strictness of `disabled`.
_MCP_ACCESS_STRICTNESS = StrictnessOrder({'enabled': 0, 'approval_required': 1, 'disabled': 2}, unknown=2)
# The gateway's `governance::enforce_strictness`.
_ENFORCE_STRICTNESS = StrictnessOrder({'clamp': 0, 'reject': 1}, unknown=2)

#: The `default_*_access` fields of an MCP governance document.
MCP_DEFAULT_ACCESS_FIELDS = (
    'default_tool_access', 'default_resource_access',
    'default_prompt_access', 'default_resource_template_access',
)


def _is_one_of(value: Any, allowed: Any) -> bool:
    """`value` is one of the strings `allowed`; a value that is not text
    (a list, a number) never is, and is not hashed."""
    return isinstance(value, str) and value in allowed


def llm_governance_value_problem(governance: Any) -> Optional[str]:
    """What is wrong with the `enforce` modes and `tool_use.access` of an
    LLM governance document, or None. The gateway reads a value it does not
    know as the most restrictive (reject, refuse), so one is refused at
    save rather than stored. Only those fields are looked at: a model's
    tenant governance holds a media model's field map as well."""
    if not isinstance(governance, dict):
        return None
    for section in ('temperature', 'context_window'):
        settings = governance.get(section)
        if isinstance(settings, dict) and 'enforce' in settings and not _is_one_of(
            settings['enforce'], _ENFORCE_STRICTNESS
        ):
            return f"{section}.enforce must be 'clamp' or 'reject'"
    tool_use = governance.get('tool_use')
    if isinstance(tool_use, dict) and 'access' in tool_use:
        if tool_use['access'] == 'approval_required':
            return f"tool_use.access: {NO_APPROVAL_ON_MODEL_TOOL_USE}"
        if not _is_one_of(tool_use['access'], ('enabled', 'disabled')):
            return "tool_use.access must be one of: disabled, enabled"
    return None


def mcp_governance_value_problem(governance: Any, *, null_states_nothing: bool = False) -> Optional[str]:
    """What is wrong with the `default_*_access` fields of an MCP
    governance document, or None. The gateway reads an access it does not
    know as denied, so one is refused at save rather than stored. A `null`
    states nothing to the gateway; it is accepted where
    `null_states_nothing`."""
    if not isinstance(governance, dict):
        return None
    for key in MCP_DEFAULT_ACCESS_FIELDS:
        if key not in governance or (null_states_nothing and governance[key] is None):
            continue
        if not _is_one_of(governance[key], _MCP_ACCESS_STRICTNESS):
            return f"{key} must be one of: {', '.join(sorted(_MCP_ACCESS_STRICTNESS))}"
    return None


def _governance_restrictive_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> None:
    """
    Merge overlay into base with governance-specific restrictive semantics.

    Unlike simple restrictive_merge (numeric min, bool OR), governance
    has richer merge rules per field:
    - temperature.max / context_window.*: min wins (numeric)
    - temperature.enforce / context_window.enforce: "reject" > "clamp"
    - tool_use.access: max strictness (enabled < approval_required < disabled)
    - tool_use.approval_timeout_seconds: min wins
    - tool_use.allowed_tools: intersection (more restrictive)
    - tool_use.blocked_tools: union (more restrictive)
    - system_prompt.mandatory_fragments: union (parent first, then child — all injected)
    - system_prompt.banned_patterns: union
    """
    for section_key in ('temperature', 'context_window', 'tool_use',
                         'system_prompt'):
        if section_key not in overlay:
            continue

        child = overlay[section_key]
        if section_key not in base:
            # Deep copy to avoid shared references
            import copy
            base[section_key] = copy.deepcopy(child)
            continue

        parent = base[section_key]

        if section_key == 'temperature':
            _merge_numeric_min(parent, child, 'max')
            _merge_enforce_strict(parent, child)

        elif section_key == 'context_window':
            _merge_numeric_min(parent, child, 'max_input_tokens')
            _merge_numeric_min(parent, child, 'max_output_tokens')
            _merge_enforce_strict(parent, child)

        elif section_key == 'tool_use':
            # Three-state access: max strictness wins
            if 'access' in child:
                p_strict = _ACCESS_STRICTNESS.rank(parent.get('access'))
                c_strict = _ACCESS_STRICTNESS.rank(child['access'])
                if c_strict > p_strict:
                    parent['access'] = child['access']
            # Timeout: min wins
            _merge_numeric_min(parent, child, 'approval_timeout_seconds')
            # Allowed tools: intersection (restrictive)
            _merge_list_intersection(parent, child, 'allowed_tools')
            # Blocked tools: union (restrictive)
            _merge_list_union(parent, child, 'blocked_tools')
            # Tool call rate limits: min wins (more restrictive)
            _merge_numeric_min(parent, child, 'max_tool_calls_per_request')
            _merge_numeric_min(parent, child, 'max_tool_calls_per_hour')
            _merge_numeric_min(parent, child, 'max_tool_calls_per_day')
            # Banned argument patterns: union (more patterns = more restrictive)
            _merge_list_union(parent, child, 'banned_argument_patterns')

        elif section_key == 'system_prompt':
            # Mandatory fragments: union (parent fragments first, then child)
            _merge_fragments_union(parent, child, 'mandatory_fragments')
            # Banned patterns: union
            _merge_list_union(parent, child, 'banned_patterns')


def _merge_numeric_min(parent: dict, child: dict, key: str) -> None:
    """Take the min of parent[key] and child[key] (if both exist)."""
    if key in child:
        if key in parent and parent[key] is not None and child[key] is not None:
            parent[key] = min(parent[key], child[key])
        elif child[key] is not None:
            parent[key] = child[key]


def _merge_enforce_strict(parent: dict, child: dict) -> None:
    """'reject' wins over 'clamp'."""
    if 'enforce' in child:
        p = _ENFORCE_STRICTNESS.rank(parent.get('enforce'))
        c = _ENFORCE_STRICTNESS.rank(child['enforce'])
        if c > p:
            parent['enforce'] = child['enforce']


def _merge_list_intersection(parent: dict, child: dict, key: str) -> None:
    """Intersect two lists (more restrictive). Only applied when both exist."""
    if key in child:
        if key in parent and parent[key] is not None and child[key] is not None:
            parent_set = set(parent[key])
            child_set = set(child[key])
            parent[key] = sorted(parent_set & child_set)
        elif child[key] is not None:
            parent[key] = list(child[key])


def _merge_list_union(parent: dict, child: dict, key: str) -> None:
    """Union two lists (more restrictive for blocked/banned)."""
    if key in child and child[key] is not None:
        if key in parent and parent[key] is not None:
            existing = set()
            # For fragments (list of dicts), use text as dedup key
            if parent[key] and isinstance(parent[key][0], dict):
                seen_texts = {item.get('text', '') for item in parent[key]}
                for item in child[key]:
                    if item.get('text', '') not in seen_texts:
                        parent[key].append(item)
            else:
                existing = set(parent[key])
                for item in child[key]:
                    if item not in existing:
                        parent[key].append(item)
        else:
            import copy
            parent[key] = copy.deepcopy(child[key])


def _merge_fragments_union(parent: dict, child: dict, key: str) -> None:
    """Union mandatory fragments (parent first, then child). Dedup by text."""
    _merge_list_union(parent, child, key)


# The longest an approval hold stays open: the gateway cuts a longer timeout
# to this, so a longer one is refused when it is saved (a group's and a
# tenant's approval window have the same bound).
MAX_APPROVAL_TIMEOUT_SECONDS = 604_800


def approval_timeout_error(value: Any, name: str = "approval_timeout_seconds") -> Optional[str]:
    """Why `value` is not an approval timeout: a whole number of seconds
    from 1 to seven days."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return f"{name} must be a positive integer"
    if value > MAX_APPROVAL_TIMEOUT_SECONDS:
        return f"{name} must be at most {MAX_APPROVAL_TIMEOUT_SECONDS} (7 days)"
    return None


# The gateway holds nothing for a person on a model's tool calls.
NO_APPROVAL_ON_MODEL_TOOL_USE = (
    "Approvals are not available for model tool calls: 'approval_required' is not a tool-use "
    "access. Use 'enabled' or 'disabled', and put an approval on the tool itself (an MCP "
    "capability, a skill, an argument policy on its own surface)."
)


NO_APPROVAL_WINDOW_ON_MODEL_TOOL_USE = (
    "Approvals are not available for model tool calls, so tool use has no approval window: "
    "remove the field. An approval window belongs to what can be held (an MCP capability, a "
    "skill, an A2A capability, an argument policy on its own surface)."
)


# The sections of LLM governance and the keys of each: the ones the gateway
# reads. Any other section or key is refused when the document is saved: it
# would be stored and never applied.
LLM_GOVERNANCE_KEYS: Dict[str, tuple] = {
    'temperature': ('max', 'enforce'),
    'context_window': ('max_input_tokens', 'max_output_tokens', 'enforce'),
    'tool_use': (
        'access', 'allowed_tools', 'blocked_tools', 'banned_argument_patterns',
        'max_tool_calls_per_request', 'max_tool_calls_per_hour', 'max_tool_calls_per_day',
    ),
    'system_prompt': ('mandatory_fragments', 'banned_patterns'),
}


def validate_llm_governance(governance: Dict[str, Any]) -> tuple:
    """
    Validate LLM governance JSON structure and values (standalone function).

    Used by API endpoints before storing governance in resource_group_llm_governance table.

    Returns:
        (is_valid, error_message)
    """
    if not governance:
        return True, None

    try:
        gov = governance if isinstance(governance, dict) else {}

        unknown = set(gov.keys()) - set(LLM_GOVERNANCE_KEYS)
        if unknown:
            return False, f"Unknown governance sections: {', '.join(sorted(unknown))}"
        for section, keys in LLM_GOVERNANCE_KEYS.items():
            if gov.get(section) is None:
                continue
            if not isinstance(gov[section], dict):
                return False, f"{section} must be a JSON object"
            if section == 'tool_use' and 'approval_timeout_seconds' in gov[section]:
                return False, f"tool_use.approval_timeout_seconds: {NO_APPROVAL_WINDOW_ON_MODEL_TOOL_USE}"
            unknown_keys = set(gov[section].keys()) - set(keys)
            if unknown_keys:
                return False, (
                    f"Unknown {section} fields: {', '.join(sorted(unknown_keys))}. "
                    f"Accepted: {', '.join(keys)}"
                )

        problem = llm_governance_value_problem(gov)
        if problem:
            return False, problem

        if 'temperature' in gov:
            temp = gov['temperature']
            if 'max' in temp:
                if not isinstance(temp['max'], (int, float)) or temp['max'] < 0.0 or temp['max'] > 2.0:
                    return False, "temperature.max must be between 0.0 and 2.0"

        if 'context_window' in gov:
            ctx = gov['context_window']
            for field in ('max_input_tokens', 'max_output_tokens'):
                if field in ctx:
                    if not isinstance(ctx[field], int) or ctx[field] < 1:
                        return False, f"context_window.{field} must be a positive integer"

        if 'tool_use' in gov:
            tu = gov['tool_use']
            for field in ('allowed_tools', 'blocked_tools', 'banned_argument_patterns'):
                if field in tu and tu[field] is not None:
                    if not isinstance(tu[field], list):
                        return False, f"tool_use.{field} must be a list of strings"
            for field in ('max_tool_calls_per_request', 'max_tool_calls_per_hour', 'max_tool_calls_per_day'):
                if field in tu:
                    if not isinstance(tu[field], int) or tu[field] < 1:
                        return False, f"tool_use.{field} must be a positive integer"

        if 'system_prompt' in gov:
            sp = gov['system_prompt']
            if 'mandatory_fragments' in sp and sp['mandatory_fragments'] is not None:
                if not isinstance(sp['mandatory_fragments'], list):
                    return False, "system_prompt.mandatory_fragments must be a list"
                for i, frag in enumerate(sp['mandatory_fragments']):
                    if not isinstance(frag, dict):
                        return False, f"system_prompt.mandatory_fragments[{i}] must be an object"
                    if 'position' not in frag or frag['position'] not in ('prepend', 'append'):
                        return False, f"system_prompt.mandatory_fragments[{i}].position must be 'prepend' or 'append'"
                    if 'text' not in frag or not isinstance(frag['text'], str):
                        return False, f"system_prompt.mandatory_fragments[{i}].text is required and must be a string"
                    if set(frag.keys()) - {'position', 'text'}:
                        return False, (
                            f"system_prompt.mandatory_fragments[{i}] takes 'position' and 'text' only"
                        )
            if 'banned_patterns' in sp and sp['banned_patterns'] is not None:
                if not isinstance(sp['banned_patterns'], list):
                    return False, "system_prompt.banned_patterns must be a list of strings"

        unavailable = release_scope.llm_governance_problem(gov)
        if unavailable:
            return False, unavailable
        return True, None

    except Exception as e:
        return False, f"Invalid LLM governance structure: {str(e)}"


# The gateway's `skill_orchestrator::skill_execution_strictness`.
_SKILL_EXECUTION_STRICTNESS = StrictnessOrder(
    {'unrestricted': 0, 'approval_required': 1, 'disabled': 2}, unknown=3,
)


def _mcp_governance_restrictive_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> None:
    """
    Merge MCP global governance with restrictive semantics.

    Access fields: max strictness wins (enabled < approval_required < disabled).
    Timeouts: min wins.
    """
    for key in ('default_tool_access', 'default_resource_access',
                'default_prompt_access', 'default_resource_template_access'):
        if key in overlay:
            p_strict = _MCP_ACCESS_STRICTNESS.rank(base.get(key))
            c_strict = _MCP_ACCESS_STRICTNESS.rank(overlay[key])
            if c_strict > p_strict:
                base[key] = overlay[key]
            elif key not in base:
                base[key] = overlay[key]
    if 'approval_timeout_seconds' in overlay:
        _merge_numeric_min(base, overlay, 'approval_timeout_seconds')


#: Every field `validate_skill_governance` accepts, and therefore every
#: field this merge must restrict. A field in the schema but missing here
#: would be stored, shown, and then ignored — the exact failure the Rust
#: side and this one drifted into: Python merged `discovery_enabled` and
#: `approval_timeout_seconds` while Rust merged the two execution ceilings,
#: so the console and the proxy disagreed about four of the five fields.
#: `tests/test_python_rust_merge_parity.py` now pins the two together.
SKILL_GOVERNANCE_FIELDS = (
    'execution_mode',
    'discovery_enabled',
    'approval_timeout_seconds',
    'max_concurrent_executions',
    'max_tool_calls_per_execution',
)


#: The numeric ceilings of skill governance and whether a number that is not
#: positive is stated: `approval_timeout_seconds` and
#: `max_concurrent_executions` are read only when positive;
#: `max_tool_calls_per_execution` is handed to the agent as stated.
SKILL_GOVERNANCE_CEILINGS = (
    ('approval_timeout_seconds', False),
    ('max_concurrent_executions', False),
    ('max_tool_calls_per_execution', True),
)


def _as_json_i64(value: Any) -> Optional[int]:
    """A JSON number as the gateway reads it with `as_i64`: a whole number
    written without a fraction, in range; None otherwise."""
    if isinstance(value, int) and not isinstance(value, bool) and -(2 ** 63) <= value < 2 ** 63:
        return value
    return None


def _skill_governance_restrictive_merge(base: Any, parent: Any) -> Any:
    """
    Merge an ancestor's `skill_global_governance` (`parent`) into the
    group's own view (`base`, the levels below it merged so far) and return
    the result; `base` is changed in place when it is a JSON object.
    MOST-RESTRICTIVE wins. The gateway's `merge_skill_governance`, rule for
    rule:

    - execution_mode: the stricter (unrestricted < approval_required <
      disabled); the parent's when the group states none.
    - discovery_enabled: false wins (AND) across the levels that state a bool.
    - the ceilings (`SKILL_GOVERNANCE_CEILINGS`): the smallest stated whole
      number; a `null`, a non-number or (where not stated) a number that is
      not positive is no value at its level and the other level's stays.
    - A group whose governance is not an object (JSON `null`) takes the
      parent's; a parent that is not an object changes nothing.
    """
    if not isinstance(parent, dict):
        return base
    if not isinstance(base, dict):
        return copy.deepcopy(parent)

    base_mode, parent_mode = base.get('execution_mode'), parent.get('execution_mode')
    if base_mode is None or (
        _SKILL_EXECUTION_STRICTNESS.rank(parent_mode) > _SKILL_EXECUTION_STRICTNESS.rank(base_mode)
    ):
        if 'execution_mode' in parent:
            base['execution_mode'] = copy.deepcopy(parent['execution_mode'])

    parent_discovery = parent.get('discovery_enabled')
    if isinstance(parent_discovery, bool):
        base_discovery = base.get('discovery_enabled')
        base['discovery_enabled'] = (
            base_discovery and parent_discovery if isinstance(base_discovery, bool) else parent_discovery
        )

    for key, non_positive_is_stated in SKILL_GOVERNANCE_CEILINGS:
        def stated(value: Any) -> Optional[int]:
            number = _as_json_i64(value)
            if number is None or (not non_positive_is_stated and number <= 0):
                return None
            return number

        parent_value = stated(parent.get(key))
        if parent_value is not None:
            base_value = stated(base.get(key))
            base[key] = parent_value if base_value is None else min(base_value, parent_value)
    return base


#: Per-field merge rules for `media_global_governance`, keyed by field name.
#: Mirrors `media_field_rule` in the core's `services/media_governance.rs`
#: — the proxy has always merged media governance across the ancestor
#: chain, but the control plane did not compute it at all, so the console
#: reported no inherited media policy while the runtime enforced one.
#:
#: * whitelist — intersection; an empty list means "unconstrained" and is
#:   the identity element, so it never empties the other side.
#: * ceiling   — min wins (an upper bound).
#: * floor     — max wins (a lower bound: a higher `speed_min` is stricter).
#: * bool_or   — true wins (a requirement, e.g. `require_image_input`).
MEDIA_GOVERNANCE_FIELD_RULES: Dict[str, str] = {
    'voice_whitelist': 'whitelist',
    'response_format_whitelist': 'whitelist',
    'language_allowlist': 'whitelist',
    'size_whitelist': 'whitelist',
    'quality_whitelist': 'whitelist',
    'style_whitelist': 'whitelist',
    'resolution_whitelist': 'whitelist',
    'dim_whitelist': 'whitelist',
    'max_input_characters': 'ceiling',
    'speed_max': 'ceiling',
    'max_audio_size_mb': 'ceiling',
    'max_n': 'ceiling',
    'max_prompt_characters': 'ceiling',
    'max_duration_seconds': 'ceiling',
    'max_batch_size': 'ceiling',
    'speed_min': 'floor',
    'require_image_input': 'bool_or',
}


def _same_json(a: Any, b: Any) -> bool:
    """JSON equality as the gateway's `Value` compares: `1`, `1.0` and
    `true` are three values."""
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def _media_governance_restrictive_merge(base: Any, parent: Any) -> Any:
    """
    Merge an ancestor's `media_global_governance` (`parent`) into the
    group's own view (`base`, the levels below it merged so far) and return
    the result; `base` is changed in place when it is a JSON object.
    Most-restrictive wins per field (`MEDIA_GOVERNANCE_FIELD_RULES`), on the
    full ``{mode: {field: value}}`` document. The gateway's
    `merge_media_governance`, rule for rule:

    - A level's `null` (a document, a mode or a field) states nothing: the
      other level's stays. A mode or field only the parent states is
      inherited.
    - whitelist: intersection (by JSON value); an empty or unreadable list is
      unconstrained, the identity element.
    - ceiling / floor: the smaller / larger stated bound, kept as stated. A
      bound is a finite number, 0 or more; anything else is no bound at its
      level.
    - bool_or: true wins; a value that is not a JSON bool is false.
    - A field with no rule: the parent's only where the group states none.
    """
    if not isinstance(parent, dict):
        return base
    if not isinstance(base, dict):
        return copy.deepcopy(parent)

    def bound(value: Any) -> Optional[float]:
        number = _as_finite_float(value)
        return number if number is not None and number >= 0 else None

    for mode, parent_fields in parent.items():
        if not isinstance(parent_fields, dict):
            continue
        base_fields = base.setdefault(mode, {})
        if not isinstance(base_fields, dict):
            base[mode] = copy.deepcopy(parent_fields)
            continue

        for field, parent_val in parent_fields.items():
            rule = MEDIA_GOVERNANCE_FIELD_RULES.get(field)
            if rule == 'whitelist':
                if field not in base_fields:
                    base_fields[field] = copy.deepcopy(parent_val)
                    continue
                child_val = base_fields[field]
                p_list = list(parent_val) if isinstance(parent_val, list) else []
                c_list = list(child_val) if isinstance(child_val, list) else []
                # An empty list is "unconstrained", the identity element —
                # intersecting with it would wrongly forbid everything.
                if not c_list:
                    merged = p_list
                elif not p_list:
                    merged = c_list
                else:
                    merged = [x for x in c_list if any(_same_json(x, p) for p in p_list)]
                base_fields[field] = copy.deepcopy(merged)
            elif rule in ('ceiling', 'floor'):
                parent_bound = bound(parent_val)
                if parent_bound is None:
                    continue
                child_bound = bound(base_fields.get(field))
                if (
                    child_bound is None
                    or (rule == 'floor' and parent_bound > child_bound)
                    or (rule == 'ceiling' and parent_bound < child_bound)
                ):
                    base_fields[field] = copy.deepcopy(parent_val)
            elif rule == 'bool_or':
                base_fields[field] = base_fields.get(field) is True or parent_val is True
            elif base_fields.get(field) is None:
                base_fields[field] = copy.deepcopy(parent_val)
    return base


def _merge_governance_child_first(documents: List[Any], merge: Any) -> Any:
    """Governance documents of a chain, root first, merged as the gateway
    walks it: the group's own first, then each ancestor's into it (`merge`
    is `_skill_governance_restrictive_merge` or
    `_media_governance_restrictive_merge`). None when no level states one."""
    effective = None
    for document in reversed(documents):
        if document is None:
            continue
        effective = copy.deepcopy(document) if effective is None else merge(effective, document)
    return effective


def _a2a_governance_restrictive_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> None:
    """
    Merge A2A global governance with restrictive semantics.

    Shape:
        {
            "capability": {"default_access": "enabled|approval_required|disabled",
                           "approval_timeout_seconds": <int>},
            "data": {"deny_classifications": ["PII", "PHI", ...]},
            "remote": {"allowed_globs": ["https://*.partner.com/*", ...]}
        }

    Rules:
    - capability.default_access: max strictness wins (same enum as MCP).
    - capability.approval_timeout_seconds: min wins.
    - data.deny_classifications: union — more deny tags = more restrictive.
    - remote.allowed_globs: not merged. The gateway judges an outbound
        address by each level's list on its own; the merged document
        carries no `remote` section (`ResourceGroup.a2a_remote_allow_lists`
        gives the list of each level).
    """
    # capability section
    if 'capability' in overlay:
        child_cap = overlay['capability']
        if 'capability' not in base:
            import copy
            base['capability'] = copy.deepcopy(child_cap)
        else:
            parent_cap = base['capability']
            if 'default_access' in child_cap:
                p_strict = _MCP_ACCESS_STRICTNESS.rank(parent_cap.get('default_access'))
                c_strict = _MCP_ACCESS_STRICTNESS.rank(child_cap['default_access'])
                if c_strict > p_strict:
                    parent_cap['default_access'] = child_cap['default_access']
                elif 'default_access' not in parent_cap:
                    parent_cap['default_access'] = child_cap['default_access']
            _merge_numeric_min(parent_cap, child_cap, 'approval_timeout_seconds')

    # data section — deny classifications union
    if 'data' in overlay:
        child_data = overlay['data']
        if 'data' not in base:
            import copy
            base['data'] = copy.deepcopy(child_data)
        else:
            _merge_list_union(base['data'], child_data, 'deny_classifications')

    # remote section: no merge. An outbound address has to match the
    # allow-list of EVERY level that states one, and no single list says
    # that (the patterns two lists share may be none while addresses match
    # both). The lists are shown per level: `a2a_remote_allow_lists`.
    base.pop('remote', None)


# The token quotas of an LLM limits document: the totals, and the input and
# the output side of each window.
LLM_TOKEN_QUOTA_KEYS = (
    "daily_limit", "monthly_limit",
    "daily_input_limit", "daily_output_limit", "monthly_input_limit", "monthly_output_limit",
)


def _glob_problem(pattern: str) -> Optional[str]:
    """Why an allow-list pattern cannot be read: an unclosed character
    class or alternation. The gateway drops a pattern it cannot compile,
    and a list of only such patterns allows nothing."""
    depth = 0
    in_class = False
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            index += 2
            continue
        if in_class:
            if char == "]":
                in_class = False
        elif char == "[":
            in_class = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                return "a closing brace without an opening one"
        index += 1
    if in_class:
        return "an unclosed character class"
    if depth:
        return "an unclosed alternation"
    return None


def validate_a2a_governance(governance: Any) -> tuple:
    """Validate an A2A policy document: a resource group's
    `a2a_global_governance` or an agent card's `tenant_governance`.

    The gateway judges a call by each such document and refuses the call
    when one cannot be read, so what it could not read is refused here: a
    document or a section that is not an object, an access it does not
    know, a list that is not a list of strings."""
    if governance is None or governance == {}:
        return True, None
    if not isinstance(governance, dict):
        return False, "the policy must be a JSON object"
    for section in ('capability', 'data', 'remote'):
        if governance.get(section) is not None and not isinstance(governance[section], dict):
            return False, f"{section} must be a JSON object"
    try:
        valid_sections = {'capability', 'data', 'remote'}
        unknown = set(governance.keys()) - valid_sections
        if unknown:
            return False, f"Unknown A2A governance sections: {', '.join(sorted(unknown))}"

        cap = governance.get('capability') or {}
        if cap:
            if not isinstance(cap, dict):
                return False, "capability must be a JSON object"
            valid_cap_keys = {'default_access', 'approval_timeout_seconds'}
            unknown_cap = set(cap.keys()) - valid_cap_keys
            if unknown_cap:
                return False, f"Unknown capability fields: {', '.join(sorted(unknown_cap))}"
            if 'default_access' in cap and not _is_one_of(cap['default_access'], _MCP_ACCESS_STRICTNESS):
                return False, "capability.default_access must be enabled|approval_required|disabled"
            if 'approval_timeout_seconds' in cap:
                problem = approval_timeout_error(
                    cap['approval_timeout_seconds'], "capability.approval_timeout_seconds"
                )
                if problem:
                    return False, problem

        data = governance.get('data') or {}
        if data:
            if not isinstance(data, dict):
                return False, "data must be a JSON object"
            if 'deny_classifications' in data:
                v = data['deny_classifications']
                if not isinstance(v, list) or any(not isinstance(s, str) or not s.strip() for s in v):
                    return False, "data.deny_classifications must be a list of non-empty strings"

        remote = governance.get('remote') or {}
        if remote:
            if not isinstance(remote, dict):
                return False, "remote must be a JSON object"
            if 'allowed_globs' in remote:
                v = remote['allowed_globs']
                if not isinstance(v, list) or any(not isinstance(s, str) or not s.strip() for s in v):
                    return False, "remote.allowed_globs must be a list of non-empty strings"
                for pattern in v:
                    problem = _glob_problem(pattern)
                    if problem:
                        return False, f"remote.allowed_globs: '{pattern}' is not a readable pattern ({problem})"

        return True, None
    except Exception as e:
        return False, f"Invalid A2A governance structure: {str(e)}"


def validate_a2a_limits(limits: Any) -> tuple:
    """Validate an A2A limits document: a resource group's
    `a2a_global_limits` or an agent card's `tenant_limits`. The sections
    are the ones the gateway enforces: `invocations` (a count per day and
    per month), `frequency` (calls per minute, hour and day) and `chain`
    (the delegation depth)."""
    if limits is None or limits == {}:
        return True, None
    if not isinstance(limits, dict):
        return False, "the limits must be a JSON object"

    try:
        valid_sections = {'invocations', 'chain', 'frequency'}
        unknown = set(limits.keys()) - valid_sections
        if unknown:
            return False, f"Unknown A2A limit sections: {', '.join(sorted(unknown))}"
        for section in valid_sections:
            if limits.get(section) is not None and not isinstance(limits[section], dict):
                return False, f"{section} must be a JSON object"

        freq = limits.get('frequency') or {}
        if freq:
            valid_freq = {'max_calls_per_minute', 'max_calls_per_hour', 'max_calls_per_day',
                          'burst_limit', 'cooldown_seconds'}
            unknown_freq = set(freq.keys()) - valid_freq
            if unknown_freq:
                return False, f"Unknown frequency fields: {', '.join(sorted(unknown_freq))}"
            for k in sorted(valid_freq):
                v = freq.get(k)
                if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 1):
                    return False, f"frequency.{k} must be a positive integer (>= 1)"

        inv = limits.get('invocations') or {}
        if inv:
            if not isinstance(inv, dict):
                return False, "invocations must be a JSON object"
            # A2A invocation caps always block at 100% (the gateway blocks
            # on invocations.daily_limit / monthly_limit): `hard_stop` may
            # be true or absent, and no alert is raised before the cap, so
            # the warning percentages are not available either
            # (`release_scope.a2a_limits_problem`, below).
            valid_keys = {'daily_limit', 'monthly_limit',
                          'daily_warning_percent', 'monthly_warning_percent',
                          'hard_stop'}
            unknown_inv = set(inv.keys()) - valid_keys
            if unknown_inv:
                return False, f"Unknown invocations fields: {', '.join(sorted(unknown_inv))}"
            for k in ('daily_limit', 'monthly_limit'):
                if k in inv and inv[k] is not None:
                    if not isinstance(inv[k], int) or isinstance(inv[k], bool) or inv[k] < 0:
                        return False, f"invocations.{k} must be a non-negative integer"
            for k in ('daily_warning_percent', 'monthly_warning_percent'):
                if k in inv and inv[k] is not None:
                    if not isinstance(inv[k], int) or inv[k] < 0 or inv[k] > 100:
                        return False, f"invocations.{k} must be an integer 0..100"

        chain = limits.get('chain') or {}
        if chain:
            if not isinstance(chain, dict):
                return False, "chain must be a JSON object"
            valid_chain_keys = {'max_delegation_depth'}
            unknown_chain = set(chain.keys()) - valid_chain_keys
            if unknown_chain:
                return False, f"Unknown chain fields: {', '.join(sorted(unknown_chain))}"
            if 'max_delegation_depth' in chain and chain['max_delegation_depth'] is not None:
                v = chain['max_delegation_depth']
                if not isinstance(v, int) or v < 0 or v > 32:
                    return False, "chain.max_delegation_depth must be an integer 0..32 (absolute proxy cap)"

        unavailable = release_scope.a2a_limits_problem(limits)
        if unavailable:
            return False, unavailable
        return True, None
    except Exception as e:
        return False, f"Invalid A2A limits structure: {str(e)}"


def validate_mcp_governance(governance: Dict[str, Any]) -> tuple:
    """Validate MCP global governance JSON structure."""
    if not governance:
        return True, None
    try:
        gov = governance if isinstance(governance, dict) else {}
        valid_keys = {'default_tool_access', 'default_resource_access',
                      'default_prompt_access', 'default_resource_template_access',
                      'approval_timeout_seconds'}
        unknown = set(gov.keys()) - valid_keys
        if unknown:
            return False, f"Unknown MCP governance fields: {', '.join(sorted(unknown))}"
        problem = mcp_governance_value_problem(gov)
        if problem:
            return False, problem
        if 'approval_timeout_seconds' in gov:
            problem = approval_timeout_error(gov['approval_timeout_seconds'])
            if problem:
                return False, problem
        return True, None
    except Exception as e:
        return False, f"Invalid MCP governance structure: {str(e)}"


def validate_skill_governance(governance: Dict[str, Any]) -> tuple:
    """Validate skill governance JSON structure (global or per-skill)."""
    if not governance:
        return True, None
    try:
        gov = governance if isinstance(governance, dict) else {}
        valid_keys = {'execution_mode', 'discovery_enabled', 'approval_timeout_seconds', 'max_concurrent_executions', 'max_tool_calls_per_execution'}
        unknown = set(gov.keys()) - valid_keys
        if unknown:
            return False, f"Unknown skill governance fields: {', '.join(sorted(unknown))}"
        if 'execution_mode' in gov:
            if not _is_one_of(gov['execution_mode'], _SKILL_EXECUTION_STRICTNESS):
                return False, f"execution_mode must be one of: {', '.join(sorted(_SKILL_EXECUTION_STRICTNESS))}"
        if 'discovery_enabled' in gov and not isinstance(gov['discovery_enabled'], bool):
            return False, "discovery_enabled must be a boolean"
        if 'approval_timeout_seconds' in gov:
            problem = approval_timeout_error(gov['approval_timeout_seconds'])
            if problem:
                return False, problem
        if 'max_concurrent_executions' in gov:
            if not isinstance(gov['max_concurrent_executions'], int) or gov['max_concurrent_executions'] < 1:
                return False, "max_concurrent_executions must be a positive integer (>= 1)"
        if 'max_tool_calls_per_execution' in gov:
            if not isinstance(gov['max_tool_calls_per_execution'], int) or gov['max_tool_calls_per_execution'] < 1:
                return False, "max_tool_calls_per_execution must be a positive integer (>= 1)"
        return True, None
    except Exception as e:
        return False, f"Invalid skill governance structure: {str(e)}"


# ============================================
# ResourceGroup Model
# ============================================

# Authorship and version come from the shared mixins rather than being
# re-declared here: same columns, but the flush listener only stamps
# objects it can recognise, and a hand-rolled copy silently opted this
# model out of it.
class ResourceGroup(ResourceAuthorshipMixin, ResourceVersionMixin, Base):
    """
    Resource Group model for organizing AI resources with hierarchical structure.

    Groups allow administrators to organize resources by department, team,
    project, application, agent, or any other organizational structure.

    **Flexible Hierarchy Examples:**
    - Engineering (DEPARTMENT) > Frontend Team (TEAM) > Customer Portal (APPLICATION)
    - Sales (DEPARTMENT) > Enterprise Sales (TEAM) > Lead Qualification Agent (AGENT)
    - Q1-Initiative (PROJECT) > Customer Analytics (APPLICATION)
    - Data Science (DEPARTMENT) > ML Research (TEAM) > Recommendation Assistant (ASSISTANT)

    **Hierarchical Structure**
    - Groups can have parent groups for unlimited nesting
    - Sub-groups inherit limits, models, and MCPs from parent groups
    - Depth-first traversal for limit/resource resolution

    **Access Control:**
    - MCP Server configurations can be scoped to specific groups
    - LLM models can be scoped to specific groups
    - LLM Access Keys can be scoped to specific groups
    - Users only see resources they have access to based on group membership

    **Usage Limits:**
    - LLM: Budget, tokens, requests (daily/monthly with warnings)
    - MCP: Execution time, concurrency, payload size, frequency limits
    - Inherits from parent groups unless overridden
    """
    __tablename__ = "resource_groups"

    # =========================================================================
    # PRIMARY KEY & MULTI-TENANCY
    # =========================================================================

    id = Column(String(255), primary_key=True)

    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Tenant this record belongs to"
    )

    # =========================================================================
    # HIERARCHICAL STRUCTURE
    # =========================================================================

    parent_group_id = Column(
        String(255),
        ForeignKey("resource_groups.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="Parent group ID for hierarchical organization (NULL for root groups)"
    )

    hierarchy_level = Column(
        Integer,
        nullable=False,
        default=0,
        comment="Depth in hierarchy (0=root, 1=child, 2=grandchild, etc.)"
    )

    hierarchy_path = Column(
        String(1000),
        nullable=True,
        comment="Full path from root (e.g., 'Engineering/Frontend/React')"
    )

    # =========================================================================
    # INHERITANCE CONTROL
    # =========================================================================

    inherit_resources = Column(
        "inherit_from_parent",
        Boolean,
        nullable=False,
        default=True,
        comment="Whether this group inherits resources (LLMs, MCPs, Skills) from parent groups. Governance inheritance (guardrails, limits, MCP capabilities) is always enabled."
    )

    # =========================================================================
    # GROUP INFORMATION
    # =========================================================================

    name = Column(
        String(255),
        nullable=False,
        comment="Unique group name within parent (e.g., 'Engineering', 'Marketing')"
    )

    display_name = Column(
        String(255),
        nullable=True,
        comment="Optional display name for UI"
    )

    description = Column(
        Text,
        nullable=True,
        comment="Description of the group's purpose"
    )

    # =========================================================================
    # VISUAL CUSTOMIZATION
    # =========================================================================

    icon = Column(
        String(100),
        nullable=True,
        comment="Lucide-react icon name (e.g., 'Users', 'Briefcase', 'Code')"
    )

    color = Column(
        String(50),
        nullable=True,
        comment="Color for UI display (e.g., 'blue', 'green', '#3B82F6')"
    )

    # =========================================================================
    # GROUP TYPE/CATEGORY
    # =========================================================================

    group_type = Column(
        SQLEnum(GroupType),
        nullable=True,
        default=GroupType.CUSTOM,
        comment="Group type: organization, department, team, project, ai_system, custom, etc."
    )

    system_kind = Column(
        String(24),
        nullable=True,
        comment=(
            "AI System kind: agent | assistant | application | "
            "integration | workflow | service. Only meaningful when "
            "group_type='ai_system'; selects the lifecycle signal profile. "
            "NULL for every other group type."
        )
    )

    baseline_opt_out = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment=(
            "Exclude this AI System from baseline "
            "computation and drift detection. For systems whose behaviour "
            "is legitimately erratic by design, where a baseline would only "
            "ever produce noise."
        )
    )

    autonomy_level = Column(
        String(24),
        nullable=True,
        comment=(
            "How much this group may do without a human: "
            "autonomous | approval_required | restricted | suspended. "
            "Resolved min-wins across the hierarchy and the actor, so a "
            "NULL here contributes nothing and cannot loosen a parent, and "
            "an agent identity may be more restricted than its AI System "
            "but never less. Distinct from agent_identities.enforcement_mode, "
            "which is about whether policy bites at all. Suspending an AI "
            "System suspends every actor inside it — that is what an "
            "operator means by 'turn it off'."
        )
    )

    replay_suite_id = Column(
        String(64),
        nullable=True,
        comment=(
            "The replay suite the approved → active gate "
            "consults. NULL means no replay requirement, and the gate reports "
            "that explicitly rather than silently passing: a system nobody has "
            "built a suite for has not demonstrated anything."
        )
    )

    operating_envelope = Column(
        JSON,
        nullable=True,
        comment=(
            "Declared operating-envelope terms ({term: bound}), the "
            "'what it should achieve' half of the contract next to the "
            "authorization closure's 'what it may do'. Validated against "
            "app/services/ai_system_envelope.py; folded into the resolved "
            "closure (and therefore the contract hash) only when set, so "
            "systems without one keep their existing hashes. Loosening a "
            "term is a contract widening an approver must read."
        )
    )

    declared_outcomes = Column(
        JSONB(none_as_null=True),  # None is SQL NULL ("no catalogue"), not JSON null
        nullable=True,
        comment=(
            "Outcome catalogue: how a run of this AI System may "
            "end ([{id, label, counts_toward_baseline, expect?, variants?}]). "
            "Validated by app/services/ai_system_outcome_catalogue.py; folded "
            "into the resolved closure as section `outcomes` when set, so the "
            "approver signs it. NULL = no catalogue (every reported id is "
            "undeclared once a contract governs)."
        )
    )

    run_idle_timeout_seconds = Column(
        Integer,
        nullable=True,
        comment=(
            "Seconds a run may sit with no new step before "
            "the finalizer closes it as abandoned. NULL = platform default "
            "(120s). Per-system because an 'integration' may legitimately "
            "pause for minutes between steps while an 'application' should "
            "never take two minutes. Only meaningful on AI System groups."
        )
    )

    approval_window_seconds = Column(
        Integer,
        nullable=True,
        comment=(
            "Default window of a hold raised for this group (seconds). "
            "Inherited min-wins; NULL = not stated here"
        ),
    )
    # How long a human-approval hold raised for this group stays
    # open when the policy that demanded the human names no window of its
    # own (skill runs, A2A calls, agent-grant and autonomy holds). Min-wins
    # across the group and every ancestor, then the tenant default, then the
    # platform's 300 s (`app/constants/approval_window.py`; the core's
    # `services/approval_window.rs` is the one resolver). 60 s .. 7 days.

    # =========================================================================
    # GOVERNANCE / OWNERSHIP  (AI-System attributes on the RG)
    # =========================================================================
    # An "AI System" is a Resource Group with group_type='ai_system'; these
    # fields carry the AI-System documentation that used to live on the retired
    # ai_systems entity. They are meaningful for any group (org / team / app /
    # system), so they live on every Resource Group. All optional.

    owner = Column(
        String(255),
        nullable=True,
        comment="Accountable owner (person or team) for this group / AI system"
    )

    intended_use = Column(
        Text,
        nullable=True,
        comment="ISO 42001 intended-use / purpose documentation"
    )

    # Intended clients are TYPED. A caller is either
    # another AI System (evaluated against the segments whose root is that
    # system) or a non-system client app (evaluated against the run's
    # `client_label`). One untyped list compared a system id against client
    # labels, so a service's declaration could never be evaluated.
    intended_client_labels = Column(
        JSON,
        nullable=True,
        comment="Declarative list of client_label strings (non-system callers, "
                "e.g. ['Goose', 'Internal Portal']) this is intended to be "
                "accessed through; compared against runs' client_label"
    )

    intended_client_systems = Column(
        JSON,
        nullable=True,
        comment="AI System ids that may call this system; compared "
                "against the root systems of its run segments"
    )

    # Per SERVICE: when true the core refuses an
    # A2A call from a caller system not in intended_client_systems (proxy
    # subtype `unaccepted_caller`); when false the same fact is the advisory
    # finding composition.unaccepted_caller. AI Systems only (API: 422).
    enforce_accepted_callers = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
        comment="AI System services only: refuse A2A calls from caller "
        "systems not in intended_client_systems",
    )

    lifecycle_stage = Column(
        String(32),
        nullable=True,
        comment="Governance lifecycle: proposed|approved|active|deprecated|retired"
    )

    # Attested exclusion from the estate rollup. Mission Control takes
    # the WORST health/verdict over a subtree, which is the right policy and
    # also means any real estate reads red forever: a retired system, a
    # discovered system nobody has wired up, a batch job in planned downtime.
    # This is not a mute. It carries a reason, a note, who set it and an
    # expiry; the estate card counts exclusions, the report and digest
    # disclose them, and a lapsed one re-enters the rollup and raises an
    # inbox item. Shape: {reason, note, by, at, expires_at}. NULL = included.
    rollup_exclusion = Column(
        JSONB,
        nullable=True,
        comment=(
            "Attested exclusion from the estate health/verdict rollup: "
            "{reason, note, by, at, expires_at}. NULL = included."
        ),
    )

    # Succession, recorded at deprecation.
    #
    # The deprecate gate asks "is there a successor, or is there explicitly
    # none?". These two columns keep the answer, so a deprecated system can
    # tell you where its capability went.
    #
    # Exactly one is meaningful:
    #   * successor_system_id set     → superseded; the work moved there.
    #   * no_successor_declared True  → withdrawn; nothing replaces it.
    #   * both unset                  → never asked (a system still running).
    successor_system_id = Column(
        String(255),
        ForeignKey("resource_groups.id", ondelete="SET NULL"),
        nullable=True,
        comment=(
            "The AI System that supersedes this one, recorded when it was "
            "deprecated. NULL with no_successor_declared means withdrawn."
        ),
    )
    no_successor_declared = Column(
        Boolean,
        nullable=True,
        comment=(
            "True when an operator explicitly stated this system has no "
            "successor. NULL means the question was never put."
        ),
    )

    # NOTE: the EU AI Act risk tier lives on the existing `eu_ai_act_risk_tier`
    # column (minimal|limited|high|unacceptable) below — no separate risk_tier.

    # =========================================================================
    # STATUS
    # =========================================================================

    is_active = Column(
        Boolean,
        nullable=False,
        default=True,
        comment="Whether this group is active"
    )

    is_system = Column(
        Boolean,
        nullable=False,
        default=False,
        comment="System groups are created during initialization and cannot be deleted (e.g. Default)"
    )

    # =========================================================================
    # LLM GLOBAL LIMITS & THRESHOLDS
    # =========================================================================

    llm_global_limits = Column(
        JSON(none_as_null=True),
        nullable=True,
        comment="""JSON structure for LLM global usage limits and thresholds:
        {
            "budget": {
                "daily_limit_usd": 100.0,
                "monthly_limit_usd": 3000.0,
                "daily_warning_percent": 80,
                "monthly_warning_percent": 90
            },
            "tokens": {
                "daily_limit": 1000000,
                "monthly_limit": 30000000,
                "daily_warning_percent": 85,
                "monthly_warning_percent": 90
            },
            "requests": {
                "daily_limit": 10000,
                "monthly_limit": 300000,
                "daily_warning_percent": 80,
                "monthly_warning_percent": 85
            }
        }"""
    )

    # =========================================================================
    # MCP GLOBAL LIMITS & THRESHOLDS
    # =========================================================================

    mcp_global_limits = Column(
        JSON(none_as_null=True),
        nullable=True,
        comment="""JSON structure for MCP global usage limits and thresholds:
        {
            "execution": {
                "timeout_seconds": 30,
                "max_concurrency_per_user": 5,
                "max_concurrency_per_group": 20
            },
            "data": {
                "max_payload_size_mb": 2
            },
            "frequency": {
                "max_calls_per_minute": 60,
                "max_calls_per_hour": 3600,
                "max_calls_per_day": 86400,
                "burst_limit": 10,
                "cooldown_seconds": 1
            }
        }"""
    )

    # =========================================================================
    # GLOBAL GOVERNANCE (inherited via ancestor chain, restrictive merge)
    # =========================================================================

    llm_global_governance = Column(
        JSON(none_as_null=True),
        nullable=True,
        comment="""JSON structure for LLM global governance ceiling:
        {
            "temperature": {"max": 1.0, "enforce": "clamp"},
            "context_window": {"max_input_tokens": 100000, "max_output_tokens": 4096, "enforce": "reject"},
            "tool_use": {"access": "enabled", "allowed_tools": [], "blocked_tools": [],
                         "max_tool_calls_per_request": 10, "max_tool_calls_per_hour": 500,
                         "max_tool_calls_per_day": 5000, "banned_argument_patterns": []},
            "system_prompt": {"mandatory_fragments": [], "banned_patterns": []}
        }"""
    )

    mcp_global_governance = Column(
        JSON(none_as_null=True),
        nullable=True,
        comment="""JSON structure for MCP global governance ceiling:
        {
            "default_tool_access": "enabled",
            "default_resource_access": "enabled",
            "default_prompt_access": "enabled",
            "default_resource_template_access": "enabled",
            "approval_timeout_seconds": 300
        }"""
    )

    skill_global_governance = Column(
        JSON(none_as_null=True),
        nullable=True,
        comment="""JSON structure for Agent Skills global governance:
        {
            "execution_mode": "unrestricted",
            "discovery_enabled": true,
            "approval_timeout_seconds": 300,
            "max_concurrent_executions": 5,
            "max_tool_calls_per_execution": 10   (advisory: handed to the agent)
        }"""
    )

    skill_global_limits = Column(
        JSON(none_as_null=True),
        nullable=True,
        comment="""JSON structure for Agent Skills global usage limits:
        {
            "execution": {
                "daily_limit": 1000,
                "monthly_limit": 30000,
                "daily_warning_percent": 80,
                "monthly_warning_percent": 90
            }
        }"""
    )

    # =========================================================================
    # A2A (Agent-to-Agent) GLOBAL LIMITS & GOVERNANCE
    # =========================================================================
    # Inherits restrictively from ancestors
    # (see get_effective_a2a_* methods below). The Rust proxy reads these on
    # every inbound + outbound A2A step and enforces before dispatch.

    a2a_global_limits = Column(
        JSON(none_as_null=True),
        nullable=True,
        comment="""JSON structure for A2A global usage limits:
        {
            "invocations": {
                "daily_limit": 10000,
                "monthly_limit": 300000
            },
            "chain": {
                "max_delegation_depth": 5
            }
        }"""
    )

    media_global_limits = Column(
        JSON(none_as_null=True),
        nullable=True,
        comment="""Per-modality usage limits keyed by LLModelMode value.
        Mirrors llm_global_limits / a2a_global_limits but indexed by mode
        so one column covers audio_speech, audio_transcription,
        image_generation, video_generation, and embedding without a
        new column per modality. Schema:
        {
            "audio_speech": {
                "daily_budget_usd": 5.0,
                "daily_characters": 100000,
                "daily_requests": 500
            },
            "image_generation": {
                "daily_images": 200,
                "daily_budget_usd": 20.0
            },
            ...
        }"""
    )

    media_global_governance = Column(
        JSON(none_as_null=True),
        nullable=True,
        comment="""Per-modality behavioral governance keyed by LLModelMode value.
        Sibling to llm_global_governance / mcp_global_governance — drives
        request-time validation of voice/format/size/duration whitelists
        in the Rust proxy. Schema:
        {
            "audio_speech": {
                "voice_whitelist": ["alloy", "echo"],
                "response_format_whitelist": ["mp3"],
                "max_input_characters": 4000,
                "speed_min": 0.5, "speed_max": 1.5
            },
            "image_generation": {
                "size_whitelist": ["1024x1024"],
                "quality_whitelist": ["standard"],
                "max_n": 1
            },
            ...
        }"""
    )

    a2a_global_governance = Column(
        JSON(none_as_null=True),
        nullable=True,
        comment="""JSON structure for A2A global governance ceiling:
        {
            "capability": {
                "default_access": "enabled|approval_required|disabled",
                "approval_timeout_seconds": 300
            },
            "data": {
                "deny_classifications": ["PII", "PHI"]
            },
            "remote": {
                "allowed_globs": ["https://*.partner.com/*"]
            }
        }"""
    )

    # =========================================================================
    # COMPLIANCE FRAMEWORK MAPPING
    # =========================================================================
    # Read at request time by the tagging engine; only fires when
    # the parent tenant lists the matching framework in its
    # compliance_profile. NULL / false on both is the safe default
    # — operators opt in per-RG via the admin UI.

    eu_ai_act_risk_tier = Column(
        String(32),
        nullable=True,
        comment=(
            "EU AI Act risk classification for traffic through this RG: "
            "minimal | limited | high | unacceptable. Tagged per-call "
            "when tenant lists eu_ai_act in compliance_profile. NULL "
            "= unclassified (no tag fires)."
        ),
    )

    eu_ai_act_role = Column(
        String(40),
        nullable=True,
        comment=(
            "Regulated role this tenant holds for the AI System: "
            "provider | deployer | provider_and_deployer | importer | "
            "distributor | product_manufacturer | "
            "authorised_representative. NULL = undeclared."
        ),
    )

    phi_resource = Column(
        Boolean,
        nullable=False,
        server_default="false",
        default=False,
        comment=(
            "RG handles Protected Health Information. When true + tenant "
            "lists hipaa in compliance_profile, every call through this "
            "RG carries hipaa.phi in compliance_tags. Operator opt-in."
        ),
    )

    sensitive_data = Column(
        Boolean,
        nullable=False,
        server_default="false",
        default=False,
        comment=(
            "RG handles sensitive data (privileged-read rule): "
            "read-only tool calls through this RG are sealed "
            "as evidence records like commands. Framework-neutral "
            "counterpart of phi_resource. Operator opt-in."
        ),
    )

    # Provenance (Traffic Data Import). 'manual' (default) |
    # 'import' (a workspace auto-created from a vendor usage-import).
    source = Column(
        String(16),
        nullable=False,
        server_default="manual",
        default="manual",
        comment="manual | import",
    )
    source_external_id = Column(
        String(255),
        nullable=True,
        comment="External workspace id this group was discovered under (import)",
    )

    # =========================================================================
    # AUDIT
    # =========================================================================

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False
    )

    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False
    )

    # =========================================================================
    # RELATIONSHIPS
    # =========================================================================

    # Self-referential relationship for hierarchy
    parent_group = relationship(
        "ResourceGroup",
        remote_side=[id],
        back_populates="children",
        foreign_keys=[parent_group_id]
    )

    children = relationship(
        "ResourceGroup",
        back_populates="parent_group",
        cascade="all, delete-orphan",
        foreign_keys=[parent_group_id]
    )

    # Many-to-many: ResourceGroup <-> LLModel
    llm_models: Mapped[List["LLModel"]] = relationship(
        "LLModel",
        secondary="resource_group_llm_models",
        back_populates="resource_groups",
        lazy="dynamic"
    )

    # Many-to-many: ResourceGroup <-> ServerConfig
    server_configs: Mapped[List["ServerConfig"]] = relationship(
        "ServerConfig",
        secondary="resource_group_server_configs",
        back_populates="resource_groups",
        lazy="dynamic"
    )

    # One-to-many: ResourceGroup -> APIKey
    api_keys: Mapped[List["APIKey"]] = relationship(
        "APIKey",
        back_populates="group",
        lazy="dynamic",
        cascade="all, delete-orphan"
    )

    # One-to-many
    members: Mapped[List["ResourceGroupMembership"]] = relationship(
        "ResourceGroupMembership",
        back_populates="group",
        lazy="dynamic",
        cascade="all, delete-orphan"
    )

    # Many-to-many with EndUserGroup. Operators bind a
    # *group of users* to a Resource Group; every current member of
    # the EndUserGroup inherits the binding. Independent of the
    # direct `members` (user-level) M2M above — the two coexist so a
    # tenant can mix group-mediated access with manual one-off
    # assignments. See `app/database/models/end_user_group.py`.
    end_user_groups: Mapped[List["EndUserGroup"]] = relationship(
        "EndUserGroup",
        secondary="resource_group_end_user_groups",
        back_populates="resource_groups",
        lazy="selectin",
    )

    # Many-to-many: ResourceGroup <-> AgentSkill
    agent_skills: Mapped[List["AgentSkill"]] = relationship(
        "AgentSkill",
        secondary="resource_group_agent_skills",
        back_populates="resource_groups",
        lazy="dynamic"
    )

    # Many-to-many: ResourceGroup <-> GuardrailConfig
    guardrail_configs: Mapped[List["GuardrailConfig"]] = relationship(
        "GuardrailConfig",
        secondary="resource_group_guardrails",
        back_populates="groups",
        lazy="dynamic"
    )

    # Many-to-many: ResourceGroup <-> ArgumentPolicy (POLICY-10 AST policies)
    argument_policies: Mapped[List["ArgumentPolicy"]] = relationship(
        "ArgumentPolicy",
        secondary="resource_group_argument_policies",
        back_populates="groups",
        lazy="dynamic"
    )

    # Many-to-many: ResourceGroup <-> SemanticCacheConfig
    cache_configs: Mapped[List["SemanticCacheConfig"]] = relationship(
        "SemanticCacheConfig",
        secondary="resource_group_cache_configs",
        back_populates="groups",
        lazy="dynamic"
    )

    # Many-to-many: ResourceGroup <-> PromptCachePolicy.
    # Provider-side prompt caching (Anthropic cache_control markers,
    # OpenAI automatic prefix, Gemini cachedContents). Distinct concern
    # from `cache_configs` above — see PromptCachePolicy for the split.
    # Binding table enforces "one policy per group at a time."
    prompt_cache_policies: Mapped[List["PromptCachePolicy"]] = relationship(
        "PromptCachePolicy",
        secondary="resource_group_prompt_cache_policies",
        back_populates="resource_groups",
        lazy="dynamic",
    )

    # Many-to-many: ResourceGroup <-> KbCollection (Knowledge Base)
    kb_collections: Mapped[List["KbCollection"]] = relationship(
        "KbCollection",
        secondary="resource_group_kb_collections",
        back_populates="groups",
        lazy="dynamic"
    )

    # Many-to-many: ResourceGroup <-> KbConnector (KB pull-ingest)
    # Connector-fed content lands in a KB collection, and the
    # connector is also bound directly to RGs so retrieval can
    # filter by "is the user's RG allowed to see this connector's
    # content". Mirrors kb_collections binding.
    kb_connectors: Mapped[List["KbConnector"]] = relationship(
        "KbConnector",
        secondary="resource_group_kb_connectors",
        back_populates="groups",
        lazy="dynamic",
    )

    # Many-to-many: ResourceGroup <-> AgentCard (A2A)
    # An agent card may be bound to any number of groups. Effective
    # access walks the group's ancestor chain with additive inheritance
    # (same rule as KB collections / skills / MCP servers).
    agent_cards: Mapped[List["AgentCard"]] = relationship(
        "AgentCard",
        secondary="resource_group_agent_cards",
        back_populates="resource_groups",
        lazy="dynamic"
    )


    # =========================================================================
    # INDEXES
    # =========================================================================

    __table_args__ = (
        # Unique constraint: name must be unique within parent and tenant
        UniqueConstraint('tenant_id', 'parent_group_id', 'name',
                        name='uix_end_user_groups_tenant_parent_name'),

        # Primary queries - "Get all active groups for tenant"
        Index('ix_end_user_groups_tenant_active', 'tenant_id', 'is_active'),

        # Hierarchy queries - "Get children of a group"
        Index('ix_end_user_groups_tenant_parent', 'tenant_id', 'parent_group_id'),

        # Root groups - "Get top-level groups"
        Index('ix_end_user_groups_tenant_root', 'tenant_id', 'parent_group_id',
              postgresql_where=Column('parent_group_id').is_(None)),

        # Type-based queries - "Get all teams" or "Get all applications"
        Index('ix_end_user_groups_tenant_type', 'tenant_id', 'group_type'),
        Index('ix_end_user_groups_tenant_type_active', 'tenant_id', 'group_type', 'is_active'),

        # Name lookup
        Index('ix_end_user_groups_tenant_name', 'tenant_id', 'name'),

        # Hierarchy navigation
        Index('ix_end_user_groups_hierarchy_level', 'tenant_id', 'hierarchy_level'),
        Index('ix_end_user_groups_hierarchy_path', 'hierarchy_path'),

        # Audit queries
        Index('ix_end_user_groups_created_at', 'tenant_id', 'created_at'),

        CheckConstraint(
            'approval_window_seconds IS NULL OR '
            '(approval_window_seconds >= 60 AND approval_window_seconds <= 604800)',
            name='ck_resource_groups_approval_window',
        ),

        # Skill governance queries (via JSONB)
        # Use functional index if needed: Index('ix_end_user_groups_skill_mode', text("(skill_global_governance->>'execution_mode')"))

        {
            'comment': 'User groups with hierarchical structure and flexible types'
        }
    )

    # =========================================================================
    # HIERARCHY NAVIGATION METHODS (ENHANCED)
    # =========================================================================

    def get_ancestors(self, include_self: bool = False) -> List['ResourceGroup']:
        """
        Get all ancestor groups from root to this group

        Returns list ordered from root to immediate parent
        [root, ..., grandparent, parent]

        Args:
            include_self: If True, append self to the end of the list

        Returns:
            List of ancestor groups
        """
        ancestors = []
        current = self.parent_group

        while current:
            ancestors.insert(0, current)  # Insert at beginning to maintain order
            current = current.parent_group

        if include_self:
            ancestors.append(self)

        return ancestors

    def get_descendants(self, include_self: bool = False) -> List['ResourceGroup']:
        """
        Get all descendant groups (children, grandchildren, etc.)

        Args:
            include_self: If True, include this group in the list

        Returns:
            List of all descendant groups
        """
        descendants = [self] if include_self else []

        for child in list(self.children):
            descendants.append(child)
            descendants.extend(child.get_descendants(include_self=False))

        return descendants

    def get_all_descendants(self) -> List['ResourceGroup']:
        """Get all descendant groups (alias for backwards compatibility)"""
        return self.get_descendants(include_self=False)

    def get_siblings(self, include_self: bool = False, db=None) -> List['ResourceGroup']:
        """
        Get sibling groups (groups with same parent)

        Args:
            include_self: If True, include this group in the list
            db: Database session (required for root group queries)

        Returns:
            List of sibling groups
        """
        if not self.parent_group:
            # Root group - get all other root groups
            if db is None:
                raise ValueError("Database session required to get siblings of root groups")

            from sqlalchemy import and_
            query = db.query(ResourceGroup).filter(
                and_(
                    ResourceGroup.tenant_id == self.tenant_id,
                    ResourceGroup.parent_group_id.is_(None)
                )
            )

            if not include_self:
                query = query.filter(ResourceGroup.id != self.id)

            siblings = query.all()
        else:
            # Get all children of parent
            siblings = [
                child for child in list(self.parent_group.children)
                if include_self or child.id != self.id
            ]

        return siblings

    def is_ancestor_of(self, group: 'ResourceGroup') -> bool:
        """Check if this group is an ancestor of another group"""
        return self in group.get_ancestors()

    def is_descendant_of(self, group: 'ResourceGroup') -> bool:
        """Check if this group is a descendant of another group"""
        return group in self.get_ancestors()

    # =========================================================================
    # RESOURCE INHERITANCE METHODS
    # =========================================================================

    def get_effective_llm_models(self) -> List:
        """
        Get all LLM models available to this group (including inherited)

        Returns models from:
        1. This group's directly assigned models
        2. All ancestor groups' models (if inheritance enabled)

        Returns:
            List of LLModel objects available to this group
        """
        models = list(self.llm_models)  # Direct assignments

        # Add inherited models from parent groups if inheritance enabled
        if self.inherit_resources:
            for ancestor in self.get_ancestors():
                models.extend(list(ancestor.llm_models))

        # Remove duplicates
        seen = set()
        unique_models = []
        for model in models:
            if model.id not in seen:
                seen.add(model.id)
                unique_models.append(model)

        return unique_models

    def get_effective_server_configs(self) -> List:
        """
        Get all MCP server configs available to this group (including inherited)

        Returns configs from:
        1. This group's directly assigned configs
        2. All ancestor groups' configs (if inheritance enabled)
        3. Global configs (group_id=NULL)

        Returns:
            List of ServerConfig objects available to this group
        """
        configs = list(self.server_configs)  # Direct assignments

        # Add inherited server configs from parent groups if inheritance enabled
        if self.inherit_resources:
            for ancestor in self.get_ancestors():
                configs.extend(list(ancestor.server_configs))

        # Remove duplicates
        seen = set()
        unique_configs = []
        for config in configs:
            if config.id not in seen:
                seen.add(config.id)
                unique_configs.append(config)

        return unique_configs

    def get_effective_kb_collections(self) -> List:
        """
        Get all KB collections available to this group (including inherited).

        Returns collections from:
        1. This group's directly assigned collections
        2. All ancestor groups' collections (if inheritance enabled)
        """
        collections = list(self.kb_collections)  # Direct assignments

        if self.inherit_resources:
            for ancestor in self.get_ancestors():
                collections.extend(list(ancestor.kb_collections))

        seen = set()
        unique = []
        for col in collections:
            if col.id not in seen:
                seen.add(col.id)
                unique.append(col)

        return unique

    def get_effective_kb_connectors(self) -> List:
        """
        Get all KB connectors available to this group (direct + inherited).

        Mirrors get_effective_kb_collections — connector→RG binding
        uses the same additive-inheritance rule. The portal's KB
        retrieval filter already trusts whatever the search-time ACL
        derives from each indexed doc's principals, so this method is
        purely for admin-UI display ("which connectors does this
        group inherit?").
        """
        # kb_connectors is lazy="dynamic" → call .all() to materialise.
        connectors = list(self.kb_connectors)

        if self.inherit_resources:
            for ancestor in self.get_ancestors():
                connectors.extend(list(ancestor.kb_connectors))

        seen = set()
        unique = []
        for c in connectors:
            if c.id not in seen:
                seen.add(c.id)
                unique.append(c)

        return unique

    def get_effective_agent_cards(self) -> List:
        """
        Get all A2A agent cards available to this group (direct + inherited).

        Returns cards from:
        1. This group's directly assigned cards
        2. All ancestor groups' cards (if inheritance enabled)

        Duplicates (same card bound at multiple ancestor levels) are collapsed
        to the first occurrence — consistent with how every other resource
        type is merged.
        """
        cards = list(self.agent_cards)

        if self.inherit_resources:
            for ancestor in self.get_ancestors():
                cards.extend(list(ancestor.agent_cards))

        seen = set()
        unique = []
        for card in cards:
            if card.id not in seen:
                seen.add(card.id)
                unique.append(card)

        return unique

    # =========================================================================
    # LIMIT METHODS (restrictive inheritance — always enabled)
    # =========================================================================

    def get_effective_llm_limits(self) -> Dict[str, Any]:
        """
        Get effective LLM global limits with restrictive inheritance.

        Walks from root to this group, taking the more restrictive (lower)
        value at each level. A child can tighten but never relax a parent's limit.
        Limit inheritance is always enabled (governance flows down).

        Returns:
            Effective LLM global limits dictionary
        """
        return self._merge_restrictive_limits('llm_global_limits')

    def get_effective_mcp_limits(self) -> Dict[str, Any]:
        """
        Get effective MCP global limits with restrictive inheritance.

        Walks from root to this group, taking the more restrictive (lower)
        value at each level. A child can tighten but never relax a parent's limit.
        Limit inheritance is always enabled (governance flows down).

        Returns:
            Effective MCP global limits dictionary
        """
        return self._merge_restrictive_limits('mcp_global_limits')

    def get_effective_skill_limits(self) -> Dict[str, Any]:
        """
        Get effective skill global limits with restrictive inheritance.

        Walks from root to this group, taking the more restrictive (lower)
        value at each level. A child can tighten but never relax a parent's limit.
        Limit inheritance is always enabled (governance flows down).

        Returns:
            Effective skill global limits dictionary
        """
        return self._merge_restrictive_limits('skill_global_limits')

    def get_effective_a2a_limits(self) -> Dict[str, Any]:
        """
        Get effective A2A global limits with restrictive inheritance.

        Walks from root to this group, taking the more restrictive (lower)
        value at each level. A child group can tighten but never relax an
        ancestor's limit.
        """
        return self._merge_restrictive_limits('a2a_global_limits')

    # =========================================================================
    # GOVERNANCE METHODS (restrictive inheritance — always enabled)
    # =========================================================================

    def get_effective_llm_governance(self) -> Dict[str, Any]:
        """
        Get effective LLM global governance with restrictive inheritance.

        Walks from root to this group, applying governance-specific restrictive
        merge at each level. A child can tighten but never relax a parent's governance.
        """
        return self._merge_restrictive_governance('llm_global_governance')

    def get_effective_mcp_governance(self) -> Dict[str, Any]:
        """
        Get effective MCP global governance with restrictive inheritance.

        Walks from root to this group, merging MCP governance settings.
        For access fields: max strictness wins (enabled < approval_required < disabled).
        For timeouts: min wins.
        """
        return self._merge_restrictive_governance_mcp('mcp_global_governance')

    def get_effective_skill_governance(self) -> Dict[str, Any]:
        """
        Get effective skill global governance with restrictive inheritance.

        Walks from root to this group. execution_mode uses max strictness,
        discovery_enabled uses AND (False wins), timeouts use min.
        """
        return self._merge_restrictive_governance_skill('skill_global_governance')

    def get_effective_media_limits(self) -> Dict[str, Any]:
        """
        Effective per-mode media limits with restrictive inheritance.

        The rule of every limit document (`restrictive_merge`, family
        media: a quota of 0 is a cap), mode by mode, as the proxy's
        `collect_effective_media_limits` does.
        """
        return self._merge_restrictive_limits('media_global_limits')

    def get_effective_media_governance(self) -> Dict[str, Any]:
        """
        Effective per-mode media governance with restrictive inheritance.

        Uses the per-field rules in `MEDIA_GOVERNANCE_FIELD_RULES`
        (whitelists intersect, ceilings take the min, `speed_min` takes the
        max, requirements OR), walking the chain as the proxy does.
        """
        return self._merge_governance_child_first('media_global_governance', _media_governance_restrictive_merge)

    def get_effective_a2a_governance(self) -> Dict[str, Any]:
        """
        Get effective A2A global governance with restrictive inheritance.

        Walks from root to this group. capability.default_access uses max
        strictness (enabled < approval_required < disabled); deny
        classifications are unioned (more restrictive). The allow-lists
        are not merged: see `a2a_remote_allow_lists`.
        """
        return self._merge_restrictive_governance_a2a('a2a_global_governance')

    def a2a_remote_allow_lists(self) -> List[Dict[str, Any]]:
        """The outbound allow-list of every level that states one, from the
        root to this group: `[{group_id, group_name, allowed_globs}]`. An
        outbound A2A address has to match the list of each."""
        levels = []
        for group in self.get_ancestors(include_self=True):
            remote = (group.a2a_global_governance or {}).get('remote') or {}
            globs = remote.get('allowed_globs') if isinstance(remote, dict) else None
            if globs:
                levels.append({"group_id": group.id, "group_name": group.name, "allowed_globs": list(globs)})
        return levels

    def _merge_restrictive_governance_a2a(self, attr: str) -> Dict[str, Any]:
        """Walk from root to self, merging A2A governance with A2A-specific rules."""
        chain = self.get_ancestors(include_self=True)
        effective: Dict[str, Any] = {}
        for group in chain:
            gov = getattr(group, attr, None)
            if gov:
                if not effective:
                    import copy
                    effective = copy.deepcopy(gov)
                    effective.pop('remote', None)
                else:
                    _a2a_governance_restrictive_merge(effective, gov)
        return effective

    def _merge_restrictive_governance(self, attr: str) -> Dict[str, Any]:
        """Walk from root to self, merging governance with _governance_restrictive_merge."""
        chain = self.get_ancestors(include_self=True)
        effective: Dict[str, Any] = {}
        for group in chain:
            gov = getattr(group, attr, None)
            if gov:
                if not effective:
                    import copy
                    effective = copy.deepcopy(gov)
                else:
                    _governance_restrictive_merge(effective, gov)
        return effective

    def _merge_restrictive_governance_mcp(self, attr: str) -> Dict[str, Any]:
        """Walk from root to self, merging MCP governance with access strictness."""
        chain = self.get_ancestors(include_self=True)
        effective: Dict[str, Any] = {}
        for group in chain:
            gov = getattr(group, attr, None)
            if gov:
                if not effective:
                    import copy
                    effective = copy.deepcopy(gov)
                else:
                    _mcp_governance_restrictive_merge(effective, gov)
        return effective

    def _merge_restrictive_governance_skill(self, attr: str) -> Dict[str, Any]:
        """Skill governance of the chain, merged as the proxy walks it
        (`_skill_governance_restrictive_merge`)."""
        return self._merge_governance_child_first(attr, _skill_governance_restrictive_merge)

    def _merge_governance_child_first(self, attr: str, merge: Any) -> Dict[str, Any]:
        documents = [getattr(group, attr, None) for group in self.get_ancestors(include_self=True)]
        effective = _merge_governance_child_first(documents, merge)
        return {} if effective is None else effective

    def _merge_restrictive_limits(self, attr: str) -> Dict[str, Any]:
        """
        The limit documents `attr` of the chain, root to self, merged by
        the rule of their family (`merge_limit_documents`): a group can
        tighten an ancestor's limit, never remove it.
        """
        chain = self.get_ancestors(include_self=True)  # root → ... → self
        effective = merge_limit_documents(attr, [getattr(group, attr, None) for group in chain])
        return {} if effective is None else effective

    def get_effective_mcp_capabilities(self, db) -> List[Dict[str, Any]]:
        """
        Get effective MCP capability filters with restrictive inheritance.

        Always inherited (governance). Uses three-state access model:
        enabled → approval_required → disabled (strictness order).
        Restrictive merge: max strictness wins across ancestor chain.

        Returns:
            List of capability filter dicts with effective access state and timeout
        """
        chain = self.get_ancestors(include_self=True)  # root → ... → self

        # Build a map: (server_config_id, capability_type, capability_name) → (access, timeout)
        effective: Dict[tuple, tuple] = {}

        for group in chain:
            caps = db.execute(
                resource_group_mcp_capabilities.select().where(
                    resource_group_mcp_capabilities.c.resource_group_id == group.id,
                )
            ).fetchall()
            for cap in caps:
                key = (cap.server_config_id, cap.capability_type, cap.capability_name)
                access = cap.access
                timeout = cap.approval_timeout_seconds
                if key in effective:
                    existing_access, existing_timeout = effective[key]
                    # Restrictive: max strictness wins
                    if _MCP_ACCESS_STRICTNESS.rank(access) > _MCP_ACCESS_STRICTNESS.rank(existing_access):
                        effective[key] = (access, timeout or existing_timeout)
                    elif access == existing_access and access == 'approval_required':
                        # Same level: use the shorter timeout (more restrictive)
                        if timeout and existing_timeout:
                            effective[key] = (access, min(timeout, existing_timeout))
                        elif timeout:
                            effective[key] = (access, timeout)
                else:
                    effective[key] = (access, timeout)

        result = []
        for (server_config_id, cap_type, cap_name), (access, timeout) in effective.items():
            result.append({
                'server_config_id': server_config_id,
                'capability_type': cap_type,
                'capability_name': cap_name,
                'access': access,
                'approval_timeout_seconds': timeout,
            })
        return result

    def is_skill_execution_allowed(self) -> bool:
        """Check if skill execution is allowed in this group"""
        gov = self.skill_global_governance
        if not gov or 'execution_mode' not in gov:
            return True  # Default: unrestricted
        return gov['execution_mode'] != 'disabled'

    def requires_skill_approval(self) -> bool:
        """Check if all skill executions require approval in this group"""
        gov = self.skill_global_governance
        if not gov:
            return False
        return gov.get('execution_mode') == 'approval_required'

    @property
    def skill_execution_mode_value(self) -> str:
        """Get skill execution mode from governance JSONB (backward compat helper)."""
        gov = self.skill_global_governance
        if gov and 'execution_mode' in gov:
            return gov['execution_mode']
        return 'unrestricted'

    @property
    def skill_discovery_enabled_value(self) -> bool:
        """Get skill discovery enabled from governance JSONB (backward compat helper)."""
        gov = self.skill_global_governance
        if gov and 'discovery_enabled' in gov:
            return gov['discovery_enabled']
        return True

    def get_effective_agent_skills(self) -> List:
        """
        Get all Agent Skills available to this group (including inherited)

        Returns skills from:
        1. This group's directly assigned skills
        2. All ancestor groups' skills (if inheritance enabled)

        Returns:
            List of AgentSkill objects available to this group
        """
        skills = list(self.agent_skills)  # Direct assignments

        # Add inherited skills from parent groups if inheritance enabled
        if self.inherit_resources:
            for ancestor in self.get_ancestors():
                if hasattr(ancestor, 'agent_skills'):
                    skills.extend(list(ancestor.agent_skills))

        # Remove duplicates
        seen = set()
        unique_skills = []
        for skill in skills:
            if skill.id not in seen:
                seen.add(skill.id)
                unique_skills.append(skill)

        return unique_skills


    def get_effective_guardrails(self) -> List:
        """
        Get all guardrail configs effective for this group.

        Guardrail inheritance is ALWAYS enabled (governance flows down).
        A child gets its parent's guardrails plus its own — additive,
        cannot remove a parent's guardrail.

        Returns:
            List of GuardrailConfig objects effective for this group
        """
        configs = list(self.guardrail_configs)  # Direct assignments

        # Always inherit guardrails from ancestors (governance inheritance)
        for ancestor in self.get_ancestors():
            if hasattr(ancestor, 'guardrail_configs'):
                configs.extend(list(ancestor.guardrail_configs))

        # Deduplicate
        seen = set()
        unique = []
        for cfg in configs:
            if cfg.id not in seen:
                seen.add(cfg.id)
                unique.append(cfg)

        return unique


    # =========================================================================
    # STATISTICS METHODS
    # =========================================================================

    def get_member_count(self, include_descendants: bool = False) -> int:
        """
        Get number of members (end users) in this group

        Args:
            include_descendants: If True, count members in descendant groups too

        Returns:
            Member count
        """
        count = self.members.count()

        if include_descendants:
            for descendant in self.get_descendants():
                count += descendant.members.count()

        return count

    def get_server_config_count(self, include_inherited: bool = False) -> int:
        """
        Get number of server configs for this group

        Args:
            include_inherited: If True, count inherited configs too
            db: Database session (required if not counting inherited)

        Returns:
            Server config count
        """
        if include_inherited:
            return len(self.get_effective_server_configs())
        else:
            return len([c for c in self.server_configs if c.enabled])

    def get_llm_model_count(self, include_inherited: bool = False, db=None) -> int:
        """
        Get number of LLM models for this group

        Args:
            include_inherited: If True, count inherited models too
            db: Database session (required if not counting inherited)

        Returns:
            LLM model count
        """
        if include_inherited:
            return len(self.get_effective_llm_models())
        else:
            return len([m for m in self.llm_models if getattr(m, 'enabled', True)])

    def get_access_key_count(self, db=None) -> int:
        """
        Get number of LLM access keys scoped to this group

        Args:
            db: Database session
        """
        return len([k for k in self.api_keys if k.enabled and not k.revoked])


    # =========================================================================
    # VALIDATION METHODS
    # =========================================================================

    def validate_llm_limits(self) -> tuple[bool, Optional[str]]:
        """
        Validate LLM global limits structure and values

        Returns:
            (is_valid, error_message)
        """
        if not self.llm_global_limits:
            return True, None

        try:
            limits = self.llm_global_limits if isinstance(self.llm_global_limits, dict) else {}

            # Validate budget section
            if 'budget' in limits:
                budget = limits['budget']
                if 'daily_limit_usd' in budget and budget['daily_limit_usd'] < 0:
                    return False, "Daily budget limit must be non-negative"
                if 'monthly_limit_usd' in budget and budget['monthly_limit_usd'] < 0:
                    return False, "Monthly budget limit must be non-negative"
                if 'daily_warning_percent' in budget:
                    if not 0 <= budget['daily_warning_percent'] <= 100:
                        return False, "Daily warning percent must be 0-100"

            # Validate tokens section: the totals, and the input and the
            # output side of each (the gateway enforces all six).
            if 'tokens' in limits:
                tokens = limits['tokens']
                if not isinstance(tokens, dict):
                    return False, "tokens must be a JSON object"
                for key in LLM_TOKEN_QUOTA_KEYS:
                    value = tokens.get(key)
                    if value is None:
                        continue
                    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                        return False, f"tokens.{key} must be a whole number of tokens, 0 or more"

            # Validate requests section
            if 'requests' in limits:
                requests = limits['requests']
                if 'daily_limit' in requests and requests['daily_limit'] < 0:
                    return False, "Daily request limit must be non-negative"
                if 'monthly_limit' in requests and requests['monthly_limit'] < 0:
                    return False, "Monthly request limit must be non-negative"

            unavailable = release_scope.llm_limits_problem(limits)
            if unavailable:
                return False, unavailable
            return True, None

        except Exception as e:
            return False, f"Invalid LLM limits structure: {str(e)}"

    MCP_LIMITS_KEYS = MCP_LIMITS_KEYS

    @staticmethod
    def _validate_mcp_frequency_block(freq: Any, ctx: str) -> Optional[str]:
        """
        Validate one MCP frequency block (token bucket + quota + cooldown).

        `ctx` is a human-readable label for error messages, e.g.
        "frequency" or "per_tool 'generate_report'". Returns an error
        string, or None when the block is valid.
        """
        if not isinstance(freq, dict):
            return f"MCP {ctx} block must be an object"
        unknown = _unknown_mcp_limits_key(freq, MCP_FREQUENCY_KEYS, ctx)
        if unknown:
            return unknown

        # All count/duration fields must be positive integers when present.
        # bool is a subclass of int in Python — reject it explicitly.
        for field in ('max_calls_per_minute', 'max_calls_per_hour',
                      'max_calls_per_day', 'burst_limit', 'cooldown_seconds'):
            val = freq.get(field)
            if val is None:
                continue
            if not isinstance(val, int) or isinstance(val, bool) or val < 1:
                return f"MCP {ctx}.{field} must be a positive integer (>= 1)"

        pct = freq.get('daily_warning_percent')
        if pct is not None:
            if not isinstance(pct, int) or isinstance(pct, bool) or not 0 <= pct <= 100:
                return f"MCP {ctx}.daily_warning_percent must be an integer 0-100"

        # Cross-field sanity: a per-minute rate above the per-hour or
        # per-day quota is always a misconfiguration.
        per_min = freq.get('max_calls_per_minute')
        per_hour = freq.get('max_calls_per_hour')
        per_day = freq.get('max_calls_per_day')
        if isinstance(per_min, int) and isinstance(per_hour, int) and per_min > per_hour:
            return (f"MCP {ctx}: max_calls_per_minute ({per_min}) cannot exceed "
                    f"max_calls_per_hour ({per_hour})")
        if isinstance(per_hour, int) and isinstance(per_day, int) and per_hour > per_day:
            return (f"MCP {ctx}: max_calls_per_hour ({per_hour}) cannot exceed "
                    f"max_calls_per_day ({per_day})")
        return None

    @staticmethod
    def validate_mcp_limits_structure(limits: Optional[dict]) -> tuple[bool, Optional[str]]:
        """
        Validate an MCP limits structure — group-global (`mcp_global_limits`)
        or per-server (`resource_group_server_configs.limits`).

        Covers execution / data / frequency / per_tool sections and rejects
        the removed 'cost' section. Returns (is_valid, error_message).
        """
        if not limits:
            return True, None

        try:
            limits = limits if isinstance(limits, dict) else {}

            # Reject removed sections
            if 'cost' in limits:
                return False, "MCP 'cost' section is no longer supported (MCP calls have no inherent cost)"

            unavailable = release_scope.mcp_limits_problem(limits)
            if unavailable:
                return False, unavailable

            # A key the gateway does not read would be stored and ignored.
            unknown = _unknown_mcp_limits_key(limits, tuple(MCP_LIMITS_KEYS), "")
            for section in ('execution', 'data'):
                if unknown:
                    break
                if isinstance(limits.get(section), dict):
                    unknown = _unknown_mcp_limits_key(limits[section], MCP_LIMITS_KEYS[section], section)
            if unknown:
                return False, unknown

            # The execution limits the gateway reads (`execution`, `data`):
            # a value it cannot read would refuse every call.
            unreadable = mcp_exec_limits.problem(limits)
            if unreadable:
                return False, unreadable

            # Validate server/group-wide frequency section
            freq = limits.get('frequency')
            if freq is not None:
                err = ResourceGroup._validate_mcp_frequency_block(freq, 'frequency')
                if err:
                    return False, err

            # Validate per-tool overrides
            per_tool = limits.get('per_tool')
            if per_tool is not None:
                if not isinstance(per_tool, dict):
                    return False, "MCP 'per_tool' section must be an object keyed by tool name"
                for tool_name, tool_freq in per_tool.items():
                    if not isinstance(tool_name, str) or not tool_name.strip():
                        return False, "MCP 'per_tool' keys must be non-empty tool names"
                    err = ResourceGroup._validate_mcp_frequency_block(
                        tool_freq, f"per_tool '{tool_name}'")
                    if err:
                        return False, err

            return True, None

        except Exception as e:
            return False, f"Invalid MCP limits structure: {str(e)}"

    def validate_mcp_limits(self) -> tuple[bool, Optional[str]]:
        """
        Validate this group's `mcp_global_limits`.

        Thin wrapper over `validate_mcp_limits_structure` — see that method
        for the full coverage. Returns (is_valid, error_message).
        """
        return ResourceGroup.validate_mcp_limits_structure(self.mcp_global_limits)

    def validate_skill_limits(self) -> tuple[bool, Optional[str]]:
        """
        Validate skill global limits structure and values.
        Rejects 'budget' and 'tokens' sections (cost/tokens tracked at LLM level).

        Returns:
            (is_valid, error_message)
        """
        if not self.skill_global_limits:
            return True, None

        try:
            limits = self.skill_global_limits if isinstance(self.skill_global_limits, dict) else {}

            # Reject removed sections
            if 'budget' in limits:
                return False, "Skill 'budget' section is no longer supported (cost tracked at LLM level)"
            if 'tokens' in limits:
                return False, "Skill 'tokens' section is no longer supported (tokens tracked at LLM level)"

            # Validate execution section
            if 'execution' in limits:
                exec_limits = limits['execution']
                if 'daily_limit' in exec_limits and exec_limits['daily_limit'] < 0:
                    return False, "Daily execution limit must be non-negative"
                if 'monthly_limit' in exec_limits and exec_limits['monthly_limit'] < 0:
                    return False, "Monthly execution limit must be non-negative"

            return True, None

        except Exception as e:
            return False, f"Invalid skill limits structure: {str(e)}"

    # =========================================================================
    # VALIDATION
    # =========================================================================

    @validates("lifecycle_stage")
    def _validate_lifecycle_stage(self, _key, value):
        if value is not None and value not in LIFECYCLE_STAGES:
            raise ValueError(
                f"lifecycle_stage must be one of {sorted(LIFECYCLE_STAGES)}, got {value!r}"
            )
        return value

    @validates("autonomy_level")
    def _validate_autonomy_level(self, _key, value):
        # App-level rather than a CHECK: adding a level
        # later should not need a migration.
        if value is not None and value not in AUTONOMY_LEVELS:
            raise ValueError(
                f"autonomy_level must be one of {sorted(AUTONOMY_LEVELS)}, got {value!r}"
            )
        return value

    @validates("eu_ai_act_risk_tier")
    def _validate_eu_ai_act_risk_tier(self, _key, value):
        if value is not None and value not in RISK_TIERS:
            raise ValueError(
                f"eu_ai_act_risk_tier must be one of {sorted(RISK_TIERS)}, got {value!r}"
            )
        return value

    @validates("eu_ai_act_role")
    def _validate_eu_ai_act_role(self, _key, value):
        if value is not None and value not in EU_AI_ACT_ROLES:
            raise ValueError(
                f"eu_ai_act_role must be one of {sorted(EU_AI_ACT_ROLES)}, got {value!r}"
            )
        return value

    @validates("system_kind")
    def _validate_system_kind(self, _key, value):
        """Validate the AI System kind.

        Membership only — the *coupling* rule ("set iff group_type is
        ai_system") is deliberately not enforced here. SQLAlchemy fires
        column validators in attribute-assignment order, so a validator
        that read `self.group_type` would reject a perfectly valid
        ``ResourceGroup(system_kind=..., group_type=...)`` purely because
        the kwargs happened to be ordered that way. The coupling is
        enforced where it can see a complete object: `validate_system_kind`
        below (called by the API layer) and, for anything constructed
        in-process, `_enforce_system_kind_coupling` on the flush hook.
        """
        if value is not None and value not in SYSTEM_KINDS:
            raise ValueError(
                f"system_kind must be one of {sorted(SYSTEM_KINDS)}, got {value!r}"
            )
        return value

    def validate_system_kind(self) -> Tuple[bool, Optional[str]]:
        """Check the group_type/system_kind coupling on a fully-built object.

        Returns ``(is_valid, error_message)``, matching the other
        ``validate_*`` helpers on this model.
        """
        is_ai_system = self.group_type == GroupType.AI_SYSTEM
        if is_ai_system and not self.system_kind:
            return False, (
                "system_kind is required when group_type is 'ai_system' "
                f"(one of {sorted(SYSTEM_KINDS)})"
            )
        if not is_ai_system and self.system_kind:
            return False, (
                f"system_kind is only valid when group_type is 'ai_system'; "
                f"this group is {self.group_type.value if self.group_type else None!r}"
            )
        return True, None

    # =========================================================================
    # SERIALIZATION
    # =========================================================================

    def __repr__(self) -> str:
        parent_info = f", parent='{self.parent_group.name}'" if self.parent_group else ""
        type_info = f", type={self.group_type.value}" if self.group_type else ""
        return f"<ResourceGroup(id='{self.id}', name='{self.name}', level={self.hierarchy_level}{type_info}{parent_info})>"

    def to_dict(
            self,
            include_stats: bool = False,
            include_resources: bool = False,
            include_hierarchy: bool = False,
            include_limits: bool = False
    ) -> Dict[str, Any]:
        """
        Convert group to dictionary representation
        """
        data = {
            'id': self.id,
            'name': self.name,
            'display_name': self.display_name,
            'description': self.description,
            'group_type': self.group_type.value if self.group_type else None,
            # AI System kind — null for non-ai_system groups.
            'system_kind': self.system_kind,
            'run_idle_timeout_seconds': self.run_idle_timeout_seconds,
            # The live (unapproved) outcome catalogue.
            'declared_outcomes': self.declared_outcomes,
            'approval_window_seconds': self.approval_window_seconds,
            # Governance / ownership (AI-System attributes on the RG).
            'owner': self.owner,
            'intended_use': self.intended_use,
            'intended_client_labels': self.intended_client_labels,
            'intended_client_systems': self.intended_client_systems,
            'enforce_accepted_callers': bool(self.enforce_accepted_callers),
            'lifecycle_stage': self.lifecycle_stage,
            'autonomy_level': self.autonomy_level,
            'version': self.version,
            'replay_suite_id': self.replay_suite_id,
            'rollup_exclusion': self.rollup_exclusion,
            'parent_group_id': self.parent_group_id,
            'hierarchy_level': self.hierarchy_level,
            'hierarchy_path': self.hierarchy_path,
            'inherit_resources': self.inherit_resources,
            'icon': self.icon,
            'color': self.color,
            'is_active': self.is_active,
            'is_system': self.is_system,
            'created_by': self.created_by,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'updated_by': self.updated_by,
            # Per-RG compliance posture. Always
            # surfaced (no include_limits gate) so the admin UI can
            # render the chips without an extra round trip.
            'eu_ai_act_risk_tier': self.eu_ai_act_risk_tier,
            'eu_ai_act_role': self.eu_ai_act_role,
            'phi_resource': self.phi_resource,
            'sensitive_data': bool(self.sensitive_data),
            'source': self.source,
            'source_external_id': self.source_external_id,
        }

        # Include hierarchy information
        if include_hierarchy:
            data['parent_group_name'] = self.parent_group.name if self.parent_group else None
            data[
                'parent_group_type'] = self.parent_group.group_type.value if self.parent_group and self.parent_group.group_type else None
            data['children_count'] = len(list(self.children))
            data['children'] = [
                {
                    'id': child.id,
                    'name': child.name,
                    'display_name': child.display_name,
                    'group_type': child.group_type.value if child.group_type else None,
                    'hierarchy_level': child.hierarchy_level,
                    'is_active': child.is_active
                }
                for child in list(self.children)
            ]
            data['ancestors'] = [
                {
                    'id': ancestor.id,
                    'name': ancestor.name,
                    'group_type': ancestor.group_type.value if ancestor.group_type else None,
                    'hierarchy_level': ancestor.hierarchy_level
                }
                for ancestor in self.get_ancestors()
            ]

        # Include statistics
        if include_stats:
            data['stats'] = {
                'member_count': self.get_member_count(),
                'member_count_with_descendants': self.get_member_count(include_descendants=True),
                'server_config_count': len(list(self.server_configs)),
                'server_config_count_with_inherited': len(self.get_effective_server_configs()),
                'llm_model_count': len(list(self.llm_models)),
                'llm_model_count_with_inherited': len(self.get_effective_llm_models()),
                'api_key_count': len([k for k in self.api_keys if k.enabled and not k.revoked]),
                'direct_children_count': len(list(self.children)),
                'total_descendants_count': len(self.get_descendants())
            }
            data['stats']['agent_skill_count'] = len(list(self.agent_skills))
            data['stats']['agent_skill_count_with_inherited'] = len(self.get_effective_agent_skills())
            # skill_binding_count removed — bindings merged into resource_group_agent_skills

        # Include resources
        if include_resources:
            data['server_configs'] = [
                {
                    'id': config.id,
                    'name': config.name,
                    'display_name': config.display_name,
                    'is_inherited': config.group_id != self.id
                }
                for config in self.get_effective_server_configs()
            ]

            # Build set of direct model IDs first
            direct_model_ids = {m.id for m in self.llm_models}

            data['llm_models'] = [
                {
                    'id': model.id,
                    'name': model.name,
                    'is_inherited': model.id not in direct_model_ids
                }
                for model in self.get_effective_llm_models()
            ]

        # Skill governance settings (always included — these are group-level toggles)
        data['skill_discovery_enabled'] = self.skill_discovery_enabled_value
        data['skill_execution_mode'] = self.skill_execution_mode_value

        # Include limits and governance (no inheritance in the raw values)
        if include_limits:
            data['llm_global_limits'] = self.llm_global_limits
            data['mcp_global_limits'] = self.mcp_global_limits
            data['skill_global_limits'] = self.skill_global_limits
            # a2a + media were stored & accepted by PATCH but never emitted
            # here, so GET (and the PATCH response) returned null — operators'
            # edits "vanished" on form reload. Emit them so the round-trip is
            # honest (same field-drop class fixed for is_system/updated_by in
            # the response model).
            data['a2a_global_limits'] = self.a2a_global_limits
            data['media_global_limits'] = self.media_global_limits
            data['llm_global_governance'] = self.llm_global_governance
            data['mcp_global_governance'] = self.mcp_global_governance
            data['skill_global_governance'] = self.skill_global_governance
            data['a2a_global_governance'] = self.a2a_global_governance
            data['media_global_governance'] = self.media_global_governance

        return data


# ============================================================================
# AI System invariant enforcement
# ============================================================================


def _enforce_system_kind_coupling(_mapper, _connection, target: "ResourceGroup") -> None:
    """Keep `system_kind` and `group_type` consistent at flush time.

    The API layer rejects bad combinations with a 422 long before this runs
    (see `validate_system_kind`). This hook is the backstop for every other
    writer — seeds, provisioning scripts, tests, future services — so the
    invariant is a property of the *table*, not of one code path:

      * AI System without a kind  -> defaulted to DEFAULT_SYSTEM_KIND.
        Silent defaulting is right here: an AI System without a kind is
        an agent, so the alternative is breaking existing seeds to state
        something the platform already assumes.

      * Non-AI-System with a kind -> raises. This one is *not* silently
        cleared: it means a caller believes a group is an AI System when it
        is not, and quietly discarding their intent would surface later as
        an AI System mysteriously missing from the fleet board.
    """
    if target.group_type == GroupType.AI_SYSTEM:
        if not target.system_kind:
            target.system_kind = DEFAULT_SYSTEM_KIND
    elif target.system_kind:
        raise ValueError(
            f"system_kind={target.system_kind!r} is only valid when "
            f"group_type is 'ai_system'; group {target.id!r} is "
            f"{target.group_type.value if target.group_type else None!r}"
        )


event.listen(ResourceGroup, "before_insert", _enforce_system_kind_coupling)
event.listen(ResourceGroup, "before_update", _enforce_system_kind_coupling)