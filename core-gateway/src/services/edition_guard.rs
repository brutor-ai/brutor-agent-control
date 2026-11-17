// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # The Community Edition does not start on a database the Enterprise
//! # Edition has used
//!
//! One schema serves both editions. A Community Edition that started on
//! Enterprise data would ignore it: policy bundles, a second tenant,
//! knowledge-base bindings, cache configuration. Governance controls would be
//! switched off without a sign. So, at start, before anything is served and
//! before a background task runs:
//!
//! * an **Enterprise** composition does nothing here: no read, no write, and
//!   it never refuses (the control plane writes the marker; the core never
//!   does, in either edition);
//! * a **Community** composition reads the edition marker and looks for
//!   Enterprise data directly ([`direct_checks`]), and refuses to start if
//!   anything is found.
//!
//! **It fails closed.** A marker that cannot be read, a table that is not
//! there, a statement that fails or does not answer in time, an answer that
//! is no count: each is a refusal. The Community Edition starts only when
//! every check was made and found nothing. There is no retry and no override.
//!
//! The refusal lists kinds and counts, never a row's contents, and of an
//! error only a class name ([`error_class`]): a driver error can quote a
//! statement and its parameters.
//!
//! The control plane makes the same check with the same statements,
//! thresholds, order and wording; `tests/edition_guard_contract.rs` holds this module
//! equal to the written contract both follow. Which edition a process is comes from its composition
//! (`services::edition`).

use std::fmt;
use std::time::Duration;

use chrono::{DateTime, Utc};
use sqlx::{PgPool, Row};

use crate::services::AppState;
use crate::services::edition::Edition;

/// An Enterprise-only table is counted up to this many rows.
pub const ROW_CAP: i64 = 1000;

/// How long one statement may take before it counts as "could not be
/// decided". The statements read at most [`ROW_CAP`] rows of a table, or
/// count a small one.
pub const STATEMENT_TIMEOUT: Duration = Duration::from_secs(30);

/// How long past [`STATEMENT_TIMEOUT`] the client waits for the server to
/// cancel a statement before it gives the check up itself.
const CLIENT_GRACE: Duration = Duration::from_secs(5);

pub const MARKER_TABLE: &str = "edition_marker";

/// Contract section 4.1. A row is a finding; no row is not.
pub const MARKER_READ_SQL: &str = "SELECT enterprise_first_started_at, enterprise_last_started_at, enterprise_first_version, enterprise_last_version FROM edition_marker WHERE id = 1";

/// The tables only the Enterprise Edition writes, with their feature, in the
/// order of `control_plane.enterprise_only_tables` of `editions.yaml`.
pub const ENTERPRISE_ONLY_TABLES: &[(&str, &str)] = &[
    ("docker_images", "deployment"),
    ("docker_deployments", "deployment"),
    ("ecs_deployments", "deployment"),
    ("kubernetes_deployments", "deployment"),
    ("kubernetes_clusters", "deployment"),
    ("llm_runner_machines", "deployment"),
    ("resource_group_deployment_machines", "deployment"),
    ("usage_import_checkpoints", "traffic_import"),
    ("usage_import_run_logs", "traffic_import"),
    ("semantic_cache_configs", "response_cache"),
    ("resource_group_cache_configs", "response_cache"),
    ("semantic_cache_tool_rules", "response_cache"),
    ("router_cache_configs", "response_cache"),
    ("prompt_cache_policies", "prompt_cache"),
    ("resource_group_prompt_cache_policies", "prompt_cache"),
    ("mcp_registry_configs", "mcp_registry"),
    ("kb_collections", "knowledge_base"),
    ("kb_documents", "knowledge_base"),
    ("kb_chunks", "knowledge_base"),
    ("kb_connectors", "knowledge_base"),
    ("resource_group_kb_collections", "knowledge_base"),
    ("resource_group_kb_connectors", "knowledge_base"),
    ("kb_connector_identity_mappings", "knowledge_base"),
    ("policy_bundles", "policy_as_code"),
    ("policy_bundle_events", "policy_as_code"),
    ("policy_templates", "policy_as_code"),
    ("batch_jobs", "batch_jobs"),
    ("batch_requests", "batch_jobs"),
    ("system_admins", "multi_tenant"),
    ("system_admin_sessions", "multi_tenant"),
    ("system_admin_audit_logs", "multi_tenant"),
    ("platform_settings", "multi_tenant"),
    ("collector_keys", "discovery"),
    ("discovered_assets", "discovery"),
    ("discovery_evidence", "discovery"),
    ("discovery_events", "discovery"),
    ("observed_agent_events", "discovery"),
];

