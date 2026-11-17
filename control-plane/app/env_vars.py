# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The environment variables of the control plane, Community Edition: each
defined once, here, and read through its definition.

`app/env_registry.py` says what a definition is and what follows from it
(the refusal of malformed values and missing required ones at start,
`.env.example`, the configuration tables, `--print-env`). The Enterprise
Edition's variables are defined in its own package and join the registry
through its extension.

A name is the product's own when it starts with `BRUTOR_`. The others are
names that belong to something else and are kept as that something spells
them: a library's or a service's (`DATABASE_URL`, `REDIS_URL`), the two
secrets every component of an installation shares (`JWT_SECRET`,
`ENCRYPTION_KEY`, with `JWT_ALGORITHM` beside them), and Gunicorn's number
of workers (`GUNICORN_WORKERS`, read by `gunicorn.conf.py`; every other
Gunicorn setting is fixed there).
"""

from __future__ import annotations

import json
from typing import Optional

from app.env_registry import (
    BOOLEAN,
    INTEGER,
    LIST,
    REGISTRY,
    STRING,
    URL,
    EnvVar,
)

_BOOLEAN = "true, false, 1, 0, yes, no, on, off"
_SECRET = "<output of: openssl rand -hex 32>"


def _json_array_of_objects(raw: str) -> Optional[str]:
    try:
        parsed = json.loads(raw)
    except ValueError:
        return "a JSON array of objects"
    if not isinstance(parsed, list) or not all(isinstance(entry, dict) for entry in parsed):
        return "a JSON array of objects"
    return None


def _http_url(raw: str) -> Optional[str]:
    return None if raw.strip().lower().startswith(("http://", "https://")) else "an http:// or https:// URL"


_HOST_PORT = "host:port (an IPv6 address in brackets, [::]:5050), port 1 to 65535"


def split_listen_addr(raw: str) -> Optional[tuple]:
    """`(host, port)` of a `host:port` listen address (an IPv6 host in
    brackets, returned without them); None when `raw` is not one."""
    host, separator, port = raw.strip().rpartition(":")
    if not separator or not port.isascii() or not port.isdigit() or not 1 <= int(port) <= 65535:
        return None
    if host.startswith("[") or host.endswith("]"):
        if not (host.startswith("[") and host.endswith("]")) or len(host) < 3:
            return None
        host = host[1:-1]
    elif ":" in host:
        return None
    if not host or any(c.isspace() or c in "/[]" for c in host):
        return None
    return host, int(port)


def _listen_addr(raw: str) -> Optional[str]:
    return None if split_listen_addr(raw) else _HOST_PORT


# --- the database, Redis and the two shared secrets --------------------------------

DATABASE_URL = EnvVar(
    name="DATABASE_URL",
    kind=URL,
    required=True,
    secret=True,
    description=(
        "PostgreSQL connection string of the installation's database. The Control Plane creates "
        "and migrates the schema there at start; the Core Gateway and the worker services read "
        "the same database. TLS towards PostgreSQL is set in the URL with libpq's own "
        "parameters (`?sslmode=verify-full&sslrootcert=/path/root.crt`, and `sslcert`, "
        "`sslkey` for a client certificate), as for the Core Gateway; the Control Plane "
        "never replaces them."
    ),
    example="postgresql://<user>:<password>@localhost:5432/<database>",
    accepted="a postgresql:// URL",
)

REDIS_URL = EnvVar(
    name="REDIS_URL",
    kind=URL,
    secret=True,
    description=(
        "Redis (or Valkey) the Control Plane publishes cache invalidations on, so that the Core "
        "Gateway applies a configuration change at once. Must be the gateway's Redis. Unset: "
        "nothing is published and the gateway picks changes up when its caches expire."
    ),
    example="redis://<host>:6379/0",
    accepted="a redis:// or rediss:// URL",
)

JWT_SECRET = EnvVar(
    name="JWT_SECRET",
    kind=STRING,
    required=True,
    secret=True,
    description=(
        "Secret the Control Plane signs administrator sessions with. Must equal the Core "
        "Gateway's JWT_SECRET. A value from an example file is refused."
    ),
    example=_SECRET,
    accepted="at least 32 characters, not a placeholder",
)

JWT_ALGORITHM = EnvVar(
    name="JWT_ALGORITHM",
    kind=STRING,
    default="HS256",
    choices=("HS256", "HS384", "HS512"),
    description=(
        "Algorithm of the tokens signed with JWT_SECRET. Must equal the Core Gateway's "
        "JWT_ALGORITHM."
    ),
    example="HS512",
    accepted="HS256, HS384 or HS512",
)

ENCRYPTION_KEY = EnvVar(
    name="ENCRYPTION_KEY",
    kind=STRING,
    required=True,
    secret=True,
    description=(
        "Encrypts the credentials the Control Plane stores (provider keys, MCP server tokens, "
        "OAuth client secrets). Must equal the Core Gateway's ENCRYPTION_KEY, which decrypts "
        "them. A value from an example file is refused."
    ),
    example=_SECRET,
    accepted="at least 32 characters, not a placeholder",
)

# --- the server -------------------------------------------------------------------------

BRUTOR_CONTROL_PLANE_LISTEN_ADDR = EnvVar(
    name="BRUTOR_CONTROL_PLANE_LISTEN_ADDR",
    kind=STRING,
    default="0.0.0.0:5050",
    description=(
        "Address the Control Plane's HTTP server binds, under Gunicorn (every image) and under "
        "`python main.py` (where --listen-addr takes precedence)."
    ),
    example="127.0.0.1:5050",
    accepted=_HOST_PORT,
    check=_listen_addr,
)

BRUTOR_CONTROL_PLANE_PUBLIC_URL = EnvVar(
    name="BRUTOR_CONTROL_PLANE_PUBLIC_URL",
    kind=URL,
    description=(
        "Base URL of the Control Plane as a browser reaches it. Reported by GET / and in the "
        "start banner. The Admin Console is given the same value under the same name. Unset: "
        "http:// and BRUTOR_CONTROL_PLANE_LISTEN_ADDR."
    ),
    example="https://control.example.com",
    accepted="an http:// or https:// URL",
    check=_http_url,
)

BRUTOR_VERSION = EnvVar(
    name="BRUTOR_VERSION",
    kind=STRING,
    default="dev",
    description=(
        "Version the Control Plane reports (GET /, the OpenAPI document) and records with what "
        "it measures. The images set it at build time; the packages set it to the version of "
        "the package."
    ),
    example="1.4.0",
    accepted="a version string",
)

def log_level_name(raw: str) -> str:
    """The stored spelling of a log level: upper case, and `WARN` (the
    Rust services' spelling, which the packages set for every service)
    as `WARNING`."""
    level = raw.strip().upper()
    return "WARNING" if level == "WARN" else level


BRUTOR_LOG_LEVEL = EnvVar(
    name="BRUTOR_LOG_LEVEL",
    kind=STRING,
    default="INFO",
    choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
    normalize=log_level_name,
    description=(
        "Lowest level the Control Plane logs, under Gunicorn and under `python main.py` (where "
        "--log-level takes precedence). Gunicorn's own messages follow the same level. `warn` "
        "and `warning` are the same level, so that one value serves the Python and the Rust "
        "services of a package."
    ),
    example="DEBUG",
    accepted="DEBUG, INFO, WARN or WARNING, ERROR or CRITICAL (any case)",
)

BRUTOR_LOG_JSON_ENABLED = EnvVar(
    name="BRUTOR_LOG_JSON_ENABLED",
    kind=BOOLEAN,
    default="false",
    description=(
        "Writes the Control Plane's log as one JSON object per line instead of text, under "
        "Gunicorn and under `python main.py` (where --json-logs also switches it on)."
    ),
    example="true",
    accepted=_BOOLEAN,
)

# --- CORS -------------------------------------------------------------------------------

BRUTOR_CORS_ORIGINS = EnvVar(
    name="BRUTOR_CORS_ORIGINS",
    kind=LIST,
    default="http://localhost:3001,http://localhost:3002",
    description=(
        "Origins that may call the admin API from a browser: the Admin Console's. The Core "
        "Gateway reads the same variable."
    ),
    example="https://admin.example.com,https://portal.example.com",
    accepted="comma-separated origins (scheme://host[:port])",
)

# --- reverse proxies --------------------------------------------------------------------

def _check_trusted_proxies(raw: str):
    from app.middleware.trusted_proxies import check

    return check(raw)


BRUTOR_TRUSTED_PROXIES = EnvVar(
    name="BRUTOR_TRUSTED_PROXIES",
    kind=LIST,
    check=_check_trusted_proxies,
    empty_unsets=True,
    description=(
        "Reverse proxies whose forwarded headers are believed. A request's client IP (audit "
        "rows, sessions, dynamic client registration) is the connecting address unless that "
        "address is listed; then it is the right-most X-Forwarded-For address that is not "
        "itself listed. X-Forwarded-Proto and X-Forwarded-Host are used only from a listed "
        "proxy. Unset: no forwarded header is believed, and behind a proxy the proxy's address "
        "is recorded. The Core Gateway reads the same variable."
    ),
    example="10.0.0.0/8,192.168.1.10",
    accepted="comma-separated IP addresses and CIDR ranges (no host names, no *)",
)

# --- the database engine ------------------------------------------------------------------

BRUTOR_CONTROL_PLANE_DB_POOL_SIZE = EnvVar(
    name="BRUTOR_CONTROL_PLANE_DB_POOL_SIZE",
    kind=INTEGER,
    default="20",
    minimum=1,
    description=(
        "Advanced. Connections each Control Plane worker keeps open to PostgreSQL; under load "
        "a worker opens as many again, closed when idle. With GUNICORN_WORKERS it bounds the "
        "connections the Control Plane can hold: workers x 2 x pool size, which must stay "
        "below PostgreSQL's max_connections together with the Core Gateway's pool."
    ),
    example="10",
    accepted="a whole number, 1 or more",
)

BRUTOR_DB_COMMAND_TIMEOUT_SECS = EnvVar(
    name="BRUTOR_DB_COMMAND_TIMEOUT_SECS",
    kind=INTEGER,
    default="60",
    minimum=1,
    description=(
        "Advanced. Seconds a statement may run before PostgreSQL cancels it (statement_timeout). "
        "Migrations at start are not bound by it."
    ),
    example="120",
    accepted="a whole number of seconds, 1 or more",
)

# --- the Core Gateway ------------------------------------------------------------------------

BRUTOR_GATEWAY_INTERNAL_URL = EnvVar(
    name="BRUTOR_GATEWAY_INTERNAL_URL",
    kind=URL,
    default="http://127.0.0.1:8100",
    description=(
        "Base URL of the Core Gateway as the Control Plane reaches it: every call it makes to "
        "the gateway (evidence, model probes, health checks, approvals) goes there. The default "
        "serves a gateway run on the same machine; a container sets the gateway's service "
        "address."
    ),
    example="http://core:8100",
    accepted="an http:// or https:// URL",
    check=_http_url,
)

BRUTOR_GATEWAY_SERVICE_API_KEY = EnvVar(
    name="BRUTOR_GATEWAY_SERVICE_API_KEY",
    kind=STRING,
    secret=True,
    description=(
        "API key the Control Plane presents to the Core Gateway on every call it makes to it. "
        "The Control Plane registers it in the database at the first start, where the gateway "
        "finds it; the KB Uploader is given the same variable. Unset at the first start: a key "
        "is generated and written to the log once, to be set here; while it is unset the "
        "Control Plane's own calls to the gateway carry no key."
    ),
    example=_SECRET,
    accepted="a long random string",
)

BRUTOR_GATEWAY_DB_POOL_SIZE = EnvVar(
    name="BRUTOR_GATEWAY_DB_POOL_SIZE",
    kind=INTEGER,
    default="20",
    minimum=1,
    description=(
        "Size of the Core Gateway's PostgreSQL pool: the gateway's own setting, given to the "
        "Control Plane under the same name so that its bulk operations against the gateway "
        "(health checks of many models) stay inside it."
    ),
    example="50",
    accepted="a whole number, 1 or more",
)

# --- the first administrator ------------------------------------------------------------------

BRUTOR_DEFAULT_TENANT_ADMIN_USERNAME = EnvVar(
    name="BRUTOR_DEFAULT_TENANT_ADMIN_USERNAME",
    kind=STRING,
    default="admin",
    description=(
        "User name of the administrator the first start creates in the default tenant, and, in the "
        "Enterprise Edition, of the first administrator of a tenant created without one named."
    ),
    example="admin",
    accepted="a user name",
)

BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD = EnvVar(
    name="BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD",
    kind=STRING,
    secret=True,
    description=(
        "Password of the administrator the first start creates. There is no default: the first "
        "start is refused without it. Read only when the account has to be created; changing it "
        "later does not change the account's password."
    ),
    example="<a password of your own>",
    accepted="a password",
)

BRUTOR_DEFAULT_TENANT_ADMIN_EMAIL = EnvVar(
    name="BRUTOR_DEFAULT_TENANT_ADMIN_EMAIL",
    kind=STRING,
    default="admin@example.com",
    description="Email address of the administrator the first start creates.",
    example="admin@example.com",
    accepted="an email address",
)

# --- what a new tenant is seeded with ------------------------------------------------------------

BRUTOR_EMBEDDING_MODEL = EnvVar(
    name="BRUTOR_EMBEDDING_MODEL",
    kind=STRING,
    description=(
        "The installation's fallback embedding model, by name: used only for a tenant that has no "
        "default embedding model. A new tenant that has a model of this name is given it as its "
        "default. The Core Gateway and the KB Uploader are given the same variable and resolve in "
        "the same order (the tenant's default first), so that a query is embedded with the model "
        "its documents were. There is no default: with neither, an embedding is refused."
    ),
    example="text-embedding-3-small",
    accepted="a model name",
)

BRUTOR_DEFAULT_JUDGE_MODEL = EnvVar(
    name="BRUTOR_DEFAULT_JUDGE_MODEL",
    kind=STRING,
    description=(
        "Model name a new tenant gets as its default judge model (LLM-judged assurance checks), "
        "when the tenant has a model of that name. Unset: the seed's own choice."
    ),
    example="claude-haiku-5-5",
    accepted="a model name",
)

BRUTOR_OLLAMA_BASE_URL = EnvVar(
    name="BRUTOR_OLLAMA_BASE_URL",
    kind=URL,
    default="http://localhost:11434",
    description="Address the seeded Ollama models are registered with: where the Core Gateway reaches Ollama.",
    example="http://ollama:11434",
    accepted="an http:// or https:// URL",
    check=_http_url,
)

BRUTOR_A2A_SAMPLE_PUBLIC_URL = EnvVar(
    name="BRUTOR_A2A_SAMPLE_PUBLIC_URL",
    kind=URL,
    description=(
        "Address of the sample A2A agent (the Enterprise trial's samples/a2a): where the Core "
        "Gateway reaches it. Set, a new tenant is seeded with the sample's agent card "
        "(hello-world-a2a) carrying this address; the gateway calls it only when the outbound "
        "policy allows the host (BRUTOR_OUTBOUND_ALLOW_HOSTS). Unset: no sample card is seeded."
    ),
    example="http://a2a-sample:9100",
    accepted="an http:// or https:// URL",
    check=_http_url,
)

BRUTOR_SHARED_MCP_SERVERS = EnvVar(
    name="BRUTOR_SHARED_MCP_SERVERS",
    kind=STRING,
    description=(
        "MCP servers every new tenant is registered with: a JSON array of objects with `slug` "
        "and `base_url`, and optionally `name`, `description`, `auth_type`, `auth_token`, "
        "`timeout`, `icon`. An `auth_token` may name another variable as ${NAME}; the token is "
        "stored encrypted."
    ),
    example='[{"slug":"map","base_url":"http://mcp-app-map:8000/mcp"}]',
    accepted="a JSON array of objects",
    check=_json_array_of_objects,
)

BRUTOR_LOCAL_WITNESS_URL = EnvVar(
    name="BRUTOR_LOCAL_WITNESS_URL",
    kind=URL,
    empty_unsets=True,
    description=(
        "Address of the witness service that runs beside this installation. Set, the witness is "
        "registered at start so that the Core Gateway has evidence tree heads countersigned "
        "there. Unset or empty: no witness is registered."
    ),
    example="http://witness:8000",
    accepted="an http:// or https:// URL, or empty for none",
    check=_http_url,
)

BRUTOR_LOCAL_WITNESS_PINNED_KEY = EnvVar(
    name="BRUTOR_LOCAL_WITNESS_PINNED_KEY",
    kind=STRING,
    description=(
        "Public key of the local witness, pinned. Unset: the key is fetched from the witness "
        "when it is first registered."
    ),
    example="3b6a27bcceb6a42d62a3a8d02a6f0d73653215771de243a63ac048a18b59da29",
    accepted="64 hexadecimal characters (an Ed25519 public key)",
    check=lambda raw: None if _is_hex64(raw) else "64 hexadecimal characters (an Ed25519 public key)",
)

BRUTOR_LOCAL_WITNESS_OPERATOR = EnvVar(
    name="BRUTOR_LOCAL_WITNESS_OPERATOR",
    kind=STRING,
    default="brutor-witness (this deployment, same operator)",
    description="Who operates the local witness, as the witness list shows it.",
    example="Example Corp platform team",
    accepted="text",
)

# --- outbound calls -------------------------------------------------------------------------------

def _check_outbound_allow_list(raw: str):
    from app.services.outbound_policy import check_allow_list

    return check_allow_list(raw)


BRUTOR_OUTBOUND_ALLOW_HOSTS = EnvVar(
    name="BRUTOR_OUTBOUND_ALLOW_HOSTS",
    kind=LIST,
    check=_check_outbound_allow_list,
    description=(
        "Outbound policy. Destinations of MCP servers, model endpoints, agents, IdPs, webhooks, "
        "registries and witnesses at a private address (loopback, RFC 1918, carrier-grade NAT, "
        "IPv6 unique-local, and a name that resolves to one) are refused unless listed here; "
        "link-local and cloud metadata addresses are refused whatever this says. A host name "
        "allows every address it resolves to, *.domain the domain and its subdomains, an address "
        "or CIDR range those addresses. The Core Gateway and KB Connector Sync read the same "
        "variable."
    ),
    example="mcp.internal,*.corp.example,10.20.0.0/16,ollama",
    accepted=(
        "comma-separated host names, *.domain, IP addresses and CIDR ranges (no port, scheme or "
        "path)"
    ),
)

BRUTOR_GITHUB_TOKEN = EnvVar(
    name="BRUTOR_GITHUB_TOKEN",
    kind=STRING,
    secret=True,
    description=(
        "GitHub token the skill-source sync reads repositories with. Unset: GitHub's anonymous "
        "rate limit applies and private repositories cannot be read."
    ),
    example="<a GitHub token with read access to the repositories>",
    accepted="a GitHub personal access token",
)

# --- email ---------------------------------------------------------------------------------------------

BRUTOR_RESEND_API_KEY = EnvVar(
    name="BRUTOR_RESEND_API_KEY",
    kind=STRING,
    secret=True,
    description=(
        "API key of the Resend account the Control Plane sends email with (the welcome message "
        "of a new user, the notice that two-factor sign-in was switched on). Unset: no email is "
        "sent."
    ),
    example="<a Resend API key>",
    accepted="a Resend API key",
)

BRUTOR_RESEND_FROM_EMAIL = EnvVar(
    name="BRUTOR_RESEND_FROM_EMAIL",
    kind=STRING,
    default="noreply@brutor.ai",
    description="Sender address of the email the Control Plane sends. A domain verified with Resend.",
    example="noreply@example.com",
    accepted="an email address",
)

# --- Gunicorn (read by gunicorn.conf.py) ----------------------------------------------------------------

GUNICORN_WORKERS = EnvVar(
    name="GUNICORN_WORKERS",
    kind=INTEGER,
    default="2",
    minimum=1,
    description=(
        "Advanced. Worker processes. The Control Plane serves administrators, not traffic: 2 is "
        "enough for most installations. Each worker has its own database pool. Every other "
        "Gunicorn setting is fixed."
    ),
    example="4",
    accepted="a whole number, 1 or more",
)


def _is_hex64(raw: str) -> bool:
    value = raw.strip().lower()
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


# In the order of the generated documents: what an installation must set,
# then what it usually sets, then the rest by subject.
VARIABLES = (
    DATABASE_URL,
    JWT_SECRET,
    ENCRYPTION_KEY,
    REDIS_URL,
    BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD,
    BRUTOR_DEFAULT_TENANT_ADMIN_USERNAME,
    BRUTOR_DEFAULT_TENANT_ADMIN_EMAIL,
    BRUTOR_GATEWAY_INTERNAL_URL,
    BRUTOR_GATEWAY_SERVICE_API_KEY,
    BRUTOR_CONTROL_PLANE_PUBLIC_URL,
    BRUTOR_CORS_ORIGINS,
    BRUTOR_TRUSTED_PROXIES,
    JWT_ALGORITHM,
    BRUTOR_CONTROL_PLANE_LISTEN_ADDR,
    BRUTOR_VERSION,
    BRUTOR_LOG_LEVEL,
    BRUTOR_LOG_JSON_ENABLED,
    BRUTOR_CONTROL_PLANE_DB_POOL_SIZE,
    BRUTOR_DB_COMMAND_TIMEOUT_SECS,
    BRUTOR_GATEWAY_DB_POOL_SIZE,
    BRUTOR_EMBEDDING_MODEL,
    BRUTOR_DEFAULT_JUDGE_MODEL,
    BRUTOR_OLLAMA_BASE_URL,
    BRUTOR_A2A_SAMPLE_PUBLIC_URL,
    BRUTOR_SHARED_MCP_SERVERS,
    BRUTOR_LOCAL_WITNESS_URL,
    BRUTOR_LOCAL_WITNESS_PINNED_KEY,
    BRUTOR_LOCAL_WITNESS_OPERATOR,
    BRUTOR_OUTBOUND_ALLOW_HOSTS,
    BRUTOR_GITHUB_TOKEN,
    BRUTOR_RESEND_API_KEY,
    BRUTOR_RESEND_FROM_EMAIL,
    GUNICORN_WORKERS,
)

REGISTRY.register(VARIABLES)
