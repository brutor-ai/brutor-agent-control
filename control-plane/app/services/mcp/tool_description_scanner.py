# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# MCP tool-description poison scanner (THREAT-01)

When an upstream MCP server is registered or its capabilities are
re-discovered, scan every tool's `description` field for prompt-
injection payloads. A poisoned description never reaches a per-
request guardrail (which only sees the tool *invocation* arguments
and *result*) — it lives in the static catalog the client model
consumes during planning, which is exactly the surface an attacker
targets.

The check piggy-backs on the existing ContentFilter prompt-injection
detector so we don't maintain a second pattern list. Gated on TWO
toggles, both of which must be enabled for the scan to fire:

    GuardrailConfig.mcp_registration_enabled  (surface toggle)
    GuardrailConfig.prompt_injection_enabled  (check toggle)

The motivation for the second gate is that an operator who has
disabled prompt-injection detection at the request layer almost
certainly does not want it firing during registration either — and
without the check being on, the detector's pattern list isn't the
authoritative source the operator has chosen.

# Why a separate scanner module (not a method on capability_diff)

Capability diff is about CHANGE detection — a tool that's always
had a poisoned description doesn't fail the drift check, because it
hasn't drifted. Poison scanning is about ABSOLUTE content detection
— every refresh re-validates every description even if it hasn't
changed. Keeping the two concerns in separate modules makes the
intent obvious in `mcp_server_manager.discover_capabilities()` and
lets each evolve independently.

# Alert shape

One system_alert row per (server, tool_name) tuple. Dedup key is
`mcp_poison:<server_id>:<tool_name>` — a fresh discovery that
detects the same poisoned description bumps `occurrence_count`
rather than spawning a duplicate alert. The first detection of a
DIFFERENT tool on the same server creates a separate row, so an
operator can ack each tool independently from the Mission Control
alerts list.

Category is `infrastructure_threshold` (this matches the existing
Mission Control rendering — we considered a new `tool_poisoning`
category but the alert UI groups by category and adding one would
have orphaned the row in its own bucket of size 1). The
`error_code` field carries the specific signal `mcp_tool_description_poisoned`
for SIEM-side dashboards that want to count this distinctly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session
from ulid import ULID

from app.database.models.guardrail_config import GuardrailConfig
from app.database.models.mcp_server import MCPServer
from app.database.models.system_alert import SystemAlert
from app.services.guardrails.content_filter import ContentFilter, ContentPolicy


log = logging.getLogger("brutor-mcp-tool-poison-scanner")

ERROR_CODE = "mcp_tool_description_poisoned"
ALERT_CATEGORY = "infrastructure_threshold"
ALERT_SEVERITY = "high"


# Field labels we scan. `description` is the canonical THREAT-01
# surface; `name` and `title` are equally operator-supplied
# metadata that reaches the client model's planner (a name like
# `ignore_previous_instructions` or a title with Unicode tag-char
# smuggling is the same attack class). We track which field
# tripped on each hit so the alert + modal name it accurately.
FIELD_DESCRIPTION = "description"
FIELD_NAME = "name"
FIELD_TITLE = "title"


@dataclass
class PoisonHit:
    tool_name: str
    description: str        # the offending text (named for backward compat — actually carries the matched field's value, see field_kind)
    confidence: float
    field_kind: str = FIELD_DESCRIPTION  # which tool field tripped: "description", "name", or "title"
    patterns_matched: List[Dict[str, Any]] = field(default_factory=list)
    suspicious_indicator_count: int = 0

    def to_alert_context_entry(self) -> Dict[str, Any]:
        """Single field's findings in the shape stored on the
        system_alert's `context.findings[]` array. Truncate the
        excerpt to 500 chars so a pathological 50 kB blob doesn't
        bloat the alert row (the full value still lives in
        `mcp_servers.capabilities` for forensics)."""
        return {
            "tool_name": self.tool_name,
            "field_kind": self.field_kind,
            "description_excerpt": self.description[:500],
            "description_truncated": len(self.description) > 500,
            "confidence": round(self.confidence, 3),
            "patterns_matched": self.patterns_matched,
            "suspicious_indicator_count": self.suspicious_indicator_count,
        }


