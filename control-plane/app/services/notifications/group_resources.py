# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Group-resource binding notifications.

When an admin assigns or removes a *resource* (LLM model, MCP server,
agent skill, KB collection) to/from a resource group, every member of
that group should see an inbox entry — same UX as when a user is added
to or removed from the group itself.

The originating endpoint typically:

  1. Performs the binding (insert/delete in a join table).
  2. Calls `notify_group_members_about_resource_change(...)`.
  3. Commits the transaction.

The helper:

* Reads `resource_group_memberships` for the affected groups.
* Deduplicates users that appear in more than one of the supplied
  groups so each user gets exactly one notification per resource
  change (e.g. an admin adds an MCP server to two groups in one
  PATCH — a user in both still only gets one notification, listing
  both groups).
* Inserts one `Notification` row per recipient. The caller commits.

Notification shape:

    notification_type = "group_resource"
    title             = "‘{resource_name}’ added to {group}"  / "removed from {group}"
    body              = markdown lines that name the resource and
                        the affected groups + a closing "by {admin}"
                        attribution.
    metadata          = {
        "resource_kind": "model" | "mcp_server" | "agent_skill" | "kb_collection",
        "resource_id":   "...",
        "resource_name": "...",
        "group_ids":     ["grp-…", …],
        "group_names":   ["Default Group", …],
        "action":        "added" | "removed",
        "actor":         "<admin username>",
    }

