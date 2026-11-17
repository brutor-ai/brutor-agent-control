# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# EndUserGroup service

Business logic for the EndUserGroup entity: administrators create and edit
user groups by hand in the Admin Console, with source='manual', the only
source in this release (SAML single sign-on and SCIM provisioning, which
created groups of their own, are not in it). The columns those wrote
(`source_external_id`, `idp_server_id`) are off-limits here.

## What lives here vs. elsewhere

  * CRUD + membership + RG binding → here.
  * Access resolution (does user X have access to RG Y) →
    `LLMAccessService` + Rust `usage.rs`. Both walk the
    `user_groups → resource_groups` edge; the walk logic does NOT live
    in this manager.
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session, selectinload
from ulid import ULID

from app.core.logging_system import BrutorLogger
from app.database.models.end_user import EndUser
from app.database.models.end_user_group import (
    EndUserGroup,
    end_user_group_members,
    resource_group_end_user_groups,
)
from app.database.models.resource_group import ResourceGroup


# Valid source enum (also enforced at the model layer; duplicating
# here keeps the manager free of import-order fragility).
VALID_SOURCES = frozenset({"manual"})


class EndUserGroupError(Exception):
    """Manager-level error. Caught by the endpoint layer and
    translated into HTTP 4xx responses with a stable error code."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


# ---------------------------------------------------------------------------


class EndUserGroupManager:
    """CRUD and membership for EndUserGroup."""

    def __init__(self, logger: Optional[BrutorLogger] = None):
        self.logger = logger or BrutorLogger("end-user-group-manager")

    # =====================================================================
    # CRUD
    # =====================================================================

    def create_manual(
        self,
        db: Session,
        *,
        tenant_id: str,
        name: str,
        display_name: Optional[str] = None,
        description: Optional[str] = None,
        initial_member_ids: Optional[Sequence[int]] = None,
    ) -> EndUserGroup:
        """Create a manual (admin-console-sourced) group.

        Reserved for the admin-console endpoint.
        """
        if not tenant_id:
            raise ValueError("tenant_id is required")
        name = (name or "").strip()
        if not name:
            raise EndUserGroupError(
                "invalid_name", "name must not be empty"
            )

        # Pre-check the (tenant_id, name) uniqueness so we can return
        # a tidy 409 instead of an opaque IntegrityError. The DB
        # constraint is still the authority — a concurrent create with
        # the same name will hit it.
        existing = db.execute(
            select(EndUserGroup.id).where(
                EndUserGroup.tenant_id == tenant_id,
                EndUserGroup.name == name,
            )
        ).first()
        if existing:
            raise EndUserGroupError(
                "name_conflict",
                f"EndUserGroup '{name}' already exists in tenant",
            )

        group = EndUserGroup(
            id=f"eug-{ULID()}",
            tenant_id=tenant_id,
            name=name,
            display_name=display_name,
            description=description,
            source="manual",
        )
        db.add(group)
        db.flush()

        if initial_member_ids:
            self._attach_members(
                db,
                group=group,
                end_user_ids=initial_member_ids,
                source="manual",
            )

        self.logger.info(
            f"Created manual EndUserGroup {group.id} "
            f"({name}) in tenant {tenant_id} "
            f"with {len(initial_member_ids or [])} initial members"
        )
        return group

    def get(
        self,
        db: Session,
        *,
        tenant_id: str,
        group_id: str,
        with_members: bool = False,
        with_resource_groups: bool = False,
    ) -> Optional[EndUserGroup]:
        """Tenant-scoped fetch. Returns None when missing.

        `with_members` / `with_resource_groups` switch on selectinload
        for the detail endpoint; list endpoints leave them off to
        keep payload size bounded.
        """
        stmt = select(EndUserGroup).where(
            EndUserGroup.id == group_id,
            EndUserGroup.tenant_id == tenant_id,
        )
        if with_members:
            stmt = stmt.options(selectinload(EndUserGroup.members))
        if with_resource_groups:
            stmt = stmt.options(selectinload(EndUserGroup.resource_groups))
        return db.execute(stmt).scalar_one_or_none()

    def list(
        self,
        db: Session,
        *,
        tenant_id: str,
        source: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[EndUserGroup], int]:
        """Paginated list. Returns (rows, total_count).

        `source` filters on provenance ('manual', the only one in this release).
        `search` is a case-insensitive substring against name +
        display_name.
        """
        where = [EndUserGroup.tenant_id == tenant_id]
        if source is not None:
            if source not in VALID_SOURCES:
                raise EndUserGroupError(
                    "invalid_source_filter",
                    f"source must be one of {sorted(VALID_SOURCES)}",
                )
            where.append(EndUserGroup.source == source)
        if search:
            needle = f"%{search.lower()}%"
            where.append(
                func.lower(EndUserGroup.name).like(needle)
                | func.lower(
                    func.coalesce(EndUserGroup.display_name, "")
                ).like(needle)
            )

        total = db.execute(
            select(func.count())
            .select_from(EndUserGroup)
            .where(and_(*where))
        ).scalar_one()

        rows = db.execute(
            select(EndUserGroup)
            .where(and_(*where))
            .order_by(EndUserGroup.name.asc())
            .limit(limit)
            .offset(offset)
        ).scalars().all()
        return list(rows), int(total or 0)

    def update(
        self,
        db: Session,
        *,
        tenant_id: str,
        group_id: str,
        name: Optional[str] = None,
        display_name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> EndUserGroup:
        """Partial update of operator-controllable fields.

        Refuses to rename a group that was not created by an
        administrator (a stored row of another source).
        """
        group = self.get(db, tenant_id=tenant_id, group_id=group_id)
        if group is None:
            raise EndUserGroupError(
                "not_found",
                f"EndUserGroup {group_id!r} not found in tenant",
            )
        if name is not None and name != group.name:
            if group.source != "manual":
                raise EndUserGroupError(
                    "rename_not_allowed",
                    f"Cannot rename {group.source}-sourced group "
                    f"{group_id!r}: only groups created by an administrator "
                    f"can be renamed.",
                )
            name_stripped = name.strip()
            if not name_stripped:
                raise EndUserGroupError(
                    "invalid_name", "name must not be empty"
                )
            collision = db.execute(
                select(EndUserGroup.id).where(
                    EndUserGroup.tenant_id == tenant_id,
                    EndUserGroup.name == name_stripped,
                    EndUserGroup.id != group_id,
                )
            ).first()
            if collision:
                raise EndUserGroupError(
                    "name_conflict",
                    f"EndUserGroup '{name_stripped}' already exists",
                )
            group.name = name_stripped
        if display_name is not None:
            group.display_name = display_name
        if description is not None:
            group.description = description
        db.flush()
        return group

    def delete(
        self,
        db: Session,
        *,
        tenant_id: str,
        group_id: str,
    ) -> None:
        """Hard-delete. M2M rows on `end_user_group_members` and
        `resource_group_end_user_groups` cascade away via the FK
        ondelete=CASCADE rules of the foreign keys.
        """
        group = self.get(db, tenant_id=tenant_id, group_id=group_id)
        if group is None:
            # Idempotent — repeated deletes are not an error.
            return
        db.delete(group)
        db.flush()
        self.logger.info(
            f"Deleted EndUserGroup {group_id} (source={group.source}) "
            f"in tenant {tenant_id}"
        )

    # =====================================================================
    # Membership mutation
    # =====================================================================

    def add_members(
        self,
        db: Session,
        *,
        tenant_id: str,
        group_id: str,
        end_user_ids: Sequence[int],
        source: str = "manual",
    ) -> EndUserGroup:
        """Idempotent additive change. Users already in the group
        are skipped silently."""
        group = self._require_group(db, tenant_id, group_id)
        self._attach_members(
            db,
            group=group,
            end_user_ids=end_user_ids,
            source=source,
        )
        db.flush()
        db.refresh(group)
        return group

    def remove_members(
        self,
        db: Session,
        *,
        tenant_id: str,
        group_id: str,
        end_user_ids: Sequence[int],
    ) -> EndUserGroup:
        """Idempotent subtractive change. Users not in the group
        are silently skipped."""
        group = self._require_group(db, tenant_id, group_id)
        ids = list({int(uid) for uid in end_user_ids})
        if not ids:
            return group
        db.execute(
            end_user_group_members.delete().where(
                end_user_group_members.c.end_user_group_id == group.id,
                end_user_group_members.c.end_user_id.in_(ids),
            )
        )
        db.flush()
        db.refresh(group)
        return group

    def set_members(
        self,
        db: Session,
        *,
        tenant_id: str,
        group_id: str,
        end_user_ids: Sequence[int],
        source: str = "manual",
    ) -> EndUserGroup:
        """Authoritative roster replace. Members not in the list are
        removed; members in the list are added. Used by the UI's
        multi-select-and-save flow.

        """
        group = self._require_group(db, tenant_id, group_id)
        desired = {int(uid) for uid in end_user_ids}

        # Validate the supplied users belong to this tenant. A
        # cross-tenant uid would otherwise sneak in via a malicious
        # admin payload.
        if desired:
            tenant_check = db.execute(
                select(EndUser.id).where(
                    EndUser.id.in_(desired),
                    EndUser.tenant_id == tenant_id,
                )
            ).scalars().all()
            if len(tenant_check) != len(desired):
                bad = desired - {int(x) for x in tenant_check}
                raise EndUserGroupError(
                    "cross_tenant_user",
                    f"EndUser ids not in tenant: {sorted(bad)}",
                )

        current = set(
            db.execute(
                select(end_user_group_members.c.end_user_id).where(
                    end_user_group_members.c.end_user_group_id == group.id
                )
            ).scalars().all()
        )

        to_add = desired - current
        to_remove = current - desired

        if to_remove:
            db.execute(
                end_user_group_members.delete().where(
                    end_user_group_members.c.end_user_group_id == group.id,
                    end_user_group_members.c.end_user_id.in_(to_remove),
                )
            )
        if to_add:
            db.execute(
                end_user_group_members.insert(),
                [
                    {
                        "end_user_group_id": group.id,
                        "end_user_id": uid,
                        "source": source,
                    }
                    for uid in to_add
                ],
            )
        db.flush()
        db.refresh(group)
        return group

    # =====================================================================
    # Resource Group binding
    # =====================================================================

    def bind_to_resource_group(
        self,
        db: Session,
        *,
        tenant_id: str,
        resource_group_id: str,
        end_user_group_ids: Sequence[str],
        created_by: Optional[str] = None,
    ) -> ResourceGroup:
        """Idempotent — already-bound groups are skipped."""
        rg = self._require_resource_group(
            db, tenant_id, resource_group_id
        )
        ug_ids = list({gid for gid in end_user_group_ids})
        if not ug_ids:
            return rg

        # Validate the user-groups belong to the same tenant.
        valid = set(
            db.execute(
                select(EndUserGroup.id).where(
                    EndUserGroup.id.in_(ug_ids),
                    EndUserGroup.tenant_id == tenant_id,
                )
            ).scalars().all()
        )
        bad = [gid for gid in ug_ids if gid not in valid]
        if bad:
            raise EndUserGroupError(
                "cross_tenant_group",
                f"EndUserGroup ids not in tenant: {bad}",
            )

        existing = set(
            db.execute(
                select(
                    resource_group_end_user_groups.c.end_user_group_id
                ).where(
                    resource_group_end_user_groups.c.resource_group_id == rg.id
                )
            ).scalars().all()
        )
        to_add = [gid for gid in ug_ids if gid not in existing]
        if to_add:
            db.execute(
                resource_group_end_user_groups.insert(),
                [
                    {
                        "resource_group_id": rg.id,
                        "end_user_group_id": gid,
                        "created_by": created_by,
                    }
                    for gid in to_add
                ],
            )
        db.flush()
        db.refresh(rg)
        return rg

    def unbind_from_resource_group(
        self,
        db: Session,
        *,
        tenant_id: str,
        resource_group_id: str,
        end_user_group_ids: Sequence[str],
    ) -> ResourceGroup:
        """Idempotent — bindings that don't exist are skipped."""
        rg = self._require_resource_group(
            db, tenant_id, resource_group_id
        )
        ug_ids = list({gid for gid in end_user_group_ids})
        if not ug_ids:
            return rg
        db.execute(
            resource_group_end_user_groups.delete().where(
                resource_group_end_user_groups.c.resource_group_id == rg.id,
                resource_group_end_user_groups.c.end_user_group_id.in_(ug_ids),
            )
        )
        db.flush()
        db.refresh(rg)
        return rg

    # =====================================================================
    # Internals
    # =====================================================================

    def _require_group(
        self,
        db: Session,
        tenant_id: str,
        group_id: str,
    ) -> EndUserGroup:
        group = self.get(db, tenant_id=tenant_id, group_id=group_id)
        if group is None:
            raise EndUserGroupError(
                "not_found",
                f"EndUserGroup {group_id!r} not found in tenant",
            )
        return group

    def _require_resource_group(
        self,
        db: Session,
        tenant_id: str,
        resource_group_id: str,
    ) -> ResourceGroup:
        rg = db.execute(
            select(ResourceGroup).where(
                ResourceGroup.id == resource_group_id,
                ResourceGroup.tenant_id == tenant_id,
            )
        ).scalar_one_or_none()
        if rg is None:
            raise EndUserGroupError(
                "not_found",
                f"ResourceGroup {resource_group_id!r} not found in tenant",
            )
        return rg

    def _attach_members(
        self,
        db: Session,
        *,
        group: EndUserGroup,
        end_user_ids: Sequence[int],
        source: str,
    ) -> None:
        """Insert new (group, user) rows, skipping duplicates.

        Validates every uid against the group's tenant so a malicious
        payload can't add a cross-tenant user."""
        ids = list({int(uid) for uid in end_user_ids})
        if not ids:
            return

        valid = set(
            db.execute(
                select(EndUser.id).where(
                    EndUser.id.in_(ids),
                    EndUser.tenant_id == group.tenant_id,
                )
            ).scalars().all()
        )
        bad = [uid for uid in ids if uid not in valid]
        if bad:
            raise EndUserGroupError(
                "cross_tenant_user",
                f"EndUser ids not in tenant {group.tenant_id!r}: {bad}",
            )

        existing = set(
            db.execute(
                select(end_user_group_members.c.end_user_id).where(
                    end_user_group_members.c.end_user_group_id == group.id,
                    end_user_group_members.c.end_user_id.in_(valid),
                )
            ).scalars().all()
        )
        to_insert = [uid for uid in valid if uid not in existing]
        if not to_insert:
            return
        db.execute(
            end_user_group_members.insert(),
            [
                {
                    "end_user_group_id": group.id,
                    "end_user_id": uid,
                    "source": source,
                }
                for uid in to_insert
            ],
        )


__all__ = [
    "EndUserGroupManager",
    "EndUserGroupError",
    "VALID_SOURCES",
]
