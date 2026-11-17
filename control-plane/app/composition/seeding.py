# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
What a starting control plane writes into its database, as an ordered list of
named steps.

`INSTALL_SEED` is everything `seed_initial_data` (`app/database/__init__.py`)
does, in order. That function opens the session, runs the list, and on any
exception rolls back, logs and re-raises; it holds no step of its own.

A step is a function of the `SeedContext`. Each step keeps its own commits and
its own error handling, because the steps differ on purpose: the system admin,
the default tenant, its provisioning and the role permission sync must
succeed, so they raise and the seed fails; the others log a warning, roll back
and let the seed continue.

Later steps read what earlier ones found (`default_tenant`, `prov_result`,
`tenant_admin_count`), so the order is part of the contract.

These are the Community Edition's steps: none names a
feature. The Enterprise Edition's steps (the system administrator, the cache
defaults, the portal user, the policy templates) are its extension's, each
tagged with its feature (`SeedStep.feature`), and run after these.

The helpers a step calls are imported from `app.database` when the step runs,
not when this module loads: `app.database` runs this list while it is being
used by everything else, and tests replace those helpers there.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging_system import BrutorLogger
from app.env_vars import (
    BRUTOR_DEFAULT_TENANT_ADMIN_EMAIL,
    BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD,
    BRUTOR_DEFAULT_TENANT_ADMIN_USERNAME,
)


@dataclass
class SeedContext:
    """What a seed step works with, and what earlier steps found.

    `db` and `logger` are given. The rest is filled in as the list runs:
    `default_tenant` by the `default_tenant` step, the others by
    `provision_default_tenant`. `tenant_admin_count` is the number of admins
    the default tenant had before it was provisioned; zero means a first
    install, which is when the convenience accounts are created.
    """

    db: Session
    logger: BrutorLogger
    default_tenant: Any = None
    prov_result: Any = None
    tenant_admin_count: int = 0
    tenant_admin_username: str = ""
    tenant_admin_password: str = ""


@dataclass(frozen=True)
class SeedStep:
    """One named step of the install seed.

    `feature` is the Enterprise Edition feature the step belongs to, or None
    for Community.
    """

    name: str
    run: Callable[[SeedContext], None]
    feature: Optional[str] = None
    # Refuses the whole seed (by raising) before any step has written
    # anything: what the step needs from the operator and cannot do without.
    check: Optional[Callable[[SeedContext], None]] = None


# The password of the default tenant's administrator. Required when that
# account has to be created; there is no default.
TENANT_ADMIN_PASSWORD_VARIABLE = BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD


def run_seed_steps(context: SeedContext, steps: Sequence[SeedStep]) -> None:
    """Every step's `check`, then the steps in order. A check or a step that
    raises stops the run; a check that raises stops it before the first
    step, so a refused start leaves the database as it was."""
    for step in steps:
        if step.check is not None:
            step.check(context)
    for step in steps:
        step.run(context)



def _default_tenant(ctx: SeedContext) -> None:
    """The default tenant."""
    from app.database import get_or_create_default_tenant

    db = ctx.db

    # =====================================================================
    # 2. Seed Default Tenant
    # =====================================================================
    default_tenant = get_or_create_default_tenant(db)

    ctx.default_tenant = default_tenant


def _tenant_admin_username() -> str:
    return BRUTOR_DEFAULT_TENANT_ADMIN_USERNAME.require()


def _require_tenant_admin_password(ctx: SeedContext) -> None:
    """Refuse the seed when the default tenant's administrator would have to
    be created and no password is configured for it: there is no default password.
    Runs before any step, so the default tenant does not exist yet on a
    first start; `_provision_default_tenant` applies the same rule again on
    the tenant it is given."""
    from app.core.initial_credentials import initial_password
    from app.database.models.admin import TenantAdmin
    from app.database.models.tenant import Tenant

    username = _tenant_admin_username()
    default_tenants = select(Tenant.id).where(Tenant.is_default.is_(True))
    exists = (
        ctx.db.query(TenantAdmin)
        .filter(TenantAdmin.username == username, TenantAdmin.tenant_id.in_(default_tenants))
        .count()
        > 0
    )
    if not exists:
        initial_password(TENANT_ADMIN_PASSWORD_VARIABLE, f"the administrator '{username}' of the default tenant")


