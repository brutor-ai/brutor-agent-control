// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Guard: what the gateway's images are called and what they say about
//! themselves is ONE list, `scripts/image-names.txt`.
//!
//! * Each edition's Dockerfile carries the labels of its line
//!   (`org.opencontainers.image.title`, `.licenses`, `.source`), a
//!   description, the vendor "Brutor AI Ltd" and `ai.brutor.edition`; and no
//!   `maintainer`, no fixed `version`, no source outside the organisation.
//! * The image workflow names the images as the list does, and no workflow
//!   derives an image name from the repository's name
//!   (`github.repository`): a repository that is renamed, forked or imported
//!   elsewhere would publish under another name.
//!
//! A built image is held to the same list by `scripts/image-check.sh`
//! (`scripts/image-smoke.sh` runs it).
//!
//! Proven against the shapes it forbids
//! ([`the_scans_catch_the_shapes_they_forbid`]).
//!
//! The same workflows also have time for a cold run: on the first run on a
//! new runner (no Cargo cache) the `test` job was cancelled by its
//! 45-minute timeout while the benchmarks compiled, after the suite had
//! passed, and `community-alone` by its 60-minute timeout at the same step.
//! A timeout covers the cold run measured then with half again as much room
//! ([`the_long_jobs_have_time_for_a_cold_run`], its reader proven by
//! [`the_reader_finds_a_jobs_timeout`]).

use std::collections::BTreeMap;
use std::fs;

#[path = "support/roots.rs"]
mod roots;
#[path = "support/workflows.rs"]
mod workflows;

const LIST: &str = "scripts/image-names.txt";
const VENDOR: &str = "Brutor AI Ltd";
const ORGANISATION: &str = "https://github.com/brutor-ai/";
/// This tree's directory in the public repository, which names its root
/// workflow (`../.github/workflows/core-gateway.yml`).
const COMPONENT: &str = "core-gateway";

struct Image {
    edition: String,
    name: String,
    title: String,
    licenses: String,
    source: String,
    dockerfile: String,
}

fn images() -> Vec<Image> {
    fs::read_to_string(LIST)
        .expect(LIST)
        .lines()
        .filter(|l| !l.starts_with('#') && !l.trim().is_empty())
        .map(|l| {
            let c: Vec<&str> = l.split('\t').collect();
            assert_eq!(c.len(), 6, "{LIST}: six tab-separated columns: {l:?}");
            Image {
                edition: c[0].to_string(),
                name: c[1].to_string(),
                title: c[2].to_string(),
                licenses: c[3].to_string(),
                source: c[4].to_string(),
                dockerfile: c[5].to_string(),
            }
        })
        .collect()
}

/// The labels of the image a Dockerfile builds: `LABEL key="value"` lines of
/// its last stage (the labels of an earlier stage are not the image's).
fn labels(dockerfile: &str) -> BTreeMap<String, String> {
    let last_stage = dockerfile
        .rfind("\nFROM ")
        .map_or(dockerfile, |at| &dockerfile[at..]);
    let mut out = BTreeMap::new();
    for line in last_stage.lines() {
        let Some(rest) = line.trim().strip_prefix("LABEL ") else {
            continue;
        };
        let (key, value) = rest
            .split_once('=')
            .unwrap_or_else(|| panic!("a LABEL without `=`: {line}"));
        let value = value.trim();
        assert!(
            value.len() >= 2 && value.starts_with('"') && value.ends_with('"'),
            "one quoted value per LABEL line: {line}"
        );
        let previous = out.insert(
            key.trim().to_string(),
            value[1..value.len() - 1].to_string(),
        );
        assert!(previous.is_none(), "the label {key} is set twice");
    }
    out
}

/// Why the labels are not the ones of `image`, if they are not.
fn label_faults(image: &Image, labels: &BTreeMap<String, String>) -> Vec<String> {
    let mut faults = Vec::new();
    for (key, want) in [
        ("org.opencontainers.image.title", image.title.as_str()),
        ("org.opencontainers.image.licenses", image.licenses.as_str()),
        ("org.opencontainers.image.source", image.source.as_str()),
        ("org.opencontainers.image.vendor", VENDOR),
        ("ai.brutor.edition", image.edition.as_str()),
    ] {
        match labels.get(key) {
            Some(have) if have == want => {}
            Some(have) => faults.push(format!("{key} is {have:?}, not {want:?}")),
            None => faults.push(format!("{key} is not set (it is {want:?})")),
        }
    }
    match labels.get("org.opencontainers.image.description") {
        Some(text) if text.contains("Brutor Agent Control Core Gateway") => {}
        other => faults.push(format!(
            "org.opencontainers.image.description names the product and the component: {other:?}"
        )),
    }
    for (key, value) in labels {
        // A person's name is not image metadata; a version written into the
        // file is wrong the day after; the unprefixed keys are not the
        // standard's.
        if matches!(
            key.as_str(),
            "maintainer"
                | "version"
                | "description"
                | "org.opencontainers.image.version"
                | "org.opencontainers.image.authors"
        ) {
            faults.push(format!(
                "the label {key} ({value:?}) is not set in a Dockerfile"
            ));
        }
        if value.contains("github.com/") && !value.starts_with(ORGANISATION) {
            faults.push(format!(
                "{key} names a GitHub namespace that is not the organisation's: {value:?}"
            ));
        }
    }
    faults
}

