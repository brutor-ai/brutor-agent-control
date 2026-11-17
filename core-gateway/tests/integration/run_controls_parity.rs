// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Run-scoped controls are uniform: every governed surface refuses an aborted
//! run and an over-budget run, for agent and non-agent callers alike.
//!
//! The run budget and an operator's abort were once wired
//! surface by surface, each next to that surface's autonomy gate — which
//! lives in the NON-agent branch, because for an agent principal autonomy
//! runs inside `decide_for_agent`. So an agent-keyed call skipped the run
//! controls on MCP, the LLM path (abort) and A2A v1 (abort
//! and budget); `/v1/responses` ran neither for anyone; skills keyed the
//! abort on the correlation id and had no budget at all; A2A inbound had
//! neither. Every surface now asks `run_controls::check` once, before its
//! authorization planes.
//!
//! The matrix: surface × {API key, agent key} × {aborted, over budget}. The
//! agent is default-deny with NO grants, so if a surface skipped the run
//! controls for it the call would still be refused — by agent authz. That is
//! why every case asserts the refusal's audit subtype, not just a 4xx.
//!
//! Embeddings and the media routes (images, speech, transcription, video)
//! were outside the run controls altogether: they attributed their refusals
//! to the run but never asked whether it could take another step, so an
//! aborted or capped run kept embedding and generating. The media routes
//! serve an API key as well as a portal user, so
//! they are in the matrix for all three callers.

use axum::body::Body;
use axum::http::Request;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::method;
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::middleware;
use brutor_gateway_core::routes;
use brutor_gateway_core::services::SharedState;
use brutor_gateway_core::services::call_chain::{asserted_root, derive_tenant_key};

use crate::common;

pub(crate) const T: &str = "t-rcpar";
pub(crate) const RUN: &str = "run-parity-1";
pub(crate) const CARD: &str = "card-rcpar";
const CARD_NAME: &str = "rcpar-agent";
const SKILL: &str = "sk-rcpar";

#[derive(Clone, Copy, Debug)]
pub(crate) enum Surface {
    LlmChat,
    LlmResponses,
    LlmMessages,
    Mcp,
    A2aOutbound,
    A2aInbound,
    A2aV1,
    Skill,
    Embeddings,
    Images,
    Speech,
    Transcription,
    Video,
    /// The Agent Skills system server's tools. Only
    /// `skills__run_script` reached `execute_skill`, and so the run controls;
    /// listing, loading, reading a resource and rendering a template did not.
    SkillServerList,
    SkillServerLoad,
    SkillServerReadResource,
    SkillServerRenderTemplate,
    SkillServerRunScript,
    /// The Web Search system server: its `tools/call` asked
    /// no run control at all.
    SearchServerWeb,
}

impl Surface {
    /// The system server and tool a system-server surface calls. The run
    /// controls are asked once, in the MCP route, for every `tools/call` of
    /// every system server, and answer as they do for an external MCP
    /// server: a 4xx, before any tool does anything.
    pub(crate) fn system_server_tool(self) -> Option<(&'static str, &'static str)> {
        use brutor_gateway_core::constants::system_server::agent_skill as ask;
        use brutor_gateway_core::constants::system_server::search;
        match self {
            Surface::SkillServerList => Some((SKILL_SERVER, ask::TOOL_LIST)),
            Surface::SkillServerLoad => Some((SKILL_SERVER, ask::TOOL_LOAD)),
            Surface::SkillServerReadResource => Some((SKILL_SERVER, ask::TOOL_READ_RESOURCE)),
            Surface::SkillServerRenderTemplate => Some((SKILL_SERVER, ask::TOOL_RENDER_TEMPLATE)),
            Surface::SkillServerRunScript => Some((SKILL_SERVER, ask::TOOL_RUN_SCRIPT)),
            Surface::SearchServerWeb => Some((SEARCH_SERVER, search::TOOL_WEB)),
            _ => None,
        }
    }
}

const SKILL_SERVER: &str = "system-agent-skill-server-t-rcpar";
const SEARCH_SERVER: &str = "system-search-server-t-rcpar";

#[derive(Clone, Copy, Debug)]
pub(crate) enum Caller {
    ApiKey,
    Agent,
    /// A portal user's session (the media routes serve one where the Portal
    /// is composed).
    PortalUser,
}

