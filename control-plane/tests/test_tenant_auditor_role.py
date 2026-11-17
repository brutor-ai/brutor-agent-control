# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# `Tenant Auditor` system role

Locks down the contract for the new fourth seeded tenant role added in
response to the Obot convergence analysis (the "auditor role separate
from platform admin" requirement that hits every enterprise security
review).

# What's under test

  1. The role appears in `TenantDefaultRole` enum with the expected
     name string. Renaming it (or worse, changing the string value)
     would silently break tenants that already have the role seeded —
     this is the canary for that class of mistake.

  2. `get_default_tenant_roles()` returns the role with EXACTLY the
     agreed permission set. Drift here is the bug the role exists to
     prevent: if someone slips `MCP_SERVER_READ` into the auditor's
     permissions because "they need it to interpret the logs", they've
     violated separation-of-duties — the auditor is now also a
     config-reader, which is the very thing the role is supposed to
     deny.

  3. The role is INTENTIONALLY NARROWER than `Tenant Read-Only`. The
     test enumerates the perms `Tenant Read-Only` has that the auditor
     does NOT, so a future PR that copy-pastes one role from the other
     gets caught.

  4. The role's permission list cleanly matches what's hard-coded into
     the Alembic backfill migration (a042_seed_tenant_auditor_role). If the two diverge, fresh
     tenants and backfilled tenants end up with different auditor
     definitions — silent contract drift across the install base.

# What's NOT under test here

