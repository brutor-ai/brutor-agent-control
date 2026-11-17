// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! `GatewayBuilder` and the `Extension` trait.
//!
//! A test-only extension, defined here and nowhere in `src/`, contributes
//! one of each thing an extension can own — state, a route, a task, an
//! invalidation handler, an OpenAPI fragment — and each takes effect where
//! the built-in one is read. With no extensions the assembly is the built-in
//! gateway: the same routes, tasks, handlers and document. A contribution
//! that collides with what exists is refused as a whole.

use std::net::SocketAddr;
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};

use axum::Router;
use axum::body::Body;
use axum::extract::{ConnectInfo, State};
use axum::http::{Request, StatusCode};
use axum::routing::get;
use serde_json::json;
use sqlx::PgPool;
use tower::ServiceExt;
use utoipa::OpenApi;

use brutor_gateway_core::builder::{COMMUNITY_API_LICENCE, Extension, GatewayBuilder};
use brutor_gateway_core::config::AppConfig;
use brutor_gateway_core::openapi::ApiDoc;
use brutor_gateway_core::services::cache::invalidation::HandlerFuture;
use brutor_gateway_core::services::cache::{
    CacheInvalidator, InvalidationHandler, InvalidationMessage,
};
use brutor_gateway_core::services::telemetry::TelemetryState;
use brutor_gateway_core::services::{AppState, SharedState};
use brutor_gateway_core::tasks::{BACKGROUND_TASKS, BackgroundTask, TaskContext};

use crate::common;

const TENANT: &str = "tenant-ext-assembly";
const GROUP: &str = "rg-ext-assembly";

// ---------------------------------------------------------------------------
// The test-only extension
// ---------------------------------------------------------------------------

/// The extension's own state: a counter the task bumps and the route reads.
struct ReportsState {
    task_started: AtomicUsize,
    label: &'static str,
}

static INVALIDATIONS: AtomicUsize = AtomicUsize::new(0);

async fn reports(State(state): State<SharedState>) -> String {
    let own = state
        .extensions
        .get::<ReportsState>()
        .expect("the extension's state is on AppState");
    format!("{}:{}", own.label, own.task_started.load(Ordering::SeqCst))
}

fn reports_task(ctx: &TaskContext<'_>) {
    ctx.state
        .extensions
        .get::<ReportsState>()
        .expect("state installed before tasks start")
        .task_started
        .fetch_add(1, Ordering::SeqCst);
}

fn reports_invalidation<'a>(
    _invalidator: &'a CacheInvalidator,
    _message: &'a InvalidationMessage,
) -> HandlerFuture<'a> {
    Box::pin(async {
        INVALIDATIONS.fetch_add(1, Ordering::SeqCst);
    })
}

struct Reports;

impl Extension for Reports {
    fn name(&self) -> &'static str {
        "reports"
    }
    fn interface_version(&self) -> u32 {
        1
    }
    fn install_state(&self, state: &mut AppState) -> anyhow::Result<()> {
        state.extensions.insert(ReportsState {
            task_started: AtomicUsize::new(0),
            label: "reports",
        })?;
        Ok(())
    }
    fn routes(&self) -> Option<Router<SharedState>> {
        Some(Router::new().route("/v1/proxy/ext/reports", get(reports)))
    }
    fn tasks(&self) -> &'static [BackgroundTask] {
        &[BackgroundTask {
            name: "ext_reports_task",
            feature: None,
            start: reports_task,
        }]
    }
    fn invalidation_handlers(&self) -> &'static [InvalidationHandler] {
        &[InvalidationHandler {
            name: "ext_reports",
            resource_types: &["ext_report"],
            feature: None,
            handle: reports_invalidation,
            flush: None,
        }]
    }
    fn openapi(&self) -> Option<serde_json::Value> {
        Some(json!({
            "paths": { "/v1/proxy/ext/reports": { "get": { "operationId": "ext_reports", "responses": { "200": { "description": "ok" } } } } },
            "components": { "schemas": { "ExtReport": { "type": "object" } } }
        }))
    }
}

/// An extension that mounts a route under a prefix of its own, and — when
/// told to — registers what the tenant layer requires of it.
struct OwnPrefix {
    rule: Option<brutor_gateway_core::services::tenant_paths::TenantRule>,
}

