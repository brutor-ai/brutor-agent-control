# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Capability snapshot diff + drift alerting

When an upstream MCP server changes its tool set silently —
adding a `delete_database` tool, renaming `read_record` to
`read_records`, changing a tool's description to inject an
instruction — Brutor should NOTICE and RAISE AN ALERT so the
security team reviews before the new shape is silently used in
production.

# The pipeline

  1. `discover_capabilities()` (in mcp_server_manager.py) fetches
     the live tool/resource/prompt list from the upstream.
  2. After updating `mcp_servers.capabilities`, it calls
     `record_and_diff()` here.
  3. We diff against the most recent PRIOR snapshot.
  4. If the contract changed, we persist the new snapshot as one
     row in `mcp_capability_snapshots` (see "recorded only when the
     contract changed" below).
  5. If the diff is non-empty, we raise / refresh a
     `resource_health` system_alert with structured context
     describing exactly what changed.

# Diff categories

  - `new_tool`         — a tool name that wasn't in the prior snapshot
  - `removed_tool`     — a tool that disappeared
  - `description_change` — same tool name, different description text
  - `input_schema_change` — same tool name, different inputSchema
                            (defensive against parameter changes
                            that bypass guardrails)
  - `annotation_change` — same tool name, different MCP annotations
                          (readOnlyHint / destructiveHint / …). The
                          runtime gates autonomy and `deny_tool_risk`
                          on these, so a flip that makes a tool look
                          SAFER (e.g. readOnlyHint false→true) raises
                          the alert at `high` severity.
  - `output_schema_change`, `title_change`, `meta_change` — the other
                          MCP spec Tool fields.

Resources + prompts get the same treatment with their natural
identity keys (uri for resources, name for prompts).

# Why a single alert, not one per change

Operators get spammed if a server adds 20 new tools at once.
We coalesce into one `resource_health` alert per server-per-
discovery, with the structured `context.changes` listing all the
individual category-tagged changes. Operators see the alert,
expand it, see all changes at once.

The alert's `dedup_key` is keyed by server (not by change), so
repeated discoveries that find the SAME drift bump
`occurrence_count` rather than spawning new alert rows.

# Snapshots are recorded only when the contract changed

A row is appended only when the canonical content hash
(`tool_snapshot.content_hash`: order-insensitive, excluding volatile
`_brutor` metadata) differs from the latest row's. Routine re-discovery of
an unchanged server writes nothing. Each appended row carries `is_change`:
true iff the semantic diff against its predecessor is non-empty. The core's
drift cause attribution (`drift/cause.rs::find_dependency_change`) counts
only `is_change` rows — a routine refresh or a first-ever snapshot is not a
dependency change.

# What's NOT alerted

  - First-ever snapshot — no prior to diff against. Recorded but
    no alert. (Otherwise EVERY server raises an alert on the
    first discovery, which is noise.)
  - Reordering of tools (same names, same descriptions, just a
    different order in the list).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session
from ulid import ULID

from app.database.models.mcp_capability_snapshot import (
    MCPCapabilitySnapshot,
    TRIGGER_DISCOVERY,
)
from app.database.models.mcp_server import MCPServer
from app.database.models.system_alert import SystemAlert
from app.services.mcp.tool_snapshot import (
    annotation_change_relaxes,
    content_hash,
    normalize_mcp_capabilities,
    normalize_tools,
)


log = logging.getLogger("brutor-capability-diff")


# Change category constants — surface in `context.changes[].kind`
# so SIEM-side dashboards can count them.
KIND_NEW_TOOL = "new_tool"
KIND_REMOVED_TOOL = "removed_tool"
KIND_DESCRIPTION_CHANGE = "description_change"
KIND_INPUT_SCHEMA_CHANGE = "input_schema_change"
KIND_ANNOTATION_CHANGE = "annotation_change"
KIND_OUTPUT_SCHEMA_CHANGE = "output_schema_change"
KIND_TITLE_CHANGE = "title_change"
KIND_META_CHANGE = "meta_change"
KIND_NEW_RESOURCE = "new_resource"
KIND_REMOVED_RESOURCE = "removed_resource"
KIND_NEW_PROMPT = "new_prompt"
KIND_REMOVED_PROMPT = "removed_prompt"


@dataclass
class Change:
    """One structured change item — serialised into the alert's
    `context.changes` array."""
    kind: str
    name: str
    before: Optional[Any] = None
    after: Optional[Any] = None
    # annotation_change only: the new hints make the tool look safer to
    # the runtime gate (became read-only / lost destructive / open-world).
    relaxes: bool = False

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"kind": self.kind, "name": self.name}
        if self.before is not None:
            out["before"] = self.before
        if self.after is not None:
            out["after"] = self.after
        if self.relaxes:
            out["relaxes"] = True
        return out