def _resolve_active_registration_config(
    db: Session, server: MCPServer
) -> Optional[GuardrailConfig]:
    """Return the first enabled GuardrailConfig in this server's
    tenant that has BOTH `mcp_registration_enabled` and
    `prompt_injection_enabled` set. None if no config wants the
    scan — caller then short-circuits.

    For a multi-config tenant we use the highest-priority enabled
    config (lower priority number = higher precedence, matching the
    pattern in `_resolve_configs` for per-request checks). A more
    sophisticated implementation would resolve per the server's
    resource-group binding, but registration is a low-frequency
    admin action where the marginal benefit of per-group resolution
    is small and the cost of a missed scan (a poisoned tool reaching
    the catalog unflagged) is high.
    """
    return (
        db.query(GuardrailConfig)
        .filter(GuardrailConfig.tenant_id == server.tenant_id)
        .filter(GuardrailConfig.enabled.is_(True))
        .filter(GuardrailConfig.mcp_registration_enabled.is_(True))
        .filter(GuardrailConfig.prompt_injection_enabled.is_(True))
        .order_by(GuardrailConfig.priority.asc().nullslast())
        .first()
    )


def _content_filter_from(config: GuardrailConfig) -> ContentFilter:
    """Build a ContentFilter wired to the same sensitivity the
    operator picked for per-request prompt-injection checks. We
    only need the prompt-injection portion — all other detectors
    are off — but ContentPolicy is constructed in one shot so we
    explicitly disable the others to avoid surprise side-effects
    if the policy adds new defaults later."""
    sensitivity = (
        config.prompt_injection_sensitivity
        if config.prompt_injection_sensitivity is not None
        else 0.7
    )
    policy = ContentPolicy(
        detect_email=False,
        detect_phone=False,
        detect_ssn=False,
        detect_credit_card=False,
        detect_api_keys=False,
        detect_prompt_injection=True,
        prompt_injection_sensitivity=float(sensitivity),
        detect_toxic_content=False,
        detect_jailbreak=False,
    )
    return ContentFilter(policy=policy)


def _scan_one_field(
    cf: ContentFilter,
    tool_name: str,
    field_kind: str,
    text: str,
) -> Optional[PoisonHit]:
    """Run a single tool field through the detector. Returns a
    PoisonHit if `detected=True`, else None. Shared by the
    description / name / title scanners so the detector-invocation
    + finding-construction logic lives in one place."""
    if not isinstance(text, str) or not text.strip():
        return None
    # pylint: disable=protected-access — we deliberately reach
    # into the detector here because building a full FilterResult
    # would also run PII / toxic / jailbreak paths we don't need.
    detection = cf._detect_prompt_injection(text)
    if not detection.get("detected"):
        return None
    return PoisonHit(
        tool_name=tool_name or "<unnamed>",
        description=text,
        confidence=float(detection.get("confidence") or 0.0),
        field_kind=field_kind,
        patterns_matched=detection.get("patterns_matched") or [],
        suspicious_indicator_count=int(
            detection.get("suspicious_indicator_count") or 0
        ),
    )


def _scan_descriptions(
    cf: ContentFilter,
    tools: List[Dict[str, Any]],
    sensitivity: float,
) -> List[PoisonHit]:
    """Scan each tool's name + title + description against the
    prompt-injection detector. A "poisoned" field is one where
    the detector reports `detected=True` for any matched pattern
    OR suspicious-indicator cluster — no further confidence
    threshold.

    The historical name (`_scan_descriptions`) is preserved for
    backward compat; the scope is broader — every operator-supplied
    metadata field that reaches the client model's planner is
    scanned. A tool with both a poisoned name AND a poisoned
    description produces two distinct PoisonHits so they can be
    triaged + acknowledged independently in Mission Control.

    Why we ignore the operator's per-request sensitivity here:
    tool metadata is static, operator-curated text that should
    never legitimately contain injection-shaped content
    ("ignore previous instructions", "you are now in admin mode",
    base64-smuggled commands, names like `ignore_previous_instructions`,
    titles with Unicode tag-character smuggling). Per-request
    prompt-injection detection rightly applies the sensitivity
    threshold because a user message may contain words like
    "ignore" or "override" in benign contexts; for catalog
    scanning that false-positive pressure is essentially zero,
    and the cost of missing a canonical payload (which the
    built-in detector caps at ~0.3 confidence on a single-pattern
    match) is high — the catalog is exactly the surface THREAT-01
    targets.

    Operators who DO want a higher catalog-scan threshold can
    disable Check MCP Registration entirely on the policy, or
    move to a Lakera Guard provider whose confidence numbers
    align with the per-request threshold semantics. The
    `sensitivity` arg is retained for that future per-provider
    tuning rather than removed outright.
    """
    _ = sensitivity  # retained for future per-provider tuning
    hits: List[PoisonHit] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name") or "<unnamed>"

        # Scan order: name → title → description. Order doesn't
        # affect correctness (each produces its own PoisonHit
        # keyed by field_kind in the dedup logic), but matches
        # the natural reading order operators see in the
        # admin UI's catalog view.
        for field_kind, field_value in (
            (FIELD_NAME, tool.get("name")),
            (FIELD_TITLE, tool.get("title")),
            (FIELD_DESCRIPTION, tool.get("description")),
        ):
            if field_value is None:
                continue
            hit = _scan_one_field(cf, name, field_kind, field_value)
            if hit is not None:
                hits.append(hit)
    return hits