impl Extension for OwnPrefix {
    fn name(&self) -> &'static str {
        "own_prefix"
    }
    fn interface_version(&self) -> u32 {
        1
    }
    fn install_state(&self, state: &mut AppState) -> anyhow::Result<()> {
        use brutor_gateway_core::services::tenant_paths::PathPattern;
        if let Some(rule) = self.rule {
            state
                .tenant_paths
                .register(PathPattern::Prefix("/v1/ext/"), rule)?;
        }
        Ok(())
    }
    fn routes(&self) -> Option<Router<SharedState>> {
        Some(Router::new().route("/v1/ext/hello", get(|| async { "hello" })))
    }
}

/// An extension that collides with something; which thing is the parameter.
struct Colliding(&'static str);

impl Extension for Colliding {
    fn name(&self) -> &'static str {
        if self.0 == "name" {
            "reports"
        } else {
            "colliding"
        }
    }
    fn interface_version(&self) -> u32 {
        1
    }
    fn tasks(&self) -> &'static [BackgroundTask] {
        match self.0 {
            "builtin_task" => &[BackgroundTask {
                name: "pool_sampler",
                feature: None,
                start: reports_task,
            }],
            "ext_task" => &[BackgroundTask {
                name: "ext_reports_task",
                feature: None,
                start: reports_task,
            }],
            _ => &[],
        }
    }
    fn invalidation_handlers(&self) -> &'static [InvalidationHandler] {
        match self.0 {
            "handler" => &[InvalidationHandler {
                name: "ext_reports",
                resource_types: &["other"],
                feature: None,
                handle: reports_invalidation,
                flush: None,
            }],
            "handler_no_types" => &[InvalidationHandler {
                name: "ext_other",
                resource_types: &[],
                feature: None,
                handle: reports_invalidation,
                flush: None,
            }],
            _ => &[],
        }
    }
    fn openapi(&self) -> Option<serde_json::Value> {
        match self.0 {
            "path" => Some(json!({ "paths": { "/health": {} } })),
            "ext_path" => Some(json!({ "paths": { "/v1/proxy/ext/reports": {} } })),
            "schema" => Some(json!({ "components": { "schemas": { "ExtReport": {} } } })),
            _ => None,
        }
    }
    fn install_state(&self, state: &mut AppState) -> anyhow::Result<()> {
        if self.0 == "state" {
            state.extensions.insert(ReportsState {
                task_started: AtomicUsize::new(0),
                label: "x",
            })?;
        }
        Ok(())
    }
}

// ---------------------------------------------------------------------------

