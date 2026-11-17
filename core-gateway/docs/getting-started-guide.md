# Getting started: the Community gateway from source

From a checkout of this repository to a running Core Gateway and a first call through it,
against a PostgreSQL and a Redis on your machine.

If you want to use Brutor Agent Control rather than work on the gateway, start the Community
package instead (seven containers, one command, nothing to build):
<https://docs.brutor.ai/installation/quickstart/>.

## What you need

- Rust: `rustup` installs the pinned toolchain (`rust-toolchain.toml`) on the first build.
- Docker, for PostgreSQL and Redis.
- The Control Plane, `brutor-gateway-control`, checked out beside this repository. The gateway
  cannot be set up without it: the Control Plane creates the database schema, the tenant
  `default` and its administrator, and it is where a resource group, an API key and a model are
  created. The gateway only reads them.

## 1. PostgreSQL and Redis

The Control Plane's repository has a Compose file for development infrastructure:

```bash
cd ../brutor-gateway-control
docker compose up -d postgres redis
```

PostgreSQL listens on 5432 and Redis on 6379 unless `DB_PORT` / `REDIS_PORT` say otherwise; the
database name, user and password are the ones that Compose file sets.

Redis is not optional. The gateway enforces every usage limit on it and refuses to start
without it.

## 2. The Control Plane, once, to create the database

The Control Plane and the gateway use the same database, the same Redis and the same two
secrets. Set the four once and use the same values in both:

```bash
export DATABASE_URL=postgresql://<user>:<password>@localhost:5432/brutor
export REDIS_URL=redis://localhost:6379/0
export JWT_SECRET=$(openssl rand -hex 32)
export ENCRYPTION_KEY=$(openssl rand -hex 32)
```

The gateway refuses a `JWT_SECRET` or `ENCRYPTION_KEY` that is shorter than 32 characters or
that comes from an example file. It needs all four: without one of them it does not start, and
says which:

<!-- env-registry:begin (generated from src/env_vars.rs) -->

| Variable | Type | Default | Edition | Description | Accepted | Example |
|---|---|---|---|---|---|---|
| `DATABASE_URL` (secret) | url | **required** | both | PostgreSQL connection string of the database the Control Plane created and migrated. The gateway reads its configuration there and never alters the schema. | a postgresql:// URL | `postgresql://<user>:<password>@localhost:5432/<database>` |
| `REDIS_URL` (secret) | url | **required** | both | Redis (or Valkey) the gateway enforces every usage limit on and listens on for cache invalidation from the Control Plane. The gateway does not start without it. The Control Plane must use the same Redis. | a redis:// or rediss:// URL | `redis://<host>:6379/0` |
| `JWT_SECRET` (secret) | string | **required** | both | Secret the gateway verifies the Control Plane's tokens with and signs its own with. Must equal the Control Plane's JWT_SECRET. A value from an example file is refused. | at least 32 characters, not a placeholder | `<output of: openssl rand -hex 32>` |
| `ENCRYPTION_KEY` (secret) | string | **required** | both | Decrypts the credentials the Control Plane stored (provider keys, OAuth tokens, client secrets) and keys the gateway's own derived secrets. Must equal the Control Plane's ENCRYPTION_KEY. A value from an example file is refused. | at least 32 characters, not a placeholder | `<output of: openssl rand -hex 32>` |

<!-- env-registry:end -->

