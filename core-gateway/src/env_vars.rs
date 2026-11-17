// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The Core Gateway's environment variables: every one the Community gateway
//! reads, defined once. `.env.example`, the configuration table in
//! `README.md` and `--print-env` are generated from [`REGISTRY`];
//! `config::AppConfig` and the services read through it. Adding a variable
//! here without a reader, or reading the environment anywhere else, fails
//! `tests/env_registry.rs`.
//!
//! A variable only the Enterprise Edition reads is not here: it is defined
//! and read in the Enterprise workspace, in the module of the extension that
//! reads it, and joins the registry of that distribution through
//! `Extension::env_vars` ([`assemble`], which the builder calls on the
//! extensions it was composed with).

use std::str::FromStr;
use std::sync::{Arc, PoisonError, RwLock};

use crate::env_registry::{Edition, Env, EnvError, EnvVar, Kind, Problem, Registry};

// ── listening ──────────────────────────────────────────────────────────────

pub static BRUTOR_GATEWAY_LISTEN_ADDR: EnvVar = EnvVar {
    name: "BRUTOR_GATEWAY_LISTEN_ADDR",
    kind: Kind::SocketAddr,
    default: Some("0.0.0.0:8100"),
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Address the gateway's HTTP server binds: every /v1 route. The Control Plane \
                  and the workers reach the gateway at this address.",
    example: "127.0.0.1:8100",
    accepted: "host:port",
};

pub static BRUTOR_GATEWAY_PUBLIC_URL: EnvVar = EnvVar {
    name: "BRUTOR_GATEWAY_PUBLIC_URL",
    kind: Kind::Url,
    default: None,
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "URL at which browsers and agents reach the gateway: the base of the OAuth \
                  metadata, redirect URIs and the MCP endpoints it advertises. Unset: \
                  http://localhost:<port of BRUTOR_GATEWAY_LISTEN_ADDR>.",
    example: "https://gateway.example.com",
    accepted: "an http:// or https:// URL",
};

pub static BRUTOR_METRICS_ENABLED: EnvVar = EnvVar {
    name: "BRUTOR_METRICS_ENABLED",
    kind: Kind::Boolean,
    default: Some("false"),
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Records Prometheus metrics and serves them at /metrics on \
                  BRUTOR_METRICS_LISTEN_ADDR. Off: nothing is recorded and that listener is \
                  not opened.",
    example: "true",
    accepted: "true, false, 1, 0, yes, no, on, off",
};

pub static BRUTOR_METRICS_LISTEN_ADDR: EnvVar = EnvVar {
    name: "BRUTOR_METRICS_LISTEN_ADDR",
    kind: Kind::SocketAddr,
    default: Some("127.0.0.1:9464"),
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Address of the /metrics listener, apart from the public one. Bound only when \
                  BRUTOR_METRICS_ENABLED is true. It has no authentication: the default is \
                  loopback only. To let a Prometheus on another host (or another container) \
                  scrape it, set 0.0.0.0:9464 and keep the port on a private network; never \
                  publish it.",
    example: "0.0.0.0:9464",
    accepted: "host:port",
};

pub static BRUTOR_CORS_ORIGINS: EnvVar = EnvVar {
    name: "BRUTOR_CORS_ORIGINS",
    kind: Kind::List,
    default: None,
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Origins that may read the non-protocol routes cross-origin. The Control Plane \
                  reads the same variable. Unset: http://localhost:3001 and \
                  http://localhost:3002.",
    example: "https://admin.example.com,https://portal.example.com",
    accepted: "comma-separated origins (scheme://host[:port])",
};

pub static BRUTOR_TRUSTED_PROXIES: EnvVar = EnvVar {
    name: "BRUTOR_TRUSTED_PROXIES",
    kind: Kind::List,
    default: None,
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Reverse proxies whose X-Forwarded-For is believed. A request's client IP (audit \
                  rows, the request log, run and agent identities) is the connecting address \
                  unless that address is listed; then it is the right-most X-Forwarded-For \
                  address that is not itself listed. Forwarded and X-Real-IP are never read, \
                  and no URL is built from a request: OAuth metadata and redirect URIs come \
                  from BRUTOR_GATEWAY_PUBLIC_URL. Unset: no forwarded header is believed, and \
                  behind a reverse proxy its address is recorded. The Control Plane reads the \
                  same variable.",
    example: "10.0.0.0/8,192.168.1.10",
    accepted: crate::services::trusted_proxies::ACCEPTED,
};

// ── database and Redis ─────────────────────────────────────────────────────

pub static DATABASE_URL: EnvVar = EnvVar {
    name: "DATABASE_URL",
    kind: Kind::Url,
    default: None,
    required: true,
    secret: true,
    edition: Edition::Both,
    description: "PostgreSQL connection string of the database the Control Plane created and \
                  migrated. The gateway reads its configuration there and never alters the \
                  schema.",
    example: "postgresql://<user>:<password>@localhost:5432/<database>",
    accepted: "a postgresql:// URL",
};

pub static BRUTOR_GATEWAY_DB_POOL_SIZE: EnvVar = EnvVar {
    name: "BRUTOR_GATEWAY_DB_POOL_SIZE",
    kind: Kind::Integer,
    default: Some("20"),
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Advanced. Most connections the gateway's PostgreSQL pool opens. The Control \
                  Plane reads the same variable to cap its bulk operations against the gateway.",
    example: "50",
    accepted: "a whole number, 1 or more",
};

