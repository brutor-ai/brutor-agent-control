# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Run data scope and cross-run record reach.

* `AISystemDataScope` — per AI System: which tool argument names which kind of
  record, and whether a run's calls outside its declared scope are recorded
  (`audit`) or refused (`enforce`).
* `AISystemRunScope` — what a run declared (`X-Brutor-Run-Scope`). The first
  value of a key binds; written by the core.
* `AISystemRecordAccess` — one evaluated record reference per tool call, with
  the verdict. Written by the core; the raw value is never stored (keyed
  reference). Pruned after 35 days by the core.
* `AISystemReachRule` — a declared limit on distinct records per requester,
  key, agent or system in a window, swept by the core.

Wire contract with `gateway-core/src/services/data_scope.rs` and
`record_reach.rs`: the mode, source, verdict and group-by vocabularies below.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.database import Base

MODE_OFF = "off"
MODE_AUDIT = "audit"
MODE_ENFORCE = "enforce"
MODES = (MODE_OFF, MODE_AUDIT, MODE_ENFORCE)

ON_UNBOUND = ("deny", "allow")

SOURCE_DECLARED = "declared"
SOURCE_SUBJECT = "subject"
SOURCES = (SOURCE_DECLARED, SOURCE_SUBJECT)

VERDICT_IN_SCOPE = "in_scope"
VERDICTS = (
    VERDICT_IN_SCOPE,
    "unbound",
    "conflict",
    "out_of_scope",
    "missing_argument",
    "unscoped_tool",
)
VIOLATION_VERDICTS = ("out_of_scope", "missing_argument", "unscoped_tool", "conflict")

GROUP_BY_FIXED = ("subject", "api_key", "agent", "system")
GROUP_BY_SCOPE_PREFIX = "scope:"
REACH_SEVERITIES = ("low", "medium", "high", "critical")
REACH_MAX_WINDOW_HOURS = 720

_KEY = re.compile(r"^[a-z0-9_-]{1,64}$")
_PATH = re.compile(r"^[A-Za-z0-9_-]+(\[\])?(\.[A-Za-z0-9_-]+(\[\])?)*$")

# Rule names for calls that are not MCP tools (wire contract with the core,
# `services::data_scope::surface`). An MCP tool name cannot hold a ':', so a
# name with one must use a known prefix: `skills:report` would never match.
SURFACE_SKILL = "skill:"
SURFACE_A2A = "a2a:"
# A tool call a model asks for, which the agent may run itself.
SURFACE_MODEL = "model:"
SURFACES = (SURFACE_SKILL, SURFACE_A2A, SURFACE_MODEL)


def _check_target(name: str) -> None:
    if ":" not in name:
        return
    prefix = name.split(":", 1)[0] + ":"
    if prefix not in SURFACES or not name[len(prefix):]:
        raise ValueError(
            f"{name!r}: a name with ':' must be 'skill:<skill name>', 'a2a:<agent card name>' "
            "or 'model:<function name>'"
        )


