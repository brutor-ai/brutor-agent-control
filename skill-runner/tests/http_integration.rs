// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! HTTP-level integration tests for the runner.
//!
//! Mounts the real axum router in-process via `tower::ServiceExt::oneshot`
//! and exercises the same code path a real client would. No real
//! TCP socket, no docker, no parent process — fast enough to live in
//! `cargo test`.

use std::time::{SystemTime, UNIX_EPOCH};

use axum::body::{Body, to_bytes};
use axum::http::{Request, StatusCode, header};
use brutor_skill_runner::api::{AppState, build_router};
use brutor_skill_runner::config::Config;
use jsonwebtoken::{EncodingKey, Header, encode};
use serde::Serialize;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_SECRET: &str = "test-secret-at-least-32-characters-long-aaa";
const BODY_CAP: usize = 4 * 1024 * 1024;

#[derive(Serialize)]
struct Claims<'a> {
    exec: &'a str,
    tnt: &'a str,
    skl: &'a str,
    exp: u64,
}

fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs()
}

fn mint(execution_id: &str, ttl_secs: u64) -> String {
    let claims = Claims {
        exec: execution_id,
        tnt: "tenant-test",
        skl: "skill-test",
        exp: now() + ttl_secs,
    };
    encode(
        &Header::default(),
        &claims,
        &EncodingKey::from_secret(TEST_SECRET.as_bytes()),
    )
    .unwrap()
}

fn test_state() -> AppState {
    let config = Config {
        listen_addr: "127.0.0.1:0".parse().unwrap(),
        jwt_secret: TEST_SECRET.to_string(),
        sandbox_timeout_secs: 5,
        trusted_timeout_secs: 5,
        workspace_root: std::env::temp_dir().join("brutor-skill-workspaces-test"),
        trusted_env: Default::default(),
    };
    AppState::new(config)
}

/// Build a JSON request body matching the wire shape of `ExecuteRequest`.
/// `ExecuteRequest` is `Deserialize`-only, so we don't try to serialize
/// the typed struct — we hand-build the JSON the runner will decode.
fn echo_body(execution_id: &str, payload: &str) -> Value {
    json!({
        "skill_id": "skill-test",
        "skill_name": "test-skill",
        "skill_version": "1.0.0",
        "skill_description": "test skill",
        "execution_id": execution_id,
        "tenant_id": "tenant-test",
        "skill_md_content": "# test\n\n[run](echo.py)\n",
        "input_params": {"in": payload},
        "scripts": [{
            "filename": "echo.py",
            "language": "PYTHON",
            "execution_mode": "SANDBOX",
            "script_content": format!("print({payload:?})"),
        }],
        "assets": [],
        "resources": []
    })
}

