// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The structured result every entry point returns.

use serde::Serialize;
use serde_json::{Map, Value};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Severity {
    Error,
    Warning,
    Info,
}

impl Severity {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Error => "error",
            Self::Warning => "warning",
            Self::Info => "info",
        }
    }
}

impl From<brutor_evidence::Severity> for Severity {
    fn from(s: brutor_evidence::Severity) -> Self {
        match s {
            brutor_evidence::Severity::Error => Self::Error,
            brutor_evidence::Severity::Warning => Self::Warning,
            brutor_evidence::Severity::Info => Self::Info,
        }
    }
}

impl From<brutor_evidence::bundle::Severity> for Severity {
    fn from(s: brutor_evidence::bundle::Severity) -> Self {
        match s {
            brutor_evidence::bundle::Severity::Error => Self::Error,
            brutor_evidence::bundle::Severity::Warning => Self::Warning,
            brutor_evidence::bundle::Severity::Info => Self::Info,
        }
    }
}

/// One finding. `code` is stable across releases; `detail` is prose.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Finding {
    pub code: String,
    pub severity: Severity,
    pub detail: String,
    /// The record a bundle finding is about, when it is about one.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub record_id: Option<String>,
    /// The record rule's number for record findings.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub rule: Option<u8>,
}

/// The literal outcome of one check. `Ok` means only that the named check
/// held for these inputs.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Outcome {
    Ok,
    Failed,
    Skipped,
}

impl Outcome {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Ok => "ok",
            Self::Failed => "failed",
            Self::Skipped => "skipped",
        }
    }
}

/// A check that ran (or was skipped), with a neutral one-line statement of
/// what it established — e.g. "valid under kid 3fa2…", never a verdict
/// about the system the evidence describes.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Check {
    pub name: &'static str,
    pub outcome: Outcome,
    pub detail: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct Report {
    /// `record` | `statement` | `tree_head` | `receipt` | `bundle`.
    pub artefact: &'static str,
    /// No `error` finding.
    pub ok: bool,
    /// 0 = no error finding, 1 = at least one error finding, 2 = the
    /// input could not be read (usage / IO).
    pub exit_code: i32,
    pub checks: Vec<Check>,
    pub findings: Vec<Finding>,
    /// Artefact facts (ids, sizes, roots, kids, grades) — reported whatever
    /// the outcome; trust them only when `ok`.
    pub facts: Map<String, Value>,
}

/// 0 when no finding is an error, else 1; 2 when an input was unreadable.
pub fn exit_code(findings: &[Finding]) -> i32 {
    if findings
        .iter()
        .any(|f| f.code == crate::codes::INPUT_INVALID)
    {
        2
    } else if findings.iter().any(|f| f.severity == Severity::Error) {
        1
    } else {
        0
    }
}

impl Report {
    pub(crate) fn new(artefact: &'static str) -> Self {
        Self {
            artefact,
            ok: false,
            exit_code: 2,
            checks: Vec::new(),
            findings: Vec::new(),
            facts: Map::new(),
        }
    }

    pub(crate) fn find(&mut self, code: &str, severity: Severity, detail: impl Into<String>) {
        self.findings.push(Finding {
            code: code.to_owned(),
            severity,
            detail: detail.into(),
            record_id: None,
            rule: None,
        });
    }

    pub(crate) fn check(
        &mut self,
        name: &'static str,
        outcome: Outcome,
        detail: impl Into<String>,
    ) {
        self.checks.push(Check {
            name,
            outcome,
            detail: detail.into(),
        });
    }

    pub(crate) fn fact(&mut self, key: &str, value: impl Into<Value>) {
        self.facts.insert(key.to_owned(), value.into());
    }

    /// Seal `ok` / `exit_code` from the findings.
    pub(crate) fn finish(mut self) -> Self {
        self.exit_code = exit_code(&self.findings);
        self.ok = self.exit_code == 0;
        self
    }

    /// Finding codes in emission order (what the vector corpus pins).
    pub fn codes(&self) -> Vec<&str> {
        self.findings.iter().map(|f| f.code.as_str()).collect()
    }

    pub fn has_errors(&self) -> bool {
        self.findings.iter().any(|f| f.severity == Severity::Error)
    }

    /// Human-readable rendering: one line per check, then the findings,
    /// then the counts. Neutral wording by construction — only the check
    /// names and their literal outcomes.
    pub fn render_text(&self) -> String {
        let mut out = String::new();
        out.push_str(&format!("artefact: {}\n", self.artefact));
        for c in &self.checks {
            out.push_str(&format!(
                "  [{:<7}] {}: {}\n",
                c.outcome.as_str(),
                c.name,
                c.detail
            ));
        }
        if !self.findings.is_empty() {
            out.push_str("findings:\n");
            for f in &self.findings {
                let about = f
                    .record_id
                    .as_deref()
                    .map(|r| format!(" (record {})", short(r)))
                    .unwrap_or_default();
                out.push_str(&format!(
                    "  {:<7} {}{} — {}\n",
                    f.severity.as_str(),
                    f.code,
                    about,
                    f.detail
                ));
            }
        }
        let count = |s: Severity| self.findings.iter().filter(|f| f.severity == s).count();
        out.push_str(&format!(
            "result: {} error(s), {} warning(s), {} info (exit {})\n",
            count(Severity::Error),
            count(Severity::Warning),
            count(Severity::Info),
            self.exit_code
        ));
        out
    }
}

/// First 12 hex characters and an ellipsis, for human output only.
pub(crate) fn short(hex: &str) -> String {
    if hex.chars().count() > 16 {
        // chars, not bytes: an id from a hostile bundle need not be ASCII.
        format!("{}…", hex.chars().take(12).collect::<String>())
    } else {
        hex.to_owned()
    }
}
