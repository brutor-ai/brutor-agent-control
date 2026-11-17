// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The Enterprise-data check at start.
//!
//! A Community composition (a state as `AppState::new` leaves it) is put
//! through the checked assembly the builder uses
//! (`GatewayBuilder::assemble_checked`) on a real Postgres holding the
//! control plane's schema. It starts on a clean database and on nothing
//! else: the edition marker, each of the 43 direct checks of the contract
//! (`tests/edition_guard_contract.rs`), and everything that keeps
//! a check from being decided (the marker table gone, a table of a check
//! gone, a statement that fails or does not answer, an answer that is not
//! what the contract describes) is a refusal. An Enterprise composition
//! starts on all of it, reads nothing and writes nothing.
//!
//! What "a row of an Enterprise table" is here: a synthetic row built from
//! the table's own column list ([`insert_rows`]), with foreign keys not
//! checked. The check counts rows; it does not read them.

use std::time::Duration;

use sqlx::PgPool;

use brutor_gateway_core::builder::GatewayBuilder;
use brutor_gateway_core::config::AppConfig;
use brutor_gateway_core::services::AppState;
use brutor_gateway_core::services::edition::Edition;
use brutor_gateway_core::services::edition_guard::{
    self, DirectCheck, Kind, REFUSAL_FIRST_LINE, REFUSAL_FOUND_LINE, REFUSAL_UNDECIDED_LINE,
    REFUSAL_WAYS_FORWARD, Refusal,
};
use brutor_gateway_core::services::telemetry::TelemetryState;

use crate::common;

/// The one tenant of a Community installation.
pub(crate) const TENANT: &str = "default";
/// Every text the tests put into a row starts with this. The refusal must
/// never contain it.
pub(crate) const ROW_TEXT: &str = "ZQX";

pub(crate) fn community_state(pool: PgPool) -> AppState {
    AppState::new(
        AppConfig::for_test("postgresql://placeholder/replaced".to_string()),
        pool,
        None,
        reqwest::Client::new(),
        TelemetryState {
            prometheus_handle: None,
        },
    )
}

/// A state that holds an Enterprise piece: what an Enterprise extension's
/// `install_state` records (`composition.enterprise_piece`), and all the
/// check at start reads of the edition. The Enterprise features themselves
/// are installed by the Enterprise workspace's twin of this file.
pub(crate) fn enterprise_state(pool: PgPool) -> AppState {
    let mut state = community_state(pool);
    state.composition.enterprise_piece("portal");
    state
}

/// The start, as the builder makes it: assembled, then checked.
pub(crate) async fn start(state: AppState) -> Result<(), Refusal> {
    match GatewayBuilder::assemble_checked(state, vec![]).await {
        Ok(_gateway) => Ok(()),
        Err(error) => Err(error
            .downcast::<Refusal>()
            .expect("the start fails with the refusal and nothing else")),
    }
}

pub(crate) async fn start_community(pool: &PgPool) -> Result<(), Refusal> {
    start(community_state(pool.clone())).await
}

/// A Community installation's data: one tenant with Community flags on and
/// Enterprise flags absent or off, an OIDC identity provider, an
/// administrator with a password.
pub(crate) async fn seed_community_data(pool: &PgPool) {
    common::seed_tenant(pool, TENANT).await;
    sqlx::query(
        "UPDATE tenants SET features = '{\"mcp_servers\": true, \"models\": true, \
         \"guardrails\": true, \"knowledge_base\": false, \"mcp_registry\": false}'::json \
         WHERE id = $1",
    )
    .bind(TENANT)
    .execute(pool)
    .await
    .expect("features");
    insert_rows(pool, "idp_servers", 2, &[("protocol", "'oidc'")]).await;
    sqlx::query(
        "UPDATE idp_servers SET protocol = NULL WHERE id = (SELECT min(id) FROM idp_servers)",
    )
    .execute(pool)
    .await
    .expect("a NULL protocol is OIDC");
    insert_rows(pool, "tenant_admins", 1, &[("idp_subject", "NULL")]).await;
}

