// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The Community gateway is a single-instance
//! product, and its source says so.
//!
//! Three things a behaviour test cannot hold for the code nobody has written
//! yet:
//!
//! 1. **Community `src/` names no `replica_id` fan-out.** The reply to the
//!    control plane's cache-statistics request names no instance, nothing
//!    tells instances apart, and nothing reads the deployment's per-instance
//!    name for it. That code is the Enterprise workspace's
//!    (`enterprise/src/clustering.rs`).
//! 2. **The lease key and its timings have one definition**
//!    (`src/services/instance_lease.rs`), and the binary takes that lease:
//!    no second key, no other settings built in `src/`, no call of the
//!    builder's test seam.
//! 3. **The builder makes the instance check where the design puts it**:
//!    after Redis is connected, before the state exists and long before
//!    anything serves; skipped only for a composition one of whose
//!    extensions allows several instances; a refusal is the start's error.
//!
//! The behaviour is `tests/integration/instance_lease.rs` and
//! `tests/integration/cache_statistics.rs`. Each scanner below is proven
//! against the shape it forbids.

#[path = "support/roots.rs"]
mod roots;

use std::fs;
use std::path::{Path, PathBuf};

/// Is `file` one this run has? A file of the Enterprise workspace is not, in
/// a Community tree alone (`tests/support/roots.rs`); what this guard holds
/// about it is held where the workspace is.
fn here(file: &str) -> bool {
    !file.starts_with("enterprise/") || roots::tree("enterprise").is_some()
}

/// Every `.rs` file under `dir`, with its path relative to the repository.
fn rust_sources(dir: &str) -> Vec<(String, String)> {
    fn walk(dir: &Path, into: &mut Vec<PathBuf>) {
        for entry in fs::read_dir(dir).unwrap_or_else(|e| panic!("{}: {e}", dir.display())) {
            let path = entry.expect("directory entry").path();
            if path.is_dir() {
                walk(&path, into);
            } else if path.extension().is_some_and(|e| e == "rs") {
                into.push(path);
            }
        }
    }
    let mut files = Vec::new();
    walk(Path::new(dir), &mut files);
    files.sort();
    files
        .into_iter()
        .map(|p| {
            let text = fs::read_to_string(&p).unwrap_or_else(|e| panic!("{}: {e}", p.display()));
            (p.to_string_lossy().replace('\\', "/"), text)
        })
        .collect()
}

/// Code only: comment lines dropped, so prose that names a call is not one.
fn code(text: &str) -> String {
    text.lines()
        .filter(|l| !l.trim_start().starts_with("//"))
        .collect::<Vec<_>>()
        .join("\n")
}

/// The body of `fn name` in `source`: from its opening brace to the matching
/// closing one.
fn body_of<'a>(source: &'a str, name: &str) -> &'a str {
    let at = source
        .find(&format!("fn {name}("))
        .unwrap_or_else(|| panic!("no fn {name}"));
    let open = at + source[at..].find('{').expect("opening brace");
    let mut depth = 0usize;
    for (i, c) in source[open..].char_indices() {
        match c {
            '{' => depth += 1,
            '}' => {
                depth -= 1;
                if depth == 0 {
                    return &source[open..=open + i];
                }
            }
            _ => {}
        }
    }
    panic!("fn {name}: unbalanced braces");
}

// ── 1. no replica identity in Community ──────────────────────────────────

/// The one Community file that reads the deployment's per-instance name, and
/// why it is not the fan-out: the audit chain labels the chain a process
/// writes with it (`audit_chain::chain_id`), which one instance has too.
const REPLICA_ENV_READERS: &[&str] = &["src/services/audit_chain.rs"];

/// What in `text` (the file `path`) belongs to the per-replica fan-out.
/// Comments count: Community source does not describe replicas answering
/// either.
fn replica_fan_out_in(path: &str, text: &str) -> Vec<String> {
    let mut found = Vec::new();
    for (number, line) in text.lines().enumerate() {
        let names_a_replica = line.contains("replica_id") || line.contains("ReplicaId");
        let reads_the_variable =
            line.contains("REPLICA_ID") && !REPLICA_ENV_READERS.contains(&path);
        if names_a_replica || reads_the_variable {
            found.push(format!("{path}:{}: {}", number + 1, line.trim()));
        }
    }
    found
}

