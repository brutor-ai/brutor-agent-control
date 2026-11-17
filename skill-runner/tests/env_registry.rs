// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The environment-variable registry is the only way this service reads its
//! environment, and everything written about the variables comes from it.
//!
//! Held here:
//!
//! * the registry is sound, and every name is `BRUTOR_*` or a conventional
//!   name on the keep list;
//! * no file under `src/` other than `src/env_registry.rs` reads the
//!   environment;
//! * every registered variable is read by running code;
//! * the binary refuses a malformed value or a missing required variable,
//!   naming it and never printing its value;
//! * `.env.example` and the README's configuration table are what the
//!   registry generates (`UPDATE_ENV_DOCS=1 cargo test --test env_registry`
//!   rewrites them);
//! * `--print-env` prints the registry as JSON, and neither it nor a refusal
//!   prints a value.
//!
//! Everything below the "service" section is the same in the three worker
//! services.

use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};
use std::time::{Duration, Instant};

#[allow(dead_code)]
#[path = "../src/env_registry.rs"]
mod env_registry;
#[allow(dead_code)]
#[path = "../src/env_vars.rs"]
mod env_vars;

use env_vars::REGISTRY;

// ── service ────────────────────────────────────────────────────────────────

const BINARY: &str = env!("CARGO_BIN_EXE_brutor-skill-runner");

/// Names kept without the `BRUTOR_` prefix: names operators already know
/// from other software, and names that belong to the platform or to other
/// software. Prefixing those would make them harder to find, not easier.
const KEPT: &[&str] = &["RUST_LOG", "TMPDIR"];

/// A complete, valid environment: with it the binary gets past its
/// environment check.
const VALID: &[(&str, &str)] = &[(
    "BRUTOR_SKILL_RUNNER_JWT_SECRET",
    "value-that-must-never-be-printed-0123456789",
)];

// ── shared ─────────────────────────────────────────────────────────────────

const SENTINEL: &str = "value-that-must-never-be-printed";
/// How long the binary gets to refuse its environment and exit.
const START_DEADLINE: Duration = Duration::from_secs(15);
const TABLE_BEGIN: &str = "<!-- env-registry:begin (generated from src/env_vars.rs) -->";
const TABLE_END: &str = "<!-- env-registry:end -->";

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

fn files_under(path: &Path, out: &mut Vec<PathBuf>) {
    if path.is_dir() {
        let Ok(entries) = fs::read_dir(path) else {
            return;
        };
        for entry in entries.flatten() {
            files_under(&entry.path(), out);
        }
    } else if path.is_file() {
        out.push(path.to_path_buf());
    }
}

fn relative(path: &Path) -> String {
    path.strip_prefix(root())
        .unwrap_or(path)
        .to_string_lossy()
        .replace('\\', "/")
}

/// `src` with every `#[cfg(test)]` item removed: from the attribute to the
/// end of the brace-balanced block (or `;`) that follows it.
fn without_test_items(src: &str) -> String {
    let mut out = String::with_capacity(src.len());
    let mut rest = src;
    while let Some(at) = rest.find("#[cfg(test)]") {
        out.push_str(&rest[..at]);
        let after = &rest[at..];
        let semi = after.find(';').unwrap_or(after.len());
        let Some(open) = after.find('{').filter(|o| *o < semi) else {
            rest = &after[(semi + 1).min(after.len())..];
            continue;
        };
        let mut depth = 0usize;
        let mut end = after.len();
        for (i, c) in after[open..].char_indices() {
            match c {
                '{' => depth += 1,
                '}' => {
                    depth -= 1;
                    if depth == 0 {
                        end = open + i + 1;
                        break;
                    }
                }
                _ => {}
            }
        }
        rest = &after[end..];
    }
    out.push_str(rest);
    out
}

/// Running code only: no test items, no comments.
fn running_code(src: &str) -> String {
    without_test_items(src)
        .lines()
        .map(|line| line.split("//").next().unwrap_or(""))
        .collect::<Vec<_>>()
        .join("\n")
}

/// `(path relative to the root, running code)` of every file under `src/`.
fn sources() -> Vec<(String, String)> {
    let mut files = Vec::new();
    files_under(&root().join("src"), &mut files);
    files
        .into_iter()
        .filter(|f| f.extension().and_then(|e| e.to_str()) == Some("rs"))
        .map(|f| (relative(&f), running_code(&fs::read_to_string(&f).unwrap())))
        .collect()
}

fn is_name_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || c == '_'
}

/// `needle` as a whole name: not a part of a longer one.
fn contains_name(text: &str, needle: &str) -> bool {
    text.match_indices(needle).any(|(at, _)| {
        let before = text[..at].chars().next_back();
        let after = text[at + needle.len()..].chars().next();
        !before.is_some_and(is_name_char) && !after.is_some_and(is_name_char)
    })
}