pub static REDIS_URL: EnvVar = EnvVar {
    name: "REDIS_URL",
    kind: Kind::Url,
    default: None,
    required: true,
    secret: true,
    edition: Edition::Both,
    description: "Redis (or Valkey) the gateway enforces every usage limit on and listens on \
                  for cache invalidation from the Control Plane. The gateway does not start \
                  without it. The Control Plane must use the same Redis.",
    example: "redis://<host>:6379/0",
    accepted: "a redis:// or rediss:// URL",
};

// ── tokens and secrets ─────────────────────────────────────────────────────

pub static JWT_SECRET: EnvVar = EnvVar {
    name: "JWT_SECRET",
    kind: Kind::String,
    default: None,
    required: true,
    secret: true,
    edition: Edition::Both,
    description: "Secret the gateway verifies the Control Plane's tokens with and signs its own \
                  with. Must equal the Control Plane's JWT_SECRET. A value from an example \
                  file is refused.",
    example: "<output of: openssl rand -hex 32>",
    accepted: "at least 32 characters, not a placeholder",
};

pub static JWT_ALGORITHM: EnvVar = EnvVar {
    name: "JWT_ALGORITHM",
    kind: Kind::String,
    default: Some("HS256"),
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Algorithm of the tokens the gateway issues after an MCP OAuth exchange. Must \
                  equal the Control Plane's JWT_ALGORITHM.",
    example: "HS512",
    accepted: "HS256, HS384 or HS512",
};

pub static JWT_EXPIRES_IN_SECS: EnvVar = EnvVar {
    name: "JWT_EXPIRES_IN_SECS",
    kind: Kind::Integer,
    default: Some("3600"),
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Lifetime, in seconds, of the tokens the gateway issues (MCP OAuth exchange; \
                  portal sessions in the Enterprise Edition). The Control Plane reads the same \
                  variable.",
    example: "7200",
    accepted: "a whole number of seconds, 1 or more",
};

pub static ENCRYPTION_KEY: EnvVar = EnvVar {
    name: "ENCRYPTION_KEY",
    kind: Kind::String,
    default: None,
    required: true,
    secret: true,
    edition: Edition::Both,
    description: "Decrypts the credentials the Control Plane stored (provider keys, OAuth \
                  tokens, client secrets) and keys the gateway's own derived secrets. Must \
                  equal the Control Plane's ENCRYPTION_KEY. A value from an example file is \
                  refused.",
    example: "<output of: openssl rand -hex 32>",
    accepted: "at least 32 characters, not a placeholder",
};

// ── proxying ───────────────────────────────────────────────────────────────

pub static BRUTOR_MCP_TIMEOUT_SECS: EnvVar = EnvVar {
    name: "BRUTOR_MCP_TIMEOUT_SECS",
    kind: Kind::Integer,
    default: Some("30"),
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Seconds the gateway waits for an MCP server that has no timeout of its own \
                  configured.",
    example: "60",
    accepted: "a whole number of seconds, 1 or more",
};

pub static BRUTOR_A2A_DELEGATION_MAX_AGE_SECS: EnvVar = EnvVar {
    name: "BRUTOR_A2A_DELEGATION_MAX_AGE_SECS",
    kind: Kind::Integer,
    default: Some("600"),
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Advanced. Oldest parent step, in seconds, an agent-to-agent delegation may \
                  name (plus 60 s for clock skew): an older one is refused as a replay. An \
                  agent that works longer than this on a delegated task before it delegates \
                  further is refused too, so raise it above the longest such task; raising it \
                  widens the replay window by as much.",
    example: "900",
    accepted: "a whole number of seconds, 1 or more",
};

pub static BRUTOR_EMBEDDING_MODEL: EnvVar = EnvVar {
    name: "BRUTOR_EMBEDDING_MODEL",
    kind: Kind::String,
    default: None,
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Name of the embedding model of a tenant that has not chosen one. An agent \
                  needs no grant of its own for the tenant's embedding model. In the \
                  Enterprise Edition it is also what the knowledge base and the response \
                  cache embed with, and must equal the KB Uploader's and the Control Plane's \
                  BRUTOR_EMBEDDING_MODEL. No default.",
    example: "text-embedding-3-small",
    accepted: "a model name in the tenants' catalogs",
};

pub static BRUTOR_JUDGE_MODEL: EnvVar = EnvVar {
    name: "BRUTOR_JUDGE_MODEL",
    kind: Kind::String,
    default: None,
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Name of the model that judges semantic policies for a tenant that has not \
                  chosen one. Without either, a semantic policy has no judge and says so.",
    example: "gpt-4o-mini",
    accepted: "a model name in the tenants' catalogs",
};

// ── media store ────────────────────────────────────────────────────────────

