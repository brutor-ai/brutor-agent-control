// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Run data scope.
//!
//! A run states the records it is for (`X-Brutor-Run-Scope: customer=C0001`,
//! or the run's subject), and an AI System states which tool argument names
//! which kind of record. Every MCP tool call that names a record is compared
//! with the run's scope before it goes upstream: recorded in `audit`, refused
//! in `enforce`.
//!
//! Why the argument is checked: a run that read another
//! customer's record uses the same tools, in the same order, as a good one.
//! Nothing that looked at the run's shape can tell them apart; the argument
//! can.
//!
//! Every evaluated value is written to `ai_system_record_access` as a keyed
//! reference ([`record_ref`]), which is also what the cross-run record reach counts.

use std::collections::BTreeMap;
use std::sync::{Arc, OnceLock};
use std::time::Duration;

use hmac::{Hmac, Mac};
use moka::sync::Cache;
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use sqlx::PgPool;

/// The request header a run's runtime declares its scope with.
pub const SCOPE_HEADER: &str = "x-brutor-run-scope";

/// Values longer than this are not record ids; they are refused as missing.
const MAX_VALUE_LEN: usize = 256;
/// Bounds on a declaration, so a header cannot write unbounded rows.
const MAX_DECLARED_KEYS: usize = 16;
const MAX_DECLARED_VALUES: usize = 64;

/// How long a system's config is served before re-reading. The documented
/// propagation time for assurance configuration.
const CONFIG_TTL: Duration = Duration::from_secs(30);

pub mod mode {
    pub const OFF: &str = "off";
    pub const AUDIT: &str = "audit";
    pub const ENFORCE: &str = "enforce";
}

pub mod source {
    pub const DECLARED: &str = "declared";
    pub const SUBJECT: &str = "subject";
}

/// Wire contract with the control plane (`app/database/models/ai_system_data_scope.py`)
/// and the run facts. Ordered weakest to strictest.
pub mod verdict {
    pub const IN_SCOPE: &str = "in_scope";
    pub const UNBOUND: &str = "unbound";
    pub const CONFLICT: &str = "conflict";
    pub const OUT_OF_SCOPE: &str = "out_of_scope";
    pub const MISSING_ARGUMENT: &str = "missing_argument";
    pub const UNSCOPED_TOOL: &str = "unscoped_tool";

    /// Verdicts counted as `scope_violation_count`.
    pub const VIOLATIONS: [&str; 4] = [OUT_OF_SCOPE, MISSING_ARGUMENT, UNSCOPED_TOOL, CONFLICT];
}

fn rank(v: &str) -> u8 {
    match v {
        verdict::IN_SCOPE => 0,
        verdict::UNBOUND => 1,
        verdict::CONFLICT => 2,
        verdict::OUT_OF_SCOPE => 3,
        verdict::MISSING_ARGUMENT => 4,
        _ => 5,
    }
}

#[derive(Debug, Clone, Deserialize, PartialEq)]
pub struct KeyDef {
    pub key: String,
    #[serde(default = "default_source")]
    pub source: String,
}

fn default_source() -> String {
    source::DECLARED.to_string()
}

#[derive(Debug, Clone, Deserialize, PartialEq)]
pub struct Rule {
    pub tool: String,
    pub argument: String,
    pub key: String,
}

/// One AI System's data-scope configuration.
#[derive(Debug, Clone, PartialEq)]
pub struct Config {
    pub mode: String,
    pub on_unbound_deny: bool,
    pub keys: Vec<KeyDef>,
    pub rules: Vec<Rule>,
    pub unscoped_tools: Vec<String>,
}

impl Config {
    fn key_source(&self, key: &str) -> &str {
        self.keys
            .iter()
            .find(|k| k.key == key)
            .map_or(source::DECLARED, |k| k.source.as_str())
    }

    fn enforcing(&self) -> bool {
        self.mode == mode::ENFORCE
    }
}

#[derive(sqlx::FromRow)]
struct ConfigRow {
    mode: String,
    on_unbound: String,
    keys: Value,
    rules: Value,
    unscoped_tools: Value,
}

impl ConfigRow {
    fn into_config(self) -> Result<Config, String> {
        Ok(Config {
            mode: self.mode,
            on_unbound_deny: self.on_unbound != "allow",
            keys: serde_json::from_value(self.keys).map_err(|e| format!("keys: {e}"))?,
            rules: serde_json::from_value(self.rules).map_err(|e| format!("rules: {e}"))?,
            unscoped_tools: serde_json::from_value(self.unscoped_tools)
                .map_err(|e| format!("unscoped_tools: {e}"))?,
        })
    }
}

type ConfigCache = Cache<(String, String), Arc<Option<Config>>>;