def _dedup_key(server_id: str, tool_name: str, field_kind: str) -> str:
    """One alert per (server, tool, field). A tool with both a
    poisoned name AND a poisoned description produces two distinct
    alerts so operators can acknowledge / resolve each
    independently. Older alert rows (pre-tool-name-scanning) used
    the 3-segment form `mcp_poison:{server_id}:{tool_name}` —
    those continue to dedupe correctly because new hits append
    `:description` and never collide with the old key."""
    return f"mcp_poison:{server_id}:{tool_name}:{field_kind}"


# Human-friendly label for the field that tripped — used in
# alert titles and the admin UI modal. Distinct from FIELD_*
# constants so we can keep the wire-level value stable while
# the operator-facing copy evolves.
_FIELD_LABELS = {
    FIELD_DESCRIPTION: "description",
    FIELD_NAME: "name",
    FIELD_TITLE: "title",
}


def _raise_or_refresh_alert(
    db: Session, server: MCPServer, hit: PoisonHit, config_id: str
) -> SystemAlert:
    """One alert per (server, tool, field). Same pattern as
    `capability_diff.raise_or_refresh_alert` — repeat detections
    bump occurrence_count + last_seen_at on the existing active
    row; a different (tool, field) tuple gets its own row."""
    dedup_key = _dedup_key(server.id, hit.tool_name, hit.field_kind)
    existing = (
        db.query(SystemAlert)
        .filter(SystemAlert.dedup_key == dedup_key)
        .filter(SystemAlert.status.in_(("active", "acknowledged")))
        .first()
    )

    field_label = _FIELD_LABELS.get(hit.field_kind, hit.field_kind)
    title = f"Poisoned tool {field_label} on {server.name}: {hit.tool_name}"
    message = (
        f"Tool {field_label} for '{hit.tool_name}' on MCP server "
        f"'{server.name}' matched the prompt-injection detector "
        f"(confidence {hit.confidence:.2f}). Catalog has been "
        f"recorded but operators should review before authorising "
        f"this tool for client use."
    )
    context = {
        "server_id": server.id,
        "server_name": server.name,
        "tool_name": hit.tool_name,
        "guardrail_config_id": config_id,
        "findings": [hit.to_alert_context_entry()],
    }

    if existing is not None:
        existing.last_seen_at = datetime.now(timezone.utc)
        existing.occurrence_count = (existing.occurrence_count or 0) + 1
        existing.title = title
        existing.message = message
        existing.context = context
        db.flush()
        log.info(
            "Refreshed mcp-tool-poison alert",
            extra={
                "alert_id": existing.id,
                "server_id": server.id,
                "tool_name": hit.tool_name,
                "occurrence_count": existing.occurrence_count,
            },
        )
        return existing

    alert = SystemAlert(
        id=f"sysalert-{ULID()}",
        tenant_id=server.tenant_id,
        severity=ALERT_SEVERITY,
        category=ALERT_CATEGORY,
        source=f"mcp.server.{server.id}",
        resource_type="mcp_server",
        resource_id=server.id,
        resource_name=server.name,
        error_code=ERROR_CODE,
        title=title,
        message=message,
        context=context,
        status="active",
        dedup_key=dedup_key,
    )
    db.add(alert)
    db.flush()
    log.warning(
        "Raised mcp-tool-poison alert",
        extra={
            "alert_id": alert.id,
            "server_id": server.id,
            "tool_name": hit.tool_name,
            "confidence": hit.confidence,
        },
    )
    return alert


