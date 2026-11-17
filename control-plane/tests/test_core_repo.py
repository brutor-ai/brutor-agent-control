# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The parity guards read the core at its committed HEAD.

A guard that compares this repository with the gateway's source read the
gateway's working tree, so it went red while somebody was half-way through
an edit there and compared against a core nobody had committed. It reads the
commit now: `tests/core_repo.py` exports the paths the tests read from `HEAD`
of the checkout and falls back to the tree only where there is no git
repository. `BRUTOR_CORE_REPO` says where the core is, and nothing else does:
without it a test that reads the core is skipped, and the reason names it.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from tests import core_repo

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _git(cwd, *args):
    subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


@pytest.fixture()
def checkout(tmp_path, monkeypatch):
    repo = tmp_path / "core"
    (repo / "src" / "services").mkdir(parents=True)
    (repo / "tests" / "fixtures").mkdir(parents=True)
    (repo / "src" / "services" / "providers.rs").write_text("committed\n")
    (repo / "tests" / "fixtures" / "f.json").write_text("{}\n")
    monkeypatch.setenv("BRUTOR_CORE_REPO", str(repo))
    monkeypatch.setattr(core_repo, "_resolved", {})
    return repo


def _commit(repo):
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "one")
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, stdout=subprocess.PIPE
    ).stdout.decode().strip()


def test_a_git_checkout_is_read_at_its_committed_head_not_its_working_tree(checkout):
    head = _commit(checkout)
    # Somebody is editing the core: a changed file, a new one, a deleted one.
    (checkout / "src" / "services" / "providers.rs").write_text("half an edit\n")
    (checkout / "src" / "services" / "new.rs").write_text("uncommitted\n")
    (checkout / "tests" / "fixtures" / "f.json").unlink()

    assert core_repo.core_source("services", "providers.rs").read_text() == "committed\n"
    assert not core_repo.core_source("services", "new.rs").exists()
    assert (core_repo.core_repo_path() / "tests" / "fixtures" / "f.json").read_text() == "{}\n"
    # Not the checkout itself, and the message names the commit.
    assert core_repo.core_repo_path() != checkout
    assert head in core_repo.core_revision()
    assert str(checkout) in core_repo.core_revision()


def test_a_directory_that_is_not_a_git_checkout_is_read_as_it_is(checkout):
    assert core_repo.core_repo_path() == checkout
    assert core_repo.core_source("services", "providers.rs").read_text() == "committed\n"
    assert "not a git checkout" in core_repo.core_revision()


def test_a_plain_directory_inside_another_repository_is_not_taken_for_a_checkout(checkout, tmp_path, monkeypatch):
    # The core unpacked into a directory of some other repository: that
    # repository's HEAD is not the core's.
    _commit(tmp_path)
    (checkout / "src" / "services" / "providers.rs").write_text("the unpacked tree\n")
    assert core_repo.core_repo_path() == checkout
    assert core_repo.core_source("services", "providers.rs").read_text() == "the unpacked tree\n"


def test_the_override_says_where_the_core_is(checkout):
    assert core_repo.core_checkout_path() == checkout


def test_a_variable_that_names_no_directory_fails(tmp_path, monkeypatch):
    """Given and not there: a failure, never a skip. A parity guard pointed
    at nothing must not pass by skipping."""
    monkeypatch.setenv("BRUTOR_CORE_REPO", str(tmp_path / "nowhere"))
    monkeypatch.setattr(core_repo, "_resolved", {})
    with pytest.raises(AssertionError, match="BRUTOR_CORE_REPO="):
        core_repo.core_source("services", "providers.rs")


@pytest.mark.parametrize("value", [None, "", "   "])
def test_without_the_variable_the_core_is_not_looked_for_anywhere(monkeypatch, value):
    """No default: the core is not assumed to sit beside this repository (in
    the public layout it does not, and its directory has another name). A test
    that reads it is skipped, and the reason names the variable."""
    if value is None:
        monkeypatch.delenv("BRUTOR_CORE_REPO", raising=False)
    else:
        monkeypatch.setenv("BRUTOR_CORE_REPO", value)
    monkeypatch.setattr(core_repo, "_resolved", {})
    for read in (core_repo.core_checkout_path, core_repo.core_repo_path, core_repo.core_revision,
                 lambda: core_repo.core_source("services", "providers.rs")):
        with pytest.raises(pytest.skip.Exception) as skipped:
            read()
        assert "BRUTOR_CORE_REPO is not set" in str(skipped.value)
        # Raised inside a test, it skips that test; at import time it is an
        # error, not a skipped module: a module-level read would hide every
        # other test of the file.
        assert skipped.value.allow_module_level is False
    assert core_repo.core_checkout_or_none() is None


def test_no_test_writes_the_path_of_the_core():
    """`tests/core_repo.py` is the one place that says where the core is, and
    it says it from the variable alone: no file of either test tree, no
    script, spells the sibling directory."""
    import ast
    import tokenize

    from tests.source_trees import REPO, test_sources

    name = "brutor-gateway" + "-core"
    spelt = []
    files = [*test_sources(), *sorted((REPO / "scripts").rglob("*.py")), REPO / "conftest.py"]
    for path in files:
        if "__pycache__" in path.parts or not path.is_file():
            continue
        with path.open(encoding="utf-8") as handle:
            for token in tokenize.generate_tokens(handle.readline):
                if token.type != tokenize.STRING:
                    continue
                try:
                    value = ast.literal_eval(token.string)
                except (ValueError, SyntaxError):
                    continue
                if isinstance(value, str) and (value == name or f"../{name}" in value or value.endswith(f"/{name}")):
                    spelt.append(f"{path.relative_to(REPO)}:{token.start[0]}")
    assert spelt == []


def test_the_real_core_is_read_at_head():
    """The core `BRUTOR_CORE_REPO` names, when it is a git checkout: what the
    guards read is a commit, whatever the working tree looks like. The export
    is made once per run, at the HEAD of the moment; somebody may commit to the
    core while a long run is going, so the commit read is the one the export
    names, and it is HEAD or behind it."""
    import re

    checkout = core_repo.core_checkout_path()
    if not (checkout / ".git").exists():
        pytest.skip(f"{checkout} is not a git checkout")
    named = re.search(r"commit ([0-9a-f]{40})", core_repo.core_revision())
    assert named, core_repo.core_revision()
    commit = named.group(1)
    subprocess.run(["git", "-C", str(checkout), "merge-base", "--is-ancestor", commit, "HEAD"], check=True)
    committed = subprocess.run(
        ["git", "-C", str(checkout), "show", f"{commit}:src/services/providers.rs"], check=True, stdout=subprocess.PIPE
    ).stdout
    assert core_repo.core_source("services", "providers.rs").read_bytes() == committed
