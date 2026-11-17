// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Test-harness guard: no hand-rolled `Drop` that blocks on async work.
//!
//! A cleanup guard whose `Drop` runs `block_on` on a fresh runtime while the
//! test's current-thread runtime waits in `join()` hangs if anything it
//! touches (a pool, a client, a channel) is driven by the blocked runtime.
//! That pattern hung every core CI run for weeks, twice: it was fixed in one
//! file and survived, copied, in another. `test_helpers::DbCleanupGuard` is
//! the one implementation that gets it right (own pool, own runtime, bounded
//! timeout); every other `Drop` under `tests/` must not call `block_on`.
//!
//! This scans the source because the failure is a hang, which no assertion
//! inside the hanging test can report.
#[path = "support/roots.rs"]
mod roots;

use std::fs;
use std::path::{Path, PathBuf};

/// `file:type` pairs allowed to block in `Drop`.
const ALLOWED: &[&str] = &["tests/test_helpers.rs:DbCleanupGuard"];

fn rust_files(dir: &Path, out: &mut Vec<PathBuf>) {
    for e in fs::read_dir(dir).unwrap().flatten() {
        let p = e.path();
        if p.is_dir() {
            rust_files(&p, out);
        } else if p.extension().and_then(|s| s.to_str()) == Some("rs") {
            out.push(p);
        }
    }
}

/// Every `impl Drop for T { … }` body in `src` that calls `block_on`, as
/// `T`. The body is the brace-balanced block after the header.
fn blocking_drops(src: &str) -> Vec<String> {
    let mut found = Vec::new();
    let mut rest = src;
    while let Some(at) = rest.find("impl Drop for ") {
        let after = &rest[at + "impl Drop for ".len()..];
        let name: String = after
            .chars()
            .take_while(|c| c.is_alphanumeric() || *c == '_')
            .collect();
        let Some(open) = after.find('{') else { break };
        let mut depth = 0usize;
        let mut end = after.len();
        for (i, c) in after[open..].char_indices() {
            match c {
                '{' => depth += 1,
                '}' => {
                    depth -= 1;
                    if depth == 0 {
                        end = open + i;
                        break;
                    }
                }
                _ => {}
            }
        }
        if after[open..end].contains("block_on") {
            found.push(name);
        }
        rest = &after[end..];
    }
    found
}

#[test]
fn no_test_drop_blocks_on_async_work() {
    let mut files = Vec::new();
    rust_files(Path::new("tests"), &mut files);
    // The Enterprise workspace too, where this repository carries it.
    if let Some(enterprise) = roots::tree("enterprise") {
        let before = files.len();
        rust_files(&enterprise.join("tests"), &mut files);
        assert!(files.len() > before + 20, "the Enterprise tests");
    }

    let mut offenders = Vec::new();
    for f in &files {
        let rel = f.to_string_lossy().replace('\\', "/");
        // This file holds the forbidden pattern as a string fixture.
        if rel == "tests/test_harness_guard.rs" {
            continue;
        }
        let src = fs::read_to_string(f).unwrap();
        for ty in blocking_drops(&src) {
            let key = format!("{rel}:{ty}");
            if !ALLOWED.contains(&key.as_str()) {
                offenders.push(key);
            }
        }
    }
    assert!(
        offenders.is_empty(),
        "these test `Drop` impls call `block_on`; use `test_helpers::DbCleanupGuard` \
         (own pool, own runtime, bounded timeout) instead: {offenders:?}"
    );
}

/// The scanner must catch the exact shape that hung CI, or the guard above
/// proves nothing.
#[test]
fn the_scanner_catches_the_pattern_that_hung_ci() {
    let old = r#"
        impl Drop for TestCleanupGuard {
            fn drop(&mut self) {
                let db = self.db.clone();
                let _ = std::thread::spawn(move || {
                    if let Ok(rt) = tokio::runtime::Builder::new_current_thread().build() {
                        rt.block_on(async move { cleanup(&db).await; });
                    }
                }).join();
            }
        }
        impl Drop for Harmless { fn drop(&mut self) { self.n += 1; } }
    "#;
    assert_eq!(blocking_drops(old), vec!["TestCleanupGuard".to_string()]);
}

/// The allow-list must not outlive what it allows.
#[test]
fn the_allow_list_is_not_stale() {
    for entry in ALLOWED {
        let (file, ty) = entry.split_once(':').unwrap();
        let src = fs::read_to_string(file).unwrap();
        assert!(
            blocking_drops(&src).iter().any(|t| t == ty),
            "allow-list entry {entry} no longer matches a blocking Drop; remove it"
        );
    }
}

/// A module whose tests share state outside the test's own database: a
/// per-test value in the process environment, or the fixed Redis spool lists.
fn shares_process_state(src: &str) -> bool {
    let per_test_env = src
        .match_indices("set_var(")
        .any(|(at, _)| src[at..].lines().next().unwrap_or("").contains(".uri()"));
    per_test_env || src.contains("with_shadow_redis(")
}

