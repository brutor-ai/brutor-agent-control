# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The permission registry (`app/composition/permissions.py`): which permission
strings exist, what the Admin Console shows for each, which Enterprise Edition
feature each belongs to, and which default tenant roles are granted it.

Why it matters: the `Permission` enum is closed, and five places used to ask
it what exists (the strings a role may be given, the catalogue, what a system
administrator is created with, what Tenant Admin holds, the check of a saved
role). A permission from outside this code base (the Enterprise Edition's) could reach none of
them. The registry is the one answer, and this file holds three things to it:

  1. Nothing changed for the permissions that were there: the same strings in
     the same order, and the default roles hold what they held.
  2. A permission registered from outside reaches every one of the five
     places, and a route can require it.
  3. Nothing in `app/` asks the enum what exists any more.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi import APIRouter, Depends

from app.composition.features import (
    PROMPT_CACHE,
    BATCH_JOBS,
    DEPLOYMENT,
    DISCOVERY,
    ENTERPRISE_FEATURES,
    KNOWLEDGE_BASE,
    MCP_REGISTRY,
    POLICY_AS_CODE,
)
from app.composition.permissions import (
    BUILTIN_PERMISSIONS,
    PERMISSIONS,
    PermissionRegistry,
    PermissionSpec,
)
from app.constants.tenant import TenantDefaultRole
from app.core.admin_user.dependencies import require_permission
from app.core.admin_user.permissions import (
    PERMISSION_DESCRIPTIONS,
    Permission,
    get_all_permission_strings,
    validate_permission,
)
from app.core.logging_system import BrutorLogger
from app.database.models.admin import TenantRole
from app.services.tenant.tenant_provisioning import TenantProvisioner
from tests.test_admin_rbac_endpoints import harness  # noqa: F401  (fixture)
from tests.app_factory import every_permission
from tests.source_trees import python_sources

APP_DIR = Path(__file__).resolve().parent.parent / "app"
REGISTRY_FILE = APP_DIR / "composition" / "permissions.py"

READ_ONLY = TenantDefaultRole.READ_ONLY_USER.value
AUDITOR = TenantDefaultRole.AUDITOR.value
TENANT_ADMIN = TenantDefaultRole.ADMIN_USER.value
END_USER = TenantDefaultRole.END_USER.value


def _default_roles() -> dict:
    return TenantProvisioner(logger=BrutorLogger(__name__)).get_default_tenant_roles()


# ---------------------------------------------------------------------------
# 1. The permissions that were there.
# ---------------------------------------------------------------------------

# The Enterprise Edition permissions, as recorded. A permission moving between
# the editions is a decision, so it has to be made here as well.
RECORDED_ENTERPRISE = {
    MCP_REGISTRY: ["registry:read", "registry:config", "registry:search", "registry:health"],
    DEPLOYMENT: [
        "docker:images:read",
        "docker:images:write",
        "docker:images:delete",
        "docker:containers:read",
        "docker:containers:write",
        "docker:containers:delete",
        "docker:system:read",
        "docker:system:write",
        "llm:runner:machine:read",
        "llm:runner:machine:create",
        "llm:runner:machine:update",
        "llm:runner:machine:delete",
        "llm:runner:model:catalog:read",
        "llm:runner:model:deployed:read",
        "llm:runner:model:pull",
        "llm:runner:model:start",
        "llm:runner:model:stop",
        "llm:runner:model:delete",
    ],
    KNOWLEDGE_BASE: ["knowledge_base:read", "knowledge_base:create", "knowledge_base:update", "knowledge_base:delete"],
    BATCH_JOBS: ["batch_jobs:read", "batch_jobs:manage"],
    PROMPT_CACHE: ["prompt_cache:read", "prompt_cache:manage"],
    DISCOVERY: ["discovery:read", "discovery:write"],
    POLICY_AS_CODE: [
        "policy:read",
        "policy:validate",
        "policy:apply",
        "policy:rollback",
        "policy:export",
        "policy:template:read",
        "policy:template:manage",
    ],
}

# What the registry grants to Tenant Read-Only. Until C4 these were entries of
# the role's own list in `get_default_tenant_roles`.
RECORDED_READ_ONLY_GRANTS = [
    "registry:read",
    "registry:search",
    "registry:health",
    "docker:images:read",
    "docker:containers:read",
    "docker:system:read",
    "batch_jobs:read",
    "llm:runner:machine:read",
    "llm:runner:model:catalog:read",
    "llm:runner:model:deployed:read",
    "prompt_cache:read",
    "discovery:read",
    "policy:read",
    "policy:template:read",
]

# Everything Tenant Read-Only holds, sorted: what it held before the registry
# existed, without the two permissions that change something
# (`registry:config`) and with `prompt_cache:read`.
READ_ONLY_BEFORE_C4 = [
    "a2a:card:read",
    "a2a:delegation:read",
    "a2a:task:read",
    "a2a:tenant_key:read",
    "admin:audit:read",
    "admin:user:read",
    "agent_identity:read",
    "agent_skill:read",
    "ai:model:read",
    "ai_asset:read",
    "assurance_check:read",
    "assurance_inbox:read",
    "batch_jobs:read",
    "compliance:read",
    "discovery:read",
    "docker:containers:read",
    "docker:images:read",
    "docker:system:read",
    "enduser:read",
    "endusergroup:read",
    "evidence:read",
    "gateway:config:read",
    "gateway:status:read",
    "guardrail:read",
    "identity_policy:read",
    "idp:server:read",
    "llm:router:read",
    "llm:runner:machine:read",
    "llm:runner:model:catalog:read",
    "llm:runner:model:deployed:read",
    "mcp:server:health",
    "mcp:server:read",
    "notification:read",
    "oauth:client:read",
    "permissions:read",
    "policy:read",
    "policy:template:read",
    "prompt_cache:read",
    "proxy:logs:read",
    "proxy:logs:read_bodies",
    "proxy:stats:read",
    "registry:health",
    "registry:read",
    "registry:search",
    "resourcegroup:members:read",
    "resourcegroup:read",
    "roles:read",
    "semantic_policy:read",
    "server:config:read",
    "system:read",
    "virtual:server:read",
]


def test_the_registry_starts_as_the_enum_in_the_enums_order():
    # The enum is the Community Edition's (135); the Enterprise Edition's 42
    # are an enum of its package, registered by its extension after these.
    # The 177 were once one enum and the registry had its order, the
    # Enterprise members in between; each Enterprise permission is listed
    # there again (it says which permission it comes after; tests/test_permission_order.py holds the
    # recorded order). The Community ones keep the enum's order among themselves.
    assert [spec.value for spec in BUILTIN_PERMISSIONS] == [p.value for p in Permission]
    assert sorted(PERMISSIONS.values()) == sorted(p.value for p in every_permission())
    assert len(PERMISSIONS) == len(every_permission())
    community = {p.value for p in Permission}
    assert [v for v in PERMISSIONS.values() if v in community] == [p.value for p in Permission]


def test_descriptions_are_the_enums_and_a_missing_one_stays_missing():
    for permission in Permission:
        spec = PERMISSIONS.get(permission.value)
        assert spec is not None
        assert spec.description == PERMISSION_DESCRIPTIONS.get(permission), permission
        assert spec.category == permission.value.split(":")[0]


def test_the_enums_helpers_answer_from_the_registry():
    assert get_all_permission_strings() == PERMISSIONS.values()
    assert validate_permission("mcp:server:read") is True
    for not_a_permission in ("nope:read", "", "mcp:server", "MCP:SERVER:READ", " mcp:server:read"):
        assert validate_permission(not_a_permission) is False, not_a_permission


# ---------------------------------------------------------------------------
# 2. A permission from outside.
# ---------------------------------------------------------------------------

EXTENSION = PermissionSpec(
    value="ext:report:read",
    description="Read the extension's reports",
    feature=None,
    default_roles=(READ_ONLY,),
    read_only=True,
)
EXTENSION_UNGRANTED = PermissionSpec(value="ext:report:write")


@pytest.fixture()
def extended(monkeypatch):
    """The application's registry with two permissions added, for one test.

    Every reader holds the registry object itself, so the object is what has
    to change: its table is swapped for a copy, and put back afterwards.
    """
    monkeypatch.setattr(PERMISSIONS, "_specs", dict(PERMISSIONS._specs))
    PERMISSIONS.register([EXTENSION, EXTENSION_UNGRANTED])
    return PERMISSIONS


def test_the_fixture_leaves_the_registry_as_it_found_it():
    assert not PERMISSIONS.is_registered(EXTENSION.value)


def test_a_registered_permission_is_a_valid_permission(extended):
    assert get_all_permission_strings()[-2:] == ["ext:report:read", "ext:report:write"]
    assert validate_permission("ext:report:read") is True


def test_a_grant_the_role_already_holds_is_not_listed_twice(monkeypatch):
    monkeypatch.setattr(PERMISSIONS, "_specs", dict(PERMISSIONS._specs))
    held = PERMISSIONS._specs["mcp:server:read"]
    PERMISSIONS._specs["mcp:server:read"] = PermissionSpec(
        value=held.value, description=held.description, default_roles=(READ_ONLY,), read_only=True
    )
    permissions = _default_roles()[READ_ONLY]["permissions"]
    assert permissions.count("mcp:server:read") == 1


def _mount_extension_route(harness) -> None:  # noqa: F811
    router = APIRouter(prefix="/ext")

    @router.get("/report")
    def report(_: object = Depends(require_permission("ext:report:read"))):
        return {"report": "ok"}

    harness.client.app.include_router(router, prefix="/v1")


def _role(harness, token: str, name: str, permissions: list):  # noqa: F811
    return harness.client.post(
        "/v1/admin/users/roles?tenant_id=t1",
        headers=harness.bearer(token),
        json={"name": name, "description": "d", "permissions": permissions},
    )


# ---------------------------------------------------------------------------
# What the registry refuses.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec,why",
    [
        (PermissionSpec(value="mcp:server:read"), "registered twice"),
        (PermissionSpec(value=""), "empty or padded"),
        (PermissionSpec(value=" ext:x "), "empty or padded"),
        (PermissionSpec(value="ext:x", feature="not_a_feature"), "unknown feature"),
        (PermissionSpec(value="ext:x", default_roles=("Tenant Superuser",)), "not a default role"),
        # Tenant Admin holds everything already; naming it would be a second,
        # weaker statement of the same thing.
        (PermissionSpec(value="ext:x", default_roles=(TENANT_ADMIN,)), "not a default role"),
    ],
)
def test_registration_refuses(spec, why):
    registry = PermissionRegistry(BUILTIN_PERMISSIONS)
    with pytest.raises(ValueError, match=why):
        registry.register([PermissionSpec(value="ext:fine"), spec])
    # All or none: the good one that came with it is not registered either.
    assert registry.values() == [p.value for p in Permission]


