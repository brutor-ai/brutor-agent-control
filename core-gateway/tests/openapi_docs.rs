// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The API document and Swagger UI routes (`openapi::docs_router`).
//!
//! utoipa-swagger-ui 10 is built on utoipa 6 while the routes derive a
//! utoipa 5 document, so the document reaches Swagger UI as JSON. These pin
//! that the URL is unchanged and that what it serves is exactly the derived
//! document — nothing lost or rewritten in the hand-off.

use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use tower::ServiceExt;
use utoipa::OpenApi;

use brutor_gateway_core::openapi::{
    ApiDoc, OPENAPI_JSON_PATH, SWAGGER_UI_PATH, docs_router, document,
};

async fn get(path: &str) -> (StatusCode, Vec<u8>) {
    let app: axum::Router = docs_router(document().expect("document"));
    let resp = app
        .oneshot(Request::get(path).body(Body::empty()).unwrap())
        .await
        .unwrap();
    let status = resp.status();
    let body = resp
        .into_body()
        .collect()
        .await
        .unwrap()
        .to_bytes()
        .to_vec();
    (status, body)
}

#[test]
fn the_urls_are_the_published_ones() {
    assert_eq!(OPENAPI_JSON_PATH, "/api-docs/openapi.json");
    assert_eq!(SWAGGER_UI_PATH, "/docs");
}

#[tokio::test]
async fn the_document_served_is_the_derived_one() {
    let (status, body) = get(OPENAPI_JSON_PATH).await;
    assert_eq!(status, StatusCode::OK);
    let served: serde_json::Value = serde_json::from_slice(&body).expect("JSON document");
    let derived = serde_json::to_value(ApiDoc::openapi()).unwrap();
    assert_eq!(served, derived);
    assert!(served["paths"].as_object().is_some_and(|p| !p.is_empty()));
}

#[tokio::test]
async fn swagger_ui_is_served_and_points_at_the_document() {
    let (status, body) = get(&format!("{SWAGGER_UI_PATH}/")).await;
    assert_eq!(status, StatusCode::OK);
    assert!(String::from_utf8_lossy(&body).contains("swagger"));

    let (status, body) = get(&format!("{SWAGGER_UI_PATH}/swagger-initializer.js")).await;
    assert_eq!(status, StatusCode::OK);
    assert!(String::from_utf8_lossy(&body).contains(OPENAPI_JSON_PATH));
}