/// The keys of `tenants.features` that switch an Enterprise feature on, in
/// the order of `control_plane.tenant_feature_flags.enterprise`.
pub const ENTERPRISE_FEATURE_FLAGS: &[(&str, &str)] = &[
    ("mcp_server_deployment", "deployment"),
    ("ai_model_deployment", "deployment"),
    ("model_traffic_import", "traffic_import"),
    ("mcp_registry", "mcp_registry"),
    ("knowledge_base", "knowledge_base"),
];

/// What a check found when the statement's answer cannot be read as a count.
pub const UNEXPECTED_ANSWER: &str = "UnexpectedAnswer";
/// A statement that did not answer within the timeout.
pub const TIMEOUT: &str = "Timeout";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    Marker,
    Table,
    Tenants,
    FeatureFlag,
}

impl Kind {
    /// The kind's name in the contract.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Marker => "marker",
            Self::Table => "table",
            Self::Tenants => "tenants",
            Self::FeatureFlag => "feature_flag",
        }
    }
}

/// One direct check: a statement that answers one integer, and how many is
/// too many.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DirectCheck {
    pub kind: Kind,
    /// The table, or the flag.
    pub subject: &'static str,
    pub feature: Option<&'static str>,
    /// A finding when the answer is greater than this.
    pub more_than: i64,
    pub sql: String,
}

/// Every direct check, in the order they run and are reported (contract
/// section 4.2). The statements are built from the constants of this module
/// only: nothing of a request or of the database reaches them.
pub fn direct_checks() -> Vec<DirectCheck> {
    let mut checks: Vec<DirectCheck> = ENTERPRISE_ONLY_TABLES
        .iter()
        .map(|(table, feature)| DirectCheck {
            kind: Kind::Table,
            subject: table,
            feature: Some(feature),
            more_than: 0,
            sql: format!("SELECT count(*) FROM (SELECT 1 FROM {table} LIMIT {ROW_CAP}) AS capped"),
        })
        .collect();
    checks.push(DirectCheck {
        kind: Kind::Tenants,
        subject: "tenants",
        feature: None,
        more_than: 1,
        sql: "SELECT count(*) FROM tenants".to_string(),
    });
    checks.extend(
        ENTERPRISE_FEATURE_FLAGS
            .iter()
            .map(|(flag, feature)| DirectCheck {
                kind: Kind::FeatureFlag,
                subject: flag,
                feature: Some(feature),
                more_than: 0,
                sql: format!("SELECT count(*) FROM tenants WHERE (features ->> '{flag}') = 'true'"),
            }),
    );
    checks
}

/// One kind of Enterprise data that was found: what, and how much.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Finding {
    pub kind: Kind,
    pub subject: String,
    pub feature: Option<&'static str>,
    pub count: i64,
    /// The marker's line (when and by which version), for [`Kind::Marker`].
    pub detail: Option<String>,
}

impl Finding {
    /// The finding's line in the refusal, without the `  - `.
    pub fn line(&self) -> String {
        let feature = self.feature.unwrap_or("none");
        match self.kind {
            Kind::Marker => format!(
                "edition marker: {}",
                self.detail.as_deref().unwrap_or("present")
            ),
            Kind::Table => {
                let rows = if self.count >= ROW_CAP {
                    format!("{ROW_CAP} or more rows")
                } else if self.count == 1 {
                    "1 row".to_string()
                } else {
                    format!("{} rows", self.count)
                };
                format!("Enterprise table {} ({feature}): {rows}", self.subject)
            }
            Kind::Tenants => format!(
                "tenants: {} (the Community Edition runs a single tenant)",
                self.count
            ),
            Kind::FeatureFlag => {
                let tenants = if self.count == 1 {
                    "1 tenant".to_string()
                } else {
                    format!("{} tenants", self.count)
                };
                format!(
                    "Enterprise feature flag {} ({feature}): switched on for {tenants}",
                    self.subject
                )
            }
        }
    }
}

