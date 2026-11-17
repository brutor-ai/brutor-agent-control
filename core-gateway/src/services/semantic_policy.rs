// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Semantic Governance Policy (SGP) — natural-language, LLM-judged policy.
//!
//! Operators write an intent constraint in plain English ("agents may not send
//! customer PII to external peers"); an LLM "judge" evaluates each governed
//! request's content + context against it at runtime. This complements the
//! deterministic planes (agent grants, argument policies, RG ACLs) with the
//! intent-based layer the big agent platforms lead with.
//!
//! Design (matching how the big products ship intent policy):
//!   * **shadow → enforce** per policy (= Google SGP DRY_RUN/ENFORCE). Shadow
//!     records a would-block verdict on proxy_logs without blocking.
//!   * the judge runs on the policy's `judge_model_id`, else the tenant
//!     default, else `BRUTOR_JUDGE_MODEL` — every tier an operator choice — via the
//!     gateway's own **governed self-call** to
//!     `/v1/proxy/llm/chat/completions` (same pattern as semantic-cache
//!     embeddings), so the judge call is itself authenticated, access-checked,
//!     logged, and billed. Saving a policy auto-binds the judge model to its
//!     resource groups, so that access check passes without an exemption.
//!   * **verdict caching** (short TTL) bounds the per-request cost.
//!   * **fail CLOSED by default**: when the judge cannot render a
//!     verdict — model error, timeout, unparseable reply, or no resolvable
//!     judge model — the call is refused, because a policy that silently
//!     stopped evaluating is indistinguishable from one that approved
//!     everything. An operator who prefers availability sets `fail_open` on
//!     the policy and gets log-and-allow instead. Either way an alert is
//!     raised: an invisible fail-open is the failure mode this design exists
//!     to prevent. Pairs with shadow-first rollout, which is a *separate*
//!     control (shadow never blocks on a real violation).
//!
//! The `Judge` trait isolates the model call so the evaluator is unit-testable
//! without a live model.

use std::collections::HashMap;
use std::sync::{Arc, OnceLock};
use std::time::{Duration, Instant};

use async_trait::async_trait;
use dashmap::DashMap;
use sqlx::PgPool;
use tokio::sync::RwLock;

/// What a policy does to a violating request in `enforce` mode.
pub const ACTION_BLOCK: &str = "block";
pub const ACTION_WARN: &str = "warn";
/// Rollout modes.
pub const MODE_SHADOW: &str = "shadow";
pub const MODE_ENFORCE: &str = "enforce";

/// A semantic policy row (Rust mirror of `semantic_policies`).
#[derive(Debug, Clone)]
pub struct SemanticPolicy {
    pub id: String,
    pub name: String,
    pub constraint_text: String,
    pub action: String, // block | warn
    pub mode: String,   // shadow | enforce
    pub surfaces: Option<Vec<String>>,
    pub judge_model_id: Option<String>,
    /// What happens when the judge cannot render a verdict.
    /// `false` (the default) refuses the call; `true` logs and allows.
    /// Distinct from `mode = shadow`, which is about real violations —
    /// this is only about the judge being unavailable.
    pub fail_open: bool,
}

impl SemanticPolicy {
    fn applies_to(&self, surface: &str) -> bool {
        match &self.surfaces {
            // No surface list → applies to every policy surface.
            None => true,
            Some(list) => list.iter().any(|s| s == surface),
        }
    }
}

/// Request context the judge sees alongside the content.
#[derive(Debug, Clone, Default)]
pub struct PolicyContext {
    pub surface: String,
    pub actor_agent_id: Option<String>,
    pub action_type: Option<String>,
    pub target: Option<String>,
    pub subject: Option<String>,
}

/// The judge's verdict for one (policy, content) pair.
#[derive(Debug, Clone)]
pub struct JudgeVerdict {
    pub violated: bool,
    pub reason: String,
}

/// Final decision after evaluating all applicable policies.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PolicyDecision {
    Allow,
    Warn,
    Block,
}

