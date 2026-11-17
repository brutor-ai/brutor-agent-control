# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Three Enterprise behaviours are behind behaviour hooks
(`app/composition/hooks.py`), not inline in Community code.

* **Deployment teardown on MCP server delete** (`deployment`): deleting an MCP
  server removes its container / Kubernetes deployment / ECS task first.
* **The containerised step of catalog install** (`deployment`): importing a
  containerised catalog entry with `deploy_container=true` deploys it.
* **The Qdrant probe** (`knowledge_base`): the System Operations overview
  asks Qdrant's `/readyz`, and the connections page lists a Qdrant entry.

`TestRecorder*` is what the composition with everything does, written on the
code before the hooks existed and unchanged by them. `TestCommunity*` is the
contract of a composition without the Enterprise hooks: the behaviour is
absent, the Community code works without it, and where the caller asked for
the Enterprise behaviour by name (a containerised install) the answer says so
instead of succeeding quietly.
"""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.composition.extensions import EXTENSION_INTERFACE_VERSION
from fastapi.testclient import TestClient

from app.database import get_db
from app.database.models.docker_deploy import DockerDeployment
from app.database.models.ecs_deploy import EcsDeployment
from app.database.models.kubernetes_deploy import KubernetesDeployment
from app.database.models.mcp_catalog_entry import MCPCatalogEntry
from app.database.models.mcp_server import MCPServer
from app.database.models.server_config import ServerConfig
from app.database.models.system_alert import SystemAlert
from tests.admin_endpoint_harness import build_admin_harness
from tests.principal_matrix import community_app, no_network

ROOT = Path(__file__).resolve().parent.parent
DISCOVER = "app.services.mcp.server.mcp_server_manager.MCPServerManager.discover_capabilities"


# ---------------------------------------------------------------------------
# Fake deployment drivers: real subclasses (the teardown asks `isinstance`),
# with a recording manager and no backend.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The two compositions
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def h():
    """The composition that runs today, on the harness's database."""
    harness = build_admin_harness([], logger_name="test-c10")
    from tests.app_factory import create_app

    real = create_app()
    harness.app.router.routes.extend(real.router.routes)
    real.dependency_overrides = harness.app.dependency_overrides
    harness.real = real
    yield harness
    harness.close()


@pytest.fixture()
def full(h):
    return h.client, h.headers("t1")


@pytest.fixture(scope="module")
def community_client(h):
    return TestClient(community_app(h.app.dependency_overrides[get_db]), raise_server_exceptions=False)


@pytest.fixture()
def community(h, community_client):
    """The Community composition: its routers, its principals, its hooks."""
    from tests.principal_matrix import community_hooks, community_principals

    tenant = h.headers("t1")
    with community_principals(), community_hooks():
        yield community_client, tenant


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

_n = iter(range(1, 10_000))


def _server(h, client, headers, **deployment):
    """An MCP server of tenant t1, optionally with a deployment row."""
    name = f"c10-server-{next(_n)}"
    with no_network():
        r = h.client.post(
            "/v1/admin/mcp-servers?discover_capabilities=false",
            json={
                "name": name,
                "description": "c10",
                "base_url": f"http://{name}.internal:3000",
                "mcp_endpoint_path": "/mcp",
                "auth_type": "none",
            },
            headers=h.headers("t1"),
        )
    assert r.status_code in (200, 201), r.text
    body = r.json()
    sid = body.get("server", body)["id"]
    now = datetime.now(timezone.utc)

    def add(s):
        kind = deployment.get("kind")
        if kind == "docker":
            s.add(
                DockerDeployment(
                    tenant_id="t1", deployment_name=name, mcp_server_id=sid, mcp_server_name=name,
                    image_name="img:1", container_id=f"cid-{name}", container_name=f"tenant-t1-{name}",
                    internal_port=8099, status="running", deployed_at=now,
                )
            )
        elif kind == "k8s":
            s.add(
                KubernetesDeployment(
                    id=f"dpl-{name}", tenant_id="t1", deployment_name=name, namespace="mcp",
                    mcp_server_id=sid, deployed_at=now, image_name="img:1",
                )
            )
        elif kind == "ecs":
            s.add(
                EcsDeployment(
                    id=f"ecs-{name}", tenant_id="t1", deployment_name=name, mcp_server_id=sid,
                    cluster_name="cluster-a", task_definition_arn=f"arn:td/{name}", task_arn=f"arn:task/{name}",
                    region="eu-north-1", image_name="img:1", deployed_at=now,
                )
            )

    h.seed(add)
    return sid, name


