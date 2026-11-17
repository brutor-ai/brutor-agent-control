// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Guard: the Community tree holds no knowledge-base connector icon.
//!
//! The connectors (Confluence, Notion, Google Drive, Slack, GitHub, Jira,
//! SharePoint, the web crawler) are an Enterprise feature, and their icons
//! are third-party brand marks. They live in the Enterprise workspace
//! (`enterprise/assets/connector_icons/`) and are served by its
//! knowledge-base extension at `/v1/assets/connector-icons/<name>.svg`. This
//! scan holds that they do not come back into the Community tree: no icon
//! file, no directory for them, no source that serves or embeds them, and
//! nothing in the Community image's build that copies them.
//!
//! Proven against the shapes it forbids
//! ([`the_scan_catches_the_shapes_it_forbids`]).

use std::fs;
use std::path::{Path, PathBuf};

#[path = "support/roots.rs"]
mod roots;

/// The connector kinds of the catalogue; an icon is `<kind>.svg`.
const CONNECTORS: [&str; 8] = [
    "confluence",
    "github",
    "google_drive",
    "jira",
    "notion",
    "sharepoint",
    "slack",
    "web_crawler",
];

const THIS_FILE: &str = "tests/connector_icons_guard.rs";

/// Is the text of `path` held to the rule? Everything a Community build or
/// image is made from, and the two recordings of what a Community assembly
/// serves. Not the rest of `tests/`: this file names what it forbids, and
/// the credential and tenant matrices are recordings of the FULL
/// composition, which serves the route (the Community suite derives its own
/// expectation from them). Not `scripts/image-check.sh`: it is the check
/// that a built Community image holds none of them.
fn text_is_scanned(path: &str) -> bool {
    if path == "scripts/image-check.sh" {
        return false;
    }
    !path.starts_with("tests/")
        || path.starts_with("tests/fixtures/mounted_routes/")
        || path.starts_with("tests/fixtures/openapi_coverage/")
}

/// Every file of the Community tree: the repository without the Enterprise
/// workspace, build output and version control.
fn community_files(dir: &Path, enterprise: Option<&Path>, out: &mut Vec<PathBuf>) {
    for entry in fs::read_dir(dir).unwrap().flatten() {
        let path = entry.path();
        let name = entry.file_name().to_string_lossy().to_string();
        if path.is_dir() {
            let skipped = matches!(name.as_str(), "target" | ".git" | ".claude" | ".idea")
                || enterprise.is_some_and(|e| same(&path, e));
            if !skipped {
                community_files(&path, enterprise, out);
            }
        } else {
            out.push(path);
        }
    }
}

fn same(a: &Path, b: &Path) -> bool {
    matches!((a.canonicalize(), b.canonicalize()), (Ok(a), Ok(b)) if a == b)
}

/// Why `path` (relative to the Community root) is a connector icon or a
/// place for them, if it is one.
fn icon_path(path: &str) -> Option<&'static str> {
    let lower = path.to_ascii_lowercase();
    if lower.contains("connector_icons") || lower.contains("connector-icons") {
        return Some("a path for connector icons");
    }
    let file = lower.rsplit('/').next().unwrap_or(&lower);
    let stem = file.rsplit_once('.').map_or(file, |(stem, _)| stem);
    let image = [".svg", ".png", ".jpg", ".jpeg", ".webp", ".ico"]
        .iter()
        .any(|x| file.ends_with(x));
    if image && CONNECTORS.contains(&stem) {
        return Some("an image named after a connector");
    }
    None
}

/// The lines of `text` that serve, embed or copy connector icons.
fn icon_lines(text: &str) -> Vec<String> {
    text.lines()
        .enumerate()
        .filter(|(_, line)| {
            let lower = line.to_ascii_lowercase();
            lower.contains("connector_icons")
                || lower.contains("connector-icons")
                || lower.contains("connector_icon")
        })
        .map(|(i, line)| format!("{}: {}", i + 1, line.trim()))
        .collect()
}

fn relative(path: &Path) -> String {
    path.to_string_lossy()
        .trim_start_matches("./")
        .replace('\\', "/")
}