def test_a_batch_may_not_repeat_itself():
    registry = PermissionRegistry(BUILTIN_PERMISSIONS)
    with pytest.raises(ValueError, match="registered twice"):
        registry.register([PermissionSpec(value="ext:x"), PermissionSpec(value="ext:x")])
    assert not registry.is_registered("ext:x")


def test_an_enterprise_feature_and_a_grant_are_accepted():
    registry = PermissionRegistry(BUILTIN_PERMISSIONS)
    registry.register([PermissionSpec(value="ext:x", feature=DISCOVERY, default_roles=(AUDITOR, READ_ONLY), read_only=True)])
    assert registry.default_role_grants(AUDITOR) == ["ext:x"]
    assert registry.default_role_grants(READ_ONLY)[-1] == "ext:x"
    assert registry.get("ext:x").feature == DISCOVERY


# ---------------------------------------------------------------------------
# 3. Nothing asks the enum what exists.
# ---------------------------------------------------------------------------

_ENUMERATORS = {"len", "list", "set", "frozenset", "tuple", "sorted", "iter", "enumerate", "dict", "map", "filter"}
_ENUM_TABLES = {"__members__", "_member_map_", "_value2member_map_", "_member_names_"}


def enumerations_of_the_enum(source: str) -> list:
    """Where a module asks `Permission` what exists, or asks it whether a
    string is a permission (`Permission("x")`), as line numbers.

    Naming a member (`Permission.MCP_SERVER_READ`), annotating with the enum
    and `isinstance(x, Permission)` are how the enum is meant to be used.
    """
    found = []
    for node in ast.walk(ast.parse(source)):
        def is_enum(n: ast.AST) -> bool:
            return isinstance(n, ast.Name) and n.id in ("Permission", "PERMISSION_DESCRIPTIONS")

        if isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)) and is_enum(node.iter):
            found.append(node.iter.lineno)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "Permission":
                found.append(node.lineno)
            elif node.func.id in _ENUMERATORS and any(is_enum(a) for a in node.args):
                found.append(node.lineno)
        elif isinstance(node, ast.Attribute) and is_enum(node.value) and node.attr in _ENUM_TABLES:
            found.append(node.lineno)
        elif isinstance(node, ast.Compare) and any(is_enum(c) for c in node.comparators):
            if any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
                found.append(node.lineno)
        elif isinstance(node, ast.Starred) and is_enum(node.value):
            found.append(node.lineno)
    return sorted(found)


