# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0


"""
Tenant settings (Community Edition): the installation's own tenant.

Four routes under `/admin/tenants/current`, all answering for the tenant of
the administrator who calls them and for no other: read it, change its
settings, read the theme the console draws itself in (flat and four-variant).
None takes a tenant id, so there is nothing to compare one with.

Managing tenants (list, create, any tenant by id, quotas, feature flags,
billing, suspension, deletion, statistics, theme editing) is the Enterprise
Edition's `tenant_management` router (tenant_management.py, feature
`multi_tenant`). It reuses `update_tenant_record` and the two theme response
builders below; nothing here imports from it.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.api.v1.models.admin.tenant_settings import (
    GOOGLE_DRIVE_PICKER_FIELDS,
    CurrentTenantResponse,
    NoCurrentTenant,
    TenantResponse,
    TenantSettingsUpdate,
)
from app.api.v1.models.admin.tenant_theme import (
    TenantThemeConfig,
    TenantThemeResponse,
    TenantThemeVariantsResponse,
)
from app.constants.admin import AdminType
from app.core.admin_user.dependencies import (
    get_audit_logger,
    get_current_admin,
    require_permission,
)
from app.core.admin_user.permissions import Permission
from app.core.sync_bridge import block_on
from app.services.cache_invalidation import invalidate_proxy_cache
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.database.models.admin import TenantAdmin, TenantAdminSession
from app.core.admin_user.admin_principal import AdminPrincipal
from app.database.models.end_user import EndUser
from app.database.models.tenant import Tenant
from app.services.mfa.policy import (
    MFA_OPTIONAL,
    effective_mfa_policy,
    has_2fa,
    is_stricter,
    requires_2fa_for_end_users,
)
from app.middleware.trusted_proxies import client_ip

#: `deactivation_reason` of an admin session ended by a stricter policy.
MFA_POLICY_TIGHTENED = "mfa_policy_tightened"

OWN_2FA_FIRST = (
    "Enable two-factor authentication on your own account first. This policy "
    "would require it of every administrator, and you would be signed out and "
    "have to enrol at your next sign-in."
)


def check_mfa_policy_change(
        *, new_policy: str, admin: AdminPrincipal, admin_type: AdminType
) -> None:
    """Refuse (409) a tenant administrator who requires 2FA without having it.

    The policy would apply to the administrator who sets it: the request that
    saved it would also end their session. A system administrator is not an
    account of the tenant and is under no tenant's policy.
    """
    if admin_type != AdminType.TENANT_ADMIN_TYPE:
        return
    if effective_mfa_policy(new_policy) != MFA_OPTIONAL and not has_2fa(admin):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=OWN_2FA_FIRST)


def end_sessions_the_policy_excludes(db: Session, tenant: Tenant, new_policy: str) -> dict:
    """End the sessions of the people `new_policy` requires 2FA of and who
    have none. Flushes in the caller's transaction; the caller commits, so the
    stored policy and the ended sessions change together or not at all.

    Administrators: their active `TenantAdminSession` rows are deactivated
    (`deactivate()` also drops the session from this worker's read model; other
    workers and the core proxy stop accepting it when their cached answer
    expires, 5 s and 30 s).

    End users, under `required_all`: `end_users.sessions_valid_after` is set,
    the column the core proxy's Portal compares a token's `iat` with (it sets
    it itself on a password change). Whole seconds from this clock, like the
    core's: a token issued in the same second passes.
    """
    without_2fa = (
        db.query(TenantAdmin.id)
        .filter(TenantAdmin.tenant_id == tenant.id)
        .filter((TenantAdmin.totp_enabled.isnot(True)) | (TenantAdmin.totp_secret.is_(None)))
    )
    sessions = (
        db.query(TenantAdminSession)
        .filter(
            TenantAdminSession.tenant_id == tenant.id,
            TenantAdminSession.is_active.is_(True),
            TenantAdminSession.admin_id.in_(without_2fa),
        )
        .all()
    )
    for session in sessions:
        session.deactivate(MFA_POLICY_TIGHTENED)

    end_users = 0
    if requires_2fa_for_end_users(new_policy):
        end_users = (
            db.query(EndUser)
            .filter(EndUser.tenant_id == tenant.id)
            .filter((EndUser.totp_enabled.isnot(True)) | (EndUser.totp_secret.is_(None)))
            .update({EndUser.sessions_valid_after: datetime.now(timezone.utc)}, synchronize_session=False)
        )
    db.flush()
    return {"admin_sessions": len(sessions), "end_users": end_users}


def tenant_theme_response(tenant: Tenant) -> TenantThemeResponse:
    """The flat (single-variant) theme of `tenant`."""
    return TenantThemeResponse(
        tenant_id=tenant.id,
        tenant_name=tenant.display_name,
        theme=TenantThemeConfig(**tenant.to_theme_dict()),
        is_custom=tenant.has_custom_theme(),
    )


def tenant_theme_variants_response(tenant: Tenant) -> TenantThemeVariantsResponse:
    """The four-variant theme bundle of `tenant`."""
    bundle = tenant.get_theme_variants()
    return TenantThemeVariantsResponse(
        tenant_id=tenant.id,
        company_name=bundle["company_name"],
        variants=bundle["variants"],
    )


def kept_settings_keys() -> set:
    """The keys of `tenants.settings` that have a route of their own
    (`TENANT_SETTINGS_KEPT_KEYS`): written where they are decided, and by
    no general writer of the settings."""
    from app.composition.hooks import HOOKS, TENANT_SETTINGS_KEPT_KEYS

    return {key for hook in HOOKS.all(TENANT_SETTINGS_KEPT_KEYS) for key in hook()}


def refuse_kept_settings(requested: Any) -> None:
    """422 when `settings` for a tenant that is being created names a key
    that has a route of its own. There is no stored value to keep: the key
    would be set without that route's validation and audit record."""
    named = sorted(kept_settings_keys() & set(requested)) if isinstance(requested, dict) else []
    if named:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"settings may not carry {', '.join(named)}: each is set on its own route, "
                "after the tenant is created"
            ),
        )


