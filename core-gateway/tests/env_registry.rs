// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Guard: the environment-variable registry (`src/env_vars.rs`) is the only
//! way the Community gateway reads its environment, and everything written
//! about the variables comes from it.
//!
//! Held here:
//!
//! * the registry is sound; every name is `BRUTOR_*` or a conventional name
//!   on the keep list; none is an Enterprise variable;
//! * no running code under `src/` or `crates/*/src` reads the environment
//!   outside `src/env_registry.rs` (the developer benchmark tool apart);
//! * every registered variable is read by running code;
//! * the real binary, and `run-backfill`, refuse a malformed value and a
//!   missing required variable by name;
//! * neither a refusal, nor what a start logs, nor `--print-env` holds a
//!   value;
//! * `.env.example`, the README's configuration table and the
//!   getting-started guide's table of the required variables are what the
//!   registry generates (`UPDATE_ENV_DOCS=1 cargo test --test env_registry`
//!   rewrites them);
//! * no name of an Enterprise variable is in the Community registry, in a
//!   line of its code, in its binary or in what the registry generates;
//! * `src/env_registry.rs` is the worker services' module.
//!
//! The Enterprise workspace holds the same for its own registry
//! (`enterprise/tests/env_registry.rs`).

#[path = "support/env_guard.rs"]
mod env_guard;
#[path = "support/roots.rs"]
mod roots;

use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};

use brutor_gateway_core::env_registry::Edition;
use brutor_gateway_core::env_vars::{self, REGISTRY};
use env_guard::SENTINEL;

const BINARY: &str = env!("CARGO_BIN_EXE_brutor-gateway-core");
const BACKFILL: &str = env!("CARGO_BIN_EXE_run-backfill");

/// Names kept without the `BRUTOR_` prefix: conventional names
/// (the `JWT_*` family among them) and names that belong to other
/// software.
const KEPT: &[&str] = &[
    "DATABASE_URL",
    "REDIS_URL",
    "JWT_SECRET",
    "JWT_ALGORITHM",
    "JWT_EXPIRES_IN_SECS",
    "ENCRYPTION_KEY",
    "AWS_REGION",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_SERVICE_NAME",
    "RUST_LOG",
];

/// A complete, valid environment: with it the binary gets past its
/// environment check (and then fails to connect: nothing listens there).
/// Every value holds the sentinel, so that a value in anything the binary
/// writes is seen.
const VALID: &[(&str, &str)] = &[
    (
        "DATABASE_URL",
        "postgresql://envguard:value-that-must-never-be-printed@127.0.0.1:1/envguard",
    ),
    (
        "REDIS_URL",
        "redis://:value-that-must-never-be-printed@127.0.0.1:1/0",
    ),
    (
        "JWT_SECRET",
        "value-that-must-never-be-printed-jwt-0123456789",
    ),
    (
        "ENCRYPTION_KEY",
        "value-that-must-never-be-printed-enc-0123456789",
    ),
];

/// Running code of the Community workspace.
const SOURCE_DIRS: &[&str] = &["src", "crates"];

/// The registry module, and what else may read the environment, with the
/// reason.
const MAY_READ: &[(&str, &str)] = &[
    ("src/env_registry.rs", "the registry module"),
    (
        "src/bin/benchmark.rs",
        "a developer tool, not the gateway: its BENCHMARK_* variables are documented in \
         benches/README.md",
    ),
];

/// The files that define and load the variables: not readers.
const DEFINING: &[&str] = &["src/env_registry.rs", "src/env_vars.rs"];

/// The getting-started guide.
const GUIDE: &str = "docs/getting-started-guide.md";

fn root() -> PathBuf {
    roots::tree("community").expect("the Community tree")
}

fn allowed() -> Vec<&'static str> {
    MAY_READ.iter().map(|(path, _)| *path).collect()
}

/// The Agent Action Capsule export follows one company's individual
/// Internet-Draft whose format has changed twice: its setting says it is
/// experimental, and what that means, so every generated table says so too.
#[test]
fn the_aac_export_setting_says_it_is_experimental() {
    let description = env_vars::BRUTOR_EVIDENCE_PROFILE_AAC_ENABLED.description;
    assert!(
        description.starts_with("Experimental."),
        "BRUTOR_EVIDENCE_PROFILE_AAC_ENABLED: {description}"
    );
    assert!(
        description.contains("may change") && description.contains("compatibility promise"),
        "BRUTOR_EVIDENCE_PROFILE_AAC_ENABLED must say its format may change and that it is \
         not covered by the evidence compatibility promise: {description}"
    );
}