#[derive(Clone, Copy, Debug)]
enum Stop {
    Aborted,
    OverBudget,
}

impl Stop {
    fn subtype(self) -> &'static str {
        match self {
            Stop::Aborted => "run_aborted",
            Stop::OverBudget => "run_cap_reached",
        }
    }
}

/// One of everything the matrix calls, bound to one group, behind one key.
pub(crate) async fn topology(pool: &PgPool, upstream: &str, caller: Caller, rg: &str) -> String {
    common::seed_tenant(pool, T).await;
    common::seed_resource_group(pool, rg, T).await;
    common::seed_end_user(pool, 9901, T).await;
    common::add_direct_membership(pool, 9901, rg, T).await;

    common::seed_llm_provider(pool, "prov-rcpar", T, "openai").await;
    common::seed_llm_model_with_api_url(pool, "m-rcpar", T, "prov-rcpar", "test-gpt", upstream)
        .await;
    common::bind_llm_model_to_rg(pool, "m-rcpar", rg).await;

    common::seed_mcp_server_with_base_url(pool, "ms-rcpar", T, "rcparsrv", upstream).await;
    common::seed_server_config(pool, "sc-rcpar", T, "ms-rcpar").await;
    common::bind_server_config_to_rg(pool, "sc-rcpar", rg).await;

    common::seed_agent_card(pool, CARD, T, CARD_NAME).await;
    common::bind_agent_card_to_rg(pool, CARD, rg).await;
    sqlx::query(
        "INSERT INTO agent_card_capabilities (id, agent_card_id, capability_name, handler_type, enabled) \
         VALUES ('cap-rcpar', $1, 'echo', 'noop', true)",
    )
    .bind(CARD)
    .execute(pool)
    .await
    .expect("seed capability");

    common::seed_agent_skill(
        pool,
        SKILL,
        T,
        "rcpar-skill",
        "# Skill\nDoes a thing.",
        "A skill",
    )
    .await;
    common::bind_skill_to_rg(pool, SKILL, rg).await;

    common::seed_mcp_server_with_base_url(
        pool,
        SKILL_SERVER,
        T,
        "skills",
        brutor_gateway_core::constants::system_server::agent_skill::BASE_URL,
    )
    .await;
    common::seed_server_config(pool, "sc-rcpar-skills", T, SKILL_SERVER).await;
    common::bind_server_config_to_rg(pool, "sc-rcpar-skills", rg).await;

    common::seed_mcp_server_with_base_url(
        pool,
        SEARCH_SERVER,
        T,
        "websearch",
        brutor_gateway_core::constants::system_server::search::BASE_URL,
    )
    .await;
    common::seed_server_config(pool, "sc-rcpar-search", T, SEARCH_SERVER).await;
    common::bind_server_config_to_rg(pool, "sc-rcpar-search", rg).await;

    let key = common::seed_api_key(pool, T, rg, Some(9901), "rcpar-key").await;
    if let Caller::PortalUser = caller {
        return common::make_portal_jwt(9901, "u-rcpar", T);
    }
    if let Caller::Agent = caller {
        // Default-deny, no grants: a surface that skipped the run controls
        // for an agent would refuse it anyway — as `agent_not_authorized`.
        sqlx::query(
            "INSERT INTO agent_identities (id, tenant_id, name, source, enabled, status, default_deny) \
             VALUES ('agent-rcpar', $1, 'agent-rcpar', 'manual', true, 'active', true)",
        )
        .bind(T)
        .execute(pool)
        .await
        .expect("agent identity");
        sqlx::query("UPDATE api_keys SET agent_id = 'agent-rcpar' WHERE tenant_id = $1")
            .bind(T)
            .execute(pool)
            .await
            .expect("bind key to agent");
    }
    key
}

/// The `root_task_id` the gateway derives for this key's asserted run — the
/// id an operator aborts and the run counters are keyed on.
pub(crate) fn run_root(state: &SharedState, key: &str) -> String {
    run_root_of(state, key, RUN)
}

/// [`run_root`] for the run the caller asserts as `run`.
pub(crate) fn run_root_of(state: &SharedState, key: &str, run: &str) -> String {
    let tenant_key = derive_tenant_key(state.config.encryption_key.as_bytes(), T);
    let auth = format!("Bearer {key}");
    let get = |n: &str| match n {
        "authorization" => Some(auth.clone()),
        "x-brutor-run-id" => Some(run.to_string()),
        _ => None,
    };
    asserted_root(&get, &tenant_key)
        .expect("an authenticated caller is entitled to assert its run")
        .to_string()
}

