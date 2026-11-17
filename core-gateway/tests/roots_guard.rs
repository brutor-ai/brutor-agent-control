// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Guard: the tests take the trees they read from ONE function,
//! `tests/support/roots.rs`, and that function never turns a tree that was
//! given and is not there into "nothing to check".
//!
//! Two halves:
//!
//! * **Behaviour.** A given Enterprise root that holds no source file fails
//!   the run; a tree that is not given and not there is `None`; a sibling
//!   repository that is absent fails the test that needs it, or, where the
//!   run said siblings are optional, is skipped with a line on stderr and
//!   one in the skip log. Asked in a child process (this test binary, run
//!   again with the variables set), because the answer is read from the
//!   environment.
//! * **Source scan.** No test decides for itself whether the Enterprise
//!   tree or a sibling repository is there (`"enterprise/src").is_dir()`,
//!   `"../brutor-…"`): a guard that walks a directory "if it is there"
//!   passes without reading a file once the layout changes.
//!
//! Proven against the shapes it forbids
//! ([`the_scan_catches_the_shapes_it_forbids`]).

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

#[path = "support/roots.rs"]
mod roots;

/// The variable that makes [`probe`] ask; unset in a normal run.
const PROBE: &str = "ROOTS_GUARD_PROBE";

const VARIABLES: [&str; 7] = [
    "BRUTOR_COMMUNITY_ROOT",
    "BRUTOR_ENTERPRISE_ROOT",
    "BRUTOR_CONTROL_REPO",
    "BRUTOR_SKILL_RUNNER_REPO",
    "BRUTOR_EDITION_RECORDS_DIR",
    "BRUTOR_TEST_SIBLINGS",
    "BRUTOR_TEST_SKIP_LOG",
];

/// Not a test of its own: what the child process runs. It prints what
/// `roots::tree` answers for the tree named in [`PROBE`].
#[test]
fn probe() {
    let Ok(name) = std::env::var(PROBE) else {
        return;
    };
    match roots::tree(&name) {
        Some(root) => println!("ANSWER=Some({})", root.display()),
        None => println!("ANSWER=None"),
    }
}

/// Ask `roots::tree(name)` in a child process with exactly `variables` set.
/// `(the child passed, everything it printed)`.
fn ask(name: &str, variables: &[(&str, &Path)]) -> (bool, String) {
    let mut command = Command::new(std::env::current_exe().expect("this test binary"));
    command
        .args(["--exact", "probe", "--nocapture", "--test-threads", "1"])
        .env(PROBE, name);
    for variable in VARIABLES {
        command.env_remove(variable);
    }
    for (variable, value) in variables {
        command.env(variable, value);
    }
    let output = command.output().expect("run the probe");
    let mut text = String::from_utf8_lossy(&output.stdout).to_string();
    text.push_str(&String::from_utf8_lossy(&output.stderr));
    (output.status.success(), text)
}

/// A directory of this test's own under the build directory, empty.
fn scratch(name: &str) -> PathBuf {
    let dir = Path::new(env!("CARGO_TARGET_TMPDIR")).join(format!("roots-guard-{name}"));
    if dir.exists() {
        fs::remove_dir_all(&dir).expect("clear the scratch directory");
    }
    fs::create_dir_all(&dir).expect("create the scratch directory");
    dir
}

#[test]
fn a_given_enterprise_root_that_holds_no_source_fails_the_run() {
    let empty = scratch("empty");
    let missing = empty.join("not-there");
    let no_source = scratch("no-source");
    fs::create_dir_all(no_source.join("src")).unwrap();
    fs::write(no_source.join("src/README.md"), "not Rust").unwrap();
    for given in [&empty, &missing, &no_source] {
        let (passed, said) = ask("enterprise", &[("BRUTOR_ENTERPRISE_ROOT", given)]);
        assert!(
            !passed,
            "{}: a root with no source is accepted: {said}",
            given.display()
        );
        assert!(
            said.contains("holds no Rust source under src/") && !said.contains("ANSWER="),
            "{said}"
        );
    }
}

#[test]
fn a_given_enterprise_root_that_holds_source_is_the_root() {
    let root = scratch("with-source");
    fs::create_dir_all(root.join("src/routes")).unwrap();
    fs::write(root.join("src/routes/a.rs"), "// a file\n").unwrap();
    let (passed, said) = ask("enterprise", &[("BRUTOR_ENTERPRISE_ROOT", &root)]);
    assert!(passed, "{said}");
    assert!(
        said.contains(&format!("ANSWER=Some({})", root.display())),
        "{said}"
    );
}