/// Outcome of an SGP evaluation. `shadow_hits` are would-block verdicts from
/// policies in shadow mode (logged, not enforced).
#[derive(Debug, Clone)]
pub struct SemanticOutcome {
    pub decision: PolicyDecision,
    pub matched_policy_id: Option<String>,
    pub reason: Option<String>,
    pub shadow_hits: Vec<(String, String)>, // (policy_id, reason)
}

impl SemanticOutcome {
    fn allow() -> Self {
        Self {
            decision: PolicyDecision::Allow,
            matched_policy_id: None,
            reason: None,
            shadow_hits: Vec::new(),
        }
    }
}

// ---------------------------------------------------------------------------
// Judge trait + production self-call implementation
// ---------------------------------------------------------------------------

/// Evaluates one constraint against content+context using a model. Abstracted
/// so the evaluator is unit-testable without a live LLM.
#[async_trait]
pub trait Judge: Send + Sync {
    async fn evaluate(
        &self,
        model_id: &str,
        constraint: &str,
        content: &str,
        ctx: &PolicyContext,
        auth_headers: &[(String, String)],
    ) -> Result<JudgeVerdict, String>;
}

/// Builds the judge prompt. Pure + unit-testable. The judge must reply with a
/// single strict-JSON object `{"violated": bool, "reason": string}`.
pub fn build_judge_prompt(constraint: &str, content: &str, ctx: &PolicyContext) -> String {
    let mut envelope = format!("surface={}", ctx.surface);
    if let Some(a) = &ctx.actor_agent_id {
        envelope.push_str(&format!("\nactor_agent={a}"));
    }
    if let Some(a) = &ctx.action_type {
        envelope.push_str(&format!("\naction={a}"));
    }
    if let Some(t) = &ctx.target {
        envelope.push_str(&format!("\ntarget={t}"));
    }
    if let Some(s) = &ctx.subject {
        envelope.push_str(&format!("\non_behalf_of={s}"));
    }
    format!(
        "You are a governance policy judge. Decide whether the REQUEST violates the POLICY.\n\
         Answer with ONLY a strict JSON object: {{\"violated\": <true|false>, \"reason\": \"<short>\"}}.\n\
         Do not include any other text.\n\n\
         POLICY:\n{constraint}\n\n\
         REQUEST CONTEXT:\n{envelope}\n\n\
         REQUEST CONTENT:\n{content}"
    )
}

/// What an unavailable judge means, as a pure function.
///
/// Extracted from the call site so the decision can be tested without a live
/// judge, a database or an alert sink — the branch that decides whether a
/// broken control blocks traffic is exactly the one that must not rely on
/// integration coverage.
///
/// `violated: true` on the fail-closed path is set *because fail-closed made
/// it so*, not because the content was judged. The reason string carries that
/// distinction into the audit row, mirroring how the guardrail orchestrator
/// reports a detector error.
pub fn verdict_for_unavailable_judge(fail_open: bool, error: &str) -> JudgeVerdict {
    if fail_open {
        JudgeVerdict {
            violated: false,
            reason: format!("judge unavailable, policy allows on failure: {error}"),
        }
    } else {
        JudgeVerdict {
            violated: true,
            reason: format!("judge unavailable (fail-closed): {error}"),
        }
    }
}

/// Parse the judge's reply into a verdict. Tolerant: extracts the first
/// JSON object even when the judge wraps it in prose or fences.
///
/// Returns `None` when no verdict can be extracted. That is a judge
/// *failure*, not a "no violation" verdict — the caller routes it into the
/// same path as a judge call error so one place decides what an unavailable
/// judge means, governed by the policy's `fail_open`.
pub fn parse_judge_reply(reply: &str) -> Option<JudgeVerdict> {
    let parse_slice = |s: &str| -> Option<JudgeVerdict> {
        let v: serde_json::Value = serde_json::from_str(s).ok()?;
        let violated = v.get("violated")?.as_bool()?;
        let reason = v
            .get("reason")
            .and_then(|r| r.as_str())
            .unwrap_or("")
            .to_string();
        Some(JudgeVerdict { violated, reason })
    };
    if let Some(v) = parse_slice(reply.trim()) {
        return Some(v);
    }
    // Extract the first {...} span (judges sometimes wrap JSON in prose/fences).
    if let (Some(start), Some(end)) = (reply.find('{'), reply.rfind('}')) {
        if end > start {
            if let Some(v) = parse_slice(&reply[start..=end]) {
                return Some(v);
            }
        }
    }
    None
}