def validate_config(body: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Validate a data-scope config. Returns (normalised, warnings); raises
    ValueError with an operator-facing sentence on an error."""
    mode = body.get("mode", MODE_AUDIT)
    if mode not in MODES:
        raise ValueError(f"mode must be one of {list(MODES)}")
    on_unbound = body.get("on_unbound", "deny")
    if on_unbound not in ON_UNBOUND:
        raise ValueError("on_unbound must be 'deny' or 'allow'")
    keys = body.get("keys") or []
    seen = {}
    for k in keys:
        key = (k or {}).get("key", "")
        src = (k or {}).get("source", SOURCE_DECLARED)
        if not _KEY.match(key):
            raise ValueError(f"key {key!r} must be lower-case letters, digits, '_' or '-'")
        if src not in SOURCES:
            raise ValueError(f"key {key!r}: source must be one of {list(SOURCES)}")
        if key in seen:
            raise ValueError(f"key {key!r} is declared twice")
        seen[key] = src
    if sum(1 for s in seen.values() if s == SOURCE_SUBJECT) > 1:
        raise ValueError("only one key can come from the subject")
    rules = body.get("rules") or []
    norm_rules = []
    for r in rules:
        tool, arg, key = (r or {}).get("tool", ""), (r or {}).get("argument", ""), (r or {}).get("key", "")
        if not tool:
            raise ValueError("every rule needs a tool")
        _check_target(tool)
        if not _PATH.match(arg or ""):
            raise ValueError(f"rule for {tool}: argument {arg!r} must be a dotted path, e.g. 'customer_id' or 'accounts[].owner'")
        if key not in seen:
            raise ValueError(f"rule for {tool}: key {key!r} is not one of the declared keys")
        norm_rules.append({"tool": tool, "argument": arg, "key": key})
    unscoped = [t for t in (body.get("unscoped_tools") or []) if isinstance(t, str) and t]
    for t in unscoped:
        _check_target(t)
    warnings: List[str] = []
    ruled = {r["tool"] for r in norm_rules}
    both = sorted(ruled & set(unscoped))
    if both:
        raise ValueError(f"{', '.join(both)} cannot both name a record and be unscoped")
    if mode == MODE_ENFORCE and unscoped:
        warnings.append(
            "In enforce mode, a run that declared a scope cannot call "
            + ", ".join(unscoped)
            + ". If the agent needs a list or search tool inside such runs, split the tool or stay in audit."
        )
    if mode != MODE_OFF and not norm_rules and not unscoped:
        warnings.append("No rules: nothing will be checked.")
    if mode == MODE_ENFORCE and on_unbound == "deny":
        warnings.append(
            "Runs that declare no scope will be refused any call a rule covers. "
            "Make sure the agent sends X-Brutor-Run-Scope before switching this on."
        )
    return (
        {
            "mode": mode,
            "on_unbound": on_unbound,
            "keys": [{"key": k, "source": s} for k, s in seen.items()],
            "rules": norm_rules,
            "unscoped_tools": unscoped,
        },
        warnings,
    )


def validate_reach_rule(body: Dict[str, Any], scope_keys: List[str]) -> Dict[str, Any]:
    """Validate a reach rule against the system's declared keys."""
    scope_key = body.get("scope_key", "")
    if scope_key not in scope_keys:
        raise ValueError(f"scope_key {scope_key!r} is not a key of this system's data scope")
    group_by = body.get("group_by", "")
    if group_by not in GROUP_BY_FIXED:
        if not group_by.startswith(GROUP_BY_SCOPE_PREFIX) or not _KEY.match(
            group_by[len(GROUP_BY_SCOPE_PREFIX):]
        ):
            raise ValueError(
                "group_by must be subject, api_key, agent, system or scope:<key>"
            )
    window = body.get("window_hours", 24)
    if not isinstance(window, int) or not 1 <= window <= REACH_MAX_WINDOW_HOURS:
        raise ValueError(f"window_hours must be 1 to {REACH_MAX_WINDOW_HOURS}")
    limit = body.get("max_distinct")
    if not isinstance(limit, int) or limit < 1:
        raise ValueError("max_distinct must be a positive whole number")
    severity = body.get("severity", "high")
    if severity not in REACH_SEVERITIES:
        raise ValueError(f"severity must be one of {list(REACH_SEVERITIES)}")
    return {
        "scope_key": scope_key,
        "group_by": group_by,
        "window_hours": window,
        "max_distinct": limit,
        "severity": severity,
        "enabled": bool(body.get("enabled", True)),
    }


class AISystemDataScope(Base):
    __tablename__ = "ai_system_data_scopes"

    id = Column(String(64), primary_key=True)
    tenant_id = Column(String(255), nullable=False)
    ai_system_id = Column(String(255), nullable=False)
    mode = Column(String(8), nullable=False, default=MODE_AUDIT, server_default=MODE_AUDIT)
    on_unbound = Column(String(8), nullable=False, default="deny", server_default="deny")
    keys = Column(JSONB, nullable=False, default=list, server_default="[]")
    rules = Column(JSONB, nullable=False, default=list, server_default="[]")
    unscoped_tools = Column(JSONB, nullable=False, default=list, server_default="[]")
    version = Column(Integer, nullable=False, default=1, server_default="1")
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_by = Column(String(255), nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "ai_system_id", name="uq_ai_system_data_scopes_system"),
        CheckConstraint("mode IN ('off','audit','enforce')", name="ck_data_scope_mode"),
        CheckConstraint("on_unbound IN ('deny','allow')", name="ck_data_scope_on_unbound"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ai_system_id": self.ai_system_id,
            "mode": self.mode,
            "on_unbound": self.on_unbound,
            "keys": self.keys or [],
            "rules": self.rules or [],
            "unscoped_tools": self.unscoped_tools or [],
            "version": self.version,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "updated_by": self.updated_by,
        }


class AISystemRunScope(Base):
    __tablename__ = "ai_system_run_scopes"

    id = Column(String(64), primary_key=True)
    tenant_id = Column(String(255), nullable=False)
    root_task_id = Column(String(255), nullable=False)
    scope_key = Column(String(64), nullable=False)
    scope_values = Column(JSONB, nullable=False)
    source = Column(String(16), nullable=False, default=SOURCE_DECLARED, server_default=SOURCE_DECLARED)
    declared_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("tenant_id", "root_task_id", "scope_key", name="uq_ai_system_run_scopes_key"),
        Index("ix_ai_system_run_scopes_declared", "declared_at"),
    )


