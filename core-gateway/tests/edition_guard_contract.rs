// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The core's Enterprise-data check is the edition-install contract.
//!
//! The contract is one file, written from the control
//! plane's implementation and held equal to it by the control plane's own
//! test. This test holds the core equal to the same file: the marker
//! statement, every direct check (kind, subject, feature, threshold and
//! SQL, in order), and the wording of the refusal. The two planes cannot
//! drift apart without one of the two tests failing.
//!
//! The contract is kept outside this repository. Where it is not there
//! (a build of this repository alone) the comparison is skipped, and says so;
//! the lists in the code are then pinned by the unit tests of
//! `services::edition_guard` only.

#[path = "support/roots.rs"]
mod roots;

use brutor_gateway_core::services::edition_guard::{
    self, Finding, Kind, Refusal, Undecided, direct_checks,
};
use chrono::{DateTime, Utc};

/// The edition-install contract, from where `tests/support/roots.rs` says
/// it is kept. **Outside this repository:** where it is absent
/// the comparison is skipped, and the skip is recorded.
fn contract() -> Option<String> {
    let path = roots::tree("edition-records")?.join("d15-contract.md");
    Some(
        std::fs::read_to_string(&path)
            .unwrap_or_else(|e| panic!("the edition-install contract at {}: {e}", path.display())),
    )
}

/// The text of the n-th fenced block of `language` after `heading`.
fn fenced(contract: &str, heading: &str, language: &str, nth: usize) -> String {
    let section = &contract[contract
        .find(heading)
        .unwrap_or_else(|| panic!("the contract has no heading {heading:?}"))..];
    let open = format!("```{language}\n");
    let mut rest = section;
    for _ in 0..nth {
        rest = &rest[rest.find(&open).expect("fenced block") + open.len()..];
        rest = &rest[rest.find("```").expect("closing fence") + 3..];
    }
    let body = &rest[rest.find(&open).expect("fenced block") + open.len()..];
    body[..body.find("\n```").expect("closing fence")].to_string()
}

fn ticked(cell: &str) -> String {
    cell.trim().trim_matches('`').to_string()
}

#[test]
fn the_marker_statement_is_the_contracts() {
    let Some(contract) = contract() else { return };
    assert_eq!(
        fenced(&contract, "### 4.1 The marker", "sql", 0),
        edition_guard::MARKER_READ_SQL
    );
}

#[test]
fn the_direct_checks_are_the_contracts_in_order() {
    let Some(contract) = contract() else { return };
    let section = &contract[contract.find("### 4.2 The direct checks").expect("4.2")..];
    let section = &section[..section.find("\n## 5.").expect("section 5")];
    let mut rows = Vec::new();
    for line in section.lines() {
        let cells: Vec<&str> = line.split('|').collect();
        // | # | kind | subject | feature | more than | SQL |
        if cells.len() != 8 || cells[1].trim().parse::<usize>().is_err() {
            continue;
        }
        rows.push((
            cells[1].trim().parse::<usize>().expect("number"),
            ticked(cells[2]),
            ticked(cells[3]),
            ticked(cells[4]),
            cells[5].trim().parse::<i64>().expect("threshold"),
            ticked(cells[6]),
        ));
    }
    assert_eq!(rows.len(), 43, "the contract lists 43 checks");
    let code: Vec<_> = direct_checks()
        .into_iter()
        .enumerate()
        .map(|(i, c)| {
            (
                i + 1,
                c.kind.as_str().to_string(),
                c.subject.to_string(),
                c.feature.unwrap_or("none").to_string(),
                c.more_than,
                c.sql,
            )
        })
        .collect();
    assert_eq!(code.len(), rows.len());
    for (ours, theirs) in code.iter().zip(&rows) {
        assert_eq!(ours, theirs);
    }
    // The cap the contract's prose states is the one in the statements.
    assert!(section.contains(&format!("`LIMIT {}` inside", edition_guard::ROW_CAP)));
}

