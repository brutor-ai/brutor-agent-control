# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Shared helpers for the live GOVERNANCE suite.

The demo videos are the spec: each governed scenario (a call that should be
blocked / redacted / quota-capped / parked for approval) is a test, and the
proof is the **proxy-log audit row** it leaves. These helpers do three things:

  1. `provision(...)`   — run the demo setup scripts (the "first half"): base
                          scaffolding (setup-llm-demo) + the per-surface script.
  2. `<surface>_call`   — make the governed call, tagging it with a client-set
                          `x-correlation-id` so we can find its exact log row.
  3. `fetch_log` /      — pull that row back from the control plane's proxy-logs
     `assert_log`         query API and assert its governance fields.

Configuration is env-driven (same convention as the parent live conftest) so
the suite can target a staging deployment unchanged.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import httpx

# repo layout: .../brutor-gateway-control/tests/live/governance/helpers.py
_HERE = Path(__file__).resolve()
DEMO_DIR = Path(
    os.environ.get("BRUTOR_DEMO_DIR", _HERE.parents[4] / "brutor-demo-mcp")
)
PROVIDER_KEYS_FILE = Path(
    os.environ.get(
        "BRUTOR_PROVIDER_KEYS",
        _HERE.parents[2] / ".secrets" / "provider_keys.env",
    )
)

# The core reads the caller's correlation id from this header (constants.rs
# `X_CORRELATION_ID`). Setting it ourselves lets us fetch the *exact* audit row
# for a call instead of guessing "the latest".
CORR_HEADER = "x-correlation-id"

# The control plane's proxy-logs query endpoint (POST) and the field names on a
# returned log row that we assert against.
PROXY_LOG_QUERY = "/v1/admin/proxy-logs/query"


def new_corr() -> str:
    return str(uuid.uuid4())


# --------------------------------------------------------------------------- #
# Provisioning — run the demo setup scripts ("first half")
# --------------------------------------------------------------------------- #

def _env_file(name: str) -> Dict[str, str]:
    """Parse a KEY=VALUE / `export KEY="value"` env file into a dict."""
    out: Dict[str, str] = {}
    p = DEMO_DIR / name
    if not p.exists():
        return out
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _subprocess_env() -> Dict[str, str]:
    """Base env for a setup script: inherit ours + fold in provider keys so the
    LLM scaffolding can add models against real providers."""
    env = dict(os.environ)
    env.update(_env_file_abs(PROVIDER_KEYS_FILE))
    env.setdefault("CP_URL", os.environ.get("BRUTOR_CONTROL_URL", "http://localhost:5050"))
    env.setdefault("GW_URL", os.environ.get("BRUTOR_PROXY_URL", "http://localhost:8100"))
    env.setdefault("TENANT_ID", os.environ.get("BRUTOR_TENANT_ID", "default"))
    env.setdefault("ADMIN_USER", os.environ.get("BRUTOR_ADMIN_USERNAME", "admin"))
    env.setdefault("ADMIN_PASS", os.environ["BRUTOR_ADMIN_PASSWORD"])
    return env


