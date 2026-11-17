# Brutor Agent Control — Core Gateway (Community Edition)

The enforcement plane of Brutor Agent Control: a Rust/Axum service that carries all runtime
traffic (LLM, MCP, A2A, agent skills, the OAuth proxy) and applies, per request, the
configuration the Control Plane (`brutor-gateway-control`, Python) wrote to the shared
PostgreSQL database: who may call what, guardrails, limits and budgets, approvals, the audit
trail and its sealed evidence. It never writes configuration and never alters the schema.

**This repository's root crate is the Community Edition.** The Enterprise Edition is a second
cargo workspace in [`enterprise/`](enterprise/README.md) that composes extensions onto the same
gateway; nothing in the Community crate depends on it, and a Community build works with
`enterprise/` deleted.

Documentation for users and operators is at <https://docs.brutor.ai>. Running the gateway from
source for the first time: [`docs/getting-started-guide.md`](docs/getting-started-guide.md).

## The two editions

| | Community | Enterprise |
|---|---|---|
| Where | repository root: `src/`, `crates/*` | `enterprise/`: its own workspace, `Cargo.lock` and `deny.toml` |
| Crate and binary | `brutor-gateway-core` | `brutor-gateway-enterprise` (depends on `..` and on the private licence library) |
| Image | `Dockerfile` → `ghcr.io/brutor-ai/brutor-agent-control-core-gateway` (Apache-2.0) | `enterprise/Dockerfile` → `ghcr.io/brutor-ai/brutor-agent-control-enterprise-core-gateway` |
| Licence key | none | `BRUTOR_LICENSE_KEY`, checked before anything is connected |
| Instances | one (see "One instance") | several, with the clustering extension |

The Enterprise gateway is the Community gateway plus eight extensions: the licence gate, batch
jobs, the User Portal API (`/v1/portal/*`), the prompt cache, the response cache, the Knowledge
Base, system administrators and clustering. They register through the extension interface,
`GatewayBuilder` / `Extension` in `src/builder.rs`. A Community gateway serves no `/v1/portal/*`
route and answers a system administrator's token as an unknown credential.

The boundary between the editions is one file, `editions.yaml`, kept outside this repository and
checked against it by `check_editions.py`, which is kept with that file.

## Requirements

- **Rust 1.97.1**, pinned in `rust-toolchain.toml` (rustup installs it on the first build).
- **PostgreSQL 16** holding the schema the Control Plane created. The gateway does not run
  migrations: start the Control Plane against the database first.
- **Redis, or Valkey.** Required. Every usage limit (quotas, budgets, rates, run and session
  caps, in-flight slots) is admitted on shared counters there, and configuration changes reach
  the gateway over Redis pub/sub. Without `REDIS_URL`, or without a Redis that answers there,
  the gateway refuses to start. (The test suites build their states without the builder; a
  test that needs Redis connects to `TEST_REDIS_URL` itself.)
- The same `JWT_SECRET` and `ENCRYPTION_KEY` as the Control Plane.

No licence key, no Qdrant and no object store are needed by the Community gateway.

### Where media files are kept

Uploaded and generated files (`/v1/proxy/files`, images, speech, video) go to one of three
stores, chosen by `BRUTOR_MEDIA_STORAGE_BACKEND`; unset, the filesystem store when
`BRUTOR_MEDIA_FILESYSTEM_DIR` is set and the in-memory one otherwise:

- **filesystem**: files under `BRUTOR_MEDIA_FILESYSTEM_DIR` (in the packages, a named volume
  mounted into the gateway), at most `BRUTOR_MEDIA_FILESYSTEM_MAX_BYTES` bytes in total
  (default 10 GiB). Each tenant's files are under a directory of their own
  (`<tenant>/<kind>/<yyyy>/<mm>/<file_id>.<ext>`, with the Content-Type in a `.meta` file
  beside each); names are ids the gateway mints, never a caller's. A write is atomic (a
  temporary file under `.tmp/`, renamed into place) and one that would pass the limit is
  refused with HTTP 507 naming the setting; the media GC's deletes make room again, and the
  tenants' own media quotas apply within the limit. The gateway serves the signed download
  links itself, at `<BRUTOR_GATEWAY_PUBLIC_URL>/v1/media/<key>?expires=…&signature=…`: the
  signature is HMAC-SHA256 over the tenant, the key and the expiry, with a key derived from
  `JWT_SECRET`; a link lives `BRUTOR_MEDIA_SIGNED_URL_TTL_SECS`, and an unknown, expired or
  altered link answers 404. **One gateway instance only**: with several, use s3 (a
  composition that runs as several instances logs a warning at start, and an error when a
  second instance uses the store). Back the directory up with the database.