/// The judge's self-call: the chat request body and the headers it is sent
/// with — what the surface forwarded for the caller (`forwarded`: the
/// credential, the tenant and the run, `llm_helpers::judge_self_call_headers`)
/// and this process's assertion that the call is the judge's
/// (`internal_call::token`), which is this function's to set and never a
/// forwarded value. The one way the call is built.
pub fn judge_self_call(
    model_id: &str,
    constraint: &str,
    content: &str,
    ctx: &PolicyContext,
    forwarded: &[(String, String)],
) -> (serde_json::Value, Vec<(String, String)>) {
    let prompt = build_judge_prompt(constraint, content, ctx);
    let body = serde_json::json!({
        // By id, not name: two configured models can share a name,
        // and name resolution picks one without regard to which the
        // operator chose as judge.
        "model_id": model_id,
        "messages": [{ "role": "user", "content": prompt }],
        "temperature": 0,
        "max_tokens": 200,
    });
    let marker = crate::constants::headers::X_BRUTOR_SEMANTIC_POLICY;
    let mut headers: Vec<(String, String)> = forwarded
        .iter()
        .filter(|(name, _)| !name.eq_ignore_ascii_case(marker))
        .cloned()
        .collect();
    headers.push((
        marker.to_string(),
        crate::services::internal_call::token().to_string(),
    ));
    (body, headers)
}

/// Production judge: self-calls the gateway's own governed chat endpoint.
pub struct SelfCallJudge {
    pub http_client: reqwest::Client,
}

#[async_trait]
impl Judge for SelfCallJudge {
    async fn evaluate(
        &self,
        model_id: &str,
        constraint: &str,
        content: &str,
        ctx: &PolicyContext,
        auth_headers: &[(String, String)],
    ) -> Result<JudgeVerdict, String> {
        let (body, headers) = judge_self_call(model_id, constraint, content, ctx, auth_headers);
        let mut req = self
            .http_client
            .post(format!(
                "{}{}",
                crate::constants::self_call::BASE_URL,
                crate::constants::routes::LLM_CHAT_COMPLETIONS
            ))
            .json(&body);
        for (name, value) in &headers {
            req = req.header(name.as_str(), value.as_str());
        }
        let resp = req
            .send()
            .await
            .map_err(|e| format!("judge self-call failed: {e}"))?;
        if !resp.status().is_success() {
            return Err(format!("judge model returned {}", resp.status()));
        }
        let v: serde_json::Value = resp
            .json()
            .await
            .map_err(|e| format!("judge response decode failed: {e}"))?;
        let reply = v
            .get("choices")
            .and_then(|c| c.get(0))
            .and_then(|c| c.get("message"))
            .and_then(|m| m.get("content"))
            .and_then(|c| c.as_str())
            .unwrap_or("");
        parse_judge_reply(reply).ok_or_else(|| "judge reply unparseable".to_string())
    }
}

// ---------------------------------------------------------------------------
// Policy cache (TTL, mirrors GuardrailsCache)
// ---------------------------------------------------------------------------

struct CacheEntry {
    policies: Vec<SemanticPolicy>,
    loaded_at: Instant,
}

pub struct SemanticPolicyCache {
    cache: Arc<RwLock<HashMap<String, CacheEntry>>>,
    ttl_secs: u64,
}

impl SemanticPolicyCache {
    pub fn new(ttl_secs: u64) -> Self {
        Self {
            cache: Arc::new(RwLock::new(HashMap::new())),
            ttl_secs,
        }
    }

