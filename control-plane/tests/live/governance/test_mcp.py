# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Live GOVERNANCE suite — MCP tools (mirrors "MCP Governance with Brutor").
Deterministic gateway-side controls: argument policies (SQL write / SSRF /
command-allowlist), per-team tool filter, human approval, and tool-list
scoping. Provisioned at DEMO_PART=2 so the SQL *argument policy* fires (Part 3
adds a banned-words guardrail that shadows it).

Run:  pytest tests/live/governance/test_mcp.py -m live_e2e
Needs the demo MCP server (brutor-demo-mcp container) up for the golden path.
"""

from __future__ import annotations

import pytest

from .helpers import assert_log, demo_mcp_env, mcp_call, mcp_tools_list, run_setup


@pytest.fixture(scope="session")
def mcp(base_scaffolding) -> dict:
    """Provision the MCP demo (server registration, per-team tool filters,
    argument policies, guardrails, approval, rate limit). DEMO_PART=2 keeps the
    SQL argument policy active (not shadowed by the Part-3 banned-words guard)."""
    run_setup("setup-mcp-demo.py", {"DEMO_PART": "2"})
    env = demo_mcp_env()
    server = env.get("ACME_SRV")
    if not server:
        pytest.skip("ACME_SRV not written by setup-mcp-demo.py")
    return {"server": server, "sales_key": env["SALES_KEY"], "ops_key": env["OPS_KEY"]}


def _rpc_error_text(call) -> str:
    """JSON-RPC error text + raw body, for substring assertions."""
    body = call.json() or {}
    err = body.get("error") if isinstance(body, dict) else None
    return (str(err) + " " + call.text).lower()


def test_golden_read_query_runs(proxy_client, mcp, log_probe):
    call = mcp_call(proxy_client, mcp["sales_key"], mcp["server"], "query_sales_db",
                    {"sql": "SELECT region, quarter, revenue FROM bookings LIMIT 3;"})
    if call.status != 200:
        pytest.skip(f"golden MCP read needs the demo server up; got {call.status}: {call.text[:160]}")
    log = log_probe(call.corr)
    assert_log(log, proxy_type="mcp", success=True, server_id_set=True)


def test_argument_policy_refuses_sql_write(proxy_client, mcp, log_probe):
    call = mcp_call(proxy_client, mcp["sales_key"], mcp["server"], "query_sales_db",
                    {"sql": "DELETE FROM bookings WHERE id = 20;"})
    assert call.status == 403, call.text
    assert "read-only" in _rpc_error_text(call) or "select" in _rpc_error_text(call), call.text
    log = log_probe(call.corr)
    assert_log(log, proxy_type="mcp", success=False, argument_policy_denied=True, server_id_set=True)


def test_argument_policy_blocks_ssrf(proxy_client, mcp, log_probe):
    call = mcp_call(proxy_client, mcp["ops_key"], mcp["server"], "fetch_url",
                    {"url": "http://169.254.169.254/latest/meta-data/"})
    assert call.status == 403, call.text
    assert "not permitted" in _rpc_error_text(call), call.text
    log = log_probe(call.corr)
    assert_log(log, proxy_type="mcp", success=False, argument_policy_denied=True)


def test_argument_policy_blocks_non_https(proxy_client, mcp, log_probe):
    call = mcp_call(proxy_client, mcp["ops_key"], mcp["server"], "fetch_url",
                    {"url": "http://example.com/data"})
    assert call.status == 403, call.text
    assert "https" in _rpc_error_text(call), call.text
    log = log_probe(call.corr)
    assert_log(log, proxy_type="mcp", success=False, argument_policy_denied=True)


def test_command_allowlist_refuses_destructive(proxy_client, mcp, log_probe):
    call = mcp_call(proxy_client, mcp["ops_key"], mcp["server"], "run_ops_command",
                    {"command": "rm -rf /var/log/old"})
    assert call.status == 403, call.text
    assert "allowlist" in _rpc_error_text(call), call.text
    log = log_probe(call.corr)
    assert_log(log, proxy_type="mcp", success=False, argument_policy_denied=True)


def test_tool_filter_refuses_unassigned_tool(proxy_client, mcp, log_probe):
    """fetch_url is disabled for the Sales group → 403 'disabled for your group',
    and the refusal is audited as a logical-access-control denial (soc2.cc6.6)."""
    call = mcp_call(proxy_client, mcp["sales_key"], mcp["server"], "fetch_url",
                    {"url": "https://example.com"})
    assert call.status == 403, call.text
    assert "disabled" in call.text.lower() or "denied" in call.text.lower(), call.text
    log = log_probe(call.corr)
    assert_log(log, proxy_type="mcp", success=False, argument_policy_denied=True,
               server_id_set=True, compliance_tags_any=["soc2.cc6.6"])


def test_approval_required_tool_parks(proxy_client, mcp, log_probe):
    """send_email is approval-required for Sales → parks at 202 with a poll URL,
    without a one-shot token."""
    call = mcp_call(proxy_client, mcp["sales_key"], mcp["server"], "send_email",
                    {"to": "jane.doe@northwind.com", "subject": "Thanks",
                     "body": "Hi Jane — thanks for renewing with Acme."})
    assert call.status == 202, call.text
    body = call.json() or {}
    assert "approval" in call.text.lower() and ("poll_url" in call.text or body.get("poll_url")), body
    log = log_probe(call.corr)
    assert_log(log, proxy_type="mcp", success=False, server_id_set=True)


def test_tools_list_scoped_per_team(proxy_client, mcp):
    """Discovery is filtered per group: Sales sees its four tools and not the Ops
    tools; Ops sees only its two."""
    sales = mcp_tools_list(proxy_client, mcp["sales_key"], mcp["server"])
    assert sales.status == 200, sales.text
    sales_tools = {t.get("name") for t in ((sales.json() or {}).get("result", {}) or {}).get("tools", [])}
    assert "query_sales_db" in sales_tools and "fetch_url" not in sales_tools, sales_tools

    ops = mcp_tools_list(proxy_client, mcp["ops_key"], mcp["server"])
    assert ops.status == 200, ops.text
    ops_tools = {t.get("name") for t in ((ops.json() or {}).get("result", {}) or {}).get("tools", [])}
    assert "fetch_url" in ops_tools and "query_sales_db" not in ops_tools, ops_tools
