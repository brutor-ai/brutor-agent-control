# Live end-to-end tests

This suite drives a **running** Brutor stack — Postgres + control plane
(:5050) + Rust proxy (:8100) — over HTTP. It catches the kind of break
unit tests can't: a route that no longer matches, an auth header the
proxy stopped honouring, a seeded skill that doesn't show up in the
trial user's catalog.

It is opt-in: nothing under `tests/live/` runs in the default `pytest`
invocation. CI doesn't pay for it; only developers running the live
stack do.

## Prereqs

1. Stack up:

   ```bash
   cd brutor-gateway-control && docker compose up -d      # db, redis, qdrant, ollama, mcp-app-map
   cd brutor-gateway-control && python main.py            # :5050
   cd brutor-gateway-core    && cargo run                 # :8100
   ```

2. At least one provider key in
   `brutor-gateway-control/tests/.secrets/provider_keys.env`:

   ```env
   OPENAI_API_KEY=sk-...
   ANTHROPIC_API_KEY=sk-ant-...
   FAL_KEY=...                  # optional — needed for the video gen test
   ```

   Per-provider env vars (e.g. `OPENAI_API_KEY=...` exported in your
   shell) are also picked up. The file is gitignored.

   The **multimodal tier** (Tier 5, see below) requires:
   - `OPENAI_API_KEY` for `gpt-image-2.6-flare`, `tts-1`, and `gpt-transcribe`
   - `FAL_KEY` for the `luma/agent/ray/v3.2/text-to-video` video-generation test

   Each multimodal test skips cleanly when its provider key isn't
   configured, so a partial setup still produces a green run on the
   tiers it CAN cover.

3. Default credentials (override with env vars):

   | Var | Default |
   | --- | --- |
   | `BRUTOR_CONTROL_URL` | `http://localhost:5050` |
   | `BRUTOR_PROXY_URL` | `http://localhost:8100` |
   | `BRUTOR_TENANT_ID` | `default` |
   | `BRUTOR_ADMIN_USERNAME` | `admin` (the **tenant admin** of the default tenant, not the system admin) |
   | `BRUTOR_ADMIN_PASSWORD` | the installation's administrator password (required; there is no default) |
   | `BRUTOR_TRIAL_USERNAME` | `trial` |
   | `BRUTOR_TRIAL_PASSWORD` | `Trial123!` |

## Run it

```bash
pytest -m live_e2e tests/live/ -v
```

The whole suite skips if the stack isn't reachable or no provider keys
are configured — so you get a clean message rather than a wall of
connection errors.

## Setup the suite does itself (every run, idempotent)

A fresh stack with only the seed needs **no manual step**. A session-scoped
autouse fixture in `conftest.py` (`_e2e_setup` → `e2e_environment`) runs
before any test — including `governance/` — and:

1. **Creates the `E2ETests` resource group** if missing (re-activates it if
   an operator disabled it). Override the name with `BRUTOR_E2E_GROUP`.
2. **Configures the E2E models** (`E2E_MODELS` in `conftest.py`). The LLM
   catalog and the configured models are separate: a catalog entry is only
   a configured model (`llm_model` row, Admin UI → AI Models →
   Configuration) once imported. For every provider with a key, each listed
   model is imported with `POST /v1/admin/llm-catalog/{catalog_id}/import`
   (the Admin UI's Browse → Add flow) with its API key, `enabled=true` and
   `group_ids=[E2ETests]`. A model that is already configured is reused:
   its key is re-set, it is enabled, and E2ETests is **added** to its groups.
   The Default group is never added.
3. **Ensures the `trial` end user** exists, logs in with
   `BRUTOR_TRIAL_PASSWORD` (default `Trial123!`) and is a member of E2ETests
   (first group joined becomes primary). There is no admin endpoint to reset
   an end user's password, so a `trial` user whose password drifted is
   deleted and recreated through the admin end-user API — never via SQL.
4. **Binds the suite's generic dependencies to E2ETests** (additive — the
   Default bindings are left in place): the seeded `hello-world` skill, the
   Agent Skills system MCP server config, the tenant's default guardrail
   policy, and the `Default` prompt-cache policy.
5. **Creates one shared API key bound to E2ETests** (`e2e_api_key`). Its
   cleartext is only returned once, so it is kept in the gitignored
   `tests/.secrets/e2e_api_key.env` and reused while the proxy accepts it.
   Since v0.11.22 the proxy refuses a key with no resource group (403
   "API key is not bound to a resource group") or whose group is inactive.

Per-test fixtures add the rest on demand, also bound to E2ETests:
the **AWS Knowledge Base** and **Cloudflare Docs** MCP servers are installed
from the MCP catalog if missing (`POST /v1/admin/mcp-catalog/{id}/import`),
the **Brutor Docs Aggregation** VMCP + its server config are created, and
the seeded `hello-world-a2a` agent card is bound to E2ETests.

Portal calls send `X-Resource-Group: <E2ETests id>` (`trial_headers`), so
runtime governance, usage and logs for the suite land on that one group.