    /// Load enabled policies that apply to this tenant + resolved group: those
    /// bound to the group, plus tenant-default policies (no group binding).
    pub async fn get_policies(
        &self,
        tenant_id: &str,
        group_id: Option<&str>,
        db: &PgPool,
    ) -> Result<Vec<SemanticPolicy>, sqlx::Error> {
        let cache_key = match group_id {
            Some(g) => format!("{tenant_id}:g:{g}"),
            None => format!("{tenant_id}:__default__"),
        };
        {
            let cache = self.cache.read().await;
            if let Some(entry) = cache.get(&cache_key) {
                if entry.loaded_at.elapsed().as_secs() < self.ttl_secs {
                    return Ok(entry.policies.clone());
                }
            }
        }

        // A policy applies if it has no group binding (tenant default) OR is
        // bound to the caller's resolved group.
        let rows: Vec<(
            String,
            String,
            String,
            String,
            String,
            Option<serde_json::Value>,
            Option<String>,
            bool,
        )> = sqlx::query_as(
            "SELECT DISTINCT sp.id, sp.name, sp.constraint_text, sp.action, sp.mode, \
                    sp.surfaces, sp.judge_model_id, sp.fail_open \
             FROM semantic_policies sp \
             LEFT JOIN resource_group_semantic_policies b ON b.semantic_policy_id = sp.id \
             WHERE sp.tenant_id = $1 AND sp.enabled = true \
               AND (b.semantic_policy_id IS NULL OR b.resource_group_id = $2)",
        )
        .bind(tenant_id)
        .bind(group_id.unwrap_or(""))
        .fetch_all(db)
        .await?;

        let policies: Vec<SemanticPolicy> = rows
            .into_iter()
            .map(
                |(id, name, constraint_text, action, mode, surfaces, judge_model_id, fail_open)| {
                    let surfaces = surfaces.and_then(|v| {
                        v.as_array().map(|a| {
                            a.iter()
                                .filter_map(|x| x.as_str().map(str::to_owned))
                                .collect::<Vec<_>>()
                        })
                    });
                    SemanticPolicy {
                        id,
                        name,
                        constraint_text,
                        action,
                        mode,
                        surfaces,
                        judge_model_id,
                        fail_open,
                    }
                },
            )
            .collect();

        {
            let mut cache = self.cache.write().await;
            cache.insert(
                cache_key,
                CacheEntry {
                    policies: policies.clone(),
                    loaded_at: Instant::now(),
                },
            );
        }
        Ok(policies)
    }

    /// Drop all cached policies (called on config-change invalidation).
    pub async fn invalidate_all(&self) {
        self.cache.write().await.clear();
    }

    /// Drop one tenant's cached policies.
    ///
    /// The control plane publishes a `semantic_policy`
    /// invalidation when a policy is created, edited or deleted; the proxy had
    /// no arm for it, so an operator who tightened a policy kept serving the
    /// old one until the TTL lapsed. Keyed by the same `tenant:` /
    /// `tenant:g:group` prefixes `get_policies` writes, so one tenant's edit
    /// does not drop every tenant's cache.
    pub async fn invalidate_tenant(&self, tenant_id: &str) {
        let prefix = format!("{tenant_id}:");
        self.cache
            .write()
            .await
            .retain(|k, _| !k.starts_with(&prefix));
    }
}

// ---------------------------------------------------------------------------
// Verdict memo (short-TTL, process-global) — bounds judge cost
// ---------------------------------------------------------------------------

const VERDICT_TTL: Duration = Duration::from_secs(60);

#[allow(clippy::type_complexity)]
static VERDICT_MEMO: OnceLock<DashMap<u64, (Instant, JudgeVerdict)>> = OnceLock::new();
fn verdict_memo() -> &'static DashMap<u64, (Instant, JudgeVerdict)> {
    VERDICT_MEMO.get_or_init(DashMap::new)
}

fn verdict_key(policy_id: &str, model: &str, ctx: &PolicyContext, content: &str) -> u64 {
    use std::hash::{Hash, Hasher};
    let mut h = std::collections::hash_map::DefaultHasher::new();
    policy_id.hash(&mut h);
    model.hash(&mut h);
    ctx.surface.hash(&mut h);
    ctx.actor_agent_id.hash(&mut h);
    ctx.target.hash(&mut h);
    content.hash(&mut h);
    h.finish()
}

