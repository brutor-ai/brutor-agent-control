// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Continuous Assurance Checks — tier-A evaluator.
//!
//! This is the bounded CEL profile, built ledger-first: expressions
//! evaluate against the pinned `run` facts object
//! ([`crate::services::run_ledger::RUN_FACT_KEYS`]) as a follower of run
//! finalization — read-only, off the hot path, backtestable.
//!
//! The trust boundary, enforced here and nowhere else:
//!
//! * **Profile, not CEL.** Result must be typed per the spec's declared
//!   output; only whitelisted functions; only the `run` variable; source
//!   length capped. An expression from an Istio config will not
//!   necessarily compile, by design.
//! * **One engine.** The control plane never evaluates CEL — it calls this
//!   process (`/v1/assurance/checks/compile|backtest`) so author-time
//!   validation and runtime evaluation can never disagree.
//! * **Fail-open toward the ledger.** A check failure (bad spec, DB error)
//!   logs and moves on; finalization never breaks. Per the error taxonomy
//!   an operator-authored expression failing is WARN, not ERROR.

use std::collections::HashMap;
use std::sync::{Arc, OnceLock, RwLock};
use std::time::{Duration, Instant};

use serde_json::{Value, json};
use sqlx::PgPool;
use ulid::Ulid;

use crate::services::run_ledger;

/// Max expression source length (bytes).
pub const MAX_EXPRESSION_BYTES: usize = 4096;

/// How long a tenant's compiled spec set is served before reloading.
/// Checks change rarely; ≤30 s propagation is the documented contract.
const SPEC_CACHE_TTL: Duration = Duration::from_secs(30);

/// Backtest bounds: window and per-run sample rows persisted.
pub const BACKTEST_MAX_RUNS: i64 = 10_000;
pub const BACKTEST_SAMPLE_ROWS: usize = 50;

/// The function whitelist (ledger profile). Comprehension
/// macros (`exists`, `all`, …) surface as function references in the cel
/// AST, so they are listed alongside plain functions. Everything else —
/// including any future stdlib addition in the `cel` crate — is refused
/// at compile time, never discovered at runtime.
pub const ALLOWED_FUNCTIONS: &[&str] = &[
    "has",
    "size",
    "exists",
    "exists_one",
    "all",
    "filter",
    "map",
    "contains",
    "matches",
    "startsWith",
    "endsWith",
    "string",
    "int",
    "double",
    "uint",
];

/// The only variable an expression may reference.
pub const CONTEXT_VARIABLE: &str = "run";

pub const OUTPUT_BOOLEAN: &str = "boolean";
pub const OUTPUT_CLASSIFICATION: &str = "classification";
pub const OUTPUT_SCORE: &str = "score";
pub const OUTPUT_COUNT: &str = "count";

// ---------------------------------------------------------------------------
// Compile (profile enforcement)
// ---------------------------------------------------------------------------

/// Compile an expression under the bounded profile.
///
/// Errors are operator-facing sentences — they end up verbatim in the
/// authoring UI.
pub fn compile_expression(expression: &str) -> Result<cel::Program, Vec<String>> {
    if expression.trim().is_empty() {
        return Err(vec!["expression is empty".to_string()]);
    }
    if expression.len() > MAX_EXPRESSION_BYTES {
        return Err(vec![format!(
            "expression is {} bytes; the profile caps source at {} bytes",
            expression.len(),
            MAX_EXPRESSION_BYTES
        )]);
    }
    let program = match cel::Program::compile(expression) {
        Ok(p) => p,
        Err(e) => return Err(vec![format!("parse error: {e}")]),
    };

    let mut errors = Vec::new();
    // Comprehension-bound identifiers (`m` in `all(m, ...)`, plus the
    // internal accumulator) are lexically scoped, not context variables —
    // the crate's `references()` reports them as free, so subtract them.
    let mut bound: std::collections::HashSet<String> = std::collections::HashSet::new();
    collect_bound_vars(program.expression(), &mut bound);
    let refs = program.references();
    for var in refs.variables() {
        if var != CONTEXT_VARIABLE && !bound.contains(var) && !var.starts_with("__") {
            errors.push(format!(
                "unknown variable '{var}' — only '{CONTEXT_VARIABLE}' is \
                 available at the ledger evaluation point"
            ));
        }
    }
    for func in refs.functions() {
        // Operators surface as function references with mangled names
        // (`_&&_`, `_>_`, `!_`, `-_`, `_?_:_`, `_[_]`, `@in`). Comparison
        // and logic are part of the profile; only *named* functions go
        // through the whitelist.
        let is_operator = func.starts_with('_')
            || func.starts_with('!')
            || func.starts_with('-')
            || func.starts_with('@')
            || func == "in";
        if !is_operator && !ALLOWED_FUNCTIONS.contains(&func) {
            errors.push(format!("function '{func}' is outside the bounded profile"));
        }
    }
    if errors.is_empty() {
        Ok(program)
    } else {
        Err(errors)
    }
}