pub static BRUTOR_MEDIA_STORAGE_BACKEND: EnvVar = EnvVar {
    name: "BRUTOR_MEDIA_STORAGE_BACKEND",
    kind: Kind::String,
    default: None,
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Where uploaded and generated files are kept. filesystem: files under \
                  BRUTOR_MEDIA_FILESYSTEM_DIR, for ONE gateway instance; the gateway serves \
                  their signed download links itself at /v1/media/. s3: the bucket named by \
                  BRUTOR_MEDIA_BUCKET, for any number of instances. memory: in the gateway's \
                  process, lost at restart (development and tests). Unset: filesystem when \
                  BRUTOR_MEDIA_FILESYSTEM_DIR is set, memory otherwise.",
    example: "s3",
    accepted: "filesystem, s3 or memory",
};

pub static BRUTOR_MEDIA_FILESYSTEM_DIR: EnvVar = EnvVar {
    name: "BRUTOR_MEDIA_FILESYSTEM_DIR",
    kind: Kind::Path,
    default: None,
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Directory of the filesystem media store: a volume of its own, mounted into \
                  the gateway and backed up with the database. Setting it selects the \
                  filesystem store unless BRUTOR_MEDIA_STORAGE_BACKEND says otherwise. Each \
                  tenant's files are under a directory of their own; names are ids the \
                  gateway mints. The gateway creates the directory and refuses to start when \
                  it cannot write there. One gateway instance only: with several, use s3.",
    example: "/var/lib/brutor/media",
    accepted: "an absolute or relative directory path",
};

pub static BRUTOR_MEDIA_FILESYSTEM_MAX_BYTES: EnvVar = EnvVar {
    name: "BRUTOR_MEDIA_FILESYSTEM_MAX_BYTES",
    kind: Kind::Integer,
    default: Some("10737418240"),
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Size limit of the filesystem media store, in bytes: the total of the stored \
                  files. A write that would pass it is refused (HTTP 507, naming this \
                  variable); the media GC's deletes make room again. Each tenant's own media \
                  quota still applies within it. Default 10 GiB.",
    example: "53687091200",
    accepted: "a whole number of bytes, 1 or more",
};

pub static BRUTOR_MEDIA_BUCKET: EnvVar = EnvVar {
    name: "BRUTOR_MEDIA_BUCKET",
    kind: Kind::String,
    default: None,
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Bucket of the media store. Required when BRUTOR_MEDIA_STORAGE_BACKEND is s3: \
                  the gateway refuses to start without it.",
    example: "brutor-media",
    accepted: "a bucket name",
};

pub static BRUTOR_MEDIA_S3_REGION: EnvVar = EnvVar {
    name: "BRUTOR_MEDIA_S3_REGION",
    kind: Kind::String,
    default: None,
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Region of the media bucket. Unset: AWS_REGION, and us-east-1 when that is \
                  unset too.",
    example: "eu-north-1",
    accepted: "a region name",
};

pub static AWS_REGION: EnvVar = EnvVar {
    name: "AWS_REGION",
    kind: Kind::String,
    default: None,
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "The AWS SDK's region. The gateway itself reads it only as the region of the \
                  media bucket when BRUTOR_MEDIA_S3_REGION is unset.",
    example: "eu-north-1",
    accepted: "a region name",
};

pub static BRUTOR_MEDIA_S3_ENDPOINT: EnvVar = EnvVar {
    name: "BRUTOR_MEDIA_S3_ENDPOINT",
    kind: Kind::Url,
    default: None,
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Endpoint of an S3-compatible store (MinIO, R2) as the gateway reaches it. \
                  Unset: AWS S3 in the region.",
    example: "http://minio:9000",
    accepted: "an http:// or https:// URL",
};

pub static BRUTOR_MEDIA_S3_PUBLIC_ENDPOINT: EnvVar = EnvVar {
    name: "BRUTOR_MEDIA_S3_PUBLIC_ENDPOINT",
    kind: Kind::Url,
    default: None,
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Endpoint of the same store as a browser reaches it, used only in the signed \
                  download URLs the gateway hands out. Set it when that differs from \
                  BRUTOR_MEDIA_S3_ENDPOINT (a store on a container network). Unset: the \
                  endpoint the gateway uses.",
    example: "http://localhost:9000",
    accepted: "an http:// or https:// URL",
};

pub static BRUTOR_MEDIA_S3_FORCE_PATH_STYLE_ENABLED: EnvVar = EnvVar {
    name: "BRUTOR_MEDIA_S3_FORCE_PATH_STYLE_ENABLED",
    kind: Kind::Boolean,
    default: Some("true"),
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Addresses the bucket as a path (endpoint/bucket), which MinIO and most \
                  S3-compatible stores need. Set false for AWS S3.",
    example: "false",
    accepted: "true, false, 1, 0, yes, no, on, off",
};

pub static BRUTOR_MEDIA_S3_ACCESS_KEY_ID: EnvVar = EnvVar {
    name: "BRUTOR_MEDIA_S3_ACCESS_KEY_ID",
    kind: Kind::String,
    default: None,
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Access key of the media store. Set it together with \
                  BRUTOR_MEDIA_S3_SECRET_ACCESS_KEY, or set neither and the AWS SDK's own \
                  credential chain is used (an instance or task role).",
    example: "brutor-media",
    accepted: "an access key id",
};

pub static BRUTOR_MEDIA_S3_SECRET_ACCESS_KEY: EnvVar = EnvVar {
    name: "BRUTOR_MEDIA_S3_SECRET_ACCESS_KEY",
    kind: Kind::String,
    default: None,
    required: false,
    secret: true,
    edition: Edition::Both,
    description: "Secret key of the media store. See BRUTOR_MEDIA_S3_ACCESS_KEY_ID.",
    example: "<the secret key of the media store>",
    accepted: "a secret access key",
};

