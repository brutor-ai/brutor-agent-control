# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The last places where Community modules reached into
Enterprise features outside the composition registries.

* The provider-catalog seed stamped the Traffic Data Import columns itself;
  it now hands each row to the `LLM_PROVIDER_CATALOG_ROW` behaviour hook.
* `LLMRouterService` (Community) held the router cache configuration's get
  and update; they are `RouterCacheConfigService`, in the Enterprise router
  module that calls them.
* `resource_groups.py` (Community) served a group's effective KB collections
  and connectors; they are the router `resource_group_knowledge_base`
  (Enterprise), on the same paths.
* `app/services/llm/kserve_status_poller.py` had no importer and is gone.

`TestTheFullComposition` holds what did not change (the provider tests in it
were run against the code before the change). `TestTheCommunityComposition`
is the new contract.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import tests.conftest  # noqa: F401  (registers the SQLite JSONB shims)

import app.database.models  # noqa: F401
from app.composition import hooks
from app.composition.features import KNOWLEDGE_BASE, TRAFFIC_IMPORT
from app.core.logging_system import BrutorLogger
from app.database import Base
from app.database.init_llm_providers import PROVIDER_SEED_CATALOG, seed_llm_provider_catalog
from app.database.models.llm_provider_catalog import LlmProviderCatalog
from tests.community_process import community_composition

APP = Path(__file__).resolve().parents[1] / "app"
KB_PATHS = (
    "/v1/admin/resource-groups/{group_id}/effective-kb-collections",
    "/v1/admin/resource-groups/{group_id}/effective-kb-connectors",
)
# Provider keys the Traffic Data Import feature has an adapter for, and the
# import API of each.
IMPORTABLE = {
    "anthropic": "anthropic_enterprise_analytics",
    "openai": "openai_usage_costs",
}


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, future=True)()
    yield session
    session.close()
    engine.dispose()


def _catalog(db) -> dict:
    return {row.key: row for row in db.query(LlmProviderCatalog).all()}


def _seed(db) -> dict:
    seed_llm_provider_catalog(db, logger=BrutorLogger("test-l1"))
    db.commit()
    return _catalog(db)


def _paths(application) -> dict:
    return application.openapi()["paths"]


class TestTheFullComposition:

    def test_a_provider_without_an_adapter_is_left_alone(self, db):
        rows = _seed(db)
        for key in ("openai_compatible", "routing_group"):
            assert not rows[key].supports_usage_import, key
            assert rows[key].import_api is None, key


class TestTheCommunityComposition:
    def test_the_provider_seed_writes_every_provider_and_no_import_column(self, db):
        with community_composition():
            rows = _seed(db)
        assert len(rows) == len(PROVIDER_SEED_CATALOG)
        for key, row in rows.items():
            assert not row.supports_usage_import, key
            assert row.import_api is None, key
            assert not row.import_capabilities, key

    def test_no_hook_is_registered_at_the_provider_point(self):
        with community_composition():
            assert hooks.HOOKS.all(hooks.LLM_PROVIDER_CATALOG_ROW) == ()

    def test_the_effective_kb_routes_are_not_mounted(self):
        from tests.app_factory import create_community_app

        paths = _paths(create_community_app())
        assert not set(KB_PATHS) & set(paths)
        # ... while the Community routes of the prefix are.
        assert "/v1/admin/resource-groups/{group_id}/effective-agent-cards" in paths
        assert "/v1/admin/resource-groups/{group_id}/effective-llm-models" in paths