def scan_for_poisoned_descriptions(
    db: Session, server: MCPServer, capabilities: Dict[str, Any]
) -> List[PoisonHit]:
    """Top-level entry point. Called from
    `mcp_server_manager.discover_capabilities()` after capability
    snapshot and diff. The caller wraps in try/except so a scan
    failure does not roll back the capability update — same
    convention as `record_and_diff`.

    Returns the list of detected poison hits (possibly empty). The
    caller can log the count alongside its other discovery
    telemetry; alert rows have already been written to the
    session for the caller to commit.
    """
    config = _resolve_active_registration_config(db, server)
    if config is None:
        return []

    tools = (capabilities or {}).get("tools") or []
    if not tools:
        return []

    sensitivity = (
        float(config.prompt_injection_sensitivity)
        if config.prompt_injection_sensitivity is not None
        else 0.7
    )
    cf = _content_filter_from(config)
    hits = _scan_descriptions(cf, tools, sensitivity)
    for hit in hits:
        _raise_or_refresh_alert(db, server, hit, config_id=config.id)
    return hits


# ---------------------------------------------------------------------------
# VMCP variant — same detector, different ownership semantics
# ---------------------------------------------------------------------------
#
# Upstream MCP tool descriptions come from an external server the
# operator doesn't control (the attack surface the bench's
# THREAT-01 probe targets). VMCP descriptions / exposed names are
# operator-supplied OVERRIDES the operator writes themselves in the
# admin UI — a different threat profile:
#
#   * A malicious upstream's poisoned description is caught by the
#     `scan_for_poisoned_descriptions` path above.
#   * A compromised admin account, an LLM-generated description the
#     operator pasted without reading, or a copy-paste from
#     untrusted docs is what the VMCP scan catches.
#
# Same detector, same alert shape, different `resource_type` on
# the system_alert (`virtual_server` vs `mcp_server`) and a
# distinct dedup-key namespace so VMCP alerts don't collide with
# upstream-MCP alerts.


def _vmcp_dedup_key(vmcp_id: str, exposed_name: str, field_kind: str) -> str:
    return f"vmcp_poison:{vmcp_id}:{exposed_name}:{field_kind}"


def _raise_or_refresh_vmcp_alert(
    db: Session,
    vmcp_id: str,
    vmcp_name: str,
    tenant_id: str,
    hit: PoisonHit,
    config_id: str,
) -> SystemAlert:
    """VMCP-side counterpart to `_raise_or_refresh_alert`. Same
    shape; `resource_type = virtual_server` so the Mission Control
    alert filter / dashboard groups VMCP issues separately from
    upstream-MCP issues."""
    dedup_key = _vmcp_dedup_key(vmcp_id, hit.tool_name, hit.field_kind)
    existing = (
        db.query(SystemAlert)
        .filter(SystemAlert.dedup_key == dedup_key)
        .filter(SystemAlert.status.in_(("active", "acknowledged")))
        .first()
    )

    field_label = _FIELD_LABELS.get(hit.field_kind, hit.field_kind)
    title = f"Poisoned VMCP tool {field_label} on {vmcp_name}: {hit.tool_name}"
    message = (
        f"Operator-supplied {field_label} for tool '{hit.tool_name}' on "
        f"virtual MCP server '{vmcp_name}' matched the prompt-injection "
        f"detector (confidence {hit.confidence:.2f}). Catalog has been "
        f"saved but operators should review the override before any "
        f"client uses this VMCP."
    )
    context = {
        "vmcp_id": vmcp_id,
        "vmcp_name": vmcp_name,
        "tool_name": hit.tool_name,
        "guardrail_config_id": config_id,
        "findings": [hit.to_alert_context_entry()],
    }

    if existing is not None:
        existing.last_seen_at = datetime.now(timezone.utc)
        existing.occurrence_count = (existing.occurrence_count or 0) + 1
        existing.title = title
        existing.message = message
        existing.context = context
        db.flush()
        log.info(
            "Refreshed vmcp-tool-poison alert",
            extra={
                "alert_id": existing.id,
                "vmcp_id": vmcp_id,
                "tool_name": hit.tool_name,
                "field_kind": hit.field_kind,
                "occurrence_count": existing.occurrence_count,
            },
        )
        return existing

    alert = SystemAlert(
        id=f"sysalert-{ULID()}",
        tenant_id=tenant_id,
        severity=ALERT_SEVERITY,
        category=ALERT_CATEGORY,
        source=f"vmcp.{vmcp_id}",
        resource_type="virtual_server",
        resource_id=vmcp_id,
        resource_name=vmcp_name,
        error_code=ERROR_CODE,
        title=title,
        message=message,
        context=context,
        status="active",
        dedup_key=dedup_key,
    )
    db.add(alert)
    db.flush()
    log.warning(
        "Raised vmcp-tool-poison alert",
        extra={
            "alert_id": alert.id,
            "vmcp_id": vmcp_id,
            "tool_name": hit.tool_name,
            "field_kind": hit.field_kind,
            "confidence": hit.confidence,
        },
    )
    return alert


