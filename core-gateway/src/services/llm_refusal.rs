// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # The one way an LLM surface refuses a request
//!
//! A governed refusal — a 403 or 429 the gateway itself decides, before or
//! instead of a provider call — is evidence. It has to leave a `proxy_logs`
//! row that:
//!
//! * names the control that refused (`proxy_subtype`, per the house
//!   taxonomy in `constants::proxy_subtype`: a `*_block` / `access_denied` /
//!   `engine_failure` subtype for a policy decision, `rate_limit_<scope>` for
//!   a rate, quota or capacity ceiling), so the evidence writer seals it as a
//!   `denied` record (`evidence::mint::is_refusal`) and the run ledger counts
//!   it as a denial;
//! * lands on the run it stopped (`root_task_id` / `current_task_id` /
//!   depth), so the run's timeline shows why it ended;
//! * carries the model, group and caller facts every other row carries.
//!
//! Before this module each refusal site wrote its own row, or did not: the
//! embeddings and media-governance 403s, the LLM parameter-governance 403
//! and the resource-group concurrency 429 returned without any row, so a
//! reviewer asking "what did governance refuse?" got an answer with holes in
//! it. [`refuse`] is the single path — it writes the row and hands back the
//! response, whose status the row records (they cannot disagree). The static
//! guard `tests/llm_refusal_guard.rs` fails if an LLM surface builds a
//! 403/429 anywhere else.

use std::time::Instant;

use axum::http::HeaderMap;
use axum::response::Response;

use crate::models::auth::AuthContext;
use crate::models::db::{LlmModel, ProxyLogInsert};
use crate::services::SharedState;
use crate::services::a2a_delegation::{CallChain, TraceContinuity};
use crate::services::{proxy_logger, proxy_metrics};

/// Where a refused request sits in its run.
#[derive(Debug, Clone)]
pub enum RunAttribution {
    /// The request's own call chain (the normal case: every LLM surface
    /// establishes it at entry, before any gate runs).
    Chain {
        chain: CallChain,
        current_task_id: String,
        continuity: TraceContinuity,
    },
    /// Only the run is known — for callers that hold a run id but no chain.
    /// Stamps `root_task_id` and, when known, the delegation depth.
    Run {
        root_task_id: String,
        depth: Option<u32>,
    },
    /// A step taken on a run's behalf after its request is gone (the batch
    /// result fetcher): the run and the step it descends from are recorded,
    /// the step's own id is minted. No trace continuity is claimed — nothing
    /// arrived over the wire.
    Step {
        root_task_id: String,
        parent_task_id: Option<String>,
        current_task_id: String,
        depth: Option<u32>,
    },
}

impl RunAttribution {
    pub fn chain(chain: &CallChain, current_task_id: &str, continuity: TraceContinuity) -> Self {
        RunAttribution::Chain {
            chain: chain.clone(),
            current_task_id: current_task_id.to_string(),
            continuity,
        }
    }

    /// The run's `root_task_id`.
    pub fn root_task_id(&self) -> String {
        match self {
            RunAttribution::Chain { chain, .. } => chain.root_task_id.to_string(),
            RunAttribution::Run { root_task_id, .. }
            | RunAttribution::Step { root_task_id, .. } => root_task_id.clone(),
        }
    }

    /// Stamp the run (and, for a chain, the step) on a row — for a surface's
    /// own outcome row, so it names the same run as its refusal rows.
    pub fn stamp(&self, log: &mut ProxyLogInsert, auth: &AuthContext) {
        match self {
            RunAttribution::Chain {
                chain,
                current_task_id,
                continuity,
            } => proxy_logger::apply_call_chain(
                log,
                chain,
                current_task_id,
                *continuity,
                proxy_logger::root_kind_for_auth(&auth.auth_type, chain.depth == 0),
            ),
            RunAttribution::Run {
                root_task_id,
                depth,
            } => {
                log.root_task_id = Some(root_task_id.clone());
                log.delegation_depth = depth.map(|d| d as i32);
            }
            RunAttribution::Step {
                root_task_id,
                parent_task_id,
                current_task_id,
                depth,
            } => {
                log.root_task_id = Some(root_task_id.clone());
                log.parent_task_id = parent_task_id.clone();
                log.current_task_id = Some(current_task_id.clone());
                log.delegation_depth = depth.map(|d| d as i32);
            }
        }
    }
}