pub static BRUTOR_MEDIA_SIGNED_URL_TTL_SECS: EnvVar = EnvVar {
    name: "BRUTOR_MEDIA_SIGNED_URL_TTL_SECS",
    kind: Kind::Integer,
    default: Some("900"),
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Lifetime, in seconds, of a signed download URL. Never less than 60.",
    example: "300",
    accepted: "a whole number of seconds; below 60 is 60",
};

// ── skill runner ───────────────────────────────────────────────────────────

pub static BRUTOR_SKILL_RUNNER_URL: EnvVar = EnvVar {
    name: "BRUTOR_SKILL_RUNNER_URL",
    kind: Kind::Url,
    default: Some("http://skill-runner:8210"),
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Base URL of the Skill Runner, which runs skill scripts for the gateway. It \
                  must point at the runner's BRUTOR_SKILL_RUNNER_LISTEN_ADDR.",
    example: "http://localhost:8210",
    accepted: "an http:// or https:// URL",
};

pub static BRUTOR_SKILL_RUNNER_JWT_SECRET: EnvVar = EnvVar {
    name: "BRUTOR_SKILL_RUNNER_JWT_SECRET",
    kind: Kind::String,
    default: None,
    required: false,
    secret: true,
    edition: Edition::Both,
    description: "Secret the gateway signs each execution token for the Skill Runner with. Must \
                  equal the runner's BRUTOR_SKILL_RUNNER_JWT_SECRET. Unset: the gateway starts, \
                  and every skill execution fails with a message that names this variable.",
    example: "<output of: openssl rand -hex 32>",
    accepted: "at least 32 characters (the runner refuses a shorter one)",
};

pub static BRUTOR_SKILL_RUNNER_SANDBOX_TIMEOUT_SECS: EnvVar = EnvVar {
    name: "BRUTOR_SKILL_RUNNER_SANDBOX_TIMEOUT_SECS",
    kind: Kind::Integer,
    default: Some("30"),
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Advanced. Wall-clock limit, in seconds, of a skill script in SANDBOX mode. The \
                  Skill Runner enforces it; the gateway reads the same variable to wait for a \
                  run that long plus a margin. Set the same value on both.",
    example: "60",
    accepted: "a whole number of seconds, 1 or more",
};

pub static BRUTOR_SKILL_RUNNER_TRUSTED_TIMEOUT_SECS: EnvVar = EnvVar {
    name: "BRUTOR_SKILL_RUNNER_TRUSTED_TIMEOUT_SECS",
    kind: Kind::Integer,
    default: Some("120"),
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Advanced. Wall-clock limit, in seconds, of a skill script in TRUSTED mode. The \
                  Skill Runner enforces it; the gateway reads the same variable to wait for a \
                  run that long plus a margin. Set the same value on both.",
    example: "300",
    accepted: "a whole number of seconds, 1 or more",
};

// ── outbound policy and autonomy ───────────────────────────────────────────

pub static BRUTOR_OUTBOUND_ALLOW_HOSTS: EnvVar = EnvVar {
    name: "BRUTOR_OUTBOUND_ALLOW_HOSTS",
    kind: Kind::List,
    default: None,
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Outbound policy. Destinations of MCP servers, model endpoints, agents, IdPs, \
                  webhooks and witnesses at a private address (loopback, RFC 1918, carrier-grade \
                  NAT, IPv6 unique-local, and a name that resolves to one) are refused unless \
                  listed here; link-local and cloud metadata addresses are refused whatever this \
                  says. A host name allows every address it resolves to, *.domain the domain and \
                  its subdomains, an address or CIDR range those addresses. The Control Plane and \
                  KB Connector Sync read the same variable.",
    example: "mcp.internal,*.corp.example,10.20.0.0/16,ollama",
    accepted: "comma-separated host names, *.domain, IP addresses and CIDR ranges (no port, \
               scheme or path)",
};

pub static BRUTOR_AUTONOMY_FALLBACK_FLOOR: EnvVar = EnvVar {
    name: "BRUTOR_AUTONOMY_FALLBACK_FLOOR",
    kind: Kind::String,
    default: Some("restricted"),
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Autonomy level an agent runs at while the governance database cannot be read \
                  and the gateway has no recent level for it. restricted keeps reads and chat \
                  serving and holds everything that changes the world; suspended stops the \
                  agent.",
    example: "suspended",
    accepted: "autonomous, approval_required, restricted or suspended",
};

// ── audit chain and evidence ───────────────────────────────────────────────

pub static BRUTOR_AUDIT_SIGNING_KEY: EnvVar = EnvVar {
    name: "BRUTOR_AUDIT_SIGNING_KEY",
    kind: Kind::String,
    default: None,
    required: false,
    secret: true,
    edition: Edition::Both,
    description: "Ed25519 seed the gateway signs audit-chain checkpoints with. Unset: the rows \
                  are still hash-chained, and no checkpoint is signed.",
    example: "<output of: openssl rand -hex 32>",
    accepted: "exactly 64 hexadecimal characters (32 bytes)",
};

