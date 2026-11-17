// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//
// LLM routing — resolves model, builds upstream request, handles
// fallback chains, routing groups, and streaming.
//
// This is the router of the proxy layer. Provider-specific URL
// discovery, parameter mapping, and parameter dropping are handled
// by the `providers` module.
//
// Implements these routing strategies:
//   - usage-based-routing-v2 (lowest TPM with RPM/TPM limit enforcement)
//   - least-busy (fewest active requests)
//   - latency-based-routing (lowest EMA latency)
//   - cost-based-routing (cheapest by blended input+output rate; deterministic,
//     so across capability tiers it is a static pin, not an optimisation)
//   - simple-shuffle (weighted random)
//
// Plus: cooldown tracking, system prompt injection, context window
// pre-call filtering, concurrency limiting, and response caching.

use std::time::Duration;

use reqwest::Client;
use serde_json::Value;
use sqlx::PgPool;

use crate::models::db::{LlmModel, LlmRoutingGroup};
use crate::models::proxy::{ChatCompletionRequest, UsageInfo};
use crate::services::db_errors::DbResultExt;
use crate::services::encryption::EncryptionService;
use crate::services::prompt_caching::PromptCacheSession;
use crate::services::providers;
use crate::services::router_state::RouterState;

tokio::task_local! {
    /// Prompt-derived signals for content-aware routing strategies, scoped by
    /// the preflight path just around model resolution.
    ///
    /// Model selection is otherwise deliberately content-blind — selectors see
    /// candidate models and rate-limit state, never the prompt. The
    /// `complexity-based-routing` strategy is the one exception, and it reads
    /// the prompt through here rather than by widening `resolve_model`'s public
    /// signature for a strategy almost nobody enables. Unset (the default, and
    /// the case for every caller that does not opt in) means content-aware
    /// strategies fall back to the group's ordinary behaviour.
    pub static ROUTING_REQUEST_CONTEXT: Option<RoutingRequestContext>;
}

/// The slice of a request that content-aware routing may look at.
///
/// Deliberately narrow: the latest user turn, the system instructions, and how
/// many tools were offered. No history, no tool *contents*, no identifiers —
/// a routing strategy has no business reading more of the prompt than it
/// scores, and keeping this small bounds what a future strategy can reach for.
#[derive(Debug, Clone, Default)]
pub struct RoutingRequestContext {
    pub user_text: String,
    pub system_text: Option<String>,
    pub tool_count: usize,
}

impl RoutingRequestContext {
    /// Extract routing signals from a raw JSON request body.
    ///
    /// Returns `None` when the body is absent, unparseable, or carries no user
    /// turn — all of which mean "no content to route on", never an error. This
    /// runs on the hot path for every request, so it must never fail loudly and
    /// must never panic on a hostile body.
    pub fn from_body(body: Option<&str>) -> Option<Self> {
        let raw = body?;
        let json: serde_json::Value = serde_json::from_str(raw).ok()?;

        // Tools may be declared as `tools` (chat/Anthropic) or `functions`
        // (legacy). Count only; contents are never read.
        let tool_count = json
            .get("tools")
            .or_else(|| json.get("functions"))
            .and_then(|t| t.as_array())
            .map(|a| a.len())
            .unwrap_or(0);

        // Anthropic carries the system prompt as a top-level field; OpenAI
        // carries it as a message role. Collect both shapes.
        let mut system_parts: Vec<String> = Vec::new();
        if let Some(sys) = json.get("system") {
            if let Some(s) = content_to_text(sys) {
                system_parts.push(s);
            }
        }

        let mut user_text: Option<String> = None;
        if let Some(messages) = json.get("messages").and_then(|m| m.as_array()) {
            // Walk backwards: the latest user turn is the request, and earlier
            // turns are context we deliberately do not score.
            for msg in messages.iter().rev() {
                let role = msg.get("role").and_then(|r| r.as_str()).unwrap_or("");
                let text = msg.get("content").and_then(content_to_text);
                match (role, text) {
                    ("user", Some(t)) if user_text.is_none() && !t.trim().is_empty() => {
                        user_text = Some(t);
                    }
                    ("system" | "developer", Some(t)) if !t.trim().is_empty() => {
                        system_parts.push(t);
                    }
                    _ => {}
                }
            }
        }

        // Completions-style bodies have no messages at all.
        let user_text = user_text.or_else(|| {
            json.get("prompt")
                .and_then(content_to_text)
                .or_else(|| json.get("input").and_then(content_to_text))
        })?;

        Some(Self {
            user_text,
            system_text: if system_parts.is_empty() {
                None
            } else {
                system_parts.reverse();
                Some(system_parts.join(" "))
            },
            tool_count,
        })
    }
}

/// Flatten a `content` value to plain text.
///
/// Handles the three shapes providers use: a bare string, an array of content
/// parts (`[{"type":"text","text":"..."}]`), and an array of bare strings.
/// Non-text parts (images, audio) contribute nothing rather than erroring —
/// a multimodal request simply scores on whatever text it carries.
fn content_to_text(v: &serde_json::Value) -> Option<String> {
    match v {
        serde_json::Value::String(s) => Some(s.clone()),
        serde_json::Value::Array(parts) => {
            let joined: Vec<String> = parts
                .iter()
                .filter_map(|p| match p {
                    serde_json::Value::String(s) => Some(s.clone()),
                    serde_json::Value::Object(_) => p
                        .get("text")
                        .and_then(|t| t.as_str())
                        .map(|s| s.to_string()),
                    _ => None,
                })
                .collect();
            if joined.is_empty() {
                None
            } else {
                Some(joined.join(" "))
            }
        }
        _ => None,
    }
}

tokio::task_local! {
    /// The exact JSON body POSTed to the upstream provider for the
    /// current request, captured by the deepest forward function just
    /// before send. The route handler scopes this and reads it so the
    /// proxy log records *what was actually sent to the LLM* — resolved
    /// real model, provider-translated shape (e.g. OpenAI→Anthropic
    /// Messages), injected system prompt, and prompt-cache markers —
    /// rather than the inbound client body. Audit fidelity: the log
    /// matches the bytes the provider received.
    pub static OUTBOUND_REQUEST_BODY: std::cell::RefCell<Option<String>>;
}

/// Record the final outbound provider body for proxy logging. No-op
/// unless the caller scoped [`OUTBOUND_REQUEST_BODY`] (so internal
/// callers / tests that don't care are unaffected). Call this at every
/// upstream send site, after all request mutations are applied.
pub fn record_outbound_body(body: &Value) {
    let _ = OUTBOUND_REQUEST_BODY.try_with(|cell| {
        *cell.borrow_mut() = Some(serde_json::to_string(body).unwrap_or_default());
    });
}

#[derive(Debug)]
pub struct LlmRouteResult {
    pub model: LlmModel,
    pub response_body: Value,
    pub usage: Option<UsageInfo>,
    pub status_code: u16,
    pub used_fallback: bool,
    pub fallback_reason: Option<String>,
}

// =========================================================================
// Model resolution
// =========================================================================

/// Open the row's two stored credentials in place: `api_key` and
/// `provider_config` (JSON, cloud credentials). One rule for every loader
/// (the model cache, the router's own queries, the batch loops)):
///
/// * the columns hold ciphertext and nothing else (the Control Plane
///   encrypts both, and refuses to store either without its encryption
///   service). A stored ciphertext that decrypts is replaced with its
///   plaintext;
/// * a stored value the gateway cannot open (written under another
///   `ENCRYPTION_KEY`, an envelope the gateway cannot unwrap, or a value
///   that is not ciphertext at all) is removed and the row is marked
///   `credential_unreadable`, which `providers::dispatch_refusal` refuses.
///   It is never used as the credential: sent as read, it would hand the
///   provider whatever is stored as the bearer;
/// * a row is opened once (`credentials_opened`). A row the cache opened
///   can pass here again and is left as it is: whether a value is stored
///   or opened is a fact carried on the row, never read from its shape.
///
/// The log line names the row, never the value.
pub fn open_model_credentials(model: &mut LlmModel, encryption: &EncryptionService) {
    if model.credentials_opened {
        return;
    }
    model.credentials_opened = true;
    for (field, name) in [
        (&mut model.api_key, "api_key"),
        (&mut model.provider_config, "provider_config"),
    ] {
        let Some(stored) = field.as_deref().filter(|v| !v.is_empty()) else {
            continue;
        };
        match encryption.decrypt(stored) {
            Ok(plain) => *field = Some(plain),
            Err(e) => {
                tracing::error!(
                    tenant_id = %model.tenant_id,
                    model_id = %model.id,
                    field = name,
                    "stored model credential is not a ciphertext this gateway can open ({e}); \
                     the model is refused until it is saved again under this ENCRYPTION_KEY"
                );
                *field = None;
                model.credential_unreadable = true;
            }
        }
    }
}

/// Resolve a model by name or ID, considering routing groups and tenant scope.
/// Uses ModelCache when available (cache-first, DB on miss).
/// Decrypts the model's `api_key` field if it's encrypted (Fernet token).
/// Route a "virtual" routing-group selector model to one of its
/// member models. The control plane creates one `LlmModel` per
/// routing group (id = `rg-<group_id>`, model_name = the group's
/// id, mode = "routing", api_key = NULL, provider = the synthetic
/// `routing_group` provider) so admins can target the group as
/// if it were any other model. Without this redirect, that
/// virtual row gets returned to the request pipeline verbatim,
/// the proxy treats it as a real upstream, sends it (with no
/// API key) to the routing_group "provider" — which isn't a real
/// upstream — and the user sees a misleading 401 from OpenAI
/// because the proxy fell through to its default endpoint.
///
/// We catch every exit from `resolve_model` (cache hit, cache
/// miss, by-id, by-name) so the redirect happens regardless of
/// the path the admin used to address the group.
async fn redirect_if_routing(
    model: LlmModel,
    db: &PgPool,
    tenant_id: &str,
    router_state: &RouterState,
    encryption: Option<&EncryptionService>,
    provider_cache: Option<&std::sync::Arc<crate::services::cache::ProviderCache>>,
) -> Result<LlmModel, LlmRouterError> {
    // Pass-through for the common case — direct model.
    if model.mode.as_deref() != Some("routing") {
        return Ok(model);
    }

    // Routing virtual model. The `routing_group_id` column on
    // the virtual row points at the parent group; look it up by
    // id (NOT by name — `ModelCache::get_routing_group` keys on
    // name and would miss the convention `model_name = group.id`
    // used by the control plane's `create_routing_group`).
    let group_id = model.routing_group_id.as_deref().ok_or_else(|| {
        LlmRouterError::ModelNotFound(format!(
            "virtual routing model {} has mode=routing but routing_group_id is NULL — \
             control-plane create_routing_group bug",
            model.id
        ))
    })?;

    let group: LlmRoutingGroup = sqlx::query_as(
        "SELECT * FROM llm_routing_group WHERE id = $1 AND tenant_id = $2 AND enabled = true",
    )
    .bind(group_id)
    .bind(tenant_id)
    .fetch_optional(db)
    .await
    .map_err(|e| LlmRouterError::DbError(e.to_string()))?
    .ok_or_else(|| {
        LlmRouterError::ModelNotFound(format!(
            "routing group {group_id} (referenced by virtual model {}) not found or disabled",
            model.id
        ))
    })?;

    let mut selected = select_from_routing_group(db, &group, tenant_id, router_state).await?;
    if let Some(pc) = provider_cache {
        pc.stamp_one(&mut selected).await;
    }
    // Member API keys are encrypted at rest. The virtual row
    // had no key — we MUST decrypt the chosen member's,
    // otherwise the upstream call goes out with the ciphertext
    // bytes (or no auth header at all) and 401s. This was the
    // exact failure mode the user hit before this redirect was
    // wired in.
    if let Some(enc) = encryption {
        open_model_credentials(&mut selected, enc);
    }
    tracing::debug!(
        virtual_model_id = %model.id,
        group_id = %group.id,
        group_name = %group.name,
        selected_model_id = %selected.id,
        selected_model_name = %selected.model_name,
        selected_provider = %selected.provider,
        "Redirected routing-group virtual model to member"
    );
    Ok(selected)
}

/// Refuse a model whose endpoint the outbound policy denies.
///
/// `api_url` is tenant-configured. The policy client's DNS resolver drops
/// denied addresses for hostnames, but hyper never resolves an IP-literal
/// host, so `http://169.254.169.254/…` or `http://[fd00:ec2::254]/` would
/// be fetched directly. A model with no `api_url` uses a provider default
/// (a public hostname) and passes. Called on every model the router can
/// dispatch to: [`resolve_model`], fallback candidates, sticky pins, batch
/// jobs, video jobs and the catalog probe.
pub fn guard_model_url(model: &LlmModel) -> Result<(), LlmRouterError> {
    let Some(url) = model
        .api_url
        .as_deref()
        .map(str::trim)
        .filter(|u| !u.is_empty())
    else {
        return Ok(());
    };
    crate::services::outbound_policy::check_url(url).map_err(|denied| {
        tracing::warn!(
            model_id = %model.id,
            host = %denied.host,
            reason = denied.reason,
            "model endpoint refused by outbound policy"
        );
        LlmRouterError::OutboundDenied(denied.to_string())
    })
}

/// Resolve the model a request names, then refuse it if its endpoint is
/// not a permitted outbound destination ([`guard_model_url`]).
pub async fn resolve_model(
    db: &PgPool,
    tenant_id: &str,
    model_name: Option<&str>,
    model_id: Option<&str>,
    router_state: &RouterState,
    encryption: Option<&EncryptionService>,
    model_cache: Option<&crate::services::cache::ModelCache>,
    provider_cache: Option<&std::sync::Arc<crate::services::cache::ProviderCache>>,
) -> Result<LlmModel, LlmRouterError> {
    let model = resolve_model_unchecked(
        db,
        tenant_id,
        model_name,
        model_id,
        router_state,
        encryption,
        model_cache,
        provider_cache,
    )
    .await?;
    guard_model_url(&model)?;
    Ok(model)
}

async fn resolve_model_unchecked(
    db: &PgPool,
    tenant_id: &str,
    model_name: Option<&str>,
    model_id: Option<&str>,
    router_state: &RouterState,
    encryption: Option<&EncryptionService>,
    model_cache: Option<&crate::services::cache::ModelCache>,
    provider_cache: Option<&std::sync::Arc<crate::services::cache::ProviderCache>>,
) -> Result<LlmModel, LlmRouterError> {
    tracing::debug!(
        model_name = ?model_name,
        model_id = ?model_id,
        tenant_id,
        "Resolving model"
    );

    // By explicit ID
    if let Some(id) = model_id {
        // Cache-first lookup
        if let Some(cache) = model_cache {
            if let Some(model) = cache.get_model_by_id(tenant_id, id).await {
                tracing::debug!(
                    model_id = %model.id,
                    model_name = %model.model_name,
                    provider = %model.provider,
                    cached = true,
                    "Resolved model by ID (cached)"
                );
                // ModelCache already decrypts api_key on load.
                // Routing-virtual models still need to redirect
                // to a real member — see `redirect_if_routing`.
                return redirect_if_routing(
                    model.as_ref().clone(),
                    db,
                    tenant_id,
                    router_state,
                    encryption,
                    provider_cache,
                )
                .await;
            }
            return Err(LlmRouterError::ModelNotFound(id.to_string()));
        }

        // Fallback: direct DB query (no cache available)
        let model: Option<LlmModel> = sqlx::query_as(concat!(
            "SELECT * FROM llm_model WHERE id = $1 AND tenant_id = $2 AND ",
            crate::model_available_sql!("")
        ))
        .bind(id)
        .bind(tenant_id)
        .fetch_optional(db)
        .await
        .map_err(|e| LlmRouterError::DbError(e.to_string()))?;

        let mut model = model.ok_or_else(|| LlmRouterError::ModelNotFound(id.to_string()))?;
        if let Some(pc) = provider_cache {
            pc.stamp_one(&mut model).await;
        }
        tracing::debug!(
            model_id = %model.id,
            model_name = %model.model_name,
            provider = %model.provider,
            "Resolved model by ID"
        );

        if let Some(enc) = encryption {
            open_model_credentials(&mut model, enc);
        }
        return redirect_if_routing(
            model,
            db,
            tenant_id,
            router_state,
            encryption,
            provider_cache,
        )
        .await;
    }

    // By model_name (provider model string like "gpt-4o")
    if let Some(name) = model_name {
        // Cache-first: check routing group, then direct model
        if let Some(cache) = model_cache {
            // Check routing group first
            // A failed read is a database error (503), not a miss that
            // falls through to "Model not found".
            if let Some(group) = cache
                .get_routing_group(tenant_id, name)
                .await
                .map_err(|e| LlmRouterError::DbError(e.to_string()))?
            {
                tracing::debug!(
                    group_name = %group.name,
                    strategy = ?group.routing_strategy,
                    cached = true,
                    "Resolved as routing group (cached), selecting model"
                );
                let mut model =
                    select_from_routing_group(db, &group, tenant_id, router_state).await?;
                if let Some(enc) = encryption {
                    open_model_credentials(&mut model, enc);
                }
                return Ok(model);
            }

            // Direct model lookup via cache. Note: a request
            // with `model = <routing_group_id>` lands here when
            // the cache's name-keyed routing-group lookup misses
            // (it keys on `name`, not `id`, while the virtual
            // model's `model_name` is the group's id). The
            // virtual row IS findable via this direct lookup;
            // `redirect_if_routing` then dispatches it to a
            // member. Without that helper the proxy would
            // forward the virtual row upstream and 401 because
            // it has no API key.
            if let Some(model) = cache.get_model(tenant_id, name).await {
                tracing::debug!(
                    model_id = %model.id,
                    model_name = %model.model_name,
                    provider = %model.provider,
                    cached = true,
                    "Resolved model by name (cached)"
                );
                return redirect_if_routing(
                    model.as_ref().clone(),
                    db,
                    tenant_id,
                    router_state,
                    encryption,
                    provider_cache,
                )
                .await;
            }
            return Err(LlmRouterError::ModelNotFound(name.to_string()));
        }

        // Fallback: direct DB queries (no cache available)

        // First check if it's a routing group name
        let group: Option<LlmRoutingGroup> = sqlx::query_as(
            "SELECT * FROM llm_routing_group WHERE name = $1 AND tenant_id = $2 AND enabled = true",
        )
        .bind(name)
        .bind(tenant_id)
        .fetch_optional(db)
        .await
        .map_err(|e| LlmRouterError::DbError(e.to_string()))?;

        if let Some(group) = group {
            tracing::debug!(
                group_name = %group.name,
                strategy = ?group.routing_strategy,
                "Resolved as routing group, selecting model"
            );
            let mut model = select_from_routing_group(db, &group, tenant_id, router_state).await?;
            if let Some(enc) = encryption {
                open_model_credentials(&mut model, enc);
            }
            return Ok(model);
        }

        // Direct model lookup
        let model: Option<LlmModel> = sqlx::query_as(concat!(
            "SELECT * FROM llm_model \
                 WHERE (model_name = $1 OR name = $1 OR id = $1) AND tenant_id = $2 AND ",
            crate::model_available_sql!(""),
            " ORDER BY routing_weight DESC NULLS LAST LIMIT 1"
        ))
        .bind(name)
        .bind(tenant_id)
        .fetch_optional(db)
        .await
        .map_err(|e| LlmRouterError::DbError(e.to_string()))?;

        let mut model = model.ok_or_else(|| LlmRouterError::ModelNotFound(name.to_string()))?;
        if let Some(pc) = provider_cache {
            pc.stamp_one(&mut model).await;
        }
        tracing::debug!(
            model_id = %model.id,
            model_name = %model.model_name,
            provider = %model.provider,
            "Resolved model by name"
        );

        if let Some(enc) = encryption {
            open_model_credentials(&mut model, enc);
        }
        return redirect_if_routing(
            model,
            db,
            tenant_id,
            router_state,
            encryption,
            provider_cache,
        )
        .await;
    }

    Err(LlmRouterError::NoModelSpecified)
}