/// Insert `n` synthetic rows into `table`: every column that must have a
/// value gets one of its type, text values start with [`ROW_TEXT`], and
/// `fixed` sets columns to SQL expressions. Foreign keys are not checked
/// (`session_replication_role = replica`): the rows stand for "the
/// Enterprise Edition wrote here", not for a consistent installation.
pub(crate) async fn insert_rows(pool: &PgPool, table: &str, n: i64, fixed: &[(&str, &str)]) {
    let columns: Vec<(String, String, String, Option<i32>, bool)> = sqlx::query_as(
        "SELECT c.column_name::text, c.data_type::text, c.udt_name::text, \
                c.character_maximum_length::int4, \
                (c.is_nullable = 'NO' AND c.column_default IS NULL \
                 AND c.is_identity = 'NO') AS needed \
         FROM information_schema.columns c \
         WHERE c.table_schema = 'public' AND c.table_name = $1 \
         ORDER BY c.ordinal_position",
    )
    .bind(table)
    .fetch_all(pool)
    .await
    .expect("columns");
    assert!(!columns.is_empty(), "{table} is not in the schema");

    let mut names = Vec::new();
    let mut values = Vec::new();
    for (name, data_type, udt, max_len, needed) in &columns {
        let constrained = CONSTRAINED
            .iter()
            .find(|(t, column, _)| *t == table && column == name)
            .map(|(_, _, expr)| *expr);
        let set = fixed
            .iter()
            .find(|(column, _)| column == name)
            .map(|(_, expr)| *expr)
            .or(constrained);
        if let Some(expr) = set {
            let expr = &expr;
            names.push(format!("\"{name}\""));
            values.push((*expr).to_string());
            continue;
        }
        if !needed {
            continue;
        }
        let value = match data_type.as_str() {
            "character varying" | "text" | "character" => match max_len {
                Some(len) => format!("left('{ROW_TEXT}' || n::text, {len})"),
                None => format!("'{ROW_TEXT}' || n::text"),
            },
            "integer" | "bigint" | "smallint" => "n".to_string(),
            "numeric" | "double precision" | "real" => "0".to_string(),
            "boolean" => "false".to_string(),
            "timestamp with time zone" | "timestamp without time zone" => "now()".to_string(),
            "date" => "current_date".to_string(),
            "json" => "'{}'::json".to_string(),
            "jsonb" => "'{}'::jsonb".to_string(),
            "uuid" => "gen_random_uuid()".to_string(),
            "bytea" => "''::bytea".to_string(),
            "ARRAY" => "'{}'".to_string(),
            "USER-DEFINED" => format!("enum_first(NULL::\"{udt}\")"),
            other => panic!("{table}.{name}: no synthetic value for type {other}"),
        };
        names.push(format!("\"{name}\""));
        values.push(value);
    }
    // Keys of one call never meet another call's.
    static NEXT: std::sync::atomic::AtomicI64 = std::sync::atomic::AtomicI64::new(1);
    let first = NEXT.fetch_add(n, std::sync::atomic::Ordering::Relaxed);
    let sql = format!(
        "INSERT INTO \"{table}\" ({}) SELECT {} FROM generate_series({first}, {}) AS n",
        names.join(", "),
        values.join(", "),
        first + n - 1
    );
    let mut conn = pool.acquire().await.expect("connection");
    sqlx::query("SET session_replication_role = replica")
        .execute(&mut *conn)
        .await
        .expect("replica role (the test server's user is a superuser)");
    let inserted = sqlx::query(&sql).execute(&mut *conn).await;
    sqlx::query("RESET session_replication_role")
        .execute(&mut *conn)
        .await
        .expect("reset role");
    let inserted = inserted.unwrap_or_else(|e| panic!("synthetic rows for {table}: {e}\n{sql}"));
    assert_eq!(inserted.rows_affected(), n as u64, "{table}");
}