pub static BRUTOR_AUDIT_TRUSTED_KEYS: EnvVar = EnvVar {
    name: "BRUTOR_AUDIT_TRUSTED_KEYS",
    kind: Kind::List,
    default: None,
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Public keys, besides the gateway's own, whose audit-chain checkpoints verify: \
                  the keys of other and of earlier instances.",
    example: "3f9a0c1e5b7d2f4a6c8e0b1d3f5a7c9e1b3d5f7a9c0e2b4d6f8a1c3e5b7d9f0a",
    accepted: "comma-separated Ed25519 public keys, 64 hexadecimal characters each",
};

pub static BRUTOR_EVIDENCE_SIGNING_KEY: EnvVar = EnvVar {
    name: "BRUTOR_EVIDENCE_SIGNING_KEY",
    kind: Kind::String,
    default: None,
    required: false,
    secret: true,
    edition: Edition::Both,
    description: "Ed25519 seed of the deployment's evidence key: every governed decision is \
                  sealed as a signed statement with it. Unset: no record is sealed, traffic is \
                  unaffected, and the console says so. Before replacing it, add its public key \
                  to BRUTOR_EVIDENCE_RETIRED_KEYS.",
    example: "<output of: openssl rand -hex 32>",
    accepted: "exactly 64 hexadecimal characters (32 bytes)",
};

pub static BRUTOR_EVIDENCE_RETIRED_KEYS: EnvVar = EnvVar {
    name: "BRUTOR_EVIDENCE_RETIRED_KEYS",
    kind: Kind::List,
    default: None,
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Public keys of earlier deployment evidence keys. Still published and trusted \
                  for verification; nothing is signed with them.",
    example: "3f9a0c1e5b7d2f4a6c8e0b1d3f5a7c9e1b3d5f7a9c0e2b4d6f8a1c3e5b7d9f0a",
    accepted: "comma-separated Ed25519 public keys, 64 hexadecimal characters each",
};

pub static BRUTOR_DEPLOYMENT_ID: EnvVar = EnvVar {
    name: "BRUTOR_DEPLOYMENT_ID",
    kind: Kind::String,
    default: Some("default"),
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Stable identity of this deployment. It is hashed into the identifier of every \
                  tenant's evidence log: set it once and never change it, a new value starts \
                  new logs.",
    example: "acme-prod",
    accepted: "any text",
};

pub static BRUTOR_EVIDENCE_ISSUER: EnvVar = EnvVar {
    name: "BRUTOR_EVIDENCE_ISSUER",
    kind: Kind::String,
    default: None,
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Issuer written into every signed statement. Unset: \
                  urn:brutor:deployment:<BRUTOR_DEPLOYMENT_ID>. A deployment that publishes a \
                  did:web sets it here.",
    example: "did:web:gateway.example.com",
    accepted: "a URI",
};

pub static BRUTOR_EVIDENCE_HEAD_CADENCE_ENTRIES: EnvVar = EnvVar {
    name: "BRUTOR_EVIDENCE_HEAD_CADENCE_ENTRIES",
    kind: Kind::Integer,
    default: Some("100"),
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "A signed tree head is issued for a tenant's evidence log once this many \
                  entries are not yet covered by one.",
    example: "500",
    accepted: "a whole number, 1 or more",
};

pub static BRUTOR_EVIDENCE_HEAD_CADENCE_SECS: EnvVar = EnvVar {
    name: "BRUTOR_EVIDENCE_HEAD_CADENCE_SECS",
    kind: Kind::Integer,
    default: Some("900"),
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "A signed tree head is issued once the oldest entry not yet covered by one is \
                  this many seconds old. Checked when an entry is appended: an idle log issues \
                  none.",
    example: "300",
    accepted: "a whole number of seconds, 1 or more",
};

pub static BRUTOR_EVIDENCE_WITNESS_ENABLED: EnvVar = EnvVar {
    name: "BRUTOR_EVIDENCE_WITNESS_ENABLED",
    kind: Kind::Boolean,
    default: Some("true"),
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Registers signed tree heads with the tenants' witnesses. An incident kill \
                  switch: false stops every call to a witness and traffic is unaffected; each \
                  head issued meanwhile is recorded as unwitnessed (a receipt row with verdict \
                  skipped per witness, naming this variable, and a warning in the log), so a \
                  verifier sees standalone heads for that period. Back to true, the retry loop \
                  submits those heads (up to 30 days old).",
    example: "false",
    accepted: "true, false, 1, 0, yes, no, on, off",
};

pub static BRUTOR_EVIDENCE_PROFILE_AAC_ENABLED: EnvVar = EnvVar {
    name: "BRUTOR_EVIDENCE_PROFILE_AAC_ENABLED",
    kind: Kind::Boolean,
    default: Some("false"),
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Experimental. Serves the Agent Action Capsule export of evidence records, \
                  which follows an individual Internet-Draft: its format, its route and this \
                  setting may change or be removed in any release, and a capsule is not \
                  covered by the evidence compatibility promise (records and bundles are). \
                  Off: that route answers 404.",
    example: "true",
    accepted: "true, false, 1, 0, yes, no, on, off",
};

// ── telemetry ──────────────────────────────────────────────────────────────