// =========================================================================
// Routing strategy selection
// =========================================================================

/// Select a model from a routing group based on the group's strategy.
///
/// Pre-call checks applied before strategy selection:
///   1. Filter out cooled-down models
///   2. Filter out models that exceeded RPM limits
///   3. Filter out models that exceeded TPM limits
///
/// Nothing else is checked here: the group's `enable_pre_call_checks`,
/// `region_filter` and `min_context_window` were never read,
/// and the group's concurrency and rate caps are dimensions of
/// the call's admission, not of selection.
///
/// With no member left the error is `AllModelsBusy`, carrying the shortest
/// wait after which one is available again.
async fn select_from_routing_group(
    db: &PgPool,
    group: &LlmRoutingGroup,
    tenant_id: &str,
    router_state: &RouterState,
) -> Result<LlmModel, LlmRouterError> {
    // Spend caps used to be enforced here against the routing
    // group's own `budget_limit_usd` / `current_month_cost_usd`
    // columns. Those columns were removed because the platform
    // already enforces tenant + group + user spend limits via
    // the resource-group `usage_limits` system, which integrates
    // with per-request cost accounting. Routing-group-only
    // budgets were a duplicate path that never got wired into
    // the cost-write loop, so they were effectively dead.

    // Group concurrency (`max_parallel_requests`) is NOT acquired here.
    // It used to be taken as a `RouterState` counter at selection time and never
    // released, so the group wedged permanently after `max_parallel_requests`
    // lifetime requests. It is now enforced as an RAII slot in
    // `usage::acquire_llm_concurrency` (keyed `llm:rg:{group_id}`), acquired once
    // the request is admitted and released on Drop — see the selected member's
    // `routed_via_group_id` stamp below, which the handler passes through.

    let mut models = get_group_models(db, &group.id, tenant_id)
        .await
        .map_err(|e| LlmRouterError::DbError(e.to_string()))?;

    // Filter out virtual routing models (mode = "routing")
    models.retain(|m| m.mode.as_deref() != Some("routing"));

    if models.is_empty() {
        return Err(LlmRouterError::ModelNotFound(format!(
            "No enabled models in routing group '{}'",
            group.name
        )));
    }

    let cooldowns_disabled = group.disable_cooldowns.unwrap_or(false);
    // When each member left out below is available again, in seconds: the
    // shortest is the retry hint if none is left.
    let mut available_in: Vec<u64> = Vec::new();

    // Filter out cooled-down models
    if !cooldowns_disabled {
        let before = models.len();
        models.retain(|m| match router_state.cooldown_remaining(&m.id) {
            Some(left) => {
                available_in.push(left.as_secs() + u64::from(left.subsec_nanos() > 0));
                false
            }
            None => true,
        });
        let filtered = before - models.len();
        if filtered > 0 {
            tracing::debug!(
                group = %group.name,
                filtered_cooldown = filtered,
                remaining = models.len(),
                "Filtered cooled-down models"
            );
        }
    }

    // Filter out models that exceeded RPM limits
    {
        let before = models.len();
        models.retain(|m| {
            if let Some(rpm_limit) = m.rate_limit_rpm {
                if rpm_limit > 0 && router_state.check_rpm(&m.id, rpm_limit) {
                    tracing::debug!(
                        model = %m.model_name,
                        rpm_limit,
                        current_rpm = router_state.get_rpm(&m.id),
                        "Model excluded: RPM limit reached"
                    );
                    available_in.push(RouterState::seconds_until_rate_window_ends());
                    return false;
                }
            }
            true
        });
        let filtered = before - models.len();
        if filtered > 0 {
            tracing::debug!(group = %group.name, filtered_rpm = filtered, "Filtered RPM-exceeded models");
        }
    }

    // Filter out models that exceeded TPM limits
    {
        let before = models.len();
        models.retain(|m| {
            if let Some(tpm_limit) = m.rate_limit_tpm {
                if tpm_limit > 0 && router_state.check_tpm(&m.id, tpm_limit) {
                    tracing::debug!(
                        model = %m.model_name,
                        tpm_limit,
                        current_tpm = router_state.get_tpm(&m.id),
                        "Model excluded: TPM limit reached"
                    );
                    available_in.push(RouterState::seconds_until_rate_window_ends());
                    return false;
                }
            }
            true
        });
        let filtered = before - models.len();
        if filtered > 0 {
            tracing::debug!(group = %group.name, filtered_tpm = filtered, "Filtered TPM-exceeded models");
        }
    }

    // The group-level RPM/TPM caps are not decided here: they are dimensions
    // of the call's admission (`routing_limit_dims`), reserved atomically on
    // the shared counters with every other limit of the call. The per-model
    // filters above only steer selection away from a member this replica has
    // seen saturated; the admission is what enforces.

    if models.is_empty() {
        return Err(LlmRouterError::AllModelsBusy {
            group_id: group.id.clone(),
            group: group.name.clone(),
            retry_after_secs: available_in.iter().copied().min().map(|s| s.max(1)),
        });
    }

    let strategy = group
        .routing_strategy
        .as_deref()
        .unwrap_or("usage-based-routing-v2");

    tracing::debug!(
        group = %group.name,
        strategy,
        model_count = models.len(),
        models = %models.iter().map(|m| m.model_name.as_str()).collect::<Vec<_>>().join(", "),
        "Selecting from routing group"
    );

    let mut selected = match strategy {
        "simple-shuffle" => select_simple_shuffle(&models),
        "usage-based-routing" | "usage-based-routing-v2" => {
            select_usage_based(&models, router_state)
        }
        "least-busy" => select_least_busy(&models, router_state),
        "latency-based-routing" => select_latency_based(&models, router_state),
        "complexity-based-routing" => {
            select_complexity_based(db, group, tenant_id, &models, router_state).await
        }
        "cost-based-routing" => match select_cost_based(
            &models,
            cost_output_ratio(group.routing_strategy_args.as_ref()),
        ) {
            Some(m) => Ok(m),
            None => {
                // No candidate carries pricing, so there is no cost decision to
                // make. Fall through rather than dressing an arbitrary pick up
                // as one.
                tracing::warn!(
                    group_id = %group.id,
                    "cost-based routing: no model in the group has pricing — \
                     falling back to usage-based-routing"
                );
                select_usage_based(&models, router_state)
            }
        },
        _ => {
            // Unknown strategy — fall back to usage-based
            tracing::warn!(
                strategy,
                "Unknown routing strategy, using usage-based-routing-v2"
            );
            select_usage_based(&models, router_state)
        }
    }?;

    // Selection heuristics (usage-based, least-busy) read these local counts.
    if group.rpm_limit.is_some_and(|l| l > 0) {
        router_state.increment_rpm(&group.id);
    }
    // Always stamp the routing group: its `max_parallel_requests` and its
    // RPM/TPM caps bind every call routed through it. (Stamped only when a
    // TPM cap was set, the group's concurrency cap went unenforced.)
    selected.routed_via_group_id = Some(group.id.clone());
    // And its first-chunk timeout for a streamed call, when it states one.
    selected.routed_stream_timeout = group.stream_timeout.filter(|t| t.is_finite() && *t > 0.0);
    Ok(selected)
}

/// The per-minute caps of a call, as admission dimensions: the model's own
/// `rate_limit_rpm` / `rate_limit_tpm` on every call it takes (a direct
/// call, a routing-group member, a fallback), and, for a call routed
/// through a routing group, the group's `rpm_limit` / `tpm_limit`. Rolling
/// buckets on the shared counters, so they hold across replicas and under
/// concurrency (they were per-pod counts read before acting). `Err` when the
/// group cannot be read (fail closed). A model with no cap, called directly,
/// has none and reads nothing.
pub async fn routing_limit_dims(
    db: &sqlx::PgPool,
    tenant_id: &str,
    model: &LlmModel,
) -> Result<Vec<crate::services::limits::LimitDim>, sqlx::Error> {
    use crate::services::cache::usage_state::{Dim, Measure};
    use crate::services::limits::{DimRefusal, LimitDim, RateRetry};
    // The routing group's caps apply to a call routed through it; the
    // model's own caps apply to every call the model takes.
    let rg = model.routed_via_group_id.as_deref();
    let (g_rpm, g_tpm, g_name) = match rg {
        Some(rg) => {
            let row: Option<(Option<i32>, Option<i32>, String)> = sqlx::query_as(
                "SELECT rpm_limit, tpm_limit, name FROM llm_routing_group \
                 WHERE id = $1 AND tenant_id = $2",
            )
            .bind(rg)
            .bind(tenant_id)
            .fetch_optional(db)
            .await?;
            row.unwrap_or((None, None, rg.to_string()))
        }
        None => (None, None, String::new()),
    };
    let rg = rg.unwrap_or_default();
    let mut dims = Vec::new();
    for (scope, key, limit, measure, label, what) in [
        (
            "routing_group",
            format!("brutor:rgl:{rg}:rpm"),
            g_rpm,
            Measure::One,
            "rpm",
            format!(
                "Routing group '{g_name}' RPM limit ({}) reached",
                g_rpm.unwrap_or(0)
            ),
        ),
        (
            "routing_group",
            format!("brutor:rgl:{rg}:tpm"),
            g_tpm,
            Measure::TotalTokens,
            "tpm",
            format!(
                "Routing group '{g_name}' TPM limit ({}) reached",
                g_tpm.unwrap_or(0)
            ),
        ),
        (
            "model",
            format!("brutor:mdl:{}:rpm", model.id),
            model.rate_limit_rpm,
            Measure::One,
            "rpm",
            format!(
                "Model '{}' RPM limit ({}) reached",
                model.model_name,
                model.rate_limit_rpm.unwrap_or(0)
            ),
        ),
        (
            "model",
            format!("brutor:mdl:{}:tpm", model.id),
            model.rate_limit_tpm,
            Measure::TotalTokens,
            "tpm",
            format!(
                "Model '{}' TPM limit ({}) reached",
                model.model_name,
                model.rate_limit_tpm.unwrap_or(0)
            ),
        ),
    ] {
        let Some(limit) = limit.filter(|l| *l > 0) else {
            continue;
        };
        dims.push(LimitDim {
            dim: Dim::bucket(key, limit as f64, limit as f64 / 60.0, measure),
            refusal: DimRefusal::Rate {
                scope,
                limit_type: label,
                what,
                retry: RateRetry::Bucket {
                    cooldown_secs: None,
                },
            },
        });
    }
    Ok(dims)
}

/// Simple shuffle: weighted random selection.
pub fn select_simple_shuffle(models: &[LlmModel]) -> Result<LlmModel, LlmRouterError> {
    let total_weight: i32 = models.iter().map(|m| m.routing_weight.unwrap_or(1)).sum();

    if total_weight <= 0 {
        return Ok(models[0].clone());
    }

    let mut rng_val = (std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .subsec_nanos() as i32)
        % total_weight;

    for model in models {
        rng_val -= model.routing_weight.unwrap_or(1);
        if rng_val < 0 {
            return Ok(model.clone());
        }
    }
    Ok(models[0].clone())
}

/// Usage-based routing v2: select the model with the lowest current TPM usage.
/// The selection logic:
///   1. Filter models not at RPM/TPM limits (already done in pre-call checks)
///   2. Select the one with lowest TPM ratio (current_tpm / tpm_limit)
///   3. Tie-break by weight
pub fn select_usage_based(
    models: &[LlmModel],
    router_state: &RouterState,
) -> Result<LlmModel, LlmRouterError> {
    let mut best: Option<&LlmModel> = None;
    let mut best_ratio = f64::MAX;

    for model in models {
        let current_tpm = router_state.get_tpm(&model.id) as f64;
        let tpm_limit = model.rate_limit_tpm.unwrap_or(1_000_000) as f64;

        // Ratio of usage — lower is better
        let ratio = if tpm_limit > 0.0 {
            current_tpm / tpm_limit
        } else {
            0.0
        };

        if ratio < best_ratio
            || (ratio == best_ratio
                && best.map_or(true, |b| {
                    model.routing_weight.unwrap_or(1) > b.routing_weight.unwrap_or(1)
                }))
        {
            best_ratio = ratio;
            best = Some(model);
        }
    }

    Ok(best.unwrap_or(&models[0]).clone())
}

/// Least-busy routing: select the model with the fewest active requests.
pub fn select_least_busy(
    models: &[LlmModel],
    router_state: &RouterState,
) -> Result<LlmModel, LlmRouterError> {
    let mut best: Option<&LlmModel> = None;
    let mut min_active = i64::MAX;

    for model in models {
        let active = router_state.get_active_requests(&model.id);
        if active < min_active
            || (active == min_active
                && best.map_or(true, |b| {
                    model.routing_weight.unwrap_or(1) > b.routing_weight.unwrap_or(1)
                }))
        {
            min_active = active;
            best = Some(model);
        }
    }

    Ok(best.unwrap_or(&models[0]).clone())
}

/// Latency-based routing: select the model with the lowest EMA latency.
pub fn select_latency_based(
    models: &[LlmModel],
    router_state: &RouterState,
) -> Result<LlmModel, LlmRouterError> {
    let mut best: Option<&LlmModel> = None;
    let mut min_latency = f64::MAX;

    for model in models {
        let avg = router_state.get_avg_latency(&model.id);
        if avg < min_latency {
            min_latency = avg;
            best = Some(model);
        }
    }

    Ok(best.unwrap_or(&models[0]).clone())
}

// =========================================================================
// Complexity-based routing (opt-in)
// =========================================================================

/// Load a routing group's tier → model-id mapping, ordered by preference.
async fn get_group_tier_models(
    db: &PgPool,
    group_id: &str,
    tenant_id: &str,
    tier: &str,
) -> Result<Vec<String>, sqlx::Error> {
    let rows: Vec<(String,)> = sqlx::query_as(
        "SELECT llm_model_id FROM llm_routing_group_tier \
         WHERE routing_group_id = $1 AND tenant_id = $2 AND tier = $3 \
         ORDER BY position ASC",
    )
    .bind(group_id)
    .bind(tenant_id)
    .bind(tier)
    .fetch_all(db)
    .await?;
    Ok(rows.into_iter().map(|(id,)| id).collect())
}

/// Does this group have any tier mapping at all?
///
/// Distinguishes "configured but this tier is empty" from "never configured",
/// which decide differently: the former escalates, the latter means the
/// strategy is inert and we fall back silently.
async fn group_has_tier_mappings(
    db: &PgPool,
    group_id: &str,
    tenant_id: &str,
) -> Result<bool, sqlx::Error> {
    let row: Option<(i64,)> = sqlx::query_as(
        "SELECT COUNT(*) FROM llm_routing_group_tier \
         WHERE routing_group_id = $1 AND tenant_id = $2",
    )
    .bind(group_id)
    .bind(tenant_id)
    .fetch_optional(db)
    .await?;
    Ok(row.map(|(n,)| n > 0).unwrap_or(false))
}

/// Build the scorer config for a group: stored overrides merged over defaults.
///
/// A malformed `complexity_config` degrades to defaults rather than failing the
/// request — a bad config value must never take inference down.
fn complexity_config_for(group: &LlmRoutingGroup) -> crate::services::complexity::ComplexityConfig {
    use crate::services::complexity::ComplexityConfig;

    let mut cfg = ComplexityConfig::default();
    let Some(raw) = group.complexity_config.as_ref() else {
        return cfg;
    };

    if let Some(b) = raw.get("boundaries") {
        if let Some(v) = b.get("simple_max").and_then(|x| x.as_f64()) {
            cfg.boundaries.simple_max = v;
        }
        if let Some(v) = b.get("medium_max").and_then(|x| x.as_f64()) {
            cfg.boundaries.medium_max = v;
        }
        if let Some(v) = b.get("complex_max").and_then(|x| x.as_f64()) {
            cfg.boundaries.complex_max = v;
        }
    }

    let str_list = |key: &str| -> Option<Vec<String>> {
        raw.get(key)?.as_array().map(|a| {
            a.iter()
                .filter_map(|s| s.as_str().map(|s| s.to_lowercase()))
                .collect()
        })
    };
    if let Some(v) = str_list("code_keywords") {
        cfg.code_keywords = v;
    }
    if let Some(v) = str_list("reasoning_keywords") {
        cfg.reasoning_keywords = v;
    }
    if let Some(v) = str_list("technical_keywords") {
        cfg.technical_keywords = v;
    }
    if let Some(v) = str_list("simple_keywords") {
        cfg.simple_keywords = v;
    }

    if let Some(w) = raw.get("weights") {
        let set = |key: &str, target: &mut f64| {
            if let Some(v) = w.get(key).and_then(|x| x.as_f64()) {
                if v.is_finite() && v >= 0.0 {
                    *target = v;
                }
            }
        };
        set("token_count", &mut cfg.weights.token_count);
        set("code_presence", &mut cfg.weights.code_presence);
        set("reasoning_markers", &mut cfg.weights.reasoning_markers);
        set("technical_terms", &mut cfg.weights.technical_terms);
        set("simple_indicators", &mut cfg.weights.simple_indicators);
        set("multi_step", &mut cfg.weights.multi_step);
        set("question_complexity", &mut cfg.weights.question_complexity);
        set("tool_presence", &mut cfg.weights.tool_presence);
    }

    cfg
}