/// Columns a CHECK constraint restricts to a list: a value of the list.
pub(crate) const CONSTRAINED: &[(&str, &str, &str)] = &[
    ("kb_connectors", "connector_type", "'slack'"),
    (
        "kb_connector_identity_mappings",
        "connector_type",
        "'slack'",
    ),
    (
        "kb_connector_identity_mappings",
        "mapping_source",
        "'manual'",
    ),
    ("kb_documents", "source", "'upload'"),
];

pub(crate) async fn delete_rows(pool: &PgPool, table: &str) {
    let mut conn = pool.acquire().await.expect("connection");
    sqlx::query("SET session_replication_role = replica")
        .execute(&mut *conn)
        .await
        .expect("replica role");
    let deleted = sqlx::query(&format!("DELETE FROM \"{table}\" WHERE true"))
        .execute(&mut *conn)
        .await;
    sqlx::query("RESET session_replication_role")
        .execute(&mut *conn)
        .await
        .expect("reset role");
    deleted.unwrap_or_else(|e| panic!("delete from {table}: {e}"));
}

pub(crate) async fn insert_marker(pool: &PgPool) {
    sqlx::query(
        "INSERT INTO edition_marker (id, enterprise_first_started_at, \
         enterprise_last_started_at, enterprise_first_version, enterprise_last_version) \
         VALUES (1, '2026-11-11T10:00:00+01:00', '2026-11-12T07:30:00Z', '0.11.40', '0.11.41')",
    )
    .execute(pool)
    .await
    .expect("marker");
}

pub(crate) const MARKER_LINE: &str = "edition marker: the Enterprise Edition first started on this database at \
     2026-11-11T09:00:00Z (version 0.11.40) and last started at 2026-11-12T07:30:00Z (version \
     0.11.41)";

/// The refusal's text for these lines, written out here independently of
/// the code that formats it.
pub(crate) fn refusal_text(found: &[String], undecided: &[String]) -> String {
    let mut text = format!("{REFUSAL_FIRST_LINE}\n");
    if !found.is_empty() {
        text.push_str(&format!("{REFUSAL_FOUND_LINE}\n"));
        for line in found {
            text.push_str(&format!("  - {line}\n"));
        }
    }
    if !undecided.is_empty() {
        text.push_str(&format!("{REFUSAL_UNDECIDED_LINE}\n"));
        for line in undecided {
            text.push_str(&format!("  - {line}\n"));
        }
    }
    text.push_str(REFUSAL_WAYS_FORWARD);
    text
}

/// Put what `check` looks for into the database: `count` of it. Returns the
/// line the refusal must show.
pub(crate) async fn plant(pool: &PgPool, check: &DirectCheck, count: i64) -> String {
    let feature = check.feature.unwrap_or("none");
    match check.kind {
        Kind::Table => {
            insert_rows(pool, check.subject, count, &[]).await;
            let rows = if count == 1 {
                "1 row".to_string()
            } else {
                format!("{count} rows")
            };
            format!("Enterprise table {} ({feature}): {rows}", check.subject)
        }
        Kind::Tenants => {
            // `count` tenants in all: the Community one and the rest.
            for i in 1..count {
                common::seed_tenant(pool, &format!("{ROW_TEXT}-tenant-{i}")).await;
            }
            format!("tenants: {count} (the Community Edition runs a single tenant)")
        }
        Kind::FeatureFlag => {
            assert_eq!(count, 1, "one tenant in a Community database");
            sqlx::query(&format!(
                "UPDATE tenants SET features = (features::jsonb || '{{\"{}\": true}}'::jsonb)::json \
                 WHERE id = '{TENANT}'",
                check.subject
            ))
            .execute(pool)
            .await
            .expect("flag on");
            format!(
                "Enterprise feature flag {} ({feature}): switched on for 1 tenant",
                check.subject
            )
        }
        Kind::Marker => unreachable!("the marker is not a direct check"),
    }
}