#[test]
fn the_registry_is_sound_and_its_names_follow_the_rule() {
    assert_eq!(REGISTRY.defects(), Vec::<String>::new());
    let stray: Vec<&str> = REGISTRY
        .vars
        .iter()
        .map(|var| var.name)
        .filter(|name| !name.starts_with(env_vars::PRODUCT_PREFIX) && !KEPT.contains(name))
        .collect();
    assert!(
        stray.is_empty(),
        "neither BRUTOR_* nor on the keep list: {stray:?}"
    );
    let stale: Vec<&&str> = KEPT
        .iter()
        .filter(|name| REGISTRY.find(name).is_none())
        .collect();
    assert!(stale.is_empty(), "kept and not registered: {stale:?}");

    // Every `pub static …: EnvVar` of src/env_vars.rs is in REGISTRY, once,
    // and the names written in the file are the registered names.
    let definitions = env_guard::definitions(&root().join("src/env_vars.rs"));
    assert_eq!(definitions.len(), REGISTRY.vars.len());
    let mut written = env_guard::written_names(&root().join("src/env_vars.rs"));
    let mut registered: Vec<String> = REGISTRY
        .vars
        .iter()
        .map(|var| var.name.to_string())
        .collect();
    written.sort_unstable();
    registered.sort_unstable();
    assert_eq!(written, registered);

    // A boolean ends in `_ENABLED`; a duration carries its unit, spelt one way.
    for var in REGISTRY.vars {
        if var.kind == brutor_gateway_core::env_registry::Kind::Boolean {
            assert!(var.name.ends_with("_ENABLED"), "{}", var.name);
        }
        assert!(!var.name.contains("_SECONDS"), "{}", var.name);
    }
}

#[test]
fn nothing_outside_the_registry_reads_the_environment() {
    let offences = env_guard::reads_outside(&root(), SOURCE_DIRS, &allowed());
    assert!(
        offences.is_empty(),
        "the environment is read outside src/env_registry.rs; read it through \
         env_registry::Env with a variable from src/env_vars.rs:\n{}",
        offences.join("\n")
    );
    // What may read it is there, and the developer tool still does: an
    // exemption nothing needs is removed.
    for (path, why) in MAY_READ {
        let text = fs::read_to_string(root().join(path))
            .unwrap_or_else(|e| panic!("{path} ({why}) is not there: {e}"));
        assert!(
            !env_guard::environment_reads(&env_guard::running_code(&text)).is_empty(),
            "{path} no longer reads the environment: remove it from MAY_READ"
        );
    }
}

#[test]
fn the_scan_sees_every_way_of_reading_the_environment() {
    env_guard::the_scan_is_proven();
}

#[test]
fn every_registered_variable_has_a_reader() {
    let definitions = env_guard::definitions(&root().join("src/env_vars.rs"));
    let unread = unread(&root(), SOURCE_DIRS, DEFINING, &definitions);
    assert!(
        unread.is_empty(),
        "registered in src/env_vars.rs and read nowhere as `env_vars::NAME`: {unread:?}"
    );
}

/// The backfill tool reads `DATABASE_URL` through the same registry: a
/// malformed value stops it before it connects.
#[test]
fn the_backfill_tool_refuses_a_malformed_value() {
    let mut env = VALID.to_vec();
    env.push(("BRUTOR_GATEWAY_DB_POOL_SIZE", SENTINEL));
    let output = env_guard::run(BACKFILL, &["--dry-run"], &env);
    let text = env_guard::stderr(&output);
    assert!(!output.status.success(), "a malformed value was accepted");
    assert!(
        text.contains("BRUTOR_GATEWAY_DB_POOL_SIZE is not valid"),
        "{text}"
    );
    assert!(!env_guard::written(&output).contains(SENTINEL), "{text}");
    // Without DATABASE_URL it says so, and nothing else is required of it.
    let output = env_guard::run(BACKFILL, &["--dry-run"], &[]);
    let text = env_guard::stderr(&output);
    assert!(!output.status.success());
    assert!(
        text.contains("DATABASE_URL is required and is not set") && !text.contains("JWT_SECRET"),
        "{text}"
    );
}

#[test]
fn a_refusal_names_the_variable_and_never_its_value() {
    env_guard::a_refusal_names_the_variable_and_never_its_value(BINARY, &REGISTRY, VALID);
}