fn cache() -> &'static ConfigCache {
    static C: OnceLock<ConfigCache> = OnceLock::new();
    C.get_or_init(|| {
        Cache::builder()
            .max_capacity(10_000)
            .time_to_live(CONFIG_TTL)
            .build()
    })
}

/// The system's config, or `None` when it has none. An error is returned, not
/// turned into "no config": the caller decides what an unknown config means.
pub async fn load_config(
    db: &PgPool,
    tenant_id: &str,
    ai_system_id: &str,
) -> Result<Arc<Option<Config>>, String> {
    let key = (tenant_id.to_string(), ai_system_id.to_string());
    if let Some(hit) = cache().get(&key) {
        return Ok(hit);
    }
    let row: Option<ConfigRow> = sqlx::query_as(
        "SELECT mode, on_unbound, keys, rules, unscoped_tools FROM ai_system_data_scopes \
          WHERE tenant_id = $1 AND ai_system_id = $2",
    )
    .bind(tenant_id)
    .bind(ai_system_id)
    .fetch_optional(db)
    .await
    .map_err(|e| e.to_string())?;
    let config = match row {
        Some(r) => Some(r.into_config()?),
        None => None,
    };
    let config = Arc::new(config);
    cache().insert(key, Arc::clone(&config));
    Ok(config)
}

/// Drop cached configs (tests, and the control plane's cache invalidation).
pub fn invalidate_all() {
    cache().invalidate_all();
}

// ---------------------------------------------------------------------------
// Pure parts
// ---------------------------------------------------------------------------

/// Parse `customer=C1; account=ACC1,ACC2`. Keys are lower-cased; empty parts and
/// over-long values are dropped; the bounds keep a header from writing
/// unbounded rows.
pub fn parse_declaration(header: &str) -> BTreeMap<String, Vec<String>> {
    let mut out: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for part in header.split(';') {
        let Some((k, v)) = part.split_once('=') else {
            continue;
        };
        let k = k.trim().to_ascii_lowercase();
        if k.is_empty()
            || k.len() > 64
            || !k
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
        {
            continue;
        }
        if out.len() >= MAX_DECLARED_KEYS && !out.contains_key(&k) {
            continue;
        }
        let values: Vec<String> = v
            .split(',')
            .map(str::trim)
            .filter(|x| !x.is_empty() && x.len() <= MAX_VALUE_LEN)
            .take(MAX_DECLARED_VALUES)
            .map(str::to_string)
            .collect();
        if !values.is_empty() {
            let entry = out.entry(k).or_default();
            for v in values {
                if !entry.contains(&v) {
                    entry.push(v);
                }
            }
        }
    }
    out
}

/// Values at a dotted path. `name[]` walks every element of an array. `None`
/// when the path is absent or reaches something that is not a string or
/// number: there is no record id to check.
pub fn values_at(args: &Value, path: &str) -> Option<Vec<String>> {
    let mut current: Vec<&Value> = vec![args];
    for seg in path.split('.') {
        let (name, each) = match seg.strip_suffix("[]") {
            Some(n) => (n, true),
            None => (seg, false),
        };
        let mut next = Vec::new();
        for v in current {
            let child = if name.is_empty() {
                Some(v)
            } else {
                v.get(name)
            };
            match (child, each) {
                (Some(Value::Array(items)), true) => next.extend(items.iter()),
                (Some(_), true) | (None, _) => return None,
                (Some(c), false) => next.push(c),
            }
        }
        current = next;
    }
    let mut out = Vec::with_capacity(current.len());
    for v in current {
        match v {
            Value::String(s) if !s.is_empty() && s.len() <= MAX_VALUE_LEN => out.push(s.clone()),
            Value::Number(n) => out.push(n.to_string()),
            _ => return None,
        }
    }
    (!out.is_empty()).then_some(out)
}

/// Whether `args` has the first segment of `path` at all.
fn names_path(args: &Value, path: &str) -> bool {
    let first = path.split('.').next().unwrap_or("");
    let first = first.strip_suffix("[]").unwrap_or(first);
    first.is_empty() || args.get(first).is_some()
}

fn record_key() -> &'static [u8; 32] {
    static K: OnceLock<[u8; 32]> = OnceLock::new();
    K.get_or_init(|| {
        let secret = crate::env_vars::process()
            .string(&crate::env_vars::ENCRYPTION_KEY)
            .unwrap_or_else(|| {
                tracing::error!(
                    "ENCRYPTION_KEY is not set; record references are keyed with an empty secret"
                );
                String::new()
            });
        let mut h = Sha256::new();
        h.update(b"brutor-record-ref:v1:");
        h.update(secret.as_bytes());
        h.finalize().into()
    })
}