pub(crate) async fn unplant(pool: &PgPool, check: &DirectCheck) {
    match check.kind {
        Kind::Table => delete_rows(pool, check.subject).await,
        Kind::Tenants => {
            sqlx::query(&format!("DELETE FROM tenants WHERE id LIKE '{ROW_TEXT}-%'"))
                .execute(pool)
                .await
                .expect("delete tenants");
        }
        Kind::FeatureFlag => {
            sqlx::query(&format!(
                "UPDATE tenants SET features = (features::jsonb - '{}')::json",
                check.subject
            ))
            .execute(pool)
            .await
            .expect("flag off");
        }
        Kind::Marker => unreachable!(),
    }
}

// ---------------------------------------------------------------------------
// A clean database
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_community_assembly_starts_on_a_clean_database(pool: PgPool) {
    // The schema and nothing in it: the control plane has not seeded yet.
    assert_eq!(start_community(&pool).await, Ok(()));
    // A Community installation's own data is not Enterprise data.
    seed_community_data(&pool).await;
    assert_eq!(start_community(&pool).await, Ok(()));
    let state = community_state(pool.clone());
    assert_eq!(state.composition.edition(), Edition::Community);
}

/// The check reads and never writes: it works unchanged on connections
/// that can only read, where any write would be an error (and so a refusal).
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_start_reads_and_never_writes(pool: PgPool) {
    use sqlx::Executor;
    seed_community_data(&pool).await;
    let read_only = sqlx::postgres::PgPoolOptions::new()
        .max_connections(3)
        .after_connect(|conn, _| {
            Box::pin(async move {
                conn.execute("SET default_transaction_read_only = on")
                    .await?;
                Ok(())
            })
        })
        .connect_with((*pool.connect_options()).clone())
        .await
        .expect("read-only pool");
    assert!(
        sqlx::query("DELETE FROM tenants WHERE false")
            .execute(&read_only)
            .await
            .is_err(),
        "the pool refuses writes"
    );
    assert_eq!(start_community(&read_only).await, Ok(()));
    insert_marker(&pool).await;
    insert_rows(&pool, "batch_jobs", 1, &[]).await;
    let refusal = start_community(&read_only)
        .await
        .expect_err("Enterprise data");
    assert_eq!(
        refusal.to_string(),
        refusal_text(
            &[
                MARKER_LINE.to_string(),
                "Enterprise table batch_jobs (batch_jobs): 1 row".to_string()
            ],
            &[]
        )
    );
    assert_eq!(start(enterprise_state(read_only.clone())).await, Ok(()));
    read_only.close().await;
    // And on a pool that could write, nothing was written: the marker is
    // the control plane's, in either edition.
    assert!(start_community(&pool).await.is_err());
    assert_eq!(start(enterprise_state(pool.clone())).await, Ok(()));
    let marker: (i64, Option<String>) =
        sqlx::query_as("SELECT count(*), max(enterprise_last_version) FROM edition_marker")
            .fetch_one(&pool)
            .await
            .expect("marker");
    assert_eq!(marker, (1, Some("0.11.41".to_string())));
}

// ---------------------------------------------------------------------------
// The marker
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_community_assembly_refuses_a_database_with_the_marker(pool: PgPool) {
    seed_community_data(&pool).await;
    insert_marker(&pool).await;
    let refusal = start_community(&pool)
        .await
        .expect_err("the marker is there");
    assert_eq!(refusal.found.len(), 1);
    assert_eq!(refusal.found[0].kind, Kind::Marker);
    assert!(refusal.undecided.is_empty());
    assert_eq!(
        refusal.to_string(),
        refusal_text(&[MARKER_LINE.to_string()], &[])
    );
}

// ---------------------------------------------------------------------------
// Every direct check
// ---------------------------------------------------------------------------