/// Ways of reading the environment found in `code`.
fn environment_reads(code: &str) -> Vec<String> {
    let mut found = Vec::new();
    for (index, line) in code.lines().enumerate() {
        let mut offences = Vec::new();
        for (at, _) in line.match_indices("env::") {
            let before = line[..at].chars().next_back();
            if before.is_some_and(is_name_char) {
                continue; // a longer name that ends in `env`
            }
            if !line[at + "env::".len()..].starts_with("args") {
                offences.push("std::env");
            }
        }
        for pattern in [
            "use std::env;",
            "option_env!",
            "from_default_env",
            "from_env_lossy",
            "EnvFilter::from_env",
            "EnvFilter::try_from_env",
        ] {
            if line.contains(pattern) {
                offences.push(pattern);
            }
        }
        for (at, _) in line.match_indices("dotenvy::") {
            if !line[at..].starts_with("dotenvy::dotenv()") {
                offences.push("dotenvy");
            }
        }
        for offence in offences {
            found.push(format!("line {}: {offence}: {}", index + 1, line.trim()));
        }
    }
    found
}

/// The binary, started in an empty directory (no `.env` to pick up) with
/// exactly `env`, and what it wrote when it exited.
///
/// # Panics
/// When it is still running after [`START_DEADLINE`]: it accepted the
/// environment and went on to serve. Every call here expects a refusal or an
/// immediate exit, so that is a failure, not something to wait for.
fn run(args: &[&str], env: &[(&str, &str)]) -> Output {
    let dir = std::env::temp_dir().join(format!(
        "envreg-{}-{}",
        REGISTRY.service,
        std::process::id()
    ));
    fs::create_dir_all(&dir).unwrap();
    let mut child = Command::new(BINARY)
        .args(args)
        .current_dir(&dir)
        .env_clear()
        .envs(env.iter().copied())
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("the binary starts");
    let deadline = Instant::now() + START_DEADLINE;
    loop {
        match child.try_wait().expect("the binary can be waited for") {
            Some(_) => break,
            None if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(20)),
            None => {
                let _ = child.kill();
                let _ = child.wait();
                let names: Vec<&str> = env.iter().map(|(name, _)| *name).collect();
                panic!(
                    "the binary was still running after {START_DEADLINE:?}: it accepted an \
                     environment it should have refused (variables set: {names:?})"
                );
            }
        }
    }
    child.wait_with_output().expect("the binary's output")
}

fn stderr(output: &Output) -> String {
    String::from_utf8_lossy(&output.stderr).into_owned()
}

/// `static NAME: EnvVar` and `static NAME: &[&EnvVar]` in `src/env_vars.rs`:
/// the variables' identifiers, and the groups with their members.
fn definitions() -> (Vec<String>, Vec<(String, Vec<String>)>) {
    let text = fs::read_to_string(root().join("src/env_vars.rs")).unwrap();
    let mut vars = Vec::new();
    let mut groups = Vec::new();
    let mut lines = text.lines();
    while let Some(line) = lines.next() {
        let Some(rest) = line.strip_prefix("pub static ") else {
            continue;
        };
        let Some((ident, ty)) = rest.split_once(':') else {
            continue;
        };
        let ty = ty.trim();
        if ty.starts_with("EnvVar =") {
            vars.push(ident.to_string());
        } else if ty.starts_with("&[&EnvVar] =") {
            // The members are on the same line, or on the lines up to `];`.
            let mut body = ty
                .split_once("&[&EnvVar] =")
                .map_or("", |(_, b)| b)
                .to_string();
            if !body.contains("];") {
                for more in lines.by_ref() {
                    body.push_str(more);
                    if more.trim_start().starts_with("];") {
                        break;
                    }
                }
            }
            let members: Vec<String> = body
                .trim()
                .trim_start_matches("&[")
                .split(',')
                .map(|m| {
                    m.trim()
                        .trim_end_matches("];")
                        .trim_start_matches('&')
                        .to_string()
                })
                .filter(|m| !m.is_empty())
                .collect();
            groups.push((ident.to_string(), members));
        }
    }
    (vars, groups)
}

#[test]
fn the_registry_is_sound_and_its_names_follow_the_rule() {
    assert_eq!(REGISTRY.defects(), Vec::<String>::new());
    let stray: Vec<&str> = REGISTRY
        .vars
        .iter()
        .map(|var| var.name)
        .filter(|name| !name.starts_with("BRUTOR_") && !KEPT.contains(name))
        .collect();
    assert!(
        stray.is_empty(),
        "neither BRUTOR_* nor on the keep list: {stray:?}"
    );
    let (idents, _) = definitions();
    assert_eq!(
        idents.len(),
        REGISTRY.vars.len(),
        "every `pub static …: EnvVar` in src/env_vars.rs is in REGISTRY, once"
    );
}