class AISystemRecordAccess(Base):
    __tablename__ = "ai_system_record_access"

    id = Column(String(64), primary_key=True)
    tenant_id = Column(String(255), nullable=False)
    ai_system_id = Column(String(255), nullable=False)
    root_task_id = Column(String(255), nullable=False)
    correlation_id = Column(String(255), nullable=True)
    tool = Column(String(255), nullable=False)
    scope_key = Column(String(64), nullable=False)
    record_ref = Column(String(64), nullable=True, comment="HMAC reference, never the value")
    verdict = Column(String(20), nullable=False)
    mode = Column(String(8), nullable=False)
    subject_id = Column(String(255), nullable=True)
    api_key_id = Column(String(255), nullable=True)
    agent_id = Column(String(255), nullable=True)
    accessed_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_record_access_reach", "tenant_id", "ai_system_id", "scope_key", "accessed_at"),
        Index("ix_record_access_run", "tenant_id", "root_task_id"),
        Index("ix_record_access_accessed", "accessed_at"),
        Index("ix_record_access_subject", "tenant_id", "subject_id"),
    )


class AISystemReachRule(Base):
    __tablename__ = "ai_system_reach_rules"

    id = Column(String(64), primary_key=True)
    tenant_id = Column(String(255), nullable=False)
    ai_system_id = Column(String(255), nullable=False)
    scope_key = Column(String(64), nullable=False)
    group_by = Column(String(72), nullable=False)
    window_hours = Column(Integer, nullable=False, default=24, server_default="24")
    max_distinct = Column(Integer, nullable=False)
    severity = Column(String(8), nullable=False, default="high", server_default="high")
    enabled = Column(Boolean, nullable=False, default=True, server_default="true")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    created_by = Column(String(255), nullable=True)

    __table_args__ = (
        Index("ix_ai_system_reach_rules_system", "tenant_id", "ai_system_id"),
        CheckConstraint("window_hours BETWEEN 1 AND 720", name="ck_reach_window"),
        CheckConstraint("max_distinct >= 1", name="ck_reach_max"),
        CheckConstraint("severity IN ('low','medium','high','critical')", name="ck_reach_severity"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "ai_system_id": self.ai_system_id,
            "scope_key": self.scope_key,
            "group_by": self.group_by,
            "window_hours": self.window_hours,
            "max_distinct": self.max_distinct,
            "severity": self.severity,
            "enabled": bool(self.enabled),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "created_by": self.created_by,
        }