pub static OTEL_EXPORTER_OTLP_ENDPOINT: EnvVar = EnvVar {
    name: "OTEL_EXPORTER_OTLP_ENDPOINT",
    kind: Kind::String,
    default: None,
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "OTLP endpoint (gRPC) traces are exported to. Unset: no trace is exported. The \
                  OpenTelemetry SDK reads its other OTEL_* variables itself.",
    example: "http://otel-collector:4317",
    accepted: "an OTLP endpoint URL",
};

pub static OTEL_SERVICE_NAME: EnvVar = EnvVar {
    name: "OTEL_SERVICE_NAME",
    kind: Kind::String,
    default: Some("brutor-gateway-core"),
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "service.name of the exported traces.",
    example: "brutor-gateway-core-eu",
    accepted: "a service name",
};

pub static BRUTOR_LOG_JSON_ENABLED: EnvVar = EnvVar {
    name: "BRUTOR_LOG_JSON_ENABLED",
    kind: Kind::Boolean,
    default: Some("false"),
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Writes the log as JSON lines, one object per event, for a log collector. \
                  Off: human-readable lines. RUST_LOG still chooses what is logged.",
    example: "true",
    accepted: "true, false, 1, 0, yes, no, on, off",
};

pub static RUST_LOG: EnvVar = EnvVar {
    name: "RUST_LOG",
    kind: Kind::String,
    default: Some("brutor_gateway_core=info,tower_http=info"),
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Log filter. The gateway does not start with a filter that does not parse.",
    example: "brutor_gateway_core=debug,tower_http=info",
    accepted: "tracing filter directives (target=level, comma-separated)",
};

/// The Community gateway's variables.
pub static REGISTRY: Registry = Registry {
    service: "core",
    title: "Brutor Agent Control, Core Gateway (Community Edition)",
    vars: &[
        &DATABASE_URL,
        &REDIS_URL,
        &JWT_SECRET,
        &ENCRYPTION_KEY,
        &BRUTOR_GATEWAY_LISTEN_ADDR,
        &BRUTOR_GATEWAY_PUBLIC_URL,
        &BRUTOR_CORS_ORIGINS,
        &BRUTOR_TRUSTED_PROXIES,
        &JWT_ALGORITHM,
        &JWT_EXPIRES_IN_SECS,
        &BRUTOR_GATEWAY_DB_POOL_SIZE,
        &BRUTOR_MCP_TIMEOUT_SECS,
        &BRUTOR_A2A_DELEGATION_MAX_AGE_SECS,
        &BRUTOR_EMBEDDING_MODEL,
        &BRUTOR_JUDGE_MODEL,
        &BRUTOR_MEDIA_STORAGE_BACKEND,
        &BRUTOR_MEDIA_BUCKET,
        &BRUTOR_MEDIA_S3_REGION,
        &AWS_REGION,
        &BRUTOR_MEDIA_S3_ENDPOINT,
        &BRUTOR_MEDIA_S3_PUBLIC_ENDPOINT,
        &BRUTOR_MEDIA_S3_FORCE_PATH_STYLE_ENABLED,
        &BRUTOR_MEDIA_S3_ACCESS_KEY_ID,
        &BRUTOR_MEDIA_S3_SECRET_ACCESS_KEY,
        &BRUTOR_MEDIA_SIGNED_URL_TTL_SECS,
        &BRUTOR_MEDIA_FILESYSTEM_DIR,
        &BRUTOR_MEDIA_FILESYSTEM_MAX_BYTES,
        &BRUTOR_SKILL_RUNNER_URL,
        &BRUTOR_SKILL_RUNNER_JWT_SECRET,
        &BRUTOR_SKILL_RUNNER_SANDBOX_TIMEOUT_SECS,
        &BRUTOR_SKILL_RUNNER_TRUSTED_TIMEOUT_SECS,
        &BRUTOR_OUTBOUND_ALLOW_HOSTS,
        &BRUTOR_AUTONOMY_FALLBACK_FLOOR,
        &BRUTOR_AUDIT_SIGNING_KEY,
        &BRUTOR_AUDIT_TRUSTED_KEYS,
        &BRUTOR_EVIDENCE_SIGNING_KEY,
        &BRUTOR_EVIDENCE_RETIRED_KEYS,
        &BRUTOR_DEPLOYMENT_ID,
        &BRUTOR_EVIDENCE_ISSUER,
        &BRUTOR_EVIDENCE_HEAD_CADENCE_ENTRIES,
        &BRUTOR_EVIDENCE_HEAD_CADENCE_SECS,
        &BRUTOR_EVIDENCE_WITNESS_ENABLED,
        &BRUTOR_EVIDENCE_PROFILE_AAC_ENABLED,
        &BRUTOR_METRICS_ENABLED,
        &BRUTOR_METRICS_LISTEN_ADDR,
        &OTEL_EXPORTER_OTLP_ENDPOINT,
        &OTEL_SERVICE_NAME,
        &BRUTOR_LOG_JSON_ENABLED,
        &RUST_LOG,
    ],
};

/// The prefix of the product's own variables.
pub const PRODUCT_PREFIX: &str = "BRUTOR_";

// ── the registry of a distribution ─────────────────────────────────────────