def _provision_default_tenant(ctx: SeedContext) -> None:
    """Roles, admin, resource groups, servers and models of the default tenant."""
    db = ctx.db
    logger = ctx.logger
    default_tenant = ctx.default_tenant

    # =====================================================================
    # 3. Provision Default Tenant (roles, admin, resource group, servers, LLMs, registries)
    # =====================================================================
    # Uses TenantProvisioner — the same service used when creating tenants
    # via the Admin API, ensuring consistent initialization.
    from app.services.tenant.tenant_provisioning import TenantProvisioner
    from app.database.models.admin import TenantAdmin

    tenant_admin_count = db.query(TenantAdmin).filter(
        TenantAdmin.tenant_id == default_tenant.id
    ).count()

    tenant_admin_username = _tenant_admin_username()
    tenant_admin_email = BRUTOR_DEFAULT_TENANT_ADMIN_EMAIL.require()

    # There is no default password. The administrator is created when the tenant has no admin of
    # this name; then the password must be configured, and a start without
    # it is refused before anything of the tenant is provisioned. A start
    # that creates no account asks for nothing.
    from app.core.initial_credentials import configured_password, initial_password

    administrator_exists = db.query(TenantAdmin).filter(
        TenantAdmin.tenant_id == default_tenant.id,
        TenantAdmin.username == tenant_admin_username,
    ).count() > 0
    if administrator_exists:
        tenant_admin_password = configured_password(TENANT_ADMIN_PASSWORD_VARIABLE)
    else:
        tenant_admin_password = initial_password(
            TENANT_ADMIN_PASSWORD_VARIABLE,
            f"the administrator '{tenant_admin_username}' of the default tenant",
        )

    provisioner = TenantProvisioner(logger=logger)
    prov_result = provisioner.provision_tenant(
        db=db,
        tenant=default_tenant,
        admin_username=tenant_admin_username,
        admin_password=tenant_admin_password,
        admin_email=tenant_admin_email,
        admin_full_name="Default Tenant Administrator",
        seed_llms=True,
    )
    db.commit()

    ctx.tenant_admin_count = tenant_admin_count
    ctx.tenant_admin_username = tenant_admin_username
    ctx.tenant_admin_password = tenant_admin_password
    ctx.prov_result = prov_result

    if not administrator_exists:
        logger.info(f"Tenant admin created: {tenant_admin_username} (tenant: {default_tenant.name})")


def _provisioning_warnings(ctx: SeedContext) -> None:
    """Report what provisioning could not do."""
    logger = ctx.logger
    prov_result = ctx.prov_result

    if prov_result.warnings:
        logger.warning(f"Provisioning warnings: {prov_result.warnings}")





def _unbound_resource_groups(ctx: SeedContext) -> None:
    """The Unbound system group of every tenant."""
    from app.database import ensure_unbound_resource_groups

    db = ctx.db
    logger = ctx.logger

    # =====================================================================
    # 7b. Unbound system group for every tenant
    # =====================================================================
    # Where the core bills the platform service key's calls. Created
    # before the key is converged to unbound in step 8.
    try:
        ensure_unbound_resource_groups(db)
        db.commit()
    except Exception as e:
        logger.error("Failed to ensure the Unbound resource groups", context={
            "error": str(e),
        })
        db.rollback()


def _system_health_check_key(ctx: SeedContext) -> None:
    """The platform's health check API key."""
    from app.database import _seed_system_health_check_key

    db = ctx.db
    logger = ctx.logger
    default_tenant = ctx.default_tenant

    # =====================================================================
    # 8. Seed System Health Check API Key
    # =====================================================================
    try:
        _seed_system_health_check_key(
            db=db,
            tenant_id=default_tenant.id,
            logger=logger,
        )
        db.commit()
    except Exception as e:
        logger.warning("Failed to seed health check key (non-fatal)", context={
            "error": str(e),
        })
        db.rollback()


def _role_permissions(ctx: SeedContext) -> None:
    """Add permissions introduced since a role was created."""
    from app.database import sync_role_permissions

    db = ctx.db

    # =====================================================================
    # 9. Sync Role Permissions (adds new permissions to existing roles)
    # =====================================================================
    sync_role_permissions(db)



def _iso42001_controls(ctx: SeedContext) -> None:
    """The ISO/IEC 42001 Annex A control catalogue."""
    db = ctx.db
    logger = ctx.logger

    # ISO/IEC 42001 Annex A control catalog (code-owned reference data).
    try:
        from app.database.seed_iso42001_controls import seed_iso42001_controls
        seed_iso42001_controls(db)
        db.commit()
    except Exception as e:
        logger.warning("Failed to seed ISO 42001 controls (non-fatal)", context={
            "error": str(e),
        })
        db.rollback()


def _compliance_frameworks(ctx: SeedContext) -> None:
    """The compliance framework registry."""
    db = ctx.db
    logger = ctx.logger

    # The compliance framework registry. Migration `a177_evidence_records` seeds
    # the same rows for upgraded installs; fresh installs (create_all +
    # stamp) only get them from here.
    try:
        from app.database.seed_compliance_frameworks import seed_compliance_frameworks
        seed_compliance_frameworks(db)
        db.commit()
    except Exception as e:
        logger.warning("Failed to seed compliance frameworks (non-fatal)", context={
            "error": str(e),
        })
        db.rollback()


def _local_witness(ctx: SeedContext) -> None:
    """The deployment's own witness."""
    logger = ctx.logger

    # The deployment's own witness (trial bundle
    # `brutor-witness`), registered as a same-operator platform witness.
    # Inert unless BRUTOR_LOCAL_WITNESS_URL is set; its pin fetch runs on
    # a daemon thread so a witness still starting never delays boot.
    try:
        from app.database.seed_local_witness import seed_local_witness
        seed_local_witness()
    except Exception as e:
        logger.warning("Failed to seed the local witness (non-fatal)", context={
            "error": str(e),
        })


INSTALL_SEED: Sequence[SeedStep] = (
    SeedStep("default_tenant", _default_tenant),
    SeedStep("provision_default_tenant", _provision_default_tenant, check=_require_tenant_admin_password),
    SeedStep("provisioning_warnings", _provisioning_warnings),
    SeedStep("unbound_resource_groups", _unbound_resource_groups),
    SeedStep("system_health_check_key", _system_health_check_key),
    SeedStep("role_permissions", _role_permissions),
    SeedStep("iso42001_controls", _iso42001_controls),
    SeedStep("compliance_frameworks", _compliance_frameworks),
    SeedStep("local_witness", _local_witness),
)