/// Not given: the nested workspace when this repository carries it, and
/// `None` in a Community tree alone. Under the name the guards use.
#[test]
fn without_a_given_root_the_enterprise_tree_is_the_nested_workspace_or_none() {
    let (passed, said) = ask("enterprise", &[]);
    assert!(passed, "{said}");
    let nested = Path::new("enterprise").join("src").is_dir();
    let expected = if nested {
        "ANSWER=Some(enterprise)"
    } else {
        "ANSWER=None"
    };
    assert!(said.contains(expected), "expected {expected}: {said}");
    assert_eq!(roots::tree("enterprise").is_some(), nested);
}

#[test]
fn an_absent_sibling_repository_fails_or_is_skipped_and_recorded() {
    let nowhere = scratch("siblings").join("not-checked-out");
    let log = scratch("skip-log").join("skips.tsv");

    // The control plane: needed. Absent, the test that asks fails.
    let (passed, said) = ask("control-plane", &[("BRUTOR_CONTROL_REPO", &nowhere)]);
    assert!(!passed, "{said}");
    assert!(
        said.contains("needs the control-plane repository") && !said.contains("ANSWER="),
        "{said}"
    );
    assert!(!log.exists());

    // Where the run said siblings are optional: skipped, and recorded.
    let optional = Path::new("optional");
    let (passed, said) = ask(
        "control-plane",
        &[
            ("BRUTOR_CONTROL_REPO", &nowhere),
            ("BRUTOR_TEST_SIBLINGS", optional),
            ("BRUTOR_TEST_SKIP_LOG", &log),
        ],
    );
    assert!(passed, "{said}");
    assert!(
        said.contains("ANSWER=None") && said.contains("SKIPPED: probe: it needs the control-plane"),
        "{said}"
    );
    let recorded = fs::read_to_string(&log).expect("the skip log");
    assert_eq!(
        recorded,
        format!("probe\tcontrol-plane\t{}\n", nowhere.display())
    );

    // The edition records have no default place: with their variable not
    // set the test is skipped, the skip names the variable, and it is
    // recorded.
    let (passed, said) = ask("edition-records", &[("BRUTOR_TEST_SKIP_LOG", &log)]);
    assert!(passed, "{said}");
    assert!(
        said.contains("ANSWER=None")
            && said.contains("SKIPPED: probe:")
            && said.contains("BRUTOR_EDITION_RECORDS_DIR is not set"),
        "{said}"
    );
    let recorded = fs::read_to_string(&log).unwrap();
    assert_eq!(recorded.lines().count(), 2);
    assert_eq!(
        recorded.lines().last(),
        Some("probe\tedition-records\tBRUTOR_EDITION_RECORDS_DIR is not set")
    );
    // Given and not there: a failure, not a skip.
    let (passed, said) = ask(
        "edition-records",
        &[
            ("BRUTOR_EDITION_RECORDS_DIR", &nowhere),
            ("BRUTOR_TEST_SKIP_LOG", &log),
        ],
    );
    assert!(!passed, "{said}");
    assert!(said.contains("is not a directory"), "{said}");
    assert_eq!(fs::read_to_string(&log).unwrap().lines().count(), 2);
    // Given and there: returned.
    let records = scratch("records-there");
    let (passed, said) = ask(
        "edition-records",
        &[("BRUTOR_EDITION_RECORDS_DIR", &records)],
    );
    assert!(passed, "{said}");
    assert!(
        said.contains(&format!("ANSWER=Some({})", records.display())),
        "{said}"
    );

    // A sibling that is there is returned, and nothing is recorded.
    let there = scratch("sibling-there");
    let (passed, said) = ask(
        "control-plane",
        &[
            ("BRUTOR_CONTROL_REPO", &there),
            ("BRUTOR_TEST_SKIP_LOG", &log),
        ],
    );
    assert!(passed, "{said}");
    assert!(
        said.contains(&format!("ANSWER=Some({})", there.display())) && !said.contains("SKIPPED"),
        "{said}"
    );
    assert_eq!(fs::read_to_string(&log).unwrap().lines().count(), 2);
}

/// A Community root, `<dir>/<name>`, that `BRUTOR_COMMUNITY_ROOT` accepts:
/// it has `src/lib.rs` and `tests/support/roots.rs`. Returned canonical,
/// as `roots::tree` reads it.
fn community_root_in(dir: &Path, name: &str) -> PathBuf {
    let root = dir.join(name);
    fs::create_dir_all(root.join("src")).unwrap();
    fs::create_dir_all(root.join("tests/support")).unwrap();
    fs::write(root.join("src/lib.rs"), "// the crate\n").unwrap();
    fs::write(root.join("tests/support/roots.rs"), "// the one function\n").unwrap();
    root.canonicalize().unwrap()
}