#[test]
fn community_source_names_no_replica_id_fan_out() {
    let mut found = Vec::new();
    for (path, text) in rust_sources("src") {
        found.extend(replica_fan_out_in(&path, &text));
    }
    assert!(
        found.is_empty(),
        "Community source names the per-replica statistics fan-out; it is the Enterprise \
         clustering extension's (enterprise/src/clustering.rs):\n{}",
        found.join("\n")
    );

    // The statistics reply of the Community listener is built from the
    // request's correlation id and the counters, and nothing else.
    let listener = fs::read_to_string("src/services/system_notify.rs").expect("system_notify.rs");
    let production = listener.split("#[cfg(test)]").next().unwrap_or("");
    let reply = body_of(production, "handle_cache_stats_request");
    assert!(
        reply.contains("CacheStatsReplyPayload {")
            && reply.contains("correlation_id: req.correlation_id.clone(),")
            && reply.contains("layers: snapshot_layers(state),"),
        "the Community reply is the correlation id and the counters"
    );
    for forbidden in ["hostname::", "std::env::var", "env::var("] {
        assert!(
            !code(production).contains(forbidden),
            "system_notify.rs contains `{forbidden}`: the Community listener names no instance"
        );
    }
    // The extension that does is there, and says it allows several instances.
    if here("enterprise/src/clustering.rs") {
        let clustering = fs::read_to_string("enterprise/src/clustering.rs")
            .expect("enterprise/src/clustering.rs");
        assert!(clustering.contains("pub replica_id: String,"));
        assert!(code(body_of(&clustering, "allows_multiple_instances")).contains("true"));
    }
}

#[test]
fn the_replica_scanner_sees_what_it_forbids() {
    // The shapes the Community listener had before the move.
    for forbidden in [
        "    pub replica_id: String,",
        "        replica_id: replica_id.clone(),",
        "    let replica_id = std::env::var(\"BRUTOR_REPLICA_ID\")",
        "/// `replica_id` lets the control plane render \"N of M replicas responding\"",
        "pub struct ReplicaId(pub String);",
    ] {
        assert_eq!(
            replica_fan_out_in("src/services/system_notify.rs", forbidden).len(),
            1,
            "not seen: {forbidden}"
        );
    }
    // The audit chain's label is the one reader of the variable; anywhere
    // else it is seen, and the identifier is seen there too.
    let chain = "        match std::env::var(\"BRUTOR_REPLICA_ID\") {";
    assert!(replica_fan_out_in("src/services/audit_chain.rs", chain).is_empty());
    assert_eq!(replica_fan_out_in("src/services/other.rs", chain).len(), 1);
    assert_eq!(
        replica_fan_out_in("src/services/audit_chain.rs", "let replica_id = 1;").len(),
        1
    );
    // What the Community reply is made of is not flagged.
    assert!(
        replica_fan_out_in(
            "src/services/system_notify.rs",
            "    pub correlation_id: String,\n    pub layers: HashMap<String, CacheLayerStatsPayload>,"
        )
        .is_empty()
    );
}

// ── 2. one definition of the lease ───────────────────────────────────────

const LEASE_MODULE: &str = "src/services/instance_lease.rs";

/// What defines or overrides a lease in the Community sources given, other
/// than the one definition in the lease module.
fn second_lease_definitions(sources: &[(String, String)]) -> Vec<String> {
    let mut faults = Vec::new();
    let mut key_literals = 0;
    for (path, text) in sources {
        let production = code(text.split("#[cfg(test)]").next().unwrap_or(""));
        key_literals += production.matches("\"brutor:instance:").count();
        if path != LEASE_MODULE {
            for forbidden in ["brutor:instance:", "LeaseSettings {"] {
                if production.contains(forbidden) {
                    faults.push(format!("{path} contains `{forbidden}`"));
                }
            }
        }
        // The builder's seam for a test's own lease is defined in the
        // builder and called nowhere in the gateway.
        let calls = production.matches(".instance_lease(").count();
        if calls > 0 {
            faults.push(format!(
                "{path} calls .instance_lease(…): the binary takes the Community lease"
            ));
        }
    }
    if key_literals != 1 {
        faults.push(format!(
            "the lease key is written {key_literals} times in src/ (once: LEASE_KEY)"
        ));
    }
    faults
}

