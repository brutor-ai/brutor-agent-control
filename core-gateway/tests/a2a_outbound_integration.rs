// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # A2A Routes — integration tests
//!
//! Covers all three A2A routes end-to-end against a real Postgres DB:
//!
//!   * `POST /v1/proxy/a2a/outbound` — wiremock stands in for the
//!     remote agent; assertions focus on delegation-header signing
//!     and the relay path.
//!   * `POST /v1/proxy/a2a/inbound`  — no network needed; asserts
//!     handler dispatch (currently the `noop` branch end-to-end;
//!     `skill` is covered by dedicated unit tests).
//!   * `GET /.well-known/agent-card/{tenant}/{name}.json` — the
//!     unauthenticated public discovery path; asserts the 404/200
//!     branches the route enforces around feature-flag + revocation
//!     state.
//!
//! The tests require Postgres: the same dev DB `route_tests.rs` uses.
//! They skip themselves with a printed message when `DATABASE_URL`
//! can't be reached, so they don't break local `cargo test` runs on
//! machines without a DB while still running under CI.
//!
//! Each test seeds its own rows with ULID-keyed tenant/card IDs, then
//! deletes them in a best-effort cleanup block. We do *not* use a
//! transaction rollback because `background_writer` writes
//! `proxy_logs` on its own pool connection — it would be rolled back
//! by the test's transaction and we couldn't assert on it.

mod test_helpers;

use std::time::Duration;