/// Each of the 43 checks alone, with no marker: the database is refused for
/// that kind and names it and its count, and starts again once it is gone.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_community_assembly_refuses_each_kind_of_enterprise_data(pool: PgPool) {
    seed_community_data(&pool).await;
    assert_eq!(start_community(&pool).await, Ok(()));
    let checks = edition_guard::direct_checks();
    assert_eq!(checks.len(), 43);
    for check in &checks {
        let what = format!("{} {}", check.kind.as_str(), check.subject);
        // As little as is a finding: one row, or the second tenant.
        let count = check.more_than + 1;
        let line = plant(&pool, check, count).await;
        let refusal = start_community(&pool)
            .await
            .expect_err(&format!("{what}: started on Enterprise data"));
        assert!(refusal.undecided.is_empty(), "{what}: {refusal:?}");
        assert_eq!(refusal.found.len(), 1, "{what}: {refusal:?}");
        let found = &refusal.found[0];
        assert_eq!(
            (
                found.kind,
                found.subject.as_str(),
                found.feature,
                found.count
            ),
            (check.kind, check.subject, check.feature, count),
            "{what}"
        );
        assert_eq!(refusal.to_string(), refusal_text(&[line], &[]), "{what}");
        unplant(&pool, check).await;
        assert_eq!(
            edition_guard::inspect(&pool, Duration::from_secs(30)).await,
            Ok(()),
            "{what}: gone, and the database is clean again"
        );
    }
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn counts_are_reported_and_an_enterprise_table_is_counted_to_the_cap(pool: PgPool) {
    seed_community_data(&pool).await;
    insert_rows(&pool, "policy_bundle_events", 3, &[]).await;
    insert_rows(&pool, "discovery_events", 999, &[]).await;
    insert_rows(&pool, "observed_agent_events", 1200, &[]).await;
    common::seed_tenant(&pool, "ZQX-tenant-b").await;
    common::seed_tenant(&pool, "ZQX-tenant-c").await;
    sqlx::query(
        "UPDATE tenants SET features = '{\"mcp_registry\": true, \"knowledge_base\": \"true\", \
         \"ai_model_deployment\": false, \"model_traffic_import\": 1}'::json WHERE id LIKE 'ZQX-%'",
    )
    .execute(&pool)
    .await
    .expect("flags");
    let refusal = start_community(&pool).await.expect_err("Enterprise data");
    assert_eq!(
        refusal.to_string(),
        refusal_text(
            &[
                "Enterprise table policy_bundle_events (policy_as_code): 3 rows".to_string(),
                "Enterprise table discovery_events (discovery): 999 rows".to_string(),
                "Enterprise table observed_agent_events (discovery): 1000 or more rows".to_string(),
                "tenants: 3 (the Community Edition runs a single tenant)".to_string(),
                "Enterprise feature flag mcp_registry (mcp_registry): switched on for 2 tenants"
                    .to_string(),
                "Enterprise feature flag knowledge_base (knowledge_base): switched on for 2 tenants"
                    .to_string(),
            ],
            &[]
        )
    );
}

// ---------------------------------------------------------------------------
// What cannot be decided is a refusal
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_community_assembly_refuses_when_the_marker_table_is_missing(pool: PgPool) {
    seed_community_data(&pool).await;
    sqlx::query("DROP TABLE edition_marker")
        .execute(&pool)
        .await
        .expect("drop");
    let refusal = start_community(&pool)
        .await
        .expect_err("no marker table: not decided, so not started");
    assert!(refusal.found.is_empty(), "{refusal:?}");
    assert_eq!(
        refusal.to_string(),
        refusal_text(
            &[],
            &["edition marker: the check failed (UndefinedTable)".to_string()]
        )
    );
}