#[test]
fn the_lease_key_and_its_timings_have_one_definition() {
    let sources = rust_sources("src");
    let faults = second_lease_definitions(&sources);
    assert!(faults.is_empty(), "{}", faults.join("\n"));

    let lease = fs::read_to_string(LEASE_MODULE).expect("the lease module");
    for definition in [
        "pub const LEASE_KEY: &str = \"brutor:instance:core\";",
        "pub const LEASE_TTL: Duration = Duration::from_millis(15_000);",
        "pub const LEASE_RENEW_EVERY: Duration = Duration::from_secs(5);",
        "pub const START_WAIT: Duration = Duration::from_secs(20);",
        "pub const START_RETRY: Duration = Duration::from_secs(1);",
    ] {
        assert_eq!(
            lease.matches(definition).count(),
            1,
            "the design's value, defined once: {definition}"
        );
    }
    // The builder starts from that definition.
    let builder = fs::read_to_string("src/builder.rs").expect("builder.rs");
    assert!(code(body_of(&builder, "new")).contains("lease: LeaseSettings::community(),"));
    // And neither composition root overrides it.
    for main in ["src/main.rs", "enterprise/src/main.rs"]
        .into_iter()
        .filter(|m| here(m))
    {
        let text = fs::read_to_string(main).unwrap_or_else(|e| panic!("{main}: {e}"));
        assert!(!code(&text).contains("instance_lease"), "{main}");
    }
}

#[test]
fn the_lease_scanner_sees_a_second_definition() {
    let module = (
        LEASE_MODULE.to_string(),
        "pub const LEASE_KEY: &str = \"brutor:instance:core\";\n\
         fn community() -> LeaseSettings { LeaseSettings { key: LEASE_KEY.to_string() } }\n"
            .to_string(),
    );
    assert!(second_lease_definitions(std::slice::from_ref(&module)).is_empty());
    for (path, text, what) in [
        (
            "src/tasks.rs",
            "let key = \"brutor:instance:core\";",
            "the key written again",
        ),
        (
            "src/builder.rs",
            "let lease = LeaseSettings { key, ttl, renew_every, start_wait, start_retry };",
            "settings built outside the module",
        ),
        (
            "src/main.rs",
            "GatewayBuilder::new(config).instance_lease(custom).run().await",
            "the binary overrides the lease",
        ),
    ] {
        let faults =
            second_lease_definitions(&[module.clone(), (path.to_string(), text.to_string())]);
        assert!(!faults.is_empty(), "not seen: {what}");
    }
    // The key removed from the module is seen too.
    assert!(
        !second_lease_definitions(&[(LEASE_MODULE.to_string(), "// nothing".to_string())])
            .is_empty()
    );
    // A comment, and a test module, are not definitions.
    assert!(
        second_lease_definitions(&[
            module,
            (
                "src/x.rs".to_string(),
                "// the key is brutor:instance:core\n#[cfg(test)]\nmod tests { const K: &str = \
                 \"brutor:instance:core\"; }"
                    .to_string()
            )
        ])
        .is_empty()
    );
}

// ── 3. the builder's check ───────────────────────────────────────────────