/// A record reference: keyed, so a low-entropy id (a customer number, a
/// national id) cannot be recovered by enumerating candidates.
pub fn record_ref(tenant_id: &str, key: &str, value: &str) -> String {
    let mut mac =
        Hmac::<Sha256>::new_from_slice(record_key()).expect("HMAC accepts any key length");
    mac.update(tenant_id.as_bytes());
    mac.update(&[0]);
    mac.update(key.as_bytes());
    mac.update(&[0]);
    mac.update(value.as_bytes());
    hex::encode(mac.finalize().into_bytes())
}

/// One evaluated value (or, with no value, one evaluated rule).
#[derive(Debug, Clone, PartialEq)]
pub struct Access {
    pub scope_key: String,
    pub value: Option<String>,
    pub verdict: &'static str,
}

/// The outcome of one tool call against the config and the run's scope.
#[derive(Debug, Clone, PartialEq)]
pub struct Evaluation {
    /// Strictest verdict across the matched rules.
    pub verdict: &'static str,
    pub accesses: Vec<Access>,
    /// The rule (or unscoped tool) behind the strictest verdict.
    pub argument: String,
    pub scope_key: String,
}

/// Judge one call. `None` when no rule names the tool and it is not unscoped.
/// `scope` maps each key to its bound values (declared or subject).
pub fn evaluate(
    config: &Config,
    tool: &str,
    args: &Value,
    scope: &BTreeMap<String, Vec<String>>,
) -> Option<Evaluation> {
    evaluate_parts(config, tool, &[args], scope)
}

/// [`evaluate`] over a call whose arguments arrive in several parts (an A2A
/// message's data parts). A rule's values are gathered from every part that
/// has its path; the argument is missing only when no part has it. Every
/// value found is judged, so one in-scope part cannot cover for another.
pub fn evaluate_parts(
    config: &Config,
    tool: &str,
    parts: &[&Value],
    scope: &BTreeMap<String, Vec<String>>,
) -> Option<Evaluation> {
    let mut accesses = Vec::new();
    let mut worst: Option<(&'static str, String, String)> = None;
    let mut note = |v: &'static str, arg: &str, key: &str| {
        if worst.as_ref().is_none_or(|(w, _, _)| rank(v) > rank(w)) {
            worst = Some((v, arg.to_string(), key.to_string()));
        }
    };

    if config.unscoped_tools.iter().any(|t| t == tool) && !scope.is_empty() {
        accesses.push(Access {
            scope_key: String::new(),
            value: None,
            verdict: verdict::UNSCOPED_TOOL,
        });
        note(verdict::UNSCOPED_TOOL, "", "");
    }

    for rule in config.rules.iter().filter(|r| r.tool == tool) {
        let bound = scope.get(&rule.key);
        // A part that names the argument but holds no record id there (an
        // object, an empty list) is missing, even beside a part that does:
        // the tool reads every part.
        let mut found: Option<Vec<String>> = None;
        let mut malformed = false;
        for p in parts {
            match values_at(p, &rule.argument) {
                Some(v) => found.get_or_insert_with(Vec::new).extend(v),
                None => malformed |= names_path(p, &rule.argument),
            }
        }
        if malformed {
            found = None;
        }
        match found {
            None => {
                accesses.push(Access {
                    scope_key: rule.key.clone(),
                    value: None,
                    verdict: verdict::MISSING_ARGUMENT,
                });
                note(verdict::MISSING_ARGUMENT, &rule.argument, &rule.key);
            }
            Some(values) => {
                for v in values {
                    let verdict = match bound {
                        None => verdict::UNBOUND,
                        Some(b) if b.contains(&v) => verdict::IN_SCOPE,
                        Some(_) => verdict::OUT_OF_SCOPE,
                    };
                    note(verdict, &rule.argument, &rule.key);
                    accesses.push(Access {
                        scope_key: rule.key.clone(),
                        value: Some(v),
                        verdict,
                    });
                }
            }
        }
    }

    let (verdict, argument, scope_key) = worst?;
    Some(Evaluation {
        verdict,
        accesses,
        argument,
        scope_key,
    })
}

/// Whether `enforce` refuses this verdict.
pub fn refuses(config: &Config, v: &str) -> bool {
    config.enforcing()
        && match v {
            verdict::IN_SCOPE => false,
            verdict::UNBOUND => config.on_unbound_deny,
            _ => true,
        }
}

// ---------------------------------------------------------------------------
// The run's scope
// ---------------------------------------------------------------------------