/// Everything about the refused request that is not the refusal itself.
#[derive(Clone)]
pub struct RefusalScope<'a> {
    pub auth_ctx: &'a AuthContext,
    pub headers: &'a HeaderMap,
    pub correlation_id: &'a str,
    /// The endpoint label of the row (the surface's path, or a preflight
    /// label such as `preflight_rate_limit` that existing queries key on).
    pub endpoint: &'a str,
    pub started: Option<Instant>,
    /// `(model, model name the caller asked for)` when the model resolved.
    pub model: Option<(&'a LlmModel, Option<String>)>,
    pub group_id: Option<String>,
    pub run: Option<&'a RunAttribution>,
    /// The raw request body, captured so the row shows what was attempted.
    pub request_body: Option<&'a str>,
}

impl<'a> RefusalScope<'a> {
    /// A scope naming the caller, the request and the run; the optional
    /// facts are added with the builder methods below.
    pub fn new(
        auth_ctx: &'a AuthContext,
        headers: &'a HeaderMap,
        correlation_id: &'a str,
        endpoint: &'a str,
        run: &'a RunAttribution,
    ) -> Self {
        RefusalScope {
            auth_ctx,
            headers,
            correlation_id,
            endpoint,
            started: None,
            model: None,
            group_id: None,
            run: Some(run),
            request_body: None,
        }
    }

    pub fn started(mut self, started: Instant) -> Self {
        self.started = Some(started);
        self
    }

    pub fn model(mut self, model: &'a LlmModel, requested: Option<String>) -> Self {
        self.model = Some((model, requested));
        self
    }

    pub fn group(mut self, group_id: Option<String>) -> Self {
        self.group_id = group_id;
        self
    }
}

/// What refused, and why.
pub struct Refusal {
    /// `proxy_logs.proxy_subtype` — a `constants::proxy_subtype` value or a
    /// `rate_limit_<scope>` string. `None` only for a [`Refusal::flagged`]
    /// block, whose control is named by a flag the decorator sets.
    pub subtype: Option<String>,
    pub message: String,
    pub error_type: Option<String>,
    pub tags: Option<serde_json::Value>,
}

impl Refusal {
    pub fn new(subtype: impl Into<String>, message: impl Into<String>) -> Self {
        Refusal {
            subtype: Some(subtype.into()),
            message: message.into(),
            error_type: None,
            tags: None,
        }
    }

    /// A block whose control is named by a row FLAG rather than a subtype —
    /// a guardrail (`guardrail_blocked`, stamped by [`stamp_guardrail_block`])
    /// or an argument policy (`argument_policy_denied`). The caller's
    /// decorator sets the flag; the evidence writer seals the row on it.
    pub fn flagged(message: impl Into<String>) -> Self {
        Refusal {
            subtype: None,
            message: message.into(),
            error_type: None,
            tags: None,
        }
    }

    /// A rate, quota or capacity ceiling: `rate_limit_<scope>`, the prefix
    /// the evidence writer and the control plane read as a refusal.
    pub fn rate_limit(scope: &str, message: impl Into<String>) -> Self {
        Refusal::new(format!("{RATE_LIMIT_PREFIX}{scope}"), message)
    }

    pub fn error_type(mut self, t: impl Into<String>) -> Self {
        self.error_type = Some(t.into());
        self
    }

    pub fn tags(mut self, tags: serde_json::Value) -> Self {
        self.tags = Some(tags);
        self
    }
}

/// The subtype prefix of every rate / quota / capacity refusal.
pub const RATE_LIMIT_PREFIX: &str = "rate_limit_";

/// The `proxy_logs` row for a refusal. Pure — the row's status is the
/// response's status.
pub fn build_row(scope: &RefusalScope<'_>, refusal: &Refusal, status: u16) -> ProxyLogInsert {
    let duration_ms = scope
        .started
        .map(|s| s.elapsed().as_secs_f64() * 1000.0)
        .unwrap_or(0.0);
    let mut log = proxy_logger::build_proxy_log(
        crate::constants::proxy::LLM,
        scope.auth_ctx,
        "POST",
        scope.endpoint,
        None,
        scope.model.as_ref().map(|(m, _)| m.id.as_str()),
        status,
        duration_ms,
        false,
        Some(refusal.message.as_str()),
        scope.correlation_id,
    );
    log.user_agent = proxy_logger::extract_user_agent(scope.headers);
    log.proxy_subtype = refusal.subtype.clone();
    log.error_type = refusal.error_type.clone();
    log.tags = refusal.tags.clone();
    if let Some((model, requested)) = &scope.model {
        log.llm_model_requested = requested.clone().or_else(|| Some(model.model_name.clone()));
        log.llm_model_used = Some(model.model_name.clone());
        log.llm_provider = Some(model.provider.clone());
    }
    log.resource_group_id = scope.group_id.clone();
    if let Some(rb) = scope.request_body {
        log.request_body_preview = Some(proxy_logger::body_preview(rb));
        log.request_size_bytes = Some(rb.len() as i32);
    }
    if let Some(run) = scope.run {
        run.stamp(&mut log, scope.auth_ctx);
    }
    log
}