/// The lines of a workflow that derive a name from the repository's.
fn derived_names(workflow: &str) -> Vec<String> {
    workflow
        .lines()
        .enumerate()
        .filter(|(_, l)| !l.trim_start().starts_with('#'))
        .filter(|(_, l)| {
            let flat: String = l.chars().filter(|c| !c.is_whitespace()).collect();
            flat.contains("github.repository") || flat.contains("GITHUB_REPOSITORY")
        })
        .map(|(i, l)| format!("{}: {}", i + 1, l.trim()))
        .collect()
}

#[test]
fn the_list_is_the_two_editions_of_the_core_gateway() {
    let images = images();
    let editions: Vec<&str> = images.iter().map(|i| i.edition.as_str()).collect();
    assert_eq!(editions, ["community", "enterprise"]);
    for image in &images {
        let (infix, licence, repository, dockerfile) = match image.edition.as_str() {
            "community" => ("", "Apache-2.0", "brutor-agent-control", "Dockerfile"),
            _ => (
                "enterprise-",
                "LicenseRef-Brutor-Proprietary",
                "brutor-agent-control-enterprise",
                "enterprise/Dockerfile",
            ),
        };
        assert_eq!(
            image.name,
            format!("ghcr.io/brutor-ai/brutor-agent-control-{infix}core-gateway")
        );
        assert_eq!(image.licenses, licence);
        assert_eq!(image.source, format!("{ORGANISATION}{repository}"));
        assert_eq!(image.dockerfile, dockerfile);
        assert!(
            image
                .title
                .starts_with("Brutor Agent Control Core Gateway (")
                && image
                    .title
                    .to_lowercase()
                    .contains(&format!("({} edition)", image.edition)),
            "{}",
            image.title
        );
    }
}

#[test]
fn each_dockerfile_carries_the_labels_of_its_edition() {
    let mut read = 0;
    for image in images() {
        // The Enterprise Dockerfile is the Enterprise workspace's: read where
        // this run has it (`tests/support/roots.rs`).
        let path = match image.dockerfile.strip_prefix("enterprise/") {
            None => std::path::PathBuf::from(&image.dockerfile),
            Some(rest) => match roots::tree("enterprise") {
                Some(enterprise) => enterprise.join(rest),
                None => continue,
            },
        };
        let text = fs::read_to_string(&path).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
        let faults = label_faults(&image, &labels(&text));
        assert!(
            faults.is_empty(),
            "{} does not say what {LIST} says of the {} image:\n{}",
            path.display(),
            image.edition,
            faults.join("\n")
        );
        read += 1;
    }
    assert!(read >= 1, "no Dockerfile was read");
}