async fn stop_the_run(pool: &PgPool, state: &SharedState, root: &str, stop: Stop, rg: &str) {
    match stop {
        Stop::Aborted => {
            sqlx::query(
                "INSERT INTO run_interventions \
                 (id, tenant_id, root_task_id, action, reason, status, requested_at, \
                  steps_blocked, created_at, updated_at) \
                 VALUES ('ri-rcpar', $1, $2, 'abort', 'runaway', 'requested', now(), 0, now(), now())",
            )
            .bind(T)
            .bind(root)
            .execute(pool)
            .await
            .expect("abort the run");
        }
        Stop::OverBudget => {
            common::set_rg_operating_envelope(pool, rg, json!({"max_llm_calls_per_run": 1})).await;
            // The run has already made its one permitted call.
            state.usage_state.run_commit(T, root, 10, 0.0).await;
        }
    }
}

pub(crate) fn app(state: &SharedState, surface: Surface) -> axum::Router {
    match surface {
        Surface::LlmChat | Surface::LlmResponses => common::build_llm_app(state.clone()),
        Surface::LlmMessages => common::build_anthropic_app(state.clone()),
        Surface::Mcp
        | Surface::SkillServerList
        | Surface::SkillServerLoad
        | Surface::SkillServerReadResource
        | Surface::SkillServerRenderTemplate
        | Surface::SkillServerRunScript
        | Surface::SearchServerWeb => common::build_mcp_proxy_app(state.clone()),
        Surface::A2aV1 => common::build_a2a_v1_app(state.clone()),
        Surface::Skill
        | Surface::Images
        | Surface::Speech
        | Surface::Transcription
        | Surface::Video => common::build_multimodal_app(state.clone()),
        Surface::Embeddings => common::build_llm_app(state.clone()),
        Surface::A2aOutbound | Surface::A2aInbound => axum::Router::new()
            .nest("/v1/proxy/a2a", routes::a2a::router())
            .layer(middleware::tenant::TenantLayer::new(state.clone()))
            .layer(middleware::run_end::RunEndLayer::new(
                state.mcp_sessions.clone(),
            ))
            .with_state(state.clone()),
    }
}