#[test]
fn nothing_outside_the_registry_reads_the_environment() {
    let mut offences = Vec::new();
    for (path, code) in sources() {
        if path == "src/env_registry.rs" {
            continue;
        }
        for read in environment_reads(&code) {
            offences.push(format!("{path}: {read}"));
        }
    }
    assert!(
        offences.is_empty(),
        "the environment is read outside src/env_registry.rs; read it through \
         env_registry::Env with a variable from src/env_vars.rs:\n{}",
        offences.join("\n")
    );
}

#[test]
fn the_guard_sees_every_way_of_reading_the_environment() {
    for read in [
        "let v = std::env::var(\"X\");",
        "for (k, v) in env::vars() {}",
        "if let Some(v) = std::env::var_os(key) {}",
        "let dir = std::env::temp_dir();",
        "use std::env;",
        "let v = option_env!(\"X\");",
        "EnvFilter::try_from_default_env()",
        "EnvFilter::from_default_env()",
        "let v = dotenvy::var(\"X\");",
    ] {
        assert!(!environment_reads(read).is_empty(), "missed: {read}");
    }
    for fine in [
        "let _ = dotenvy::dotenv();",
        "std::env::args().skip(1)",
        "\"version\": env!(\"CARGO_PKG_VERSION\"),",
        "crate::env_vars::process_env::get()",
    ] {
        assert!(
            environment_reads(&running_code(fine)).is_empty(),
            "wrongly refused: {fine}"
        );
    }
    let test_only =
        "fn f() {}\n#[cfg(test)]\nmod tests {\n    fn g() { std::env::var(\"X\"); }\n}\n";
    assert!(environment_reads(&running_code(test_only)).is_empty());
    assert!(environment_reads(&running_code("// std::env::var is not used")).is_empty());
}

#[test]
fn the_definitions_are_read_as_they_are_written() {
    let (idents, groups) = definitions();
    // One identifier per registered variable, and the names written in the
    // file are the registered names.
    assert_eq!(idents.len(), REGISTRY.vars.len());
    let text = fs::read_to_string(root().join("src/env_vars.rs")).unwrap();
    let mut written: Vec<&str> = text
        .lines()
        .filter_map(|line| line.strip_prefix("    name: \""))
        .filter_map(|rest| rest.strip_suffix("\","))
        .collect();
    let mut registered: Vec<&str> = REGISTRY.vars.iter().map(|var| var.name).collect();
    written.sort_unstable();
    registered.sort_unstable();
    assert_eq!(written, registered);
    // A group holds only variables, and no group swallows the registry.
    for (group, members) in &groups {
        assert!(
            !members.is_empty() && members.len() < idents.len(),
            "{group}: {members:?}"
        );
        for member in members {
            assert!(
                idents.contains(member),
                "{group}: {member} is not a variable"
            );
        }
    }
}

#[test]
fn every_registered_variable_has_a_reader() {
    let (idents, groups) = definitions();
    let readers: String = sources()
        .into_iter()
        .filter(|(path, _)| path != "src/env_registry.rs" && path != "src/env_vars.rs")
        .map(|(_, code)| code)
        .collect();
    let read = |ident: &str| contains_name(&readers, &format!("env_vars::{ident}"));
    let unread: Vec<&String> = idents
        .iter()
        .filter(|ident| {
            !read(ident)
                && !groups
                    .iter()
                    .any(|(group, members)| members.contains(ident) && read(group))
        })
        .collect();
    assert!(
        unread.is_empty(),
        "registered in src/env_vars.rs and read nowhere (as `env_vars::NAME`, or through a \
         group that is): {unread:?}"
    );
}

#[test]
fn a_refusal_names_the_variable_and_never_its_value() {
    // Nothing set: every required variable is named.
    let output = run(&[], &[]);
    let text = stderr(&output);
    assert!(!output.status.success());
    for var in REGISTRY.vars.iter().filter(|var| var.required) {
        assert!(
            text.contains(&format!("{} is required and is not set", var.name)),
            "{text}"
        );
    }
    // Every variable whose kind can be wrong, set to something wrong.
    for var in REGISTRY.vars {
        if !matches!(
            var.kind,
            env_registry::Kind::Integer
                | env_registry::Kind::Boolean
                | env_registry::Kind::Url
                | env_registry::Kind::SocketAddr
        ) {
            continue;
        }
        let mut env: Vec<(&str, &str)> = VALID
            .iter()
            .copied()
            .filter(|(name, _)| *name != var.name)
            .collect();
        env.push((var.name, SENTINEL));
        let output = run(&[], &env);
        let text = stderr(&output);
        assert!(!output.status.success(), "{} accepted garbage", var.name);
        assert!(
            text.contains(&format!("{} is not valid", var.name)),
            "{}: {text}",
            var.name
        );
        assert!(!text.contains(SENTINEL), "{}: {text}", var.name);
    }
}

