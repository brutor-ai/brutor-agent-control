# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Live GOVERNANCE suite — Governed Agent Skills *progressive disclosure*
over MCP (the L1→L3 flow that replaces one-shot REST execute).

Where `test_skills.py` drove the legacy REST `/skills/execute`, this drives the
real Agent-Skill way: the model **discovers** skills (L1 tools/list), **loads**
SKILL.md + manifest (L2 skills__load), then **acts** (L3 skills__run_script /
read_resource / render_template) — every layer access-scoped, governed, and on
the record.

Provisioning: setup-llm-demo.py (base) + setup-skill-demo.py.
Run:  pytest tests/live/governance/test_skills_progressive.py -m live_e2e
"""

from __future__ import annotations

import json
import time

import pytest

from .helpers import (
    assert_log,
    demo_env,
    demo_keys,
    mcp_call,
    mcp_rpc,
    mcp_tools_list,
    run_setup,
)


@pytest.fixture(scope="session")
def agent_skill_server(default_tenant_id) -> str:
    """The Agent Skills system MCP server id is tenant-suffixed
    (app/constants/system_server.py SERVER_ID + '-' + tenant)."""
    return f"system-agent-skill-server-{default_tenant_id}"


@pytest.fixture(scope="session")
def skills(base_scaffolding) -> dict:
    run_setup("setup-skill-demo.py")
    env = demo_env()
    keys = demo_keys()
    return {
        "sales_key": keys["SALES_KEY"],
        "ops_key": keys["OPS_KEY"],
        "sales_group": env.get("SALES_GROUP"),
        "revenue_report_id": env.get("REVENUE_REPORT_ID"),
    }


# --------------------------------------------------------------------------- #
# Response helpers
# --------------------------------------------------------------------------- #

def _content_texts(call) -> list[str]:
    body = call.json() or {}
    result = body.get("result") or {}
    return [b.get("text", "") for b in result.get("content", []) if b.get("type") == "text"]


def _refused(call) -> bool:
    """A call is 'refused' if it did not yield a successful tool result — either
    an HTTP error, a JSON-RPC error envelope (server-level denial), or a tool
    result flagged isError (per-skill / per-arg governance)."""
    if call.status != 200:
        return True
    body = call.json() or {}
    if body.get("error"):
        return True
    return bool((body.get("result") or {}).get("isError"))


def _tool_names(call) -> list[str]:
    return [t.get("name") for t in (call.json() or {}).get("result", {}).get("tools", [])]


def _load_manifest(proxy, key, server, skill_name, corr=None):
    call = mcp_call(proxy, key, server, "skills__load", {"skill_name": skill_name}, corr=corr)
    manifest = None
    for t in _content_texts(call):
        try:
            parsed = json.loads(t)
            if isinstance(parsed, dict) and "manifest" in parsed:
                manifest = parsed["manifest"]
                break
        except Exception:
            continue
    return call, manifest


# --------------------------------------------------------------------------- #
# L1 — discovery
# --------------------------------------------------------------------------- #

def test_l1_tools_list_is_verbs_only(proxy_client, skills, agent_skill_server):
    """Option B: skills are NOT tools. tools/list is EXACTLY the five skills__*
    verbs (incl. skills__list for discovery) — no per-skill skill__<name>
    entries, no one-shot execute."""
    call = mcp_tools_list(proxy_client, skills["sales_key"], agent_skill_server)
    assert call.status == 200, call.text
    names = _tool_names(call)
    assert set(names) == {
        "skills__list", "skills__load", "skills__read_resource",
        "skills__run_script", "skills__render_template",
    }, names
    # No `skill__<name>` (singular = a specific skill) entries — skills aren't tools.
    assert not any(n and n.startswith("skill__") for n in names), names


def test_l1_skills_list_returns_scoped_catalog(proxy_client, skills, agent_skill_server):
    """skills__list is the client-independent discovery path: a TOOL (always in
    tools/list) that returns the caller's access-scoped catalog — so the model
    can discover skills even on clients that drop initialize.instructions."""
    call = mcp_call(proxy_client, skills["sales_key"], agent_skill_server, "skills__list", {})
    assert not _refused(call), call.text
    names = set()
    for t in _content_texts(call):
        try:
            for s in (json.loads(t).get("skills") or []):
                names.add(s.get("name"))
        except Exception:
            continue
    assert "revenue-report" in names, names   # Sales-assigned skill is discoverable
    # Ops-only scoping: a caller with no skills sees an empty catalog.
    ops = mcp_call(proxy_client, skills["ops_key"], agent_skill_server, "skills__list", {})
    ops_names = set()
    for t in _content_texts(ops):
        try:
            for s in (json.loads(t).get("skills") or []):
                ops_names.add(s.get("name"))
        except Exception:
            continue
    assert "revenue-report" not in ops_names, ops_names  # Ops isn't assigned it


def test_l1_initialize_instructions_carry_scoped_catalog(proxy_client, skills, agent_skill_server):
    """Option B L1 discovery: the access-scoped skill catalog is delivered in the
    initialize `instructions` field (Anthropic-style), not as tools. Sales sees
    revenue-report there; the preamble points at skills__load."""
    call = mcp_rpc(
        proxy_client, skills["sales_key"], agent_skill_server, "initialize",
        {"protocolVersion": "2024-11-05", "capabilities": {},
         "clientInfo": {"name": "gov-test", "version": "1"}},
    )
    assert call.status == 200, call.text
    instructions = ((call.json() or {}).get("result") or {}).get("instructions", "")
    assert "skills__load" in instructions, instructions[:400]
    assert "revenue-report" in instructions, instructions[:600]


# --------------------------------------------------------------------------- #
# L2 — load
# --------------------------------------------------------------------------- #

def test_l2_load_returns_skillmd_preamble_and_manifest(
    proxy_client, skills, agent_skill_server, log_probe
):
    """skills__load returns the injected L3 protocol preamble + SKILL.md + a
    manifest of runnable scripts/resources/templates, and writes a load audit
    row carrying the skill id."""
    call, manifest = _load_manifest(proxy_client, skills["sales_key"], agent_skill_server, "revenue-report")
    assert not _refused(call), call.text
    joined = "\n".join(_content_texts(call))
    assert "SKILL.md" in joined
    assert "skills__run_script" in joined  # the injected protocol preamble
    assert manifest is not None, f"no manifest block: {call.text}"
    assert set(manifest.keys()) == {"resources", "scripts", "templates"}

    # A skill call over MCP writes ONE row (the mcp/system_server row) with the
    # skill_load verdict folded on — skill_id and all. (Single-row invariant is
    # asserted by test_proxy_log_skill_load_is_one_full_row.)
    row = log_probe(call.corr, predicate=lambda l: bool(l.get("skill_id")))
    assert_log(row, skill_id_set=True)


def test_l2_load_access_denied_for_unassigned_skill(proxy_client, skills, agent_skill_server):
    """A skill the caller's group is NOT allowed cannot be loaded — refused
    rather than leaking SKILL.md."""
    call = mcp_call(
        proxy_client, skills["ops_key"], agent_skill_server, "skills__load",
        {"skill_name": "revenue-report"},
    )
    assert _refused(call), call.text


# --------------------------------------------------------------------------- #
# L3 — act (governed)
# --------------------------------------------------------------------------- #

def test_l3_run_script_is_governed_and_attributed(
    proxy_client, skills, agent_skill_server, log_probe
):
    """L3 skills__run_script runs a manifest script in the sandbox and returns
    only its output; the audit row is a skill run carrying the skill id."""
    _, manifest = _load_manifest(proxy_client, skills["sales_key"], agent_skill_server, "revenue-report")
    assert manifest and manifest.get("scripts"), "revenue-report should expose a script"
    script = manifest["scripts"][0]["name"]

    call = mcp_call(
        proxy_client, skills["sales_key"], agent_skill_server, "skills__run_script",
        {"skill_name": "revenue-report", "script": script, "args": {}},
    )
    assert call.status == 200, call.text
    assert not _refused(call), call.text
    assert _content_texts(call), call.text  # output returned, script source never

    # The single folded row (skill_id set) — attribution for Mission Control.
    row = log_probe(call.corr, predicate=lambda l: bool(l.get("skill_id")))
    assert_log(row, skill_id_set=True)


def test_l3_run_script_unknown_script_is_refused(proxy_client, skills, agent_skill_server):
    """A script name not in the manifest is refused (allowlist) — the model
    cannot name an arbitrary or disabled script (traversal attempt here)."""
    call = mcp_call(
        proxy_client, skills["sales_key"], agent_skill_server, "skills__run_script",
        {"skill_name": "revenue-report", "script": "../../etc/passwd", "args": {}},
    )
    assert _refused(call), call.text


def test_l3_read_resource_unknown_path_is_refused(proxy_client, skills, agent_skill_server):
    """skills__read_resource only resolves files in the skill's manifest."""
    call = mcp_call(
        proxy_client, skills["sales_key"], agent_skill_server, "skills__read_resource",
        {"skill_name": "revenue-report", "path": "does-not-exist.md"},
    )
    assert _refused(call), call.text