def _with_kept_settings(stored: Any, requested: Any) -> Any:
    """`requested` with every kept key (`TENANT_SETTINGS_KEPT_KEYS`) as it
    is in `stored`."""
    kept = kept_settings_keys()
    if not kept or not isinstance(requested, dict):
        return requested
    stored = stored if isinstance(stored, dict) else {}
    result = {key: value for key, value in requested.items() if key not in kept}
    result.update({key: stored[key] for key in kept if key in stored})
    return result


def update_tenant_record(
        *,
        db: Session,
        tenant: Tenant,
        updates: TenantSettingsUpdate,
        admin: AdminPrincipal,
        admin_type: AdminType,
        request: Request,
        audit: UnifiedAdminAuditLogger,
        logger: BrutorLogger,
        also: Optional[Dict[str, Any]] = None,
) -> TenantResponse:
    """Apply `updates` to `tenant`, write the audit record, return the tenant.

    `also` are columns the route derived from the request (a date from a
    number of days): written and audited with the rest.

    The one implementation behind `PATCH /admin/tenants/current` and tenant
    management's `PATCH /admin/tenants/{tenant_id}`. It writes every field the
    model it is handed declares and set, so **what a caller may change is
    decided by the model of its route** (`TenantSettingsUpdate` here,
    `TenantUpdate` there) and by which tenant the route resolved, never here.
    """
    tenant_id = tenant.id
    admin_id = admin.id

    try:
        logger.debug("Tenant update requested", context={
            "admin_id": admin_id,
            "tenant_id": tenant_id,
            "admin_type": str(admin_type),
        })

        # Track changes for audit
        changes = {}
        identity_default_changed = False

        # Update fields
        update_data = updates.dict(exclude_none=True)

        # data_residency: explicit-null is meaningful (it's how the
        # operator turns enforcement OFF). The default exclude_none
        # drops it; re-add when the caller set it explicitly so
        # `{"data_residency": null}` actually clears the column.
        if "data_residency" in updates.model_fields_set:
            update_data["data_residency"] = (
                updates.data_residency.model_dump()
                if updates.data_residency is not None
                else None
            )
        # data_retention carries the same explicit-null contract.
        if "data_retention" in updates.model_fields_set:
            update_data["data_retention"] = (
                updates.data_retention.model_dump()
                if updates.data_retention is not None
                else None
            )

        # The fields the model says an explicit null clears (a quota: null
        # is "no ceiling"). A field that was not sent is not in
        # `model_fields_set` and is left alone.
        for name in getattr(type(updates), "CLEARED_BY_NULL", ()):
            if name in updates.model_fields_set:
                update_data[name] = getattr(updates, name)
        # The Drive picker's two values: a clear (empty or null) is written
        # as null, and the two are set or clear together once the update
        # is applied.
        for name in GOOGLE_DRIVE_PICKER_FIELDS:
            if name in updates.model_fields_set:
                update_data[name] = getattr(updates, name)
        picker = [update_data.get(name, getattr(tenant, name, None)) for name in GOOGLE_DRIVE_PICKER_FIELDS]
        if any(picker) and not all(picker):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "portal_google_drive_client_id and portal_google_drive_api_key are set and "
                    "cleared together"
                ),
            )
        update_data.update(also or {})

        # AI System assurance. Same explicit-null semantics:
        # null is a decision — "stop capturing on a schedule", "go back to
        # the built-in gate requirements" — not "leave alone". Without
        # this, `exclude_none` drops the clear and the setting can be
        # switched on but never off.
        if "assurance_capture_interval_hours" in updates.model_fields_set:
            update_data["assurance_capture_interval_hours"] = (
                updates.assurance_capture_interval_hours
            )
        if "default_approval_window_seconds" in updates.model_fields_set:
            update_data["default_approval_window_seconds"] = (
                updates.default_approval_window_seconds
            )
        if "ai_system_gate_policy" in updates.model_fields_set:
            # Validated on write rather than trusted. The gate is
            # deliberately forgiving when it *reads* a policy (so a policy
            # written for a later release cannot break promotion today),
            # which means a typo would otherwise save cleanly and enforce
            # nothing — the worst outcome for a governance control.
            from app.services.ai_system_lifecycle_gate import (
                validate_tenant_policy,
            )

            problems = validate_tenant_policy(updates.ai_system_gate_policy)
            if problems:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={
                        "message": "Invalid AI System gate policy",
                        "problems": problems,
                    },
                )
            update_data["ai_system_gate_policy"] = updates.ai_system_gate_policy
        if "identity_policy_default" in updates.model_fields_set:
            # Its own permission besides the route's: the default takes part
            # in every group's effective identity policy.
            if not admin.has_permission(Permission.IDENTITY_POLICY_MANAGE.value):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "message": "Changing the tenant's identity default needs identity_policy:manage",
                        "code": "permission_denied",
                        "permission": Permission.IDENTITY_POLICY_MANAGE.value,
                    },
                )
            value = updates.identity_policy_default
            if value is not None:
                from app.api.v1.endpoints.admin.identity_policy import refuse_unknown_issuers

                refuse_unknown_issuers(db, tenant_id, value.allowed_issuers)
            update_data["identity_policy_default"] = value.model_dump() if value is not None else None
            identity_default_changed = update_data["identity_policy_default"] != tenant.identity_policy_default

        # Keys of `settings` that have a route of their own are left as
        # they are stored: the general update neither sets nor drops them.
        if "settings" in update_data:
            update_data["settings"] = _with_kept_settings(tenant.settings, update_data["settings"])

        # Check for duplicate domain
        if 'domain' in update_data and update_data['domain'] != tenant.domain:
            existing = db.query(Tenant).filter(
                Tenant.domain == update_data['domain'],
                Tenant.id != tenant_id
            ).first()
            if existing:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Domain '{update_data['domain']}' is already in use"
                )

        # The two-factor policy is enforced, so changing it has two
        # consequences that are decided before anything is written.
        mfa_policy_before = tenant.mfa_policy
        mfa_policy_changes = (
            "mfa_policy" in update_data and update_data["mfa_policy"] != mfa_policy_before
        )
        if mfa_policy_changes:
            check_mfa_policy_change(
                new_policy=update_data["mfa_policy"], admin=admin, admin_type=admin_type
            )

        for key, value in update_data.items():
            old_value = getattr(tenant, key, None)
            if old_value != value:
                # Special handling for JSON fields
                if key in ['features', 'settings', 'billing_metadata']:
                    if old_value != value:
                        changes[key] = {'old': old_value, 'new': value}
                        setattr(tenant, key, value)
                else:
                    changes[key] = {'old': old_value, 'new': value}
                    setattr(tenant, key, value)

        tenant.updated_at = datetime.now(timezone.utc)

        db.flush()
        db.refresh(tenant)

        if identity_default_changed:
            # The default takes part in every group's effective identity
            # policy: the core drops the tenant's group views.
            block_on(invalidate_proxy_cache(request, "resource_group", tenant_id, tenant_id, action="update"))

        audit_details = {"tenant_name": tenant.name, "changes": changes}
        if mfa_policy_changes and is_stricter(tenant.mfa_policy, than=mfa_policy_before):
            # Same transaction as the policy itself.
            audit_details["mfa_sessions_ended"] = end_sessions_the_policy_excludes(
                db, tenant, tenant.mfa_policy
            )

        # Audit log
        audit.log(
            action="tenant_update",
            admin=admin,
            admin_type=admin_type,
            tenant_id=tenant_id,
            success=True,
            resource_type="tenant",
            resource_id=tenant_id,
            details=audit_details,
            ip_address=client_ip(request),
            user_agent=request.headers.get('user-agent'),
            request_method=request.method,
            request_path=str(request.url.path),
            db=db
        )

        logger.info("Tenant updated", context={
            "admin_id": admin_id,
            "tenant_id": tenant_id,
            "changes": list(changes.keys())
        })

        return TenantResponse(**tenant.to_dict())

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to update tenant", context={
            "admin_id": admin_id,
            "tenant_id": tenant_id,
            "error": str(e)
        }, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update tenant: {str(e)}"
        )