/// `base` and the variables `vars`: the registry of a composition that
/// reads more than `base` does.
///
/// The result lives for the rest of the process: call it once per
/// composition and keep it.
pub fn extend(
    base: &'static Registry,
    title: &'static str,
    vars: &[&'static EnvVar],
) -> &'static Registry {
    let mut all_vars: Vec<&'static EnvVar> = base.vars.to_vec();
    for var in vars {
        if !all_vars.iter().any(|known| known.name == var.name) {
            all_vars.push(var);
        }
    }
    Box::leak(Box::new(Registry {
        service: base.service,
        title,
        vars: Box::leak(all_vars.into_boxed_slice()),
    }))
}

/// The registry of a distribution: the Community variables and those its
/// extensions declare (`Extension::env_vars`). Without an extension that
/// declares one it is [`REGISTRY`] itself.
pub fn assemble(
    title: &'static str,
    extensions: &[&dyn crate::builder::Extension],
) -> &'static Registry {
    let vars: Vec<&'static EnvVar> = extensions
        .iter()
        .flat_map(|extension| extension.env_vars().iter().copied())
        .collect();
    if vars.is_empty() {
        return &REGISTRY;
    }
    extend(&REGISTRY, title, &vars)
}

// ── the process environment ────────────────────────────────────────────────

/// The environment of this process, and the registry it was read through.
static PROCESS: RwLock<Option<(&'static Registry, Arc<Env>)>> = RwLock::new(None);

fn installed() -> Option<(&'static Registry, Arc<Env>)> {
    PROCESS
        .read()
        .unwrap_or_else(PoisonError::into_inner)
        .clone()
}

fn install(registry: &'static Registry, env: Env) -> Arc<Env> {
    let env = Arc::new(env);
    *PROCESS.write().unwrap_or_else(PoisonError::into_inner) = Some((registry, Arc::clone(&env)));
    env
}

/// Read the process environment through `registry` where no start did.
///
/// # Panics
/// When the environment is refused. The message is the refusal.
fn read_unstarted(registry: &'static Registry) -> (&'static Registry, Arc<Env>) {
    let env = Env::from_process(registry)
        .and_then(|env| crate::config::check(&env).map(|()| env))
        .unwrap_or_else(|refusal| panic!("{refusal}"));
    (registry, install(registry, env))
}

/// The environment of this process.
///
/// A binary reads it first thing ([`load`], through the builder) and stops
/// on a refusal. Where nothing did (a test that composes the gateway in its
/// own process) it is read at the first call, through [`REGISTRY`], under
/// the same rules.
///
/// # Panics
/// When nothing loaded it and the environment is refused: a value is
/// malformed. The message is the refusal.
pub fn process() -> Arc<Env> {
    installed().unwrap_or_else(|| read_unstarted(&REGISTRY)).1
}

/// [`process`] for an extension, which reads variables of its own: `vars`
/// (what its `Extension::env_vars` declares).
///
/// In a gateway the builder started, the environment was read through the
/// composition's registry, which holds them. Where nothing started (a test
/// that composes extensions in its own process), the registry the
/// environment is read through grows by them, under the same rules.
///
/// # Panics
/// As [`process`].
pub fn process_with(vars: &'static [&'static EnvVar]) -> Arc<Env> {
    let (registry, env) = installed().unwrap_or_else(|| read_unstarted(&REGISTRY));
    if vars.iter().all(|var| registry.find(var.name).is_some()) {
        return env;
    }
    read_unstarted(extend(registry, registry.title, vars)).1
}

/// Read the process environment for a start, through `registry` (the
/// composition's: [`assemble`]): a malformed
/// value and a missing required variable are each refused, every one found,
/// one line per name and never a value. On success the result is what
/// [`process`] returns from then on. `config::load` is what a binary calls,
/// through the builder: it also refuses a value that is out of range.
pub fn load(registry: &'static Registry) -> Result<Arc<Env>, EnvError> {
    Ok(install(registry, Env::load(registry)?))
}

/// [`load`] for a tool that needs only some of the Community gateway's
/// variables: a missing required variable is refused by the accessor that
/// needs it.
pub fn load_partial() -> Result<Arc<Env>, EnvError> {
    let env = Env::from_process(&REGISTRY)?;
    crate::config::check(&env)?;
    Ok(install(&REGISTRY, env))
}

/// Read the process environment again. For a test that changed it: the
/// gateway reads its environment once.
#[cfg(any(test, feature = "db-tests"))]
pub fn reload_for_tests() {
    let registry = installed().map_or(&REGISTRY, |(registry, _)| registry);
    let _ = read_unstarted(registry);
}

// ── reading ────────────────────────────────────────────────────────────────

/// A refusal of `var`: what it must be, never what it was.
pub fn invalid(var: &'static EnvVar, expected: impl Into<String>) -> EnvError {
    EnvError(vec![Problem::Invalid {
        name: var.name,
        expected: expected.into(),
    }])
}

