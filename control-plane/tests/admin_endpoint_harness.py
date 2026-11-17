# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Shared harness for HTTP-level admin-endpoint tests.

Tests that import Python internals pin the implementation rather than the
wire contract. The admin endpoint tests instead speak HTTP through a real app,
real auth and a real (SQLite) database, so they double as a behavioural spec of the admin API.

This module is the copied-per-file plumbing from
`test_ai_system_contract_endpoints.py`, factored out once:

  * in-memory SQLite with the FULL schema — `Base.metadata.create_all` over
    every registered model works under the conftest JSONB/notify shims, so
    no per-file table cherry-picking (the exemplar's REQUIRED_TABLES list
    predates the shims)
  * the real UnifiedAuthService with two tenants ("t1", "t2"), each with a
    full-permission Tenant Admin — tenant isolation is a first-class
    assertion in every file that uses this — and whatever the edition under
    test adds (`tests/app_factory.py`): for the full composition, the system
    admin and its login route
  * token caching: the exemplar logs in per request, which is one bcrypt
    verify per call; at hundreds of requests per file that is the difference
    between a 3-second and a 3-minute test module
"""

import os
from tests.pg_test_url import pg_test_url

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import uuid
from typing import Callable, Dict, Iterable, Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import tests.conftest  # noqa: F401 — registers the SQLite JSONB/notify shims

from app.api.v1.endpoints.admin.admin_rbac import create_admin_users_router
from app.core import account_lock
from app.core.admin_user.permissions import Permission
from tests.app_factory import every_permission
from app.core.admin_user.services.unified_auth_service import UnifiedAuthService
from app.core.logging_system import BrutorLogger
from app.database import Base, get_db
import app.database.models  # noqa: F401 — registers every model on Base.metadata
from app.database.models.admin import TenantRole, TenantRolePermission
from app.database.models.resource_group import GroupType, ResourceGroup
from app.database.models.tenant import Tenant
from tests.app_factory import harness_extras

JWT_SECRET = os.environ["JWT_SECRET"]
PASSWORD = "Secret123!"
TENANTS = ("t1", "t2")


class AdminHarness:
    """A running app + client + session factory + cached auth tokens."""

    def __init__(self, client: TestClient, factory, app: FastAPI, engine):
        self.client = client
        self.Factory = factory
        self.app = app
        self.engine = engine
        self._tokens: Dict[str, str] = {}

    # -- auth ------------------------------------------------------------
    def headers(self, tenant_id: str = "t1") -> Dict[str, str]:
        """Bearer headers for the tenant's full-permission admin (cached)."""
        if tenant_id not in self._tokens:
            r = self.client.post(
                "/v1/admin/users/tenant/login",
                json={
                    "username": f"admin_{tenant_id}",
                    "password": PASSWORD,
                    "tenant_id": tenant_id,
                },
            )
            assert r.status_code == 200, f"tenant login failed: {r.text}"
            self._tokens[tenant_id] = r.json()["access_token"]
        return {"Authorization": f"Bearer {self._tokens[tenant_id]}"}

    def sys_headers(self) -> Dict[str, str]:
        """Bearer headers for the platform-wide system admin (cached)."""
        if "__sys__" not in self._tokens:
            r = self.client.post(
                "/v1/admin/users/system/login",
                json={"username": "sysadmin", "password": PASSWORD},
            )
            assert r.status_code == 200, f"system login failed: {r.text}"
            self._tokens["__sys__"] = r.json()["access_token"]
        return {"Authorization": f"Bearer {self._tokens['__sys__']}"}

    def limited_headers(
        self, tenant_id: str, permissions: Iterable[Permission], username: Optional[str] = None
    ) -> Dict[str, str]:
        """Bearer headers for a tenant admin holding only `permissions` (cached
        per username), for asserting that a route refuses a missing permission."""
        perms = sorted(p.value for p in permissions)
        username = username or f"lim_{tenant_id}_{uuid.uuid5(uuid.NAMESPACE_OID, ','.join(perms)).hex[:10]}"
        if username not in self._tokens:
            s = self.Factory()
            try:
                role = TenantRole(
                    id=f"role-{uuid.uuid4().hex[:12]}",
                    tenant_id=tenant_id,
                    name=username,
                    description="limited admin",
                    is_system=False,
                )
                s.add(role)
                s.flush()
                for value in perms:
                    s.add(
                        TenantRolePermission(
                            id=f"perm-{uuid.uuid4().hex[:10]}", role_id=role.id, permission=value
                        )
                    )
                UnifiedAuthService(jwt_secret=JWT_SECRET).create_tenant_admin(
                    username=username,
                    email=f"{username}@{tenant_id}.example",
                    password=PASSWORD,
                    tenant_id=tenant_id,
                    db=s,
                    role_id=role.id,
                )
                s.commit()
            finally:
                s.close()
            r = self.client.post(
                "/v1/admin/users/tenant/login",
                json={"username": username, "password": PASSWORD, "tenant_id": tenant_id},
            )
            assert r.status_code == 200, f"limited login failed: {r.text}"
            self._tokens[username] = r.json()["access_token"]
        return {"Authorization": f"Bearer {self._tokens[username]}"}

    # -- data ------------------------------------------------------------
    def seed(self, fn: Callable) -> None:
        """Run `fn(session)` in a fresh session and commit."""
        s = self.Factory()
        try:
            fn(s)
            s.commit()
        finally:
            s.close()

    def close(self) -> None:
        self.app.dependency_overrides.clear()
        self.engine.dispose()


