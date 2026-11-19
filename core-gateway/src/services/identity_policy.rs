// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Identity policy: which credentials an AI System accepts
//!
//! Three pure parts and one call:
//!
//! * [`IdentityPolicy`]: the six-member policy a group (and the tenant
//!   default) stores, with the writer's validation rules ([`IdentityPolicy::validate`]).
//!   A stored value that fails them is read as the strictest policy
//!   ([`IdentityPolicy::from_stored`]): a writer bug never opens the floor.
//! * [`resolve`]: the effective policy of a call from the tenant default and
//!   the acting group's ancestor chain. Every contribution (the development
//!   default, the tenant default, each group's explicit policy, the
//!   production lifecycle default of an AI System, the high-risk floor) is a
//!   floor; field by field, the stricter wins. The control plane resolves
//!   the same way; both are held to `tests/fixtures/identity_policy_resolver.json`.
//! * [`judge`]: a credential against an effective policy: `met`,
//!   `below_policy` (audit mode: the call proceeds and the row says so),
//!   `refused` (enforce mode), or `exempt` (a person's session, the
//!   platform's own credential: outside the ladder).
//! * [`judge_call`]: what every governed surface calls after authentication
//!   (`tests/identity_policy_surface_guard.rs` holds that). It stores the
//!   verdict on the context, writes the one `proxy_logs` row of a refused
//!   call, and turns a refusal into
//!   [`AuthError::CredentialAssuranceInsufficient`], which carries the
//!   [`CredentialRequirement`] every surface's envelope states
//!   ([`refusal_response`], [`attach_requirement`]).
//!
//! The policy store: [`policy_for_call`] reads the caller's bound group's
//! chain from the group cache ([`GroupIdentity`], loaded with the group) and
//! the tenant's `identity_policy_default` from the tenant cache. A caller no
//! group governs is judged by the tenant default, else the development
//! default (`audit`, `key`). A store that cannot be read refuses the call
//! (503): no floor is ever read as open.

use axum::http::{HeaderValue, StatusCode};
use axum::response::Response;
use serde_json::{Map, Value};

use crate::models::auth::{AuthContext, CredentialAssurance, CredentialKind};
use crate::services::auth::AuthError;

/// The public documentation site. The refusal body's `docs_url` is built
/// from it and [`CREDENTIAL_ERRORS_PATH`].
pub const DOCS_BASE_URL: &str = "https://docs.brutor.ai";

/// Where the documentation explains a credential refusal.
pub const CREDENTIAL_ERRORS_PATH: &str =
    "/build-an-agent/identity/errors/#credential_assurance_insufficient";

/// The code of a credential refusal: the first word of its sentence, and the
/// `code` of every envelope that has one.
pub const REFUSAL_CODE: &str = "CREDENTIAL_ASSURANCE_INSUFFICIENT";

/// The response header that names the level a refused call needed.
pub const CREDENTIAL_REQUIRED_HEADER: &str = "brutor-credential-required";

/// The `@type` of the requirement in a `google.rpc` error's `details`.
pub const REQUIREMENT_DETAIL_TYPE: &str = "type.brutor.ai/CredentialRequirement";

/// `proxy_logs.error_type` of a refused call's row.
pub const ERROR_TYPE: &str = "credential_policy";

/// The six members of a policy, in their stored order.
pub const POLICY_FIELDS: [&str; 6] = [
    "mode",
    "min_assurance",
    "allow_user_keys",
    "allowed_issuers",
    "max_token_lifetime_s",
    "max_key_ttl_days",
];

/// Lifecycle stages in which an AI System is in production.
pub const PRODUCTION_STAGES: [&str; 3] = ["active", "deprecated", "retired"];

/// Risk tiers that carry the high-risk floor.
pub const HIGH_RISK_TIERS: [&str; 2] = ["high", "unacceptable"];

/// The lifetime ceiling of a workload token, whatever the policy says.
pub const TOKEN_LIFETIME_CEILING_S: u32 = 3600;

/// The personal-key ceiling when no tenant default sets one.
pub const DEFAULT_MAX_USER_KEY_TTL_DAYS: u32 = 90;

/// `audit` lets a call below the policy through and records it; `enforce`
/// refuses it. `audit` < `enforce`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Mode {
    Audit,
    Enforce,
}

impl Mode {
    pub const fn as_str(self) -> &'static str {
        match self {
            Mode::Audit => "audit",
            Mode::Enforce => "enforce",
        }
    }

    fn parse(name: &str) -> Option<Self> {
        match name {
            "audit" => Some(Mode::Audit),
            "enforce" => Some(Mode::Enforce),
            _ => None,
        }
    }
}

/// A complete identity policy.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IdentityPolicy {
    pub mode: Mode,
    pub min_assurance: CredentialAssurance,
    pub allow_user_keys: bool,
    /// `None`: any issuer. Otherwise the identity providers whose workload
    /// credentials count (sorted; may be empty after an intersection, which
    /// accepts no workload credential).
    pub allowed_issuers: Option<Vec<String>>,
    pub max_token_lifetime_s: u32,
    /// `None`: no ceiling.
    pub max_key_ttl_days: Option<u32>,
}

/// Why a stored policy is not one: the first member that fails.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InvalidPolicy {
    pub field: String,
    pub problem: &'static str,
}

impl IdentityPolicy {
    /// The policy of a call nothing else governs: audit, at level `key`.
    pub fn development_default() -> Self {
        Self {
            mode: Mode::Audit,
            min_assurance: CredentialAssurance::Key,
            allow_user_keys: true,
            allowed_issuers: None,
            max_token_lifetime_s: TOKEN_LIFETIME_CEILING_S,
            max_key_ttl_days: None,
        }
    }

    /// What a stored value that is not a valid policy contributes: the
    /// strictest policy there is.
    pub fn strictest() -> Self {
        Self {
            mode: Mode::Enforce,
            min_assurance: CredentialAssurance::WorkloadPop,
            allow_user_keys: false,
            allowed_issuers: Some(Vec::new()),
            max_token_lifetime_s: 60,
            max_key_ttl_days: Some(1),
        }
    }

    /// The writer's rules: exactly the six members, each of its type and in
    /// its range.
    pub fn validate(value: &Value) -> Result<Self, InvalidPolicy> {
        let object = value.as_object().ok_or(InvalidPolicy {
            field: String::new(),
            problem: "a policy is a JSON object",
        })?;
        Self::from_members(object, &POLICY_FIELDS)
    }

    /// A stored value, read the way an enforcement path must: a value that
    /// is not a valid policy is the strictest one, and the error is logged.
    pub fn from_stored(value: &Value, owner: &str) -> Self {
        match Self::validate(value) {
            Ok(policy) => policy,
            Err(invalid) => {
                tracing::error!(
                    owner,
                    field = %invalid.field,
                    problem = invalid.problem,
                    "stored identity policy is invalid; read as the strictest policy"
                );
                Self::strictest()
            }
        }
    }