#[test]
fn print_env_prints_the_registry_and_no_value() {
    // Every variable set, the secrets included: none of it is in the output.
    let env: Vec<(&str, &str)> = REGISTRY
        .vars
        .iter()
        .map(|var| (var.name, SENTINEL))
        .collect();
    let output = run(&[env_registry::PRINT_FLAG], &env);
    assert!(output.status.success(), "{}", stderr(&output));
    let printed = String::from_utf8(output.stdout).unwrap();
    assert_eq!(printed, REGISTRY.json_text());
    assert!(!printed.contains(SENTINEL));

    // It needs nothing set.
    let output = run(&[env_registry::PRINT_FLAG], &[]);
    assert!(output.status.success(), "{}", stderr(&output));

    let json: serde_json::Value = serde_json::from_str(&printed).unwrap();
    assert_eq!(json["schema_version"], env_registry::SCHEMA_VERSION);
    assert_eq!(json["service"], REGISTRY.service);
    let variables = json["variables"].as_array().unwrap();
    assert_eq!(variables.len(), REGISTRY.vars.len());
    for variable in variables {
        let object = variable.as_object().unwrap();
        let keys: Vec<&str> = object.keys().map(String::as_str).collect();
        assert_eq!(
            keys,
            [
                "accepted",
                "default",
                "description",
                "edition",
                "example",
                "name",
                "required",
                "secret",
                "type"
            ]
        );
        if object["secret"] == true {
            assert!(object["default"].is_null(), "{variable}");
        }
    }
    assert!(json.get("removed").is_none());
}

/// `actual` must equal `expected`; with `UPDATE_ENV_DOCS=1` the file is
/// rewritten instead.
fn hold(path: &Path, actual: &str, expected: &str) {
    if actual == expected {
        return;
    }
    if std::env::var_os("UPDATE_ENV_DOCS").is_some() {
        fs::write(path, expected).unwrap();
        return;
    }
    panic!(
        "{} is not what the registry generates. Regenerate it:\n  \
         UPDATE_ENV_DOCS=1 cargo test --test env_registry\n\
         --- generated ---\n{expected}\n--- on disk ---\n{actual}",
        relative(path)
    );
}

#[test]
fn env_example_is_generated_from_the_registry() {
    let path = root().join(".env.example");
    let actual = fs::read_to_string(&path).unwrap_or_default();
    hold(&path, &actual, &REGISTRY.env_example());
    let actual = fs::read_to_string(&path).unwrap();

    // What the template sets is exactly what the service cannot start
    // without.
    let set: BTreeSet<&str> = actual
        .lines()
        .filter(|line| !line.starts_with('#'))
        .filter_map(|line| line.split_once('=').map(|(name, _)| name))
        .collect();
    let required: BTreeSet<&str> = REGISTRY
        .vars
        .iter()
        .filter(|var| var.required)
        .map(|var| var.name)
        .collect();
    assert_eq!(set, required);
}

#[test]
fn the_readme_table_is_generated_from_the_registry() {
    let path = root().join("README.md");
    let actual = fs::read_to_string(&path).unwrap();
    let (Some(begin), Some(end)) = (actual.find(TABLE_BEGIN), actual.find(TABLE_END)) else {
        panic!("README.md has no `{TABLE_BEGIN}` … `{TABLE_END}` section");
    };
    let expected = format!(
        "{}{TABLE_BEGIN}\n\n{}\n{}",
        &actual[..begin],
        REGISTRY.markdown(),
        &actual[end..]
    );
    hold(&path, &actual, &expected);
}

#[test]
fn the_registry_module_is_the_same_in_the_three_services() {
    let mine = fs::read_to_string(root().join("src/env_registry.rs")).unwrap();
    for sibling in [
        "brutor-skill-runner",
        "brutor-gateway-kb-uploader",
        "brutor-kb-connector-sync",
    ] {
        let path = root().join("..").join(sibling).join("src/env_registry.rs");
        match fs::read_to_string(&path) {
            Ok(theirs) => assert!(
                mine == theirs,
                "src/env_registry.rs differs from {sibling}'s copy"
            ),
            // A checkout of this repository alone (CI) has no siblings.
            Err(_) => eprintln!("SKIPPED: {sibling} is not checked out beside this repository"),
        }
    }
}