/// Store a declaration for a run. The FIRST value of each key binds; a
/// different later value is returned as a conflict and changes nothing.
pub async fn declare(
    db: &PgPool,
    tenant_id: &str,
    root_task_id: &str,
    declared: &BTreeMap<String, Vec<String>>,
) -> Result<Vec<String>, sqlx::Error> {
    let mut conflicts = Vec::new();
    for (key, values) in declared {
        let inserted: Option<(String,)> = sqlx::query_as(
            "INSERT INTO ai_system_run_scopes \
                 (id, tenant_id, root_task_id, scope_key, scope_values, source, declared_at) \
             VALUES ($1, $2, $3, $4, $5, $6, NOW()) \
             ON CONFLICT (tenant_id, root_task_id, scope_key) DO NOTHING \
             RETURNING id",
        )
        .bind(format!("rsc-{}", ulid::Ulid::new()))
        .bind(tenant_id)
        .bind(root_task_id)
        .bind(key)
        .bind(json!(values))
        .bind(source::DECLARED)
        .fetch_optional(db)
        .await?;
        if inserted.is_some() {
            continue;
        }
        let (existing,): (Value,) = sqlx::query_as(
            "SELECT scope_values FROM ai_system_run_scopes \
              WHERE tenant_id = $1 AND root_task_id = $2 AND scope_key = $3",
        )
        .bind(tenant_id)
        .bind(root_task_id)
        .bind(key)
        .fetch_one(db)
        .await?;
        let mut a: Vec<String> = serde_json::from_value(existing).unwrap_or_default();
        let mut b = values.clone();
        a.sort();
        b.sort();
        if a != b {
            conflicts.push(key.clone());
        }
    }
    Ok(conflicts)
}

/// Store the scope a request declares, if it declares one. Returns the keys
/// whose declared value differed from the binding (which is unchanged). A
/// failure to store is logged and returned as no conflict: the binding a tool
/// call is judged against is read separately, and a missing binding is
/// `unbound`, which `enforce` refuses.
pub async fn capture(
    db: &PgPool,
    tenant_id: &str,
    root_task_id: &str,
    header: Option<&str>,
) -> Vec<String> {
    let Some(h) = header else {
        return Vec::new();
    };
    let declared = parse_declaration(h);
    if declared.is_empty() {
        return Vec::new();
    }
    match declare(db, tenant_id, root_task_id, &declared).await {
        Ok(conflicts) => {
            if !conflicts.is_empty() {
                tracing::warn!(
                    tenant_id,
                    root_task_id,
                    keys = ?conflicts,
                    "data scope: a request declared a different scope; the first binding stands"
                );
            }
            conflicts
        }
        Err(e) => {
            crate::services::db_errors::record(&e);
            tracing::warn!(error = %e, "data scope: declaration not stored");
            Vec::new()
        }
    }
}

/// The run's bound scope: declared keys from the store, subject-sourced keys
/// from the caller's subject.
pub async fn run_scope(
    db: &PgPool,
    config: &Config,
    tenant_id: &str,
    root_task_id: &str,
    subject: Option<&str>,
) -> Result<BTreeMap<String, Vec<String>>, sqlx::Error> {
    let rows: Vec<(String, Value)> = sqlx::query_as(
        "SELECT scope_key, scope_values FROM ai_system_run_scopes \
          WHERE tenant_id = $1 AND root_task_id = $2",
    )
    .bind(tenant_id)
    .bind(root_task_id)
    .fetch_all(db)
    .await?;
    let mut out = BTreeMap::new();
    for (key, values) in rows {
        if config.key_source(&key) == source::DECLARED {
            out.insert(key, serde_json::from_value(values).unwrap_or_default());
        }
    }
    for k in config.keys.iter().filter(|k| k.source == source::SUBJECT) {
        if let Some(s) = subject {
            out.insert(k.key.clone(), vec![s.to_string()]);
        }
    }
    Ok(out)
}

/// Where an access happened and who made the call, as recorded with each
/// access (the cross-run record reach groups by the caller fields).
#[derive(Debug, Clone, Default)]
pub struct AccessContext<'a> {
    pub tenant_id: &'a str,
    pub ai_system_id: &'a str,
    pub root_task_id: &'a str,
    pub tool: &'a str,
    pub mode: &'a str,
    pub subject_id: Option<&'a str>,
    pub api_key_id: Option<&'a str>,
    pub agent_id: Option<&'a str>,
    pub correlation_id: &'a str,
}

