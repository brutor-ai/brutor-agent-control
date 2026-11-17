// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The MCP, A2A and skills surfaces resolve a workload-identity agent to the
//! group it acts in.
//!
//! The same gap as model access, in the three resolvers those
//! surfaces share, each of which knew a key's group and an end user's
//! memberships and nothing of an agent's own:
//!
//! * `usage::resolve_all_mcp_server_groups` / `resolve_mcp_server_access`
//!   (the MCP proxy): no group, so every MCP call of a workload agent was
//!   403;
//! * `usage::resolve_target_group` (A2A, skills, the system MCP servers):
//!   the tenant's `Default` group — the agent was moved to a group it is not
//!   a member of, and governed and billed there;
//! * `system_server::resolve_scoping_group_ids` (the Agent Skills server):
//!   "unrestricted" — the agent was offered every skill of the tenant.
//!
//! Now each answers with the agent's acting group, and an agent in no group
//! gets none: not `Default`, not everything.

use axum::body::Body;
use axum::http::{HeaderMap, Request, StatusCode};
use http_body_util::BodyExt;
use serde_json::json;
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::method;
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::models::auth::AuthContext;
use brutor_gateway_core::services::{auth, system_server, usage};

use super::workload_identity as wl;
use crate::common;

const T: &str = "tenant-wos";
const RG: &str = "rg-wos";
const RG_B: &str = "rg-wos-b";
/// The tenant's `Default` group: the agent is not a member of it.
const DEFAULT: &str = "rg-wos-default";
const AGENT: &str = "agent-wos";
const ISSUER: &str = "https://idp.wos.example/";
const SUB: &str = "spiffe://wos/agent";
const KID: &str = "kid-wos";
/// Bound to [`RG`].
const SERVER: &str = "ms-wos";
/// Bound to [`RG_B`].
const SERVER_B: &str = "ms-wos-b";
/// Bound to [`DEFAULT`].
const SERVER_DEFAULT: &str = "ms-wos-default";

struct World {
    mcp: MockServer,
    _jwks: MockServer,
}

async fn seed(pool: &PgPool) -> World {
    let mcp = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "jsonrpc": "2.0", "id": 1,
            "result": {"tools": [{"name": "ok", "description": "ok"}]}
        })))
        .mount(&mcp)
        .await;
    common::seed_tenant(pool, T).await;
    for group in [RG, RG_B, DEFAULT] {
        common::seed_resource_group(pool, group, T).await;
    }
    sqlx::query("UPDATE resource_groups SET name = 'Default' WHERE id = $1")
        .bind(DEFAULT)
        .execute(pool)
        .await
        .expect("the Default group");
    for (server, group) in [(SERVER, RG), (SERVER_B, RG_B), (SERVER_DEFAULT, DEFAULT)] {
        common::seed_mcp_server_with_base_url(pool, server, T, server, &mcp.uri()).await;
        let config = format!("sc-{server}");
        common::seed_server_config(pool, &config, T, server).await;
        common::bind_server_config_to_rg(pool, &config, group).await;
    }
    for (skill, group) in [
        ("sk-wos", RG),
        ("sk-wos-b", RG_B),
        ("sk-wos-default", DEFAULT),
    ] {
        common::seed_agent_skill(pool, skill, T, skill, "# skill", "a skill").await;
        common::bind_skill_to_rg(pool, skill, group).await;
    }
    let jwks = wl::mock_jwks(KID).await;
    wl::seed_idp(pool, "idp-wos", T, ISSUER, &format!("{}/jwks", jwks.uri())).await;
    wl::seed_agent_with_idp(pool, AGENT, T, "idp-wos", SUB).await;
    World { mcp, _jwks: jwks }
}

async fn join(pool: &PgPool, group: &str) {
    sqlx::query(
        "INSERT INTO resource_group_agent_members (tenant_id, agent_id, group_id, joined_at) \
         VALUES ($1, $2, $3, clock_timestamp())",
    )
    .bind(T)
    .bind(AGENT)
    .bind(group)
    .execute(pool)
    .await
    .expect("membership");
}

fn token() -> String {
    let exp = (common::db_now().timestamp() + 3600) as usize;
    wl::mint_token(ISSUER, SUB, KID, exp)
}

