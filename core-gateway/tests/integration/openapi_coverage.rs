// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! What the gateway's OpenAPI document says, route by route.
//!
//! The document (`/api-docs/openapi.json`) is derived from the handlers
//! that carry a `#[utoipa::path]` annotation **and** are listed in
//! `openapi::ApiDoc`, plus the fragment of each extension
//! (`Extension::openapi`). A route that is mounted and in neither is served
//! and not documented, and nothing said so: the document listed 22 paths
//! while the Community gateway served 96 routes.
//!
//! `tests/fixtures/openapi_coverage/community.txt` (and the Enterprise
//! workspace's `enterprise.txt`) states, for every mounted route of the
//! assembled gateway, whether the document it serves documents it. The
//! mounted routes are read from the assembled application
//! (`mounted_routes.rs`) and the document from the URL it is served at. A
//! new route is therefore either documented or, on purpose, recorded as
//! undocumented; and the document names no operation that no route serves.
//!
//! Re-record (say why in the change):
//! `OPENAPI_COVERAGE_RECORD=1 cargo nextest run --features db-tests -E 'test(openapi_coverage)'`

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use axum::Router;
use axum::body::Body;
use axum::http::Request;
use http_body_util::BodyExt;
use serde_json::Value;
use tower::ServiceExt;

use crate::common;
use crate::mounted_routes::{METHODS, mounted_routes, unconnected_pool};

pub(crate) const FIXTURE_DIR: &str = "tests/fixtures/openapi_coverage";

/// A mounted route the document describes under another spelling:
/// `(mounted row, documented operation, why)`.
pub(crate) const DOCUMENTED_AS: &[(&str, &str, &str)] = &[(
    "POST /v1beta/models/{model_action}",
    "POST /v1beta/models/{model}:generateContent",
    "Gemini's `model:action` is one path segment; the route takes it whole and the document \
     spells the action out",
)];

/// The document the application serves.
pub(crate) async fn served_document(app: &Router) -> Value {
    let response = app
        .clone()
        .oneshot(
            Request::get(brutor_gateway_core::openapi::OPENAPI_JSON_PATH)
                .body(Body::empty())
                .expect("request"),
        )
        .await
        .expect("router");
    assert!(response.status().is_success(), "{}", response.status());
    let body = response.into_body().collect().await.expect("body");
    serde_json::from_slice(&body.to_bytes()).expect("the document is JSON")
}

/// `METHOD /path` with every path parameter's name removed: axum's
/// `{*rest}` and OpenAPI's `{rest}` are one parameter, whatever it is
/// called.
fn shape(row: &str) -> String {
    let mut out = String::new();
    let mut in_parameter = false;
    for c in row.chars() {
        match c {
            '{' => {
                in_parameter = true;
                out.push_str("{}");
            }
            '}' => in_parameter = false,
            _ if in_parameter => {}
            _ => out.push(c),
        }
    }
    out
}

/// The operations of the document, as `METHOD /path` rows.
pub(crate) fn documented_operations(document: &Value) -> Vec<String> {
    let mut rows = Vec::new();
    let paths = document["paths"].as_object().expect("paths");
    for (path, item) in paths {
        for method in METHODS {
            if item.get(method).is_some() {
                rows.push(format!("{} {path}", method.to_uppercase()));
            }
        }
    }
    rows
}

/// The statement: one line per mounted route, `documented` or
/// `undocumented`; and the operations of the document that no mounted
/// route serves.
pub(crate) fn coverage(mounted: &[String], document: &Value) -> (String, Vec<String>) {
    let operations = documented_operations(document);
    let operation_shapes: BTreeSet<String> = operations.iter().map(|op| shape(op)).collect();
    // The shapes of every method documented on a path, for an `ANY` route.
    let path_shapes: BTreeSet<String> = operations
        .iter()
        .map(|op| shape(op.split_once(' ').expect("row").1))
        .collect();

    let mut served = BTreeSet::new();
    let mut text = String::new();
    for row in mounted {
        let (method, path) = row.split_once(' ').expect("row");
        let alias = DOCUMENTED_AS.iter().find(|(mounted, _, _)| mounted == row);
        let documented = if let Some((_, operation, _)) = alias {
            served.insert(shape(operation));
            operation_shapes.contains(&shape(operation))
        } else if method == "ANY" {
            let any = path_shapes.contains(&shape(path));
            if any {
                for m in METHODS {
                    served.insert(shape(&format!("{} {path}", m.to_uppercase())));
                }
            }
            any
        } else {
            served.insert(shape(row));
            operation_shapes.contains(&shape(row))
        };
        text.push_str(if documented {
            "documented    "
        } else {
            "undocumented  "
        });
        text.push_str(row);
        text.push('\n');
    }
    let unserved = operations
        .into_iter()
        .filter(|op| !served.contains(&shape(op)))
        .collect();
    (text, unserved)
}