#[test]
fn the_workflows_name_the_images_as_the_list_does() {
    let root = roots::tree("community").expect("the Community root");
    let found = workflows::find(&root, COMPONENT, "docker-build.yml");
    for path in &found.all {
        let text = fs::read_to_string(path).expect("a workflow");
        let derived = derived_names(&text);
        assert!(
            derived.is_empty(),
            "{} derives a name from the repository's name; write the name out \
             ({LIST}):\n{}",
            path.display(),
            derived.join("\n")
        );
    }
    assert!(
        found.all.len() >= 3,
        "the scan found {} workflows",
        found.all.len()
    );

    let workflow = fs::read_to_string(&found.image).expect("the image workflow");
    let publish = fs::read_to_string(&found.publish).expect("what names the published image");
    let image_file = found.image.display();
    match found.layout {
        workflows::Layout::Own => {
            assert_eq!(workflow.matches("\n  REGISTRY: ghcr.io\n").count(), 1);
            assert_eq!(publish.matches("\n  REGISTRY: ghcr.io\n").count(), 1);
        }
        // The public repository writes the names once, in its bake file; the
        // component's workflow builds its image through the bake target and
        // names no published image.
        workflows::Layout::Monorepo => {
            assert!(
                workflows::bake_registry_is_ghcr_once(&publish),
                "{}: REGISTRY is ghcr.io, set once",
                found.publish.display()
            );
            assert_eq!(
                workflows::built_through_bake(&workflow, &found.component),
                1,
                "{image_file}: builds the image through {} ({})",
                workflows::BAKE,
                found.component
            );
            assert!(
                !workflow.contains("brutor-ai/brutor-agent-control-"),
                "{image_file} names a published image; the names are in {}",
                workflows::BAKE
            );
        }
    }
    for image in images() {
        let variable = match image.edition.as_str() {
            "community" => "COMMUNITY_IMAGE_NAME",
            _ => "IMAGE_NAME",
        };
        let repository = image
            .name
            .strip_prefix("ghcr.io/")
            .expect("an image on ghcr.io");
        let named = workflow
            .matches(&format!("\n  {variable}: {repository}\n"))
            .count();
        match (found.layout, image.edition.as_str()) {
            // The public repository builds and publishes the Community
            // image only; neither a workflow nor the bake file of it names
            // the Enterprise image.
            (workflows::Layout::Monorepo, "enterprise") => {
                for path in found.all.iter().chain([&found.publish]) {
                    let text = fs::read_to_string(path).expect("a workflow");
                    assert!(
                        !text.contains(repository),
                        "{} names the Enterprise image {repository}",
                        path.display()
                    );
                }
            }
            (workflows::Layout::Monorepo, _) => {
                assert_eq!(
                    workflows::published_from_component(&publish, repository, &found.component),
                    1,
                    "{}: the bake target publishes {repository} from {}/ ({LIST})",
                    found.publish.display(),
                    found.component
                );
            }
            (workflows::Layout::Own, _) => {
                assert_eq!(
                    named, 1,
                    "{image_file}: {variable} is {repository} ({LIST})"
                );
            }
        }
    }
    // And the labels the build adds are the ones only it knows: the
    // metadata action's own set would replace the Dockerfile's title, source
    // and licence with the repository's.
    for path in &found.mine {
        let text = fs::read_to_string(path).expect("a workflow");
        assert!(
            !text.contains("steps.meta.outputs.labels"),
            "{} passes the metadata action's labels, which replace the Dockerfile's",
            path.display()
        );
    }
}

#[test]
fn the_workflows_are_found_in_either_layout_and_never_in_neither() {
    workflows::check_the_layouts();
}

#[test]
fn the_scans_catch_the_shapes_they_forbid() {
    let community = Image {
        edition: "community".to_string(),
        name: "ghcr.io/brutor-ai/brutor-agent-control-core-gateway".to_string(),
        title: "Brutor Agent Control Core Gateway (Community Edition)".to_string(),
        licenses: "Apache-2.0".to_string(),
        source: "https://github.com/brutor-ai/brutor-agent-control".to_string(),
        dockerfile: "Dockerfile".to_string(),
    };
    let good = "FROM rust AS builder\nLABEL stage=\"builder\"\nFROM debian\n\
        LABEL org.opencontainers.image.title=\"Brutor Agent Control Core Gateway (Community Edition)\"\n\
        LABEL org.opencontainers.image.description=\"Brutor Agent Control Core Gateway, Community Edition\"\n\
        LABEL org.opencontainers.image.licenses=\"Apache-2.0\"\n\
        LABEL org.opencontainers.image.source=\"https://github.com/brutor-ai/brutor-agent-control\"\n\
        LABEL org.opencontainers.image.vendor=\"Brutor AI Ltd\"\n\
        LABEL ai.brutor.edition=\"community\"\n";
    assert_eq!(
        label_faults(&community, &labels(good)),
        Vec::<String>::new()
    );
    // What the Dockerfiles held, and what a copy of the other edition's would.
    for (bad, about) in [
        (
            good.replace("Apache-2.0", "LicenseRef-Brutor-Proprietary"),
            "licenses",
        ),
        (
            good.replace("brutor-ai/brutor-agent-control\"", "someone/brutor-gateway-core\""),
            "source",
        ),
        (good.replace("(Community Edition)", "(Enterprise Edition)"), "title"),
        (good.replace("Brutor AI Ltd", "Brutor"), "vendor"),
        (good.replace("=\"community\"", "=\"enterprise\""), "ai.brutor.edition"),
        (format!("{good}LABEL maintainer=\"A Person\"\n"), "maintainer"),
        (format!("{good}LABEL version=\"0.3.0\"\n"), "version"),
        (
            good.replace(
                "LABEL org.opencontainers.image.licenses=\"Apache-2.0\"\n",
                "",
            ),
            "licenses",
        ),
        (
            good.replace(
                "LABEL org.opencontainers.image.description=\"Brutor Agent Control Core Gateway, Community Edition\"\n",
                "LABEL description=\"High-performance proxy in Rust\"\n",
            ),
            "description",
        ),
    ] {
        let faults = label_faults(&community, &labels(&bad));
        assert!(
            faults.iter().any(|f| f.contains(about)),
            "not caught ({about}): {faults:?}"
        );
    }
    // A label of the builder stage is not the image's.
    assert!(!labels(good).contains_key("stage"));

    for line in [
        "  IMAGE_NAME: ${{ github.repository }}",
        "          images: ghcr.io/${{github.repository}}",
        "          docker build -t \"ghcr.io/$GITHUB_REPOSITORY:test\" .",
    ] {
        assert_eq!(derived_names(line).len(), 1, "{line}");
    }
    for line in [
        "  IMAGE_NAME: brutor-ai/brutor-agent-control-core-gateway",
        "  # never ${{ github.repository }}",
        "          repository: brutor-ai/brutor-gateway-control",
    ] {
        assert!(derived_names(line).is_empty(), "{line}");
    }
}