// ---------------------------------------------------------------------------
// Evaluator
// ---------------------------------------------------------------------------

/// How a *violating* policy affects the running decision. Pure — depends only
/// on the policy's mode + action.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ViolationEffect {
    /// enforce + block → terminal block.
    BlockNow,
    /// enforce + warn → degrade to warn (keep scanning for a block).
    Warn,
    /// shadow (any action) → record a would-block, never enforce.
    Shadow,
}

pub fn violation_effect(mode: &str, action: &str) -> ViolationEffect {
    if mode == MODE_ENFORCE {
        if action == ACTION_WARN {
            ViolationEffect::Warn
        } else {
            ViolationEffect::BlockNow
        }
    } else {
        ViolationEffect::Shadow
    }
}

/// Resolve the judge model NAME for a policy, in precedence order:
///   1. the policy's own `judge_model_id` (if enabled),
///   2. the tenant-wide default judge model (`tenants.default_judge_model_id`),
///   3. the deployment-level `BRUTOR_JUDGE_MODEL` (`fallback_model`), mirroring
///      `BRUTOR_EMBEDDING_MODEL` for the semantic cache.
///      `None` when none is resolvable — the caller then fails open AND raises a
///      system alert, because a policy that isn't enforcing must not be silent.
///
/// Returns the model's **id**: the judge self-call names the exact
/// configured model, never a name that another row may share.
async fn resolve_judge_model(
    db: &PgPool,
    tenant_id: &str,
    judge_model_id: Option<&str>,
    fallback_model: Option<&str>,
) -> Option<String> {
    // 1. Per-policy pinned judge.
    if let Some(id) = judge_model_id {
        if let Ok(Some(name)) = sqlx::query_scalar::<_, String>(concat!(
            "SELECT id FROM llm_model WHERE id = $1 AND tenant_id = $2 AND ",
            crate::model_available_sql!("")
        ))
        .bind(id)
        .bind(tenant_id)
        .fetch_optional(db)
        .await
        {
            return Some(name);
        }
    }

    // 2. Tenant-wide default judge model.
    if let Ok(Some(name)) = sqlx::query_scalar::<_, String>(concat!(
        "SELECT m.id FROM tenants t \
             JOIN llm_model m ON m.id = t.default_judge_model_id \
             WHERE t.id = $1 AND ",
        crate::model_available_sql!("m.")
    ))
    .bind(tenant_id)
    .fetch_optional(db)
    .await
    {
        return Some(name);
    }

    // 3. Deployment-level default (`BRUTOR_JUDGE_MODEL`), mirroring how the
    //    semantic cache falls back to `BRUTOR_EMBEDDING_MODEL`.
    //
    //    This replaces an earlier "first enabled chat model, ordered by
    //    name" fallback. That was the one remaining way a policy could
    //    silently misbehave: the pick changed whenever a model was added
    //    or renamed, it could land on an expensive frontier model — or on
    //    one no resource group had bound, which 403s and fails the policy
    //    open. A judge is an operator decision, so it now comes only from
    //    an operator: the policy, the tenant default, or this env var.
    if let Some(name) = fallback_model {
        if let Ok(Some(found)) = sqlx::query_scalar::<_, String>(concat!(
            "SELECT id FROM llm_model WHERE tenant_id = $1 AND model_name = $2 AND ",
            crate::model_available_sql!(""),
            " ORDER BY id LIMIT 1"
        ))
        .bind(tenant_id)
        .bind(name)
        .fetch_optional(db)
        .await
        {
            return Some(found);
        }
        tracing::warn!(
            tenant_id,
            judge_model = name,
            "BRUTOR_JUDGE_MODEL is set but no enabled model of that name exists for this tenant",
        );
    }

    None
}