/// A value the kind accepts and the gateway cannot use: refused at start,
/// by name. Each of these was replaced by a default, or by "not configured",
/// before.
#[test]
fn a_value_out_of_range_refuses_the_start() {
    for (name, bad) in [
        ("BRUTOR_GATEWAY_DB_POOL_SIZE", "0"),
        ("BRUTOR_MCP_TIMEOUT_SECS", "0"),
        ("BRUTOR_A2A_DELEGATION_MAX_AGE_SECS", "0"),
        ("BRUTOR_SKILL_RUNNER_SANDBOX_TIMEOUT_SECS", "0"),
        ("BRUTOR_SKILL_RUNNER_TRUSTED_TIMEOUT_SECS", "0"),
        ("BRUTOR_OUTBOUND_ALLOW_HOSTS", "never-printed:8080"),
        ("BRUTOR_EVIDENCE_HEAD_CADENCE_ENTRIES", "0"),
        ("BRUTOR_EVIDENCE_HEAD_CADENCE_SECS", "0"),
        ("JWT_EXPIRES_IN_SECS", "0"),
        ("BRUTOR_GATEWAY_DB_POOL_SIZE", "99999999999"),
        ("JWT_ALGORITHM", "RS256-never-printed"),
        ("BRUTOR_MEDIA_STORAGE_BACKEND", "gcs-never-printed"),
        ("BRUTOR_AUTONOMY_FALLBACK_FLOOR", "careful-never-printed"),
        ("BRUTOR_AUDIT_SIGNING_KEY", "not-hex-never-printed"),
        ("BRUTOR_EVIDENCE_SIGNING_KEY", "abcd"),
        ("BRUTOR_AUDIT_TRUSTED_KEYS", "abcd,never-printed"),
        ("BRUTOR_EVIDENCE_RETIRED_KEYS", "never-printed"),
        ("RUST_LOG", "a=b=never-printed"),
    ] {
        let mut env = VALID.to_vec();
        env.push((name, bad));
        let output = env_guard::run(BINARY, &[], &env);
        let text = env_guard::stderr(&output);
        assert!(!output.status.success(), "{name} accepted {bad:?}");
        assert!(
            text.contains(&format!("{name} is not valid")),
            "{name}: {text}"
        );
        assert!(!text.contains("never-printed"), "{name}: {text}");
    }
    // A placeholder secret is one too.
    for name in ["JWT_SECRET", "ENCRYPTION_KEY"] {
        let mut env: Vec<(&str, &str)> = VALID
            .iter()
            .copied()
            .filter(|(known, _)| *known != name)
            .collect();
        env.push((name, "your-secret-change-this-in-production-0123456789"));
        let output = env_guard::run(BINARY, &[], &env);
        let text = env_guard::stderr(&output);
        assert!(!output.status.success());
        assert!(text.contains(&format!("{name} is not valid")), "{text}");
        assert!(!text.contains("change-this"), "{text}");
    }
}

/// With a complete environment the binary goes on to start, and what it
/// writes on the way (it cannot connect: nothing listens there) holds no
/// value of any secret.
#[test]
fn a_start_logs_no_secret() {
    // The two keys must be keys to be accepted; theirs is not the sentinel.
    let key = "5e".repeat(32);
    let mut env: Vec<(&str, &str)> = VALID.to_vec();
    for var in REGISTRY.vars.iter().filter(|var| var.secret) {
        if !env.iter().any(|(name, _)| *name == var.name) {
            let value = if var.name.ends_with("_SIGNING_KEY") {
                key.as_str()
            } else {
                SENTINEL
            };
            env.push((var.name, value));
        }
    }
    // A product variable this gateway does not read is reported by name.
    env.push(("BRUTOR_ENVGUARD_NOT_A_VARIABLE", SENTINEL));
    let output = env_guard::run(BINARY, &[], &env);
    let text = env_guard::written(&output);
    assert!(!output.status.success(), "it started without a database");
    assert!(
        !text.contains("the environment is refused"),
        "the environment was refused: {text}"
    );
    assert!(!text.contains(SENTINEL), "a value was written: {text}");
    assert!(!text.contains(&key), "a key was written: {text}");
    assert!(
        text.contains("BRUTOR_ENVGUARD_NOT_A_VARIABLE") && text.contains("does not read them"),
        "the variable it does not read was not reported: {text}"
    );
}

#[test]
fn print_env_prints_the_registry_and_no_value() {
    env_guard::print_env_prints_the_registry_and_no_value(BINARY, &REGISTRY);
}