/// Write one row per access. In `enforce` this is awaited before the call is
/// answered, so a run finalised right after it counts it.
pub async fn record(
    db: &PgPool,
    ctx: &AccessContext<'_>,
    accesses: &[Access],
) -> Result<(), sqlx::Error> {
    for a in accesses {
        sqlx::query(
            "INSERT INTO ai_system_record_access \
                (id, tenant_id, ai_system_id, root_task_id, correlation_id, tool, scope_key, \
                 record_ref, verdict, mode, subject_id, api_key_id, agent_id, accessed_at) \
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, NOW())",
        )
        .bind(format!("rac-{}", ulid::Ulid::new()))
        .bind(ctx.tenant_id)
        .bind(ctx.ai_system_id)
        .bind(ctx.root_task_id)
        .bind(ctx.correlation_id)
        .bind(ctx.tool)
        .bind(&a.scope_key)
        .bind(
            a.value
                .as_deref()
                .map(|v| record_ref(ctx.tenant_id, &a.scope_key, v)),
        )
        .bind(a.verdict)
        .bind(ctx.mode)
        .bind(ctx.subject_id)
        .bind(ctx.api_key_id)
        .bind(ctx.agent_id)
        .execute(db)
        .await?;
    }
    Ok(())
}

/// A finding for the inbox: one per system, tool and verdict while open.
pub async fn raise_violation(
    db: &PgPool,
    tenant_id: &str,
    ai_system_id: &str,
    tool: &str,
    eval: &Evaluation,
    enforcing: bool,
    root_task_id: &str,
) {
    let what = match eval.verdict {
        verdict::OUT_OF_SCOPE => "named a record outside the run's scope",
        verdict::UNSCOPED_TOOL => "called a tool that reads beyond any one record in a scoped run",
        verdict::MISSING_ARGUMENT => "named no record where one was required",
        verdict::UNBOUND => "named a record in a run that declared no scope",
        verdict::CONFLICT => "tried to change the run's declared scope",
        _ => return,
    };
    crate::services::assurance_inbox::raise_item(
        db,
        tenant_id,
        Some(ai_system_id),
        "scope_violation",
        if enforcing { "critical" } else { "warning" },
        &format!("scope_violation:{ai_system_id}:{tool}:{}", eval.verdict),
        &format!("Data scope: a call to {tool} {what}"),
        json!({
            "tool": tool,
            "verdict": eval.verdict,
            "argument": eval.argument,
            "scope_key": eval.scope_key,
            "run_id_hint": root_task_id,
            "refused": enforcing,
        }),
    )
    .await;
}

// ---------------------------------------------------------------------------
// The gate — one implementation for every surface
// ---------------------------------------------------------------------------

/// Rule names for calls that are not MCP tools. MCP tool names cannot hold a
/// `:` (SEP-986 limits them to `[A-Za-z0-9_.-]`), so the prefixes cannot
/// collide with a tool.
pub mod surface {
    /// `skill:<skill name>` — a skill run; arguments are its input params.
    pub const SKILL: &str = "skill:";
    /// `a2a:<agent card name>` — a message to an agent; arguments are the
    /// message's data parts.
    pub const A2A: &str = "a2a:";
    /// `model:<function name>` — a tool call a model asks for, which the
    /// agent may run itself, outside the gateway; arguments
    /// are the call's parsed arguments.
    pub const MODEL: &str = "model:";
}

/// One governed call as the gate sees it, on any surface.
pub struct Call<'a> {
    pub tenant_id: &'a str,
    /// The AI System whose config applies: the caller's.
    pub ai_system_id: &'a str,
    /// The rule name: an MCP tool, or a [`surface`]-prefixed name.
    pub tool: &'a str,
    /// The call's arguments; several for a multi-part A2A message.
    pub arguments: &'a [&'a Value],
    pub root_task_id: &'a str,
    /// Keys this request tried to re-declare (the binding is unchanged).
    pub conflicts: &'a [String],
    pub auth_ctx: &'a crate::models::auth::AuthContext,
    pub correlation_id: &'a str,
}

/// Why the gate refused: a message for the caller and the detail every
/// surface returns as `data_scope`.
#[derive(Debug, Clone, PartialEq)]
pub struct Refusal {
    pub message: String,
    pub detail: Value,
}

impl Refusal {
    /// The refusal subtype an audit row carries: a gate
    /// that could not decide is `engine_failure`; a verdict is an access
    /// refusal at record level, `access_denied`.
    pub fn subtype(&self) -> &'static str {
        match self.detail.get("verdict").and_then(Value::as_str) {
            Some("config_unavailable" | "scope_unavailable" | "record_unavailable") => {
                crate::constants::proxy_subtype::ENGINE_FAILURE
            }
            _ => crate::constants::proxy_subtype::ACCESS_DENIED,
        }
    }

    /// The JSON error body the REST surfaces (skills) return.
    pub fn body(&self) -> Value {
        json!({"error": {
            "code": "data_scope_violation",
            "message": self.message,
            "data_scope": self.detail,
        }})
    }
}

