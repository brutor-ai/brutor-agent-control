// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The cause-attribution ladder.
//!
//! This is the differentiator.
//!
//! An alert that says *"step count up 180%, p=0.001"* hands the whole
//! investigation to the operator. One that says *"step count up 180% since
//! 14:00 on the 14th; the provider shipped a new point release of the
//! underlying model at 13:52"* ends it before it starts.
//!
//! Brutor is the only component that sees both the traffic **and** the
//! model registry, the MCP capability snapshots, and the resource-group
//! composition. No APM can make that correlation, because no APM has the
//! other half of the data.
//!
//! ## How it works
//!
//! Candidates are walked in a fixed order and the first one whose change
//! window overlaps the detected **change point** wins. Matching against
//! the change point rather than the detection time matters: the sweep runs
//! on a timer, so matching on "when we noticed" would blame whatever
//! happened during the sweep.
//!
//! ## The rung that stops false alarms
//!
//! `input_drift` sits deliberately below the dependency rungs and above
//! `unexplained`. When behavioural drift and input drift move together,
//! the system did not change — the *questions* did. That single
//! distinction is worth more than three extra metrics, because without it
//! every Monday-morning traffic shift reads as a regression.
//!
//! ## Saying "I don't know"
//!
//! The ladder terminates at `unexplained`, which is a real answer and is
//! shipped with the top correlated metrics attached. Inventing a cause to
//! avoid an empty field would be worse than useless: an operator who
//! chases one fabricated explanation stops believing the true ones.

use chrono::{DateTime, Duration, Utc};
use serde_json::{Value, json};
use sqlx::PgPool;

use super::CAUSE_WINDOW_HOURS;

/// The cause labels written to `drift_attributions.cause`.
///
/// Named `kind` rather than `cause`: nesting a `cause` module inside
/// `cause.rs` made call sites read `cause::cause::INPUT_DRIFT`, which says
/// the word twice and the useful part once.
pub mod kind {
    /// The change point falls inside, or at the edge of, an
    /// incident window a human declared for the system or its tenant (a
    /// provider outage, a platform incident, maintenance). Samples inside
    /// a window are never judged, so a change point there is the window's
    /// own edge: the incident's onset or its tail.
    pub const INCIDENT_WINDOW: &str = "incident_window";
    pub const CONTRACT_CHANGE: &str = "contract_change";
    /// The agent behind the system shipped a new release (name /
    /// version / build), or kept its release but started behaving like
    /// different software (a new implementation fingerprint).
    pub const AGENT_RELEASE: &str = "agent_release";
    pub const MODEL_CHANGE: &str = "model_change";
    /// The instructions changed with no release: the dominant
    /// system-prompt fingerprint moved across the change point.
    pub const PROMPT_CHANGE: &str = "prompt_change";
    pub const DEPENDENCY_CHANGE: &str = "dependency_change";
    pub const DEPENDENCY_DEGRADED: &str = "dependency_degraded";
    pub const COMPOSITION_CHANGE: &str = "composition_change";
    /// What the system retrieves from changed: the collection set its
    /// runs read from moved across the change point.
    pub const CORPUS_CHANGE: &str = "corpus_change";
    pub const INPUT_DRIFT: &str = "input_drift";
    pub const TRAFFIC_MIX: &str = "traffic_mix";
    pub const UNEXPLAINED: &str = "unexplained";

    /// Walked in this order; first match wins.
    pub const LADDER: [&str; 12] = [
        INCIDENT_WINDOW,
        CONTRACT_CHANGE,
        AGENT_RELEASE,
        MODEL_CHANGE,
        PROMPT_CHANGE,
        DEPENDENCY_CHANGE,
        DEPENDENCY_DEGRADED,
        COMPOSITION_CHANGE,
        CORPUS_CHANGE,
        INPUT_DRIFT,
        TRAFFIC_MIX,
        UNEXPLAINED,
    ];
}

/// What the ladder concluded.
#[derive(Debug, Clone, PartialEq)]
pub struct Attribution {
    pub cause: &'static str,
    /// 0..1. High when a dated, announced change lines up with the change
    /// point; low when we are inferring from a correlation.
    pub confidence: f64,
    pub evidence: Value,
    /// Human-readable, and written to be the first line an operator reads.
    pub summary: String,
}

impl Attribution {
    pub fn unexplained(correlated: Vec<String>) -> Self {
        Self {
            cause: kind::UNEXPLAINED,
            confidence: 0.0,
            evidence: json!({ "correlated_metrics": correlated }),
            summary: "No change to the model, tools, composition or input distribution \
                 lines up with this. Starting points are the correlated metrics below."
                .to_string(),
        }
    }

    pub fn is_explained(&self) -> bool {
        self.cause != kind::UNEXPLAINED
    }
}

/// Everything the ladder needs, gathered before it runs so the decision
/// itself is a pure function over facts.
#[derive(Debug, Clone, Default)]
pub struct CauseFacts {
    /// A declared incident window overlapping the change point.
    pub incident_window: Option<IncidentWindowRef>,
    /// The served system's active contract moved across the
    /// change point (segment basis: every segment records the contract it
    /// was served under).
    pub contract_change: Option<ContractChange>,
    /// A release transition in the system's runs (or, for a
    /// service, the calls it served) near the change point.
    pub release_change: Option<ReleaseChange>,
    /// A model whose identifier or catalog entry changed near the change point.
    pub model_change: Option<ModelChange>,
    /// The dominant instruction fingerprint moved near the change point.
    pub prompt_change: Option<PromptChange>,
    /// An MCP tool schema that moved near the change point.
    pub dependency_change: Option<DependencyChange>,
    /// A tool whose error rate rose without its schema changing.
    pub dependency_degraded: Option<DependencyDegraded>,
    /// Resource-group membership changed near the change point.
    pub composition_change: Option<CompositionChange>,
    /// The retrieval collection set moved near the change point.
    pub corpus_change: Option<CorpusChange>,
    /// Input-distribution metrics moved in the same window.
    pub input_drifted: bool,
    /// A caller appeared that had not been seen before.
    pub new_caller: Option<String>,
    /// Metrics that moved alongside, for the unexplained case.
    pub correlated_metrics: Vec<String>,
}

/// A declared incident window (`ai_system_incident_windows`).
#[derive(Debug, Clone, PartialEq)]
pub struct IncidentWindowRef {
    pub id: String,
    /// Declared for every system in the tenant (`ai_system_id IS NULL`).
    pub tenant_wide: bool,
    pub starts_at: DateTime<Utc>,
    /// `None` while the window is still open.
    pub ends_at: Option<DateTime<Utc>>,
    pub cause_category: String,
    pub reason: Option<String>,
}

impl IncidentWindowRef {
    /// Whether `at` lies inside the window or within the cause window of
    /// either edge.
    pub fn overlaps(&self, at: DateTime<Utc>) -> bool {
        let slack = Duration::hours(CAUSE_WINDOW_HOURS);
        at >= self.starts_at - slack && self.ends_at.is_none_or(|e| at <= e + slack)
    }
}

/// The contract a service was serving under, before and after
/// the change point.
#[derive(Debug, Clone, PartialEq)]
pub struct ContractChange {
    pub from_contract_id: String,
    pub to_contract_id: String,
    pub changed_at: DateTime<Utc>,
}

/// One side of a release transition.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReleaseRef {
    pub name: Option<String>,
    pub version: Option<String>,
    pub build: Option<String>,
}

