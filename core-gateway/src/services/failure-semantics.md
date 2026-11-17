# Failure semantics of the Core Gateway

**Rule: a control that cannot decide refuses.**

When a lookup a control depends on fails (database error, Redis error, a value
this build does not understand, input an analyzer cannot parse), the request is
refused. It is never treated as "nothing configured", "nothing used" or "nothing
matched". Availability is restored by retrying, never by skipping the control.

Corollaries:

1. **Unreadable ≠ absent.** An error is never folded into `None`, `0`, an empty
   list or `Default`. Where the type system allows it, the API makes that
   impossible (`GuardrailConfigs` has no `Default`; `try_get_effective_group` and
   `run_usage` return `Result`).
2. **Never cache a failure as an answer.** A partial or empty result produced by
   an error is not cached; the next request tries again.
3. **Unknown ≠ allowed.** An access level, predicate or analyzer this build does
   not know is treated as the most restrictive option, so a newer control plane
   cannot open an older proxy.
4. **A refusal is evidence.** Where the surface has an audit writer, the refusal
   writes a row (subtype `engine_failure`, check `*_unavailable`).
5. **Best-effort is allowed only where nothing is enforced.** Attribution,
   caching decisions and telemetry may degrade softly. Enforcement may not depend
   on them: the limits read live counters, not the audit log.

## Controls and their outage behaviour