/// A database the control plane has never created the schema in: every
/// check is undecided, and the Community Edition does not start.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_community_assembly_refuses_a_database_without_the_schema(pool: PgPool) {
    sqlx::query("DROP SCHEMA public CASCADE")
        .execute(&pool)
        .await
        .expect("drop schema");
    sqlx::query("CREATE SCHEMA public")
        .execute(&pool)
        .await
        .expect("create schema");
    let refusal = start_community(&pool).await.expect_err("no schema");
    assert!(refusal.found.is_empty());
    assert_eq!(refusal.undecided.len(), 44, "the marker and the 43 checks");
    assert!(
        refusal
            .undecided
            .iter()
            .all(|u| u.error == "UndefinedTable"),
        "{refusal:?}"
    );
    // The Enterprise Edition is not held up by it.
    assert!(start(enterprise_state(pool.clone())).await.is_ok());
}

/// A check whose statement fails is "not checked", it does not stop the
/// checks after it, and what the others found is still listed.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_failing_check_is_a_refusal_and_does_not_hide_the_others(pool: PgPool) {
    seed_community_data(&pool).await;
    // The tables of two checks are gone.
    for table in ["collector_keys", "observed_agent_events"] {
        sqlx::query(&format!("DROP TABLE {table} CASCADE"))
            .execute(&pool)
            .await
            .expect("drop");
    }
    let refusal = start_community(&pool).await.expect_err("two checks failed");
    assert!(refusal.found.is_empty(), "{refusal:?}");
    assert_eq!(
        refusal.to_string(),
        refusal_text(
            &[],
            &[
                "Enterprise table collector_keys: the check failed (UndefinedTable)".to_string(),
                "Enterprise table observed_agent_events: the check failed (UndefinedTable)"
                    .to_string(),
            ]
        )
    );

    // With something found before and after the failing checks, and the marker.
    insert_marker(&pool).await;
    insert_rows(&pool, "docker_images", 2, &[]).await;
    sqlx::query(
        "UPDATE tenants SET features = '{\"knowledge_base\": true}'::json WHERE id = 'default'",
    )
    .execute(&pool)
    .await
    .expect("flag");
    let refusal = start_community(&pool)
        .await
        .expect_err("found and undecided");
    assert_eq!(
        refusal.to_string(),
        refusal_text(
            &[
                MARKER_LINE.to_string(),
                "Enterprise table docker_images (deployment): 2 rows".to_string(),
                "Enterprise feature flag knowledge_base (knowledge_base): switched on for 1 tenant"
                    .to_string(),
            ],
            &[
                "Enterprise table collector_keys: the check failed (UndefinedTable)".to_string(),
                "Enterprise table observed_agent_events: the check failed (UndefinedTable)"
                    .to_string(),
            ]
        )
    );
}

/// Every check, one at a time: its statement made to fail (the table it
/// reads renamed away) is a refusal that names that check and no other.
#[sqlx::test(migrations = "./tests/migrations")]
async fn each_check_that_cannot_be_made_is_a_refusal(pool: PgPool) {
    seed_community_data(&pool).await;
    let checks = edition_guard::direct_checks();
    let mut tables: Vec<&str> = vec!["edition_marker"];
    for check in &checks {
        let table = match check.kind {
            Kind::Table | Kind::Tenants => check.subject,
            Kind::FeatureFlag => "tenants",
            Kind::Marker => unreachable!(),
        };
        if !tables.contains(&table) {
            tables.push(table);
        }
    }
    assert_eq!(tables.len(), 39, "37 tables, tenants, the marker");
    for table in tables {
        sqlx::query(&format!(
            "ALTER TABLE \"{table}\" RENAME TO \"{table}_gone\""
        ))
        .execute(&pool)
        .await
        .expect("rename away");
        let refusal = start_community(&pool).await.expect_err(&format!(
            "{table} cannot be read: not decided, so not started"
        ));
        assert!(refusal.found.is_empty(), "{table}: {refusal:?}");
        let expected: Vec<(Kind, &str)> = if table == "edition_marker" {
            vec![(Kind::Marker, "edition_marker")]
        } else {
            checks
                .iter()
                .filter(|c| {
                    c.sql.contains(&format!("FROM {table} "))
                        || c.sql.ends_with(&format!("FROM {table}"))
                })
                .map(|c| (c.kind, c.subject))
                .collect()
        };
        assert!(!expected.is_empty(), "{table}");
        let undecided: Vec<(Kind, &str)> = refusal
            .undecided
            .iter()
            .map(|u| (u.kind, u.subject.as_str()))
            .collect();
        assert_eq!(undecided, expected, "{table}");
        assert!(
            refusal
                .undecided
                .iter()
                .all(|u| u.error == "UndefinedTable"),
            "{table}: {refusal:?}"
        );
        sqlx::query(&format!(
            "ALTER TABLE \"{table}_gone\" RENAME TO \"{table}\""
        ))
        .execute(&pool)
        .await
        .expect("rename back");
    }
    assert_eq!(start_community(&pool).await, Ok(()));
}

