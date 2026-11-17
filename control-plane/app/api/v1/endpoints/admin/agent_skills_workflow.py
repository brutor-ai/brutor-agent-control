# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Agent Skill Workflow Endpoints
Multi-Tenant with RBAC and Audit Logging

Manages DAG-based workflow definitions attached to agent skills.
Each skill can have at most one workflow (1:1 relationship).

The workflow builder is not available in this release: nothing runs a
workflow, so saving, publishing and executing one are refused (422) with
`release_scope.WORKFLOW_BUILDER`. Reading, validating and deleting a stored
workflow, and reading recorded executions, still answer.

Follows the same patterns as agent_skills.py:
- Factory function for router creation
- Dependency injection for services
- require_permission() for RBAC
- require_tenant_resource_access() for multi-tenancy
- UnifiedAdminAuditLogger for audit trail

Endpoints:
    GET    /{skill_id}/workflow                       — Get workflow for skill
    PATCH  /{skill_id}/workflow                       — Refused: not available in this release
    DELETE /{skill_id}/workflow                       — Delete workflow
    POST   /{skill_id}/workflow/validate              — Validate DAG structure
    POST   /{skill_id}/workflow/publish               — Refused: not available in this release
    POST   /{skill_id}/workflow/execute               — Refused: not available in this release
    GET    /{skill_id}/workflow/executions             — List executions
    GET    /{skill_id}/workflow/executions/{exec_id}  — Get execution detail
"""


from datetime import datetime, timezone
from typing import Union, Optional

from fastapi import APIRouter, HTTPException, Depends, Query, Request, status
from sqlalchemy.orm import Session

from app.constants.tenant import TenantResourceTypes
from app.database import get_db
from app.database.models.agent_skill import AgentSkill
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
from app.core.logging_system import BrutorLogger
from app.services import release_scope

from app.api.v1.models.admin.agent_skill_workflow import (
    WorkflowDefinition,
    WorkflowResponse,
    WorkflowValidationResult,
    WorkflowStatus,
    WorkflowExecution,
    WorkflowExecutionListResponse,

    validate_workflow_dag,
)


# =============================================================================
# Helper Functions
# =============================================================================

def _now_iso() -> str:
    """Return current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat()


def _get_skill_or_404(
    db: Session,
    skill_id: str,
    tenant_id: str,
) -> AgentSkill:
    """Fetch a skill ensuring tenant isolation, or raise 404."""
    skill = db.query(AgentSkill).filter(
        AgentSkill.id == skill_id,
        AgentSkill.tenant_id == tenant_id,
    ).first()

    if not skill:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent Skill '{skill_id}' not found",
        )
    return skill


# =============================================================================
# Router Factory
# =============================================================================