/// Record a refusal and return `response` — the only way an LLM surface
/// refuses (see the module docs).
pub fn refuse(
    state: &SharedState,
    scope: &RefusalScope<'_>,
    refusal: Refusal,
    response: Response,
) -> Response {
    refuse_with(state, scope, refusal, |_| {}, response)
}

/// [`refuse`], with extra facts stamped on the row (a guardrail's check, an
/// agent-authz decision, a residency decision) before it is written.
pub fn refuse_with(
    state: &SharedState,
    scope: &RefusalScope<'_>,
    refusal: Refusal,
    decorate: impl FnOnce(&mut ProxyLogInsert),
    response: Response,
) -> Response {
    // A refused call's admission (if it got one) is given back by its hold
    // guard, which the surface drops with this response — never by id here,
    // where a refusal raised before admission could hand back another
    // request's place.
    let status = response.status().as_u16();
    let mut log = build_row(scope, &refusal, status);
    decorate(&mut log);
    tracing::debug!(
        subtype = ?refusal.subtype,
        endpoint = %scope.endpoint,
        status,
        "LLM refusal recorded",
    );
    proxy_metrics::record_request(
        crate::constants::proxy::LLM,
        "POST",
        status,
        scope
            .model
            .as_ref()
            .map(|(m, _)| m.provider.as_str())
            .unwrap_or("n/a"),
    );
    state
        .background_writer
        .write_proxy_log(state.db.clone(), log);
    response
}

/// Stamp a guardrail block on a row: which config, which check, what action,
/// plus `pii_detected` for a PII-category check.
///
/// The one definition of "this row is a guardrail block", shared by the
/// pre-flight and post-response helpers (through [`refuse_with`]) and by the
/// streaming surfaces, whose block is recorded on the stream's own outcome
/// row — the row that already carries the run, the step and the tokens the
/// provider billed before the guard stopped it.
pub fn stamp_guardrail_block(
    log: &mut ProxyLogInsert,
    violation: &crate::services::guardrails::GuardrailViolation,
) {
    proxy_logger::apply_guardrail_violation(log, violation);
    // A streamed or post-response block is a block whatever the config's
    // action label says: the gateway stopped the content.
    log.guardrail_blocked = true;
    if crate::services::llm_preflight::check_is_pii(&violation.check) {
        proxy_logger::apply_pii_detected(log);
    }
}

/// The refusal subtype of a router error that is the gateway's own limit —
/// a routing group's RPM / TPM ceiling or a model's concurrency cap — as
/// opposed to a downstream failure. The surface's error row for such an error
/// is the refusal row and carries this subtype, so it is sealed `denied`.
pub fn router_limit_subtype(err: &crate::services::llm_router::LlmRouterError) -> Option<String> {
    use crate::services::llm_router::LlmRouterError as E;
    match err {
        E::RateLimitExceeded(_) => Some(format!(
            "{RATE_LIMIT_PREFIX}{}",
            crate::services::llm_preflight::ROUTING_SCOPE
        )),
        E::ConcurrencyLimitReached(_) => Some(format!(
            "{RATE_LIMIT_PREFIX}{}",
            crate::services::llm_preflight::CONCURRENCY_SCOPE
        )),
        _ => None,
    }
}

/// Take the cluster-wide resource-group / per-model concurrency slot
/// (`llm_global_limits.concurrency.max_concurrent`), or refuse with
/// `rate_limit_concurrency`. `Ok(None)` when there is no group to gate.
pub async fn acquire_concurrency(
    state: &SharedState,
    group: Option<&crate::models::db::ResourceGroup>,
    model: &LlmModel,
    scope: &RefusalScope<'_>,
) -> Result<Option<crate::services::usage::LlmConcurrencyGuard>, Response> {
    let Some(g) = group else { return Ok(None) };
    match crate::services::usage::acquire_llm_concurrency(
        &state.usage_state,
        &state.db,
        g,
        &model.id,
        model.routed_via_group_id.as_deref(),
    )
    .await
    {
        Ok(guard) => Ok(Some(guard)),
        Err(block) => {
            let scope = RefusalScope {
                group_id: Some(g.id.clone()),
                ..scope.clone()
            };
            Err(refuse(
                state,
                &scope,
                Refusal::rate_limit(
                    crate::services::llm_preflight::CONCURRENCY_SCOPE,
                    block.message.as_str(),
                )
                .error_type(format!("llm_concurrency:{}", block.scope)),
                crate::services::llm_resilience::gate_rejection_response(
                    &crate::services::llm_resilience::GateRejection::RgConcurrencySaturated {
                        scope: block.scope,
                        message: block.message.clone(),
                    },
                ),
            ))
        }
    }
}

