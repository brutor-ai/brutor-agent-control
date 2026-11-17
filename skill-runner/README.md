# Brutor Agent Control — Skill Runner

The Skill Runner is the sandbox that runs Agent Skill scripts (Python,
JavaScript, TypeScript, Bash) for Brutor Agent Control. It is the only
component that executes code a tenant wrote, so it runs as its own container
with nothing worth stealing in it: no database credentials, no encryption
keys, no provider or OAuth tokens. The Core Gateway sends it one
self-contained request per run, signed with a short-lived token, and gets the
captured output back.

**Edition: both.** The Community package and the Enterprise trial package run
the same service (`skill-runner`, port 8210, not published to the host).

- Running it from source and a first run:
  [docs/getting-started-guide.md](docs/getting-started-guide.md).
- Adversary, assets, controls and residual risks:
  [THREAT-MODEL.md](THREAT-MODEL.md).
- How skills use it (execution modes, timeouts, session workspaces, failure
  responses): <https://docs.brutor.ai/agent-skills/sandbox/>.

## What it serves

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/v1/runner/run_script` | Bearer JWT | Run one named script in a per-session workspace and return its output. |
| `POST` | `/v1/runner/execute` | Bearer JWT | Run the scripts a skill's `SKILL.md` references and return the outputs. |
| `GET` | `/health` | none | Liveness: `{"status":"ok","service":"brutor-skill-runner","version":"…"}`. |
| `GET` | `/metrics` | none | Prometheus text: `skill_runner_executions_total`, `skill_runner_execution_duration_ms`, `skill_runner_script_executions_total`. |

There are no other routes. The token is HS256, signed by the Core Gateway
with `BRUTOR_SKILL_RUNNER_JWT_SECRET`; its `exec` claim must equal the request's
`execution_id`, so a token cannot be replayed for another run. The runner only
verifies tokens and holds nothing that can sign one.

It is a separate crate, not a member of the Core Gateway's workspace, so it
cannot link the gateway's code. It has no database access and keeps no state
except session workspaces under `BRUTOR_SKILL_RUNNER_WORKSPACE_ROOT`, which expire.

## Build, run, test

```bash
cargo build --release             # target/release/brutor-skill-runner

export BRUTOR_SKILL_RUNNER_JWT_SECRET="$(openssl rand -hex 32)"
cargo run --release               # listens on 0.0.0.0:8210

docker build -t skill-runner .    # the image the packages run
```

Running scripts outside the image needs the interpreters on `PATH`: `python3`,
`node`, `tsx` and `bash`. Outside Linux the runner logs a warning at start:
its own environment is readable by the scripts it runs, so use a throwaway
secret.

Gates, as CI runs them (`.github/workflows/ci.yml`):

```bash
cargo fmt --all -- --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked --all-targets
cargo audit                       # cargo install cargo-audit --locked
```

The tests need `python3` and `bash`; they start no server and need no Docker.
`tests/http_integration.rs` mounts the real router in process. `.coverage-floor`
is the line-coverage floor the pre-push check holds.

## Configuration

Every environment variable the runner reads is defined once, in
`src/env_vars.rs` (the registry), and read through `src/env_registry.rs`;
nothing else in `src/` reads the environment. This table and `.env.example`
are generated from the registry and held to it by `tests/env_registry.rs`
(`UPDATE_ENV_DOCS=1 cargo test --test env_registry` rewrites both).

<!-- env-registry:begin (generated from src/env_vars.rs) -->

| Variable | Type | Default | Edition | Description | Accepted | Example |
|---|---|---|---|---|---|---|
| `BRUTOR_SKILL_RUNNER_JWT_SECRET` (secret) | string | **required** | both | The secret the Core Gateway signs execution tokens with (HS256). The runner only verifies tokens. Must equal the Core Gateway's BRUTOR_SKILL_RUNNER_JWT_SECRET. | at least 32 characters | `<output of: openssl rand -hex 32>` |
| `BRUTOR_SKILL_RUNNER_LISTEN_ADDR` | socket_addr | `0.0.0.0:8210` | both | Address the HTTP server binds. The Core Gateway's BRUTOR_SKILL_RUNNER_URL must point at it. | an IP address and a port (ip:port) | `127.0.0.1:8210` |
| `BRUTOR_SKILL_RUNNER_SANDBOX_TIMEOUT_SECS` | integer | `30` | both | Advanced. Wall-clock limit, in seconds, of a script in SANDBOX mode. | a whole number of seconds | `60` |
| `BRUTOR_SKILL_RUNNER_TRUSTED_TIMEOUT_SECS` | integer | `120` | both | Advanced. Wall-clock limit, in seconds, of a script in TRUSTED mode. | a whole number of seconds | `300` |
| `BRUTOR_SKILL_RUNNER_WORKSPACE_ROOT` | path | none | both | Advanced. Directory under which session workspaces live (&lt;root&gt;/&lt;tenant&gt;/&lt;session&gt;). Unset: brutor-skill-workspaces under the temporary directory (TMPDIR). | a directory the runner can write to | `/var/lib/brutor/skill-workspaces` |
| `BRUTOR_SKILL_RUNNER_TRUSTED_ENV_ALLOWLIST` | list | `OTEL_EXPORTER_OTLP_ENDPOINT,OTEL_SERVICE_NAME,LC_ALL,TZ` | both | Names of the runner's environment variables a TRUSTED script is handed, when set. Setting it replaces the default list. Proxy variables (HTTP_PROXY, HTTPS_PROXY, NO_PROXY and the lower-case forms) and OTEL_EXPORTER_OTLP_HEADERS may carry credentials and are handed on only when named here. The runner's own variables (this table) cannot be added. SANDBOX scripts see none. | comma-separated variable names | `OTEL_EXPORTER_OTLP_ENDPOINT,OTEL_SERVICE_NAME,LC_ALL,TZ,HTTPS_PROXY,NO_PROXY` |
| `BRUTOR_LOG_JSON_ENABLED` | boolean | `false` | both | Write each log line as one JSON object (time, level, target, span, fields), for a log pipeline. false: human-readable text lines with the same fields. | true or false (also 1/0, yes/no, on/off) | `true` |
| `RUST_LOG` | string | `brutor_skill_runner=info,tower_http=info` | both | Log filter. A filter that does not parse is replaced by the default. | tracing filter directives (target=level, comma-separated) | `brutor_skill_runner=debug,tower_http=info` |
| `TMPDIR` | path | none | both | The platform's temporary directory. Only the default of BRUTOR_SKILL_RUNNER_WORKSPACE_ROOT uses it. Unset: the platform default (/tmp). | a directory | `/tmp` |

<!-- env-registry:end -->

At start the runner reads its environment once and refuses to start, with one
line per name and never a value, when a value is not of its type (a number
that is not digits, a boolean that is not one of `true false 1 0 yes no on
off`, an empty value for either), or when a required variable is missing. A
name that is not in the table is not read.

`brutor-skill-runner --print-env` prints the registry as JSON and exits
without reading any variable:

```text
{ "schema_version": 2, "service": "skill-runner",
  "variables": [ { "name", "type", "default" (string or null), "required",
                   "secret", "edition", "description", "example",
                   "accepted" } ] }
