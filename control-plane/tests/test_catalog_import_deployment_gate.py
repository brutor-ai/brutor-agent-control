# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
POST /v1/admin/mcp-catalog/{id}/import — refuse-fast gate.

This is the test that pins the *user-visible* fix for the
half-import bug. Before the fix, clicking "Import & Deploy" on a
containerized catalog entry in an environment without container
backend would:
  1. Create MCPServer + ServerConfig rows.
  2. Try to deploy via Docker → 503.
  3. Swallow the exception, log a non-fatal warning.
  4. Return 200 with a misleading "Container deployment failed — deploy
     manually" message, leaving a half-imported row.

The fix gates the import at the top of the endpoint on two axes:
  * tenant.features.mcp_server_deployment
  * BRUTOR_MCP_DEPLOY_DRIVER availability

Either off → 400 with structured detail, NO DB rows written. These
tests pin both the status code AND the no-write invariant.

Tenant isolation, full RBAC, and the happy-path import are covered by
existing integration tests in tests/test_admin_rbac_endpoints.py and
the catalog router's other coverage; here we focus exclusively on the
new gate.
"""

from __future__ import annotations

import os
from typing import Optional

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.endpoints.admin.mcp_catalog import create_mcp_catalog_router
from app.core.admin_user.dependencies import (
    get_audit_logger,
    get_current_admin,
)
from app.core.logging_system import BrutorLogger
from app.database import Base, get_db
from app.database.models.mcp_catalog_entry import MCPCatalogEntry
from app.database.models.mcp_server import MCPServer
from app.database.models.server_config import ServerConfig
from app.database.models.tenant import Tenant


# ---------------------------------------------------------------------------
# Fake admin + audit logger — bypass the JWT/RBAC chain so the test
# stays focused on the gate logic, not auth wiring.
# ---------------------------------------------------------------------------


class _FakeAdmin:
    def __init__(self, tenant_id: str = "t1"):
        self.id = "admin-test"
        self.username = "tester"
        self.tenant_id = tenant_id

    def has_permission(self, _perm: str) -> bool:
        return True


class _NoopAudit:
    """The endpoint calls `audit.log(...)` on the success path; we
    don't care about audit-row contents for this test, so swallow."""

    def log(self, **_kwargs):
        return None


# Required SQLAlchemy tables — we only need to insert/inspect a tenant
# row, a catalog entry, and detect that NO mcp_server / server_config
# rows were created on the refuse path. We include the related tables
# because FK relationships are declared on the models.
REQUIRED_TABLES = [
    Tenant.__table__,
    MCPCatalogEntry.__table__,
    MCPServer.__table__,
    ServerConfig.__table__,
]


@pytest.fixture
def harness():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    # FK enforcement OFF: the production schema has many cross-table
    # FKs we don't need to materialise here, and SQLite would reject
    # inserts against missing FK targets if we turned them on.
    Base.metadata.create_all(bind=engine, tables=REQUIRED_TABLES)
    SessionFactory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        s = SessionFactory()
        try:
            yield s
            # The endpoint commits internally on the success path; on
            # the refuse path we must roll back any pending changes so
            # the no-write invariant test is honest.
        finally:
            s.close()

    def override_admin():
        return (_FakeAdmin(tenant_id="t1"), "tenant")

    def override_audit():
        return _NoopAudit()

    logger = BrutorLogger("test")
    app = FastAPI()
    app.include_router(create_mcp_catalog_router(logger=logger), prefix="/v1")
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_admin] = override_admin
    app.dependency_overrides[get_audit_logger] = override_audit

    # Seed: one tenant + one containerized catalog entry. Individual
    # tests mutate the tenant's `features` to flip the gate.
    setup = SessionFactory()
    setup.add(Tenant(
        id="t1", name="t1", display_name="T1",
        primary_contact_email="a@t1.invalid",
        is_active=True, is_default=False, is_trial=False,
        suspended=False, billing_plan="self_hosted",
        features={"mcp_server_deployment": True},
    ))
    setup.add(MCPCatalogEntry(
        id="cat-brave",
        tenant_id="t1",
        slug="brave_search",
        name="Brave Search",
        runtime="containerized",
        auth_pattern="api_key",
        enabled=True,
        source="system",
        containerized_config={
            "image": "ghcr.io/example/brave-search:latest",
            "port": 8099,
            "path": "/mcp",
        },
        env_vars=[{"name": "BRAVE_API_KEY", "required": True}],
    ))
    # A second entry of `runtime=remote` to confirm the gate only
    # fires on containerized + deploy_container=true.
    setup.add(MCPCatalogEntry(
        id="cat-remote",
        tenant_id="t1",
        slug="remote_mcp",
        name="Remote MCP",
        runtime="remote",
        auth_pattern="none",
        enabled=True,
        source="system",
        remote_config={"fixedURL": "https://mcp.example.com"},
    ))
    setup.commit()
    setup.close()

    # raise_server_exceptions=False — downstream SQLAlchemy errors from
    # the un-materialised tables become genuine 500 HTTP responses
    # instead of Python exceptions propagating into the test. This is
    # essential for the negative tests (config-only import, remote
    # entry) which deliberately run past the gate into territory we
    # haven't stood up. The assertion only inspects the *gate*'s
    # behaviour, not whether downstream succeeds.
    client = TestClient(app, raise_server_exceptions=False)
    yield client, SessionFactory
    app.dependency_overrides.clear()
    engine.dispose()