/// Complexity-based routing: score the prompt locally, then pick from the
/// models mapped to the resulting tier.
///
/// Governance properties, all load-bearing:
///
/// * **Never widens access.** The tier's models are intersected with
///   `candidates`, which the caller has already filtered for access, health,
///   cooldown and rate limits. A tier mapping can therefore only ever *narrow*
///   what was already permitted — it cannot reach a model the caller could not
///   otherwise use, even if an operator maps one there.
/// * **Floor tier is enforced after scoring.** `complexity_floor_tier` raises
///   the tier, never lowers it — the quality control for a scorer that has no
///   confidence value to threshold.
/// * **Fails open to the group's ordinary behaviour.** No request context, no
///   tier mappings, an empty tier, or a database error all fall through to
///   usage-based selection over the full candidate list. A cost optimisation
///   must never be able to fail a request.
async fn select_complexity_based(
    db: &PgPool,
    group: &LlmRoutingGroup,
    tenant_id: &str,
    candidates: &[LlmModel],
    router_state: &RouterState,
) -> Result<LlmModel, LlmRouterError> {
    use crate::services::complexity::{self, ClassifyInput, ComplexityTier};

    // The prompt is only in scope when the preflight path opted in. Every
    // other caller gets the group's ordinary behaviour.
    let ctx = ROUTING_REQUEST_CONTEXT
        .try_with(|c| c.clone())
        .ok()
        .flatten();

    let Some(ctx) = ctx else {
        tracing::debug!(
            group_id = %group.id,
            "complexity routing: no request context in scope — using usage-based selection"
        );
        return select_usage_based(candidates, router_state);
    };

    let cfg = complexity_config_for(group);
    let verdict = complexity::classify(
        &ClassifyInput {
            user_text: &ctx.user_text,
            system_text: ctx.system_text.as_deref(),
            tool_count: ctx.tool_count,
        },
        &cfg,
    );

    // The floor raises, never lowers — `Ord` on ComplexityTier is cheapest-first.
    let floor = group
        .complexity_floor_tier
        .as_deref()
        .and_then(ComplexityTier::from_str);
    let effective_tier = match floor {
        Some(f) => verdict.tier.max(f),
        None => verdict.tier,
    };

    let signals = verdict.signals.join(",");
    tracing::debug!(
        group_id = %group.id,
        scored_tier = %verdict.tier,
        effective_tier = %effective_tier,
        score = verdict.score,
        signals = %signals,
        floored = floor.is_some_and(|f| f > verdict.tier),
        "complexity routing verdict"
    );

    let tier_model_ids =
        match get_group_tier_models(db, &group.id, tenant_id, effective_tier.as_str()).await {
            Ok(ids) => ids,
            Err(e) => {
                tracing::warn!(
                    group_id = %group.id,
                    error = %e,
                    "complexity routing: tier lookup failed — using usage-based selection"
                );
                return select_usage_based(candidates, router_state);
            }
        };

    // Intersect with the already-authorized, already-healthy candidate list.
    // This is the "never widens access" guarantee, and it is why the
    // intersection direction matters: we filter `candidates`, we do not load
    // models from the tier mapping.
    let pool: Vec<LlmModel> = candidates
        .iter()
        .filter(|m| tier_model_ids.iter().any(|id| id == &m.id))
        .cloned()
        .collect();

    if pool.is_empty() {
        // Configured-but-unavailable and never-configured are different
        // situations; only the first is worth an operator's attention.
        match group_has_tier_mappings(db, &group.id, tenant_id).await {
            Ok(true) => tracing::warn!(
                group_id = %group.id,
                tier = %effective_tier,
                "complexity routing: no healthy, permitted model mapped to this tier — \
                 using usage-based selection over the full group"
            ),
            _ => tracing::debug!(
                group_id = %group.id,
                "complexity routing: group has no tier mappings — strategy inert"
            ),
        }
        return select_usage_based(candidates, router_state);
    }

    // A tier may map to several models; the group's ordinary balancing picks
    // within the tier. The tier selects the pool, not the member.
    let mut chosen = select_usage_based(&pool, router_state)?;
    chosen.routed_via_tier = Some(effective_tier.as_str().to_string());
    chosen.routed_tier_signals = if signals.is_empty() {
        None
    } else {
        Some(signals)
    };
    Ok(chosen)
}

/// Default weight applied to a model's output-token rate when blending it with
/// the input rate for cost ranking.
///
/// Ranking on the input rate alone is wrong: output tokens are typically priced
/// 3–5× input, so a model that is cheaper per input token can be markedly more
/// expensive per request. Summing the two raw rates is the
/// other extreme — it implicitly assumes one output token per input token,
/// which no real workload produces. Chat and especially agent traffic are
/// heavily input-weighted (long system prompts, tool definitions and history
/// against a short completion), so the output rate is weighted down.
///
/// Only the *relative order* of the blend matters for selection, so a fixed
/// ratio is sufficient and a per-request estimate would be false precision.
/// Operators can override per routing group via
/// `routing_strategy_args.cost_output_ratio` (set it to `1.0` for the
/// sum-the-rates behaviour other gateways use).
pub const DEFAULT_COST_OUTPUT_RATIO: f64 = 0.3;

/// Read the output-rate weight for cost ranking from a group's
/// `routing_strategy_args`, falling back to [`DEFAULT_COST_OUTPUT_RATIO`].
///
/// Non-finite or negative values are rejected rather than propagated into the
/// comparison, where they would silently corrupt the ordering.
fn cost_output_ratio(strategy_args: Option<&serde_json::Value>) -> f64 {
    strategy_args
        .and_then(|args| args.get("cost_output_ratio"))
        .and_then(|v| v.as_f64())
        .filter(|r| r.is_finite() && *r >= 0.0)
        .unwrap_or(DEFAULT_COST_OUTPUT_RATIO)
}

/// Blended per-token rate used to rank a model for cost, or `None` when the
/// model carries no pricing at all.
///
/// A model priced on only one side is still rankable — the missing side
/// contributes nothing rather than disqualifying the model.
pub fn blended_token_cost(model: &LlmModel, output_ratio: f64) -> Option<f64> {
    match (model.cost_per_input_token, model.cost_per_output_token) {
        (None, None) => None,
        (input, output) => Some(input.unwrap_or(0.0) + output.unwrap_or(0.0) * output_ratio),
    }
}

/// Cost-based routing: select the cheapest model by blended input+output rate.
///
/// Returns `None` when **no** candidate carries pricing. The caller must then
/// fall through to another strategy: returning `models[0]` in that case (the
/// previous behaviour) presents an arbitrary pick as a cost decision, which is
/// the kind of quiet lie that costs a governance product its credibility.
///
/// Note this strategy is deterministic — with heterogeneous models it selects
/// the same model on every request, which is a static pin, not a per-request
/// optimisation. It is intended for arbitrage between *equivalent* deployments
/// (same model at two providers, reserved vs on-demand) plus failover, where
/// the health pre-filter is what makes it interesting.
pub fn select_cost_based(models: &[LlmModel], output_ratio: f64) -> Option<LlmModel> {
    let mut cheapest: Option<(&LlmModel, f64)> = None;

    for m in models {
        match blended_token_cost(m, output_ratio) {
            Some(cost) => {
                if cheapest.is_none_or(|(_, best)| cost < best) {
                    cheapest = Some((m, cost));
                }
            }
            None => {
                // Unpriced models rank last by exclusion. Warn: an operator who
                // chose cost routing and silently never sees a model is owed the
                // reason.
                tracing::warn!(
                    model_id = %m.id,
                    model_name = %m.model_name,
                    "cost-based routing: model has no input or output pricing — not \
                     considered for selection"
                );
            }
        }
    }

    cheapest.map(|(m, _)| m.clone())
}

// =========================================================================
// DB queries
// =========================================================================

/// Get routing group configuration for a model.
pub async fn get_routing_group(
    db: &PgPool,
    group_id: &str,
    tenant_id: &str,
) -> Result<Option<LlmRoutingGroup>, sqlx::Error> {
    sqlx::query_as(
        "SELECT * FROM llm_routing_group WHERE id = $1 AND tenant_id = $2 AND enabled = true",
    )
    .bind(group_id)
    .bind(tenant_id)
    .fetch_optional(db)
    .await
}

/// Get all models in a routing group (for fallback selection).
pub async fn get_group_models(
    db: &PgPool,
    group_id: &str,
    tenant_id: &str,
) -> Result<Vec<LlmModel>, sqlx::Error> {
    // Bare model load — caller stamps `provider` slug from
    // `provider_cache` before any adapter dispatch.
    sqlx::query_as(concat!(
        "SELECT * FROM llm_model WHERE routing_group_id = $1 AND tenant_id = $2 AND ",
        crate::model_available_sql!(""),
        " ORDER BY routing_weight DESC NULLS LAST"
    ))
    .bind(group_id)
    .bind(tenant_id)
    .fetch_all(db)
    .await
}

// Fallback has two sources, and both are read where a call fails over:
// the routing group's (`fallback_models`, `default_fallbacks` and the two
// conditional lists on `llm_routing_groups`) for a call routed through a
// group, and the model's own `llm_model.fallback_models` chain
// (`resolve_fallback_chain`), which a direct call walks too. A candidate is
// served only if it is available (enabled, not retired, provider enabled).

// =========================================================================
// Provider helpers
// =========================================================================

/// Build the upstream provider URL for a model (delegates to providers module).
///
/// `stream` matters only for providers with distinct streaming URLs
/// (Gemini's `:streamGenerateContent`). Pass `false` for plain JSON
/// responses; pass `true` for SSE.
pub fn build_provider_url(model: &LlmModel, endpoint: &str, stream: bool) -> String {
    providers::build_url(model, endpoint, stream)
}

/// Build authorization headers for the upstream provider (delegates to providers module).
pub fn build_provider_headers(model: &LlmModel) -> Vec<(String, String)> {
    providers::build_headers(model)
}

// =========================================================================
// System prompt injection
// =========================================================================
//
// The canonical implementation lives in `services::providers::inject_system_prompt`
// and is invoked as part of `providers::prepare_request` — see the call
// sites in `forward_chat_completion*` below. The local copy that used
// to live here was a vestigial duplicate; under the merge
// semantics it would double-prepend.

// =========================================================================
// Forward chat completion (non-streaming)
// =========================================================================

/// Forward a chat completion request to the upstream provider.
/// Integrates RouterState for:
///   - RPM increment
///   - System prompt injection
///   - Active request tracking
///   - Response caching
pub async fn forward_chat_completion(
    client: &Client,
    model: &LlmModel,
    request: &ChatCompletionRequest,
    stream: bool,
    router_state: &RouterState,
) -> Result<(Value, u16), LlmRouterError> {
    forward_chat_completion_impl(client, model, request, stream, router_state, None).await
}

/// Variant that takes the request's prompt-cache session
/// (`services::prompt_caching`). The session, when there is one, is asked
/// to mark the outbound body for the provider's cache; what it marks and
/// how is the session's business. Used by the chat/completions entry
/// point, which opens the session per request before dispatching.
pub async fn forward_chat_completion_with_prompt_cache(
    client: &Client,
    model: &LlmModel,
    request: &ChatCompletionRequest,
    stream: bool,
    router_state: &RouterState,
    prompt_cache: Option<&dyn PromptCacheSession>,
) -> Result<(Value, u16), LlmRouterError> {
    forward_chat_completion_impl(client, model, request, stream, router_state, prompt_cache).await
}

async fn forward_chat_completion_impl(
    client: &Client,
    model: &LlmModel,
    request: &ChatCompletionRequest,
    stream: bool,
    router_state: &RouterState,
    prompt_cache: Option<&dyn PromptCacheSession>,
) -> Result<(Value, u16), LlmRouterError> {
    // The dispatch layer of the response cache, when the route scoped one
    // (`response_cache::with_dispatch_cache`). Which requests it answers,
    // for whom and for how long is the cache's business; with none
    // installed, or for a caller the cache declined, there is nothing here
    // and the request goes upstream. A streamed answer is never read from
    // or written to it.
    let dispatch_cache = if stream {
        None
    } else {
        crate::services::response_cache::current_dispatch_cache()
    };
    if let Some(cache) = dispatch_cache.as_ref()
        && let Some((cached_resp, cached_status)) = cache.lookup(router_state, model, request)
    {
        tracing::debug!(
            model = %model.model_name,
            "Cache hit for chat completion"
        );
        return Ok((cached_resp, cached_status));
    }

    // A member or fallback the gateway cannot dispatch to is not
    // sent anywhere; the caller moves on to the next candidate.
    if let Some(refusal) = providers::dispatch_refusal(model, providers::ProviderApi::ChatForward) {
        return Err(LlmRouterError::UpstreamError(refusal.reason));
    }
    // A Bedrock row served through Converse (`bedrock_runtime`): its own
    // URL, a SigV4 signature in place of the adapter's headers, and the
    // body translated once it is final. No cloud credential of a row is
    // ever put in a header; a row with a cloud `auth_type` this
    // forwarder cannot authenticate was refused above.
    let converse = crate::services::bedrock_runtime::serves_chat(model);
    let mut url = providers::build_url(model, "/chat/completions", stream && !converse);
    let mut auth_headers = if converse {
        Vec::new()
    } else {
        providers::build_headers(model)
    };

    let mut body = serde_json::to_value(request)
        .map_err(|e| LlmRouterError::SerializationError(e.to_string()))?;

    // `prepare_request` runs the full pipeline including
    // `inject_system_prompt` (which now merges server + caller systems
    // rather than skipping when a caller-supplied system is present).
    providers::prepare_request(&mut body, model);

    // Provider-side prompt caching, when the caller opened a session for
    // it (`services::prompt_caching`). Asked AFTER `prepare_request`, so
    // that when the Anthropic adapter has moved `system` messages to the
    // top-level `system` field the marks travel with the content. What is
    // marked, and how, is the session's business; the caller reads the
    // actual cache usage from the response body.
    if let Some(session) = prompt_cache
        && let Some(decision) = session.mark(&mut body, model).await
    {
        crate::services::prompt_caching::record_decision(&decision);
        if decision.applied {
            tracing::debug!(
                provider = %model.provider,
                breakpoints = decision.breakpoints_inserted,
                "Prompt cache markers applied",
            );
        } else {
            tracing::debug!(
                provider = %model.provider,
                reason = decision.reason,
                "Prompt cache skipped",
            );
        }
    }

    // Set stream flag.
    //
    // Most providers accept `stream: bool` as a body field. Gemini's
    // native REST API rejects it with 400 ("Unknown name 'stream'") —
    // streaming is encoded entirely in the URL path
    // (`:streamGenerateContent` vs `:generateContent`). We use that
    // routing already (see `GeminiAdapter::chat_endpoint`), so the
    // body field is both redundant and harmful here.
    let body_takes_stream_flag =
        !converse && !matches!(model.provider.as_str(), "gemini" | "google_ai_studio");
    if body_takes_stream_flag {
        if let Some(obj) = body.as_object_mut() {
            obj.insert("stream".into(), Value::Bool(stream));
        }
    }

    tracing::debug!(
        provider = %model.provider,
        model_name = %model.model_name,
        url = %url,
        stream,
        has_tools = body.get("tools").and_then(|t| t.as_array()).map_or(false, |a| !a.is_empty()),
        has_system = body.get("messages").and_then(|m| m.as_array()).map_or(false, |msgs| {
            msgs.iter().any(|m| m.get("role").and_then(|r| r.as_str()) == Some("system"))
        }),
        max_tokens = ?body.get("max_tokens").and_then(|v| v.as_i64()),
        "Forwarding chat completion request"
    );

    // Converse: the prepared body becomes the Converse body, signed as the
    // bytes that are sent. A request the model cannot serve (a feature it
    // does not have) is not sent; as a fallback or a member the caller
    // moves on to the next candidate.
    let signed_body: Option<Vec<u8>> = if converse {
        let wire = crate::services::bedrock_runtime::converse::to_converse(&body, model, false)
            .map_err(LlmRouterError::UpstreamError)?;
        let call = crate::services::bedrock_runtime::signed_call(model, "converse", &wire)
            .map_err(LlmRouterError::UpstreamError)?;
        url = call.url;
        auth_headers = call.headers;
        body = wire;
        Some(call.body)
    } else {
        None
    };

    // Increment RPM counter
    router_state.increment_rpm(&model.id);

    // Track active request
    router_state.begin_request(&model.id);

    let timeout_secs = model.timeout.map(|t| t as u64).unwrap_or(120);

    // Inline single retry, only where the provider provably did NOT
    // process the request. Further retries belong in
    // `forward_with_fallbacks`.
    //
    // Retried (rejected or never reached the origin):
    //   529  Anthropic "Overloaded" (refused before generation)
    //   503  Service Unavailable
    //   521  Cloudflare "Web server is down"
    //   522  Cloudflare "Connection timed out" (to the origin)
    //   523  Cloudflare "Origin is unreachable"
    //
    // NOT retried, recorded as ambiguous: the origin had the request and
    // may have generated, and billed, a completion we never saw. Retrying
    // could buy it twice with the first cost never recorded.
    //   502  Bad Gateway
    //   504  Gateway Timeout
    //   520  Cloudflare "Web server returned an unknown error"
    //   524  Cloudflare "A timeout occurred" (origin accepted, did not answer)
    //
    // A timeout after sending is ambiguous for the same reason.
    fn is_unprocessed(status: u16) -> bool {
        matches!(status, 503 | 521 | 522 | 523 | 529)
    }

    // Capture the exact body we're about to POST upstream (resolved
    // model, provider-translated shape, injected system prompt, prompt
    // -cache markers) so the route can log what the provider actually
    // received. `body` is final here — all mutations are applied above.
    record_outbound_body(&body);

    let mut status: u16 = 0;
    let mut resp_body: Value = Value::Null;
    let mut last_err: Option<String> = None;
    let mut sent_and_lost = false;
    // Bedrock names its error in a header.
    let mut bedrock_error_type: Option<String> = None;
    for attempt in 0u8..2 {
        // `.timeout(..)` bounds the WHOLE request (connect + send +
        // upstream generation). The TCP/TLS connect phase is bounded
        // separately by the shared client's `connect_timeout` (10s, set
        // in `main.rs`) — `connect_timeout` is a client-level setting and
        // cannot be overridden per-request, so an unreachable host fails
        // in ~10s instead of hanging to the full `timeout_secs` deadline.
        let mut req = client
            .post(&url)
            .header("Content-Type", "application/json")
            .timeout(Duration::from_secs(timeout_secs));
        for (k, v) in &auth_headers {
            req = req.header(k.as_str(), v.as_str());
        }
        // Emit the verified call-chain
        // headers on every outbound LLM upstream call. Read from the
        // task-local set by the route handler at request entry. The
        // upstream provider doesn't care about these (OpenAI, Anthropic,
        // etc. ignore unknown headers) — they're for downstream Brutor
        // peers if/when the response is itself an agent-to-agent step.
        if let Some(pairs) = crate::services::call_chain::current_outbound_headers() {
            for (k, v) in pairs {
                req = req.header(k, v);
            }
        }
        let req = match &signed_body {
            // The bytes the signature covers.
            Some(bytes) => req.body(bytes.clone()),
            None => req.json(&body),
        };
        let send_result = req.send().await;
        match send_result {
            Ok(resp) => {
                // The vendor's own request id, before the body is consumed.
                crate::middleware::run_end::record_upstream_response(
                    resp.status().as_u16(),
                    resp.headers(),
                );
                if converse {
                    bedrock_error_type = resp
                        .headers()
                        .get("x-amzn-errortype")
                        .and_then(|v| v.to_str().ok())
                        .map(str::to_string);
                }
                let s = resp.status().as_u16();
                let parsed: Result<Value, _> = resp.json().await;
                match parsed {
                    Ok(v) => {
                        status = s;
                        resp_body = v;
                        last_err = None;
                    }
                    Err(e) => {
                        last_err = Some(format!("Failed to parse response: {e}"));
                        status = s;
                    }
                }
            }
            Err(e) => {
                sent_and_lost = !e.is_connect();
                if !e.is_connect() {
                    crate::services::evidence::request::record_ambiguous_upstream(
                        if e.is_timeout() {
                            "llm: timeout after the request was sent"
                        } else {
                            "llm: transport error after the request was sent"
                        },
                    );
                }
                last_err = Some(e.to_string());
            }
        }

        if is_ambiguous_status(status) {
            crate::services::evidence::request::record_ambiguous_upstream(&format!(
                "llm: HTTP {status} after the request was sent"
            ));
        }
        if is_unprocessed(status) && attempt == 0 {
            tracing::warn!(
                provider = %model.provider,
                model_name = %model.model_name,
                status,
                "Upstream returned transient gateway error — retrying once after 500ms"
            );
            tokio::time::sleep(Duration::from_millis(500)).await;
            continue;
        }
        break;
    }

    // End active request tracking
    router_state.end_request(&model.id);

    if let Some(e) = last_err {
        if sent_and_lost {
            return Err(LlmRouterError::UpstreamAmbiguous(e));
        }
        return Err(LlmRouterError::UpstreamError(e));
    }

    // Normalize non-OpenAI response formats to OpenAI chat completion format
    if converse {
        resp_body = if (200..300).contains(&status) {
            // A 2xx that is not a Converse answer, or has no usage, is a
            // provider failure, not an empty completion.
            crate::services::bedrock_runtime::converse::response_to_openai(&resp_body, model)
                .map_err(LlmRouterError::UpstreamError)?
        } else {
            crate::services::bedrock_runtime::error_body(
                status,
                &resp_body,
                bedrock_error_type.as_deref(),
            )
        };
    }
    if (200..300).contains(&status) {
        if !converse {
            providers::normalize_response(&model.provider, &mut resp_body);
        }

        let usage = resp_body.get("usage");
        let prompt_tokens = usage
            .and_then(|u| u.get("prompt_tokens"))
            .and_then(|v| v.as_i64());
        let completion_tokens = usage
            .and_then(|u| u.get("completion_tokens"))
            .and_then(|v| v.as_i64());

        tracing::debug!(
            provider = %model.provider,
            model_name = %model.model_name,
            status,
            prompt_tokens = ?prompt_tokens,
            completion_tokens = ?completion_tokens,
            "Upstream response OK"
        );

        // Track TPM usage (per-model, and the routing group if this
        // request was routed through one — keeps the group-level TPM counter
        // that `select_from_routing_group` checks in sync).
        if let Some(total) = prompt_tokens.zip(completion_tokens).map(|(p, c)| p + c) {
            router_state.add_tpm(&model.id, total);
            if let Some(ref rg) = model.routed_via_group_id {
                router_state.add_tpm(rg, total);
            }
        }

        // A successful answer is offered to the dispatch layer of the
        // response cache, if this request has one (see the lookup above).
        if let Some(cache) = dispatch_cache.as_ref() {
            cache.store(router_state, model, request, &resp_body, status);
        }
    } else {
        tracing::warn!(
            provider = %model.provider,
            model_name = %model.model_name,
            status,
            error = %resp_body.to_string().chars().take(500).collect::<String>(),
            "Upstream response error"
        );
    }

    Ok((resp_body, status))
}

