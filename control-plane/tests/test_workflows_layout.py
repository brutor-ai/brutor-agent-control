# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The tests that read CI find it in either layout (`tests/workflows.py`): this
repository's `.github/workflows`, or the public repository's root workflows
above this component's directory. Neither is a failure, never a skip.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests import workflows


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "brutor-gateway-control"
    (repo / ".github" / "workflows").mkdir(parents=True)
    for name in ("ci.yml", "docker-build.yml"):
        (repo / ".github" / "workflows" / name).write_text(f"name: {name}\n")
    return repo


def _monorepo(tmp_path: Path) -> Path:
    root = tmp_path / "public"
    (root / ".github" / "workflows").mkdir(parents=True)
    for name in ("control-plane.yml", "publish-images.yml", "core-gateway.yml"):
        (root / ".github" / "workflows" / name).write_text(f"name: {name}\n")
    repo = root / "control-plane"
    repo.mkdir()
    return repo


def test_this_checkout_has_the_workflows_the_tests_read():
    for name in workflows.PUBLIC:
        assert workflows.workflow_path(name).is_file()


def test_this_repository_reads_its_own_workflows(tmp_path):
    repo = _repository(tmp_path)
    assert workflows.layout(repo) == workflows.REPOSITORY
    assert workflows.workflow_path("ci.yml", repo) == repo / ".github" / "workflows" / "ci.yml"
    assert workflows.workflow_text("docker-build.yml", repo) == "name: docker-build.yml\n"
    assert [p.name for p in workflows.workflow_files(repo)] == ["ci.yml", "docker-build.yml"]
    assert workflows.monorepo_core_dir(repo) is None


def test_the_public_repository_reads_the_root_workflows(tmp_path):
    repo = _monorepo(tmp_path)
    root = repo.parent / ".github" / "workflows"
    assert workflows.layout(repo) == workflows.MONOREPO
    assert workflows.workflow_path("ci.yml", repo) == root / "control-plane.yml"
    assert workflows.workflow_path("docker-build.yml", repo) == root / "publish-images.yml"
    assert [p.name for p in workflows.workflow_files(repo)] == ["control-plane.yml", "core-gateway.yml", "publish-images.yml"]
    assert workflows.monorepo_core_dir(repo) == "${{ github.workspace }}/core-gateway"


def test_the_root_workflows_are_the_cuts_names():
    """The public names are the cut's: the control plane's workflow and the
    images' publish workflow."""
    assert workflows.PUBLIC == {"ci.yml": "control-plane.yml", "docker-build.yml": "publish-images.yml"}


def test_neither_layout_fails_and_does_not_skip(tmp_path):
    repo = tmp_path / "control-plane"
    repo.mkdir()
    for call in (
        lambda: workflows.layout(repo),
        lambda: workflows.workflow_path("ci.yml", repo),
        lambda: workflows.workflow_files(repo),
    ):
        with pytest.raises(AssertionError, match="no CI workflows"):
            call()


def test_a_missing_workflow_fails_in_either_layout(tmp_path):
    repo = _repository(tmp_path)
    (repo / ".github" / "workflows" / "ci.yml").unlink()
    with pytest.raises(AssertionError, match="ci.yml.*does not exist"):
        workflows.workflow_path("ci.yml", repo)
    mono = _monorepo(tmp_path)
    (mono.parent / ".github" / "workflows" / "control-plane.yml").unlink()
    with pytest.raises(AssertionError, match="control-plane.yml does not exist"):
        workflows.workflow_path("ci.yml", mono)
    with pytest.raises(AssertionError, match="no workflow of the public repository"):
        workflows.workflow_path("qdrant-singleton-check.yml", mono)


def test_the_public_repository_names_its_images_in_the_bake_file(tmp_path):
    """In the public layout the published image names are written once, in
    `package/docker-bake.hcl` at the repository root; this repository has no
    bake file (its image workflow names the images)."""
    repo = _monorepo(tmp_path)
    with pytest.raises(AssertionError, match="package/docker-bake.hcl does not exist"):
        workflows.bake_text(repo)
    (repo.parent / "package").mkdir()
    (repo.parent / "package" / "docker-bake.hcl").write_text('target "control-plane" {}\n')
    assert workflows.BAKE == "package/docker-bake.hcl"
    assert workflows.bake_path(repo) == repo.parent / "package" / "docker-bake.hcl"
    assert workflows.bake_text(repo) == 'target "control-plane" {}\n'
    with pytest.raises(AssertionError, match="only the public repository"):
        workflows.bake_path(_repository(tmp_path))