fn test_state(pool: PgPool) -> AppState {
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

async fn get_as(app: &Router, path: &str, api_key: &str) -> (StatusCode, String) {
    let mut builder = Request::builder().method("GET").uri(path);
    if !api_key.is_empty() {
        // The tenant layer resolves the tenant from the key or from the header;
        // an anonymous probe sends neither.
        builder = builder
            .header("authorization", format!("Bearer {api_key}"))
            .header("x-tenant-id", TENANT);
    }
    let mut request = builder.body(Body::empty()).expect("request");
    let peer: SocketAddr = "127.0.0.1:55555".parse().expect("peer");
    request.extensions_mut().insert(ConnectInfo(peer));
    let response = app.clone().oneshot(request).await.expect("router");
    let status = response.status();
    // 1 MiB: the served OpenAPI document is read through here and is over
    // 64 KiB.
    let body = axum::body::to_bytes(response.into_body(), 1 << 20)
        .await
        .expect("body");
    (status, String::from_utf8_lossy(&body).into_owned())
}

async fn api_key(pool: &PgPool) -> String {
    common::seed_tenant(pool, TENANT).await;
    common::seed_resource_group(pool, GROUP, TENANT).await;
    common::seed_api_key(pool, TENANT, GROUP, None, "ext-assembly").await
}

async fn document_of(app: &Router) -> serde_json::Value {
    let (status, body) = get_as(app, brutor_gateway_core::openapi::OPENAPI_JSON_PATH, "").await;
    assert_eq!(status, StatusCode::OK);
    serde_json::from_str(&body).expect("json")
}

/// Without the registry a route under a prefix of
/// the extension's own would answer an anonymous request: the tenant layer
/// would require nothing of a path it does not know. Such a path gets the
/// most restrictive rule, and the extension says what it wants instead.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_extensions_own_prefix_is_restricted_until_it_registers_a_rule(pool: PgPool) {
    use brutor_gateway_core::services::tenant_paths::{PathPattern, TenantRule};
    let key = api_key(&pool).await;
    sqlx::query(
        "INSERT INTO tenants (id, name, display_name, primary_contact_email, is_active, \
         is_default, is_trial, suspended, features, settings) VALUES ('ext-suspended', \
         'ext-suspended', 'ext-suspended', 'a@test.local', true, false, false, true, \
         '{}'::json, '{}'::json)",
    )
    .execute(&pool)
    .await
    .expect("a suspended tenant");
    let with_tenant = |app: Router, tenant: &'static str| async move {
        let mut request = Request::builder()
            .method("GET")
            .uri("/v1/ext/hello")
            .header("x-tenant-id", tenant)
            .body(Body::empty())
            .expect("request");
        let peer: SocketAddr = "127.0.0.1:55555".parse().expect("peer");
        request.extensions_mut().insert(ConnectInfo(peer));
        app.oneshot(request).await.expect("router").status()
    };

    // Nothing registered: a tenant is required, an unknown one refused, a
    // suspended one blocked. Never "no tenant required".
    let gateway = GatewayBuilder::assemble(
        test_state(pool.clone()),
        vec![Box::new(OwnPrefix { rule: None })],
    )
    .expect("assemble");
    assert_eq!(
        gateway.state.tenant_paths.rule_for("/v1/ext/hello"),
        TenantRule::MOST_RESTRICTIVE
    );
    assert_eq!(
        get_as(&gateway.app, "/v1/ext/hello", "").await.0,
        StatusCode::UNAUTHORIZED,
        "anonymous, on a path nobody registered"
    );
    assert_eq!(
        get_as(&gateway.app, "/v1/ext/hello", &key).await,
        (StatusCode::OK, "hello".to_string())
    );
    assert_eq!(
        with_tenant(gateway.app.clone(), "no-such-tenant").await,
        StatusCode::FORBIDDEN
    );
    assert_eq!(
        with_tenant(gateway.app.clone(), "ext-suspended").await,
        StatusCode::FORBIDDEN
    );

    // The extension registers its prefix as open: its route answers anyone.
    let gateway = GatewayBuilder::assemble(
        test_state(pool.clone()),
        vec![Box::new(OwnPrefix {
            rule: Some(TenantRule::Open),
        })],
    )
    .expect("assemble");
    assert_eq!(
        get_as(&gateway.app, "/v1/ext/hello", "").await,
        (StatusCode::OK, "hello".to_string())
    );
    assert_eq!(
        with_tenant(gateway.app.clone(), "ext-suspended").await,
        StatusCode::OK
    );

    // Runtime traffic that needs no tenant header: blocked when suspended.
    let gateway = GatewayBuilder::assemble(
        test_state(pool.clone()),
        vec![Box::new(OwnPrefix {
            rule: Some(TenantRule::RUNTIME),
        })],
    )
    .expect("assemble");
    assert_eq!(
        get_as(&gateway.app, "/v1/ext/hello", "").await.0,
        StatusCode::OK
    );
    assert_eq!(
        with_tenant(gateway.app.clone(), "ext-suspended").await,
        StatusCode::FORBIDDEN
    );

    // An extension cannot take a pattern that is in place: the assembly is
    // refused as a whole, and the proxy rule stands.
    struct Loosens;
    impl Extension for Loosens {
        fn name(&self) -> &'static str {
            "loosens"
        }
        fn interface_version(&self) -> u32 {
            1
        }
        fn install_state(&self, state: &mut AppState) -> anyhow::Result<()> {
            state
                .tenant_paths
                .register(PathPattern::Prefix("/v1/proxy/"), TenantRule::Open)?;
            Ok(())
        }
    }
    let refused = GatewayBuilder::assemble(test_state(pool.clone()), vec![Box::new(Loosens)])
        .err()
        .expect("refused");
    assert!(
        refused.to_string().contains("already has a rule"),
        "{refused}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn without_extensions_the_assembly_is_the_builtin_gateway(pool: PgPool) {
    let gateway = GatewayBuilder::assemble(test_state(pool.clone()), vec![]).expect("assemble");
    assert!(gateway.extensions.is_empty());
    assert_eq!(
        gateway.task_names(),
        BACKGROUND_TASKS.iter().map(|t| t.name).collect::<Vec<_>>()
    );
    assert_eq!(
        gateway
            .state
            .invalidation
            .handlers()
            .iter()
            .map(|h| h.name)
            .collect::<Vec<_>>(),
        brutor_gateway_core::services::cache::InvalidationRegistry::builtin()
            .handlers()
            .iter()
            .map(|h| h.name)
            .collect::<Vec<_>>()
    );
    assert!(gateway.state.extensions.is_empty());
    let key = api_key(&pool).await;
    assert_eq!(
        get_as(&gateway.app, "/v1/proxy/ext/reports", &key).await.0,
        StatusCode::NOT_FOUND
    );
    assert_eq!(get_as(&gateway.app, "/health", "").await.0, StatusCode::OK);
    // the document served is the derived one, byte for byte, with the
    // Community licence as `info.license`
    assert_eq!(document_of(&gateway.app).await, community_document());
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_extension_contributes_state_a_route_a_task_a_handler_and_openapi(pool: PgPool) {
    let gateway = GatewayBuilder::assemble(test_state(pool.clone()), vec![Box::new(Reports)])
        .expect("assemble");
    assert_eq!(gateway.extensions, vec!["reports"]);

    // state: installed before anything reads it
    assert_eq!(
        gateway
            .state
            .extensions
            .get::<ReportsState>()
            .expect("state")
            .label,
        "reports"
    );

    // tasks: after the built-in ones, and started by start_tasks
    let names = gateway.task_names();
    assert_eq!(
        names[..BACKGROUND_TASKS.len()],
        BACKGROUND_TASKS.iter().map(|t| t.name).collect::<Vec<_>>()[..]
    );
    assert_eq!(names[BACKGROUND_TASKS.len()..], ["ext_reports_task"]);
    // (only the extension's task is started here: the built-in ones need Redis
    // and a live config; `start_tasks` is the one `main` uses, pinned by the
    // task-registry guard, so what is proven is the list it starts from)
    (gateway.tasks.last().expect("task").start)(&TaskContext {
        state: &gateway.state,
        config: &gateway.state.config,
    });

    // route: inside the middleware stack. Under `/v1/proxy/` the tenant layer
    // requires a tenant (`services::tenant_paths`): a request with an API key
    // is admitted, an anonymous one refused. A route an extension mounts
    // under a prefix of its own gets the most restrictive rule unless the
    // extension registers one for it: see
    // `an_extensions_own_prefix_is_restricted_until_it_registers_a_rule`.
    let key = api_key(&pool).await;
    assert_eq!(
        get_as(&gateway.app, "/v1/proxy/ext/reports", &key).await,
        (StatusCode::OK, "reports:1".to_string())
    );
    assert_eq!(
        get_as(&gateway.app, "/v1/proxy/ext/reports", "").await.0,
        StatusCode::UNAUTHORIZED
    );

    // invalidation handler: after the built-in ones, consulted for its type
    let handlers = gateway.state.invalidation.handlers();
    assert_eq!(handlers.last().expect("handler").name, "ext_reports");
    assert_eq!(
        gateway
            .state
            .invalidation
            .handlers_for("ext_report")
            .map(|h| h.name)
            .collect::<Vec<_>>(),
        vec!["ext_reports"]
    );

    // openapi: the fragment is in the served document, the rest unchanged
    let document = document_of(&gateway.app).await;
    assert_eq!(
        document["paths"]["/v1/proxy/ext/reports"]["get"]["operationId"],
        "ext_reports"
    );
    assert!(document["components"]["schemas"]["ExtReport"].is_object());
    let mut without = document.clone();
    without["paths"]
        .as_object_mut()
        .unwrap()
        .remove("/v1/proxy/ext/reports");
    without["components"]["schemas"]
        .as_object_mut()
        .unwrap()
        .remove("ExtReport");
    assert_eq!(without, community_document());
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_colliding_extension_is_refused_as_a_whole(pool: PgPool) {
    for (what, expect) in [
        ("name", "two extensions are named reports"),
        (
            "builtin_task",
            "a task named pool_sampler is already registered",
        ),
        (
            "ext_task",
            "a task named ext_reports_task is already registered",
        ),
        ("handler", "invalidation handler refused"),
        ("handler_no_types", "invalidation handler refused"),
        ("path", "OpenAPI path /health is already documented"),
        (
            "ext_path",
            "OpenAPI path /v1/proxy/ext/reports is already documented",
        ),
        ("schema", "OpenAPI schema ExtReport is already documented"),
        ("state", "already holds a value of type"),
    ] {
        let err = GatewayBuilder::assemble(
            test_state(pool.clone()),
            vec![Box::new(Reports), Box::new(Colliding(what))],
        )
        .err()
        .unwrap_or_else(|| panic!("{what}: assembled"));
        assert!(err.to_string().contains(expect), "{what}: {err}");
    }
    // an empty name
    struct Nameless;
    impl Extension for Nameless {
        fn name(&self) -> &'static str {
            ""
        }
        fn interface_version(&self) -> u32 {
            1
        }
    }
    let err = GatewayBuilder::assemble(test_state(pool.clone()), vec![Box::new(Nameless)])
        .err()
        .expect("refused");
    assert!(err.to_string().contains("empty name"), "{err}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn two_extensions_keep_their_order(pool: PgPool) {
    struct Second;
    impl Extension for Second {
        fn name(&self) -> &'static str {
            "second"
        }
        fn interface_version(&self) -> u32 {
            1
        }
        fn tasks(&self) -> &'static [BackgroundTask] {
            &[BackgroundTask {
                name: "ext_second_task",
                feature: None,
                start: reports_task,
            }]
        }
    }
    let gateway =
        GatewayBuilder::assemble(test_state(pool), vec![Box::new(Reports), Box::new(Second)])
            .expect("assemble");
    assert_eq!(gateway.extensions, vec!["reports", "second"]);
    let names = gateway.task_names();
    assert_eq!(
        names[names.len() - 2..],
        ["ext_reports_task", "ext_second_task"]
    );
}

