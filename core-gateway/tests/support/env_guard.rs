// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! What the environment-registry guards of both workspaces are made of
//! (`tests/env_registry.rs` here, `enterprise/tests/env_registry.rs`): the
//! source scans, the runs of the real binary and the generated files. Both
//! use all of it; what only one of them needs is in that guard's own file. The
//! scans are the ones of the worker services' `tests/env_registry.rs`
//! (`brutor-skill-runner` and its two siblings), so that "reads the
//! environment" means the same thing in every service.

use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};
use std::time::{Duration, Instant};

use brutor_gateway_core::env_registry::{self, Registry};

pub const SENTINEL: &str = "value-that-must-never-be-printed";
/// How long the binary gets to refuse its environment and exit.
pub const START_DEADLINE: Duration = Duration::from_secs(30);
pub const TABLE_BEGIN: &str = "<!-- env-registry:begin (generated from src/env_vars.rs) -->";
pub const TABLE_END: &str = "<!-- env-registry:end -->";
pub fn files_under(path: &Path, out: &mut Vec<PathBuf>) {
    if path.is_dir() {
        let Ok(entries) = fs::read_dir(path) else {
            return;
        };
        for entry in entries.flatten() {
            let child = entry.path();
            // Build output is not source.
            if child.file_name().is_some_and(|name| name == "target") {
                continue;
            }
            files_under(&child, out);
        }
    } else if path.is_file() {
        out.push(path.to_path_buf());
    }
}

pub fn relative(root: &Path, path: &Path) -> String {
    path.strip_prefix(root)
        .unwrap_or(path)
        .to_string_lossy()
        .replace('\\', "/")
}

