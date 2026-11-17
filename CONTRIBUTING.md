# Contributing to Brutor Agent Control

Thank you for helping. This page says how to make a change that can be merged.

By taking part you agree to the [Code of Conduct](CODE_OF_CONDUCT.md). To report a
vulnerability, do not open an issue: follow [SECURITY.md](SECURITY.md).

## Before you start

- For a bug, open an issue with the steps to reproduce it. For a feature, open an issue first
  and describe the problem you want to solve, so that we can agree on the approach before you
  write code.
- This repository is the Community Edition. A feature on the Enterprise list in
  [`editions.yaml`](editions.yaml) is not accepted here. See "The edition boundary" below.
- Keep a pull request to one change. A fix and an unrelated clean-up are two pull requests.

## Sign your commits (DCO)

Every commit must be signed off under the Developer Certificate of Origin
(<https://developercertificate.org/>). The sign-off says that you wrote the change or have
the right to submit it under the project's licence. Add it with `-s`:

```bash
git commit -s -m "Describe the change"
```

This adds a line `Signed-off-by: Your Name <your address>` to the commit message. Use your
real name. A pull request with a commit that has no sign-off fails the DCO check and cannot
be merged. There is no separate contributor licence agreement.

Your contribution is licensed under the Apache License, Version 2.0, like the rest of the
repository (see [`LICENSE`](LICENSE)). Every new source file starts with the two header lines
that the other files of the same language carry:

```
Copyright 2026 Brutor AI Ltd.
SPDX-License-Identifier: Apache-2.0
```

## Rules for every change

1. **A defect gets a failing test first.** Write the test that shows the bug, see it fail,
   then fix the bug. If the bug is one of a kind that can come back elsewhere, add a guard
   test for the whole kind, and show that the guard fails on the shape it forbids.
2. **Never weaken a gate to get a green build.** Do not delete or loosen a test, an
   assertion, a lint, a dependency rule, a timeout or a refusal path, and do not skip hooks
   with `--no-verify`. If you think a gate is wrong, say so in the pull request, with the
   reason, and change it in the open.
3. **Caps only go down.** Lint warnings and lint suppressions are counted, and the count is
   a cap (see each component below). A change may lower a cap. It may never raise one.
4. **Fail closed.** A control that cannot decide refuses the request. Do not turn an error
   into "nothing found", zero, an empty list or a default value on a path that enforces
   something, and do not cache a result that an error produced. An unknown value means the
   most restrictive option.
5. **No default credentials.** Never write a stock password, key or token into code, tests
   or documentation.
6. **No backwards compatibility layers.** Do not add shims or aliases for an old route, name
   or import path, and do not mark things "deprecated". Remove what is replaced.
7. **Do not duplicate large pieces of code.** Extract and reuse.
8. **Report evidence.** In the pull request say what you ran and what it showed. If you
   could not run something, say that.
9. **A schema change is a migration.** The Control Plane owns the database schema. Add a
   revision under `control-plane/alembic/versions/` for every schema change. The Core
   Gateway never changes the schema.
10. **Dependencies are reviewed.** For a new or upgraded dependency, say in the pull request
    why it is needed, how well it is maintained, its licence and what it pulls in. Lock files
    are committed and builds use them.

## The edition boundary

[`editions.yaml`](editions.yaml) is the one place where the boundary between the Community
Edition and the Enterprise Edition is written down. Everything it does not list as Enterprise
is Community.

- Community code never imports Enterprise code and never names an Enterprise package.
- Community code never reads or writes an Enterprise-only table, except in the modules that
  `editions.yaml` lists under `enterprise_table_readers`, each with its reason.
- The database schema is not split: all models and all migrations live in
  `control-plane/`.
- Enterprise features attach through the extension interfaces, and a change to one of them
  is a change to a public interface: `core-gateway/src/builder.rs`,
  `control-plane/app/composition/`, and the extension the Admin Console application accepts.
- If your change adds, renames or removes something that `editions.yaml` names (a route
  prefix, a module, a table, a navigation id), update `editions.yaml` in the same pull
  request and run [`check_editions.py`](check_editions.py).

The tests that hold the boundary:

| Component | Tests |
|---|---|
| Core Gateway | `core-gateway/tests/community_source_guard.rs` |
| Control Plane | `control-plane/tests/test_edition_import_guard.py`, `test_community_import_probe.py`, `test_edition_table_guard.py`, `test_packaging.py` |
| Admin Console | `admin-console/src/app/__tests__/editionBoundary.test.ts`, the ESLint rule `community/no-enterprise-imports`, `npm run scan:bundle` |

## Critical files in the Core Gateway

A bug in the Core Gateway is usually a governance bug: a control that silently does not
apply. Changes are held to a standard that depends on what they touch.

| Class | Paths under `core-gateway/` (examples) | Needed before the change is done |
|---|---|---|
| **Critical** | `src/services/auth.rs`, `src/middleware/tenant.rs`, `src/services/guardrails/`, `src/services/usage.rs`, `src/services/cache/group_cache.rs`, `src/services/cache/usage_state.rs`, `src/services/argument_policy/`, `src/services/agent_approval.rs`, `src/services/run_*.rs`, `src/services/accepted_callers.rs`, `src/services/outbound_policy.rs`, request classification in `src/routes/mcp.rs`, everything in `crates/` (evidence) | A failing test first; a test that attacks or breaks the failure path; the full suite green |
| **High** | Concurrency and accounting (background writer, retry, stream handlers), parsers (SSE, A2A codecs, CEL), cache invalidation | Property, fuzz, concurrency or real-datastore evidence, as fits |
| **Standard** | Isolated features | The gates and targeted tests |
| **Low** | Documentation, formatting, tooling | The fast gates |

Two more rules for the Core Gateway:

- **Safe Rust.** No `unsafe` in production code.
- **Limits are admitted, never read and then decided.** A usage limit (quota, budget, run or
  session cap, rate, in-flight slot) is part of the request's one atomic admission
  (`src/services/limits.rs`): reserved for the worst case before the call and settled to the
  actual amount after it. Never compare a counter you have just read, never charge a limit
  only after the response, never fall back to a counter held in one process. Redis is
  required. `tests/limits_guard.rs` and `tests/fail_closed_guard.rs` hold these rules.
  A test with an in-memory stand-in is not evidence that a limit holds across instances:
  that needs a test against a real Redis.

## Gates

Run the gates of every component you touched. All of them must pass before a pull request
is merged; CI runs the same commands.

### Core Gateway (`core-gateway/`)

```bash
cargo fmt --all -- --check
cargo clippy --locked --workspace --all-targets --all-features -- -D warnings
cargo deny --locked check
scripts/lint-ratchet.sh
cargo bench --no-run --locked
cargo nextest run --locked --workspace --lib --tests --features db-tests
```

- The last command needs PostgreSQL and Redis. Use a throwaway PostgreSQL server only, never
  a database you keep. Start it with `-c max_locks_per_transaction=512` and prepare it once
  with `BRUTOR_TEST_DB_IS_DISPOSABLE=1 tests/schema/prepare-test-template.sh`.
- `scripts/lint-ratchet.sh` counts `#[allow(...)]` per lint and fails when a count rises or a
  new lint is suppressed. Do not add a suppression without a stated reason and the narrowest
  scope. Run `scripts/lint-ratchet.sh --update` only after removing suppressions.
- The toolchain is pinned in `rust-toolchain.toml`, and the `rust:` image tag in the
  `Dockerfile` must match it. Change either on purpose, in its own pull request.
- A test that hangs is a defect. Do not raise the time limit in `.config/nextest.toml`.
- Tests named `tests/*_guard.rs` scan the source for a kind of bug. Keep them passing and
  extend them; do not add exceptions.

### Control Plane (`control-plane/`)

```bash
pip install -e ".[dev]"
BRUTOR_TEST_EDITION=community pytest tests/
BRUTOR_TEST_EDITION=community pytest --cov=app tests/     # with the coverage floor
```

- The suite needs no network and no running services. Tests that need PostgreSQL run only
  when you point them at a throwaway server (`BRUTOR_PG_TESTS=1` and `BRUTOR_PG_TEST_URL`);
  each creates and drops its own database there.
- The gates are tests: the coverage floor in `pyproject.toml`, the recorded OpenAPI
  document, the recorded principal matrix and install seed, the edition boundary and the
  packaging check. A recording changes only on purpose; each test says how to re-record it.
- A new field of an API response goes into both the model's `to_dict()` and the Pydantic
  response model. Response models drop fields they do not declare.

### Admin Console (`admin-console/`)

```bash
npm install
npm run type-check
npm run lint
npm run test:run
npm run build
npm run scan:bundle
```

- Node: the version in `.nvmrc`.
- `npm run lint` must report zero errors. Warnings are capped by `--max-warnings` in
  `package.json`, pinned at the measured count: one new warning fails the gate. When you
  clear warnings, lower the cap and delete the rule's downgrade line at the bottom of
  `eslint.config.js`. Never raise the cap. `npm run lint:report` groups the violations by
  rule.
- Values from `JSON.parse`, a caught error or a `postMessage` payload are `unknown`, not
  `any`. Read them through `src/lib/narrow.ts` (`errorMessage`, `getString`, `getNumber`,
  `getRecord`). `catch (e: any)` is not allowed.
- `npm run scan:bundle` checks that the build contains no Enterprise route or navigation id.

### Skill Runner (`skill-runner/`)

```bash
cargo fmt --all -- --check
cargo clippy --all-targets -- -D warnings
cargo test --all-targets
cargo audit
```

### The images and the package (`package/`, `deploy/compose/`)

`package/docker-bake.hcl` is the one place the image names are written. A new image, or a
change to how one is built, goes there; every workflow builds through it.

If you change a Dockerfile, the Compose file or the start scripts, start the package from
nothing, built from your clone, and check that every service becomes healthy:

```bash
cd deploy/compose
docker compose -f docker-compose.yml -f docker-compose.build.yml down -v
COMPOSE_FILE=docker-compose.yml:docker-compose.build.yml ./start.sh
```

The workflow `.github/workflows/compose.yml` does the same on every pull request.

## Pull requests

- Describe what changes and why, and how you tested it.
- Fill in the checklist of the pull request template.
- A maintainer reviews the change. Changes to the Critical files above get a closer review
  and may take longer.