def create_agent_skill_workflows_router(
    logger: BrutorLogger,
) -> APIRouter:
    """
    Factory function to create Agent Skill Workflows router with full tenant isolation.

    Args:
        logger: Logger instance

    Returns:
        Configured APIRouter with all workflow endpoints.
        Register under the same prefix as agent skills:
            app.include_router(router, prefix="/admin/agent-skills")
    """

    router = APIRouter(prefix="/admin/agent-skills", tags=["Agent Skill Workflows"])

    # =========================================================================
    # GET /{skill_id}/workflow — Get workflow for a skill
    # =========================================================================

    @router.get(
        "/{skill_id}/workflow",
        response_model=WorkflowResponse,
        summary="Get workflow definition for a skill",
        description="Returns the workflow DAG attached to this skill, or 404 if none exists.",
    )
    def get_skill_workflow(
        skill_id: str,
        request: Request,
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)
        ),
        db: Session = Depends(get_db),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.AGENT_SKILL_READ)
        ),
    ):
        """Get workflow for a skill with tenant isolation."""
        admin, tenant_id, admin_type = admin_and_tenant
        admin_id = admin.id

        try:
            skill = _get_skill_or_404(db, skill_id, tenant_id)

            # Retrieve workflow from skill's workflow JSON column
            workflow_data = skill.workflow_definition
            if not workflow_data:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"No workflow found for skill '{skill_id}'",
                )

            workflow = WorkflowDefinition(**workflow_data)

            logger.debug("Workflow retrieved", context={
                "skill_id": skill_id,
                "skill_name": skill.name,
                "workflow_id": workflow.id,
                "workflow_status": workflow.status,
                "node_count": len(workflow.nodes),
                "tenant_id": tenant_id,
                "admin_id": admin_id,
            })

            return WorkflowResponse(workflow=workflow)

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get workflow", context={
                "skill_id": skill_id,
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "error": str(e),
            }, exc_info=True)
            raise HTTPException(
                status_code=500,
                detail="Failed to retrieve workflow",
            )

    # =========================================================================
    # PATCH /{skill_id}/workflow — Create or replace workflow
    # =========================================================================

    @router.patch(
        "/{skill_id}/workflow",
        response_model=WorkflowResponse,
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        summary="Save a workflow (not available in this release)",
        description=(
            "The skill workflow builder is not available in this release: a saved "
            "workflow is not run. Every request is refused (422)."
        ),
    )
    def save_skill_workflow(
        skill_id: str,
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)
        ),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.AGENT_SKILL_UPDATE)
        ),
    ):
        """Not available in this release: refused with `release_scope.WORKFLOW_BUILDER`."""
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=release_scope.WORKFLOW_BUILDER,
        )

    # =========================================================================

    @router.delete(
        "/{skill_id}/workflow",
        summary="Delete workflow for a skill",
        description="Removes the workflow from this skill. Published workflows cannot be deleted.",
    )
    def delete_skill_workflow(
        skill_id: str,
        request: Request,
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)
        ),
        db: Session = Depends(get_db),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.AGENT_SKILL_UPDATE)
        ),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        """Delete workflow for a skill with tenant isolation."""
        admin, tenant_id, admin_type = admin_and_tenant
        admin_id = admin.id

        try:
            skill = _get_skill_or_404(db, skill_id, tenant_id)

            existing_data = skill.workflow_definition
            if not existing_data:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"No workflow found for skill '{skill_id}'",
                )

            existing = WorkflowDefinition(**existing_data)
            if existing.status == WorkflowStatus.PUBLISHED:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Published workflows cannot be deleted",
                )

            workflow_id = existing.id
            workflow_name = existing.name

            # Remove workflow
            skill.workflow_definition = None
            skill.updated_at = datetime.now(timezone.utc)
            skill.updated_by = actor_name(admin)

            db.flush()

            logger.info("Workflow deleted", context={
                "skill_id": skill_id,
                "skill_name": skill.name,
                "workflow_id": workflow_id,
                "tenant_id": tenant_id,
                "admin_id": admin_id,
            })

            # Audit
            audit.log(
                action="agent_skill_workflow_delete",
                db=db,
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                resource_type="agent_skill_workflow",
                resource_id=workflow_id,
                details={
                    "skill_id": skill_id,
                    "skill_name": skill.name,
                    "workflow_name": workflow_name,
                },
            )

            return {"success": True, "message": "Workflow deleted"}

        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.error("Failed to delete workflow", context={
                "skill_id": skill_id,
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "error": str(e),
            }, exc_info=True)
            raise HTTPException(
                status_code=500,
                detail="Failed to delete workflow",
            )

    # =========================================================================
    # POST /{skill_id}/workflow/validate — Validate DAG
    # =========================================================================

    @router.post(
        "/{skill_id}/workflow/validate",
        response_model=WorkflowValidationResult,
        summary="Validate workflow DAG structure",
        description=(
            "Performs structural validation on the workflow: checks for cycles, "
            "reachability, correct branching, required configurations, and more. "
            "If valid, transitions status from 'draft' to 'validated'."
        ),
    )
    def validate_skill_workflow(
        skill_id: str,
        request: Request,
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)
        ),
        db: Session = Depends(get_db),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.AGENT_SKILL_UPDATE)
        ),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        """Validate workflow DAG with tenant isolation."""
        admin, tenant_id, admin_type = admin_and_tenant
        admin_id = admin.id

        try:
            skill = _get_skill_or_404(db, skill_id, tenant_id)

            existing_data = skill.workflow_definition
            if not existing_data:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"No workflow found for skill '{skill_id}'",
                )

            workflow = WorkflowDefinition(**existing_data)
            result = validate_workflow_dag(workflow)

            # Transition to validated if valid and currently draft
            if result.valid and workflow.status == WorkflowStatus.DRAFT:
                now = _now_iso()
                workflow.status = WorkflowStatus.VALIDATED
                workflow.validated_at = now
                workflow.updated_at = now

                # Persist updated status
                skill.workflow_definition = workflow.model_dump(mode="json")
                skill.updated_at = datetime.now(timezone.utc)
                skill.updated_by = actor_name(admin)

                db.flush()

            logger.debug("Workflow validated", context={
                "skill_id": skill_id,
                "skill_name": skill.name,
                "workflow_id": workflow.id,
                "valid": result.valid,
                "error_count": len(result.errors),
                "warning_count": len(result.warnings),
                "tenant_id": tenant_id,
                "admin_id": admin_id,
            })

            # Audit
            audit.log(
                action="agent_skill_workflow_validate",
                db=db,
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                resource_type="agent_skill_workflow",
                resource_id=workflow.id,
                details={
                    "skill_id": skill_id,
                    "valid": result.valid,
                    "error_count": len(result.errors),
                    "warning_count": len(result.warnings),
                    "node_count": result.node_count,
                },
            )

            return result

        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.error("Failed to validate workflow", context={
                "skill_id": skill_id,
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "error": str(e),
            }, exc_info=True)
            raise HTTPException(
                status_code=500,
                detail="Failed to validate workflow",
            )

    # =========================================================================
    # POST /{skill_id}/workflow/publish — Publish workflow
    # =========================================================================

    @router.post(
        "/{skill_id}/workflow/publish",
        response_model=WorkflowResponse,
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        summary="Publish a workflow (not available in this release)",
        description=(
            "The skill workflow builder is not available in this release: a saved "
            "workflow is not run. Every request is refused (422)."
        ),
    )
    def publish_skill_workflow(
        skill_id: str,
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)
        ),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.AGENT_SKILL_UPDATE)
        ),
    ):
        """Not available in this release: refused with `release_scope.WORKFLOW_BUILDER`."""
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=release_scope.WORKFLOW_BUILDER,
        )

    # =========================================================================

    @router.post(
        "/{skill_id}/workflow/execute",
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        summary="Execute a workflow (not available in this release)",
        description=(
            "The skill workflow builder is not available in this release: nothing "
            "runs a workflow. Every request is refused (422)."
        ),
    )
    def execute_skill_workflow(
        skill_id: str,
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)
        ),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.AGENT_SKILL_UPDATE)
        ),
    ):
        """Not available in this release: refused with `release_scope.WORKFLOW_BUILDER`."""
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=release_scope.WORKFLOW_BUILDER,
        )

    # =========================================================================

    @router.get(
        "/{skill_id}/workflow/executions",
        response_model=WorkflowExecutionListResponse,
        summary="List workflow executions",
        description="Returns paginated list of workflow executions for this skill.",
    )
    def list_workflow_executions(
        skill_id: str,
        request: Request,
        status_filter: Optional[str] = Query(
            None, alias="status", description="Filter by execution status"
        ),
        offset: int = Query(0, ge=0),
        limit: int = Query(20, ge=1, le=100),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)
        ),
        db: Session = Depends(get_db),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.AGENT_SKILL_READ)
        ),
    ):
        """List workflow executions with tenant isolation."""
        admin, tenant_id, admin_type = admin_and_tenant
        admin_id = admin.id

        try:
            skill = _get_skill_or_404(db, skill_id, tenant_id)

            existing_data = skill.workflow_definition
            if not existing_data:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"No workflow found for skill '{skill_id}'",
                )

            # Read executions from skill
            all_executions_data = skill.workflow_executions or []

            # Deserialize and filter
            all_executions = [
                WorkflowExecution(**e) for e in all_executions_data
            ]

            if status_filter:
                all_executions = [
                    e for e in all_executions
                    if e.status == status_filter
                ]

            total = len(all_executions)

            # Sort by started_at descending (newest first)
            all_executions.sort(
                key=lambda e: e.started_at or "",
                reverse=True,
            )

            # Paginate
            page = all_executions[offset: offset + limit]

            logger.debug("Workflow executions listed", context={
                "skill_id": skill_id,
                "total": total,
                "returned": len(page),
                "status_filter": status_filter,
                "tenant_id": tenant_id,
                "admin_id": admin_id,
            })

            return WorkflowExecutionListResponse(
                executions=page,
                total=total,
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to list workflow executions", context={
                "skill_id": skill_id,
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "error": str(e),
            }, exc_info=True)
            raise HTTPException(
                status_code=500,
                detail="Failed to list workflow executions",
            )

    # =========================================================================
    # GET /{skill_id}/workflow/executions/{execution_id} — Get execution detail
    # =========================================================================

    @router.get(
        "/{skill_id}/workflow/executions/{execution_id}",
        summary="Get workflow execution detail",
        description="Returns full execution detail including node-level status.",
    )
    def get_workflow_execution(
        skill_id: str,
        execution_id: str,
        request: Request,
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)
        ),
        db: Session = Depends(get_db),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.AGENT_SKILL_READ)
        ),
    ):
        """Get workflow execution detail with tenant isolation."""
        admin, tenant_id, admin_type = admin_and_tenant
        admin_id = admin.id

        try:
            skill = _get_skill_or_404(db, skill_id, tenant_id)

            existing_data = skill.workflow_definition
            if not existing_data:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"No workflow found for skill '{skill_id}'",
                )

            all_executions_data = skill.workflow_executions or []
            execution_data = next(
                (e for e in all_executions_data if e.get("id") == execution_id),
                None,
            )

            if not execution_data:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Execution '{execution_id}' not found",
                )

            execution = WorkflowExecution(**execution_data)

            logger.debug("Workflow execution retrieved", context={
                "skill_id": skill_id,
                "execution_id": execution_id,
                "execution_status": execution.status,
                "tenant_id": tenant_id,
                "admin_id": admin_id,
            })

            return {"execution": execution}

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get workflow execution", context={
                "skill_id": skill_id,
                "execution_id": execution_id,
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "error": str(e),
            }, exc_info=True)
            raise HTTPException(
                status_code=500,
                detail="Failed to get execution details",
            )

    # =========================================================================
    # Return router
    # =========================================================================

    return router