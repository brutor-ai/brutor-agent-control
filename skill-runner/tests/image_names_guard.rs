// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Guard: what the Skill Runner's image is called and what it says about
//! itself is ONE list, `scripts/image-names.txt`.
//!
//! * The Dockerfile carries the labels of the list's line
//!   (`org.opencontainers.image.title`, `.licenses`, `.source`), a
//!   description, the vendor "Brutor AI Ltd" and `ai.brutor.edition`; and no
//!   `maintainer`, no fixed `version`, no source outside the organisation.
//! * The image workflow writes the image's name out as the list does, and no
//!   workflow derives an image name from the repository's name
//!   (`github.repository`): a repository that is renamed, forked or imported
//!   elsewhere would publish under another name.
//!
//! Proven against the shapes it forbids
//! ([`the_scans_catch_the_shapes_they_forbid`]).

use std::collections::BTreeMap;
use std::fs;
use std::path::PathBuf;

#[path = "common/workflows.rs"]
mod workflows;

const LIST: &str = "scripts/image-names.txt";
const VENDOR: &str = "Brutor AI Ltd";
const ORGANISATION: &str = "https://github.com/brutor-ai/";
const COMPONENT: &str = "Brutor Agent Control Skill Runner";
/// This tree's directory in the public repository, which names its root
/// workflow (`../.github/workflows/skill-runner.yml`).
const DIRECTORY: &str = "skill-runner";

struct Image {
    edition: String,
    name: String,
    title: String,
    licenses: String,
    source: String,
    dockerfile: String,
}

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

fn images() -> Vec<Image> {
    fs::read_to_string(root().join(LIST))
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
        Some(text) if text.contains(COMPONENT) => {}
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
fn the_list_is_the_community_skill_runner() {
    let images = images();
    assert_eq!(images.len(), 1, "{LIST}: one image, the Community build");
    let image = &images[0];
    assert_eq!(image.edition, "community");
    assert_eq!(
        image.name,
        "ghcr.io/brutor-ai/brutor-agent-control-skill-runner"
    );
    assert_eq!(image.title, format!("{COMPONENT} (Community Edition)"));
    assert_eq!(image.licenses, "Apache-2.0");
    assert_eq!(image.source, format!("{ORGANISATION}brutor-agent-control"));
    assert_eq!(image.dockerfile, "Dockerfile");
}

#[test]
fn the_dockerfile_carries_the_labels_of_the_list() {
    for image in images() {
        let path = root().join(&image.dockerfile);
        let text = fs::read_to_string(&path).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
        let faults = label_faults(&image, &labels(&text));
        assert!(
            faults.is_empty(),
            "{} does not say what {LIST} says of the image:\n{}",
            image.dockerfile,
            faults.join("\n")
        );
    }
}

#[test]
fn the_workflows_name_the_image_as_the_list_does() {
    let found = workflows::find(&root(), DIRECTORY, "docker-build.yml");
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
        found.all.len() >= 2,
        "the scan found {} workflows",
        found.all.len()
    );

    let workflow = fs::read_to_string(&found.publish).expect("what names the published image");
    let publish_file = found.publish.display();
    match found.layout {
        workflows::Layout::Own => {
            assert_eq!(workflow.matches("\n  REGISTRY: ghcr.io\n").count(), 1);
        }
        // The public repository writes the names once, in its bake file; the
        // component's workflow builds its image through the bake target and
        // names no published image.
        workflows::Layout::Monorepo => {
            assert!(
                workflows::bake_registry_is_ghcr_once(&workflow),
                "{publish_file}: REGISTRY is ghcr.io, set once"
            );
            let image = fs::read_to_string(&found.image).expect("the image workflow");
            assert_eq!(
                workflows::built_through_bake(&image, &found.component),
                1,
                "{}: builds the image through {}",
                found.image.display(),
                workflows::BAKE
            );
            assert!(
                !image.contains("brutor-ai/brutor-agent-control-"),
                "{} names a published image; the names are in {}",
                found.image.display(),
                workflows::BAKE
            );
        }
    }
    for image in images() {
        let repository = image
            .name
            .strip_prefix("ghcr.io/")
            .expect("an image on ghcr.io");
        let named = match found.layout {
            workflows::Layout::Own => workflow
                .matches(&format!("\n  IMAGE_NAME: {repository}\n"))
                .count(),
            // The public repository's bake file publishes every image.
            workflows::Layout::Monorepo => {
                workflows::published_from_component(&workflow, repository, &found.component)
            }
        };
        assert_eq!(
            named, 1,
            "{publish_file}: the image is published as {repository}, once ({LIST})"
        );
    }
    // The metadata action's own labels would replace the Dockerfile's title,
    // source and licence with the repository's.
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
        name: "ghcr.io/brutor-ai/brutor-agent-control-skill-runner".to_string(),
        title: "Brutor Agent Control Skill Runner (Community Edition)".to_string(),
        licenses: "Apache-2.0".to_string(),
        source: "https://github.com/brutor-ai/brutor-agent-control".to_string(),
        dockerfile: "Dockerfile".to_string(),
    };
    let good = "FROM rust AS builder\nLABEL stage=\"builder\"\nFROM debian\n\
        LABEL org.opencontainers.image.title=\"Brutor Agent Control Skill Runner (Community Edition)\"\n\
        LABEL org.opencontainers.image.description=\"Brutor Agent Control Skill Runner, Community Edition\"\n\
        LABEL org.opencontainers.image.licenses=\"Apache-2.0\"\n\
        LABEL org.opencontainers.image.source=\"https://github.com/brutor-ai/brutor-agent-control\"\n\
        LABEL org.opencontainers.image.vendor=\"Brutor AI Ltd\"\n\
        LABEL ai.brutor.edition=\"community\"\n";
    assert_eq!(
        label_faults(&community, &labels(good)),
        Vec::<String>::new()
    );
    for (bad, about) in [
        (
            good.replace("Apache-2.0", "LicenseRef-Brutor-Proprietary"),
            "licenses",
        ),
        (
            good.replace("brutor-ai/brutor-agent-control\"", "someone/brutor-skill-runner\""),
            "source",
        ),
        (good.replace("(Community Edition)", "(Enterprise Edition)"), "title"),
        (good.replace("Brutor AI Ltd", "Brutor"), "vendor"),
        (good.replace("=\"community\"", "=\"enterprise\""), "ai.brutor.edition"),
        (format!("{good}LABEL maintainer=\"A Person\"\n"), "maintainer"),
        (format!("{good}LABEL version=\"0.11.32\"\n"), "version"),
        (
            good.replace(
                "LABEL org.opencontainers.image.licenses=\"Apache-2.0\"\n",
                "",
            ),
            "licenses",
        ),
        (
            good.replace(
                "LABEL org.opencontainers.image.description=\"Brutor Agent Control Skill Runner, Community Edition\"\n",
                "LABEL description=\"Sandbox\"\n",
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
        "  IMAGE_NAME: brutor-ai/brutor-agent-control-skill-runner",
        "  # never ${{ github.repository }}",
    ] {
        assert!(derived_names(line).is_empty(), "{line}");
    }
}