def _exists(h, model, **by):
    s = h.Factory()
    try:
        return s.query(model).filter_by(**by).count()
    finally:
        s.close()


def _entry(h, runtime, **over):
    eid = f"cat-c10-{next(_n)}"
    fields = dict(
        id=eid, tenant_id="t1", slug=eid.replace("-", "_"), name=f"Entry {eid}", runtime=runtime,
        auth_pattern="none", enabled=True, source="system",
    )
    if runtime == "containerized":
        fields["containerized_config"] = {"image": "ghcr.io/example/brave:1", "port": 8099, "path": "/mcp"}
        fields["env_vars"] = [{"name": "BRAVE_API_KEY", "required": True}]
    else:
        fields["remote_config"] = {"fixedURL": "https://mcp.example.com"}
    fields.update(over)
    h.seed(lambda s: s.add(MCPCatalogEntry(**fields)))
    return eid


# =============================================================================
# 1. Deployment teardown on MCP server delete
# =============================================================================


class TestCommunityTeardown:
    def test_no_teardown_hook_is_registered(self, community):
        from app.composition.hooks import HOOKS, MCP_SERVER_DEPLOYMENT_TEARDOWN

        assert HOOKS.all(MCP_SERVER_DEPLOYMENT_TEARDOWN) == ()


# =============================================================================
# 2. The containerised step of catalog install
# =============================================================================

NOT_DEPLOYED_TODAY = ". Container deployment failed — deploy manually from MCP Server Deployment."


class TestCommunityContainerInstall:
    def test_no_install_hook_is_registered(self, community):
        from app.composition.hooks import HOOKS, MCP_CATALOG_CONTAINER_INSTALL

        assert HOOKS.one(MCP_CATALOG_CONTAINER_INSTALL) is None


# =============================================================================
# 3. The Qdrant probe
# =============================================================================


def _kinds(payload, key):
    return [item["kind"] for item in payload[key]]


class TestRecorderQdrant:

    def test_an_unconfigured_qdrant_has_no_card(self, h, full, monkeypatch):
        client, headers = full
        monkeypatch.delenv("REDIS_URL", raising=False)
        monkeypatch.delenv("BRUTOR_QDRANT_HTTP_URL", raising=False)
        monkeypatch.delenv("BRUTOR_QDRANT_URL", raising=False)
        r = client.get("/v1/admin/system/overview", headers=headers)
        assert r.status_code == 200, r.text
        assert "qdrant" not in _kinds(r.json(), "cards")


class TestCommunityQdrant:
    def test_no_probe_and_no_pool_hook_is_registered(self, community):
        from app.composition.hooks import HOOKS, SYSTEM_OPS_CONNECTION_POOLS, SYSTEM_OPS_DEPENDENCY_PROBES

        assert HOOKS.all(SYSTEM_OPS_DEPENDENCY_PROBES) == ()
        assert HOOKS.all(SYSTEM_OPS_CONNECTION_POOLS) == ()


    def test_the_connections_page_has_no_qdrant_entry(self, h, community):
        client, headers = community
        now = datetime.now(timezone.utc)
        h.seed(
            lambda s: s.add(
                SystemAlert(
                    id="sysalert-c10-ce", tenant_id=None, severity="critical", category="downstream_vector_db",
                    source="proxy", title="Qdrant unreachable", message="m", status="active",
                    first_seen_at=now, last_seen_at=now, dedup_key="c10-ce",
                )
            )
        )
        try:
            r = client.get("/v1/admin/system/connections", headers=headers)
        finally:
            h.seed(lambda s: s.query(SystemAlert).filter_by(id="sysalert-c10-ce").delete())
        assert r.status_code == 200, r.text
        assert _kinds(r.json(), "pools")[:2] == ["postgres", "redis"]
        assert "qdrant" not in _kinds(r.json(), "pools")
        assert "Qdrant" not in r.text