// =========================================================================
// Fallback resolution
// =========================================================================

/// Return true when the candidate
/// fallback model should be skipped because its provider's
/// region isn't allowed by the tenant's residency profile.
///
/// `None` profile → never blocks (no enforcement).
/// Provider cache miss → treats region as unspecified, which the
/// residency check itself decides (a no-profile tenant or a
/// `block_global=false` profile lets it pass).
async fn residency_blocks_candidate(
    profile: Option<&crate::services::residency::ResidencyProfile>,
    provider_cache: Option<&std::sync::Arc<crate::services::cache::ProviderCache>>,
    tenant_id: &str,
    candidate: &LlmModel,
) -> bool {
    let Some(profile) = profile else {
        return false;
    };
    let region = match provider_cache {
        Some(pc) => pc
            .get(tenant_id, &candidate.provider_id)
            .await
            .and_then(|p| p.region.clone()),
        None => None,
    };
    match crate::services::residency::check(Some(profile), region.as_deref()) {
        Some(crate::services::residency::ResidencyDecision::Blocked { reason }) => {
            tracing::debug!(
                tenant_id,
                candidate = %candidate.model_name,
                provider = %candidate.provider,
                candidate_region = ?region,
                %reason,
                "Fallback candidate skipped — residency block",
            );
            true
        }
        _ => false,
    }
}

/// Try to resolve a fallback from a JSON array of model/group names.
async fn try_fallback_list(
    client: &Client,
    db: &PgPool,
    tenant_id: &str,
    fallback_list: &serde_json::Value,
    request: &ChatCompletionRequest,
    stream: bool,
    max_attempts: usize,
    router_state: &RouterState,
    provider_cache: Option<&std::sync::Arc<crate::services::cache::ProviderCache>>,
    // Encryption service to decrypt each fallback candidate's at-rest
    // api_key / provider_config. Without it, a candidate loaded raw here keeps
    // its Fernet ciphertext and the upstream 401s. `None` only on legacy/test
    // paths (candidate then used as-is, matching prior behaviour).
    encryption: Option<&EncryptionService>,
    // Tenant residency profile. When set,
    // each candidate fallback model's provider region is checked
    // against the profile BEFORE forward_chat_completion is
    // invoked. Blocked candidates are skipped (tracing::debug)
    // without consuming an attempt — a tenant under strict
    // residency would otherwise exhaust max_fallbacks on bad
    // candidates and produce a misleading "no fallback succeeded"
    // outcome when the right answer is "no fallback was allowed".
    residency_profile: Option<&crate::services::residency::ResidencyProfile>,
) -> Option<(Value, u16, LlmModel)> {
    let arr = fallback_list.as_array()?;
    let mut attempts = 0;
    tracing::debug!(
        fallback_count = arr.len(),
        max_attempts,
        residency_enabled = residency_profile.is_some(),
        "Trying fallback list"
    );

    for item in arr {
        if attempts >= max_attempts {
            break;
        }

        // Item can be a string (model/group name) or an object {group_name: [fallback_names]}
        let names: Vec<&str> = if let Some(s) = item.as_str() {
            vec![s]
        } else if let Some(obj) = item.as_object() {
            obj.values()
                .filter_map(|v| v.as_array())
                .flat_map(|arr| arr.iter().filter_map(|v| v.as_str()))
                .collect()
        } else {
            continue;
        };

        for name in names {
            if attempts >= max_attempts {
                break;
            }
            attempts += 1;

            // Try as routing group first
            let group: Option<LlmRoutingGroup> = sqlx::query_as(
                "SELECT * FROM llm_routing_group WHERE (id = $1 OR name = $1) AND tenant_id = $2 AND enabled = true",
            )
            .bind(name)
            .bind(tenant_id)
            .fetch_optional(db)
            .await
            // Degrade (logged and counted): an unreadable fallback is skipped; the
            // request still fails or succeeds on its other candidates.
            .or_logged(None);

            if let Some(grp) = group {
                if let Ok(mut fb_model) =
                    select_from_routing_group(db, &grp, tenant_id, router_state).await
                {
                    if let Some(pc) = provider_cache {
                        pc.stamp_one(&mut fb_model).await;
                    }
                    // The member came back raw; decrypt its key.
                    if let Some(enc) = encryption {
                        open_model_credentials(&mut fb_model, enc);
                    }
                    // Residency check before
                    // forwarding. Look up the candidate's provider
                    // region via the cache and intersect with the
                    // tenant's profile. Blocked → skip to next
                    // candidate without consuming an attempt slot.
                    if residency_blocks_candidate(
                        residency_profile,
                        provider_cache,
                        tenant_id,
                        &fb_model,
                    )
                    .await
                    {
                        attempts = attempts.saturating_sub(1);
                        continue;
                    }
                    // A candidate whose endpoint the outbound
                    // policy denies is skipped, never dispatched.
                    if guard_model_url(&fb_model).is_err() {
                        attempts = attempts.saturating_sub(1);
                        continue;
                    }
                    if let Ok((fb_body, fb_status)) =
                        forward_chat_completion(client, &fb_model, request, stream, router_state)
                            .await
                    {
                        if (200..300).contains(&fb_status) {
                            router_state.record_success(&fb_model.id);
                            return Some((fb_body, fb_status, fb_model));
                        } else {
                            // Record failure for cooldown
                            let cooldown_secs = grp.cooldown_time.unwrap_or(60);
                            router_state.record_failure(
                                &fb_model.id,
                                fb_status,
                                cooldown_secs,
                                "Fallback attempt failed",
                            );
                        }
                    }
                }
                continue;
            }

            // Try as direct model
            let fb_model: Option<LlmModel> = sqlx::query_as(concat!(
                "SELECT * FROM llm_model \
                     WHERE (model_name = $1 OR id = $1) AND tenant_id = $2 AND ",
                crate::model_available_sql!("")
            ))
            .bind(name)
            .bind(tenant_id)
            .fetch_optional(db)
            .await
            // Degrade (logged and counted): an unreadable fallback is skipped; the
            // request still fails or succeeds on its other candidates.
            .or_logged(None);

            if let Some(mut fb) = fb_model {
                if let Some(pc) = provider_cache {
                    pc.stamp_one(&mut fb).await;
                }
                // Decrypt the candidate's at-rest api_key /
                // provider_config, mirroring the primary model's path
                // (`resolve_model`/`ModelCache`). Without this the adapter sends
                // the Fernet ciphertext as the key and the upstream returns 401.
                if let Some(enc) = encryption {
                    open_model_credentials(&mut fb, enc);
                }
                // Residency check before
                // forwarding. Same semantics as the routing-group
                // branch above.
                if residency_blocks_candidate(residency_profile, provider_cache, tenant_id, &fb)
                    .await
                {
                    attempts = attempts.saturating_sub(1);
                    continue;
                }
                // Same for a direct fallback model.
                if guard_model_url(&fb).is_err() {
                    attempts = attempts.saturating_sub(1);
                    continue;
                }
                if let Ok((fb_body, fb_status)) =
                    forward_chat_completion(client, &fb, request, stream, router_state).await
                {
                    if (200..300).contains(&fb_status) {
                        router_state.record_success(&fb.id);
                        return Some((fb_body, fb_status, fb));
                    } else {
                        router_state.record_failure(
                            &fb.id,
                            fb_status,
                            60,
                            "Fallback attempt failed",
                        );
                    }
                }
            }
        }
    }
    None
}

// =========================================================================
// Model-level fallback for NON-chat modes (embeddings / images / audio)
// =========================================================================
//
// These modes don't go through routing groups (those are chat-only), so a
// model's own ordered `fallback_models` chain is their only failover. The two
// helpers below are mode-agnostic: `resolve_fallback_chain` turns the stored
// id list into ready-to-call (decrypted) models, and `send_first_success`
// walks [primary, ...fallbacks] with a caller-supplied request builder,
// returning the first 2xx.

/// Outcome of `send_first_success`: either an HTTP response (the winning 2xx,
/// or the last non-2xx if every candidate failed) or a transport error from
/// the last candidate. `used_fallback` is true when the winning/last model was
/// not the primary. The RAW `reqwest::Response` is handed back so each mode can
/// consume it its own way (JSON for chat/embeddings, bytes for audio/images).
pub enum FallbackSend {
    Response {
        model: LlmModel,
        response: reqwest::Response,
        used_fallback: bool,
    },
    Transport {
        model: LlmModel,
        error: reqwest::Error,
        used_fallback: bool,
    },
}

/// Whether a non-2xx status warrants trying the next fallback model. We fail
/// over on anything that means "this model/endpoint is unavailable or broken"
/// (auth, rate-limit, server, timeout, conflict) but NOT on a malformed
/// request (400/422) — those are the caller's fault and every model rejects
/// them, so retrying just burns quota.
fn is_fallback_worthy(status: u16) -> bool {
    !matches!(status, 400 | 422)
}

/// Resolve + decrypt a model's `fallback_models` id chain into ready-to-call
/// models, preserving order. Silently skips ids that don't resolve. Empty when
/// the model has no chain.
pub async fn resolve_fallback_chain(
    db: &PgPool,
    tenant_id: &str,
    primary: &LlmModel,
    router_state: &RouterState,
    encryption: Option<&EncryptionService>,
    model_cache: Option<&crate::services::cache::ModelCache>,
    provider_cache: Option<&std::sync::Arc<crate::services::cache::ProviderCache>>,
    // The provider API the chain is walked on.
    api: providers::ProviderApi,
) -> Vec<LlmModel> {
    let mut out = Vec::new();
    let Some(arr) = primary.fallback_models.as_ref().and_then(|v| v.as_array()) else {
        return out;
    };
    for item in arr {
        let Some(id) = item.as_str() else { continue };
        // Candidates are stored as model ids; resolve_model decrypts the key +
        // redirects a routing-group id to a live member. Fall back to a
        // model_name lookup if the id form misses (robustness / hand-edited config).
        let resolved = resolve_model(
            db,
            tenant_id,
            None,
            Some(id),
            router_state,
            encryption,
            model_cache,
            provider_cache,
        )
        .await
        .or(resolve_model(
            db,
            tenant_id,
            Some(id),
            None,
            router_state,
            encryption,
            model_cache,
            provider_cache,
        )
        .await);
        match resolved {
            // The dispatch check of the requested model, on each candidate:
            // one the gateway cannot send is not a candidate.
            Ok(m) => match providers::dispatch_refusal(&m, api) {
                None => out.push(m),
                Some(refusal) => tracing::warn!(
                    fallback_id = id,
                    provider = %m.provider,
                    error_type = refusal.error_type,
                    "model fallback candidate cannot be dispatched to — skipping"
                ),
            },
            Err(_) => tracing::warn!(
                fallback_id = id,
                "model fallback candidate did not resolve — skipping"
            ),
        }
    }
    out
}

/// Try `build(model).send()` against each candidate in order, returning the
/// first 2xx. On a fallback-worthy failure it records the failure (cooldown)
/// and tries the next; on 400/422 it stops (all models would reject it).
/// `candidates` must be non-empty (index 0 = primary).
pub async fn send_first_success<F>(
    candidates: &[LlmModel],
    router_state: &RouterState,
    build: F,
) -> FallbackSend
where
    F: Fn(&LlmModel) -> reqwest::RequestBuilder,
{
    send_first_success_with(candidates, router_state, |model| {
        CandidateSend::Request(Box::new(build(model)))
    })
    .await
}

/// How one candidate of [`send_first_success_with`] is asked.
pub enum CandidateSend {
    /// One request.
    Request(Box<reqwest::RequestBuilder>),
    /// A candidate that is not one request: the future answers its response
    /// however it obtains it (a Bedrock embedding row is one InvokeModel
    /// call per text, `bedrock_runtime::embeddings`).
    Deferred(
        std::pin::Pin<
            Box<dyn std::future::Future<Output = Result<reqwest::Response, reqwest::Error>> + Send>,
        >,
    ),
}

/// [`send_first_success`] where a candidate may be more than one request.
pub async fn send_first_success_with<F>(
    candidates: &[LlmModel],
    router_state: &RouterState,
    send: F,
) -> FallbackSend
where
    F: Fn(&LlmModel) -> CandidateSend,
{
    let mut last_transport: Option<FallbackSend> = None;
    let n = candidates.len();
    for (idx, model) in candidates.iter().enumerate() {
        let used_fallback = idx > 0;
        let is_last = idx + 1 == n;
        let sent = match send(model) {
            CandidateSend::Request(request) => request.send().await,
            CandidateSend::Deferred(answer) => answer.await,
        };
        match sent {
            Ok(resp) => {
                // Recorded per attempt, so the id belongs to the model
                // that actually answered rather than the first one tried.
                crate::middleware::run_end::record_upstream_response(
                    resp.status().as_u16(),
                    resp.headers(),
                );
                let status = resp.status().as_u16();
                if (200..300).contains(&status) {
                    if used_fallback {
                        router_state.record_success(&model.id);
                    }
                    return FallbackSend::Response {
                        model: model.clone(),
                        response: resp,
                        used_fallback,
                    };
                }
                router_state.record_failure(&model.id, status, 60, "model-level fallback");
                // Return this failing response to the caller when it's the last
                // candidate or the status isn't worth failing over (400/422);
                // otherwise drop it (body unread) and try the next model.
                if is_last || !is_fallback_worthy(status) {
                    return FallbackSend::Response {
                        model: model.clone(),
                        response: resp,
                        used_fallback,
                    };
                }
            }
            Err(e) => {
                router_state.record_failure(&model.id, 0, 60, "model-level fallback (transport)");
                last_transport = Some(FallbackSend::Transport {
                    model: model.clone(),
                    error: e,
                    used_fallback,
                });
            }
        }
    }
    // Reached only when every candidate transport-errored (a non-2xx on the
    // last candidate returns in-loop). `candidates` is non-empty, so this holds
    // the last transport error.
    last_transport.expect("send_first_success requires a non-empty candidate list")
}

// =========================================================================
// Forward with fallbacks (non-streaming)
// =========================================================================

