# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The fetch-time outbound policy at the Control Plane's call sites: a destination an administrator entered is fetched through
`app/services/outbound_http.py`, so a host name that resolves to a private
address is refused when it is fetched, and the answer names the setting
that would allow it (`BRUTOR_OUTBOUND_ALLOW_HOSTS`).

Every destination here is `localhost:9`: a name (so the save-time check,
which judges only IP literals, lets it through), resolving to loopback, on a
port nothing listens on (`tests/hermetic.py` DEAD_PORTS). Before the policy
clients, each of these was a plain "connection refused"; nothing leaves the
machine either way.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.logging_system import BrutorLogger

DEAD = "http://localhost:9"
SETTING = "BRUTOR_OUTBOUND_ALLOW_HOSTS"


@pytest.fixture(autouse=True)
def _no_allow_list(monkeypatch):
    monkeypatch.delenv(SETTING, raising=False)


# ── MCP servers ─────────────────────────────────────────────────────────────


def _mcp_server(url: str):
    return SimpleNamespace(
        id="srv-1", tenant_id="t1", name="srv", mcp_url=url, base_url=url, timeout=5,
        auth_type="none", oauth_config=None, idp_server_id=None,
    )


async def test_the_mcp_connection_test_is_refused_by_the_policy():
    from app.services.mcp.server.mcp_server_manager import MCPServerManager

    result = await MCPServerManager(BrutorLogger("test-outbound-sites")).test_connection(
        _mcp_server(f"{DEAD}/mcp"), timeout=5
    )
    assert result["connected"] is False
    assert result["error_type"] == "outbound_policy", result
    assert SETTING in result["message"]


def test_the_mcp_sdk_sessions_use_the_policy_client(monkeypatch):
    """Capability discovery goes through the MCP SDK, which builds its own
    httpx client: it is handed the policy's."""
    from app.services.mcp.server import mcp_server_manager as module
    from app.services.outbound_http import AsyncPolicyTransport

    seen = {}

    def fake_streamablehttp_client(url, **kwargs):
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(module, "streamablehttp_client", fake_streamablehttp_client)
    module.MCPServerManager(BrutorLogger("test-outbound-sites"))._create_session_context(_mcp_server(f"{DEAD}/mcp"))
    factory = seen["httpx_client_factory"]
    client = factory(headers={"a": "b"})
    assert isinstance(client._transport, AsyncPolicyTransport)


# ── identity providers ──────────────────────────────────────────────────────


@pytest.fixture()
def idp_h():
    from app.api.v1.endpoints.admin.idp_servers import create_idp_admin_router
    from tests.admin_endpoint_harness import build_admin_harness

    harness = build_admin_harness([create_idp_admin_router(BrutorLogger("test-outbound-sites"))])
    yield harness
    harness.close()


def _idp(h) -> str:
    from app.database.models.idp_server import IdPServer

    def fn(s):
        s.add(IdPServer(
            id="idp-1", tenant_id="t1", name="corp", type="oidc", issuer="https://corp.example",
            well_known_url=f"{DEAD}/.well-known/openid-configuration", enabled=True,
        ))

    h.seed(fn)
    return "idp-1"


def test_the_idp_discovery_is_refused_with_400(idp_h):
    r = idp_h.client.post(f"/v1/admin/idps/{_idp(idp_h)}/discover", headers=idp_h.headers("t1"))
    assert r.status_code == 400, r.text
    assert SETTING in r.json()["detail"]


def test_the_idp_test_reports_the_refusal(idp_h):
    r = idp_h.client.post(f"/v1/admin/idps/{_idp(idp_h)}/test", headers=idp_h.headers("t1"))
    assert r.status_code == 200, r.text
    assert r.json()["connected"] is False
    assert SETTING in (r.json()["error"] or "")


# ── agent cards ─────────────────────────────────────────────────────────────


async def test_an_agent_card_fetch_is_refused_with_400():
    from fastapi import HTTPException

    from app.api.v1.endpoints.admin.agent_cards import _fetch_well_known_card

    with pytest.raises(HTTPException) as refused:
        await _fetch_well_known_card(DEAD, 5)
    assert refused.value.status_code == 400
    assert SETTING in refused.value.detail


# ── audit export ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", ["webhook", "splunk_hec"])
def test_an_audit_export_destination_is_refused(backend):
    from app.services.audit_export import backends

    if backend == "webhook":
        ok, error = backends.send_webhook(b"x", url=f"{DEAD}/hook", timeout_seconds=5)
    else:
        import gzip

        ok, error = backends.send_splunk_hec(gzip.compress(b'{"a":1}\n'), url=f"{DEAD}/services/collector/event", token="t",
                                             timeout_seconds=5)
    assert ok is False
    assert SETTING in error


# ── MCP registries (aiohttp) ────────────────────────────────────────────────


@pytest.mark.parametrize("url", [DEAD, "http://127.0.0.1:9"])
async def test_a_registry_request_is_refused_with_400(url):
    pytest.importorskip("brutor_enterprise")
    from fastapi import HTTPException

    from brutor_enterprise.mcp_registry.services.registry.mcp_registry_manager import MCPRegistryManager

    registry = SimpleNamespace(id="reg-1", name="corp", url=url, timeout=5, auth_type="none", auth_token=None,
                               registry_type="official")
    with pytest.raises(HTTPException) as refused:
        await MCPRegistryManager(BrutorLogger("test-outbound-sites")).make_request(registry, "servers")
    assert refused.value.status_code == 400, refused.value.detail
    assert SETTING in refused.value.detail


# ── model runner machines ───────────────────────────────────────────────────


async def test_a_runner_machine_test_reports_the_refusal():
    pytest.importorskip("brutor_enterprise")
    from brutor_enterprise.deployment.services.llm_adapter.deploy.dmr_deployment_adapter import DMRAdapter

    result = await DMRAdapter().test_connection(DEAD)
    assert result.success is False
    assert SETTING in (result.error or "")


# ── the rest: dynamic client registration, notification channels, guardrail providers ─


async def test_a_dynamic_client_registration_is_refused():
    from app.services.oauth.client_registration_service import DynamicClientRegistrationService
    from app.services.outbound_http import OutboundRefused

    idp = SimpleNamespace(id="idp-1", name="corp", registration_endpoint=f"{DEAD}/register")
    with pytest.raises(OutboundRefused):
        await DynamicClientRegistrationService(None, BrutorLogger("test-outbound-sites")).register_client(
            idp=idp, client_name="c", redirect_uris=["https://app.example/cb"]
        )


def test_a_notification_channel_is_refused():
    from app.services.assurance_notifications import NotificationDeliverer

    ok, error = NotificationDeliverer().post(f"{DEAD}/hook", b"{}", {})
    assert ok is False
    assert SETTING in error


async def test_a_guardrail_provider_probe_is_refused():
    from app.services.guardrails.provider_probe import _probe_presidio

    result = await _probe_presidio(f"{DEAD}/analyze", {})
    assert SETTING in (result.error or "")