def build_admin_harness(
    routers: Iterable,
    *,
    extra_tenants: Iterable[str] = (),
    logger_name: str = "admin-endpoint-tests",
    database_url: str | None = None,
) -> AdminHarness:
    """Assemble an app from `routers` (already-constructed APIRouters).

    Each router is mounted under the same "/v1" prefix `app.py` uses, next
    to the real admin-users router so login works exactly as in production.

    `database_url` (optional) points the harness at an EMPTY Postgres database
    instead of in-memory SQLite, for endpoints whose raw SQL is
    Postgres-only (window functions, FILTER, jsonb). The caller owns creating
    and dropping that database.
    """
    if database_url:
        engine = create_engine(database_url, future=True)
    else:
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
            future=True,
        )

        @event.listens_for(engine, "connect")
        def _fk(dbapi_conn, _rec):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    def override_get_db():
        s = factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app = FastAPI()
    # As the application factory does: a 423 carries its reason.
    account_lock.install(app)
    logger = BrutorLogger(logger_name)
    # What the edition under test adds: for the full
    # composition, the system administrator's login router and account. This
    # module names nothing of the Enterprise Edition.
    extras = harness_extras()
    for r in extras.routers(logger):
        app.include_router(r, prefix="/v1")
    app.include_router(create_admin_users_router(logger), prefix="/v1")
    for r in routers:
        app.include_router(r, prefix="/v1")
    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)

    auth = UnifiedAuthService(jwt_secret=JWT_SECRET)
    s = factory()
    try:
        extras.seed_principals(auth, s, PASSWORD)
        for tid in (*TENANTS, *extra_tenants):
            s.add(
                Tenant(
                    id=tid,
                    name=tid,
                    display_name=tid.upper(),
                    organization_name=tid.upper(),
                    primary_contact_email=f"admin@{tid}.example",
                    is_active=True,
                    is_default=False,
                    is_trial=False,
                    suspended=False,
                    billing_plan="self_hosted",
                    features={},
                )
            )
            s.flush()
            role = TenantRole(
                id=f"role-{tid}-admin",
                tenant_id=tid,
                name="Tenant Admin",
                description="full-perm admin",
                is_system=True,
            )
            s.add(role)
            s.flush()
            for p in every_permission():
                if p == Permission.SYSTEM_ADMIN:
                    continue
                s.add(
                    TenantRolePermission(
                        id=f"perm-{uuid.uuid4().hex[:10]}",
                        role_id=role.id,
                        permission=p.value,
                    )
                )
            # Every provisioned tenant has a system Default group; several
            # admin routers hard-fail without it ("critical configuration
            # error"), so the harness mirrors provisioning.
            s.add(
                ResourceGroup(
                    id=f"group-default-{tid}",
                    tenant_id=tid,
                    name="Default",
                    description="Default group",
                    hierarchy_level=0,
                    inherit_resources=True,
                    is_active=True,
                    is_system=True,
                    group_type=GroupType.CUSTOM,
                )
            )
            s.flush()
            auth.create_tenant_admin(
                username=f"admin_{tid}",
                email=f"admin@{tid}.example",
                password=PASSWORD,
                tenant_id=tid,
                db=s,
                role_id=role.id,
            )
        s.commit()
    finally:
        s.close()

    return AdminHarness(client=client, factory=factory, app=app, engine=engine)


def install_schema(url: str) -> None:
    """Run the real install (`init_database()`) against `url`, in a subprocess
    so the app's process-wide engine never binds to it."""
    import subprocess
    import sys
    import tempfile
    import textwrap
    from pathlib import Path

    boot = textwrap.dedent(
        """
        import app.database as db
        import app.database.models  # noqa: F401
        db.init_database()
        print("INIT_OK")
        """
    )
    # init_database() skips a second pass under the same parent PID (its
    # Gunicorn sibling-worker sentinel, kept in the temp dir): a private
    # temp dir keeps this install from skipping, or making another test's
    # install skip.
    env = {
        **os.environ,
        "DATABASE_URL": url,
        "JWT_SECRET": os.environ.get("JWT_SECRET", "x" * 48),
        "ENCRYPTION_KEY": os.environ.get("ENCRYPTION_KEY", "a" * 44 + "="),
        "TMPDIR": tempfile.mkdtemp(prefix="pg-install-"),
    }
    root = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [sys.executable, "-c", boot], cwd=root, env=env, capture_output=True, text=True, timeout=600
    )
    assert "INIT_OK" in proc.stdout, proc.stderr[-4000:]


def throwaway_installed_pg_database(prefix: str):
    """Module fixture generator: like `throwaway_pg_database`, with the schema
    installed. For suites that write rows straight into a real schema."""
    for url in throwaway_pg_database(prefix):
        if url is not None:
            install_schema(url)
        yield url


def throwaway_pg_database(prefix: str):
    """Generator for a module fixture: yields the URL of an EMPTY Postgres
    database when BRUTOR_PG_TESTS=1 (created on BRUTOR_PG_TEST_URL's server,
    dropped afterwards), else None — pass it to `build_admin_harness`."""
    if os.environ.get("BRUTOR_PG_TESTS") != "1":
        yield None
        return
    import uuid as _uuid

    from sqlalchemy import text
    from sqlalchemy.engine import make_url

    admin = make_url(pg_test_url())
    name = f"{prefix}_{_uuid.uuid4().hex[:10]}"
    server = create_engine(admin, isolation_level="AUTOCOMMIT", future=True)
    with server.connect() as c:
        # From `template0`, not the server's default template: a shared test
        # server's `template1` can carry a schema (the core's test harness
        # installs its snapshot there), and a database made from it is not
        # empty. `init_database()` then takes it for a legacy install, stamps
        # it at head and creates nothing, so every table newer than that
        # snapshot is missing (as happened with `edition_marker`).
        c.execute(text(f'CREATE DATABASE "{name}" TEMPLATE template0'))
    try:
        yield admin.set(database=name).render_as_string(hide_password=False)
    finally:
        with server.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        server.dispose()