# --------------------------------------------------------------------------- #
# L3 — governance chain on run_script args (the "re-point" of test_skills.py's
# argument-policy + guardrail scenarios onto the progressive MCP path)
# --------------------------------------------------------------------------- #

def _first_script(proxy, key, server, skill_name):
    _, manifest = _load_manifest(proxy, key, server, skill_name)
    if not manifest or not manifest.get("scripts"):
        return None
    return manifest["scripts"][0]["name"]


def test_l3_run_script_argument_policy_refuses_destructive_sql(
    proxy_client, skills, agent_skill_server, log_probe
):
    """revenue-report is read-only: a destructive SQL arg is parsed and refused
    by the argument policy BEFORE the sandbox runs — same governance as the REST
    path, now on run_script args. Audited as an argument-policy denial."""
    script = _first_script(proxy_client, skills["sales_key"], agent_skill_server, "revenue-report")
    assert script, "revenue-report should expose a script"
    call = mcp_call(
        proxy_client, skills["sales_key"], agent_skill_server, "skills__run_script",
        {"skill_name": "revenue-report", "script": script,
         "args": {"query_sql": "UPDATE sales SET amount = 0 WHERE region = 'EMEA'"}},
    )
    assert _refused(call), call.text
    row = log_probe(call.corr, predicate=lambda l: bool(l.get("skill_id")))
    assert_log(row, argument_policy_denied=True, skill_id_set=True)


