// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

// Shared by several integration-test binaries; each uses a subset, so the
// rest read as dead in every individual compilation unit.
#![allow(dead_code)]

//! # Test Helpers
//!
//! Shared utilities for integration tests that need a live database connection
//! and a fully-wired Axum application.
//!
//! ## Usage
//!
//! ```ignore
//! mod test_helpers;
//! use test_helpers::{create_test_state, create_full_app, make_test_jwt, spawn_app};
//! ```

use std::net::SocketAddr;

use axum::Router;
use sqlx::postgres::PgPoolOptions;
use tokio::net::TcpListener;
use tower_http::cors::CorsLayer;

use brutor_gateway_core::config::AppConfig;
use brutor_gateway_core::middleware;
use brutor_gateway_core::routes;
use brutor_gateway_core::services::telemetry::TelemetryState;
use brutor_gateway_core::services::{AppState, SharedState};

/// The JWT secret used across all integration tests.
pub const TEST_JWT_SECRET: &str = "test-jwt-secret-for-integration-tests-min32chars!!";

/// The encryption key used across all integration tests (must be exactly 32 bytes).
pub const TEST_ENCRYPTION_KEY: &str = "test-encryption-key-32-chars-ok!";

/// The database these tests write to: `DATABASE_URL` (CI sets it), else a
/// dedicated local test database, `brutor_core_test`.
///
/// These tests INSERT and DELETE rows (tenant `default`, resource groups,
/// agent cards). The fallback used to be the live dev database,
/// `ai_gateway`, so a plain `cargo test` on a laptop running the trial
/// wrote into it; a test run against that database wipes it.
/// A URL naming `ai_gateway` is refused unless the run is CI's throwaway
/// Postgres service (`CI=true`, set by GitHub Actions) or the caller says
/// the database is disposable (`BRUTOR_TEST_DB_IS_DISPOSABLE=1`).
pub fn test_database_url() -> String {
    let url = std::env::var("DATABASE_URL").unwrap_or_else(|_| {
        "postgresql://brutor:brutor123@localhost:5432/brutor_core_test".to_string()
    });
    assert_disposable_test_db(&url);
    url
}

/// The database names an installation uses: `brutor` (the default;
/// the Community package creates it) and `ai_gateway`
/// (developer machines may keep one). Both are refused.
pub const LIVE_DATABASE_NAMES: &[&str] = &["brutor", "ai_gateway"];

/// Whether `url` names one of [`LIVE_DATABASE_NAMES`].
pub fn names_a_live_database(url: &str) -> bool {
    let db_name = url
        .rsplit('/')
        .next()
        .unwrap_or("")
        .split('?')
        .next()
        .unwrap_or("");
    LIVE_DATABASE_NAMES.contains(&db_name)
}

/// Panic before touching a database that may be a live one.
pub fn assert_disposable_test_db(url: &str) {
    let ci = std::env::var("CI").is_ok_and(|v| v == "true");
    let disposable = std::env::var("BRUTOR_TEST_DB_IS_DISPOSABLE").is_ok_and(|v| v == "1");
    assert!(
        !names_a_live_database(url) || ci || disposable,
        "refusing to run tests against a database named `brutor` or `ai_gateway` (the names of a \
         live installation's database): point DATABASE_URL at a throwaway database such as \
         brutor_core_test, or set BRUTOR_TEST_DB_IS_DISPOSABLE=1 if this database really is \
         disposable"
    );
}

/// Create a `SharedState` backed by the test database.
///
/// The pool is intentionally small (2 connections) to avoid starving the
/// database when many tests run in parallel.
///
/// On CI, creates the minimal schema needed for route tests.
pub async fn create_test_state() -> SharedState {
    create_test_state_with(Vec::new()).await
}

/// [`create_test_state`] assembled with `extensions`: for the binaries of a
/// distribution that composes some (the Enterprise route tests).
pub async fn create_test_state_with(
    extensions: Vec<Box<dyn brutor_gateway_core::builder::Extension>>,
) -> SharedState {
    let db_url = test_database_url();
    let db_pool = PgPoolOptions::new()
        .max_connections(2)
        .acquire_timeout(std::time::Duration::from_secs(5))
        .connect(&db_url)
        .await
        .expect("Failed to connect to test database — is PostgreSQL running?");

    // Create minimal schema if tables don't exist (CI has an empty DB)
    ensure_test_schema(&db_pool).await;

    let config = AppConfig {
        listen_addr: "127.0.0.1:0".to_string(),
        metrics_listen_addr: "127.0.0.1:0".to_string(),
        cors_origins: brutor_gateway_core::middleware::cors::parse_origins(None),
        trusted_proxies: Default::default(),
        public_url: "http://localhost:8100".to_string(),
        database_url: db_url,
        db_pool_size: 2,
        jwt_secret: TEST_JWT_SECRET.to_string(),
        jwt_algorithm: "HS256".to_string(),
        jwt_expires_in: 3600,
        encryption_key: TEST_ENCRYPTION_KEY.to_string(),
        redis_url: None,
        default_mcp_timeout_secs: 30,
        guardrail_cache_ttl_secs: 60,
        mcp_request_state_max_bytes: 64 * 1024,
        embedding_model: None,
        judge_model: None,
        // Media (Files API). Tests don't actually
        // upload — `media_storage_backend = "memory"` keeps the
        // pipeline in-process so no MinIO/S3 needs to be running.
        media_storage_backend: "memory".to_string(),
        media_filesystem_dir: None,
        media_filesystem_max_bytes: 10 * 1024 * 1024 * 1024,
        media_bucket: None,
        media_s3_region: "us-east-1".to_string(),
        media_s3_endpoint: None,
        media_s3_public_endpoint: None,
        media_s3_force_path_style: true,
        media_s3_access_key_id: None,
        media_s3_secret_access_key: None,
        media_signed_url_ttl_secs: 900,
        media_gc_interval_secs: 21600,
        // Diagnostics: leave the diagnostic pool off for
        // tests — we don't exercise the slow-request capture path
        // and a second pool just adds DB-connection pressure.
        diagnostic_pool_enabled: false,
        diagnostic_pool_size: 2,
        pool_sampler_interval_secs: 5,
        // Model-metrics aggregator: a small flush interval
        // keeps tests snappy; 0 is interpreted internally as 100 ms.
        model_metrics_flush_interval_secs: 0,
        // Pool sizing — tests use the legacy `db_pool_size`
        // path (None) so the explicit `db_pool_size: 2` above is
        // what governs.
        // Saturation alerting — defaults match the
        // production thresholds. Tests don't exercise the alert
        // firing window.
        pool_saturation_sustained_secs: 60,
    };

    let telemetry = TelemetryState {
        prometheus_handle: None,
    };

    let http_client = reqwest::Client::new();

    compose(
        AppState::new(config, db_pool, None, http_client, telemetry),
        extensions,
    )
}