- **s3**: a bucket (`BRUTOR_MEDIA_BUCKET`, AWS S3 or an S3-compatible store such as MinIO),
  for any number of instances; the links are the store's presigned URLs.
- **memory**: in the gateway's process, lost at a restart (development and tests).

### Logs

Human-readable lines by default; `BRUTOR_LOG_JSON_ENABLED=true` writes one JSON object per
line, for a log collector. `RUST_LOG` chooses what is logged.

### One instance

A Community gateway runs as one instance per Redis and database. At start it takes a lease in
Redis (`brutor:instance:core`, 15 seconds, renewed every 5). A second instance waits up to 20
seconds for the lease and then refuses to start, naming the Enterprise Edition; a graceful stop
releases the lease, so a restart does not wait. Running several instances is the Enterprise
Edition's clustering extension.

## Configuration

Read from the environment, once, at start; a `.env` file in the working directory is loaded
first (`.env.example` is a starting point). Every variable the Community gateway reads is
defined once, in `src/env_vars.rs` (the registry), and nothing reads the environment any other
way. This table, `.env.example` and `--print-env` are generated from it:

```bash
cargo run --bin brutor-gateway-core -- --print-env    # the registry as JSON; reads no variable
UPDATE_ENV_DOCS=1 cargo test --test env_registry      # rewrites this table and .env.example
```

The start is refused, with one line per variable and never a value, when:

- a required variable is not set;
- a value is not of its variable's type (a number that is not one, a boolean that is not one of
  `true`, `false`, `1`, `0`, `yes`, `no`, `on`, `off`, a variable of those types set to
  nothing), is out of range or is not one of the accepted values. Nothing falls back to a
  default;
- a retired name is set (the second table): the line says what to set instead, or why the
  variable was removed. There are no aliases.

A `BRUTOR_*` variable that is set and that this gateway does not read (a misspelt name, a
variable of another service or of the Enterprise Edition) does not refuse the start: it is
named in one warning in the log. The names a container platform gives every pod for a service
called `brutor-…` (`…_SERVICE_HOST`, `…_PORT_8100_TCP_ADDR`) are left out of it.

The variables only the Enterprise Edition reads are not here: see `enterprise/README.md`. The
developer benchmark tool's `BENCHMARK_*` variables are in `benches/README.md`.

<!-- env-registry:begin (generated from src/env_vars.rs) -->