fn at(text: &str) -> DateTime<Utc> {
    DateTime::parse_from_rfc3339(text)
        .expect("time")
        .with_timezone(&Utc)
}

fn found(kind: Kind, subject: &str, count: i64) -> Finding {
    let feature = direct_checks()
        .into_iter()
        .find(|c| c.kind == kind && c.subject == subject)
        .unwrap_or_else(|| panic!("no check {subject}"))
        .feature;
    Finding {
        kind,
        subject: subject.to_string(),
        feature,
        count,
        detail: None,
    }
}

/// The two refusals the contract prints, built from the same findings.
#[test]
fn the_refusal_is_the_contracts_text() {
    let Some(contract) = contract() else { return };
    let marker = Finding {
        kind: Kind::Marker,
        subject: edition_guard::MARKER_TABLE.to_string(),
        feature: None,
        count: 1,
        detail: Some(edition_guard::marker_detail(
            at("2026-11-11T09:00:00Z"),
            "0.11.40",
            at("2026-11-12T07:30:00Z"),
            "0.11.41",
        )),
    };
    let with_findings = Refusal {
        found: vec![
            marker,
            found(Kind::Table, "policy_bundles", 3),
            found(Kind::Table, "system_admins", 1),
            found(Kind::Table, "kb_chunks", 1000),
            found(Kind::Tenants, "tenants", 2),
            found(Kind::FeatureFlag, "knowledge_base", 1),
            found(Kind::FeatureFlag, "mcp_registry", 2),
        ],
        undecided: vec![],
    };
    assert_eq!(
        with_findings.to_string(),
        fenced(&contract, "## 6. The wording of the refusal", "text", 0)
    );
    let undecided = Refusal {
        found: vec![found(Kind::Tenants, "tenants", 2)],
        undecided: vec![
            Undecided {
                kind: Kind::Marker,
                subject: edition_guard::MARKER_TABLE.to_string(),
                error: "UndefinedTable",
            },
            Undecided {
                kind: Kind::Table,
                subject: "batch_jobs".to_string(),
                error: "ProgrammingError",
            },
        ],
    };
    assert_eq!(
        undecided.to_string(),
        fenced(&contract, "## 6. The wording of the refusal", "text", 1)
    );
}

/// The lists are `editions.yaml`'s, in its order, when that file is there.
#[test]
fn the_tables_and_flags_are_editions_yamls() {
    let Some(records) = roots::tree("edition-records") else {
        return; // skipped, and recorded: the file is not here
    };
    let path = records.join("editions.yaml");
    let text = std::fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("editions.yaml at {}: {e}", path.display()));
    let block = &text[text.find("  enterprise_only_tables:\n").expect("tables")..];
    let block = &block[..block.find("\n\n").expect("end of the block")];
    let mut tables = Vec::new();
    for line in block.lines().skip(1) {
        let (feature, list) = line.trim().split_once(':').expect("feature: [tables]");
        for table in list.trim().trim_matches(['[', ']']).split(',') {
            tables.push((table.trim().to_string(), feature.to_string()));
        }
    }
    let ours: Vec<(String, String)> = edition_guard::ENTERPRISE_ONLY_TABLES
        .iter()
        .map(|(t, f)| (t.to_string(), f.to_string()))
        .collect();
    assert_eq!(ours, tables);

    let flags = &text[text.find("  tenant_feature_flags:\n").expect("flags")..];
    let line = flags
        .lines()
        .find(|l| l.trim_start().starts_with("enterprise:"))
        .expect("enterprise flags");
    let listed: Vec<String> = line
        .split_once(':')
        .expect("list")
        .1
        .trim()
        .trim_matches(['[', ']'])
        .split(',')
        .map(|f| f.trim().to_string())
        .collect();
    let ours: Vec<String> = edition_guard::ENTERPRISE_FEATURE_FLAGS
        .iter()
        .map(|(f, _)| f.to_string())
        .collect();
    assert_eq!(ours, listed);
}