The portal's notification badge picker filters by `notification_type`
so adding the new `group_resource` type doesn't disturb existing
listings — but the existing UI shows all types in the inbox, so the
new entries appear automatically with no further changes.
"""

from __future__ import annotations

from typing import Iterable, List, Sequence

from sqlalchemy.orm import Session
from ulid import ULID

from app.database.models.notification import Notification
from app.database.models.resource_group import ResourceGroup
from app.database.models.resource_group_membership import (
    ResourceGroupMembership,
)
from app.core.logging_system import BrutorLogger

# Stable enum-like values stored in `notification_type`. Kept as a
# constant to avoid string drift across endpoints; tests assert on
# exact equality.
NOTIFICATION_TYPE = "group_resource"

ResourceKind = str  # "model" | "mcp_server" | "agent_skill" | "kb_collection"

# Display labels used in the title + body. Plural forms are used when
# the change applies to multiple groups in one call.
_KIND_LABELS: dict[ResourceKind, tuple[str, str]] = {
    "model":         ("AI model",      "AI models"),
    "mcp_server":    ("MCP server",    "MCP servers"),
    "agent_skill":   ("Agent Skill",   "Agent Skills"),
    "kb_collection": ("Knowledge Base collection",
                      "Knowledge Base collections"),
}


def _label(kind: ResourceKind, *, plural: bool = False) -> str:
    """Lookup display label for a kind, falling back to the raw key.

    Falling back rather than raising means an endpoint passing a new
    `kind` value still produces a sane notification — at worst the
    label reads "skillkind"-ish — without breaking the user-facing
    flow.
    """
    pair = _KIND_LABELS.get(kind)
    if pair is None:
        return kind.replace("_", " ")
    return pair[1] if plural else pair[0]


def _format_group_list(group_names: Sequence[str]) -> str:
    """Human-friendly join: ['A'] -> 'A', ['A','B'] -> 'A and B',
    ['A','B','C'] -> 'A, B, and C'."""
    items = [n for n in group_names if n]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def notify_group_members_about_resource_change(
    *,
    db: Session,
    tenant_id: str,
    group_ids: Iterable[str],
    resource_kind: ResourceKind,
    resource_id: str,
    resource_name: str,
    action: str,
    actor_username: str | None,
    logger: BrutorLogger | None = None,
) -> int:
    """Insert one Notification per distinct member of the given
    groups. Returns the number of notifications staged.

    Caller MUST commit. The helper only `db.add()`s + `db.flush()`es
    so the rows are visible inside the same transaction (lets the
    endpoint roll everything back if the binding write fails after
    the notification fan-out).

    Args:
        db: SQLAlchemy session held by the endpoint.
        tenant_id: Tenant scope. Used in the `Notification` row and
            in the membership lookup.
        group_ids: Groups affected by the binding change. Empty
            list → no-op; returns 0.
        resource_kind: One of the `_KIND_LABELS` keys (see module
            docstring). Unknown kinds still produce a notification
            but with a less polished label.
        resource_id, resource_name: Identify the resource in the
            metadata + body.
        action: "added" | "removed". Anything else is treated as
            "added" defensively.
        actor_username: Admin who performed the action, surfaced as
            "by <admin>" in the body. May be `None` for system-
            triggered changes.
    """
    assert action in {"added", "removed"}, (
        f"action must be 'added' or 'removed', got {action!r}"
    )

    group_id_list: List[str] = list({gid for gid in group_ids if gid})
    if not group_id_list:
        return 0

    # Resolve group display names so the notification body can name
    # them. Missing groups (deleted between binding and notify) are
    # silently dropped from the message rather than aborting — the
    # binding write already succeeded.
    group_rows = (
        db.query(ResourceGroup)
        .filter(
            ResourceGroup.tenant_id == tenant_id,
            ResourceGroup.id.in_(group_id_list),
        )
        .all()
    )
    group_name_by_id: dict[str, str] = {
        g.id: (g.display_name or g.name) for g in group_rows
    }
    group_names = [group_name_by_id[gid] for gid in group_id_list
                   if gid in group_name_by_id]

    # Distinct members across all affected groups. A user in 3 of
    # the groups still gets one notification.
    user_ids: set[int] = {
        row.user_id
        for row in db.query(ResourceGroupMembership.user_id)
        .filter(
            ResourceGroupMembership.tenant_id == tenant_id,
            ResourceGroupMembership.group_id.in_(group_id_list),
        )
        .all()
    }
    if not user_ids:
        return 0

    label_singular = _label(resource_kind)
    group_list_str = _format_group_list(group_names) or "(unnamed group)"
    plural_groups = len(group_names) > 1

    # Title — kept short so it fits the inbox row width.
    if action == "added":
        title = (
            f"{label_singular} '{resource_name}' added to "
            f"{'groups you belong to' if plural_groups else group_list_str}"
        )
    else:
        title = (
            f"{label_singular} '{resource_name}' removed from "
            f"{'groups you belong to' if plural_groups else group_list_str}"
        )

    # Body — markdown, mirrors the user-add-to-group notification
    # style (bold field labels). When multiple groups are involved
    # we list them so the user can see the scope.
    body_lines = [
        f"**Resource:** {label_singular} '{resource_name}'",
        f"**{'Added to' if action == 'added' else 'Removed from'}:** "
        + (group_list_str if group_names else "(unknown groups)"),
    ]
    if actor_username:
        body_lines.append(
            f"**By:** {actor_username}"
        )
    body = "\n\n".join(body_lines)

    metadata = {
        "resource_kind": resource_kind,
        "resource_id": resource_id,
        "resource_name": resource_name,
        "group_ids": group_id_list,
        "group_names": group_names,
        "action": action,
        "actor": actor_username,
    }

    inserted = 0
    for uid in user_ids:
        notif = Notification(
            id=f"notif-{ULID()}",
            tenant_id=tenant_id,
            user_id=uid,
            notification_type=NOTIFICATION_TYPE,
            title=title,
            body=body,
            notification_metadata=metadata,
        )
        db.add(notif)
        inserted += 1

    db.flush()
    if logger is not None:
        logger.info(
            "Group-resource notifications staged",
            context={
                "tenant_id": tenant_id,
                "resource_kind": resource_kind,
                "resource_id": resource_id,
                "action": action,
                "group_count": len(group_id_list),
                "notified_users": inserted,
            },
        )
    return inserted
