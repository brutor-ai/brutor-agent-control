# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Compliance admin router structural + helper tests.

Pins the public surface (route list + Pydantic boundary shapes)
and the pure helpers (`_parse_window`, `_resolve_tenant_id`).
DB-level integration is left for the live-DB e2e suite — the
helpers + Pydantic side of the router are the boundary we want
to lock against drift.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1.endpoints.admin.compliance import (
    _parse_window,
    _resolve_tenant_id,
    create_compliance_admin_router,
)
from app.api.v1.models.admin.compliance import (
    ComplianceProfile,
    ResourceGroupComplianceFields,
)
from app.constants.admin import AdminType


# ---------------------------------------------------------------------------
# Router public surface
# ---------------------------------------------------------------------------


class TestRouterSurface:
    def test_router_registers_all_eight_endpoints(self):
        """Eight endpoints under /admin/compliance/ — drift in this
        count usually means an endpoint was renamed or removed
        without updating the docs."""
        router = create_compliance_admin_router(logging.getLogger("t"))
        paths = sorted(
            (route.path, tuple(sorted(route.methods)))
            for route in router.routes
        )
        expected = sorted([
            ("/admin/compliance/dashboard", ("GET",)),
            ("/admin/compliance/eu-ai-act/risk-classification", ("GET",)),
            ("/admin/compliance/gdpr/article-30", ("GET",)),
            ("/admin/compliance/hipaa/phi-access", ("GET",)),
            ("/admin/compliance/iso-42001/activity", ("GET",)),
            ("/admin/compliance/profile", ("GET",)),
            ("/admin/compliance/profile", ("PATCH",)),
            ("/admin/compliance/soc2/control-coverage", ("GET",)),
        ])
        assert paths == expected, (
            f"route surface drift; got {paths}"
        )

    def test_router_carries_compliance_tag(self):
        # OpenAPI groups by `tags=...`; misnaming this would split
        # the compliance endpoints across UI sections.
        router = create_compliance_admin_router(logging.getLogger("t"))
        assert router.tags == ["Compliance"]


# ---------------------------------------------------------------------------
# Pydantic boundary shapes
# ---------------------------------------------------------------------------


class TestComplianceProfileValidator:
    def test_accepts_known_frameworks(self):
        p = ComplianceProfile(frameworks=["gdpr_art_30", "soc2"])
        assert p.frameworks == ["gdpr_art_30", "soc2"]

    def test_rejects_unknown_framework(self):
        with pytest.raises(ValueError, match="unknown framework"):
            ComplianceProfile(frameworks=["gdpr_art_30", "fictional_regime"])

    def test_dedupes_and_lowercases(self):
        # Operators sometimes paste from spreadsheets with mixed
        # case / dupes. Pydantic normalises so the persisted
        # JSONB stays clean.
        p = ComplianceProfile(frameworks=["GDPR_ART_30", "soc2", "gdpr_art_30"])
        assert p.frameworks == ["gdpr_art_30", "soc2"]

    def test_empty_list_is_valid_default(self):
        # The default "no enforcement" state.
        p = ComplianceProfile()
        assert p.frameworks == []


class TestRgComplianceFieldsValidator:
    def test_accepts_known_risk_tier(self):
        rg = ResourceGroupComplianceFields(eu_ai_act_risk_tier="high")
        assert rg.eu_ai_act_risk_tier == "high"

    def test_rejects_unknown_risk_tier(self):
        with pytest.raises(ValueError, match="eu_ai_act_risk_tier"):
            ResourceGroupComplianceFields(eu_ai_act_risk_tier="catastrophic")

    def test_null_explicitly_clears(self):
        # The admin UI submits None to clear an existing tier.
        rg = ResourceGroupComplianceFields(eu_ai_act_risk_tier=None)
        assert rg.eu_ai_act_risk_tier is None


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


class TestResolveTenantId:
    def test_tenant_admin_gets_own_tenant(self):
        admin = SimpleNamespace(tenant_id="t-1")
        out = _resolve_tenant_id(admin, AdminType.TENANT_ADMIN_TYPE, None)
        assert out == "t-1"

    def test_tenant_admin_rejected_when_overriding(self):
        admin = SimpleNamespace(tenant_id="t-1")
        with pytest.raises(HTTPException) as exc:
            _resolve_tenant_id(admin, AdminType.TENANT_ADMIN_TYPE, "t-2")
        assert exc.value.status_code == 403

    def test_tenant_admin_silently_accepts_own_id_in_query(self):
        # Some admin UIs pass the tenant_id explicitly even when
        # the admin is scoped to it. We accept the no-op case.
        admin = SimpleNamespace(tenant_id="t-1")
        out = _resolve_tenant_id(admin, AdminType.TENANT_ADMIN_TYPE, "t-1")
        assert out == "t-1"


class TestParseWindow:
    def test_default_is_last_30_days(self):
        from_dt, to_dt = _parse_window(None, None, default_days=30)
        # 30-day window, approximately. Don't pin exactly because
        # time passes during the test.
        delta = (to_dt - from_dt).total_seconds()
        assert 29 * 86400 <= delta <= 31 * 86400

    def test_default_days_override(self):
        from_dt, to_dt = _parse_window(None, None, default_days=7)
        delta = (to_dt - from_dt).total_seconds()
        assert 6 * 86400 <= delta <= 8 * 86400

    def test_explicit_window_iso_8601(self):
        from_dt, to_dt = _parse_window(
            "2026-01-01T00:00:00Z",
            "2026-01-31T23:59:59Z",
            default_days=30,
        )
        assert from_dt == datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert to_dt == datetime(2026, 1, 31, 23, 59, 59, tzinfo=timezone.utc)

    def test_unparseable_date_returns_400(self):
        with pytest.raises(HTTPException) as exc:
            _parse_window("not-a-date", None)
        assert exc.value.status_code == 400

    def test_inverted_window_returns_400(self):
        # from >= to is operator error, not silent shift.
        with pytest.raises(HTTPException) as exc:
            _parse_window(
                "2026-01-31T00:00:00Z",
                "2026-01-01T00:00:00Z",
            )
        assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# Cross-reference vs the engine
# ---------------------------------------------------------------------------


class TestDashboardCardsCoverAllFrameworks:
    def test_dashboard_handler_imports_match_engine_constants(self):
        """The dashboard handler's `framework_tag_map` lists the
        top-level tags emitted by the engine. Verify every key
        of FRAMEWORK_KEYS appears so the dashboard never has a
        silent gap (a framework that fires tags but has no card)."""
        from app.api.v1.endpoints.admin.compliance import (
            create_compliance_admin_router,
        )
        from app.api.v1.models.admin.compliance import FRAMEWORK_KEYS

        # Run the router builder so the closure runs; then pull
        # the framework_tag_map out of the closure cell. We don't
        # have a clean public hook, so inspect the source.
        import inspect
        source = inspect.getsource(create_compliance_admin_router)
        for fw in FRAMEWORK_KEYS:
            assert f'"{fw}"' in source, (
                f"framework {fw!r} from FRAMEWORK_KEYS missing from "
                f"dashboard framework_tag_map — every regime needs a "
                f"card or the operator sees a silent gap."
            )