### Gateway sweep — the whole catalog through the proxy

`test_e2e_gateway_sweep.py` parametrizes at collection time from the live
LLM catalog (`GET /v1/admin/llm-catalog?provider_key=…`): every entry of a
keyed provider whose mode is chat, responses, embedding, image, TTS or STT,
plus the one fal video model (`E2E_VIDEO_MODEL`). An entry is excluded only
when the catalog itself marks it unusable: `deprecated: true` or a
`retire_date` on/before today (`conftest.sweep_entry_excluded`). The session
setup imports each one through the catalog import endpoint, keys, enables and
binds it to E2ETests. Each model is one line, `test_gateway_sweep[provider/model]`,
with one minimal call (text via the E2ETests API key; image/audio/video via the
trial user's JWT, because those are portal routes that refuse API keys):
PASS on a well-formed 2xx; XFAIL with the provider's error when the provider
refuses (model retired / not available to this key, region or tier, rate-limited,
overloaded); FAIL for anything gateway-side (Brutor refusal, Brutor 5xx, a
provider 400 about a parameter the gateway sent, malformed success, timeout).
`E2E_MODELS` stays as the small representative set for the focused tests.

### Out-of-credit keys fail loudly

`conftest.check_out_of_credit` matches each provider's real billing errors
(OpenAI `insufficient_quota`, Anthropic "credit balance is too low", DeepSeek
402 "Insufficient Balance", xAI "used all available credits", Moonshot
`exceeded_current_quota_error`, MiniMax status 1008, fal "Exhausted balance",
Gemini "prepayment credits are depleted", Groq `organization_restricted`; any
HTTP 402). A hit FAILS the test with an `OUT OF CREDIT` reason, is listed in a
"PROVIDER KEYS OUT OF CREDIT" terminal summary, and forces a non-zero exit.

### Tool calling only where supported

Tests that surface tools (hello-world skill, AWS KB MCP, VMCP) use the
configured model's `supports_function_calling` (the `llm_model` column, shown
by the portal as `capabilities.supports_function_calling`). Missing or false
→ the model is skipped with "model does not support tool calling"; its plain
chat calls still run.

### No tenant-wide policies

Any enabled, non-seeded policy (semantic policies, guardrail configs, argument
policies, semantic-cache configs, prompt-cache policies) that is tenant-wide
(no group) or bound to Default is moved onto E2ETests via the admin API — at
the end of session setup and right after every demo provisioning script
(`governance/helpers.run_setup`). Policies a demo script binds to its own demo
group (Sales/Ops/acme-…) stay there. The tenant's seeded `*-default-<tenant>`
rows are left alone. Setup then fails if any enabled suite policy is still
tenant-wide or on Default.

### One group — E2ETests

Everything the suite provisions is associated with **E2ETests**, not Default,
so a run never widens what Default's members can reach and its footprint is
visible in one place (Admin UI → Resource Groups → E2ETests). The
`governance/` suite additionally runs the demo provisioning scripts, which
create their own ACME / Sales / Ops groups and keys — those tests keep using
them, because the governed outcomes are defined by the demo's policies.

### E2E_MODELS

One dict in `conftest.py`, keyed by provider slug. Every name must exist in
that provider's catalog file under `app/database/provider_data/`
(`test_e2e_models_exist_in_catalog` fails loudly when one leaves). Chat
tests sweep only these models; the multimodal tier uses `gpt-image-2.6-flare`,
`tts-1`, `gpt-transcribe` and `luma/agent/ray/v3.2/text-to-video`. When a
provider retires one, replace it in the dict with the cheapest current entry
of the same catalog file.

## What's covered

1. Admin login → token round-trip; E2ETests group resolves
2. For every provider with a key:
   - Every E2E_MODELS entry is still in the catalog, configured (imported
     from the catalog), enabled and bound to E2ETests
   - Per-model health check via `POST /v1/admin/llms/{id}/health-check`.
     Probes run with the platform key and are billed to the `Unbound`
     system group. The suite tolerates per-model `unhealthy` (a model the
     provider no longer serves) and only fails if an *entire provider* has
     zero healthy models with a key-shaped error (401/403).
3. Trial user login → token round-trip
4. Chat completion with prompt "Hello World!" against each E2E chat model —
   the trial user's `hello-world` skill is surfaced as a tool, and the
   assertion checks that the LLM emitted a `tool_calls` entry naming it.

5. **External MCP tool invocation** (`test_e2e_aws_kb_mcp.py`):
   For each chat-capable model the trial user can reach, drive a
   single tool call against the **AWS Knowledge Base** MCP server
   (auth-free; catalog-installable). Asserts the model emits a
   `aws___search_documentation` tool_call, the proxy routes the
   `tools/call` to the upstream, and a non-empty text result comes
   back. Complements the `hello-world` test — that one round-trips
   inside the gateway, this one validates the full external-MCP
   path including HTTP egress, capability snapshot lookup, and
   guardrail wrap.

   The *AWS Knowledge Base* catalog entry is installed (if missing)
   and bound to E2ETests by the fixture.