// --------------------------------------------------------------- time for a cold run

/// A cold run of each job, in minutes, measured on the first public run
/// (ubuntu-latest, no Cargo cache): where the job stood when its timeout
/// cancelled it.
const COLD_RUN_MINUTES: &[(&str, u32)] = &[
    // the suite done after 33 minutes, the benchmarks still compiling at 45
    ("test", 60),
    // the gates and the suite done after 47 minutes, the benchmarks still compiling at 60
    ("community-alone", 75),
];

/// The `timeout-minutes` of the job `job` of a workflow, read as text: the
/// job's block starts at `  <job>:` and ends at the next line indented by
/// two spaces; None where the job or its timeout is not there.
fn timeout_minutes(workflow: &str, job: &str) -> Option<u32> {
    job_lines(workflow, job)?.into_iter().find_map(|line| {
        line.strip_prefix("    timeout-minutes:")
            .and_then(|v| v.trim().parse().ok())
    })
}

/// Whether `line` opens a job: a key at the jobs' indentation (two spaces).
fn opens_a_job(line: &str) -> bool {
    line.starts_with("  ") && line[2..].starts_with(|c: char| !c.is_whitespace() && c != '#')
}

/// The lines of the job `job`, after its `  <job>:` line and up to the next
/// job; None where the job is not there.
fn job_lines<'a>(workflow: &'a str, job: &str) -> Option<Vec<&'a str>> {
    let start = workflow.find(&format!("\n  {job}:\n"))? + 1;
    Some(
        workflow[start..]
            .lines()
            .skip(1)
            .take_while(|line| !opens_a_job(line))
            .collect(),
    )
}

/// The names of the workflow's jobs, in order (the keys under `jobs:`).
fn job_names(workflow: &str) -> Vec<&str> {
    let Some(start) = workflow.find("\njobs:\n") else {
        return Vec::new();
    };
    workflow[start + 1..]
        .lines()
        .skip(1)
        .take_while(|line| line.is_empty() || line.starts_with(' ') || line.starts_with('#'))
        .filter(|line| opens_a_job(line))
        .filter_map(|line| line.trim_end().strip_suffix(':'))
        .map(|name| name.trim())
        .collect()
}

/// What runs cargo-about: the licence check, alone or inside the
/// Community-alone script.
const NEEDS_CARGO_ABOUT: &[&str] = &[
    "scripts/ci-community-alone.sh",
    "scripts/third-party-licenses.sh",
];

/// The jobs of `workflow` that reach a script of [`NEEDS_CARGO_ABOUT`]
/// before a step installs `cargo-about` at `version` (`cargo install
/// --locked --features cli cargo-about@<version>`).
fn jobs_without_cargo_about<'a>(workflow: &'a str, version: &str) -> Vec<&'a str> {
    let install = format!("cargo install --locked --features cli cargo-about@{version}");
    job_names(workflow)
        .into_iter()
        .filter(|job| {
            let lines = job_lines(workflow, job).unwrap_or_default();
            let first_use = lines.iter().position(|line| {
                NEEDS_CARGO_ABOUT.iter().any(|script| {
                    line.contains(&format!("run: {script}"))
                        || line.trim_start().starts_with(script)
                })
            });
            match first_use {
                None => false,
                Some(i) => !lines[..i].iter().any(|line| line.contains(&install)),
            }
        })
        .collect()
}

