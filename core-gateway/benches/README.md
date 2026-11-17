# Benchmarks and test suites

Developer reference for what this repository measures and tests, and the commands that run it.
The repository holds two cargo workspaces: the root (the Community gateway, with `crates/*` as
members) and `enterprise/` (separate workspace, own `Cargo.lock`; the root excludes it). Every
command below is run once per workspace, from that workspace's directory.

The toolchain is pinned in `rust-toolchain.toml` (Rust 1.97.1). Builds use `--locked`.

## What exists

| Kind | Where | Needs | Runs in CI |
|---|---|---|---|
| Criterion micro-benchmarks | `benches/*.rs`, `enterprise/benches/*.rs` | nothing (no network, no database) | compiled only, root workspace only |
| Hot-path latency measurement | `tests/integration/llm_hot_path_latency.rs` (`#[ignore]`d) | throwaway Postgres and Redis | no |
| Live-provider benchmark binary | `src/bin/benchmark.rs` (bin `benchmark`) | provider API keys, optionally a running gateway | no |
| Test suite | in-source unit tests, `tests/*.rs`, `tests/integration/` (and the same under `enterprise/`) | throwaway Postgres and Redis | yes, both workspaces |

## Criterion micro-benchmarks

Criterion 0.5 with `html_reports`; each bench is a `[[bench]]` entry with `harness = false`.

### Root workspace (`Cargo.toml`)

| Bench | Criterion group(s) | What it measures |
|---|---|---|
| `router_state` | `router_state` | `RouterState`: `is_cooled_down_miss`, `is_cooled_down_hit`, `increment_rpm`, `add_tpm`, `check_rpm`, `record_latency`, `get_avg_latency`, `begin_end_request`, and `cleanup` at 100 and 1,000 entries |
| `routing_strategies` | `routing_strategies` | `simple_shuffle`, `usage_based`, `least_busy`, `latency_based`, `cost_based`, each at pool sizes 3, 10 and 50 |
| `adapter_transform` | `adapter_transform` | `transform_request` for openai, anthropic, gemini, mistral, xai, cohere, bedrock and ollama (`transform_<provider>`); `normalize_anthropic_response` and `normalize_anthropic_tool_response` |
| `stream_normalizer` | `stream_normalizer` | `AnthropicStreamNormalizer`: `anthropic_message_start`, `anthropic_text_delta_short`, `anthropic_text_delta_long`, `anthropic_tool_call_delta`, `anthropic_message_delta`, `anthropic_message_stop`, `anthropic_full_stream_50_tokens` |
| `anthropic_adapter` | `anthropic_client_detection`, `anthropic_input_extraction`, `anthropic_beta_filter`, `anthropic_backend_transform`, `anthropic_eventstream`, `anthropic_full_pipeline` | The Anthropic Messages adapter: client detection, header extraction and preprocessing; input text extraction (small request, 20 turns); beta filter (allow-all, strip-all, allowlist); request transform and URL construction for Anthropic direct, Bedrock and Vertex; EventStream frame parsing (single frame, 50 frames, partial frame); and `pipeline_anthropic`, `pipeline_bedrock`, `pipeline_vertex_ai`, which chain detect, extract headers, preprocess, transform, filter beta headers, extract input text |
| `jsonrpc_body` | none (ids `tools_call_300B/*`, `tools_call_8KB/*`) | The MCP proxy's one reading of a request body (`jsonrpc_body::read`, `after_one_read`) beside three plain `serde_json` parses of the same `tools/call` (`before_three_parses`), at two body sizes |
| `argument_analysis` | `argument_analysis` | What one JSON argument policy costs per call: `analyze/<leaves>` (the analysis alone) and `analyze_and_evaluate/<leaves>` (with a guard and a two-step band) on a flat argument of 16 to `AnalysisLimits::CEILING.max_leaves` leaves; `shape/default_limits` and `shape/every_ceiling` for arguments that also reach the depth and array-width limits. The hard ceilings of the per-policy analysis limits are chosen from it |

### Enterprise workspace (`enterprise/Cargo.toml`)