impl ReleaseRef {
    /// `name@version (build)`, the way the console names a release.
    pub fn label(&self) -> String {
        let mut s = format!(
            "{}@{}",
            self.name.as_deref().unwrap_or("unnamed"),
            self.version.as_deref().unwrap_or("unversioned"),
        );
        if let Some(b) = &self.build {
            s.push_str(&format!(" ({b})"));
        }
        s
    }

    fn evidence(&self) -> Value {
        json!({ "name": self.name, "version": self.version, "build": self.build })
    }
}

/// A release transition, or (`version_changed == false`) the
/// same release showing a new implementation fingerprint: the undeclared
/// change, which is the one nobody announced.
#[derive(Debug, Clone, PartialEq)]
pub struct ReleaseChange {
    /// The agent identity (run basis); `None` on the segment basis, where
    /// the delegate's identity is not recorded per segment.
    pub agent_id: Option<String>,
    pub from: ReleaseRef,
    pub to: ReleaseRef,
    pub changed_at: DateTime<Utc>,
    pub version_changed: bool,
    pub from_fingerprint: Option<String>,
    pub to_fingerprint: Option<String>,
    pub trust: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ModelChange {
    pub model_id: String,
    pub from_version: Option<String>,
    pub to_version: Option<String>,
    pub changed_at: DateTime<Utc>,
    /// Set when the change is explained by a scheduled retirement — the
    /// strongest evidence available, because it was announced in advance
    /// and names its own successor.
    pub retirement: Option<RetirementContext>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct RetirementContext {
    pub retire_date: String,
    pub replacement_model_name: Option<String>,
    pub notes: Option<String>,
}

/// The dominant instruction fingerprint before and after the change point.
#[derive(Debug, Clone, PartialEq)]
pub struct PromptChange {
    pub from_fingerprint: String,
    pub to_fingerprint: String,
    pub changed_at: DateTime<Utc>,
}

/// The retrieval collection set before and after the change point.
#[derive(Debug, Clone, PartialEq)]
pub struct CorpusChange {
    pub from_collections: Vec<String>,
    pub to_collections: Vec<String>,
    pub changed_at: DateTime<Utc>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct DependencyChange {
    pub mcp_server_id: String,
    pub tools_added: Vec<String>,
    pub tools_removed: Vec<String>,
    pub schemas_changed: Vec<String>,
    pub changed_at: DateTime<Utc>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct DependencyDegraded {
    pub tool_or_type: String,
    pub baseline_error_rate: f64,
    pub observed_error_rate: f64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct CompositionChange {
    pub description: String,
    pub changed_at: DateTime<Utc>,
}

/// Whether a candidate's timestamp is close enough to the change point to
/// be its explanation.
pub fn within_cause_window(candidate_at: DateTime<Utc>, change_point: DateTime<Utc>) -> bool {
    (candidate_at - change_point).num_seconds().abs()
        <= Duration::hours(CAUSE_WINDOW_HOURS).num_seconds()
}

/// Walk the ladder. Pure — every fact is gathered beforehand.
pub fn attribute(facts: &CauseFacts, change_point: Option<DateTime<Utc>>) -> Attribution {
    // Rung 0 — a human already declared an incident here. Its
    // samples are excluded from judgement, so a change point at its edge is
    // the incident's onset or tail, and saying so is the one explanation an
    // operator has already signed.
    if let Some(w) = &facts.incident_window {
        if change_point.map(|cp| w.overlaps(cp)).unwrap_or(true) {
            let until = w
                .ends_at
                .map(|e| e.to_rfc3339())
                .unwrap_or_else(|| "still open".to_string());
            return Attribution {
                cause: kind::INCIDENT_WINDOW,
                confidence: 0.9,
                evidence: json!({
                    "incident_window_id": w.id,
                    "tenant_wide": w.tenant_wide,
                    "starts_at": w.starts_at.to_rfc3339(),
                    "ends_at": w.ends_at.map(|e| e.to_rfc3339()),
                    "cause_category": w.cause_category,
                    "reason": w.reason,
                }),
                summary: format!(
                    "This lines up with a declared incident window ({}{}, {} → {}){}. \
                     Runs inside the window are not judged; this change is at its edge.",
                    w.cause_category,
                    if w.tenant_wide { ", tenant-wide" } else { "" },
                    w.starts_at.to_rfc3339(),
                    until,
                    w.reason
                        .as_deref()
                        .filter(|r| !r.is_empty())
                        .map(|r| format!(": {r}"))
                        .unwrap_or_default(),
                ),
            };
        }
    }

    // Rung 1 — the system promoted a new contract. A promotion is a signed,
    // dated, reviewed change to what the system may use; when it lines up
    // with the change point nothing else is a better explanation.
    if let Some(cc) = &facts.contract_change {
        if change_point
            .map(|cp| within_cause_window(cc.changed_at, cp))
            .unwrap_or(true)
        {
            return Attribution {
                cause: kind::CONTRACT_CHANGE,
                confidence: 0.9,
                evidence: json!({
                    "from_contract_id": cc.from_contract_id,
                    "to_contract_id": cc.to_contract_id,
                    "changed_at": cc.changed_at.to_rfc3339(),
                }),
                summary: format!(
                    "This system started serving under a new contract at {} ({} → {}). \
                     Its callers are now getting the behaviour the new contract permits.",
                    cc.changed_at.to_rfc3339(),
                    cc.from_contract_id,
                    cc.to_contract_id,
                ),
            };
        }
    }

    // Rung 2 — the agent itself changed. A redeploy is the most
    // common change of all, and until the agent declared its release the
    // gateway could not see it. The undeclared variant (same release, new
    // fingerprint) is weaker evidence but the more important finding.
    if let Some(rc) = &facts.release_change {
        if change_point
            .map(|cp| within_cause_window(rc.changed_at, cp))
            .unwrap_or(true)
        {
            let evidence = json!({
                "agent_id": rc.agent_id,
                "from": rc.from.evidence(),
                "to": rc.to.evidence(),
                "changed_at": rc.changed_at.to_rfc3339(),
                "version_change": rc.version_changed,
                "from_fingerprint": rc.from_fingerprint,
                "to_fingerprint": rc.to_fingerprint,
                "trust": rc.trust,
            });
            return if rc.version_changed {
                Attribution {
                    cause: kind::AGENT_RELEASE,
                    confidence: 0.85,
                    evidence,
                    summary: format!(
                        "The agent behind this system changed release at {} ({} → {}). \
                         A new release of the agent is the most direct explanation.",
                        rc.changed_at.to_rfc3339(),
                        rc.from.label(),
                        rc.to.label(),
                    ),
                }
            } else {
                Attribution {
                    cause: kind::AGENT_RELEASE,
                    confidence: 0.7,
                    evidence,
                    summary: format!(
                        "No version change: the agent still declares {} but started \
                         behaving like different software at {} (new implementation \
                         fingerprint). An undeclared change to the agent is the likely \
                         explanation.",
                        rc.to.label(),
                        rc.changed_at.to_rfc3339(),
                    ),
                }
            };
        }
    }

    // Rung 3 — a model changed. The demo moment, and the correlation
    // nobody else can make.
    if let Some(mc) = &facts.model_change {
        if change_point
            .map(|cp| within_cause_window(mc.changed_at, cp))
            .unwrap_or(true)
        {
            // A scheduled retirement is the strongest evidence there is:
            // dated, announced ahead of time, and it names the successor.
            if let Some(ret) = &mc.retirement {
                return Attribution {
                    cause: kind::MODEL_CHANGE,
                    confidence: 0.95,
                    evidence: json!({
                        "model_id": mc.model_id,
                        "from_version": mc.from_version,
                        "to_version": mc.to_version,
                        "changed_at": mc.changed_at.to_rfc3339(),
                        "retire_date": ret.retire_date,
                        "replacement_model_name": ret.replacement_model_name,
                        "notes": ret.notes,
                    }),
                    summary: format!(
                        "Model '{}' reached its announced retirement date ({}){}. \
                         Traffic moved to the replacement, which is the most likely \
                         explanation for this change.",
                        mc.model_id,
                        ret.retire_date,
                        ret.replacement_model_name
                            .as_ref()
                            .map(|r| format!(" and was replaced by '{r}'"))
                            .unwrap_or_default(),
                    ),
                };
            }
            return Attribution {
                cause: kind::MODEL_CHANGE,
                confidence: 0.85,
                evidence: json!({
                    "model_id": mc.model_id,
                    "from_version": mc.from_version,
                    "to_version": mc.to_version,
                    "changed_at": mc.changed_at.to_rfc3339(),
                }),
                summary: format!(
                    "The model serving this system changed at {} ({} → {}). \
                     Provider-side model updates are the most common cause of a \
                     behavioural shift with no configuration change.",
                    mc.changed_at.to_rfc3339(),
                    mc.from_version.as_deref().unwrap_or("unknown"),
                    mc.to_version.as_deref().unwrap_or("unknown"),
                ),
            };
        }
    }

    // Rung 3 — a tool's declared schema moved.
    if let Some(pc) = &facts.prompt_change {
        if change_point
            .map(|cp| within_cause_window(pc.changed_at, cp))
            .unwrap_or(true)
        {
            return Attribution {
                cause: kind::PROMPT_CHANGE,
                confidence: 0.85,
                evidence: json!({
                    "from_fingerprint": pc.from_fingerprint,
                    "to_fingerprint": pc.to_fingerprint,
                    "changed_at": pc.changed_at.to_rfc3339(),
                }),
                summary: format!(
                    "The instructions this system runs with changed at {} (prompt \
                     fingerprint {}… → {}…). A prompt edit ships with no release and \
                     no model change; this is the door it came through.",
                    pc.changed_at.to_rfc3339(),
                    &pc.from_fingerprint[..pc.from_fingerprint.len().min(12)],
                    &pc.to_fingerprint[..pc.to_fingerprint.len().min(12)],
                ),
            };
        }
    }
    if let Some(dc) = &facts.dependency_change {
        if change_point
            .map(|cp| within_cause_window(dc.changed_at, cp))
            .unwrap_or(true)
        {
            return Attribution {
                cause: kind::DEPENDENCY_CHANGE,
                confidence: 0.8,
                evidence: json!({
                    "mcp_server_id": dc.mcp_server_id,
                    "tools_added": dc.tools_added,
                    "tools_removed": dc.tools_removed,
                    "schemas_changed": dc.schemas_changed,
                    "changed_at": dc.changed_at.to_rfc3339(),
                }),
                summary: format!(
                    "MCP server '{}' changed its tool definitions at {} \
                     (+{} / -{} / {} modified). The system is working against a \
                     different contract than it was.",
                    dc.mcp_server_id,
                    dc.changed_at.to_rfc3339(),
                    dc.tools_added.len(),
                    dc.tools_removed.len(),
                    dc.schemas_changed.len(),
                ),
            };
        }
    }

    // Rung 4 — a tool got worse without changing shape.
    if let Some(dd) = &facts.dependency_degraded {
        return Attribution {
            cause: kind::DEPENDENCY_DEGRADED,
            confidence: 0.7,
            evidence: json!({
                "tool": dd.tool_or_type,
                "baseline_error_rate": dd.baseline_error_rate,
                "observed_error_rate": dd.observed_error_rate,
            }),
            summary: format!(
                "Tool '{}' error rate rose from {:.1}% to {:.1}% with no change to \
                 its definition — the dependency is degrading, not changing.",
                dd.tool_or_type,
                dd.baseline_error_rate * 100.0,
                dd.observed_error_rate * 100.0,
            ),
        };
    }

    // Rung 5 — the system's own composition moved. Invisible if you key
    // the lifecycle on an agent credential rather than an AI System.
    if let Some(cc) = &facts.composition_change {
        if change_point
            .map(|cp| within_cause_window(cc.changed_at, cp))
            .unwrap_or(true)
        {
            return Attribution {
                cause: kind::COMPOSITION_CHANGE,
                confidence: 0.75,
                evidence: json!({
                    "description": cc.description,
                    "changed_at": cc.changed_at.to_rfc3339(),
                }),
                summary: format!(
                    "This AI System's composition changed at {}: {}.",
                    cc.changed_at.to_rfc3339(),
                    cc.description,
                ),
            };
        }
    }

    // Rung 6 — the disambiguator. The system did not change; the questions
    // did. Placed below the dependency rungs because a real dependency
    // change is a better explanation than a coincident traffic shift, and
    // above `unexplained` because it is a genuine answer.
    if let Some(cc) = &facts.corpus_change {
        if change_point
            .map(|cp| within_cause_window(cc.changed_at, cp))
            .unwrap_or(true)
        {
            return Attribution {
                cause: kind::CORPUS_CHANGE,
                confidence: 0.75,
                evidence: json!({
                    "from_collections": cc.from_collections,
                    "to_collections": cc.to_collections,
                    "changed_at": cc.changed_at.to_rfc3339(),
                }),
                summary: format!(
                    "The knowledge this system retrieves from changed at {} \
                     ({} → {}). A different corpus behind the same prompt is a \
                     different system, and this is when it became one.",
                    cc.changed_at.to_rfc3339(),
                    if cc.from_collections.is_empty() {
                        "none".to_string()
                    } else {
                        cc.from_collections.join(", ")
                    },
                    if cc.to_collections.is_empty() {
                        "none".to_string()
                    } else {
                        cc.to_collections.join(", ")
                    },
                ),
            };
        }
    }
    if facts.input_drifted {
        return Attribution {
            cause: kind::INPUT_DRIFT,
            confidence: 0.65,
            evidence: json!({ "input_metrics_moved": true }),
            summary: "The input distribution moved at the same time. This looks like a \
                 change in what the system is being asked to do rather than a change \
                 in the system itself."
                .to_string(),
        };
    }

    // Rung 7 — someone new started calling.
    if let Some(caller) = &facts.new_caller {
        return Attribution {
            cause: kind::TRAFFIC_MIX,
            confidence: 0.5,
            evidence: json!({ "new_caller": caller }),
            summary: format!(
                "A caller not previously seen ('{caller}') started using this system \
                 in the same window."
            ),
        };
    }

    Attribution::unexplained(facts.correlated_metrics.clone())
}

// ---------------------------------------------------------------------------
// Fact gathering
// ---------------------------------------------------------------------------

/// The incident window declared for this system (or its whole
/// tenant) that the change point falls inside or within the cause window of.
/// A system's own window is preferred over a tenant-wide one; among equals,
/// the most recent.
pub async fn find_incident_window(
    db: &PgPool,
    tenant_id: &str,
    ai_system_id: &str,
    change_point: DateTime<Utc>,
) -> Result<Option<IncidentWindowRef>, sqlx::Error> {
    type Row = (
        String,
        bool,
        DateTime<Utc>,
        Option<DateTime<Utc>>,
        String,
        Option<String>,
    );
    let row: Option<Row> = sqlx::query_as(
        "SELECT id, ai_system_id IS NULL, starts_at, ends_at, cause_category, reason
           FROM ai_system_incident_windows
          WHERE tenant_id = $1
            AND (ai_system_id IS NULL OR ai_system_id = $2)
            AND $3::timestamptz >= starts_at - MAKE_INTERVAL(hours => $4)
            AND (ends_at IS NULL OR $3::timestamptz <= ends_at + MAKE_INTERVAL(hours => $4))
          ORDER BY (ai_system_id IS NULL) ASC, starts_at DESC
          LIMIT 1",
    )
    .bind(tenant_id)
    .bind(ai_system_id)
    .bind(change_point)
    .bind(CAUSE_WINDOW_HOURS as i32)
    .fetch_optional(db)
    .await?;
    Ok(row.map(
        |(id, tenant_wide, starts_at, ends_at, cause_category, reason)| IncidentWindowRef {
            id,
            tenant_wide,
            starts_at,
            ends_at,
            cause_category,
            reason,
        },
    ))
}

/// Look for a model bound to this AI System whose retirement date fell
/// near the change point.
///
/// Retirement data is the only *forward-looking* input the platform has:
/// dated, announced ahead of time, and naming its own successor. When it
/// lines up with a change point there is very little left to guess.
pub async fn find_model_retirement(
    db: &PgPool,
    tenant_id: &str,
    ai_system_id: &str,
    change_point: DateTime<Utc>,
) -> Result<Option<ModelChange>, sqlx::Error> {
    let row: Option<(String, chrono::NaiveDate, Option<String>, Option<String>)> = sqlx::query_as(
        "SELECT m.id, m.retire_date, m.retirement_replacement_model_name, m.retirement_notes
               FROM llm_model m
               JOIN resource_group_llm_models b ON b.llm_model_id = m.id
              WHERE m.tenant_id = $1
                AND b.resource_group_id = $2
                AND m.retire_date IS NOT NULL
                AND m.retire_date BETWEEN ($3::timestamptz - INTERVAL '3 days')::date
                                      AND ($3::timestamptz + INTERVAL '1 day')::date
              ORDER BY m.retire_date DESC
              LIMIT 1",
    )
    .bind(tenant_id)
    .bind(ai_system_id)
    .bind(change_point)
    .fetch_optional(db)
    .await?;

    Ok(
        row.map(|(model_id, retire_date, replacement, notes)| ModelChange {
            model_id,
            from_version: None,
            to_version: replacement.clone(),
            changed_at: change_point,
            retirement: Some(RetirementContext {
                retire_date: retire_date.to_string(),
                replacement_model_name: replacement,
                notes,
            }),
        }),
    )
}

/// Look for an MCP tool-definition change near the change point.
///
/// `mcp_capability_snapshots` is append-only, so consecutive rows are a
/// diff of the declared tool contract — the platform's existing
/// supply-chain signal, reused here as a cause rather than an alert.
///
/// Only rows with `is_change` count. The control plane appends a
/// row only when the contract hash moved and sets `is_change` only when
/// the semantic diff against the predecessor is non-empty — so a routine
/// refresh or a server's first-ever snapshot is never blamed.
pub async fn find_dependency_change(
    db: &PgPool,
    tenant_id: &str,
    ai_system_id: &str,
    change_point: DateTime<Utc>,
) -> Result<Option<DependencyChange>, sqlx::Error> {
    let row: Option<(String, DateTime<Utc>)> = sqlx::query_as(
        "SELECT s.mcp_server_id, s.snapshot_at
           FROM mcp_capability_snapshots s
           JOIN server_configs sc ON sc.mcp_server_id = s.mcp_server_id
           JOIN resource_group_server_configs b ON b.server_config_id = sc.id
          WHERE s.tenant_id = $1
            AND b.resource_group_id = $2
            AND s.is_change
            AND s.snapshot_at BETWEEN $3::timestamptz - MAKE_INTERVAL(hours => $4)
                                  AND $3::timestamptz + MAKE_INTERVAL(hours => $4)
          ORDER BY s.snapshot_at DESC
          LIMIT 1",
    )
    .bind(tenant_id)
    .bind(ai_system_id)
    .bind(change_point)
    .bind(CAUSE_WINDOW_HOURS as i32)
    .fetch_optional(db)
    .await?;

    Ok(row.map(|(mcp_server_id, snapshot_at)| DependencyChange {
        mcp_server_id,
        // The snapshot diff itself is computed by the control plane's
        // capability-diff service; here we only need to know that the
        // contract moved at this time.
        tools_added: Vec::new(),
        tools_removed: Vec::new(),
        schemas_changed: Vec::new(),
        changed_at: snapshot_at,
    }))
}

/// Did the dominant instruction fingerprint move across the change point?
///
/// Dominant before vs dominant after, over the runs in the cause window on
/// each side. Both sides need a fingerprint for this to be a *change* rather
/// than "we started recording it".
pub async fn find_prompt_change(
    db: &PgPool,
    tenant_id: &str,
    ai_system_id: &str,
    change_point: DateTime<Utc>,
) -> Result<Option<PromptChange>, sqlx::Error> {
    let Some(sides) = dominant_before_after(
        db,
        tenant_id,
        ai_system_id,
        change_point,
        "prompt_fingerprint",
    )
    .await?
    else {
        return Ok(None);
    };
    let (before, after, first_after) = sides;
    if before == after {
        return Ok(None);
    }
    Ok(Some(PromptChange {
        from_fingerprint: before,
        to_fingerprint: after,
        changed_at: first_after,
    }))
}

/// Did the retrieval collection set move across the change point?
pub async fn find_corpus_change(
    db: &PgPool,
    tenant_id: &str,
    ai_system_id: &str,
    change_point: DateTime<Utc>,
) -> Result<Option<CorpusChange>, sqlx::Error> {
    let Some((before, after, first_after)) = dominant_before_after(
        db,
        tenant_id,
        ai_system_id,
        change_point,
        "rag_collection_ids",
    )
    .await?
    else {
        return Ok(None);
    };
    if before == after {
        return Ok(None);
    }
    let split = |s: &str| -> Vec<String> {
        s.split(',')
            .map(str::trim)
            .filter(|x| !x.is_empty())
            .map(String::from)
            .collect()
    };
    Ok(Some(CorpusChange {
        from_collections: split(&before),
        to_collections: split(&after),
        changed_at: first_after,
    }))
}

/// The most common non-null value of `column` on the run rows in the cause
/// window before the change point, the same after it, and when the first
/// run carrying the after-value started. `column` is one of two pinned
/// identifiers, never caller input.
async fn dominant_before_after(
    db: &PgPool,
    tenant_id: &str,
    ai_system_id: &str,
    change_point: DateTime<Utc>,
    column: &'static str,
) -> Result<Option<(String, String, DateTime<Utc>)>, sqlx::Error> {
    debug_assert!(column == "prompt_fingerprint" || column == "rag_collection_ids");
    let sql = format!(
        "WITH before AS (
             SELECT {col} AS v FROM ai_system_runs
              WHERE tenant_id = $1 AND ai_system_id = $2 AND NOT is_replay AND {col} IS NOT NULL
                AND started_at >= $3::timestamptz - MAKE_INTERVAL(hours => $4) AND started_at < $3::timestamptz
              GROUP BY {col} ORDER BY COUNT(*) DESC LIMIT 1),
             after AS (
             SELECT {col} AS v FROM ai_system_runs
              WHERE tenant_id = $1 AND ai_system_id = $2 AND NOT is_replay AND {col} IS NOT NULL
                AND started_at >= $3::timestamptz AND started_at < $3::timestamptz + MAKE_INTERVAL(hours => $4)
              GROUP BY {col} ORDER BY COUNT(*) DESC LIMIT 1)
         SELECT b.v, a.v,
                (SELECT MIN(started_at) FROM ai_system_runs r
                  WHERE r.tenant_id = $1 AND r.ai_system_id = $2 AND r.{col} = a.v
                    AND r.started_at >= $3::timestamptz)
           FROM before b, after a",
        col = column
    );
    let row: Option<(String, String, Option<DateTime<Utc>>)> = sqlx::query_as(&sql)
        .bind(tenant_id)
        .bind(ai_system_id)
        .bind(change_point)
        .bind(CAUSE_WINDOW_HOURS as i32)
        .fetch_optional(db)
        .await?;
    Ok(row.map(|(b, a, at)| (b, a, at.unwrap_or(change_point))))
}

// ---------------------------------------------------------------------------
// Facts only the segment basis has
// ---------------------------------------------------------------------------

/// Did the contract a service serves under change near the change point?
///
/// Every segment records the served system's ACTIVE contract at
/// finalisation (`contract_id`), so the transition is read straight from
/// the ledger: the served call nearest the change point whose contract
/// differs from the call before it. A promotion nobody's traffic crossed is
/// never blamed.
///
/// A transition, not "dominant before vs dominant after": the change point
/// is where the detector *declared* the change, a few calls after it began,
/// so a ±window vote either side of it can see the new contract on both
/// sides and conclude nothing moved.
pub async fn find_segment_contract_change(
    db: &PgPool,
    tenant_id: &str,
    ai_system_id: &str,
    change_point: DateTime<Utc>,
) -> Result<Option<ContractChange>, sqlx::Error> {
    let row: Option<(String, String, DateTime<Utc>)> = sqlx::query_as(
        "SELECT prev, contract_id, first_action_at FROM (
             SELECT contract_id, first_action_at,
                    LAG(contract_id) OVER (ORDER BY first_action_at, id) AS prev
               FROM ai_system_run_segments
              WHERE tenant_id = $1 AND segment_system_id = $2 AND contract_id IS NOT NULL
                AND first_action_at >= $3::timestamptz - MAKE_INTERVAL(days => $5)
                AND first_action_at < $3::timestamptz + MAKE_INTERVAL(hours => $4)
         ) t
          WHERE prev IS NOT NULL AND prev <> contract_id
            AND first_action_at >= $3::timestamptz - MAKE_INTERVAL(hours => $4)
          ORDER BY ABS(EXTRACT(EPOCH FROM (first_action_at - $3::timestamptz))) ASC
          LIMIT 1",
    )
    .bind(tenant_id)
    .bind(ai_system_id)
    .bind(change_point)
    .bind(CAUSE_WINDOW_HOURS as i32)
    .bind(super::BASELINE_WINDOW_DAYS as i32)
    .fetch_optional(db)
    .await?;
    Ok(row.map(|(from, to, at)| ContractChange {
        from_contract_id: from,
        to_contract_id: to,
        changed_at: at,
    }))
}

/// The release transition nearest the change point.
///
/// Run basis: the system's own runs, per agent identity, in start order —
/// the run whose `(name, version, build)` differs from the same agent's
/// previous run. Segment basis (a shared service): the calls it served, per
/// release name. Like the contract rung, a transition rather than a
/// before/after vote, so a change point declared a few calls late still
/// sees it.
///
/// With no transition, the run basis looks for the undeclared change: a run
/// whose release is unchanged but whose implementation fingerprint was
/// never seen on that release before, after the release had runs on another
/// fingerprint.
pub async fn find_release_change(
    db: &PgPool,
    tenant_id: &str,
    ai_system_id: &str,
    change_point: DateTime<Utc>,
    segments: bool,
) -> Result<Option<ReleaseChange>, sqlx::Error> {
    type Row = (
        Option<String>,
        Option<String>,
        Option<String>,
        Option<String>,
        Option<String>,
        Option<String>,
        Option<String>,
        Option<String>,
        Option<String>,
        DateTime<Utc>,
        Option<String>,
    );
    // (agent, prev name, prev version, prev build, prev fingerprint,
    //  name, version, build, fingerprint, at, trust)
    let transition_sql = if segments {
        "SELECT NULL::varchar, pn, pv, pb, NULL::varchar, n, v, b, NULL::varchar, at, t FROM (
             SELECT agent_impl_name AS n, agent_impl_version AS v, agent_impl_build AS b,
                    agent_impl_trust AS t, first_action_at AS at,
                    LAG(agent_impl_name) OVER w AS pn, LAG(agent_impl_version) OVER w AS pv,
                    LAG(agent_impl_build) OVER w AS pb,
                    LAG(first_action_at) OVER w AS pat
               FROM ai_system_run_segments
              WHERE tenant_id = $1 AND segment_system_id = $2
                AND (agent_impl_name IS NOT NULL OR agent_impl_version IS NOT NULL)
                AND first_action_at >= $3::timestamptz - MAKE_INTERVAL(days => $5)
                AND first_action_at < $3::timestamptz + MAKE_INTERVAL(hours => $4)
             WINDOW w AS (PARTITION BY agent_impl_name ORDER BY first_action_at, id)
         ) t
          WHERE pat IS NOT NULL
            AND (pv IS DISTINCT FROM v OR pb IS DISTINCT FROM b)
            AND at >= $3::timestamptz - MAKE_INTERVAL(hours => $4)
          ORDER BY ABS(EXTRACT(EPOCH FROM (at - $3::timestamptz))) ASC
          LIMIT 1"
    } else {
        "SELECT agent_id, pn, pv, pb, pf, n, v, b, f, at, t FROM (
             SELECT agent_id, agent_impl_name AS n, agent_impl_version AS v,
                    agent_impl_build AS b, implementation_fingerprint AS f,
                    agent_impl_trust AS t, started_at AS at,
                    LAG(agent_impl_name) OVER w AS pn, LAG(agent_impl_version) OVER w AS pv,
                    LAG(agent_impl_build) OVER w AS pb,
                    LAG(implementation_fingerprint) OVER w AS pf,
                    LAG(started_at) OVER w AS pat
               FROM ai_system_runs
              WHERE tenant_id = $1 AND ai_system_id = $2 AND NOT is_replay
                AND agent_id IS NOT NULL
                AND (agent_impl_name IS NOT NULL OR agent_impl_version IS NOT NULL)
                AND started_at >= $3::timestamptz - MAKE_INTERVAL(days => $5)
                AND started_at < $3::timestamptz + MAKE_INTERVAL(hours => $4)
             WINDOW w AS (PARTITION BY agent_id ORDER BY started_at, id)
         ) t
          WHERE pat IS NOT NULL
            AND (pn IS DISTINCT FROM n OR pv IS DISTINCT FROM v OR pb IS DISTINCT FROM b)
            AND at >= $3::timestamptz - MAKE_INTERVAL(hours => $4)
          ORDER BY ABS(EXTRACT(EPOCH FROM (at - $3::timestamptz))) ASC
          LIMIT 1"
    };
    let row: Option<Row> = sqlx::query_as(transition_sql)
        .bind(tenant_id)
        .bind(ai_system_id)
        .bind(change_point)
        .bind(CAUSE_WINDOW_HOURS as i32)
        .bind(super::BASELINE_WINDOW_DAYS as i32)
        .fetch_optional(db)
        .await?;
    let to_change = |r: Row, version_changed: bool| ReleaseChange {
        agent_id: r.0,
        from: ReleaseRef {
            name: r.1,
            version: r.2,
            build: r.3,
        },
        from_fingerprint: r.4,
        to: ReleaseRef {
            name: r.5,
            version: r.6,
            build: r.7,
        },
        to_fingerprint: r.8,
        changed_at: r.9,
        version_changed,
        trust: r.10,
    };
    if let Some(r) = row {
        return Ok(Some(to_change(r, true)));
    }
    if segments {
        return Ok(None);
    }

    // The undeclared change: same release, a fingerprint that release had
    // never shown, after it had shown another.
    let row: Option<Row> = sqlx::query_as(
        "SELECT agent_id, n, v, b, pf, n, v, b, f, at, t FROM (
             SELECT agent_id, agent_impl_name AS n, agent_impl_version AS v,
                    agent_impl_build AS b, implementation_fingerprint AS f,
                    agent_impl_trust AS t, started_at AS at,
                    LAG(implementation_fingerprint) OVER w AS pf,
                    COUNT(*) OVER w_before AS runs_before,
                    COUNT(*) OVER w_same_fp AS same_fp_before
               FROM ai_system_runs
              WHERE tenant_id = $1 AND ai_system_id = $2 AND NOT is_replay
                AND agent_id IS NOT NULL AND implementation_fingerprint IS NOT NULL
                AND (agent_impl_name IS NOT NULL OR agent_impl_version IS NOT NULL)
                AND started_at >= $3::timestamptz - MAKE_INTERVAL(days => $5)
                AND started_at < $3::timestamptz + MAKE_INTERVAL(hours => $4)
             WINDOW w AS (PARTITION BY agent_id, agent_impl_name, agent_impl_version,
                                       COALESCE(agent_impl_build, '')
                          ORDER BY started_at, id),
                    w_before AS (w ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),
                    w_same_fp AS (PARTITION BY agent_id, agent_impl_name, agent_impl_version,
                                               COALESCE(agent_impl_build, ''),
                                               implementation_fingerprint
                                  ORDER BY started_at, id
                                  ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
         ) t
          WHERE runs_before > 0 AND same_fp_before = 0
            AND at >= $3::timestamptz - MAKE_INTERVAL(hours => $4)
          ORDER BY ABS(EXTRACT(EPOCH FROM (at - $3::timestamptz))) ASC
          LIMIT 1",
    )
    .bind(tenant_id)
    .bind(ai_system_id)
    .bind(change_point)
    .bind(CAUSE_WINDOW_HOURS as i32)
    .bind(super::BASELINE_WINDOW_DAYS as i32)
    .fetch_optional(db)
    .await?;
    Ok(row.map(|r| to_change(r, false)))
}

/// Was a declared dependency edge touching this service created or edited
/// near the change point? For a per-caller baseline only that caller's edge
/// to the service counts; for the aggregate, any caller's edge to it and
/// any edge the service itself declares.
pub async fn find_segment_composition_change(
    db: &PgPool,
    tenant_id: &str,
    ai_system_id: &str,
    caller_system_id: Option<&str>,
    change_point: DateTime<Utc>,
) -> Result<Option<CompositionChange>, sqlx::Error> {
    let row: Option<(String, String, Option<String>, String, bool, DateTime<Utc>)> =
        sqlx::query_as(
            "SELECT d.ai_system_id,
                    COALESCE(rg.display_name, rg.name, d.ai_system_id),
                    d.depends_on_system_id, d.criticality,
                    d.updated_at > d.declared_at + INTERVAL '1 second' AS edited,
                    GREATEST(d.declared_at, d.updated_at) AS changed_at
               FROM ai_system_dependencies d
               LEFT JOIN resource_groups rg ON rg.id = d.ai_system_id
              WHERE d.tenant_id = $1
                AND ( ($3::text IS NULL AND (d.depends_on_system_id = $2 OR d.ai_system_id = $2))
                   OR ($3::text IS NOT NULL AND d.depends_on_system_id = $2 AND d.ai_system_id = $3) )
                AND GREATEST(d.declared_at, d.updated_at)
                    BETWEEN $4::timestamptz - MAKE_INTERVAL(hours => $5)
                        AND $4::timestamptz + MAKE_INTERVAL(hours => $5)
              ORDER BY changed_at DESC
              LIMIT 1",
        )
        .bind(tenant_id)
        .bind(ai_system_id)
        .bind(caller_system_id)
        .bind(change_point)
        .bind(CAUSE_WINDOW_HOURS as i32)
        .fetch_optional(db)
        .await?;
    Ok(row.map(
        |(declarer, declarer_name, target, criticality, edited, changed_at)| {
            let verb = if edited { "changed" } else { "declared" };
            let description = if declarer == ai_system_id {
                format!(
                    "this system {verb} its {criticality} dependency on '{}'",
                    target.unwrap_or_else(|| "an external agent".into())
                )
            } else {
                format!("'{declarer_name}' {verb} its {criticality} dependency on this system")
            };
            CompositionChange {
                description,
                changed_at,
            }
        },
    ))
}

/// A caller whose first call to this service in the baseline window landed
/// near the change point — a new population, not a changed service. The
/// caller is the IMMEDIATE calling AI System (`caller_system_id`), the one
/// definition the per-caller baselines and accepted-caller enforcement use.
pub async fn find_new_caller(
    db: &PgPool,
    tenant_id: &str,
    ai_system_id: &str,
    window_start: DateTime<Utc>,
    change_point: DateTime<Utc>,
) -> Result<Option<String>, sqlx::Error> {
    let row: Option<(String,)> = sqlx::query_as(
        "SELECT caller_system_id
           FROM ai_system_run_segments
          WHERE tenant_id = $1 AND segment_system_id = $2
            AND caller_system_id IS NOT NULL
            AND first_action_at >= $3
          GROUP BY caller_system_id
         HAVING MIN(first_action_at) BETWEEN $4::timestamptz - MAKE_INTERVAL(hours => $5)
                                         AND $4::timestamptz + MAKE_INTERVAL(hours => $5)
          ORDER BY MIN(first_action_at) ASC
          LIMIT 1",
    )
    .bind(tenant_id)
    .bind(ai_system_id)
    .bind(window_start)
    .bind(change_point)
    .bind(CAUSE_WINDOW_HOURS as i32)
    .fetch_optional(db)
    .await?;
    Ok(row.map(|(c,)| c))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn at(hour: u32) -> DateTime<Utc> {
        DateTime::parse_from_rfc3339(&format!("2026-07-14T{hour:02}:00:00Z"))
            .unwrap()
            .with_timezone(&Utc)
    }

    fn model_change(hour: u32) -> ModelChange {
        ModelChange {
            model_id: "gpt-x".into(),
            from_version: Some("2026-05-01".into()),
            to_version: Some("2026-07-10".into()),
            changed_at: at(hour),
            retirement: None,
        }
    }

    // -- ordering ----------------------------------------------------------

    #[test]
    fn model_change_wins_when_it_lines_up() {
        let facts = CauseFacts {
            model_change: Some(model_change(13)),
            input_drifted: true,
            ..Default::default()
        };
        let a = attribute(&facts, Some(at(14)));
        assert_eq!(a.cause, kind::MODEL_CHANGE);
        assert!(a.summary.contains("model serving this system changed"));
    }

    #[test]
    fn a_model_change_outside_the_window_does_not_explain_it() {
        // Changed a week earlier; the drift started today. Blaming it
        // would be a confident wrong answer, which is worse than none.
        let facts = CauseFacts {
            model_change: Some(ModelChange {
                changed_at: at(2),
                ..model_change(2)
            }),
            ..Default::default()
        };
        let a = attribute(&facts, Some(at(20)));
        assert_eq!(a.cause, kind::UNEXPLAINED);
    }

    #[test]
    fn dependency_change_beats_degradation() {
        let facts = CauseFacts {
            dependency_change: Some(DependencyChange {
                mcp_server_id: "srv".into(),
                tools_added: vec!["x".into()],
                tools_removed: vec![],
                schemas_changed: vec![],
                changed_at: at(14),
            }),
            dependency_degraded: Some(DependencyDegraded {
                tool_or_type: "timeout".into(),
                baseline_error_rate: 0.01,
                observed_error_rate: 0.4,
            }),
            ..Default::default()
        };
        assert_eq!(
            attribute(&facts, Some(at(14))).cause,
            kind::DEPENDENCY_CHANGE
        );
    }

    #[test]
    fn input_drift_ranks_below_a_real_dependency_change() {
        let facts = CauseFacts {
            dependency_change: Some(DependencyChange {
                mcp_server_id: "srv".into(),
                tools_added: vec![],
                tools_removed: vec!["gone".into()],
                schemas_changed: vec![],
                changed_at: at(14),
            }),
            input_drifted: true,
            ..Default::default()
        };
        assert_eq!(
            attribute(&facts, Some(at(14))).cause,
            kind::DEPENDENCY_CHANGE
        );
    }

    #[test]
    fn input_drift_is_reported_when_nothing_underneath_moved() {
        // The disambiguator: the system did not change, the questions did.
        let facts = CauseFacts {
            input_drifted: true,
            ..Default::default()
        };
        let a = attribute(&facts, Some(at(14)));
        assert_eq!(a.cause, kind::INPUT_DRIFT);
        assert!(
            a.summary
                .contains("rather than a change in the system itself")
        );
    }

    #[test]
    fn traffic_mix_is_the_last_real_answer() {
        let facts = CauseFacts {
            new_caller: Some("new-service".into()),
            ..Default::default()
        };
        assert_eq!(attribute(&facts, Some(at(14))).cause, kind::TRAFFIC_MIX);
    }

    // -- retirement --------------------------------------------------------

    #[test]
    fn a_scheduled_retirement_is_the_strongest_evidence() {
        let mut mc = model_change(14);
        mc.retirement = Some(RetirementContext {
            retire_date: "2026-07-14".into(),
            replacement_model_name: Some("gpt-y".into()),
            notes: Some("different output format".into()),
        });
        let facts = CauseFacts {
            model_change: Some(mc),
            ..Default::default()
        };
        let a = attribute(&facts, Some(at(14)));

        assert_eq!(a.cause, kind::MODEL_CHANGE);
        assert!(
            a.confidence > 0.9,
            "announced in advance and names its successor"
        );
        assert!(a.summary.contains("retirement date"));
        assert!(a.summary.contains("gpt-y"));
    }

    // -- saying "I don't know" ---------------------------------------------

    #[test]
    fn unexplained_is_a_real_answer_with_somewhere_to_start() {
        let facts = CauseFacts {
            correlated_metrics: vec!["tool_error_rate".into(), "duration_ms".into()],
            ..Default::default()
        };
        let a = attribute(&facts, Some(at(14)));
        assert_eq!(a.cause, kind::UNEXPLAINED);
        assert_eq!(a.confidence, 0.0);
        assert!(!a.is_explained());
        assert_eq!(a.evidence["correlated_metrics"][0], "tool_error_rate");
    }

    #[test]
    fn no_change_point_does_not_block_attribution() {
        // Some detectors (categorical shifts) have no single change point.
        // The ladder should still name a cause rather than shrug.
        let facts = CauseFacts {
            model_change: Some(model_change(14)),
            ..Default::default()
        };
        assert_eq!(attribute(&facts, None).cause, kind::MODEL_CHANGE);
    }

    // -- window ------------------------------------------------------------

    #[test]
    fn cause_window_is_symmetric() {
        let cp = at(14);
        assert!(within_cause_window(at(13), cp), "just before");
        assert!(
            within_cause_window(at(15), cp),
            "just after — clock skew is real"
        );
        assert!(!within_cause_window(at(10), cp));
    }

    #[test]
    fn ladder_order_is_pinned() {
        assert_eq!(
            kind::LADDER,
            [
                "incident_window",
                "contract_change",
                "agent_release",
                "model_change",
                "prompt_change",
                "dependency_change",
                "dependency_degraded",
                "composition_change",
                "corpus_change",
                "input_drift",
                "traffic_mix",
                "unexplained",
            ]
        );
    }

    #[test]
    fn every_rung_the_ladder_can_return_is_in_the_list() {
        let cases = [
            CauseFacts {
                model_change: Some(model_change(14)),
                ..Default::default()
            },
            CauseFacts {
                dependency_degraded: Some(DependencyDegraded {
                    tool_or_type: "t".into(),
                    baseline_error_rate: 0.0,
                    observed_error_rate: 1.0,
                }),
                ..Default::default()
            },
            CauseFacts {
                composition_change: Some(CompositionChange {
                    description: "model added".into(),
                    changed_at: at(14),
                }),
                ..Default::default()
            },
            CauseFacts {
                input_drifted: true,
                ..Default::default()
            },
            CauseFacts {
                new_caller: Some("x".into()),
                ..Default::default()
            },
            CauseFacts::default(),
        ];
        for facts in cases {
            let a = attribute(&facts, Some(at(14)));
            assert!(kind::LADDER.contains(&a.cause), "{} not in ladder", a.cause);
            assert!(a.confidence >= 0.0 && a.confidence <= 1.0);
            assert!(!a.summary.is_empty(), "every rung must explain itself");
        }
    }

    // The two new rungs sit where the ladder says they do.
    #[test]
    fn a_prompt_change_outranks_a_dependency_change_but_not_a_model_change() {
        let pc = PromptChange {
            from_fingerprint: "aaaa".into(),
            to_fingerprint: "bbbb".into(),
            changed_at: at(10),
        };
        let facts = CauseFacts {
            prompt_change: Some(pc.clone()),
            model_change: Some(model_change(10)),
            ..Default::default()
        };
        assert_eq!(attribute(&facts, Some(at(10))).cause, kind::MODEL_CHANGE);
        let facts = CauseFacts {
            prompt_change: Some(pc),
            dependency_change: Some(DependencyChange {
                mcp_server_id: "mcp".into(),
                tools_added: vec![],
                tools_removed: vec![],
                schemas_changed: vec![],
                changed_at: at(10),
            }),
            ..Default::default()
        };
        let a = attribute(&facts, Some(at(10)));
        assert_eq!(a.cause, kind::PROMPT_CHANGE);
        assert!(a.summary.contains("instructions"));
    }

    #[test]
    fn a_corpus_change_outranks_input_drift() {
        let facts = CauseFacts {
            corpus_change: Some(CorpusChange {
                from_collections: vec!["kb-old".into()],
                to_collections: vec!["kb-new".into()],
                changed_at: at(10),
            }),
            input_drifted: true,
            ..Default::default()
        };
        let a = attribute(&facts, Some(at(10)));
        assert_eq!(a.cause, kind::CORPUS_CHANGE);
        assert!(a.summary.contains("kb-old") && a.summary.contains("kb-new"));
    }

    #[test]
    fn the_ladder_lists_every_kind_once_and_ends_unexplained() {
        assert_eq!(kind::LADDER.len(), 12);
        assert_eq!(*kind::LADDER.last().unwrap(), kind::UNEXPLAINED);
        assert!(
            kind::LADDER.contains(&kind::PROMPT_CHANGE)
                && kind::LADDER.contains(&kind::CORPUS_CHANGE)
        );
    }

    // -- the contract rung ---------------------------------------

    fn contract_change(hour: u32) -> ContractChange {
        ContractChange {
            from_contract_id: "ctr-1".into(),
            to_contract_id: "ctr-2".into(),
            changed_at: at(hour),
        }
    }

    #[test]
    fn a_contract_promotion_outranks_a_model_change() {
        let facts = CauseFacts {
            contract_change: Some(contract_change(14)),
            model_change: Some(model_change(14)),
            ..Default::default()
        };
        let a = attribute(&facts, Some(at(14)));
        assert_eq!(a.cause, kind::CONTRACT_CHANGE);
        assert_eq!(a.evidence["to_contract_id"], "ctr-2");
        assert!(a.summary.contains("new contract"));
    }

    #[test]
    fn a_contract_promotion_outside_the_window_explains_nothing() {
        let facts = CauseFacts {
            contract_change: Some(contract_change(2)),
            ..Default::default()
        };
        assert_eq!(attribute(&facts, Some(at(20))).cause, kind::UNEXPLAINED);
    }

    #[test]
    fn the_contract_rung_is_reachable() {
        let a = attribute(
            &CauseFacts {
                contract_change: Some(contract_change(14)),
                ..Default::default()
            },
            Some(at(14)),
        );
        assert!(kind::LADDER.contains(&a.cause));
        assert_eq!(
            kind::LADDER[1],
            a.cause,
            "contract is the first rung after a declared incident"
        );
    }

    // -- the incident-window rung ---------------------------------

    fn window(from: u32, to: Option<u32>, tenant_wide: bool) -> IncidentWindowRef {
        IncidentWindowRef {
            id: "iw-1".into(),
            tenant_wide,
            starts_at: at(from),
            ends_at: to.map(at),
            cause_category: "provider_outage".into(),
            reason: Some("credits exhausted".into()),
        }
    }

    #[test]
    fn a_change_point_at_the_edge_of_a_declared_incident_names_it() {
        let facts = CauseFacts {
            incident_window: Some(window(8, Some(12), true)),
            contract_change: Some(contract_change(13)),
            model_change: Some(model_change(13)),
            ..Default::default()
        };
        let a = attribute(&facts, Some(at(13)));
        assert_eq!(
            a.cause,
            kind::INCIDENT_WINDOW,
            "a declared incident outranks every inferred cause"
        );
        assert_eq!(a.evidence["incident_window_id"], "iw-1");
        assert_eq!(a.evidence["tenant_wide"], true);
        assert_eq!(a.evidence["cause_category"], "provider_outage");
        assert!(a.summary.contains("provider_outage, tenant-wide"));
        assert!(a.summary.contains("credits exhausted"));
        assert_eq!(kind::LADDER[0], a.cause);
    }

    #[test]
    fn inside_a_window_and_at_either_edge_it_overlaps_but_not_beyond() {
        let w = window(8, Some(12), false);
        assert!(w.overlaps(at(10)), "inside");
        assert!(w.overlaps(at(7)), "onset edge");
        assert!(w.overlaps(at(14)), "tail edge");
        assert!(!w.overlaps(at(5)));
        assert!(!w.overlaps(at(15)));
        assert!(window(8, None, false).overlaps(at(23)), "an open window");
        let facts = CauseFacts {
            incident_window: Some(w),
            ..Default::default()
        };
        assert_eq!(attribute(&facts, Some(at(20))).cause, kind::UNEXPLAINED);
    }

    #[test]
    fn the_incident_rung_is_pinned_with_the_control_plane() {
        // Mirrored in the CP's attributed-cause vocabulary; a rename is a
        // two-repo change.
        assert_eq!(kind::INCIDENT_WINDOW, "incident_window");
        assert!(
            kind::INCIDENT_WINDOW.len() <= 32,
            "attributed_cause is varchar(32)"
        );
    }

    // -- the agent-release rung ----------------------------------

    fn release_change(hour: u32, version_changed: bool) -> ReleaseChange {
        ReleaseChange {
            agent_id: Some("agent-1".into()),
            from: ReleaseRef {
                name: Some("screening-agent".into()),
                version: Some("1.4.2".into()),
                build: None,
            },
            to: ReleaseRef {
                name: Some("screening-agent".into()),
                version: Some(if version_changed { "1.5.0" } else { "1.4.2" }.into()),
                build: Some("sha256:beef".into()),
            },
            changed_at: at(hour),
            version_changed,
            from_fingerprint: Some("sha256:aa".into()),
            to_fingerprint: Some("sha256:bb".into()),
            trust: Some("declared".into()),
        }
    }

    #[test]
    fn a_release_transition_names_both_releases_and_outranks_a_model_change() {
        let facts = CauseFacts {
            release_change: Some(release_change(14, true)),
            model_change: Some(model_change(14)),
            ..Default::default()
        };
        let a = attribute(&facts, Some(at(14)));
        assert_eq!(a.cause, kind::AGENT_RELEASE);
        assert!(a.summary.contains("screening-agent@1.4.2"));
        assert!(a.summary.contains("screening-agent@1.5.0 (sha256:beef)"));
        assert_eq!(a.evidence["from"]["version"], "1.4.2");
        assert_eq!(a.evidence["to"]["version"], "1.5.0");
        assert_eq!(a.evidence["version_change"], true);
    }

    #[test]
    fn a_contract_promotion_still_outranks_a_release() {
        let facts = CauseFacts {
            contract_change: Some(contract_change(14)),
            release_change: Some(release_change(14, true)),
            ..Default::default()
        };
        assert_eq!(attribute(&facts, Some(at(14))).cause, kind::CONTRACT_CHANGE);
    }

    #[test]
    fn an_undeclared_change_says_no_version_change() {
        let a = attribute(
            &CauseFacts {
                release_change: Some(release_change(14, false)),
                ..Default::default()
            },
            Some(at(14)),
        );
        assert_eq!(a.cause, kind::AGENT_RELEASE);
        assert!(a.summary.starts_with("No version change"));
        assert_eq!(a.evidence["version_change"], false);
        assert_eq!(a.evidence["to_fingerprint"], "sha256:bb");
    }

    #[test]
    fn a_release_outside_the_window_explains_nothing() {
        let facts = CauseFacts {
            release_change: Some(release_change(2, true)),
            ..Default::default()
        };
        assert_eq!(attribute(&facts, Some(at(20))).cause, kind::UNEXPLAINED);
    }
}
