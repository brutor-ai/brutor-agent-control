# Database Migrations (Alembic)

Brutor Agent Control Plane uses [Alembic](https://alembic.sqlalchemy.org/) for database schema migrations.

## How it works

- SQLAlchemy models (`app/database/models/`) define the desired schema.
- Alembic tracks schema versions in the `alembic_version` table.
- Migration scripts live in `alembic/versions/`.
- On startup, `init_database()` runs `alembic upgrade head` to apply any pending migrations.

## Workflow for schema changes

1. Edit a model in `app/database/models/`.
2. Generate a migration:
   ```bash
   alembic revision --autogenerate -m "describe your change"
   ```
3. Review the generated file in `alembic/versions/`. Autogenerate is a starting point — verify:
   - Column types are correct
   - Server defaults match the model
   - `NOT NULL` columns added to populated tables have a default or backfill strategy
   - Data migrations are separate from schema migrations
4. Test locally:
   ```bash
   alembic upgrade head
   # ...test the app...
   alembic downgrade -1  # verify rollback works
   alembic upgrade head
   ```
5. Commit the migration file with your model changes. Next deploy applies it automatically.

## Changes that alter how a metric is MEASURED

A schema change is not the only change that needs a declared, one-time
step. **A change that alters how a drift metric is measured must add an
entry to `app/constants/measurement_changes.py`** —
for example a new definition of a run's client label, a new source
column for `input_bytes`, a different way of counting actions. Otherwise
every AI System's samples shift on upgrade day and the drift detector
reports it as the agent changing (a changed definition of the client
label, unregistered, fired `client_mix` drift three times and demoted a
system once).

- Add a `MeasurementChange` (id `mNNNN_<slug>`, the metric keys, the bases,
  the reason, the release) — append-only, never edit a released entry.
- Set `MEASUREMENT_REGISTRY_HEAD` in the core
  (`src/services/drift/measurement.rs`) to the new id and re-pin its
  `MEASUREMENT_DEFINITIONS_SHA256`. The core's
  `the_metric_definitions_are_the_reviewed_ones` test fails when the metric
  definitions change without this; the control plane's
  `tests/test_measurement_changes.py` fails when the two heads disagree.
- Nothing else: `init_database()` applies each entry once per installation
  after migrations (a `measurement_change` baseline epoch for every system
  with a baseline on the affected metric, recorded in
  `platform_measurement_changes`); baselines re-learn from the upgrade and
  the assurance report lists the change with its reason.

## Common commands

| Command | What it does |
|---------|--------------|
| `alembic current` | Show the current revision |
| `alembic history` | List all revisions |
| `alembic upgrade head` | Apply all pending migrations |
| `alembic upgrade +1` | Apply one migration |
| `alembic downgrade -1` | Roll back one migration |
| `alembic revision --autogenerate -m "msg"` | Generate a migration from model changes |
| `alembic stamp head` | Mark current DB as up-to-date without running migrations |

## Behavior on startup

`init_database()` runs `alembic upgrade head`:

- **Empty DB**: the baseline revision (`a000_baseline`, the whole schema) and every revision after it.
- **Alembic-managed DB**: the revisions it lacks.
- **Tables but no `alembic_version`**: refused; it was not made by this release.

Then, still inside the advisory lock: seeding, then pending platform
measurement changes (see above). Reference data (providers, compliance
frameworks, roles and permissions, catalogs) is written by the seeding, never
by a revision.

`tests/test_alembic_head_is_the_models_schema.py` (Postgres) builds one
database with the revisions and one with `create_all()` and requires the same
schema: a revision and its model change land together.

## Operational notes

- **Concurrent workers**: startup uses a PostgreSQL advisory lock so only one Gunicorn worker runs migrations at a time.
- **Aurora Serverless**: migrations run against a single connection; scaling events may drop the connection on long-running migrations. Keep migrations short; run data backfills as separate operational steps.
- **Rollback**: every migration should define both `upgrade()` and `downgrade()` so rollback is possible.
- **Data migrations**: keep separate from schema migrations. Schema migration adds the column; a follow-up Python script or separate data-migration revision populates it.
