# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Administrators are registered principals.

`verify_token` dispatches on the token's `admin_type` through
`app/composition/principals.py`; `resolve_tenant_id` asks the principal; the
writes of what belongs to the installation ask `require_platform_admin`.

Three parts:

* the registry itself, and the refusals: a type nobody registered, a claim
  that is missing, a verifier that raises, a tenant administrator naming
  another tenant — each refused, never read as some default principal;
* the composition that runs today (both editions): the system administrator
  is the platform administrator, and no permission makes a tenant
  administrator one;
* the Community composition (the registries without the Enterprise entries):
  a system administrator's token is refused everywhere, its login route and
  the Enterprise routers are not there, a tenant administrator works as
  before, and `platform:catalog:manage` gates the catalog writes.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.composition.features import MULTI_TENANT
from app.composition.principals import (
    BUILTIN_PRINCIPALS,
    HUMAN_SESSION,
    PLATFORM_CATALOG_MANAGE,
    PRINCIPALS,
    PrincipalRegistry,
    PrincipalSpec,
)
from app.composition.routers import ROUTERS
from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.unified_auth_service import UnifiedAuthService
from app.database.models.admin import SystemAdminSession, TenantAdminSession
from tests.admin_endpoint_harness import JWT_SECRET, build_admin_harness
from tests.app_factory import edition_under_test
from tests.principal_matrix import admin_routes, community_app, community_principals, key, sweep

ENTERPRISE = edition_under_test() == "enterprise"

RECORDING = Path(__file__).parent / "fixtures" / "admin_principal_matrix.json"

LLM_CATALOG_ENTRY = {"provider_key": "openai", "model_name": "c7-model", "display_name": "C7", "mode": "chat"}
GLOBAL_MCP_ENTRY = {"name": "C7", "slug": "c7-global", "runtime": "remote", "is_global": True}


def _noop_verify(service, payload, db):
    return None


def _noop_resolve(admin, tenant_id_query):
    raise HTTPException(status_code=403)


def _noop_session(db, session_id):
    return None


def _spec(name="robot", **kw):
    fields = dict(
        name=name, credential_class=HUMAN_SESSION, verify=_noop_verify,
        resolve_tenant=_noop_resolve, find_session=_noop_session,
    )
    fields.update(kw)
    return PrincipalSpec(**fields)


class registered:
    """`spec` in the process-wide registry for a `with` block."""

    def __init__(self, *specs):
        self.specs = specs

    def __enter__(self):
        self.saved = dict(PRINCIPALS._specs)
        PRINCIPALS.register(self.specs)

    def __exit__(self, *exc):
        PRINCIPALS._specs = self.saved
        return False


@pytest.fixture(scope="module")
def h():
    harness = build_admin_harness([], logger_name="test-principal-registry")
    from tests.app_factory import create_app

    real = create_app()
    harness.app.router.routes.extend(real.router.routes)
    real.dependency_overrides = harness.app.dependency_overrides
    harness.real = real
    yield harness
    harness.close()


@pytest.fixture(scope="module")
def auth():
    return UnifiedAuthService(jwt_secret=JWT_SECRET)


def _token(h, tenant=None):
    headers = h.headers(tenant) if tenant else h.sys_headers()
    return headers["Authorization"].split(" ", 1)[1]


def _reissue(token, **claims):
    """The token's claims, changed, signed with the real secret. `None` drops a claim."""
    payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    for name, value in claims.items():
        if value is None:
            payload.pop(name, None)
        else:
            payload[name] = value
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


# =============================================================================
# The registry
# =============================================================================