| Variable | Type | Default | Edition | Description | Accepted | Example |
|---|---|---|---|---|---|---|
| `DATABASE_URL` (secret) | url | **required** | both | PostgreSQL connection string of the database the Control Plane created and migrated. The gateway reads its configuration there and never alters the schema. | a postgresql:// URL | `postgresql://<user>:<password>@localhost:5432/<database>` |
| `REDIS_URL` (secret) | url | **required** | both | Redis (or Valkey) the gateway enforces every usage limit on and listens on for cache invalidation from the Control Plane. The gateway does not start without it. The Control Plane must use the same Redis. | a redis:// or rediss:// URL | `redis://<host>:6379/0` |
| `JWT_SECRET` (secret) | string | **required** | both | Secret the gateway verifies the Control Plane's tokens with and signs its own with. Must equal the Control Plane's JWT_SECRET. A value from an example file is refused. | at least 32 characters, not a placeholder | `<output of: openssl rand -hex 32>` |
| `ENCRYPTION_KEY` (secret) | string | **required** | both | Decrypts the credentials the Control Plane stored (provider keys, OAuth tokens, client secrets) and keys the gateway's own derived secrets. Must equal the Control Plane's ENCRYPTION_KEY. A value from an example file is refused. | at least 32 characters, not a placeholder | `<output of: openssl rand -hex 32>` |
| `BRUTOR_GATEWAY_LISTEN_ADDR` | socket_addr | `0.0.0.0:8100` | both | Address the gateway's HTTP server binds: every /v1 route. The Control Plane and the workers reach the gateway at this address. | host:port | `127.0.0.1:8100` |
| `BRUTOR_GATEWAY_PUBLIC_URL` | url | none | both | URL at which browsers and agents reach the gateway: the base of the OAuth metadata, redirect URIs and the MCP endpoints it advertises. Unset: http://localhost:&lt;port of BRUTOR_GATEWAY_LISTEN_ADDR&gt;. | an http:// or https:// URL | `https://gateway.example.com` |
| `BRUTOR_CORS_ORIGINS` | list | none | both | Origins that may read the non-protocol routes cross-origin. The Control Plane reads the same variable. Unset: http://localhost:3001 and http://localhost:3002. | comma-separated origins (scheme://host[:port]) | `https://admin.example.com,https://portal.example.com` |
| `BRUTOR_TRUSTED_PROXIES` | list | none | both | Reverse proxies whose X-Forwarded-For is believed. A request's client IP (audit rows, the request log, run and agent identities) is the connecting address unless that address is listed; then it is the right-most X-Forwarded-For address that is not itself listed. Forwarded and X-Real-IP are never read, and no URL is built from a request: OAuth metadata and redirect URIs come from BRUTOR_GATEWAY_PUBLIC_URL. Unset: no forwarded header is believed, and behind a reverse proxy its address is recorded. The Control Plane reads the same variable. | comma-separated IP addresses and CIDR ranges (no host names, no *) | `10.0.0.0/8,192.168.1.10` |
| `JWT_ALGORITHM` | string | `HS256` | both | Algorithm of the tokens the gateway issues after an MCP OAuth exchange. Must equal the Control Plane's JWT_ALGORITHM. | HS256, HS384 or HS512 | `HS512` |
| `JWT_EXPIRES_IN_SECS` | integer | `3600` | both | Lifetime, in seconds, of the tokens the gateway issues (MCP OAuth exchange; portal sessions in the Enterprise Edition). The Control Plane reads the same variable. | a whole number of seconds, 1 or more | `7200` |
| `BRUTOR_GATEWAY_DB_POOL_SIZE` | integer | `20` | both | Advanced. Most connections the gateway's PostgreSQL pool opens. The Control Plane reads the same variable to cap its bulk operations against the gateway. | a whole number, 1 or more | `50` |
| `BRUTOR_MCP_TIMEOUT_SECS` | integer | `30` | both | Seconds the gateway waits for an MCP server that has no timeout of its own configured. | a whole number of seconds, 1 or more | `60` |
| `BRUTOR_A2A_DELEGATION_MAX_AGE_SECS` | integer | `600` | both | Advanced. Oldest parent step, in seconds, an agent-to-agent delegation may name (plus 60 s for clock skew): an older one is refused as a replay. An agent that works longer than this on a delegated task before it delegates further is refused too, so raise it above the longest such task; raising it widens the replay window by as much. | a whole number of seconds, 1 or more | `900` |
| `BRUTOR_EMBEDDING_MODEL` | string | none | both | Name of the embedding model of a tenant that has not chosen one. An agent needs no grant of its own for the tenant's embedding model. In the Enterprise Edition it is also what the knowledge base and the response cache embed with, and must equal the KB Uploader's and the Control Plane's BRUTOR_EMBEDDING_MODEL. No default. | a model name in the tenants' catalogs | `text-embedding-3-small` |
| `BRUTOR_JUDGE_MODEL` | string | none | both | Name of the model that judges semantic policies for a tenant that has not chosen one. Without either, a semantic policy has no judge and says so. | a model name in the tenants' catalogs | `gpt-4o-mini` |
| `BRUTOR_MEDIA_STORAGE_BACKEND` | string | none | both | Where uploaded and generated files are kept. filesystem: files under BRUTOR_MEDIA_FILESYSTEM_DIR, for ONE gateway instance; the gateway serves their signed download links itself at /v1/media/. s3: the bucket named by BRUTOR_MEDIA_BUCKET, for any number of instances. memory: in the gateway's process, lost at restart (development and tests). Unset: filesystem when BRUTOR_MEDIA_FILESYSTEM_DIR is set, memory otherwise. | filesystem, s3 or memory | `s3` |
| `BRUTOR_MEDIA_BUCKET` | string | none | both | Bucket of the media store. Required when BRUTOR_MEDIA_STORAGE_BACKEND is s3: the gateway refuses to start without it. | a bucket name | `brutor-media` |
| `BRUTOR_MEDIA_S3_REGION` | string | none | both | Region of the media bucket. Unset: AWS_REGION, and us-east-1 when that is unset too. | a region name | `eu-north-1` |
| `AWS_REGION` | string | none | both | The AWS SDK's region. The gateway itself reads it only as the region of the media bucket when BRUTOR_MEDIA_S3_REGION is unset. | a region name | `eu-north-1` |
| `BRUTOR_MEDIA_S3_ENDPOINT` | url | none | both | Endpoint of an S3-compatible store (MinIO, R2) as the gateway reaches it. Unset: AWS S3 in the region. | an http:// or https:// URL | `http://minio:9000` |
| `BRUTOR_MEDIA_S3_PUBLIC_ENDPOINT` | url | none | both | Endpoint of the same store as a browser reaches it, used only in the signed download URLs the gateway hands out. Set it when that differs from BRUTOR_MEDIA_S3_ENDPOINT (a store on a container network). Unset: the endpoint the gateway uses. | an http:// or https:// URL | `http://localhost:9000` |
| `BRUTOR_MEDIA_S3_FORCE_PATH_STYLE_ENABLED` | boolean | `true` | both | Addresses the bucket as a path (endpoint/bucket), which MinIO and most S3-compatible stores need. Set false for AWS S3. | true, false, 1, 0, yes, no, on, off | `false` |
| `BRUTOR_MEDIA_S3_ACCESS_KEY_ID` | string | none | both | Access key of the media store. Set it together with BRUTOR_MEDIA_S3_SECRET_ACCESS_KEY, or set neither and the AWS SDK's own credential chain is used (an instance or task role). | an access key id | `brutor-media` |
| `BRUTOR_MEDIA_S3_SECRET_ACCESS_KEY` (secret) | string | none | both | Secret key of the media store. See BRUTOR_MEDIA_S3_ACCESS_KEY_ID. | a secret access key | `<the secret key of the media store>` |
| `BRUTOR_MEDIA_SIGNED_URL_TTL_SECS` | integer | `900` | both | Lifetime, in seconds, of a signed download URL. Never less than 60. | a whole number of seconds; below 60 is 60 | `300` |
| `BRUTOR_MEDIA_FILESYSTEM_DIR` | path | none | both | Directory of the filesystem media store: a volume of its own, mounted into the gateway and backed up with the database. Setting it selects the filesystem store unless BRUTOR_MEDIA_STORAGE_BACKEND says otherwise. Each tenant's files are under a directory of their own; names are ids the gateway mints. The gateway creates the directory and refuses to start when it cannot write there. One gateway instance only: with several, use s3. | an absolute or relative directory path | `/var/lib/brutor/media` |
| `BRUTOR_MEDIA_FILESYSTEM_MAX_BYTES` | integer | `10737418240` | both | Size limit of the filesystem media store, in bytes: the total of the stored files. A write that would pass it is refused (HTTP 507, naming this variable); the media GC's deletes make room again. Each tenant's own media quota still applies within it. Default 10 GiB. | a whole number of bytes, 1 or more | `53687091200` |
| `BRUTOR_SKILL_RUNNER_URL` | url | `http://skill-runner:8210` | both | Base URL of the Skill Runner, which runs skill scripts for the gateway. It must point at the runner's BRUTOR_SKILL_RUNNER_LISTEN_ADDR. | an http:// or https:// URL | `http://localhost:8210` |
| `BRUTOR_SKILL_RUNNER_JWT_SECRET` (secret) | string | none | both | Secret the gateway signs each execution token for the Skill Runner with. Must equal the runner's BRUTOR_SKILL_RUNNER_JWT_SECRET. Unset: the gateway starts, and every skill execution fails with a message that names this variable. | at least 32 characters (the runner refuses a shorter one) | `<output of: openssl rand -hex 32>` |
| `BRUTOR_SKILL_RUNNER_SANDBOX_TIMEOUT_SECS` | integer | `30` | both | Advanced. Wall-clock limit, in seconds, of a skill script in SANDBOX mode. The Skill Runner enforces it; the gateway reads the same variable to wait for a run that long plus a margin. Set the same value on both. | a whole number of seconds, 1 or more | `60` |
| `BRUTOR_SKILL_RUNNER_TRUSTED_TIMEOUT_SECS` | integer | `120` | both | Advanced. Wall-clock limit, in seconds, of a skill script in TRUSTED mode. The Skill Runner enforces it; the gateway reads the same variable to wait for a run that long plus a margin. Set the same value on both. | a whole number of seconds, 1 or more | `300` |
| `BRUTOR_OUTBOUND_ALLOW_HOSTS` | list | none | both | Outbound policy. Destinations of MCP servers, model endpoints, agents, IdPs, webhooks and witnesses at a private address (loopback, RFC 1918, carrier-grade NAT, IPv6 unique-local, and a name that resolves to one) are refused unless listed here; link-local and cloud metadata addresses are refused whatever this says. A host name allows every address it resolves to, *.domain the domain and its subdomains, an address or CIDR range those addresses. The Control Plane and KB Connector Sync read the same variable. | comma-separated host names, *.domain, IP addresses and CIDR ranges (no port, scheme or path) | `mcp.internal,*.corp.example,10.20.0.0/16,ollama` |
| `BRUTOR_AUTONOMY_FALLBACK_FLOOR` | string | `restricted` | both | Autonomy level an agent runs at while the governance database cannot be read and the gateway has no recent level for it. restricted keeps reads and chat serving and holds everything that changes the world; suspended stops the agent. | autonomous, approval_required, restricted or suspended | `suspended` |
| `BRUTOR_AUDIT_SIGNING_KEY` (secret) | string | none | both | Ed25519 seed the gateway signs audit-chain checkpoints with. Unset: the rows are still hash-chained, and no checkpoint is signed. | exactly 64 hexadecimal characters (32 bytes) | `<output of: openssl rand -hex 32>` |
| `BRUTOR_AUDIT_TRUSTED_KEYS` | list | none | both | Public keys, besides the gateway's own, whose audit-chain checkpoints verify: the keys of other and of earlier instances. | comma-separated Ed25519 public keys, 64 hexadecimal characters each | `3f9a0c1e5b7d2f4a6c8e0b1d3f5a7c9e1b3d5f7a9c0e2b4d6f8a1c3e5b7d9f0a` |
| `BRUTOR_EVIDENCE_SIGNING_KEY` (secret) | string | none | both | Ed25519 seed of the deployment's evidence key: every governed decision is sealed as a signed statement with it. Unset: no record is sealed, traffic is unaffected, and the console says so. Before replacing it, add its public key to BRUTOR_EVIDENCE_RETIRED_KEYS. | exactly 64 hexadecimal characters (32 bytes) | `<output of: openssl rand -hex 32>` |
| `BRUTOR_EVIDENCE_RETIRED_KEYS` | list | none | both | Public keys of earlier deployment evidence keys. Still published and trusted for verification; nothing is signed with them. | comma-separated Ed25519 public keys, 64 hexadecimal characters each | `3f9a0c1e5b7d2f4a6c8e0b1d3f5a7c9e1b3d5f7a9c0e2b4d6f8a1c3e5b7d9f0a` |
| `BRUTOR_DEPLOYMENT_ID` | string | `default` | both | Stable identity of this deployment. It is hashed into the identifier of every tenant's evidence log: set it once and never change it, a new value starts new logs. | any text | `acme-prod` |
| `BRUTOR_EVIDENCE_ISSUER` | string | none | both | Issuer written into every signed statement. Unset: urn:brutor:deployment:&lt;BRUTOR_DEPLOYMENT_ID&gt;. A deployment that publishes a did:web sets it here. | a URI | `did:web:gateway.example.com` |
| `BRUTOR_EVIDENCE_HEAD_CADENCE_ENTRIES` | integer | `100` | both | A signed tree head is issued for a tenant's evidence log once this many entries are not yet covered by one. | a whole number, 1 or more | `500` |
| `BRUTOR_EVIDENCE_HEAD_CADENCE_SECS` | integer | `900` | both | A signed tree head is issued once the oldest entry not yet covered by one is this many seconds old. Checked when an entry is appended: an idle log issues none. | a whole number of seconds, 1 or more | `300` |
| `BRUTOR_EVIDENCE_WITNESS_ENABLED` | boolean | `true` | both | Registers signed tree heads with the tenants' witnesses. An incident kill switch: false stops every call to a witness and traffic is unaffected; each head issued meanwhile is recorded as unwitnessed (a receipt row with verdict skipped per witness, naming this variable, and a warning in the log), so a verifier sees standalone heads for that period. Back to true, the retry loop submits those heads (up to 30 days old). | true, false, 1, 0, yes, no, on, off | `false` |
| `BRUTOR_EVIDENCE_PROFILE_AAC_ENABLED` | boolean | `false` | both | Experimental. Serves the Agent Action Capsule export of evidence records, which follows an individual Internet-Draft: its format, its route and this setting may change or be removed in any release, and a capsule is not covered by the evidence compatibility promise (records and bundles are). Off: that route answers 404. | true, false, 1, 0, yes, no, on, off | `true` |
| `BRUTOR_METRICS_ENABLED` | boolean | `false` | both | Records Prometheus metrics and serves them at /metrics on BRUTOR_METRICS_LISTEN_ADDR. Off: nothing is recorded and that listener is not opened. | true, false, 1, 0, yes, no, on, off | `true` |
| `BRUTOR_METRICS_LISTEN_ADDR` | socket_addr | `127.0.0.1:9464` | both | Address of the /metrics listener, apart from the public one. Bound only when BRUTOR_METRICS_ENABLED is true. It has no authentication: the default is loopback only. To let a Prometheus on another host (or another container) scrape it, set 0.0.0.0:9464 and keep the port on a private network; never publish it. | host:port | `0.0.0.0:9464` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | string | none | both | OTLP endpoint (gRPC) traces are exported to. Unset: no trace is exported. The OpenTelemetry SDK reads its other OTEL_* variables itself. | an OTLP endpoint URL | `http://otel-collector:4317` |
| `OTEL_SERVICE_NAME` | string | `brutor-gateway-core` | both | service.name of the exported traces. | a service name | `brutor-gateway-core-eu` |
| `BRUTOR_LOG_JSON_ENABLED` | boolean | `false` | both | Writes the log as JSON lines, one object per event, for a log collector. Off: human-readable lines. RUST_LOG still chooses what is logged. | true, false, 1, 0, yes, no, on, off | `true` |
| `RUST_LOG` | string | `brutor_gateway_core=info,tower_http=info` | both | Log filter. The gateway does not start with a filter that does not parse. | tracing filter directives (target=level, comma-separated) | `brutor_gateway_core=debug,tower_http=info` |

