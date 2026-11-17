# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Regression suite for the tenant quota system.

The quota surface is exactly the ten tenant-created resources:

    admin users · end users · end-user groups · AI models (enabled only —
    configured, not the catalog) · MCP servers (enabled only) · MCP registry
    configs · KB collections · agent skills · agent cards · agent identities

plus the live media byte ceiling (`media_storage_quota_bytes`), which is
enforced by the Rust core at upload and deliberately not an object count.

What this pins:
  * every quota enforces (429 at the limit, pass under it, unlimited on
    null) against REAL rows in the real schema — not a mocked count;
  * AI models / MCP servers count ENABLED rows only (the catalog can hold
    hundreds of disabled entries);
  * the retired quota types (virtual servers, OAuth clients, guardrails,
    semantic cache, resource groups, docker deployments, request rates,
    storage GB) are gone from the surface and no-op if an old caller
    still passes them;
  * each quota is actually WIRED into its create endpoint (source-level
    contract — a quota that exists but is never called is decoration).
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

import tests.conftest  # noqa: F401 — registers the SQLite JSONB/notify shims

from app.constants.tenant import TenantResourceTypes
from app.database import Base
import app.database.models  # noqa: F401 — registers every model
from app.database.models.tenant import Tenant
from app.database.models.admin import TenantAdmin
from app.database.models.end_user import EndUser
from app.database.models.end_user_group import EndUserGroup
from app.database.models.llm_model import LLModel
from app.database.models.mcp_server import MCPServer
from app.database.models.agent_skill import AgentSkill
from app.database.models.agent_card import AgentCard
from app.database.models.agent_identity import AgentIdentity
from app.services.tenant.tenant_service import TenantService
from tests.source_trees import admin_endpoint_module

TENANT = "quota-t1"
OTHER_TENANT = "quota-t2"


# ---------------------------------------------------------------------------
# Generic minimal-row factory: fills every non-nullable column without a
# default so the suite doesn't silently rot when a model grows a column.
# ---------------------------------------------------------------------------
_SEQ = {"n": 0}


def _make_row(model, session, tenant_id=TENANT, **overrides):
    _SEQ["n"] += 1
    n = _SEQ["n"]
    kwargs = {}
    for col in model.__table__.columns:
        if col.name in overrides or col.name == "tenant_id":
            continue
        if col.primary_key and col.autoincrement in (True, "auto") and getattr(col.type, "python_type", None) is int:
            continue  # DB assigns it (e.g. end_users.id)
        if col.nullable or col.default is not None or col.server_default is not None:
            continue
        pytype = None
        try:
            pytype = col.type.python_type
        except NotImplementedError:
            pass
        if pytype is str:
            # Model validators expect URL-shaped values in *url* columns.
            if "url" in col.name:
                kwargs[col.name] = f"https://example.com/qx-{n}"
            else:
                kwargs[col.name] = f"qx-{n}-{col.name}"[:64]
        elif pytype is int:
            kwargs[col.name] = 1
        elif pytype is float:
            kwargs[col.name] = 1.0
        elif pytype is bool:
            kwargs[col.name] = False
        elif pytype in (dict, list):
            kwargs[col.name] = pytype()
        else:
            kwargs[col.name] = f"qx-{n}"
    if "tenant_id" in model.__table__.columns:
        kwargs["tenant_id"] = tenant_id
    kwargs.update(overrides)
    row = model(**kwargs)
    session.add(row)
    session.flush()
    return row


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, future=True)
    s = Session()
    for tid in (TENANT, OTHER_TENANT):
        s.add(
            Tenant(
                id=tid,
                name=tid,
                display_name=tid,
                primary_contact_email=f"{tid}@example.com",
                features={},
                settings={},
            )
        )
    s.flush()
    yield s
    s.close()
    engine.dispose()


def _set_quota(db, field, value, tenant_id=TENANT):
    t = db.query(Tenant).get(tenant_id)
    setattr(t, field, value)
    db.flush()