/// The composition seam of these binaries:
/// the one place a state built here is assembled. `create_test_state` is a
/// Community assembly (no extension); a binary of a distribution that
/// composes extensions hands them to `create_test_state_with`.
/// `tests/integration/common.rs` has the integration suite's seam.
fn compose(
    state: AppState,
    extensions: Vec<Box<dyn brutor_gateway_core::builder::Extension>>,
) -> SharedState {
    record_composition(&extensions);
    brutor_gateway_core::builder::GatewayBuilder::assemble(state, extensions)
        .expect("the composition assembles")
        .state
}

/// As `tests/integration/common.rs::record_composition`:
/// with `BRUTOR_TEST_COMPOSITION_LOG=<file>` set, the composition of every
/// state built here is appended to that file.
fn record_composition(extensions: &[Box<dyn brutor_gateway_core::builder::Extension>]) {
    use std::io::Write;
    let Ok(file) = std::env::var("BRUTOR_TEST_COMPOSITION_LOG") else {
        return;
    };
    let test = std::env::var("NEXTEST_TEST_NAME").unwrap_or_else(|_| {
        std::thread::current()
            .name()
            .unwrap_or("unnamed")
            .to_string()
    });
    let binary = std::env::var("NEXTEST_BINARY_ID").unwrap_or_default();
    let names: Vec<&str> = extensions.iter().map(|e| e.name()).collect();
    let line = format!("{binary}\t{test}\t[{}]\n", names.join(","));
    std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(file)
        .and_then(|mut f| f.write_all(line.as_bytes()))
        .expect("the composition log is writable");
}

/// Build an Axum `Router` with the Community gateway's surfaces under test,
/// including the tenant resolution middleware and CORS layer.
pub fn create_full_app(state: SharedState) -> Router {
    create_app_with(state, |router| router)
}

/// [`create_full_app`] with the routes `extra` nests beside the Community
/// ones, under the same layers: for the binaries of a distribution whose
/// extensions mount routes.
pub fn create_app_with(
    state: SharedState,
    extra: impl FnOnce(Router<SharedState>) -> Router<SharedState>,
) -> Router {
    let community = Router::new()
        .nest("/v1/proxy/llm", routes::llm::router())
        .nest("/v1/proxy/mcp", routes::mcp::router())
        .nest("/v1/proxy/vmcp", routes::vmcp::router())
        .nest("/v1/proxy/skills", routes::skills::router())
        .nest("/v1/proxy/a2a", routes::a2a::router())
        .merge(routes::a2a::discovery_router())
        .nest("/v1/proxy/oauth", routes::oauth::router())
        .nest("/v1/approvals", routes::approvals::router());
    extra(community)
        .nest("/v1/assets", routes::icons::router())
        .merge(routes::gateway::router())
        .layer(middleware::tenant::TenantLayer::new(state.clone()))
        // Outside the tenant layer, as in the gateway: `/metrics` is served
        // on a listener of its own (`app::metrics_router`), which has no
        // tenant layer. Under it, a path nobody registered needs a tenant.
        .route(
            "/metrics",
            axum::routing::get(routes::metrics::prometheus_metrics),
        )
        .layer(CorsLayer::permissive())
        .with_state(state)
}

/// A running test server with its base URL and the underlying shared state.
#[allow(dead_code)]
pub struct TestApp {
    pub base_url: String,
    pub state: SharedState,
    pub client: reqwest::Client,
}

#[allow(dead_code)]
impl TestApp {
    /// Send a GET request to the given path (relative to base URL).
    pub async fn get(&self, path: &str) -> reqwest::Response {
        self.client
            .get(format!("{}{}", self.base_url, path))
            .send()
            .await
            .expect("Failed to send GET request")
    }

    /// Send a GET request with an Authorization header.
    pub async fn get_with_auth(&self, path: &str, token: &str) -> reqwest::Response {
        self.client
            .get(format!("{}{}", self.base_url, path))
            .header("authorization", format!("Bearer {}", token))
            .send()
            .await
            .expect("Failed to send authenticated GET request")
    }

