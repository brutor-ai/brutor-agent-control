# Integration tests

The tests under `tests/integration/` run the gateway's routes and services against a real
Postgres and a real Redis. They are one test binary (`integration`, entry `main.rs`), compiled
only with the `db-tests` feature, so a plain `cargo test` needs no database.

The Enterprise workspace runs the same files a second time, on the Enterprise composition
(`enterprise/tests/integration/main.rs` includes this directory's `main.rs` by path), next to
the Enterprise features' own tests.

## Run

Only ever against a throwaway Postgres. **Never the development database `ai_gateway`**:
`#[sqlx::test]` creates and drops databases on the server it is given.
Two containers on non-default ports are enough:

```bash
docker run -d --name brutor-test-pg -p 127.0.0.1:25432:5432 \
  -e POSTGRES_USER=brutor -e POSTGRES_PASSWORD=throwaway -e POSTGRES_DB=brutor_test \
  postgres:16 -c max_locks_per_transaction=512 -c fsync=off
docker run -d --name brutor-test-redis -p 127.0.0.1:26379:6379 redis:7.4-alpine
```

```bash
export DATABASE_URL=postgresql://<user>:<password>@localhost:<port>/<database>   # the throwaway server
export TEST_REDIS_URL=redis://localhost:<port>/15
export BRUTOR_TEST_DB_IS_DISPOSABLE=1

BRUTOR_TEST_DB_IS_DISPOSABLE=1 tests/schema/prepare-test-template.sh   # once per server, and after the schema fixture changes
cargo nextest run --locked --workspace --lib --tests --features db-tests
```

The test states are built without Redis (`AppConfig::for_test`, `UsageState::new(None)`, the
in-memory twin) unless a test connects to `TEST_REDIS_URL` itself; no variable switches Redis
off. The settings the services read where they use them (the evidence key, the skill runner,
the outbound policy) come from the process environment through the registry, under the
gateway's own rules: a retired variable name left in the shell (the list is in the root
`README.md`) fails the tests that reach such a setting, with the refusal as the message. A test that changes the environment calls `env_vars::reload_for_tests()`. A served gateway refuses to start without Redis. Tests that
prove a limit across replicas use the real Redis (`admission_redis.rs`, `quota_counter_redis.rs`,
`limits_http.rs`, `media_budget_settle.rs`): they take key names of their own and never flush.

One test: `cargo nextest run --locked --features db-tests --test integration -E 'test(<name>)'`.

## Trees a test reads

A test that opens a file outside its own crate's `src/` asks one function where the tree is,
`tests/support/roots.rs` (`roots::tree("enterprise")`, `"control-plane"`, `"edition-records"`,
`"community"`), and never spells the layout itself (`tests/roots_guard.rs` holds that).

| Variable | Tree | Default | When it is not there |
|---|---|---|---|
| `BRUTOR_ENTERPRISE_ROOT` | the Enterprise workspace | `enterprise/` under the Community root, when it is there | not given: the guards read Community code only. **Given and without source: the run fails** |
| `BRUTOR_CONTROL_REPO` | the control plane (`crypto_interop_v1.rs`, `skill_limits_contract.rs`, the evidence parity fixture) | `../brutor-gateway-control`, else `../control-plane` (its directory in the public monorepo) | the test fails; with `BRUTOR_TEST_SIBLINGS=optional` it is skipped and the skip is recorded |
| `BRUTOR_SKILL_RUNNER_REPO` | the skill runner (`skill_limits_contract.rs`, the shared scanner and registry checks) | `../brutor-skill-runner`, else `../skill-runner` (its directory in the public monorepo) | the test is skipped and the skip is recorded |
| `BRUTOR_EDITION_RECORDS_DIR` | the directory that holds `editions.yaml` and the edition-install contract (`edition_guard_contract.rs`, `tenant_paths_guard.rs`) | none: it is kept outside this repository, and only the variable says where | not set: the tests that read it are skipped, each with a `SKIPPED:` line that names the variable, and recorded. **Set and not a directory: the run fails** |
| `BRUTOR_COMMUNITY_ROOT` | this crate | found from the crate being compiled | (always there) |

A recorded skip is a `SKIPPED:` line on stderr and, when `BRUTOR_TEST_SKIP_LOG` names a file, a
line in it. `scripts/ci-community-alone.sh` sets both variables and prints the skips in its
summary.

## How a test gets its database

`#[sqlx::test(migrations = "./tests/migrations")]` creates a database per test and applies the
one migration, `tests/migrations/00000000000000_schema.sql`. That file is generated
(`tests/schema/gen-migration.py`) from `tests/schema/schema.sql`, a `pg_dump --schema-only` of a
database the control plane built, and wraps it in a guard:

- the database was cloned from a `template1` stamped with this schema's SHA-256
  (`prepare-test-template.sh`): the replay is skipped;
- the template holds another schema: the migration fails and says to re-stamp, so no test runs
  against a stale schema;
- no stamped template: the dump is replayed (slow, still correct).

`tests/schema_fixture_guard.rs` fails when the migration does not wrap the current fixture.

## When the control plane's schema changes

The gateway never alters the schema; the control plane's Alembic chain does. After a new
revision:

1. Build a scratch database the way a deployment does: the control plane's `init_database()`
   (`create_all` and stamp), not `alembic upgrade head` (the header of
   `tests/migrations/regen-schema.sh` says why).
2. `POSTGRES_CONTAINER=<container> POSTGRES_DB=<scratch database> tests/migrations/regen-schema.sh`
   rewrites `schema.sql` and the migration (`--check` compares without writing).
3. `tests/schema/gen-test-superset.py` rewrites `tests/schema/test_schema_superset.sql`
   (`--check` in CI).
4. Re-stamp the template (`prepare-test-template.sh`) and drop the scratch database.

## Helpers

`common.rs` seeds tenants, groups, keys and models and builds the application: `full_app` and
`edition_app` are what the binary of the edition running the suite serves; `build_llm_app`,
`build_multimodal_app` and the others mount one route family behind the tenant layer. Upstreams
are `wiremock` servers. Time comes from `common::db_now()`, never `Utc::now()`
(`integration_tests_take_time_from_db_now` guards it).

## Recorded matrices

`credential_surface_matrix.rs` and `tenant_resolution_matrix.rs` ask every route with every kind
of credential and compare with `tests/fixtures/`. Re-record only on purpose, on the Enterprise
composition, and say why in the change; the header of each file gives the command. A re-record
writes every cell as it answers now: when only some kinds are meant to change, merge those kinds
into the previous recording.
