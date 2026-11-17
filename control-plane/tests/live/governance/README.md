# Live Governance Suite

Governance regression tests driven by the **demo videos**. The demos already
encode every governed outcome (a call that should be blocked / redacted /
quota-capped / parked for approval), so each one becomes a test — and the proof
is the **proxy-log audit row** it leaves, because the audit trail *is* the
product.

This is the layer the old live tests missed: they proved *"can I call a model /
run a skill / call a tool"*. These prove *"is it **governed**, and is it on the
record"*.

## Shape of every test

```
provision (setup scripts = "first half")
  → make the governed call, tagged with a client-set x-correlation-id
    → assert the live HTTP response (status + body)
      → fetch THAT call's audit row by correlation id
        → assert its governance fields (guardrail_blocked / pii_detected /
          argument_policy_denied / agent_authz_decision / compliance_tags /
          skill_id / server_id / resource_group_id / success)
```

Two independent proofs per scenario: the wire response *and* the audit row.

## Layout

| File | What |
|------|------|
| `helpers.py` | provisioning runner (idempotent, retries once), correlation-tagged call wrappers (`skill_execute`/`llm_chat`/`mcp_call`/`mcp_tools_list`/`a2a_*`), `fetch_log`, `assert_log`, per-skill quota reset |
| `conftest.py` | `base_scaffolding` (runs `setup-llm-demo.py`), `log_probe` fixture; inherits the parent live conftest (URLs, admin/trial login, reachability + provider-key guards) |
| `test_skills.py` | Agent Skills via legacy REST execute (Parts 1 & 2) — **9 pass** |
| `test_skills_progressive.py` | Governed **progressive disclosure** over MCP: L1 tools/list → L2 skills__load (SKILL.md + manifest) → L3 skills__run_script / read_resource / render_template, access-scoped + audited |
| `test_llm.py` | LLM (input guardrails: injection/toxic/secrets; RBAC; golden) — **5 pass** |
| `test_mcp.py` | MCP (SQL/SSRF/command arg-policies, tool filter, approval, tool-list scoping) — **8 pass** |
| `test_agents.py` | Agents (authz trio deny/approval/allow, signed-card discovery, A2A caller-binding) — **5 pass** |
| `test_evidence.py` | Evidence series — a refusal seals `denied` (no effect), a golden read seals `executed` with a confirmed effect, a query seals nothing; each record verifies via the CP evidence API and its RFC 6962 inclusion proof is recomputed in Python; skips when the core has no evidence key |
| `test_ui.py` | UI verification via Playwright (console renders the governed row) — **1 pass** |
| `ui_assert_proxy_log.mjs` | Node/Playwright helper: opens Proxy Logs, returns the most-recent rendered row |

**Full suite: 33 passed** against a clean-DB stack (incl. 10 progressive-disclosure tests — L1 verbs-only + catalog-in-instructions, L2/L3 governed, verb-level operator argument policy — and 4 REST-execute-removal tests). Run `pytest tests/live/governance -m live_e2e`.

## Running

Needs a live stack (Postgres + control :5050 + core :8100), the Presidio
analyzer container for PII checks, and `tests/.secrets/provider_keys.env` with
≥1 provider key. Then:

```bash
# whole governance suite
pytest tests/live/governance -m live_e2e -v

# one series
pytest tests/live/governance/test_skills.py -m live_e2e -v
```

`live_e2e` is auto-applied to everything under `tests/live/` and excluded by the
default `pytest` run, so these never run in the unit suite.

### Provisioning order matters

`setup-llm-demo.py` is the **base scaffolding** — it creates the ACME org, the
Sales/Ops resource groups, and the SDK keys everything else binds to. It must
run **before** the per-surface scripts. The fixtures enforce this
(`base_scaffolding` → series fixture). Run the suite against a **clean DB**: the
setup scripts are create-once, so a resource provisioned out of order (e.g. a
guardrail bound to the wrong group before Sales existed) will *stay* wrong on
re-runs.

Keys are read from **`demo-api-keys.env`** (written fresh by `setup-llm-demo`),
never from the per-surface env files — those can carry a stale `SALES_KEY`
copied from an older run.

## Extending to a new series

1. Add a session fixture that provisions it, depending on `base_scaffolding`:
   ```python
   @pytest.fixture(scope="session")
   def mcp(base_scaffolding):
       run_setup("setup-mcp-demo.py")      # + DEMO_PART if the script needs it
       return {...keys + ids from demo-api-keys.env / demo-mcp-env.env...}
   ```