| Bench | Criterion group(s) | What it measures |
|---|---|---|
| `extension_hooks` | none (ids `response_cache/*`, `prompt_cache/*`, `context_enricher/*`, `credential_verifier/*`, `tenant_paths/*`) | The cost of the extension hooks' Community defaults, the credential-verifier registry and the tenant path registry. The module documentation at the top of `enterprise/benches/extension_hooks.rs` describes each id |
| `dispatch_store` | `router_state` | `cache_hit` and `cache_miss` of the exact-match response cache's store. They moved here from the root `router_state` bench and keep its group name and ids |

### Running

```bash
cargo bench                                          # every bench of the workspace
cargo bench --bench router_state                     # one bench
cargo bench --bench router_state -- --quick          # Criterion's quick mode
cargo bench --bench routing_strategies -- usage_based   # ids matching a filter
cargo bench -- --save-baseline main                  # save a named Criterion baseline
cargo bench -- --baseline main                       # compare against it
```

Criterion prints its estimate and the change against the previous run, and writes HTML reports
under the workspace's `target/criterion/` (`report/index.html`).

No figures are recorded in this repository. A figure from another machine, or from the same
machine under another load, is not comparable: compare two revisions on one machine in one
sitting.

### Build gate

```bash
cargo bench --no-run --locked
```

Compiles every bench without running it. CI runs it in the root workspace as the last step of
the `Tests` job (`.github/workflows/ci.yml`, step "Compile benchmarks"), also when the tests
failed. The `enterprise` CI job does not build the Enterprise benches. No CI job runs a bench or
compares timings.

## Hot-path latency measurement

What it is for: the response cache, the prompt-cache policy, the context enricher and the
credential verifier are extension hooks with no-op defaults. "A no-op costs nothing" is a claim
about the request path, so it is measured on the request path.

It is a measurement, not a gate: the tests are `#[ignore]`d, nothing in CI runs them, and they
assert nothing about speed (a threshold on a developer machine fails for reasons that are not
the code).

### What is measured

`tests/integration/llm_hot_path_latency.rs`: one chat completion at a time through the
assembled gateway (`GatewayBuilder::assemble`, every middleware layer), with a real Postgres and
a real Redis behind it and a wiremock upstream that answers at once. The installation is the
Community shape: one tenant, one resource group, one model bound to it, one API key; no response
cache, no prompt-cache policy, no knowledge base. What is left is the gateway's own time per
request, including its database and Redis round trips. The requests are sequential: this is
latency per request, not throughput under load.

`tests/integration/mcp_hot_path_latency.rs` is the same for MCP: a `tools/call` to an external
server (`proxy`), a `tools/call` on the Agent Skills system server (`system`) and a `tools/list`
to the external server (`list`); its module documentation says what each one passes through.

### How to run it

A throwaway Postgres and Redis, never a database that holds data
([`tests/integration/README.md`](../tests/integration/README.md) shows how to start them):

```bash
export BRUTOR_BENCH_LABEL=<revision or step> BRUTOR_BENCH_OUT=/path/to/results.jsonl
cargo nextest run --locked --features db-tests --test integration \
  --run-ignored only -E 'test(llm_hot_path_latency)' --no-capture
```

Each run prints one JSON line (p50, p90, p99, mean, min, max in microseconds; requests per
second) and appends it to `BRUTOR_BENCH_OUT`. `BRUTOR_BENCH_WARMUP` (default 200) and
`BRUTOR_BENCH_REQUESTS` (default 2,000) change the sizes; the protocol uses the defaults.
`BRUTOR_BENCH_ENVELOPE` (a JSON object, both tests) declares the group's operating envelope, to
measure the path with the run ceilings and the limit on new runs armed (for example
`{"max_actions_per_run": 1000000, "max_new_runs_per_window": 1000000}`: every measured request
is a run of its own, so neither may be reached); the protocol does not use it.
`BRUTOR_BENCH_MCP_LIMITS` (a JSON object, the MCP test) is written to the group's
`mcp_global_limits`, to measure the MCP path with limits armed: for example
`{"execution": {"max_concurrency_per_group": 1000000, "max_concurrency_per_user": 1000000}}`
holds the concurrency slots of every `tools/call` (the held path) without ever refusing one. The
protocol does not use it; the held path's cost is reported beside the comparison, not against
its thresholds.
`BRUTOR_BENCH_EDITION=1` assembles the extensions of the edition running the suite on the
measured gateway (none in the Community workspace; the Enterprise distribution's in
`enterprise/`, with no policy bound); the protocol does not use it, it exists to explain a
difference.