def test_verb_level_argument_policy_governs_run_script(
    control_client, admin_headers, proxy_client, skills, agent_skill_server, log_probe
):
    """Operator governance: an McpInput argument policy attached to the
    `skills__run_script` VERB is enforced at the system-server tool layer (before
    the L3 handler) — so operators can govern the verbs like any MCP tool, on top
    of the per-skill governance. Targets a destructive flag the happy-path never
    uses, so it coexists with the other tests."""
    # Attach a shell-analyzer deny to the run_script verb's `script` argument.
    control_client.post(
        "/v1/admin/argument-policies",
        headers=admin_headers,
        json={
            "name": "gov-verb-run-script-flags",
            "description": "Deny destructive flags on a skills__run_script call (verb-level).",
            "surface": "mcp_input", "target": "skills__run_script", "argument_key": "script",
            "analyzer": "shell", "severity": "deny", "group_ids": [skills["sales_group"]],
            "rules": [{
                "deny_if": "flag_present", "values": ["-rf", "--force", "-f"],
                "message": "Destructive flags are not permitted in run_script.",
            }],
        },
    )  # tolerant: a prior run may have created it (shared-DB re-runs)

    # The argument-policy cache is TTL-evicting (~60s), so poll until the verb
    # policy takes effect. The verb-level deny is an HTTP 403 (returned from the
    # system-server governance layer BEFORE the L3 handler); a plain L3 refusal
    # (e.g. the manifest allowlist) is an HTTP 200 with isError — so gate on 403
    # to be sure we're seeing the argument policy, not the allowlist.
    deadline = time.monotonic() + 80
    call = None
    while time.monotonic() < deadline:
        call = mcp_call(
            proxy_client, skills["sales_key"], agent_skill_server, "skills__run_script",
            {"skill_name": "revenue-report", "script": "run.py --force", "args": {}},
        )
        if call.status == 403:
            break
        time.sleep(5)
    assert call is not None and call.status == 403, (
        f"verb-level McpInput argument policy never enforced (403) on skills__run_script: "
        f"status={call.status if call else None} body={call.text if call else None}"
    )
    assert "argument" in call.text.lower() or "flag" in call.text.lower(), call.text
    row = log_probe(call.corr, predicate=lambda l: bool(l.get("argument_policy_denied")))
    assert_log(row, argument_policy_denied=True)