def _resolve_active_vmcp_config(
    db: Session, tenant_id: str
) -> Optional[GuardrailConfig]:
    """Same gates as `_resolve_active_registration_config` but
    keyed by tenant_id since VMCP entries aren't bound to an
    upstream MCP server. Picks the highest-priority enabled config
    for the tenant that has BOTH `mcp_registration_enabled` and
    `prompt_injection_enabled` set — same toggle pair operators
    flipped to opt into THREAT-01 scanning."""
    return (
        db.query(GuardrailConfig)
        .filter(GuardrailConfig.tenant_id == tenant_id)
        .filter(GuardrailConfig.enabled.is_(True))
        .filter(GuardrailConfig.mcp_registration_enabled.is_(True))
        .filter(GuardrailConfig.prompt_injection_enabled.is_(True))
        .order_by(GuardrailConfig.priority.asc().nullslast())
        .first()
    )


def _scan_vmcp_tools(
    cf: ContentFilter,
    tools: List[Dict[str, Any]],
) -> List[PoisonHit]:
    """Walk each tool entry in a VMCP `capabilities.tools` list
    and scan the operator-supplied `description_override` +
    `exposed_name` fields. Skips empty / missing overrides
    (the operator chose to inherit the upstream value, which is
    covered by the upstream MCP server's own scan)."""
    hits: List[PoisonHit] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        exposed_name = (tool.get("exposed_name") or tool.get("capability_name") or "<unnamed>")

        # Operator-supplied override fields. Inherited (None /
        # empty) values fall back to the upstream tool_def and
        # are out of scope here — the upstream MCP scan covers
        # those at registration time.
        for field_kind, field_value in (
            (FIELD_NAME, tool.get("exposed_name")),
            (FIELD_DESCRIPTION, tool.get("description_override")),
        ):
            if field_value is None:
                continue
            hit = _scan_one_field(cf, exposed_name, field_kind, field_value)
            if hit is not None:
                hits.append(hit)
    return hits


def scan_for_poisoned_vmcp_overrides(
    db: Session,
    vmcp_id: str,
    vmcp_name: str,
    tenant_id: str,
    capabilities: Dict[str, Any],
) -> List[PoisonHit]:
    """Top-level entry point for VMCP create / update. Called
    from `routes/admin/virtual_servers.py` after the normalizer
    has accepted the operator-supplied capabilities and BEFORE
    the row is committed (so a Block-on-poison decision could be
    layered in later if the operator wants stricter handling than
    the current "load + alert + warn" contract).

    Returns the list of detected poison hits (possibly empty).
    The caller wraps in try/except so a scan failure does not
    roll back the VMCP create / update — same convention as
    `scan_for_poisoned_descriptions` for upstream MCP servers.
    """
    config = _resolve_active_vmcp_config(db, tenant_id)
    if config is None:
        return []

    tools = (capabilities or {}).get("tools") or []
    if not tools:
        return []

    cf = _content_filter_from(config)
    hits = _scan_vmcp_tools(cf, tools)
    for hit in hits:
        _raise_or_refresh_vmcp_alert(
            db, vmcp_id, vmcp_name, tenant_id, hit, config_id=config.id
        )
    return hits