/// Forward a chat completion request with fallback chain.
/// Tries the primary model first, then falls back using:
///   1. Model-level context_window_fallback (on context window errors)
///   2. Routing group context_window_fallbacks
///   3. Routing group content_policy_fallbacks (on content policy errors)
///   4. Model-level fallback_models
///   5. Routing group fallback_models
///   6. Routing group default_fallbacks
///
/// Integrates RouterState for cooldown tracking on failures/successes.
pub async fn forward_with_fallbacks(
    client: &Client,
    db: &PgPool,
    primary_model: &LlmModel,
    request: &ChatCompletionRequest,
    stream: bool,
    router_state: &RouterState,
    // The request's prompt-cache session (`services::prompt_caching`), or
    // `None` for a caller that is not asking for prompt caching.
    prompt_cache: Option<&dyn PromptCacheSession>,
    resilience: Option<&crate::services::SharedState>,
    // Entitlement group the caller was authorized under — carries the
    // RG concurrency cap (`llm_global_limits.concurrency`) that the gate
    // enforces at group + model scope. `None` on legacy/test paths.
    target_group: Option<&crate::models::db::ResourceGroup>,
) -> Result<(Value, u16, LlmModel, bool), LlmRouterError> {
    let tenant_id = &primary_model.tenant_id;

    // Load the tenant's residency profile
    // once per fallback walk. The profile is shared across all
    // candidate evaluations (`try_fallback_list` consumes it by
    // reference) so the runtime cost is one PK lookup per chain
    // run regardless of how many fallback candidates are tried.
    let residency_profile = crate::services::residency::load_tenant_profile(db, tenant_id).await;
    let residency_profile_ref = residency_profile.as_ref();

    // Load routing group if model belongs to one
    let routing_group = if let Some(ref gid) = primary_model.routing_group_id {
        get_routing_group(db, gid, tenant_id).await.unwrap_or(None)
    } else {
        None
    };

    // Sticky-routing pin lookup. If a sibling model in the same
    // routing group already warmed a provider-side prompt cache for
    // this prefix within the TTL window, swap it in as the primary —
    // round-robin / weighted routing would otherwise cold-start the
    // cache on every call and torpedo hit rate. The pin is bounded by
    // policy.ttl_seconds and self-expires; clear-on-failure below
    // ensures a dead pin never wedges traffic.
    let sticky_pin: Option<LlmModel> = match prompt_cache {
        Some(session) => session.pinned_model(primary_model, request).await,
        None => None,
    };

    // Decrypt the pinned model before it can become the
    // primary.
    //
    // The session returns the sibling as it is stored, untouched, but `api_key` and `provider_config` are
    // Fernet-encrypted at rest. Every other load path decrypts — `resolve_model`
    // does, and `try_fallback_list` does so explicitly for this exact reason:
    // otherwise the adapter sends the ciphertext as the bearer token, the
    // provider 401s, and the breaker then opens on a model that was perfectly
    // healthy. The sticky path was the one that never got the treatment.
    //
    // With no `SharedState` (legacy and test call paths) there is no key to
    // decrypt with, so the pin is declined rather than honoured: falling back
    // to the original primary costs a warm provider-side cache, while
    // dispatching ciphertext costs a guaranteed 401 and a tripped breaker.
    let sticky_pin: Option<LlmModel> = match (sticky_pin, resilience) {
        (Some(mut pinned), Some(state)) => {
            // The pin must clear residency before it can
            // become the primary.
            //
            // Every other way a model is chosen runs this check: the fallback
            // walk skips a candidate whose provider region the tenant's
            // profile forbids. The sticky path swapped in a sibling from the
            // same routing group without asking, so a prompt-cache warm-up
            // could quietly move a tenant's traffic to a region they had
            // explicitly blocked — and residency is the one control whose
            // whole value is that it is not best-effort.
            if residency_blocks_candidate(
                residency_profile_ref,
                Some(&state.provider_cache),
                tenant_id,
                &pinned,
            )
            .await
            {
                tracing::warn!(
                    tenant_id,
                    pinned_model = %pinned.model_name,
                    "sticky pin declined — the pinned model's region is blocked by the \
                     tenant's residency profile"
                );
                None
            } else if guard_model_url(&pinned).is_err() {
                // The pinned model's endpoint is refused by the
                // outbound policy; decline the pin.
                None
            } else {
                open_model_credentials(&mut pinned, &state.encryption);
                Some(pinned)
            }
        }
        (Some(pinned), None) => {
            tracing::warn!(
                pinned_model = %pinned.model_name,
                "sticky pin resolved but no encryption service is in scope — \
                 declining the pin rather than dispatching an encrypted key"
            );
            None
        }
        (None, _) => None,
    };

    // Effective primary: either the pinned model or the original.
    let primary_model: &LlmModel = sticky_pin.as_ref().unwrap_or(primary_model);
    if let Some(ref pinned) = sticky_pin {
        tracing::debug!(
            tenant = %tenant_id,
            pinned_model = %pinned.model_name,
            "Sticky-routing pin honoured — overriding primary"
        );
    }

    let max_fallbacks = routing_group
        .as_ref()
        .and_then(|g| g.max_fallbacks)
        .unwrap_or(5) as usize;

    let cooldown_time = routing_group
        .as_ref()
        .and_then(|g| g.cooldown_time)
        .unwrap_or(60);

    // Determine retry count from group config
    let num_retries = routing_group
        .as_ref()
        .and_then(|g| g.num_retries)
        .unwrap_or(0) as usize;

    tracing::debug!(
        model = %primary_model.model_name,
        provider = %primary_model.provider,
        routing_group = ?routing_group.as_ref().map(|g| g.name.as_str()),
        num_retries,
        max_fallbacks,
        stream,
        "Starting forward_with_fallbacks"
    );

    // Provider slug resolution for fallback models loaded inside
    // `try_fallback_list`. We pull the cache off `resilience` (i.e.
    // `AppState`) so the fallback path matches the primary path's
    // adapter dispatch without an extra DB JOIN. `None` here is the
    // legacy path (rare — only test harnesses pass `resilience=None`)
    // and just leaves `model.provider` empty; the dispatcher's
    // unknown-slug branch is already defensive.
    let provider_cache_for_fallbacks = resilience.map(|s| &s.provider_cache);

    // Encryption service for decrypting a fallback candidate's at-rest api_key /
    // provider_config. The primary model is decrypted upstream by
    // `resolve_model`/`ModelCache`, but fallback candidates are loaded raw inside
    // `try_fallback_list`, so they MUST be decrypted there — otherwise the
    // adapter sends the Fernet ciphertext as the key and the upstream 401s.
    let encryption_for_fallbacks = resilience.map(|s| &s.encryption);

    // Gate the primary via the resilience layer (health check + circuit
    // breaker + concurrency limiter). On rejection we skip the primary
    // retry loop entirely and fall through to fallback selection —
    // burning retries against a known-bad upstream wastes latency
    // budget and amplifies load on a recovering provider.
    //
    // `resilience = None` preserves legacy behaviour for callers that
    // don't have SharedState handy (tests, programmatic clients).
    let primary_gate = match resilience {
        Some(state) => {
            match crate::services::llm_resilience::gate_dispatch(
                state,
                primary_model,
                tenant_id,
                target_group,
            )
            .await
            {
                Ok(guard) => Some(Ok(guard)),
                Err(rej) => {
                    tracing::debug!(
                        model = %primary_model.model_name,
                        reason = %rej,
                        "Primary model rejected at dispatch — failing over",
                    );
                    Some(Err(rej))
                }
            }
        }
        None => None,
    };

    // Try primary model (with retries) unless the gate refused us.
    let primary_attempt_allowed = !matches!(primary_gate, Some(Err(_)));
    let mut last_result: Option<Result<(Value, u16), LlmRouterError>> = None;
    let primary_was_gate_rejected = matches!(primary_gate, Some(Err(_)));

    if primary_attempt_allowed {
        for attempt in 0..=num_retries {
            if attempt > 0 {
                // Retry delay from group config
                let delay_ms = routing_group
                    .as_ref()
                    .and_then(|g| g.retry_after)
                    .unwrap_or(1) as u64
                    * 1000;
                tokio::time::sleep(Duration::from_millis(delay_ms.min(5000))).await;
                tracing::debug!(model = %primary_model.model_name, attempt, "Retrying primary model");
            }

            match forward_chat_completion_with_prompt_cache(
                client,
                primary_model,
                request,
                stream,
                router_state,
                prompt_cache,
            )
            .await
            {
                Ok((body, status)) if (200..300).contains(&status) => {
                    // Record success — clears cooldown
                    router_state.record_success(&primary_model.id);
                    // Feed the resilience layer so the breaker resets.
                    if let Some(Ok(guard)) = primary_gate {
                        guard.record_success();
                    }
                    return Ok((body, status, primary_model.clone(), false));
                }
                result => {
                    // After an outcome that may have been billed, do
                    // not buy it again from the same model.
                    let ambiguous = outcome_is_ambiguous(&result);
                    last_result = Some(result);
                    if ambiguous {
                        tracing::warn!(
                            model = %primary_model.model_name,
                            attempt,
                            "Upstream outcome unknown (may have been billed); not retrying this model"
                        );
                        break;
                    }
                }
            }
        }
        // All primary retries exhausted — feed failure to the breaker
        // once so it can trip / stay tripped for the next call.
        //
        // A malformed request (400/422) is the caller's fault, not a sign
        // the upstream is unhealthy — the rule the router's own cooldown
        // already applies in `should_cooldown`. Counting it here let a
        // handful of bad requests open the breaker and gate the model for
        // every other caller (seen live: Anthropic answering 400 for a
        // retired `temperature`, then "primary gated by resilience layer"
        // for everyone). Those outcomes are abandoned, not recorded.
        if let Some(Ok(guard)) = primary_gate {
            match &last_result {
                Some(Ok((_, status @ (400 | 422)))) => {
                    tracing::debug!(
                        model = %primary_model.model_name,
                        status,
                        "Malformed-request status from upstream; not counted by the breaker"
                    );
                    guard.abandon();
                }
                Some(Ok((_, status))) => guard.record_failure(format!("HTTP {status}")),
                Some(Err(e)) => guard.record_failure(format!("{e}")),
                None => guard.record_failure("no attempts made"),
            }
        }
    }

    // When the primary was gate-rejected we never had a `last_result` —
    // inject a synthetic 503 so the downstream error-classification
    // logic (context-window / content-policy detection) has a shape
    // to match on but treats this as a generic failover path.
    if primary_was_gate_rejected && last_result.is_none() {
        last_result = Some(Ok((
            serde_json::json!({
                "error": {
                    "message": "primary gated by resilience layer",
                    "type": "gateway_resilience_gated"
                }
            }),
            503,
        )));
    }

    // Primary failed — record failure for cooldown
    if let Some(Ok((_, status))) = &last_result {
        router_state.record_failure(
            &primary_model.id,
            *status,
            cooldown_time,
            "Primary model failed",
        );
    }

    // Primary failed — drop any sticky pin so the next call can
    // re-pick a different model in the group instead of waiting for
    // the TTL to expire on a known-bad pin. Cheap and safe: an
    // already-cleared key is a no-op.
    if let Some(session) = prompt_cache {
        session.clear_pin(request, primary_model).await;
    }

    // Primary failed — classify the error and try appropriate fallbacks
    match last_result {
        Some(Ok((body, status))) => {
            let body_str = body.to_string().to_lowercase();

            tracing::warn!(
                model = %primary_model.model_name,
                status,
                "Primary model returned error, trying fallbacks"
            );

            let is_context_error = status == 400
                && (body_str.contains("context")
                    || body_str.contains("token") && body_str.contains("limit"));
            let is_content_policy_error = status == 400
                && (body_str.contains("content_policy")
                    || body_str.contains("content policy")
                    || body_str.contains("safety")
                    || body_str.contains("moderation"));

            tracing::debug!(
                is_context_error,
                is_content_policy_error,
                "Classifying error for fallback selection"
            );

            // 1. Context window fallbacks (routing group-level)
            //
            // Model-level fallback is gone
            // (the `context_window_fallback` /
            // `fallback_models` columns are no longer in `llm_model`).
            // A direct/non-routed call to an unreachable model now relies
            // on the per-model circuit breaker + the HTTP `connect_timeout`
            // to fail fast — only routing groups carry fallback config.
            if is_context_error {
                if let Some(ref group) = routing_group {
                    if let Some(ref cwf) = group.context_window_fallbacks {
                        if let Some(result) = try_fallback_list(
                            client,
                            db,
                            tenant_id,
                            cwf,
                            request,
                            stream,
                            max_fallbacks,
                            router_state,
                            provider_cache_for_fallbacks,
                            encryption_for_fallbacks,
                            residency_profile_ref,
                        )
                        .await
                        {
                            return Ok((result.0, result.1, result.2, true));
                        }
                    }
                }
            }

            // 2. Content policy fallbacks (routing group-level)
            if is_content_policy_error {
                if let Some(ref group) = routing_group {
                    if let Some(ref cpf) = group.content_policy_fallbacks {
                        if let Some(result) = try_fallback_list(
                            client,
                            db,
                            tenant_id,
                            cpf,
                            request,
                            stream,
                            max_fallbacks,
                            router_state,
                            provider_cache_for_fallbacks,
                            encryption_for_fallbacks,
                            residency_profile_ref,
                        )
                        .await
                        {
                            return Ok((result.0, result.1, result.2, true));
                        }
                    }
                }
            }

            // 3a. Model-level fallback chain — the primary model's OWN
            // ordered `fallback_models` (industry-standard model→model
            // failover). Tried before the routing-group lists so a per-model
            // chain takes precedence over the coarser group→group fallback.
            // Only fire on an availability error: a 400/422 is a malformed
            // request every model rejects, so don't burn the chain (matches the
            // non-chat `send_first_success` path).
            if is_fallback_worthy(status) {
                if let Some(ref mfb) = primary_model.fallback_models {
                    if let Some(result) = try_fallback_list(
                        client,
                        db,
                        tenant_id,
                        mfb,
                        request,
                        stream,
                        max_fallbacks,
                        router_state,
                        provider_cache_for_fallbacks,
                        encryption_for_fallbacks,
                        residency_profile_ref,
                    )
                    .await
                    {
                        return Ok((result.0, result.1, result.2, true));
                    }
                }
            }

            // 3. Routing group fallback_models (group-level fallback groups)
            if let Some(ref group) = routing_group {
                if let Some(ref gfb) = group.fallback_models {
                    if let Some(result) = try_fallback_list(
                        client,
                        db,
                        tenant_id,
                        gfb,
                        request,
                        stream,
                        max_fallbacks,
                        router_state,
                        provider_cache_for_fallbacks,
                        encryption_for_fallbacks,
                        residency_profile_ref,
                    )
                    .await
                    {
                        return Ok((result.0, result.1, result.2, true));
                    }
                }
            }

            // 4. Default fallbacks (routing group-level)
            if let Some(ref group) = routing_group {
                if let Some(ref dfb) = group.default_fallbacks {
                    if let Some(result) = try_fallback_list(
                        client,
                        db,
                        tenant_id,
                        dfb,
                        request,
                        stream,
                        max_fallbacks,
                        router_state,
                        provider_cache_for_fallbacks,
                        encryption_for_fallbacks,
                        residency_profile_ref,
                    )
                    .await
                    {
                        return Ok((result.0, result.1, result.2, true));
                    }
                }
            }

            // All fallbacks exhausted — return primary error
            Ok((body, status, primary_model.clone(), false))
        }
        Some(Err(e)) => {
            // Transport error (connection refused / DNS / timeout). Try the
            // model's OWN fallback chain first, then routing-group lists.
            if let Some(ref mfb) = primary_model.fallback_models {
                if let Some(result) = try_fallback_list(
                    client,
                    db,
                    tenant_id,
                    mfb,
                    request,
                    stream,
                    max_fallbacks,
                    router_state,
                    provider_cache_for_fallbacks,
                    encryption_for_fallbacks,
                    residency_profile_ref,
                )
                .await
                {
                    return Ok((result.0, result.1, result.2, true));
                }
            }
            if let Some(ref group) = routing_group {
                if let Some(ref gfb) = group.fallback_models {
                    if let Some(result) = try_fallback_list(
                        client,
                        db,
                        tenant_id,
                        gfb,
                        request,
                        stream,
                        max_fallbacks,
                        router_state,
                        provider_cache_for_fallbacks,
                        encryption_for_fallbacks,
                        residency_profile_ref,
                    )
                    .await
                    {
                        return Ok((result.0, result.1, result.2, true));
                    }
                }
                if let Some(ref dfb) = group.default_fallbacks {
                    if let Some(result) = try_fallback_list(
                        client,
                        db,
                        tenant_id,
                        dfb,
                        request,
                        stream,
                        max_fallbacks,
                        router_state,
                        provider_cache_for_fallbacks,
                        encryption_for_fallbacks,
                        residency_profile_ref,
                    )
                    .await
                    {
                        return Ok((result.0, result.1, result.2, true));
                    }
                }
            }

            Err(e)
        }
        None => Err(LlmRouterError::UpstreamError(
            "No response from primary model".into(),
        )),
    }
}

// =========================================================================
// Streaming
// =========================================================================

/// Forward a request and stream SSE chunks back.
/// Integrates system prompt injection and active request tracking.
/// Streaming counterpart of the model-level failover: try the primary, and if
/// the upstream errors BEFORE the stream starts (transport error or a
/// fallback-worthy non-2xx), walk the model's `fallback_models` chain. Once
/// bytes are flowing we can't retry, so failover only covers the initial
/// connect/handshake — which is where provider outages surface. Returns the
/// first successful streaming `Response`.
pub async fn forward_streaming_with_model_fallback(
    state: &crate::services::SharedState,
    primary: &LlmModel,
    request: &ChatCompletionRequest,
) -> Result<reqwest::Response, LlmRouterError> {
    let primary_err = match forward_chat_completion_streaming(
        &state.http_client,
        primary,
        request,
        &state.router_state,
    )
    .await
    {
        Ok(resp) => return Ok(resp),
        Err(e) => {
            let status = match &e {
                LlmRouterError::UpstreamHttpError { status, .. } => *status,
                _ => 0,
            };
            // A malformed-request status (400/422) won't be helped by
            // another model — surface it immediately.
            if status != 0 && !is_fallback_worthy(status) {
                return Err(e);
            }
            state.router_state.record_failure(
                &primary.id,
                status,
                60,
                "streaming model-level fallback",
            );
            e
        }
    };

    let chain = resolve_fallback_chain(
        &state.db,
        &primary.tenant_id,
        primary,
        &state.router_state,
        Some(&state.encryption),
        Some(&state.model_cache),
        Some(&state.provider_cache),
        providers::ProviderApi::ChatForward,
    )
    .await;
    if chain.is_empty() {
        return Err(primary_err);
    }

    let mut last_err = primary_err;
    for cand in &chain {
        match forward_chat_completion_streaming(
            &state.http_client,
            cand,
            request,
            &state.router_state,
        )
        .await
        {
            Ok(resp) => {
                state.router_state.record_success(&cand.id);
                tracing::info!(model = %cand.model_name, "streaming: served via model-level fallback");
                return Ok(resp);
            }
            Err(e) => {
                let status = match &e {
                    LlmRouterError::UpstreamHttpError { status, .. } => *status,
                    _ => 0,
                };
                state.router_state.record_failure(
                    &cand.id,
                    status,
                    60,
                    "streaming model-level fallback",
                );
                last_err = e;
            }
        }
    }
    Err(last_err)
}

