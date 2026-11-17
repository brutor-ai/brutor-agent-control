# Brutor Agent Control

Brutor Agent Control governs the traffic between AI agents and the things they use: LLM
models, MCP servers, other agents (A2A) and skills. Agents and clients call one gateway. The
gateway checks each call against the configuration you set (who may use what, limits,
guardrails, approvals) and records what happened, so that you can show afterwards what an
AI system did.

This repository is the **Community Edition**. It is open source under the Apache License,
Version 2.0.

## What is in it

| Component | Directory | What it does |
|---|---|---|
| Core Gateway | [`core-gateway/`](core-gateway/) | The gateway your agents and clients call (Rust). It carries all LLM, MCP, A2A and skill traffic and enforces the configuration on every request. Port 8100. |
| Control Plane | [`control-plane/`](control-plane/) | The administration API (Python, FastAPI). It owns the database schema and all configuration writes. Port 5050. |
| Admin Console | [`admin-console/`](admin-console/) | The web console (React, TypeScript). It talks to the Control Plane. Port 3002. |
| Skill Runner | [`skill-runner/`](skill-runner/) | A sandbox that runs skill scripts for the Core Gateway (Rust). |
| Witness | [`witness/`](witness/) | The local transparency service that countersigns the heads of the evidence trees, so that signed evidence can be checked against a second party. |
| Image build | [`package/`](package/) | [`docker-bake.hcl`](package/docker-bake.hcl) builds the five images with the names they are published under. |
| Compose package | [`deploy/compose/`](deploy/compose/) | The Docker Compose package that starts all of the above with PostgreSQL and Valkey: from the published images, or built from this clone. |

The Control Plane writes configuration to PostgreSQL. The Core Gateway reads it and never
changes it. A change made in the Control Plane reaches a running gateway through Redis (the
package ships Valkey, which speaks the Redis protocol).

## The two editions

Brutor Agent Control comes in two editions built from one codebase.

**Community Edition** (this repository): one tenant, named `default`, and no licence key. It
contains everything that is not on the Enterprise list below. That includes:

- governing LLM, MCP, A2A and skill traffic through the Core Gateway;
- administrators and roles, resource groups, API keys, LLM models and routing, MCP servers,
  A2A agent cards, skills, guardrails;
- argument, semantic and agent policies;
- human approvals, decided in the Admin Console;
- the MCP catalog and the LLM catalog;
- Ollama and LM Studio as LLM providers;
- OIDC identity providers for MCP OAuth, and agent workload identity;
- the agent registry and the Asset Register;
- OpenTelemetry ingest;
- files, images, audio and video generation;
- the Compliance console and signed compliance evidence.

**Enterprise Edition**: the Community code plus extensions. It is a separate, proprietary
product and needs a licence key. It adds:

| Feature | What it is |
|---|---|
| Deployment | Model and MCP server deployment (Docker, Kubernetes, ECS, KServe, llm-d, model runner) |
| Traffic import | Provider traffic data import |
| Response cache | Exact and semantic response cache |
| Prompt cache | Prompt cache policies and marker injection |
| MCP Registry | A self-hosted MCP Registry instance and external registry search |
| Knowledge base | Collections, documents, connectors, retrieval |
| Policy-as-Code | Policy bundles and templates |
| Batch jobs | Asynchronous batch jobs |
| Multi-tenant | Tenant management and system administration |
| Portal | The end-user Portal |
| Discovery | Shadow-AI discovery |
| Clustering | Running more than one Core Gateway or Control Plane instance |

The Enterprise Edition never copies Community code. It adds to it through an extension
interface that is part of this repository. A Community build contains no Enterprise code.

## Start

You need Docker with Compose v2. Check with `docker compose version`.

The simplest way is the Community package, which pulls ready-made images:

```bash
cd deploy/compose
./start.sh            # macOS, Linux.  Windows: .\start.ps1
```

`start.sh` writes the secrets this installation needs into `deploy/compose/.env`, starts seven
containers, waits until they are healthy and prints the addresses. It never replaces a value
that is already in `.env`. The first start pulls the images and sets up the database.

| What | Address |
|---|---|
| Admin Console | http://localhost:3002 |
| Control Plane API | http://localhost:5050 |
| Core Gateway | http://localhost:8100 |