// ---------------------------------------------------------------------------
// Interface version 1
// ---------------------------------------------------------------------------

type StepLog = Arc<std::sync::Mutex<Vec<String>>>;

/// What [`Steps`] installs in `prepare`: proof that the step ran with a
/// connected pool, read back from the shared state afterwards.
struct Prepared {
    answer_from_the_database: i32,
}

/// An extension that records each step the builder calls, in order, and
/// fails the one it is told to.
struct Steps {
    name: &'static str,
    version: u32,
    fail: &'static str,
    multiple_instances: bool,
    log: StepLog,
}

impl Steps {
    fn new(log: &StepLog) -> Self {
        Self {
            name: "steps",
            version: 1,
            fail: "",
            multiple_instances: false,
            log: log.clone(),
        }
    }
    fn step(&self, step: &str) -> anyhow::Result<()> {
        self.log
            .lock()
            .expect("log")
            .push(format!("{}:{step}", self.name));
        if self.fail == step {
            anyhow::bail!("{step} said no");
        }
        Ok(())
    }
}

#[async_trait::async_trait]
impl Extension for Steps {
    fn name(&self) -> &'static str {
        self.name
    }
    fn interface_version(&self) -> u32 {
        self.version
    }
    fn allows_multiple_instances(&self) -> bool {
        self.multiple_instances
    }
    fn preflight(&self, config: &AppConfig) -> anyhow::Result<()> {
        // The configuration is the gateway's own.
        assert_eq!(config.jwt_algorithm, "HS256");
        self.step("preflight")
    }
    async fn prepare(&self, state: &mut AppState) -> anyhow::Result<()> {
        self.step("prepare")?;
        // The pool is connected, and the state is still the extension's to
        // write.
        let answer: i32 = sqlx::query_scalar("SELECT 41 + 1")
            .fetch_one(&state.db)
            .await?;
        state.extensions.insert(Prepared {
            answer_from_the_database: answer,
        })?;
        Ok(())
    }
    fn install_state(&self, _state: &mut AppState) -> anyhow::Result<()> {
        self.step("install_state")
    }
    async fn after_assembly(&self, state: &SharedState) -> anyhow::Result<()> {
        self.step("after_assembly")?;
        // The state is the shared, complete one: what `prepare` installed is
        // on it.
        let prepared = state
            .extensions
            .get::<Prepared>()
            .ok_or_else(|| anyhow::anyhow!("what prepare installed is not on the state"))?;
        anyhow::ensure!(prepared.answer_from_the_database == 42);
        Ok(())
    }
}