@dataclass
class DiffResult:
    """Summary of the comparison between two snapshots."""
    changes: List[Change] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.changes

    @property
    def relaxes_risk(self) -> bool:
        """Any annotation change that makes a tool look safer."""
        return any(c.relaxes for c in self.changes)

    def summary_message(self) -> str:
        """One-line operator-facing summary."""
        if self.is_empty:
            return "No capability changes detected"

        counts: Dict[str, int] = {}
        for c in self.changes:
            counts[c.kind] = counts.get(c.kind, 0) + 1
        parts = [f"{n} {k.replace('_', ' ')}" for k, n in sorted(counts.items())]
        return f"Capability drift detected: {', '.join(parts)}"


# ---------------------------------------------------------------------------
# Diff algorithm — pure function, no DB access
# ---------------------------------------------------------------------------


def diff_capabilities(
    old: Optional[Dict[str, Any]],
    new: Optional[Dict[str, Any]],
) -> DiffResult:
    """Compute the structured diff between two capability snapshots.

    Args:
        old: The PRIOR snapshot's capabilities dict. None or empty
             means "no prior" — returns an empty DiffResult (we
             don't alert on first snapshot).
        new: The freshly-discovered capabilities dict.

    Returns:
        DiffResult with categorised Change entries. Empty when no
        meaningful changes were found.
    """
    result = DiffResult()
    if not old:
        # First snapshot — nothing to diff against. Don't alert.
        return result

    new = new or {}

    _diff_named_list(
        normalize_tools(old.get("tools") or []),
        normalize_tools(new.get("tools") or []),
        name_field="name",
        kind_new=KIND_NEW_TOOL,
        kind_removed=KIND_REMOVED_TOOL,
        check_description=True,
        check_input_schema=True,
        check_extended=True,
        result=result,
    )
    _diff_named_list(
        old.get("resources") or [], new.get("resources") or [],
        name_field="uri",
        kind_new=KIND_NEW_RESOURCE,
        kind_removed=KIND_REMOVED_RESOURCE,
        check_description=False,
        check_input_schema=False,
        result=result,
    )
    _diff_named_list(
        old.get("prompts") or [], new.get("prompts") or [],
        name_field="name",
        kind_new=KIND_NEW_PROMPT,
        kind_removed=KIND_REMOVED_PROMPT,
        check_description=False,
        check_input_schema=False,
        result=result,
    )

    return result


def _diff_named_list(
    old_items: List[Any],
    new_items: List[Any],
    *,
    name_field: str,
    kind_new: str,
    kind_removed: str,
    check_description: bool,
    check_input_schema: bool,
    result: DiffResult,
    check_extended: bool = False,
) -> None:
    """Diff two lists of `{name|uri, description?, inputSchema?, ...}`
    dicts. `check_extended` also compares the other MCP Tool fields
    (annotations, outputSchema, title, _meta). Mutates `result` in place."""
    old_by_name: Dict[str, Dict[str, Any]] = {}
    for item in old_items:
        if isinstance(item, dict):
            key = item.get(name_field)
            if isinstance(key, str):
                old_by_name[key] = item

    new_by_name: Dict[str, Dict[str, Any]] = {}
    for item in new_items:
        if isinstance(item, dict):
            key = item.get(name_field)
            if isinstance(key, str):
                new_by_name[key] = item

    old_names = set(old_by_name.keys())
    new_names = set(new_by_name.keys())

    # New names (in new, not in old)
    for name in sorted(new_names - old_names):
        result.changes.append(Change(kind=kind_new, name=name))

    # Removed names (in old, not in new)
    for name in sorted(old_names - new_names):
        result.changes.append(Change(kind=kind_removed, name=name))

    # Modified names — same name, different description or schema
    if check_description or check_input_schema or check_extended:
        for name in sorted(old_names & new_names):
            old_item = old_by_name[name]
            new_item = new_by_name[name]
            if check_description:
                old_desc = old_item.get("description")
                new_desc = new_item.get("description")
                if old_desc != new_desc:
                    result.changes.append(Change(
                        kind=KIND_DESCRIPTION_CHANGE,
                        name=name,
                        before=old_desc,
                        after=new_desc,
                    ))
            if check_input_schema:
                old_schema = old_item.get("inputSchema")
                new_schema = new_item.get("inputSchema")
                if old_schema != new_schema:
                    result.changes.append(Change(
                        kind=KIND_INPUT_SCHEMA_CHANGE,
                        name=name,
                        # Don't embed full schemas — they can be
                        # large. Just record the change occurred;
                        # the snapshot history has the full payload.
                    ))
            if check_extended:
                _diff_extended_tool_fields(
                    name, old_by_name[name], new_by_name[name], result,
                )


