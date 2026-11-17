# Getting started with Brutor Agent Control Community Edition

From the downloaded package to a signed-in Admin Console, a first API key and
a first call through the Core Gateway. The figures were measured on an Apple
silicon Mac with Docker Desktop.

The package's `README.md` has the prerequisites, the list of services and the
known limits.

## Start

```bash
docker compose up -d
```

The package's `.env` already holds the secrets and the administrator's
password, generated for your download. There is nothing to edit.

The first start pulls the images and sets up the database. With the images
already on the machine, `docker compose up -d` returned after 16.7 seconds and
all seven services were healthy after 22.5 seconds.

```bash
docker compose ps
```

```text
SERVICE         STATE     HEALTH
admin-console   running   healthy
control-plane   running   healthy
core            running   healthy
postgres        running   healthy
redis           running   healthy
skill-runner    running   healthy
witness         running   healthy
```

The Core Gateway's log says which edition is running:

```bash
docker compose logs core | grep Edition
```

```text
{"level":"INFO","fields":{"message":"Edition check passed: community edition"}, ...}
```

`./start.sh` (macOS, Linux) and `.\start.ps1` (Windows) run the same
`docker compose up`, then wait until the stack is healthy and print the
addresses. They ask no questions. Use them if you cloned the source repository
instead of downloading the package: they generate the values a downloaded
package already has in `.env`.

Do not export `COMPOSE_PROJECT_NAME` in your shell. The package sets its own
Compose project name, `brutor-agent-control`; an exported one overrides it, and
`docker compose down` then does nothing, without saying so.

## Check it

```bash
curl -s localhost:5050/health                                     # Control Plane
curl -s -o /dev/null -w '%{http_code}\n' localhost:8100/health    # Core Gateway
curl -s localhost:3002/env-config.js | grep URL                   # Admin Console
```

The first answers a JSON document that begins `{"status":"healthy"`. The second
prints `200`. The third prints the two addresses the Admin Console calls from
your browser:

```text
  BRUTOR_CONTROL_PLANE_PUBLIC_URL: "http://localhost:5050",
  BRUTOR_GATEWAY_PUBLIC_URL: "http://localhost:8100",
```

The health endpoints need no credential: Docker's health checks call them.
`docker compose ps` shows the state of every service.

## Sign in

Open the Admin Console at http://localhost:3002 and sign in with:

- User name: the value of `BRUTOR_DEFAULT_TENANT_ADMIN_USERNAME` in `.env` (`admin`)
- Password: the value of `BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD` in `.env`

The password was generated for your copy of the package; there is no stock
password. Both values are read once, when the database is created. To change
the password afterwards, change it in the Admin Console. This administrator is
the only account the installation starts with.

Five wrong passwords lock the account for 15 minutes. If the only administrator
is locked out, the operator command `python main.py admin-unlock USERNAME`,
run where the Control Plane runs, ends the lock: see
[Administrator sign-in lock](https://docs.brutor.ai/installation/administrator-sign-in-lock/).

Keep `.env`: it holds the keys this installation encrypts and signs with. A
stack started with a different `.env` cannot read the data of this one.

## First API key and first call through the gateway

Agents, scripts and command-line tools call the Core Gateway with a Brutor API
key. A key belongs to a resource group, and the group's model access,
guardrails and limits come with it.

Run these from the package folder: the first line reads the administrator's
password from `.env`, so it is never typed or printed.

```bash
# sign in as the tenant administrator
PW=$(sed -n 's/^BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD=//p' .env)
TOKEN=$(curl -s -X POST localhost:5050/v1/admin/users/tenant/login \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"admin\",\"password\":\"$PW\",\"tenant_id\":\"default\"}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')

# a resource group, and an API key for it
GID=$(curl -s -X POST localhost:5050/v1/admin/resource-groups -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"first-steps","display_name":"First steps","description":"getting started"}' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print((d.get("group") or d)["id"])')
KEY=$(curl -s -X POST localhost:5050/v1/admin/resource-groups/$GID/api-keys -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"first-key","description":"getting started","access_mode":"shared"}' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("key_value") or d.get("full_key") or d.get("key"))')
```

The sign-in answers `200` with an `access_token`; a wrong password answers
`401`. The group and the key each answer `201`. The key starts with
`sk_brutor_api_` and its full value is returned this once only.

Now call the Core Gateway, first without the key and then with it:

```bash
curl -s -o /dev/null -w '%{http_code}\n' localhost:8100/v1/proxy/llm/models      # 401: no credential
curl -s localhost:8100/v1/proxy/llm/models -H "Authorization: Bearer $KEY"       # 200
```

The second call answers `200` with `object` and `data`, and `data` is empty: a
resource group reaches only the models that are bound to it, and nothing is
bound to `first-steps` yet. The key names the tenant and the group, so no
tenant header is needed.

## First governed model call

A model call needs a model with a provider key, bound to the key's resource
group. In the Admin Console: paste your provider API key into a model (AI
Models, pick a provider) and create an API key on the Default resource group.
Then send a chat completion to
`http://localhost:8100/v1/proxy/llm/chat/completions` with that key as the
bearer token. The call is checked against the group's access rules, guardrails
and limits, metered and logged.

A catalog entry marked as needing an API URL is saved only with the address
of your own endpoint: Azure, Ollama, LM Studio, vLLM, Databricks and the other
providers that have no address of their own. Without one the Control Plane
refuses the model (`422`, naming `api_url`). The Core Gateway calls that
address from inside the `core` container, where `localhost` is the container
itself, not your machine. This package has no Ollama service. For a model
server on your own machine, Docker Desktop (macOS, Windows) gives containers
the name `host.docker.internal`: an Ollama on its default port is
`http://host.docker.internal:11434`, once `host.docker.internal` is added to
`BRUTOR_OUTBOUND_ALLOW_HOSTS` in `.env` (the gateway refuses a private address
that is not listed). On Linux this compose file does not map
that name; use an address of your machine that containers can reach.

## Run a governed agent end to end

The Brutor Demo System (open source, Apache-2.0,
https://github.com/brutor-ai/brutor-demo-system) is a loan pre-screening agent
that works through models, MCP servers, a skill and a second agent over A2A,
all through the Core Gateway. Its four containers join the package's Docker
network `brutor-network` and reach the gateway as `http://core:8100`. It needs
Python 3.10 or later, an OpenAI API key, and ports 3014, 3015, 9200 and 9201.

```bash
cd brutor-demo-system/brutor-demo-setup
cp .env.example .env
#   OPENAI_API_KEY=...        your key
#   BRUTOR_ENV_FILE=/path/to/this/package/.env
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.txt
./demo.sh up
```

`BRUTOR_ENV_FILE` points the demo at this package's `.env`, from which it reads
the administrator's password. One screening run made three model calls, took 9
to 15 seconds and cost about 0.02 USD. Decisions the agent must not take alone
are held for a person, who decides them in the Admin Console under Mission
Control, Approvals, or with `./demo.sh approve`.

## Stop and remove

```bash
docker compose down        # stop; data is kept
docker compose down -v     # stop and delete all data
```

If the demo system is running, stop it first with `./demo.sh down`:
`docker compose down` removes `brutor-network`, which the demo's containers are
attached to.

After `docker compose down`, `docker compose up -d` brought the stack back
healthy in 24 seconds with its data intact: groups, keys, approvals and logs.

## More

- Documentation: https://docs.brutor.ai
- The Enterprise Edition adds more, on a license: it is a separate download
  with its own getting-started guide.
