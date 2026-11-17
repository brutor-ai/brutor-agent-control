# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Tenant Provisioning Service

Handles the full lifecycle of setting up a new tenant with all required resources:
    1. Tenant RBAC roles (Tenant Admin, End User, Read-Only)
    2. Default Tenant Admin user (with Tenant Admin role)
    3. Default Resource Group (system, non-deletable)
    4. System MCP Servers (Agent Skills, etc.)
    5. Default LLM Model configurations

This module extracts the reusable provisioning logic from seed_initial_data()
so it can be called both:
    - At startup (for the default tenant)
    - Via the Admin API (when creating new tenants)

Usage:
    from app.services.tenant_provisioning import TenantProvisioner

    provisioner = TenantProvisioner(logger)
    result = provisioner.provision_tenant(
        db=db,
        tenant=new_tenant,
        admin_username="admin",
        admin_password="SecurePassword123!",
        admin_email="admin@example.com",
    )
"""

import time
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Callable

import bcrypt
from ulid import ULID
from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError

from app.constants.tenant import TenantDefaultRole
from app.constants.groups import ResourceGroupName
from app.core.logging_system import BrutorLogger
from app.database.models.tenant import Tenant


def run_seed_with_retry(
    db: Session,
    label: str,
    seed_fn: Callable[[], None],
    logger: BrutorLogger,
    warnings: List[str],
    max_attempts: int = 3,
    base_delay: float = 0.5,
) -> bool:
    """
    Run a seed step with retry on transient connection errors.

    When Aurora Serverless v2 scales, SSL connections drop mid-query — which
    invalidates the session and raises OperationalError. A plain rollback
    lets us reconnect and retry the whole seed from the top, so any partial
    work the seed had done (and that we just rolled back) gets redone.

    Non-connection errors aren't retried — they indicate a real bug, not a
    transient blip, and we keep the existing "log and continue" semantics.
    """
    delay = base_delay
    for attempt in range(1, max_attempts + 1):
        try:
            seed_fn()
            return True
        except OperationalError as e:
            db.rollback()  # clears invalid transaction, forces reconnect
            if attempt == max_attempts:
                logger.error(
                    f"{label} failed after {attempt} attempts (non-fatal)",
                    context={"error": str(e)},
                )
                warnings.append(f"{label} failed: {e}")
                return False
            logger.warning(
                f"{label} hit connection error (attempt {attempt}/{max_attempts}); "
                f"retrying in {delay:.1f}s",
                context={"error": str(e)},
            )
            time.sleep(delay)
            delay = min(delay * 2, 4.0)
        except Exception as e:
            db.rollback()
            logger.error(f"{label} failed (non-fatal)", context={"error": str(e)})
            warnings.append(f"{label} failed: {e}")
            return False
    return False


@dataclass
class ProvisioningResult:
    """Result of tenant provisioning"""
    tenant_id: str
    tenant_name: str

    # Roles
    role_ids: Dict[str, str] = field(default_factory=dict)

    # Admin
    admin_id: Optional[str] = None
    admin_username: Optional[str] = None

    # Resource Group
    default_group_id: Optional[str] = None
    unbound_group_id: Optional[str] = None

    # LLMs
    llm_count: int = 0

    # System servers
    system_servers_seeded: bool = False

    # Errors (non-fatal)
    warnings: List[str] = field(default_factory=list)


class TenantProvisioner:
    """
    Provisions a new tenant with all required resources.

    All operations run within the caller's database session — the caller
    is responsible for commit/rollback. This allows the provisioning to
    be atomic with the tenant creation itself.
    """

    def __init__(self, logger: BrutorLogger):
        self.logger = logger


    def provision_tenant(self, *args, **kwargs) -> ProvisioningResult:
        """Provision a tenant, attributing the seeded rows to the platform.

        Everything provisioning creates — the example MCP servers, the
        starter skills, the default guardrails — is seeded content, not
        something the admin who clicked "Create tenant" authored. Binding
        `SYSTEM_ACTOR` for the whole call keeps that distinction: an
        operator reading `created_by` on a starter server learns nobody
        typed it in, which is true and useful. Attributing it to whoever
        happened to create the tenant would be neither.

        A thin wrapper rather than a `with` inside the body, so the
        several-hundred-line method below stays at one indent level.
        """
        from app.database.models.resource_mixins import SYSTEM_ACTOR, acting_as

        with acting_as(SYSTEM_ACTOR):
            return self._provision_tenant(*args, **kwargs)

    def _provision_tenant(
        self,
        db: Session,
        tenant: Tenant,
        admin_username: str,
        admin_password: str,
        admin_email: str,
        admin_full_name: str = "Tenant Administrator",
        admin_avatar_url: str = "",
        seed_llms: bool = True,
        portal_user: dict = None,
    ) -> ProvisioningResult:
        """
        Fully provision a new tenant.

        Creates roles, default admin, resource group, system servers, and LLMs.
        Caller manages the transaction (commit/rollback).

        Args:
            db: Database session (caller manages transaction)
            tenant: Tenant ORM object (must already be flushed with a valid id)
            admin_username: Username for the default tenant admin
            admin_password: Plaintext password (will be hashed)
            admin_email: Email for the default tenant admin
            admin_full_name: Full name for the admin
            seed_llms: Whether to seed default LLM configurations

        Returns:
            ProvisioningResult with IDs of all created resources
        """
        from app.composition.extensions import PROVISIONING
        from app.composition.provisioning import (
            ProvisioningContext,
            run_provisioning_steps,
        )

        result = ProvisioningResult(
            tenant_id=tenant.id,
            tenant_name=tenant.name,
        )

        # What is created, and in what order, is the TENANT_PROVISIONING list,
        # plus the steps extensions registered (PROVISIONING).
        run_provisioning_steps(
            ProvisioningContext(
                provisioner=self,
                db=db,
                tenant=tenant,
                result=result,
                admin_username=admin_username,
                admin_password=admin_password,
                admin_email=admin_email,
                admin_full_name=admin_full_name,
                admin_avatar_url=admin_avatar_url,
                seed_llms=seed_llms,
                portal_user=portal_user,
            ),
            PROVISIONING.steps(),
        )

        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def get_default_tenant_roles(self):
        """
        Get default roles to seed for each new tenant

        Returns dict with role configurations
        """
        from app.composition.permissions import PERMISSIONS
        from app.composition.principals import PRINCIPALS
        from app.constants.tenant import READ_ONLY_ROLES
        from app.core.admin_user.permissions import Permission

        # Not a tenant administrator's, ever: `system:admin`. And, where the
        # installation has a system administrator (a platform-wide principal
        # is registered), the permission to administer what the installation
        # shares: it is the Tenant Admin's only in a single-tenant
        # installation.
        not_tenant_admins = {Permission.SYSTEM_ADMIN.value}
        if PRINCIPALS.has_platform_wide():
            not_tenant_admins.add(Permission.PLATFORM_CATALOG_MANAGE.value)

        roles = {
            TenantDefaultRole.ADMIN_USER.value: {
                "description": "Full administrative access within tenant (except system admin)",
                "is_system": True,
                "permissions": [
                    # All permissions EXCEPT SYSTEM_ADMIN (sysadmin-only).
                    # Tenant Admin owns their tenant — including the
                    # ability to put it into read-only / drain /
                    # paused. The Maintenance page surfaces an
                    # explicit, name-typed confirm dialog before every
                    # action so an accidental click can't drain the
                    # tenant. Carving the toggle out into a separate
                    # role (an earlier stance) added
                    # friction without proportional safety.
                    value for value in PERMISSIONS.values()
                    if value not in not_tenant_admins
                ]
            },
            TenantDefaultRole.END_USER.value: {
                "description": "Standard user access to portal endpoints",
                "is_system": True,
                "permissions": [
                    Permission.AI_MODEL_READ.value,
                    Permission.SERVER_CONFIG_READ.value
                ]
            },
            TenantDefaultRole.AUDITOR.value: {
                # Compliance / SecOps auditor — sees what *happened*, not
                # what could happen. Deliberately narrower than
                # `Tenant Read-Only` per the separation-of-duties
                # principle: audit logs can carry PII the guardrails
                # were configured to block, so the auditor must NOT also
                # be able to read the configuration that determines
                # what got logged (mcp_servers, models, policies,
                # users). They see proxy_logs and their statistics,
                # admin_audit, the gateway's status, and A2A delegation
                # graphs — everything you'd need to answer "did anyone
                # call tool X with PII payload Y on Tuesday".
                #
                # If you find yourself wanting to add `MCP_SERVER_READ`
                # or `RESOURCE_GROUP_READ` here, you probably want
                # `Tenant Read-Only` instead — it exists for the
                # "broad inspector who is not on the audit team" use
                # case. Keep the audit role narrow.
                "description": (
                    "Compliance / SecOps auditor — read-only access to "
                    "proxy logs and statistics, admin audit logs, and "
                    "A2A delegation graphs. No visibility into platform "
                    "configuration (models, MCP servers, users, policies)."
                ),
                "is_system": True,
                "permissions": [
                    Permission.PROXY_LOGS_READ.value,
                    # Bodies are the auditor's evidence — what was actually
                    # sent and answered — so the audit role keeps them. A
                    # custom "log viewer" role can hold READ without this.
                    Permission.PROXY_LOGS_READ_BODIES.value,
                    Permission.PROXY_STATS_READ.value,
                    Permission.ADMIN_AUDIT_READ.value,
                    Permission.GATEWAY_STATUS_READ.value,
                    Permission.A2A_DELEGATION_READ.value,
                    Permission.A2A_TASK_READ.value,
                    # Inbox items + dispositions are review
                    # EVIDENCE ("did someone look, what did they decide"),
                    # squarely in the auditor's "what happened" remit; no
                    # configuration is exposed through them.
                    Permission.ASSURANCE_INBOX_READ.value,
                    # Check specs + results are likewise evidence — what
                    # was continuously verified, with what spec version.
                    # (Notification channel config stays excluded: it is
                    # configuration, not evidence.)
                    Permission.ASSURANCE_CHECK_READ.value,
                    # Sealed action records, tenant logs, tree
                    # heads and receipts ARE the evidence an auditor comes
                    # for; the framework registry and profiles say which
                    # obligations that evidence is read under. Read only:
                    # witnesses and enablement are configuration.
                    Permission.EVIDENCE_READ.value,
                    Permission.COMPLIANCE_READ.value,
                    # Auditor sees the System Operations
                    # Console's read-only panes (Overview, Counters
                    # status, Cache stats, Sessions list, Audit tab).
                    # The Audit tab is the primary surface for the
                    # auditor role — every operator click is one row.
                    Permission.SYSTEM_READ.value,
                ],
            },
            TenantDefaultRole.READ_ONLY_USER.value: {
                "description": "Read-only access for auditing and monitoring",
                "is_system": True,
                "permissions": [
                    Permission.GUARDRAIL_READ.value,
                    Permission.MCP_SERVER_READ.value,
                    Permission.MCP_SERVER_HEALTH.value,
                    Permission.VIRTUAL_SERVER_READ.value,
                    Permission.PROXY_LOGS_READ.value,
                    Permission.PROXY_LOGS_READ_BODIES.value,
                    Permission.PROXY_STATS_READ.value,
                    Permission.OAUTH_CLIENT_READ.value,
                    Permission.SERVER_CONFIG_READ.value,
                    Permission.IDP_SERVER_READ.value,
                    Permission.ADMIN_USER_READ.value,
                    Permission.ADMIN_AUDIT_READ.value,
                    Permission.END_USER_READ.value,
                    Permission.END_USER_GROUP_READ.value,
                    Permission.RESOURCE_GROUP_READ.value,
                    Permission.RESOURCE_GROUP_MEMBERS_READ.value,
                    Permission.ROLES_READ.value,
                    Permission.PERMISSIONS_READ.value,
                    Permission.GATEWAY_CONFIG_READ.value,
                    Permission.GATEWAY_STATUS_READ.value,
                    Permission.AGENT_SKILL_READ.value,
                    Permission.AI_ASSET_READ.value,
                    Permission.ASSURANCE_INBOX_READ.value,
                    Permission.NOTIFICATION_READ.value,
                    Permission.ASSURANCE_CHECK_READ.value,
                    # Evidence ledger + compliance registry, read.
                    Permission.EVIDENCE_READ.value,
                    Permission.COMPLIANCE_READ.value,
                    Permission.AGENT_IDENTITY_READ.value,
                    Permission.SEMANTIC_POLICY_READ.value,
                    Permission.AI_MODEL_READ.value,
                    Permission.LLM_ROUTER_READ.value,
                    # A2A read-only view of agent cards + tenant key
                    Permission.A2A_CARD_READ.value,
                    Permission.A2A_TENANT_KEY_READ.value,
                    # A2A Mission Control delegation graphs
                    Permission.A2A_DELEGATION_READ.value,
                    # A2A read-only v1.0 task browser
                    Permission.A2A_TASK_READ.value,
                    # The broad-inspector role gets the
                    # System page's read panes too. Sees the same
                    # status grids as Tenant Admin without any of the
                    # action buttons (SYSTEM_OPERATE not granted).
                    Permission.SYSTEM_READ.value,
                ]
            }
        }

        # A permission the registry grants to a default role: the
        # read permissions of the Enterprise Edition features reach Tenant
        # Read-Only this way, each listed beside its feature in
        # `app/composition/permissions.py`. Tenant Admin needs none: it holds
        # every registered permission above.
        for role_name, role_config in roles.items():
            if role_name == TenantDefaultRole.ADMIN_USER.value:
                continue
            held = set(role_config["permissions"])
            role_config["permissions"].extend(
                value for value in PERMISSIONS.default_role_grants(role_name) if value not in held
            )

        # A role that only reads holds read-only permissions and no others.
        for role_name in READ_ONLY_ROLES:
            changing = [value for value in roles[role_name]["permissions"] if not PERMISSIONS.is_read_only(value)]
            if changing:
                raise ValueError(f"{role_name} is a role that only reads and is granted: {changing}")

        return roles


    def _seed_roles(self, db: Session, tenant_id: str) -> Dict[str, str]:
        """
        Create default RBAC roles for a tenant.

        Idempotent — skips roles that already exist.

        Returns:
            Dict mapping role_name -> role_id
        """
        from app.database.models.admin import TenantRole, TenantRolePermission

        role_ids = {}
        default_roles = self.get_default_tenant_roles()

        for role_name, role_config in default_roles.items():
            existing = db.query(TenantRole).filter(
                TenantRole.tenant_id == tenant_id,
                TenantRole.name == role_name,
            ).first()

            if existing:
                role_ids[role_name] = existing.id
                continue

            role_id = f"role-{uuid.uuid4().hex[:12]}"
            role = TenantRole(
                id=role_id,
                tenant_id=tenant_id,
                name=role_name,
                description=role_config["description"],
                is_system=role_config["is_system"],
                created_at=datetime.now(timezone.utc),
            )
            db.add(role)
            db.flush()

            for perm in role_config["permissions"]:
                db.add(TenantRolePermission(
                    id=f"perm-{uuid.uuid4().hex[:12]}",
                    role_id=role.id,
                    permission=perm,
                    created_at=datetime.now(timezone.utc),
                ))

            role_ids[role_name] = role_id
            self.logger.info(f"  ✓ Created role: {role_name} ({len(role_config['permissions'])} perms)", context={
                "tenant_id": tenant_id,
            })

        db.flush()
        return role_ids

    def _create_tenant_admin(
        self,
        db: Session,
        tenant_id: str,
        role_id: str,
        username: str,
        password: str,
        email: str,
        full_name: str,
        avatar_url: str = "",
    ) -> tuple[str, str]:
        """
        Create a tenant admin user with the specified role.

        Returns:
            Tuple of (admin_id, username)
        """
        from app.database.models.admin import TenantAdmin

        # Check for existing admin with same username
        existing = db.query(TenantAdmin).filter(
            TenantAdmin.tenant_id == tenant_id,
            TenantAdmin.username == username,
        ).first()

        if existing:
            return existing.id, existing.username

        from app.core.password_rule import check_password

        check_password(password, "The administrator's password")
        salt = bcrypt.gensalt()
        password_hash = bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")

        admin_id = f"tenant-admin-{uuid.uuid4().hex[:12]}"
        admin = TenantAdmin(
            id=admin_id,
            tenant_id=tenant_id,
            role_id=role_id,
            username=username,
            email=email,
            password_hash=password_hash,
            full_name=full_name,
            avatar_url=avatar_url or None,
            is_active=True,
            is_locked=False,
            failed_login_attempts=0,
            created_by="system",
            created_at=datetime.now(timezone.utc),
        )

        db.add(admin)
        db.flush()

        self.logger.info("Created tenant admin", context={
            "admin_id": admin_id,
            "tenant_id": tenant_id,
            "username": username,
            "role_id": role_id,
        })

        return admin_id, username

    def _create_unbound_resource_group(self, db: Session, tenant_id: str) -> str:
        """
        Create the Unbound system group for a tenant. Idempotent.

        The core bills calls made with the platform service key (model health
        checks, the assurance judge, knowledge-base embeddings, the KB
        uploader) to this group, so that platform spend is visible in one
        place instead of landing on whichever team holds the model. It is
        seeded with no limits: a health check or the judge must not be
        refused for platform spend. Every other call that is not bound to a
        resource group is refused by the core.

        Identified by `is_system` AND the name, so a tenant's own group that
        happens to be called "Unbound" is never taken for it.
        """
        from app.database.models.resource_group import ResourceGroup

        existing = db.query(ResourceGroup).filter(
            ResourceGroup.tenant_id == tenant_id,
            ResourceGroup.name == ResourceGroupName.UNBOUND.value,
            ResourceGroup.is_system == True,
        ).first()
        if existing:
            return existing.id

        group_id = f"group-{ULID()}"
        db.add(ResourceGroup(
            id=group_id,
            tenant_id=tenant_id,
            name=ResourceGroupName.UNBOUND.value,
            display_name=ResourceGroupName.UNBOUND.value,
            description=(
                "System resource group. Platform-internal calls that belong to "
                "no team are recorded here: model health checks, the assurance "
                "judge and knowledge-base embeddings. Any other call that is not "
                "bound to a resource group is refused."
            ),
            group_type="custom",
            parent_group_id=None,
            hierarchy_level=0,
            hierarchy_path=ResourceGroupName.UNBOUND.value,
            inherit_resources=False,
            icon="Shield",
            color="#64748B",
            is_active=True,
            is_system=True,
            created_by="system",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        ))
        db.flush()
        self.logger.info("Created Unbound resource group", context={
            "group_id": group_id, "tenant_id": tenant_id,
        })
        return group_id

    def _create_default_resource_group(self, db: Session, tenant_id: str) -> str:
        """
        Create the Default resource group for a tenant.

        Idempotent — returns existing group ID if already present.

        Returns:
            Resource group ID
        """
        from app.database.models.resource_group import ResourceGroup

        existing = db.query(ResourceGroup).filter(
            ResourceGroup.tenant_id == tenant_id,
            ResourceGroup.name == ResourceGroupName.DEFAULT.value,
            ResourceGroup.is_system == True,
        ).first()

        if existing:
            return existing.id

        group_id = f"group-{ULID()}"
        group = ResourceGroup(
            id=group_id,
            tenant_id=tenant_id,
            name=ResourceGroupName.DEFAULT.value,
            display_name=ResourceGroupName.DEFAULT.value,
            description=(
                "System resource group. All seeded models are assigned here. "
                f"Models in {ResourceGroupName.DEFAULT.value} are accessible to all users. "
                "Remove a model from Default to restrict it to specific groups only."
            ),
            group_type="custom",
            parent_group_id=None,
            hierarchy_level=0,
            hierarchy_path=ResourceGroupName.DEFAULT.value,
            inherit_resources=False,
            icon="Shield",
            color="#6366F1",
            is_active=True,
            is_system=True,
            created_by="system",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

        db.add(group)
        db.flush()

        self.logger.info("Created Default resource group", context={
            "group_id": group_id, "tenant_id": tenant_id,
        })

        return group_id