/// The context authentication makes of the agent's token.
async fn agent_context(pool: &PgPool, group: Option<&str>) -> AuthContext {
    let mut headers = HeaderMap::new();
    headers.insert(
        "authorization",
        format!("Bearer {}", token()).parse().unwrap(),
    );
    if let Some(group) = group {
        headers.insert("x-resource-group", group.parse().unwrap());
    }
    auth::authenticate_request(
        &headers,
        T,
        common::TEST_JWT_SECRET,
        pool,
        &brutor_gateway_core::services::credential_verifier::CredentialVerifiers::community(
            brutor_gateway_core::services::credential_verifier::RefuseUnregistered,
        ),
        None,
        true,
    )
    .await
    .expect("the workload token authenticates")
}

async fn mcp_groups(pool: &PgPool, ctx: &AuthContext, server: &str) -> Vec<String> {
    usage::resolve_all_mcp_server_groups(pool, ctx, T, server)
        .await
        .expect("mcp access")
        .into_iter()
        .map(|g| g.id)
        .collect()
}

/// MCP server access: the agent's acting group, when it holds the server.
#[sqlx::test(migrations = "./tests/migrations")]
async fn mcp_server_access_is_the_agents_acting_group(pool: PgPool) {
    let _world = seed(&pool).await;
    join(&pool, RG).await;
    join(&pool, RG_B).await;

    let ctx = agent_context(&pool, None).await;
    assert_eq!(mcp_groups(&pool, &ctx, SERVER).await, vec![RG.to_string()]);
    assert_eq!(
        usage::resolve_mcp_server_access(&pool, &ctx, T, SERVER)
            .await
            .expect("access")
            .map(|g| g.id)
            .as_deref(),
        Some(RG)
    );
    // Another of its groups holds this one: not the acting group's.
    assert!(mcp_groups(&pool, &ctx, SERVER_B).await.is_empty());
    // A group it is not in.
    assert!(mcp_groups(&pool, &ctx, SERVER_DEFAULT).await.is_empty());

    // Acting in RG_B.
    let ctx = agent_context(&pool, Some(RG_B)).await;
    assert_eq!(
        mcp_groups(&pool, &ctx, SERVER_B).await,
        vec![RG_B.to_string()]
    );
    assert!(mcp_groups(&pool, &ctx, SERVER).await.is_empty());
}

/// The group an A2A or skill call runs in and is billed to.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_target_group_is_the_agents_acting_group_never_default(pool: PgPool) {
    let _world = seed(&pool).await;

    // In no group: no group. Not the tenant's Default.
    let ctx = agent_context(&pool, None).await;
    assert_eq!(
        usage::resolve_target_group(&pool, &ctx, T)
            .await
            .map(|g| g.id),
        None,
        "an agent in no group was given a group"
    );
    assert!(mcp_groups(&pool, &ctx, SERVER_DEFAULT).await.is_empty());

    join(&pool, RG).await;
    join(&pool, RG_B).await;
    let ctx = agent_context(&pool, None).await;
    assert_eq!(
        usage::resolve_target_group(&pool, &ctx, T)
            .await
            .map(|g| g.id)
            .as_deref(),
        Some(RG)
    );
    let ctx = agent_context(&pool, Some(RG_B)).await;
    assert_eq!(
        usage::resolve_target_group(&pool, &ctx, T)
            .await
            .map(|g| g.id)
            .as_deref(),
        Some(RG_B)
    );
}

/// The Agent Skills server offers the agent its acting group's skills.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_skill_scope_is_the_agents_acting_group_never_everything(pool: PgPool) {
    let _world = seed(&pool).await;

    let ctx = agent_context(&pool, None).await;
    assert_eq!(
        system_server::resolve_scoping_group_ids(&pool, &ctx)
            .await
            .expect("scope"),
        Some(Vec::new()),
        "an agent in no group is offered no skill"
    );

    join(&pool, RG).await;
    let ctx = agent_context(&pool, None).await;
    assert_eq!(
        system_server::resolve_scoping_group_ids(&pool, &ctx)
            .await
            .expect("scope"),
        Some(vec![RG.to_string()])
    );
}