# =============================================================================
# The registry, and the Community modules
# =============================================================================


class TestTheHookRegistry:

    def _registry(self):
        from app.composition.hooks import HookRegistry

        return HookRegistry()

    def test_an_unknown_point_is_refused(self):
        from app.composition.hooks import HookSpec

        with pytest.raises(ValueError, match="unknown hook point"):
            self._registry().register([HookSpec("x", "mcp_server.something_else", lambda: None)])
        with pytest.raises(ValueError, match="unknown hook point"):
            self._registry().all("mcp_server.something_else")

    def test_a_name_taken_an_empty_name_and_a_non_callable_are_refused(self):
        from app.composition.hooks import HookSpec, MCP_SERVER_DEPLOYMENT_TEARDOWN as POINT

        registry = self._registry()
        registry.register([HookSpec("a", POINT, len)])
        with pytest.raises(ValueError, match="already registered"):
            registry.register([HookSpec("a", POINT, print)])
        registry.register([HookSpec("a", POINT, len)])  # the same hook again is a no-op
        assert len(registry.specs()) == 1
        for bad in (HookSpec("", POINT, len), HookSpec(" b", POINT, len), HookSpec("c", POINT, None)):
            with pytest.raises(ValueError):
                registry.register([bad])
        with pytest.raises(ValueError, match="not an Enterprise Edition feature"):
            registry.register([HookSpec("d", POINT, len, feature="free_lunch")])

    def test_a_refused_registration_registers_nothing(self):
        from app.composition.hooks import HookSpec, MCP_SERVER_DEPLOYMENT_TEARDOWN as POINT

        registry = self._registry()
        with pytest.raises(ValueError):
            registry.register([HookSpec("ok", POINT, len), HookSpec("bad", "nowhere", len)])
        assert registry.specs() == ()

    def test_a_single_point_takes_one_hook_only(self):
        from app.composition.hooks import HookSpec, MCP_CATALOG_CONTAINER_INSTALL as POINT

        registry = self._registry()
        assert registry.one(POINT) is None
        registry.register([HookSpec("first", POINT, len)])
        with pytest.raises(ValueError, match="takes one hook"):
            registry.register([HookSpec("second", POINT, print)])
        assert registry.one(POINT) is len

    def test_an_extension_registers_hooks_all_or_none(self):
        from app.composition.extensions import Extension, compose
        from app.composition.hooks import HOOKS, HookSpec, MCP_SERVER_DEPLOYMENT_TEARDOWN as POINT

        saved = dict(HOOKS._specs)
        try:
            with pytest.raises(ValueError):
                compose([Extension(name="x", interface_version=EXTENSION_INTERFACE_VERSION, hooks=[HookSpec("c10_ok", POINT, len), HookSpec("c10_bad", "nowhere", len)])])
            assert set(HOOKS._specs) == set(saved)
            compose([Extension(name="x", interface_version=EXTENSION_INTERFACE_VERSION, hooks=[HookSpec("c10_ok", POINT, len)])])
            assert len in HOOKS.all(POINT)
        finally:
            HOOKS._specs = saved


def _imports(path):
    tree = ast.parse((ROOT / path).read_text())
    out = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    out |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    return out


@pytest.mark.parametrize(
    "module,words",
    [
        ("app/api/v1/endpoints/admin/mcp_servers.py", ("DockerDriver", "K8sDriver", "ECSDriver", "remove_container", "stop_task")),
        ("app/api/v1/endpoints/admin/mcp_catalog.py", ("DeploymentSpec", "get_deployment_driver", "NANOBOT_META_ENV", "docker_manager")),
        ("app/services/system_ops/overview.py", ("readyz", "qdrant_task")),
        ("app/services/system_ops/connections.py", ("downstream_vector_db", "_qdrant_pool")),
    ],
)
def test_the_community_module_has_no_enterprise_code_left(module, words):
    tree = ast.parse((ROOT / module).read_text())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    # Prose is not code: a string that is a statement of its own (a docstring).
    docstrings = {
        n.value.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)
    }
    code_strings = [s for s in strings if s not in docstrings]
    for word in words:
        assert word not in names, word
        assert not [s for s in code_strings if word in s], word