/// Walk the AST collecting every comprehension-bound identifier.
fn collect_bound_vars(expr: &cel::IdedExpr, out: &mut std::collections::HashSet<String>) {
    use cel::common::ast::{EntryExpr, Expr};
    match &expr.expr {
        Expr::Call(c) => {
            if let Some(target) = &c.target {
                collect_bound_vars(target, out);
            }
            for arg in &c.args {
                collect_bound_vars(arg, out);
            }
        }
        Expr::Comprehension(c) => {
            out.insert(c.iter_var.clone());
            if let Some(v2) = &c.iter_var2 {
                out.insert(v2.clone());
            }
            out.insert(c.accu_var.clone());
            collect_bound_vars(&c.iter_range, out);
            collect_bound_vars(&c.accu_init, out);
            collect_bound_vars(&c.loop_cond, out);
            collect_bound_vars(&c.loop_step, out);
            collect_bound_vars(&c.result, out);
        }
        Expr::List(l) => {
            for e in &l.elements {
                collect_bound_vars(e, out);
            }
        }
        Expr::Map(m) => {
            for entry in &m.entries {
                match &entry.expr {
                    EntryExpr::StructField(f) => collect_bound_vars(&f.value, out),
                    EntryExpr::MapEntry(me) => {
                        collect_bound_vars(&me.key, out);
                        collect_bound_vars(&me.value, out);
                    }
                }
            }
        }
        Expr::Select(s) => collect_bound_vars(&s.operand, out),
        Expr::Struct(s) => {
            for entry in &s.entries {
                match &entry.expr {
                    EntryExpr::StructField(f) => collect_bound_vars(&f.value, out),
                    EntryExpr::MapEntry(me) => {
                        collect_bound_vars(&me.key, out);
                        collect_bound_vars(&me.value, out);
                    }
                }
            }
        }
        Expr::Unspecified | Expr::Ident(_) | Expr::Literal(_) => {}
    }
}

/// Referenced `run.*` field check: every top-level member access on `run`
/// must be a pinned fact key. Best-effort — nested/dynamic access is let
/// through (the evaluator's always-present keys make NoSuchKey impossible
/// for the pinned set anyway).
pub fn unknown_fact_fields(expression: &str) -> Vec<String> {
    let mut unknown = Vec::new();
    let bytes = expression.as_bytes();
    let needle = b"run.";
    let mut i = 0;
    while i + needle.len() <= bytes.len() {
        if &bytes[i..i + needle.len()] == needle
            && (i == 0 || !(bytes[i - 1].is_ascii_alphanumeric() || bytes[i - 1] == b'_'))
        {
            let start = i + needle.len();
            let mut end = start;
            while end < bytes.len() && (bytes[end].is_ascii_alphanumeric() || bytes[end] == b'_') {
                end += 1;
            }
            if end > start {
                let field = &expression[start..end];
                if !run_ledger::RUN_FACT_KEYS.contains(&field)
                    && !unknown.iter().any(|u| u == field)
                {
                    unknown.push(field.to_string());
                }
            }
            i = end;
        } else {
            i += 1;
        }
    }
    unknown
}

/// Evaluate a compiled program against a facts object.
pub fn evaluate(program: &cel::Program, facts: &Value) -> Result<Value, String> {
    let mut ctx = cel::Context::default();
    let cel_facts = cel::to_value(facts).map_err(|e| format!("context build failed: {e}"))?;
    ctx.add_variable_from_value(CONTEXT_VARIABLE, cel_facts);
    let out = program.execute(&ctx).map_err(|e| e.to_string())?;
    out.json()
        .map_err(|e| format!("untranslatable result: {e}"))
}