/// Judge one call against its run's data scope, record what it touched, and
/// return the refusal when `enforce` says no. `None`: let the call through.
///
/// If the config cannot be read, whether the system enforces is unknowable,
/// so the call is refused (fail closed, like argument policies). In
/// `enforce`, an unreadable scope or an access that could not be recorded
/// refuses too: the evidence is part of the control.
pub async fn gate(db: &PgPool, call: Call<'_>) -> Option<Refusal> {
    gate_recording(db, call, Record::EveryAccess).await
}

/// What [`gate_recording`] writes for the call it judges.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Record {
    /// Every access the call makes, and a violation when it is one: the
    /// gate of a call that is about to run or be refused here.
    EveryAccess,
    /// Only a call this gate refuses (its accesses and its violation). A
    /// call it lets through leaves nothing: the preview of a gate the
    /// request has not reached, which records the call when it gets there.
    ARefusalOnly,
}

/// [`gate`], writing what `record` says.
pub async fn gate_recording(db: &PgPool, call: Call<'_>, record_what: Record) -> Option<Refusal> {
    let Call {
        tenant_id,
        ai_system_id,
        tool,
        arguments,
        root_task_id,
        conflicts,
        auth_ctx,
        correlation_id,
    } = call;
    let refusal = |message: &str, verdict: &str| Refusal {
        message: message.to_string(),
        detail: json!({"tool": tool, "verdict": verdict}),
    };

    let config = match load_config(db, tenant_id, ai_system_id).await {
        Ok(c) => c,
        Err(e) => {
            tracing::error!(group_id = %ai_system_id, error = %e, "data scope: config unreadable — refusing (fail closed)");
            return Some(refusal(
                "Refused: the data-scope configuration could not be read, so the call cannot be checked",
                "config_unavailable",
            ));
        }
    };
    let config = config.as_ref().as_ref()?;
    if config.mode == mode::OFF {
        return None;
    }
    let subject = auth_ctx
        .api_key_user_id
        .map(|u| u.to_string())
        .or_else(|| auth_ctx.gateway_user_id.clone());
    let scope = match run_scope(db, config, tenant_id, root_task_id, subject.as_deref()).await {
        Ok(s) => s,
        Err(e) => {
            crate::services::db_errors::record(&e);
            return config.enforcing().then(|| {
                refusal(
                    "Refused: the run's data scope could not be read, so the call cannot be checked",
                    "scope_unavailable",
                )
            });
        }
    };
    let mut eval = evaluate_parts(config, tool, arguments, &scope);
    // A request that tried to re-declare a bound key is a violation of its
    // own, whatever the call does.
    if !conflicts.is_empty() {
        let keys = conflicts.join(",");
        let conflict = Access {
            scope_key: keys.clone(),
            value: None,
            verdict: verdict::CONFLICT,
        };
        eval = Some(match eval {
            Some(mut e) => {
                e.accesses.push(conflict);
                if rank(e.verdict) < rank(verdict::CONFLICT) {
                    e.verdict = verdict::CONFLICT;
                    e.scope_key = keys;
                }
                e
            }
            None => Evaluation {
                verdict: verdict::CONFLICT,
                accesses: vec![conflict],
                argument: String::new(),
                scope_key: keys,
            },
        });
    }
    let eval = eval?;
    let ctx = AccessContext {
        tenant_id,
        ai_system_id,
        root_task_id,
        tool,
        mode: &config.mode,
        subject_id: subject.as_deref(),
        api_key_id: auth_ctx.api_key_id.as_deref(),
        agent_id: auth_ctx.actor_agent_id.as_deref(),
        correlation_id,
    };
    let refuse = refuses(config, eval.verdict);
    if !refuse && record_what == Record::ARefusalOnly {
        return None;
    }
    if let Err(e) = record(db, &ctx, &eval.accesses).await {
        crate::services::db_errors::record(&e);
        if config.enforcing() {
            return Some(refusal(
                "Refused: the record access could not be recorded",
                "record_unavailable",
            ));
        }
    }
    if eval.verdict != verdict::IN_SCOPE {
        raise_violation(
            db,
            tenant_id,
            ai_system_id,
            tool,
            &eval,
            refuse,
            root_task_id,
        )
        .await;
    }
    if !refuse {
        return None;
    }
    tracing::warn!(tool, verdict = eval.verdict, "data scope deny — refused");
    Some(Refusal {
        message: format!(
            "Data scope: {tool} was refused ({}) for this run's scope",
            eval.verdict
        ),
        detail: json!({
            "tool": tool,
            "argument": eval.argument,
            "key": eval.scope_key,
            "verdict": eval.verdict,
        }),
    })
}

/// The arguments of an A2A message: each data part's value. v1.0 parts
/// carry `data` directly; v0.3 parts say `kind: "data"`. A message that is
/// not part-shaped (the legacy opaque body) is its own single argument.
pub fn a2a_arguments(message: &Value) -> Vec<&Value> {
    match message.get("parts").and_then(Value::as_array) {
        Some(parts) => parts.iter().filter_map(|p| p.get("data")).collect(),
        None => vec![message],
    }
}