def test_the_guard_sees_every_way_of_asking():
    asked = {
        "for p in Permission: pass": 1,
        "x = [p.value for p in Permission]": 1,
        "x = {p for p in Permission if p}": 1,
        "n = len(Permission)": 1,
        "x = list(Permission)": 1,
        "x = sorted(Permission, key=str)": 1,
        "x = Permission.__members__": 1,
        "x = Permission._value2member_map_": 1,
        "ok = Permission('mcp:server:read')": 1,
        "ok = value in Permission": 1,
        "ok = value not in PERMISSION_DESCRIPTIONS": 1,
        "x = [*Permission]": 1,
        "for p in PERMISSION_DESCRIPTIONS: pass": 1,
    }
    for source, count in asked.items():
        assert len(enumerations_of_the_enum(source)) == count, source
    meant = [
        "x = Permission.MCP_SERVER_READ.value",
        "def f(p: Permission) -> Permission: return p",
        "ok = isinstance(p, Permission)",
        "ok = p is Permission.SYSTEM_ADMIN",
        "d = PERMISSION_DESCRIPTIONS.get(p)",
        "from app.core.admin_user.permissions import Permission",
        "for value in PERMISSIONS.values(): pass",
    ]
    for source in meant:
        assert enumerations_of_the_enum(source) == [], source


def test_nothing_in_the_app_asks_the_enum_what_exists():
    """The registry is the one module that reads the enum as a list. The
    enum's own module holds the descriptions table, which is keyed by member
    and enumerates nothing."""
    offenders = {}
    for path in python_sources(APP_DIR):
        if path == REGISTRY_FILE:
            continue
        lines = enumerations_of_the_enum(path.read_text())
        if lines:
            offenders[str(path.relative_to(APP_DIR.parent))] = lines
    assert offenders == {}


def test_the_registry_itself_reads_the_enum_once():
    assert len(enumerations_of_the_enum(REGISTRY_FILE.read_text())) == 1