    /// Send a GET request with custom headers.
    pub async fn get_with_headers(
        &self,
        path: &str,
        headers: Vec<(&str, &str)>,
    ) -> reqwest::Response {
        let mut req = self.client.get(format!("{}{}", self.base_url, path));
        for (key, value) in headers {
            req = req.header(key, value);
        }
        req.send()
            .await
            .expect("Failed to send GET request with headers")
    }

    /// Send a POST request with a JSON body.
    pub async fn post_json(&self, path: &str, body: &serde_json::Value) -> reqwest::Response {
        self.client
            .post(format!("{}{}", self.base_url, path))
            .json(body)
            .send()
            .await
            .expect("Failed to send POST request")
    }

    /// Send a POST request with an Authorization header and JSON body.
    pub async fn post_json_with_auth(
        &self,
        path: &str,
        token: &str,
        body: &serde_json::Value,
    ) -> reqwest::Response {
        self.client
            .post(format!("{}{}", self.base_url, path))
            .header("authorization", format!("Bearer {}", token))
            .json(body)
            .send()
            .await
            .expect("Failed to send authenticated POST request")
    }

    /// Send a POST request with custom headers and JSON body.
    pub async fn post_json_with_headers(
        &self,
        path: &str,
        headers: Vec<(&str, &str)>,
        body: &serde_json::Value,
    ) -> reqwest::Response {
        let mut req = self
            .client
            .post(format!("{}{}", self.base_url, path))
            .json(body);
        for (key, value) in headers {
            req = req.header(key, value);
        }
        req.send()
            .await
            .expect("Failed to send POST request with headers")
    }

    /// Send a request with an arbitrary HTTP method.
    pub async fn request(&self, method: reqwest::Method, path: &str) -> reqwest::Response {
        self.client
            .request(method, format!("{}{}", self.base_url, path))
            .send()
            .await
            .expect("Failed to send request")
    }
}

/// Spawn a test server on a random port and return a `TestApp` handle.
///
/// The server runs in a background tokio task. Since we bind to port 0,
/// each test gets its own port and there are no conflicts.
pub async fn spawn_app() -> TestApp {
    let state = create_test_state().await;
    serve(state.clone(), create_full_app(state)).await
}

/// Serve `app` over `state` on a random port.
pub async fn serve(state: SharedState, app: Router) -> TestApp {
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("Failed to bind to random port");
    let addr: SocketAddr = listener.local_addr().unwrap();

    tokio::spawn(async move {
        axum::serve(
            listener,
            app.into_make_service_with_connect_info::<SocketAddr>(),
        )
        .await
        .expect("Server error");
    });

    let client = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .expect("Failed to create HTTP client");

    TestApp {
        base_url: format!("http://{}", addr),
        state,
        client,
    }
}

/// Generate a Gateway JWT suitable for portal endpoint authentication.
///
/// The token is signed with `TEST_JWT_SECRET` using HS256 and contains
/// standard portal claims.
pub fn make_test_jwt(user_id: i32, username: &str, tenant_id: &str) -> String {
    use jsonwebtoken::{EncodingKey, Header, encode};

    // The shape every issuer mints: `typ` and a `jti`. Without them a portal
    // route refuses the token, and the cases that sign in with it failed
    // whenever an end user existed in the shared database.
    let claims = serde_json::json!({
        "typ": "portal",
        "jti": uuid::Uuid::new_v4().to_string(),
        "user_id": user_id.to_string(),
        "tenant_id": tenant_id,
        "username": username,
        "user_type": "end_user",
        "exp": (chrono::Utc::now().timestamp() + 3600) as u64,
        "iat": chrono::Utc::now().timestamp() as u64,
    });

    encode(
        &Header::default(),
        &claims,
        &EncodingKey::from_secret(TEST_JWT_SECRET.as_bytes()),
    )
    .expect("Failed to encode test JWT")
}

/// Panic-safe per-test cleanup: runs `job` against the test database when
/// dropped, on test pass or failure.
///
/// The cleanup gets its OWN pool on its own runtime on a helper thread. It
/// must never reuse the test's pool: `Drop` is synchronous, so the test's
/// current-thread runtime sits blocked in `join()`, and a pool created on
/// that runtime has its connections driven by it — the cleanup query can
/// then never complete. That pattern hangs CI until the job timeout;
/// `tests/test_harness_guard.rs` refuses it.
/// The 30-second ceiling makes a slow database a leaked row, never a hung build.
pub struct DbCleanupGuard {
    job: Option<Box<dyn FnOnce(sqlx::PgPool) -> futures::future::BoxFuture<'static, ()> + Send>>,
}

impl DbCleanupGuard {
    pub fn new<F, Fut>(job: F) -> Self
    where
        F: FnOnce(sqlx::PgPool) -> Fut + Send + 'static,
        Fut: std::future::Future<Output = ()> + Send + 'static,
    {
        Self {
            job: Some(Box::new(move |pool| Box::pin(job(pool)))),
        }
    }
}

impl Drop for DbCleanupGuard {
    fn drop(&mut self) {
        let Some(job) = self.job.take() else { return };
        let url = test_database_url();
        // Swallow join errors: the test may already be unwinding, and a panic
        // from Drop would abort the process and hide the original failure.
        let _ = std::thread::spawn(move || {
            let Ok(rt) = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
            else {
                return;
            };
            rt.block_on(async move {
                let work = async {
                    let pool = PgPoolOptions::new()
                        .max_connections(1)
                        .connect(&url)
                        .await
                        .ok()?;
                    job(pool.clone()).await;
                    pool.close().await;
                    Some(())
                };
                let _ = tokio::time::timeout(std::time::Duration::from_secs(30), work).await;
            });
        })
        .join();
    }
}

