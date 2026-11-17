# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Agent Skills Constants and Enums
Defines enumerations for Agent Skills orchestration lifecycle,
execution states, governance controls, and plugin hook types.
"""

from enum import Enum


class SkillStatus(str, Enum):
    """
    Skill publishing lifecycle

    Flow: DRAFT → VALIDATED → PUBLISHED → DEPRECATED
    - DRAFT: Initial state, editable, not discoverable
    - VALIDATED: Passed all validation checks, awaiting approval
    - PUBLISHED: Live, discoverable, immutable (new version for changes)
    - DEPRECATED: Still functional but hidden from catalog, scheduled for removal
    - ARCHIVED: Fully disabled, retained for audit history
    """
    DRAFT = "draft"
    VALIDATED = "validated"
    PUBLISHED = "published"
    DEPRECATED = "deprecated"
    ARCHIVED = "archived"


class ExecutionStatus(str, Enum):
    """
    Skill execution lifecycle

    Flow: PENDING → RUNNING → COMPLETED | FAILED | CANCELLED | TIMED_OUT
    - PENDING: Queued or awaiting approval
    - RUNNING: Currently executing
    - COMPLETED: Successfully finished
    - FAILED: Terminated with error
    - CANCELLED: Cancelled by user or system
    - TIMED_OUT: Exceeded execution time limit
    """
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class ApprovalStatus(str, Enum):
    """
    Human-in-the-loop approval states

    - NOT_REQUIRED: Skill does not require approval
    - PENDING: Awaiting human review
    - APPROVED: Approved for execution
    - REJECTED: Denied by reviewer
    - EXPIRED: Approval window elapsed without action
    """
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class SkillResourceType(str, Enum):
    """
    Types of files in the skill's references/ directory

    - REFERENCE: General reference documentation (REFERENCE.md)
    - FORM: Form definitions and templates (FORMS.md)
    - SCHEMA: Data schemas (JSON Schema, OpenAPI fragments)
    - TEMPLATE: Output templates (Jinja2, Mustache, etc.)
    - EXAMPLE: Example inputs/outputs for the skill
    - ASSET: Binary or other non-text assets
    """
    REFERENCE = "reference"
    FORM = "form"
    SCHEMA = "schema"
    TEMPLATE = "template"
    EXAMPLE = "example"
    ASSET = "asset"


class ScriptLanguage(str, Enum):
    """Supported languages for skill scripts"""
    PYTHON = "python"
    JAVASCRIPT = "javascript"
    TYPESCRIPT = "typescript"
    BASH = "bash"


class ScriptExecutionMode(str, Enum):
    """
    Security mode for script execution

    - SANDBOX: Restricted environment, no network, limited filesystem (default)
    - TRUSTED: Full access, requires explicit admin approval
    - DISABLED: Script stored but never executed
    """
    SANDBOX = "sandbox"
    TRUSTED = "trusted"
    DISABLED = "disabled"


class AssetType(str, Enum):
    """
    Types of files in the skill's assets/ directory

    - TEMPLATE: Document or configuration templates
    - IMAGE: Diagrams, screenshots, example images
    - DATA: Lookup tables, schemas, CSV data files
    """
    TEMPLATE = "template"
    IMAGE = "image"
    DATA = "data"



class SkillExecutionMode(str, Enum):
    """
    Group-level master switch for skill execution

    - UNRESTRICTED: Skills execute without additional approval
    - APPROVAL_REQUIRED: All skill executions require human approval
    - DISABLED: No skill execution allowed in this group
    """
    UNRESTRICTED = "unrestricted"
    APPROVAL_REQUIRED = "approval_required"
    DISABLED = "disabled"


class SkillDisclosureMode(str, Enum):
    """
    How a skill may be consumed over the Agent Skills MCP surface.

    - PROGRESSIVE: The only supported model path. The agent discovers the skill
      (L1 tools/list), loads SKILL.md (L2 skills__load), then reads resources /
      runs scripts / renders templates (L3) — every layer governed by Brutor,
      nothing executed outside the sandbox. A one-shot "execute the skill" is a
      *tool*, not a skill, so it is deliberately not offered here.
    - EXECUTE: Legacy one-shot execute-and-return (REST /skills/execute and the
      old skill__<name> MCP tool). Retained only for a deprecation/transition
      window; slated for removal.
    - BOTH: Transition mode — the skill is exposed for progressive disclosure
      AND the legacy execute path still answers, so existing callers keep
      working while they migrate.
    """
    PROGRESSIVE = "progressive"
    EXECUTE = "execute"
    BOTH = "both"


class DependencyType(str, Enum):
    """
    Relationship type between parent and dependency skills

    - REQUIRED: Dependency must be available and succeed
    - OPTIONAL: Execution continues if dependency fails
    - CONDITIONAL: Only triggered when condition evaluates true
    """
    REQUIRED = "required"
    OPTIONAL = "optional"
    CONDITIONAL = "conditional"


class SkillHookType(str, Enum):
    """
    Plugin hook types for Agent Skills governance

    Each hook fires at a specific point in the skill execution lifecycle.

    - SKILL_PRE_EXECUTION: Before skill starts, can modify context or block
    - SKILL_POST_EXECUTION: After skill completes, can transform output or trigger alerts
    - SKILL_VALIDATION: Validate skill inputs against custom rules
    - SKILL_AUTHORIZATION: Custom authorization logic beyond RBAC
    - SKILL_RESOURCE_RESOLUTION: Override or transform resource resolution at runtime
    """
    SKILL_PRE_EXECUTION = "skill_pre_execution"
    SKILL_POST_EXECUTION = "skill_post_execution"
    SKILL_VALIDATION = "skill_validation"
    SKILL_AUTHORIZATION = "skill_authorization"
    SKILL_RESOURCE_RESOLUTION = "skill_resource_resolution"


# --- size ---------------------------------------------------------------------------------------

#: The most bytes one skill may hold, uncompressed (the sum of its files):
#: 30 MB, Anthropic Agent Skills' limit. Defined here only; every check reads
#: it from this module, and the list of skills states it (`limits`). The Core
#: Gateway and the Skill Runner derive their request limit from it: a request
#: carries the skill's files base64-encoded in JSON, about 4/3 of this plus
#: the call's input and framing (about 41 MB).
SKILL_MAX_BYTES = 30_000_000

#: One zip may hold several skills (one per top-level folder with a
#: SKILL.md): at most this many ...
ARCHIVE_MAX_SKILLS = 50
#: ... and at most this many bytes uncompressed. Both are read from the
#: zip's directory before anything is decompressed.
ARCHIVE_MAX_BYTES = 300_000_000


def _megabytes(value: int) -> str:
    return f"{value / 1_000_000:g} MB"


def __getattr__(name: str):
    # The texts follow the numbers, even where a test sets them.
    if name == "SKILL_MAX_TEXT":
        return _megabytes(SKILL_MAX_BYTES)
    if name == "ARCHIVE_MAX_TEXT":
        return _megabytes(ARCHIVE_MAX_BYTES)
    raise AttributeError(name)