def _env_file_abs(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def run_setup(script: str, extra_env: Optional[Dict[str, str]] = None) -> None:
    """Run a demo setup script under DEMO_DIR. Setup scripts are idempotent
    ("create-once") so re-runs are safe. Raises with captured output on failure
    so a provisioning break is a loud, actionable test error."""
    path = DEMO_DIR / script
    if not path.exists():
        raise FileNotFoundError(f"setup script not found: {path}")
    env = _subprocess_env()
    if extra_env:
        env.update(extra_env)
    timeout = int(os.environ.get("BRUTOR_SETUP_TIMEOUT", "300"))
    last = None
    # Setup scripts are idempotent; a transient failure (a race between the base
    # scaffolding and a per-surface script, a brief cache-invalidation window) is
    # cleared by a second run, so retry once before giving up loudly.
    for attempt in range(2):
        last = subprocess.run(
            [sys.executable, str(path)], cwd=str(DEMO_DIR), env=env,
            capture_output=True, text=True, timeout=timeout,
        )
        if last.returncode == 0:
            _scope_policies_after(script)
            return
    raise RuntimeError(
        f"provisioning script {script} failed twice (exit {last.returncode}):\n"
        f"--- stdout ---\n{last.stdout[-2000:]}\n--- stderr ---\n{last.stderr[-2000:]}"
    )


def _scope_policies_after(script: str) -> None:
    """The demo scripts can create tenant-wide policies (no group binding —
    e.g. setup-llm-demo's 'No individualized financial/legal/medical advice',
    setup-agent-demo's 'Flag competitor disparagement') or bind them to
    Default. Right after each script, move those onto E2ETests via the admin
    API and fail if any enabled suite policy is still tenant-wide / on
    Default. Policies a script binds to a demo group (Sales/Ops/...) for its
    own scenario stay there."""
    from ..conftest import ADMIN_PASSWORD, ADMIN_USERNAME, CONTROL_URL, DEFAULT_TENANT_ID
    from ..conftest import enforce_policy_scoping

    with httpx.Client(base_url=CONTROL_URL, timeout=30.0) as c:
        r = c.post("/v1/admin/users/tenant/login", json={
            "username": ADMIN_USERNAME, "password": ADMIN_PASSWORD,
            "tenant_id": DEFAULT_TENANT_ID,
        })
        r.raise_for_status()
        headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
        changed = enforce_policy_scoping(c, headers)
    for line in changed:
        print(f"[policy-scope after {script}] {line}")


def demo_env() -> Dict[str, str]:
    """The skill demo's recorder env (group ids + skill ids). Keys here can be
    stale (see `demo_keys`), but the *ids* are freshly written each run."""
    return _env_file("demo-skill-env.env")


def demo_mcp_env() -> Dict[str, str]:
    """The MCP demo's recorder env — ACME_SRV (server id) + freshly-minted
    MCP SALES_KEY / OPS_KEY (setup-mcp-demo mints its own group keys)."""
    return _env_file("demo-mcp-env.env")


def demo_agent_env() -> Dict[str, str]:
    """The agent demo's recorder env — AGENT_KEY (the Data Export Worker's
    credential), FINANCE_AGENT_ID / BILLING_AGENT_ID cards, AI_RG, plus SALES_KEY
    (a non-agent key, for the 'outside key refused' scene)."""
    return _env_file("demo-agent-env.env")


def reset_skill_daily_usage(group_id: str, skill_id: str) -> bool:
    """Clear today's per-skill execution counter so a quota scenario starts from
    a known slate — exactly what the demo does before its quota scene.

    There is no HTTP API for this (usage is a ledger, not a settable counter),
    so we DELETE today's usage_logs rows via the local Postgres. Returns False
    when the DB isn't reachable this way (e.g. a remote staging target), so the
    caller can `pytest.skip` a quota test it can't set up deterministically.
    """
    container = os.environ.get("BRUTOR_PG_CONTAINER", "brutor-postgres")
    sql = (
        "DELETE FROM usage_logs WHERE group_id='{g}' AND resource_type='skill' "
        "AND resource_id='{s}' AND created_at >= date_trunc('day', now() at time zone 'utc');"
    ).format(g=group_id, s=skill_id)
    try:
        proc = subprocess.run(
            ["docker", "exec", container, "psql", "-U",
             os.environ.get("BRUTOR_PG_USER", "brutor"), "-d",
             os.environ.get("BRUTOR_PG_DB", "ai_gateway"), "-tAc", sql],
            capture_output=True, text=True, timeout=15,
        )
        return proc.returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


def demo_keys() -> Dict[str, str]:
    """The authoritative fresh SDK keys the base scaffolding wrote.

    `demo-api-keys.env` (written by setup-llm-demo) is the source of truth — the
    per-surface scripts' env files can carry a *stale* SALES_KEY/OPS_KEY copied
    from an older run, so we never read those for keys."""
    keys = _env_file("demo-api-keys.env")
    missing = [k for k in ("SALES_KEY", "OPS_KEY") if not keys.get(k)]
    if missing:
        raise RuntimeError(
            f"demo-api-keys.env missing {missing} after provisioning — "
            f"did setup-llm-demo.py run? ({DEMO_DIR / 'demo-api-keys.env'})"
        )
    return keys


# --------------------------------------------------------------------------- #
# Governed calls (correlation-tagged)
# --------------------------------------------------------------------------- #

def _auth_headers(key: str, corr: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        CORR_HEADER: corr,
    }


def skill_execute(
    proxy: httpx.Client,
    key: str,
    skill_name: str,
    input_params: Dict[str, Any],
    corr: Optional[str] = None,
) -> "Call":
    """POST /v1/proxy/skills/execute with a correlation-tagged auth header."""
    corr = corr or new_corr()
    r = proxy.post(
        "/v1/proxy/skills/execute",
        headers=_auth_headers(key, corr),
        json={"skill_name": skill_name, "input_params": input_params},
    )
    return Call(r, corr)


def skill_catalog(proxy: httpx.Client, key: str, corr: Optional[str] = None) -> "Call":
    corr = corr or new_corr()
    r = proxy.get("/v1/proxy/skills/catalog", headers=_auth_headers(key, corr))
    return Call(r, corr)


def llm_chat(
    proxy: httpx.Client, key: str, *, messages, model: Optional[str] = None,
    model_id: Optional[str] = None, corr: Optional[str] = None, **extra,
) -> "Call":
    """POST /v1/proxy/llm/chat/completions (OpenAI shape). Target a concrete
    model via `model` or a routing group via `model_id="rg-<group>"`."""
    corr = corr or new_corr()
    body: Dict[str, Any] = {"messages": messages, **extra}
    if model:
        body["model"] = model
    if model_id:
        body["model_id"] = model_id
    r = proxy.post("/v1/proxy/llm/chat/completions", headers=_auth_headers(key, corr), json=body)
    return Call(r, corr)


def mcp_call(
    proxy: httpx.Client, key: str, server_id: str, tool: str,
    arguments: Dict[str, Any], corr: Optional[str] = None, rpc_id: str = "t",
    extra_headers: Optional[Dict[str, str]] = None,
) -> "Call":
    """POST /v1/proxy/mcp/{server_id} — JSON-RPC tools/call."""
    corr = corr or new_corr()
    headers = _auth_headers(key, corr)
    if extra_headers:
        headers.update(extra_headers)
    r = proxy.post(
        f"/v1/proxy/mcp/{server_id}",
        headers=headers,
        json={"jsonrpc": "2.0", "id": rpc_id, "method": "tools/call",
              "params": {"name": tool, "arguments": arguments}},
    )
    return Call(r, corr)


def mcp_tools_list(proxy: httpx.Client, key: str, server_id: str, corr: Optional[str] = None) -> "Call":
    corr = corr or new_corr()
    r = proxy.post(
        f"/v1/proxy/mcp/{server_id}",
        headers=_auth_headers(key, corr),
        json={"jsonrpc": "2.0", "id": "list", "method": "tools/list", "params": {}},
    )
    return Call(r, corr)


def mcp_rpc(
    proxy: httpx.Client, key: str, server_id: str, method: str,
    params: Optional[Dict[str, Any]] = None, corr: Optional[str] = None, rpc_id: str = "r",
) -> "Call":
    """Raw JSON-RPC call to an MCP server (e.g. `initialize`)."""
    corr = corr or new_corr()
    r = proxy.post(
        f"/v1/proxy/mcp/{server_id}",
        headers=_auth_headers(key, corr),
        json={"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params or {}},
    )
    return Call(r, corr)


def a2a_message_send(
    proxy: httpx.Client, key: str, tenant: str, card: str, text: str,
    corr: Optional[str] = None,
) -> "Call":
    """POST /a2a/{tenant}/{card}/message:send — A2A JSON-RPC-ish message."""
    corr = corr or new_corr()
    headers = _auth_headers(key, corr)
    headers["X-Tenant-ID"] = tenant
    r = proxy.post(
        f"/a2a/{tenant}/{card}/message:send",
        headers=headers,
        json={"message": {"messageId": "gov-" + corr[:8], "role": "ROLE_USER",
                          "parts": [{"text": text}]}},
    )
    return Call(r, corr)


def a2a_discover_card(proxy: httpx.Client, tenant: str, card: str) -> "Call":
    """GET /.well-known/agent-card/{tenant}/{card} — public signed-card fetch."""
    corr = new_corr()
    r = proxy.get(f"/.well-known/agent-card/{tenant}/{card}")
    return Call(r, corr)


class Call:
    """A governed call's HTTP response + its correlation id."""

    def __init__(self, response: httpx.Response, corr: str):
        self.response = response
        self.corr = corr

    @property
    def status(self) -> int:
        return self.response.status_code

    def json(self) -> Any:
        try:
            return self.response.json()
        except Exception:
            return None

    @property
    def text(self) -> str:
        return self.response.text or ""


# --------------------------------------------------------------------------- #
# Proxy-log fetch + assertions ("check the proxy log after each call")
# --------------------------------------------------------------------------- #

def _logs_of(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, dict):
        return payload.get("logs") or payload.get("items") or []
    if isinstance(payload, list):
        return payload
    return []


def fetch_log(
    control: httpx.Client,
    admin_headers: Dict[str, str],
    corr: str,
    *,
    timeout: float = 20.0,
    predicate=None,
) -> Dict[str, Any]:
    """Poll the proxy-logs query API for the row with this correlation id.

    Proxy logs are written by a batched background writer, so a fresh row can
    lag the HTTP response by up to a second or two — hence the poll. `predicate`
    lets a caller wait for a *specific* row when a call emits more than one."""
    deadline = time.monotonic() + timeout
    body = {"correlation_id": corr, "limit": 25}
    last: List[Dict[str, Any]] = []
    while time.monotonic() < deadline:
        r = control.post(PROXY_LOG_QUERY, headers=admin_headers, json=body)
        if r.status_code == 200:
            last = _logs_of(r.json())
            if predicate is not None:
                match = next((l for l in last if predicate(l)), None)
                if match:
                    return match
            elif last:
                return last[0]
        time.sleep(0.4)
    raise AssertionError(
        f"no proxy_log for correlation_id={corr} within {timeout}s "
        f"(saw {len(last)} row(s); predicate={'set' if predicate else 'none'})"
    )


def fetch_all_logs(
    control: httpx.Client,
    admin_headers: Dict[str, str],
    corr: str,
    *,
    settle: float = 2.0,
    timeout: float = 20.0,
) -> List[Dict[str, Any]]:
    """Return ALL proxy-log rows for a correlation id, after a settle window.

    Used by the anti-double-logging regression: a skill call over MCP is ONE
    operation and must produce exactly ONE row. The batched background writer
    can lag, so we wait until the first row appears, then keep polling for
    `settle` more seconds — long enough that a stray SECOND row (the old
    outer-mcp + inner-skill double) would have shown up — and return the final
    set.
    """
    deadline = time.monotonic() + timeout
    seen: List[Dict[str, Any]] = []
    first_at = None
    while time.monotonic() < deadline:
        r = control.post(
            PROXY_LOG_QUERY, headers=admin_headers,
            json={"correlation_id": corr, "limit": 25},
        )
        if r.status_code == 200:
            seen = _logs_of(r.json())
            if seen:
                if first_at is None:
                    first_at = time.monotonic()
                elif time.monotonic() - first_at >= settle:
                    return seen
        time.sleep(0.3)
    if seen:
        return seen
    raise AssertionError(f"no proxy_log rows for correlation_id={corr} within {timeout}s")


def assert_log(
    log: Dict[str, Any],
    *,
    proxy_type: Optional[str] = None,
    success: Optional[bool] = None,
    guardrail_blocked: Optional[bool] = None,
    pii_detected: Optional[bool] = None,
    argument_policy_denied: Optional[bool] = None,
    agent_authz_decision: Optional[str] = None,
    skill_id_set: Optional[bool] = None,
    server_id_set: Optional[bool] = None,
    resource_group_set: Optional[bool] = None,
    compliance_tags_any: Optional[Iterable[str]] = None,
    compliance_tags_all: Optional[Iterable[str]] = None,
    error_contains: Optional[str] = None,
) -> None:
    """Assert governance fields on a fetched proxy-log row.

    Every check is opt-in (None = don't care). HTTP status is asserted on the
    live response, not here — the query API leaves `http_status_code` null on
    some refusal paths, whereas the governance *flags* are the reliable record.
    """
    def field(name: str):
        return log.get(name)

    ctx = f"(proxy_type={field('proxy_type')} corr={field('correlation_id')})"

    if proxy_type is not None:
        assert field("proxy_type") == proxy_type, \
            f"proxy_type: want {proxy_type!r}, got {field('proxy_type')!r} {ctx}"
    if success is not None:
        assert bool(field("success")) is success, \
            f"success: want {success}, got {field('success')!r} {ctx}"
    if guardrail_blocked is not None:
        assert bool(field("guardrail_blocked")) is guardrail_blocked, \
            f"guardrail_blocked: want {guardrail_blocked}, got {field('guardrail_blocked')!r} {ctx}"
    if pii_detected is not None:
        assert bool(field("pii_detected")) is pii_detected, \
            f"pii_detected: want {pii_detected}, got {field('pii_detected')!r} {ctx}"
    if argument_policy_denied is not None:
        assert bool(field("argument_policy_denied")) is argument_policy_denied, \
            f"argument_policy_denied: want {argument_policy_denied}, got {field('argument_policy_denied')!r} {ctx}"
    if agent_authz_decision is not None:
        assert field("agent_authz_decision") == agent_authz_decision, \
            f"agent_authz_decision: want {agent_authz_decision!r}, got {field('agent_authz_decision')!r} {ctx}"
    if skill_id_set is not None:
        got = bool(field("skill_id"))
        assert got is skill_id_set, f"skill_id set: want {skill_id_set}, got {field('skill_id')!r} {ctx}"
    if server_id_set is not None:
        assert bool(field("server_id")) is server_id_set, \
            f"server_id set: want {server_id_set}, got {field('server_id')!r} {ctx}"
    if resource_group_set is not None:
        assert bool(field("resource_group_id")) is resource_group_set, \
            f"resource_group_id set: want {resource_group_set}, got {field('resource_group_id')!r} {ctx}"
    if compliance_tags_any is not None:
        tags = set(field("compliance_tags") or [])
        want = set(compliance_tags_any)
        assert tags & want, f"compliance_tags: want any of {sorted(want)}, got {sorted(tags)} {ctx}"
    if compliance_tags_all is not None:
        tags = set(field("compliance_tags") or [])
        want = set(compliance_tags_all)
        assert want <= tags, f"compliance_tags: want all of {sorted(want)}, got {sorted(tags)} {ctx}"
    if error_contains is not None:
        err = (field("error_message") or "") + " " + str(field("response_body_preview") or "")
        assert error_contains.lower() in err.lower(), \
            f"error text: want substring {error_contains!r}, got {err[:160]!r} {ctx}"