/// Create the minimal database schema needed for integration tests.
/// Idempotent — uses CREATE TABLE IF NOT EXISTS.
/// Uses an advisory lock to prevent parallel test races on type creation.
/// Build the shared test schema. Public so a binary that opens its own
/// pool (rather than going through `spawn_app`) can still guarantee the
/// tables exist — otherwise it passes or fails on whether some other test
/// binary happened to run first.
pub async fn ensure_test_schema(pool: &sqlx::PgPool) {
    // Once per process. Several binaries call this from `test_pool()`, which
    // every test invokes — without this the DDL below would run once per
    // test, concurrently, and DDL takes AccessExclusiveLock. Against a busy
    // database (a dev box with the gateway attached) that deadlocks against
    // the readers the other tests are running:
    //
    //   deadlock detected: process A waits for RowShareLock ...
    //   blocked by process B; process B waits for AccessExclusiveLock ...
    static SCHEMA_READY: tokio::sync::OnceCell<()> = tokio::sync::OnceCell::const_new();
    SCHEMA_READY
        .get_or_init(|| ensure_test_schema_inner(pool.clone()))
        .await;
}

async fn ensure_test_schema_inner(pool: sqlx::PgPool) {
    // Serialise schema setup across parallel tests — and across PROCESSES:
    // under cargo-nextest every test is its own process, so the once-per-
    // process cell above no longer means once per run.
    //
    // PostgreSQL's CREATE TABLE IF NOT EXISTS has a race condition where concurrent
    // transactions can both try to create the implicit row type, causing:
    //   "duplicate key value violates unique constraint pg_type_typname_nsp_index"
    //
    // The completeness probe runs INSIDE the lock. Outside it, a second
    // process could see the tables the first one had just created, start
    // its test, and then deadlock against the first one's remaining DDL
    // (AccessExclusiveLock vs the test's AccessShareLock).
    //
    // The lock, the probe, the schema and the unlock must all run on the SAME
    // connection. `execute(pool)` picks any free one, and an advisory lock
    // is session-scoped — so locking on connection A and unlocking on
    // connection C leaves A holding it forever, with `pg_advisory_unlock`
    // quietly returning false rather than erroring. Every later caller
    // then blocks until the pool acquire times out (30s) and the test
    // fails as if the data were missing.
    let mut conn = pool
        .acquire()
        .await
        .expect("Failed to acquire a connection for schema setup");

    sqlx::query("SELECT pg_advisory_lock(42)")
        .execute(&mut *conn)
        .await
        .expect("Failed to acquire advisory lock");

    // Cheap probe: if the schema is already complete, touch nothing.
    // A dev database built by the control plane has every table already, and
    // running a few hundred DDL statements across it just to discover that
    // is both slow and the thing that deadlocks.
    let complete: Option<(bool,)> = sqlx::query_as(
        "SELECT EXISTS (
             SELECT 1 FROM information_schema.columns
             WHERE table_name = 'tenants' AND column_name = 'organization_name'
         ) AND EXISTS (
             SELECT 1 FROM information_schema.tables
             WHERE table_name = 'system_alerts'
         )",
    )
    .fetch_optional(&mut *conn)
    .await
    .expect("probe the test schema");
    if matches!(complete, Some((true,))) {
        sqlx::query("SELECT pg_advisory_unlock(42)")
            .execute(&mut *conn)
            .await
            .expect("Failed to release advisory lock");
        return;
    }

    // Every table the application knows about, created up front.
    //
    // The hand-written blocks below only ever declared what some test
    // needed when it was written, so they fell behind the application and
    // the resulting failures never mentioned a table or a column — a
    // missing column made a query fail, and the caller turned that into a
    // 500, an empty result, or a silently dropped telemetry row. Four test
    // binaries were failing this way, each hidden behind the last because
    // cargo stops at the first one.
    //
    // Generated from `tests/migrations/00000000000000_schema.sql` — the
    // dump the `db-tests` suite clones and CI drift-checks — by
    // `CREATE TABLE IF NOT EXISTS` with NOT NULL and sequence defaults
    // stripped. Deliberately permissive: tests insert only the columns
    // they care about, and enforcing the real constraints here would fail
    // rows the suite has always been allowed to write.
    //
    // Runs first, so these definitions win and the `CREATE TABLE IF NOT
    // EXISTS` blocks below are no-ops: every table and column they name is
    // in the generated schema (`tests/schema_fixture_guard.rs` holds them
    // to it), and no column is added to a table anywhere but there.
    sqlx::raw_sql(include_str!("schema/test_schema_superset.sql"))
        .execute(&mut *conn)
        .await
        .expect("Failed to create the baseline test schema");

    // Minimal tables needed for route tests to not crash.
    // This doesn't replicate the full schema — just enough for handlers to query without errors.
    let schema = r#"
        CREATE TABLE IF NOT EXISTS tenants (
            id VARCHAR(255) PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            display_name VARCHAR(255),
            -- The columns below are not needed by the route tests, but
            -- `a2a_outbound_integration` seeds a tenant with the full
            -- profile so its INSERT runs against the dev database too.
            -- CI starts from an empty Postgres where this DDL is the only
            -- definition of `tenants`, so anything absent here is a column
            -- that does not exist — which is exactly how that suite failed:
            -- 14 tests, all `column "organization_name" ... does not exist`,
            -- none of them about the behaviour under test.
            organization_name VARCHAR(500),
            primary_contact_email VARCHAR(255),
            is_active BOOLEAN NOT NULL DEFAULT true,
            is_default BOOLEAN NOT NULL DEFAULT false,
            is_trial BOOLEAN NOT NULL DEFAULT false,
            suspended BOOLEAN NOT NULL DEFAULT false,
            features JSON,
            settings JSON,
            -- Read by the tenant middleware on every request
            -- (`SELECT suspended, is_trial, suspension_reason,
            -- trial_ends_at FROM tenants`). A missing column there fails
            -- the query, and the middleware turns any failure into a 503 —
            -- so every test in the binary reports the wrong status code
            -- and none of them mention a column.
            suspension_reason TEXT,
            trial_ends_at TIMESTAMPTZ,
            maintenance_mode VARCHAR(32),
            drain_grace_secs INTEGER,
            data_residency JSON,
            compliance_profile JSON,
            media_storage_quota_bytes BIGINT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS end_users (
            id SERIAL PRIMARY KEY,
            tenant_id VARCHAR(255) NOT NULL,
            username VARCHAR(255) NOT NULL,
            email VARCHAR(255),
            display_name VARCHAR(255),
            password_hash VARCHAR(255) NOT NULL DEFAULT '',
            is_active BOOLEAN NOT NULL DEFAULT true,
            is_verified BOOLEAN NOT NULL DEFAULT false,
            last_login TIMESTAMPTZ,
            user_metadata JSON
        );

        CREATE TABLE IF NOT EXISTS mcp_servers (
            id VARCHAR(255) PRIMARY KEY,
            tenant_id VARCHAR(255) NOT NULL,
            name VARCHAR(255) NOT NULL,
            base_url VARCHAR(1000) NOT NULL DEFAULT '',
            mcp_endpoint_path VARCHAR(500),
            auth_type VARCHAR(50),
            auth_token TEXT,
            timeout FLOAT8,
            enabled BOOLEAN NOT NULL DEFAULT true,
            capabilities JSON,
            health_status VARCHAR(50),
            idp_id VARCHAR(255),
            icon_data BYTEA,
            icon_mime_type VARCHAR(100),
            icon_hash VARCHAR(64),
            icon_size INTEGER,
            icon_uploaded_at TIMESTAMPTZ,
            discovered_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS notifications (
            id VARCHAR(64) PRIMARY KEY,
            tenant_id VARCHAR(255) NOT NULL,
            user_id INTEGER NOT NULL,
            notification_type VARCHAR(50) NOT NULL,
            title VARCHAR(500) NOT NULL,
            body TEXT,
            metadata JSON,
            is_read BOOLEAN NOT NULL DEFAULT false,
            read_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            expires_at TIMESTAMPTZ
        );

        CREATE TABLE IF NOT EXISTS resource_groups (
            id VARCHAR(255) PRIMARY KEY,
            tenant_id VARCHAR(255) NOT NULL,
            parent_group_id VARCHAR(255),
            hierarchy_level INTEGER NOT NULL DEFAULT 0,
            hierarchy_path VARCHAR(1000),
            inherit_from_parent BOOLEAN NOT NULL DEFAULT true,
            name VARCHAR(255) NOT NULL,
            display_name VARCHAR(255),
            description TEXT,
            icon VARCHAR(100),
            color VARCHAR(20),
            group_type VARCHAR(50),
            is_active BOOLEAN NOT NULL DEFAULT true,
            is_system BOOLEAN NOT NULL DEFAULT false,
            llm_global_limits JSON,
            mcp_global_limits JSON,
            llm_global_governance JSON,
            mcp_global_governance JSON,
            skill_global_governance JSON,
            skill_global_limits JSON,
            created_by VARCHAR(255),
            updated_by VARCHAR(255),
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        );

        -- Read by ResourceGroupCache when it resolves a group's effective
        -- membership; a missing table fails the query and the cache
        -- returns nothing, so inheritance tests see empty sets rather than
        -- an error naming the table.
        -- A2A card tables. These used to be created only by
        -- `a2a_outbound_integration`, which meant every other binary that
        -- touches cards passed or failed depending on whether that one had
        -- run first. Creating them here makes each binary self-sufficient.
        CREATE TABLE IF NOT EXISTS agent_cards (
            id character varying(255) NOT NULL,
            tenant_id character varying(255) NOT NULL,
            name character varying(255) NOT NULL,
            version character varying(64) DEFAULT '1.0.0'::character varying NOT NULL,
            description text,
            source_url character varying(1024),
            icon_url character varying(2048),
            card_json json NOT NULL,
            supported_interfaces json,
            signatures json,
            extensions json,
            protocol_version character varying(8) DEFAULT '0.3'::character varying NOT NULL,
            extended_card_json json,
            tenant_limits json,
            tenant_governance json,
            signature text,
            signature_algorithm character varying(32),
            signed_by character varying(255),
            signed_at timestamp with time zone,
            trust_tier character varying(32) DEFAULT 'unsigned'::character varying NOT NULL,
            revoked_at timestamp with time zone,
            revoked_reason text,
            enabled boolean DEFAULT true NOT NULL,
            created_at timestamp with time zone DEFAULT now() NOT NULL,
            updated_at timestamp with time zone DEFAULT now() NOT NULL,
            health_status character varying(32) DEFAULT 'unknown'::character varying NOT NULL,
            last_health_check_at timestamp with time zone,
            health_check_error text,
            created_by character varying(255),
            updated_by character varying(255)
        );

        CREATE TABLE IF NOT EXISTS agent_card_capabilities (
            id character varying(255) NOT NULL,
            agent_card_id character varying(255) NOT NULL,
            capability_name character varying(255) NOT NULL,
            description text,
            input_schema json,
            output_schema json,
            timeout_secs integer,
            rate_limit_per_hour integer,
            data_classification json,
            handler_type character varying(32),
            handler_ref character varying(255),
            extensions json,
            enabled boolean DEFAULT true NOT NULL,
            created_at timestamp with time zone DEFAULT now() NOT NULL
        );

        CREATE TABLE IF NOT EXISTS resource_group_agent_cards (
            id character varying(255) NOT NULL,
            resource_group_id character varying(255) NOT NULL,
            agent_card_id character varying(255) NOT NULL,
            limits json,
            created_at timestamp with time zone NOT NULL,
            created_by character varying(255)
        );

        -- Tables the ResourceGroupCache joins when resolving effective
        -- membership. Generated from
        -- `tests/migrations/00000000000000_schema.sql` — the same dump the
        -- `db-tests` suite clones and CI drift-checks — rather than
        -- hand-written, because a hand-written copy is how the rest of
        -- this file fell behind the real schema in the first place.
        --
        -- A missing table here does not surface as "table missing": the
        -- cache swallows the query error and returns an empty set, so the
        -- inheritance tests just see nothing inherited.
        CREATE TABLE IF NOT EXISTS end_user_group_members (
            end_user_group_id character varying(64) NOT NULL,
            end_user_id integer NOT NULL,
            created_at timestamp with time zone NOT NULL,
            source character varying(16) NOT NULL
        );

        CREATE TABLE IF NOT EXISTS kb_collections (
            id character varying(255) NOT NULL,
            tenant_id character varying(255) NOT NULL,
            name character varying(255) NOT NULL,
            description character varying(1024),
            enabled boolean,
            top_k integer,
            similarity_threshold double precision,
            max_context_tokens integer,
            system_prompt_template text,
            created_at timestamp with time zone DEFAULT now() NOT NULL,
            updated_at timestamp with time zone DEFAULT now(),
            created_by character varying(255),
            updated_by character varying(255),
            version character varying(50) DEFAULT '1.0'::character varying NOT NULL
        );

        CREATE TABLE IF NOT EXISTS llm_model (
            id character varying(100) NOT NULL,
            tenant_id character varying(255) NOT NULL,
            name character varying(200) NOT NULL,
            description text,
            usage text,
            provider_id character varying(64) NOT NULL,
            model_name character varying(200) NOT NULL,
            mode character varying(20) NOT NULL,
            parameters double precision,
            tags json,
            api_url_auto_discovered boolean NOT NULL,
            api_url character varying(500),
            auth_type character varying(50),
            api_key text,
            provider_config text,
            default_temperature double precision,
            default_max_tokens integer,
            system_prompt text,
            top_p double precision,
            n integer,
            stop_sequences json,
            seed integer,
            presence_penalty double precision,
            frequency_penalty double precision,
            logit_bias json,
            response_format json,
            logprobs boolean,
            top_logprobs integer,
            stream_options json,
            supports_streaming boolean,
            supports_function_calling boolean,
            supports_vision boolean,
            supports_batch boolean,
            tools json,
            tool_choice character varying(50),
            parallel_tool_calls boolean,
            user_identifier character varying(255),
            api_version character varying(50),
            extra_headers json,
            fallback_models json,
            model_metadata json,
            drop_params boolean,
            additional_drop_params json,
            thinking_enabled boolean NOT NULL,
            routing_weight integer,
            rate_limit_rpm integer,
            rate_limit_tpm integer,
            routing_group_id character varying(100),
            cost_per_input_token double precision,
            cost_per_output_token double precision,
            cost_per_image double precision,
            supported_image_sizes json,
            supported_image_qualities json,
            max_images_per_request integer DEFAULT 1 NOT NULL,
            cost_per_audio_char double precision,
            supported_voices json,
            supported_audio_formats json,
            cost_per_audio_minute double precision,
            max_audio_duration_seconds integer,
            supported_input_formats json,
            supported_response_formats json,
            cost_per_video_second double precision,
            supported_video_resolutions json,
            supported_video_durations json,
            max_video_duration_seconds integer,
            timeout double precision,
            max_concurrent_requests integer,
            allowed_fails integer,
            cooldown_time integer,
            enabled boolean,
            tenant_limits json,
            tenant_governance json,
            retire_date date,
            retirement_announced_at date,
            retirement_reminder_date date,
            retirement_replacement_model_name character varying(255),
            retirement_replacement_provider_key character varying(100),
            retirement_notes text,
            capabilities json,
            catalog_id character varying(255),
            source character varying(16) DEFAULT 'manual'::character varying NOT NULL,
            source_external_id character varying(255),
            health_status character varying(50),
            last_health_check timestamp with time zone,
            last_health_success timestamp with time zone,
            consecutive_health_failures integer,
            health_check_error text,
            last_probe_type character varying(20),
            total_requests integer,
            failed_requests integer,
            total_tokens_consumed integer,
            avg_latency_ms double precision,
            created_at timestamp with time zone DEFAULT now() NOT NULL,
            updated_at timestamp with time zone DEFAULT now() NOT NULL,
            created_by character varying(255),
            updated_by character varying(255),
            version character varying(50) DEFAULT '1.0'::character varying NOT NULL
        );

        CREATE TABLE IF NOT EXISTS resource_group_agent_skills (
            id character varying(255) NOT NULL,
            resource_group_id character varying(255) NOT NULL,
            agent_skill_id character varying(255) NOT NULL,
            created_at timestamp with time zone NOT NULL,
            created_by character varying(255),
            limits json,
            governance json
        );

        CREATE TABLE IF NOT EXISTS resource_group_cache_configs (
            resource_group_id character varying(255) NOT NULL,
            cache_config_id character varying(255) NOT NULL
        );

        CREATE TABLE IF NOT EXISTS resource_group_kb_collections (
            resource_group_id character varying(255) NOT NULL,
            kb_collection_id character varying(255) NOT NULL
        );

        CREATE TABLE IF NOT EXISTS resource_group_llm_models (
            id character varying(255) NOT NULL,
            resource_group_id character varying(255) NOT NULL,
            llm_model_id character varying(255) NOT NULL,
            created_at timestamp with time zone NOT NULL,
            created_by character varying(255),
            limits json,
            portal_visible boolean DEFAULT true NOT NULL,
            auto_bound boolean DEFAULT false NOT NULL
        );

        CREATE TABLE IF NOT EXISTS resource_group_server_configs (
            id character varying(255) NOT NULL,
            resource_group_id character varying(255) NOT NULL,
            server_config_id character varying(255) NOT NULL,
            created_at timestamp with time zone NOT NULL,
            created_by character varying(255),
            limits json
        );

        CREATE TABLE IF NOT EXISTS semantic_cache_configs (
            id character varying(255) NOT NULL,
            tenant_id character varying(255) NOT NULL,
            name character varying(255) NOT NULL,
            description character varying(1024),
            enabled boolean,
            exact_match_enabled boolean,
            cache_key_mode character varying(20) DEFAULT 'last_user_turn'::character varying NOT NULL,
            exact_match_ttl_seconds integer,
            semantic_match_enabled boolean,
            similarity_threshold double precision,
            semantic_match_ttl_seconds integer,
            max_entries integer,
            time_bucketing character varying(10),
            exclude_tool_calls boolean,
            exclude_temperature_above double precision,
            exclude_guardrail_flagged boolean,
            shared_pool_enabled boolean,
            shared_pool_contribute boolean,
            shared_pool_consume boolean,
            shared_pool_model_filter json,
            created_at timestamp with time zone DEFAULT now() NOT NULL,
            updated_at timestamp with time zone DEFAULT now(),
            created_by character varying(255)
        );

        CREATE TABLE IF NOT EXISTS semantic_cache_tool_rules (
            id integer NOT NULL,
            cache_config_id character varying(255) NOT NULL,
            tenant_id character varying(255) NOT NULL,
            server_config_id character varying(255),
            capability_type character varying(20),
            capability_name character varying(500),
            cacheable boolean NOT NULL,
            priority integer NOT NULL,
            created_at timestamp with time zone DEFAULT now() NOT NULL,
            updated_at timestamp with time zone DEFAULT now()
        );

        CREATE TABLE IF NOT EXISTS server_configs (
            id character varying(255) NOT NULL,
            tenant_id character varying(255) NOT NULL,
            mcp_server_id character varying(255),
            virtual_server_id character varying(255),
            oauth_client_id character varying(255),
            name character varying(255) NOT NULL,
            description text,
            priority integer NOT NULL,
            is_default boolean NOT NULL,
            enabled boolean NOT NULL,
            display_name character varying(255),
            display_description text,
            icon_url character varying(512),
            usage_count integer NOT NULL,
            last_used timestamp without time zone,
            created_at timestamp without time zone DEFAULT now() NOT NULL,
            updated_at timestamp without time zone DEFAULT now() NOT NULL,
            created_by character varying(255),
            updated_by character varying(255)
        );

        -- pg_dump emits primary keys as separate ALTER TABLE statements,
        -- so the CREATE TABLE bodies copied above carry none. Without a
        -- unique constraint an `ON CONFLICT (id)` upsert fails with
        -- "no unique or exclusion constraint matching the ON CONFLICT
        -- specification". Unique indexes give the same guarantee and,
        -- unlike ADD CONSTRAINT, can be declared IF NOT EXISTS.
        CREATE UNIQUE INDEX IF NOT EXISTS agent_cards_pk_ix ON agent_cards (id);
        CREATE UNIQUE INDEX IF NOT EXISTS agent_card_capabilities_pk_ix ON agent_card_capabilities (id);
        CREATE UNIQUE INDEX IF NOT EXISTS resource_group_agent_cards_pk_ix ON resource_group_agent_cards (id);
        CREATE UNIQUE INDEX IF NOT EXISTS end_user_group_members_pk_ix ON end_user_group_members (end_user_group_id, end_user_id);
        CREATE UNIQUE INDEX IF NOT EXISTS kb_collections_pk_ix ON kb_collections (id);
        CREATE UNIQUE INDEX IF NOT EXISTS llm_model_pk_ix ON llm_model (id);
        CREATE UNIQUE INDEX IF NOT EXISTS resource_group_agent_skills_pk_ix ON resource_group_agent_skills (id);
        CREATE UNIQUE INDEX IF NOT EXISTS resource_group_cache_configs_pk_ix ON resource_group_cache_configs (resource_group_id, cache_config_id);
        CREATE UNIQUE INDEX IF NOT EXISTS resource_group_kb_collections_pk_ix ON resource_group_kb_collections (resource_group_id, kb_collection_id);
        CREATE UNIQUE INDEX IF NOT EXISTS resource_group_llm_models_pk_ix ON resource_group_llm_models (id);
        CREATE UNIQUE INDEX IF NOT EXISTS resource_group_server_configs_pk_ix ON resource_group_server_configs (id);
        CREATE UNIQUE INDEX IF NOT EXISTS semantic_cache_configs_pk_ix ON semantic_cache_configs (id);
        CREATE UNIQUE INDEX IF NOT EXISTS semantic_cache_tool_rules_pk_ix ON semantic_cache_tool_rules (id);
        CREATE UNIQUE INDEX IF NOT EXISTS server_configs_pk_ix ON server_configs (id);

        CREATE TABLE IF NOT EXISTS resource_group_end_user_groups (
            resource_group_id VARCHAR(255) NOT NULL,
            end_user_group_id VARCHAR(64) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_by VARCHAR(255),
            PRIMARY KEY (resource_group_id, end_user_group_id)
        );

        CREATE TABLE IF NOT EXISTS semantic_policies (
            id VARCHAR(255) PRIMARY KEY,
            tenant_id VARCHAR(255) NOT NULL,
            name VARCHAR(255) NOT NULL,
            description TEXT,
            constraint_text TEXT NOT NULL,
            action VARCHAR(32) NOT NULL,
            mode VARCHAR(32) NOT NULL,
            surfaces JSONB,
            judge_model_id VARCHAR(255),
            enabled BOOLEAN NOT NULL DEFAULT true,
            created_by VARCHAR(255),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS resource_group_memberships (
            id SERIAL PRIMARY KEY,
            tenant_id VARCHAR(255) NOT NULL,
            user_id INTEGER NOT NULL,
            group_id VARCHAR(255) NOT NULL
        );

        CREATE TABLE IF NOT EXISTS usage_alerts (
            id VARCHAR(255) PRIMARY KEY,
            tenant_id VARCHAR(255) NOT NULL,
            resource_type VARCHAR(50) NOT NULL,
            group_id VARCHAR(255),
            metric VARCHAR(100) NOT NULL,
            threshold_pct INTEGER NOT NULL,
            alert_type VARCHAR(50) NOT NULL DEFAULT 'warning',
            status VARCHAR(50) NOT NULL DEFAULT 'active',
            current_value FLOAT8,
            limit_value FLOAT8,
            message TEXT,
            period_start TIMESTAMPTZ,
            acknowledged_by VARCHAR(255),
            acknowledged_at TIMESTAMPTZ,
            resolved_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );

        -- `auth.rs` authenticates with `SELECT ak.* FROM api_keys ak`,
        -- which maps the whole row into a struct — so every field that
        -- struct declares has to exist as a column or sqlx fails the query
        -- with `no column found for name: <field>`. The route then answers
        -- 500, and the test sees only the status code. Keep this in step
        -- with the real `api_keys` table, not with what one test happens
        -- to insert.
        CREATE TABLE IF NOT EXISTS api_keys (
            id VARCHAR(255) PRIMARY KEY,
            tenant_id VARCHAR(255) NOT NULL,
            key_hash VARCHAR(255) NOT NULL,
            key_prefix VARCHAR(255),
            name VARCHAR(255),
            description TEXT,
            scope VARCHAR(50),
            allowed_skill_ids JSON,
            skill_rate_limit_daily INTEGER,
            user_id INTEGER,
            user_email VARCHAR(255),
            agent_id VARCHAR(255),
            group_id VARCHAR(255),
            expires_at TIMESTAMPTZ,
            enabled BOOLEAN NOT NULL DEFAULT true,
            revoked BOOLEAN NOT NULL DEFAULT false,
            revoked_at TIMESTAMPTZ,
            revoked_by VARCHAR(255),
            revoked_reason TEXT,
            last_used_at TIMESTAMPTZ,
            last_used_by_user_id INTEGER,
            usage_count INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            created_by VARCHAR(255),
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            updated_by VARCHAR(255)
        );

        -- On CI (empty DB), the minimal tenants table created above is used.
        -- On dev, the real tenants table already has data.
        -- This INSERT only works on the minimal CI schema (no extra NOT NULL columns).
        -- It's a no-op on dev because tenants already exist.
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM tenants LIMIT 1) THEN
                -- Spell out the status flags. The real schema declares
                -- them NOT NULL with no default, and the baseline schema
                -- above drops NOT NULL so tests can insert partial rows —
                -- which leaves these NULL. A tenant that is neither active
                -- nor un-suspended fails the middleware's status check and
                -- every route answers 503, with nothing logged to say why.
                INSERT INTO tenants
                    (id, name, display_name, is_active, is_default,
                     is_trial, suspended)
                VALUES
                    ('default', 'default', 'default', true, true,
                     false, false);
            END IF;
        END $$;
    "#;

    sqlx::raw_sql(schema)
        .execute(&mut *conn)
        .await
        .expect("Failed to create test schema");

    // Release the advisory lock — same connection that took it.
    sqlx::query("SELECT pg_advisory_unlock(42)")
        .execute(&mut *conn)
        .await
        .expect("Failed to release advisory lock");
}
