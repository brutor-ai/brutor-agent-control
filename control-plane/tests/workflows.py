# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Where this component's CI workflows are, in either layout.

* This repository: `.github/workflows/<name>.yml` beside `app/`.
* The public repository (one monorepo, this component in its directory
  `control-plane/`): the component's own `.github/workflows` is not there,
  and the root's `.github/workflows/<public name>.yml` replaces it. `PUBLIC`
  says which root workflow carries what a workflow of this repository did.

A test that reads a workflow takes it from here. When neither layout has it,
the test FAILS and says where it looked: a check of CI that cannot find the CI
must not pass by skipping.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from tests.source_trees import REPO

REPOSITORY = "repository"
MONOREPO = "monorepo"

# The root workflows of the public repository that carry what each workflow of
# this repository does for the Community Edition.
PUBLIC = {
    # The Community suite, the audit, the fresh-install schema, the Rust
    # schema fixture and the Community image built and started.
    "ci.yml": "control-plane.yml",
    # The images' names, built and published: the Community image is
    # published by the root's publish workflow.
    "docker-build.yml": "publish-images.yml",
}

# The core's directory in the public repository, beside this component's: the
# same checkout.
MONOREPO_CORE_DIR = "core-gateway"

# The public repository's image build, relative to its root: the one place the
# published image names, their tags and the version and revision labels are
# written. Its root workflows build and publish through it.
BAKE = "package/docker-bake.hcl"


def _own(repo: Path) -> Path:
    return repo / ".github" / "workflows"


def _root(repo: Path) -> Path:
    return repo.parent / ".github" / "workflows"


def layout(repo: Path = REPO) -> str:
    """`repository` when this checkout has its own workflows, `monorepo` when
    the directory above it has them; fails when neither does."""
    if _own(repo).is_dir():
        return REPOSITORY
    if _root(repo).is_dir():
        return MONOREPO
    raise AssertionError(
        f"no CI workflows: neither {_own(repo)} (this repository) nor {_root(repo)} "
        "(the public repository's root) exists"
    )


def workflows_dir(repo: Path = REPO) -> Path:
    return _own(repo) if layout(repo) == REPOSITORY else _root(repo)


def workflow_path(name: str, repo: Path = REPO) -> Path:
    """The workflow that is `name` in this repository: that file, or in the
    public repository the root workflow `PUBLIC[name]`. Fails when it is not
    there."""
    if layout(repo) == REPOSITORY:
        path = _own(repo) / name
    else:
        public = PUBLIC.get(name)
        if public is None:
            raise AssertionError(f"{name}: no workflow of the public repository is named for it (tests/workflows.py PUBLIC)")
        path = _root(repo) / public
    if not path.is_file():
        raise AssertionError(f"{name}: {path} does not exist ({layout(repo)} layout)")
    return path


def workflow_text(name: str, repo: Path = REPO) -> str:
    return workflow_path(name, repo).read_text(encoding="utf-8")


def workflow_files(repo: Path = REPO) -> List[Path]:
    """Every workflow of the layout: this repository's, or the public
    repository's root ones."""
    return sorted(workflows_dir(repo).glob("*.y*ml"))


def monorepo_core_dir(repo: Path = REPO) -> Optional[str]:
    """In the public layout, the core's directory as a workflow names it:
    `${{ github.workspace }}/core-gateway`, the same checkout. None in this
    repository, whose CI has no core."""
    if layout(repo) == MONOREPO:
        return "${{ github.workspace }}/" + MONOREPO_CORE_DIR
    return None


def bake_path(repo: Path = REPO) -> Path:
    """The public repository's bake file. Fails in this repository, which has
    none (its image workflow names the images), and when it is not there."""
    if layout(repo) != MONOREPO:
        raise AssertionError(f"{BAKE}: only the public repository has one (this is the {layout(repo)} layout)")
    path = repo.parent / BAKE
    if not path.is_file():
        raise AssertionError(f"{path}: {BAKE} does not exist ({MONOREPO} layout)")
    return path


def bake_text(repo: Path = REPO) -> str:
    return bake_path(repo).read_text(encoding="utf-8")