/// A check that could not be made. `error` is a class name, never a text.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Undecided {
    pub kind: Kind,
    pub subject: String,
    pub error: &'static str,
}

impl Undecided {
    /// The line in the refusal, without the `  - `.
    pub fn line(&self) -> String {
        let what = match self.kind {
            Kind::Marker => "edition marker".to_string(),
            Kind::Table => format!("Enterprise table {}", self.subject),
            Kind::Tenants => "tenants".to_string(),
            Kind::FeatureFlag => format!("Enterprise feature flag {}", self.subject),
        };
        format!("{what}: the check failed ({})", self.error)
    }
}

/// Why the Community Edition does not start: what was found, and what could
/// not be checked. At least one of the two is not empty.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Refusal {
    pub found: Vec<Finding>,
    pub undecided: Vec<Undecided>,
}

pub const REFUSAL_FIRST_LINE: &str = "The Community Edition does not start on this database.";
pub const REFUSAL_FOUND_LINE: &str =
    "This database has been used with the Enterprise Edition. Found:";
pub const REFUSAL_UNDECIDED_LINE: &str = "Whether this database has been used with the Enterprise Edition could not be decided, and the Community Edition does not start when it cannot decide. Not checked:";
pub const REFUSAL_WAYS_FORWARD: &str = "Two ways forward:
  1. Start the Enterprise Edition on this database.
  2. Run the reset command of the control plane, which deletes the Enterprise data so that the Community Edition can start:
       python main.py edition-reset
     It prints what it would delete and changes nothing until it is given --confirm-delete-enterprise-data. Take a pg_dump first.";

impl fmt::Display for Refusal {
    /// The contract's text (section 6), the same in both planes.
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        writeln!(f, "{REFUSAL_FIRST_LINE}")?;
        if !self.found.is_empty() {
            writeln!(f, "{REFUSAL_FOUND_LINE}")?;
            for finding in &self.found {
                writeln!(f, "  - {}", finding.line())?;
            }
        }
        if !self.undecided.is_empty() {
            writeln!(f, "{REFUSAL_UNDECIDED_LINE}")?;
            for undecided in &self.undecided {
                writeln!(f, "  - {}", undecided.line())?;
            }
        }
        write!(f, "{REFUSAL_WAYS_FORWARD}")
    }
}

impl std::error::Error for Refusal {}

/// What one direct check says.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Verdict {
    /// The check was made and found nothing.
    Clear,
    Found(Finding),
    Undecided(Undecided),
}

/// Read a check's answer. Only a non-negative integer at or under the
/// threshold is [`Verdict::Clear`]: a failure, NULL and a negative number
/// are "could not be decided", never "nothing found".
pub fn judge(check: &DirectCheck, answer: Result<Option<i64>, &'static str>) -> Verdict {
    let undecided = |error| {
        Verdict::Undecided(Undecided {
            kind: check.kind,
            subject: check.subject.to_string(),
            error,
        })
    };
    match answer {
        Err(error) => undecided(error),
        Ok(None) => undecided(UNEXPECTED_ANSWER),
        Ok(Some(count)) if count < 0 => undecided(UNEXPECTED_ANSWER),
        Ok(Some(count)) if count > check.more_than => Verdict::Found(Finding {
            kind: check.kind,
            subject: check.subject.to_string(),
            feature: check.feature,
            count,
            detail: None,
        }),
        Ok(Some(_)) => Verdict::Clear,
    }
}

