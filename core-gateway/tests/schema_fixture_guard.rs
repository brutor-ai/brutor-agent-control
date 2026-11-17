// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The schema migration is the generated wrapper of the schema fixture:
//! `tests/schema/schema.sql` is the pg_dump, and
//! `tests/migrations/00000000000000_schema.sql` embeds it verbatim, stamped
//! with its SHA-256. An edit to one without regenerating the other would
//! build test databases from a schema nobody reviewed, or make a prepared
//! template's stamp match a schema it does not hold.
use sha2::{Digest, Sha256};

fn read(path: &str) -> String {
    std::fs::read_to_string(path).unwrap_or_else(|e| panic!("{path}: {e}"))
}

/// Whether `migration` is the wrapper of `schema`.
fn wraps(migration: &str, schema: &str) -> bool {
    let digest = hex::encode(Sha256::digest(schema.as_bytes()));
    migration.contains(&format!("-- schema-sha256: {digest}\n"))
        && migration.contains(&format!("'{digest}'"))
        && migration.contains(&format!(
            "EXECUTE $brutor_schema$\n{schema}$brutor_schema$;"
        ))
}

#[test]
fn the_migration_wraps_the_current_schema_fixture() {
    let schema = read("tests/schema/schema.sql");
    let migration = read("tests/migrations/00000000000000_schema.sql");
    assert!(
        wraps(&migration, &schema),
        "tests/migrations/00000000000000_schema.sql is stale: run tests/schema/gen-migration.py"
    );
}

#[test]
fn the_guard_sees_a_stale_wrapper() {
    let schema = read("tests/schema/schema.sql");
    let migration = read("tests/migrations/00000000000000_schema.sql");
    let edited = schema.replacen("CREATE TABLE", "CREATE UNLOGGED TABLE", 1);
    assert!(
        !wraps(&migration, &edited),
        "a schema edit without regenerating"
    );
    let hand_edited = migration.replacen("CREATE TABLE", "CREATE UNLOGGED TABLE", 1);
    assert!(
        !wraps(&hand_edited, &schema),
        "a hand edit of the migration"
    );
}

// ---------------------------------------------------------------------------
// The test helper declares nothing the recorded schema does not have
// ---------------------------------------------------------------------------

use std::collections::{BTreeMap, BTreeSet};

/// `table -> columns` of every `CREATE TABLE IF NOT EXISTS` in `sql`. A line
/// of a table's body that starts with a constraint keyword names no column.
fn declared_tables(sql: &str) -> BTreeMap<String, BTreeSet<String>> {
    const CONSTRAINTS: [&str; 5] = ["PRIMARY", "UNIQUE", "CONSTRAINT", "FOREIGN", "CHECK"];
    let mut tables = BTreeMap::new();
    let mut rest = sql;
    while let Some(at) = rest.find("CREATE TABLE IF NOT EXISTS ") {
        rest = &rest[at + "CREATE TABLE IF NOT EXISTS ".len()..];
        let Some(open) = rest.find('(') else { break };
        let name = rest[..open]
            .trim()
            .trim_start_matches("public.")
            .to_string();
        // Prose that mentions the statement names no table.
        if name.is_empty() || !name.chars().all(|c| c.is_ascii_alphanumeric() || c == '_') {
            continue;
        }
        let Some(close) = rest.find("\n);").or_else(|| rest.find(");")) else {
            break;
        };
        let mut columns = BTreeSet::new();
        for line in rest[open + 1..close].lines() {
            let line = line.trim();
            let Some(first) = line.split_whitespace().next() else {
                continue;
            };
            if line.starts_with("--") || CONSTRAINTS.contains(&first.to_uppercase().as_str()) {
                continue;
            }
            columns.insert(first.trim_matches('"').trim_end_matches(',').to_string());
        }
        tables
            .entry(name)
            .or_insert_with(BTreeSet::new)
            .extend(columns);
        rest = &rest[close..];
    }
    tables
}

/// What the helper's own DDL (`helper`) says that the generated schema
/// (`superset`) does not: a column added to a table by hand, a table or a
/// column the recording does not have.
fn helper_schema_drift(helper: &str, superset: &str) -> Vec<String> {
    let mut drift = Vec::new();
    if helper.contains("ADD COLUMN") {
        drift.push(
            "the helper adds a column to a table (`ADD COLUMN`): columns come from the \
             generated schema only"
                .to_string(),
        );
    }
    let recorded = declared_tables(superset);
    for (table, columns) in declared_tables(helper) {
        match recorded.get(&table) {
            None => drift.push(format!("`{table}`: not a table of the recorded schema")),
            Some(known) => {
                for column in columns.difference(known) {
                    drift.push(format!(
                        "`{table}.{column}`: not a column of the recorded schema"
                    ));
                }
            }
        }
    }
    drift
}

/// `tests/test_helpers.rs` builds the non-`sqlx::test` binaries' schema from
/// the generated superset of the recording (`gen-test-superset.py --check`
/// keeps that current). What it still declares by hand is checked against
/// the same recording here: a hand-kept table or column the product no
/// longer has would let a test pass on a schema no installation runs.
#[test]
fn the_test_helper_declares_nothing_the_recorded_schema_lacks() {
    let drift = helper_schema_drift(
        &read("tests/test_helpers.rs"),
        &read("tests/schema/test_schema_superset.sql"),
    );
    assert!(
        drift.is_empty(),
        "tests/test_helpers.rs declares schema the recording does not have:\n{}",
        drift.join("\n")
    );
}

#[test]
fn the_helper_guard_sees_hand_kept_schema() {
    let superset = "CREATE TABLE IF NOT EXISTS public.tenants (\n    id character varying(255),\n    \
                    name character varying(255)\n);\n";
    let in_step = "CREATE TABLE IF NOT EXISTS tenants (\n    id VARCHAR(255) PRIMARY KEY,\n    \
                   -- a comment\n    name VARCHAR(255) NOT NULL,\n    UNIQUE (name)\n);";
    assert_eq!(helper_schema_drift(in_step, superset), Vec::<String>::new());
    // The shape that was there: columns widened by hand, some of them
    // columns the recording had dropped.
    let widened =
        format!("{in_step}\nALTER TABLE tenants ADD COLUMN IF NOT EXISTS max_guardrails integer;");
    assert_eq!(helper_schema_drift(&widened, superset).len(), 1);
    let stale_column = in_step.replace("name VARCHAR(255) NOT NULL,", "max_storage_gb INTEGER,");
    assert_eq!(
        helper_schema_drift(&stale_column, superset),
        ["`tenants.max_storage_gb`: not a column of the recorded schema"]
    );
    let stale_table = "CREATE TABLE IF NOT EXISTS gone (\n    id INTEGER\n);";
    assert_eq!(
        helper_schema_drift(stale_table, superset),
        ["`gone`: not a table of the recorded schema"]
    );
}