pub async fn forward_chat_completion_streaming(
    client: &Client,
    model: &LlmModel,
    request: &ChatCompletionRequest,
    router_state: &RouterState,
) -> Result<reqwest::Response, LlmRouterError> {
    if let Some(refusal) = providers::dispatch_refusal(model, providers::ProviderApi::ChatForward) {
        return Err(LlmRouterError::UpstreamError(refusal.reason));
    }
    // A Bedrock row served through ConverseStream: see the non-streaming
    // forwarder.
    let converse = crate::services::bedrock_runtime::serves_chat(model);
    let mut url = providers::build_url(model, "/chat/completions", true);
    let mut auth_headers = if converse {
        Vec::new()
    } else {
        providers::build_headers(model)
    };

    let mut body = serde_json::to_value(request)
        .map_err(|e| LlmRouterError::SerializationError(e.to_string()))?;

    // `prepare_request` injects the model's system prompt (merging
    // with any caller-supplied system messages).
    providers::prepare_request(&mut body, model);

    // Force streaming on and request usage in final chunk
    // (OpenAI-compatible providers).
    //
    // Gemini's native REST encodes streaming in the URL path
    // (`:streamGenerateContent`) and rejects `stream` in the body —
    // skip the body flag for that family. Same gating as the
    // non-streaming dispatcher above.
    let body_takes_stream_flag =
        !converse && !matches!(model.provider.as_str(), "gemini" | "google_ai_studio");
    if body_takes_stream_flag {
        if let Some(obj) = body.as_object_mut() {
            obj.insert("stream".into(), Value::Bool(true));
            if !providers::needs_stream_normalization(&model.provider) {
                obj.insert(
                    "stream_options".into(),
                    serde_json::json!({"include_usage": true}),
                );
            }
        }
    }

    let signed_body: Option<Vec<u8>> = if converse {
        let wire = crate::services::bedrock_runtime::converse::to_converse(&body, model, true)
            .map_err(LlmRouterError::UpstreamError)?;
        let call = crate::services::bedrock_runtime::signed_call(model, "converse-stream", &wire)
            .map_err(LlmRouterError::UpstreamError)?;
        url = call.url;
        auth_headers = call.headers;
        body = wire;
        Some(call.body)
    } else {
        None
    };

    // Increment RPM counter
    router_state.increment_rpm(&model.id);

    // Track active request (caller must call end_request after stream completes)
    router_state.begin_request(&model.id);

    let timeout_secs = model.timeout.map(|t| t as u64).unwrap_or(120);
    // `.timeout(..)` bounds the WHOLE streaming request; the TCP/TLS
    // connect phase is bounded separately by the shared client's
    // `connect_timeout` (10s, set in `main.rs`). `connect_timeout` is a
    // client-level setting that cannot be set per-request, so an
    // unreachable host fails fast (~10s) rather than hanging to
    // `timeout_secs`.
    let mut req = client
        .post(&url)
        .header("Content-Type", "application/json")
        .timeout(Duration::from_secs(timeout_secs));

    for (k, v) in &auth_headers {
        req = req.header(k.as_str(), v.as_str());
    }
    // Emit call-chain headers on
    // streaming upstream calls too. Same source (route-handler
    // task-local) as the non-streaming path above.
    if let Some(pairs) = crate::services::call_chain::current_outbound_headers() {
        for (k, v) in pairs {
            req = req.header(k, v);
        }
    }

    // Capture the exact streaming body POSTed upstream for audit logging
    // (same rationale as the non-streaming path).
    record_outbound_body(&body);

    let req = match signed_body {
        // The bytes the signature covers.
        Some(bytes) => req.body(bytes),
        None => req.json(&body),
    };
    // The routing group's `stream_timeout`: the longest this member is
    // waited for to send its FIRST chunk. A member that stays silent past
    // it is given up on (an upstream error with no status, which the
    // caller fails over on) instead of being waited for to the model's
    // whole-request timeout. Not set: the model's timeout alone applies.
    let first_chunk_deadline = first_chunk_deadline(model.routed_stream_timeout);
    let no_first_chunk = |limit: f64| {
        router_state.end_request(&model.id);
        LlmRouterError::UpstreamError(format!(
            "the model sent no first chunk within the routing group's stream_timeout ({limit} s)"
        ))
    };
    let sent = match first_chunk_deadline {
        Some((limit, deadline)) => tokio::time::timeout_at(deadline, req.send())
            .await
            .map_err(|_| no_first_chunk(limit))?,
        None => req.send().await,
    };
    let mut resp = sent.map_err(|e| {
        // End request tracking on connection error
        router_state.end_request(&model.id);
        LlmRouterError::UpstreamError(e.to_string())
    })?;

    // Recorded on both the success and error paths: a disputed call is
    // usually a failed one, and that is exactly when the vendor asks for it.
    crate::middleware::run_end::record_upstream_response(resp.status().as_u16(), resp.headers());

    // The answer's head is here; its first chunk has to be, too, before
    // the deadline. The chunk read here is put back in front of the rest.
    if let (Some((limit, deadline)), true) = (first_chunk_deadline, resp.status().is_success()) {
        let first = match tokio::time::timeout_at(deadline, resp.chunk()).await {
            Err(_) => return Err(no_first_chunk(limit)),
            Ok(Err(e)) => {
                router_state.end_request(&model.id);
                return Err(LlmRouterError::UpstreamError(e.to_string()));
            }
            Ok(Ok(first)) => first,
        };
        resp = with_first_chunk(resp, first);
    }

    if !resp.status().is_success() {
        // End request tracking on error
        router_state.end_request(&model.id);
        let status = resp.status().as_u16();
        let error_type = resp
            .headers()
            .get("x-amzn-errortype")
            .and_then(|v| v.to_str().ok())
            .map(str::to_string);
        let mut body: Value = crate::services::providers::upstream_json(resp).await;
        if converse {
            body =
                crate::services::bedrock_runtime::error_body(status, &body, error_type.as_deref());
        }
        return Err(LlmRouterError::UpstreamHttpError { status, body });
    }

    // Note: caller is responsible for calling router_state.end_request()
    // and router_state.record_latency() after the stream completes.
    if converse {
        // The event stream as the SSE of an OpenAI-compatible provider.
        return Ok(crate::services::bedrock_runtime::converse::openai_sse_response(resp, model));
    }
    Ok(resp)
}

/// `resp` again, with the chunk that was read off its body (`first`; `None`
/// when the body had already ended) in front of what is left of it.
fn with_first_chunk(resp: reqwest::Response, first: Option<bytes::Bytes>) -> reqwest::Response {
    use futures::StreamExt;
    let status = resp.status();
    let headers = resp.headers().clone();
    let rest = resp.bytes_stream();
    let body = futures::stream::iter(first.map(Ok::<_, reqwest::Error>)).chain(rest);
    let mut rebuilt = axum::http::Response::new(reqwest::Body::wrap_stream(body));
    *rebuilt.status_mut() = status;
    *rebuilt.headers_mut() = headers;
    reqwest::Response::from(rebuilt)
}

// =========================================================================
// Post-stream helpers (called from route handler after streaming completes)
// =========================================================================

/// Record post-stream metrics: end active request, record latency, record TPM, record success.
///
/// `routed_via_group_id` is `Some` when the request was routed through a routing
/// group with a TPM cap — the group-level TPM counter (checked by
/// `select_from_routing_group`) is then charged the same token total, so the
/// group throughput limit actually enforces on the streaming path too.
pub fn record_stream_success(
    router_state: &RouterState,
    model_id: &str,
    latency_ms: f64,
    total_tokens: Option<i64>,
    routed_via_group_id: Option<&str>,
) {
    router_state.end_request(model_id);
    router_state.record_latency(model_id, latency_ms);
    if let Some(tokens) = total_tokens {
        router_state.add_tpm(model_id, tokens);
        if let Some(rg) = routed_via_group_id {
            router_state.add_tpm(rg, tokens);
        }
    }
    router_state.record_success(model_id);
}

/// Record post-stream failure: end active request, record failure for cooldown.
pub fn record_stream_failure(
    router_state: &RouterState,
    model_id: &str,
    status_code: u16,
    cooldown_time: i32,
) {
    router_state.end_request(model_id);
    router_state.record_failure(model_id, status_code, cooldown_time, "Stream failed");
}

// =========================================================================
// Errors
// =========================================================================

/// Whether an attempt's result leaves its outcome unknown: a status that
/// says the origin had the request and did not answer, or a send that was
/// lost after leaving.
pub(crate) fn outcome_is_ambiguous(result: &Result<(Value, u16), LlmRouterError>) -> bool {
    match result {
        Ok((_, status)) => is_ambiguous_status(*status),
        Err(LlmRouterError::UpstreamAmbiguous(_)) => true,
        Err(LlmRouterError::UpstreamHttpError { status, .. }) => is_ambiguous_status(*status),
        Err(_) => false,
    }
}

/// 502, 504, 520 and 524: the origin (or an edge in front of it) had the
/// request and gave no answer.
pub(crate) fn is_ambiguous_status(status: u16) -> bool {
    matches!(status, 502 | 504 | 520 | 524)
}

#[derive(Debug, thiserror::Error)]
pub enum LlmRouterError {
    #[error("No model specified in request")]
    NoModelSpecified,
    #[error("Model not found: {0}")]
    ModelNotFound(String),
    #[error("Database error: {0}")]
    DbError(String),
    #[error("Serialization error: {0}")]
    SerializationError(String),
    #[error("Upstream error: {0}")]
    UpstreamError(String),
    /// The request was sent and then timed out or broke: the provider may
    /// have generated (and billed) a completion we never saw. Never
    /// retried against the same model.
    #[error("Upstream error (outcome unknown, may have been billed): {0}")]
    UpstreamAmbiguous(String),
    // Body is `serde_json::Value` which `Display`s as compact JSON —
    // good enough for `health_check_error` storage and tracing logs.
    // Without including the body here, `e.to_string()` at the
    // `record_llm_failure` callsites in `routes/llm.rs` produces a
    // useless "Upstream HTTP error" with no diagnostic value, and
    // operators see "HTTP 400" / similar in the Admin Console with
    // no clue why their model is unhealthy.
    #[error("Upstream HTTP {status}: {body}")]
    UpstreamHttpError { status: u16, body: Value },
    /// The routing group exists and has members, and none can take a call
    /// now: each is cooling down after failures or has used up its own
    /// per-minute rate. `retry_after_secs` is the shortest wait after which
    /// one of them is available again, as this replica knows it.
    #[error("All models busy or in cooldown for group '{group}'")]
    AllModelsBusy {
        group_id: String,
        group: String,
        retry_after_secs: Option<u64>,
    },
    #[error("Rate limit exceeded: {0}")]
    RateLimitExceeded(String),
    #[error("Concurrency limit reached for group '{0}'")]
    ConcurrencyLimitReached(String),
    /// The model's endpoint is refused by the outbound policy.
    #[error("Model endpoint refused by outbound policy: {0}")]
    OutboundDenied(String),
}

// =========================================================================
// Tests
// =========================================================================