/// Does a produced value match the spec's declared output type?
pub fn value_matches_output(value: &Value, output: &str) -> bool {
    match output {
        OUTPUT_BOOLEAN => value.is_boolean(),
        OUTPUT_COUNT => value.is_i64() || value.is_u64(),
        OUTPUT_SCORE => value.as_f64().is_some_and(|f| (0.0..=1.0).contains(&f)),
        OUTPUT_CLASSIFICATION => value.is_string(),
        // `extraction` is tier-B only; a deterministic spec declaring it is
        // refused upstream, but be permissive here.
        _ => true,
    }
}

// ---------------------------------------------------------------------------
// Thresholds
// ---------------------------------------------------------------------------

/// A value threshold, evaluated per result. (`rate_over` thresholds are
/// windowed aggregations, evaluated by the CP scheduler over stored
/// results — deliberately not here.)
#[derive(Debug, Clone)]
pub struct ValueThreshold {
    pub op: String,
    pub value: Value,
    pub severity: String,
}

pub fn parse_value_threshold(threshold: Option<&Value>) -> Option<ValueThreshold> {
    let t = threshold?;
    if t.get("kind").and_then(|k| k.as_str()) != Some("value") {
        return None;
    }
    Some(ValueThreshold {
        op: t
            .get("op")
            .and_then(|o| o.as_str())
            .unwrap_or("eq")
            .to_string(),
        value: t.get("value").cloned().unwrap_or(Value::Bool(true)),
        severity: t
            .get("severity")
            .and_then(|s| s.as_str())
            .unwrap_or("warning")
            .to_string(),
    })
}