    fn from_members(object: &Map<String, Value>, allowed: &[&str]) -> Result<Self, InvalidPolicy> {
        let invalid = |field: &str, problem| InvalidPolicy {
            field: field.to_string(),
            problem,
        };
        if let Some(extra) = object.keys().find(|k| !allowed.contains(&k.as_str())) {
            return Err(invalid(extra, "not a member of an identity policy"));
        }
        let member = |field: &str| object.get(field).ok_or_else(|| invalid(field, "missing"));

        let mode = member("mode")?
            .as_str()
            .and_then(Mode::parse)
            .ok_or_else(|| invalid("mode", "must be audit or enforce"))?;
        let min_assurance = member("min_assurance")?
            .as_str()
            .and_then(CredentialAssurance::parse)
            .ok_or_else(|| invalid("min_assurance", "must be a credential level"))?;
        let allow_user_keys = member("allow_user_keys")?
            .as_bool()
            .ok_or_else(|| invalid("allow_user_keys", "must be a JSON boolean"))?;
        let allowed_issuers = match member("allowed_issuers")? {
            Value::Null => None,
            Value::Array(items) => {
                let mut issuers = Vec::with_capacity(items.len());
                for item in items {
                    let id = item
                        .as_str()
                        .filter(|s| !s.is_empty())
                        .ok_or_else(|| invalid("allowed_issuers", "entries are IdP ids"))?;
                    if issuers.iter().any(|seen: &String| seen == id) {
                        return Err(invalid("allowed_issuers", "entries must be distinct"));
                    }
                    issuers.push(id.to_string());
                }
                if issuers.is_empty() || issuers.len() > 50 {
                    return Err(invalid("allowed_issuers", "1 to 50 IdP ids, or null"));
                }
                issuers.sort();
                Some(issuers)
            }
            _ => return Err(invalid("allowed_issuers", "an array of IdP ids, or null")),
        };
        let max_token_lifetime_s = member("max_token_lifetime_s")?
            .as_u64()
            .filter(|n| (60..=u64::from(TOKEN_LIFETIME_CEILING_S)).contains(n))
            .ok_or_else(|| invalid("max_token_lifetime_s", "an integer from 60 to 3600"))?
            as u32;
        let max_key_ttl_days = match member("max_key_ttl_days")? {
            Value::Null => None,
            value => Some(
                value
                    .as_u64()
                    .filter(|n| (1..=3650).contains(n))
                    .ok_or_else(|| {
                        invalid("max_key_ttl_days", "an integer from 1 to 3650, or null")
                    })? as u32,
            ),
        };
        Ok(Self {
            mode,
            min_assurance,
            allow_user_keys,
            allowed_issuers,
            max_token_lifetime_s,
            max_key_ttl_days,
        })
    }

    /// The stored JSON form.
    pub fn to_json(&self) -> Value {
        serde_json::json!({
            "mode": self.mode.as_str(),
            "min_assurance": self.min_assurance.as_str(),
            "allow_user_keys": self.allow_user_keys,
            "allowed_issuers": self.allowed_issuers,
            "max_token_lifetime_s": self.max_token_lifetime_s,
            "max_key_ttl_days": self.max_key_ttl_days,
        })
    }

    /// Field by field, the stricter of the two.
    pub fn stricter(&self, other: &Self) -> Self {
        Self {
            mode: self.mode.max(other.mode),
            min_assurance: self.min_assurance.max(other.min_assurance),
            allow_user_keys: self.allow_user_keys && other.allow_user_keys,
            allowed_issuers: match (&self.allowed_issuers, &other.allowed_issuers) {
                (None, None) => None,
                (Some(a), None) | (None, Some(a)) => Some(a.clone()),
                (Some(a), Some(b)) => Some(intersect(a, b)),
            },
            max_token_lifetime_s: self.max_token_lifetime_s.min(other.max_token_lifetime_s),
            max_key_ttl_days: min_ceiling(self.max_key_ttl_days, other.max_key_ttl_days),
        }
    }
}

/// The tenant default: a policy, and the personal-key ceiling.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IdentityPolicyDefault {
    pub policy: IdentityPolicy,
    pub max_user_key_ttl_days: u32,
}

impl IdentityPolicyDefault {
    /// The writer's rules for the tenant default.
    pub fn validate(value: &Value) -> Result<Self, InvalidPolicy> {
        let object = value.as_object().ok_or(InvalidPolicy {
            field: String::new(),
            problem: "a tenant default is a JSON object",
        })?;
        let mut allowed = POLICY_FIELDS.to_vec();
        allowed.push("max_user_key_ttl_days");
        let policy = IdentityPolicy::from_members(object, &allowed)?;
        let max_user_key_ttl_days = object
            .get("max_user_key_ttl_days")
            .and_then(Value::as_u64)
            .filter(|n| (1..=3650).contains(n))
            .ok_or(InvalidPolicy {
                field: "max_user_key_ttl_days".into(),
                problem: "an integer from 1 to 3650",
            })? as u32;
        Ok(Self {
            policy,
            max_user_key_ttl_days,
        })
    }

    /// A stored tenant default, read fail closed (as [`IdentityPolicy::from_stored`]).
    pub fn from_stored(value: &Value, tenant_id: &str) -> Self {
        match Self::validate(value) {
            Ok(default) => default,
            Err(invalid) => {
                tracing::error!(
                    tenant_id,
                    field = %invalid.field,
                    problem = invalid.problem,
                    "stored tenant identity default is invalid; read as the strictest policy"
                );
                Self {
                    policy: IdentityPolicy::strictest(),
                    max_user_key_ttl_days: 1,
                }
            }
        }
    }
}

/// One group of the acting group's ancestor chain.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ChainNode {
    pub id: String,
    pub is_ai_system: bool,
    pub lifecycle_stage: Option<String>,
    pub risk_tier: Option<String>,
    pub identity_policy: Option<IdentityPolicy>,
    /// An unexpired exception to the workload-identity gate requirement
    /// (expiry judged by whoever loads it).
    pub gate_exception: bool,
}

impl ChainNode {
    fn in_production(&self) -> bool {
        self.is_ai_system
            && self
                .lifecycle_stage
                .as_deref()
                .is_some_and(|stage| PRODUCTION_STAGES.contains(&stage))
    }

    fn high_risk(&self) -> bool {
        self.risk_tier
            .as_deref()
            .is_some_and(|tier| HIGH_RISK_TIERS.contains(&tier))
    }
}

/// What a call's effective policy is resolved from.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ResolverInput {
    pub tenant_default: Option<IdentityPolicyDefault>,
    /// Root first, the acting group last. Empty: no group governs the call.
    pub chain: Vec<ChainNode>,
    /// The AI System the call is attributed to: the acting group or one of
    /// its ancestors.
    pub attributed_ai_system_id: Option<String>,
}

/// Where a winning value came from.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SourceKind {
    Default,
    TenantDefault,
    Explicit,
    Lifecycle,
    RiskFloor,
    Intersection,
}

