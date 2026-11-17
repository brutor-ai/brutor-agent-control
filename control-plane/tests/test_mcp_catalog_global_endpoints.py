# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
MCP catalog global + curation endpoint behaviour (migration `a093_mcp_catalog_global`).

Pins the global-vs-custom model:
  * Browse returns GLOBAL (tenant_id NULL) ∪ own-tenant custom entries.
  * System admins can create / update / delete a GLOBAL entry.
  * Tenant admins CANNOT curate a global entry (403) but CAN manage their
    own custom entry.
  * Import resolves a GLOBAL entry from browse-scope.

SQLite-backed FastAPI harness mirroring test_catalog_import_deployment_gate.py
— bypasses the JWT/RBAC chain by overriding get_current_admin.
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
from app.core.admin_user.dependencies import get_audit_logger, get_current_admin
from app.core.logging_system import BrutorLogger
from app.database import Base, get_db
from app.database.models.mcp_catalog_entry import MCPCatalogEntry
from app.database.models.mcp_server import MCPServer
from app.database.models.server_config import ServerConfig
from app.database.models.tenant import Tenant


class _SysAdmin:
    def __init__(self):
        self.id = "sysadmin-1"
        self.username = "root"
        # NB: no tenant_id — system admins have no home tenant.

    def has_permission(self, _perm: str) -> bool:
        return True


class _TenantAdmin:
    def __init__(self, tenant_id: str):
        self.id = "tenant-admin-1"
        self.username = "ta"
        self.tenant_id = tenant_id

    def has_permission(self, _perm: str) -> bool:
        return True


class _NoopAudit:
    def log(self, **_kwargs):
        return None


REQUIRED_TABLES = [
    Tenant.__table__,
    MCPCatalogEntry.__table__,
    MCPServer.__table__,
    ServerConfig.__table__,
]


# Mutable holder so each test can swap the active admin identity.
_ACTIVE = {"admin": None, "type": "tenant"}


@pytest.fixture
def harness():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine, tables=REQUIRED_TABLES)
    SessionFactory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        s = SessionFactory()
        try:
            yield s
        finally:
            s.close()

    def override_admin():
        return (_ACTIVE["admin"], _ACTIVE["type"])

    def override_audit():
        return _NoopAudit()

    logger = BrutorLogger("test")
    app = FastAPI()
    app.include_router(create_mcp_catalog_router(logger=logger), prefix="/v1")
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_admin] = override_admin
    app.dependency_overrides[get_audit_logger] = override_audit

    # Seed: two tenants, one GLOBAL system entry, and one custom entry per
    # tenant.
    setup = SessionFactory()
    for tid in ("t1", "t2"):
        setup.add(Tenant(
            id=tid, name=tid, display_name=tid.upper(),
            primary_contact_email=f"a@{tid}.invalid",
            is_active=True, is_default=False, is_trial=False,
            suspended=False, billing_plan="self_hosted", features={},
        ))
    setup.add(MCPCatalogEntry(
        id="mcp-cat-system-github",
        tenant_id=None,
        slug="github",
        name="GitHub (global)",
        runtime="remote",
        auth_pattern="oauth",
        source="system",
        enabled=True,
        remote_config={"fixedURL": "https://api.githubcopilot.com/mcp"},
    ))
    setup.add(MCPCatalogEntry(
        id="mcp-cat-custom-t1",
        tenant_id="t1",
        slug="t1-internal",
        name="T1 Internal",
        runtime="remote",
        auth_pattern="none",
        source="custom",
        enabled=True,
        remote_config={"fixedURL": "https://t1.example.com/mcp"},
    ))
    setup.add(MCPCatalogEntry(
        id="mcp-cat-custom-t2",
        tenant_id="t2",
        slug="t2-internal",
        name="T2 Internal",
        runtime="remote",
        auth_pattern="none",
        source="custom",
        enabled=True,
        remote_config={"fixedURL": "https://t2.example.com/mcp"},
    ))
    setup.commit()
    setup.close()

    client = TestClient(app, raise_server_exceptions=False)
    yield client, SessionFactory
    app.dependency_overrides.clear()
    engine.dispose()


def _as_tenant(tenant_id="t1"):
    _ACTIVE["admin"] = _TenantAdmin(tenant_id)
    _ACTIVE["type"] = "tenant"


def _as_sysadmin():
    _ACTIVE["admin"] = _SysAdmin()
    _ACTIVE["type"] = "system"


def _as_platform_admin():
    """The administrator of the installation (`require_platform_admin`): the
    system administrator where the edition under test has one, else a tenant
    administrator holding `platform:catalog:manage`."""
    from app.composition.principals import PRINCIPALS

    if any(PRINCIPALS.get(name).platform_wide for name in PRINCIPALS.names()):
        _as_sysadmin()
    else:
        _as_tenant("t1")


# ===========================================================================
# Browse = global ∪ own
# ===========================================================================