pub(crate) fn request(surface: Surface, key: &str) -> Request<Body> {
    let (path, body): (String, Value) = match surface {
        Surface::LlmChat => (
            "/v1/proxy/llm/chat/completions".into(),
            json!({"model_id": "m-rcpar", "messages": [{"role": "user", "content": "hi"}]}),
        ),
        Surface::LlmResponses => (
            "/v1/proxy/llm/responses".into(),
            json!({"model_id": "m-rcpar", "input": "hi"}),
        ),
        Surface::LlmMessages => (
            "/v1/messages".into(),
            json!({"model": "m-rcpar", "max_tokens": 16,
                   "messages": [{"role": "user", "content": "hi"}]}),
        ),
        Surface::Mcp => (
            "/v1/proxy/mcp/ms-rcpar".into(),
            json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "do_thing", "arguments": {}}}),
        ),
        Surface::A2aOutbound => (
            "/v1/proxy/a2a/outbound".into(),
            json!({"target_card_id": CARD, "capability": "echo", "message": {"text": "hi"}}),
        ),
        Surface::A2aInbound => (
            "/v1/proxy/a2a/inbound".into(),
            json!({"target_card_id": CARD, "capability": "echo", "message": {"text": "hi"}}),
        ),
        Surface::A2aV1 => (
            format!("/a2a/{T}/{CARD_NAME}/message:send"),
            json!({"message": {"messageId": "01rcpar", "role": "ROLE_USER",
                               "parts": [{"text": "hi"}]}}),
        ),
        Surface::Skill => (
            "/v1/proxy/skills/execute".into(),
            json!({"skill_id": SKILL, "input_params": {"period": "Q2"}}),
        ),
        Surface::Embeddings => (
            "/v1/proxy/llm/embeddings".into(),
            json!({"model_id": "m-rcpar", "input": "hi"}),
        ),
        Surface::Images => (
            "/v1/proxy/llm/images/generations".into(),
            json!({"model": "test-gpt", "prompt": "a lighthouse"}),
        ),
        Surface::Speech => (
            "/v1/proxy/llm/audio/speech".into(),
            json!({"model": "test-gpt", "voice": "alloy", "input": "hi"}),
        ),
        Surface::Video => (
            "/v1/proxy/llm/video/generations".into(),
            json!({"model": "test-gpt", "prompt": "a lighthouse"}),
        ),
        Surface::SkillServerList
        | Surface::SkillServerLoad
        | Surface::SkillServerReadResource
        | Surface::SkillServerRenderTemplate
        | Surface::SkillServerRunScript
        | Surface::SearchServerWeb => {
            let (server, tool) = surface.system_server_tool().expect("a system server");
            (
                format!("/v1/proxy/mcp/{server}"),
                json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {
                    "name": tool,
                    "arguments": {"skill_name": "rcpar-skill", "path": "notes.md",
                                  "template": "t.md", "script": "run.py",
                                  "query": "lighthouses"},
                }}),
            )
        }
        Surface::Transcription => {
            // multipart/form-data: a 1-byte `file` part + the `model` part.
            let boundary = "----rcparboundary";
            let body = format!(
                "--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.mp3\"\r\n\
                 Content-Type: application/octet-stream\r\n\r\n\x00\r\n\
                 --{b}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\ntest-gpt\r\n\
                 --{b}--\r\n",
                b = boundary
            );
            let mut req = Request::builder()
                .method("POST")
                .uri("/v1/proxy/llm/audio/transcriptions")
                .header("X-Tenant-ID", T)
                .header("authorization", format!("Bearer {key}"))
                .header(
                    "content-type",
                    format!("multipart/form-data; boundary={boundary}"),
                )
                .header("x-brutor-run-id", RUN)
                .body(Body::from(body))
                .expect("multipart request");
            req.extensions_mut().insert(axum::extract::ConnectInfo(
                "127.0.0.1:55555"
                    .parse::<std::net::SocketAddr>()
                    .expect("peer"),
            ));
            return req;
        }
    };
    let mut req = common::build_post_request(&path, body, Some(key), Some(T));
    req.headers_mut()
        .insert("x-brutor-run-id", RUN.parse().expect("run header"));
    req
}

/// Poll for the refusal's audit row — the writer is batched.
pub(crate) async fn refusal_row(pool: &PgPool, subtype: &str) -> Option<Option<String>> {
    for _ in 0..60 {
        let row: Option<(Option<String>,)> = sqlx::query_as(
            "SELECT root_task_id FROM proxy_logs WHERE tenant_id = $1 AND proxy_subtype = $2 \
             ORDER BY timestamp DESC LIMIT 1",
        )
        .bind(T)
        .bind(subtype)
        .fetch_optional(pool)
        .await
        .expect("proxy_logs query");
        if let Some((root,)) = row {
            return Some(root);
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    None
}

async fn is_refused(pool: PgPool, surface: Surface, caller: Caller, stop: Stop) {
    let upstream = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "chatcmpl-x", "object": "chat.completion", "model": "test-gpt",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        })))
        .mount(&upstream)
        .await;
    // Unique per case: run caps are cached process-wide by group id, so two
    // parallel cases sharing a group would read each other's envelope.
    let rg = format!("rg-rcpar-{surface:?}-{caller:?}-{stop:?}").to_lowercase();
    let key = topology(&pool, &upstream.uri(), caller, &rg).await;
    let state = common::build_test_state(pool.clone());
    let root = run_root(&state, &key);
    stop_the_run(&pool, &state, &root, stop, &rg).await;

    let resp = app(&state, surface)
        .oneshot(request(surface, &key))
        .await
        .expect("router call");
    let status = resp.status();
    // A portal session is a credential only where the Portal extension is
    // composed. A Community gateway refuses it as it refuses any token it
    // cannot verify, before a run exists: the run controls are not skipped.
    if matches!(caller, Caller::PortalUser) && !common::edition_composes("portal") {
        assert_eq!(
            status,
            axum::http::StatusCode::UNAUTHORIZED,
            "{surface:?} × {caller:?} × {stop:?}: a portal session is no credential here"
        );
        assert_eq!(upstream.received_requests().await.map_or(0, |r| r.len()), 0);
        return;
    }
    assert!(
        status.is_client_error(),
        "{surface:?} × {caller:?} × {stop:?}: a stopped run must be refused, got {status}"
    );
    if surface.system_server_tool().is_some() {
        // Nothing of the skill (or of a search) in the refusal.
        use http_body_util::BodyExt;
        let bytes = resp.into_body().collect().await.expect("body").to_bytes();
        let body = String::from_utf8_lossy(&bytes).to_string();
        assert!(
            !body.contains("Does a thing") && !body.contains("\"result\""),
            "{surface:?} × {caller:?} × {stop:?}: a tool answered a stopped run: {body}"
        );
    }
    let row = refusal_row(&pool, stop.subtype()).await;
    assert!(
        row.is_some(),
        "{surface:?} × {caller:?} × {stop:?}: refused with {status}, but not by the run \
         controls — no `{}` row. For an agent caller that means this surface still skips \
         the run controls and something else (agent authz) refused it.",
        stop.subtype(),
    );
    // A2A v1 refusals before a task exists carry the asserted run; every
    // other surface stamps the run on the refusal row too.
    assert_eq!(
        row.flatten().as_deref(),
        Some(root.as_str()),
        "{surface:?} × {caller:?} × {stop:?}: the refusal must land on the run it stopped"
    );
    if let Stop::Aborted = stop {
        let (blocked,): (i32,) =
            sqlx::query_as("SELECT steps_blocked FROM run_interventions WHERE id = 'ri-rcpar'")
                .fetch_one(&pool)
                .await
                .expect("intervention");
        assert!(
            blocked >= 1,
            "{surface:?} × {caller:?}: the intervention must record that it bit"
        );
    }
    // Nothing was sent upstream for a refused step.
    assert_eq!(
        upstream.received_requests().await.map_or(0, |r| r.len()),
        0,
        "{surface:?} × {caller:?} × {stop:?}: a refused step must not reach the upstream"
    );
}