### Protocol for comparing two revisions

1. Same machine, same containers, same build profile (the test profile, as above), nothing else
   heavy running: no other suite, no build.
2. Five runs per revision (2,000 requests after 200 warm-up each).
3. Compare the **median of the five p50 values** and the **median of the five p99 values**.
4. A change to the request path is accepted when p50 is within ±3 % and p99 within ±10 % of the
   revision before it. Outside that, the difference is explained in the change's description
   before it is called done (a faster result is explained too).
5. The baseline is re-taken whenever the machine, the container images, the toolchain or the
   benchmark itself changes.

The `criterion` bench `extension_hooks` (above) times the no-op calls themselves: the direct
evidence that a default costs nanoseconds, where this measurement shows that the request as a
whole did not move.

## Live-provider benchmark binary

`src/bin/benchmark.rs` sends streaming requests to the OpenAI and Anthropic APIs directly and,
when a gateway URL is given, through the gateway, and reports the difference. It makes real,
billed API calls. Run it by hand only; it is not part of CI. Root workspace only.

```bash
BENCHMARK_OPENAI_KEY=... \
BENCHMARK_ANTHROPIC_KEY=... \
BENCHMARK_PROXY_URL=http://localhost:8100 \
BENCHMARK_PROXY_API_KEY=... \
cargo run --release --locked --bin benchmark
```

| Variable | Default | Meaning |
|---|---|---|
| `BENCHMARK_OPENAI_KEY` | none | OpenAI API key. At least one of the two keys is required |
| `BENCHMARK_ANTHROPIC_KEY` | none | Anthropic API key |
| `BENCHMARK_PROXY_URL` | none | Gateway base URL. Unset: no gateway targets |
| `BENCHMARK_PROXY_API_KEY` | none | Gateway API key. With a URL and no key, the gateway refuses every proxied call |
| `BENCHMARK_OPENAI_MODEL` | `gpt-6-luna` | Model sent to OpenAI |
| `BENCHMARK_ANTHROPIC_MODEL` | `claude-haiku-5-5` | Model sent to Anthropic |
| `BENCHMARK_PROXY_OPENAI_MODEL` | the OpenAI model | The name the gateway knows that model by, when it differs |
| `BENCHMARK_PROXY_ANTHROPIC_MODEL` | the Anthropic model | Likewise |
| `BENCHMARK_CONCURRENCY` | `1,5,10` | Comma-separated concurrency levels |
| `BENCHMARK_REQUESTS` | `10` | Requests per concurrency level |
| `BENCHMARK_OUTPUT_DIR` | `./benchmark-results` | Output directory |

Targets, built from what is set:

| Target | Built when | URL | Body | Auth header |
|---|---|---|---|---|
| `openai-direct` | OpenAI key | `https://api.openai.com/v1/chat/completions` | OpenAI | `Authorization: Bearer` |
| `anthropic-direct` | Anthropic key | `https://api.anthropic.com/v1/messages` | Anthropic | `x-api-key` |
| `proxy-openai` | gateway URL and OpenAI key | `{proxy}/v1/proxy/llm/chat/completions` | OpenAI | `Authorization: Bearer` (gateway key) |
| `proxy-anthropic` | gateway URL and Anthropic key | `{proxy}/v1/proxy/llm/chat/completions` | OpenAI, Anthropic model | `Authorization: Bearer` (gateway key) |
| `proxy-anthropic-native` | gateway URL and Anthropic key | `{proxy}/v1/messages` | Anthropic | `X-API-Key` (gateway key) |
| `proxy-claude-code` | gateway URL and Anthropic key | `{proxy}/v1/messages` | Anthropic | as above, plus `anthropic-beta: claude-code-20250219` and a `claude-code/...` user agent |

Every request is a streaming one with the same fixed prompt. The OpenAI body sets
`max_completion_tokens: 100`; the Anthropic body sets `max_tokens: 100` and `temperature: 0.0`.

Output, in `BENCHMARK_OUTPUT_DIR`:

- `results-{timestamp}.json`: one record per request (target, concurrency, TTFB, end-to-end
  time, completion tokens, tokens per second, status code, error).
- `summary-{timestamp}.md`: per target and concurrency level, P50, P95 and P99 in ms, average
  TTFB, average tokens per second and the error count, followed by a "Proxy Overhead" table
  (each gateway target against its direct counterpart). The summary is also printed.

