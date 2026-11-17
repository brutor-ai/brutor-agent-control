// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Where this repository's CI workflows are: ONE function, [`find`], for
//! every test that reads a workflow.
//!
//! The workflows live in one of two places, and a test never spells either:
//!
//! * **its own repository**: `.github/workflows/` under the Community root,
//!   where the image workflow is `docker-build.yml`;
//! * **one directory of the public repository**: this tree is a component
//!   directory (`core-gateway/`) and GitHub runs only the repository root's
//!   workflows, so they are `../.github/workflows/`: the component's own
//!   `<component>.yml` (it builds and starts the image). The published
//!   image names are written once, in `../package/docker-bake.hcl` (the
//!   root's `publish-images.yml` publishes every image through it, by hand):
//!   in this layout that file is what [`Workflows::publish`] is.
//!
//! Neither there is a failure, never a skip: a guard that reads no workflow
//! would pass without reading one.
//!
//! Included by path (`#[path = "support/workflows.rs"] mod workflows;`).

use std::path::{Path, PathBuf};

/// The public repository's image build, relative to its root: the one place
/// the published image names are written.
pub const BAKE: &str = "package/docker-bake.hcl";

/// Which of the two places the workflows were found in.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Layout {
    /// `.github/workflows/` of this repository.
    Own,
    /// `../.github/workflows/` of the repository this tree is a directory of.
    Monorepo,
}

/// The workflows a test reads.
#[derive(Debug)]
pub struct Workflows {
    pub layout: Layout,
    /// Every workflow of the repository that runs them, sorted.
    pub all: Vec<PathBuf>,
    /// The workflow that names and builds this component's image: the own
    /// `docker-build.yml`, or the root's `<component>.yml`.
    pub image: PathBuf,
    /// What names the published image: the own `docker-build.yml`, or the
    /// public repository's bake file ([`BAKE`]).
    pub publish: PathBuf,
    /// The files that are this component's: all of [`Workflows::all`] in
    /// its own repository, the image workflow and the bake file in the
    /// public one.
    pub mine: Vec<PathBuf>,
    /// The component's directory in the public repository (`core-gateway`).
    pub component: String,
}

/// The workflows of the tree at `root`, whose directory in the public
/// repository is `component` and whose own image workflow is `own_image`.
/// Panics when neither layout is there.
pub fn find(root: &Path, component: &str, own_image: &str) -> Workflows {
    let own = root.join(".github/workflows");
    let parent = root
        .canonicalize()
        .unwrap_or_else(|_| root.to_path_buf())
        .parent()
        .map(Path::to_path_buf);
    let shared = parent.as_ref().map(|p| p.join(".github/workflows"));
    let (layout, dir, image, publish) = if own.is_dir() {
        let image = own.join(own_image);
        (Layout::Own, own, image.clone(), image)
    } else if let Some(dir) = shared.filter(|d| d.join(format!("{component}.yml")).is_file()) {
        let image = dir.join(format!("{component}.yml"));
        let publish = parent.expect("the directory above").join(BAKE);
        (Layout::Monorepo, dir, image, publish)
    } else {
        panic!(
            "no workflows: neither {} nor ../.github/workflows/{component}.yml beside {} is \
             there, and a guard that reads no workflow would pass without reading one",
            own.display(),
            root.display()
        );
    };
    for file in [&image, &publish] {
        assert!(
            file.is_file(),
            "the {layout:?} layout has no {}",
            file.display()
        );
    }
    let mut all: Vec<PathBuf> = std::fs::read_dir(&dir)
        .unwrap_or_else(|e| panic!("{}: {e}", dir.display()))
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|x| x == "yml" || x == "yaml"))
        .collect();
    all.sort();
    let mine = match layout {
        Layout::Own => all.clone(),
        Layout::Monorepo => vec![image.clone(), publish.clone()],
    };
    Workflows {
        layout,
        all,
        mine,
        image,
        publish,
        component: component.to_string(),
    }
}

/// How many times the bake file publishes `repository` built from this
/// component's directory: the target block
/// `target "<component>" {`, `inherits = ["community"]`,
/// `context = "<component>"`, `tags = tags("<repository>")`.
pub fn published_from_component(bake: &str, repository: &str, component: &str) -> usize {
    bake.matches(&format!(
        "target \"{component}\" {{\n  inherits = [\"community\"]\n  context = \"{component}\"\n  tags = tags(\"{repository}\")\n"
    ))
    .count()
}