/// What is wrong with where and how `build` makes the instance check; empty
/// when it is right.
fn instance_check_faults(builder: &str) -> Vec<String> {
    let mut faults = Vec::new();
    let production = builder.split("#[cfg(test)]").next().unwrap_or("");
    let build = code(body_of(production, "build"));

    let redis = build.find("connect_redis(");
    let asked = build.find(".any(|e| e.allows_multiple_instances())");
    let taken = build.find("InstanceLease::take(");
    let state = build.find("AppState::new(");
    let assembled = build.find("Self::assemble_checked(");
    match (redis, asked, taken, state, assembled) {
        (Some(r), Some(a), Some(t), Some(s), Some(g)) if r < a && a < t && t < s && s < g => {}
        _ => faults.push(
            "build does not connect Redis, ask the extensions whether several instances are \
             allowed, take the lease, and only then build the state and assemble"
                .to_string(),
        ),
    }
    if build.matches("InstanceLease::take(").count() != 1 {
        faults.push("build takes the lease in more than one place, or in none".to_string());
    }
    // The lease is taken exactly when no extension allows several
    // instances: the `else` of that question.
    if let (Some(a), Some(t)) = (asked, taken) {
        let between = &build[a..t];
        if !between.contains("None") || !between.contains("} else {") {
            faults.push(
                "the lease is not the alternative to a composition that allows several instances"
                    .to_string(),
            );
        }
    }
    // A refusal is the start's error.
    if let Some(t) = taken {
        let after = &build[t..];
        let arm = after.find("Err(refusal) => {");
        let returned = after.find("return Err(refusal.into());");
        match (arm, returned) {
            (Some(a), Some(r)) if a < r && r - a < 200 => {}
            _ => faults.push("a refused lease is not returned as the error".to_string()),
        }
    }
    if !build.contains("app_state.instance_lease = instance_lease;") {
        faults
            .push("the lease taken is not kept in the state (nothing would renew it)".to_string());
    }
    // Nothing else in the builder decides it: `assemble` and
    // `assemble_checked` take no lease, `run` only gives it back.
    for other in ["assemble", "assemble_checked", "admit"] {
        if code(body_of(production, other)).contains("InstanceLease::") {
            faults.push(format!("{other} touches the instance lease"));
        }
    }
    faults
}

#[test]
fn the_builder_makes_the_instance_check_after_redis_and_before_the_state() {
    let builder = fs::read_to_string("src/builder.rs").expect("builder.rs");
    let faults = instance_check_faults(&builder);
    assert!(faults.is_empty(), "{}", faults.join("\n"));

    // The renewal is a registered task that starts the lease's own loop.
    let tasks = fs::read_to_string("src/tasks.rs").expect("tasks.rs");
    let renewal = code(body_of(&tasks, "instance_lease_renewal"));
    assert!(renewal.contains("ctx.state.instance_lease.clone()"));
    assert!(renewal.contains("InstanceLease::spawn_renewal("));
    // A lease lost while serving stops nothing: the lease module cancels no
    // token and exits no process.
    let lease = fs::read_to_string(LEASE_MODULE).expect("the lease module");
    let production = code(lease.split("#[cfg(test)]").next().unwrap_or(""));
    for forbidden in [
        ".cancel()",
        "process::exit",
        "panic!(",
        "std::process::abort",
    ] {
        assert!(
            !production.contains(forbidden),
            "the lease module contains `{forbidden}`: a lost lease keeps the gateway serving"
        );
    }
}

#[test]
fn the_instance_check_scanner_sees_the_shapes_it_forbids() {
    let right = fs::read_to_string("src/builder.rs").expect("builder.rs");
    assert!(instance_check_faults(&right).is_empty());

    let take = "match InstanceLease::take(redis_pool.clone(), self.lease).await {";
    assert!(
        right.contains(take),
        "the scanner's own anchor is in builder.rs"
    );

    // The check gone.
    let gone = right.replace(take, "match Ok::<InstanceLease, TakeError>(lease) {");
    assert!(!instance_check_faults(&gone).is_empty(), "no lease taken");

    // Taken whatever the extensions say.
    let always = right.replace(".any(|e| e.allows_multiple_instances())", ".any(|_| false)");
    assert!(
        !instance_check_faults(&always).is_empty(),
        "extensions not asked"
    );

    // A refusal that is logged and passed over.
    let ignored = right.replace(
        "                    return Err(refusal.into());\n                }\n            }\n        };\n\n        // HTTP client",
        "                    None\n                }\n            }\n        };\n\n        // HTTP client",
    );
    assert_ne!(ignored, right, "the replacement applied");
    assert!(
        !instance_check_faults(&ignored).is_empty(),
        "refusal not returned"
    );

    // The lease not kept: nothing would renew it.
    let dropped = right.replace("app_state.instance_lease = instance_lease;", "");
    assert!(
        !instance_check_faults(&dropped).is_empty(),
        "lease not kept"
    );

    // The check after the state exists.
    let state_first = right
        .replacen("AppState::new(", "AppState::new_later(", 1)
        .replacen(
            "let redis_pool = connect_redis(",
            "let early = AppState::new(config.clone());\n        let redis_pool = connect_redis(",
            1,
        );
    assert!(
        !instance_check_faults(&state_first).is_empty(),
        "state first"
    );
}
