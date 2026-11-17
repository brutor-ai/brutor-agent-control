# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# System Operations Console — permissions + role-seed regressions

Three contracts:

  1. **The three new permissions exist on the Permission enum** with
     the wire-format strings the endpoint guards will use, and each
     has an entry in `PERMISSION_DESCRIPTIONS` (operators read the
     description in the role editor).
  2. **The default tenant-admin grant INCLUDES
     `SYSTEM_MAINTENANCE_TOGGLE`** — Tenant Admin owns their tenant
     and gets the toggle; the Maintenance pane prevents accidents via
     a typed-tenant-id confirm dialog rather than a permission split.
     Only `SYSTEM_ADMIN` stays sysadmin-only.
  3. **Auditor and Read-Only roles get `SYSTEM_READ`** so the audit-
     trail and inspector flows surface the System page's read panes
     without granting any destructive actions. They still must NOT
     get `SYSTEM_OPERATE` or `SYSTEM_MAINTENANCE_TOGGLE`.

These are pure-Python configuration assertions — no DB harness
required. The endpoint-layer permission checks (`require_permission`)
are exercised in the per-endpoint test packs.
"""

import pytest

from app.core.admin_user.permissions import (
    PERMISSION_DESCRIPTIONS,
    Permission,
    get_all_permission_strings,
    validate_permission,
)
from app.services.tenant.tenant_provisioning import (
    TenantDefaultRole,
    TenantProvisioner,
)


# ---------------------------------------------------------------------------
# Enum + descriptions
# ---------------------------------------------------------------------------


class TestPermissionEnum:
    """The three permissions land on the wire as `system:read` etc.
    Any rename here is a wire-protocol break."""

    def test_system_read_wire_format(self):
        assert Permission.SYSTEM_READ.value == "system:read"

    def test_system_operate_wire_format(self):
        assert Permission.SYSTEM_OPERATE.value == "system:operate"

    def test_system_maintenance_toggle_wire_format(self):
        assert (
            Permission.SYSTEM_MAINTENANCE_TOGGLE.value
            == "system:maintenance:toggle"
        )

    @pytest.mark.parametrize(
        "perm",
        (
            Permission.SYSTEM_READ,
            Permission.SYSTEM_OPERATE,
            Permission.SYSTEM_MAINTENANCE_TOGGLE,
        ),
    )
    def test_permission_appears_in_strings_list(self, perm: Permission):
        # The strings list is what the admin UI shows in the role
        # editor and what `validate_permission` accepts on the wire.
        assert perm.value in get_all_permission_strings()
        assert validate_permission(perm.value) is True

    @pytest.mark.parametrize(
        "perm",
        (
            Permission.SYSTEM_READ,
            Permission.SYSTEM_OPERATE,
            Permission.SYSTEM_MAINTENANCE_TOGGLE,
        ),
    )
    def test_permission_has_description(self, perm: Permission):
        # Without a description the role editor renders an empty cell;
        # operators read the description to decide whether to grant.
        assert perm in PERMISSION_DESCRIPTIONS
        text = PERMISSION_DESCRIPTIONS[perm]
        assert isinstance(text, str) and len(text) > 0

    def test_distinct_wire_strings(self):
        # Belt-and-braces: a copy-paste typo could make two perms map
        # to the same wire string.
        values = {
            Permission.SYSTEM_READ.value,
            Permission.SYSTEM_OPERATE.value,
            Permission.SYSTEM_MAINTENANCE_TOGGLE.value,
        }
        assert len(values) == 3


# ---------------------------------------------------------------------------
# Tenant Admin — the carve-out is the whole point of the permission split
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def default_roles() -> dict:
    """Provisioning service holds the canonical role → permissions
    mapping. Call once per test module — the dict is just config."""
    from app.core.logging_system import BrutorLogger
    return TenantProvisioner(logger=BrutorLogger(__name__)).get_default_tenant_roles()


class TestTenantAdminCarveOut:
    """Tenant Admin owns their tenant: SYSTEM_READ, SYSTEM_OPERATE,
    and SYSTEM_MAINTENANCE_TOGGLE all default on. Accidental
    maintenance-mode toggles are prevented by the name-typed confirm
    dialog on the Maintenance page, not by an RBAC carve-out — see the
    Maintenance pane in the admin UI. Only SYSTEM_ADMIN stays
    sysadmin-only."""

    def test_admin_role_seeded(self, default_roles):
        assert TenantDefaultRole.ADMIN_USER.value in default_roles

    def test_admin_gets_system_read(self, default_roles):
        perms = default_roles[TenantDefaultRole.ADMIN_USER.value]["permissions"]
        assert Permission.SYSTEM_READ.value in perms

    def test_admin_gets_system_operate(self, default_roles):
        perms = default_roles[TenantDefaultRole.ADMIN_USER.value]["permissions"]
        assert Permission.SYSTEM_OPERATE.value in perms

    def test_admin_gets_maintenance_toggle(self, default_roles):
        # Tenant Admin defaults include the maintenance toggle — the
        # name-typed confirm dialog on the Maintenance page is the
        # real safety net against accidental drain / pause.
        perms = default_roles[TenantDefaultRole.ADMIN_USER.value]["permissions"]
        assert Permission.SYSTEM_MAINTENANCE_TOGGLE.value in perms

    def test_admin_does_not_get_system_admin(self, default_roles):
        # Sanity: the SYSTEM_ADMIN exclusion must still hold. If a
        # future refactor broadens the comprehension we'd ship tenant
        # admins with sysadmin powers; lock it.
        perms = default_roles[TenantDefaultRole.ADMIN_USER.value]["permissions"]
        assert Permission.SYSTEM_ADMIN.value not in perms


# ---------------------------------------------------------------------------
# Auditor + Read-Only — both get SYSTEM_READ, neither gets writes
# ---------------------------------------------------------------------------


class TestAuditorRole:
    """The Auditor is the primary consumer of the Audit tab. Must see
    the operator audit log; must NOT see destructive actions."""

    def test_auditor_gets_system_read(self, default_roles):
        perms = default_roles[TenantDefaultRole.AUDITOR.value]["permissions"]
        assert Permission.SYSTEM_READ.value in perms

    def test_auditor_does_not_get_system_operate(self, default_roles):
        perms = default_roles[TenantDefaultRole.AUDITOR.value]["permissions"]
        assert Permission.SYSTEM_OPERATE.value not in perms

    def test_auditor_does_not_get_maintenance_toggle(self, default_roles):
        perms = default_roles[TenantDefaultRole.AUDITOR.value]["permissions"]
        assert Permission.SYSTEM_MAINTENANCE_TOGGLE.value not in perms

    def test_auditor_still_narrow(self, default_roles):
        # Belt-and-braces against future drift: the auditor role is
        # deliberately narrow per the existing module comment. Adding
        # SYSTEM_READ shouldn't have accidentally unlocked, say,
        # MCP_SERVER_READ. Spot-check a forbidden permission.
        perms = default_roles[TenantDefaultRole.AUDITOR.value]["permissions"]
        assert Permission.MCP_SERVER_READ.value not in perms
        assert Permission.RESOURCE_GROUP_READ.value not in perms


class TestReadOnlyRole:
    """Broad inspector — sees everything safe to view. Should pick up
    SYSTEM_READ alongside the other *_READ permissions it already
    carries."""

    def test_read_only_gets_system_read(self, default_roles):
        perms = default_roles[TenantDefaultRole.READ_ONLY_USER.value]["permissions"]
        assert Permission.SYSTEM_READ.value in perms

    def test_read_only_does_not_get_system_operate(self, default_roles):
        perms = default_roles[TenantDefaultRole.READ_ONLY_USER.value]["permissions"]
        assert Permission.SYSTEM_OPERATE.value not in perms

    def test_read_only_does_not_get_maintenance_toggle(self, default_roles):
        perms = default_roles[TenantDefaultRole.READ_ONLY_USER.value]["permissions"]
        assert Permission.SYSTEM_MAINTENANCE_TOGGLE.value not in perms


# ---------------------------------------------------------------------------
# Tenant User — must NOT see the System page at all
# ---------------------------------------------------------------------------


class TestEndUserRoleHasNoSystemAccess:
    """End User is the default chat-portal role. The System page nav
    item is hidden when SYSTEM_READ is absent; assert it stays
    absent."""

    def test_end_user_has_no_system_permissions(self, default_roles):
        perms = default_roles[TenantDefaultRole.END_USER.value]["permissions"]
        for forbidden in (
            Permission.SYSTEM_READ.value,
            Permission.SYSTEM_OPERATE.value,
            Permission.SYSTEM_MAINTENANCE_TOGGLE.value,
        ):
            assert forbidden not in perms, (
                f"End User role granted {forbidden} by accident — "
                f"the System page would render in the portal nav."
            )