pub(crate) fn fixture_path(root: &Path, name: &str) -> PathBuf {
    root.join(FIXTURE_DIR).join(format!("{name}.txt"))
}

pub(crate) fn recorded(root: &Path, name: &str) -> String {
    let path = fixture_path(root, name);
    std::fs::read_to_string(&path).unwrap_or_else(|e| {
        panic!(
            "{}: {e} (record it with OPENAPI_COVERAGE_RECORD=1)",
            path.display()
        )
    })
}

pub(crate) async fn compare_or_record(root: &Path, name: &str, app: &Router) {
    let mounted = mounted_routes(app);
    let document = served_document(app).await;
    let (text, unserved) = coverage(&mounted, &document);
    assert!(
        unserved.is_empty(),
        "the {name} gateway's document describes operations no mounted route serves:\n{}",
        unserved.join("\n")
    );
    assert!(
        text.contains("documented    GET /health\n") && text.contains("undocumented  GET /docs\n"),
        "the reader tells nothing apart:\n{text}"
    );
    if std::env::var("OPENAPI_COVERAGE_RECORD").is_ok() {
        std::fs::create_dir_all(root.join(FIXTURE_DIR)).expect("fixture directory");
        std::fs::write(fixture_path(root, name), text).expect("write the fixture");
        return;
    }
    let on_disk = recorded(root, name);
    let (was, is): (Vec<&str>, Vec<&str>) = (on_disk.lines().collect(), text.lines().collect());
    let mut diff = Vec::new();
    for line in &was {
        if !is.contains(line) {
            diff.push(format!("- {line}"));
        }
    }
    for line in &is {
        if !was.contains(line) {
            diff.push(format!("+ {line}"));
        }
    }
    assert!(
        diff.is_empty(),
        "what the {name} gateway's OpenAPI document covers differs from \
         {FIXTURE_DIR}/{name}.txt (- recorded, + now). A new route is documented (a \
         `#[utoipa::path]` listed in `openapi::ApiDoc` or in its extension's fragment) or \
         recorded as undocumented, on purpose:\n{}",
        diff.join("\n")
    );
    assert_eq!(on_disk, text, "{name}: same lines, not the same file");
}

/// How many routes of a recording are documented, and how many are not.
pub(crate) fn counts(recording: &str) -> (usize, usize) {
    let documented = recording
        .lines()
        .filter(|l| l.starts_with("documented "))
        .count();
    (documented, recording.lines().count() - documented)
}

#[tokio::test]
async fn openapi_coverage_of_the_community_gateway_is_as_recorded() {
    let app = common::community_app(unconnected_pool());
    compare_or_record(common::community_root(), "community", &app).await;
}

/// The reader, against documents it must tell apart.
#[test]
fn openapi_coverage_reads_methods_parameters_wildcards_and_aliases() {
    let mounted: Vec<String> = [
        "GET /a/{id}",
        "POST /a/{id}",
        "ANY /m/{server_id}/{*path}",
        "ANY /n/{server_id}",
        "POST /v1beta/models/{model_action}",
        "GET /only-mounted",
    ]
    .iter()
    .map(|s| s.to_string())
    .collect();
    let document = serde_json::json!({"paths": {
        "/a/{other_name}": {"get": {}},
        "/m/{server_id}/{path}": {"post": {}},
        "/v1beta/models/{model}:generateContent": {"post": {}},
        "/only-documented": {"get": {}},
        "/a/{id}/more": {"post": {}},
    }});
    let (text, unserved) = coverage(&mounted, &document);
    assert_eq!(
        text,
        "documented    GET /a/{id}\n\
         undocumented  POST /a/{id}\n\
         documented    ANY /m/{server_id}/{*path}\n\
         undocumented  ANY /n/{server_id}\n\
         documented    POST /v1beta/models/{model_action}\n\
         undocumented  GET /only-mounted\n"
    );
    assert_eq!(unserved, ["POST /a/{id}/more", "GET /only-documented"]);
    assert_eq!(counts(&text), (3, 3));
}