use serde_json::json;
use sqlx::PgPool;
use tokio::sync::OnceCell;
use ulid::Ulid;
use wiremock::matchers::{header_exists, method, path as wm_path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use test_helpers::spawn_app;

// ---------------------------------------------------------------------------
// Test-only schema bootstrap
// ---------------------------------------------------------------------------

/// Idempotently ensures the A2A tables + `features`/`proxy_logs`
/// columns exist in whatever DB we're pointed at. Matches the shape
/// the control plane's migrations produce — we
/// reproduce the CREATE here so tests don't depend on Alembic
/// ordering with the Python gateway.
/// One-shot schema bootstrap.
///
/// Earlier this helper ran on every test invocation, taking
/// `AccessExclusive` on `api_keys` (et al.) via `ALTER TABLE IF NOT
/// EXISTS`. Worker A would finish DDL, start an INSERT (RowShareLock),
/// and Worker B would then try DDL again — deadlock. Wrapping the DDL
/// in a `pg_advisory_lock` only serialized *concurrent* DDL; the
/// DDL-after-INSERT race remained.
///
/// Fix: run the DDL exactly once per test binary via a static
/// `OnceCell`. The advisory lock still serializes the initial racers
/// before any of them complete the cell.
static A2A_SCHEMA_READY: OnceCell<()> = OnceCell::const_new();

async fn ensure_a2a_schema(pool: &PgPool) {
    A2A_SCHEMA_READY
        .get_or_init(|| ensure_a2a_schema_inner(pool.clone()))
        .await;
}

async fn ensure_a2a_schema_inner(pool: PgPool) {
    // Base tables first. This binary reaches the database through its own
    // pool as well as through `spawn_app`, and the seeding below runs on
    // the former — so it cannot rely on the latter having gone first.
    test_helpers::ensure_test_schema(&pool).await;

    let sql = r#"
        ALTER TABLE tenants ADD COLUMN IF NOT EXISTS features JSON;

        CREATE TABLE IF NOT EXISTS agent_cards (
            id VARCHAR(255) PRIMARY KEY,
            tenant_id VARCHAR(255) NOT NULL,
            name VARCHAR(255) NOT NULL,
            version VARCHAR(64) NOT NULL DEFAULT '1.0.0',
            description TEXT,
            source_url VARCHAR(1024),
            card_json JSON NOT NULL,
            signature TEXT,
            signature_algorithm VARCHAR(32),
            signed_by VARCHAR(255),
            signed_at TIMESTAMPTZ,
            trust_tier VARCHAR(32) NOT NULL DEFAULT 'unsigned',
            revoked_at TIMESTAMPTZ,
            revoked_reason TEXT,
            enabled BOOLEAN NOT NULL DEFAULT true,
            created_by VARCHAR(255),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        -- The inbound/outbound A2A paths resolve a card's resource-group
        -- bindings before dispatching. Absent, the query errors and the
        -- route answers 500 — which reads as "the handler is broken"
        -- rather than "a table is missing", because the status code is
        -- all the test sees. Dev databases carry the real schema, so this
        -- only ever showed up on a database built from this file alone.
        CREATE TABLE IF NOT EXISTS resource_group_agent_cards (
            id VARCHAR(255) PRIMARY KEY,
            resource_group_id VARCHAR(255) NOT NULL,
            agent_card_id VARCHAR(255) NOT NULL,
            limits JSON,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_by VARCHAR(255)
        );

        -- `agent_cards` gained columns since this DDL was written, and
        -- the card queries select them by name. The table is created by
        -- whichever binary touches the database first, so CREATE TABLE IF
        -- NOT EXISTS above cannot widen it — these have to be separate.
        ALTER TABLE agent_cards ADD COLUMN IF NOT EXISTS extended_card_json JSON;
        ALTER TABLE agent_cards ADD COLUMN IF NOT EXISTS supported_interfaces JSON;
        ALTER TABLE agent_cards ADD COLUMN IF NOT EXISTS signatures JSON;
        ALTER TABLE agent_cards ADD COLUMN IF NOT EXISTS extensions JSON;
        ALTER TABLE agent_cards ADD COLUMN IF NOT EXISTS protocol_version VARCHAR(8) NOT NULL DEFAULT '0.3';
        ALTER TABLE agent_cards ADD COLUMN IF NOT EXISTS icon_url VARCHAR(2048);
        ALTER TABLE agent_cards ADD COLUMN IF NOT EXISTS tenant_limits JSON;
        ALTER TABLE agent_cards ADD COLUMN IF NOT EXISTS tenant_governance JSON;
        ALTER TABLE agent_cards ADD COLUMN IF NOT EXISTS health_status VARCHAR(32);
        ALTER TABLE agent_cards ADD COLUMN IF NOT EXISTS last_health_check_at TIMESTAMPTZ;
        ALTER TABLE agent_cards ADD COLUMN IF NOT EXISTS health_check_error TEXT;
        ALTER TABLE agent_cards ADD COLUMN IF NOT EXISTS updated_by VARCHAR(255);

        CREATE TABLE IF NOT EXISTS agent_card_capabilities (
            id VARCHAR(255) PRIMARY KEY,
            agent_card_id VARCHAR(255) NOT NULL,
            capability_name VARCHAR(255) NOT NULL,
            input_schema JSON,
            output_schema JSON,
            description TEXT,
            timeout_secs INTEGER,
            rate_limit_per_hour INTEGER,
            data_classification JSON,
            enabled BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        -- a2a_outbound_rules: no such table. The tenant
        -- ceiling now lives on each tenant's root resource group's
        -- a2a_global_limits + a2a_global_governance JSON columns and
        -- is evaluated via the per-group path.

        -- proxy_logs in CI is a minimal table. For these tests we need
        -- the columns ProxyLogInsert binds. We reuse the structure
        -- defined by the background_writer INSERT verbatim; unknown
        -- columns are simply whatever the existing schema already has.
        CREATE TABLE IF NOT EXISTS proxy_logs (
            id VARCHAR(64) PRIMARY KEY,
            tenant_id VARCHAR(255) NOT NULL,
            timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            proxy_type VARCHAR(50) NOT NULL,
            proxy_subtype VARCHAR(50),
            skill_id VARCHAR(255),
            skill_execution_id VARCHAR(255),
            correlation_id VARCHAR(128),
            parent_request_id VARCHAR(128),
            request_id VARCHAR(128),
            end_user_session_id VARCHAR(128) NOT NULL DEFAULT '',
            gateway_user_id VARCHAR(255),
            gateway_user_username VARCHAR(255),
            idp_user_id VARCHAR(255),
            idp_user_username VARCHAR(255),
            idp_user_email VARCHAR(255),
            client_ip VARCHAR(64),
            user_agent TEXT,
            server_id VARCHAR(255),
            idp_server_id VARCHAR(255),
            resource_group_id VARCHAR(255),
            http_method VARCHAR(10),
            endpoint VARCHAR(500),
            target_url TEXT,
            request_size_bytes INTEGER,
            -- TEXT, not JSON: the writer binds these as strings, and a
            -- JSON column rejects a text bind outright.
            request_headers TEXT,
            request_body_preview TEXT,
            http_status_code INTEGER,
            response_size_bytes INTEGER,
            response_headers TEXT,
            response_body_preview TEXT,
            success BOOLEAN NOT NULL DEFAULT true,
            error_message TEXT,
            error_type VARCHAR(50),
            duration_ms FLOAT8,
            mcp_method VARCHAR(100),
            mcp_request_id VARCHAR(128),
            mcp_capability_name VARCHAR(255),
            llm_model_requested VARCHAR(255),
            llm_model_used VARCHAR(255),
            llm_provider VARCHAR(100),
            llm_prompt_tokens INTEGER,
            llm_completion_tokens INTEGER,
            llm_total_tokens INTEGER,
            llm_streaming BOOLEAN,
            llm_temperature FLOAT8,
            llm_max_tokens INTEGER,
            llm_function_calling BOOLEAN,
            llm_used_fallback BOOLEAN,
            llm_fallback_reason TEXT,
            llm_estimated_cost_usd FLOAT8,
            llm_cache_hit BOOLEAN,
            llm_cache_layer VARCHAR(50),
            llm_tokens_saved INTEGER,
            llm_cache_similarity_score FLOAT8,
            prompt_cache_write_tokens INTEGER,
            prompt_cache_read_tokens INTEGER,
            prompt_cache_savings_usd FLOAT8,
            rag_enabled BOOLEAN,
            rag_chunks_retrieved INTEGER,
            rag_context_tokens INTEGER,
            rag_collection_ids VARCHAR(1024),
            rag_top_similarity_score FLOAT8,
            rag_retrieval_duration_ms FLOAT8,
            oauth_flow_type VARCHAR(50),
            oauth_scope TEXT,
            root_task_id VARCHAR(64),
            delegation_depth INTEGER,
            current_task_id VARCHAR(64),
            parent_task_id VARCHAR(64),
            custom_metadata JSON,
            tags JSON,
            -- The background writer binds 93 columns; this table
            -- declared 71 of them. A bind for a column that does not
            -- exist fails the INSERT, and the writer drops failed
            -- telemetry rather than propagating — so the log row
            -- simply never appeared and the assertion read as
            -- "the route did not log" instead of "the test table is
            -- behind the writer".
            actor_agent_id VARCHAR(255),
            agent_authz_decision VARCHAR(20),
            agent_grant_id VARCHAR(255),
            api_key_id VARCHAR(255),
            api_key_name VARCHAR(255),
            argument_policy_denied BOOLEAN,
            compliance_tags JSONB,
            guardrail_blocked BOOLEAN,
            is_auth_event BOOLEAN,
            media_audio_chars BIGINT,
            media_audio_seconds_input DOUBLE PRECISION,
            media_images_generated INTEGER,
            media_video_seconds_output DOUBLE PRECISION,
            pii_detected BOOLEAN,
            region_routed_to VARCHAR(32),
            residency_class VARCHAR(32),
            run_end_state VARCHAR(24),
            outcome_id VARCHAR(128),
            semantic_policy_id VARCHAR(255),
            subject_id VARCHAR(255),
            subject_kind VARCHAR(16),
            trace_continuity VARCHAR(32),
            trace_root_kind VARCHAR(32)
        );
        ALTER TABLE proxy_logs ADD COLUMN IF NOT EXISTS root_task_id VARCHAR(64);
        ALTER TABLE proxy_logs ADD COLUMN IF NOT EXISTS delegation_depth INTEGER;
        -- Tree-reconstruction columns.
        ALTER TABLE proxy_logs ADD COLUMN IF NOT EXISTS current_task_id VARCHAR(64);
        ALTER TABLE proxy_logs ADD COLUMN IF NOT EXISTS parent_task_id VARCHAR(64);

        -- Capability handler columns.
        ALTER TABLE agent_card_capabilities ADD COLUMN IF NOT EXISTS handler_type VARCHAR(32);
        ALTER TABLE agent_card_capabilities ADD COLUMN IF NOT EXISTS handler_ref VARCHAR(255);

        -- Columns that may be missing on the minimal test schema's api_keys
        -- but that the proxy's ApiKey row expects.
        ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS key_prefix VARCHAR(64) DEFAULT '';
        ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS user_email VARCHAR(255);
        ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS allowed_skill_ids JSON;
        ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS skill_rate_limit_daily INTEGER;
        ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS enabled BOOLEAN NOT NULL DEFAULT true;
        ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS revoked BOOLEAN NOT NULL DEFAULT false;
        ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS usage_count INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ;
    "#;
    // These tests use plain `#[tokio::test]`, so they share the same
    // DB across the whole binary. The CREATE TABLE IF NOT EXISTS and
    // ALTER ... ADD COLUMN IF NOT EXISTS statements take AccessExclusive
    // locks; when the test runner fans out workers in parallel and they
    // all race to apply this SQL, they deadlock pairwise on the same
    // set of tables. Serialize behind a session-scoped advisory lock —
    // only one worker runs the DDL at a time, the rest wait briefly.
    // The key is an arbitrary 64-bit constant unique to this helper.
    let mut conn = pool
        .acquire()
        .await
        .expect("acquire pool conn for schema setup");
    sqlx::query("SELECT pg_advisory_lock($1)")
        .bind(7_424_242_424_242_i64) // arbitrary stable key for "A2A schema 7" advisory lock
        .execute(&mut *conn)
        .await
        .expect("acquire advisory lock for A2A test schema setup");
    // Touch nothing when there is nothing to do. On a test server
    // whose template is stamped (the normal case) every table and column
    // below is already there, and the statements would still each take an
    // `AccessExclusiveLock` on tables that other test binaries are reading
    // and writing at that moment — under a different advisory key
    // (`test_helpers` uses its own), or under none. That deadlocked once on
    // an 18-core run. The probe is derived from the statements themselves,
    // so it cannot fall behind them, and it only reads the catalogue.
    let res = if a2a_schema_is_complete(&mut conn, sql).await {
        Ok(Default::default())
    } else {
        sqlx::raw_sql(sql).execute(&mut *conn).await
    };
    // Always release, even on error, so a single failure doesn't wedge
    // every subsequent test in the run.
    let _ = sqlx::query("SELECT pg_advisory_unlock($1)")
        .bind(7_424_242_424_242_i64)
        .execute(&mut *conn)
        .await;
    res.expect("Failed to ensure A2A test schema");
}

/// The tables and `(table, column)` pairs a schema script of
/// `CREATE TABLE IF NOT EXISTS` and `ALTER TABLE … ADD COLUMN IF NOT EXISTS`
/// statements is there to guarantee.
fn schema_script_targets(sql: &str) -> (Vec<String>, Vec<(String, String)>) {
    let mut tables = Vec::new();
    let mut columns = Vec::new();
    for line in sql.lines() {
        let words: Vec<&str> = line.split_whitespace().collect();
        match words.as_slice() {
            ["CREATE", "TABLE", "IF", "NOT", "EXISTS", table, ..] => {
                tables.push(table.trim_end_matches('(').to_string());
            }
            [
                "ALTER",
                "TABLE",
                table,
                "ADD",
                "COLUMN",
                "IF",
                "NOT",
                "EXISTS",
                column,
                ..,
            ] => columns.push((table.to_string(), column.to_string())),
            _ => {}
        }
    }
    (tables, columns)
}

/// True when everything `sql` would create is already in the database.
/// Anything short of that — one column, or a probe that fails — is "no", and
/// the script runs as before.
async fn a2a_schema_is_complete(conn: &mut sqlx::PgConnection, sql: &str) -> bool {
    let (tables, columns) = schema_script_targets(sql);
    let (column_tables, column_names): (Vec<String>, Vec<String>) = columns.into_iter().unzip();
    let missing: Result<(i64, i64), sqlx::Error> = sqlx::query_as(
        "SELECT
            (SELECT count(*) FROM unnest($1::text[]) AS wanted(name)
              WHERE NOT EXISTS (SELECT 1 FROM information_schema.tables t
                                 WHERE t.table_schema = current_schema()
                                   AND t.table_name = wanted.name)),
            (SELECT count(*) FROM unnest($2::text[], $3::text[]) AS wanted(tbl, col)
              WHERE NOT EXISTS (SELECT 1 FROM information_schema.columns c
                                 WHERE c.table_schema = current_schema()
                                   AND c.table_name = wanted.tbl
                                   AND c.column_name = wanted.col))",
    )
    .bind(&tables)
    .bind(&column_tables)
    .bind(&column_names)
    .fetch_one(&mut *conn)
    .await;
    matches!(missing, Ok((0, 0)))
}

/// The probe reads the script it guards: every table and every added column.
#[test]
fn the_schema_probe_covers_every_statement_of_a_script() {
    let (tables, columns) = schema_script_targets(
        "
        ALTER TABLE tenants ADD COLUMN IF NOT EXISTS features JSON;
        CREATE TABLE IF NOT EXISTS agent_cards (
            id VARCHAR(64) PRIMARY KEY
        );
        -- a comment about ALTER TABLE
        ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS enabled BOOLEAN NOT NULL DEFAULT true;
        ",
    );
    assert_eq!(tables, ["agent_cards"]);
    assert_eq!(
        columns,
        [
            ("tenants".to_string(), "features".to_string()),
            ("api_keys".to_string(), "enabled".to_string())
        ]
    );
}

struct Seed {
    tenant_id: String,
    api_key_plain: String,
    card_id: String,
    card_url: String,
    capability: &'static str,
}

async fn seed_tenant_with_card(pool: &PgPool, remote_url: &str) -> Seed {
    ensure_a2a_schema(pool).await;

    let tenant_id = format!("a2a-test-{}", Ulid::new());
    let card_id = format!("card-{}", Ulid::new());
    let cap_id = format!("cap-{}", Ulid::new());
    let key_id = format!("apikey-{}", Ulid::new());
    let key_plain = format!("sk_brutor_api_{}", Ulid::new());
    let key_hash = hex_sha256(&key_plain);
    let capability = "billing.refund";

    // Tenant with a2a on. The live dev schema has NOT-NULL constraints
    // on a number of "profile" columns; populate all of them with
    // sensible defaults so the insert runs against both dev and CI
    // databases (which use different schema snapshots).
    sqlx::query(
        r#"INSERT INTO tenants
           (id, name, display_name, organization_name,
            primary_contact_email, is_active, is_default,
            is_trial, suspended, features, settings)
           VALUES ($1, $1, $1, $1, 'test@example.com',
                   true, false, false, false, $2::json, '{}'::json)"#,
    )
    .bind(&tenant_id)
    .bind(r#"{"a2a": true}"#)
    .execute(pool)
    .await
    .expect("seed tenant");

    // Every key is bound to a resource group (the core refuses one
    // that is not); this key used to have none.
    let group_id = format!("rg-{}", Ulid::new());
    sqlx::query(
        "INSERT INTO resource_groups (id, tenant_id, hierarchy_level, \
         inherit_from_parent, name, is_active, is_system) \
         VALUES ($1, $2, 0, false, $1, true, false)",
    )
    .bind(&group_id)
    .bind(&tenant_id)
    .execute(pool)
    .await
    .expect("seed resource group");
    sqlx::query(
        r#"INSERT INTO api_keys
           (id, tenant_id, name, key_hash, scope, group_id,
            enabled, revoked, key_prefix, usage_count)
           VALUES ($1, $2, 'a2a-test', $3, 'tenant', $4,
                   true, false, 'sk_brutor_api_', 0)"#,
    )
    .bind(&key_id)
    .bind(&tenant_id)
    .bind(&key_hash)
    .bind(&group_id)
    .execute(pool)
    .await
    .expect("seed api_key");

    let card_json = json!({
        "name": "test-card",
        "description": "for integration test",
        "version": "1.0.0",
        "url": remote_url,
        "capabilities": [{"name": capability}],
    });
    sqlx::query(
        r#"INSERT INTO agent_cards
           (id, tenant_id, name, version, card_json, trust_tier, enabled)
           VALUES ($1, $2, $3, '1.0.0', $4, 'tenant_signed', true)"#,
    )
    .bind(&card_id)
    .bind(&tenant_id)
    .bind(format!("card-{}", Ulid::new()))
    .bind(&card_json)
    .execute(pool)
    .await
    .expect("seed card");

    sqlx::query(
        r#"INSERT INTO agent_card_capabilities
           (id, agent_card_id, capability_name, enabled)
           VALUES ($1, $2, $3, true)"#,
    )
    .bind(&cap_id)
    .bind(&card_id)
    .bind(capability)
    .execute(pool)
    .await
    .expect("seed capability");

    Seed {
        tenant_id,
        api_key_plain: key_plain,
        card_id,
        card_url: remote_url.to_string(),
        capability,
    }
}

fn hex_sha256(s: &str) -> String {
    use sha2::{Digest, Sha256};
    hex::encode(Sha256::digest(s.as_bytes()))
}

async fn cleanup_tenant(pool: &PgPool, tenant_id: &str) {
    // Best-effort — parallel tests shouldn't clash on tenant IDs (ULID),
    // so we delete only this tenant's rows.
    let _ = sqlx::query("DELETE FROM proxy_logs WHERE tenant_id = $1")
        .bind(tenant_id)
        .execute(pool)
        .await;
    let _ = sqlx::query(
        "DELETE FROM agent_card_capabilities WHERE agent_card_id IN (SELECT id FROM agent_cards WHERE tenant_id = $1)",
    )
    .bind(tenant_id)
    .execute(pool)
    .await;
    let _ = sqlx::query("DELETE FROM agent_cards WHERE tenant_id = $1")
        .bind(tenant_id)
        .execute(pool)
        .await;
    let _ = sqlx::query("DELETE FROM api_keys WHERE tenant_id = $1")
        .bind(tenant_id)
        .execute(pool)
        .await;
    let _ = sqlx::query("DELETE FROM tenants WHERE id = $1")
        .bind(tenant_id)
        .execute(pool)
        .await;
}

async fn db_available() -> bool {
    use sqlx::postgres::PgPoolOptions;
    let url = test_helpers::test_database_url();
    PgPoolOptions::new()
        .max_connections(1)
        .acquire_timeout(Duration::from_secs(1))
        .connect(&url)
        .await
        .is_ok()
}

/// Bind a handler to the seeded capability. Only affects the one row
/// seeded in `seed_tenant_with_card` — other capabilities are left
/// untouched.
async fn set_capability_handler(
    pool: &PgPool,
    card_id: &str,
    handler_type: &str,
    handler_ref: Option<&str>,
) {
    sqlx::query(
        "UPDATE agent_card_capabilities \
         SET handler_type = $1, handler_ref = $2 \
         WHERE agent_card_id = $3",
    )
    .bind(handler_type)
    .bind(handler_ref)
    .bind(card_id)
    .execute(pool)
    .await
    .expect("update handler");
}

async fn disable_card(pool: &PgPool, card_id: &str) {
    sqlx::query("UPDATE agent_cards SET enabled = false WHERE id = $1")
        .bind(card_id)
        .execute(pool)
        .await
        .expect("disable card");
}

async fn revoke_card(pool: &PgPool, card_id: &str) {
    sqlx::query(
        "UPDATE agent_cards SET trust_tier = 'revoked', \
         revoked_at = NOW(), revoked_reason = 'test', enabled = false \
         WHERE id = $1",
    )
    .bind(card_id)
    .execute(pool)
    .await
    .expect("revoke card");
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

/// Poll until the tenant has a `proxy_logs` row, up to 10 s. A fixed sleep
/// raced the background writer under a loaded full-suite run.
async fn wait_for_proxy_log(db: &sqlx::PgPool, tenant_id: &str) {
    let deadline = tokio::time::Instant::now() + Duration::from_secs(10);
    loop {
        let n: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM proxy_logs WHERE tenant_id = $1")
            .bind(tenant_id)
            .fetch_one(db)
            .await
            .expect("count proxy_logs");
        if n > 0 || tokio::time::Instant::now() >= deadline {
            return;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
}

#[tokio::test]
async fn outbound_happy_path_signs_delegation_and_logs() {
    if !db_available().await {
        eprintln!("skipping: no DATABASE_URL / no reachable Postgres");
        return;
    }

    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/"))
        .and(header_exists("x-brutor-delegation-root"))
        .and(header_exists("x-brutor-delegation-parent"))
        .and(header_exists("x-brutor-delegation-depth"))
        .and(header_exists("x-brutor-delegation-sig"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"ok": true})))
        .expect(1)
        .mount(&mock)
        .await;

    let app = spawn_app().await;
    let seed = seed_tenant_with_card(&app.state.db, &format!("{}/", mock.uri())).await;

    let resp = app
        .post_json_with_headers(
            "/v1/proxy/a2a/outbound",
            vec![
                ("x-tenant-id", seed.tenant_id.as_str()),
                ("x-api-key", seed.api_key_plain.as_str()),
                ("x-correlation-id", "corr-a2a-happy"),
            ],
            &json!({
                "target_card_id": seed.card_id,
                "capability": seed.capability,
                "message": {"amount_cents": 1299, "reason": "unit test"},
            }),
        )
        .await;

    assert_eq!(resp.status(), 200, "body: {:?}", resp.text().await);

    // The mock's `.expect(1)` enforces exactly one matching call with
    // all four delegation headers present. That alone proves the
    // signing/chain-minting path ran.

    // The background writer flushes asynchronously: wait for the row
    // (bounded) instead of a fixed sleep that loses the race under load.
    wait_for_proxy_log(&app.state.db, &seed.tenant_id).await;

    let row: (Option<String>, Option<i32>, String) = sqlx::query_as(
        "SELECT root_task_id, delegation_depth, proxy_type
         FROM proxy_logs
         WHERE tenant_id = $1
         ORDER BY timestamp DESC
         LIMIT 1",
    )
    .bind(&seed.tenant_id)
    .fetch_one(&app.state.db)
    .await
    .expect("proxy_log row present");

    assert_eq!(row.2, "a2a");
    assert_eq!(row.1, Some(0), "root hop must log depth=0");
    assert!(row.0.is_some(), "root_task_id must be populated");

    cleanup_tenant(&app.state.db, &seed.tenant_id).await;
}

#[tokio::test]
async fn outbound_unknown_card_returns_404() {
    if !db_available().await {
        eprintln!("skipping: no DATABASE_URL / no reachable Postgres");
        return;
    }

    let mock = MockServer::start().await;
    // No routes registered — the route must not reach the remote.
    let app = spawn_app().await;
    let seed = seed_tenant_with_card(&app.state.db, &format!("{}/", mock.uri())).await;

    let resp = app
        .post_json_with_headers(
            "/v1/proxy/a2a/outbound",
            vec![
                ("x-tenant-id", seed.tenant_id.as_str()),
                ("x-api-key", seed.api_key_plain.as_str()),
            ],
            &json!({
                "target_card_id": "card-does-not-exist",
                "capability": seed.capability,
                "message": {},
            }),
        )
        .await;

    assert_eq!(resp.status(), 404);

    cleanup_tenant(&app.state.db, &seed.tenant_id).await;
}

#[tokio::test]
async fn outbound_with_a2a_feature_disabled_returns_403() {
    if !db_available().await {
        eprintln!("skipping: no DATABASE_URL / no reachable Postgres");
        return;
    }

    let mock = MockServer::start().await;
    let app = spawn_app().await;
    let seed = seed_tenant_with_card(&app.state.db, &format!("{}/", mock.uri())).await;

    // Flip features.a2a off after seeding.
    sqlx::query("UPDATE tenants SET features = '{\"a2a\": false}'::json WHERE id = $1")
        .bind(&seed.tenant_id)
        .execute(&app.state.db)
        .await
        .expect("flip features.a2a off");

    let resp = app
        .post_json_with_headers(
            "/v1/proxy/a2a/outbound",
            vec![
                ("x-tenant-id", seed.tenant_id.as_str()),
                ("x-api-key", seed.api_key_plain.as_str()),
            ],
            &json!({
                "target_card_id": seed.card_id,
                "capability": seed.capability,
                "message": {},
            }),
        )
        .await;

    assert_eq!(resp.status(), 403);
    let body: serde_json::Value = resp.json().await.unwrap();
    // The canonical error envelope wraps the message in
    // `error.message`. Support both shapes so the test stays
    // green if the envelope is ever flattened back to a string.
    let err_text = body["error"]
        .as_str()
        .or_else(|| body["error"]["message"].as_str())
        .unwrap_or("");
    assert!(err_text.contains("A2A"), "body: {:?}", body);

    // Do not expect any upstream traffic; mock has no `.expect(N)` set.

    cleanup_tenant(&app.state.db, &seed.tenant_id).await;
}

// Silence dead_code warning from the helper struct field we don't assert
// on in every test — `card_url` is used by the seed builder but not
// read back in assertions.
#[allow(dead_code)]
fn _touch_seed(s: &Seed) -> &str {
    &s.card_url
}

// ===========================================================================
// Discovery
// ===========================================================================

#[tokio::test]
async fn discovery_returns_card_json_for_enabled_card() {
    if !db_available().await {
        eprintln!("skipping: no DATABASE_URL / no reachable Postgres");
        return;
    }

    let app = spawn_app().await;
    let seed = seed_tenant_with_card(&app.state.db, "https://noop.example/").await;

    // The card row the seed inserted uses a generated name; fetch it
    // back by id so the URL lines up with what got stored.
    let card_name: String = sqlx::query_scalar("SELECT name FROM agent_cards WHERE id = $1")
        .bind(&seed.card_id)
        .fetch_one(&app.state.db)
        .await
        .expect("fetch card name");

    let resp = app
        .get(&format!(
            "/.well-known/agent-card/{}/{}",
            seed.tenant_id, card_name
        ))
        .await;

    assert_eq!(resp.status(), 200);
    // Cache header must be set so intermediaries can cache short-term.
    let cache = resp
        .headers()
        .get("cache-control")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_string();
    assert!(cache.contains("max-age"), "cache-control: {:?}", cache);

    let body: serde_json::Value = resp.json().await.unwrap();
    assert_eq!(body["id"], seed.card_id);
    assert_eq!(body["tenant_id"], seed.tenant_id);
    assert_eq!(body["name"], card_name);
    // Response field renamed from `card_json` → `card` when the
    // discovery handler started running every card through the
    // v0.3 → v1.0 translator (see services::a2a_v1::translator).
    // Translated cards retain `name`, `description`, `version`,
    // and gain a `capabilities` object + `skills` / `signatures` /
    // `supportedInterfaces` arrays. The v0.3 `capabilities` ARRAY
    // does not migrate into v1.0 `skills` automatically because
    // v1.0 skills require more fields than v0.3 capabilities
    // provide — operators are expected to re-seed the card in v1.0
    // shape when they want the skill list populated. The test
    // therefore asserts the discovery surface works end-to-end
    // (200, content-type, identity fields, translated card body)
    // but not the lossy capability→skill migration.
    assert_eq!(body["card"]["name"], "test-card");
    assert_eq!(body["card"]["version"], "1.0.0");
    assert_eq!(body["card"]["description"], "for integration test");
    // `skills` exists as an array even when empty — the translator
    // guarantees the v1.0 surface.
    assert!(
        body["card"]["skills"].is_array(),
        "translator must emit `skills` as an array: {:?}",
        body["card"]["skills"]
    );

    cleanup_tenant(&app.state.db, &seed.tenant_id).await;
}

#[tokio::test]
async fn discovery_returns_404_when_a2a_feature_disabled() {
    if !db_available().await {
        eprintln!("skipping: no DATABASE_URL / no reachable Postgres");
        return;
    }

    let app = spawn_app().await;
    let seed = seed_tenant_with_card(&app.state.db, "https://noop.example/").await;
    sqlx::query("UPDATE tenants SET features = '{\"a2a\": false}'::json WHERE id = $1")
        .bind(&seed.tenant_id)
        .execute(&app.state.db)
        .await
        .expect("disable a2a");

    let card_name: String = sqlx::query_scalar("SELECT name FROM agent_cards WHERE id = $1")
        .bind(&seed.card_id)
        .fetch_one(&app.state.db)
        .await
        .expect("fetch card name");

    let resp = app
        .get(&format!(
            "/.well-known/agent-card/{}/{}",
            seed.tenant_id, card_name
        ))
        .await;

    assert_eq!(resp.status(), 404);
    cleanup_tenant(&app.state.db, &seed.tenant_id).await;
}

#[tokio::test]
async fn discovery_returns_404_when_card_revoked() {
    if !db_available().await {
        eprintln!("skipping: no DATABASE_URL / no reachable Postgres");
        return;
    }

    let app = spawn_app().await;
    let seed = seed_tenant_with_card(&app.state.db, "https://noop.example/").await;
    revoke_card(&app.state.db, &seed.card_id).await;

    let card_name: String = sqlx::query_scalar("SELECT name FROM agent_cards WHERE id = $1")
        .bind(&seed.card_id)
        .fetch_one(&app.state.db)
        .await
        .expect("fetch card name");

    let resp = app
        .get(&format!(
            "/.well-known/agent-card/{}/{}",
            seed.tenant_id, card_name
        ))
        .await;
    assert_eq!(resp.status(), 404);
    cleanup_tenant(&app.state.db, &seed.tenant_id).await;
}

#[tokio::test]
async fn discovery_returns_404_for_unknown_card_name() {
    if !db_available().await {
        eprintln!("skipping: no DATABASE_URL / no reachable Postgres");
        return;
    }

    let app = spawn_app().await;
    let seed = seed_tenant_with_card(&app.state.db, "https://noop.example/").await;

    let resp = app
        .get(&format!(
            "/.well-known/agent-card/{}/no-such-card",
            seed.tenant_id
        ))
        .await;
    assert_eq!(resp.status(), 404);
    cleanup_tenant(&app.state.db, &seed.tenant_id).await;
}

#[tokio::test]
async fn discovery_does_not_require_tenant_header() {
    if !db_available().await {
        eprintln!("skipping: no DATABASE_URL / no reachable Postgres");
        return;
    }

    let app = spawn_app().await;
    let seed = seed_tenant_with_card(&app.state.db, "https://noop.example/").await;

    let card_name: String = sqlx::query_scalar("SELECT name FROM agent_cards WHERE id = $1")
        .bind(&seed.card_id)
        .fetch_one(&app.state.db)
        .await
        .expect("fetch card name");

    // Note: `get()` sends no X-Tenant-ID / Authorization — if the
    // tenant middleware didn't add `/.well-known/agent-card/` to its
    // skip list, this would fail with 400.
    let resp = app
        .get(&format!(
            "/.well-known/agent-card/{}/{}",
            seed.tenant_id, card_name
        ))
        .await;
    assert_eq!(resp.status(), 200);
    cleanup_tenant(&app.state.db, &seed.tenant_id).await;
}

// ===========================================================================
// Inbound
// ===========================================================================

#[tokio::test]
async fn inbound_noop_handler_returns_echo() {
    if !db_available().await {
        eprintln!("skipping: no DATABASE_URL / no reachable Postgres");
        return;
    }

    let app = spawn_app().await;
    let seed = seed_tenant_with_card(&app.state.db, "https://noop.example/").await;
    // Default seeded capability has null handler_type → treated as noop.

    let resp = app
        .post_json_with_headers(
            "/v1/proxy/a2a/inbound",
            vec![
                ("x-tenant-id", seed.tenant_id.as_str()),
                ("x-api-key", seed.api_key_plain.as_str()),
                ("x-correlation-id", "corr-inbound-noop"),
            ],
            &json!({
                "target_card_id": seed.card_id,
                "capability": seed.capability,
                "message": {"ping": "pong"},
            }),
        )
        .await;

    assert_eq!(resp.status(), 200);
    let body: serde_json::Value = resp.json().await.unwrap();
    assert_eq!(body["handler"], "noop");
    assert_eq!(body["capability"], seed.capability);
    assert_eq!(body["received_message"]["ping"], "pong");

    // The background writer flushes asynchronously: wait for the row
    // (bounded) instead of a fixed sleep that loses the race under load.
    wait_for_proxy_log(&app.state.db, &seed.tenant_id).await;

    let row: (Option<String>, Option<i32>, String, Option<String>) = sqlx::query_as(
        "SELECT root_task_id, delegation_depth, proxy_type, proxy_subtype \
         FROM proxy_logs \
         WHERE tenant_id = $1 \
         ORDER BY timestamp DESC \
         LIMIT 1",
    )
    .bind(&seed.tenant_id)
    .fetch_one(&app.state.db)
    .await
    .expect("proxy_log row present");
    assert_eq!(row.2, "a2a");
    assert_eq!(row.3.as_deref(), Some("inbound"));
    assert_eq!(row.1, Some(0), "root hop must log depth=0");
    assert!(row.0.is_some());

    cleanup_tenant(&app.state.db, &seed.tenant_id).await;
}

#[tokio::test]
async fn inbound_with_explicit_handler_type_noop() {
    if !db_available().await {
        eprintln!("skipping: no DATABASE_URL / no reachable Postgres");
        return;
    }

    let app = spawn_app().await;
    let seed = seed_tenant_with_card(&app.state.db, "https://noop.example/").await;
    set_capability_handler(&app.state.db, &seed.card_id, "noop", None).await;

    let resp = app
        .post_json_with_headers(
            "/v1/proxy/a2a/inbound",
            vec![
                ("x-tenant-id", seed.tenant_id.as_str()),
                ("x-api-key", seed.api_key_plain.as_str()),
            ],
            &json!({
                "target_card_id": seed.card_id,
                "capability": seed.capability,
                "message": {"a": 1},
            }),
        )
        .await;

    assert_eq!(resp.status(), 200);
    let body: serde_json::Value = resp.json().await.unwrap();
    assert_eq!(body["handler"], "noop");

    cleanup_tenant(&app.state.db, &seed.tenant_id).await;
}

#[tokio::test]
async fn inbound_disabled_card_returns_403() {
    if !db_available().await {
        eprintln!("skipping: no DATABASE_URL / no reachable Postgres");
        return;
    }

    let app = spawn_app().await;
    let seed = seed_tenant_with_card(&app.state.db, "https://noop.example/").await;
    disable_card(&app.state.db, &seed.card_id).await;

    let resp = app
        .post_json_with_headers(
            "/v1/proxy/a2a/inbound",
            vec![
                ("x-tenant-id", seed.tenant_id.as_str()),
                ("x-api-key", seed.api_key_plain.as_str()),
            ],
            &json!({
                "target_card_id": seed.card_id,
                "capability": seed.capability,
                "message": {},
            }),
        )
        .await;

    assert_eq!(resp.status(), 403);
    cleanup_tenant(&app.state.db, &seed.tenant_id).await;
}

#[tokio::test]
async fn inbound_unknown_capability_returns_404() {
    if !db_available().await {
        eprintln!("skipping: no DATABASE_URL / no reachable Postgres");
        return;
    }

    let app = spawn_app().await;
    let seed = seed_tenant_with_card(&app.state.db, "https://noop.example/").await;

    let resp = app
        .post_json_with_headers(
            "/v1/proxy/a2a/inbound",
            vec![
                ("x-tenant-id", seed.tenant_id.as_str()),
                ("x-api-key", seed.api_key_plain.as_str()),
            ],
            &json!({
                "target_card_id": seed.card_id,
                "capability": "does.not.exist",
                "message": {},
            }),
        )
        .await;

    assert_eq!(resp.status(), 404);
    cleanup_tenant(&app.state.db, &seed.tenant_id).await;
}

#[tokio::test]
async fn inbound_with_a2a_feature_disabled_returns_403() {
    if !db_available().await {
        eprintln!("skipping: no DATABASE_URL / no reachable Postgres");
        return;
    }

    let app = spawn_app().await;
    let seed = seed_tenant_with_card(&app.state.db, "https://noop.example/").await;
    sqlx::query("UPDATE tenants SET features = '{\"a2a\": false}'::json WHERE id = $1")
        .bind(&seed.tenant_id)
        .execute(&app.state.db)
        .await
        .expect("disable a2a");

    let resp = app
        .post_json_with_headers(
            "/v1/proxy/a2a/inbound",
            vec![
                ("x-tenant-id", seed.tenant_id.as_str()),
                ("x-api-key", seed.api_key_plain.as_str()),
            ],
            &json!({
                "target_card_id": seed.card_id,
                "capability": seed.capability,
                "message": {},
            }),
        )
        .await;

    assert_eq!(resp.status(), 403);
    cleanup_tenant(&app.state.db, &seed.tenant_id).await;
}

#[tokio::test]
async fn inbound_unknown_handler_type_returns_501() {
    if !db_available().await {
        eprintln!("skipping: no DATABASE_URL / no reachable Postgres");
        return;
    }

    let app = spawn_app().await;
    let seed = seed_tenant_with_card(&app.state.db, "https://noop.example/").await;
    // Bypass the SQLAlchemy validator by writing raw SQL — the Rust
    // proxy must reject unknown values at dispatch regardless of what
    // made it into the row.
    set_capability_handler(&app.state.db, &seed.card_id, "webhook", Some("x")).await;

    let resp = app
        .post_json_with_headers(
            "/v1/proxy/a2a/inbound",
            vec![
                ("x-tenant-id", seed.tenant_id.as_str()),
                ("x-api-key", seed.api_key_plain.as_str()),
            ],
            &json!({
                "target_card_id": seed.card_id,
                "capability": seed.capability,
                "message": {},
            }),
        )
        .await;

    assert_eq!(resp.status(), 501);
    cleanup_tenant(&app.state.db, &seed.tenant_id).await;
}