/// The routing group's `stream_timeout` as stated (for the refusal's text)
/// and the deadline it sets from now; `None` when it states no positive
/// number. A value above the ceiling configuration may state is the ceiling
/// (`configured_duration`).
fn first_chunk_deadline(stream_timeout: Option<f64>) -> Option<(f64, tokio::time::Instant)> {
    let t = stream_timeout?;
    let wait = crate::services::configured_duration::positive_secs(t)?;
    Some((t, tokio::time::Instant::now() + wait))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A routing group's `stream_timeout` above what a deadline can hold
    /// is the ceiling, not a panic; one that is not a positive number sets
    /// no deadline.
    #[tokio::test]
    async fn a_huge_stream_timeout_sets_the_ceiling_as_the_first_chunk_deadline() {
        let ceiling = crate::services::configured_duration::MAX_CONFIGURED_DURATION;
        for huge in [1e300, f64::MAX, 1.9e19, 1e15] {
            let before = tokio::time::Instant::now();
            let (stated, deadline) = first_chunk_deadline(Some(huge)).expect("a deadline");
            assert_eq!(stated, huge);
            assert!(
                deadline >= before + ceiling && deadline <= tokio::time::Instant::now() + ceiling
            );
        }
        for none in [
            None,
            Some(0.0),
            Some(-1.0),
            Some(f64::NAN),
            Some(f64::INFINITY),
        ] {
            assert!(first_chunk_deadline(none).is_none(), "{none:?}");
        }
    }

    /// A model endpoint the outbound policy denies is refused; a
    /// public endpoint and the provider default (no api_url) pass.
    #[test]
    fn guard_model_url_refuses_denied_ip_literal_endpoints() {
        let mut m = make_test_model("m-d32", "gpt-x", "openai");
        for denied in ["http://169.254.169.254/v1", "http://[fd00:ec2::254]/"] {
            m.api_url = Some(denied.into());
            let err = guard_model_url(&m).expect_err(denied);
            assert!(
                matches!(err, LlmRouterError::OutboundDenied(_)),
                "{denied}: {err:?}"
            );
            assert!(
                err.to_string().contains("refused by outbound policy"),
                "{err}"
            );
        }
        m.api_url = Some("https://api.openai.com/v1".into());
        assert!(guard_model_url(&m).is_ok());
        m.api_url = None;
        assert!(guard_model_url(&m).is_ok());
        m.api_url = Some("  ".into());
        assert!(
            guard_model_url(&m).is_ok(),
            "blank falls back to the provider default"
        );
    }
    use serde_json::json;

    use crate::models::db::LlmModel;
    use crate::services::router_state::RouterState;

    /// The outbound-body capture is a no-op when unscoped (existing
    /// callers / tests unaffected) and round-trips the exact JSON when the
    /// caller scopes the task-local — that's how the route logs what was
    /// actually sent to the provider.
    #[tokio::test]
    async fn record_outbound_body_roundtrips_within_scope() {
        // Unscoped: must not panic, just silently no-op.
        record_outbound_body(&json!({"model": "x"}));

        let translated = json!({
            "model": "claude-haiku-4-5-20251001",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 512,
            "stream": true
        });

        let captured = OUTBOUND_REQUEST_BODY
            .scope(std::cell::RefCell::new(None), async {
                record_outbound_body(&translated);
                OUTBOUND_REQUEST_BODY.with(|c| c.borrow().clone())
            })
            .await;

        let captured = captured.expect("body captured within scope");
        let parsed: serde_json::Value = serde_json::from_str(&captured).unwrap();
        // The captured body is the resolved/translated one, NOT a routing
        // group name or OpenAI-only null fields.
        assert_eq!(parsed["model"], "claude-haiku-4-5-20251001");
        assert_eq!(parsed["max_tokens"], 512);
        assert_eq!(parsed["stream"], true);
    }

    /// Outside any scope, the last-write does not leak into a later scope
    /// (each request's scope starts empty).
    #[tokio::test]
    async fn record_outbound_body_scope_starts_empty() {
        let captured = OUTBOUND_REQUEST_BODY
            .scope(std::cell::RefCell::new(None), async {
                OUTBOUND_REQUEST_BODY.with(|c| c.borrow().clone())
            })
            .await;
        assert!(captured.is_none(), "fresh scope must start empty");
    }

    // ----- Regression coverage for the encrypted-api_key boundary.
    //     Direct DB readers (video_pipeline::load_model,
    //     search_server::load_provider_config) used to forward
    //     Fernet ciphertext as `Authorization: Key …`, which fal
    //     rejected as "Cannot access application…". The shared
    //     `open_model_credentials` here is the one opener —
    //     these tests pin its behaviour so a future refactor
    //     can't silently re-introduce the "ship the ciphertext"
    //     bug.

    #[test]
    fn is_fallback_worthy_covers_availability_errors_but_not_bad_requests() {
        // Fail over on "this model is unavailable/erroring" — auth, rate-limit,
        // server, timeout, conflict.
        for s in [401u16, 403, 408, 409, 429, 500, 502, 503, 504, 529] {
            assert!(super::is_fallback_worthy(s), "status {s} should fail over");
        }
        // Never fail over a malformed request — every model rejects it.
        for s in [400u16, 422] {
            assert!(
                !super::is_fallback_worthy(s),
                "status {s} must NOT fail over"
            );
        }
    }

    #[test]
    fn open_model_credentials_decrypts_the_key_in_place() {
        let enc = EncryptionService::new("regression-test-key");
        let plaintext = "sk_brutor_api_some-real-looking-value-123";
        let ciphertext = enc.encrypt(plaintext).unwrap();

        let mut model = make_test_model("llm-1", "test-model", "openai");
        model.api_key = Some(ciphertext);

        open_model_credentials(&mut model, &enc);
        assert_eq!(
            model.api_key.as_deref(),
            Some(plaintext),
            "plaintext should land back on the model row after decrypt"
        );
    }

    /// The columns hold ciphertext and nothing else. A stored value that is
    /// not a ciphertext the gateway can open is never the credential: it is
    /// removed and the row is refused at dispatch, as for a ciphertext
    /// written under another key.
    #[test]
    fn open_model_credentials_removes_a_stored_value_that_is_not_ciphertext() {
        let enc = EncryptionService::new("any-key");
        let mut by_key = make_test_model("llm-2", "test-model", "fal_ai");
        by_key.api_key = Some("plaintext-fal-key-that-isnt-fernet".to_string());
        let mut by_config = make_test_model("llm-3", "claude-3-haiku", "bedrock");
        by_config.api_key = None;
        by_config.provider_config = Some(r#"{"region":"eu-west-1"}"#.to_string());
        for mut model in [by_key, by_config] {
            open_model_credentials(&mut model, &enc);
            assert_eq!((&model.api_key, &model.provider_config), (&None, &None));
            assert!(model.credential_unreadable);
            assert!(providers::unreadable_credential_refusal(&model).is_some());
        }
    }

    /// A row is opened once. Passing an opened row again (the cache opened
    /// it, a loader passes it on) leaves the plaintext where it is: "opened"
    /// is a fact about the row, not a guess from the shape of its value.
    #[test]
    fn open_model_credentials_opens_a_row_once() {
        let enc = EncryptionService::new("any-key");
        let mut model = make_test_model("llm-2", "test-model", "openai");
        model.api_key = Some(enc.encrypt("sk-made-up").unwrap());
        open_model_credentials(&mut model, &enc);
        open_model_credentials(&mut model, &enc);
        assert_eq!(model.api_key.as_deref(), Some("sk-made-up"));
        assert!(!model.credential_unreadable);
        // A row with nothing stored is opened too, and stays readable.
        let mut none = make_test_model("llm-4", "test-model", "openai");
        none.api_key = None;
        open_model_credentials(&mut none, &enc);
        open_model_credentials(&mut none, &enc);
        assert!(!none.credential_unreadable);
    }

    /// A ciphertext that does not decrypt is never
    /// the credential: it is removed, and the row is refused at dispatch on
    /// every API.
    #[test]
    fn open_model_credentials_removes_a_ciphertext_that_does_not_decrypt() {
        let enc = EncryptionService::new("this-gateways-key");
        let other = EncryptionService::new("another-installations-key");
        let token = other.encrypt("sk-made-up").unwrap();
        for stored in [
            token.clone(),
            format!("BAEv1:aws_kms:YWJjZA:{token}"),
            format!("BAEv1:vault:YWJjZA:{token}"),
        ] {
            let mut by_key = make_test_model("llm-2", "test-model", "openai");
            by_key.api_url = Some("http://127.0.0.1:9/v1".into());
            by_key.api_key = Some(stored.clone());
            let mut by_config = make_test_model("llm-2", "claude-3-haiku", "bedrock");
            by_config.api_key = None;
            by_config.provider_config = Some(stored.clone());
            for mut model in [by_key, by_config] {
                open_model_credentials(&mut model, &enc);
                assert_eq!((&model.api_key, &model.provider_config), (&None, &None));
                assert!(model.credential_unreadable);
                for api in [
                    providers::ProviderApi::Llm,
                    providers::ProviderApi::ChatForward,
                    providers::ProviderApi::Batch,
                ] {
                    let refusal = providers::dispatch_refusal(&model, api).expect("refused");
                    assert_eq!(refusal.error_type, "provider_credential_missing");
                    assert!(!refusal.reason.contains(&stored));
                }
            }
        }
    }

    #[test]
    fn open_model_credentials_preserves_none() {
        // Some adapters legitimately have api_key=None (e.g. when
        // upstream auth comes from a separate idp_id field). The
        // helper must not synthesize an empty string or panic.
        let enc = EncryptionService::new("any-key");
        let mut model = make_test_model("llm-3", "test-model", "ollama");
        model.api_key = None;

        open_model_credentials(&mut model, &enc);
        assert!(model.api_key.is_none());
    }

    fn make_test_model(id: &str, model_name: &str, provider: &str) -> LlmModel {
        LlmModel {
            id: id.to_string(),
            tenant_id: "tenant-1".to_string(),
            name: format!("Test {}", model_name),
            description: None,
            usage: None,
            provider_id: format!("prov-{}", provider),
            provider: provider.to_string(),
            provider_additional_drop_params: None,
            model_name: model_name.to_string(),
            mode: None,
            parameters: None,
            tags: None,
            api_url_auto_discovered: None,
            api_url: None,
            auth_type: None,
            api_key: Some("sk-test".to_string()),
            default_temperature: None,
            default_max_tokens: None,
            system_prompt: None,
            top_p: None,
            n: None,
            stop_sequences: None,
            seed: None,
            presence_penalty: None,
            frequency_penalty: None,
            logit_bias: None,
            response_format: None,
            logprobs: None,
            top_logprobs: None,
            stream_options: None,
            supports_streaming: None,
            supports_function_calling: None,
            supports_vision: None,
            tools: None,
            tool_choice: None,
            parallel_tool_calls: None,
            user_identifier: None,
            api_version: None,
            extra_headers: None,
            model_metadata: None,
            drop_params: None,
            additional_drop_params: None,
            thinking_enabled: None,
            routing_group_id: None,
            routed_via_group_id: None,
            routed_stream_timeout: None,
            routed_via_tier: None,
            routed_tier_signals: None,
            routing_weight: None,
            fallback_models: None,
            rate_limit_rpm: None,
            rate_limit_tpm: None,
            cost_per_input_token: None,
            cost_per_output_token: None,
            cost_per_image: None,
            supported_image_sizes: None,
            supported_image_qualities: None,
            max_images_per_request: None,
            cost_per_audio_char: None,
            supported_voices: None,
            supported_audio_formats: None,
            cost_per_audio_minute: None,
            max_audio_duration_seconds: None,
            supported_input_formats: None,
            supported_response_formats: None,
            cost_per_video_second: None,
            supported_video_resolutions: None,
            supported_video_durations: None,
            max_video_duration_seconds: None,
            timeout: None,
            max_concurrent_requests: None,
            allowed_fails: None,
            cooldown_time: None,
            enabled: true,
            capabilities: None,
            health_status: None,
            last_health_check: None,
            last_health_success: None,
            consecutive_health_failures: None,
            health_check_error: None,
            total_requests: None,
            failed_requests: None,
            total_tokens_consumed: None,
            avg_latency_ms: None,
            supports_batch: None,
            provider_config: None,
            credential_unreadable: false,
            credentials_opened: false,
            created_at: None,
            updated_at: None,
        }
    }

    // =====================================================================
    // select_simple_shuffle
    // =====================================================================

    #[test]
    fn simple_shuffle_single_model_returns_that_model() {
        let m = make_test_model("m1", "gpt-4o", "openai");
        let result = select_simple_shuffle(&[m.clone()]).unwrap();
        assert_eq!(result.id, "m1");
    }

    #[test]
    fn simple_shuffle_multiple_models_returns_one_of_them() {
        let mut m1 = make_test_model("m1", "gpt-4o", "openai");
        m1.routing_weight = Some(3);
        let mut m2 = make_test_model("m2", "claude-3", "anthropic");
        m2.routing_weight = Some(7);

        let result = select_simple_shuffle(&[m1, m2]).unwrap();
        assert!(result.id == "m1" || result.id == "m2");
    }

    #[test]
    fn simple_shuffle_zero_weight_does_not_crash() {
        let mut m1 = make_test_model("m1", "gpt-4o", "openai");
        m1.routing_weight = Some(0);
        let mut m2 = make_test_model("m2", "claude-3", "anthropic");
        m2.routing_weight = Some(0);

        let result = select_simple_shuffle(&[m1, m2]).unwrap();
        // total_weight <= 0 branch returns first model
        assert_eq!(result.id, "m1");
    }

    #[test]
    fn simple_shuffle_no_weight_does_not_crash() {
        let m1 = make_test_model("m1", "gpt-4o", "openai");
        let m2 = make_test_model("m2", "claude-3", "anthropic");

        let result = select_simple_shuffle(&[m1, m2]).unwrap();
        assert!(result.id == "m1" || result.id == "m2");
    }

    // =====================================================================
    // select_usage_based
    // =====================================================================

    #[test]
    fn usage_based_picks_lowest_tpm_ratio() {
        let mut m1 = make_test_model("m1", "gpt-4o", "openai");
        m1.rate_limit_tpm = Some(1000);
        let mut m2 = make_test_model("m2", "claude-3", "anthropic");
        m2.rate_limit_tpm = Some(1000);

        let state = RouterState::new(None);
        // m1 has higher usage so m2 should be selected
        state.add_tpm("m1", 500);

        let result = select_usage_based(&[m1, m2], &state).unwrap();
        assert_eq!(result.id, "m2");
    }

    #[test]
    fn usage_based_equal_usage_picks_higher_weight() {
        let mut m1 = make_test_model("m1", "gpt-4o", "openai");
        m1.routing_weight = Some(1);
        let mut m2 = make_test_model("m2", "claude-3", "anthropic");
        m2.routing_weight = Some(10);

        let state = RouterState::new(None);
        // No usage for either — ratio is 0 for both, tie-break by weight
        let result = select_usage_based(&[m1, m2], &state).unwrap();
        assert_eq!(result.id, "m2");
    }

    #[test]
    fn usage_based_no_usage_recorded_picks_first_or_highest_weight() {
        let m1 = make_test_model("m1", "gpt-4o", "openai");
        let m2 = make_test_model("m2", "claude-3", "anthropic");

        let state = RouterState::new(None);
        let result = select_usage_based(&[m1, m2], &state).unwrap();
        // Both have ratio 0, same weight (None = 1), so first wins
        assert_eq!(result.id, "m1");
    }

    // =====================================================================
    // select_least_busy
    // =====================================================================

    #[test]
    fn least_busy_picks_fewest_active() {
        let m1 = make_test_model("m1", "gpt-4o", "openai");
        let m2 = make_test_model("m2", "claude-3", "anthropic");

        let state = RouterState::new(None);
        state.begin_request("m1");
        state.begin_request("m1");
        state.begin_request("m2");

        let result = select_least_busy(&[m1, m2], &state).unwrap();
        assert_eq!(result.id, "m2");
    }

    #[test]
    fn least_busy_equal_active_picks_higher_weight() {
        let mut m1 = make_test_model("m1", "gpt-4o", "openai");
        m1.routing_weight = Some(1);
        let mut m2 = make_test_model("m2", "claude-3", "anthropic");
        m2.routing_weight = Some(10);

        let state = RouterState::new(None);
        state.begin_request("m1");
        state.begin_request("m2");

        let result = select_least_busy(&[m1, m2], &state).unwrap();
        assert_eq!(result.id, "m2");
    }

    #[test]
    fn least_busy_no_active_picks_first() {
        let m1 = make_test_model("m1", "gpt-4o", "openai");
        let m2 = make_test_model("m2", "claude-3", "anthropic");

        let state = RouterState::new(None);
        let result = select_least_busy(&[m1, m2], &state).unwrap();
        assert_eq!(result.id, "m1");
    }

    // =====================================================================
    // select_latency_based
    // =====================================================================

    #[test]
    fn latency_based_picks_lowest_latency() {
        let m1 = make_test_model("m1", "gpt-4o", "openai");
        let m2 = make_test_model("m2", "claude-3", "anthropic");

        let state = RouterState::new(None);
        state.record_latency("m1", 200.0);
        state.record_latency("m2", 50.0);

        let result = select_latency_based(&[m1, m2], &state).unwrap();
        assert_eq!(result.id, "m2");
    }

    #[test]
    fn latency_based_no_latency_picks_first() {
        let m1 = make_test_model("m1", "gpt-4o", "openai");
        let m2 = make_test_model("m2", "claude-3", "anthropic");

        let state = RouterState::new(None);
        // No latency recorded — both return f64::MAX, first wins
        let result = select_latency_based(&[m1, m2], &state).unwrap();
        assert_eq!(result.id, "m1");
    }

    // =====================================================================
    // select_cost_based
    // =====================================================================

    #[test]
    fn cost_based_picks_cheapest() {
        let mut m1 = make_test_model("m1", "gpt-4o", "openai");
        m1.cost_per_input_token = Some(0.03);
        let mut m2 = make_test_model("m2", "claude-3", "anthropic");
        m2.cost_per_input_token = Some(0.01);

        let result = select_cost_based(&[m1, m2], DEFAULT_COST_OUTPUT_RATIO).unwrap();
        assert_eq!(result.id, "m2");
    }

    #[test]
    fn cost_based_none_costs_yields_no_decision() {
        let m1 = make_test_model("m1", "gpt-4o", "openai");
        let m2 = make_test_model("m2", "claude-3", "anthropic");

        // Previously both priced at f64::MAX so the first model "won" and an
        // arbitrary pick was returned as though it were a cost decision. With
        // nothing priced there is no decision to make: the selector declines
        // and the caller falls through to another strategy.
        assert!(select_cost_based(&[m1, m2], DEFAULT_COST_OUTPUT_RATIO).is_none());
    }

    // inject_system_prompt tests live alongside the canonical
    // implementation in `services::providers::tests`. The local
    // duplicate function was removed as part of the merge
    // fix — keeping two copies risked double-prepending the server
    // prompt when merge semantics replaced the old skip-on-present
    // behavior.

    // =====================================================================
    // record_stream_success
    // =====================================================================

    #[test]
    fn record_stream_success_updates_state() {
        let state = RouterState::new(None);
        state.begin_request("m1");
        assert_eq!(state.get_active_requests("m1"), 1);

        record_stream_success(&state, "m1", 150.0, Some(500), None);

        // Active requests decremented
        assert_eq!(state.get_active_requests("m1"), 0);
        // Latency recorded
        assert!(state.get_avg_latency("m1") < f64::MAX);
        // TPM recorded
        assert_eq!(state.get_tpm("m1"), 500);
        // Cooldown cleared (record_success clears cooldowns)
        assert!(!state.is_cooled_down("m1"));
    }

    #[test]
    fn record_stream_success_no_tokens() {
        let state = RouterState::new(None);
        state.begin_request("m1");

        record_stream_success(&state, "m1", 100.0, None, None);

        assert_eq!(state.get_active_requests("m1"), 0);
        assert_eq!(state.get_tpm("m1"), 0);
    }

    #[test]
    fn record_stream_success_charges_routing_group_tpm() {
        // Regression: routing-group TPM limits were dead because only the
        // per-model counter (`m.id`) was ever charged, while the group check
        // reads the `group.id` counter. When a request is routed through a
        // group, the group's TPM counter must be charged the same tokens.
        let state = RouterState::new(None);
        state.begin_request("m1");

        record_stream_success(&state, "m1", 120.0, Some(500), Some("grp-routing-1"));

        // Both the per-model and the routing-group counters advanced.
        assert_eq!(state.get_tpm("m1"), 500);
        assert_eq!(state.get_tpm("grp-routing-1"), 500);
    }

    // =====================================================================
    // record_stream_failure
    // =====================================================================

    #[test]
    fn record_stream_failure_updates_state() {
        let state = RouterState::new(None);
        state.begin_request("m1");
        assert_eq!(state.get_active_requests("m1"), 1);

        // 500 triggers cooldown
        record_stream_failure(&state, "m1", 500, 60);

        assert_eq!(state.get_active_requests("m1"), 0);
        assert!(state.is_cooled_down("m1"));
    }

    #[test]
    fn record_stream_failure_non_cooldown_status() {
        let state = RouterState::new(None);
        state.begin_request("m1");

        // 400 does NOT trigger cooldown per should_cooldown logic
        record_stream_failure(&state, "m1", 400, 60);

        assert_eq!(state.get_active_requests("m1"), 0);
        assert!(!state.is_cooled_down("m1"));
    }

    // ===================================================================
    // Cloud auth integration tests
    // ===================================================================

    #[test]
    fn bedrock_claude_routes_via_native_pipeline_on_openai_surface() {
        // Bedrock regression guard: Bedrock-Claude requests
        // hitting /v1/proxy/llm/chat/completions are dispatched through
        // the native Anthropic pipeline (via
        // `anthropic_adapter::should_route_via_openai_surface`) BEFORE
        // the standard `forward_chat_completion_*` path runs. Without
        // this short-circuit the row would go to the generic forwarder,
        // which cannot serve it.
        //
        // The assertion below is on the routing predicate, not on the
        // post-build_url URL: the predicate is what guarantees the
        // generic forward path is never reached for Bedrock-Claude.
        let model = make_test_model("m1", "anthropic.claude-sonnet-4-5", "bedrock");
        assert!(
            crate::services::anthropic_adapter::should_route_via_openai_surface(&model),
            "Bedrock-Claude must route via the native Anthropic pipeline"
        );

        // The generic forwarder has no destination for the row (it must never
        // answer `https://api.openai.com/v1/chat/completions`), and
        // refuses it before building a URL.
        let mut model_with_creds = model.clone();
        model_with_creds.api_url = None;
        model_with_creds.auth_type = Some("aws_iam".into());
        model_with_creds.provider_config = Some(
            serde_json::json!({
                "aws_access_key_id": "AKIATEST",
                "aws_secret_access_key": "secret123",
                "aws_region": "us-east-1"
            })
            .to_string(),
        );
        let url = providers::build_url(&model_with_creds, "/chat/completions", false);
        assert_eq!(url, "no-upstream-base://bedrock/chat/completions");
        assert!(
            providers::dispatch_refusal(&model_with_creds, providers::ProviderApi::Llm).is_some()
        );
    }

    // ── a cloud credential never becomes a request header ─────────

    /// A row of an OpenAI-compatible provider with a cloud `auth_type` and
    /// the cloud credential in its `provider_config`.
    fn cloud_auth_row(auth_type: &str, config: serde_json::Value) -> LlmModel {
        let mut model = make_test_model("m1", "gpt-4", "openai");
        model.auth_type = Some(auth_type.into());
        model.provider_config = Some(config.to_string());
        model
    }

    fn refusal_type(model: &LlmModel) -> Option<&'static str> {
        providers::dispatch_refusal(model, providers::ProviderApi::ChatForward)
            .map(|r| r.error_type)
    }

    fn headers_carry(model: &LlmModel, secret: &str) -> bool {
        providers::build_headers(model)
            .iter()
            .any(|(k, v)| v.contains(secret) || k.starts_with("x-brutor-"))
    }

    #[test]
    fn an_aws_iam_row_on_the_generic_forwarder_is_refused_and_its_secret_is_in_no_header() {
        let mut model = cloud_auth_row(
            "aws_iam",
            serde_json::json!({
                "aws_access_key_id": "AKIATEST",
                "aws_secret_access_key": "secret123",
                "aws_region": "eu-west-1"
            }),
        );
        assert_eq!(refusal_type(&model), Some("provider_auth_unsupported"));
        assert!(!headers_carry(&model, "secret123"));
        assert!(!headers_carry(&model, "AKIATEST"));
        // An API key beside it does not make the forwarder an AWS client.
        model.api_key = Some("sk-beside".into());
        assert_eq!(refusal_type(&model), Some("provider_auth_unsupported"));
    }

    #[test]
    fn an_azure_ad_row_is_refused_without_an_api_key_and_sent_with_one() {
        let mut model = cloud_auth_row(
            "azure_ad",
            serde_json::json!({
                "azure_client_id": "app-id",
                "azure_client_secret": "client-secret-1",
                "azure_tenant_id": "tenant"
            }),
        );
        model.api_key = None;
        assert_eq!(refusal_type(&model), Some("provider_auth_unsupported"));
        model.api_key = Some("  ".into());
        assert_eq!(refusal_type(&model), Some("provider_auth_unsupported"));
        // With the resource's own API key the row is sent with that key,
        // and the client secret stays in the gateway.
        model.api_key = Some("azure-api-key".into());
        assert_eq!(refusal_type(&model), None);
        assert!(!headers_carry(&model, "client-secret-1"));
    }

    #[test]
    fn a_gcp_service_account_row_on_the_generic_forwarder_is_refused() {
        let mut model = cloud_auth_row(
            "gcp_service_account",
            serde_json::json!({
                "gcp_project_id": "proj-1",
                "gcp_location": "europe-west4",
                "gcp_credentials_json": "{\"type\":\"service_account\",\"private_key\":\"sa-key\"}"
            }),
        );
        assert_eq!(refusal_type(&model), Some("provider_auth_unsupported"));
        assert!(!headers_carry(&model, "sa-key"));
        model.api_key = Some("sk-beside".into());
        assert_eq!(refusal_type(&model), Some("provider_auth_unsupported"));
    }

    #[test]
    fn an_api_key_row_is_not_refused_for_its_auth_type() {
        let mut model = make_test_model("m1", "gpt-4", "openai");
        model.auth_type = Some("api_key".into());
        assert_eq!(refusal_type(&model), None);
    }

    #[test]
    fn a_row_with_no_auth_type_is_not_refused_for_it() {
        let model = make_test_model("m1", "gpt-4", "openai");
        assert_eq!(refusal_type(&model), None);
    }

    #[test]
    fn a_cloud_auth_row_without_a_provider_config_is_refused() {
        let mut model = make_test_model("m1", "gpt-4", "openai");
        model.auth_type = Some("aws_iam".into());
        model.provider_config = None;
        assert_eq!(refusal_type(&model), Some("provider_auth_unsupported"));
    }

    #[test]
    fn a_cloud_auth_row_with_an_unreadable_provider_config_is_refused() {
        let mut model = make_test_model("m1", "gpt-4", "openai");
        model.auth_type = Some("aws_iam".into());
        model.provider_config = Some("not-json".into());
        assert_eq!(refusal_type(&model), Some("provider_auth_unsupported"));
        assert!(
            providers::build_headers(&model)
                .iter()
                .all(|(k, _)| !k.starts_with("x-brutor-"))
        );
    }

    #[test]
    fn open_model_credentials_decrypts_the_provider_config() {
        // Use the encryption service to encrypt, then verify decryption
        let encryption = EncryptionService::new("QM2FwRHuJRzTTRZR7AqB3dSxMUYFGnbVHYKVTR76b00=");
        let plain_json = r#"{"aws_access_key_id":"AKIATEST","aws_secret_access_key":"secret"}"#;
        let encrypted = encryption.encrypt(plain_json).unwrap();

        let mut model = make_test_model("m1", "claude-3-haiku", "bedrock");
        model.provider_config = Some(encrypted);

        open_model_credentials(&mut model, &encryption);

        assert_eq!(model.provider_config.as_deref(), Some(plain_json));
    }

    #[test]
    fn open_model_credentials_noop_when_there_is_no_provider_config() {
        let encryption = EncryptionService::new("QM2FwRHuJRzTTRZR7AqB3dSxMUYFGnbVHYKVTR76b00=");
        let mut model = make_test_model("m1", "gpt-4o", "openai");
        model.provider_config = None;

        open_model_credentials(&mut model, &encryption);

        assert!(model.provider_config.is_none());
    }

    // ──────────────────────────────────────────────────────────────────────
    // redirect_if_routing — virtual routing-group selector dispatch
    //
    // The control plane creates one virtual `LlmModel` per
    // routing group (mode="routing", model_name=<group_id>,
    // provider="routing_group", api_key=NULL). Without the
    // redirect helper, `resolve_model` would return that
    // virtual row to the request pipeline, the proxy would
    // forward it upstream as if it were a real model, and the
    // upstream would 401 because the row has no API key. The
    // user hit exactly that 401 storm before this redirect was
    // wired in.
    //
    // Tests below lock in the contract:
    //   - Non-routing models pass through unchanged.
    //   - A routing virtual model with `routing_group_id=NULL`
    //     surfaces as a clear error (control-plane bug
    //     indicator) instead of a misleading 401 from upstream.
    //
    // The dispatch happy-path needs a real DB (lookup of the
    // routing group + member models) so it lives in the
    // integration test module under `tests/`, not here.
    // ──────────────────────────────────────────────────────────────────────

    #[tokio::test]
    async fn redirect_if_routing_passes_non_routing_models_through() {
        // A normal model — mode is None or anything other than
        // "routing" — must come back from the helper unchanged.
        // The helper has no DB / state side-effects in this
        // path, so we can pass dummy refs without setting up a
        // pool.
        use std::time::Duration;
        let model = make_test_model("m1", "gpt-4o", "openai");
        let pool = sqlx::postgres::PgPoolOptions::new()
            .max_connections(1)
            .acquire_timeout(Duration::from_millis(1))
            .connect_lazy("postgres://test:test@localhost:5432/test")
            .expect("connect_lazy never fails on a syntactically-valid URL");
        let state = RouterState::new(None);

        let returned = redirect_if_routing(model.clone(), &pool, "tenant-1", &state, None, None)
            .await
            .expect("non-routing pass-through must not fail");

        assert_eq!(returned.id, model.id);
        assert_eq!(returned.model_name, model.model_name);
        // The dispatch DB query is never issued — no Postgres
        // connection was made, proven by the 1-ms acquire
        // timeout above which would have failed any real query.
    }

    #[tokio::test]
    async fn redirect_if_routing_errors_clearly_when_routing_group_id_missing() {
        // A virtual row with mode="routing" but no
        // routing_group_id is a control-plane bug — the
        // create_routing_group flow stamps the FK at insert
        // time. If we ever see one, surfacing a clear
        // ModelNotFound error here is far better than the
        // pre-redirect failure mode (401 from OpenAI). This
        // test catches a regression in `create_routing_group`
        // that forgets to set `routing_group_id`.
        use std::time::Duration;
        let mut model = make_test_model("rg-orphan", "orphan", "routing_group");
        model.mode = Some("routing".to_string());
        model.routing_group_id = None;
        model.api_key = None;

        let pool = sqlx::postgres::PgPoolOptions::new()
            .max_connections(1)
            .acquire_timeout(Duration::from_millis(1))
            .connect_lazy("postgres://test:test@localhost:5432/test")
            .expect("connect_lazy never fails on a syntactically-valid URL");
        let state = RouterState::new(None);

        let err = redirect_if_routing(model, &pool, "tenant-1", &state, None, None)
            .await
            .expect_err("orphan virtual model must surface as an error, not a pass-through");

        match err {
            LlmRouterError::ModelNotFound(msg) => {
                // Error must explicitly reference the bad invariant
                // so an operator reading logs can fix it. Vague
                // "model not found" is the bug we're guarding
                // against.
                assert!(
                    msg.contains("routing_group_id is NULL"),
                    "error must name the missing FK; got: {msg}"
                );
            }
            other => panic!("expected ModelNotFound, got {other:?}"),
        }
    }

    // -----------------------------------------------------------------------
    // residency_blocks_candidate
    //
    // Pure-function tests. The helper takes a profile + provider
    // cache + candidate model and decides whether the chain
    // walker should skip the candidate. No DB or network — the
    // provider cache path is exercised via `None` (forces the
    // "region unspecified" branch) since we can't easily stand up
    // a real ProviderCache in a unit test.
    // -----------------------------------------------------------------------

    use crate::services::residency::ResidencyProfile;

    fn profile_with(allowed: &[&str], block_global: bool) -> ResidencyProfile {
        ResidencyProfile {
            allowed_regions: allowed.iter().map(|s| s.to_string()).collect(),
            block_global,
        }
    }

    #[tokio::test]
    async fn residency_blocks_candidate_no_profile_never_blocks() {
        // No profile = no enforcement. Even if the candidate has
        // no region info (we pass `None` for provider_cache so
        // region resolves to None), the walker proceeds.
        let candidate = make_test_model("m-1", "model-1", "openai");
        let blocked = residency_blocks_candidate(None, None, "tenant-1", &candidate).await;
        assert!(!blocked, "no-profile path must never block");
    }

    #[tokio::test]
    async fn residency_blocks_candidate_blocks_when_block_global_and_no_region() {
        // Tenant with block_global=true + provider_cache=None
        // (region resolves to None) → blocked. This is the
        // common path: tenant requires region pinning, candidate
        // hasn't been stamped, so the chain walker skips it.
        let candidate = make_test_model("m-2", "model-2", "openai");
        let profile = profile_with(&["eu-west-1"], true);
        let blocked =
            residency_blocks_candidate(Some(&profile), None, "tenant-1", &candidate).await;
        assert!(blocked, "block_global must skip an unstamped candidate");
    }

    #[tokio::test]
    async fn residency_blocks_candidate_blocks_when_allowed_regions_explicit_and_no_region() {
        // Tenant listed specific regions (block_global irrelevant)
        // — unspecified candidate is presumed unsafe. The chain
        // walker skips it.
        let candidate = make_test_model("m-3", "model-3", "anthropic");
        let profile = profile_with(&["eu-west-1"], false);
        let blocked =
            residency_blocks_candidate(Some(&profile), None, "tenant-1", &candidate).await;
        assert!(
            blocked,
            "candidate with no region must skip when tenant requires a specific region"
        );
    }

    #[tokio::test]
    async fn residency_blocks_candidate_passes_when_no_constraint() {
        // Empty allowed_regions + block_global=false = "no
        // constraint". Even an unstamped candidate passes.
        let candidate = make_test_model("m-4", "model-4", "openai");
        let profile = profile_with(&[], false);
        let blocked =
            residency_blocks_candidate(Some(&profile), None, "tenant-1", &candidate).await;
        assert!(!blocked, "empty-allowlist + block_global=false must pass");
    }

    // ===================================================================
    // Cost-based routing — blended input+output ranking
    // ===================================================================

    /// Build a model priced at the given per-token rates.
    fn priced_model(id: &str, input: Option<f64>, output: Option<f64>) -> LlmModel {
        let mut m = make_test_model(id, id, "openai");
        m.cost_per_input_token = input;
        m.cost_per_output_token = output;
        m
    }

    #[test]
    fn cost_ranking_counts_output_tokens_not_just_input() {
        // The regression this whole change exists for: `cheap-in` wins on the
        // input rate alone but is far more expensive to actually run, because
        // output tokens are priced ~10x here. Ranking on input only picked it.
        let cheap_in_dear_out = priced_model("cheap-in", Some(1.0), Some(100.0));
        let balanced = priced_model("balanced", Some(2.0), Some(3.0));
        let models = vec![cheap_in_dear_out, balanced];

        let picked = select_cost_based(&models, DEFAULT_COST_OUTPUT_RATIO)
            .expect("priced models must yield a selection");

        assert_eq!(
            picked.id, "balanced",
            "ranking must blend the output rate, not rank on input alone"
        );
    }

    #[test]
    fn cost_output_ratio_of_one_sums_the_raw_rates() {
        // ratio 1.0 == "one output token per input token", the behaviour other
        // gateways ship. Kept reachable so an operator can opt into it.
        // blended: a = 5 + 5 = 10, b = 1 + 20 = 21  -> a wins
        let a = priced_model("a", Some(5.0), Some(5.0));
        let b = priced_model("b", Some(1.0), Some(20.0));
        let models = vec![a, b];

        let picked = select_cost_based(&models, 1.0).expect("selection");
        assert_eq!(picked.id, "a");
    }

    #[test]
    fn cost_ranking_tolerates_one_sided_pricing() {
        // A model priced on only one side is still rankable; the missing side
        // contributes nothing rather than disqualifying it.
        let input_only = priced_model("input-only", Some(1.0), None);
        let dearer = priced_model("dearer", Some(9.0), Some(9.0));
        let models = vec![dearer, input_only];

        let picked = select_cost_based(&models, DEFAULT_COST_OUTPUT_RATIO).expect("selection");
        assert_eq!(picked.id, "input-only");
    }

    #[test]
    fn cost_ranking_skips_entirely_unpriced_models() {
        // Unpriced models are excluded, not treated as free — the opposite
        // mistake would route everything to whatever we know least about.
        let unpriced = priced_model("unpriced", None, None);
        let priced = priced_model("priced", Some(50.0), Some(50.0));
        let models = vec![unpriced, priced];

        let picked = select_cost_based(&models, DEFAULT_COST_OUTPUT_RATIO).expect("selection");
        assert_eq!(picked.id, "priced");
    }

    #[test]
    fn cost_ranking_returns_none_when_nothing_is_priced() {
        // No pricing anywhere means there is no cost decision to make. The
        // caller falls through to another strategy; previously this returned
        // models[0] and presented an arbitrary pick as a cost decision.
        let models = vec![priced_model("a", None, None), priced_model("b", None, None)];

        assert!(
            select_cost_based(&models, DEFAULT_COST_OUTPUT_RATIO).is_none(),
            "an all-unpriced group must not fabricate a cost decision"
        );
    }

    #[test]
    fn blended_cost_applies_the_configured_ratio() {
        let m = priced_model("m", Some(2.0), Some(10.0));
        assert_eq!(
            blended_token_cost(&m, 0.0),
            Some(2.0),
            "ratio 0 = input only"
        );
        assert_eq!(blended_token_cost(&m, 0.5), Some(7.0));
        assert_eq!(blended_token_cost(&m, 1.0), Some(12.0));
        assert_eq!(
            blended_token_cost(&priced_model("u", None, None), 0.5),
            None
        );
    }

    // ---- cost_output_ratio config extraction ----

    // ===================================================================
    // RoutingRequestContext extraction
    // ===================================================================

    #[test]
    fn extracts_latest_user_turn_and_system_prompt() {
        let body = serde_json::json!({
            "messages": [
                {"role": "system", "content": "You are a Python assistant."},
                {"role": "user", "content": "first question"},
                {"role": "assistant", "content": "an answer"},
                {"role": "user", "content": "the actual request"}
            ]
        })
        .to_string();

        let ctx = RoutingRequestContext::from_body(Some(&body)).expect("context");
        assert_eq!(
            ctx.user_text, "the actual request",
            "must score the LATEST user turn, not the first"
        );
        assert_eq!(
            ctx.system_text.as_deref(),
            Some("You are a Python assistant.")
        );
        assert_eq!(ctx.tool_count, 0);
    }

    #[test]
    fn counts_tools_without_reading_them() {
        let body = serde_json::json!({
            "messages": [{"role": "user", "content": "go"}],
            "tools": [{"function": {"name": "a"}}, {"function": {"name": "b"}}]
        })
        .to_string();
        let ctx = RoutingRequestContext::from_body(Some(&body)).expect("context");
        assert_eq!(ctx.tool_count, 2);
    }

    #[test]
    fn handles_anthropic_top_level_system_field() {
        let body = serde_json::json!({
            "system": "Be concise.",
            "messages": [{"role": "user", "content": "hi"}]
        })
        .to_string();
        let ctx = RoutingRequestContext::from_body(Some(&body)).expect("context");
        assert_eq!(ctx.system_text.as_deref(), Some("Be concise."));
    }

    #[test]
    fn flattens_content_part_arrays_and_skips_non_text() {
        // Multimodal content: score the text, ignore the image part rather
        // than failing the extraction.
        let body = serde_json::json!({
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe this"},
                    {"type": "image_url", "image_url": {"url": "data:..."}},
                    {"type": "text", "text": "in detail"}
                ]
            }]
        })
        .to_string();
        let ctx = RoutingRequestContext::from_body(Some(&body)).expect("context");
        assert_eq!(ctx.user_text, "describe this in detail");
    }

    #[test]
    fn falls_back_to_prompt_and_input_shapes() {
        for key in ["prompt", "input"] {
            let body = serde_json::json!({ key: "legacy shape" }).to_string();
            let ctx = RoutingRequestContext::from_body(Some(&body))
                .unwrap_or_else(|| panic!("{key} shape must extract"));
            assert_eq!(ctx.user_text, "legacy shape");
        }
    }

    #[test]
    fn extraction_never_errors_on_hostile_or_absent_bodies() {
        // This runs on the hot path for every request. "No content to route
        // on" is a None, never a failure.
        assert!(RoutingRequestContext::from_body(None).is_none());
        assert!(RoutingRequestContext::from_body(Some("")).is_none());
        assert!(RoutingRequestContext::from_body(Some("not json")).is_none());
        assert!(RoutingRequestContext::from_body(Some("{}")).is_none());
        assert!(RoutingRequestContext::from_body(Some("[]")).is_none());
        assert!(RoutingRequestContext::from_body(Some(r#"{"messages":[]}"#)).is_none());
        // Messages present but no user turn.
        assert!(
            RoutingRequestContext::from_body(Some(
                r#"{"messages":[{"role":"assistant","content":"hi"}]}"#
            ))
            .is_none()
        );
        // Wrong types everywhere.
        assert!(
            RoutingRequestContext::from_body(Some(
                r#"{"messages":"nope","tools":42,"system":true}"#
            ))
            .is_none()
        );
    }

    #[test]
    fn blank_user_turn_is_not_treated_as_content() {
        let body = r#"{"messages":[{"role":"user","content":"   "}]}"#;
        assert!(RoutingRequestContext::from_body(Some(body)).is_none());
    }

    // ===================================================================
    // complexity_config_for — overrides merged over defaults
    // ===================================================================

    fn group_with_complexity(cfg: Option<serde_json::Value>) -> LlmRoutingGroup {
        LlmRoutingGroup {
            id: "rg-1".into(),
            tenant_id: "t-1".into(),
            name: "g".into(),
            routing_strategy: Some("complexity-based-routing".into()),
            complexity_config: cfg,
            ..Default::default()
        }
    }

    #[test]
    fn complexity_config_defaults_when_absent_or_malformed() {
        use crate::services::complexity::ComplexityConfig;
        let default = ComplexityConfig::default();

        assert_eq!(complexity_config_for(&group_with_complexity(None)), default);
        // Malformed shapes must degrade to defaults, never fail the request.
        for junk in [
            serde_json::json!("a string"),
            serde_json::json!({"boundaries": "nope"}),
            serde_json::json!({"weights": {"code_presence": "heavy"}}),
            serde_json::json!({"code_keywords": "not an array"}),
        ] {
            assert_eq!(
                complexity_config_for(&group_with_complexity(Some(junk.clone()))),
                default,
                "malformed config {junk} must degrade to defaults"
            );
        }
    }

    #[test]
    fn complexity_config_applies_operator_overrides() {
        let cfg = complexity_config_for(&group_with_complexity(Some(serde_json::json!({
            "boundaries": { "simple_max": 0.05, "medium_max": 0.5 },
            "weights": { "code_presence": 0.9 },
            "reasoning_keywords": ["ponder", "MUSE"]
        }))));

        assert_eq!(cfg.boundaries.simple_max, 0.05);
        assert_eq!(cfg.boundaries.medium_max, 0.5);
        // Untouched boundary keeps its default.
        assert_eq!(
            cfg.boundaries.complex_max,
            crate::services::complexity::Boundaries::default().complex_max
        );
        assert_eq!(cfg.weights.code_presence, 0.9);
        // Keyword overrides are lowercased for matching.
        assert_eq!(cfg.reasoning_keywords, vec!["ponder", "muse"]);
    }

    #[test]
    fn complexity_config_rejects_negative_weights() {
        let cfg = complexity_config_for(&group_with_complexity(Some(serde_json::json!({
            "weights": { "code_presence": -5.0 }
        }))));
        assert_eq!(
            cfg.weights.code_presence,
            crate::services::complexity::Weights::default().code_presence,
            "a negative weight would invert the dimension — reject it"
        );
    }

    #[test]
    fn cost_output_ratio_defaults_when_unset() {
        assert_eq!(cost_output_ratio(None), DEFAULT_COST_OUTPUT_RATIO);
        let empty = serde_json::json!({});
        assert_eq!(cost_output_ratio(Some(&empty)), DEFAULT_COST_OUTPUT_RATIO);
    }

    #[test]
    fn cost_output_ratio_reads_the_operator_override() {
        let args = serde_json::json!({ "cost_output_ratio": 0.75 });
        assert_eq!(cost_output_ratio(Some(&args)), 0.75);
    }

    #[test]
    fn cost_output_ratio_rejects_nonsense_values() {
        // A negative or non-numeric ratio would silently invert/corrupt the
        // ordering. Fall back to the default rather than propagate it.
        for bad in [
            serde_json::json!({ "cost_output_ratio": -1.0 }),
            serde_json::json!({ "cost_output_ratio": "cheap" }),
            serde_json::json!({ "cost_output_ratio": null }),
        ] {
            assert_eq!(
                cost_output_ratio(Some(&bad)),
                DEFAULT_COST_OUTPUT_RATIO,
                "bad ratio {bad} must fall back to the default"
            );
        }
    }
}