def _diff_extended_tool_fields(
    name: str,
    old_item: Dict[str, Any],
    new_item: Dict[str, Any],
    result: DiffResult,
) -> None:
    """annotations / outputSchema / title / _meta for one tool."""
    old_ann = old_item.get("annotations")
    new_ann = new_item.get("annotations")
    if old_ann != new_ann:
        # Annotations are small and their before/after IS the finding
        # (readOnlyHint false→true is what an operator must see).
        result.changes.append(Change(
            kind=KIND_ANNOTATION_CHANGE,
            name=name,
            before=old_ann if old_ann is not None else {},
            after=new_ann if new_ann is not None else {},
            relaxes=annotation_change_relaxes(old_ann, new_ann),
        ))
    if old_item.get("outputSchema") != new_item.get("outputSchema"):
        result.changes.append(Change(kind=KIND_OUTPUT_SCHEMA_CHANGE, name=name))
    if old_item.get("title") != new_item.get("title"):
        result.changes.append(Change(
            kind=KIND_TITLE_CHANGE,
            name=name,
            before=old_item.get("title"),
            after=new_item.get("title"),
        ))
    if old_item.get("_meta") != new_item.get("_meta"):
        result.changes.append(Change(kind=KIND_META_CHANGE, name=name))


# ---------------------------------------------------------------------------
# Snapshot recording
# ---------------------------------------------------------------------------


def record_snapshot(
    db: Session,
    server: MCPServer,
    capabilities: Dict[str, Any],
    *,
    trigger: str = TRIGGER_DISCOVERY,
    is_change: bool = False,
) -> MCPCapabilitySnapshot:
    """Append a new snapshot row. Caller owns db.commit().

    Low-level: always appends. Discovery goes through `record_and_diff`,
    which appends only when the contract changed.
    """
    from datetime import datetime, timezone

    caps = normalize_mcp_capabilities(capabilities or {}) or {}
    snap = MCPCapabilitySnapshot(
        id=f"snap-{ULID()}",
        tenant_id=server.tenant_id,
        mcp_server_id=server.id,
        # Python-side timestamp (µs) rather than the server default:
        # "latest prior" ordering must be exact even for two
        # discoveries inside the same second.
        snapshot_at=datetime.now(timezone.utc),
        capabilities=caps,
        trigger=trigger,
        content_hash=content_hash(caps),
        is_change=is_change,
    )
    db.add(snap)
    db.flush()
    return snap


def latest_prior_snapshot(
    db: Session,
    server: MCPServer,
    before_snapshot_id: Optional[str] = None,
) -> Optional[MCPCapabilitySnapshot]:
    """Find the most recent snapshot for `server` BEFORE the given
    snapshot (or the most recent if none specified).

    Used by the diff path: after recording a new snapshot, we ask
    for the prior one to compare against.
    """
    query = (
        db.query(MCPCapabilitySnapshot)
        .filter(MCPCapabilitySnapshot.mcp_server_id == server.id)
        .filter(MCPCapabilitySnapshot.tenant_id == server.tenant_id)
    )
    if before_snapshot_id:
        query = query.filter(MCPCapabilitySnapshot.id != before_snapshot_id)
    return query.order_by(
        MCPCapabilitySnapshot.snapshot_at.desc(),
        MCPCapabilitySnapshot.id.desc(),
    ).first()


