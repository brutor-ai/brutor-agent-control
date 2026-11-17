#!/usr/bin/env bash
# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

# Regenerate the schema fixture `tests/schema/schema.sql` from a live
# gateway-control schema, then the migration that wraps it
# (`00000000000000_schema.sql`, via tests/schema/gen-migration.py).
# Run this whenever a gateway-control migration changes the production
# schema — every integration test database is built from this fixture, so
# a stale fixture silently masks drift. After regenerating, re-stamp any
# prepared test template (tests/schema/prepare-test-template.sh).
#
# Dump from a database built the way a deployment builds one: the control
# plane's `init_database()` (create_all + stamp), NOT `alembic upgrade head`.
# The migration chain has no base revision — its first migration ALTERs tables
# it assumes exist — so it cannot construct a schema from empty, and the two
# paths also order columns differently. Dumping a migration-built database
# would produce a fixture that no real install matches.
#
# Prereqs:
#   - Postgres dev container running (docker compose up -d postgres)
#   - Container name: brutor-postgres (the default in the dev compose)
#   - The control plane has booted against that DB at least once
#
# Verifying drift (without overwriting):
#   ./regen-schema.sh --check
#
# Updating the fixture:
#   ./regen-schema.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIXTURE="$SCRIPT_DIR/../schema/schema.sql"
GEN_MIGRATION="$SCRIPT_DIR/../schema/gen-migration.py"
CONTAINER="${POSTGRES_CONTAINER:-brutor-postgres}"
DB="${POSTGRES_DB:-ai_gateway}"
USER="${POSTGRES_USER:-brutor}"

# Two ways to reach a Postgres holding the Alembic-managed schema:
#
#   docker — the dev default. `pg_dump` runs *inside* the container so its
#            version always matches the server.
#   local  — CI, where Postgres is a service container with a generated name
#            and `docker exec` isn't usable. Requires DATABASE_URL and a local
#            pg_dump whose major version matches the server, or the output
#            differs and the drift check fires spuriously.
#
# Forced with BRUTOR_SCHEMA_DUMP_MODE=docker|local; otherwise auto-detected.
MODE="${BRUTOR_SCHEMA_DUMP_MODE:-}"
if [ -z "$MODE" ]; then
    if docker exec "$CONTAINER" pg_isready -U "$USER" >/dev/null 2>&1; then
        MODE=docker
    elif [ -n "${DATABASE_URL:-}" ] && command -v pg_dump >/dev/null 2>&1; then
        MODE=local
    else
        echo "ERROR: no Postgres reachable." >&2
        echo "  docker mode: container '$CONTAINER' is not running." >&2
        echo "    Try: docker compose -f brutor-gateway-control/docker-compose.yml up -d postgres" >&2
        echo "  local mode:  set DATABASE_URL and install a matching pg_dump." >&2
        exit 1
    fi
fi

dump_schema() {
    if [ "$MODE" = docker ]; then
        docker exec "$CONTAINER" pg_dump "$@" -U "$USER" "$DB"
    else
        pg_dump "$@" "$DATABASE_URL"
    fi
}

TMP="$(mktemp -t schema.XXXXXX.sql)"
trap 'rm -f "$TMP"' EXIT

# The `-- Dumped from/by ... version` header lines are dropped: they carry
# the packaging build string, which differs between environments (`16.14`
# vs `16.14 (Debian ...)` vs `16.14 (Ubuntu ...)`) even when the schema is
# byte-identical. Comparing them makes the drift check fail on where it ran
# rather than on what the schema is.
#
# Server-version pg_dump runs inside the container — avoids local vs
# server major-version mismatches. `--no-comments` and the restrict
# filter keep the output sqlx-importable (no \restrict / \unrestrict).
# `--schema=public` skips the `_sqlx_test` bookkeeping schema that
# `sqlx::test` creates in the dev DB when integration tests have
# previously run — otherwise the drift check fires on something that
# doesn't belong to the production schema.
dump_schema \
    --schema-only --no-owner --no-privileges --no-comments \
    --exclude-schema=_sqlx_test \
    --exclude-table=alembic_version \
    | grep -vE '^\\(restrict|unrestrict)' \
    | grep -vE '^-- Dumped (from database|by pg_dump) version' \
    > "$TMP"

# Restore the default search_path — pg_dump emits a `SET search_path TO ''`
# at the top which breaks sqlx's `_sqlx_migrations` lookup.
cat >> "$TMP" <<'SQL'

-- Restore default search_path so sqlx can write _sqlx_migrations to public.
SELECT pg_catalog.set_config('search_path', 'public', false);
SQL

if [[ "${1:-}" == "--check" ]]; then
    if ! diff -q "$FIXTURE" "$TMP" >/dev/null; then
        echo "SCHEMA DRIFT detected — fixture is out of date." >&2
        echo "  Run: tests/migrations/regen-schema.sh (without --check) to update." >&2
        diff "$FIXTURE" "$TMP" | head -40
        exit 1
    fi
    echo "Schema fixture is up to date."
    exec python3 "$GEN_MIGRATION" --check
fi

mv "$TMP" "$FIXTURE"
echo "Regenerated $FIXTURE"
python3 "$GEN_MIGRATION"