/// A sibling with a default place is looked for beside the Community root
/// under the name of its own repository (`brutor-gateway-control`,
/// `brutor-skill-runner`) and then under its directory in the public
/// monorepo (`control-plane`, `skill-runner`: the layout of the export);
/// the first that is there is the tree. Neither: the rule of an absent
/// sibling, with both places named.
#[test]
fn a_sibling_is_looked_for_under_its_repository_name_then_its_monorepo_directory() {
    let mono = scratch("monorepo");
    let community = community_root_in(&mono, "core-gateway");
    let mono = community.parent().unwrap().to_path_buf();
    let log = mono.join("skips.tsv");
    for (tree, private, public) in [
        ("control-plane", "brutor-gateway-control", "control-plane"),
        ("skill-runner", "brutor-skill-runner", "skill-runner"),
    ] {
        let (private, public) = (mono.join(private), mono.join(public));

        // Neither: the control plane fails the test that needs it, the
        // skill runner is skipped; both name the two places.
        let (passed, said) = ask(
            tree,
            &[
                ("BRUTOR_COMMUNITY_ROOT", &community),
                ("BRUTOR_TEST_SKIP_LOG", &log),
            ],
        );
        assert_eq!(passed, tree == "skill-runner", "{tree}: {said}");
        assert!(
            said.contains(&private.display().to_string())
                && said.contains(&public.display().to_string())
                && !said.contains("ANSWER=Some"),
            "{tree}: {said}"
        );

        // The monorepo's directory alone: that is the tree.
        fs::create_dir_all(&public).unwrap();
        let (passed, said) = ask(tree, &[("BRUTOR_COMMUNITY_ROOT", &community)]);
        assert!(passed, "{tree}: {said}");
        assert!(
            said.contains(&format!("ANSWER=Some({})", public.display())),
            "{tree}: {said}"
        );

        // Both: the repository's own name first.
        fs::create_dir_all(&private).unwrap();
        let (passed, said) = ask(tree, &[("BRUTOR_COMMUNITY_ROOT", &community)]);
        assert!(passed, "{tree}: {said}");
        assert!(
            said.contains(&format!("ANSWER=Some({})", private.display())),
            "{tree}: {said}"
        );
    }
    // Only the skill runner's absence was recorded, with both places.
    let recorded = fs::read_to_string(&log).expect("the skip log");
    assert_eq!(
        recorded,
        format!(
            "probe\tskill-runner\t{} or {}\n",
            mono.join("brutor-skill-runner").display(),
            mono.join("skill-runner").display()
        )
    );

    // The variable still decides on its own: given, nothing else is asked.
    let given = scratch("sibling-given");
    for (tree, variable) in [
        ("control-plane", "BRUTOR_CONTROL_REPO"),
        ("skill-runner", "BRUTOR_SKILL_RUNNER_REPO"),
    ] {
        let (passed, said) = ask(
            tree,
            &[("BRUTOR_COMMUNITY_ROOT", &community), (variable, &given)],
        );
        assert!(passed, "{tree}: {said}");
        assert!(
            said.contains(&format!("ANSWER=Some({})", given.display())),
            "{tree}: {said}"
        );
    }
}

#[test]
fn a_tree_nobody_named_is_a_failure() {
    let (passed, said) = ask("enterprize", &[]);
    assert!(!passed, "{said}");
    assert!(said.contains("no tree is called"), "{said}");
    // The Community root is always there.
    let root = roots::tree("community").expect("the Community root");
    assert!(root.join("tests/support/roots.rs").is_file());
}

// ---------------------------------------------------------------------------
// The source scan
// ---------------------------------------------------------------------------

/// Files that may spell a layout, and why.
const EXEMPT: &[(&str, &str)] = &[
    ("tests/support/roots.rs", "the one function"),
    (
        "tests/roots_guard.rs",
        "this guard: it names what it forbids",
    ),
    (
        "crates/brutor-verify/tests/corpus/mod.rs",
        "the vector generator (an example, not a test) writes the control plane's parity \
         fixture and must know where it goes; the test's decision to compare or skip is \
         `roots::tree` (`crates/brutor-verify/tests/vectors.rs`)",
    ),
];