# ---------------------------------------------------------------------------
# Alert raising — coalesced per server, dedup_key-stable
# ---------------------------------------------------------------------------


def _build_dedup_key(server: MCPServer) -> str:
    # Same shape as the rest of the system_alerts table:
    # `tenant|category|source|error_code`
    return (
        f"{server.tenant_id}|resource_health|"
        f"mcp.server.{server.id}|capability_drift"
    )


def raise_or_refresh_alert(
    db: Session,
    server: MCPServer,
    diff: DiffResult,
    severity: Optional[str] = None,
) -> Optional[SystemAlert]:
    """Create or refresh a SystemAlert for the given diff.

    Re-runs that find the SAME drift bump `occurrence_count` and
    `last_seen_at` on the existing active alert (the
    `uq_system_alerts_active_dedup` partial index enforces one
    active row per dedup_key). When operators acknowledge an
    alert and later drift produces a different diff, the
    occurrence count bumps but the message refreshes to the new
    diff — operators see "the situation evolved."

    Returns the alert row (new or refreshed), or None if the diff
    was empty.
    """
    if diff.is_empty:
        return None
    if severity is None:
        # A tool that now CLAIMS to be safer bypasses autonomy gating and
        # `deny_tool_risk` if the claim is false — review before trusting.
        severity = "high" if diff.relaxes_risk else "medium"

    dedup_key = _build_dedup_key(server)
    existing = (
        db.query(SystemAlert)
        .filter(SystemAlert.dedup_key == dedup_key)
        .filter(SystemAlert.status.in_(("active", "acknowledged")))
        .first()
    )

    title = f"Capability drift on {server.name}"
    message = diff.summary_message()
    context = {
        "server_id": server.id,
        "server_name": server.name,
        "change_count": len(diff.changes),
        "changes": [c.to_dict() for c in diff.changes],
    }

    if existing is not None:
        from datetime import datetime, timezone
        existing.last_seen_at = datetime.now(timezone.utc)
        existing.occurrence_count = (existing.occurrence_count or 0) + 1
        existing.title = title
        existing.message = message
        existing.context = context
        existing.severity = severity
        db.flush()
        log.info(
            "Refreshed capability-drift alert",
            extra={
                "alert_id": existing.id,
                "server_id": server.id,
                "occurrence_count": existing.occurrence_count,
                "change_count": len(diff.changes),
            },
        )
        return existing

    alert = SystemAlert(
        id=f"sysalert-{ULID()}",
        tenant_id=server.tenant_id,
        severity=severity,
        category="resource_health",
        source=f"mcp.server.{server.id}",
        resource_type="mcp_server",
        resource_id=server.id,
        resource_name=server.name,
        error_code="capability_drift",
        title=title,
        message=message,
        context=context,
        status="active",
        dedup_key=dedup_key,
    )
    db.add(alert)
    db.flush()
    log.info(
        "Raised capability-drift alert",
        extra={
            "alert_id": alert.id,
            "server_id": server.id,
            "change_count": len(diff.changes),
        },
    )
    return alert


# ---------------------------------------------------------------------------
# Top-level helper — what `discover_capabilities()` calls
# ---------------------------------------------------------------------------


def record_and_diff(
    db: Session,
    server: MCPServer,
    capabilities: Dict[str, Any],
    *,
    trigger: str = TRIGGER_DISCOVERY,
) -> DiffResult:
    """Diff against the latest snapshot, append a new one only if the
    contract changed, raise alerts if needed. The caller owns
    `db.commit()`.

    This is the only function the existing `discover_capabilities()`
    path needs to call. Returns the DiffResult so the caller can
    log the summary alongside its own discovery telemetry.
    """
    caps = normalize_mcp_capabilities(capabilities or {}) or {}
    prior = latest_prior_snapshot(db, server)
    if prior is not None and prior.content_hash == content_hash(caps):
        # Routine refresh of an unchanged server: no row, no cause.
        return DiffResult()
    diff = diff_capabilities(
        old=prior.capabilities if prior is not None else None,
        new=caps,
    )
    record_snapshot(
        db, server, caps, trigger=trigger,
        # A first-ever snapshot is a baseline, not a change.
        is_change=prior is not None and not diff.is_empty,
    )
    if not diff.is_empty:
        raise_or_refresh_alert(db, server, diff)
    return diff