/// Load the applicable policies for (tenant, group) and evaluate them — the
/// one-call entry every request surface uses. Returns `Allow` (no work) when no
/// policy is bound or the load fails (fail-open). `content` empty → `Allow`.
#[allow(clippy::too_many_arguments)]
pub async fn load_and_evaluate(
    cache: &SemanticPolicyCache,
    judge: &dyn Judge,
    db: &PgPool,
    tenant_id: &str,
    group_id: Option<&str>,
    content: &str,
    ctx: &PolicyContext,
    auth_headers: &[(String, String)],
    judge_fallback_model: Option<&str>,
) -> SemanticOutcome {
    if content.is_empty() {
        return SemanticOutcome::allow();
    }
    let policies = match cache.get_policies(tenant_id, group_id, db).await {
        Ok(p) if !p.is_empty() => p,
        _ => return SemanticOutcome::allow(),
    };
    evaluate(
        db,
        tenant_id,
        &policies,
        content,
        ctx,
        judge,
        auth_headers,
        judge_fallback_model,
    )
    .await
}

/// Evaluate all applicable policies against `content` + `ctx`. Enforce-mode
/// violations short-circuit to Block/Warn; shadow-mode violations are collected
/// in `shadow_hits`. Fail-open on judge/model errors.
#[allow(clippy::too_many_arguments)]
pub async fn evaluate(
    db: &PgPool,
    tenant_id: &str,
    policies: &[SemanticPolicy],
    content: &str,
    ctx: &PolicyContext,
    judge: &dyn Judge,
    auth_headers: &[(String, String)],
    judge_fallback_model: Option<&str>,
) -> SemanticOutcome {
    let mut outcome = SemanticOutcome::allow();
    let mut warn_hit: Option<(String, String)> = None;

    for policy in policies.iter().filter(|p| p.applies_to(&ctx.surface)) {
        let Some(model) = resolve_judge_model(
            db,
            tenant_id,
            policy.judge_model_id.as_deref(),
            judge_fallback_model,
        )
        .await
        else {
            // A governance control that isn't running is worse than one
            // that errors loudly — without an alert this is invisible
            // until someone audits why nothing was ever blocked.
            tracing::warn!(policy = %policy.id, "SGP: no judge model resolvable; failing open");
            crate::services::system_alerts::spawn_raise(
                db.clone(),
                crate::services::system_alerts::SystemAlertInput::resource_health_change(
                    Some(tenant_id.to_string()),
                    "semantic_policy",
                    &policy.id,
                    Some(policy.name.clone()),
                    "unhealthy",
                    format!(
                        "Semantic policy '{}' has no resolvable judge model and is NOT enforcing. \
                         Pin a judge model on the policy, set the tenant default judge model \
                         (Configuration → Tenant → Default models), or set BRUTOR_JUDGE_MODEL.",
                        policy.name
                    ),
                )
                .with_title("Semantic policy not enforcing (no judge model)"),
            );
            continue;
        };

        // Verdict memo (short TTL) to bound cost.
        let key = verdict_key(&policy.id, &model, ctx, content);
        let verdict = if let Some(entry) = verdict_memo().get(&key) {
            if entry.0.elapsed() < VERDICT_TTL {
                entry.1.clone()
            } else {
                drop(entry);
                run_and_memo(
                    db,
                    tenant_id,
                    judge,
                    &model,
                    policy,
                    content,
                    ctx,
                    auth_headers,
                    key,
                )
                .await
            }
        } else {
            run_and_memo(
                db,
                tenant_id,
                judge,
                &model,
                policy,
                content,
                ctx,
                auth_headers,
                key,
            )
            .await
        };

        if !verdict.violated {
            continue;
        }

        match violation_effect(&policy.mode, &policy.action) {
            ViolationEffect::BlockNow => {
                // Block is terminal — return immediately (skip remaining judges).
                return SemanticOutcome {
                    decision: PolicyDecision::Block,
                    matched_policy_id: Some(policy.id.clone()),
                    reason: Some(verdict.reason),
                    shadow_hits: outcome.shadow_hits,
                };
            }
            ViolationEffect::Warn => {
                if warn_hit.is_none() {
                    warn_hit = Some((policy.id.clone(), verdict.reason));
                }
            }
            ViolationEffect::Shadow => {
                outcome
                    .shadow_hits
                    .push((policy.id.clone(), verdict.reason));
            }
        }
    }

    if let Some((pid, reason)) = warn_hit {
        outcome.decision = PolicyDecision::Warn;
        outcome.matched_policy_id = Some(pid);
        outcome.reason = Some(reason);
    }
    outcome
}