impl SourceKind {
    pub const fn as_str(self) -> &'static str {
        match self {
            SourceKind::Default => "default",
            SourceKind::TenantDefault => "tenant_default",
            SourceKind::Explicit => "explicit",
            SourceKind::Lifecycle => "lifecycle",
            SourceKind::RiskFloor => "risk_floor",
            SourceKind::Intersection => "intersection",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PolicySource {
    pub kind: SourceKind,
    pub group_id: Option<String>,
    pub stage: Option<String>,
    pub risk_tier: Option<String>,
    pub lowered_by_exception: bool,
}

impl PolicySource {
    fn of(kind: SourceKind) -> Self {
        Self {
            kind,
            group_id: None,
            stage: None,
            risk_tier: None,
            lowered_by_exception: false,
        }
    }

    /// The JSON form: members that do not apply are absent.
    pub fn to_json(&self) -> Value {
        let mut object = Map::new();
        object.insert("kind".into(), Value::from(self.kind.as_str()));
        if let Some(id) = &self.group_id {
            object.insert("group_id".into(), Value::from(id.as_str()));
        }
        if let Some(stage) = &self.stage {
            object.insert("stage".into(), Value::from(stage.as_str()));
        }
        if let Some(tier) = &self.risk_tier {
            object.insert("risk_tier".into(), Value::from(tier.as_str()));
        }
        if self.lowered_by_exception {
            object.insert("lowered_by_exception".into(), Value::Bool(true));
        }
        Value::Object(object)
    }
}

/// The source of each policy member, in [`POLICY_FIELDS`] order.
pub type PolicySources = [PolicySource; 6];

/// A call's resolved policy.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EffectivePolicy {
    /// What the call is judged by.
    pub effective: IdentityPolicy,
    /// Where each member's value came from.
    pub sources: PolicySources,
    /// The policy without the acting group's own explicit policy: what a
    /// write of that policy may not relax.
    pub inherited_floor: IdentityPolicy,
    /// The members in which the acting group's own policy is looser than
    /// the inherited floor.
    pub put_refused_fields: Vec<&'static str>,
    /// The personal-key ceiling.
    pub max_user_key_ttl_days: u32,
}

/// One contribution: the members it sets (`None`: not set) and its source.
/// For the two nullable members, `Some(None)` sets null.
#[derive(Debug, Clone)]
struct Contribution {
    source: PolicySource,
    mode: Option<Mode>,
    min_assurance: Option<CredentialAssurance>,
    allow_user_keys: Option<bool>,
    allowed_issuers: Option<Option<Vec<String>>>,
    max_token_lifetime_s: Option<u32>,
    max_key_ttl_days: Option<Option<u32>>,
}

impl Contribution {
    fn whole(policy: &IdentityPolicy, source: PolicySource) -> Self {
        Self {
            source,
            mode: Some(policy.mode),
            min_assurance: Some(policy.min_assurance),
            allow_user_keys: Some(policy.allow_user_keys),
            allowed_issuers: Some(policy.allowed_issuers.clone()),
            max_token_lifetime_s: Some(policy.max_token_lifetime_s),
            max_key_ttl_days: Some(policy.max_key_ttl_days),
        }
    }

    fn partial(source: PolicySource) -> Self {
        Self {
            source,
            mode: None,
            min_assurance: None,
            allow_user_keys: None,
            allowed_issuers: None,
            max_token_lifetime_s: None,
            max_key_ttl_days: None,
        }
    }
}

/// The contributions of an input, in resolution order.
fn contributions(
    tenant_default: Option<&IdentityPolicyDefault>,
    chain: &[ChainNode],
) -> Vec<Contribution> {
    let mut out = vec![Contribution::whole(
        &IdentityPolicy::development_default(),
        PolicySource::of(SourceKind::Default),
    )];
    if let Some(default) = tenant_default {
        out.push(Contribution::whole(
            &default.policy,
            PolicySource::of(SourceKind::TenantDefault),
        ));
    }
    let any_high_risk = chain.iter().any(ChainNode::high_risk);
    for node in chain {
        if let Some(policy) = &node.identity_policy {
            let mut source = PolicySource::of(SourceKind::Explicit);
            source.group_id = Some(node.id.clone());
            out.push(Contribution::whole(policy, source));
        }
        if node.in_production() {
            let lowered = node.gate_exception && !any_high_risk;
            let mut source = PolicySource::of(SourceKind::Lifecycle);
            source.group_id = Some(node.id.clone());
            source.stage = node.lifecycle_stage.clone();
            source.lowered_by_exception = lowered;
            let mut c = Contribution::partial(source);
            c.mode = Some(Mode::Enforce);
            c.min_assurance = Some(if lowered {
                CredentialAssurance::BoundKey
            } else {
                CredentialAssurance::Workload
            });
            c.allow_user_keys = Some(false);
            out.push(c);
        }
        if node.high_risk() {
            let mut c = Contribution::partial(risk_source(node));
            c.mode = Some(Mode::Enforce);
            c.allow_user_keys = Some(false);
            out.push(c);
        }
    }
    if let Some(first_high) = chain.iter().find(|n| n.high_risk())
        && chain.iter().any(ChainNode::in_production)
    {
        let mut c = Contribution::partial(risk_source(first_high));
        c.min_assurance = Some(CredentialAssurance::WorkloadPop);
        out.push(c);
    }
    out
}

fn risk_source(node: &ChainNode) -> PolicySource {
    let mut source = PolicySource::of(SourceKind::RiskFloor);
    source.group_id = Some(node.id.clone());
    source.risk_tier = node.risk_tier.clone();
    source
}

/// The strictest value of one member over the contributions that set it,
/// and the first contribution that holds it. `stricter(a, b)`: is `a`
/// stricter than `b`.
fn winner<T: Clone + PartialEq>(
    contributions: &[Contribution],
    get: impl Fn(&Contribution) -> Option<T>,
    stricter: impl Fn(&T, &T) -> bool,
) -> (T, PolicySource) {
    let mut best: Option<(T, PolicySource)> = None;
    for c in contributions {
        if let Some(value) = get(c) {
            let replace = match &best {
                None => true,
                Some((current, _)) => stricter(&value, current),
            };
            if replace {
                best = Some((value, c.source.clone()));
            }
        }
    }
    // The development default sets every member, so there is always one.
    best.unwrap_or_else(|| unreachable!("the development default sets every member"))
}

fn combine(contributions: &[Contribution]) -> (IdentityPolicy, PolicySources) {
    let (mode, mode_src) = winner(contributions, |c| c.mode, |a, b| a > b);
    let (min_assurance, min_src) = winner(contributions, |c| c.min_assurance, |a, b| a > b);
    let (allow_user_keys, users_src) = winner(contributions, |c| c.allow_user_keys, |a, b| !a & b);
    let (max_token_lifetime_s, lifetime_src) =
        winner(contributions, |c| c.max_token_lifetime_s, |a, b| a < b);
    let (max_key_ttl_days, ttl_src) = winner(
        contributions,
        |c| c.max_key_ttl_days,
        |a, b| match (a, b) {
            (Some(a), Some(b)) => a < b,
            (Some(_), None) => true,
            (None, _) => false,
        },
    );

    // Issuers: the intersection of every list.
    let lists: Vec<(&Vec<String>, &PolicySource)> = contributions
        .iter()
        .filter_map(|c| match &c.allowed_issuers {
            Some(Some(list)) => Some((list, &c.source)),
            _ => None,
        })
        .collect();
    let (allowed_issuers, issuers_src) = match lists.as_slice() {
        [] => {
            let first_null = contributions
                .iter()
                .find(|c| matches!(c.allowed_issuers, Some(None)))
                .map(|c| c.source.clone())
                .unwrap_or_else(|| PolicySource::of(SourceKind::Default));
            (None, first_null)
        }
        [(list, source)] => {
            let mut list = (*list).clone();
            list.sort();
            (Some(list), (*source).clone())
        }
        [(first, _), rest @ ..] => {
            let mut acc = (*first).clone();
            for (list, _) in rest {
                acc = intersect(&acc, list);
            }
            (Some(acc), PolicySource::of(SourceKind::Intersection))
        }
    };

    (
        IdentityPolicy {
            mode,
            min_assurance,
            allow_user_keys,
            allowed_issuers,
            max_token_lifetime_s,
            max_key_ttl_days,
        },
        [
            mode_src,
            min_src,
            users_src,
            issuers_src,
            lifetime_src,
            ttl_src,
        ],
    )
}

fn intersect(a: &[String], b: &[String]) -> Vec<String> {
    let mut out: Vec<String> = a.iter().filter(|x| b.contains(x)).cloned().collect();
    out.sort();
    out.dedup();
    out
}

fn min_ceiling(a: Option<u32>, b: Option<u32>) -> Option<u32> {
    match (a, b) {
        (Some(a), Some(b)) => Some(a.min(b)),
        (Some(a), None) | (None, Some(a)) => Some(a),
        (None, None) => None,
    }
}

/// The members in which `own` is looser than `floor`, in member order.
fn looser_fields(own: &IdentityPolicy, floor: &IdentityPolicy) -> Vec<&'static str> {
    let mut out = Vec::new();
    if own.mode < floor.mode {
        out.push("mode");
    }
    if own.min_assurance < floor.min_assurance {
        out.push("min_assurance");
    }
    if own.allow_user_keys && !floor.allow_user_keys {
        out.push("allow_user_keys");
    }
    if let Some(floor_list) = &floor.allowed_issuers {
        let within = own
            .allowed_issuers
            .as_ref()
            .is_some_and(|list| list.iter().all(|id| floor_list.contains(id)));
        if !within {
            out.push("allowed_issuers");
        }
    }
    if own.max_token_lifetime_s > floor.max_token_lifetime_s {
        out.push("max_token_lifetime_s");
    }
    if let Some(floor_ttl) = floor.max_key_ttl_days
        && own.max_key_ttl_days.is_none_or(|ttl| ttl > floor_ttl)
    {
        out.push("max_key_ttl_days");
    }
    out
}

