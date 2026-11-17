# Brutor Agent Control — Admin Console

The web console administrators use to configure and observe Brutor Agent
Control: models and providers, MCP servers, agents and skills, groups and
limits, guardrails and policies, approvals, evidence and compliance, logs and
metrics. It is a React and TypeScript single-page application that talks to the
Control Plane's admin API. This repository holds two packages; the one at the
root (`brutor-agent-control-admin-console`) is the **Community Edition**
console. To run it and sign in for the first time, follow
[docs/getting-started-guide.md](docs/getting-started-guide.md). User and
operator documentation is at <https://docs.brutor.ai>.

## The two packages

| Package | Where | Entry point | What it is |
|---|---|---|---|
| Community | repository root, `src/` | `src/index.tsx` | The console on its own. Builds and runs without `enterprise/`. |
| Enterprise | `enterprise/` (an npm workspace) | `enterprise/src/index.tsx` | The Community console composed with one extension, `enterprise/src/extension.tsx`. See [enterprise/README.md](enterprise/README.md). |

The Enterprise package adds the screens, navigation entries and API client
modules for: model and MCP server deployment, traffic import, response cache
and prompt cache, the MCP Registry, the Knowledge Base, Policy-as-Code, batch
jobs, multi-tenant and system administration, administration of the end-user
Portal, and shadow-AI discovery. SAML single sign-on and SCIM provisioning are
not in this release. It needs an Enterprise Control
Plane; the Community console needs no licence key.

## Prerequisites

- Node.js `^20.19.0`, `^22.13.0` or `>=24` (`package.json` `engines`). `.nvmrc`
  pins 22.22.0, and CI and both Dockerfiles use Node 22.
- A running Control Plane (default `http://localhost:5050`). A few screens also
  call the Core Gateway directly (default `http://localhost:8100`).

## Configure

```bash
npm install
cp .env.example .env    # optional: without it the defaults below apply
```

The console needs two addresses, both as the browser reaches them: the
Control Plane's and the Core Gateway's. Every environment variable is defined
once, in `src/envRegistry.json` (the Enterprise package's own in
`enterprise/envRegistry.json`): its name, type, default, description and
example. `src/env.ts` is the one module that reads the console's
configuration; `scripts/tool-env.mjs` is the one that reads the build
tools'. The tables below and `.env.example` are written from the
registry by `npm run gen:env-docs` (`node scripts/gen-env-docs.mjs`), and
`src/__tests__/envRegistry.test.ts` fails when they differ from it or when
another file reads the environment.

<!-- env-registry:begin (generated from src/envRegistry.json by scripts/gen-env-docs.mjs) -->

Read by the console:

| In a container (environment) | From source (`.env`, read by Vite) | Type | Default | Edition | Description | Accepted | Example |
|---|---|---|---|---|---|---|---|
| `BRUTOR_CONTROL_PLANE_PUBLIC_URL` | `VITE_CONTROL_PLANE_PUBLIC_URL` | url | `http://localhost:5050` | both | Base URL of the Control Plane (the admin API), as the browser reaches it. Every /v1/admin/* call goes there. The Control Plane is given the same value under the same name. | an http:// or https:// URL the browser can reach | `https://control.example.com` |
| `BRUTOR_GATEWAY_PUBLIC_URL` | `VITE_GATEWAY_PUBLIC_URL` | url | `http://localhost:8100` | both | Base URL of the Core Gateway, as the browser reaches it. Used for the gateway addresses the console shows (MCP, model and agent endpoints) and for the health probes it calls directly. The Core Gateway is given the same value under the same name. | an http:// or https:// URL the browser can reach | `https://gateway.example.com` |
| `BRUTOR_DEBUG_ENABLED` | none | boolean | `false` | both | Write the console's diagnostics, and the configuration it started with, to the browser's console. Off, the console writes nothing there but failures. Run time only. | one of true, false, 1, 0, yes, no, on, off | `true` |

Read by the build tools and tests:

| Variable | Type | Default | Edition | Description | Accepted | Example |
|---|---|---|---|---|---|---|
| `BRUTOR_CONTROL_PLANE_DOCUMENTS_DIR` | path | none | both | For the contract scripts and the tests that check their output: a directory holding the Control Plane's two OpenAPI documents under their own file names, read instead of the sibling Control Plane working tree; and, for the permission list written with --documents-dir, its two permission modules at their Control Plane paths. Unset: the sibling working tree. | a directory | `/tmp/control-plane-documents` |
| `PRIVATE_REFERENCES_REPORT` | boolean | `false` | both | For one test (src/__tests__/noPrivateReferences.test.ts): print what the generated contract files carry. | one of true, false, 1, 0, yes, no, on, off | `1` |

<!-- env-registry:end -->

**From source.** The `VITE_*` values in `.env` are embedded in the bundle at
build time: put no secret in them. Both packages read the same `.env` at the
repository root. A name the registry does not define is not read.

**In a container.** At start-up `docker-entrypoint.sh` writes `env-config.js`
beside the build from the container's environment, under the variables' own
names. The page loads that file before the application, it sets
`window._env_`, and `src/env.ts` prefers what is there to the build-time
values. One image therefore serves any deployment: the addresses are set when
the container starts, not when the image is built. In development
`public/env-config.js` leaves `window._env_` unset, so the `VITE_*` values
apply. The Enterprise image runs the same entrypoint: the Enterprise console
reads no other run-time setting. No name but the registry's is read.

## Run, build, test

Root scripts are the Community package's; `enterprise:*` runs the same script
in the Enterprise package; `all:*` runs Community, then Enterprise.

| Task | Community | Enterprise | Both |
|---|---|---|---|
| Dev server on port 3002 | `npm run dev` | `npm run enterprise:dev` | |
| Type-check | `npm run type-check` | `npm run enterprise:type-check` | `npm run all:type-check` |
| Lint | `npm run lint` | `npm run enterprise:lint` | `npm run all:lint` |
| Lint findings grouped by rule | `npm run lint:report` | `npm run enterprise:lint:report` | |
| Tests, watch mode | `npm test` | `npm run enterprise:test` | |
| Tests, one run | `npm run test:run` | `npm run enterprise:test:run` | `npm run all:test` |
| Tests with coverage floors | `npm run test:coverage` | `npm run enterprise:test:coverage` | `npm run all:test:coverage` |
| Production build | `npm run build` (to `dist/`) | `npm run enterprise:build` (to `enterprise/dist/`) | `npm run all:build` |
| Scan the Community build | `npm run scan:bundle` | | |

Also: `npm run preview` serves the production build locally;
`npm run lint:fix` applies ESLint's automatic fixes. `npm run format` and
`npm run format:check` run Prettier over `src/`; they are not gates, and most
of the tree has not been formatted with it.

## Gates

- **Type-check and lint errors are zero.** The type-check of a package is
  its build program and then its test program (`tsconfig.test.json`, also
  `npm run type-check:tests`): the test files, fixtures and builders are
  held to the same compiler options as the code they test. Lint warnings are
  capped with `--max-warnings` at the measured count of each package (2,665
  Community, 590 Enterprise). A cap only goes down: lower it when warnings are cleared, never
  raise it, and do not add `eslint-disable` to get under it.
- **Coverage floors** in `vitest.config.ts` and `enterprise/vitest.config.ts`
  only go up. The same holds for the API typing ratchet
  (`src/lib/__tests__/apiTypingRatchet.test.ts`).
- **Runtime boundaries.** What comes from `JSON.parse`, a caught error or a
  `postMessage` payload is `unknown`, read through `src/lib/narrow.ts`
  (`errorMessage`, `getString`, `getNumber`, `getRecord`). `catch (e: any)` is
  not written.
- **Boundary rule.** Nothing under `src/` may load from `enterprise/`: not by a
  relative path, not by the `@enterprise` alias, not by a dynamic `import()` or
  `require`, and not a package only the Enterprise package declares (the
  policy editor, the obfuscator). The ESLint block
  `community/no-enterprise-imports` in `eslint.config.js` holds it at error
  level with no allow-list. Enterprise code imports the Community tree through
  the `@community` alias; what the Enterprise Edition adds reaches the
  Community console through the extension interface (`src/app/extensions.ts`).
- **No reference to a record the reader cannot open.** A comment, a test
  name, a string on screen or a script's output says what the code does and
  why in its own words. It does not cite a numbered design document, finding,
  plan step, decision, sprint or work item, the nickname of an internal build
  or a path into an unpublished repository, and it does not tell the history
  of a change. Published standards may be cited; each one is listed with its
  title in `src/test/privateReferences.ts`, which also holds the rules.
  `src/__tests__/noPrivateReferences.test.ts` reads every text file of the
  tree, and the Enterprise package runs it again over both trees. The
  generated contract files are not edited here: they are counted separately
  against a ceiling that only goes down.
- **Bundle scan.** `npm run scan:bundle` (`scripts/scan-community-bundle.mjs`,
  after `npm run build`) fails if the Community build contains a Control Plane
  route under an Enterprise prefix, an Enterprise route under a prefix both
  editions have routes under (the system administrator's:
  `/v1/admin/users/system` and everything under it; the tenant
  administrators' routes under `/v1/admin/users` are Community) or the id of
  an Enterprise navigation entry
  (`scripts/community-bundle-denylist.json`). It runs in the Community
  Dockerfile and in CI, where the `community-alone` job also deletes
  `enterprise/` and then type-checks, builds, scans, tests and lints.

## API types and the route contract

Each package is held to the Control Plane's OpenAPI document of its own
edition. `scripts/control-plane-documents.mjs` names, per edition, the
document, the vendored route list and the generated types file. Regeneration
needs `brutor-gateway-control` checked out beside this repository.

```bash
npm run gen:api-types:control               # src/lib/generated/control-plane-api.d.ts
npm run enterprise:gen:api-types:control    # enterprise/src/lib/generated/control-plane-api.d.ts
npm run gen:api-types:core                  # src/lib/generated/core-proxy-api.d.ts (needs brutor-gateway-core)
npm run gen:api-types                       # the Community control types, then the core types
node scripts/sync-control-plane-routes.mjs  # vendored route lists, every edition in the tree
```

Run them after a Control Plane route or schema change. The route-contract
tests check every client call against the vendored list of its edition.

The scripts read the two documents from the sibling working tree. To generate
from the documents of one Control Plane commit instead (while that tree is
being changed), put the two files, under the same names, in a directory and
name it in `BRUTOR_CONTROL_PLANE_DOCUMENTS_DIR`:

```bash
mkdir -p /tmp/cp
git -C ../brutor-gateway-control show <commit>:enterprise/tests/fixtures/openapi_snapshot.json > /tmp/cp/openapi_snapshot.json
git -C ../brutor-gateway-control show <commit>:tests/fixtures/openapi_snapshot_community.json > /tmp/cp/openapi_snapshot_community.json
BRUTOR_CONTROL_PLANE_DOCUMENTS_DIR=/tmp/cp node scripts/gen-control-plane-types.mjs
BRUTOR_CONTROL_PLANE_DOCUMENTS_DIR=/tmp/cp node scripts/sync-control-plane-routes.mjs
```

The npm scripts honour the variable too. With no edition named,
`gen-control-plane-types.mjs` writes the types of every edition in the tree.

Both scripts record the SHA-256 of the document each file was written from in
`scripts/control-plane-sources.json`. Beside the Control Plane repository, two
tests compare the vendored files with the sibling documents; when they differ
the failure prints the document's hash now and the recorded one, which tells a
Control Plane document that moved from a file edited by hand. A client
method that still returns `Promise<any>` is one whose route the Control Plane
serves without a typed response; their count is held by
`src/lib/__tests__/apiTypingRatchet.test.ts` and only goes down.

### Permission strings

The console shows an action or a sidebar entry to an administrator who holds
a permission. `scripts/control-plane-permissions.json` lists the permissions
the Control Plane registers, per edition, and names the Control Plane commit
it was read at (or none, when it was read from a directory of documents).
`src/__tests__/permissionStrings.test.ts` fails when the
console checks a string that is not in it: nobody holds such a permission, so
what it guards would never be shown. After a permission is added or renamed
in the Control Plane, write the list again:

```bash
node scripts/gen-control-plane-permissions.mjs            # at the Control Plane's HEAD
node scripts/gen-control-plane-permissions.mjs <commit>   # at one commit
```

It reads the two permission modules with `git show`, never from the working
tree. To write the list from modules that are in no commit yet, put the two
files in the directory of documents at their Control Plane paths
(`app/core/admin_user/permissions.py`,
`enterprise/brutor_enterprise/permissions.py`) and name the directory:

```bash
BRUTOR_CONTROL_PLANE_DOCUMENTS_DIR=/tmp/cp node scripts/gen-control-plane-permissions.mjs --documents-dir
```

The file then records no commit (`control_plane_commit` is null). In both
cases it records the SHA-256 of each module read, and the test compares the
list with the modules it names: the commit's, or the directory's.

## Docker images

Both images are built from the repository root and serve the build with nginx
on port 3002.

```bash
docker build -t admin-console-community .                            # Dockerfile: Community
docker build -t admin-console-enterprise -f enterprise/Dockerfile .  # Enterprise
```

`Dockerfile` leaves `enterprise/` out of its context and runs the bundle scan.

**Third-party licences.** Each build writes `THIRD_PARTY_LICENSES` into its
output (`scripts/third-party-licenses.mjs`, a Vite plugin): every package of
the modules it bundled, Vite's helpers and Tailwind's base styles, each at
its `package-lock.json` version with its licence, then each distinct licence
text once. A licence that is unknown or not in the one allowed list,
`scripts/third-party-licenses.json`, fails the build. The committed copies
(`THIRD_PARTY_LICENSES`, `enterprise/THIRD_PARTY_LICENSES`) must equal what
the build wrote: `npm run all:licenses:check` after `npm run all:build`, in
CI and in both Dockerfiles. After a dependency change, `npm run build && npm
run licenses:update` (Enterprise: `npm run enterprise:build && npm run
enterprise:licenses:update`). Each image carries the file at
`/usr/share/licenses/brutor/THIRD_PARTY_LICENSES` (label
`ai.brutor.third-party-licenses`) and serves it beside the console as
`/THIRD_PARTY_LICENSES`. dompurify, offered under MPL-2.0 or Apache-2.0, is
taken under Apache-2.0.
Set `BRUTOR_CONTROL_PLANE_PUBLIC_URL` and `BRUTOR_GATEWAY_PUBLIC_URL` on the
container (see [Configure](#configure)).

## Project layout

```
.
├── src/                    Community console
│   ├── index.tsx, App.tsx  entry point and application shell
│   ├── env.ts              the one reader of the console's configuration
│   ├── envRegistry.json    every environment variable, defined once
│   ├── app/                composition: screens, navigation, slots, extension interface
│   ├── components/         screens and UI, one directory per area
│   ├── hooks/              React hooks (useGateway, useAuth, ...)
│   ├── lib/                API client (gatewayApiClient.ts, gatewayApi/), types, generated/
│   ├── context/            tenant features and theme
│   ├── ui-style/, styles/  shared style system
│   └── test/               test set-up and shared fixtures
├── enterprise/             Enterprise package (npm workspace)
│   ├── src/                extension.tsx, features/, host/, hooks/, lib/
│   ├── Dockerfile          Enterprise image
│   └── package.json, vite.config.ts, vitest.config.ts, eslint.config.js, ...
├── scripts/                type generation, route sync, permission list, bundle scan, lint report,
│                           tool-env.mjs (the build tools' environment), gen-env-docs.mjs
├── docs/                   getting-started-guide.md
├── public/                 static files, the development env-config.js
├── Dockerfile              Community image
├── docker-entrypoint.sh    writes env-config.js, starts nginx (both images)
├── nginx.conf
└── .github/workflows/      ci.yml, docker-build.yml
```

## Documentation

- [docs/getting-started-guide.md](docs/getting-started-guide.md): run the
  console from source, sign in, first steps.
- Installation, configuration and use of the product: <https://docs.brutor.ai>.

## Licence

See [LICENSE](LICENSE).