/// The class of a database error, for the refusal. A name only: the text of
/// a driver error can quote a statement and its parameters.
pub fn error_class(error: &sqlx::Error) -> &'static str {
    match error {
        sqlx::Error::Database(db) => match db.code().as_deref() {
            Some("42P01") => "UndefinedTable",
            Some("42703") => "UndefinedColumn",
            Some("42501") => "InsufficientPrivilege",
            Some("42883") => "UndefinedFunction",
            Some("57014") => "QueryCanceled",
            Some("55P03") => "LockNotAvailable",
            _ => "DatabaseError",
        },
        sqlx::Error::ColumnDecode { .. }
        | sqlx::Error::Decode(_)
        | sqlx::Error::ColumnNotFound(_)
        | sqlx::Error::ColumnIndexOutOfBounds { .. }
        | sqlx::Error::TypeNotFound { .. }
        | sqlx::Error::RowNotFound => UNEXPECTED_ANSWER,
        sqlx::Error::PoolTimedOut => "PoolTimedOut",
        sqlx::Error::PoolClosed => "PoolClosed",
        sqlx::Error::Io(_) => "ConnectionError",
        sqlx::Error::Tls(_) => "TlsError",
        sqlx::Error::Protocol(_) => "ProtocolError",
        _ => "Error",
    }
}

/// Run one statement in a transaction of its own, so that a failure of one
/// check does not prevent the others. The transaction is read-only (the
/// server refuses a write) and the server cancels the statement after
/// `timeout`, lock waits included; the outer timer is for a server that
/// does not answer at all.
async fn rows_of(
    db: &PgPool,
    sql: &str,
    timeout: Duration,
) -> Result<Vec<sqlx::postgres::PgRow>, &'static str> {
    let run = async {
        let mut tx = db.begin().await?;
        sqlx::raw_sql(&format!(
            "SET TRANSACTION READ ONLY; SET LOCAL statement_timeout = {}",
            timeout.as_millis().max(1)
        ))
        .execute(&mut *tx)
        .await?;
        // Not a cached prepared statement: it runs once per start.
        let rows = sqlx::query(sql)
            .persistent(false)
            .fetch_all(&mut *tx)
            .await?;
        tx.rollback().await?;
        Ok::<_, sqlx::Error>(rows)
    };
    match tokio::time::timeout(timeout + CLIENT_GRACE, run).await {
        Err(_elapsed) => Err(TIMEOUT),
        Ok(Err(error)) => Err(error_class(&error)),
        Ok(Ok(rows)) => Ok(rows),
    }
}

/// The one integer a check's statement answers.
async fn count_of(db: &PgPool, sql: &str, timeout: Duration) -> Result<Option<i64>, &'static str> {
    let rows = rows_of(db, sql, timeout).await?;
    let [row] = rows.as_slice() else {
        return Err(UNEXPECTED_ANSWER);
    };
    if row.len() != 1 {
        return Err(UNEXPECTED_ANSWER);
    }
    match row.try_get::<Option<i64>, _>(0) {
        Ok(count) => Ok(count),
        Err(error) => Err(error_class(&error)),
    }
}

fn iso(at: DateTime<Utc>) -> String {
    at.format("%Y-%m-%dT%H:%M:%SZ").to_string()
}

/// A version as the marker's line prints it: the stored text, with anything
/// that would break the line (a control character) replaced.
fn printable(version: &str) -> String {
    version
        .chars()
        .map(|c| if c.is_control() { '?' } else { c })
        .collect()
}

/// The marker's line, as the contract writes it.
pub fn marker_detail(
    first_started_at: DateTime<Utc>,
    first_version: &str,
    last_started_at: DateTime<Utc>,
    last_version: &str,
) -> String {
    format!(
        "the Enterprise Edition first started on this database at {} (version {}) and last started at {} (version {})",
        iso(first_started_at),
        printable(first_version),
        iso(last_started_at),
        printable(last_version),
    )
}

async fn read_marker(db: &PgPool, timeout: Duration) -> Result<Option<Finding>, Undecided> {
    let undecided = |error| Undecided {
        kind: Kind::Marker,
        subject: MARKER_TABLE.to_string(),
        error,
    };
    let rows = rows_of(db, MARKER_READ_SQL, timeout)
        .await
        .map_err(undecided)?;
    let row = match rows.as_slice() {
        [] => return Ok(None),
        [row] => row,
        // The table holds zero rows or one. More is not this table.
        _ => return Err(undecided(UNEXPECTED_ANSWER)),
    };
    let read = || -> Result<String, sqlx::Error> {
        Ok(marker_detail(
            row.try_get::<DateTime<Utc>, _>(0)?,
            &row.try_get::<String, _>(2)?,
            row.try_get::<DateTime<Utc>, _>(1)?,
            &row.try_get::<String, _>(3)?,
        ))
    };
    match read() {
        Ok(detail) => Ok(Some(Finding {
            kind: Kind::Marker,
            subject: MARKER_TABLE.to_string(),
            feature: None,
            count: 1,
            detail: Some(detail),
        })),
        Err(error) => Err(undecided(error_class(&error))),
    }
}

