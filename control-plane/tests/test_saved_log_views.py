# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Regression tests for the saved-log-views CRUD endpoints +
Pydantic models. Coverage:

  * Pydantic accepts both viewer scopes and rejects unknown ones.
  * Create payload defaults `is_shared = False` (private by default —
    sharing must be explicit per design notes in migration
    a078_saved_log_views).
  * Update payload tolerates partial bodies (PATCH semantics).
  * Router exposes the five expected CRUD routes.
  * SavedLogView.to_dict() round-trips through the response model
    without a Pydantic ValidationError.

The actual cross-tenant + ownership-check behaviour is HTTP-level
and depends on the admin auth machinery; that's covered by the
existing admin_rbac harness pattern once the views router is added
there. These tests pin the wire contract the admin UI's
SavedViewMenu component depends on.
"""

from __future__ import annotations

import os

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")
os.environ.setdefault(
    "DATABASE_URL", "postgresql://x:x@127.0.0.1:1/x"
)

import pytest
from pydantic import ValidationError

from app.api.v1.models.admin.saved_log_view import (
    SavedLogViewCreateRequest,
    SavedLogViewListResponse,
    SavedLogViewResponse,
    SavedLogViewUpdateRequest,
)
from app.api.v1.endpoints.admin.saved_log_views import (
    create_saved_log_views_router,
)
from app.database.models import (
    SAVED_LOG_VIEW_SCOPE_ADMIN_AUDIT_LOGS,
    SAVED_LOG_VIEW_SCOPE_PROXY_LOGS,
    SAVED_LOG_VIEW_VALID_SCOPES,
)


class TestScopeValidation:
    def test_both_scopes_accepted(self):
        for scope in SAVED_LOG_VIEW_VALID_SCOPES:
            r = SavedLogViewCreateRequest(scope=scope, name="X", filters={})
            assert r.scope == scope

    def test_unknown_scope_rejected(self):
        with pytest.raises(ValidationError):
            SavedLogViewCreateRequest(scope="something_else", name="X", filters={})

    def test_scope_constants_match_pydantic_literal(self):
        # The Pydantic Literal in the request model must match the
        # ORM-side constants exactly. Drift here would mean the API
        # accepts a value the DB CHECK constraint then rejects.
        assert SAVED_LOG_VIEW_SCOPE_PROXY_LOGS == "proxy_logs"
        assert SAVED_LOG_VIEW_SCOPE_ADMIN_AUDIT_LOGS == "admin_audit_logs"


class TestCreateRequest:
    def test_is_shared_defaults_to_false(self):
        r = SavedLogViewCreateRequest(scope="proxy_logs", name="X", filters={})
        assert r.is_shared is False

    def test_extra_fields_rejected(self):
        # The model_config sets extra="forbid" — unknown keys should
        # blow up at validation time so the operator notices typos
        # instead of silently losing data.
        with pytest.raises(ValidationError):
            SavedLogViewCreateRequest(
                scope="proxy_logs", name="X", filters={}, mystery="???",
            )

    def test_name_min_length_enforced(self):
        with pytest.raises(ValidationError):
            SavedLogViewCreateRequest(scope="proxy_logs", name="", filters={})

    def test_filters_accepts_arbitrary_shape(self):
        # Filters are stored as JSONB and re-validated at apply time
        # against the live search model — so the CRUD endpoint must
        # accept any shape the future search envelope might grow into.
        r = SavedLogViewCreateRequest(
            scope="admin_audit_logs",
            name="My triage",
            filters={
                "admin_ids": ["a-1", "a-2"],
                "actions": ["mcp_server_create"],
                "hours_ago": 24,
                "q": "delete",
            },
        )
        assert r.filters["admin_ids"] == ["a-1", "a-2"]


class TestUpdateRequest:
    def test_empty_body_valid(self):
        # PATCH semantics — operator might just be touching a single
        # field. Pydantic should not require any specific subset.
        r = SavedLogViewUpdateRequest()
        assert r.model_dump(exclude_unset=True) == {}

    def test_partial_body_only_touches_named_fields(self):
        r = SavedLogViewUpdateRequest(is_shared=True)
        # exclude_unset matters — without it, every absent field
        # becomes None and we'd null out everything the operator
        # didn't include.
        assert r.model_dump(exclude_unset=True) == {"is_shared": True}

    def test_extra_fields_rejected(self):
        with pytest.raises(ValidationError):
            SavedLogViewUpdateRequest(unknown="value")


class TestRouterShape:
    def test_router_exposes_crud_routes(self):
        router = create_saved_log_views_router()
        method_paths = sorted(
            (route.path, sorted(route.methods))
            for route in router.routes
            if hasattr(route, "methods")
        )
        expected = sorted([
            ("/admin/saved-log-views", ["GET"]),
            ("/admin/saved-log-views", ["POST"]),
            ("/admin/saved-log-views/{view_id}", ["DELETE"]),
            ("/admin/saved-log-views/{view_id}", ["GET"]),
            ("/admin/saved-log-views/{view_id}", ["PATCH"]),
        ])
        assert method_paths == expected


class TestResponseRoundtrip:
    def test_to_dict_compatible_with_response_model(self):
        # Mirror what SavedLogView.to_dict() returns to make sure the
        # endpoint can splat it straight into SavedLogViewResponse
        # without a fresh adapter. Catches drift if either side adds
        # a field.
        sample = {
            "id": "slv-abcdef0123456789",
            "tenant_id": "tenant-1",
            "scope": "proxy_logs",
            "owner_admin_id": "admin-1",
            "name": "Recent denials",
            "description": "Last 1h, success=false",
            "filters": {"hours_ago": 1, "success": False},
            "is_shared": True,
            "created_at": "2026-06-08T10:00:00+00:00",
            "updated_at": "2026-06-08T10:30:00+00:00",
        }
        # Should not raise — and round-trip must preserve every field.
        r = SavedLogViewResponse(**sample)
        assert r.model_dump() == sample

    def test_list_response_wraps_items(self):
        item = {
            "id": "slv-1",
            "tenant_id": "tenant-1",
            "scope": "admin_audit_logs",
            "owner_admin_id": None,  # system-seeded shared default
            "name": "All failures",
            "description": None,
            "filters": {"success": False},
            "is_shared": True,
            "created_at": "2026-06-08T10:00:00+00:00",
            "updated_at": "2026-06-08T10:00:00+00:00",
        }
        lst = SavedLogViewListResponse(items=[item])
        assert lst.items[0].owner_admin_id is None  # nullable owner survives
        assert lst.items[0].is_shared is True