| Control | Dependency fails | Behaviour |
|---|---|---|
| Guardrail configs (all surfaces: LLM in/out and streams, Anthropic, Gemini, MCP, system server, skills, A2A); tenant-wide API keys resolve to the tenant defaults | DB, cold cache | **503 / block** (`guardrails_unavailable`); warm cache serves last known good |
| Guardrail detector | provider error/timeout | Block, unless the operator set fail-open (recorded as skipped) |
| Guardrail check that is configured and cannot be built (a provider selected with no entry, key or endpoint, or an endpoint the outbound policy refuses; a Bedrock guardrail with no credential or region of its own; a custom webhook with no endpoint; a banned word or pattern entry that cannot be read or compiled; a provider selector that is not one of the check's; a secrets or PII category the detector does not know; an enabled `providers` entry of a provider the gateway does not know) | the policy's own configuration | The check stays in the policy as one that cannot answer and goes down the detector ladder: the local built-in answers where the check has one (`pii`, `prompt_injection`, `jailbreak`, `toxic_content`; recorded as degraded, tier `fallback`), otherwise **block**, unless the operator set fail-open (recorded as skipped). It was dropped with a log line and the call passed unchecked |
| MCP request classification | unrecognised envelope | JSON-RPC batch refused (`-32600`); content type ignored |
| MCP capability ACL | DB (configs, filters, ceiling, chain) | **Deny** |
| MCP capability ACL | unknown access level (a capability-filter row, or a group's or server's `default_*_access`: another string or not a string; null is unset) | **Deny** |
| LLM governance `enforce` (temperature, context window) | a value other than `clamp` / `reject` | **Reject**; the chain merge keeps it over both known values |
| Skill `execution_mode` chain merge | a mode other than the three known ones (or not text) at any level | Kept by the merge over every known mode; the gate refuses on it |
| Autonomy level (agent, group, binding chain) | a level the gateway does not know | **Suspended** |
| Agent grant | an `effect` other than `allow` / `deny` / `approval_required` | **Deny** (the grant is the decision; it was dropped) |
| A duration read from configuration (`services::configured_duration`) | a positive number of seconds too large for a `Duration` or a deadline (an MCP call's `timeout_seconds`, a routing group's `stream_timeout`, a model's `timeout` used as a polling deadline) | Read as the ceiling, 365 days; never a panic |
| MCP tools/list filter | DB | Group contributes no tools |
| Resource-group ancestry (limits, policies, A2A governance) | DB mid-walk | **Load fails; nothing cached**; enforcement callers refuse |
| User group membership | DB | No groups for this request; **not cached** |
| A2A routes (outbound, inbound, v1) | caller group load | **403 `engine_failure`** |
| A2A v1 quota | group load | **Refuse** |
| A2A policy (group chain, tenant roots, agent card; every A2A route) | the tenant's root policy cannot be read; a document or a part of one cannot be read; an access value the gateway does not know; an allow-list with no readable pattern | **403** (`engine_failure` when the load failed); an unreadable or unknown value refuses as the strictest one |
| A2A accepted callers | service group load / edge lookup | **Refuse** (`Unavailable`) |
| Run caps | Redis | **Refuse** (`run_usage_unavailable`) |
| Run caps | parallel steps | **Atomic reservation**; settle at commit; release on refusal |
| Run caps | caps lookup (DB) | Last known caps, else **refuse**; errors never cached |
| Run action ceiling (`max_actions_per_run`) | Redis, caps lookup | **Refuse** (`run_usage_unavailable`); a step a later gate refuses gives its unit back |
| New runs per credential (`max_new_runs_per_window`) | Redis, caps lookup | **Refuse**: `run_usage_unavailable` on an action surface, `limits_unavailable` on a model call |
| Tenant-wide / ancestor LLM budgets, skill quotas, MCP ancestor budgets | parallel calls | **Atomic reservation** (Redis) |
| Tenant limits / ancestor walk | DB | **Refuse** (`budget_unavailable`); a failed hop is no longer cached as "no ancestor" |
| Argument policy | cache load | **Deny** |
| Argument policy | unknown analyzer / predicate (of an enabled policy that targets the call) | **Deny**; an unknown predicate is *undecidable*, a violation under `deny_if` and under `allow_only_if` |
| Argument policy | unparseable SQL / shell / URL | Every predicate is **undecidable**: a violation under `deny_if` and under `allow_only_if` |
| Argument policy | a compared field that is present and not comparable with the operand (`null`, an array, an object, `"20,000"`, a quoted number under `strict_numbers`, another type) | **Undecidable → violation** in both directions. A boolean is compared with a boolean operand. An absent field keeps its semantics (the presence guard refuses it) |
| Argument policy | arguments beyond the policy's analysis limits (`AnalysisLimits`: 512 leaves, 8 levels, 64 array elements unless the policy's `analyzer_config` states `max_leaves`, `max_depth`, `max_array_elements`) when a JSON policy targets the call. Each policy analyses under its own limits: the call has to fit every policy that targets it | **Deny** (`arguments_truncated`), whatever the rules' severities |
| Argument policy | a stored policy whose `rules` this build cannot decode | The load fails → **Deny** (`policy_unavailable`) for the group on that surface. A *disabled* policy loads with no rules and refuses nothing |
| Argument policy | an enabled JSON policy whose `analyzer_config` states an analysis limit that is not an integer from 1 to its hard ceiling (`AnalysisLimits::CEILING`: 4,096 leaves, 32 levels, 1,024 array elements) | The load fails → **Deny** (`policy_unavailable`). Never clamped to the nearest acceptable limit |
| Argument policy | a policy-level `severity` this build does not know | Enforced as **deny** (`deny`, `approval_required` and `warn` are read as stored; a rule that states none takes the policy's) |
| Argument policy | a compared field that more than one data part of an A2A message gives | **Undecidable → violation** at the rule's severity. A presence guard is satisfied: the field is there |
| MCP server static token | `mcp_servers.auth_token` of a server whose `auth_type` is `bearer` is not a ciphertext the gateway can open: it does not decrypt (another `ENCRYPTION_KEY`, an envelope the gateway cannot unwrap) or it is not ciphertext at all | **502**, nothing is sent upstream: neither the stored value as the bearer nor the call without one (MCP proxy, Portal MCP-app resource). The token is read for a `bearer` server only: for any other `auth_type` (`none`, `oauth`, absent, unknown) the stored value is not read and not sent, and the call goes out without it |
| Stored model credential (`llm_model.api_key`, `provider_config`) and guardrail provider secret (`api_key`, a Bedrock entry's `aws_secret_access_key` / `aws_session_token`) | the stored value is not a ciphertext the gateway can open: it does not decrypt, or it is not ciphertext at all | **Never used.** The model row is marked unreadable and every dispatch to it is refused (`credential_unreadable`); the guardrail entry is marked and its check is not built (it goes down the failure ladder of a check that cannot be built). A row is opened once and carries that fact (`credentials_opened`); nothing decides "stored or opened" from the shape of a value |
| Skill execution (REST, `skills__run_script`, A2A) | the caller's group lookup fails | **503 `engine_failure`** |
| Per-tenant evidence key | the tenant's active seed does not decrypt or match its kid; the key lookup fails | **Nothing is sealed** for the tenant (seal API 503 `tenant_key_unavailable`; writer rows land without a record and are backfilled); not cached. Was: sealed under the deployment key with a warning. Rotation hands over under the deployment key |
| Stored OAuth credential | a refresh in flight while the credential is revoked, or while its revoked row is taken by another IdP's subject | The refresh result is **discarded** and no token served; a stale refresh revokes only the row it read (same IdP, still live) |
| Run abort lookup | DB (map and Redis had nothing) | **Refuse** (`ABORT_UNKNOWN_MESSAGE`) |
| Every usage limit (quotas, budgets, run / session / control caps, frequency, throughput, routing RPM/TPM, in-flight slots, MCP / A2A / skill / media / grant counts) | Redis | **Refuse** (`limits_unavailable`); no per-pod fallback |
| Every usage limit | parallel calls, any replica | **One atomic admission** of every dimension; worst case reserved; output bounded upstream |
| Proxy startup | `REDIS_URL` unset, or Redis unreachable | **Refuses to start** |
| Proxy startup, the environment (`env_registry`, `config::load`) | a retired or removed variable name is set; a required variable is missing; a value is not of its variable's type, is out of range or is not an accepted value | **Refuses to start**, one line per variable, never a value. Was: an unparsable number or an unknown value read as the default, a malformed signing key as "no key" |
| Proxy startup, Community Edition (`services::edition_guard`) | the edition marker or any of the 42 Enterprise-data checks cannot be decided: table missing, statement fails or is cancelled after 30 s, answer is NULL or no count | **Refuses to start**, listing what was not checked by kind and error class (never the error text). Finding the marker or Enterprise data is the same refusal. No retry, no override. The Enterprise Edition does not check |
| Tenant layer, what a path requires (`services::tenant_paths`) | a path nobody registered (an unknown prefix, an extension route that registered nothing, a portal path on a Community assembly) | **The most restrictive rule**: a tenant is required (401), an unknown tenant refused (403), a suspended tenant blocked (403). Was: "no tenant required" for every path outside three hard-coded lists |
| Limit configuration (group chain, assignment rows, tenant-wide limits, routing caps, control attachments, session caps, media ledger) | DB | **Refuse**; errors never cached |
| A call's admission hold | replica dies / client hangs up after a 2xx | Lease lapses → **kept as spent** (worst case), slots freed by any replica's reaper |
| A call's admission hold | refused by a later gate / provider never answered 2xx | **Released** in full |
| Tool-call limits (`max_tool_calls_per_hour/day`) | Redis | **Refuse** |
| Governance configuration (LLM / media / skill; unparseable LLM policy) | DB | **Refuse** (503 `governance_unavailable`) |
| LLM quota counters after a Redis blip | settle lost | Reconcile raises every counter to ≥ the ledger (atomic, never lowers) |
| Concurrency slots | Redis acquire | 429 (`limits_unavailable` on a Redis error) |
| Tenant status / admin session / portal revocation | DB | 503 / refuse |
| Media quota, session budget | proxy_logs under backpressure | Live counters; only the cold-start seed reads the ledger, and its rows are no longer dropped (spooled, replayed) |
| proxy_logs (audit) write | channel full; DB not taking writes; row refused | Overflow → durable Redis spool, replayed through the pipeline; a stamped batch spooled whole and landed with its chain sequence numbers (chain intact); a refused row declared `write_failed`; loss only past every bound (`audit_lost`, logged) |
| Portal end-user lookup | DB | **503** |
| Portal token shape | no / empty `jti`; `typ` not `portal` | **401** |
| Workload identity | IdP has no `workload_audiences`; token `aud` missing or not listed; symmetric JWK; unlisted or substituted algorithm | Token does not authenticate (401) |
| Outbound destination (tenant URLs) | IP-literal host the policy denies, first hop or any redirect hop | Refused before any request: LLM **403**; detector not built (block); IdP / MCP / search / media calls error |
| Outbound redirect (credentialed clients) | redirect to another origin | Refused; the key never leaves (public URL fetches use a separate uncredentialed client) |
| OAuth configuration lookups | DB | **503**; token exchange refuses rather than storing a token without its gateway user |
| Config cache freshness | pub/sub or NOTIFY disconnect; invalidation during a load | Flush all config caches on every (re)subscribe; a load older than the last invalidation is not cached |
| Ambiguous upstream outcome | timeout or 502/504/520/524 after sending | Not retried (LLM inline and same-model retries; non-idempotent MCP); recorded as `custom_metadata.ambiguous_upstream` |
| Approval token | presented by another principal | Refused, token not consumed |
| usage_logs write | channel full; DB down; row refused | Bounded overflow to durable Redis spool, replayed; refused rows dead-lettered; loss only if overflow also full (counted) |
| Any database read on an enforcement, evidence or request path | DB | **Propagated**: 503 (`db_errors::unavailable`) or the caller's refusal; never read as "no row" / empty / 0, never cached. Background sweeps skip the tick |
| Database read that only feeds display, metadata or a self-healing sweep (37 sites) | DB | **Degraded**, logged with its call site and counted (`brutor_db_errors_degraded_total{site}`); each site states why |
| Quota counter repair / backfill (ledger SUM or COUNT) | DB | **Skip the scope** for the pass, write nothing. The request path still refuses that one request |

## Deliberate defaults that are not failure behaviour

These are product rules, not outage handling, and are recorded so nobody
mistakes them for fail-open bugs:

- **A group with no MCP server configurations can reach every MCP server in
  its tenant** (`usage.rs`, "unconfigured = open"). This is the product rule, not
  an outage behaviour.
- **Multi-group access is allow-wins** across a user's groups; limits and
  policies inherited from ancestors are restrictive (min-wins).
- **Session identity (`clientInfo`) is best-effort attribution:** a store
  timeout reads as absent and is counted. It never grants access.