<!-- env-registry:end -->

## Build, run, test

```bash
cargo build                              # Community gateway, debug
cargo run --bin brutor-gateway-core      # serves on :8100; needs the variables above
cargo build --release --locked --bin brutor-gateway-core
docker build -t ghcr.io/brutor-ai/brutor-agent-control-core-gateway:dev .   # the Community image; takes no build secret
```

```bash
curl -s -o /dev/null -w '%{http_code}\n' localhost:8100/health   # 200, no credential needed
curl -s localhost:8100/v1/version                                # version and edition
```

Tests need a throwaway PostgreSQL and Redis, never a database that holds data (the test
harness creates and drops databases on the server it is given):
[`tests/integration/README.md`](tests/integration/README.md).

```bash
cargo nextest run --locked --workspace --lib --tests --features db-tests
```

The same command in `enterprise/` runs the Enterprise tests and this suite again on the
Enterprise composition.

## Gates

All of these pass before a change is done, in this workspace and in `enterprise/`:

```bash
cargo fmt --all -- --check
cargo clippy --locked --workspace --all-targets --all-features -- -D warnings
cargo deny --locked check
cargo bench --no-run --locked
cargo nextest run --locked --workspace --lib --tests --features db-tests
```

Once, from the root: `scripts/lint-ratchet.sh` (the count of lint suppressions only goes down),
`./tests/schema/gen-test-superset.py --check` and `scripts/third-party-licenses.sh --check`
(and `--workspace enterprise --check`; see [Licence](#licence)).

**The Community workspace stands alone.** `scripts/ci-community-alone.sh` runs all of the
above in a tree that has no `enterprise/` (the CI job `community-alone` removes it first), with
no private dependency and no sibling repository. A test that reads another tree asks
`tests/support/roots.rs` where it is (`BRUTOR_ENTERPRISE_ROOT`, `BRUTOR_CONTROL_REPO`,
`BRUTOR_SKILL_RUNNER_REPO`, `BRUTOR_EDITION_RECORDS_DIR`). A sibling is looked for beside this
repository under its repository name (`../brutor-gateway-control`) and then under its directory in
the public monorepo (`../control-plane`); a test that needs a sibling that is absent is skipped
there, and the script's summary lists it.

What the images are called and say about themselves is one list, `scripts/image-names.txt`
(`tests/image_names_guard.rs`; `scripts/image-check.sh <edition> <image>` for a built image).

## Engineering rules

A bug here is usually a governance bug: a control that silently does not apply. Before changing
anything on an enforcement path:

1. **Fail closed.** A control that cannot decide refuses. An error is never turned into `None`,
   `0`, an empty list or `Default` on an enforcement path, and a result produced by an error is
   never cached. The rule and every control's outage behaviour:
   [`src/services/failure-semantics.md`](src/services/failure-semantics.md);
   `tests/fail_closed_guard.rs` pins it.
2. **Never weaken a gate to get green.** No deleted or loosened test, assertion, lint,
   `deny.toml` rule or timeout. If a gate is wrong, the change says so, with the reason.
3. **A defect gets a failing test first,** then the fix, then a guard for the class when the
   bug is a pattern (`tests/*_guard.rs`; each is proven against the shape it forbids).
4. **Builds are locked.** `Cargo.lock` is committed, CI and the image build with `--locked`,
   the toolchain is pinned in `rust-toolchain.toml` and the Dockerfile's `rust:` tag matches it.
5. **Dependencies are reviewed.** `cargo deny --locked check` passes and every `deny.toml`
   exception carries its reason.
6. **No new lint suppression** without a stated reason and the narrowest scope
   (`scripts/lint-ratchet.sh`).
7. **Safe Rust.** No `unsafe` in production code.
8. **Limits are admitted, never read-then-decided.** A usage limit is a dimension of the
   request's one atomic admission (`src/services/limits.rs`, `usage_state::admission`),
   reserved against the worst case and settled to the actual. A limit that holds across
   replicas is proven against a real Redis, not the in-memory twin
   (`tests/integration/admission_redis.rs`, `quota_counter_redis.rs`).

## What the gateway serves

| Prefix | What |
|---|---|
| `/v1/proxy/llm` | OpenAI-compatible LLM API: chat completions, responses, completions, embeddings, model listing; images, speech, transcription and video under the same prefix |
| `/v1/messages` | Anthropic Messages API, native (and `/v1/messages/count_tokens`) |
| `/v1beta/models/{model}:…` | Gemini API, native |
| `/v1/proxy/mcp/{server_id}`, `/v1/proxy/vmcp/{virtual_server_id}` | MCP servers and virtual MCP servers |
| `/a2a/{tenant_id}/{card_name}`, `/v1/proxy/a2a`, `/.well-known/agent-card/…` | A2A v1.0: tasks, messages, push notification configs, agent cards |
| `/v1/proxy/skills` | Agent skills: catalog, execution |
| `/v1/proxy/files` | Files used by the media routes |
| `/v1/media` | Signed download links of the filesystem media store (the link is the credential) |
| `/v1/proxy/oauth` | OAuth proxy in front of MCP servers' identity providers |
| `/v1/approvals` | Held tool calls: an agent polls `/v1/approvals/{id}/poll` |
| `/v1/runs`, `/v1/audit`, `/v1/evidence`, `/.well-known/brutor-evidence-keys.json` | Runs, audit chain verification, sealed action records and the public evidence keys |
| `/v1/ingest/otel` | OpenTelemetry GenAI trace ingest (reported evidence) |
| `/v1/assurance`, `/v1/internal/approvals` | Called by the Control Plane with the platform service key |
| `/health`, `/healthz`, `/v1/version` | Liveness and version, no credential |
| `/docs` | Swagger UI over the gateway's OpenAPI document (`openapi/proxy-openapi.json`). The document does not describe every route: `tests/fixtures/openapi_coverage/community.txt` says, route by route, which it does (and `enterprise/tests/fixtures/openapi_coverage/enterprise.txt` for the Enterprise gateway) |

Every other route needs a credential (an API key, prefix `sk_brutor_api_`, as
`Authorization: Bearer` or `X-API-Key`; an administrator's token; an agent's workload token) and
a tenant (`X-Tenant-ID`, or the key's own). `tests/integration/credential_surface_matrix.rs`
records what every route answers to every kind of credential.

## Layout

| Path | What |
|---|---|
| `src/main.rs`, `src/builder.rs`, `src/app.rs` | The binary, the builder (pools, state, tasks, the extension interface) and the router |
| `src/routes/` | HTTP handlers, one module per surface |
| `src/services/` | Authentication, limits and admission, guardrails, routing, provider adapters, caches, evidence. [`failure-semantics.md`](src/services/failure-semantics.md): what each control does when it cannot decide |
| `src/middleware/tenant.rs` | Tenant resolution and isolation |
| `src/config/mod.rs` | Configuration from the environment |
| `crates/` | The evidence crates (`brutor-evidence`, `brutor-cose`, `brutor-ctlog`, `brutor-tsp`, `brutor-aac`, `brutor-verify`) |
| `enterprise/` | The Enterprise workspace |
| `tests/` | Source-scan guards (`*_guard.rs`), conformance and route tests; `tests/integration/` needs a database |
| `benches/` | Criterion benchmarks: [`benches/README.md`](benches/README.md) |
| `docs/getting-started-guide.md` | From a checkout to a running gateway and a first call. The only file under `docs/`; user documentation is on docs.brutor.ai |
| `deploy/`, `docker-compose.yml` | A development observability stack (OpenTelemetry collector, Prometheus, Loki, Grafana) |
| `vectors/` | Test vectors for the evidence formats |

Developer notes beside the code: [`src/services/cache/README.md`](src/services/cache/README.md)
(configuration caches and invalidation),
[`src/services/llm_adapter/README.md`](src/services/llm_adapter/README.md) (provider adapters).
The A2A types follow the A2A v1.0.0 release:
[specification](https://github.com/a2aproject/A2A/blob/v1.0.0/docs/specification.md),
[proto](https://github.com/a2aproject/A2A/blob/v1.0.0/specification/a2a.proto).

## Documentation

- Install and first steps: <https://docs.brutor.ai/installation/quickstart/>
- Configuration: <https://docs.brutor.ai/installation/configuration/>
- LLM API: <https://docs.brutor.ai/reference/llm-proxy-api/>; MCP and A2A:
  <https://docs.brutor.ai/reference/mcp-a2a-api/>
- LLM routing: <https://docs.brutor.ai/governance/llm-routing/>
- Metrics, logs and slow-request triage: <https://docs.brutor.ai/observability/metrics/>,
  <https://docs.brutor.ai/observability/slow-request-triage/>

## Licence

See [`LICENSE`](LICENSE).

**Third-party licences.** [`THIRD_PARTY_LICENSES`](THIRD_PARTY_LICENSES) lists every crate the
binary is built from (from `Cargo.lock`: version, SPDX licence) and the full licence texts,
with the Swagger UI the binary embeds; the Enterprise workspace has its own,
`enterprise/THIRD_PARTY_LICENSES`. Both are written by `scripts/third-party-licenses.sh`
(cargo-about 0.9.3, `cargo install --locked --features cli cargo-about@0.9.3`; configuration
`about.toml`, which accepts exactly `deny.toml`'s licences, and `about.hbs`); `--check` fails
when a file is not current, and CI and the image build run it. Each image carries the file at
`/usr/share/licenses/brutor/THIRD_PARTY_LICENSES` and `/app/THIRD_PARTY_LICENSES` (label
`ai.brutor.third-party-licenses`). `tests/third_party_licenses_guard.rs` holds the pieces together.