/// A statement that does not answer in time is not "nothing found".
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_check_that_does_not_answer_in_time_is_a_refusal(pool: PgPool) {
    seed_community_data(&pool).await;
    let mut holder = pool.begin().await.expect("begin");
    sqlx::query("LOCK TABLE batch_jobs IN ACCESS EXCLUSIVE MODE")
        .execute(&mut *holder)
        .await
        .expect("lock");
    let refusal = edition_guard::inspect(&pool, Duration::from_millis(400))
        .await
        .expect_err("a locked table cannot be counted");
    holder.rollback().await.expect("rollback");
    assert!(refusal.found.is_empty(), "{refusal:?}");
    assert_eq!(
        refusal.to_string(),
        refusal_text(
            &[],
            &["Enterprise table batch_jobs: the check failed (QueryCanceled)".to_string()]
        )
    );
    assert_eq!(
        edition_guard::inspect(&pool, Duration::from_secs(30)).await,
        Ok(())
    );
}

/// An answer that is not what the contract describes: a marker table of
/// another shape, and one that holds the row twice.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_unexpected_answer_is_a_refusal(pool: PgPool) {
    seed_community_data(&pool).await;
    // Columns that are not timestamps.
    sqlx::query("DROP TABLE edition_marker")
        .execute(&pool)
        .await
        .expect("drop");
    sqlx::query(
        "CREATE TABLE edition_marker (id integer, enterprise_first_started_at text, \
         enterprise_last_started_at text, enterprise_first_version text, \
         enterprise_last_version text)",
    )
    .execute(&pool)
    .await
    .expect("create");
    assert_eq!(
        start_community(&pool).await,
        Ok(()),
        "an empty marker table of any shape holds no marker"
    );
    sqlx::query("INSERT INTO edition_marker VALUES (1, 'ZQX-when', 'ZQX-when', 'ZQX-v', 'ZQX-v')")
        .execute(&pool)
        .await
        .expect("insert");
    let refusal = start_community(&pool).await.expect_err("not a marker row");
    assert_eq!(
        refusal.to_string(),
        refusal_text(
            &[],
            &["edition marker: the check failed (UnexpectedAnswer)".to_string()]
        )
    );

    // NULL where the contract says NOT NULL.
    sqlx::query("DROP TABLE edition_marker")
        .execute(&pool)
        .await
        .expect("drop");
    sqlx::query(
        "CREATE TABLE edition_marker (id integer, enterprise_first_started_at timestamptz, \
         enterprise_last_started_at timestamptz, enterprise_first_version varchar(50), \
         enterprise_last_version varchar(50))",
    )
    .execute(&pool)
    .await
    .expect("create");
    sqlx::query("INSERT INTO edition_marker VALUES (1, NULL, now(), NULL, 'ZQX-v')")
        .execute(&pool)
        .await
        .expect("insert");
    let refusal = start_community(&pool)
        .await
        .expect_err("NULLs in the marker");
    assert_eq!(
        refusal.to_string(),
        refusal_text(
            &[],
            &["edition marker: the check failed (UnexpectedAnswer)".to_string()]
        )
    );

    // The single row, twice.
    sqlx::query("DELETE FROM edition_marker WHERE true")
        .execute(&pool)
        .await
        .expect("delete");
    sqlx::query(
        "INSERT INTO edition_marker VALUES (1, now(), now(), 'ZQX-a', 'ZQX-a'), \
         (1, now(), now(), 'ZQX-b', 'ZQX-b')",
    )
    .execute(&pool)
    .await
    .expect("insert");
    let refusal = start_community(&pool).await.expect_err("two marker rows");
    assert_eq!(
        refusal.to_string(),
        refusal_text(
            &[],
            &["edition marker: the check failed (UnexpectedAnswer)".to_string()]
        )
    );
}