Every other variable it reads has a default or is optional; the table of all of them, generated
from the same registry, is in the [README](../README.md#configuration), and
`cargo run --bin brutor-gateway-core -- --print-env` prints it as JSON. A variable name the
gateway used to read (`PROXY_*`, an unprefixed product name) refuses the start, with the name
to use instead.

Start the Control Plane (its `README.md` has the details). The first start needs a password for
the administrator it creates; there is no default:

```bash
export BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD=<a password of your own>
pip install -e ".[dev]"
python main.py            # http://localhost:5050; creates the schema on an empty database
```

Leave it running: the next steps create a key through it.

## 3. The gateway

In a second terminal, with the same four variables exported:

```bash
cd ../brutor-gateway-core
cargo run --bin brutor-gateway-core
```

The first build compiles the whole dependency tree and takes several minutes. The gateway then
listens on port 8100 (`BRUTOR_GATEWAY_LISTEN_ADDR` changes it). Its log is JSON; three lines to look for:

```text
{"level":"INFO","fields":{"message":"Instance lease taken"}, ...}
{"level":"INFO","fields":{"message":"Edition check passed: community edition"}, ...}
{"level":"INFO","fields":{"message":"Listening on 0.0.0.0:8100"}, ...}
```

Check it:

```bash
curl -s -o /dev/null -w '%{http_code}\n' localhost:8100/health    # 200
curl -s localhost:8100/v1/version
```

`/health` needs no credential and answers within a few seconds of the start. `/v1/version`
answers, among other fields:

```json
{"edition":"community","service":"brutor-gateway-core","version":"0.11.32", ...}
```

The gateway refuses to send a request to a private address (loopback, the RFC 1918 ranges, link-local)
unless it is allowed: an MCP server or model endpoint on this machine, such as Ollama on
`localhost:11434`, is refused with a message naming `BRUTOR_OUTBOUND_ALLOW_HOSTS`. To use one, start
the gateway with it allowed, for example `export BRUTOR_OUTBOUND_ALLOW_HOSTS=localhost,127.0.0.1`
(host names, `*.domain`, IP addresses or CIDR ranges, comma-separated). Cloud metadata addresses
(169.254.169.254) are never allowed.

Only one Community gateway runs against a Redis and a database. A second one waits up to 20
seconds for the first one's lease and then exits, saying that the Community Edition runs as a
single instance. A normal stop (Ctrl-C, `SIGTERM`) releases the lease at once; after a crash a
restart can wait up to 15 seconds for the old lease to lapse.

## 4. A resource group and an API key

Every call to the gateway is made with an API key bound to a resource group. Create both in the
Admin Console, or with the Control Plane's API:

```bash
TOKEN=$(curl -s -X POST localhost:5050/v1/admin/users/tenant/login \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"admin\",\"password\":\"$BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD\",\"tenant_id\":\"default\"}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')

GID=$(curl -s -X POST localhost:5050/v1/admin/resource-groups -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"first-steps","display_name":"First steps","description":"getting started"}' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print((d.get("group") or d)["id"])')

KEY=$(curl -s -X POST localhost:5050/v1/admin/resource-groups/$GID/api-keys -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"first-key","description":"getting started","access_mode":"shared"}' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("key_value") or d.get("full_key") or d.get("key"))')
```

The key (prefix `sk_brutor_api_`) is shown once.

## 5. A first call

```bash
curl -s -o /dev/null -w '%{http_code}\n' localhost:8100/v1/proxy/llm/models -H 'X-Tenant-ID: default'   # 401
curl -s localhost:8100/v1/proxy/llm/models -H "Authorization: Bearer $KEY"                               # 200
```

Without a credential the gateway answers 401. With the key it answers the models the key's
group may use. A new group has none:

```json
{"data":[],"object":"list"}
```

The list stays empty until a model is bound to the group: in the Admin Console (AI Models) pick a
provider, give it your provider API key, and bind the model to the group. Then:

```bash
curl -s localhost:8100/v1/proxy/llm/chat/completions -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"<the model name>","messages":[{"role":"user","content":"hello"}]}'
```

A model the group cannot use answers 404 (`Model not found`).

A change made in the Control Plane reaches the running gateway over Redis within moments; no
restart is needed.

## What a Community gateway does not serve

The User Portal API (`/v1/portal/*`), batch jobs, the Knowledge Base and the response and
prompt caches are the Enterprise Edition's ([`../enterprise/README.md`](../enterprise/README.md)).
With a tenant named, `POST localhost:8100/v1/portal/auth/login` answers 404 on a Community
gateway.

## Next

- Tests and the gates: [`../README.md`](../README.md), [`../tests/integration/README.md`](../tests/integration/README.md).
- Engineering rules: the section of that name in [`../README.md`](../README.md).
- Using the gateway: <https://docs.brutor.ai/getting-started/first-llm-call/>,
  <https://docs.brutor.ai/getting-started/first-mcp-call/>.