#[test]
fn env_example_is_generated_from_the_registry() {
    env_guard::env_example_is(&root(), &REGISTRY, &REGISTRY.env_example());
}

#[test]
fn the_readme_table_is_generated_from_the_registry() {
    env_guard::the_table_is_generated(&root(), "README.md", &REGISTRY);
}

/// The getting-started guide's table of the variables the gateway does not
/// start without is the registry's, and the guide exports each of them.
#[test]
fn the_guide_table_is_generated_from_the_registry() {
    let required: Vec<&'static brutor_gateway_core::env_registry::EnvVar> = REGISTRY
        .vars
        .iter()
        .copied()
        .filter(|var| var.required)
        .collect();
    assert!(!required.is_empty());
    let registry: &'static brutor_gateway_core::env_registry::Registry =
        Box::leak(Box::new(brutor_gateway_core::env_registry::Registry {
            service: REGISTRY.service,
            title: REGISTRY.title,
            vars: Box::leak(required.clone().into_boxed_slice()),
        }));
    env_guard::the_table_is_generated(&root(), GUIDE, registry);
    let guide = fs::read_to_string(root().join(GUIDE)).unwrap();
    for var in required {
        assert!(
            guide.contains(&format!("export {}=", var.name)),
            "{GUIDE} does not export {}",
            var.name
        );
    }
}

/// The Community gateway neither reads nor lists a variable of the
/// Enterprise Edition: not in its registry, not in a file of its tree, not
/// in its binary.
#[test]
fn no_enterprise_variable_is_in_the_community_gateway() {
    for var in REGISTRY.vars {
        assert_ne!(var.edition, Edition::Enterprise, "{}", var.name);
    }
    // The Enterprise names are the Enterprise workspace's to say. Without
    // it (a Community tree alone) there is none to look for.
    let Some(enterprise) = roots::tree("enterprise") else {
        eprintln!(
            "SKIPPED: no_enterprise_variable_is_in_the_community_gateway: the Enterprise \
             workspace is not here, so its variable names are not known"
        );
        return;
    };
    let names = env_guard::written_names_under(&enterprise, &["src"]);
    assert!(names.len() >= 10, "the Enterprise variables: {names:?}");
    assert!(
        names
            .iter()
            .all(|name| name.starts_with(env_vars::PRODUCT_PREFIX))
    );

    for name in &names {
        assert!(REGISTRY.find(name).is_none(), "{name} is registered here");
    }
    let generated = [
        REGISTRY.json_text(),
        REGISTRY.env_example(),
        REGISTRY.markdown(),
    ]
    .concat();
    let mut offences = Vec::new();
    for name in &names {
        if contains_name(&generated, name) {
            offences.push(format!("what the registry generates: {name}"));
        }
    }
    // The Community source: every line of code (prose in a comment may say
    // what an Enterprise variable is called; code may not).
    for (path, code) in env_guard::sources(&root(), SOURCE_DIRS) {
        for (index, line) in code.lines().enumerate() {
            for name in &names {
                if contains_name(line, name) {
                    offences.push(format!("{path}:{}: {name}", index + 1));
                }
            }
        }
    }
    // In the binary a name is looked for as it is, wherever it stands; so
    // none may be the beginning of a name the Community gateway does have.
    for name in &names {
        for var in REGISTRY.vars {
            assert!(
                !var.name.starts_with(name.as_str()),
                "{name} begins the Community variable {}",
                var.name
            );
        }
    }
    let binary = fs::read(BINARY).expect("the Community binary");
    for name in names_in_bytes(&binary, env_vars::PRODUCT_PREFIX, &names) {
        offences.push(format!("the Community binary holds the string {name}"));
    }
    assert!(
        offences.is_empty(),
        "names of Enterprise variables in the Community gateway:\n{}",
        offences.join("\n")
    );
    // The scan of the binary finds a name that is there, also where the
    // next literal follows it without a gap.
    let present = vec![env_vars::BRUTOR_GATEWAY_LISTEN_ADDR.name.to_string()];
    assert_eq!(
        names_in_bytes(&binary, env_vars::PRODUCT_PREFIX, &present),
        present
    );
    assert_eq!(
        names_in_bytes(
            b"allBRUTOR_GATEWAY_LISTEN_ADDRinstance_lease",
            env_vars::PRODUCT_PREFIX,
            &present
        ),
        present
    );
}