#[test]
fn the_community_tree_holds_no_connector_icon() {
    let enterprise = roots::tree("enterprise");
    let mut files = Vec::new();
    community_files(Path::new("."), enterprise.as_deref(), &mut files);
    assert!(
        files.len() > 500,
        "the scan found only {} files",
        files.len()
    );
    assert!(
        files.iter().any(|p| relative(p) == "src/routes/icons.rs")
            && files.iter().any(|p| relative(p) == "Dockerfile"),
        "the scan does not reach the icon routes and the Dockerfile"
    );
    assert!(
        !files.iter().any(|p| relative(p).starts_with("enterprise/")),
        "the scan walks the Enterprise workspace"
    );

    let mut hits = Vec::new();
    for path in &files {
        let rel = relative(path);
        // (This guard's own file is named after what it forbids.)
        if let Some(why) = icon_path(&rel).filter(|_| rel != THIS_FILE) {
            hits.push(format!("{rel}: {why}"));
        }
        if !text_is_scanned(&rel) {
            continue;
        }
        // Text that names them: source, build files, recordings. A binary
        // file is not text and is covered by its name above.
        let Ok(text) = fs::read_to_string(path) else {
            continue;
        };
        for line in icon_lines(&text) {
            hits.push(format!("{rel}:{line}"));
        }
    }
    assert!(
        hits.is_empty(),
        "the knowledge-base connector icons are the Enterprise Edition's \
         (enterprise/assets/connector_icons/, served by its knowledge-base extension); \
         the Community tree holds or names them:\n{}",
        hits.join("\n")
    );
}

/// Where this repository carries the Enterprise workspace, it is where the
/// icons are: all eight, each an SVG.
#[test]
fn the_enterprise_workspace_holds_the_eight_icons() {
    let Some(enterprise) = roots::tree("enterprise") else {
        return; // a Community tree alone: nothing to hold them
    };
    let directory = enterprise.join("assets/connector_icons");
    let mut held: Vec<String> = fs::read_dir(&directory)
        .unwrap_or_else(|e| panic!("{}: {e}", directory.display()))
        .flatten()
        .map(|e| e.file_name().to_string_lossy().to_string())
        .collect();
    held.sort();
    let expected: Vec<String> = CONNECTORS.iter().map(|c| format!("{c}.svg")).collect();
    assert_eq!(held, expected);
    for name in &held {
        let text = fs::read_to_string(directory.join(name)).expect("an SVG is text");
        assert!(text.contains("<svg"), "{name} is not an SVG");
    }
}

#[test]
fn the_scan_catches_the_shapes_it_forbids() {
    for path in [
        "assets/connector_icons/notion.svg",
        "assets/connector_icons/.gitkeep",
        "src/assets/connector-icons/readme.txt",
        "assets/notion.svg",
        "static/img/Google_Drive.PNG",
        "web_crawler.svg",
    ] {
        assert!(icon_path(path).is_some(), "{path} is not caught");
    }
    for path in [
        "src/routes/icons.rs",
        "src/services/slack.rs",
        "tests/fixtures/github.json",
        "deploy/grafana/provisioning/dashboards/notion.json",
        "assets/logo.svg",
    ] {
        assert!(icon_path(path).is_none(), "{path} is caught");
    }
    for line in [
        r#".route("/connector-icons/{filename}", get(get_connector_icon))"#,
        r#"include_bytes!("../../assets/connector_icons/notion.svg"),"#,
        "COPY assets/connector_icons/ assets/connector_icons/",
        "GET /v1/assets/connector-icons/{filename}",
        "const CONNECTOR_ICONS: &[(&str, &[u8])] = &[",
    ] {
        assert_eq!(icon_lines(line).len(), 1, "{line} is not caught");
    }
    for path in [
        "src/routes/icons.rs",
        "Dockerfile",
        ".github/workflows/docker-build.yml",
        "tests/fixtures/mounted_routes/community.txt",
        "tests/fixtures/openapi_coverage/community.txt",
    ] {
        assert!(text_is_scanned(path), "{path} is not scanned");
    }
    for path in [
        "tests/connector_icons_guard.rs",
        "tests/fixtures/credential_surface_matrix/part_1.json",
    ] {
        assert!(!text_is_scanned(path), "{path} is scanned");
    }
    for line in [
        ".route(\"/mcp-servers/{server_id}/icon\", get(get_mcp_icon))",
        "COPY src/ src/",
        "the connectors are an Enterprise feature",
    ] {
        assert!(icon_lines(line).is_empty(), "{line} is caught");
    }
}