/// The effective policy of a call (the one algorithm both planes run).
pub fn resolve(input: &ResolverInput) -> EffectivePolicy {
    let tenant_default = input.tenant_default.as_ref();
    let (mut effective, sources) = combine(&contributions(tenant_default, &input.chain));

    // A call attributed to an ancestor AI System is judged by the stricter
    // of the acting chain and the chain cut at that system.
    if let Some(attributed) = &input.attributed_ai_system_id
        && let Some(at) = input.chain.iter().position(|n| &n.id == attributed)
    {
        let (cut, _) = combine(&contributions(tenant_default, &input.chain[..=at]));
        effective = effective.stricter(&cut);
    }

    let mut without_own = input.chain.clone();
    let own = without_own
        .last_mut()
        .and_then(|acting| acting.identity_policy.take());
    let (inherited_floor, _) = combine(&contributions(tenant_default, &without_own));
    let put_refused_fields = own
        .map(|own| looser_fields(&own, &inherited_floor))
        .unwrap_or_default();

    EffectivePolicy {
        effective,
        sources,
        inherited_floor,
        put_refused_fields,
        max_user_key_ttl_days: tenant_default
            .map_or(DEFAULT_MAX_USER_KEY_TTL_DAYS, |d| d.max_user_key_ttl_days),
    }
}

// ---------------------------------------------------------------------------
// The judge
// ---------------------------------------------------------------------------

/// What the judge decided about a call.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Outcome {
    /// The credential meets the policy.
    Met,
    /// Below the policy, in audit mode: served, and recorded so.
    BelowPolicy,
    /// Below the policy, in enforce mode: refused.
    Refused,
    /// Outside the ladder: a person's session, the platform's credential.
    Exempt,
}

impl Outcome {
    pub const fn as_str(self) -> &'static str {
        match self {
            Outcome::Met => "met",
            Outcome::BelowPolicy => "below_policy",
            Outcome::Refused => "refused",
            Outcome::Exempt => "exempt",
        }
    }
}

/// Why a credential falls below a policy, in order of precedence.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Reason {
    BelowMinimum,
    TokenLifetimeExceedsPolicy,
    IssuerNotAllowed,
    UserKeyNotAllowed,
}

impl Reason {
    pub const fn as_str(self) -> &'static str {
        match self {
            Reason::BelowMinimum => "below_minimum",
            Reason::TokenLifetimeExceedsPolicy => "token_lifetime_exceeds_policy",
            Reason::IssuerNotAllowed => "issuer_not_allowed",
            Reason::UserKeyNotAllowed => "user_key_not_allowed",
        }
    }
}

/// The judge's verdict.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct IdentityVerdict {
    pub outcome: Outcome,
    /// The first reason in precedence order; `None` when met or exempt.
    pub reason: Option<Reason>,
    /// The policy's minimum level.
    pub required: CredentialAssurance,
}

/// The facts the judge reads about a credential.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct JudgedCredential<'a> {
    pub kind: CredentialKind,
    pub assurance: Option<CredentialAssurance>,
    pub is_user_key: bool,
    pub idp_server_id: Option<&'a str>,
    pub token_lifetime_s: Option<i64>,
}

impl<'a> JudgedCredential<'a> {
    pub fn of(auth: &'a AuthContext) -> Self {
        Self {
            kind: auth.credential_kind,
            assurance: auth.credential_assurance,
            is_user_key: auth.is_personal_key(),
            idp_server_id: auth.credential_detail.idp_server_id.as_deref(),
            token_lifetime_s: auth.credential_detail.token_lifetime_s,
        }
    }
}

/// A credential against a policy. Pure.
pub fn judge_credential(
    credential: &JudgedCredential<'_>,
    policy: &IdentityPolicy,
) -> IdentityVerdict {
    let required = policy.min_assurance;
    if credential.kind.is_exempt() {
        return IdentityVerdict {
            outcome: Outcome::Exempt,
            reason: None,
            required,
        };
    }
    let mut reasons = Vec::new();
    if credential.assurance.is_none_or(|level| level < required) {
        reasons.push(Reason::BelowMinimum);
    }
    if credential
        .token_lifetime_s
        .is_some_and(|lifetime| lifetime > i64::from(policy.max_token_lifetime_s))
    {
        reasons.push(Reason::TokenLifetimeExceedsPolicy);
    }
    if let Some(allowed) = &policy.allowed_issuers
        && credential.kind.is_workload()
        && !credential
            .idp_server_id
            .is_some_and(|idp| allowed.iter().any(|a| a == idp))
    {
        reasons.push(Reason::IssuerNotAllowed);
    }
    if credential.is_user_key && !policy.allow_user_keys {
        reasons.push(Reason::UserKeyNotAllowed);
    }
    match reasons.into_iter().min() {
        None => IdentityVerdict {
            outcome: Outcome::Met,
            reason: None,
            required,
        },
        Some(reason) => IdentityVerdict {
            outcome: match policy.mode {
                Mode::Enforce => Outcome::Refused,
                Mode::Audit => Outcome::BelowPolicy,
            },
            reason: Some(reason),
            required,
        },
    }
}

/// An authenticated caller against a policy. Pure: reads the credential
/// facts authentication stated on the context.
pub fn judge(auth: &AuthContext, policy: &IdentityPolicy) -> IdentityVerdict {
    judge_credential(&JudgedCredential::of(auth), policy)
}

// ---------------------------------------------------------------------------
// The policy store
// ---------------------------------------------------------------------------