fn steps_of(log: &StepLog) -> Vec<String> {
    log.lock().expect("log").clone()
}

fn refusal_of(result: anyhow::Result<brutor_gateway_core::builder::Gateway>) -> String {
    result.err().expect("refused").to_string()
}

#[test]
fn the_interface_is_version_one() {
    // A change of this number is a change of what extensions must be
    // rebuilt for: make it on purpose, with the plan.
    assert_eq!(brutor_gateway_core::builder::EXTENSION_INTERFACE_VERSION, 1);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_checked_assembly_calls_an_extensions_steps_in_order(pool: PgPool) {
    let log = StepLog::default();
    let gateway = GatewayBuilder::assemble_checked(
        test_state(pool.clone()),
        vec![Box::new(Steps::new(&log))],
    )
    .await
    .expect("assemble");
    assert_eq!(
        steps_of(&log),
        [
            "steps:preflight",
            "steps:prepare",
            // the pure assembly admits its extensions itself
            "steps:preflight",
            "steps:install_state",
            "steps:after_assembly",
        ]
    );
    assert_eq!(
        gateway
            .state
            .extensions
            .get::<Prepared>()
            .expect("installed by prepare")
            .answer_from_the_database,
        42
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_pure_assembly_runs_no_async_step(pool: PgPool) {
    let log = StepLog::default();
    let gateway = GatewayBuilder::assemble(test_state(pool), vec![Box::new(Steps::new(&log))])
        .expect("assemble");
    assert_eq!(steps_of(&log), ["steps:preflight", "steps:install_state"]);
    assert!(gateway.state.extensions.get::<Prepared>().is_none());
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_extension_written_for_another_interface_version_is_refused(pool: PgPool) {
    for version in [0, 2, u32::MAX] {
        let log = StepLog::default();
        let first_log = StepLog::default();
        let other = Steps {
            name: "from_another_release",
            version,
            ..Steps::new(&log)
        };
        // Refused on both paths, and whatever stands before it in the list
        // has run nothing either: versions are checked before any step.
        let expected = format!(
            "extension from_another_release was written for version {version} of the extension \
             interface and this gateway implements version 1: refusing to assemble it. Build \
             the extension against this gateway's interface."
        );
        let refused = refusal_of(
            GatewayBuilder::assemble_checked(
                test_state(pool.clone()),
                vec![Box::new(Steps::new(&first_log)), Box::new(other)],
            )
            .await,
        );
        assert_eq!(refused, expected);
        let other = Steps {
            name: "from_another_release",
            version,
            ..Steps::new(&log)
        };
        let refused = refusal_of(GatewayBuilder::assemble(
            test_state(pool.clone()),
            vec![Box::new(Steps::new(&first_log)), Box::new(other)],
        ));
        assert_eq!(refused, expected);
        assert!(steps_of(&log).is_empty(), "{:?}", steps_of(&log));
        assert!(
            steps_of(&first_log).is_empty(),
            "{:?}",
            steps_of(&first_log)
        );
    }
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_extension_whose_step_fails_refuses_the_start(pool: PgPool) {
    for (fail, expected, ran) in [
        (
            "preflight",
            "extension steps: refused before start: preflight said no",
            vec!["steps:preflight"],
        ),
        (
            "prepare",
            "extension steps: preparation failed: prepare said no",
            vec!["steps:preflight", "steps:prepare"],
        ),
        (
            "install_state",
            "extension steps: state: install_state said no",
            vec![
                "steps:preflight",
                "steps:prepare",
                "steps:preflight",
                "steps:install_state",
            ],
        ),
        (
            "after_assembly",
            "extension steps: the step after assembly failed: after_assembly said no",
            vec![
                "steps:preflight",
                "steps:prepare",
                "steps:preflight",
                "steps:install_state",
                "steps:after_assembly",
            ],
        ),
    ] {
        let log = StepLog::default();
        let refused = refusal_of(
            GatewayBuilder::assemble_checked(
                test_state(pool.clone()),
                vec![Box::new(Steps {
                    fail,
                    ..Steps::new(&log)
                })],
            )
            .await,
        );
        assert_eq!(refused, expected, "{fail}");
        assert_eq!(steps_of(&log), ran, "{fail}");
    }
    // A refusal before start stops every extension, not only the one that
    // refused: the one listed first has prepared nothing.
    let first = StepLog::default();
    let second = StepLog::default();
    let refused = refusal_of(
        GatewayBuilder::assemble_checked(
            test_state(pool.clone()),
            vec![
                Box::new(Steps {
                    name: "first",
                    ..Steps::new(&first)
                }),
                Box::new(Steps {
                    name: "second",
                    fail: "preflight",
                    ..Steps::new(&second)
                }),
            ],
        )
        .await,
    );
    assert_eq!(
        refused,
        "extension second: refused before start: preflight said no"
    );
    assert_eq!(steps_of(&first), ["first:preflight"]);
}

/// The step after assembly reads the installation, so it comes after the
/// Enterprise-data check: on a database the check refuses,
/// it never runs.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_step_after_assembly_does_not_run_on_a_refused_start(pool: PgPool) {
    common::seed_tenant(&pool, TENANT).await;
    sqlx::query("UPDATE tenants SET features = '{\"knowledge_base\": true}'::json WHERE id = $1")
        .bind(TENANT)
        .execute(&pool)
        .await
        .expect("an Enterprise feature switched on");
    let log = StepLog::default();
    let refused = refusal_of(
        GatewayBuilder::assemble_checked(test_state(pool), vec![Box::new(Steps::new(&log))]).await,
    );
    assert!(
        refused.contains("Enterprise feature flag knowledge_base"),
        "{refused}"
    );
    assert_eq!(
        steps_of(&log),
        [
            "steps:preflight",
            "steps:prepare",
            "steps:preflight",
            "steps:install_state"
        ]
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_composition_is_one_instance_unless_an_extension_says_otherwise(pool: PgPool) {
    let log = StepLog::default();
    let none = GatewayBuilder::assemble(test_state(pool.clone()), vec![]).expect("assemble");
    assert!(!none.allows_multiple_instances());
    // The default of an extension that says nothing.
    let silent = GatewayBuilder::assemble(test_state(pool.clone()), vec![Box::new(Reports)])
        .expect("assemble");
    assert!(!silent.allows_multiple_instances());
    let single = GatewayBuilder::assemble(
        test_state(pool.clone()),
        vec![Box::new(Reports), Box::new(Steps::new(&log))],
    )
    .expect("assemble");
    assert!(!single.allows_multiple_instances());
    let clustered = GatewayBuilder::assemble(
        test_state(pool),
        vec![
            Box::new(Reports),
            Box::new(Steps {
                multiple_instances: true,
                ..Steps::new(&log)
            }),
        ],
    )
    .expect("assemble");
    assert!(clustered.allows_multiple_instances());
}

#[test]
fn the_builder_lists_its_extensions_in_order() {
    let builder = GatewayBuilder::new(AppConfig::for_test(
        "postgresql://placeholder/unused".to_string(),
    ))
    .extension(Reports)
    .extension(Colliding("none"));
    assert_eq!(builder.extension_names(), vec!["reports", "colliding"]);
    let _ = Arc::new(());
}

/// The document a gateway composed with no extension that states a licence
/// serves: the derived one, with the Community licence as `info.license`
/// (the builder sets it; the derived document's own is the package
/// manifest's).
fn community_document() -> serde_json::Value {
    let mut document = serde_json::to_value(ApiDoc::openapi()).expect("derived");
    document["info"]["license"] = json!({
        "name": COMMUNITY_API_LICENCE.name,
        "identifier": COMMUNITY_API_LICENCE.identifier,
    });
    document
}
