// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Guard: the run-abort check consults what can answer, and says so.
//!
//! Why it matters: the check runs on every governed action. It used to ask
//! Redis for a key (`run:{tenant}:{run}:abort`) that nothing in the platform
//! ever wrote — one wasted round trip per action on the hot path — while the
//! module's documentation described that key as one of three deliberate
//! propagation paths and the local map as "the common case, zero I/O". For a
//! run that is not aborted (the common case) the database is what answers,
//! every time. A control whose documentation overstates how it propagates is
//! the kind of thing an external review catches; this keeps the code and the
//! description the same thing.

use std::fs;

fn module() -> String {
    fs::read_to_string("src/services/run_intervention.rs").expect("read run_intervention.rs")
}

/// The module without its test section.
fn production(src: &str) -> &str {
    src.split("#[cfg(test)]").next().unwrap_or(src)
}

#[test]
fn the_abort_check_makes_no_redis_round_trip() {
    let src = module();
    let code = production(&src);
    for forbidden in [
        "redis::cmd(",
        "redis::aio::ConnectionManager",
        "fn redis_key(",
    ] {
        assert!(
            !code.contains(forbidden),
            "run_intervention.rs contains `{forbidden}`: nothing writes an abort key to Redis, \
             so reading one is a round trip per governed action that can never hit"
        );
    }
}

#[test]
fn the_documentation_does_not_describe_a_path_that_does_not_exist() {
    let src = module();
    let docs: String = src
        .lines()
        .filter(|l| l.trim_start().starts_with("//"))
        .collect::<Vec<_>>()
        .join("\n");
    assert!(
        !docs.contains("Redis key"),
        "the module documentation names a Redis key as a propagation path; there is none"
    );
    assert!(
        !docs.contains("Three propagation paths"),
        "there are two: the notification into the local map, and the durable record"
    );
    assert!(
        !docs.contains("zero I/O"),
        "the local map answers only for runs already known to be aborted; \
         every other check reads the database"
    );
}

#[test]
fn the_callers_pass_no_redis_handle_to_the_check() {
    for path in [
        "src/services/run_controls.rs",
        "src/services/stream_guard.rs",
    ] {
        let src = fs::read_to_string(path).expect("read caller");
        let at = src
            .find("run_intervention::check(")
            .unwrap_or_else(|| panic!("{path} no longer calls the abort check"));
        let call = &src[at..(at + 260).min(src.len())];
        assert!(
            !call.contains("redis"),
            "{path} passes a Redis handle to the abort check:\n{call}"
        );
    }
}