/// The identity facts of a group's chain, as the group cache keeps them:
/// every group from the root to the group itself, each with its name.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct GroupIdentity {
    /// Root first, the group itself last.
    pub chain: Vec<ChainNode>,
    /// Each node's display name, in [`Self::chain`] order.
    pub names: Vec<String>,
}

impl GroupIdentity {
    /// The position of the AI System a call of this group is attributed to:
    /// the group itself or its nearest ancestor that is one.
    pub fn ai_system(&self) -> Option<usize> {
        self.chain.iter().rposition(|node| node.is_ai_system)
    }
}

/// Read the identity facts of a chain. `self_first` is the group and its
/// ancestors, the group first (the order the group cache walks them in).
/// A group of the chain that is not found fails the read: a chain cut short
/// would lose a floor.
pub async fn load_group_identity(
    db: &sqlx::PgPool,
    tenant_id: &str,
    self_first: &[String],
) -> Result<GroupIdentity, sqlx::Error> {
    type Row = (
        String,
        Option<String>,
        Option<String>,
        Option<String>,
        Option<Value>,
        String,
    );
    let rows: Vec<Row> = sqlx::query_as(
        "SELECT id, group_type::text, lifecycle_stage, eu_ai_act_risk_tier, identity_policy, \
                COALESCE(NULLIF(display_name, ''), name) \
           FROM resource_groups WHERE tenant_id = $1 AND id = ANY($2)",
    )
    .bind(tenant_id)
    .bind(self_first)
    .fetch_all(db)
    .await?;
    let mut identity = GroupIdentity::default();
    for id in self_first.iter().rev() {
        let (_, group_type, stage, tier, policy, name) = rows
            .iter()
            .find(|row| &row.0 == id)
            .ok_or(sqlx::Error::RowNotFound)?;
        identity.chain.push(ChainNode {
            id: id.clone(),
            // The enum stores member names (`AI_SYSTEM`); the API speaks
            // values (`ai_system`).
            is_ai_system: group_type
                .as_deref()
                .is_some_and(|t| t.eq_ignore_ascii_case("ai_system")),
            lifecycle_stage: stage.as_deref().map(str::to_ascii_lowercase),
            risk_tier: tier.as_deref().map(str::to_ascii_lowercase),
            identity_policy: policy
                .as_ref()
                .filter(|v| !v.is_null())
                .map(|v| IdentityPolicy::from_stored(v, id)),
            // Gate exceptions are not stored yet: none lowers a floor.
            gate_exception: false,
        });
        identity.names.push(name.clone());
    }
    Ok(identity)
}

/// The policy a call is judged by, and the groups the refusal names.
#[derive(Debug, Clone)]
pub struct CallPolicy {
    pub resolved: EffectivePolicy,
    /// The acting group (the caller's bound group) and its name.
    pub group: Option<(String, String)>,
    /// The AI System the call is attributed to, and its name.
    pub ai_system: Option<(String, String)>,
}

impl CallPolicy {
    /// The policy of `identity` (the acting group's chain, possibly empty)
    /// under the tenant's default.
    pub fn of(tenant_default: Option<IdentityPolicyDefault>, identity: &GroupIdentity) -> Self {
        let named = |i: usize| (identity.chain[i].id.clone(), identity.names[i].clone());
        let ai_system = identity.ai_system().map(named);
        let group = identity.chain.len().checked_sub(1).map(named);
        let resolved = resolve(&ResolverInput {
            tenant_default,
            chain: identity.chain.clone(),
            attributed_ai_system_id: ai_system.as_ref().map(|(id, _)| id.clone()),
        });
        Self {
            resolved,
            group,
            ai_system,
        }
    }
}

/// The effective policy of an authenticated call: the caller's bound group's
/// chain (a group-scoped key's group, a workload agent's group) under the
/// tenant default. A caller no group governs (a person's session, the
/// platform service key, an anonymous caller) is judged by the tenant
/// default, else the development default. A store that cannot be read is an
/// error: the call is refused, never judged by a floor read as open.
pub async fn policy_for_call(
    state: &crate::services::AppState,
    auth: &AuthContext,
) -> Result<CallPolicy, AuthError> {
    let unavailable = |what: &str| {
        AuthError::Unavailable(format!(
            "the identity policy of this call could not be read ({what})"
        ))
    };
    let group_unreadable = |e: &sqlx::Error, tenant_id: &str, group_id: &str| {
        tracing::error!(error = %e, tenant_id, group_id, "group identity policy unreadable; refusing");
        unavailable("the caller's resource group")
    };
    // The group governs the credential, and its tenant's default with it:
    // a key that a listed tenant accepts is still judged by its own group.
    let (policy_tenant, identity) = match auth.bound_group_id() {
        None => (auth.tenant_id.clone(), GroupIdentity::default()),
        Some(group_id) => {
            let in_request = state
                .group_cache
                .try_get_effective_group(&auth.tenant_id, group_id)
                .await
                .map_err(|e| group_unreadable(&e, &auth.tenant_id, group_id))?;
            match in_request {
                Some(group) => (auth.tenant_id.clone(), group.identity.clone()),
                None => {
                    let owner: Option<String> = sqlx::query_scalar(
                        "SELECT tenant_id FROM resource_groups WHERE id = $1 AND is_active = true",
                    )
                    .bind(group_id)
                    .fetch_optional(&state.db)
                    .await
                    .map_err(|e| group_unreadable(&e, &auth.tenant_id, group_id))?;
                    let Some(owner) = owner.filter(|owner| owner != &auth.tenant_id) else {
                        return Err(unavailable("the caller's resource group"));
                    };
                    let group = state
                        .group_cache
                        .try_get_effective_group(&owner, group_id)
                        .await
                        .map_err(|e| group_unreadable(&e, &owner, group_id))?
                        .ok_or_else(|| unavailable("the caller's resource group"))?;
                    (owner, group.identity.clone())
                }
            }
        }
    };
    let tenant_default = match crate::services::tenant_features::status(state, &policy_tenant).await
    {
        Ok(Some(status)) => status.identity_policy_default,
        Ok(None) => return Err(unavailable("no such tenant")),
        Err(e) => {
            tracing::error!(error = %e, tenant_id = %policy_tenant, "tenant identity default unreadable; refusing");
            return Err(unavailable("the tenant"));
        }
    };
    Ok(CallPolicy::of(tenant_default, &identity))
}

// ---------------------------------------------------------------------------
// The refusal
// ---------------------------------------------------------------------------

/// What a refused caller is told: which policy refused it, what it needed,
/// what it presented and why that was not enough.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CredentialRequirement {
    /// The AI System the call is attributed to (`None` when no AI System
    /// governs the acting group).
    pub ai_system_id: Option<String>,
    pub ai_system_name: Option<String>,
    /// The acting group (`None` for a caller no group governs).
    pub group_id: Option<String>,
    pub group_name: Option<String>,
    /// The effective `min_assurance`.
    pub required: CredentialAssurance,
    pub presented: Option<CredentialAssurance>,
    pub presented_kind: CredentialKind,
    pub reason: Reason,
    /// The effective `max_token_lifetime_s`, and the token's lifetime.
    pub max_token_lifetime_s: u32,
    pub token_lifetime_s: Option<i64>,
}

