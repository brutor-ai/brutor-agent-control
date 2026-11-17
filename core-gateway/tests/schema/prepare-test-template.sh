#!/usr/bin/env bash
# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

# Load the schema fixture into `template1` of a THROWAWAY Postgres, stamped
# with its SHA-256, so every `#[sqlx::test]` database starts with the schema.
# sqlx creates each test database with a plain `CREATE DATABASE`,
# which clones `template1`; the migration (tests/migrations/
# 00000000000000_schema.sql) sees the stamp and skips its ~13 000-line
# replay. Without this the suite still passes, it is just much slower.
#
# DANGER: every database later created on this server, by anything, starts
# with the gateway schema. Never run it against a server that holds or will
# hold real data. It refuses unless
# BRUTOR_TEST_DB_IS_DISPOSABLE=1, which the throwaway test setups set.
#
# Usage (DATABASE_URL = the suite's URL; only host, port and credentials
# are used):
#   BRUTOR_TEST_DB_IS_DISPOSABLE=1 tests/schema/prepare-test-template.sh
# Re-run after the fixture changes: the migration refuses a template whose
# stamp no longer matches.
set -euo pipefail

if [ "${BRUTOR_TEST_DB_IS_DISPOSABLE:-}" != "1" ]; then
    echo "REFUSED: set BRUTOR_TEST_DB_IS_DISPOSABLE=1 to confirm this Postgres is a throwaway test server." >&2
    exit 1
fi
: "${DATABASE_URL:?set DATABASE_URL to the test server}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCHEMA="$HERE/schema.sql"
DIGEST="$(python3 -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$SCHEMA")"
# The same server, database template1.
TEMPLATE_URL="$(python3 - "$DATABASE_URL" <<'PY'
import sys
from urllib.parse import urlsplit, urlunsplit
u = urlsplit(sys.argv[1])
print(urlunsplit((u.scheme, u.netloc, "/template1", u.query, u.fragment)))
PY
)"
PSQL=(psql -X -q -v ON_ERROR_STOP=1 "$TEMPLATE_URL")

stamped=""
if [ "$("${PSQL[@]}" -tAc "SELECT to_regclass('public._brutor_test_template') IS NOT NULL")" = "t" ]; then
    stamped="$("${PSQL[@]}" -tAc "SELECT schema_sha256 FROM public._brutor_test_template")"
fi
if [ "$stamped" = "$DIGEST" ]; then
    echo "template1 already holds schema $DIGEST"
    exit 0
fi
tables="$("${PSQL[@]}" -tAc "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")"
if [ -z "$stamped" ] && [ "$tables" != "0" ]; then
    echo "REFUSED: template1 has $tables tables that this script did not put there." >&2
    exit 1
fi
# A stale stamp, or a fresh template1: rebuild the public schema.
"${PSQL[@]}" -c "DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;"
"${PSQL[@]}" -f "$SCHEMA" >/dev/null
"${PSQL[@]}" -c "CREATE TABLE public._brutor_test_template (schema_sha256 text NOT NULL); INSERT INTO public._brutor_test_template VALUES ('$DIGEST');"
echo "template1 now holds schema $DIGEST"