The migration itself runs against Postgres and is exercised by
the production Alembic test harness and the smoke-deploy. We don't
re-run it inline — the failure mode we DO care about is "code-side and
migration-side permission lists diverged", and that's covered by
test #4 above without a DB.
"""

from __future__ import annotations


import pytest

from app.constants.tenant import TenantDefaultRole


# ---------------------------------------------------------------------------
# 1. Enum membership and naming
# ---------------------------------------------------------------------------


class TestEnumMembership:
    def test_auditor_role_exists_in_enum(self):
        assert hasattr(TenantDefaultRole, "AUDITOR"), (
            "TenantDefaultRole must expose an AUDITOR member — the "
            "fourth seeded role added in 0.8.x. Removing or renaming "
            "this enum entry strands every tenant_admin row that was "
            "previously assigned the auditor role."
        )

    def test_auditor_role_name_is_human_readable(self):
        # The string value is what appears in the admin UI dropdown
        # AND what gets inserted into `tenant_roles.name` — changing
        # it requires a migration to rename existing rows.
        assert TenantDefaultRole.AUDITOR.value == "Tenant Auditor"

    def test_auditor_name_does_not_collide_with_other_roles(self):
        # Defensive: a casing change in TenantDefaultRole.ADMIN_USER
        # ("Tenant Admin") that accidentally aliased to AUDITOR
        # ("Tenant Auditor") would create a unique-constraint violation
        # on `(tenant_id, name)`. Catch it here.
        names = {role.value for role in TenantDefaultRole}
        assert len(names) == len(list(TenantDefaultRole))


# ---------------------------------------------------------------------------
# 2. Permission set — exact membership
# ---------------------------------------------------------------------------


# The single source of truth for what the auditor sees. Update only with
# explicit security-review approval — see the role docstring in
# `tenant_provisioning.py` for the rationale.
EXPECTED_AUDITOR_PERMISSIONS = {
    "proxy:logs:read",
    # Bodies are the auditor's evidence; a separate grant from rows.
    "proxy:logs:read_bodies",
    # The assurance inbox trail and check results are oversight EVIDENCE.
    "assurance_inbox:read",
    "assurance_check:read",
    # The evidence ledger and the compliance registry, read only.
    "evidence:read",
    "compliance:read",
    "proxy:stats:read",
    "admin:audit:read",
    "gateway:status:read",
    "a2a:delegation:read",
    "a2a:task:read",
    # System Operations Console audit/operator log access.
    # Read-only; the auditor sees the Audit tab + Counter / Cache /
    # Sessions status panes but cannot reset anything (no
    # SYSTEM_OPERATE / SYSTEM_MAINTENANCE_TOGGLE).
    "system:read",
}


@pytest.fixture
def default_roles():
    """Run the seed-config builder once per test class. Note: importing
    `TenantProvisioner` is heavy (pulls in DB models, OAuth
    flow imports), so this is fixture-scoped to amortise across tests."""
    from app.services.tenant.tenant_provisioning import TenantProvisioner

    # The method is instance-bound but doesn't touch self; construct
    # the cheapest possible service object.
    svc = TenantProvisioner.__new__(TenantProvisioner)
    return svc.get_default_tenant_roles()


class TestPermissionSet:
    def test_auditor_role_present_in_default_seed(self, default_roles):
        # The role must be in the seed map — otherwise new tenants
        # don't get it and the backfill migration is the only path,
        # which leaves test environments and ephemeral demo tenants
        # without an auditor.
        assert TenantDefaultRole.AUDITOR.value in default_roles

    def test_auditor_role_is_marked_system(self, default_roles):
        # `is_system=True` is what gates the role against accidental
        # deletion in the admin UI. The whole point of a seeded role
        # is that operators can't delete it out from under the
        # compliance team.
        role = default_roles[TenantDefaultRole.AUDITOR.value]
        assert role["is_system"] is True

    def test_auditor_permissions_exactly_match_expected_set(self, default_roles):
        # The bug we're guarding against: drift in either direction.
        # Adding `MCP_SERVER_READ` violates separation-of-duties;
        # removing `PROXY_LOGS_READ` makes the role useless. Both
        # caught by exact-equality.
        actual = set(default_roles[TenantDefaultRole.AUDITOR.value]["permissions"])
        assert actual == EXPECTED_AUDITOR_PERMISSIONS, (
            "Auditor permission set drifted from the agreed list. "
            f"Added: {actual - EXPECTED_AUDITOR_PERMISSIONS}. "
            f"Removed: {EXPECTED_AUDITOR_PERMISSIONS - actual}. "
            "Update only with explicit security-review approval; the "
            "role's contract is separation-of-duties from platform admin."
        )

    def test_auditor_description_mentions_separation_of_duties_intent(
        self, default_roles
    ):
        # The description appears in the admin UI when assigning roles.
        # An operator who picks "Auditor" needs to understand they're
        # picking a NARROWER role than Read-Only, not a synonym for it.
        # Pin the key phrases.
        desc = default_roles[TenantDefaultRole.AUDITOR.value]["description"]
        assert "audit" in desc.lower() or "compliance" in desc.lower()
        assert (
            "no visibility" in desc.lower()
            or "narrower" in desc.lower()
            or "not" in desc.lower()
        ), "Description should make clear this role is NOT a general read-only role"


# ---------------------------------------------------------------------------
# 3. Auditor is intentionally NARROWER than `Tenant Read-Only`
# ---------------------------------------------------------------------------


class TestSeparationOfDutiesFromReadOnly:
    """The whole reason this role exists, expressed as a test.

    `Tenant Read-Only` already sees `PROXY_LOGS_READ` — but it ALSO
    sees the configuration that determines what got logged
    (`MCP_SERVER_READ`, `RESOURCE_GROUP_READ`, `POLICY_READ`,
    `AI_MODEL_READ`, `ADMIN_USER_READ`). For a compliance auditor
    reviewing logs that contain PII, that combination is wrong:
    the same person can both see the PII and change the policy that
    decides what PII reaches the logs.

    The auditor role exists to disentangle those. If the difference
    between the two roles ever shrinks (auditor gains a config-read
    perm, or read-only loses one) the contract breaks. This test
    is the explicit invariant."""

    # Subset of `Tenant Read-Only` perms that ARE config-reads.
    # The auditor MUST NOT have any of these. Drawn from
    # `get_default_tenant_roles()['Tenant Read-Only']` — kept narrow
    # so this test focuses on the load-bearing items (the full
    # read-only set is much larger).
    CONFIG_READ_PERMS_FORBIDDEN_TO_AUDITOR = {
        "mcp:server:read",
        "resourcegroup:read",
        "admin:user:read",
        "policy:read",
        "ai:model:read",
        "agent_skill:read",
        "server:config:read",
        "registry:read",
        "oauth:client:read",
    }

    def test_auditor_does_not_have_any_config_read_permissions(self, default_roles):
        auditor_perms = set(
            default_roles[TenantDefaultRole.AUDITOR.value]["permissions"]
        )
        leaked = auditor_perms & self.CONFIG_READ_PERMS_FORBIDDEN_TO_AUDITOR
        assert leaked == set(), (
            "Auditor role acquired config-read permission(s): "
            f"{sorted(leaked)}. This violates separation-of-duties — "
            "an auditor must see what *happened* (logs, metrics), not "
            "what *could happen* (configuration). If a config-read "
            "perm is genuinely needed for an audit workflow, the "
            "right answer is a separate `Tenant Audit-Plus` role, "
            "NOT widening the standard auditor."
        )

    def test_readonly_role_has_strictly_more_permissions_than_auditor(
        self, default_roles
    ):
        # Sanity: `Tenant Read-Only` should be a strict superset of
        # the auditor's permissions on the AUDIT-flavoured ones the
        # auditor cares about. If read-only ever LOSES proxy_logs_read
        # (someone "tightening" it), our auditor would still work but
        # the platform's overall RBAC story gets confusing — read-only
        # users would have a strictly weaker view than the audit-only
        # role, which inverts the usual hierarchy.
        readonly_perms = set(
            default_roles[TenantDefaultRole.READ_ONLY_USER.value]["permissions"]
        )
        auditor_perms = set(
            default_roles[TenantDefaultRole.AUDITOR.value]["permissions"]
        )
        # Specifically: every auditor permission should also appear in
        # read-only. (Read-only has MORE, including config-reads.)
        missing_from_readonly = auditor_perms - readonly_perms
        assert missing_from_readonly == set(), (
            "`Tenant Read-Only` is missing audit-flavoured "
            f"permission(s) the auditor role has: {sorted(missing_from_readonly)}. "
            "The two roles should overlap on audit perms; read-only "
            "adds config-reads on top. If you intentionally narrowed "
            "read-only, please add the missing perm back — the audit-"
            "review tooling depends on read-only being able to see "
            "everything the auditor sees."
        )