2. One test per demo scenario. Reuse `helpers`:
   - LLM: `POST /v1/proxy/v1/chat/completions` (add an `llm_chat()` wrapper).
   - MCP: `POST /v1/proxy/mcp/{server_id}` JSON-RPC `tools/call` (add `mcp_call()`).
   - Agent/A2A: the A2A message endpoint with a signed card + agent key.
   All accept the same `x-correlation-id` header; then `assert_log(...)` on the row.
3. Assert the surface's governance columns: LLM/MCP use `server_id`/`llm_*`
   fields and `guardrail_blocked` / `argument_policy_denied`; agents use
   `agent_authz_decision` (`allow`/`deny`/`approval_required`) + `resource_group_id`.

## Scenario inventory (from the demos)

- **Skills** *(done)*: golden path (202+audit), access-deny (403, soc2.cc6.6),
  catalog scoping, arg-policy SQL deny, input PII block, output PII redact,
  per-skill quota (202/202/403), approval-required parking.
- **LLM**: clean prompt + cache hit, prompt-injection block, PII output redact,
  banned-word block, secrets block, semantic-policy (LLM-judge) block; token
  budget / RPM / TPM / daily quota → 429; RBAC refuse-by-name; routing-group
  failover (both legs audited); argument policy on a tool call.
- **MCP**: per-team tool filter, destructive-SQL arg-policy deny (even after
  approve), PII redact + secrets mask on tool results, send_email approval
  (one-shot token, replay refused), per-tool rate-limit → 429, SSRF block on
  fetch_url, command-allowlist on the Ops host, semantic-policy block.
- **Agents**: ungranted tool 403, approval-required tool 202-park, granted call
  allow — each with `agent_authz_decision` on the row; A2A signed-card verify,
  per-system egress policy deny, delegation-depth-cap refusal; semantic-policy
  shadow vs enforce.

## UI verification (next)

The audit row is asserted via the API. To also assert **the console renders it**,
reuse the Playwright admin harness in `brutor-video-gen/src/scenes/admin.mjs`:
log into the admin console, open **System Logs → Proxy Logs**, filter by the
call's correlation id, and assert the row's cells (status, the guardrail /
policy chip, the compliance tag). Runs as a thin Node step keyed off the same
correlation ids these tests already mint.

## Per-surface gotchas (learned building this)

- **LLM RPM**: the demo caps Sales at 5 req/min; a module firing several
  governance calls trips it. `test_llm.py` raises the RPM via the admin API for
  the run (an autouse fixture) so guardrail/RBAC assertions aren't masked by a
  429. Throughput limiting is a separate control to test on its own.
- **MCP `DEMO_PART`**: `setup-mcp-demo.py` is posture-scoped. `test_mcp.py`
  provisions `DEMO_PART=2` so the **SQL argument policy** fires on a write — at
  `DEMO_PART=3/all` a banned-words guardrail shadows it (403 with a different
  flag). Same request, different governed outcome by part.
- **MCP tool-filter refusals** are now audited as access denials (soc2.cc6.6);
  `test_tool_filter_refuses_unassigned_tool` asserts the row. (Previously this
  path returned the canonical HTTP error with no proxy-log — fixed in mcp.rs.)
- **Agent allow does NOT stamp `agent_authz_decision`** (only deny/approval do),
  so `test_granted_tool_allowed` asserts the decision column is *null* + the row
  carries an agent identity.
- **MCP demo keys** are the fresh ones `setup-mcp-demo` writes to
  `demo-mcp-env.env` (its own group keys), distinct from the LLM `demo-api-keys.env`.

## Findings surfaced by this suite — and fixed

- **Skill output redaction wasn't audited** → FIXED (skills.rs): a redact now
  stamps `pii_detected` on the 202 success row (which drives the PII compliance
  tag), so `test_output_redaction_is_audited` passes.
- **MCP tool-filter refusals left no audit row** → FIXED (mcp.rs): the disabled
  path now writes an access-denial row (soc2.cc6.6).
- **Skill-guardrail bound to the wrong group when provisioned out of order**
  (config/provisioning, not a code bug): if `setup-skill-demo` runs before
  `setup-llm-demo` created Sales, the skill-PII guardrail binds to *Default* and
  create-once keeps it — Sales skill calls then fail-open on PII. Run on a clean
  DB; the fixtures enforce llm→skill order.
