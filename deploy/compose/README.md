# Brutor Agent Control Community Edition

The open source edition of Brutor Agent Control (Apache-2.0): a control plane,
a core gateway and an Admin Console for governing AI agents, models and MCP
servers, with a skill runner and a local witness for compliance evidence. One
tenant, no license key.

## Prerequisites

- Docker with Compose v2 (Docker Desktop, or Docker Engine with the Compose
  plugin). Check with `docker compose version`.
- These ports free on your machine: 3002, 5050, 8100, 5432, 8300. Each
  can be changed in `.env`.
- Memory: the seven containers use about 0.5 GB at idle on a fresh install
  (control plane about 260 MB, PostgreSQL about 135 MB, the witness about
  50 MB, the rest under 20 MB each) and about 0.65 GB once a few AI Systems
  have run. Give Docker at least 4 GB; the control plane is capped at 3 GB
  and the gateway at 2 GB.
- Disk: about 2.5 GB for the seven images, plus your data.

Measured on an Apple silicon Mac with Docker Desktop.

## Start

```bash
docker compose up -d
```

The first start pulls the images, which takes a few minutes on a typical
connection, and sets up the database. Once the images are on your machine a
start from nothing takes about 25 seconds. `docker compose ps` shows every
service as `healthy` when it is ready.

`./start.sh` (macOS, Linux) and `.\start.ps1` (Windows) do the same and then
wait and print the addresses. Use them if you cloned the source repository
instead of downloading the package: they generate the secrets that a
downloaded package already has in `.env`. They ask nothing, so they also run
without a terminal. They never replace a value that is already in `.env`; if
one has been edited into something a component would refuse (a secret under
32 characters, for example), they name the line, say what it must be and
start nothing. An `.env` of a package from before this release does not
carry over: start from the `.env` this package comes with.
`start.ps1` has not been run on Windows for this release (it
was checked by reading it and by comparing its text with `start.sh`, which is
run); `docker compose up -d` does not depend on it.

### Build from a clone

In a clone of the source repository this package is `deploy/compose/`, and
`docker-compose.build.yml` beside the Compose file builds every Brutor image
from its component directory instead of pulling it (PostgreSQL and Valkey are
still pulled; every other setting stays the one in `docker-compose.yml`):

```bash
COMPOSE_FILE=docker-compose.yml:docker-compose.build.yml ./start.sh
```

That fills the empty secrets in `.env`, builds, starts and waits until every
service is healthy. Once `.env` is filled, the same without the script is
`docker compose -f docker-compose.yml -f docker-compose.build.yml up --build -d --wait`:
`--build` builds every Brutor image, `-d` starts in the background and
`--wait` returns once every service is healthy (and fails if one is not).
Without `-d --wait`, Compose stays in the foreground and prints the logs.
The images get local names (`brutor-agent-control-local/...`). A downloaded
package does not carry the build file: it has no source to build from.

## Addresses

| What | Address |
|---|---|
| Admin Console | http://localhost:3002 |
| Control plane API | http://localhost:5050 |
| Core gateway (LLM, MCP, A2A, skills) | http://localhost:8100 |

Everything listens on this machine only. `BIND_ADDRESS=0.0.0.0` in `.env`
opens these three ports to your network and nothing else: the database and the
witness stay on this machine, and Valkey is not published at all. Put TLS or
a reverse proxy in front when you do, and set the public URLs in `.env` to the
addresses your browser uses.

Containers you start yourself (the demo system, your own MCP servers and
agents) can join the Docker network `brutor-network` and reach the gateway as
`http://core:8100`.

## First sign-in

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

Then add a model: AI Models, pick a provider, paste your API key.

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

The
[getting-started guide](docs/getting-started-guide.md) continues from here:
a first API key, a first call through the gateway and the demo system.

## Keep `.env`

`.env` holds the keys this installation encrypts and signs with. A stack
started with a different `.env` cannot read the data of this one. If you
download the package again, keep using your first `.env`, or remove the old
data first (see below).

## Media

Files users upload and models generate are kept by the core gateway on the
Docker volume `brutor-agent-control_brutor-media` (`brutor-media` in
`docker-compose.yml`), up to `BRUTOR_MEDIA_FILESYSTEM_MAX_BYTES` in `.env`
(10 GiB). A write that would pass the limit is refused; deleting media makes
room again. The gateway serves the download links itself, signed and built
from `BRUTOR_GATEWAY_PUBLIC_URL`: when the gateway is reached at another
address, set that in `.env` too. Back the volume up with the database, the
stack stopped:

```bash
docker run --rm -v brutor-agent-control_brutor-media:/data -v "$(pwd)/backups:/backup" \
  postgres:16.14-alpine tar czf /backup/media.tar.gz -C /data .
```

To keep media in an S3-compatible store instead, see the configuration
reference on https://docs.brutor.ai.

## Upgrade

Every Brutor image of the package carries one tag, `BRUTOR_VERSION`; the
package's `docker-compose.yml` has the release it was made for as the default.
To move an installation to a newer release:

1. Copy the data of the database, with the stack stopped. The Control Plane
   changes the database schema when a newer release starts, and an older
   release does not serve a database a newer one has changed, so this copy is
   the way back.

   ```bash
   docker compose stop
   docker run --rm -v brutor-agent-control_postgres_data:/data -v "$(pwd)/backups:/backup" \
     postgres:16.14-alpine tar czf /backup/postgres-before-upgrade.tar.gz -C /data .
   ```

2. Download the newer package, unpack it and put the `.env` of this
   installation in its folder, in place of the one it came with. If your
   `.env` has a `BRUTOR_VERSION` line, set it to the new release.
3. From the new folder, run `./start.sh` (`.\start.ps1` on Windows). Compare
   your `.env` with the one the new package came with first: a line the new
   release no longer reads has no effect, and a component refuses to start
   with a `BRUTOR_` name it does not know. The Control Plane brings
   the database to the new release at its start, and the Core Gateway starts
   once the Control Plane is healthy.
4. Check it as after the first start: `docker compose ps` shows every service
   as `healthy`.

To go back, stop the stack, put the copy back and start the earlier package
with the same `.env`. What was written after the copy is lost.

```bash
docker compose down
docker run --rm -v brutor-agent-control_postgres_data:/data -v "$(pwd)/backups:/backup" \
  postgres:16.14-alpine sh -c 'find /data -mindepth 1 -delete && tar xzf /backup/postgres-before-upgrade.tar.gz -C /data'
```

## Stop and remove

```bash
docker compose down                  # stop; data is kept
docker compose down -v               # stop and delete all data
docker compose down -v --rmi all     # uninstall: delete the data and the images too
```

After the last one, delete the package folder; nothing else was installed.

## One installation per machine

One installation of Brutor Agent Control runs on a machine at a time: this
package, or the Enterprise trial, not both, and not two copies of either. The
Compose project name (`brutor-agent-control`), the Docker network
`brutor-network` and the default ports (3002, 5050, 8100, 5432, 8300) are
fixed, and the Enterprise trial uses the same network name and the same
default ports, with fixed container and volume names of its own. A start
next to the other package fails on the first name or port that is already
taken. A second copy of this package has the same project name: started in
another folder, it replaces the first copy's containers and uses its data.

To switch, stop the running one first, from its own folder:

```bash
docker compose down        # in the folder of the installation that runs; data is kept
```

Then start the other. `docker compose down -v` instead also deletes the
stopped installation's data.

## What is running

| Service | Purpose |
|---|---|
| `postgres` | Database |
| `redis` | Valkey, an open source key-value store that speaks the Redis protocol: usage limits, sessions and cache invalidation |
| `control-plane` | Admin API; owns the database schema |
| `core` | The gateway your agents and clients call |
| `admin-console` | The web console |
| `skill-runner` | Runs skill scripts, on a private network with no way out |
| `witness` | Receipts for signed evidence tree heads |

## Known limits

- Docker Compose is the only packaged way to run the Community Edition for
  now; there is no Helm chart yet.
- One instance of each service. Running several gateways or control planes
  side by side is an Enterprise Edition feature.
- Uploaded and generated files are kept on the Docker volume `brutor-media`,
  up to 10 GiB (`BRUTOR_MEDIA_FILESYSTEM_MAX_BYTES` in `.env`); a write past
  the limit is refused. Back the volume up with the database;
  `docker compose down -v` deletes both.

## Links

- [Getting started](docs/getting-started-guide.md), and
  [what else is documented and where](docs/README.md): the package carries
  what you need to install and run it, and every other subject is a page of
  https://docs.brutor.ai
- License: Apache-2.0, see `LICENSE` and `NOTICE`