/// The cargo-about version the licence script requires (`VERSION=` in
/// `scripts/third-party-licenses.sh`).
fn cargo_about_version() -> String {
    let root = roots::tree("community").expect("the Community root");
    let script = fs::read_to_string(root.join("scripts/third-party-licenses.sh"))
        .expect("the licence script");
    script
        .lines()
        .find_map(|line| line.strip_prefix("VERSION="))
        .expect("scripts/third-party-licenses.sh sets VERSION=")
        .trim()
        .to_string()
}

#[test]
fn every_job_that_checks_the_licence_file_installs_cargo_about_first() {
    let (path, workflow) = ci_workflow();
    let version = cargo_about_version();
    assert!(
        !job_names(&workflow).is_empty(),
        "{}: no job was read",
        path.display()
    );
    assert_eq!(
        jobs_without_cargo_about(&workflow, &version),
        Vec::<&str>::new(),
        "{}: these jobs run the licence check (scripts/third-party-licenses.sh, also inside \
         scripts/ci-community-alone.sh) and do not install cargo-about {version} before it: \
         the check fails with \"found: none\"",
        path.display()
    );
}

#[test]
fn the_cargo_about_scan_catches_a_job_that_lacks_it() {
    let install = "        run: cargo install --locked --features cli cargo-about@0.9.3\n";
    let workflow = format!(
        "on: push\njobs:\n  deny:\n    steps:\n      - name: Install cargo-about\n{install}      - name: Check\n        run: scripts/third-party-licenses.sh --check\n  community-alone:\n    steps:\n      - name: All\n        run: scripts/ci-community-alone.sh --prepare-template\n  late:\n    steps:\n      - run: |\n          scripts/third-party-licenses.sh --check\n      - name: Too late\n{install}  clippy:\n    steps:\n      - run: cargo clippy\n"
    );
    assert_eq!(
        job_names(&workflow),
        ["deny", "community-alone", "late", "clippy"]
    );
    assert_eq!(
        jobs_without_cargo_about(&workflow, "0.9.3"),
        ["community-alone", "late"]
    );
    // Another version is not the one the script needs.
    assert_eq!(
        jobs_without_cargo_about(&workflow, "0.9.4"),
        ["deny", "community-alone", "late"]
    );
}

/// The workflow that runs the tests: this repository's `ci.yml`, or the
/// public repository's `core-gateway.yml`.
fn ci_workflow() -> (std::path::PathBuf, String) {
    let root = roots::tree("community").expect("the Community root");
    let found = workflows::find(&root, COMPONENT, "docker-build.yml");
    let path = match found.layout {
        workflows::Layout::Own => root.join(".github/workflows/ci.yml"),
        workflows::Layout::Monorepo => found.image.clone(),
    };
    let text = fs::read_to_string(&path).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
    (path, text)
}

#[test]
fn the_long_jobs_have_time_for_a_cold_run() {
    let (path, workflow) = ci_workflow();
    for (job, minutes) in COLD_RUN_MINUTES {
        let timeout = timeout_minutes(&workflow, job)
            .unwrap_or_else(|| panic!("{}: the job {job} has no timeout-minutes", path.display()));
        assert!(
            timeout * 2 >= minutes * 3,
            "{}: the job {job} times out after {timeout} minutes; a cold run takes about {minutes}, \
             and the timeout must be at least half as much again",
            path.display()
        );
    }
}

#[test]
fn the_reader_finds_a_jobs_timeout() {
    let workflow = "jobs:\n  test:\n    # a comment\n    timeout-minutes: 90\n    name: Tests\n    steps:\n      - run: x\n  community-alone:\n    name: C\n    steps:\n      - run: y\n    timeout-minutes: 120\n  other:\n    name: O\n";
    assert_eq!(timeout_minutes(workflow, "test"), Some(90));
    assert_eq!(timeout_minutes(workflow, "community-alone"), Some(120));
    // A job without one, and a job that is not there.
    assert_eq!(timeout_minutes(workflow, "other"), None);
    assert_eq!(timeout_minutes(workflow, "missing"), None);
    // Another job's timeout is not this job's.
    assert_eq!(
        timeout_minutes(
            "jobs:\n  test:\n    name: T\n  later:\n    timeout-minutes: 5\n",
            "test"
        ),
        None
    );
}