/// The bake file sets the registry to `ghcr.io`, once.
pub fn bake_registry_is_ghcr_once(bake: &str) -> bool {
    bake.matches("variable \"REGISTRY\" {\n  default = \"ghcr.io\"\n}\n")
        .count()
        == 1
}

/// How many times a component's workflow builds its image through the bake
/// file's target for it.
pub fn built_through_bake(workflow: &str, component: &str) -> usize {
    workflow
        .matches(&format!("docker buildx bake -f {BAKE} {component} --load"))
        .count()
}

const COMPONENT_OF_FIXTURE: &str = "core-gateway";

/// The proof that [`find`] reads each layout and fails on neither: run by
/// a test of the including guard.
pub fn check_the_layouts() {
    let base = std::env::temp_dir().join(format!(
        "workflows-layouts-{}-{:?}",
        std::process::id(),
        std::thread::current().id()
    ));
    let _ = std::fs::remove_dir_all(&base);
    let write = |path: &Path| {
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, "name: x\n").unwrap();
    };

    // Its own repository.
    let own = base.join("own");
    write(&own.join(".github/workflows/docker-build.yml"));
    write(&own.join(".github/workflows/ci.yml"));
    let found = find(&own, "core-gateway", "docker-build.yml");
    assert_eq!(found.layout, Layout::Own);
    assert_eq!(found.all.len(), 2);
    assert_eq!(found.image, found.publish);
    assert!(found.image.ends_with("docker-build.yml"));

    // A directory of the public repository.
    let mono = base.join("mono");
    write(&mono.join(".github/workflows/core-gateway.yml"));
    write(&mono.join(".github/workflows/publish-images.yml"));
    write(&mono.join(".github/workflows/skill-runner.yml"));
    write(&mono.join(BAKE));
    std::fs::create_dir_all(mono.join("core-gateway")).unwrap();
    let found = find(
        &mono.join("core-gateway"),
        "core-gateway",
        "docker-build.yml",
    );
    assert_eq!(found.layout, Layout::Monorepo);
    assert_eq!(found.all.len(), 3);
    assert!(found.image.ends_with("core-gateway.yml"));
    assert!(found.publish.ends_with("package/docker-bake.hcl"));
    assert_eq!(found.mine, [found.image.clone(), found.publish.clone()]);
    assert_eq!(found.component, COMPONENT_OF_FIXTURE);

    // Another component's workflow is not this one's.
    std::fs::create_dir_all(mono.join("other")).unwrap();
    let neither = mono.join("other");
    let failed = std::panic::catch_unwind(|| find(&neither, "other", "docker-build.yml"));
    assert!(failed.is_err(), "neither layout is a failure");

    // Neither: a failure, never an empty list.
    let bare = base.join("bare");
    std::fs::create_dir_all(&bare).unwrap();
    let failed = std::panic::catch_unwind(|| find(&bare, "core-gateway", "docker-build.yml"));
    assert!(failed.is_err(), "neither layout is a failure");

    let block = "target \"core-gateway\" {\n  inherits = [\"community\"]\n  context = \"core-gateway\"\n  tags = tags(\"brutor-ai/x\")\n}\n";
    assert_eq!(
        published_from_component(block, "brutor-ai/x", "core-gateway"),
        1
    );
    assert_eq!(
        published_from_component(block, "brutor-ai/y", "core-gateway"),
        0
    );
    assert_eq!(
        published_from_component(
            &block.replace("context = \"core-gateway\"", "context = \"other\""),
            "brutor-ai/x",
            "core-gateway"
        ),
        0
    );
    assert!(bake_registry_is_ghcr_once(
        "variable \"REGISTRY\" {\n  default = \"ghcr.io\"\n}\n"
    ));
    assert!(!bake_registry_is_ghcr_once(
        "variable \"REGISTRY\" {\n  default = \"docker.io\"\n}\n"
    ));
    assert_eq!(
        built_through_bake(
            "run: docker buildx bake -f package/docker-bake.hcl core-gateway --load --set x",
            "core-gateway"
        ),
        1
    );
    let _ = std::fs::remove_dir_all(&base);
}