## Test suites

```bash
cargo nextest run --locked --workspace --lib --tests --features db-tests
```

Run in the root for the Community suite and in `enterprise/` for the Enterprise suite. It needs
a throwaway Postgres and a Redis.

**DB-backed tests must never touch a live database.** Use a throwaway Postgres started with
`-c max_locks_per_transaction=512`, and stamp its `template1` once with
`BRUTOR_TEST_DB_IS_DISPOSABLE=1 tests/schema/prepare-test-template.sh` (the script refuses
without the flag). [`tests/integration/README.md`](../tests/integration/README.md) shows a
complete setup with throwaway containers on non-default ports.

Environment the suite reads: `DATABASE_URL` (the throwaway server) and `TEST_REDIS_URL`
(default `redis://127.0.0.1:6379/15`). CI also sets `JWT_SECRET` and
`ENCRYPTION_KEY`.

### What the command covers

`--lib --tests` runs unit tests and test targets, not doctests. `--workspace` includes the
`crates/*` members in the root workspace.

| Workspace | Targets |
|---|---|
| Root | In-source unit tests of `brutor_gateway_core` and of the `crates/*` members; the top-level targets `tests/*.rs`, among them the `tests/*_guard.rs` source-scan guards; the `integration` target (`tests/integration/main.rs`) |
| `enterprise/` | In-source unit tests of `brutor_gateway_enterprise`; `enterprise/tests/*.rs`; the `integration` target (`enterprise/tests/integration/main.rs`), which holds the Enterprise features' own tests and includes the Community `tests/integration/main.rs` by path, so the Community suite is re-run against the Enterprise composition. Those tests are listed as `community::<module>::<test>` |

The `integration` target of each workspace has `required-features = ["db-tests"]`: without
`--features db-tests` it is not built. The Enterprise feature `db-tests` enables the Community
crate's. `enterprise/tests/migrations` is a link to the root `tests/migrations`.

### nextest configuration

`.config/nextest.toml` (root) and `enterprise/.config/nextest.toml`:

- A test is reported slow after 60 s and killed after 5 minutes. `fail-fast = false`.
- Profile `ci`: the same, `retries = 0`, and a JUnit report (`junit.xml`, written to
  `target/nextest/ci/`).
- Test group `shared-db-serial` (`max-threads = 1`) serialises tests that share one database or
  Redis database. Root: the `system_alerts_resolver_tests` binary, `usage_spool::*` and
  `audit_spool::*`. Enterprise: the `group_inheritance_tests` and `enterprise_route_tests`
  binaries, and the two spool filters also under the `community::` prefix.

### CI

`.github/workflows/ci.yml` runs the suite command with `--profile ci` in the `Tests` job (root)
and in the `enterprise` job (`working-directory: enterprise`), each against `postgres:16` and
`redis:7.4-alpine` service containers, with `cargo-nextest` 0.9.145, after raising
`max_locks_per_transaction` to 512 and stamping `template1`. Each uploads its JUnit report as
an artefact. The job `community-alone` removes `enterprise/` from its checkout and runs
`scripts/ci-community-alone.sh`: every gate and the root suite again, with no secret and no
other repository.

## Adding a benchmark

To an existing bench: add a `group.bench_function(...)` in the relevant file. Use `iter_batched`
when an iteration needs fresh state, so that set-up stays out of the measured closure:

```rust
group.bench_function("my_benchmark", |b| {
    b.iter_batched(
        || setup(),                 // not measured
        |state| operation(state),   // measured
        criterion::BatchSize::SmallInput,
    )
});
```

A new bench:

1. Create `benches/my_benchmark.rs` (or `enterprise/benches/my_benchmark.rs`).
2. Add the entry to that workspace's `Cargo.toml`:
   ```toml
   [[bench]]
   name = "my_benchmark"
   harness = false
   ```
3. Use the Criterion structure of the existing files (`criterion_group!`, `criterion_main!`).
4. Check that it builds: `cargo bench --no-run --locked`.

Notes:

- A bench links the crate as an external consumer (`use brutor_gateway_core::...`), so what it
  calls must be `pub`, and `#[cfg(test)]` items are not visible to it.
- `LlmModel` derives `Default`, so `enabled` is `false` unless set. The existing benches build
  it with `enabled: true, ..Default::default()`.