/// `src` with every item under `attribute` removed: from the attribute to
/// the end of the brace-balanced block (or `;`) that follows it.
fn without_items_under(src: &str, attribute: &str) -> String {
    let mut out = String::with_capacity(src.len());
    let mut rest = src;
    while let Some(at) = rest.find(attribute) {
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
pub fn running_code(src: &str) -> String {
    without_items_under(src, "#[cfg(test)]")
        .lines()
        .map(|line| line.split("//").next().unwrap_or(""))
        .collect::<Vec<_>>()
        .join("\n")
}

/// The files a `#[cfg(test)] mod name;` of `src` declares, relative to the
/// directory of the file that declares them: test code in a file of its own.
fn test_only_modules(src: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut rest = src;
    while let Some(at) = rest.find("#[cfg(test)]") {
        let after = rest[at + "#[cfg(test)]".len()..].trim_start();
        if let Some(declared) = after
            .strip_prefix("pub mod ")
            .or_else(|| after.strip_prefix("mod "))
        {
            let name: String = declared
                .chars()
                .take_while(|c| c.is_ascii_alphanumeric() || *c == '_')
                .collect();
            if declared[name.len()..].trim_start().starts_with(';') {
                out.push(name);
            }
        }
        rest = &rest[at + "#[cfg(test)]".len()..];
    }
    out
}

/// `(path relative to `root`, running code)` of every Rust file under
/// `dirs`, without the files that are test modules.
pub fn sources(root: &Path, dirs: &[&str]) -> Vec<(String, String)> {
    let mut files = Vec::new();
    for dir in dirs {
        files_under(&root.join(dir), &mut files);
    }
    let files: Vec<PathBuf> = files
        .into_iter()
        .filter(|f| f.extension().and_then(|e| e.to_str()) == Some("rs"))
        // A crate's own integration tests and benches are not running code.
        .filter(|f| {
            let path = relative(root, f);
            !path.contains("/tests/") && !path.contains("/benches/")
        })
        .collect();
    let mut test_only = BTreeSet::new();
    for file in &files {
        let Ok(text) = fs::read_to_string(file) else {
            continue;
        };
        let dir = file.parent().unwrap_or(root);
        let stem = file.file_stem().and_then(|s| s.to_str()).unwrap_or("");
        for module in test_only_modules(&text) {
            for candidate in [
                dir.join(format!("{module}.rs")),
                dir.join(&module).join("mod.rs"),
                dir.join(stem).join(format!("{module}.rs")),
                dir.join(stem).join(&module).join("mod.rs"),
            ] {
                test_only.insert(candidate);
            }
        }
    }
    files
        .into_iter()
        .filter(|f| !test_only.contains(f))
        .map(|f| {
            (
                relative(root, &f),
                running_code(&fs::read_to_string(&f).unwrap()),
            )
        })
        .collect()
}

pub fn is_name_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || c == '_'
}

/// Ways of reading the environment found in `code`.
pub fn environment_reads(code: &str) -> Vec<String> {
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

/// The scan sees every way of reading the environment, and nothing else.
pub fn the_scan_is_proven() {
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
        "dotenvy::dotenv().ok();",
        "std::env::args().skip(1)",
        "\"version\": env!(\"CARGO_PKG_VERSION\"),",
        "crate::env_vars::process().flag(&crate::env_vars::X)",
        "let env = brutor_gateway_core::env_vars::load_partial()?;",
        "brutor_gateway_core::env_vars::process_with(ENV_VARS)",
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
    assert_eq!(
        test_only_modules("#[cfg(test)]\nmod a_tests;\n#[cfg(test)]\nmod inline { }\nmod b;\n"),
        ["a_tests"]
    );
    // A read hands the variable to an accessor; a definition, a name and a
    // list of variables do not.
    let defined = "pub static BRUTOR_X: EnvVar = EnvVar {\n    name: \"BRUTOR_X\",\n};\n\
                   static ENV_VARS: &[&EnvVar] = &[&BRUTOR_X, &other::BRUTOR_Y];\n";
    assert!(!is_read(defined, "BRUTOR_X") && !is_read(defined, "BRUTOR_Y"));
    assert!(is_read("let on = env.flag(&BRUTOR_X);", "BRUTOR_X"));
    assert!(is_read("positive(env, &BRUTOR_X)?", "BRUTOR_X"));
    assert!(is_read("env.string(&crate::other::BRUTOR_Y)", "BRUTOR_Y"));
    assert!(!is_read("env.flag(&BRUTOR_X_ENABLED)", "BRUTOR_X"));
}

/// `path: line: what` for every read of the environment in the running code
/// under `dirs`, outside the files in `allowed`.
pub fn reads_outside(root: &Path, dirs: &[&str], allowed: &[&str]) -> Vec<String> {
    let mut offences = Vec::new();
    for (path, code) in sources(root, dirs) {
        if allowed.contains(&path.as_str()) {
            continue;
        }
        for read in environment_reads(&code) {
            offences.push(format!("{path}: {read}"));
        }
    }
    offences
}

/// `pub static NAME: EnvVar` in the file at `path`: the variables'
/// identifiers, in order.
pub fn definitions(path: &Path) -> Vec<String> {
    let text = fs::read_to_string(path).unwrap();
    text.lines()
        .filter_map(|line| line.strip_prefix("pub static "))
        .filter_map(|rest| rest.split_once(':'))
        .filter(|(_, ty)| ty.trim().starts_with("EnvVar ="))
        .map(|(ident, _)| ident.to_string())
        .collect()
}

/// The names the variables defined in the file at `path` are written with:
/// the `name: "…",` line that opens each `pub static …: EnvVar = EnvVar {`.
pub fn written_names(path: &Path) -> Vec<String> {
    let text = fs::read_to_string(path).unwrap();
    let mut names = Vec::new();
    let mut lines = text.lines();
    while let Some(line) = lines.next() {
        let defines = line
            .strip_prefix("pub static ")
            .and_then(|rest| rest.split_once(':'))
            .is_some_and(|(_, ty)| ty.trim().starts_with("EnvVar ="));
        if !defines {
            continue;
        }
        let name = lines
            .next()
            .and_then(|next| next.strip_prefix("    name: \""))
            .and_then(|rest| rest.strip_suffix("\","))
            .unwrap_or_else(|| panic!("{}: `{line}` is not followed by its name", path.display()));
        names.push(name.to_string());
    }
    names
}

/// The `name: "…",` lines of the Rust files under `dirs` that define a
/// variable.
pub fn written_names_under(root: &Path, dirs: &[&str]) -> Vec<String> {
    let mut out = Vec::new();
    for (path, _) in sources(root, dirs) {
        if !definitions(&root.join(&path)).is_empty() {
            out.extend(written_names(&root.join(&path)));
        }
    }
    out
}

/// Whether `code` hands the variable `ident` to a reader: `&NAME)`, the
/// last argument of `env.string(&NAME)`, `env.flag(&NAME)`,
/// `positive(env, &NAME)` and the rest of `Env`'s accessors. Its
/// definition, its `name: "…"` and its place in a list of variables
/// (`&NAME,`) are not reads.
pub fn is_read(code: &str, ident: &str) -> bool {
    code.contains(&format!("&{ident})")) || code.contains(&format!("::{ident})"))
}

/// `binary`, started in an empty directory (no `.env` to pick up) with
/// exactly `env`, and what it wrote when it exited.
///
/// # Panics
/// When it is still running after [`START_DEADLINE`]: it accepted the
/// environment and went on to serve. Every call here expects a refusal or an
/// immediate exit, so that is a failure, not something to wait for.
pub fn run(binary: &str, args: &[&str], env: &[(&str, &str)]) -> Output {
    static NEXT: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
    let dir = std::env::temp_dir().join(format!(
        "envreg-gateway-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed)
    ));
    fs::create_dir_all(&dir).unwrap();
    let mut child = Command::new(binary)
        .args(args)
        .current_dir(&dir)
        .env_clear()
        .envs(env.iter().copied())
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("the binary starts");
    // Drained while it runs: a full pipe would stop a binary that logs.
    let mut stdout = child.stdout.take().expect("stdout");
    let mut stderr = child.stderr.take().expect("stderr");
    let out = std::thread::spawn(move || {
        let mut buffer = Vec::new();
        let _ = std::io::Read::read_to_end(&mut stdout, &mut buffer);
        buffer
    });
    let err = std::thread::spawn(move || {
        let mut buffer = Vec::new();
        let _ = std::io::Read::read_to_end(&mut stderr, &mut buffer);
        buffer
    });
    let deadline = Instant::now() + START_DEADLINE;
    let status = loop {
        match child.try_wait().expect("the binary can be waited for") {
            Some(status) => break status,
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
    };
    let _ = fs::remove_dir(&dir);
    Output {
        status,
        stdout: out.join().unwrap_or_default(),
        stderr: err.join().unwrap_or_default(),
    }
}

pub fn stderr(output: &Output) -> String {
    String::from_utf8_lossy(&output.stderr).into_owned()
}

/// Everything the binary wrote.
pub fn written(output: &Output) -> String {
    format!(
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    )
}

/// A refusal names the variable and never its value: the required ones when
/// nothing is set, and every variable whose kind can be wrong, set wrong.
pub fn a_refusal_names_the_variable_and_never_its_value(
    binary: &str,
    registry: &Registry,
    valid: &[(&str, &str)],
) {
    let output = run(binary, &[], &[]);
    let text = stderr(&output);
    assert!(!output.status.success());
    let required: Vec<&str> = registry
        .vars
        .iter()
        .filter(|var| var.required)
        .map(|var| var.name)
        .collect();
    assert!(!required.is_empty());
    for name in required {
        assert!(
            text.contains(&format!("{name} is required and is not set")),
            "{text}"
        );
    }
    for var in registry.vars {
        if !matches!(
            var.kind,
            env_registry::Kind::Integer
                | env_registry::Kind::Boolean
                | env_registry::Kind::Url
                | env_registry::Kind::SocketAddr
        ) {
            continue;
        }
        let mut env: Vec<(&str, &str)> = valid
            .iter()
            .copied()
            .filter(|(name, _)| *name != var.name)
            .collect();
        env.push((var.name, SENTINEL));
        let output = run(binary, &[], &env);
        let text = stderr(&output);
        assert!(!output.status.success(), "{} accepted garbage", var.name);
        assert!(
            text.contains(&format!("{} is not valid", var.name)),
            "{}: {text}",
            var.name
        );
        assert!(!written(&output).contains(SENTINEL), "{}: {text}", var.name);
    }
}

/// `--print-env` prints `registry` as JSON, needs nothing set, and prints no
/// value even with every variable set.
pub fn print_env_prints_the_registry_and_no_value(binary: &str, registry: &Registry) {
    let env: Vec<(&str, &str)> = registry
        .vars
        .iter()
        .map(|var| (var.name, SENTINEL))
        .collect();
    let output = run(binary, &[env_registry::PRINT_FLAG], &env);
    assert!(output.status.success(), "{}", stderr(&output));
    let printed = String::from_utf8(output.stdout.clone()).unwrap();
    assert_eq!(printed, registry.json_text());
    assert!(!written(&output).contains(SENTINEL));

    let output = run(binary, &[env_registry::PRINT_FLAG], &[]);
    assert!(output.status.success(), "{}", stderr(&output));

    // It does not read the environment at all: a malformed value does not
    // stop it.
    if let Some(var) = registry
        .vars
        .iter()
        .find(|var| var.kind == env_registry::Kind::Integer)
    {
        let output = run(binary, &[env_registry::PRINT_FLAG], &[(var.name, SENTINEL)]);
        assert!(output.status.success(), "{}", stderr(&output));
    }

    let json: serde_json::Value = serde_json::from_str(&printed).unwrap();
    assert_eq!(json["schema_version"], env_registry::SCHEMA_VERSION);
    assert_eq!(json["service"], registry.service);
    let variables = json["variables"].as_array().unwrap();
    assert_eq!(variables.len(), registry.vars.len());
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
pub fn hold(root: &Path, path: &Path, actual: &str, expected: &str) {
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
        relative(root, path)
    );
}

/// `.env.example` under `root` is `expected` (what `registry` generates, or
/// what a distribution makes of it), and what it sets is exactly what the
/// service cannot start without.
pub fn env_example_is(root: &Path, registry: &Registry, expected: &str) {
    let path = root.join(".env.example");
    let actual = fs::read_to_string(&path).unwrap_or_default();
    hold(root, &path, &actual, expected);
    let actual = fs::read_to_string(&path).unwrap();
    let set: BTreeSet<&str> = actual
        .lines()
        .filter(|line| !line.starts_with('#'))
        .filter_map(|line| line.split_once('=').map(|(name, _)| name))
        .collect();
    let required: BTreeSet<&str> = registry
        .vars
        .iter()
        .filter(|var| var.required)
        .map(|var| var.name)
        .collect();
    assert_eq!(set, required);
}

/// The section of `file` (under `root`) between [`TABLE_BEGIN`] and
/// [`TABLE_END`] is `registry`'s table.
pub fn the_table_is_generated(root: &Path, file: &str, registry: &Registry) {
    let path = root.join(file);
    let actual = fs::read_to_string(&path).unwrap();
    let (Some(begin), Some(end)) = (actual.find(TABLE_BEGIN), actual.find(TABLE_END)) else {
        panic!("{file} has no `{TABLE_BEGIN}` … `{TABLE_END}` section");
    };
    let expected = format!(
        "{}{TABLE_BEGIN}\n\n{}\n{}",
        &actual[..begin],
        registry.markdown(),
        &actual[end..]
    );
    hold(root, &path, &actual, &expected);
}
