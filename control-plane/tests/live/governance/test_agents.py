# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Live GOVERNANCE suite — Agents (mirrors "Agent Governance with Brutor",
Parts 2 & 3). The agent principal is a normal API key BOUND to an agent
identity (auth stamps actor_agent_id). The core governance dimension is
per-agent runtime authorization — the `agent_authz_decision` on the audit row:

    ungranted tool → deny (403) · approval-required → 202 park · granted → allow (200)

plus A2A signed-card verification and the caller-binding refusal.

Provisioning: setup-mcp-demo (registers the ACME server + tools) +
setup-agent-demo (agent identities, grants, signed cards). Run:
  pytest tests/live/governance/test_agents.py -m live_e2e
"""

from __future__ import annotations

import pytest

from .helpers import (
    a2a_discover_card,
    a2a_message_send,
    assert_log,
    demo_agent_env,
    demo_mcp_env,
    mcp_call,
    run_setup,
)


@pytest.fixture(scope="session")
def agents(base_scaffolding) -> dict:
    run_setup("setup-mcp-demo.py", {"DEMO_PART": "2"})   # register ACME server + tools
    run_setup("setup-agent-demo.py")                     # agent identities + grants + cards
    aenv = demo_agent_env()
    menv = demo_mcp_env()
    if not aenv.get("AGENT_KEY"):
        pytest.skip("AGENT_KEY not written by setup-agent-demo.py")
    return {
        "agent_key": aenv["AGENT_KEY"],
        # a valid NON-agent key (Sales group), the "outside key" for the binding test
        "outside_key": base_scaffolding["SALES_KEY"],
        "server": menv.get("ACME_SRV"),
        "tenant": aenv.get("TENANT", "default"),
        "billing_card": "billing-agent",
    }


# --------------------------------------------------------------------------- #
# Per-agent runtime authorization (MCP tool surface) — the authz trio
# --------------------------------------------------------------------------- #

def test_ungranted_tool_denied(proxy_client, agents, log_probe):
    """The Data Export Worker has no grant for lookup_customer → default-deny 403;
    the audit row records agent_authz_decision='deny'."""
    if not agents["server"]:
        pytest.skip("ACME_SRV unavailable")
    call = mcp_call(proxy_client, agents["agent_key"], agents["server"], "lookup_customer",
                    {"customer_id": "C-4471"})
    assert call.status == 403, call.text
    assert "authoriz" in call.text.lower() or "grant" in call.text.lower(), call.text
    log = log_probe(call.corr)
    assert_log(log, proxy_type="mcp", success=False, agent_authz_decision="deny")


def test_approval_required_tool_parks(proxy_client, agents, log_probe):
    """generate_report is granted approval_required → parks at 202; the audit row
    records agent_authz_decision='approval_required'."""
    if not agents["server"]:
        pytest.skip("ACME_SRV unavailable")
    call = mcp_call(proxy_client, agents["agent_key"], agents["server"], "generate_report",
                    {"quarter": "Q1"})
    assert call.status == 202, call.text
    assert "approval" in call.text.lower(), call.text
    log = log_probe(call.corr)
    assert_log(log, proxy_type="mcp", success=False, agent_authz_decision="approval_required")


def test_granted_tool_allowed(proxy_client, agents, log_probe):
    """query_sales_db is granted allow → runs (200). NOTE: the allow path does NOT
    stamp agent_authz_decision (only deny/approval are stamped), so we assert the
    call carried an agent identity and succeeded, and the decision column is null.
    Provider-dependent on the demo MCP server."""
    if not agents["server"]:
        pytest.skip("ACME_SRV unavailable")
    call = mcp_call(proxy_client, agents["agent_key"], agents["server"], "query_sales_db",
                    {"sql": "SELECT region, quarter, revenue FROM bookings LIMIT 3;"})
    if call.status != 200:
        pytest.skip(f"granted-allow needs the demo MCP server up; got {call.status}: {call.text[:160]}")
    log = log_probe(call.corr)
    assert_log(log, proxy_type="mcp", success=True)
    assert not log.get("agent_authz_decision"), \
        f"allow path should leave agent_authz_decision null, got {log.get('agent_authz_decision')!r}"


# --------------------------------------------------------------------------- #
# A2A — signed cards + caller binding
# --------------------------------------------------------------------------- #

def test_signed_card_discovery(proxy_client, agents):
    """The public agent-card endpoint serves a tenant-signed (Ed25519) card."""
    call = a2a_discover_card(proxy_client, agents["tenant"], agents["billing_card"])
    assert call.status == 200, call.text
    assert "ed25519" in call.text.lower(), call.text


def test_a2a_outside_key_refused(proxy_client, agents, log_probe):
    """The billing-agent card is bound to the AI-System group; a key from another
    group (Sales) can't invoke it — the gateway refuses on the binding check
    before any dispatch."""
    call = a2a_message_send(proxy_client, agents["outside_key"], agents["tenant"],
                            agents["billing_card"], "Refund order #4471 please.")
    assert call.status in (400, 403), f"expected a binding refusal, got {call.status}: {call.text[:200]}"
    assert "not bound" in call.text.lower() or "resource group" in call.text.lower(), call.text
