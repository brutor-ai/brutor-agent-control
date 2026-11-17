// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! `GatewayBuilder::build()` with no extensions
//! is a true Community composition.
//!
//! If the builder installed Enterprise services itself,
//! a gateway built with no extensions would hold the portal's verifier, the
//! response cache's dispatch layer, prompt caching and the knowledge base's
//! enricher, and count as the Enterprise Edition. Each of them is an
//! extension. This is the statement of the result, on
//! the real `build()` path (pools connected, the Enterprise-data check
//! made): nothing Enterprise is recorded, installed, registered, mounted or
//! started.

use sqlx::PgPool;

use brutor_gateway_core::builder::GatewayBuilder;
use brutor_gateway_core::services::edition::Edition;
use brutor_gateway_core::services::tenant_paths::TenantPaths;

use crate::common::connectable_config;
use crate::mounted_routes::{mounted_routes, recorded};

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_gateway_built_with_no_extensions_is_a_community_composition(pool: PgPool) {
    let gateway = GatewayBuilder::new(connectable_config(&pool))
        // An instance lease of this test's own: the suite
        // shares one Redis.
        .instance_lease(crate::common::test_lease())
        .build()
        .await
        .expect("a gateway with no extensions builds and passes the check at start");
    let state = &gateway.state;

    // What it is.
    assert!(gateway.extensions.is_empty());
    assert_eq!(state.composition.edition(), Edition::Community);
    assert_eq!(
        state.composition.enterprise_features(),
        Vec::<&str>::new(),
        "no Enterprise feature is recorded"
    );

    // What it serves: the Community route table, row for row.
    assert_eq!(
        mounted_routes(&gateway.app),
        recorded(crate::common::community_root(), "community"),
        "the routes of a gateway built with no extensions are the Community recording"
    );

    // What is installed on its state: the Community defaults, and nothing
    // in any of the registries an extension fills.
    assert!(
        state.extensions.is_empty(),
        "extension state: {:?}",
        state.extensions.type_names()
    );
    assert_eq!(
        state.credential_verifiers.token_types(),
        ["admin", "gateway"]
    );
    assert_eq!(
        state.credential_verifiers.admin_types(),
        ["tenant"],
        "a Community gateway has tenant administrators only"
    );
    assert_eq!(
        state.tenant_paths.rules(),
        TenantPaths::community().rules(),
        "the tenant layer's rules are the Community Edition's"
    );
    assert!(state.group_cache.loader_names().is_empty());
    assert!(state.cache_layers.layers().is_empty());
    let tagged_handlers: Vec<&str> = state
        .invalidation
        .handlers()
        .iter()
        .filter(|h| h.feature.is_some())
        .map(|h| h.name)
        .collect();
    assert!(
        tagged_handlers.is_empty(),
        "invalidation handlers of an Enterprise feature: {tagged_handlers:?}"
    );

    // What it would start.
    let tagged_tasks: Vec<&str> = gateway
        .tasks
        .iter()
        .filter(|t| t.feature.is_some())
        .map(|t| t.name)
        .collect();
    assert!(
        tagged_tasks.is_empty(),
        "background tasks of an Enterprise feature: {tagged_tasks:?}"
    );
    assert!(!gateway.allows_multiple_instances());
}

/// `GET /v1/version` on `app`: the answer.
async fn version_answer(app: axum::Router) -> serde_json::Value {
    use http_body_util::BodyExt;
    use tower::ServiceExt;
    let response = app
        .oneshot(
            axum::http::Request::builder()
                .uri("/v1/version")
                .body(axum::body::Body::empty())
                .expect("request"),
        )
        .await
        .expect("router");
    assert_eq!(response.status(), axum::http::StatusCode::OK);
    let bytes = response
        .into_body()
        .collect()
        .await
        .expect("body")
        .to_bytes();
    serde_json::from_slice(&bytes).expect("JSON")
}

/// A client can tell which edition it talks to: a
/// gateway composed of no extension answers `community`, without a
/// credential and without a tenant.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_community_gateway_says_it_is_the_community_edition(pool: PgPool) {
    let gateway = crate::common::compose(crate::common::new_state(pool), Vec::new());
    let answer = version_answer(gateway.app).await;
    assert_eq!(answer["edition"], "community", "{answer}");
    assert_eq!(answer["service"], "brutor-gateway-core", "{answer}");
}

/// The answer is what the process is composed of, not a build flag: the
/// same binary, with one Enterprise piece on its state, answers `enterprise`.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_edition_answer_follows_the_composition(pool: PgPool) {
    let mut state = crate::common::new_state(pool);
    state.composition.enterprise_piece("portal");
    let gateway = crate::common::compose(state, Vec::new());
    let answer = version_answer(gateway.app).await;
    assert_eq!(answer["edition"], "enterprise", "{answer}");
}