pub fn threshold_breached(t: &ValueThreshold, value: &Value) -> bool {
    match t.op.as_str() {
        "eq" => value == &t.value,
        "ne" => value != &t.value,
        op => {
            let (Some(a), Some(b)) = (value.as_f64(), t.value.as_f64()) else {
                return false;
            };
            match op {
                "gt" => a > b,
                "gte" => a >= b,
                "lt" => a < b,
                "lte" => a <= b,
                _ => false,
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Spec cache (per tenant, TTL)
// ---------------------------------------------------------------------------

/// One enabled, compiled, on-finalize deterministic check.
pub struct CompiledCheck {
    pub id: String,
    pub name: String,
    pub version: i32,
    pub spec_hash: String,
    pub output: String,
    pub route_to_inbox: bool,
    /// Empty = whole estate.
    pub scope_systems: Vec<String>,
    pub threshold: Option<ValueThreshold>,
    pub program: cel::Program,
}

type SpecCache = RwLock<HashMap<String, (Instant, Arc<Vec<Arc<CompiledCheck>>>)>>;

fn spec_cache() -> &'static SpecCache {
    static CACHE: OnceLock<SpecCache> = OnceLock::new();
    CACHE.get_or_init(|| RwLock::new(HashMap::new()))
}

/// Drop a tenant's cached specs.
///
/// Nothing calls this in the binary, and that is the honest state
/// rather than an oversight waiting to be wired: the control plane publishes
/// invalidations for `model`, `mcp_server`, `agent_card`, `guardrail`,
/// `resource_group`, `routing_group` and `llm_provider`, and a check-spec edit
/// is none of those. Adding a dispatcher arm for a message nobody sends would
/// look like coverage without being any. So a spec change rides the TTL, which
/// is the documented behaviour; this exists for tests and for the day the
/// control plane grows a publisher, and the moment it does, wire it here.
pub fn invalidate_tenant(tenant_id: &str) {
    let suffix = format!(":{tenant_id}");
    if let Ok(mut map) = spec_cache().write() {
        map.retain(|k, _| !k.ends_with(&suffix));
    }
}

#[derive(sqlx::FromRow)]
struct CheckRow {
    id: String,
    name: String,
    version: i32,
    spec_hash: String,
    scope: Value,
    evaluator: Value,
    output: String,
    threshold: Option<Value>,
    route_to_inbox: bool,
}

fn scope_systems(scope: &Value) -> Vec<String> {
    scope
        .get("ai_system_ids")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|s| s.as_str())
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
}

fn compile_row(row: &CheckRow) -> Option<CompiledCheck> {
    let expression = row.evaluator.get("expression").and_then(|e| e.as_str())?;
    match compile_expression(expression) {
        Ok(program) => Some(CompiledCheck {
            id: row.id.clone(),
            name: row.name.clone(),
            version: row.version,
            spec_hash: row.spec_hash.clone(),
            output: row.output.clone(),
            route_to_inbox: row.route_to_inbox,
            scope_systems: scope_systems(&row.scope),
            threshold: parse_value_threshold(row.threshold.as_ref()),
            program,
        }),
        Err(errors) => {
            // A stored spec that no longer compiles is an operator-config
            // problem (or a profile regression): WARN and skip, never fail
            // finalization over it.
            tracing::warn!(
                check_id = %row.id,
                errors = ?errors,
                "assurance check: stored expression failed to compile; skipping"
            );
            None
        }
    }
}

/// Cache key: database + tenant. The database qualifier matters only for
/// the `#[sqlx::test]` harness (one process, one throwaway DB per test);
/// in production every pool points at the same database.
fn cache_key(db: &PgPool, tenant_id: &str) -> String {
    let dbname = db
        .connect_options()
        .get_database()
        .unwrap_or("")
        .to_string();
    format!("{dbname}:{tenant_id}")
}

async fn load_specs(db: &PgPool, tenant_id: &str) -> Arc<Vec<Arc<CompiledCheck>>> {
    let key = cache_key(db, tenant_id);
    if let Ok(map) = spec_cache().read() {
        if let Some((at, specs)) = map.get(&key) {
            if at.elapsed() < SPEC_CACHE_TTL {
                return Arc::clone(specs);
            }
        }
    }
    let rows: Vec<CheckRow> = sqlx::query_as(
        "SELECT id, name, version, spec_hash, scope, evaluator, output, \
                threshold, route_to_inbox \
           FROM assurance_checks \
          WHERE tenant_id = $1 AND enabled \
            AND tier = 'deterministic' AND trigger = 'on_finalize'",
    )
    .bind(tenant_id)
    .fetch_all(db)
    .await
    .unwrap_or_else(|e| {
        tracing::warn!(error = %e, tenant_id = %tenant_id,
            "assurance check: spec load failed; serving none this tick");
        Vec::new()
    });

    let specs: Arc<Vec<Arc<CompiledCheck>>> = Arc::new(
        rows.iter()
            .filter_map(|r| compile_row(r).map(Arc::new))
            .collect(),
    );
    if let Ok(mut map) = spec_cache().write() {
        map.insert(key, (Instant::now(), Arc::clone(&specs)));
    }
    specs
}

// ---------------------------------------------------------------------------
// Result persistence + inbox routing
// ---------------------------------------------------------------------------

const RESULT_UPSERT_SQL: &str = r#"
INSERT INTO assurance_check_results
    (id, tenant_id, check_id, check_version, run_id, value, evidence,
     evaluator_fingerprint, backtest, evaluated_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, false, NOW())
ON CONFLICT (check_id, check_version, run_id)
    WHERE run_id IS NOT NULL AND NOT backtest
DO UPDATE SET value = EXCLUDED.value,
              evidence = EXCLUDED.evidence,
              evaluated_at = NOW()
RETURNING id
"#;

async fn record_result(
    db: &PgPool,
    tenant_id: &str,
    check: &CompiledCheck,
    run_id: &str,
    value: &Value,
) -> Option<String> {
    let row: Result<(String,), sqlx::Error> = sqlx::query_as(RESULT_UPSERT_SQL)
        .bind(format!("ckr-{}", Ulid::new()))
        .bind(tenant_id)
        .bind(&check.id)
        .bind(check.version)
        .bind(run_id)
        .bind(json!({ "value": value }))
        .bind(json!({ "run_id": run_id }))
        .bind(&check.spec_hash)
        .fetch_one(db)
        .await;
    match row {
        Ok((id,)) => Some(id),
        Err(e) => {
            tracing::warn!(error = %e, check_id = %check.id, run_id = %run_id,
                "assurance check: result insert failed");
            None
        }
    }
}

/// Evaluate every in-scope on-finalize check for a just-finalized run.
/// Never fails; the caller is `finalize_run`.
pub async fn evaluate_on_finalize(
    db: &PgPool,
    tenant_id: &str,
    ai_system_id: Option<&str>,
    run_id: &str,
    facts: &Value,
) {
    let specs = load_specs(db, tenant_id).await;
    if specs.is_empty() {
        return;
    }
    for check in specs.iter() {
        // Scope filter: an estate-wide check sees everything, a scoped one
        // only runs attributed to one of its systems.
        if !check.scope_systems.is_empty() {
            match ai_system_id {
                Some(sys) if check.scope_systems.iter().any(|s| s == sys) => {}
                _ => continue,
            }
        }
        let value = match evaluate(&check.program, facts) {
            Ok(v) => v,
            Err(e) => {
                tracing::warn!(check_id = %check.id, run_id = %run_id, error = %e,
                    "assurance check: evaluation failed; skipping run");
                continue;
            }
        };
        if !value_matches_output(&value, &check.output) {
            tracing::warn!(check_id = %check.id, run_id = %run_id,
                output = %check.output, value = %value,
                "assurance check: result type does not match declared output; skipping");
            continue;
        }
        let result_id = record_result(db, tenant_id, check, run_id, &value).await;
        if check.route_to_inbox {
            if let Some(t) = &check.threshold {
                if threshold_breached(t, &value) {
                    crate::services::assurance_inbox::raise_item(
                        db,
                        tenant_id,
                        ai_system_id,
                        "check_breach",
                        &t.severity,
                        &format!("check_breach:{}:v{}", check.id, check.version),
                        &format!("Check breached: {}", check.name),
                        json!({
                            "check_id": check.id,
                            "check_version": check.version,
                            "run_id": run_id,
                            "result_id": result_id,
                            "value": value,
                        }),
                    )
                    .await;
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Backtest (review-the-compile safety valve)
// ---------------------------------------------------------------------------

#[derive(Debug, serde::Serialize)]
pub struct BacktestSample {
    pub run_id: String,
    pub value: Value,
    pub breached: bool,
}

#[derive(Debug, serde::Serialize)]
pub struct BacktestSummary {
    pub evaluated: usize,
    pub breaches: usize,
    pub errors: usize,
    pub window_days: i32,
    /// Breaching runs first, capped at [`BACKTEST_SAMPLE_ROWS`].
    pub samples: Vec<BacktestSample>,
}

/// Evaluate a spec version against recent recorded runs. Dispatches
/// nothing; results are persisted `backtest = true` and NEVER raise inbox
/// items.
pub async fn backtest(
    db: &PgPool,
    tenant_id: &str,
    check_id: &str,
    version: i32,
    window_days: i32,
) -> Result<BacktestSummary, String> {
    // The immutable snapshot is the source of truth for a version, not the
    // live row (which may already be a later version).
    let snapshot: Option<(Value,)> = sqlx::query_as(
        "SELECT spec FROM assurance_check_versions \
          WHERE tenant_id = $1 AND check_id = $2 AND version = $3",
    )
    .bind(tenant_id)
    .bind(check_id)
    .bind(version)
    .fetch_optional(db)
    .await
    .map_err(|e| format!("version lookup failed: {e}"))?;
    let Some((spec,)) = snapshot else {
        return Err("check version not found".to_string());
    };
    if spec.get("tier").and_then(|t| t.as_str()) != Some("deterministic") {
        return Err("backtest is only defined for deterministic checks".to_string());
    }
    let expression = spec
        .get("evaluator")
        .and_then(|e| e.get("expression"))
        .and_then(|e| e.as_str())
        .ok_or_else(|| "spec has no expression".to_string())?;
    let program = compile_expression(expression)
        .map_err(|errors| format!("expression no longer compiles: {}", errors.join("; ")))?;
    let scope = spec.get("scope").cloned().unwrap_or_else(|| json!({}));
    let systems = scope_systems(&scope);
    let threshold = parse_value_threshold(spec.get("threshold"));
    let output = spec
        .get("output")
        .and_then(|o| o.as_str())
        .unwrap_or("boolean");
    let spec_hash: String = sqlx::query_scalar(
        "SELECT spec_hash FROM assurance_check_versions \
          WHERE tenant_id = $1 AND check_id = $2 AND version = $3",
    )
    .bind(tenant_id)
    .bind(check_id)
    .bind(version)
    .fetch_one(db)
    .await
    .map_err(|e| format!("spec hash lookup failed: {e}"))?;

    let rows = run_ledger::load_run_facts_window(db, tenant_id, window_days, BACKTEST_MAX_RUNS)
        .await
        .map_err(|e| format!("run window load failed: {e}"))?;

    let mut evaluated = 0usize;
    let mut breaches = 0usize;
    let mut errors = 0usize;
    let mut breached_samples: Vec<BacktestSample> = Vec::new();
    let mut clean_samples: Vec<BacktestSample> = Vec::new();

    for row in &rows {
        if !systems.is_empty() {
            match row.ai_system_id.as_deref() {
                Some(sys) if systems.iter().any(|s| s == sys) => {}
                _ => continue,
            }
        }
        let facts = row.to_facts();
        let value = match evaluate(&program, &facts) {
            Ok(v) if value_matches_output(&v, output) => v,
            Ok(_) | Err(_) => {
                errors += 1;
                continue;
            }
        };
        evaluated += 1;
        let breached = threshold
            .as_ref()
            .is_some_and(|t| threshold_breached(t, &value));
        if breached {
            breaches += 1;
            if breached_samples.len() < BACKTEST_SAMPLE_ROWS {
                breached_samples.push(BacktestSample {
                    run_id: row.id.clone(),
                    value,
                    breached,
                });
            }
        } else if clean_samples.len() < BACKTEST_SAMPLE_ROWS {
            clean_samples.push(BacktestSample {
                run_id: row.id.clone(),
                value,
                breached,
            });
        }
    }

    // Breaching runs first; fill the remainder with clean ones.
    let mut samples = breached_samples;
    for s in clean_samples {
        if samples.len() >= BACKTEST_SAMPLE_ROWS {
            break;
        }
        samples.push(s);
    }

    // Persist the sample rows for evidence drill-in, replacing any prior
    // backtest of this version. Marked `backtest`, so they can never trip
    // the live unique key nor any threshold machinery.
    let _ = sqlx::query(
        "DELETE FROM assurance_check_results \
          WHERE check_id = $1 AND check_version = $2 AND backtest",
    )
    .bind(check_id)
    .bind(version)
    .execute(db)
    .await;
    for s in &samples {
        let _ = sqlx::query(
            "INSERT INTO assurance_check_results \
                (id, tenant_id, check_id, check_version, run_id, value, \
                 evidence, evaluator_fingerprint, backtest, evaluated_at) \
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8, true, NOW())",
        )
        .bind(format!("ckr-{}", Ulid::new()))
        .bind(tenant_id)
        .bind(check_id)
        .bind(version)
        .bind(&s.run_id)
        .bind(json!({ "value": s.value }))
        .bind(json!({ "run_id": s.run_id, "backtest": true }))
        .bind(&spec_hash)
        .execute(db)
        .await;
    }

    Ok(BacktestSummary {
        evaluated,
        breaches,
        errors,
        window_days,
        samples,
    })
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::run_ledger::{RUN_FACT_KEYS, sample_run_facts};

    #[test]
    fn sample_facts_match_the_pinned_contract() {
        let facts = sample_run_facts();
        let obj = facts.as_object().unwrap();
        let mut keys: Vec<&str> = obj.keys().map(String::as_str).collect();
        keys.sort_unstable();
        assert_eq!(
            keys, RUN_FACT_KEYS,
            "sample facts drifted from RUN_FACT_KEYS"
        );
    }

    #[test]
    fn a_check_on_the_declared_release_compiles_and_evaluates() {
        // The release facts are tier-A vocabulary: the compile gate admits
        // them and they evaluate against a run's facts.
        let program = compile_expression(
            "run.agent_impl_version != \"1.0.0\" || run.agent_impl_trust != \"declared\" \
             || run.agent_impl_name == \"\" || run.agent_impl_build != \"\" \
             || run.implementation_fingerprint != \"\"",
        )
        .expect("release facts compile");
        assert_eq!(
            evaluate(&program, &sample_run_facts()).unwrap(),
            serde_json::json!(false)
        );
        let mut facts = sample_run_facts();
        facts["agent_impl_version"] = serde_json::json!("1.4.2");
        assert_eq!(evaluate(&program, &facts).unwrap(), serde_json::json!(true));
    }

    #[test]
    fn compiles_customer_sentences() {
        // Each of these is a documented example — the suite doubles as the
        // docs example set.
        for expr in [
            // "Flag runs that were blocked by a guardrail"
            "run.guardrail_block_count > 0",
            // "Flag any run that exceeded 3 delegation hops"
            "run.max_delegation_depth > 3",
            // "Flag expensive runs outside production systems"
            "run.total_cost_usd > 5.0 && run.system_kind != \"integration\"",
            // "Flag runs that used a model outside the approved set"
            "!run.models.all(m, m.startsWith(\"approved-\"))",
            // "Flag failed runs with tool errors"
            "run.terminal_state == \"errored\" && run.tool_error_count > 0",
        ] {
            compile_expression(expr).unwrap_or_else(|e| panic!("'{expr}' should compile: {e:?}"));
        }
    }

    #[test]
    fn profile_refuses_unknown_variables_and_functions() {
        let err = compile_expression("request.model == \"gpt\"").unwrap_err();
        assert!(err[0].contains("unknown variable 'request'"), "{err:?}");

        let err = compile_expression("uuid() == \"x\"").unwrap_err();
        assert!(
            err.iter()
                .any(|e| e.contains("outside the bounded profile")),
            "{err:?}"
        );
    }

    #[test]
    fn profile_refuses_oversized_source() {
        let big = format!("run.action_count > 0 {}", " ".repeat(MAX_EXPRESSION_BYTES));
        let err = compile_expression(&big).unwrap_err();
        assert!(err[0].contains("caps source"), "{err:?}");
    }

    #[test]
    fn evaluates_against_sample_facts() {
        let p = compile_expression("run.turn_count >= 2 && run.contract_present").unwrap();
        let v = evaluate(&p, &sample_run_facts()).unwrap();
        assert_eq!(v, Value::Bool(true));

        let p = compile_expression("run.tool_call_count").unwrap();
        let v = evaluate(&p, &sample_run_facts()).unwrap();
        assert_eq!(v, json!(3));

        let p = compile_expression("run.models.exists(m, m == \"sample-model\")").unwrap();
        assert_eq!(
            evaluate(&p, &sample_run_facts()).unwrap(),
            Value::Bool(true)
        );
    }

    #[test]
    fn unknown_fields_are_reported() {
        let unknown = unknown_fact_fields(
            "run.total_cost_usd > 1.0 && run.no_such_thing == 2 && run.turn_count > 0",
        );
        assert_eq!(unknown, vec!["no_such_thing".to_string()]);
    }

    #[test]
    fn output_typing_is_enforced() {
        assert!(value_matches_output(&json!(true), OUTPUT_BOOLEAN));
        assert!(!value_matches_output(&json!(3), OUTPUT_BOOLEAN));
        assert!(value_matches_output(&json!(3), OUTPUT_COUNT));
        assert!(value_matches_output(&json!(0.5), OUTPUT_SCORE));
        assert!(!value_matches_output(&json!(1.5), OUTPUT_SCORE));
        assert!(value_matches_output(&json!("label"), OUTPUT_CLASSIFICATION));
    }

    #[test]
    fn thresholds_compare_values() {
        let t = parse_value_threshold(Some(&json!({
            "kind": "value", "op": "eq", "value": true, "severity": "critical"
        })))
        .unwrap();
        assert!(threshold_breached(&t, &json!(true)));
        assert!(!threshold_breached(&t, &json!(false)));

        let t = parse_value_threshold(Some(&json!({
            "kind": "value", "op": "gt", "value": 0.7, "severity": "warning"
        })))
        .unwrap();
        assert!(threshold_breached(&t, &json!(0.9)));
        assert!(!threshold_breached(&t, &json!(0.7)));

        // rate_over is the CP scheduler's job, never evaluated per run.
        assert!(
            parse_value_threshold(Some(&json!({
                "kind": "rate_over", "window_hours": 24, "op": "gt",
                "value": 0.05, "severity": "critical"
            })))
            .is_none()
        );
    }

    #[test]
    fn non_boolean_results_stay_typed() {
        // A classification expression: string result.
        let p =
            compile_expression("run.total_cost_usd > 1.0 ? \"expensive\" : \"normal\"").unwrap();
        let v = evaluate(&p, &sample_run_facts()).unwrap();
        assert_eq!(v, json!("normal"));
        assert!(value_matches_output(&v, OUTPUT_CLASSIFICATION));
    }
}