/// The lines of a test file that decide the layout for themselves.
fn layout_decisions(text: &str) -> Vec<String> {
    let mut found = Vec::new();
    for (i, line) in text.lines().enumerate() {
        let code = line.trim();
        if code.starts_with("//") {
            continue;
        }
        let names_enterprise = code.contains("\"enterprise")
            || code.contains("\"./enterprise")
            || code.contains("ENTERPRISE_ROOT)");
        let asks_the_disk = [".is_dir()", ".exists()", ".is_file()", "fs::metadata("]
            .iter()
            .any(|probe| code.contains(probe));
        let walks = ["rust_files(", "read_dir(", "files("]
            .iter()
            .any(|walk| code.contains(walk));
        let sibling = code.contains("\"../brutor-")
            || code.contains("join(\"brutor-")
            || code.contains("env::var(\"BRUTOR_CONTROL_REPO\")")
            || code.contains("env::var(\"BRUTOR_EDITION_RECORDS_DIR\")")
            || code.contains("env::var(\"BRUTOR_ENTERPRISE_ROOT\")");
        if (names_enterprise && (asks_the_disk || walks)) || sibling {
            found.push(format!("{}: {code}", i + 1));
        }
    }
    found
}

fn rust_files(dir: &Path, out: &mut Vec<PathBuf>) {
    for entry in fs::read_dir(dir)
        .unwrap_or_else(|e| panic!("{}: {e}", dir.display()))
        .flatten()
    {
        let path = entry.path();
        if path.is_dir() {
            if path.file_name().is_some_and(|n| n != "target") {
                rust_files(&path, out);
            }
        } else if path.extension().is_some_and(|x| x == "rs") {
            out.push(path);
        }
    }
}

#[test]
fn no_test_decides_the_layout_for_itself() {
    let community = roots::tree("community").expect("the Community root");
    let mut files = Vec::new();
    rust_files(&community.join("tests"), &mut files);
    for entry in fs::read_dir(community.join("crates"))
        .expect("crates/")
        .flatten()
    {
        let tests = entry.path().join("tests");
        if tests.is_dir() {
            rust_files(&tests, &mut files);
        }
    }
    // The Enterprise workspace's tests too, where this run has it.
    if let Some(enterprise) = roots::tree("enterprise") {
        let before = files.len();
        rust_files(&community.join(enterprise).join("tests"), &mut files);
        assert!(files.len() > before + 20, "the Enterprise tests");
    }
    assert!(files.len() > 200, "the scan found {} files", files.len());

    let mut offenders = Vec::new();
    let mut exempt_seen = 0;
    for file in &files {
        let rel = file
            .strip_prefix(&community)
            .expect("under the Community root")
            .to_string_lossy()
            .replace('\\', "/");
        if EXEMPT.iter().any(|(path, _)| *path == rel) {
            exempt_seen += 1;
            continue;
        }
        let text = fs::read_to_string(file).expect("a test file");
        for line in layout_decisions(&text) {
            offenders.push(format!("{rel}:{line}"));
        }
    }
    assert_eq!(exempt_seen, EXEMPT.len(), "an exempt file is gone");
    assert!(
        offenders.is_empty(),
        "a test decides for itself where the Enterprise tree or a sibling repository is. \
         Ask `roots::tree(..)` (tests/support/roots.rs): it reads the environment, and a \
         tree that was given and is not there fails instead of being skipped:\n{}",
        offenders.join("\n")
    );
}

#[test]
fn the_scan_catches_the_shapes_it_forbids() {
    // What the guards held before they took their roots from one function.
    for shape in [
        r#"    if Path::new("enterprise/src").is_dir() {"#,
        r#"        rust_files(Path::new("enterprise/src"), &mut files);"#,
        r#"    if root.join("enterprise/src").is_dir() {"#,
        r#"    if Path::new(ENTERPRISE_ROOT).is_dir() {"#,
        r#"        .filter(|_| std::path::Path::new("enterprise/src").is_dir());"#,
        r#"        files(Path::new("./enterprise/src"), &mut all);"#,
        r#"    if let Ok(entries) = std::fs::read_dir(root.join("enterprise/src/routes")) {"#,
        r#"        .join("../brutor-skill-runner/src/env_registry.rs");"#,
        r#"        .join("brutor-gateway-control")"#,
        r#"    if let Ok(dir) = std::env::var("BRUTOR_CONTROL_REPO") {"#,
    ] {
        assert_eq!(layout_decisions(shape).len(), 1, "not caught: {shape}");
    }
    // What a guard may still say: a file by its name in a list, read after
    // the one function said the tree is here.
    for shape in [
        r#"    "enterprise/src/portal.rs","#,
        r#"    if let Some(enterprise) = roots::tree("enterprise") {"#,
        r#"        rust_files(&enterprise.join("src"), &mut files);"#,
        r#"    let main = fs::read_to_string("enterprise/src/main.rs").unwrap();"#,
        r#"    // if Path::new("enterprise/src").is_dir() {"#,
        r#"    rust_files(Path::new("src"), &mut files);"#,
    ] {
        assert!(layout_decisions(shape).is_empty(), "caught: {shape}");
    }
}
