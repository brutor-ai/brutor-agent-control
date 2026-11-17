# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
LLM Model Access Service 
Handles group-based access control with hierarchical inheritance
"""

from typing import List, Dict, Any, Optional
from sqlalchemy.orm import Session
from sqlalchemy import and_

from app.constants.groups import ResourceGroupName
from app.database.models.llm_model import LLModel
from app.database.models.resource_group import ResourceGroup, resource_group_llm_models
from app.database.models.resource_group_membership import ResourceGroupMembership

# Sentinel value the frontend sends for the Default group.
# The actual Default group has a ULID-based ID in the database;
# this constant is only used to detect and resolve it.
DEFAULT_GROUP_SENTINEL = "default"


class LLMAccessService:
    """
    Service for managing LLM model access based on group memberships

    SUPPORTS HIERARCHICAL INHERITANCE:
    - If parent group has a model, all child groups inherit it
    - Uses ResourceGroup.get_effective_llm_models() for inheritance
    """

    @staticmethod
    def resolve_default_group_id(db: Session, tenant_id: str) -> str:
        """
        Look up the real Default group ID for a tenant.

        The Default group is the system group (is_system=True, name='Default')
        that every model must belong to.  The frontend may send the sentinel
        value 'default' instead of the real ULID; this method resolves it.

        Args:
            db: Database session
            tenant_id: Tenant ID

        Returns:
            The real Default group UUID/ULID

        Raises:
            ValueError: If no Default group exists for the tenant
        """
        default_group = db.query(ResourceGroup).filter(
            and_(
                ResourceGroup.tenant_id == tenant_id,
                ResourceGroup.name == ResourceGroupName.DEFAULT.value,
                ResourceGroup.is_system == True,
            )
        ).first()

        if not default_group:
            raise ValueError(
                f"Default system group not found for tenant {tenant_id}. "
                f"This is a critical configuration error — the {ResourceGroupName.DEFAULT.value} group "
                "should have been created during tenant initialisation."
            )

        return default_group.id

    @staticmethod
    def resolve_group_ids(
            db: Session,
            group_ids: List[str],
            tenant_id: str
    ) -> List[str]:
        """
        Resolve a list of group IDs, replacing the 'default' sentinel with
        the real Default group UUID. Respects the caller's selection — does
        NOT force the Default group into the result if the caller omitted
        it. Admins who want restricted access should be able to assign a
        model to a specific group only (matches MCP server-config behavior).

        Seed paths that want Default coverage pass the sentinel (or the real
        id) explicitly.

        Args:
            db: Database session
            group_ids: Raw group IDs (may contain sentinel 'default')
            tenant_id: Tenant ID

        Returns:
            Deduplicated list of real group UUIDs, in input order.
        """
        real_default_id = LLMAccessService.resolve_default_group_id(db, tenant_id)

        resolved: List[str] = []
        seen = set()
        for gid in group_ids:
            target = real_default_id if gid == DEFAULT_GROUP_SENTINEL else gid
            if target not in seen:
                resolved.append(target)
                seen.add(target)

        return resolved

    @staticmethod
    def get_user_group_ids(db: Session, user_id: str, tenant_id: str) -> List[str]:
        """
        Get all Resource Group IDs a user can reach.

        Two access edges are unioned:
          1. Direct user→RG memberships (`resource_group_memberships`).
             The pre-#663 path; covers the "admin manually added user
             to RG" case.
          2. Indirect user→EndUserGroup→RG (via
             `end_user_group_members` ∘ `resource_group_end_user_groups`).
             Lets operators grant RG access by group membership.

        Both are tenant-scoped to prevent cross-tenant leaks.
        Duplicate group ids (user reachable via both edges) are
        de-duplicated; order is not stable.
        """
        # Local imports avoid a top-level import cycle with
        # end_user_group.py (which imports nothing from this module,
        # but the import graph through the models package can be
        # touchy during partial reloads).
        from app.database.models.end_user_group import (
            end_user_group_members,
            resource_group_end_user_groups,
        )

        direct = db.query(ResourceGroupMembership.group_id).filter(
            and_(
                ResourceGroupMembership.tenant_id == tenant_id,
                ResourceGroupMembership.user_id == user_id,
            )
        )

        # Indirect via EndUserGroup. The join chain is:
        #   end_user_group_members  (user → user_group)
        #     → resource_group_end_user_groups  (user_group → RG)
        # Tenant scoping is enforced via the ResourceGroup row so
        # we don't have to trust that membership rows can't span
        # tenants (defence in depth).
        indirect = (
            db.query(ResourceGroup.id)
            .join(
                resource_group_end_user_groups,
                resource_group_end_user_groups.c.resource_group_id
                == ResourceGroup.id,
            )
            .join(
                end_user_group_members,
                end_user_group_members.c.end_user_group_id
                == resource_group_end_user_groups.c.end_user_group_id,
            )
            .filter(
                ResourceGroup.tenant_id == tenant_id,
                end_user_group_members.c.end_user_id == user_id,
            )
        )

        ids = {row[0] for row in direct.all()}
        ids.update(row[0] for row in indirect.all())
        return list(ids)

    @staticmethod
    def get_user_groups(db: Session, user_id: str, tenant_id: str) -> List[ResourceGroup]:
        """
        Get all ResourceGroup objects a user belongs to

        Args:
            db: Database session
            user_id: End user ID
            tenant_id: Tenant ID

        Returns:
            List of ResourceGroup instances
        """
        group_ids = LLMAccessService.get_user_group_ids(db, user_id, tenant_id)

        if not group_ids:
            return []

        return db.query(ResourceGroup).filter(
            and_(
                ResourceGroup.tenant_id == tenant_id,
                ResourceGroup.id.in_(group_ids)
            )
        ).all()

    @staticmethod
    def get_accessible_models_via_api_key(
            db: Session,
            tenant_id: str,
            api_key_group_id: Optional[str] = None,
            api_key_user_id: Optional[int] = None,
            enabled_only: bool = True,
            healthy_only: bool = True
    ) -> List[LLModel]:
        """
        Get LLM models accessible via an API key

        API keys can be:
        1. Group-scoped: Access to models in that Resource Group (+ inherited from ancestors)
        2. User-scoped: Access to models via user's group memberships
        3. Global: Access to all models (if no group or user specified)

        All models are assigned to resource groups (at minimum the 'Default' group).

        Args:
            db: Database session
            tenant_id: Tenant ID
            api_key_group_id: Resource Group ID from API key (if group-scoped)
            api_key_user_id: User ID from API key (if user-scoped, integer!)
            enabled_only: Only return enabled models
            healthy_only: Only return healthy models

        Returns:
            List of accessible LLModel instances
        """
        user_groups = []

        # Path 1: API key has a group_id (most common for enterprise keys)
        if api_key_group_id:
            # Get the Resource Group
            group = db.query(ResourceGroup).filter(
                and_(
                    ResourceGroup.tenant_id == tenant_id,
                    ResourceGroup.id == api_key_group_id
                )
            ).first()

            if group:
                user_groups = [group]

        # Path 2: API key has a user_id (user-specific key)
        elif api_key_user_id:
            # Use normal user-based access control (convert int to str for compatibility)
            user_groups = LLMAccessService.get_user_groups(db, str(api_key_user_id), tenant_id)

        # Path 3: Neither group nor user (truly global key) - get Default group models
        # With the new system, a global key should use the Default group

        # Collect all accessible models using hierarchical inheritance
        accessible_models_dict = {}  # Use dict to avoid duplicates

        # Get models from each group (includes inherited from parents)
        for group in user_groups:
            effective_models = group.get_effective_llm_models()
            for model in effective_models:
                accessible_models_dict[model.id] = model

        # Convert dict back to list
        accessible_models = list(accessible_models_dict.values())

        # Apply filters
        filtered_models = []
        for model in accessible_models:
            # Filter by enabled status
            if enabled_only and not model.enabled:
                continue

            # Filter by health status
            if healthy_only and hasattr(model, 'health_status') and model.health_status != 'healthy':
                continue

            filtered_models.append(model)

        return filtered_models

    @staticmethod
    def check_api_key_access(
            db: Session,
            tenant_id: str,
            model_id: str,
            api_key_group_id: Optional[str] = None,
            api_key_user_id: Optional[int] = None
    ) -> bool:
        """
        Check if an API key has access to a specific model

        SUPPORTS HIERARCHICAL INHERITANCE:
        - Checks if API key's group or any ancestor groups have the model

        Args:
            db: Database session
            tenant_id: Tenant ID
            model_id: Model ID
            api_key_group_id: Resource Group ID from API key (if group-scoped)
            api_key_user_id: User ID from API key (if user-scoped)

        Returns:
            True if API key can access the model
        """
        # Get model
        model = db.query(LLModel).filter(
            and_(
                LLModel.tenant_id == tenant_id,
                LLModel.id == model_id
            )
        ).first()

        if not model or not model.enabled:
            return False

        # Path 1: API key has a group_id
        if api_key_group_id:
            # Get the Resource Group
            key_group = db.query(ResourceGroup).filter(
                and_(
                    ResourceGroup.tenant_id == tenant_id,
                    ResourceGroup.id == api_key_group_id
                )
            ).first()

            if key_group:
                # Check if any of key's group effective models include this model
                effective_models = key_group.get_effective_llm_models()
                if model in effective_models:
                    return True

        # Path 2: API key has a user_id
        elif api_key_user_id:
            # Use normal user-based access control
            return LLMAccessService.check_user_access(
                db=db,
                tenant_id=tenant_id,
                user_id=str(api_key_user_id),
                model_id=model_id
            )

        return False

    @staticmethod
    def get_accessible_models(
            db: Session,
            tenant_id: str,
            user_id: Optional[str] = None,
            user_group_ids: Optional[List[str]] = None,
            enabled_only: bool = True,
            healthy_only: bool = True
    ) -> List[LLModel]:
        """
        Get LLM models accessible to a user based on group memberships

        SUPPORTS HIERARCHICAL INHERITANCE:
        - Uses ResourceGroup.get_effective_llm_models() which includes inherited models
        - If user is in child group, they get parent group's models too

        All models are assigned to resource groups (at minimum the 'Default' group). Users access
        models through their group memberships. All users belong to the Default
        group, which replaces the old "global/no-group" concept.

        Args:
            db: Database session
            tenant_id: Tenant ID
            user_id: User ID (will lookup groups if user_group_ids not provided)
            user_group_ids: Pre-fetched user group IDs (optimization)
            enabled_only: Only return enabled models
            healthy_only: Only return healthy models (with health_status='healthy')

        Returns:
            List of accessible LLModel instances
        """
        # Get user's groups if not provided
        if user_id and not user_group_ids:
            user_groups = LLMAccessService.get_user_groups(db, user_id, tenant_id)
        elif user_group_ids:
            user_groups = db.query(ResourceGroup).filter(
                and_(
                    ResourceGroup.tenant_id == tenant_id,
                    ResourceGroup.id.in_(user_group_ids)
                )
            ).all()
        else:
            user_groups = []

        # Collect all accessible models using hierarchical inheritance
        accessible_models_dict = {}  # Use dict to avoid duplicates

        # Get models from each group (includes inherited from parents)
        # The Default group ensures all default models are accessible to all users
        for group in user_groups:
            effective_models = group.get_effective_llm_models()
            for model in effective_models:
                accessible_models_dict[model.id] = model

        # Convert dict back to list
        accessible_models = list(accessible_models_dict.values())

        # Apply filters
        filtered_models = []
        for model in accessible_models:
            # Filter by enabled status
            if enabled_only and not model.enabled:
                continue

            # Filter by health status
            if healthy_only and hasattr(model, 'health_status') and model.health_status != 'healthy':
                continue

            filtered_models.append(model)

        return filtered_models

    @staticmethod
    def get_accessible_models_with_inheritance_info(
            db: Session,
            tenant_id: str,
            user_id: str,
            enabled_only: bool = True,
            healthy_only: bool = True
    ) -> List[Dict[str, Any]]:
        """
        Get accessible models with information about how they were inherited

        Returns models with metadata showing:
        - Which group(s) provide access
        - Whether access is direct or inherited
        - The inheritance path

        Args:
            db: Database session
            tenant_id: Tenant ID
            user_id: User ID
            enabled_only: Only return enabled models
            healthy_only: Only return healthy models

        Returns:
            List of dicts with model and inheritance info
        """
        user_groups = LLMAccessService.get_user_groups(db, user_id, tenant_id)

        # Track which groups provide access to which models
        model_access_info: Dict[str, Dict[str, Any]] = {}

        for group in user_groups:
            # Get effective models for this group
            effective_models = group.get_effective_llm_models()
            # `group.llm_models` is `lazy="dynamic"` on the
            # ResourceGroup side (asymmetric with the eager
            # `model.resource_groups` side — see resource_group.py).
            # `.all()` is correct here.
            direct_model_ids = {m.id for m in group.llm_models.all()}

            for model in effective_models:
                if model.id not in model_access_info:
                    model_access_info[model.id] = {
                        'model': model,
                        'access_via_groups': [],
                        'is_inherited': False,
                        'inheritance_sources': []
                    }

                # Determine if this is direct or inherited access
                is_direct = model.id in direct_model_ids

                access_info = {
                    'group_id': group.id,
                    'group_name': group.name,
                    'is_direct': is_direct
                }

                if not is_direct:
                    # This model is inherited - find which ancestor provides it
                    for ancestor in group.get_ancestors():
                        ancestor_model_ids = {m.id for m in ancestor.llm_models.all()}
                        if model.id in ancestor_model_ids:
                            access_info['inherited_from'] = {
                                'group_id': ancestor.id,
                                'group_name': ancestor.name
                            }
                            model_access_info[model.id]['is_inherited'] = True
                            break

                model_access_info[model.id]['access_via_groups'].append(access_info)

        # Convert to list and apply filters
        result = []
        for info in model_access_info.values():
            model = info['model']

            # Apply filters
            if enabled_only and not model.enabled:
                continue
            if healthy_only and hasattr(model, 'health_status') and model.health_status != 'healthy':
                continue

            result.append(info)

        return result

    @staticmethod
    def get_models_by_group(
            db: Session,
            tenant_id: str,
            enabled_only: bool = True
    ) -> Dict[str, List[LLModel]]:
        """
        Get models organized by group (direct assignments only)

        Args:
            db: Database session
            tenant_id: Tenant ID
            enabled_only: Only include enabled models

        Returns:
            Dictionary mapping group_id to list of models
        """
        query = db.query(LLModel).filter(LLModel.tenant_id == tenant_id)

        if enabled_only:
            query = query.filter(LLModel.enabled == True)

        all_models = query.all()

        result: Dict[str, List[LLModel]] = {}

        for model in all_models:
            # `model.resource_groups` is a plain list (eager-loaded);
            # `.all()` was a leftover from the dynamic-relationship era.
            model_groups = model.resource_groups if hasattr(model, 'resource_groups') else []

            for group in model_groups:
                if group.id not in result:
                    result[group.id] = []
                result[group.id].append(model)

        return result


    @staticmethod
    def check_user_access(
            db: Session,
            tenant_id: str,
            user_id: str,
            model_id: str
    ) -> bool:
        """
        Check if user has access to a specific model

        SUPPORTS HIERARCHICAL INHERITANCE:
        - Checks if user's groups or any ancestor groups have the model

        Args:
            db: Database session
            tenant_id: Tenant ID
            user_id: User ID
            model_id: Model ID

        Returns:
            True if user can access the model
        """
        # Get model
        model = db.query(LLModel).filter(
            and_(
                LLModel.tenant_id == tenant_id,
                LLModel.id == model_id
            )
        ).first()

        if not model or not model.enabled:
            return False

        # Get user's groups
        user_groups = LLMAccessService.get_user_groups(db, user_id, tenant_id)

        # Check if any of user's groups have access (includes inheritance)
        for user_group in user_groups:
            effective_models = user_group.get_effective_llm_models()
            if model in effective_models:
                return True

        return False

    @staticmethod
    def assign_model_to_groups(
            db: Session,
            model_id: str,
            group_ids: List[str],
            tenant_id: str
    ) -> None:
        """
        Set the model's group bindings to exactly `group_ids` (replace-set).

        Implemented as a DIFF against the current bindings, not delete-all +
        re-insert: groups that remain bound are left untouched so their
        per-binding `limits` and `portal_visible` survive a model edit.
        (Delete+insert-bare silently wiped both columns whenever a save
        included group_ids.) Only removed groups are deleted and only added
        groups are inserted (new rows take column defaults: limits NULL,
        portal_visible = true).

        The sentinel value 'default' is resolved to the real Default group
        UUID. The Default group is NOT force-added — the resulting set is
        exactly what the caller passed (see resolve_group_ids). Child groups
        automatically inherit via get_effective_llm_models().

        Args:
            db: Database session
            model_id: Model ID
            group_ids: List of group IDs to assign (may contain 'default' sentinel)
            tenant_id: Tenant ID
        """
        # Get model
        model = db.query(LLModel).filter(
            and_(
                LLModel.tenant_id == tenant_id,
                LLModel.id == model_id
            )
        ).first()

        if not model:
            raise ValueError(f"Model {model_id} not found")

        # Resolve sentinel values ('default' → real Default group UUID).
        # resolve_group_ids does NOT force the Default group, so the target
        # set is exactly what the caller asked for.
        resolved_ids = LLMAccessService.resolve_group_ids(db, group_ids, tenant_id)
        target_ids = set(resolved_ids)

        # Validate every target group exists BEFORE mutating, so a bad id
        # fails the whole operation rather than half-applying it.
        for group_id in target_ids:
            group = db.query(ResourceGroup).filter(
                and_(
                    ResourceGroup.tenant_id == tenant_id,
                    ResourceGroup.id == group_id
                )
            ).first()
            if not group:
                raise ValueError(f"Group {group_id} not found")

        # Diff against current bindings instead of delete-all + re-insert, so
        # bindings that stay keep their per-binding `limits` and
        # `portal_visible` (the old approach wiped both on every edit).
        existing_ids = {
            row[0]
            for row in db.query(resource_group_llm_models.c.resource_group_id)
            .filter(resource_group_llm_models.c.llm_model_id == model_id)
            .all()
        }

        # Remove only the bindings no longer targeted.
        to_remove = existing_ids - target_ids
        if to_remove:
            db.query(resource_group_llm_models).filter(
                and_(
                    resource_group_llm_models.c.llm_model_id == model_id,
                    resource_group_llm_models.c.resource_group_id.in_(list(to_remove)),
                )
            ).delete(synchronize_session=False)

        # Insert only the newly-targeted bindings. New rows take column
        # defaults: limits = NULL, portal_visible = true (server default).
        to_add = target_ids - existing_ids
        for group_id in to_add:
            db.execute(
                resource_group_llm_models.insert().values(
                    resource_group_id=group_id,
                    llm_model_id=model_id
                )
            )

        db.flush()

    @staticmethod
    def add_model_to_groups(
            db: Session,
            model_id: str,
            group_ids: List[str],
            tenant_id: str
    ) -> None:
        """
        Add model to groups (keeps existing assignments)

        Resolves the 'default' sentinel to the real Default group UUID.

        Args:
            db: Database session
            model_id: Model ID
            group_ids: List of group IDs to add (may contain 'default' sentinel)
            tenant_id: Tenant ID
        """
        model = db.query(LLModel).filter(
            and_(
                LLModel.tenant_id == tenant_id,
                LLModel.id == model_id
            )
        ).first()

        if not model:
            raise ValueError(f"Model {model_id} not found")

        # Resolve sentinel values
        resolved_ids = LLMAccessService.resolve_group_ids(db, group_ids, tenant_id)

        # Get existing group IDs (plain-list relationship after the
        # dynamic→eager flip — no `.all()`).
        existing_groups = {g.id for g in model.resource_groups} if hasattr(model, 'resource_groups') else set()

        # Add new groups
        for group_id in resolved_ids:
            if group_id not in existing_groups:
                # Verify group exists
                group = db.query(ResourceGroup).filter(
                    and_(
                        ResourceGroup.tenant_id == tenant_id,
                        ResourceGroup.id == group_id
                    )
                ).first()

                if not group:
                    raise ValueError(f"Group {group_id} not found")

                # Add assignment using the correct association table
                db.execute(
                    resource_group_llm_models.insert().values(
                        resource_group_id=group_id,
                        llm_model_id=model_id
                    )
                )

        db.flush()

    @staticmethod
    def remove_model_from_groups(
            db: Session,
            model_id: str,
            group_ids: List[str],
            tenant_id: str
    ) -> None:
        """
        Remove model from groups.

        The Default (system) group cannot be removed — it is silently
        filtered out to prevent models from becoming inaccessible.

        Args:
            db: Database session
            model_id: Model ID
            group_ids: List of group IDs to remove
            tenant_id: Tenant ID
        """
        # Resolve the Default group ID so we can protect it
        try:
            real_default_id = LLMAccessService.resolve_default_group_id(db, tenant_id)
        except ValueError:
            real_default_id = None

        # Filter out Default group and sentinel — never allow removal
        safe_ids = [
            gid for gid in group_ids
            if gid != DEFAULT_GROUP_SENTINEL and gid != real_default_id
        ]

        if not safe_ids:
            return  # Nothing to remove after filtering

        # Use the correct association table
        db.query(resource_group_llm_models).filter(
            and_(
                resource_group_llm_models.c.llm_model_id == model_id,
                resource_group_llm_models.c.resource_group_id.in_(safe_ids)
            )
        ).delete(synchronize_session=False)

        db.flush()

    @staticmethod
    def get_group_models(
            db: Session,
            group_id: str,
            tenant_id: str,
            enabled_only: bool = True,
            include_inherited: bool = False
    ) -> List[LLModel]:
        """
        Get all models assigned to a specific group

        Args:
            db: Database session
            group_id: Group ID
            tenant_id: Tenant ID
            enabled_only: Only include enabled models
            include_inherited: If True, include models inherited from ancestors

        Returns:
            List of LLModel instances
        """
        # Get the group
        group = db.query(ResourceGroup).filter(
            and_(
                ResourceGroup.tenant_id == tenant_id,
                ResourceGroup.id == group_id
            )
        ).first()

        if not group:
            return []

        # Get effective models (includes inherited if requested)
        if include_inherited:
            models = group.get_effective_llm_models()
        else:
            # Direct assignments only — `ResourceGroup.llm_models`
            # is `lazy="dynamic"` so `.all()` is the right call.
            models = group.llm_models.all()

        # Apply filters
        if enabled_only:
            models = [m for m in models if m.enabled]

        return models