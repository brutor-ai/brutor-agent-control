// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The skill size limit is one number in three places, and they agree.
//!
//! * The Control Plane checks it at upload: `SKILL_MAX_BYTES` in
//!   `app/constants/agent_skill.py` (30,000,000 bytes of files per skill,
//!   uncompressed), and it is the number `GET /v1/admin/agent-skills` gives
//!   as `limits.skill_max_bytes`.
//! * The Core Gateway (`src/services/skill_limits.rs`) derives from it the
//!   largest request it sends the skill runner, and refuses to send a larger
//!   one.
//! * The skill runner (`src/config.rs`) writes the same three definitions and
//!   answers a larger request with 413.
//!
//! **This test needs sibling repositories** (`tests/support/roots.rs`): the
//! Control Plane, unless `BRUTOR_TEST_SIBLINGS=optional` (then the skip is
//! recorded); the skill runner where it is checked out beside this one.

#[path = "support/roots.rs"]
mod roots;

use std::fs;

use brutor_gateway_core::services::skill_limits::{
    MAX_REQUEST_BYTES, REQUEST_FRAMING_BYTES, SKILL_MAX_BYTES,
};

/// `NAME = 30_000_000` in a Python module: the integer, `_` allowed.
fn python_int(source: &str, name: &str) -> u64 {
    let line = source
        .lines()
        .find(|l| l.starts_with(&format!("{name} =")))
        .unwrap_or_else(|| panic!("no top-level `{name} = …`"));
    let value = line.split_once('=').expect("an assignment").1;
    let value = value
        .split('#')
        .next()
        .expect("a value")
        .trim()
        .replace('_', "");
    value
        .parse()
        .unwrap_or_else(|e| panic!("`{line}` is not an integer literal: {e}"))
}

#[test]
fn the_skill_limit_is_the_control_planes() {
    let Some(control) = roots::tree("control-plane") else {
        return;
    };
    let path = control.join("app/constants/agent_skill.py");
    let source =
        fs::read_to_string(&path).unwrap_or_else(|e| panic!("read {}: {e}", path.display()));
    assert_eq!(
        python_int(&source, "SKILL_MAX_BYTES"),
        SKILL_MAX_BYTES as u64,
        "the Control Plane's SKILL_MAX_BYTES ({}) is not the Core Gateway's",
        path.display()
    );
}

#[test]
fn the_request_limit_is_the_skill_base64_encoded_plus_framing() {
    assert_eq!(SKILL_MAX_BYTES, 30_000_000);
    assert_eq!(REQUEST_FRAMING_BYTES, 1_048_576);
    assert_eq!(MAX_REQUEST_BYTES, 40_000_000 + 1_048_576);
}

/// The three definitions as one repository writes them: `name = expression`.
fn definitions(source: &str) -> Vec<(String, String)> {
    [
        "SKILL_MAX_BYTES",
        "REQUEST_FRAMING_BYTES",
        "MAX_REQUEST_BYTES",
    ]
    .iter()
    .map(|name| {
        let start = format!("pub const {name}: usize = ");
        let at = source
            .find(&start)
            .unwrap_or_else(|| panic!("no `{start}…;`"));
        let rest = &source[at + start.len()..];
        let expression = &rest[..rest.find(';').expect("the definition ends")];
        (name.to_string(), expression.trim().to_string())
    })
    .collect()
}

#[test]
fn the_skill_runner_derives_the_same_limit() {
    let mine = definitions(
        &fs::read_to_string(
            roots::tree("community")
                .expect("the Community tree")
                .join("src/services/skill_limits.rs"),
        )
        .expect("read src/services/skill_limits.rs"),
    );
    let Some(runner) = roots::tree("skill-runner") else {
        return;
    };
    let path = runner.join("src/config.rs");
    let theirs =
        fs::read_to_string(&path).unwrap_or_else(|e| panic!("read {}: {e}", path.display()));
    assert_eq!(
        mine,
        definitions(&theirs),
        "the skill runner's limits ({}) differ from the Core Gateway's",
        path.display()
    );
}