#[tokio::test]
async fn health_endpoint_is_public() {
    let router = build_router(test_state());
    let res = router
        .oneshot(Request::get("/health").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(res.status(), StatusCode::OK);
    let bytes = to_bytes(res.into_body(), BODY_CAP).await.unwrap();
    let body: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(body["status"], "ok");
    assert_eq!(body["service"], "brutor-skill-runner");
}

#[tokio::test]
async fn metrics_endpoint_returns_prometheus_text() {
    let router = build_router(test_state());
    let res = router
        .oneshot(Request::get("/metrics").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(res.status(), StatusCode::OK);
    let content_type = res
        .headers()
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    assert!(
        content_type.contains("text/plain"),
        "metrics should be text/plain; got {content_type:?}"
    );
}

#[tokio::test]
async fn execute_without_bearer_returns_401() {
    let router = build_router(test_state());
    let body = serde_json::to_vec(&echo_body("exec-no-auth", "hello")).unwrap();
    let res = router
        .oneshot(
            Request::post("/v1/runner/execute")
                .header(header::CONTENT_TYPE, "application/json")
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(res.status(), StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn execute_with_wrong_secret_returns_401() {
    let router = build_router(test_state());
    let exec_id = "exec-wrong-sig";
    let body = serde_json::to_vec(&echo_body(exec_id, "x")).unwrap();
    let bad = encode(
        &Header::default(),
        &Claims {
            exec: exec_id,
            tnt: "tenant-test",
            skl: "skill-test",
            exp: now() + 300,
        },
        &EncodingKey::from_secret(b"different-secret-also-32-bytes-long-bbb"),
    )
    .unwrap();
    let res = router
        .oneshot(
            Request::post("/v1/runner/execute")
                .header(header::AUTHORIZATION, format!("Bearer {bad}"))
                .header(header::CONTENT_TYPE, "application/json")
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(res.status(), StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn execute_with_execution_id_mismatch_returns_401() {
    let router = build_router(test_state());
    let body = serde_json::to_vec(&echo_body("exec-real", "x")).unwrap();
    // Token's `exec` references a DIFFERENT execution_id — should
    // be rejected even though the signature is valid.
    let tok = mint("exec-other", 300);
    let res = router
        .oneshot(
            Request::post("/v1/runner/execute")
                .header(header::AUTHORIZATION, format!("Bearer {tok}"))
                .header(header::CONTENT_TYPE, "application/json")
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(res.status(), StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn execute_happy_path_runs_python_script() {
    let router = build_router(test_state());
    let exec_id = "exec-happy-path";
    let body = serde_json::to_vec(&echo_body(exec_id, "hello from python")).unwrap();
    let tok = mint(exec_id, 300);
    let res = router
        .oneshot(
            Request::post("/v1/runner/execute")
                .header(header::AUTHORIZATION, format!("Bearer {tok}"))
                .header(header::CONTENT_TYPE, "application/json")
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(res.status(), StatusCode::OK);
    let bytes = to_bytes(res.into_body(), BODY_CAP).await.unwrap();
    let resp: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(
        resp["success"], true,
        "expected runner success; got body={resp}"
    );
    let script = &resp["script_results"]["echo.py"];
    assert!(
        !script.is_null(),
        "echo.py missing from script_results: {resp}"
    );
    assert_eq!(script["success"], true);
    assert_eq!(script["exit_code"], 0);
    assert_eq!(script["language"], "PYTHON");
    assert_eq!(script["execution_mode"], "SANDBOX");
    let stdout = script["stdout"].as_str().unwrap_or_default();
    assert!(
        stdout.contains("hello from python"),
        "stdout missing payload: {stdout:?}"
    );
}

#[tokio::test]
async fn execute_expired_token_returns_401() {
    let router = build_router(test_state());
    let exec_id = "exec-expired";
    let body = serde_json::to_vec(&echo_body(exec_id, "x")).unwrap();
    // Mint a token with exp 1 hour in the past — well outside the
    // 30s leeway.
    let claims = Claims {
        exec: exec_id,
        tnt: "tenant-test",
        skl: "skill-test",
        exp: now().saturating_sub(3600),
    };
    let tok = encode(
        &Header::default(),
        &claims,
        &EncodingKey::from_secret(TEST_SECRET.as_bytes()),
    )
    .unwrap();
    let res = router
        .oneshot(
            Request::post("/v1/runner/execute")
                .header(header::AUTHORIZATION, format!("Bearer {tok}"))
                .header(header::CONTENT_TYPE, "application/json")
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(res.status(), StatusCode::UNAUTHORIZED);
}

// ============================================================================
// L3 (act) — /v1/runner/run_script (named script + per-session workspace)
// ============================================================================

/// Build a run_script request body (wire shape of `RunScriptRequest`).
fn run_script_body(
    execution_id: &str,
    session_id: &str,
    filename: &str,
    script_content: &str,
    args: Value,
) -> Value {
    json!({
        "skill_id": "skill-test",
        "skill_name": "test-skill",
        "skill_version": "1.0.0",
        "execution_id": execution_id,
        "tenant_id": "tenant-test",
        "session_id": session_id,
        "script": {
            "filename": filename,
            "language": "PYTHON",
            "execution_mode": "SANDBOX",
            "script_content": script_content,
        },
        "args": args,
        "assets": [],
        "resources": []
    })
}

async fn post_run_script(router: axum::Router, exec_id: &str, body: Value) -> Value {
    let tok = mint(exec_id, 300);
    let res = router
        .oneshot(
            Request::post("/v1/runner/run_script")
                .header(header::AUTHORIZATION, format!("Bearer {tok}"))
                .header(header::CONTENT_TYPE, "application/json")
                .body(Body::from(serde_json::to_vec(&body).unwrap()))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(res.status(), StatusCode::OK);
    let bytes = to_bytes(res.into_body(), BODY_CAP).await.unwrap();
    serde_json::from_slice(&bytes).unwrap()
}

#[tokio::test]
async fn run_script_happy_path_passes_args_via_stdin_env() {
    let router = build_router(test_state());
    let script = "import os, json\n\
                  d = json.loads(os.environ.get('SKILL_INPUT','{}'))\n\
                  print('got:', d.get('input_params',{}).get('q'))\n";
    let resp = post_run_script(
        router,
        "exec-l3-happy",
        run_script_body(
            "exec-l3-happy",
            "sess-happy",
            "report.py",
            script,
            json!({"q": "hello-l3"}),
        ),
    )
    .await;
    assert_eq!(resp["success"], true, "body={resp}");
    assert_eq!(resp["exit_code"], 0);
    assert!(
        resp["output"]
            .as_str()
            .unwrap_or_default()
            .contains("got: hello-l3"),
        "output missing arg: {resp}"
    );
}

#[tokio::test]
async fn run_script_workspace_persists_files_across_runs_in_a_session() {
    // Call A writes a file into the session workspace (cwd); a SECOND call with
    // the SAME session_id must see it — that is what the session workspace
    // is for.
    let session = "sess-persist";

    let write = "open('state.txt','w').write('persisted-value')\nprint('wrote')\n";
    let a = post_run_script(
        build_router(test_state()),
        "exec-l3-write",
        run_script_body("exec-l3-write", session, "write.py", write, json!({})),
    )
    .await;
    assert_eq!(a["success"], true, "write failed: {a}");
    assert!(
        a["workspace_files"]
            .as_array()
            .unwrap()
            .iter()
            .any(|f| f == "state.txt"),
        "state.txt not reported in workspace_files: {a}"
    );

    let read = "print(open('state.txt').read())\n";
    let b = post_run_script(
        build_router(test_state()),
        "exec-l3-read",
        run_script_body("exec-l3-read", session, "read.py", read, json!({})),
    )
    .await;
    assert_eq!(b["success"], true, "read failed: {b}");
    assert!(
        b["output"]
            .as_str()
            .unwrap_or_default()
            .contains("persisted-value"),
        "second run did not see the file the first wrote: {b}"
    );
}

#[tokio::test]
async fn run_script_isolated_sessions_do_not_share_files() {
    // A file written in one session must NOT be visible in another.
    let write = "open('secret.txt','w').write('s')\nprint('ok')\n";
    let _ = post_run_script(
        build_router(test_state()),
        "exec-iso-a",
        run_script_body("exec-iso-a", "sess-iso-A", "w.py", write, json!({})),
    )
    .await;

    let read = "import os\nprint('exists' if os.path.exists('secret.txt') else 'absent')\n";
    let b = post_run_script(
        build_router(test_state()),
        "exec-iso-b",
        run_script_body("exec-iso-b", "sess-iso-B", "r.py", read, json!({})),
    )
    .await;
    assert!(
        b["output"].as_str().unwrap_or_default().contains("absent"),
        "cross-session leak: {b}"
    );
}

#[tokio::test]
async fn run_script_without_bearer_returns_401() {
    let router = build_router(test_state());
    let body = run_script_body("exec-l3-noauth", "s", "x.py", "print(1)", json!({}));
    let res = router
        .oneshot(
            Request::post("/v1/runner/run_script")
                .header(header::CONTENT_TYPE, "application/json")
                .body(Body::from(serde_json::to_vec(&body).unwrap()))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(res.status(), StatusCode::UNAUTHORIZED);
}

// ============================================================================
// Metrics — the duration histogram is in the unit its name states
// ============================================================================

/// The value of one sample line in the Prometheus text, e.g.
/// `skill_runner_execution_duration_ms_sum{outcome="run_script"}`.
fn sample(metrics: &str, series: &str) -> f64 {
    metrics
        .lines()
        .find_map(|l| l.strip_prefix(series))
        .unwrap_or_else(|| panic!("no series {series} in:\n{metrics}"))
        .trim()
        .parse()
        .unwrap()
}

#[tokio::test]
async fn the_duration_histogram_records_milliseconds() {
    let state = test_state();
    let router = build_router(state.clone());
    // A script that takes at least 200 ms.
    let resp = post_run_script(
        router,
        "exec-metrics-ms",
        run_script_body(
            "exec-metrics-ms",
            "sess-metrics-ms",
            "slow.py",
            "import time\ntime.sleep(0.2)\nprint('done')\n",
            json!({}),
        ),
    )
    .await;
    assert_eq!(resp["success"], true, "{resp}");

    let (_, text) = state.metrics.encode_text();
    let sum = sample(
        &text,
        "skill_runner_execution_duration_ms_sum{outcome=\"run_script\"}",
    );
    assert!(
        sum >= 200.0,
        "a 200 ms run is recorded as {sum} in a histogram named _ms"
    );
    let within_10_ms = sample(
        &text,
        "skill_runner_execution_duration_ms_bucket{outcome=\"run_script\",le=\"10\"}",
    );
    assert_eq!(within_10_ms, 0.0, "a 200 ms run is not in the 10 ms bucket");
}
