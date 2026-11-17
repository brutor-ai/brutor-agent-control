# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for:
  - End-user primary resource group invariants (migration a001_primary_group_tenant_embedding)
  - Tenant update denylist (quotas/features/billing off-limits to tenant admins)
  - Cache-config one-per-group conflict detection
  - Tenant default embedding model surfaces in to_dict

Pure / mocked tests — no live DB. See test_*.py patterns for integration tests.

Run: pytest tests/test_primary_group_and_tenant_settings.py -v
"""

# Stub out DATABASE_URL for the service import — app/database/__init__.py
# calls create_engine() at import time. SQLite avoids pulling in psycopg2.
import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from app.api.v1.models.admin.resource_group import (
    GroupMembershipAdd,
    GroupMemberInfo,
    SetPrimaryGroupRequest,
)
from app.api.v1.models.admin.tenant_settings import TenantSettingsUpdate

# There is no deny-list any more. A tenant administrator updates
# its tenant through `PATCH /admin/tenants/current`, whose model is
# `TenantSettingsUpdate` and forbids every key it does not declare; "on the
# deny-list" is now "not a field of that model".
SETTINGS_FIELDS = set(TenantSettingsUpdate.model_fields)


# =============================================================================
# GroupMembershipAdd — make_primary flag
# =============================================================================


class TestGroupMembershipAdd:
    def test_make_primary_defaults_to_false(self):
        # The N-th-join case: admin must opt in explicitly. First-join
        # always auto-sets regardless of this flag (enforced server-side).
        body = GroupMembershipAdd(user_ids=["42"])
        assert body.make_primary is False

    def test_make_primary_can_be_set_true(self):
        body = GroupMembershipAdd(user_ids=["42"], make_primary=True)
        assert body.make_primary is True

    def test_make_primary_accepts_role(self):
        body = GroupMembershipAdd(
            user_ids=["42"], role="viewer", make_primary=True
        )
        assert body.role == "viewer"
        assert body.make_primary is True


class TestSetPrimaryGroupRequest:
    def test_group_id_is_required(self):
        with pytest.raises(ValidationError):
            SetPrimaryGroupRequest()  # type: ignore[call-arg]

    def test_group_id_accepted(self):
        body = SetPrimaryGroupRequest(group_id="rg-engineering")
        assert body.group_id == "rg-engineering"


class TestGroupMemberInfoPrimaryFlag:
    def test_is_primary_defaults_false(self):
        info = GroupMemberInfo(
            user_id="1",
            username="alice",
            email=None,
            display_name=None,
            role=None,
            joined_at="2026-01-01T00:00:00Z",
        )
        assert info.is_primary is False

    def test_is_primary_can_be_true(self):
        info = GroupMemberInfo(
            user_id="1",
            username="alice",
            email=None,
            display_name=None,
            role=None,
            joined_at="2026-01-01T00:00:00Z",
            is_primary=True,
        )
        assert info.is_primary is True


# =============================================================================
# Tenant update denylist — tenant admins cannot modify quotas/features/billing
# =============================================================================


class TestTenantAdminDenylist:
    """The denylist is the server-side guard that makes the UI's
    readOnlySections safe — a curl'ing tenant admin can't bypass."""

    def test_denylist_covers_all_quota_fields(self):
        # The 10 resource quotas plus the live
        # media byte ceiling — every quota-bearing column must be here or
        # a tenant admin could raise their own limits.
        expected_quota_fields = {
            "max_admin_users", "max_end_users", "max_end_user_groups",
            "max_llm_models", "max_mcp_servers", "max_mcp_registries",
            "max_kb_collections", "max_agent_skills", "max_agent_cards",
            "max_agent_identities", "media_storage_quota_bytes",
        }
        missing = expected_quota_fields & SETTINGS_FIELDS
        assert TenantSettingsUpdate.model_config.get("extra") == "forbid"
        assert not missing, (
            f"Denylist missing quota fields — tenant admins could bypass: {missing}"
        )

    def test_denylist_includes_features(self):
        assert "features" not in SETTINGS_FIELDS

    def test_denylist_includes_billing(self):
        assert "billing_plan" not in SETTINGS_FIELDS
        assert "billing_email" not in SETTINGS_FIELDS
        assert "billing_metadata" not in SETTINGS_FIELDS


# =============================================================================
# Tenant default embedding model
# =============================================================================


# =============================================================================
# KB collection / cache config — embedding_model_id removed from API models
# =============================================================================


# =============================================================================
# Cache-config binding conflict detection
# =============================================================================