class TestTheRegistry:


    def test_the_community_principal_imports_nothing_of_the_system_administrator(self):
        import ast

        from app.core.admin_user import tenant_principal

        tree = ast.parse(Path(tenant_principal.__file__).read_text())
        names = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        } | {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        assert not {n for n in names if n and "system" in n.lower()}
        assert "SystemAdmin" not in Path(tenant_principal.__file__).read_text()

    @pytest.mark.parametrize("asked", [
        None, "", " tenant", "tenant ", "Tenant", "TENANT", "System", "system_admin", "admin", "root",
        0, 1, True, b"system", ["system"], {"system": 1}, ("system",), object(),
    ])
    def test_a_type_nobody_registered_is_no_principal(self, asked):
        assert PRINCIPALS.get(asked) is None
        assert PRINCIPALS.is_platform_wide(asked) is False
        assert PRINCIPALS.is_platform_admin(asked, SimpleNamespace(has_permission=lambda _p: True)) is False


    def test_the_same_name_twice_in_one_call_is_refused(self):
        registry = PrincipalRegistry()
        with pytest.raises(ValueError, match="registered twice"):
            registry.register([_spec("a"), _spec("a")])
        assert len(registry) == 0


    def test_the_permission_is_registered_and_is_a_community_permission(self):
        from app.composition.permissions import PERMISSIONS

        assert Permission.PLATFORM_CATALOG_MANAGE.value == PLATFORM_CATALOG_MANAGE == "platform:catalog:manage"
        spec = PERMISSIONS.get(PLATFORM_CATALOG_MANAGE)
        assert spec is not None and spec.feature is None and spec.default_roles == ()
        assert spec.description


# =============================================================================
# verify_token dispatches through the registry
# =============================================================================

class TestVerifyTokenDispatch:
    def _db(self, h):
        return h.Factory()


    def test_the_registered_verifier_is_what_decides(self, h, auth):
        """Dispatch, not a second copy of the branches: the verifier in the
        registry is the one asked, with the token's claims."""
        asked = []

        def verify(service, payload, db):
            asked.append((service, payload["admin_type"], payload["user_id"]))
            return None

        token = _token(h, "t1")
        db = self._db(h)
        saved = dict(PRINCIPALS._specs)
        try:
            PRINCIPALS._specs = {**saved, "tenant": _spec("tenant", verify=verify)}
            assert auth.verify_token(token, db) is None
            assert len(asked) == 1 and asked[0][0] is auth and asked[0][1] == "tenant"
        finally:
            PRINCIPALS._specs = saved
            db.close()
        db = self._db(h)
        try:
            assert auth.verify_token(token, db) is not None
        finally:
            db.close()

    @pytest.mark.parametrize("claims", [
        {"admin_type": None},
        {"admin_type": "robot"},
        {"admin_type": "System"},
        {"admin_type": "SYSTEM"},
        {"admin_type": " system"},
        {"admin_type": ""},
        {"admin_type": 1},
        {"admin_type": True},
        {"admin_type": ["system"]},
        {"admin_type": {"system": True}},
        {"typ": None},
        {"typ": "portal"},
        {"typ": "Admin"},
        {"typ": None, "admin_type": None},
    ])
    @pytest.mark.parametrize("who", sorted(PRINCIPALS.names()))  # every registered principal
    def test_a_live_session_with_an_unknown_or_missing_type_is_refused(self, h, auth, who, claims):
        """The session is real and active; only what the token says it is
        changed. Nothing is read as a default principal."""
        token = _reissue(_token(h, "t1" if who == "tenant" else None), **claims)
        db = self._db(h)
        try:
            assert auth.verify_token(token, db) is None
        finally:
            db.close()
        r = h.client.get("/v1/admin/users/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401

    def test_a_tenant_session_presented_as_a_system_administrator_is_refused(self, h, auth):
        """The verifier of the type the token claims is asked, against its
        own sessions table: a tenant session id is not a system session."""
        token = _reissue(_token(h, "t1"), admin_type="system")
        db = self._db(h)
        try:
            assert auth.verify_token(token, db) is None
        finally:
            db.close()


    def test_a_tenant_token_naming_another_tenant_is_refused(self, h, auth):
        token = _reissue(_token(h, "t1"), tenant_id="t2")
        db = self._db(h)
        try:
            assert auth.verify_token(token, db) is None
        finally:
            db.close()

    @pytest.mark.parametrize("error", [RuntimeError("boom"), ValueError("bad"), KeyError("jti"), HTTPException(status_code=200)])
    def test_a_verifier_that_raises_refuses(self, h, auth, error):
        def verify(service, payload, db):
            raise error

        token = _token(h, "t1")
        db = self._db(h)
        saved = dict(PRINCIPALS._specs)
        try:
            PRINCIPALS._specs = {**saved, "tenant": _spec("tenant", verify=verify)}
            assert auth.verify_token(token, db) is None
            r = h.client.get("/v1/admin/users/me", headers={"Authorization": f"Bearer {token}"})
            assert r.status_code == 401
        finally:
            PRINCIPALS._specs = saved
            db.close()

    @pytest.mark.parametrize("answer", [True, "admin", ("admin",), (None, None), [], {}, 1])
    def test_a_verifier_that_answers_something_else_than_an_administrator_refuses(self, h, auth, answer):
        token = _token(h, "t1")
        db = self._db(h)
        saved = dict(PRINCIPALS._specs)
        try:
            PRINCIPALS._specs = {**saved, "tenant": _spec("tenant", verify=lambda s, p, d: answer)}
            assert auth.verify_token(token, db) is None
        finally:
            PRINCIPALS._specs = saved
            db.close()

    def test_a_principal_of_an_extension_is_verified_by_its_own_verifier(self, h, auth):
        robot = SimpleNamespace(id="r1", username="robot", is_active=True)
        token = jwt.encode(
            {"typ": "admin", "admin_type": "robot", "user_id": "r1", "jti": str(uuid.uuid4()),
             "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
            JWT_SECRET, algorithm="HS256",
        )
        db = self._db(h)
        try:
            assert auth.verify_token(token, db) is None  # nobody registered it
            with registered(_spec("robot", verify=lambda s, p, d: (robot, None))):
                admin, session, admin_type = auth.verify_token(token, db)
                assert admin is robot and session is None and admin_type == "robot"
            assert auth.verify_token(token, db) is None
        finally:
            db.close()

    def test_logout_ends_the_session_of_the_principal_the_token_names_only(self, h, auth):
        db = self._db(h)
        try:
            fresh = auth.login_tenant_admin("admin_t2", "Secret123!", "t2", db)["access_token"]
            db.commit()
            jti = jwt.decode(fresh, JWT_SECRET, algorithms=["HS256"])["jti"]
            for claims in ({"admin_type": "system"}, {"admin_type": "robot"}, {"admin_type": None}, {"typ": None}):
                assert auth.logout(_reissue(fresh, **claims), db) is False
                assert db.query(TenantAdminSession).filter_by(id=jti).one().is_active is True
            assert auth.logout(fresh, db) is True
            assert db.query(TenantAdminSession).filter_by(id=jti).one().is_active is False
            db.commit()
        finally:
            db.close()


# =============================================================================
# resolve_tenant_id asks the principal
# =============================================================================

class TestResolveTenantId:
    def _resolve(self, *a):
        from app.core.admin_user.dependencies import resolve_tenant_id

        return resolve_tenant_id(*a)

    def test_a_tenant_administrator_is_pinned_to_its_tenant(self):
        admin = SimpleNamespace(tenant_id="t1")
        assert self._resolve(admin, "tenant") == "t1"
        assert self._resolve(admin, "tenant", None) == "t1"
        assert self._resolve(admin, "tenant", "") == "t1"
        assert self._resolve(admin, "tenant", "t1") == "t1"

    @pytest.mark.parametrize("named", ["t2", "T1", "t1 ", " t1", "t1/../t2", "*", "null"])
    def test_a_tenant_administrator_naming_another_tenant_is_refused(self, named):
        with pytest.raises(HTTPException) as e:
            self._resolve(SimpleNamespace(tenant_id="t1"), "tenant", named)
        assert e.value.status_code == 403

    @pytest.mark.parametrize("own", [None, ""])
    def test_a_tenant_administrator_without_a_tenant_acts_on_none(self, own):
        for named in (None, "t2"):
            with pytest.raises(HTTPException) as e:
                self._resolve(SimpleNamespace(tenant_id=own), "tenant", named)
            assert e.value.status_code == 403


    @pytest.mark.parametrize("admin_type", [None, "", "robot", "System", "TENANT", 1, object()])
    def test_an_unregistered_type_acts_on_no_tenant(self, admin_type):
        """It used to fall through to the system administrator's branch and
        get the tenant it named."""
        for admin in (SimpleNamespace(tenant_id="t1"), SimpleNamespace()):
            for named in (None, "t1", "t2"):
                with pytest.raises(HTTPException) as e:
                    self._resolve(admin, admin_type, named)
                assert e.value.status_code == 403

    def test_a_system_administrator_resolves_nothing_in_the_community_composition(self):
        with community_principals():
            with pytest.raises(HTTPException) as e:
                self._resolve(SimpleNamespace(), "system", "t1")
            assert e.value.status_code == 403
            assert self._resolve(SimpleNamespace(tenant_id="t1"), "tenant") == "t1"


# =============================================================================
# The composition that runs today: the system administrator is the platform
# administrator
# =============================================================================

CATALOG_WRITES = [
    ("POST", "/v1/admin/llm-catalog", LLM_CATALOG_ENTRY),
    ("PATCH", "/v1/admin/llm-catalog/nope", {"display_name": "x"}),
    ("DELETE", "/v1/admin/llm-catalog/nope", None),
    ("POST", "/v1/admin/mcp-catalog", GLOBAL_MCP_ENTRY),
]


# =============================================================================
# The Community composition
# =============================================================================

@pytest.fixture(scope="class")
def community(h):
    """The Community application, and tokens signed in for beforehand: the
    system administrator's is a real, live session of this database."""
    from app.database import get_db

    tokens = SimpleNamespace(
        system=h.sys_headers(),
        tenant=h.headers("t1"),
        other=h.headers("t2"),
        no_platform=h.limited_headers(
            "t1",
            [p for p in Permission if p not in (Permission.SYSTEM_ADMIN, Permission.PLATFORM_CATALOG_MANAGE)],
            username="c7_no_platform",
        ),
        only_platform=h.limited_headers("t1", [Permission.PLATFORM_CATALOG_MANAGE], username="c7_only_platform"),
    )
    app = community_app(h.app.dependency_overrides[get_db])
    with community_principals():
        yield SimpleNamespace(app=app, client=TestClient(app, raise_server_exceptions=False), tokens=tokens)


