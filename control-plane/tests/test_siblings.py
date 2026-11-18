# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The tests that read another component or ask git find it in either layout
(`tests/siblings.py`): this repository beside the others under their
repository names, or the public repository, where this one is
`control-plane/` and the others are its sibling directories. The git root
is found by walking up. A skip names every place that was looked at.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests import siblings


def _beside(tmp_path: Path, *directories: str) -> Path:
    """`tmp_path/<this>` with `directories` beside it; returns this repository's directory."""
    for directory in directories:
        (tmp_path / directory).mkdir(parents=True)
    return tmp_path / "this"


def test_the_names_are_the_repositorys_then_the_public_directorys():
    assert siblings.THIS == ("brutor-gateway-control", "control-plane")
    assert siblings.SIBLINGS == {"admin-console": ("brutor-gateway-admin", "admin-console")}


def test_a_sibling_is_found_under_its_repository_name(tmp_path):
    repo = _beside(tmp_path, "this", "brutor-gateway-admin")
    assert siblings.sibling("admin-console", repo) == tmp_path / "brutor-gateway-admin"


def test_a_sibling_is_found_under_its_directory_in_the_public_repository(tmp_path):
    repo = _beside(tmp_path, "this", "admin-console")
    assert siblings.sibling("admin-console", repo) == tmp_path / "admin-console"


def test_the_repository_name_is_looked_at_first(tmp_path):
    repo = _beside(tmp_path, "this", "brutor-gateway-admin", "admin-console")
    assert siblings.sibling("admin-console", repo) == tmp_path / "brutor-gateway-admin"


def test_a_file_that_is_not_a_directory_is_not_the_sibling(tmp_path):
    repo = _beside(tmp_path, "this")
    (tmp_path / "brutor-gateway-admin").write_text("not a checkout\n")
    assert siblings.sibling("admin-console", repo) is None


def test_neither_place_skips_and_the_skip_names_both(tmp_path):
    repo = _beside(tmp_path, "this")
    assert siblings.sibling("admin-console", repo) is None
    with pytest.raises(pytest.skip.Exception) as skipped:
        siblings.sibling_file("admin-console", "src/lib/x.ts", repo)
    reason = str(skipped.value)
    assert str(tmp_path / "brutor-gateway-admin" / "src/lib/x.ts") in reason
    assert str(tmp_path / "admin-console" / "src/lib/x.ts") in reason
    # Raised inside a test it skips that test; a module-level lookup would
    # hide every other test of its file.
    assert skipped.value.allow_module_level is False


def test_a_sibling_without_the_file_fails_and_does_not_skip(tmp_path):
    """The sibling is there and the file the test reads is not: the file was
    moved or renamed, and a test that skipped would stop checking anything."""
    repo = _beside(tmp_path, "this", "admin-console/src/lib")
    with pytest.raises(AssertionError, match="admin-console/src/lib/x.ts"):
        siblings.sibling_file("admin-console", "src/lib/x.ts", repo)
    (tmp_path / "admin-console" / "src" / "lib" / "x.ts").write_text("export {}\n")
    assert siblings.sibling_file("admin-console", "src/lib/x.ts", repo) == tmp_path / "admin-console/src/lib/x.ts"


def test_an_unknown_sibling_is_an_error(tmp_path):
    with pytest.raises(KeyError, match="no sibling is called"):
        siblings.sibling("portal", _beside(tmp_path, "this"))


def test_the_git_root_is_this_directory_in_this_repository(tmp_path):
    repo = _beside(tmp_path, "this/.git", "this/tests")
    assert siblings.git_root(repo) == repo
    assert siblings.git_root(repo / "tests") == repo


def test_the_git_root_is_the_public_repositorys_root_above_this_directory(tmp_path):
    (tmp_path / "public" / ".git").mkdir(parents=True)
    repo = tmp_path / "public" / "control-plane"
    (repo / "tests").mkdir(parents=True)
    assert siblings.git_root(repo) == tmp_path / "public"
    assert siblings.git_root(repo / "tests") == tmp_path / "public"


def test_a_git_file_marks_a_root_too(tmp_path):
    """A worktree or a submodule has a `.git` file, not a directory."""
    repo = _beside(tmp_path, "this")
    (repo / ".git").write_text("gitdir: /elsewhere\n")
    assert siblings.git_root(repo) == repo


def test_no_git_root_is_none_and_says_where_it_looked(tmp_path):
    repo = _beside(tmp_path, "this")
    # tmp_path is not inside a git work tree on any machine this runs on;
    # the walk stops at the filesystem root.
    assert siblings.git_root(repo) is None
    assert siblings.no_git_root(repo) == f"no .git at {repo} or at any directory above it: what is tracked cannot be asked"


def test_the_public_repository_is_recognised_by_its_root_and_this_directorys_name(tmp_path):
    (tmp_path / "public" / ".git").mkdir(parents=True)
    repo = tmp_path / "public" / "control-plane"
    repo.mkdir()
    assert siblings.public_root(repo) == tmp_path / "public"
    # This repository: its own root.
    (tmp_path / "brutor-gateway-control" / ".git").mkdir(parents=True)
    own = tmp_path / "brutor-gateway-control"
    assert siblings.public_root(own) is None
    # A directory of another name inside some repository is not the public one.
    other = tmp_path / "public" / "elsewhere"
    other.mkdir()
    assert siblings.public_root(other) is None