#[test]
fn the_registry_module_is_the_worker_services() {
    let siblings: Vec<(&str, PathBuf)> = ["skill-runner", "kb-uploader", "kb-connector-sync"]
        .into_iter()
        .filter_map(|name| roots::tree(name).map(|repository| (name, repository)))
        .collect();
    for line in the_module_is_the_workers(&root(), &siblings) {
        eprintln!("compared: {line}");
    }
}

// ── what only this guard needs ─────────────────────────────────────────────

/// The block of `src/env_registry.rs` the gateway added to the worker
/// services' module.
const EXTENSION_BEGIN: &str = "// ── extension:begin ";
const EXTENSION_END: &str = "// ── extension:end ";

/// The identifiers of `definitions` that no running code under `dirs` reads
/// as `env_vars::NAME`. The files that define and load the variables
/// (`defining`) are not readers.
fn unread(root: &Path, dirs: &[&str], defining: &[&str], definitions: &[String]) -> Vec<String> {
    let readers: String = env_guard::sources(root, dirs)
        .into_iter()
        .filter(|(path, _)| !defining.contains(&path.as_str()))
        .map(|(_, code)| code)
        .collect();
    definitions
        .iter()
        .filter(|ident| !contains_name(&readers, &format!("env_vars::{ident}")))
        .cloned()
        .collect()
}

/// `module` without the block the gateway added.
fn without_extension(module: &str) -> Option<String> {
    let begin = module.find(EXTENSION_BEGIN)?;
    let end = module.find(EXTENSION_END)?;
    let after = module[end..].find('\n').map(|at| end + at + 1)?;
    // The blank line that follows the block goes with it.
    let after = if module[after..].starts_with('\n') {
        after + 1
    } else {
        after
    };
    Some(format!("{}{}", &module[..begin], &module[after..]))
}

/// The module at `root`/src/env_registry.rs is the worker services' module:
/// byte for byte, or byte for byte without the block the gateway added
/// (until they take it over). `siblings` are the worker repositories that
/// are checked out, by name (`roots::tree` says which are, and records each
/// one that is not). What was compared, one line per sibling.
fn the_module_is_the_workers(root: &Path, siblings: &[(&str, PathBuf)]) -> Vec<String> {
    let mine = fs::read_to_string(root.join("src/env_registry.rs")).unwrap();
    let plain = without_extension(&mine).expect("the module marks the block the gateway added");
    assert!(
        mine.len() > plain.len() && !plain.contains("unregistered_in_process"),
        "the block the gateway added is not what the markers enclose"
    );
    let mut compared = Vec::new();
    for (sibling, repository) in siblings {
        let path = repository.join("src/env_registry.rs");
        let theirs = fs::read_to_string(&path)
            .unwrap_or_else(|e| panic!("{sibling} has no src/env_registry.rs: {e}"));
        assert!(
            theirs == mine || theirs == plain,
            "src/env_registry.rs differs from {sibling}'s copy outside the block between \
             `{EXTENSION_BEGIN}` and `{EXTENSION_END}`"
        );
        compared.push(format!(
            "{sibling}: {}",
            if theirs == mine {
                "identical"
            } else {
                "identical but for the gateway's block, which it has not taken over"
            }
        ));
    }
    compared
}

/// `needle` as a whole name: not a part of a longer one.
fn contains_name(text: &str, needle: &str) -> bool {
    text.match_indices(needle).any(|(at, _)| {
        let before = text[..at].chars().next_back();
        let after = text[at + needle.len()..].chars().next();
        !before.is_some_and(env_guard::is_name_char) && !after.is_some_and(env_guard::is_name_char)
    })
}

/// Those of `names` that are in `haystack`. Not as whole names: a compiler
/// packs string literals one against the next, so a name in a binary is
/// followed by whatever literal comes after it. One pass: every name starts
/// with `prefix`.
fn names_in_bytes(haystack: &[u8], prefix: &str, names: &[String]) -> Vec<String> {
    assert!(names.iter().all(|name| name.starts_with(prefix)));
    let prefix = prefix.as_bytes();
    let first = prefix[0];
    let mut found = BTreeSet::new();
    let mut from = 0;
    while let Some(offset) = haystack[from..].iter().position(|b| *b == first) {
        let at = from + offset;
        from = at + 1;
        if !haystack[at..].starts_with(prefix) {
            continue;
        }
        for name in names {
            if haystack[at..].starts_with(name.as_bytes()) {
                found.insert(name.clone());
            }
        }
    }
    found.into_iter().collect()
}
