#!/usr/bin/env python3
# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Generate `test_schema_superset.sql` from the committed schema fixture.

`tests/test_helpers.rs` used to hand-declare the tables the integration
suite needs. That list drifted behind the application — six tables were a
combined 113 columns short — and the resulting failures never mentioned a
table or a column: a missing column fails the query, and the caller turns
that into a 503, a 500, an empty result, or a silently dropped telemetry
row. Four test binaries were failing that way at once, each hidden behind
the previous one because cargo stops at the first failing binary.

So the test schema is now derived from `tests/schema/schema.sql`, the
pg_dump that the `db-tests` suite already builds from and that CI already
drift-checks against a fresh control-plane install. This script is the
derivation, and `--check` is what stops the derived file going stale in
turn — without it, adding a table would leave CI green here and the next
person debugging a 503 that means "missing column".

pg_dump splits a table's definition across four kinds of statement, and
all four matter:

  * `CREATE TYPE ... AS ENUM` — separate from the tables that use them.
    `CREATE TYPE` has no `IF NOT EXISTS`, hence the DO block.
  * `CREATE TABLE` — the column list, with no constraints.
  * `ALTER TABLE ... ADD CONSTRAINT ... PRIMARY KEY/UNIQUE` — `ON CONFLICT`
    infers its arbiter from these, so without them an upsert fails with
    "no unique or exclusion constraint matching the ON CONFLICT
    specification".
  * `ALTER TABLE ... ALTER COLUMN ... SET DEFAULT nextval(...)` — the
    serial columns. Drop these and `INSERT ... RETURNING id` hands back
    NULL, which surfaces as `ColumnDecode { UnexpectedNullError }`.

Two deliberate departures from the real schema:

  * **NOT NULL is stripped.** Tests insert only the columns they care
    about; enforcing the real constraints would fail rows the suite has
    always been allowed to write. The cost is that a column with a NOT
    NULL and no default now arrives as NULL — which is why
    `ensure_test_schema` seeds the default tenant's status flags
    explicitly rather than relying on them.
  * **Foreign keys are omitted.** Tests seed narrow slices of the graph
    and would otherwise have to satisfy the whole thing.

Usage:
    ./tests/schema/gen-test-superset.py            # rewrite the file
    ./tests/schema/gen-test-superset.py --check    # fail if it is stale (CI)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The fixture lives in `tests/migrations/`, which sqlx treats as a
# migrations directory — `sqlx::test` parses every .sql file there and
# rejects one without an integer version prefix. The generated superset
# therefore lives here, outside that directory, or it breaks the whole
# db-tests suite at compile time.
FIXTURE = HERE / "schema.sql"
TARGET = HERE / "test_schema_superset.sql"

HEADER = """-- GENERATED FILE — DO NOT EDIT.
--
-- Produced by `tests/schema/gen-test-superset.py` from
-- `tests/schema/schema.sql`. Re-run that script after regenerating the
-- fixture; CI fails if this file is stale.
--
-- Every table the application knows about, permissive: NOT NULL and
-- foreign keys are omitted so tests can insert narrow rows. See the
-- script's docstring for why each piece is here.
"""