// ---------------------------------------------------------------------------
// The Enterprise Edition
// ---------------------------------------------------------------------------

pub(crate) async fn plant_everything(pool: &PgPool) {
    seed_community_data(pool).await;
    insert_marker(pool).await;
    for check in edition_guard::direct_checks() {
        match check.kind {
            Kind::Tenants => {
                plant(pool, &check, 3).await;
            }
            Kind::FeatureFlag => {
                plant(pool, &check, 1).await;
            }
            _ => {
                plant(pool, &check, 2).await;
            }
        }
    }
}

// ---------------------------------------------------------------------------
// The message
// ---------------------------------------------------------------------------

/// Kinds and counts, the two ways forward, and nothing of the data: no row
/// text, no tenant name or identifier, no connection string, no statement.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_refusal_lists_kinds_and_counts_and_nothing_of_the_data(pool: PgPool) {
    plant_everything(&pool).await;
    sqlx::query("DROP TABLE kb_chunks CASCADE")
        .execute(&pool)
        .await
        .expect("drop");
    let refusal = start_community(&pool)
        .await
        .expect_err("everything is there");
    let text = refusal.to_string();
    assert!(
        !text.contains(ROW_TEXT),
        "row contents in the refusal:\n{text}"
    );
    for forbidden in [
        "postgres",
        "placeholder",
        "SELECT",
        "relation",
        "does not exist",
        TENANT,
    ] {
        assert!(
            !text.contains(forbidden),
            "{forbidden:?} in the refusal:\n{text}"
        );
    }
    // Line by line: the fixed lines, and one line per finding of the
    // contract's shapes. Nothing else.
    let lines: Vec<&str> = text.lines().collect();
    assert_eq!(lines[0], REFUSAL_FIRST_LINE);
    assert_eq!(lines[1], REFUSAL_FOUND_LINE);
    let found = &lines[2..2 + 43];
    assert_eq!(found[0], format!("  - {MARKER_LINE}"));
    assert!(found.iter().all(|l| l.starts_with("  - ")));
    assert_eq!(
        found
            .iter()
            .filter(|l| l.contains("Enterprise table ") && l.ends_with(": 2 rows"))
            .count(),
        36
    );
    assert_eq!(lines[45], REFUSAL_UNDECIDED_LINE);
    assert_eq!(
        lines[46],
        "  - Enterprise table kb_chunks: the check failed (UndefinedTable)"
    );
    assert_eq!(lines[47..].join("\n"), REFUSAL_WAYS_FORWARD);
    assert!(text.contains("  1. Start the Enterprise Edition on this database."));
    assert!(text.contains("python main.py edition-reset"));
    // The error the process exits with is this text and no more.
    let error = GatewayBuilder::assemble_checked(community_state(pool.clone()), vec![])
        .await
        .err()
        .expect("refused");
    assert_eq!(error.to_string(), text);
    // What `main` prints is the error's debug form: the same text, no cause.
    let printed = format!("{error:?}");
    assert!(printed.starts_with(&text), "{printed}");
    assert!(!printed.contains("Caused by"), "{printed}");
}