/// The Community Edition's reads: the marker, then every direct check. Reads
/// only. `Ok` only when every one was made and none found anything; both
/// lists of the refusal are complete (a check that fails does not stop the
/// checks after it).
pub async fn inspect(db: &PgPool, statement_timeout: Duration) -> Result<(), Refusal> {
    let mut found = Vec::new();
    let mut undecided = Vec::new();
    match read_marker(db, statement_timeout).await {
        Ok(None) => {}
        Ok(Some(finding)) => found.push(finding),
        Err(not_checked) => undecided.push(not_checked),
    }
    for check in direct_checks() {
        let answer = count_of(db, &check.sql, statement_timeout).await;
        match judge(&check, answer) {
            Verdict::Clear => {}
            Verdict::Found(finding) => found.push(finding),
            Verdict::Undecided(not_checked) => undecided.push(not_checked),
        }
    }
    if found.is_empty() && undecided.is_empty() {
        Ok(())
    } else {
        Err(Refusal { found, undecided })
    }
}

/// The check at start. An Enterprise composition does nothing: no read, no
/// write, never a refusal. A Community composition starts only when
/// [`inspect`] found nothing and could decide everything.
pub async fn enforce_at_start(state: &AppState) -> Result<(), Refusal> {
    match state.composition.edition() {
        Edition::Enterprise => {
            tracing::info!(
                enterprise_features = ?state.composition.enterprise_features(),
                "Edition: enterprise (the Enterprise-data check is the Community Edition's)"
            );
            Ok(())
        }
        Edition::Community => {
            inspect(&state.db, STATEMENT_TIMEOUT).await?;
            tracing::info!("Edition check passed: community edition");
            Ok(())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn check(kind: Kind) -> DirectCheck {
        direct_checks()
            .into_iter()
            .find(|c| c.kind == kind)
            .expect("a check of the kind")
    }

    #[test]
    fn there_are_43_checks_in_the_contract_order() {
        let checks = direct_checks();
        assert_eq!(checks.len(), 43);
        assert_eq!(ENTERPRISE_ONLY_TABLES.len(), 37);
        assert!(checks[..37].iter().all(|c| c.kind == Kind::Table));
        assert_eq!(checks[37].kind, Kind::Tenants);
        assert!(checks[38..].iter().all(|c| c.kind == Kind::FeatureFlag));
        // No subject twice within a kind: a duplicate would hide a missing one.
        let mut seen = std::collections::BTreeSet::new();
        for c in &checks {
            assert!(seen.insert((c.kind.as_str(), c.subject)), "{c:?} twice");
        }
    }

    #[test]
    fn only_a_count_at_or_under_the_threshold_is_clear() {
        let table = check(Kind::Table);
        assert_eq!(judge(&table, Ok(Some(0))), Verdict::Clear);
        assert!(matches!(judge(&table, Ok(Some(1))), Verdict::Found(f) if f.count == 1));
        let tenants = check(Kind::Tenants);
        assert_eq!(judge(&tenants, Ok(Some(0))), Verdict::Clear);
        assert_eq!(judge(&tenants, Ok(Some(1))), Verdict::Clear);
        assert!(matches!(judge(&tenants, Ok(Some(2))), Verdict::Found(f) if f.count == 2));
    }

    #[test]
    fn an_answer_that_is_no_count_is_undecided_never_clear() {
        for c in direct_checks() {
            for answer in [
                Ok(None),
                Ok(Some(-1)),
                Ok(Some(i64::MIN)),
                Err("UndefinedTable"),
                Err(TIMEOUT),
                Err(UNEXPECTED_ANSWER),
            ] {
                let verdict = judge(&c, answer);
                assert!(
                    matches!(verdict, Verdict::Undecided(_)),
                    "{} {} with {answer:?}: {verdict:?}",
                    c.kind.as_str(),
                    c.subject
                );
            }
        }
    }

    #[test]
    fn an_error_is_named_by_class_and_its_text_is_not_kept() {
        let secret = "relation \"docker_images\" password=hunter2";
        let error = sqlx::Error::Protocol(secret.to_string());
        assert_eq!(error_class(&error), "ProtocolError");
        assert_eq!(error_class(&sqlx::Error::RowNotFound), UNEXPECTED_ANSWER);
        assert_eq!(error_class(&sqlx::Error::PoolTimedOut), "PoolTimedOut");
        let line = Undecided {
            kind: Kind::Table,
            subject: "docker_images".into(),
            error: error_class(&error),
        }
        .line();
        assert_eq!(
            line,
            "Enterprise table docker_images: the check failed (ProtocolError)"
        );
        assert!(!line.contains("hunter2"));
    }

    #[test]
    fn the_lines_are_the_contracts() {
        let found = |kind, subject: &str, feature, count| {
            Finding {
                kind,
                subject: subject.to_string(),
                feature,
                count,
                detail: None,
            }
            .line()
        };
        assert_eq!(
            found(Kind::Table, "policy_bundles", Some("policy_as_code"), 3),
            "Enterprise table policy_bundles (policy_as_code): 3 rows"
        );
        assert_eq!(
            found(Kind::Table, "system_admins", Some("multi_tenant"), 1),
            "Enterprise table system_admins (multi_tenant): 1 row"
        );
        assert_eq!(
            found(Kind::Table, "kb_chunks", Some("knowledge_base"), 1000),
            "Enterprise table kb_chunks (knowledge_base): 1000 or more rows"
        );
        assert_eq!(
            found(Kind::Table, "kb_chunks", Some("knowledge_base"), 999),
            "Enterprise table kb_chunks (knowledge_base): 999 rows"
        );
        assert_eq!(
            found(Kind::Tenants, "tenants", None, 2),
            "tenants: 2 (the Community Edition runs a single tenant)"
        );
        assert_eq!(
            found(
                Kind::FeatureFlag,
                "knowledge_base",
                Some("knowledge_base"),
                1
            ),
            "Enterprise feature flag knowledge_base (knowledge_base): switched on for 1 tenant"
        );
        assert_eq!(
            found(Kind::FeatureFlag, "mcp_registry", Some("mcp_registry"), 2),
            "Enterprise feature flag mcp_registry (mcp_registry): switched on for 2 tenants"
        );
    }

    #[test]
    fn the_marker_line_gives_utc_seconds_and_keeps_to_one_line() {
        let first = DateTime::parse_from_rfc3339("2026-11-11T10:00:00.123456+01:00")
            .unwrap()
            .with_timezone(&Utc);
        let last = DateTime::parse_from_rfc3339("2026-11-12T07:30:00Z")
            .unwrap()
            .with_timezone(&Utc);
        assert_eq!(
            marker_detail(first, "0.11.40", last, "0.11.41"),
            "the Enterprise Edition first started on this database at 2026-11-11T09:00:00Z \
             (version 0.11.40) and last started at 2026-11-12T07:30:00Z (version 0.11.41)"
        );
        let broken = marker_detail(first, "1\n  - forged line", last, "2");
        assert!(!broken.contains('\n'), "{broken}");
    }

    #[test]
    fn a_refusal_with_nothing_found_leaves_the_found_block_out() {
        let refusal = Refusal {
            found: vec![],
            undecided: vec![Undecided {
                kind: Kind::Marker,
                subject: MARKER_TABLE.into(),
                error: "UndefinedTable",
            }],
        };
        let text = refusal.to_string();
        assert!(!text.contains(REFUSAL_FOUND_LINE));
        assert!(text.contains("  - edition marker: the check failed (UndefinedTable)\n"));
        assert!(text.starts_with(REFUSAL_FIRST_LINE));
        assert!(text.ends_with(REFUSAL_WAYS_FORWARD));
    }
}