/// The guardrail configs could not be read, so the call is refused (fail
/// closed) and the refusal is recorded like any other: sealed
/// `engine_failure`, because no policy decided it. The
/// 503 leaves a row on every LLM surface.
pub fn guardrails_unavailable(
    state: &SharedState,
    scope: &RefusalScope<'_>,
    surface: &str,
    error: &sqlx::Error,
) -> Response {
    refuse(
        state,
        scope,
        Refusal::new(
            crate::constants::proxy_subtype::ENGINE_FAILURE,
            format!(
                "guardrails_unavailable: the {surface} guardrail configuration could not be read"
            ),
        ),
        crate::services::guardrails::configs_unavailable(surface, error),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::auth::AuthType;

    fn auth() -> AuthContext {
        AuthContext {
            auth_type: AuthType::ApiKey,
            tenant_id: "t".into(),
            gateway_user_id: None,
            gateway_username: None,
            gateway_tenant_id: None,
            idp_user_id: None,
            idp_username: None,
            idp_email: None,
            api_key_id: None,
            api_key_name: None,
            api_key_scope: None,
            api_key_group_id: None,
            api_key_is_platform: false,
            api_key_user_id: None,
            allowed_skill_ids: None,
            session_key: None,
            client_ip: None,
            resolved_group_id: None,
            actor_agent_id: None,
            session_token_type: None,
            agent_impl: crate::services::agent_impl::request_carriers(),
            admission_hold: None,
        }
    }

    #[test]
    fn the_row_names_the_control_the_run_and_the_status() {
        let a = auth();
        let headers = HeaderMap::new();
        let chain = CallChain::root(ulid::Ulid::new());
        let run = RunAttribution::chain(&chain, "task-1", TraceContinuity::Minted);
        let scope = RefusalScope {
            auth_ctx: &a,
            headers: &headers,
            correlation_id: "c-1",
            endpoint: "/embeddings",
            started: None,
            model: None,
            group_id: Some("rg-1".into()),
            run: Some(&run),
            request_body: Some("{\"input\":\"x\"}"),
        };
        let row = build_row(
            &scope,
            &Refusal::new(
                crate::constants::proxy_subtype::LLM_GOVERNANCE_BLOCK,
                "policy: dim",
            ),
            403,
        );
        assert_eq!(row.http_status_code, Some(403));
        assert!(!row.success);
        assert_eq!(
            row.proxy_subtype.as_deref(),
            Some(crate::constants::proxy_subtype::LLM_GOVERNANCE_BLOCK)
        );
        assert_eq!(
            row.root_task_id.as_deref(),
            Some(chain.root_task_id.to_string().as_str())
        );
        assert_eq!(row.current_task_id.as_deref(), Some("task-1"));
        assert_eq!(row.resource_group_id.as_deref(), Some("rg-1"));
        assert_eq!(row.trace_root_kind.as_deref(), Some("agent_invocation"));
        assert!(crate::services::evidence::mint::should_record(&row));
    }

    #[test]
    fn only_the_gateways_own_router_limits_are_refusals() {
        use crate::services::llm_router::LlmRouterError as E;
        assert_eq!(
            router_limit_subtype(&E::RateLimitExceeded("rpm".into())).as_deref(),
            Some("rate_limit_routing")
        );
        assert_eq!(
            router_limit_subtype(&E::ConcurrencyLimitReached("g".into())).as_deref(),
            Some("rate_limit_concurrency")
        );
        // Cooldowns after provider failures: downstream, not a refusal.
        assert_eq!(
            router_limit_subtype(&E::AllModelsBusy {
                group_id: "g1".into(),
                group: "g".into(),
                retry_after_secs: Some(5),
            }),
            None
        );
    }

    #[test]
    fn a_rate_limit_is_a_refusal_to_the_evidence_writer() {
        let r = Refusal::rate_limit("concurrency", "busy");
        assert_eq!(r.subtype.as_deref(), Some("rate_limit_concurrency"));
        let a = auth();
        let headers = HeaderMap::new();
        let run = RunAttribution::Run {
            root_task_id: "root".into(),
            depth: Some(2),
        };
        let scope = RefusalScope {
            auth_ctx: &a,
            headers: &headers,
            correlation_id: "c",
            endpoint: "preflight",
            started: None,
            model: None,
            group_id: None,
            run: Some(&run),
            request_body: None,
        };
        let row = build_row(&scope, &r, 429);
        assert_eq!(row.root_task_id.as_deref(), Some("root"));
        assert_eq!(row.delegation_depth, Some(2));
        assert!(crate::services::evidence::mint::should_record(&row));
    }
}