def test_tenant_browse_returns_global_and_own_only(harness):
    client, _ = harness
    _as_tenant("t1")
    resp = client.get("/v1/admin/mcp-catalog")
    assert resp.status_code == 200, resp.text
    ids = {e["id"] for e in resp.json()["entries"]}
    assert "mcp-cat-system-github" in ids  # global visible
    assert "mcp-cat-custom-t1" in ids      # own visible
    assert "mcp-cat-custom-t2" not in ids  # other tenant's custom hidden


def test_get_global_entry_has_null_tenant_id(harness):
    client, _ = harness
    _as_tenant("t1")
    resp = client.get("/v1/admin/mcp-catalog/mcp-cat-system-github")
    assert resp.status_code == 200, resp.text
    assert resp.json()["tenant_id"] is None
    assert resp.json()["source"] == "system"


# ===========================================================================
# Sysadmin curation of GLOBAL entries
# ===========================================================================


# ===========================================================================
# Tenant admin CANNOT curate globals, CAN manage own custom
# ===========================================================================


def test_tenant_admin_creates_and_manages_own_custom(harness):
    client, _ = harness
    _as_tenant("t1")
    # Create
    resp = client.post(
        "/v1/admin/mcp-catalog",
        json={"name": "My Custom", "slug": "my-custom", "runtime": "remote"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["tenant_id"] == "t1"
    assert body["source"] == "custom"
    new_id = body["id"]
    assert new_id.startswith("mcp-cat-")
    # Update
    resp = client.put(
        f"/v1/admin/mcp-catalog/{new_id}", json={"name": "Renamed Custom"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Renamed Custom"
    # Delete
    resp = client.delete(f"/v1/admin/mcp-catalog/{new_id}")
    assert resp.status_code == 204, resp.text


def test_tenant_admin_cannot_touch_other_tenant_custom_404(harness):
    client, _ = harness
    _as_tenant("t1")
    resp = client.put(
        "/v1/admin/mcp-catalog/mcp-cat-custom-t2", json={"name": "x"}
    )
    assert resp.status_code == 404, resp.text


# ===========================================================================
# Import resolves a GLOBAL entry
# ===========================================================================


def test_import_global_entry_resolves_in_browse_scope(harness):
    """A tenant admin can import a GLOBAL catalog entry. The import path
    runs past the entry-resolution (no 404) into downstream row creation,
    which fails in this stub harness on tables we deliberately didn't
    materialise (resource_groups). The assertion only confirms the entry
    was RESOLVED — i.e. NOT a 404 'not found'."""
    client, _ = harness
    _as_tenant("t1")
    resp = client.post(
        "/v1/admin/mcp-catalog/mcp-cat-system-github/import",
        json={"deploy_container": False, "enabled": False},
    )
    # Not a 404: the global entry was found in (global ∪ own) scope.
    assert resp.status_code != 404, resp.text


def test_import_other_tenant_custom_is_404(harness):
    client, _ = harness
    _as_tenant("t1")
    resp = client.post(
        "/v1/admin/mcp-catalog/mcp-cat-custom-t2/import",
        json={"deploy_container": False, "enabled": False},
    )
    assert resp.status_code == 404, resp.text


# ===========================================================================
# The global catalog, written by the installation's administrator. Moved from
# the Enterprise half, which signed in as the system administrator and still
# runs these under their old names (`test_sysadmin_*`).
# ===========================================================================


def test_platform_admin_creates_global_entry(harness):
    client, SessionFactory = harness
    _as_platform_admin()
    resp = client.post(
        "/v1/admin/mcp-catalog",
        json={
            "name": "Slack (global)",
            "slug": "slack",
            "runtime": "remote",
            "auth_pattern": "oauth",
            "is_global": True,
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"] == "mcp-cat-system-slack"
    assert body["tenant_id"] is None
    assert body["source"] == "system"


def test_platform_admin_create_global_duplicate_slug_409(harness):
    client, _ = harness
    _as_platform_admin()
    resp = client.post(
        "/v1/admin/mcp-catalog",
        json={"name": "Dup", "slug": "github", "runtime": "remote", "is_global": True},
    )
    assert resp.status_code == 409, resp.text


def test_platform_admin_updates_global_entry(harness):
    client, _ = harness
    _as_platform_admin()
    resp = client.put(
        "/v1/admin/mcp-catalog/mcp-cat-system-github",
        json={"name": "GitHub Renamed"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "GitHub Renamed"


def test_platform_admin_deletes_global_entry(harness):
    client, SessionFactory = harness
    _as_platform_admin()
    resp = client.delete("/v1/admin/mcp-catalog/mcp-cat-system-github")
    assert resp.status_code == 204, resp.text
    s = SessionFactory()
    try:
        assert s.get(MCPCatalogEntry, "mcp-cat-system-github") is None
    finally:
        s.close()