```

`type` is one of `string`, `integer`, `boolean`, `url`, `socket_addr`, `path`,
`list`; `edition` is `community`, `enterprise` or `both`.

The Core Gateway side: `BRUTOR_SKILL_RUNNER_URL` (default
`http://skill-runner:8210`), the same `BRUTOR_SKILL_RUNNER_JWT_SECRET`, and
`BRUTOR_SKILL_RUNNER_JWT_TTL_SECS` (default 300).

Fixed in code (`src/config.rs`), not settings: the largest request body,
`MAX_REQUEST_BYTES` = 41,048,576 bytes (about 41 MB; a larger request gets
413). It is derived from the largest skill, `SKILL_MAX_BYTES` = 30,000,000
bytes of files uncompressed (the Control Plane's upload limit): the Core
Gateway sends a skill's files in one JSON request, binaries base64-encoded,
so 30,000,000 × 4/3 + 1 MiB of JSON framing. The Core Gateway derives its
own limit the same way, and `tests/request_limit.rs` checks that the two
agree. Also fixed: 30 s of clock skew on a token's expiry, and removal of a
session workspace after one hour.

A TRUSTED script sees only the variables named in
`BRUTOR_SKILL_RUNNER_TRUSTED_ENV_ALLOWLIST`. Proxy variables and
`OTEL_EXPORTER_OTLP_HEADERS` can carry credentials and are not on the default
list: name them there to hand them on.

## How the packages run it

Both download packages, Community and Enterprise, run the image with:

- a read-only root file system and a 64 MiB `tmpfs` at `/tmp` (`HOME=/tmp`);
- `cap_drop: ALL` and `no-new-privileges`, with Docker's default seccomp
  profile left on;
- limits of 1 CPU, 256 MiB and 64 processes;
- the non-root user `runner` (uid 10001), set in the `Dockerfile`;
- membership of one network only, `skill-runner-net`, which is internal (no
  outbound route) and whose other member is the Core Gateway;
- no published port.

A deployment that drops any of these weakens the sandbox; read
[THREAT-MODEL.md](THREAT-MODEL.md) first.

## Layout

```
src/
├── main.rs       entry point: configuration, router, graceful shutdown
├── lib.rs        re-exports for the integration tests
├── api.rs        routes, state, the auth gate
├── auth.rs       token verification and the exec-claim binding
├── config.rs     Config::from_env()
├── env_vars.rs   the registry: every environment variable, defined once
├── env_registry.rs  how the registry is read, checked and printed (same file in the three worker services)
├── executor.rs   the subprocess pipeline: environment scrubbing, timeouts, output caps
├── workspace.rs  per-session workspaces: path encoding, seeding, expiry
├── metrics.rs    Prometheus counters and histogram
└── types.rs      wire types
tests/
├── http_integration.rs   the router, in process
├── config_env.rs         every variable changes the configuration; malformed values are refused
├── env_registry.rs       the registry is the only reader; generated files; --print-env
├── readable_names.rs     what a person reads names the product as it is called today, and cites no private record
└── common/private_references.rs   the scanner for those citations (the same file in every Rust repository)
docs/getting-started-guide.md
THREAT-MODEL.md
```

## Third-party licences

[`THIRD_PARTY_LICENSES`](THIRD_PARTY_LICENSES) lists every crate the binary is built from (from
`Cargo.lock`: version, SPDX licence) and the full licence texts. `scripts/third-party-licenses.sh`
writes it with cargo-about 0.9.3 (`cargo install --locked --features cli cargo-about@0.9.3`;
configuration `about.toml` and `about.hbs`) and `--check` fails when it is not current; CI and the
image build run the check. The accepted licences are `deny.toml`'s allow-list; a crate under any other
licence fails the generation. The image carries the file at
`/usr/share/licenses/brutor/THIRD_PARTY_LICENSES` and `/app/THIRD_PARTY_LICENSES` (label
`ai.brutor.third-party-licenses`); `tests/third_party_licenses_guard.rs` holds the pieces together.