impl CredentialRequirement {
    /// A refused verdict on `auth` under `call`.
    pub fn of(auth: &AuthContext, call: &CallPolicy, verdict: &IdentityVerdict) -> Self {
        Self {
            ai_system_id: call.ai_system.as_ref().map(|(id, _)| id.clone()),
            ai_system_name: call.ai_system.as_ref().map(|(_, name)| name.clone()),
            group_id: call.group.as_ref().map(|(id, _)| id.clone()),
            group_name: call.group.as_ref().map(|(_, name)| name.clone()),
            required: verdict.required,
            presented: auth.credential_assurance,
            presented_kind: auth.credential_kind,
            // A refusal always names its reason; one that does not is
            // still a refusal.
            reason: verdict.reason.unwrap_or(Reason::BelowMinimum),
            max_token_lifetime_s: call.resolved.effective.max_token_lifetime_s,
            token_lifetime_s: auth.credential_detail.token_lifetime_s,
        }
    }

    /// Who refused: the AI System, else the acting group, else the tenant.
    fn subject(&self) -> String {
        match (&self.ai_system_name, &self.group_name) {
            (Some(name), _) => format!("AI System '{name}'"),
            (None, Some(name)) => format!("Resource group '{name}'"),
            (None, None) => "This tenant's identity policy".to_string(),
        }
    }

    /// The human sentence, without the code.
    pub fn sentence(&self) -> String {
        let subject = self.subject();
        match self.reason {
            Reason::BelowMinimum => format!(
                "{subject} requires credential level {}; this request presented {} ({})",
                self.required.as_str(),
                self.presented
                    .map_or("no credential", CredentialAssurance::as_str),
                self.presented_kind.as_str()
            ),
            Reason::TokenLifetimeExceedsPolicy => format!(
                "{subject} accepts workload tokens that live at most {} s; this token lives {} s",
                self.max_token_lifetime_s,
                self.token_lifetime_s.unwrap_or_default()
            ),
            Reason::IssuerNotAllowed => {
                format!("{subject} does not accept tokens from this identity provider")
            }
            Reason::UserKeyNotAllowed => format!("{subject} does not accept personal API keys"),
        }
    }

    /// The message every envelope carries: the code, then the sentence.
    pub fn message(&self) -> String {
        format!("{REFUSAL_CODE}: {}", self.sentence())
    }

    /// The `credential_requirement` member.
    pub fn to_json(&self) -> Value {
        serde_json::json!({
            "ai_system_id": self.ai_system_id,
            "ai_system_name": self.ai_system_name,
            "group_id": self.group_id,
            "required": self.required.as_str(),
            "presented": self.presented.map(CredentialAssurance::as_str),
            "presented_kind": self.presented_kind.as_str(),
            "reason": self.reason.as_str(),
            "docs_url": format!("{DOCS_BASE_URL}{CREDENTIAL_ERRORS_PATH}"),
        })
    }
}

impl std::fmt::Display for CredentialRequirement {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message())
    }
}

/// Put the requirement into a refusal body, where the surface's envelope
/// has room for it:
///
/// * an OpenAI-shaped `{"error": {...}}`: `type`, `code` and
///   `credential_requirement` inside `error`;
/// * Anthropic's `{"type": "error", "error": {...}}`: `credential_requirement`
///   inside `error`;
/// * a `google.rpc` status (`error.status`, Gemini and A2A v1): `code` 403,
///   `PERMISSION_DENIED`, and the requirement appended to `details` with
///   its `@type`;
/// * a coded refusal `{"error": "<sentence>"}`: `code` and
///   `credential_requirement` beside it.
pub fn attach_requirement(body: &mut Value, requirement: &CredentialRequirement) {
    let members = requirement.to_json();
    let Some(object) = body.as_object_mut() else {
        return;
    };
    let anthropic = object.get("type").and_then(Value::as_str) == Some("error");
    match object.get_mut("error") {
        Some(Value::Object(error)) if anthropic => {
            error.insert("type".into(), Value::from("permission_error"));
            error.insert("credential_requirement".into(), members);
        }
        Some(Value::Object(error)) if error.get("status").is_some_and(Value::is_string) => {
            error.insert("code".into(), Value::from(403));
            error.insert("status".into(), Value::from("PERMISSION_DENIED"));
            let mut detail = Map::new();
            detail.insert("@type".into(), Value::from(REQUIREMENT_DETAIL_TYPE));
            if let Value::Object(members) = members {
                detail.extend(members);
            }
            match error.get_mut("details") {
                Some(Value::Array(details)) => details.push(Value::Object(detail)),
                _ => {
                    error.insert("details".into(), Value::Array(vec![Value::Object(detail)]));
                }
            }
        }
        Some(Value::Object(error)) => {
            error.insert("type".into(), Value::from("permission_error"));
            error.insert("code".into(), Value::from(REFUSAL_CODE));
            error.insert("credential_requirement".into(), members);
        }
        Some(Value::String(_)) => {
            object.insert("code".into(), Value::from(REFUSAL_CODE));
            object.insert("credential_requirement".into(), members);
        }
        _ => {}
    }
}

/// Name the required level in the response's header.
pub fn attach_required_header(response: &mut Response, requirement: &CredentialRequirement) {
    response.headers_mut().insert(
        CREDENTIAL_REQUIRED_HEADER,
        HeaderValue::from_static(requirement.required.as_str()),
    );
}

/// The surface's own refusal `envelope` for `error`; a credential refusal
/// also gets its requirement in the body and the header. Every surface
/// answers a failed [`judge_call`] through here.
pub async fn refusal_response(error: &AuthError, envelope: Response) -> Response {
    let AuthError::CredentialAssuranceInsufficient(requirement) = error else {
        return envelope;
    };
    let (mut parts, body) = envelope.into_parts();
    // A refusal body is a few hundred bytes the surface just built. One
    // that cannot be read is replaced by the canonical envelope: the caller
    // is still refused, and still told why.
    let bytes = match axum::body::to_bytes(body, 1 << 20).await {
        Ok(bytes) => bytes,
        Err(e) => {
            tracing::error!(error = %e, "refusal body unreadable; answering with the canonical envelope");
            axum::body::Bytes::from(
                serde_json::json!({"error": {"message": requirement.message()}}).to_string(),
            )
        }
    };
    let body = match serde_json::from_slice::<Value>(&bytes) {
        Ok(mut value) => {
            attach_requirement(&mut value, requirement);
            parts.headers.remove(axum::http::header::CONTENT_LENGTH);
            axum::body::Body::from(serde_json::to_vec(&value).unwrap_or_else(|_| bytes.to_vec()))
        }
        Err(_) => axum::body::Body::from(bytes),
    };
    let mut response = Response::from_parts(parts, body);
    attach_required_header(&mut response, requirement);
    response
}

/// Write the one `proxy_logs` row of a refused call: on the surface of the
/// request's route (outside the evidence surfaces, an authentication row),
/// status 403, `access_denied`, `error_type` [`ERROR_TYPE`], the sentence,
/// the acting group, and the credential facts with the `refused` verdict.
fn record_refusal(
    state: &crate::services::AppState,
    auth: &AuthContext,
    requirement: &CredentialRequirement,
) {
    let (route, label, method) = crate::services::refused_agent_credential::current_request()
        .unwrap_or_else(|| {
            (
                None,
                crate::services::refused_agent_credential::ENDPOINT.to_string(),
                None,
            )
        });
    let (proxy_type, endpoint) = match route {
        Some(route) => (route.proxy_type, route.label),
        None => (crate::constants::proxy::OAUTH, label),
    };
    let correlation_id = ulid::Ulid::new().to_string();
    let sentence = requirement.sentence();
    let mut log = crate::services::proxy_logger::build_proxy_log(
        proxy_type,
        auth,
        method.as_deref().unwrap_or("AUTH"),
        &endpoint,
        None,
        None,
        StatusCode::FORBIDDEN.as_u16(),
        0.0,
        false,
        Some(&sentence),
        &correlation_id,
    );
    log.proxy_subtype = Some(crate::constants::proxy_subtype::ACCESS_DENIED.to_string());
    log.error_type = Some(ERROR_TYPE.to_string());
    log.resource_group_id = requirement.group_id.clone();
    state
        .background_writer
        .write_proxy_log(state.db.clone(), log);
}