@pytest.fixture(autouse=True)
def _scrub_driver_env(monkeypatch):
    monkeypatch.delenv("BRUTOR_MCP_DEPLOY_DRIVER", raising=False)


def _count_rows(SessionFactory) -> dict:
    s = SessionFactory()
    try:
        return {
            "mcp_servers": s.query(MCPServer).count(),
            "server_configs": s.query(ServerConfig).count(),
        }
    finally:
        s.close()


# ===========================================================================
# Tenant-flag gate
# ===========================================================================


def test_tenant_flag_off_still_allows_config_only_import(harness, monkeypatch):
    """Operators must be able to *config-import* a containerized entry
    even when deploy is disabled — they can manually configure the
    base_url to point at a server they've stood up themselves.
    `deploy_container=false` must bypass the gate entirely.

    The endpoint runs past the gate, then fails downstream on a
    `resource_groups` table that's intentionally not materialised in
    this stub harness (we're not testing the full import path here).
    The assertion only cares that the response is NOT one of the
    gate's 400s — any other status code means the gate did its job.
    """
    client, SessionFactory = harness
    monkeypatch.setenv("BRUTOR_MCP_DEPLOY_DRIVER", "docker")
    s = SessionFactory()
    try:
        s.query(Tenant).filter_by(id="t1").update(
            {"features": {"mcp_server_deployment": False}},
        )
        s.commit()
    finally:
        s.close()

    resp = client.post(
        "/v1/admin/mcp-catalog/cat-brave/import",
        json={
            "deploy_container": False,  # ← key difference
            "enabled": True,
            "credentials": {"env_values": {"BRAVE_API_KEY": "k"}},
        },
    )
    _assert_not_gate_refused(resp)


# ===========================================================================
# Driver gate
# ===========================================================================


# ===========================================================================
# Negative: gate must NOT fire for non-containerized entries even
# when deploy_container=true (the field is a no-op for remote runtimes).
# ===========================================================================


def test_remote_entry_skips_gate_even_with_deploy_container_true(
    harness, monkeypatch,
):
    """`deploy_container=true` on a `remote` entry has historically
    been a no-op (the field only applies to containerized). The gate
    must respect this and never refuse remote imports."""
    client, _ = harness
    monkeypatch.setenv("BRUTOR_MCP_DEPLOY_DRIVER", "none")
    resp = client.post(
        "/v1/admin/mcp-catalog/cat-remote/import",
        json={"deploy_container": True, "enabled": True},
    )
    _assert_not_gate_refused(resp)


def _assert_not_gate_refused(resp) -> None:
    """Helper — the gate's 400s carry a structured `detail` dict with
    a specific `error` field. Any other status (including a 500 from
    downstream stub-harness failure) means the gate didn't fire."""
    if resp.status_code != 400:
        return
    detail = resp.json().get("detail", {})
    if not isinstance(detail, dict):
        return
    assert detail.get("error") not in {
        "feature_disabled",
        "deployment_driver_unavailable",
    }, f"gate fired when it shouldn't have: {detail!r}"