# (resource_type, quota column, model, overrides-for-a-counted-row)
QUOTA_MATRIX = [
    (TenantResourceTypes.ADMIN_USERS, "max_admin_users", TenantAdmin, {}),
    (TenantResourceTypes.END_USERS, "max_end_users", EndUser, {}),
    (TenantResourceTypes.END_USER_GROUPS, "max_end_user_groups", EndUserGroup, {}),
    (TenantResourceTypes.LLM_MODELS, "max_llm_models", LLModel, {"enabled": True}),
    (TenantResourceTypes.MCP_SERVERS, "max_mcp_servers", MCPServer, {"enabled": True}),
    # `registries` and `knowledge_base` are counted by the Enterprise Edition
    # (the `tenant.quota_counts` hook): `enterprise/tests/test_tenant_quota_enforcer.py`.
    (TenantResourceTypes.AGENT_SKILLS, "max_agent_skills", AgentSkill, {}),
    (TenantResourceTypes.AGENT_CARDS, "max_agent_cards", AgentCard, {}),
    (TenantResourceTypes.AGENT_IDENTITIES, "max_agent_identities", AgentIdentity, {}),
]

MATRIX_IDS = [rt.value for rt, _, _, _ in QUOTA_MATRIX]


# The four checks, shared with the Enterprise half (its matrix is the two
# resource types the Enterprise Edition counts).
def check_at_limit_blocks_with_429(db, rt, field, model, ov):
    _set_quota(db, field, 1)
    _make_row(model, db, **ov)
    with pytest.raises(HTTPException) as exc:
        TenantService.check_quota(db, TENANT, rt)
    assert exc.value.status_code == 429
    # A structured answer, as the licence's 403 is: it was one
    # sentence, which the Admin Console took apart by pattern.
    detail = exc.value.detail
    assert set(detail) == {"error", "limit", "maximum", "current", "message"}
    assert (detail["error"], detail["limit"], detail["maximum"], detail["current"]) == (
        "tenant_quota_reached", field, 1, 1,
    )
    assert detail["message"].startswith("Quota exceeded for ") and "Current: 1, Maximum: 1." in detail["message"]

def check_under_limit_passes(db, rt, field, model, ov):
    _set_quota(db, field, 2)
    _make_row(model, db, **ov)
    TenantService.check_quota(db, TENANT, rt)  # must not raise

def check_null_means_unlimited(db, rt, field, model, ov):
    _set_quota(db, field, None)
    for _ in range(3):
        _make_row(model, db, **ov)
    TenantService.check_quota(db, TENANT, rt)  # must not raise

def check_other_tenants_rows_do_not_count(db, rt, field, model, ov):
    _set_quota(db, field, 1)
    _make_row(model, db, tenant_id=OTHER_TENANT, **ov)
    TenantService.check_quota(db, TENANT, rt)  # must not raise


class TestEnforcement:
    @pytest.mark.parametrize("rt,field,model,ov", QUOTA_MATRIX, ids=MATRIX_IDS)
    def test_at_limit_blocks_with_429(self, db, rt, field, model, ov):
        check_at_limit_blocks_with_429(db, rt, field, model, ov)

    @pytest.mark.parametrize("rt,field,model,ov", QUOTA_MATRIX, ids=MATRIX_IDS)
    def test_under_limit_passes(self, db, rt, field, model, ov):
        check_under_limit_passes(db, rt, field, model, ov)

    @pytest.mark.parametrize("rt,field,model,ov", QUOTA_MATRIX, ids=MATRIX_IDS)
    def test_null_means_unlimited(self, db, rt, field, model, ov):
        check_null_means_unlimited(db, rt, field, model, ov)

    @pytest.mark.parametrize("rt,field,model,ov", QUOTA_MATRIX, ids=MATRIX_IDS)
    def test_other_tenants_rows_do_not_count(self, db, rt, field, model, ov):
        check_other_tenants_rows_do_not_count(db, rt, field, model, ov)