/// Names of the async tests in `src` that do not take `SERIAL` first.
fn tests_missing_the_serial_lock(src: &str) -> Vec<String> {
    let mut missing = Vec::new();
    for (at, _) in src.match_indices("async fn ") {
        let before = src[..at].trim_end();
        let is_test = before
            .lines()
            .next_back()
            .is_some_and(|l| l.contains("::test"));
        if !is_test {
            continue;
        }
        let after = &src[at + "async fn ".len()..];
        let name: String = after
            .chars()
            .take_while(|c| c.is_alphanumeric() || *c == '_')
            .collect();
        let Some(open) = after.find('{') else {
            continue;
        };
        let first = after[open + 1..].trim_start().lines().next().unwrap_or("");
        if !first.contains("SERIAL.lock().await") {
            missing.push(name);
        }
    }
    missing
}

/// `cargo test` runs a binary's tests as threads of one process; nextest gives
/// each its own. A test that sets a per-test env value or clears a shared spool
/// passes under one and fails under the other, so both runners stay clean only
/// if such a module serialises its own tests. Seven tests failed this way under
/// `cargo test` while CI (nextest) stayed green.
#[test]
fn tests_sharing_process_state_take_the_serial_lock() {
    let mut files = Vec::new();
    rust_files(Path::new("tests/integration"), &mut files);
    // The Enterprise workspace too, where this repository carries it.
    if let Some(enterprise) = roots::tree("enterprise") {
        let before = files.len();
        rust_files(&enterprise.join("tests/integration"), &mut files);
        assert!(
            files.len() > before + 20,
            "the Enterprise integration tests"
        );
    }

    let mut offenders = Vec::new();
    for f in &files {
        let src = fs::read_to_string(f).unwrap();
        if shares_process_state(&src) {
            for name in tests_missing_the_serial_lock(&src) {
                offenders.push(format!("{}::{name}", f.display()));
            }
        }
    }
    assert!(
        offenders.is_empty(),
        "these tests share process state but do not start with \
         `let _serial = SERIAL.lock().await;`: {offenders:?}"
    );
}

/// The scanners must catch the shapes that failed, or the guard proves nothing.
#[test]
fn the_serial_scanner_catches_the_shapes_that_failed() {
    let env = r#"
        fn use_runner(r: &MockServer) { unsafe { std::env::set_var("URL", r.uri()); } }
        #[sqlx::test(migrations = "./tests/migrations")]
        async fn unlocked(pool: PgPool) {
            use_runner(&r).await;
        }
        #[sqlx::test(migrations = "./tests/migrations")]
        async fn locked(pool: PgPool) {
            let _serial = SERIAL.lock().await;
        }
        async fn helper() {}
    "#;
    assert!(shares_process_state(env));
    assert_eq!(tests_missing_the_serial_lock(env), vec!["unlocked"]);

    assert!(shares_process_state(
        "BackgroundWriter::new().with_shadow_redis(Some(r))"
    ));
    // The same fixed value everywhere cannot collide.
    assert!(!shares_process_state(
        r#"std::env::set_var("WITNESS", "off");"#
    ));
}

// ── A test database on an ephemeral port can connect to itself ───────────────
//
// `localhost` tries `::1` first. With nothing listening there and a port inside
// the OS's ephemeral range, the kernel now and then picks that same port as the
// source port and TCP connects the socket to itself. The client then reads its
// own first message back, and both SSLRequest and Startup begin with a zero
// byte: `unexpected response from SSLRequest: 0x00`, in a different test each
// run, about one a run with scratch servers on 55433 and 55500.
// 32768 is the lowest common start of that range (Linux; macOS starts at 49152).
const LOWEST_EPHEMERAL_PORT: u16 = 32768;

fn url_port(url: &str) -> Option<u16> {
    let after_scheme = url.split_once("://")?.1;
    let authority = after_scheme.split(['/', '?']).next()?;
    let host_port = authority.rsplit('@').next()?;
    host_port.rsplit_once(':')?.1.parse().ok()
}

fn can_connect_to_itself(url: &str) -> bool {
    url_port(url).is_some_and(|p| p >= LOWEST_EPHEMERAL_PORT)
}

#[test]
fn the_test_database_port_cannot_connect_to_itself() {
    let Ok(url) = std::env::var("DATABASE_URL") else {
        return; // the helpers default to port 5432
    };
    assert!(
        !can_connect_to_itself(&url),
        "DATABASE_URL uses port {:?}, inside the ephemeral range. Connections can occasionally \
         connect to themselves and fail with stray NUL bytes in an unrelated test. Publish the \
         throwaway server on a port below {LOWEST_EPHEMERAL_PORT}.",
        url_port(&url)
    );
}

#[test]
fn the_port_guard_catches_the_urls_that_failed() {
    for failed in [
        "postgresql://brutor:brutor123@localhost:55433/brutor_test?sslmode=disable",
        "postgresql://brutor:brutor123@localhost:55500/brutor_test",
    ] {
        assert!(can_connect_to_itself(failed), "{failed}");
    }
    for fine in [
        "postgresql://brutor:brutor123@localhost:5432/brutor_core_test",
        "postgresql://brutor:brutor123@127.0.0.1:15433/brutor_test",
        "postgresql://brutor@db/brutor_test",
    ] {
        assert!(!can_connect_to_itself(fine), "{fine}");
    }
}