/// A number of `var`'s type that is 1 or more.
pub fn positive<T>(env: &Env, var: &'static EnvVar) -> Result<T, EnvError>
where
    T: FromStr + PartialOrd + Default,
{
    let value: T = env.number(var)?;
    if value > T::default() {
        Ok(value)
    } else {
        Err(invalid(var, format!("{} (it is 0)", var.accepted)))
    }
}

/// An Ed25519 key or seed: 32 bytes written as 64 hexadecimal characters.
pub fn hex_32(raw: &str) -> Option<[u8; 32]> {
    let raw = raw.trim();
    if raw.len() != 64 {
        return None;
    }
    hex::decode(raw).ok()?.try_into().ok()
}

/// Every problem of `results`, as one refusal.
pub fn all_of(results: impl IntoIterator<Item = Result<(), EnvError>>) -> Result<(), EnvError> {
    let problems: Vec<Problem> = results
        .into_iter()
        .filter_map(Result::err)
        .flat_map(|refusal| refusal.0)
        .collect();
    if problems.is_empty() {
        Ok(())
    } else {
        Err(EnvError(problems))
    }
}

// ── variables this gateway does not read ───────────────────────────────────

/// Whether `name` is one a container platform sets for every service of a
/// namespace (Kubernetes service links, Docker links): `<SERVICE>_PORT`,
/// `<SERVICE>_SERVICE_HOST`, `<SERVICE>_PORT_8100_TCP_ADDR` and the like. A
/// service called `brutor-…` gives every pod such `BRUTOR_…` variables.
pub fn is_service_link(name: &str) -> bool {
    if name.ends_with("_SERVICE_HOST") || name.ends_with("_SERVICE_PORT") || name.ends_with("_PORT")
    {
        return true;
    }
    if name.contains("_SERVICE_PORT_") {
        return true;
    }
    // `_PORT_<number>_<TCP|UDP|SCTP>[_PROTO|_PORT|_ADDR]`
    name.match_indices("_PORT_").any(|(at, _)| {
        let rest = &name[at + "_PORT_".len()..];
        let digits = rest.bytes().take_while(u8::is_ascii_digit).count();
        digits > 0
            && ["_TCP", "_UDP", "_SCTP"].iter().any(|protocol| {
                rest[digits..]
                    .strip_prefix(protocol)
                    .is_some_and(|tail| matches!(tail, "" | "_PROTO" | "_PORT" | "_ADDR"))
            })
    })
}

/// The product's variables (`BRUTOR_*`) that are set in this process and
/// that `registry` does not know: a misspelt name, a variable of another
/// service, or one of an edition this binary is not. Service links are left
/// out ([`is_service_link`]).
pub fn unread_product_variables(registry: &Registry) -> Vec<String> {
    registry
        .unregistered_in_process(PRODUCT_PREFIX)
        .into_iter()
        .filter(|name| !is_service_link(name))
        .collect()
}

/// Say, once at start, which product variables are set and not read. They
/// are not refused: a container platform may set `BRUTOR_*` names of its
/// own, and no list of them is complete.
pub fn warn_of_unread_product_variables(registry: &Registry, edition: &str) {
    let unread = unread_product_variables(registry);
    if !unread.is_empty() {
        tracing::warn!(
            variables = %unread.join(", "),
            "these BRUTOR_* variables are set and this gateway ({edition}) does not read them: \
             a misspelt name, a variable of another service or of another edition. They have \
             no effect here"
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn service_links_are_recognised_and_settings_are_not() {
        for link in [
            "BRUTOR_GATEWAY_CORE_SERVICE_HOST",
            "BRUTOR_GATEWAY_CORE_SERVICE_PORT",
            "BRUTOR_GATEWAY_CORE_SERVICE_PORT_HTTP",
            "BRUTOR_GATEWAY_CORE_PORT",
            "BRUTOR_GATEWAY_CORE_PORT_8100_TCP",
            "BRUTOR_GATEWAY_CORE_PORT_8100_TCP_PROTO",
            "BRUTOR_GATEWAY_CORE_PORT_8100_TCP_PORT",
            "BRUTOR_GATEWAY_CORE_PORT_8100_TCP_ADDR",
        ] {
            assert!(is_service_link(link), "{link}");
        }
        for setting in [
            "BRUTOR_GATEWAY_LISTEN_ADDR",
            "BRUTOR_OUTBOUND_DENY_PRIVATE_ENABLE",
            "BRUTOR_PORT_FORWARDING",
            "BRUTOR_X_PORT_TCP_ADDR",
        ] {
            assert!(!is_service_link(setting), "{setting}");
        }
    }

    #[test]
    fn a_number_that_must_be_positive_refuses_zero_without_printing_it() {
        let env = Env::from_pairs(&REGISTRY, [("BRUTOR_GATEWAY_DB_POOL_SIZE", "0")]).unwrap();
        let refusal = positive::<u32>(&env, &BRUTOR_GATEWAY_DB_POOL_SIZE)
            .unwrap_err()
            .to_string();
        assert!(
            refusal.contains("BRUTOR_GATEWAY_DB_POOL_SIZE is not valid"),
            "{refusal}"
        );
        let env = Env::from_pairs(&REGISTRY, [("BRUTOR_GATEWAY_DB_POOL_SIZE", "7")]).unwrap();
        assert_eq!(positive::<u32>(&env, &BRUTOR_GATEWAY_DB_POOL_SIZE), Ok(7));
    }

    #[test]
    fn a_key_is_64_hexadecimal_characters() {
        assert!(hex_32(&"ab".repeat(32)).is_some());
        assert!(hex_32(&format!(" {} ", "ab".repeat(32))).is_some());
        assert!(hex_32(&"ab".repeat(31)).is_none());
        assert!(hex_32(&"zz".repeat(32)).is_none());
        assert!(hex_32("").is_none());
    }
}