macro_rules! matrix {
    ($($name:ident => ($s:ident, $c:ident, $x:ident)),* $(,)?) => {
        $(
            #[sqlx::test(migrations = "./tests/migrations")]
            async fn $name(pool: PgPool) {
                is_refused(pool, Surface::$s, Caller::$c, Stop::$x).await;
            }
        )*
    };
}

matrix! {
    llm_chat_api_key_aborted => (LlmChat, ApiKey, Aborted),
    llm_chat_api_key_over_budget => (LlmChat, ApiKey, OverBudget),
    llm_chat_agent_aborted => (LlmChat, Agent, Aborted),
    llm_chat_agent_over_budget => (LlmChat, Agent, OverBudget),
    llm_responses_api_key_aborted => (LlmResponses, ApiKey, Aborted),
    llm_responses_api_key_over_budget => (LlmResponses, ApiKey, OverBudget),
    llm_responses_agent_aborted => (LlmResponses, Agent, Aborted),
    llm_responses_agent_over_budget => (LlmResponses, Agent, OverBudget),
    llm_messages_api_key_aborted => (LlmMessages, ApiKey, Aborted),
    llm_messages_api_key_over_budget => (LlmMessages, ApiKey, OverBudget),
    llm_messages_agent_aborted => (LlmMessages, Agent, Aborted),
    llm_messages_agent_over_budget => (LlmMessages, Agent, OverBudget),
    mcp_api_key_aborted => (Mcp, ApiKey, Aborted),
    mcp_api_key_over_budget => (Mcp, ApiKey, OverBudget),
    mcp_agent_aborted => (Mcp, Agent, Aborted),
    mcp_agent_over_budget => (Mcp, Agent, OverBudget),
    a2a_outbound_api_key_aborted => (A2aOutbound, ApiKey, Aborted),
    a2a_outbound_api_key_over_budget => (A2aOutbound, ApiKey, OverBudget),
    a2a_outbound_agent_aborted => (A2aOutbound, Agent, Aborted),
    a2a_outbound_agent_over_budget => (A2aOutbound, Agent, OverBudget),
    a2a_inbound_api_key_aborted => (A2aInbound, ApiKey, Aborted),
    a2a_inbound_api_key_over_budget => (A2aInbound, ApiKey, OverBudget),
    a2a_inbound_agent_aborted => (A2aInbound, Agent, Aborted),
    a2a_inbound_agent_over_budget => (A2aInbound, Agent, OverBudget),
    a2a_v1_api_key_aborted => (A2aV1, ApiKey, Aborted),
    a2a_v1_api_key_over_budget => (A2aV1, ApiKey, OverBudget),
    a2a_v1_agent_aborted => (A2aV1, Agent, Aborted),
    a2a_v1_agent_over_budget => (A2aV1, Agent, OverBudget),
    skill_api_key_aborted => (Skill, ApiKey, Aborted),
    skill_api_key_over_budget => (Skill, ApiKey, OverBudget),
    skill_agent_aborted => (Skill, Agent, Aborted),
    skill_agent_over_budget => (Skill, Agent, OverBudget),
    embeddings_api_key_aborted => (Embeddings, ApiKey, Aborted),
    embeddings_api_key_over_budget => (Embeddings, ApiKey, OverBudget),
    embeddings_agent_aborted => (Embeddings, Agent, Aborted),
    embeddings_agent_over_budget => (Embeddings, Agent, OverBudget),
    images_portal_user_aborted => (Images, PortalUser, Aborted),
    images_portal_user_over_budget => (Images, PortalUser, OverBudget),
    speech_portal_user_aborted => (Speech, PortalUser, Aborted),
    speech_portal_user_over_budget => (Speech, PortalUser, OverBudget),
    transcription_portal_user_aborted => (Transcription, PortalUser, Aborted),
    transcription_portal_user_over_budget => (Transcription, PortalUser, OverBudget),
    video_portal_user_aborted => (Video, PortalUser, Aborted),
    video_portal_user_over_budget => (Video, PortalUser, OverBudget),
    // The media routes serve an API key. A key, and an
    // agent's key, are held to the run controls there as everywhere.
    images_api_key_aborted => (Images, ApiKey, Aborted),
    images_api_key_over_budget => (Images, ApiKey, OverBudget),
    images_agent_aborted => (Images, Agent, Aborted),
    images_agent_over_budget => (Images, Agent, OverBudget),
    speech_api_key_aborted => (Speech, ApiKey, Aborted),
    speech_api_key_over_budget => (Speech, ApiKey, OverBudget),
    speech_agent_aborted => (Speech, Agent, Aborted),
    speech_agent_over_budget => (Speech, Agent, OverBudget),
    transcription_api_key_aborted => (Transcription, ApiKey, Aborted),
    transcription_api_key_over_budget => (Transcription, ApiKey, OverBudget),
    transcription_agent_aborted => (Transcription, Agent, Aborted),
    transcription_agent_over_budget => (Transcription, Agent, OverBudget),
    video_api_key_aborted => (Video, ApiKey, Aborted),
    video_api_key_over_budget => (Video, ApiKey, OverBudget),
    video_agent_aborted => (Video, Agent, Aborted),
    video_agent_over_budget => (Video, Agent, OverBudget),
    // Every tool of the Agent Skills system server.
    skill_server_list_api_key_aborted => (SkillServerList, ApiKey, Aborted),
    skill_server_list_api_key_over_budget => (SkillServerList, ApiKey, OverBudget),
    skill_server_list_agent_aborted => (SkillServerList, Agent, Aborted),
    skill_server_list_agent_over_budget => (SkillServerList, Agent, OverBudget),
    skill_server_load_api_key_aborted => (SkillServerLoad, ApiKey, Aborted),
    skill_server_load_api_key_over_budget => (SkillServerLoad, ApiKey, OverBudget),
    skill_server_load_agent_aborted => (SkillServerLoad, Agent, Aborted),
    skill_server_load_agent_over_budget => (SkillServerLoad, Agent, OverBudget),
    skill_server_read_resource_api_key_aborted => (SkillServerReadResource, ApiKey, Aborted),
    skill_server_read_resource_api_key_over_budget => (SkillServerReadResource, ApiKey, OverBudget),
    skill_server_read_resource_agent_aborted => (SkillServerReadResource, Agent, Aborted),
    skill_server_read_resource_agent_over_budget => (SkillServerReadResource, Agent, OverBudget),
    skill_server_render_template_api_key_aborted => (SkillServerRenderTemplate, ApiKey, Aborted),
    skill_server_render_template_api_key_over_budget => (SkillServerRenderTemplate, ApiKey, OverBudget),
    skill_server_render_template_agent_aborted => (SkillServerRenderTemplate, Agent, Aborted),
    skill_server_render_template_agent_over_budget => (SkillServerRenderTemplate, Agent, OverBudget),
    skill_server_run_script_api_key_aborted => (SkillServerRunScript, ApiKey, Aborted),
    skill_server_run_script_api_key_over_budget => (SkillServerRunScript, ApiKey, OverBudget),
    skill_server_run_script_agent_aborted => (SkillServerRunScript, Agent, Aborted),
    skill_server_run_script_agent_over_budget => (SkillServerRunScript, Agent, OverBudget),
    // The Web Search system server.
    search_server_web_api_key_aborted => (SearchServerWeb, ApiKey, Aborted),
    search_server_web_api_key_over_budget => (SearchServerWeb, ApiKey, OverBudget),
    search_server_web_agent_aborted => (SearchServerWeb, Agent, Aborted),
    search_server_web_agent_over_budget => (SearchServerWeb, Agent, OverBudget),
}