6. **VMCP aggregation** (`test_e2e_vmcp_aggregation.py`):
   Idempotently creates a Brutor Virtual MCP named *Brutor Docs
   Aggregation* (or reuses an existing one) that fronts ONE tool
   each from AWS Knowledge Base and Cloudflare Docs, with the
   exposed names `brutor_aws_doc_search` and
   `brutor_cloudflare_doc_search`. For each chat-capable model,
   drives TWO prompts (one AWS, one Cloudflare) and asserts the
   model picks the right exposed tool each time. Exercises the
   `/v1/proxy/vmcp/{id}` route, the `reverse_name_mapping`
   rewrite, and the cross-upstream dispatch.

   Both *AWS Knowledge Base* and *Cloudflare Docs* are installed from
   the MCP catalog (if missing) and the VMCP is created, all bound to
   E2ETests, by the fixtures.

7. **Multimodal endpoints** — one end-to-end smoke per non-text mode,
   exercising the dedicated proxy routes (NOT `/v1/proxy/llm/chat/completions`):

   | Route | Model | What it asserts |
   |---|---|---|
   | `POST /v1/proxy/llm/images/generations` | OpenAI `gpt-image-2.6-flare` | 200 + `data[0]` carries `b64_json` / `url` / `brutor_file_id` |
   | `POST /v1/proxy/llm/audio/speech` | OpenAI `tts-1` | 200 + non-trivial MP3 bytes (ID3 or sync-frame magic) |
   | `POST /v1/proxy/llm/audio/transcriptions` | OpenAI `gpt-transcribe` | 200 + `text` field on the response (empty string is fine for silent input) |
   | `POST /v1/proxy/llm/video/generations` then `GET /v1/proxy/llm/video/jobs/{id}` | Fal `luma/agent/ray/v3.2/text-to-video` | submit returns a job_id, poll until `succeeded`/`failed`/`cancelled` (≤180s), succeeded jobs carry an output reference |

   Every route runs the **same governance pipeline** as the chat path
   (auth, quota, group access, input guardrails, resilience gate,
   dispatch, output guardrails where applicable, proxy_log, usage_record,
   breaker signal). A regression in any of those steps fails this tier.

8. **Prompt-cache effectiveness** (`test_e2e_prompt_caching.py`):
   Two-turn chat per provider with a stable ~5KB system prompt
   (~140KB for Gemini's 32K-token minimum). Asserts the
   provider-specific cache counters in the response usage block
   are non-zero on turn 2, proving the layered cache pipeline
   (planner → adapter marker emission → provider → response
   normalisation → counter extraction) is intact end-to-end.

   | Provider  | Counter asserted on turn 2                  |
   |-----------|----------------------------------------------|
   | OpenAI    | `usage.prompt_tokens_details.cached_tokens`  |
   | Anthropic | `usage.cache_read_input_tokens`              |
   | Gemini    | `usageMetadata.cachedContentTokenCount`      |

   Includes a **negative control**: a provider with `mechanism = None`
   (Groq, Deepseek, Moonshot — whichever is keyed) MUST NOT emit
   any cache counters. Catches normaliser regressions that fabricate
   counters from absent upstream fields.

   Needs the seeded `Default` PromptCachePolicy (`enabled=true,
   strategy=ToolsAndSystem, min_request_tokens <= 1024`); the session
   setup binds it to E2ETests. Without it the planner short-circuits with
   `disabled_or_no_mechanism` and every arm sees zero counters.

   Per-provider arms skip cleanly when the provider's API key isn't
   present in `tests/.secrets/provider_keys.env`.

## Notes

- The suite logs in as the **tenant admin** of the default tenant
  (`admin`) — not as the system admin. That avoids the
  system-admin scope blindness on tenant-scoped routes (no `tenant_id`
  query param needed) and matches how a real operator interacts with
  the admin API.
- The chat-with-skill assertion sends the trial JWT in the
  `X-Gateway-Authorization` header (alongside the standard
  `Authorization` header), which is what the proxy's chat preflight
  reads to populate the user identity. Without it the preflight treats
  the request as anonymous and returns 403 even when the user has
  group access.

## governance/ prerequisites

`governance/` also needs two containers on the stack's `brutor-network`
that the trial bundle does not start by default:

```bash
# Presidio analyzer (PII) — trial-bundle compose profile
docker compose --profile presidio up -d presidio-analyzer
# Demo MCP server the MCP / agent / evidence scenarios call
cd brutor-demo-mcp && docker build -t brutor-demo-mcp . && \
  docker run -d --name brutor-demo-mcp --network brutor-network \
    -p 127.0.0.1:3013:3013 brutor-demo-mcp
```

Run with `PRESIDIO_URL=http://brutor-presidio-analyzer:3000` exported so the
demo setup scripts wire Presidio (without it they fall back to the built-in
regex detector). `test_ui.py` needs Node ≥ 20 (`BRUTOR_NODE`) and
`brutor-video-gen` with its Playwright install as a sibling checkout.