/// End to end on the MCP proxy: the agent lists the tools of a server its
/// group holds, and is refused one its group does not hold.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_mcp_proxy_serves_the_agent_its_groups_server(pool: PgPool) {
    let world = seed(&pool).await;
    join(&pool, RG).await;
    sqlx::query(
        "INSERT INTO agent_grants (id, tenant_id, agent_id, action_type, target, effect, enabled) \
         VALUES ('g-wos', $1, $2, 'mcp_tool', '*', 'allow', true)",
    )
    .bind(T)
    .bind(AGENT)
    .execute(&pool)
    .await
    .expect("grant");

    let ask = |server: &'static str, group: Option<&'static str>| {
        let pool = pool.clone();
        async move {
            let app = common::build_mcp_proxy_app(common::build_test_state(pool));
            let mut req = Request::builder()
                .method("POST")
                .uri(format!("/v1/proxy/mcp/{server}"))
                .header("X-Tenant-ID", T)
                .header("authorization", format!("Bearer {}", token()))
                .header("Content-Type", "application/json")
                .header("Accept", "application/json");
            if let Some(group) = group {
                req = req.header("x-resource-group", group);
            }
            let req = req
                .extension(axum::extract::ConnectInfo::<std::net::SocketAddr>(
                    "127.0.0.1:0".parse().unwrap(),
                ))
                .body(Body::from(
                    json!({"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}).to_string(),
                ))
                .expect("request");
            let resp = app.oneshot(req).await.expect("oneshot");
            let status = resp.status();
            let bytes = resp.into_body().collect().await.expect("body").to_bytes();
            (status, String::from_utf8_lossy(&bytes).into_owned())
        }
    };

    let (status, body) = ask(SERVER, None).await;
    assert_eq!(status, StatusCode::OK, "its group's server: {body}");
    assert!(body.contains("\"ok\""), "{body}");
    let calls = world
        .mcp
        .received_requests()
        .await
        .unwrap_or_default()
        .len();
    assert!(calls >= 1, "the server was not asked");

    let (status, body) = ask(SERVER_DEFAULT, None).await;
    assert_eq!(
        status,
        StatusCode::FORBIDDEN,
        "another group's server: {body}"
    );
    // Naming the group that holds it, of which the agent is not a member.
    let (status, body) = ask(SERVER_DEFAULT, Some(DEFAULT)).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    assert!(body.contains("resource_group_not_member"), "{body}");
    assert_eq!(
        world
            .mcp
            .received_requests()
            .await
            .unwrap_or_default()
            .len(),
        calls,
        "a refused call reached a server"
    );
}

/// An agent identity past its `expires_at` reaches no MCP server:
/// refused at authentication, as a disabled agent is.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_mcp_proxy_refuses_an_agent_identity_past_its_expiry(pool: PgPool) {
    let world = seed(&pool).await;
    join(&pool, RG).await;
    sqlx::query(
        "INSERT INTO agent_grants (id, tenant_id, agent_id, action_type, target, effect, enabled) \
         VALUES ('g-wos-exp', $1, $2, 'mcp_tool', '*', 'allow', true)",
    )
    .bind(T)
    .bind(AGENT)
    .execute(&pool)
    .await
    .expect("grant");
    let ask = || {
        let pool = pool.clone();
        async move {
            let app = common::build_mcp_proxy_app(common::build_test_state(pool));
            let req = Request::builder()
                .method("POST")
                .uri(format!("/v1/proxy/mcp/{SERVER}"))
                .header("X-Tenant-ID", T)
                .header("authorization", format!("Bearer {}", token()))
                .header("Content-Type", "application/json")
                .header("Accept", "application/json")
                .extension(axum::extract::ConnectInfo::<std::net::SocketAddr>(
                    "127.0.0.1:0".parse().unwrap(),
                ))
                .body(Body::from(
                    json!({"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}).to_string(),
                ))
                .expect("request");
            let resp = app.oneshot(req).await.expect("oneshot");
            let status = resp.status();
            let bytes = resp.into_body().collect().await.expect("body").to_bytes();
            (status, String::from_utf8_lossy(&bytes).into_owned())
        }
    };
    let server_calls = || async {
        world
            .mcp
            .received_requests()
            .await
            .unwrap_or_default()
            .len()
    };

    // The control: the agent is served while its identity stands.
    let (status, body) = ask().await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let calls = server_calls().await;
    assert!(calls >= 1, "the server was not asked");

    sqlx::query(
        "UPDATE agent_identities SET expires_at = now() - interval '1 second' WHERE id = $1",
    )
    .bind(AGENT)
    .execute(&pool)
    .await
    .expect("expire");
    let (status, body) = ask().await;
    assert_eq!(status, StatusCode::UNAUTHORIZED, "identity expired: {body}");
    assert_eq!(
        server_calls().await,
        calls,
        "a refused call reached the server"
    );

    // Exactly the disabled agent's refusal.
    sqlx::query("UPDATE agent_identities SET expires_at = NULL, enabled = false WHERE id = $1")
        .bind(AGENT)
        .execute(&pool)
        .await
        .expect("disable");
    let disabled = ask().await;
    assert_eq!((status, body), disabled);
}
