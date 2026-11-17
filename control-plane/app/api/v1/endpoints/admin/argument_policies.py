# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Argument Policies Admin Endpoints

Multi-tenant CRUD for AST-aware argument policies. Defined once
in Governance, assigned many-to-many to Resource Groups.

The proxy reads these rows via the same cluster cache propagation
plumbing as guardrails — admin writes here trigger an
`invalidate_proxy_cache` notification so the Rust runtime picks
up the change without restart.
"""

from typing import List, Optional, Union

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from ulid import ULID

from app.core.sync_bridge import block_on
from app.api.v1.models.admin.argument_policy import (
    ArgumentPolicyCreateRequest,
    ArgumentPolicyGroupAssociationRequest,
    ArgumentPolicyListResponse,
    ArgumentPolicyResponse,
    ArgumentPolicyUpdateRequest,
    PREDICATES_BY_ANALYZER,
    PREDICATES_REQUIRING_VALUES,
    validate_policy_as_stored,
)
from app.constants.tenant import TenantResourceTypes
from app.core.admin_user.dependencies import (
    get_audit_logger,
    require_permission,
    require_tenant_resource_access,
)
from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.database.models.admin import TenantAdmin
from app.core.admin_user.admin_principal import AdminPrincipal
from app.database.models.argument_policy import ArgumentPolicy
from app.database.models.resource_group import ResourceGroup
from app.services.cache_invalidation import invalidate_proxy_cache
from app.middleware.trusted_proxies import client_ip


def _to_response(policy: ArgumentPolicy) -> ArgumentPolicyResponse:
    return ArgumentPolicyResponse(**policy.to_dict())


def _validate_merged_predicates(
    analyzer: str,
    rules: List[dict],
) -> None:
    """PATCH paths: when only `rules` is updated, validate against
    the existing analyzer. When only `analyzer` is changed, validate
    against existing rules. Raises HTTPException(400) on mismatch."""
    allowed = PREDICATES_BY_ANALYZER.get(analyzer, set())
    if not allowed:
        raise HTTPException(400, f"Unknown analyzer: {analyzer!r}")
    for i, rule in enumerate(rules):
        predicate = rule.get("deny_if") or rule.get("allow_only_if")
        if predicate not in allowed:
            raise HTTPException(
                400,
                f"rules[{i}]: predicate {predicate!r} is not valid for "
                f"analyzer {analyzer!r}. Allowed: {sorted(allowed)}",
            )
        if predicate in PREDICATES_REQUIRING_VALUES and not rule.get("values"):
            raise HTTPException(
                400,
                f"rules[{i}]: predicate {predicate!r} requires a non-empty "
                f"`values` list",
            )


#: The fields the presence guard reads. A PATCH touching none of them cannot
#: change its verdict, so a policy can always be renamed, rebound or switched
#: off.
_GUARD_INPUTS = frozenset({"rules", "analyzer_config", "argument_key", "severity", "surface"})


def create_argument_policies_router(logger: BrutorLogger) -> APIRouter:
    router = APIRouter(prefix="/admin/argument-policies", tags=["Argument Policies"])

    # =====================================================================
    # LIST
    # =====================================================================

    @router.get(
        "",
        response_model=ArgumentPolicyListResponse,
        summary="List argument policies for the current tenant",
    )
    def list_policies(
        request: Request,
        surface: Optional[str] = Query(
            None,
            description="Filter by surface (llm_output_tool_calls|mcp_input|skill_input)",
        ),
        analyzer: Optional[str] = Query(None, description="Filter by analyzer"),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.ARGUMENT_POLICIES)
        ),
        db: Session = Depends(get_db),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.ARGUMENT_POLICY_READ)
        ),
    ):
        _admin, tenant_id, _admin_type = admin_and_tenant
        q = db.query(ArgumentPolicy).filter(ArgumentPolicy.tenant_id == tenant_id)
        if surface:
            q = q.filter(ArgumentPolicy.surface == surface)
        if analyzer:
            q = q.filter(ArgumentPolicy.analyzer == analyzer)
        rows = q.order_by(ArgumentPolicy.name.asc()).all()
        return ArgumentPolicyListResponse(
            policies=[_to_response(p) for p in rows],
            total=len(rows),
        )

    # =====================================================================
    # POLICY-AS-CODE: YAML EXPORT
    # =====================================================================
    #
    # NOTE: Registered BEFORE `/{policy_id}` so FastAPI matches the
    # literal path first. If we registered it later, `GET /export.yaml`
    # would match `GET /{policy_id}` with `policy_id="export.yaml"` and
    # return 404 from the DB lookup. Same reasoning for `import.yaml`.
    # =====================================================================

    @router.get(
        "/export.yaml",
        summary="Export all argument policies for the tenant as YAML",
        response_class=Response,
    )
    def export_yaml(
        request: Request,
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.ARGUMENT_POLICIES)
        ),
        db: Session = Depends(get_db),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.ARGUMENT_POLICY_EXPORT)
        ),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        """
        Download every argument policy in this tenant as a single
        YAML document. The output is canonical (sorted keys) and
        round-trippable through `POST /import.yaml`, so operators
        can version-control policies in git and apply them via CI.

        Group assignments are exported by group **name** rather than
        ID so the YAML file is portable across environments where
        IDs differ (dev / staging / prod). The importer resolves
        names back to IDs in the target tenant.
        """
        admin_obj, tenant_id, admin_type = admin_and_tenant
        policies = (
            db.query(ArgumentPolicy)
            .filter(ArgumentPolicy.tenant_id == tenant_id)
            .order_by(ArgumentPolicy.name.asc())
            .all()
        )

        doc = {
            "apiVersion": "brutor.ai/v1",
            "kind": "ArgumentPolicyList",
            "policies": [
                {
                    "name": p.name,
                    "description": p.description,
                    "surface": p.surface,
                    "target": p.target,
                    "argument_key": p.argument_key,
                    "analyzer": p.analyzer,
                    "analyzer_config": p.analyzer_config,
                    "rules": p.rules,
                    "enabled": p.enabled,
                    "severity": p.severity,
                    "groups": [g.name for g in (p.groups or [])],
                }
                for p in policies
            ],
        }

        yaml_text = yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)

        audit.log(
            action="argument_policy_export",
            admin=admin_obj,
            admin_type=admin_type,
            tenant_id=tenant_id,
            success=True,
            resource_type=TenantResourceTypes.ARGUMENT_POLICIES.value,
            resource_id="export",
            details={"count": len(policies)},
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_method=request.method,
            request_path=str(request.url.path),
            db=db,
        )

        return Response(
            content=yaml_text,
            media_type="application/x-yaml",
            headers={
                "Content-Disposition": 'attachment; filename="argument-policies.yaml"',
            },
        )

    # =====================================================================
    # POLICY-AS-CODE: YAML IMPORT (upsert by name)
    # =====================================================================

    class _ImportResult(BaseModel):
        created: List[str] = Field(default_factory=list)
        updated: List[str] = Field(default_factory=list)
        unchanged: List[str] = Field(default_factory=list)
        errors: List[dict] = Field(default_factory=list)
        unresolved_groups: List[str] = Field(default_factory=list)

    @router.post(
        "/import.yaml",
        response_model=_ImportResult,
        summary="Apply a YAML policy bundle (idempotent upsert by name)",
    )
    def import_yaml(
        request: Request,
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.ARGUMENT_POLICIES)
        ),
        db: Session = Depends(get_db),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.ARGUMENT_POLICY_IMPORT)
        ),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        """
        Idempotent import. Accepts the YAML produced by
        `GET /export.yaml`. For each entry:

        - If no policy named `<name>` exists → CREATE
        - If one exists and differs → UPDATE
        - If one exists and matches → UNCHANGED
        - If validation fails → ERROR (others continue, response
          reports which entries failed)

        Group assignments are resolved by name within the tenant;
        names that don't resolve are reported in `unresolved_groups`
        but the policy is still written.

        Request body is raw YAML (Content-Type:
        application/x-yaml or text/yaml).
        """
        admin_obj, tenant_id, admin_type = admin_and_tenant

        raw = block_on(request.body())
        try:
            doc = yaml.safe_load(raw.decode("utf-8"))
        except yaml.YAMLError as e:
            raise HTTPException(400, f"Invalid YAML: {e}") from e
        except UnicodeDecodeError as e:
            raise HTTPException(400, f"Request body must be UTF-8: {e}") from e

        if not isinstance(doc, dict):
            raise HTTPException(400, "Top-level document must be a mapping")

        policies_in = doc.get("policies")
        if not isinstance(policies_in, list):
            raise HTTPException(
                400,
                "Document must contain a 'policies' list (see ArgumentPolicyList kind)",
            )

        result = _ImportResult()

        all_groups = (
            db.query(ResourceGroup)
            .filter(ResourceGroup.tenant_id == tenant_id)
            .all()
        )
        name_to_group = {g.name: g for g in all_groups}

        for i, entry in enumerate(policies_in):
            if not isinstance(entry, dict):
                result.errors.append({"index": i, "error": "entry must be a mapping"})
                continue

            try:
                create_body = ArgumentPolicyCreateRequest(
                    name=entry["name"],
                    description=entry.get("description"),
                    surface=entry["surface"],
                    target=entry["target"],
                    argument_key=entry["argument_key"],
                    analyzer=entry["analyzer"],
                    analyzer_config=entry.get("analyzer_config"),
                    rules=entry.get("rules") or [],
                    enabled=entry.get("enabled", True),
                    severity=entry.get("severity", "deny"),
                )
            except Exception as e:
                result.errors.append({
                    "index": i,
                    "name": entry.get("name"),
                    "error": str(e),
                })
                continue

            requested_group_names = entry.get("groups") or []
            resolved_groups = []
            for gname in requested_group_names:
                g = name_to_group.get(gname)
                if g:
                    resolved_groups.append(g)
                elif gname not in result.unresolved_groups:
                    result.unresolved_groups.append(gname)

            existing = (
                db.query(ArgumentPolicy)
                .filter(
                    ArgumentPolicy.tenant_id == tenant_id,
                    ArgumentPolicy.name == create_body.name,
                )
                .first()
            )

            if existing is None:
                new_policy = ArgumentPolicy(
                    id=f"argpol-{ULID()}",
                    tenant_id=tenant_id,
                    name=create_body.name,
                    description=create_body.description,
                    surface=create_body.surface,
                    target=create_body.target,
                    argument_key=create_body.argument_key,
                    analyzer=create_body.analyzer,
                    analyzer_config=create_body.analyzer_config,
                    rules=[r.model_dump(exclude_none=True) for r in create_body.rules],
                    enabled=create_body.enabled,
                    severity=create_body.severity,
                )
                new_policy.groups = resolved_groups
                db.add(new_policy)
                # Visible to the lookup above, for a name repeated in this document.
                db.flush()
                result.created.append(create_body.name)
            else:
                rules_dump = [
                    r.model_dump(exclude_none=True) for r in create_body.rules
                ]
                changed = (
                    existing.description != create_body.description
                    or existing.surface != create_body.surface
                    or existing.target != create_body.target
                    or existing.argument_key != create_body.argument_key
                    or existing.analyzer != create_body.analyzer
                    or (existing.analyzer_config or None) != create_body.analyzer_config
                    or (existing.rules or []) != rules_dump
                    or existing.enabled != create_body.enabled
                    or existing.severity != create_body.severity
                    or sorted([g.id for g in existing.groups]) != sorted([g.id for g in resolved_groups])
                )
                if changed:
                    existing.description = create_body.description
                    existing.surface = create_body.surface
                    existing.target = create_body.target
                    existing.argument_key = create_body.argument_key
                    existing.analyzer = create_body.analyzer
                    existing.analyzer_config = create_body.analyzer_config
                    existing.rules = rules_dump
                    existing.enabled = create_body.enabled
                    existing.severity = create_body.severity
                    existing.groups = resolved_groups
                    result.updated.append(create_body.name)
                else:
                    result.unchanged.append(create_body.name)

        db.flush()

        audit.log(
            action="argument_policy_import",
            admin=admin_obj,
            admin_type=admin_type,
            tenant_id=tenant_id,
            success=True,
            resource_type=TenantResourceTypes.ARGUMENT_POLICIES.value,
            resource_id="import",
            details={
                "created": len(result.created),
                "updated": len(result.updated),
                "unchanged": len(result.unchanged),
                "errors": len(result.errors),
                "unresolved_groups": result.unresolved_groups,
            },
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_method=request.method,
            request_path=str(request.url.path),
            db=db,
        )

        block_on(invalidate_proxy_cache(
            request, "argument_policy", tenant_id, "import", "update"
        ))

        return result

    # =====================================================================
    # GET ONE
    # =====================================================================

    @router.get(
        "/{policy_id}",
        response_model=ArgumentPolicyResponse,
        summary="Get one argument policy",
    )
    def get_policy(
        policy_id: str,
        request: Request,
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.ARGUMENT_POLICIES)
        ),
        db: Session = Depends(get_db),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.ARGUMENT_POLICY_READ)
        ),
    ):
        _admin, tenant_id, _admin_type = admin_and_tenant
        policy = (
            db.query(ArgumentPolicy)
            .filter(
                ArgumentPolicy.id == policy_id,
                ArgumentPolicy.tenant_id == tenant_id,
            )
            .first()
        )
        if not policy:
            raise HTTPException(404, f"Argument policy {policy_id} not found")
        return _to_response(policy)

    # =====================================================================
    # CREATE
    # =====================================================================

    @router.post(
        "",
        response_model=ArgumentPolicyResponse,
        status_code=201,
        summary="Create an argument policy",
    )
    def create_policy(
        body: ArgumentPolicyCreateRequest,
        request: Request,
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.ARGUMENT_POLICIES)
        ),
        db: Session = Depends(get_db),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.ARGUMENT_POLICY_CREATE)
        ),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        admin_obj, tenant_id, admin_type = admin_and_tenant

        # Uniqueness by (tenant_id, name)
        existing = (
            db.query(ArgumentPolicy)
            .filter(
                ArgumentPolicy.tenant_id == tenant_id,
                ArgumentPolicy.name == body.name,
            )
            .first()
        )
        if existing:
            raise HTTPException(
                409,
                f"Argument policy named {body.name!r} already exists",
            )

        policy = ArgumentPolicy(
            id=f"argpol-{ULID()}",
            tenant_id=tenant_id,
            name=body.name,
            description=body.description,
            surface=body.surface,
            target=body.target,
            argument_key=body.argument_key,
            analyzer=body.analyzer,
            analyzer_config=body.analyzer_config,
            rules=[rule.model_dump(exclude_none=True) for rule in body.rules],
            enabled=body.enabled,
            severity=body.severity,
        )

        if body.group_ids:
            groups = (
                db.query(ResourceGroup)
                .filter(
                    ResourceGroup.id.in_(body.group_ids),
                    ResourceGroup.tenant_id == tenant_id,
                )
                .all()
            )
            # Same rule as the group-binding route below: a binding that did not
            # land must not look like one that did.
            missing = sorted(set(body.group_ids) - {g.id for g in groups})
            if missing:
                raise HTTPException(
                    404,
                    f"Resource groups not found in this tenant: {missing}",
                )
            policy.groups = groups

        db.add(policy)
        db.flush()
        db.refresh(policy)

        audit.log(
            action="argument_policy_create",
            admin=admin_obj,
            admin_type=admin_type,
            tenant_id=tenant_id,
            success=True,
            resource_type=TenantResourceTypes.ARGUMENT_POLICIES.value,
            resource_id=policy.id,
            details={
                "name": policy.name,
                "surface": policy.surface,
                "analyzer": policy.analyzer,
                "group_count": len(body.group_ids or []),
            },
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_method=request.method,
            request_path=str(request.url.path),
            db=db,
        )

        block_on(invalidate_proxy_cache(
            request, "argument_policy", tenant_id, policy.id, "create"
        ))

        return _to_response(policy)

    # =====================================================================
    # UPDATE
    # =====================================================================

    @router.patch(
        "/{policy_id}",
        response_model=ArgumentPolicyResponse,
        summary="Update an argument policy",
    )
    def update_policy(
        policy_id: str,
        body: ArgumentPolicyUpdateRequest,
        request: Request,
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.ARGUMENT_POLICIES)
        ),
        db: Session = Depends(get_db),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.ARGUMENT_POLICY_UPDATE)
        ),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        admin_obj, tenant_id, admin_type = admin_and_tenant

        policy = (
            db.query(ArgumentPolicy)
            .filter(
                ArgumentPolicy.id == policy_id,
                ArgumentPolicy.tenant_id == tenant_id,
            )
            .first()
        )
        if not policy:
            raise HTTPException(404, f"Argument policy {policy_id} not found")

        before = policy.to_dict()

        # Apply patch fields
        patch = body.model_dump(exclude_unset=True, exclude_none=False)
        # Null clears a nullable field. Everything else is NOT NULL, so there is
        # no state to move to and it is dropped (same rule as `version`).
        patch = {k: v for k, v in patch.items() if v is not None or k in ("description", "analyzer_config")}

        # Rename: ensure uniqueness if name changed
        new_name = patch.get("name")
        if new_name and new_name != policy.name:
            conflict = (
                db.query(ArgumentPolicy)
                .filter(
                    ArgumentPolicy.tenant_id == tenant_id,
                    ArgumentPolicy.name == new_name,
                    ArgumentPolicy.id != policy_id,
                )
                .first()
            )
            if conflict:
                raise HTTPException(
                    409,
                    f"Argument policy named {new_name!r} already exists",
                )

        # Cross-field validation: rules must be valid for the
        # *effective* analyzer after the patch lands.
        effective_analyzer = patch.get("analyzer", policy.analyzer)
        if "rules" in patch:
            rules_list = [
                rule if isinstance(rule, dict) else rule.model_dump(exclude_none=True)
                for rule in patch["rules"]
            ]
            _validate_merged_predicates(effective_analyzer, rules_list)
            patch["rules"] = rules_list
        elif "analyzer" in patch:
            # analyzer changed but rules unchanged — must still validate
            _validate_merged_predicates(effective_analyzer, policy.rules or [])

        # The presence guard, on the row as it WILL be stored. Create runs it
        # in the request model; here the patch may carry only one half (new
        # rules over the stored analyzer_config, a new severity under the
        # stored guard rule), so only the merged view can answer.
        if _GUARD_INPUTS & patch.keys():
            try:
                validate_policy_as_stored(
                    patch.get("rules", policy.rules or []),
                    patch.get("analyzer_config", policy.analyzer_config),
                    patch.get("argument_key", policy.argument_key),
                    patch.get("severity", policy.severity),
                    patch.get("surface", policy.surface),
                )
            except ValueError as e:
                raise HTTPException(400, str(e)) from e

        for field, value in patch.items():
            if field == "rules" and value is not None:
                policy.rules = value
            else:
                setattr(policy, field, value)

        db.flush()
        db.refresh(policy)

        audit.log(
            action="argument_policy_update",
            admin=admin_obj,
            admin_type=admin_type,
            tenant_id=tenant_id,
            success=True,
            resource_type=TenantResourceTypes.ARGUMENT_POLICIES.value,
            resource_id=policy.id,
            details={
                "changed_fields": list(patch.keys()),
                "before": {k: before.get(k) for k in patch.keys()},
            },
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_method=request.method,
            request_path=str(request.url.path),
            db=db,
        )

        block_on(invalidate_proxy_cache(
            request, "argument_policy", tenant_id, policy.id, "update"
        ))

        return _to_response(policy)

    # =====================================================================
    # DELETE
    # =====================================================================

    @router.delete(
        "/{policy_id}",
        status_code=204,
        summary="Delete an argument policy",
    )
    def delete_policy(
        policy_id: str,
        request: Request,
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.ARGUMENT_POLICIES)
        ),
        db: Session = Depends(get_db),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.ARGUMENT_POLICY_DELETE)
        ),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        admin_obj, tenant_id, admin_type = admin_and_tenant
        policy = (
            db.query(ArgumentPolicy)
            .filter(
                ArgumentPolicy.id == policy_id,
                ArgumentPolicy.tenant_id == tenant_id,
            )
            .first()
        )
        if not policy:
            raise HTTPException(404, f"Argument policy {policy_id} not found")

        name = policy.name
        db.delete(policy)
        db.flush()

        audit.log(
            action="argument_policy_delete",
            admin=admin_obj,
            admin_type=admin_type,
            tenant_id=tenant_id,
            success=True,
            resource_type=TenantResourceTypes.ARGUMENT_POLICIES.value,
            resource_id=policy_id,
            details={"name": name},
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_method=request.method,
            request_path=str(request.url.path),
            db=db,
        )

        block_on(invalidate_proxy_cache(
            request, "argument_policy", tenant_id, policy_id, "delete"
        ))

    # =====================================================================
    # SET GROUP ASSIGNMENTS
    # =====================================================================

    @router.patch(
        "/{policy_id}/groups",
        response_model=ArgumentPolicyResponse,
        summary="Replace resource group assignments for a policy",
    )
    def set_groups(
        policy_id: str,
        body: ArgumentPolicyGroupAssociationRequest,
        request: Request,
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.ARGUMENT_POLICIES)
        ),
        db: Session = Depends(get_db),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.ARGUMENT_POLICY_UPDATE)
        ),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        admin_obj, tenant_id, admin_type = admin_and_tenant
        policy = (
            db.query(ArgumentPolicy)
            .filter(
                ArgumentPolicy.id == policy_id,
                ArgumentPolicy.tenant_id == tenant_id,
            )
            .first()
        )
        if not policy:
            raise HTTPException(404, f"Argument policy {policy_id} not found")

        old_ids = [g.id for g in policy.groups]

        groups = (
            db.query(ResourceGroup)
            .filter(
                ResourceGroup.id.in_(body.group_ids),
                ResourceGroup.tenant_id == tenant_id,
            )
            .all()
            if body.group_ids
            else []
        )

        # Reject group IDs that did not resolve (wrong tenant or
        # non-existent). Silent acceptance would let an operator
        # think a binding landed when it did not.
        resolved_ids = {g.id for g in groups}
        missing = [gid for gid in body.group_ids if gid not in resolved_ids]
        if missing:
            raise HTTPException(
                404,
                f"Resource groups not found in this tenant: {missing}",
            )

        policy.groups = groups
        db.flush()

        audit.log(
            action="argument_policy_groups_update",
            admin=admin_obj,
            admin_type=admin_type,
            tenant_id=tenant_id,
            success=True,
            resource_type=TenantResourceTypes.ARGUMENT_POLICIES.value,
            resource_id=policy_id,
            details={
                "old_group_ids": old_ids,
                "new_group_ids": body.group_ids,
            },
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_method=request.method,
            request_path=str(request.url.path),
            db=db,
        )

        block_on(invalidate_proxy_cache(
            request, "argument_policy", tenant_id, policy_id, "assign"
        ))

        return _to_response(policy)


    return router
