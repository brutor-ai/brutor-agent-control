# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Regression suite for the tenant feature system.

The canonical set is exactly TENANT_FEATURE_KEYS — fourteen platform areas
that can be switched off per tenant (partial-solution clients, mini
editions). Enforcement is two-layer: the console hides/FeatureDisables the
surface, and every area's create/apply endpoint calls
`TenantService.check_feature_access` (403). This suite pins:

  * the canonical set itself (a new flag must be added here consciously);
  * enforcement behaviour: disabled -> 403, enabled -> pass, MISSING KEY ->
    pass (existing tenants predate new flags and must not lose features),
    empty features dict -> everything enabled;
  * the retired flags are gone from the default set (those areas are
    always on: audit logs, metrics, multi-user, semantic cache, batch
    jobs, OAuth integration, user groups, docker_deployment and the old
    hyphenated keys);
  * the wiring contract: each area's endpoint file actually calls the
    check with the right key — an unenforced flag is decoration.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import tests.conftest  # noqa: F401 — SQLite shims

from app.constants.tenant import TENANT_FEATURE_KEYS
from app.database import Base
import app.database.models  # noqa: F401
from app.database.models.tenant import Tenant
from app.services.tenant.tenant_service import TenantService
from tests.source_trees import admin_endpoint_module

TENANT = "feat-t1"

CANONICAL = (
    "mcp_servers",
    "mcp_server_deployment",
    "mcp_registry",
    "virtual_mcp_servers",
    "models",
    "model_routing",
    "ai_model_deployment",
    "model_traffic_import",
    "agent_skills",
    "knowledge_base",
    "a2a",
    "guardrails",
    "policies",
    "compliance",
)

RETIRED = (
    "docker_deployment",
    "oauth_integration",
    "multi_user",
    "user_groups",
    "audit_logs",
    "metrics",
    "semantic_cache",
    "batch_jobs",
    "mcp-servers",
    "virtual-servers",
)


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine, tables=[Tenant.__table__])
    Session = sessionmaker(bind=engine, autoflush=False, future=True)
    s = Session()
    s.add(
        Tenant(
            id=TENANT,
            name=TENANT,
            display_name=TENANT,
            primary_contact_email=f"{TENANT}@example.com",
            features={},
            settings={},
        )
    )
    s.flush()
    yield s
    s.close()
    engine.dispose()


def _set_features(db, features):
    t = db.query(Tenant).get(TENANT)
    t.features = features
    db.flush()


class TestCanonicalSet:
    def test_constant_is_exactly_the_agreed_fourteen(self):
        assert TENANT_FEATURE_KEYS == CANONICAL

    def test_retired_flags_are_not_canonical(self):
        for r in RETIRED:
            assert r not in TENANT_FEATURE_KEYS, r


class TestEnforcementBehaviour:
    @pytest.mark.parametrize("key", CANONICAL)
    def test_disabled_flag_blocks_with_403(self, db, key):
        _set_features(db, {key: False})
        with pytest.raises(HTTPException) as exc:
            TenantService.check_feature_access(db, TENANT, key)
        assert exc.value.status_code == 403
        assert key in exc.value.detail

    @pytest.mark.parametrize("key", CANONICAL)
    def test_enabled_flag_passes(self, db, key):
        _set_features(db, {key: True})
        TenantService.check_feature_access(db, TENANT, key)  # no raise

    @pytest.mark.parametrize("key", CANONICAL)
    def test_missing_key_means_enabled(self, db, key):
        # Existing tenants predate new flags — absence must never lock a
        # tenant out of a feature it always had.
        _set_features(db, {"something_else": True})
        TenantService.check_feature_access(db, TENANT, key)  # no raise

    def test_empty_features_dict_means_all_enabled(self, db):
        _set_features(db, {})
        for key in CANONICAL:
            TenantService.check_feature_access(db, TENANT, key)

    def test_unknown_tenant_is_404(self, db):
        with pytest.raises(HTTPException) as exc:
            TenantService.check_feature_access(db, "nope", "models")
        assert exc.value.status_code == 404

    def test_disabling_one_flag_leaves_the_rest_alone(self, db):
        _set_features(db, {"policies": False})
        with pytest.raises(HTTPException):
            TenantService.check_feature_access(db, TENANT, "policies")
        for key in CANONICAL:
            if key != "policies":
                TenantService.check_feature_access(db, TENANT, key)


class TestGovernanceControlsHaveNoFlag:
    """Argument policies, semantic
    policies are governance controls of the Community Edition (agent
    policies — grants — follow the `a2a` flag of the agents area, which the
    core enforces too). A tenant feature flag is a database row an operator
    can flip, it fails open (a missing key is "enabled") and it would switch
    a control off without a trace in the control's own audit trail — so
    these endpoints check none. Policy-as-Code keeps the `policies` flag."""

    ENDPOINTS = Path(__file__).resolve().parents[1] / "app/api/v1/endpoints/admin"
    UNFLAGGED = ("argument_policies.py", "semantic_policies.py")

    @pytest.mark.parametrize("fname", UNFLAGGED)
    def test_the_endpoint_module_checks_no_tenant_feature(self, fname):
        source = admin_endpoint_module(fname).read_text()
        assert "check_feature_access" not in source
        assert "check_feature_enabled" not in source

    def test_the_two_policy_modules_exist(self):
        # `policies.py` (Policy-as-Code) is the Enterprise Edition's: the
        # Enterprise half of this file holds it.
        for fname in ("argument_policies.py", "semantic_policies.py"):
            assert admin_endpoint_module(fname).exists(), fname


class TestEndpointWiring:
    """Source-level contract: every feature must be enforced at its area's
    create/apply endpoint. Accepts the area's established idiom where one
    predates the standardization (KB's check_feature_enabled, the A2A
    cards' _enforce_a2a_enabled)."""

    ENDPOINTS = Path(__file__).resolve().parents[1] / "app/api/v1/endpoints/admin"

    WIRING = {
        "mcp_servers.py": ["check_feature_access(db, tenant_id, 'mcp_servers')"],
        "virtual_servers.py": ["check_feature_access(db, tenant_id, 'virtual_mcp_servers')"],
        "llm_models.py": ["check_feature_access(db, tenant_id, 'models')"],
        "llm_providers.py": ["check_feature_access(db, tenant_id, 'models')"],
        "llm_routing_groups.py": ["check_feature_access(db, tenant_id, 'model_routing')"],
        "agent_skills.py": ["check_feature_access(db, tenant_id, 'agent_skills')"],
        "agent_cards.py": ["_enforce_a2a_enabled("],
        "agent_identities.py": ["check_feature_access(db, tid, 'a2a')"],
        "guardrails.py": ["check_feature_access(db, tenant_id, 'guardrails')"],
        "compliance.py": ["check_feature_access(db, resolved_id, 'compliance')"],
    }

    # The Community package's router modules. The Enterprise package's are
    # held by `enterprise/tests/test_tenant_feature_enforcer.py`: what this
    # file asserts does not depend on whether the checkout has `enterprise/`.
    @pytest.mark.parametrize("fname,needles", sorted(WIRING.items()), ids=sorted(WIRING))
    def test_area_endpoint_enforces_its_feature(self, fname, needles):
        src = admin_endpoint_module(fname).read_text()
        for needle in needles:
            assert needle in src, (
                f"{fname} must enforce its feature flag — expected `{needle}`"
            )