#[allow(clippy::too_many_arguments)]
async fn run_and_memo(
    db: &PgPool,
    tenant_id: &str,
    judge: &dyn Judge,
    model: &str,
    policy: &SemanticPolicy,
    content: &str,
    ctx: &PolicyContext,
    auth_headers: &[(String, String)],
    key: u64,
) -> JudgeVerdict {
    match judge
        .evaluate(model, &policy.constraint_text, content, ctx, auth_headers)
        .await
    {
        Ok(v) => {
            verdict_memo().insert(key, (Instant::now(), v.clone()));
            v
        }
        Err(e) => {
            // Fail-open: a judge error must not block traffic. (Loud log; the
            // self-call path also raises a downstream alert.)
            tracing::warn!(policy = %policy.id, error = %e, "SGP judge error; failing open");
            // Surface it: the most common cause is the judge model not
            // being reachable from the calling resource group (403), which
            // otherwise leaves the policy silently non-enforcing. Deduped
            // per policy, so a hot loop raises one alert, not thousands.
            crate::services::system_alerts::spawn_raise(
                db.clone(),
                crate::services::system_alerts::SystemAlertInput::resource_health_change(
                    Some(tenant_id.to_string()),
                    "semantic_policy",
                    &policy.id,
                    Some(policy.name.clone()),
                    "unhealthy",
                    format!(
                        "Semantic policy '{}' is NOT enforcing — its judge call on model \
                         '{model}' failed: {e}",
                        policy.name
                    ),
                )
                .with_title("Semantic policy not enforcing (judge call failed)"),
            );
            // One place decides what an unavailable judge means.
            // Default (fail_open = false) refuses the call: a policy that
            // silently stopped evaluating is indistinguishable from one that
            // approved everything. `violated` is set because fail-closed made
            // it so, not because the content was judged — the reason string
            // carries that distinction into the audit row, exactly as the
            // guardrail orchestrator does for detector errors.
            verdict_for_unavailable_judge(policy.fail_open, &e.to_string())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ctx() -> PolicyContext {
        PolicyContext {
            surface: "a2a_outbound".to_string(),
            actor_agent_id: Some("agent-x".to_string()),
            action_type: Some("a2a_call".to_string()),
            target: Some("card-peer".to_string()),
            subject: Some("user:42".to_string()),
        }
    }

    fn pol(id: &str, mode: &str, action: &str, surfaces: Option<Vec<&str>>) -> SemanticPolicy {
        SemanticPolicy {
            id: id.into(),
            name: id.into(),
            constraint_text: "no PII to external peers".into(),
            action: action.into(),
            mode: mode.into(),
            surfaces: surfaces.map(|v| v.into_iter().map(str::to_owned).collect()),
            judge_model_id: None,
            // Default: an unavailable judge refuses the call.
            fail_open: false,
        }
    }

    #[test]
    fn violation_effect_maps_mode_and_action() {
        assert_eq!(
            violation_effect(MODE_ENFORCE, ACTION_BLOCK),
            ViolationEffect::BlockNow
        );
        assert_eq!(
            violation_effect(MODE_ENFORCE, ACTION_WARN),
            ViolationEffect::Warn
        );
        // shadow never enforces, regardless of action.
        assert_eq!(
            violation_effect(MODE_SHADOW, ACTION_BLOCK),
            ViolationEffect::Shadow
        );
        assert_eq!(
            violation_effect(MODE_SHADOW, ACTION_WARN),
            ViolationEffect::Shadow
        );
    }

    #[test]
    fn prompt_includes_constraint_context_and_content() {
        let p = build_judge_prompt("no secrets", "my content", &ctx());
        assert!(p.contains("no secrets"));
        assert!(p.contains("surface=a2a_outbound"));
        assert!(p.contains("actor_agent=agent-x"));
        assert!(p.contains("my content"));
        assert!(p.contains("violated"));
    }

    #[test]
    fn parse_strict_json() {
        let v = parse_judge_reply(r#"{"violated": true, "reason": "PII present"}"#)
            .expect("well-formed verdict parses");
        assert!(v.violated);
        assert_eq!(v.reason, "PII present");
    }

    #[test]
    fn parse_json_wrapped_in_prose_or_fences() {
        let v =
            parse_judge_reply("Sure!\n```json\n{\"violated\": false, \"reason\": \"clean\"}\n```")
                .expect("fenced JSON still parses");
        assert!(!v.violated);
        assert_eq!(v.reason, "clean");
    }

    #[test]
    fn parse_garbage_is_a_judge_failure_not_a_clean_verdict() {
        // The inversion that mattered. An unparseable reply used to
        // return "not violated", which is indistinguishable from the judge
        // actually approving the content. It is now a failure, so the
        // caller's fail_open decides — and the default is to refuse.
        assert!(
            parse_judge_reply("I cannot determine that.").is_none(),
            "unparseable judge reply must not masquerade as a clean verdict",
        );
    }

    // ------------------------------------------------------------------
    // What an unavailable judge does. This is the branch that decides
    // whether a broken control blocks traffic, so it is pinned directly
    // rather than left to integration coverage.
    // ------------------------------------------------------------------

    #[test]
    fn unavailable_judge_blocks_by_default() {
        let v = verdict_for_unavailable_judge(false, "connection refused");
        assert!(
            v.violated,
            "a judge that cannot render a verdict must refuse the call",
        );
        assert!(
            v.reason.contains("fail-closed"),
            "the audit row must say the block came from fail-closed, not from              a judgement about the content: {}",
            v.reason,
        );
        assert!(
            v.reason.contains("connection refused"),
            "cause must survive"
        );
    }

    #[test]
    fn unavailable_judge_allows_when_operator_opted_in() {
        let v = verdict_for_unavailable_judge(true, "timeout");
        assert!(!v.violated, "fail_open=true is log-and-allow");
        assert!(
            v.reason.contains("policy allows on failure"),
            "the row must record that this was an operator choice: {}",
            v.reason,
        );
        assert!(v.reason.contains("timeout"), "cause must survive");
    }

    #[test]
    fn the_two_failure_paths_are_distinguishable_in_audit() {
        // An operator reading proxy_logs must be able to tell "we chose to
        // allow this" from "the content was judged clean".
        let closed = verdict_for_unavailable_judge(false, "e");
        let open = verdict_for_unavailable_judge(true, "e");
        assert_ne!(closed.violated, open.violated);
        assert_ne!(closed.reason, open.reason);
    }

    #[test]
    fn an_unparseable_reply_reaches_the_same_decision() {
        // Unparseable must not return "not
        // violated" directly, bypassing fail_open entirely. It is a
        // failure, so the default refuses.
        assert!(parse_judge_reply("no json here").is_none());
        assert!(verdict_for_unavailable_judge(false, "judge reply unparseable").violated);
    }

    #[test]
    fn fail_open_defaults_to_closed_on_the_policy_struct() {
        // The default fail-closed judging rests on. If this flips, an
        // unavailable judge silently stops enforcing again.
        assert!(
            !pol("p", MODE_ENFORCE, ACTION_BLOCK, None).fail_open,
            "a policy must refuse the call when its judge is unavailable",
        );
    }

    #[test]
    fn fail_open_is_operator_selectable() {
        // The opt-out the operator asked for: log-and-allow, per policy.
        let mut p = pol("p", MODE_ENFORCE, ACTION_BLOCK, None);
        p.fail_open = true;
        assert!(p.fail_open);
    }

    #[test]
    fn applies_to_respects_surface_list() {
        assert!(pol("p", MODE_SHADOW, ACTION_BLOCK, None).applies_to("anything"));
        assert!(
            pol("p", MODE_SHADOW, ACTION_BLOCK, Some(vec!["a2a_outbound"]))
                .applies_to("a2a_outbound")
        );
        assert!(
            !pol("p", MODE_SHADOW, ACTION_BLOCK, Some(vec!["chat_input"]))
                .applies_to("a2a_outbound")
        );
    }
}