class TestEnabledOnlyCounting:
    """Configured means enabled: a disabled catalog entry costs nothing."""

    def test_disabled_llm_models_do_not_count(self, db):
        _set_quota(db, "max_llm_models", 1)
        _make_row(LLModel, db, enabled=False)
        TenantService.check_quota(db, TENANT, TenantResourceTypes.LLM_MODELS)

    def test_disabled_mcp_servers_do_not_count(self, db):
        _set_quota(db, "max_mcp_servers", 1)
        _make_row(MCPServer, db, enabled=False)
        TenantService.check_quota(db, TENANT, TenantResourceTypes.MCP_SERVERS)


class TestRetiredQuotas:
    """The dropped quota types must be gone from the surface and harmless
    if an out-of-date caller still passes them."""

    RETIRED = [
        "virtual-servers",
        "oauth_clients",
        "docker_deployments",
        "guardrails",
        "semantic_cache",
        "server_configs",
        "resource_groups",
    ]

    @pytest.mark.parametrize("rt", RETIRED)
    def test_retired_types_noop(self, db, rt):
        # No quota column, no count query — must not raise regardless of rows.
        TenantService.check_quota(db, TENANT, rt)

    def test_tenant_model_has_no_retired_columns(self):
        cols = {c.name for c in Tenant.__table__.columns}
        for gone in (
            "max_virtual_servers", "max_oauth_clients", "max_docker_deployments",
            "max_model_deployments", "max_mcp_deployments", "max_guardrails",
            "max_semantic_cache_configs", "max_resource_groups",
            "max_requests_per_hour", "max_requests_per_day", "max_storage_gb",
        ):
            assert gone not in cols, f"retired quota column still present: {gone}"

    def test_get_quotas_is_exactly_the_reshaped_surface(self, db):
        t = db.query(Tenant).get(TENANT)
        assert set(t.get_quotas().keys()) == {
            "max_admin_users", "max_end_users", "max_end_user_groups",
            "max_llm_models", "max_mcp_servers", "max_mcp_registries",
            "max_kb_collections", "max_agent_skills", "max_agent_cards",
            "max_agent_identities",
            # live byte ceiling, reported alongside the resource quotas
            "media_storage_quota_bytes",
        }


class TestEndpointWiring:
    """Source-level contract: each quota must be CALLED from its create
    endpoint. An enforcer nobody invokes is decoration — this is the test
    that fails when a new create path forgets the check (or a refactor
    drops one)."""

    ENDPOINTS = Path(__file__).resolve().parents[1] / "app/api/v1/endpoints/admin"

    WIRING = {
        "admin_rbac.py": "ADMIN_USERS",
        "end_users.py": "END_USERS",
        "end_user_groups.py": "END_USER_GROUPS",
        "llm_models.py": "LLM_MODELS",
        "mcp_servers.py": "MCP_SERVERS",
        "agent_skills.py": "AGENT_SKILLS",
        "agent_cards.py": "AGENT_CARDS",
        "agent_identities.py": "AGENT_IDENTITIES",
    }

    # The Community package's router modules. The Enterprise package's are
    # held by `enterprise/tests/test_tenant_quota_enforcer.py`: what this
    # file asserts does not depend on whether the checkout has `enterprise/`.
    @pytest.mark.parametrize("fname,enum_name", sorted(WIRING.items()))
    def test_create_endpoint_calls_check_quota(self, fname, enum_name):
        src = admin_endpoint_module(fname).read_text()
        assert f"check_quota" in src and f"TenantResourceTypes.{enum_name}" in src, (
            f"{fname} must call TenantService.check_quota(..., "
            f"TenantResourceTypes.{enum_name}) in its create path"
        )

    @pytest.mark.parametrize("fname", [
        "virtual_servers.py", "oauth_app_clients.py", "guardrails.py",
        "resource_groups.py", "server_configs.py",
    ])
    def test_retired_endpoints_no_longer_check(self, fname):
        src = admin_endpoint_module(fname).read_text()
        assert "check_quota" not in src, (
            f"{fname} still calls check_quota — a retired quota"
        )