def build() -> str:
    fixture = FIXTURE.read_text()
    parts: list[str] = [HEADER]

    # ── Enum types ────────────────────────────────────────────────────
    types = re.findall(r"CREATE TYPE public\.(\w+) AS ENUM \((.*?)\n\);", fixture, re.S)
    if types:
        parts.append(
            "\n-- Enum types. CREATE TYPE has no IF NOT EXISTS, so each is\n"
            "-- wrapped in a DO block that swallows duplicate_object."
        )
        for name, body in types:
            values = " ".join(line.strip() for line in body.split("\n") if line.strip())
            parts.append(
                f"DO $$ BEGIN\n"
                f"    CREATE TYPE {name} AS ENUM ({values});\n"
                f"EXCEPTION WHEN duplicate_object THEN NULL;\n"
                f"END $$;"
            )

    # Columns whose default is a sequence: pg_dump emits those as separate
    # ALTER COLUMN statements, so collect them before rendering the tables.
    serial: dict[str, set[str]] = {}
    for m in re.finditer(
        r"ALTER TABLE ONLY public\.(\w+) ALTER COLUMN (\w+) SET DEFAULT nextval", fixture
    ):
        serial.setdefault(m.group(1), set()).add(m.group(2))

    # ── Tables ────────────────────────────────────────────────────────
    tables = re.findall(r"CREATE TABLE public\.(\w+) \((.*?)\n\);", fixture, re.S)
    parts.append("\n-- Tables.")
    for name, body in tables:
        cols = []
        for line in body.split("\n"):
            line = line.strip().rstrip(",")
            if not line:
                continue
            # Column nullability only. A table CONSTRAINT's own `IS NOT
            # NULL` (a lifecycle CHECK) is an expression, and stripping
            # it there leaves `IS))` — invalid SQL for every consumer.
            if not line.startswith("CONSTRAINT "):
                line = line.replace(" NOT NULL", "")
            column = line.split()[0]
            if column in serial.get(name, ()):
                # Keep it auto-numbering, or RETURNING id yields NULL.
                line = re.sub(
                    r"^(\w+) (integer|bigint)\b.*$",
                    r"\1 \2 GENERATED BY DEFAULT AS IDENTITY",
                    line,
                )
            cols.append("    " + line)
        parts.append(
            f"CREATE TABLE IF NOT EXISTS {name} (\n" + ",\n".join(cols) + "\n);"
        )

    # ── Uniqueness ────────────────────────────────────────────────────
    # Emitted as indexes rather than ADD CONSTRAINT: only CREATE INDEX
    # supports IF NOT EXISTS, and an index satisfies ON CONFLICT equally.
    unique: list[str] = []
    for m in re.finditer(
        r"ALTER TABLE ONLY public\.(\w+)\s*\n?\s*ADD CONSTRAINT (\w+) PRIMARY KEY \(([^)]+)\);",
        fixture,
    ):
        unique.append(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {m.group(1)}_pk_ix "
            f"ON {m.group(1)} ({m.group(3).strip()});"
        )
    for m in re.finditer(
        r"ALTER TABLE ONLY public\.(\w+)\s*\n?\s*ADD CONSTRAINT (\w+) UNIQUE \(([^)]+)\);",
        fixture,
    ):
        unique.append(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {m.group(2)}_ix "
            f"ON {m.group(1)} ({m.group(3).strip()});"
        )
    for m in re.finditer(
        r"CREATE UNIQUE INDEX (\w+) ON public\.(\w+) USING btree \((.*?)\)(\s+WHERE \(.*?\))?;",
        fixture,
        re.S,
    ):
        where = re.sub(r"\bpublic\.", "", (m.group(4) or "")).strip()
        suffix = f" {where}" if where else ""
        unique.append(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {m.group(1)} "
            f"ON {m.group(2)} ({m.group(3)}){suffix};"
        )

    parts.append(
        "\n-- Primary keys, unique constraints and unique indexes (including\n"
        "-- partial ones). ON CONFLICT infers its arbiter from these."
    )
    parts.extend(unique)

    sql = "\n\n".join(parts) + "\n"
    # The dump qualifies everything with the schema; the test database uses
    # the default search_path.
    return re.sub(r"\bpublic\.(\w+)", r"\1", sql)


def main() -> int:
    if not FIXTURE.exists():
        print(f"ERROR: fixture not found at {FIXTURE}", file=sys.stderr)
        return 1

    generated = build()

    if "--check" in sys.argv:
        if not TARGET.exists():
            print(f"ERROR: {TARGET.name} is missing. Run {Path(__file__).name}.", file=sys.stderr)
            return 1
        if TARGET.read_text() != generated:
            print(
                f"ERROR: {TARGET.name} is stale — it does not match what\n"
                f"       {FIXTURE.name} generates.\n\n"
                f"The integration suite builds its database from that file, so a\n"
                f"stale copy means tests run against a schema the application no\n"
                f"longer has. The failures that causes do not mention a table or a\n"
                f"column — they surface as 503s, 500s and empty results.\n\n"
                f"Fix: ./tests/schema/{Path(__file__).name}  and commit the result.",
                file=sys.stderr,
            )
            return 1
        print(f"{TARGET.name} is up to date.")
        return 0

    TARGET.write_text(generated)
    tables = generated.count("CREATE TABLE IF NOT EXISTS")
    indexes = generated.count("CREATE UNIQUE INDEX IF NOT EXISTS")
    types = generated.count("CREATE TYPE")
    print(f"Wrote {TARGET.relative_to(HERE.parent.parent)}: "
          f"{tables} tables, {types} enum types, {indexes} unique indexes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