/// The run controls come before the argument policy on the
/// system-server branch, as on every other surface: a run that is aborted or
/// over its ceiling must not even queue an approval. The band's hold
/// sat in the MCP route and the run controls inside the system
/// server, behind it, so a stopped run's banded call raised a hold for a
/// person to decide on a step that could never be taken.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_stopped_run_queues_no_approval_on_a_system_server_tool(pool: PgPool) {
    use brutor_gateway_core::constants::system_server::agent_skill as ask;
    let upstream = MockServer::start().await;
    let rg = "rg-rcpar-band-before-run";
    let key = topology(&pool, &upstream.uri(), Caller::ApiKey, rg).await;
    let rules = json!([
        {"deny_if": "field_gt", "field": "args.amount", "value": 1000,
         "severity": "approval_required", "message": "Over 1,000 needs a person"}
    ]);
    sqlx::query(
        "INSERT INTO argument_policies \
         (id, tenant_id, name, surface, target, argument_key, analyzer, \
          analyzer_config, rules, enabled, severity, created_at, updated_at) \
         VALUES ('ap-rcpar-band', $1, 'rcpar-band', 'mcp_input', $2, '*', 'json', NULL, \
                 $3::jsonb, true, 'warn', now(), now())",
    )
    .bind(T)
    .bind(ask::TOOL_RUN_SCRIPT)
    .bind(rules.to_string())
    .execute(&pool)
    .await
    .expect("seed policy");
    sqlx::query(
        "INSERT INTO resource_group_argument_policies (resource_group_id, argument_policy_id) \
         VALUES ($1, 'ap-rcpar-band')",
    )
    .bind(rg)
    .execute(&pool)
    .await
    .expect("bind policy");

    let state = common::build_test_state(pool.clone());
    let root = run_root(&state, &key);
    let call = || {
        let mut req = common::build_post_request(
            &format!("/v1/proxy/mcp/{SKILL_SERVER}"),
            json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": ask::TOOL_RUN_SCRIPT,
                              "arguments": {"skill_name": "rcpar-skill", "script": "run.py",
                                            "args": {"amount": 8000}}}}),
            Some(&key),
            Some(T),
        );
        req.headers_mut()
            .insert("x-brutor-run-id", RUN.parse().expect("run header"));
        req
    };
    let holds = || async {
        let (n,): (i64,) =
            sqlx::query_as("SELECT count(*) FROM tool_approval_requests WHERE tenant_id = $1")
                .bind(T)
                .fetch_one(&pool)
                .await
                .expect("approvals");
        n
    };

    stop_the_run(&pool, &state, &root, Stop::Aborted, rg).await;
    let resp = app(&state, Surface::SkillServerRunScript)
        .oneshot(call())
        .await
        .expect("router call");
    assert_eq!(resp.status(), axum::http::StatusCode::FORBIDDEN);
    assert!(refusal_row(&pool, "run_aborted").await.is_some());
    assert_eq!(holds().await, 0, "an aborted run queued an approval");

    // The premise: the same call of a run that may go on IS held.
    sqlx::query("DELETE FROM run_interventions WHERE id = 'ri-rcpar'")
        .execute(&pool)
        .await
        .expect("lift the abort");
    let state = common::build_test_state(pool.clone());
    let resp = app(&state, Surface::SkillServerRunScript)
        .oneshot(call())
        .await
        .expect("router call");
    assert_eq!(resp.status(), axum::http::StatusCode::ACCEPTED);
    assert_eq!(holds().await, 1, "a live run's banded call is held");
}
