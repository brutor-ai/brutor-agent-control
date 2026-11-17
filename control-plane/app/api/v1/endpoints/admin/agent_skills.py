# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Agent Skills Admin Endpoints
Multi-Tenant with RBAC and Audit Logging

Manages the Agent Skills lifecycle:
- CRUD for skills (create, read, update, delete)
- Lifecycle transitions (validate, publish, deprecate, archive)
- Group assignments (via resource_group_agent_skills association table)
- Skill catalog (lightweight discovery)
- Execution history
- Search and filtering

Access control:
- Skills are assigned to Resource Groups via resource_group_agent_skills
  (same pattern as LLM models and server configs).
- Governance (limits, approval, execution mode) lives on the ResourceGroup.

Follows the same patterns as LLM Models admin endpoints:
- Factory function for router creation
- Dependency injection for services
- require_permission() for RBAC
- require_tenant_resource_access() for multi-tenancy
- UnifiedAdminAuditLogger for audit trail
"""

from app.constants import agent_skill as skill_constants
import hashlib
import io
import re
import zipfile
from ulid import ULID
from datetime import datetime, timezone
from typing import Union, Optional, List

from fastapi import APIRouter, HTTPException, Depends, Query, Request, UploadFile, File, Form
from sqlalchemy.orm import Session

from app.core.sync_bridge import block_on
from app.constants.agent_skill import SkillStatus, SkillResourceType, ScriptLanguage, ScriptExecutionMode, AssetType, SkillDisclosureMode
from app.constants.system_server import AgentSkillServer
from app.constants.tenant import TenantResourceTypes
from app.services.tenant.tenant_service import TenantService
from app.services import agent_skill_size
from app.constants.groups import ResourceGroupName
from app.database import get_db
from app.database.models.agent_skill import AgentSkill, resource_group_agent_skills
from app.database.models.agent_skill_execution import AgentSkillExecution
from app.database.models.agent_skill_resource import AgentSkillResource
from app.database.models.agent_skill_script import AgentSkillScript
from app.database.models.agent_skill_asset import AgentSkillAsset
from app.database.models.resource_group import (
    ResourceGroup,
    resource_group_server_configs,
)
from app.database.models.server_config import ServerConfig
from app.database.models.admin import TenantAdmin
from app.core.admin_user.admin_principal import AdminPrincipal
from app.database.models.resource_mixins import actor_name
from app.core.admin_user.permissions import Permission
from app.core.admin_user.dependencies import (
    require_permission,
    get_audit_logger,
    require_tenant_resource_access
)
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.api.v1.models.admin.agent_skill import (
    is_safe_skill_filename,
    AgentSkillCreate,
    AgentSkillUpdate,
    AgentSkillResponse,
    AgentSkillDetailResponse,
    AgentSkillListResponse,
    AgentSkillCatalogResponse,
    AgentSkillExecutionListResponse,
    AgentSkillExecutionDetailResponse,
    AgentSkillSearchRequest,
    AgentSkillSearchResponse,
    SkillPublishRequest,
    SkillDeprecateRequest,
    SkillValidateRequest,
    SkillBulkDeleteRequest,
    SkillBulkDeleteResponse,
    SkillBulkDeleteSkipped,
    SkillValidationResponse,
    SkillGroupAssignment,
    SkillGroupUnassignment,
    ExecutionSummary,
    AgentSkillGroupInfo,
    AgentSkillResourceCreate,
    AgentSkillResourceUpdate,
    AgentSkillResourceListResponse,
    AgentSkillScriptCreate,
    AgentSkillScriptUpdate,
    AgentSkillScriptListResponse,
    AgentSkillAssetCreate,
    AgentSkillAssetUpdate,
    AgentSkillAssetListResponse,
)
from app.services.mcp.server.system_server_capability_sync import SystemServerCapabilitySync
from app.services.cache_invalidation import invalidate_proxy_cache
from app.services.notifications import notify_group_members_about_resource_change
from app.core.logging_system import BrutorLogger
from app.middleware.trusted_proxies import client_ip


# =============================================================================
# Default group resolution (mirrors LLMAccessService pattern)
# =============================================================================

DEFAULT_GROUP_SENTINEL = "default"


def _resolve_default_group_id(db: Session, tenant_id: str) -> str:
    """Look up the real Default group ID for a tenant."""
    from sqlalchemy import and_
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
            "This is a critical configuration error."
        )
    return default_group.id


def _resolve_group_ids(db: Session, group_ids: list, tenant_id: str) -> list:
    """Resolve group IDs, replacing the 'default' sentinel with the real
    Default group UUID.

    Respects the caller's selection — if the admin unchecks Default in the
    form, Default is NOT silently re-added here. Seed paths that want
    Default coverage pass the sentinel (or the real id) explicitly.
    """
    real_default_id = _resolve_default_group_id(db, tenant_id)

    resolved: list = []
    seen = set()
    for gid in group_ids:
        target = real_default_id if gid == DEFAULT_GROUP_SENTINEL else gid
        if target not in seen:
            resolved.append(target)
            seen.add(target)

    return resolved


def _ensure_agent_skill_server_bound(
    db: Session,
    group_ids: list,
    tenant_id: str,
    created_by: Optional[str] = None,
) -> List[str]:
    """Bind the system Agent Skill MCP server to each group if missing.

    Every Agent Skill is executed via the system MCP server identified
    by `AgentSkillServer.SERVER_ID` (tenant-suffixed). Without that
    server bound to the same Resource Group the skill is assigned to,
    the runtime returns 403 to the portal user when they try to call
    the skill — the failure surfaces far from the admin who caused
    it. We close that gap here: any group that lacks the binding gets
    it added as part of the same assign-skill operation.

    Idempotent — already-bound groups are skipped. Tenant-scoped —
    the system server's id includes the tenant id so cross-tenant
    contamination is impossible. Returns the list of group IDs that
    actually had the binding inserted (i.e. the side effect), so the
    caller can audit-log and surface the change to the admin.
    """
    if not group_ids:
        return []

    # Resolve the system Agent Skill server config for this tenant.
    # Stable per-tenant IDs are produced by `seed_system_servers.py`
    # at tenant-provisioning time.
    config_id = f"{AgentSkillServer.CONFIG_ID}-{tenant_id}"
    config = db.query(ServerConfig).filter(
        ServerConfig.id == config_id,
        ServerConfig.tenant_id == tenant_id,
    ).first()

    if not config:
        # Tenant has not been provisioned with the system server (e.g.
        # very old tenants from before AgentSkillServer existed). Bail
        # out without raising — this is a non-fatal side effect; the
        # assign-skill call should still succeed and the admin will
        # see the 403 the first time they try to use the skill, at
        # which point the provisioning gap becomes visible.
        return []

    # Find groups that already have the binding so we only insert
    # the missing ones. Idempotency matters because the assign-skill
    # flow re-runs the binding on every save.
    already_bound = {
        row[0]
        for row in db.query(resource_group_server_configs.c.resource_group_id)
        .filter(
            resource_group_server_configs.c.server_config_id == config.id,
            resource_group_server_configs.c.resource_group_id.in_(group_ids),
        )
        .all()
    }

    missing = [gid for gid in group_ids if gid not in already_bound]
    if not missing:
        return []

    import uuid
    now = datetime.now(timezone.utc)
    for gid in missing:
        db.execute(
            resource_group_server_configs.insert().values(
                id=str(uuid.uuid4()),
                resource_group_id=gid,
                server_config_id=config.id,
                created_at=now,
                created_by=created_by,
            )
        )

    return missing


def _assign_skill_to_groups(
    db: Session,
    skill_id: str,
    group_ids: list,
    tenant_id: str,
    created_by: Optional[str] = None
) -> dict:
    """
    Assign skill to groups (replaces existing assignments).
    Mirrors LLMAccessService.assign_model_to_groups pattern.

    Returns a dict describing the side effects of the call so callers
    can audit-log + surface them in the API response:
        {
            "assigned_group_ids": [...],           # groups the skill is now bound to
            "auto_bound_server_group_ids": [...],  # subset that also had the
                                                   # system Agent Skill MCP
                                                   # server auto-bound this call
        }
    """
    resolved_ids = _resolve_group_ids(db, group_ids, tenant_id)

    # Validate all group IDs exist in tenant
    existing = db.query(ResourceGroup.id).filter(
        ResourceGroup.id.in_(resolved_ids),
        ResourceGroup.tenant_id == tenant_id
    ).all()
    existing_ids = {row[0] for row in existing}

    invalid = set(resolved_ids) - existing_ids
    if invalid:
        raise ValueError(f"Resource group(s) not found: {', '.join(invalid)}")

    # Remove existing assignments
    db.query(resource_group_agent_skills).filter(
        resource_group_agent_skills.c.agent_skill_id == skill_id
    ).delete(synchronize_session=False)

    # Insert new assignments
    import uuid
    now = datetime.now(timezone.utc)
    for gid in resolved_ids:
        db.execute(
            resource_group_agent_skills.insert().values(
                id=str(uuid.uuid4()),
                resource_group_id=gid,
                agent_skill_id=skill_id,
                created_at=now,
                created_by=created_by
            )
        )

    # Auto-bind the system Agent Skill MCP server to any of these
    # groups that lack it — see `_ensure_agent_skill_server_bound`.
    # Done AFTER the skill binding so a group that didn't exist (and
    # raised) above never gets a stray server binding it doesn't
    # need.
    auto_bound = _ensure_agent_skill_server_bound(
        db=db,
        group_ids=resolved_ids,
        tenant_id=tenant_id,
        created_by=created_by,
    )

    return {
        "assigned_group_ids": resolved_ids,
        "auto_bound_server_group_ids": auto_bound,
    }


# =============================================================================
# Serialization Helpers
# =============================================================================

def _skill_to_response(
    skill: AgentSkill,
    include_content: bool = False,
    include_groups: bool = False,
) -> dict:
    """
    Convert AgentSkill ORM model to API response dict.
    Mirrors LLModelsManager.to_response() pattern.
    """
    data = skill.to_dict(include_content=include_content)

    # Group assignments via resource_group_agent_skills
    try:
        group_list = list(skill.resource_groups)
        data["group_ids"] = [g.id for g in group_list]
    except Exception:
        data["group_ids"] = []

    # Detailed group information
    data["groups"] = None
    if include_groups:
        try:
            data["groups"] = [
                {
                    "id": g.id,
                    "name": g.name,
                    "display_name": g.display_name,
                    "group_type": g.group_type,
                    "is_active": g.is_active,
                    "parent_group_id": g.parent_group_id
                }
                for g in group_list
            ]
        except Exception:
            data["groups"] = None

    return data


def _execution_to_summary(execution: AgentSkillExecution) -> dict:
    """Convert execution to lightweight summary dict."""
    return {
        "id": execution.id,
        "skill_name": execution.skill_name,
        "skill_version": execution.skill_version,
        "execution_status": execution.execution_status.value if hasattr(execution.execution_status, 'value') else execution.execution_status,
        "approval_status": execution.approval_status.value if hasattr(execution.approval_status, 'value') else execution.approval_status,
        "duration_ms": execution.duration_ms,
        "total_tokens": execution.total_tokens,
        "estimated_cost_usd": execution.estimated_cost_usd,
        "error_type": execution.error_type,
        "error_message": execution.error_message,
        "started_at": execution.started_at.isoformat() if execution.started_at else None,
        "completed_at": execution.completed_at.isoformat() if execution.completed_at else None,
    }


# =============================================================================
# Router Factory
# =============================================================================

def create_agent_skills_admin_router(
    logger: BrutorLogger
) -> APIRouter:
    """
    Factory function to create Agent Skills admin router with full tenant isolation.

    Args:
        logger: Logger instance

    Returns:
        Configured APIRouter with all Agent Skill admin endpoints
    """

    router = APIRouter(prefix="/admin/agent-skills", tags=["Agent Skills Admin"])

    capability_sync = SystemServerCapabilitySync(logger=logger)

    # =========================================================================
    # List Skills
    # =========================================================================

    @router.get(
        "",
        response_model=AgentSkillListResponse,
        summary="List Agent Skills",
        description="""
        List Agent Skills with tenant isolation.
        
        Supports filtering by status, enabled, and category.
        Group filtering: use include_groups=true to get group membership info.
        """
    )
    def list_skills(
        request: Request,
        status: Optional[str] = Query(None, description="Filter by status"),
        enabled: Optional[bool] = Query(None, description="Filter by enabled"),
        category: Optional[str] = Query(None, description="Filter by category tag"),
        include_groups: bool = Query(True, description="Include group information"),
        include_content: bool = Query(False, description="Include SKILL.md body"),
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.AGENT_SKILL_READ))
    ):
        """List Agent Skills with tenant isolation"""
        admin, tenant_id, admin_type = admin_and_tenant
        admin_id = admin.id

        try:
            query = db.query(AgentSkill).filter(
                AgentSkill.tenant_id == tenant_id
            )

            # Apply filters
            if status:
                query = query.filter(AgentSkill.status == status)

            if enabled is not None:
                query = query.filter(AgentSkill.enabled == enabled)

            skills = query.order_by(AgentSkill.created_at.desc()).all()

            # Category filter (metadata JSON field)
            if category:
                skills = [
                    s for s in skills
                    if s.get_metadata_value("category") == category
                ]

            logger.debug("Agent Skills listed", context={
                "admin_id": admin_id,
                "tenant_id": tenant_id,
                "skill_count": len(skills),
                "status_filter": status,
                "enabled_filter": enabled,
                "category_filter": category
            })

            return AgentSkillListResponse(
                limits=_skill_limits(),
                skills={
                    skill.id: _skill_to_response(
                        skill,
                        include_content=include_content,
                        include_groups=include_groups
                    )
                    for skill in skills
                },
                total=len(skills)
            )

        except Exception as e:
            logger.error("Failed to list Agent Skills", context={
                "admin_id": admin_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to list Agent Skills")

    # =========================================================================
    # Get Skill Detail
    # =========================================================================

    @router.get(
        "/{skill_id}",
        response_model=AgentSkillDetailResponse,
        summary="Get Agent Skill details",
        description="Get detailed information about an Agent Skill including content and group assignments."
    )
    def get_skill(
        skill_id: str,
        request: Request,
        include_groups: bool = Query(True, description="Include group information"),
        include_content: bool = Query(True, description="Include SKILL.md body"),
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.AGENT_SKILL_READ))
    ):
        """Get Agent Skill details"""
        admin, tenant_id, admin_type = admin_and_tenant

        try:
            skill = db.query(AgentSkill).filter(
                AgentSkill.id == skill_id,
                AgentSkill.tenant_id == tenant_id
            ).first()

            if not skill:
                raise HTTPException(status_code=404, detail=f"Agent Skill {skill_id} not found")

            logger.debug("Agent Skill retrieved", context={
                "skill_id": skill_id,
                "tenant_id": tenant_id,
                "admin_id": admin.id
            })

            return AgentSkillDetailResponse(
                skill=_skill_to_response(
                    skill,
                    include_content=include_content,
                    include_groups=include_groups
                )
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get Agent Skill", context={
                "skill_id": skill_id,
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to get Agent Skill")

    @router.get(
        "/{skill_id}/manifest",
        summary="Get skill progressive-disclosure manifest",
        description="Version-pinned inventory of the resources, scripts and templates "
                    "this skill exposes at L3 — the source of truth advertised by "
                    "skills__load and enforced as the L3 allowlist."
    )
    def get_skill_manifest(
        skill_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.AGENT_SKILL_READ))
    ):
        """Return the L2 manifest for a skill."""
        admin, tenant_id, admin_type = admin_and_tenant
        skill = db.query(AgentSkill).filter(
            AgentSkill.id == skill_id,
            AgentSkill.tenant_id == tenant_id
        ).first()
        if not skill:
            raise HTTPException(status_code=404, detail=f"Agent Skill {skill_id} not found")
        return {
            "skill_id": skill.id,
            "name": skill.name,
            "version": skill.version,
            "disclosure_mode": (
                skill.disclosure_mode.value
                if hasattr(skill.disclosure_mode, "value")
                else skill.disclosure_mode
            ),
            "manifest": skill.build_manifest(),
        }

    # =========================================================================
    # Create Skill
    # =========================================================================

    @router.post(
        "",
        response_model=AgentSkillDetailResponse,
        status_code=201,
        summary="Create Agent Skill",
        description="""
        Create a new Agent Skill.
        
        The skill starts in DRAFT status. Use the validate and publish
        endpoints to move through the lifecycle.
        
        Group assignment:
        - group_ids: List of group IDs to assign
        - Empty/null: Automatically assigned to the Default group
        """
    )
    def create_skill(
        skill_data: AgentSkillCreate,
        request: Request,
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)),
        db: Session = Depends(get_db),
        admin: AdminPrincipal = Depends(require_permission(Permission.AGENT_SKILL_CREATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Create new Agent Skill with tenant isolation"""
        admin, tenant_id, admin_type = admin_and_tenant
        admin_id = admin.id

        # Feature + quota gates
        TenantService.check_feature_access(db, tenant_id, 'agent_skills')
        TenantService.check_quota(db, tenant_id, TenantResourceTypes.AGENT_SKILLS)

        try:
            # Check for duplicate name+version within tenant
            existing = db.query(AgentSkill).filter(
                AgentSkill.tenant_id == tenant_id,
                AgentSkill.name == skill_data.name,
                AgentSkill.version == skill_data.version
            ).first()

            if existing:
                raise HTTPException(
                    status_code=400,
                    detail=f"Agent Skill '{skill_data.name}' version '{skill_data.version}' already exists"
                )

            # SKILL.md alone may not be past the per-skill limit.
            md_bytes = agent_skill_size.content_bytes(skill_data.skill_md_content)
            if agent_skill_size.too_large(md_bytes):
                raise HTTPException(status_code=413, detail={
                    "reason": agent_skill_size.REASON,
                    "error": agent_skill_size.message(skill_data.name, md_bytes),
                })

            # Compute SKILL.md hash for deduplication
            skill_md_hash = hashlib.sha256(
                skill_data.skill_md_content.encode()
            ).hexdigest()

            # Create skill. Prefixed id (skill-<ULID>) mirrors agent-<ULID> /
            # agentcard-<ULID> so a skill id is self-describing on the wire and
            # in logs. Lookups are exact-match, so legacy bare/ask- rows coexist.
            skill = AgentSkill(
                id=f"skill-{ULID()}",
                tenant_id=tenant_id,
                name=skill_data.name,
                description=skill_data.description,
                skill_md_content=skill_data.skill_md_content,
                skill_md_hash=skill_md_hash,
                license=skill_data.license,
                compatibility=skill_data.compatibility,
                allowed_tools=skill_data.allowed_tools,
                tenant_limits=skill_data.tenant_limits,
                tenant_governance=skill_data.tenant_governance,
                version=skill_data.version,
                skill_metadata=skill_data.metadata,
                disclosure_mode=SkillDisclosureMode(skill_data.disclosure_mode),
                allow_local_export=skill_data.allow_local_export,
                status=SkillStatus.DRAFT,
                enabled=True,
                created_by=actor_name(admin),
                updated_by=actor_name(admin),
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc)
            )

            db.add(skill)
            db.flush()

            # Assign to groups (Default if none specified). Side-effect
            # info — including which groups had the system Agent Skill
            # MCP server auto-bound — is folded into the audit log and
            # the response payload below.
            effective_group_ids = skill_data.group_ids or []
            assign_result = _assign_skill_to_groups(
                db=db,
                skill_id=skill.id,
                group_ids=effective_group_ids,
                tenant_id=tenant_id,
                created_by=admin_id
            )
            auto_bound_server_groups = assign_result["auto_bound_server_group_ids"]

            logger.info("Agent Skill created", context={
                "skill_id": skill.id,
                "skill_name": skill.name,
                "version": skill.version,
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "group_ids": effective_group_ids,
                "auto_default": not skill_data.group_ids,
                "auto_bound_server_group_ids": auto_bound_server_groups,
            })

            # Audit
            audit.log(
                action="agent_skill_create",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.AGENT_SKILLS.value,
                resource_id=skill.id,
                details={
                    "skill_name": skill.name,
                    "version": skill.version,
                    "group_ids": effective_group_ids,
                    # Auto-bound system MCP server side effect surfaces
                    # in the audit details so an operator can trace
                    # "who added this server to this group" later.
                    "auto_bound_server_group_ids": auto_bound_server_groups,
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            block_on(invalidate_proxy_cache(request, "skill", tenant_id, skill.id, "create"))

            return AgentSkillDetailResponse(
                skill=_skill_to_response(skill, include_content=True, include_groups=True),
                auto_bound_server_group_ids=auto_bound_server_groups,
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to create Agent Skill", context={
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to create Agent Skill")

    # =========================================================================
    # Update Skill
    # =========================================================================

    @router.patch(
        "/{skill_id}",
        response_model=AgentSkillDetailResponse,
        summary="Update Agent Skill",
        description="""
        Update an Agent Skill.
        
        Published skills cannot be updated — create a new version instead.
        Only fields included in the request body are changed.
        """
    )
    def update_skill(
        skill_id: str,
        skill_data: AgentSkillUpdate,
        request: Request,
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)),
        db: Session = Depends(get_db),
        admin: AdminPrincipal = Depends(require_permission(Permission.AGENT_SKILL_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Update Agent Skill with tenant verification"""
        admin, tenant_id, admin_type = admin_and_tenant
        admin_id = admin.id

        try:
            skill = db.query(AgentSkill).filter(
                AgentSkill.id == skill_id,
                AgentSkill.tenant_id == tenant_id
            ).first()

            if not skill:
                raise HTTPException(status_code=404, detail=f"Agent Skill {skill_id} not found")

            # Track changes for audit
            changes = {}
            update_data = skill_data.model_dump(exclude_unset=True, exclude={'group_ids'})

            # Guard: Published skills are immutable for content changes (spec requirement).
            # Group assignments AND tenant-wide limits/policies are operational
            # (governance, not skill content) → always allowed, like group_ids.
            content_changes = {
                k: v for k, v in update_data.items()
                if k not in ("tenant_limits", "tenant_governance", "allow_local_export")
            }
            if skill.status == SkillStatus.PUBLISHED and content_changes:
                raise HTTPException(
                    status_code=409,
                    detail=f"Cannot update published skill '{skill.name}' v{skill.version}. "
                           f"Create a new version instead."
                )

            # Renaming or renumbering must not collide with another skill.
            # name+version is the pair operators and the runtime address a
            # skill by, so two rows sharing it makes dispatch ambiguous.
            # Only reachable for drafts — the guard above already refuses
            # any content change to a published skill.
            if "name" in update_data or "version" in update_data:
                new_name = update_data.get("name", skill.name)
                new_version = update_data.get("version", skill.version)
                clash = db.query(AgentSkill).filter(
                    AgentSkill.tenant_id == tenant_id,
                    AgentSkill.name == new_name,
                    AgentSkill.version == new_version,
                    AgentSkill.id != skill.id,
                ).first()
                if clash:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Agent Skill '{new_name}' version '{new_version}' "
                            f"already exists"
                        ),
                    )

            if update_data.get("skill_md_content") is not None:
                agent_skill_size.refuse_if_too_large(
                    db, skill, agent_skill_size.content_bytes(update_data["skill_md_content"]),
                    replacing=("skill_md", None),
                )

            for field_name, value in update_data.items():
                db_field = field_name
                # Map 'metadata' to the ORM attribute 'skill_metadata'
                if field_name == 'metadata':
                    db_field = 'skill_metadata'

                # Coerce the disclosure-mode string to its enum (SQLEnum column
                # rejects a bare string at flush; mirror the script handler).
                if field_name == 'disclosure_mode' and value is not None:
                    value = SkillDisclosureMode(value)

                if hasattr(skill, db_field):
                    old_value = getattr(skill, db_field)
                    if old_value != value:
                        changes[field_name] = {
                            "old": old_value.value if hasattr(old_value, "value") else old_value,
                            "new": value.value if hasattr(value, "value") else value,
                        }
                        setattr(skill, db_field, value)

            # Recompute hash if content changed
            if 'skill_md_content' in update_data:
                skill.skill_md_hash = hashlib.sha256(
                    skill.skill_md_content.encode()
                ).hexdigest()

            skill.updated_by = actor_name(admin)
            skill.updated_at = datetime.now(timezone.utc)

            # Handle group assignments if explicitly sent. We also
            # capture the auto-bound-server side effect from
            # `_assign_skill_to_groups` so the audit log and the
            # response surface the change uniformly with the create
            # path.
            auto_bound_server_groups: List[str] = []
            if 'group_ids' in skill_data.model_fields_set:
                assign_result = _assign_skill_to_groups(
                    db=db,
                    skill_id=skill.id,
                    group_ids=skill_data.group_ids if skill_data.group_ids is not None else [],
                    tenant_id=tenant_id,
                    created_by=admin_id
                )
                auto_bound_server_groups = assign_result["auto_bound_server_group_ids"]
                changes['group_ids'] = {"new": skill_data.group_ids}
                if auto_bound_server_groups:
                    changes['auto_bound_server_group_ids'] = auto_bound_server_groups

            db.flush()

            logger.info("Agent Skill updated", context={
                "skill_id": skill_id,
                "skill_name": skill.name,
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "changes": list(changes.keys()),
                "auto_bound_server_group_ids": auto_bound_server_groups,
            })

            # Audit
            audit.log(
                action="agent_skill_update",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.AGENT_SKILLS.value,
                resource_id=skill_id,
                details={"skill_name": skill.name, "changes": changes},
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            block_on(invalidate_proxy_cache(request, "skill", tenant_id, skill_id, "update"))

            return AgentSkillDetailResponse(
                skill=_skill_to_response(skill, include_content=True, include_groups=True),
                auto_bound_server_group_ids=auto_bound_server_groups or None,
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to update Agent Skill", context={
                "skill_id": skill_id,
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to update Agent Skill")

    # =========================================================================
    # Delete Skill
    # =========================================================================

    @router.delete(
        "/{skill_id}",
        status_code=204,
        summary="Delete Agent Skill",
        description="""
        Delete an Agent Skill and all associated data (resources, scripts, assets, group associations).
        
        Published skills cannot be deleted — deprecate first, then archive.
        """
    )
    def delete_skill(
        skill_id: str,
        request: Request,
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)),
        db: Session = Depends(get_db),
        admin: AdminPrincipal = Depends(require_permission(Permission.AGENT_SKILL_DELETE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Delete Agent Skill with tenant verification"""
        admin, tenant_id, admin_type = admin_and_tenant
        admin_id = admin.id

        try:
            skill = db.query(AgentSkill).filter(
                AgentSkill.id == skill_id,
                AgentSkill.tenant_id == tenant_id
            ).first()

            if not skill:
                raise HTTPException(status_code=404, detail=f"Agent Skill {skill_id} not found")

            skill_name = skill.name
            skill_version = skill.version

            # Guard: Published skills must be deprecated/archived before deletion
            if skill.status == SkillStatus.PUBLISHED:
                raise HTTPException(
                    status_code=409,
                    detail=f"Cannot delete published skill '{skill_name}'. "
                           f"Deprecate it first, then archive, then delete."
                )

            # Remove group assignments before deletion
            db.query(resource_group_agent_skills).filter(
                resource_group_agent_skills.c.agent_skill_id == skill_id
            ).delete(synchronize_session=False)

            db.delete(skill)
            db.flush()

            logger.info("Agent Skill deleted", context={
                "skill_id": skill_id,
                "skill_name": skill_name,
                "version": skill_version,
                "tenant_id": tenant_id,
                "admin_id": admin_id
            })

            # Audit
            audit.log(
                action="agent_skill_delete",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.AGENT_SKILLS.value,
                resource_id=skill_id,
                details={"skill_name": skill_name, "version": skill_version},
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            block_on(invalidate_proxy_cache(request, "skill", tenant_id, skill_id, "delete"))

            # Sync system server capabilities
            capability_sync.sync_single_skill(db, tenant_id, skill)

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to delete Agent Skill", context={
                "skill_id": skill_id,
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to delete Agent Skill")

    # =========================================================================
    # Bulk Delete Skills
    # =========================================================================

    @router.post(
        "/bulk-delete",
        response_model=SkillBulkDeleteResponse,
        summary="Bulk-delete Agent Skills",
        description="""
        Delete several Agent Skills in one request.

        Published skills are **skipped** (not an error) — they must be
        deprecated then archived first, mirroring the single-delete
        guard. Each successful delete is audited individually and
        triggers proxy-cache invalidation + system-server capability
        sync, exactly like the single DELETE.

        Returns the ids that were deleted and those that were skipped
        (with a reason), so the UI can report partial outcomes.
        """,
    )
    def bulk_delete_skills(
        payload: SkillBulkDeleteRequest,
        request: Request,
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)),
        db: Session = Depends(get_db),
        admin: AdminPrincipal = Depends(require_permission(Permission.AGENT_SKILL_DELETE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ) -> SkillBulkDeleteResponse:
        """Delete many skills with per-skill tenant verification + guards."""
        admin, tenant_id, admin_type = admin_and_tenant
        admin_id = admin.id

        deleted: List[str] = []
        skipped: List[SkillBulkDeleteSkipped] = []

        # De-dup while preserving order so a repeated id isn't processed
        # (and audited) twice.
        seen: set = set()
        ordered_ids = [sid for sid in payload.skill_ids if not (sid in seen or seen.add(sid))]

        for skill_id in ordered_ids:
            skill = db.query(AgentSkill).filter(
                AgentSkill.id == skill_id,
                AgentSkill.tenant_id == tenant_id,
            ).first()

            if not skill:
                skipped.append(SkillBulkDeleteSkipped(skill_id=skill_id, reason="Not found"))
                continue

            if skill.status == SkillStatus.PUBLISHED:
                skipped.append(SkillBulkDeleteSkipped(
                    skill_id=skill_id,
                    reason="Published skills must be deprecated then archived before deletion",
                ))
                continue

            skill_name = skill.name
            skill_version = skill.version

            try:
                # Wrap each skill in a SAVEPOINT so a failure on one rolls
                # back ONLY that skill — the deletes already done in this
                # request stay intact and commit at the end. A bare
                # db.rollback() here would undo the whole batch.
                with db.begin_nested():
                    db.query(resource_group_agent_skills).filter(
                        resource_group_agent_skills.c.agent_skill_id == skill_id
                    ).delete(synchronize_session=False)
                    db.delete(skill)
                    db.flush()
            except Exception as e:
                logger.error("Failed to delete Agent Skill (bulk)", context={
                    "skill_id": skill_id, "tenant_id": tenant_id, "error": str(e),
                }, exc_info=True)
                skipped.append(SkillBulkDeleteSkipped(skill_id=skill_id, reason="Delete failed"))
                continue

            logger.info("Agent Skill deleted (bulk)", context={
                "skill_id": skill_id,
                "skill_name": skill_name,
                "version": skill_version,
                "tenant_id": tenant_id,
                "admin_id": admin_id,
            })

            audit.log(
                action="agent_skill_delete",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.AGENT_SKILLS.value,
                resource_id=skill_id,
                details={"skill_name": skill_name, "version": skill_version, "bulk": True},
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db,
            )

            block_on(invalidate_proxy_cache(request, "skill", tenant_id, skill_id, "delete"))
            capability_sync.sync_single_skill(db, tenant_id, skill)
            deleted.append(skill_id)

        return SkillBulkDeleteResponse(
            deleted=deleted,
            skipped=skipped,
            deleted_count=len(deleted),
            skipped_count=len(skipped),
        )

    # =========================================================================
    # Validate Skill
    # =========================================================================

    @router.post(
        "/{skill_id}/validate",
        response_model=SkillValidationResponse,
        summary="Validate Agent Skill",
        description="Run validation checks on a skill. Moves to VALIDATED on success."
    )
    def validate_skill(
        skill_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.AGENT_SKILL_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Validate skill (DRAFT → VALIDATED)"""
        admin, tenant_id, admin_type = admin_and_tenant

        try:
            skill = db.query(AgentSkill).filter(
                AgentSkill.id == skill_id,
                AgentSkill.tenant_id == tenant_id
            ).first()

            if not skill:
                raise HTTPException(status_code=404, detail=f"Agent Skill {skill_id} not found")

            # Run validation.
            errors = skill.validate_for_publishing()
            now = datetime.now(timezone.utc)

            skill.validation_errors = errors if errors else None
            skill.last_validated_at = now
            skill.updated_at = now

            if not errors:
                if skill.status == SkillStatus.DRAFT:
                    skill.status = SkillStatus.VALIDATED

            db.flush()

            logger.debug("Agent Skill validated", context={
                "skill_id": skill_id,
                "skill_name": skill.name,
                "valid": len(errors) == 0,
                "error_count": len(errors),
                "tenant_id": tenant_id
            })

            audit.log(
                action="agent_skill_validate",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.AGENT_SKILLS.value,
                resource_id=skill_id,
                details={
                    "skill_name": skill.name,
                    "valid": len(errors) == 0,
                    "error_count": len(errors),
                    "status": skill.status.value if hasattr(skill.status, "value") else str(skill.status)
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            return SkillValidationResponse(
                skill_id=skill.id,
                skill_name=skill.name,
                valid=len(errors) == 0,
                errors=errors,
                validated_at=now.isoformat()
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to validate Agent Skill", context={
                "skill_id": skill_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to validate Agent Skill")

    # =========================================================================
    # Publish Skill
    # =========================================================================

    @router.post(
        "/{skill_id}/publish",
        response_model=AgentSkillDetailResponse,
        summary="Publish Agent Skill",
        description="Publish a skill (DRAFT/VALIDATED → PUBLISHED). Published skills are immutable."
    )
    def publish_skill(
        skill_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.AGENT_SKILL_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Publish skill"""
        admin, tenant_id, admin_type = admin_and_tenant
        admin_id = admin.id

        try:
            skill = db.query(AgentSkill).filter(
                AgentSkill.id == skill_id,
                AgentSkill.tenant_id == tenant_id
            ).first()

            if not skill:
                raise HTTPException(status_code=404, detail=f"Agent Skill {skill_id} not found")

            try:
                skill.publish(published_by=admin_id)
            except ValueError as ve:
                raise HTTPException(status_code=409, detail=str(ve))

            skill.updated_at = datetime.now(timezone.utc)
            db.flush()

            logger.info("Agent Skill published", context={
                "skill_id": skill_id,
                "skill_name": skill.name,
                "version": skill.version,
                "tenant_id": tenant_id,
                "admin_id": admin_id
            })

            audit.log(
                action="agent_skill_publish",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.AGENT_SKILLS.value,
                resource_id=skill_id,
                details={"skill_name": skill.name, "version": skill.version},
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            # Sync system server capabilities
            capability_sync.sync_single_skill(db, tenant_id, skill)

            return AgentSkillDetailResponse(
                skill=_skill_to_response(skill, include_content=True, include_groups=True)
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to publish Agent Skill", context={
                "skill_id": skill_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to publish Agent Skill")

    # =========================================================================
    # Deprecate Skill
    # =========================================================================

    @router.post(
        "/{skill_id}/deprecate",
        response_model=AgentSkillDetailResponse,
        summary="Deprecate Agent Skill",
        description="Deprecate a published skill. Still functional but hidden from catalog."
    )
    def deprecate_skill(
        skill_id: str,
        deprecation: SkillDeprecateRequest,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.AGENT_SKILL_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Deprecate skill (PUBLISHED → DEPRECATED)"""
        admin, tenant_id, admin_type = admin_and_tenant
        admin_id = admin.id

        try:
            skill = db.query(AgentSkill).filter(
                AgentSkill.id == skill_id,
                AgentSkill.tenant_id == tenant_id
            ).first()

            if not skill:
                raise HTTPException(status_code=404, detail=f"Agent Skill {skill_id} not found")

            try:
                skill.deprecate(reason=deprecation.reason, deprecated_by=admin_id)
            except ValueError as ve:
                raise HTTPException(status_code=409, detail=str(ve))

            skill.updated_at = datetime.now(timezone.utc)
            db.flush()

            logger.info("Agent Skill deprecated", context={
                "skill_id": skill_id,
                "skill_name": skill.name,
                "reason": deprecation.reason,
                "tenant_id": tenant_id,
                "admin_id": admin_id
            })

            audit.log(
                action="agent_skill_deprecate",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.AGENT_SKILLS.value,
                resource_id=skill_id,
                details={
                    "skill_name": skill.name,
                    "reason": deprecation.reason
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            # Sync system server capabilities
            capability_sync.sync_single_skill(db, tenant_id, skill)

            return AgentSkillDetailResponse(
                skill=_skill_to_response(skill, include_content=True, include_groups=True)
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to deprecate Agent Skill", context={
                "skill_id": skill_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to deprecate Agent Skill")

    # =========================================================================
    # Archive Skill
    # =========================================================================

    @router.post(
        "/{skill_id}/archive",
        response_model=AgentSkillDetailResponse,
        summary="Archive Agent Skill",
        description="Archive a skill (fully disabled, retained for audit history)."
    )
    def archive_skill(
        skill_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.AGENT_SKILL_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Archive skill"""
        admin, tenant_id, admin_type = admin_and_tenant
        admin_id = admin.id

        try:
            skill = db.query(AgentSkill).filter(
                AgentSkill.id == skill_id,
                AgentSkill.tenant_id == tenant_id
            ).first()

            if not skill:
                raise HTTPException(status_code=404, detail=f"Agent Skill {skill_id} not found")

            skill.archive()
            skill.updated_at = datetime.now(timezone.utc)
            skill.updated_by = actor_name(admin)
            db.flush()

            logger.info("Agent Skill archived", context={
                "skill_id": skill_id,
                "skill_name": skill.name,
                "tenant_id": tenant_id,
                "admin_id": admin_id
            })

            audit.log(
                action="agent_skill_archive",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.AGENT_SKILLS.value,
                resource_id=skill_id,
                details={"skill_name": skill.name},
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            # Sync system server capabilities
            capability_sync.sync_single_skill(db, tenant_id, skill)

            return AgentSkillDetailResponse(
                skill=_skill_to_response(skill, include_content=False, include_groups=True)
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to archive Agent Skill", context={
                "skill_id": skill_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to archive Agent Skill")

    # =========================================================================
    # Catalog (lightweight listing for agents)
    # =========================================================================

    @router.get(
        "/catalog",
        response_model=AgentSkillCatalogResponse,
        summary="Get Skill Catalog",
        description="""
        Lightweight catalog for skill discovery (Progressive Disclosure Level 1).
        Returns only published, enabled skills. ~50-100 tokens per entry.
        """
    )
    def get_catalog(
        request: Request,
        category: Optional[str] = Query(None, description="Filter by category"),
        tag: Optional[str] = Query(None, description="Filter by tag"),
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.AGENT_SKILL_READ))
    ):
        """Get lightweight skill catalog"""
        admin, tenant_id, admin_type = admin_and_tenant

        try:
            skills = db.query(AgentSkill).filter(
                AgentSkill.tenant_id == tenant_id,
                AgentSkill.enabled == True,
                AgentSkill.status == SkillStatus.PUBLISHED
            ).order_by(AgentSkill.name).all()

            # Apply metadata filters
            if category:
                skills = [s for s in skills if s.get_metadata_value("category") == category]
            if tag:
                skills = [s for s in skills if tag in s.get_tags()]

            return AgentSkillCatalogResponse(
                skills=[s.to_catalog_dict() for s in skills],
                total=len(skills)
            )

        except Exception as e:
            logger.error("Failed to get skill catalog", context={
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to get skill catalog")

    # =========================================================================
    # Search Skills
    # =========================================================================

    @router.post(
        "/search",
        response_model=AgentSkillSearchResponse,
        summary="Search Agent Skills",
        description="Advanced search with filtering, pagination, and sorting."
    )
    def search_skills(
        search: AgentSkillSearchRequest,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.AGENT_SKILL_READ))
    ):
        """Search skills with advanced filtering"""
        admin, tenant_id, admin_type = admin_and_tenant

        try:
            query = db.query(AgentSkill).filter(
                AgentSkill.tenant_id == tenant_id
            )

            # Text search (partial, case-insensitive)
            if search.name:
                query = query.filter(AgentSkill.name.ilike(f"%{search.name}%"))
            if search.description:
                query = query.filter(AgentSkill.description.ilike(f"%{search.description}%"))

            # Exact match filters
            if search.status:
                query = query.filter(AgentSkill.status == search.status)
            if search.enabled is not None:
                query = query.filter(AgentSkill.enabled == search.enabled)

            # Sorting
            sort_column = getattr(AgentSkill, search.sort_by, AgentSkill.created_at)
            if search.sort_order == 'asc':
                query = query.order_by(sort_column.asc())
            else:
                query = query.order_by(sort_column.desc())

            # Count total before pagination
            total = query.count()

            # Pagination
            skills = query.offset(search.offset).limit(search.limit).all()

            # Post-query filters (metadata JSON)
            if search.category:
                skills = [s for s in skills if s.get_metadata_value("category") == search.category]
                total = len(skills)  # Adjust total for metadata filter
            if search.tags:
                skills = [s for s in skills if any(t in s.get_tags() for t in search.tags)]
                total = len(skills)

            return AgentSkillSearchResponse(
                skills=[_skill_to_response(s, include_groups=True) for s in skills],
                total=total,
                offset=search.offset,
                limit=search.limit
            )

        except Exception as e:
            logger.error("Failed to search Agent Skills", context={
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to search Agent Skills")

    # =========================================================================
    # Group Assignments
    # =========================================================================

    @router.post(
        "/{skill_id}/groups",
        response_model=AgentSkillDetailResponse,
        summary="Assign skill to groups",
        description="Add skill to additional resource groups."
    )
    def assign_skill_to_groups(
        skill_id: str,
        assignment: SkillGroupAssignment,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.AGENT_SKILL_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Assign skill to resource groups"""
        admin, tenant_id, admin_type = admin_and_tenant

        try:
            skill = db.query(AgentSkill).filter(
                AgentSkill.id == skill_id,
                AgentSkill.tenant_id == tenant_id
            ).first()

            if not skill:
                raise HTTPException(status_code=404, detail=f"Agent Skill {skill_id} not found")

            # Get current group IDs and merge with new ones
            current_ids = [g.id for g in skill.resource_groups]
            merged_ids = list(set(current_ids + assignment.group_ids))
            actually_added = set(assignment.group_ids) - set(current_ids)

            try:
                _assign_skill_to_groups(
                    db=db,
                    skill_id=skill.id,
                    group_ids=merged_ids,
                    tenant_id=tenant_id,
                    created_by=admin.id
                )
            except ValueError as e:
                # A group outside this tenant, or none at all: the caller's error.
                raise HTTPException(status_code=404, detail=str(e))
            db.flush()

            if actually_added:
                notify_group_members_about_resource_change(
                    db=db,
                    tenant_id=tenant_id,
                    group_ids=actually_added,
                    resource_kind="agent_skill",
                    resource_id=skill.id,
                    resource_name=skill.name,
                    action="added",
                    actor_username=admin.username,
                    logger=logger,
                )

            audit.log(
                action="agent_skill_group_assign",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.AGENT_SKILLS.value,
                resource_id=skill_id,
                details={"added_group_ids": assignment.group_ids},
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            block_on(invalidate_proxy_cache(request, "assign", tenant_id, skill_id, "assign"))

            return AgentSkillDetailResponse(
                skill=_skill_to_response(skill, include_groups=True)
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to assign skill to groups", context={
                "skill_id": skill_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to assign skill to groups")

    @router.delete(
        "/{skill_id}/groups",
        response_model=AgentSkillDetailResponse,
        summary="Remove skill from groups",
        description="Remove skill from specific resource groups."
    )
    def remove_skill_from_groups(
        skill_id: str,
        unassignment: SkillGroupUnassignment,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.AGENT_SKILL_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Remove skill from resource groups"""
        admin, tenant_id, admin_type = admin_and_tenant

        try:
            skill = db.query(AgentSkill).filter(
                AgentSkill.id == skill_id,
                AgentSkill.tenant_id == tenant_id
            ).first()

            if not skill:
                raise HTTPException(status_code=404, detail=f"Agent Skill {skill_id} not found")

            # Get current group IDs and remove specified ones
            current_ids = [g.id for g in skill.resource_groups]
            remaining_ids = [gid for gid in current_ids if gid not in unassignment.group_ids]
            actually_removed = set(current_ids) & set(unassignment.group_ids)

            _assign_skill_to_groups(
                db=db,
                skill_id=skill.id,
                group_ids=remaining_ids,
                tenant_id=tenant_id,
                created_by=admin.id
            )
            db.flush()

            if actually_removed:
                notify_group_members_about_resource_change(
                    db=db,
                    tenant_id=tenant_id,
                    group_ids=actually_removed,
                    resource_kind="agent_skill",
                    resource_id=skill.id,
                    resource_name=skill.name,
                    action="removed",
                    actor_username=admin.username,
                    logger=logger,
                )

            audit.log(
                action="agent_skill_group_unassign",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.AGENT_SKILLS.value,
                resource_id=skill_id,
                details={"removed_group_ids": unassignment.group_ids},
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            block_on(invalidate_proxy_cache(request, "assign", tenant_id, skill_id, "assign"))

            return AgentSkillDetailResponse(
                skill=_skill_to_response(skill, include_groups=True)
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to remove skill from groups", context={
                "skill_id": skill_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to remove skill from groups")

    # =========================================================================
    # Execution History
    # =========================================================================

    @router.get(
        "/{skill_id}/executions",
        response_model=AgentSkillExecutionListResponse,
        summary="List executions for a skill"
    )
    def list_executions(
        skill_id: str,
        request: Request,
        status: Optional[str] = Query(None, description="Filter by execution status"),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.AGENT_SKILL_READ))
    ):
        """List execution history for a skill"""
        admin, tenant_id, admin_type = admin_and_tenant

        try:
            query = db.query(AgentSkillExecution).filter(
                AgentSkillExecution.tenant_id == tenant_id,
                AgentSkillExecution.skill_id == skill_id
            )

            if status:
                query = query.filter(AgentSkillExecution.execution_status == status)

            total = query.count()
            executions = query.order_by(
                AgentSkillExecution.started_at.desc()
            ).offset(offset).limit(limit).all()

            return AgentSkillExecutionListResponse(
                executions=[_execution_to_summary(e) for e in executions],
                total=total
            )

        except Exception as e:
            logger.error("Failed to list executions", context={
                "skill_id": skill_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to list executions")

    @router.get(
        "/executions/{execution_id}",
        response_model=AgentSkillExecutionDetailResponse,
        summary="Get execution details"
    )
    def get_execution(
        execution_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.AGENT_SKILL_READ))
    ):
        """Get detailed execution information"""
        admin, tenant_id, admin_type = admin_and_tenant

        try:
            execution = db.query(AgentSkillExecution).filter(
                AgentSkillExecution.id == execution_id,
                AgentSkillExecution.tenant_id == tenant_id
            ).first()

            if not execution:
                raise HTTPException(status_code=404, detail=f"Execution {execution_id} not found")

            return AgentSkillExecutionDetailResponse(
                execution=execution.to_dict()
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get execution", context={
                "execution_id": execution_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to get execution details")

    # =========================================================================
    # RESOURCES (references/ directory)
    # =========================================================================

    @router.get(
        "/{skill_id}/resources",
        response_model=AgentSkillResourceListResponse,
        summary="List skill resources"
    )
    def list_resources(
        skill_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.AGENT_SKILL_READ))
    ):
        """List all resource files for a skill (without content bodies)"""
        admin, tenant_id, admin_type = admin_and_tenant
        try:
            skill = db.query(AgentSkill).filter(
                AgentSkill.id == skill_id, AgentSkill.tenant_id == tenant_id
            ).first()
            if not skill:
                raise HTTPException(status_code=404, detail=f"Skill {skill_id} not found")

            resources = db.query(AgentSkillResource).filter(
                AgentSkillResource.skill_id == skill_id,
                AgentSkillResource.tenant_id == tenant_id
            ).order_by(AgentSkillResource.filename).all()

            return AgentSkillResourceListResponse(
                resources=[r.to_listing_dict() for r in resources],
                total=len(resources)
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to list resources", context={"skill_id": skill_id, "error": str(e)}, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to list resources")

    @router.get(
        "/{skill_id}/resources/{resource_id}",
        summary="Get resource with content"
    )
    def get_resource(
        skill_id: str,
        resource_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.AGENT_SKILL_READ))
    ):
        """Get a single resource file including its content"""
        admin, tenant_id, admin_type = admin_and_tenant
        try:
            resource = db.query(AgentSkillResource).filter(
                AgentSkillResource.id == resource_id,
                AgentSkillResource.skill_id == skill_id,
                AgentSkillResource.tenant_id == tenant_id
            ).first()
            if not resource:
                raise HTTPException(status_code=404, detail=f"Resource {resource_id} not found")

            return {"resource": resource.to_dict(include_content=True)}
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get resource", context={"resource_id": resource_id, "error": str(e)}, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to get resource")

    @router.post(
        "/{skill_id}/resources",
        status_code=201,
        summary="Create skill resource"
    )
    def create_resource(
        skill_id: str,
        data: AgentSkillResourceCreate,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.AGENT_SKILL_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Create a new resource file for a skill"""
        admin, tenant_id, admin_type = admin_and_tenant
        admin_id = admin.id
        try:
            skill = db.query(AgentSkill).filter(
                AgentSkill.id == skill_id, AgentSkill.tenant_id == tenant_id
            ).first()
            if not skill:
                raise HTTPException(status_code=404, detail=f"Skill {skill_id} not found")

            # Check unique filename per skill
            existing = db.query(AgentSkillResource).filter(
                AgentSkillResource.skill_id == skill_id,
                AgentSkillResource.filename == data.filename
            ).first()
            if existing:
                raise HTTPException(
                    status_code=400,
                    detail=f"Resource '{data.filename}' already exists for this skill"
                )

            agent_skill_size.refuse_if_too_large(db, skill, agent_skill_size.content_bytes(data.content))

            content_hash = hashlib.sha256(data.content.encode()).hexdigest()

            try:
                resource_type_enum = SkillResourceType(data.resource_type)
            except ValueError:
                resource_type_enum = SkillResourceType.REFERENCE

            resource = AgentSkillResource(
                id=f"asr-{ULID()}",
                tenant_id=tenant_id,
                skill_id=skill_id,
                filename=data.filename,
                resource_type=resource_type_enum,
                content=data.content,
                content_hash=content_hash,
                mime_type=data.mime_type,
                size_bytes=len(data.content.encode())
            )
            db.add(resource)

            logger.info("Skill resource created", context={
                "skill_id": skill_id, "resource_id": resource.id,
                "filename": data.filename, "tenant_id": tenant_id
            })

            audit.log(
                action="agent_skill_resource_create",
                admin=admin, admin_type=admin_type, tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.AGENT_SKILLS.value,
                resource_id=skill_id,
                details={"filename": data.filename, "resource_id": resource.id},
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            return {"resource": resource.to_dict(include_content=True)}

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to create resource", context={"skill_id": skill_id, "error": str(e)}, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to create resource")

    @router.patch(
        "/{skill_id}/resources/{resource_id}",
        summary="Update skill resource"
    )
    def update_resource(
        skill_id: str,
        resource_id: str,
        data: AgentSkillResourceUpdate,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.AGENT_SKILL_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Update a resource file's content or metadata"""
        admin, tenant_id, admin_type = admin_and_tenant
        try:
            resource = db.query(AgentSkillResource).filter(
                AgentSkillResource.id == resource_id,
                AgentSkillResource.skill_id == skill_id,
                AgentSkillResource.tenant_id == tenant_id
            ).first()
            if not resource:
                raise HTTPException(status_code=404, detail=f"Resource {resource_id} not found")

            if data.content is not None:
                agent_skill_size.refuse_if_too_large(
                    db, resource.skill, agent_skill_size.content_bytes(data.content),
                    replacing=("resource", resource.id),
                )
                resource.content = data.content
                resource.content_hash = hashlib.sha256(data.content.encode()).hexdigest()
                resource.size_bytes = len(data.content.encode())
            if data.resource_type is not None:
                try:
                    resource.resource_type = SkillResourceType(data.resource_type)
                except ValueError:
                    pass
            if data.mime_type is not None:
                resource.mime_type = data.mime_type

            audit.log(
                action="agent_skill_resource_update",
                admin=admin, admin_type=admin_type, tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.AGENT_SKILLS.value,
                resource_id=skill_id,
                details={"filename": resource.filename, "resource_id": resource_id},
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            return {"resource": resource.to_dict(include_content=True)}

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to update resource", context={"resource_id": resource_id, "error": str(e)}, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to update resource")

    @router.delete(
        "/{skill_id}/resources/{resource_id}",
        summary="Delete skill resource"
    )
    def delete_resource(
        skill_id: str,
        resource_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.AGENT_SKILL_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Delete a resource file from a skill"""
        admin, tenant_id, admin_type = admin_and_tenant
        try:
            resource = db.query(AgentSkillResource).filter(
                AgentSkillResource.id == resource_id,
                AgentSkillResource.skill_id == skill_id,
                AgentSkillResource.tenant_id == tenant_id
            ).first()
            if not resource:
                raise HTTPException(status_code=404, detail=f"Resource {resource_id} not found")

            filename = resource.filename
            db.delete(resource)

            audit.log(
                action="agent_skill_resource_delete",
                admin=admin, admin_type=admin_type, tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.AGENT_SKILLS.value,
                resource_id=skill_id,
                details={"filename": filename, "resource_id": resource_id},
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            return {"success": True, "message": f"Resource '{filename}' deleted"}

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to delete resource", context={"resource_id": resource_id, "error": str(e)}, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to delete resource")

    # =========================================================================
    # SCRIPTS (scripts/ directory)
    # =========================================================================

    @router.get(
        "/{skill_id}/scripts",
        response_model=AgentSkillScriptListResponse,
        summary="List skill scripts"
    )
    def list_scripts(
        skill_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.AGENT_SKILL_READ))
    ):
        """List all script files for a skill (without content bodies)"""
        admin, tenant_id, admin_type = admin_and_tenant
        try:
            skill = db.query(AgentSkill).filter(
                AgentSkill.id == skill_id, AgentSkill.tenant_id == tenant_id
            ).first()
            if not skill:
                raise HTTPException(status_code=404, detail=f"Skill {skill_id} not found")

            scripts = db.query(AgentSkillScript).filter(
                AgentSkillScript.skill_id == skill_id,
                AgentSkillScript.tenant_id == tenant_id
            ).order_by(AgentSkillScript.filename).all()

            return AgentSkillScriptListResponse(
                scripts=[s.to_listing_dict() for s in scripts],
                total=len(scripts)
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to list scripts", context={"skill_id": skill_id, "error": str(e)}, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to list scripts")

    @router.get(
        "/{skill_id}/scripts/{script_id}",
        summary="Get script with content"
    )
    def get_script(
        skill_id: str,
        script_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.AGENT_SKILL_READ))
    ):
        """Get a single script file including source code"""
        admin, tenant_id, admin_type = admin_and_tenant
        try:
            script = db.query(AgentSkillScript).filter(
                AgentSkillScript.id == script_id,
                AgentSkillScript.skill_id == skill_id,
                AgentSkillScript.tenant_id == tenant_id
            ).first()
            if not script:
                raise HTTPException(status_code=404, detail=f"Script {script_id} not found")

            return {"script": script.to_dict(include_content=True)}
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get script", context={"script_id": script_id, "error": str(e)}, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to get script")

    @router.post(
        "/{skill_id}/scripts",
        status_code=201,
        summary="Create skill script"
    )
    def create_script(
        skill_id: str,
        data: AgentSkillScriptCreate,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.AGENT_SKILL_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Create a new script file for a skill"""
        admin, tenant_id, admin_type = admin_and_tenant
        admin_id = admin.id
        try:
            skill = db.query(AgentSkill).filter(
                AgentSkill.id == skill_id, AgentSkill.tenant_id == tenant_id
            ).first()
            if not skill:
                raise HTTPException(status_code=404, detail=f"Skill {skill_id} not found")

            existing = db.query(AgentSkillScript).filter(
                AgentSkillScript.skill_id == skill_id,
                AgentSkillScript.filename == data.filename
            ).first()
            if existing:
                raise HTTPException(
                    status_code=400,
                    detail=f"Script '{data.filename}' already exists for this skill"
                )

            agent_skill_size.refuse_if_too_large(db, skill, agent_skill_size.content_bytes(data.script_content))

            script_hash = hashlib.sha256(data.script_content.encode()).hexdigest()

            try:
                language_enum = ScriptLanguage(data.language)
            except ValueError:
                language_enum = ScriptLanguage.PYTHON

            try:
                mode_enum = ScriptExecutionMode(data.execution_mode)
            except ValueError:
                mode_enum = ScriptExecutionMode.SANDBOX

            script = AgentSkillScript(
                id=f"ass-{ULID()}",
                tenant_id=tenant_id,
                skill_id=skill_id,
                filename=data.filename,
                description=data.description,
                script_content=data.script_content,
                script_hash=script_hash,
                language=language_enum,
                execution_mode=mode_enum,
                args_schema=data.args_schema,
            )
            db.add(script)
            db.flush()

            logger.info("Skill script created", context={
                "skill_id": skill_id, "script_id": script.id,
                "filename": data.filename, "language": data.language,
                "tenant_id": tenant_id
            })

            audit.log(
                action="agent_skill_script_create",
                admin=admin, admin_type=admin_type, tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.AGENT_SKILLS.value,
                resource_id=skill_id,
                details={
                    "filename": data.filename,
                    "script_id": script.id,
                    "language": data.language
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            return {"script": script.to_dict(include_content=True)}

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to create script", context={"skill_id": skill_id, "error": str(e)}, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to create script")

    @router.patch(
        "/{skill_id}/scripts/{script_id}",
        summary="Update skill script"
    )
    def update_script(
        skill_id: str,
        script_id: str,
        data: AgentSkillScriptUpdate,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.AGENT_SKILL_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Update a script file's content or metadata"""
        admin, tenant_id, admin_type = admin_and_tenant
        try:
            script = db.query(AgentSkillScript).filter(
                AgentSkillScript.id == script_id,
                AgentSkillScript.skill_id == skill_id,
                AgentSkillScript.tenant_id == tenant_id
            ).first()
            if not script:
                raise HTTPException(status_code=404, detail=f"Script {script_id} not found")

            if data.script_content is not None:
                agent_skill_size.refuse_if_too_large(
                    db, script.skill, agent_skill_size.content_bytes(data.script_content),
                    replacing=("script", script.id),
                )
                script.script_content = data.script_content
                script.script_hash = hashlib.sha256(data.script_content.encode()).hexdigest()
            if data.description is not None:
                script.description = data.description
            if data.language is not None:
                try:
                    script.language = ScriptLanguage(data.language)
                except ValueError:
                    pass
            if data.execution_mode is not None:
                try:
                    script.execution_mode = ScriptExecutionMode(data.execution_mode)
                except ValueError:
                    pass
            if data.args_schema is not None:
                script.args_schema = data.args_schema

            db.flush()

            audit.log(
                action="agent_skill_script_update",
                admin=admin, admin_type=admin_type, tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.AGENT_SKILLS.value,
                resource_id=skill_id,
                details={"filename": script.filename, "script_id": script_id},
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            return {"script": script.to_dict(include_content=True)}

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to update script", context={"script_id": script_id, "error": str(e)}, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to update script")

    @router.delete(
        "/{skill_id}/scripts/{script_id}",
        summary="Delete skill script"
    )
    def delete_script(
        skill_id: str,
        script_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.AGENT_SKILL_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Delete a script file from a skill"""
        admin, tenant_id, admin_type = admin_and_tenant
        try:
            script = db.query(AgentSkillScript).filter(
                AgentSkillScript.id == script_id,
                AgentSkillScript.skill_id == skill_id,
                AgentSkillScript.tenant_id == tenant_id
            ).first()
            if not script:
                raise HTTPException(status_code=404, detail=f"Script {script_id} not found")

            filename = script.filename
            db.delete(script)
            db.flush()

            audit.log(
                action="agent_skill_script_delete",
                admin=admin, admin_type=admin_type, tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.AGENT_SKILLS.value,
                resource_id=skill_id,
                details={"filename": filename, "script_id": script_id},
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            return {"success": True, "message": f"Script '{filename}' deleted"}

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to delete script", context={"script_id": script_id, "error": str(e)}, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to delete script")

    # =========================================================================
    # ASSETS (assets/ directory)
    # =========================================================================

    @router.get(
        "/{skill_id}/assets",
        response_model=AgentSkillAssetListResponse,
        summary="List skill assets"
    )
    def list_assets(
        skill_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.AGENT_SKILL_READ)),
    ):
        """List all asset files for a skill (without content bodies)"""
        admin, tenant_id, admin_type = admin_and_tenant
        try:
            # Verify skill exists in tenant
            skill = db.query(AgentSkill).filter(
                AgentSkill.id == skill_id, AgentSkill.tenant_id == tenant_id
            ).first()
            if not skill:
                raise HTTPException(status_code=404, detail=f"Skill {skill_id} not found")

            assets = db.query(AgentSkillAsset).filter(
                AgentSkillAsset.skill_id == skill_id,
                AgentSkillAsset.tenant_id == tenant_id
            ).order_by(AgentSkillAsset.filename).all()

            return AgentSkillAssetListResponse(
                assets=[a.to_listing_dict() for a in assets],
                total=len(assets)
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to list assets", context={"skill_id": skill_id, "error": str(e)}, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to list assets")

    @router.get(
        "/{skill_id}/assets/{asset_id}",
        summary="Get asset with content"
    )
    def get_asset(
        skill_id: str,
        asset_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.AGENT_SKILL_READ)),
    ):
        """Get a single asset file including content"""
        admin, tenant_id, admin_type = admin_and_tenant
        try:
            asset = db.query(AgentSkillAsset).filter(
                AgentSkillAsset.id == asset_id,
                AgentSkillAsset.skill_id == skill_id,
                AgentSkillAsset.tenant_id == tenant_id
            ).first()
            if not asset:
                raise HTTPException(status_code=404, detail=f"Asset {asset_id} not found")

            return {"asset": asset.to_dict(include_content=True)}
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get asset", context={"asset_id": asset_id, "error": str(e)}, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to get asset")

    @router.post(
        "/{skill_id}/assets",
        status_code=201,
        summary="Create skill asset"
    )
    def create_asset(
        skill_id: str,
        data: AgentSkillAssetCreate,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.AGENT_SKILL_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Create a new asset file for a skill"""
        admin, tenant_id, admin_type = admin_and_tenant
        admin_id = admin.id
        try:
            skill = db.query(AgentSkill).filter(
                AgentSkill.id == skill_id, AgentSkill.tenant_id == tenant_id
            ).first()
            if not skill:
                raise HTTPException(status_code=404, detail=f"Skill {skill_id} not found")

            existing = db.query(AgentSkillAsset).filter(
                AgentSkillAsset.skill_id == skill_id,
                AgentSkillAsset.filename == data.filename
            ).first()
            if existing:
                raise HTTPException(
                    status_code=400,
                    detail=f"Asset '{data.filename}' already exists for this skill"
                )

            agent_skill_size.refuse_if_too_large(
                db, skill, agent_skill_size.content_bytes(data.content, data.encoding or "utf-8")
            )

            content_hash = hashlib.sha256(data.content.encode()).hexdigest()

            try:
                type_enum = AssetType(data.asset_type)
            except ValueError:
                type_enum = AssetType.DATA

            asset = AgentSkillAsset(
                id=f"asa-{ULID()}",
                tenant_id=tenant_id,
                skill_id=skill_id,
                filename=data.filename,
                description=data.description,
                content=data.content,
                content_hash=content_hash,
                asset_type=type_enum,
                mime_type=data.mime_type,
                size_bytes=len(data.content.encode()),
                encoding=data.encoding or "utf-8",
                vars_schema=data.vars_schema,
            )
            db.add(asset)
            db.flush()

            logger.info("Skill asset created", context={
                "skill_id": skill_id, "asset_id": asset.id,
                "filename": data.filename, "asset_type": data.asset_type,
                "tenant_id": tenant_id
            })

            audit.log(
                action="agent_skill_asset_create",
                admin=admin, admin_type=admin_type, tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.AGENT_SKILLS.value,
                resource_id=skill_id,
                details={
                    "filename": data.filename,
                    "asset_id": asset.id,
                    "asset_type": data.asset_type
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            return {"asset": asset.to_dict(include_content=True)}

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to create asset", context={"skill_id": skill_id, "error": str(e)}, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to create asset")

    @router.patch(
        "/{skill_id}/assets/{asset_id}",
        summary="Update skill asset"
    )
    def update_asset(
        skill_id: str,
        asset_id: str,
        data: AgentSkillAssetUpdate,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.AGENT_SKILL_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Update an asset file's content or metadata"""
        admin, tenant_id, admin_type = admin_and_tenant
        try:
            asset = db.query(AgentSkillAsset).filter(
                AgentSkillAsset.id == asset_id,
                AgentSkillAsset.skill_id == skill_id,
                AgentSkillAsset.tenant_id == tenant_id
            ).first()
            if not asset:
                raise HTTPException(status_code=404, detail=f"Asset {asset_id} not found")

            if data.content is not None or data.encoding is not None:
                agent_skill_size.refuse_if_too_large(
                    db, asset.skill, agent_skill_size.content_bytes(
                        data.content if data.content is not None else asset.content,
                        data.encoding if data.encoding is not None else asset.encoding,
                    ),
                    replacing=("asset", asset.id),
                )
            if data.content is not None:
                asset.content = data.content
                asset.content_hash = hashlib.sha256(data.content.encode()).hexdigest()
                asset.size_bytes = len(data.content.encode())
            if data.description is not None:
                asset.description = data.description
            if data.asset_type is not None:
                try:
                    asset.asset_type = AssetType(data.asset_type)
                except ValueError:
                    pass
            if data.mime_type is not None:
                asset.mime_type = data.mime_type
            if data.encoding is not None:
                asset.encoding = data.encoding
            if data.vars_schema is not None:
                asset.vars_schema = data.vars_schema

            db.flush()

            audit.log(
                action="agent_skill_asset_update",
                admin=admin, admin_type=admin_type, tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.AGENT_SKILLS.value,
                resource_id=skill_id,
                details={"filename": asset.filename, "asset_id": asset_id},
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            return {"asset": asset.to_dict(include_content=True)}

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to update asset", context={"asset_id": asset_id, "error": str(e)}, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to update asset")

    @router.delete(
        "/{skill_id}/assets/{asset_id}",
        summary="Delete skill asset"
    )
    def delete_asset(
        skill_id: str,
        asset_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.AGENT_SKILL_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Delete an asset file from a skill"""
        admin, tenant_id, admin_type = admin_and_tenant
        try:
            asset = db.query(AgentSkillAsset).filter(
                AgentSkillAsset.id == asset_id,
                AgentSkillAsset.skill_id == skill_id,
                AgentSkillAsset.tenant_id == tenant_id
            ).first()
            if not asset:
                raise HTTPException(status_code=404, detail=f"Asset {asset_id} not found")

            filename = asset.filename
            db.delete(asset)
            db.flush()

            audit.log(
                action="agent_skill_asset_delete",
                admin=admin, admin_type=admin_type, tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.AGENT_SKILLS.value,
                resource_id=skill_id,
                details={"filename": filename, "asset_id": asset_id},
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            return {"success": True, "message": f"Asset '{filename}' deleted"}

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to delete asset", context={"asset_id": asset_id, "error": str(e)}, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to delete asset")

    # =========================================================================
    # Import Skills from Zip
    # =========================================================================

    def _parse_skill_md(content: str) -> dict:
        """
        Parse a SKILL.md file into frontmatter dict + body string.

        Returns:
            {"frontmatter": {...}, "body": "..."}
        """
        content = content.strip()
        frontmatter = {}
        body = content

        # Extract YAML frontmatter between --- delimiters
        fm_match = re.match(r'^---\s*\n(.*?)\n---\s*\n?(.*)', content, re.DOTALL)
        if fm_match:
            fm_raw = fm_match.group(1)
            body = fm_match.group(2).strip()

            # Simple YAML parser for the flat/simple frontmatter we expect
            # (avoids requiring PyYAML as a dependency)
            current_key = None
            current_value_lines = []

            def _flush():
                nonlocal frontmatter, current_key, current_value_lines
                if current_key:
                    val = "\n".join(current_value_lines).strip()
                    # Parse lists: [tag1, tag2, ...]
                    list_match = re.match(r'^\[(.*)\]$', val, re.DOTALL)
                    if list_match:
                        items = [i.strip().strip('"').strip("'") for i in list_match.group(1).split(',')]
                        frontmatter[current_key] = [i for i in items if i]
                    elif val.lower() in ('true', 'false'):
                        frontmatter[current_key] = val.lower() == 'true'
                    elif val == '[]':
                        frontmatter[current_key] = []
                    else:
                        # Strip quotes
                        if (val.startswith('"') and val.endswith('"')) or \
                           (val.startswith("'") and val.endswith("'")):
                            val = val[1:-1]
                        frontmatter[current_key] = val
                    current_key = None
                    current_value_lines = []

            for line in fm_raw.split('\n'):
                # Key: value line
                kv_match = re.match(r'^([a-zA-Z_][a-zA-Z0-9_-]*)\s*:\s*(.*)', line)
                if kv_match and not line.startswith(' ') and not line.startswith('\t'):
                    _flush()
                    current_key = kv_match.group(1)
                    val = kv_match.group(2).strip()
                    if val == '|' or val == '>':
                        # Multi-line scalar follows
                        current_value_lines = []
                    else:
                        current_value_lines = [val]
                elif current_key is not None:
                    # Continuation of multi-line value
                    current_value_lines.append(line.rstrip())
                # Skip nested keys like metadata.author — treat metadata as opaque

            _flush()

        return {"frontmatter": frontmatter, "body": body}

    def _detect_script_language(filename: str) -> str:
        """Detect script language from file extension."""
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
        return {
            'py': 'python',
            'js': 'javascript',
            'ts': 'typescript',
            'sh': 'bash',
            'ps1': 'bash',  # PowerShell treated as bash for storage
        }.get(ext, 'bash')

    def _detect_mime_type(filename: str) -> str:
        """Detect MIME type from file extension."""
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
        return {
            'md': 'text/markdown',
            'json': 'application/json',
            'yaml': 'text/yaml',
            'yml': 'text/yaml',
            'txt': 'text/plain',
            'py': 'text/x-python',
            'js': 'text/javascript',
            'ts': 'text/typescript',
            'sh': 'text/x-shellscript',
            'ps1': 'text/x-powershell',
            'csv': 'text/csv',
            'xml': 'application/xml',
            'html': 'text/html',
        }.get(ext, 'text/plain')

    def _is_script_file(filename: str) -> bool:
        """Check if a file should be treated as a script."""
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
        return ext in ('py', 'js', 'ts', 'sh', 'ps1')

    def _classify_resource_type(rel_path: str, filename: str) -> str:
        """Classify resource type based on path and filename."""
        lower_path = rel_path.lower()
        lower_name = filename.lower()

        if 'example' in lower_name or lower_name == 'examples.md':
            return 'example'
        if 'test_prompts' in lower_name:
            return 'example'
        if 'template' in lower_name or lower_path.startswith('templates/'):
            return 'template'
        if lower_name.endswith('.json') and 'schema' in lower_name:
            return 'schema'
        if 'form' in lower_name:
            return 'form'
        if lower_path.startswith('agents/'):
            return 'reference'
        return 'reference'

    def _skill_limits() -> dict:
        """The skill size limits, from `app/constants/agent_skill.py`."""
        return {
            "skill_max_bytes": skill_constants.SKILL_MAX_BYTES,
            "archive_max_bytes": skill_constants.ARCHIVE_MAX_BYTES,
            "archive_max_skills": skill_constants.ARCHIVE_MAX_SKILLS,
        }

    def _skill_result(
        zip_filename: str,
        folder: Optional[str],
        status: str,
        *,
        skill_name: Optional[str] = None,
        skill_id: Optional[str] = None,
        error: Optional[str] = None,
        reason: Optional[str] = None,
        resources_count: int = 0,
        scripts_count: int = 0,
        skipped_files: Optional[List[str]] = None,
    ) -> dict:
        """One skill of an import: `imported`, `refused` (with `reason`) or
        `skipped`. `folder` is the skill's top-level folder in the zip (""
        for a root SKILL.md; None when the whole archive is refused)."""
        return {
            "zip_filename": zip_filename,
            "folder": folder,
            "status": status,
            "success": status == "imported",
            "skill_name": skill_name or folder or zip_filename,
            "skill_id": skill_id,
            "error": error,
            "reason": reason,
            "resources_count": resources_count,
            "scripts_count": scripts_count,
            "skipped_files": skipped_files or [],
        }

    def _is_os_metadata(name: str) -> bool:
        """What an archiver adds by itself (macOS resource forks, Finder
        files, dotfiles): neither part of a skill nor reported."""
        base = name.rsplit('/', 1)[-1]
        return name.startswith('__MACOSX/') or '/__MACOSX/' in name or name.startswith('.') or base == '.DS_Store'

    class _MemberRefused(Exception):
        pass

    def _read_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
        """A member's bytes, never more than its directory entry declares:
        a member that unpacks to more is refused (its CRC check fails, or
        the read finds the extra byte)."""
        with zf.open(info) as member:
            data = member.read(info.file_size + 1)
        if len(data) > info.file_size:
            raise _MemberRefused(f"{info.filename} unpacks to more than its declared size")
        return data

    def _import_zip(
        zip_bytes: bytes,
        zip_filename: str,
        db: Session,
        tenant_id: str,
        admin_id: str,
        actor: str,
        logger: BrutorLogger
    ) -> tuple:
        """
        Import the skills of one zip: a root SKILL.md is one skill (every
        file of the zip is its); otherwise every top-level folder with a
        SKILL.md is a skill of its own. The archive's caps and each skill's
        size are read from the zip's directory before anything is
        decompressed.

        Returns (results, archive): one result per skill (`_skill_result`),
        and {zip_filename, error, unassigned_files}: the files that belong
        to no skill folder, reported and never attached to a skill.
        """
        archive = {"zip_filename": zip_filename, "error": None, "unassigned_files": []}

        def refused(reason: str, error: str) -> tuple:
            archive["error"] = error
            return [_skill_result(zip_filename, None, "refused", error=error, reason=reason)], archive

        try:
            zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
            infos = [info for info in zf.infolist() if not info.is_dir()]
        except zipfile.BadZipFile:
            return refused("invalid_archive", "Invalid zip file")

        unpacked = sum(info.file_size for info in infos)
        if unpacked > skill_constants.ARCHIVE_MAX_BYTES:
            return refused("archive_too_large", (
                f"The zip unpacks to {unpacked} bytes, more than the archive limit "
                f"({skill_constants.ARCHIVE_MAX_TEXT})"
            ))

        names = {info.filename for info in infos}
        if "SKILL.md" in names:
            folders = [""]
        else:
            folders = sorted({
                name.split('/', 1)[0] for name in names
                if name.count('/') == 1 and name.endswith('/SKILL.md') and not _is_os_metadata(name)
            })
        if not folders:
            return refused("no_skill", "No SKILL.md found at the zip's root or in a top-level folder")
        if len(folders) > skill_constants.ARCHIVE_MAX_SKILLS:
            return refused("too_many_skills", (
                f"The zip holds {len(folders)} skills, more than the archive limit of "
                f"{skill_constants.ARCHIVE_MAX_SKILLS}"
            ))

        members = {folder: [] for folder in folders}
        for info in infos:
            if _is_os_metadata(info.filename):
                continue
            if folders == [""]:
                folder = ""
            else:
                folder = info.filename.split('/', 1)[0] if '/' in info.filename else None
            if folder in members:
                members[folder].append(info)
            else:
                archive["unassigned_files"].append(info.filename)
        archive["unassigned_files"].sort()

        results = [
            _import_skill_folder(zf, zip_filename, folder, members[folder], db, tenant_id, admin_id, actor, logger)
            for folder in folders
        ]
        return results, archive

    def _import_skill_folder(
        zf: zipfile.ZipFile,
        zip_filename: str,
        root_dir: str,
        infos: List[zipfile.ZipInfo],
        db: Session,
        tenant_id: str,
        admin_id: str,
        actor: str,
        logger: BrutorLogger
    ) -> dict:
        """Import one skill of a zip: the files of `root_dir` ("" for a
        root SKILL.md)."""
        label = root_dir or zip_filename

        def refused(reason: str, error: str, skill_name: Optional[str] = None) -> dict:
            return _skill_result(zip_filename, root_dir, "refused", skill_name=skill_name, error=error, reason=reason)

        # The skill's size, from the zip's directory, before any of its
        # files is decompressed.
        unpacked = sum(info.file_size for info in infos)
        if unpacked > skill_constants.SKILL_MAX_BYTES:
            return refused(agent_skill_size.REASON, agent_skill_size.message(label, unpacked, "unpacks to"))

        prefix = f"{root_dir}/" if root_dir else ""
        skill_md_path = f"{prefix}SKILL.md"
        try:
            contents = {info.filename: _read_member(zf, info) for info in infos}
        except (_MemberRefused, zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, OSError) as e:
            return refused("invalid_archive", f"The skill '{label}' could not be read from the zip: {e}")

        try:
            skill_md_raw = contents[skill_md_path].decode('utf-8')
        except UnicodeDecodeError as e:
            return refused("invalid_skill", f"Failed to read SKILL.md: {str(e)}")

        parsed = _parse_skill_md(skill_md_raw)
        fm = parsed["frontmatter"]
        body = parsed["body"]

        # Extract skill fields from frontmatter
        skill_name = fm.get("name", "")
        if not skill_name:
            # Fallback: use root directory name or zip filename
            skill_name = root_dir or zip_filename.replace('.zip', '')
            # Normalize to kebab-case
            skill_name = re.sub(r'[^a-z0-9-]', '-', skill_name.lower()).strip('-')

        description = fm.get("description", "").strip()
        if not description:
            # Use first paragraph of body as description
            first_para = body.split('\n\n')[0].replace('#', '').strip()
            description = first_para[:1024] if first_para else f"Imported skill: {skill_name}"

        # Truncate description to 1024 chars
        if len(description) > 1024:
            description = description[:1021] + "..."

        version = fm.get("version", "1.0.0")
        # Normalize version
        if not re.match(r'^\d+\.\d+\.\d+', version):
            version = "1.0.0"
        # Take only major.minor.patch
        version_parts = version.split('.')
        if len(version_parts) >= 3:
            version = '.'.join(version_parts[:3])

        license_val = fm.get("license")
        if isinstance(license_val, list):
            license_val = license_val[0] if license_val else None

        # Parse allowed_tools (can be comma-separated string or list)
        allowed_tools = fm.get("allowed-tools") or fm.get("allowed_tools")
        if isinstance(allowed_tools, str):
            allowed_tools = [t.strip() for t in allowed_tools.split(',') if t.strip()]
        elif not isinstance(allowed_tools, list):
            allowed_tools = None

        # Build metadata
        metadata = {}
        tags = fm.get("tags")
        if isinstance(tags, list):
            metadata["tags"] = tags
        fm_author = fm.get("author")
        if isinstance(fm_author, str):
            metadata["author"] = fm_author
        # Check for nested metadata block (simple key extraction)
        if "metadata" in fm and isinstance(fm["metadata"], str):
            # Already parsed inline — metadata was a simple value
            pass

        origin = fm.get("origin")
        if origin:
            metadata["origin"] = origin

        # Check for existing skill with same name+version
        existing = db.query(AgentSkill).filter(
            AgentSkill.tenant_id == tenant_id,
            AgentSkill.name == skill_name,
            AgentSkill.version == version
        ).first()

        if existing:
            return _skill_result(zip_filename, root_dir, "skipped", skill_name=skill_name,
                                 error=f"Skill '{skill_name}' version '{version}' already exists",
                                 reason="already_exists")

        # One more skill of the tenant's quota, asked as the create route
        # asks it and before anything of this skill is written. A
        # refusal is this file's result; the other files go on.
        try:
            TenantService.check_quota(db, tenant_id, TenantResourceTypes.AGENT_SKILLS)
        except HTTPException as refusal:
            return refused("quota_exceeded", TenantService.quota_refusal_reason(refusal), skill_name)

        # Create the skill
        skill_md_content = skill_md_raw  # Store the full original SKILL.md (frontmatter + body)
        skill_md_hash = hashlib.sha256(skill_md_content.encode()).hexdigest()

        skill = AgentSkill(
            id=str(ULID()),
            tenant_id=tenant_id,
            name=skill_name,
            description=description,
            skill_md_content=skill_md_content,
            skill_md_hash=skill_md_hash,
            license=license_val,
            allowed_tools=allowed_tools,
            version=version,
            skill_metadata=metadata if metadata else None,
            status=SkillStatus.DRAFT,
            enabled=True,
            created_by=actor,
            updated_by=actor,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc)
        )
        db.add(skill)
        db.flush()

        # Assign to Default Resource Group
        _assign_skill_to_groups(
            db=db,
            skill_id=skill.id,
            group_ids=[],  # Empty = auto-assign to Default
            tenant_id=tenant_id,
            created_by=admin_id
        )

        # The skill's other files: every one under its folder (under a
        # root SKILL.md, every file of the zip).
        resources_count = 0
        scripts_count = 0
        skipped_files: List[str] = []

        for entry_name in sorted(contents):
            if entry_name == skill_md_path:
                continue
            rel_path = entry_name[len(prefix):]
            if not is_safe_skill_filename(rel_path):
                skipped_files.append(entry_name)
                continue

            file_bytes = contents[entry_name]
            try:
                # NUL is valid UTF-8 but Postgres text refuses it, which used to
                # fail the whole import. It marks a binary file, so store it as one.
                if b"\x00" in file_bytes:
                    raise UnicodeDecodeError("utf-8", file_bytes, 0, 1, "NUL byte")
                file_content = file_bytes.decode('utf-8')
            except UnicodeDecodeError:
                # Binary file: stored as a base64 asset
                import base64
                file_content = base64.b64encode(file_bytes).decode('ascii')
                content_hash = hashlib.sha256(file_bytes).hexdigest()
                asset = AgentSkillAsset(
                    id=f"asa-{ULID()}",
                    tenant_id=tenant_id,
                    skill_id=skill.id,
                    filename=rel_path,
                    asset_type="data",
                    content=file_content,
                    content_hash=content_hash,
                    mime_type=_detect_mime_type(rel_path),
                    encoding="base64",
                    size_bytes=len(file_bytes),
                    description=f"Imported from {zip_filename}"
                )
                db.add(asset)
                resources_count += 1
                continue

            filename_only = rel_path.rsplit('/', 1)[-1] if '/' in rel_path else rel_path

            # Classify: script files go to scripts table, rest go to resources
            if _is_script_file(filename_only) and 'scripts/' in rel_path.lower():
                content_hash = hashlib.sha256(file_content.encode()).hexdigest()
                language = _detect_script_language(filename_only)

                try:
                    language_enum = ScriptLanguage(language)
                except ValueError:
                    language_enum = ScriptLanguage.BASH

                script = AgentSkillScript(
                    id=f"ass-{ULID()}",
                    tenant_id=tenant_id,
                    skill_id=skill.id,
                    filename=rel_path,
                    description=f"Imported from {zip_filename}",
                    script_content=file_content,
                    script_hash=content_hash,
                    language=language_enum,
                    execution_mode=ScriptExecutionMode.SANDBOX
                )
                db.add(script)
                scripts_count += 1
            else:
                # Store as resource
                content_hash = hashlib.sha256(file_content.encode()).hexdigest()
                resource_type = _classify_resource_type(rel_path, filename_only)

                try:
                    resource_type_enum = SkillResourceType(resource_type)
                except ValueError:
                    resource_type_enum = SkillResourceType.REFERENCE

                resource = AgentSkillResource(
                    id=f"asr-{ULID()}",
                    tenant_id=tenant_id,
                    skill_id=skill.id,
                    filename=rel_path,
                    resource_type=resource_type_enum,
                    content=file_content,
                    content_hash=content_hash,
                    mime_type=_detect_mime_type(filename_only),
                    size_bytes=len(file_content.encode())
                )
                db.add(resource)
                resources_count += 1

        db.flush()

        logger.info("Skill imported from zip", context={
            "skill_id": skill.id,
            "skill_name": skill_name,
            "version": version,
            "resources_count": resources_count,
            "scripts_count": scripts_count,
            "zip_filename": zip_filename,
            "tenant_id": tenant_id
        })

        return _skill_result(
            zip_filename, root_dir, "imported", skill_name=skill_name, skill_id=skill.id,
            resources_count=resources_count, scripts_count=scripts_count, skipped_files=skipped_files,
        )

    @router.post(
        "/import",
        summary="Import Agent Skills from Zip files",
        description="""
        Import Agent Skills from one or more zip archives.

        A zip with a SKILL.md at its root is one skill. Otherwise every
        top-level folder of the zip with a SKILL.md is a skill of its own.
        Each skill's files (references/, rules/, scripts/, agents/, ...)
        are imported as its resources, scripts and assets.

        Limits (also stated by the list of skills, `limits`): a skill may
        unpack to at most SKILL_MAX_BYTES (30 MB); a zip may hold at most
        50 skills and unpack to at most 300 MB. They are read from the
        zip's directory before anything is decompressed.

        The result has one entry per skill, `imported`, `refused` (with a
        `reason`: not_a_zip, invalid_archive, archive_too_large,
        too_many_skills, no_skill, too_large, invalid_skill,
        quota_exceeded) or `skipped` (already_exists), and per zip the
        files that belong to no skill folder (`archives[].unassigned_files`):
        they are reported and never attached to a skill.

        Imported skills start in DRAFT status and are assigned to the
        Default Resource Group.
        """
    )
    def import_skills(
        request: Request,
        files: List[UploadFile] = File(..., description="One or more skill zip files"),
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.AGENT_SKILL_CREATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Import Agent Skills from uploaded zip files"""
        admin, tenant_id, admin_type = admin_and_tenant
        admin_id = admin.id

        results = []
        archives = []

        for upload_file in files:
            filename = upload_file.filename or "unknown"
            if not filename.endswith('.zip'):
                results.append(_skill_result(filename, None, "refused", reason="not_a_zip",
                                             error="File must be a .zip archive"))
                archives.append({"zip_filename": filename, "error": "File must be a .zip archive",
                                 "unassigned_files": []})
                continue

            # At most the archive limit is read: a zip larger than what it
            # may unpack to is refused unread.
            try:
                zip_bytes = block_on(upload_file.read(skill_constants.ARCHIVE_MAX_BYTES + 1))
            except Exception as e:
                error = f"Failed to read file: {str(e)}"
                results.append(_skill_result(filename, None, "refused", reason="invalid_archive", error=error))
                archives.append({"zip_filename": filename, "error": error, "unassigned_files": []})
                continue
            if len(zip_bytes) > skill_constants.ARCHIVE_MAX_BYTES:
                error = f"The zip is larger than the archive limit ({skill_constants.ARCHIVE_MAX_TEXT})"
                results.append(_skill_result(filename, None, "refused", reason="archive_too_large", error=error))
                archives.append({"zip_filename": filename, "error": error, "unassigned_files": []})
                continue

            skill_results, archive = _import_zip(
                zip_bytes=zip_bytes,
                zip_filename=filename,
                db=db,
                tenant_id=tenant_id,
                admin_id=admin_id,
                actor=actor_name(admin),
                logger=logger
            )
            results.extend(skill_results)
            archives.append(archive)

        success_count = sum(1 for r in results if r["status"] == "imported")
        refused_count = sum(1 for r in results if r["status"] == "refused")
        skipped_count = sum(1 for r in results if r["status"] == "skipped")
        error_count = refused_count + skipped_count

        # Commit all successful imports
        if success_count > 0:
            db.commit()

            # Sync capabilities after import
            try:
                capability_sync.sync_agent_skill_capabilities(db, tenant_id)
            except Exception as sync_err:
                logger.warning("Capability sync after import failed", context={
                    "error": str(sync_err), "tenant_id": tenant_id
                })

        # Audit log
        audit.log(
            action="agent_skill_import",
            admin=admin,
            admin_type=admin_type,
            tenant_id=tenant_id,
            success=success_count > 0,
            resource_type=TenantResourceTypes.AGENT_SKILLS.value,
            resource_id=None,
            details={
                "files_uploaded": len(files),
                "success_count": success_count,
                "refused_count": refused_count,
                "skipped_count": skipped_count,
                "imported_skills": [r["skill_name"] for r in results if r["status"] == "imported"],
                "failed_skills": [
                    {"name": r["skill_name"], "zip_filename": r["zip_filename"], "status": r["status"],
                     "reason": r["reason"], "error": r["error"]}
                    for r in results if r["status"] != "imported"
                ],
                "unassigned_files": {a["zip_filename"]: a["unassigned_files"] for a in archives if a["unassigned_files"]},
            },
            ip_address=client_ip(request),
            user_agent=request.headers.get('user-agent'),
            request_method=request.method,
            request_path=str(request.url.path),
            db=db
        )

        logger.info("Skill import completed", context={
            "success_count": success_count,
            "error_count": error_count,
            "tenant_id": tenant_id,
            "admin_id": admin_id
        })

        return {
            "success": success_count > 0,
            "message": f"Imported {success_count} skill(s)"
            + (f", {refused_count} refused" if refused_count else "")
            + (f", {skipped_count} skipped" if skipped_count else ""),
            "total_files": len(files),
            "success_count": success_count,
            "error_count": error_count,
            "refused_count": refused_count,
            "skipped_count": skipped_count,
            "results": results,
            "archives": archives,
            "limits": _skill_limits()
        }

    # Static paths must beat parameterized ones: /catalog and /search were
    # declared below /{skill_id} and therefore unreachable (matched as skill
    # ids). A stable sort fixes them and any future static sibling.
    router.routes.sort(key=lambda r: "{" in getattr(r, "path", ""))

    return router