def create_tenant_settings_router(logger: BrutorLogger) -> APIRouter:
    """The installation's own tenant: `/admin/tenants/current*`."""

    router = APIRouter(prefix="/admin/tenants", tags=["Tenant Settings"])

    def own_tenant(db: Session, admin, admin_type) -> Tenant:
        """The caller's tenant, read from the caller and from nothing else."""
        if admin_type != AdminType.TENANT_ADMIN_TYPE:
            # A system administrator is bound to no tenant; it works on a
            # tenant by id through tenant management.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This route answers for the caller's own tenant; a system administrator has none",
            )
        tenant = db.query(Tenant).filter(Tenant.id == admin.tenant_id).first()
        if not tenant:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
        return tenant

    @router.get(
        "/current",
        response_model=Union[CurrentTenantResponse, NoCurrentTenant],
        summary="Get current tenant",
        description=(
            "The logged-in administrator's tenant: profile, quotas, features, branding, "
            "settings and the retention policy as it is enforced. A system administrator "
            "belongs to no tenant and is answered an object whose `id` is null."
        ),
    )
    def get_current_tenant(
            request: Request,
            db: Session = Depends(get_db),
            admin_and_type: tuple = Depends(get_current_admin),
    ):
        """Return the full tenant record for the caller's tenant."""
        admin, admin_type = admin_and_type
        if admin_type == "system":
            # System admins aren't bound to a tenant. Return a minimal
            # placeholder so the UI doesn't 500 when a sysadmin navigates
            # to Configuration → Tenant (they can pick a tenant from the
            # Tenant Management page instead).
            return {
                "id": None,
                "features": {},
                "quotas": {},
                "settings": {},
            }

        tenant = db.query(Tenant).filter(Tenant.id == admin.tenant_id).first()
        if not tenant:
            raise HTTPException(404, "Tenant not found")

        data = tenant.to_dict()
        # The Data Retention tab shows the policy composed
        # with the framework retention floors (what the sweeper enforces).
        try:
            from app.services.compliance.retention_floors import retention_view

            data["data_retention_effective"] = retention_view(db, tenant)
        except Exception as exc:  # noqa: BLE001 — never fail the tenant read
            logger.warning("retention floor composition unavailable", context={"error": str(exc)})
            data["data_retention_effective"] = None
        return data

    @router.patch(
        "/current",
        response_model=TenantResponse,
        summary="Update current tenant",
        description=(
            "Update the settings of the logged-in admin's tenant: display "
            "fields, the two-factor policy, data residency, data retention, "
            "the assurance capture interval, the default approval "
            "window, the AI System gate policy, the default models and the "
            "Portal's Google Drive picker (client id and browser API key, set "
            "and cleared together; public values). Any "
            "other field is refused. A two-factor policy other than `optional` "
            "is refused with 409 unless the caller's own account has 2FA; "
            "making the policy stricter signs out the people it now covers "
            "who have no 2FA."
        ),
        # Residency and retention live here: scoping to the caller's
        # tenant alone let an Auditor switch them off.
        dependencies=[Depends(require_permission(Permission.GATEWAY_CONFIG_UPDATE))],
    )
    def update_current_tenant(
            updates: TenantSettingsUpdate,
            request: Request,
            db: Session = Depends(get_db),
            admin_and_type: tuple = Depends(get_current_admin),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        """Update the caller's own tenant. The tenant is the caller's; the
        fields are those of `TenantSettingsUpdate` and no others."""
        admin, admin_type = admin_and_type
        return update_tenant_record(
            db=db,
            tenant=own_tenant(db, admin, admin_type),
            updates=updates,
            admin=admin,
            admin_type=admin_type,
            request=request,
            audit=audit,
            logger=logger,
        )

    @router.get("/current/theme", response_model=TenantThemeResponse)
    def get_current_tenant_theme(
            db: Session = Depends(get_db),
            admin_and_type: tuple = Depends(get_current_admin),
    ):
        """
        Theme configuration of the caller's own tenant.

        Requires authentication. Unauthenticated branding for a login page is
        served by the separate public route `/v1/public/tenants/{tenant_id}/theme`.
        """
        admin, admin_type = admin_and_type
        return tenant_theme_response(own_tenant(db, admin, admin_type))

    @router.get("/current/theme-variants", response_model=TenantThemeVariantsResponse)
    def get_current_tenant_theme_variants(
            db: Session = Depends(get_db),
            admin_and_type: tuple = Depends(get_current_admin),
    ):
        """The four-variant theme bundle (admin_light / admin_dark /
        portal_light / portal_dark) of the caller's own tenant."""
        admin, admin_type = admin_and_type
        return tenant_theme_variants_response(own_tenant(db, admin, admin_type))

    return router
