# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
An outbound refusal raised inside a task group reaches the administrator.

The MCP SDK runs its transport in an anyio task group, so the policy's
`OutboundRefused` comes out of capability discovery wrapped in an
`ExceptionGroup`, whose own message is "unhandled errors in a TaskGroup (1
sub-exception)". Found on a real cluster: the console showed "Upstream
discovery failed: unhandled errors in a TaskGroup" for an in-cluster MCP
server the private-address policy refused, instead of the host and the
setting that would allow it (`BRUTOR_OUTBOUND_ALLOW_HOSTS`).

The destination here is an in-cluster service name that resolves to a
Kubernetes ClusterIP (10.43.0.17, RFC 1918), which the policy refuses by
default: the resolver is replaced, nothing leaves the machine.
"""

from __future__ import annotations

import ipaddress

import pytest

from app.core.logging_system import BrutorLogger
from app.services import outbound_policy
from app.services.outbound_http import OutboundRefused, error_text

SETTING = "BRUTOR_OUTBOUND_ALLOW_HOSTS"
IN_CLUSTER = "mcp-app-map.brutor.svc.cluster.local"
CLUSTER_IP = "10.43.0.17"


@pytest.fixture(autouse=True)
def _in_cluster_resolver(monkeypatch):
    monkeypatch.delenv(SETTING, raising=False)
    real = outbound_policy.resolve

    def resolve(host: str):
        if host == IN_CLUSTER:
            return [ipaddress.ip_address(CLUSTER_IP)]
        return real(host)

    monkeypatch.setattr(outbound_policy, "resolve", resolve)


def _refusal() -> OutboundRefused:
    return OutboundRefused(IN_CLUSTER, f"{IN_CLUSTER} resolves to a private address (RFC 1918)")


def test_a_refusal_inside_nested_exception_groups_is_what_is_said():
    try:
        try:
            raise _refusal()
        except OutboundRefused as refused:
            raise ExceptionGroup("unhandled errors in a TaskGroup", [ExceptionGroup("inner", [refused])])
    except ExceptionGroup as group:
        said = error_text(group)
    assert IN_CLUSTER in said and SETTING in said
    assert "TaskGroup" not in said


def test_a_refusal_that_caused_the_error_is_what_is_said():
    try:
        try:
            raise _refusal()
        except OutboundRefused as refused:
            raise RuntimeError("session closed") from refused
    except RuntimeError as error:
        said = error_text(error)
    assert IN_CLUSTER in said and SETTING in said


def test_a_group_without_a_refusal_says_what_its_members_say():
    group = ExceptionGroup("unhandled errors in a TaskGroup", [ConnectionError("connection reset"), TimeoutError()])
    said = error_text(group)
    assert "connection reset" in said and "TimeoutError" in said and "TaskGroup" not in said
    assert error_text(ValueError("plain")) == "plain"


@pytest.fixture()
def harness():
    from app.api.v1.endpoints.admin.mcp_servers import create_mcp_server_router
    from app.database.models.mcp_server import MCPServer
    from app.services.mcp.server.mcp_server_manager import MCPServerManager
    from tests.admin_endpoint_harness import build_admin_harness

    logger = BrutorLogger("test-refusal-in-task-groups")
    h = build_admin_harness([create_mcp_server_router(MCPServerManager(logger=logger), logger)])
    h.seed(lambda s: s.add(MCPServer(
        id="srv-cluster", tenant_id="t1", name="app-map", base_url=f"http://{IN_CLUSTER}:8000",
        mcp_endpoint_path="mcp", enabled=True, auth_type="none", timeout=5,
    )))
    yield h
    h.close()


def test_capability_discovery_of_a_refused_in_cluster_host_says_the_host_and_the_setting(harness):
    answer = harness.client.post("/v1/admin/mcp-servers/srv-cluster/discover-capabilities", headers=harness.headers())
    assert answer.status_code == 502, answer.text
    detail = answer.json()["detail"]
    assert detail.startswith("Upstream discovery failed: ")
    assert IN_CLUSTER in detail and SETTING in detail, detail
    assert "TaskGroup" not in detail, detail