Sign in to the Admin Console with the user name and password in `deploy/compose/.env`
(`DEFAULT_TENANT_ADMIN_USERNAME`, `DEFAULT_TENANT_ADMIN_PASSWORD`). The password is generated
for your installation. There is no stock password.

Stop with `docker compose down` in `deploy/compose/`. `docker compose down -v` also deletes all data.

To run what is in this clone instead of the published images, add the build override, which
builds every image from its component directory:

```bash
cd deploy/compose
docker compose -f docker-compose.yml -f docker-compose.build.yml up --build
```

`./start.sh` fills the secrets in `.env` first; run it once, or fill them yourself. To build
the images alone, with the names they are published under, run from the repository root
`docker buildx bake -f package/docker-bake.hcl --load`.

Next steps:

- [`deploy/compose/README.md`](deploy/compose/README.md): prerequisites, ports, memory, known limits.
- [`docs/getting-started-guide.md`](docs/getting-started-guide.md): a first
  API key, a first call through the gateway, a first governed model call (the package carries
  the same guide in `deploy/compose/docs/`).
- Documentation for users and operators: <https://docs.brutor.ai>.

To work on a component from source, read its guide:
[`core-gateway/docs/getting-started-guide.md`](core-gateway/docs/getting-started-guide.md),
[`control-plane/docs/getting-started-guide.md`](control-plane/docs/getting-started-guide.md),
[`admin-console/docs/getting-started-guide.md`](admin-console/docs/getting-started-guide.md).

## Repository layout

```
core-gateway/        the Core Gateway (Rust)
control-plane/       the Control Plane (Python); owns the database schema and migrations
admin-console/       the Admin Console (React, TypeScript)
skill-runner/        the Skill Runner (Rust)
witness/             the witness: the transparency service for signed evidence
package/             docker-bake.hcl: builds every image, with its published name, tags and labels
deploy/compose/      the Community package (Docker Compose): from the published images, or with
                     docker-compose.build.yml built from this clone
docs/                the getting-started guide; all other documentation is on docs.brutor.ai
editions.yaml        the boundary between the two editions
check_editions.py    checks editions.yaml against the code
scripts/             the licence-header check and the private-reference declarations
.github/workflows/   CI: one workflow per component, the repository checks, the image publishing
.gitleaks.toml       the secret scanner's configuration and its allow-list
LICENSE              Apache License, Version 2.0
NOTICE               attributions and the trademark statement
SECURITY.md          how to report a vulnerability
CONTRIBUTING.md      how to contribute
CODE_OF_CONDUCT.md   the code of conduct
```

## How the edition boundary is checked

The boundary between the editions is written down in one file,
[`editions.yaml`](editions.yaml). Everything it does not list as Enterprise is Community.

The rules:

- Community code never imports Enterprise code.
- Community code never reads an Enterprise-only table, except in the modules that
  `editions.yaml` lists with a reason.
- The database schema is not split. Every table and every migration is in this repository.
  The Community Edition leaves the Enterprise tables empty.
- A Community start refuses a database that the Enterprise Edition has used.

[`check_editions.py`](check_editions.py) checks that every path, route prefix, table and
navigation id named in `editions.yaml` exists in the code:

```bash
pip install pyyaml
python3 check_editions.py --layout public
```

The paths in `editions.yaml` are relative to each component's directory. The Enterprise trees
are not in this repository: the checks that need them are skipped and counted, and every check
of the Community side runs. The workflow `.github/workflows/repository.yml` runs it on every
change, with the licence-header check, the private-reference scan and the secret scan.

Each component also has tests that hold the boundary on every change. They are listed in
[`CONTRIBUTING.md`](CONTRIBUTING.md).

## Contributing and security

- To contribute, read [`CONTRIBUTING.md`](CONTRIBUTING.md) and the
  [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).
- To report a vulnerability, read [`SECURITY.md`](SECURITY.md). Do not open a public issue
  for a vulnerability.

## Licence

Apache License, Version 2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).

"Brutor" and the Brutor logos are trademarks of Brutor AI Ltd. The Apache License does not
grant permission to use them (Section 6).