def test_l3_run_script_input_guardrail_blocks_pii(
    proxy_client, skills, agent_skill_server, log_probe
):
    """PII in run_script args is blocked before the sandbox runs — the L3 input
    guardrail, audited guardrail-blocked + PII-detected."""
    script = _first_script(proxy_client, skills["sales_key"], agent_skill_server, "customer-followup")
    if not script:
        pytest.skip("customer-followup exposes no script in this demo build")
    call = mcp_call(
        proxy_client, skills["sales_key"], agent_skill_server, "skills__run_script",
        {"skill_name": "customer-followup", "script": script,
         "args": {"note": "follow up with jane.doe@northwind.example about renewal"}},
    )
    assert _refused(call), call.text
    row = log_probe(call.corr, predicate=lambda l: bool(l.get("skill_id")))
    assert_log(row, guardrail_blocked=True, pii_detected=True, skill_id_set=True)


# --------------------------------------------------------------------------- #
# Proxy-log invariants — ONE row per skill call, never double, never thin.
#
# Regression guards for the "double logging + missing data" defect: a skill
# call over MCP is ONE operation and must produce exactly ONE audit row (the
# mcp/system_server row), carrying the FULL request/response body, the
# capability, the skill id, and the REAL governance verdict (403/202, not the
# transport 200). Before the fix each act/load wrote a second, thinner `skill`
# row shadowing the full mcp row.
# --------------------------------------------------------------------------- #

def _sole_row(logs_probe, corr):
    rows = logs_probe(corr)
    assert len(rows) == 1, (
        f"expected exactly ONE proxy_log row for a skill call, got {len(rows)}: "
        + ", ".join(f"{r.get('proxy_type')}/{r.get('proxy_subtype')}" for r in rows)
    )
    return rows[0]


def test_proxy_log_skill_run_is_one_full_row(
    proxy_client, skills, agent_skill_server, logs_probe
):
    """skills__run_script → exactly one row with args + output + capability + skill_id."""
    script = _first_script(proxy_client, skills["sales_key"], agent_skill_server, "revenue-report")
    call = mcp_call(
        proxy_client, skills["sales_key"], agent_skill_server, "skills__run_script",
        {"skill_name": "revenue-report", "script": script,
         "args": {"query_sql": "SELECT region, SUM(amount) FROM sales GROUP BY region"}},
    )
    assert not _refused(call), call.text
    row = _sole_row(logs_probe, call.corr)
    assert row.get("skill_id"), "skill_id missing on the single folded row"
    assert (row.get("mcp") or {}).get("capability_name", "").endswith("skills__run_script")
    req = (row.get("request") or {}).get("body_preview") or ""
    resp = (row.get("response") or {}).get("body_preview") or ""
    assert "SELECT" in req, f"request body missing the args: {req!r}"
    assert resp, "response body preview missing"


def test_proxy_log_skill_load_is_one_full_row(
    proxy_client, skills, agent_skill_server, logs_probe
):
    """skills__load → exactly one row carrying the SKILL.md response body."""
    call, _ = _load_manifest(proxy_client, skills["sales_key"], agent_skill_server, "revenue-report")
    assert not _refused(call), call.text
    row = _sole_row(logs_probe, call.corr)
    assert row.get("skill_id")
    assert (row.get("response") or {}).get("body_preview"), "SKILL.md response body missing"


def test_proxy_log_skill_denial_is_one_complete_row(
    proxy_client, skills, agent_skill_server, logs_probe
):
    """A governed denial is ONE row with the 403 verdict, the blocked request
    body, the error message, and the SOC2 policy-deny tag — not a thin 403 skill
    row shadowed by a 200 mcp row."""
    script = _first_script(proxy_client, skills["sales_key"], agent_skill_server, "revenue-report")
    call = mcp_call(
        proxy_client, skills["sales_key"], agent_skill_server, "skills__run_script",
        {"skill_name": "revenue-report", "script": script,
         "args": {"query_sql": "UPDATE sales SET amount = 0"}},
    )
    assert _refused(call), call.text
    row = _sole_row(logs_probe, call.corr)
    assert (row.get("response") or {}).get("status_code") == 403, \
        f"want 403 verdict on the row, got {(row.get('response') or {}).get('status_code')}"
    assert_log(row, argument_policy_denied=True, skill_id_set=True,
               compliance_tags_any=["soc2.cc6.6"])
    req = (row.get("request") or {}).get("body_preview") or ""
    assert "UPDATE" in req, f"blocked request body missing: {req!r}"
    assert row.get("error_message"), "denial error_message missing"