/// The run's data-scope facts, read at finalisation in the same snapshot as
/// the rest of the aggregate.
#[derive(Debug, Clone, Default, PartialEq, sqlx::FromRow)]
pub struct RunScopeFacts {
    pub scope_declared: bool,
    pub records_touched: i64,
    pub scope_violation_count: i64,
    pub scope_unbound_count: i64,
}

pub async fn load_run_facts(
    conn: &mut sqlx::PgConnection,
    tenant_id: &str,
    root_task_id: &str,
) -> Result<RunScopeFacts, sqlx::Error> {
    sqlx::query_as(
        "SELECT \
            EXISTS (SELECT 1 FROM ai_system_run_scopes s \
                     WHERE s.tenant_id = $1 AND s.root_task_id = $2) AS scope_declared, \
            COUNT(DISTINCT a.record_ref) AS records_touched, \
            COUNT(*) FILTER (WHERE a.verdict = ANY($3)) AS scope_violation_count, \
            COUNT(*) FILTER (WHERE a.verdict = $4) AS scope_unbound_count \
           FROM (SELECT 1) one \
           LEFT JOIN ai_system_record_access a \
             ON a.tenant_id = $1 AND a.root_task_id = $2",
    )
    .bind(tenant_id)
    .bind(root_task_id)
    .bind(&verdict::VIOLATIONS[..])
    .bind(verdict::UNBOUND)
    .fetch_one(conn)
    .await
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config(mode: &str) -> Config {
        Config {
            mode: mode.into(),
            on_unbound_deny: true,
            keys: vec![KeyDef {
                key: "customer".into(),
                source: source::DECLARED.into(),
            }],
            rules: vec![
                Rule {
                    tool: "crm_get_customer".into(),
                    argument: "customer_id".into(),
                    key: "customer".into(),
                },
                Rule {
                    tool: "crm_transfer".into(),
                    argument: "accounts[].owner".into(),
                    key: "customer".into(),
                },
            ],
            unscoped_tools: vec!["crm_list_customers".into()],
        }
    }

    fn scope(values: &[&str]) -> BTreeMap<String, Vec<String>> {
        BTreeMap::from([(
            "customer".to_string(),
            values.iter().map(|v| v.to_string()).collect(),
        )])
    }

    #[test]
    fn the_declaration_header_parses_bounded_and_lower_cased() {
        let d =
            parse_declaration(" Customer = C1 ; account=ACC1, ACC2,,ACC1; bad key=x; =y; empty=");
        assert_eq!(d["customer"], vec!["C1"]);
        assert_eq!(d["account"], vec!["ACC1", "ACC2"]);
        assert_eq!(d.len(), 2);
        let many = (0..100)
            .map(|i| format!("v{i}"))
            .collect::<Vec<_>>()
            .join(",");
        assert_eq!(
            parse_declaration(&format!("k={many}"))["k"].len(),
            MAX_DECLARED_VALUES
        );
    }

    #[test]
    fn values_are_read_from_paths_and_arrays() {
        let args = json!({"customer_id": "C1", "n": 7,
                          "accounts": [{"owner": "C1"}, {"owner": "C2"}], "empty": []});
        assert_eq!(values_at(&args, "customer_id"), Some(vec!["C1".into()]));
        assert_eq!(values_at(&args, "n"), Some(vec!["7".into()]));
        assert_eq!(
            values_at(&args, "accounts[].owner"),
            Some(vec!["C1".into(), "C2".into()])
        );
        assert_eq!(values_at(&args, "missing"), None);
        assert_eq!(values_at(&args, "accounts"), None, "an array is not an id");
        assert_eq!(values_at(&args, "empty[]"), None, "nothing named");
        assert_eq!(values_at(&args, "customer_id[]"), None);
    }

    #[test]
    fn the_same_shape_run_for_another_customer_is_out_of_scope() {
        // The case the check exists for: same tool, same argument name, someone else.
        let e = evaluate(
            &config(mode::ENFORCE),
            "crm_get_customer",
            &json!({"customer_id": "C0002"}),
            &scope(&["C0001"]),
        )
        .unwrap();
        assert_eq!(e.verdict, verdict::OUT_OF_SCOPE);
        assert!(refuses(&config(mode::ENFORCE), e.verdict));
        assert!(!refuses(&config(mode::AUDIT), e.verdict));
        let ok = evaluate(
            &config(mode::ENFORCE),
            "crm_get_customer",
            &json!({"customer_id": "C0001"}),
            &scope(&["C0001"]),
        )
        .unwrap();
        assert_eq!(ok.verdict, verdict::IN_SCOPE);
        assert!(!refuses(&config(mode::ENFORCE), ok.verdict));
    }

    #[test]
    fn a_list_is_in_scope_only_if_every_value_is() {
        let e = evaluate(
            &config(mode::ENFORCE),
            "crm_transfer",
            &json!({"accounts": [{"owner": "C1"}, {"owner": "C9"}]}),
            &scope(&["C1"]),
        )
        .unwrap();
        assert_eq!(e.verdict, verdict::OUT_OF_SCOPE);
        assert_eq!(e.accesses.len(), 2);
    }

    #[test]
    fn missing_unbound_and_unscoped_tools_have_their_own_verdicts() {
        let c = config(mode::ENFORCE);
        let missing = evaluate(&c, "crm_get_customer", &json!({}), &scope(&["C1"])).unwrap();
        assert_eq!(missing.verdict, verdict::MISSING_ARGUMENT);
        let unbound = evaluate(
            &c,
            "crm_get_customer",
            &json!({"customer_id": "C1"}),
            &BTreeMap::new(),
        )
        .unwrap();
        assert_eq!(unbound.verdict, verdict::UNBOUND);
        assert!(refuses(&c, unbound.verdict));
        let mut lenient = c.clone();
        lenient.on_unbound_deny = false;
        assert!(!refuses(&lenient, unbound.verdict));
        let list = evaluate(&c, "crm_list_customers", &json!({}), &scope(&["C1"])).unwrap();
        assert_eq!(list.verdict, verdict::UNSCOPED_TOOL);
        // Outside a scoped run a list tool is not a scope question.
        assert!(evaluate(&c, "crm_list_customers", &json!({}), &BTreeMap::new()).is_none());
        assert!(evaluate(&c, "weather", &json!({}), &scope(&["C1"])).is_none());
    }

    #[test]
    fn a_message_in_several_parts_is_judged_on_every_part() {
        let mut c = config(mode::ENFORCE);
        c.rules.push(Rule {
            tool: "a2a:billing".into(),
            argument: "customer_id".into(),
            key: "customer".into(),
        });
        let s = scope(&["C1"]);
        let own = json!({"customer_id": "C1"});
        let other = json!({"customer_id": "C2"});
        let note = json!({"comment": "no record here"});
        let broken = json!({"customer_id": {"id": "C2"}});
        let v = |parts: &[&Value]| {
            evaluate_parts(&c, "a2a:billing", parts, &s)
                .unwrap()
                .verdict
        };
        assert_eq!(
            v(&[&own, &note]),
            verdict::IN_SCOPE,
            "a part without the argument is not missing"
        );
        assert_eq!(
            v(&[&own, &other]),
            verdict::OUT_OF_SCOPE,
            "one in-scope part cannot cover another"
        );
        assert_eq!(
            v(&[&own, &broken]),
            verdict::MISSING_ARGUMENT,
            "a part naming it badly is missing"
        );
        assert_eq!(v(&[&note]), verdict::MISSING_ARGUMENT);
        assert_eq!(
            v(&[]),
            verdict::MISSING_ARGUMENT,
            "a text-only message names no record"
        );
    }

    #[test]
    fn a2a_arguments_are_the_data_parts_or_the_opaque_body() {
        let v03 = json!({"parts": [{"kind": "text", "text": "hi"},
                                   {"kind": "data", "data": {"customer_id": "C1"}}]});
        assert_eq!(a2a_arguments(&v03), vec![&json!({"customer_id": "C1"})]);
        let opaque = json!({"customer_id": "C1"});
        assert_eq!(a2a_arguments(&opaque), vec![&opaque]);
    }

    #[test]
    fn engine_failures_and_verdicts_carry_their_own_subtypes() {
        let r = |v: &str| Refusal {
            message: String::new(),
            detail: json!({"verdict": v}),
        };
        assert_eq!(r("config_unavailable").subtype(), "engine_failure");
        assert_eq!(r("record_unavailable").subtype(), "engine_failure");
        assert_eq!(r("out_of_scope").subtype(), "access_denied");
    }

    #[test]
    fn record_references_are_stable_keyed_and_separated() {
        let a = record_ref("t1", "customer", "C1");
        assert_eq!(a, record_ref("t1", "customer", "C1"));
        assert_ne!(a, record_ref("t2", "customer", "C1"));
        assert_ne!(a, record_ref("t1", "account", "C1"));
        assert_ne!(a, record_ref("t1", "customer", "C2"));
        assert_eq!(a.len(), 64);
        let plain = hex::encode(Sha256::digest(b"t1\0customer\0C1"));
        assert_ne!(a, plain, "a plain hash could be reversed by enumeration");
    }
}