/// What every governed surface calls once authentication made `auth`:
/// resolve the call's policy ([`policy_for_call`]), [`judge`] the
/// credential, store the verdict on the context, and refuse when the
/// verdict is `refused`. The first refusal of a context writes its row
/// ([`record_refusal`]); a context judged before keeps its first verdict, so
/// a surface that judges in two places writes one row.
pub async fn judge_call(
    state: &crate::services::AppState,
    auth: &mut AuthContext,
) -> Result<IdentityVerdict, AuthError> {
    if let Some(verdict) = auth.identity_verdict
        && verdict.outcome != Outcome::Refused
    {
        return Ok(verdict);
    }
    let call = policy_for_call(state, auth).await?;
    let (verdict, first) = settle(auth, &call.resolved.effective);
    if verdict.outcome != Outcome::Refused {
        return Ok(verdict);
    }
    let requirement = CredentialRequirement::of(auth, &call, &verdict);
    if first {
        record_refusal(state, auth, &requirement);
    }
    Err(AuthError::CredentialAssuranceInsufficient(Box::new(
        requirement,
    )))
}

/// The verdict of `auth` under `policy`: the one stored on the context, or
/// a new one, stored. `true` when it is new.
fn settle(auth: &mut AuthContext, policy: &IdentityPolicy) -> (IdentityVerdict, bool) {
    match auth.identity_verdict {
        Some(verdict) => (verdict, false),
        None => {
            let verdict = judge(auth, policy);
            auth.identity_verdict = Some(verdict);
            (verdict, true)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::auth::{AuthType, make_auth_context};

    fn policy(mode: Mode, min: CredentialAssurance) -> IdentityPolicy {
        IdentityPolicy {
            mode,
            min_assurance: min,
            ..IdentityPolicy::development_default()
        }
    }

    #[test]
    fn an_empty_chain_resolves_to_the_development_default() {
        let resolved = resolve(&ResolverInput::default());
        assert_eq!(resolved.effective, IdentityPolicy::development_default());
        assert_eq!(resolved.effective.mode, Mode::Audit);
        assert_eq!(resolved.effective.min_assurance, CredentialAssurance::Key);
        assert!(resolved.put_refused_fields.is_empty());
        assert_eq!(resolved.max_user_key_ttl_days, 90);
    }

    #[test]
    fn the_development_default_serves_every_authenticated_kind() {
        let default = IdentityPolicy::development_default();
        for auth_type in AuthType::ALL {
            let auth = make_auth_context(auth_type.clone(), "t");
            let verdict = judge(&auth, &default);
            assert_ne!(verdict.outcome, Outcome::Refused, "{auth_type:?}");
        }
    }

    #[test]
    fn a_production_floor_refuses_a_key_and_meets_a_workload_token() {
        let floor = policy(Mode::Enforce, CredentialAssurance::Workload);
        let key = make_auth_context(AuthType::ApiKey, "t");
        let verdict = judge(&key, &floor);
        assert_eq!(verdict.outcome, Outcome::Refused);
        assert_eq!(verdict.reason, Some(Reason::BelowMinimum));
        assert_eq!(verdict.required, CredentialAssurance::Workload);
        let token = make_auth_context(AuthType::IdpOauth, "t");
        assert_eq!(judge(&token, &floor).outcome, Outcome::Met);
        // Audit mode: below, served.
        let audit = policy(Mode::Audit, CredentialAssurance::Workload);
        assert_eq!(judge(&key, &audit).outcome, Outcome::BelowPolicy);
    }

    #[test]
    fn the_platform_service_key_and_a_session_are_exempt_everywhere() {
        let strictest = IdentityPolicy::strictest();
        let mut platform = make_auth_context(AuthType::ApiKey, "t");
        platform.credential_kind = CredentialKind::PlatformService;
        platform.credential_assurance = None;
        assert_eq!(judge(&platform, &strictest).outcome, Outcome::Exempt);
        let session = make_auth_context(AuthType::GatewayJwt, "t");
        assert_eq!(judge(&session, &strictest).outcome, Outcome::Exempt);
        // Anonymous is not exempt: below every level.
        let anonymous = make_auth_context(AuthType::Anonymous, "t");
        assert_eq!(judge(&anonymous, &strictest).outcome, Outcome::Refused);
    }

    #[test]
    fn a_personal_key_is_refused_where_user_keys_are_not_allowed() {
        let mut no_users = IdentityPolicy::development_default();
        no_users.mode = Mode::Enforce;
        no_users.allow_user_keys = false;
        let mut personal = make_auth_context(AuthType::ApiKey, "t");
        personal.api_key_user_id = Some(7);
        let verdict = judge(&personal, &no_users);
        assert_eq!(verdict.reason, Some(Reason::UserKeyNotAllowed));
        let mut scoped = make_auth_context(AuthType::ApiKey, "t");
        scoped.api_key_scope = Some("user".into());
        assert_eq!(judge(&scoped, &no_users).outcome, Outcome::Refused);
        let shared = make_auth_context(AuthType::ApiKey, "t");
        assert_eq!(judge(&shared, &no_users).outcome, Outcome::Met);
    }

    #[test]
    fn an_invalid_stored_policy_is_read_as_the_strictest() {
        let stored = serde_json::json!({"mode": "monitor"});
        assert_eq!(
            IdentityPolicy::from_stored(&stored, "rg"),
            IdentityPolicy::strictest()
        );
        let good = IdentityPolicy::development_default().to_json();
        assert_eq!(
            IdentityPolicy::from_stored(&good, "rg"),
            IdentityPolicy::development_default()
        );
    }

    #[test]
    fn a_context_keeps_its_first_verdict() {
        let mut auth = make_auth_context(AuthType::ApiKey, "t");
        let stored = IdentityVerdict {
            outcome: Outcome::Refused,
            reason: Some(Reason::BelowMinimum),
            required: CredentialAssurance::Workload,
        };
        auth.identity_verdict = Some(stored);
        let default = IdentityPolicy::development_default();
        assert_eq!(settle(&mut auth, &default), (stored, false));
        assert_eq!(auth.identity_verdict, Some(stored));

        let mut fresh = make_auth_context(AuthType::ApiKey, "t");
        let (verdict, first) = settle(&mut fresh, &default);
        assert!(first);
        assert_eq!(verdict.outcome, Outcome::Met);
        assert_eq!(fresh.identity_verdict, Some(verdict));
    }

    fn requirement(reason: Reason) -> CredentialRequirement {
        CredentialRequirement {
            ai_system_id: Some("sys-1".into()),
            ai_system_name: Some("Loan Pre-screening".into()),
            group_id: Some("rg-1".into()),
            group_name: Some("Screening".into()),
            required: CredentialAssurance::Workload,
            presented: Some(CredentialAssurance::Key),
            presented_kind: CredentialKind::ApiKey,
            reason,
            max_token_lifetime_s: 300,
            token_lifetime_s: Some(900),
        }
    }

    #[test]
    fn every_sentence_starts_with_the_code_and_names_who_refused() {
        let cases = [
            (
                Reason::BelowMinimum,
                "AI System 'Loan Pre-screening' requires credential level workload; this request presented key (api_key)",
            ),
            (
                Reason::TokenLifetimeExceedsPolicy,
                "AI System 'Loan Pre-screening' accepts workload tokens that live at most 300 s; this token lives 900 s",
            ),
            (
                Reason::IssuerNotAllowed,
                "AI System 'Loan Pre-screening' does not accept tokens from this identity provider",
            ),
            (
                Reason::UserKeyNotAllowed,
                "AI System 'Loan Pre-screening' does not accept personal API keys",
            ),
        ];
        for (reason, sentence) in cases {
            let r = requirement(reason);
            assert_eq!(r.sentence(), sentence);
            assert_eq!(
                r.message(),
                format!("CREDENTIAL_ASSURANCE_INSUFFICIENT: {sentence}")
            );
            let e = AuthError::CredentialAssuranceInsufficient(Box::new(r));
            assert!(
                e.to_string()
                    .starts_with("CREDENTIAL_ASSURANCE_INSUFFICIENT: ")
            );
            assert_eq!(e.status_code(), StatusCode::FORBIDDEN);
        }
        // Not an AI System: the group; no group: the tenant.
        let mut r = requirement(Reason::BelowMinimum);
        r.ai_system_id = None;
        r.ai_system_name = None;
        assert!(
            r.sentence()
                .starts_with("Resource group 'Screening' requires")
        );
        r.group_name = None;
        r.presented = None;
        r.presented_kind = CredentialKind::Anonymous;
        assert_eq!(
            r.sentence(),
            "This tenant's identity policy requires credential level workload; this request presented no credential (anonymous)"
        );
    }

    #[test]
    fn the_requirement_names_the_docs_page_from_the_one_constant() {
        let json = requirement(Reason::BelowMinimum).to_json();
        assert_eq!(
            json,
            serde_json::json!({
                "ai_system_id": "sys-1",
                "ai_system_name": "Loan Pre-screening",
                "group_id": "rg-1",
                "required": "workload",
                "presented": "key",
                "presented_kind": "api_key",
                "reason": "below_minimum",
                "docs_url": "https://docs.brutor.ai/build-an-agent/identity/errors/#credential_assurance_insufficient",
            })
        );
    }

    #[test]
    fn each_envelope_carries_the_requirement_where_it_has_room() {
        let r = requirement(Reason::BelowMinimum);
        let members = r.to_json();
        let msg = r.message();

        // OpenAI-compatible (`canonical_error_response`).
        let mut body = serde_json::json!({"error": {"message": msg}});
        attach_requirement(&mut body, &r);
        assert_eq!(
            body,
            serde_json::json!({"error": {
                "message": msg, "type": "permission_error",
                "code": "CREDENTIAL_ASSURANCE_INSUFFICIENT", "credential_requirement": members,
            }})
        );

        // A coded refusal.
        let mut body = serde_json::json!({"error": msg});
        attach_requirement(&mut body, &r);
        assert_eq!(
            body,
            serde_json::json!({"error": msg, "code": "CREDENTIAL_ASSURANCE_INSUFFICIENT", "credential_requirement": members})
        );

        // Anthropic.
        let mut body = serde_json::json!({"type": "error", "error": {"type": "permission_error", "message": msg}});
        attach_requirement(&mut body, &r);
        assert_eq!(
            body,
            serde_json::json!({"type": "error", "error": {
                "type": "permission_error", "message": msg, "credential_requirement": members,
            }})
        );

        // google.rpc (Gemini; A2A v1 with its ErrorInfo already present).
        let mut body =
            serde_json::json!({"error": {"code": 403, "message": msg, "status": "Forbidden"}});
        attach_requirement(&mut body, &r);
        let mut detail = members.as_object().unwrap().clone();
        detail.insert(
            "@type".into(),
            "type.brutor.ai/CredentialRequirement".into(),
        );
        assert_eq!(
            body,
            serde_json::json!({"error": {
                "code": 403, "message": msg, "status": "PERMISSION_DENIED", "details": [detail],
            }})
        );
        let info = serde_json::json!({"@type": "type.googleapis.com/google.rpc.ErrorInfo"});
        let mut body = serde_json::json!({"error": {"code": 403, "message": msg, "status": "PERMISSION_DENIED", "details": [info]}});
        attach_requirement(&mut body, &r);
        assert_eq!(body["error"]["details"].as_array().unwrap().len(), 2);
        assert_eq!(body["error"]["details"][0], info);
    }

    #[tokio::test]
    async fn a_refusal_response_carries_the_header_and_the_member() {
        let r = requirement(Reason::BelowMinimum);
        let e = AuthError::CredentialAssuranceInsufficient(Box::new(r));
        let envelope = crate::services::error_envelope::canonical_error_response(
            e.status_code(),
            &e.to_string(),
        );
        let response = refusal_response(&e, envelope).await;
        assert_eq!(response.status(), StatusCode::FORBIDDEN);
        assert_eq!(
            response
                .headers()
                .get("Brutor-Credential-Required")
                .unwrap(),
            "workload"
        );
        let bytes = axum::body::to_bytes(response.into_body(), 1 << 20)
            .await
            .unwrap();
        let body: Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(body["error"]["code"], "CREDENTIAL_ASSURANCE_INSUFFICIENT");
        assert_eq!(
            body["error"]["credential_requirement"]["reason"],
            "below_minimum"
        );

        // Any other refusal is the envelope as it was.
        let other = AuthError::GroupNotMember;
        let envelope = crate::services::error_envelope::canonical_error_response(
            other.status_code(),
            &other.to_string(),
        );
        let response = refusal_response(&other, envelope).await;
        assert!(
            response
                .headers()
                .get("Brutor-Credential-Required")
                .is_none()
        );
    }

    #[test]
    fn a_chain_attributes_the_call_to_its_nearest_ai_system() {
        let node = |id: &str, ai: bool, stage: Option<&str>| ChainNode {
            id: id.into(),
            is_ai_system: ai,
            lifecycle_stage: stage.map(Into::into),
            ..Default::default()
        };
        let identity = GroupIdentity {
            chain: vec![
                node("root", false, None),
                node("sys", true, Some("active")),
                node("team", false, None),
            ],
            names: vec!["Root".into(), "Screening".into(), "Team".into()],
        };
        let call = CallPolicy::of(None, &identity);
        assert_eq!(call.ai_system, Some(("sys".into(), "Screening".into())));
        assert_eq!(call.group, Some(("team".into(), "Team".into())));
        assert_eq!(call.resolved.effective.mode, Mode::Enforce);
        assert_eq!(
            call.resolved.effective.min_assurance,
            CredentialAssurance::Workload
        );
        // No group: the tenant default, else the development default.
        let none = CallPolicy::of(None, &GroupIdentity::default());
        assert_eq!(none.group, None);
        assert_eq!(
            none.resolved.effective,
            IdentityPolicy::development_default()
        );
    }
}