class TestAHookThatFails:
    def test_a_hook_that_raises_fails_the_seed(self, db):
        """As a failure inside the stamping did when it was written in the
        seed: it is not swallowed."""

        def broken(**kwargs):
            raise RuntimeError("boom")

        saved = dict(hooks.HOOKS._specs)
        try:
            hooks.HOOKS._specs = {}
            hooks.HOOKS.register([hooks.HookSpec("broken", hooks.LLM_PROVIDER_CATALOG_ROW, broken)])
            with pytest.raises(RuntimeError, match="boom"):
                seed_llm_provider_catalog(db, logger=BrutorLogger("test-l1"))
        finally:
            hooks.HOOKS._specs = saved

    def test_every_registered_hook_is_handed_every_row(self, db):
        seen = []
        saved = dict(hooks.HOOKS._specs)
        try:
            hooks.HOOKS._specs = {}
            hooks.HOOKS.register(
                [
                    hooks.HookSpec("one", hooks.LLM_PROVIDER_CATALOG_ROW, lambda **k: seen.append(("one", k["key"], k["obj"].key))),
                    hooks.HookSpec("two", hooks.LLM_PROVIDER_CATALOG_ROW, lambda **k: seen.append(("two", k["key"], k["obj"].key))),
                ]
            )
            seed_llm_provider_catalog(db, logger=BrutorLogger("test-l1"))
        finally:
            hooks.HOOKS._specs = saved
        keys = [entry["key"] for entry in PROVIDER_SEED_CATALOG]
        assert [s for s in seen if s[0] == "one"] == [("one", k, k) for k in keys]
        assert [s for s in seen if s[0] == "two"] == [("two", k, k) for k in keys]


# ---------------------------------------------------------------------------
# The source: what the Community modules no longer name.
# ---------------------------------------------------------------------------


def _names(rel: str) -> set:
    tree = ast.parse((APP / rel).read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.alias):
            found.add(node.name.rpartition(".")[2])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.add(node.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.update(node.module.split("."))
    return found


@pytest.mark.parametrize(
    "module, gone",
    [
        ("database/init_llm_providers.py", {"traffic_import", "usage_import", "catalog_seed_descriptors", "_DEFAULT_IMPORT_API"}),
        ("services/llm/router/llm_router_service.py", {"RouterCacheConfig", "router_cache_config", "get_router_cache_config", "update_router_cache_config"}),
        ("api/v1/endpoints/admin/resource_groups.py", {"kb_collections", "kb_connectors", "get_effective_kb_collections", "get_effective_kb_connectors", "KbCollection", "KbConnector"}),
        ("core/admin_user/services/session_revocation.py", {"SystemAdmin", "SystemAdminSession", "TenantAdminSession"}),
        ("services/system_ops/audit_row.py", {"SystemAdmin", "TenantAdmin"}),
        ("services/compliance/resolvers/oversight.py", {"SystemAdmin"}),
        ("api/v1/endpoints/service_info.py", {"SystemAdmin"}),
        ("api/v1/endpoints/admin/admin_rbac.py", {"SystemAdmin", "SystemAdminAuditLog"}),
    ],
)
def test_the_community_module_no_longer_names(module, gone):
    assert sorted(_names(module) & gone) == []


def test_the_status_poller_nobody_imported_is_gone():
    assert not (APP / "services/llm/kserve_status_poller.py").exists()


def test_the_admin_principal_type_imports_nothing_of_the_application():
    tree = ast.parse((APP / "core/admin_user/admin_principal.py").read_text(encoding="utf-8"))
    imported = [
        (node.module if isinstance(node, ast.ImportFrom) else alias.name)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    ]
    assert sorted(set(imported)) == ["__future__", "typing"]


def test_no_community_module_annotates_with_the_system_administrators_model():
    """`AdminPrincipal` is the annotation; the model is named only where its
    rows are made, which is the Enterprise extension's seed step since L3: no
    Community module names it."""
    from tests.edition_boundary import control_plane_boundary, enterprise_paths, is_enterprise, python_files

    paths = enterprise_paths(control_plane_boundary())
    naming = []
    for rel in python_files():
        if is_enterprise(rel, paths) or rel.startswith("app/database/models/"):
            continue
        tree = ast.parse((APP.parent / rel).read_text(encoding="utf-8"))
        if any(isinstance(n, ast.Name) and n.id in ("SystemAdmin", "SystemAdminSession", "SystemAdminAuditLog") for n in ast.walk(tree)):
            naming.append(rel)
    assert naming == []
