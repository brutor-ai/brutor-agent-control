# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
No log, coverage database or environment file is tracked.

The repository tracked `.coverage` (a 6 MB database every test run
rewrites) and two test logs, `e2e-tests.log` and `unit-tests.log`, with
home-directory paths and, in one, a token. They are ignored now, and this
holds both halves: the ignore rules exist, and no tracked file matches one.
`.env.example` is the template and stays tracked.
"""

from __future__ import annotations

import fnmatch
import shutil
import subprocess

import pytest

from tests.source_trees import REPO

# What a run or a deployment writes. `.gitignore` must ignore each.
PATTERNS = ("*.log", ".coverage", ".coverage.*", "coverage.xml", "htmlcov", ".env", ".env.*")
# The generated templates of the two editions (`scripts/gen_env_docs.py`).
KEPT = {".env.example", "enterprise/.env.example"}

# A name of each kind, at the root and in a directory.
SAMPLES = (
    "unit-tests.log", "e2e-tests.log", "tests/run.log", "enterprise/anything.log",
    ".coverage", ".coverage.host.1234", "enterprise/.coverage", "coverage.xml", "htmlcov/index.html",
    ".env", ".env.local", ".env.production", "enterprise/.env",
)

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or not (REPO / ".git").exists(),
    reason="not a git checkout: what is tracked cannot be asked",
)


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=True).stdout


def _matches(path: str) -> bool:
    if path in KEPT:
        return False
    return any(fnmatch.fnmatch(part, pattern) for part in path.split("/") for pattern in PATTERNS)


def test_the_patterns_find_what_they_are_for():
    assert all(_matches(sample) for sample in SAMPLES)
    assert not _matches(".env.example")
    assert not _matches("app/core/logging_system.py")
    assert not _matches("app/api/v1/endpoints/admin/audit_logs.py")


def test_no_tracked_file_is_a_log_a_coverage_database_or_an_environment_file():
    tracked = _git("ls-files").splitlines()
    assert len(tracked) > 1000, "git ls-files returned too little to mean anything"
    assert sorted(path for path in tracked if _matches(path)) == []


def test_git_ignores_each_of_them():
    ignored = subprocess.run(
        ["git", "check-ignore", "--no-index", *SAMPLES], cwd=REPO, capture_output=True, text=True
    ).stdout.splitlines()
    assert sorted(set(SAMPLES) - set(ignored)) == []


def test_the_template_is_tracked_and_not_ignored():
    assert ".env.example" in _git("ls-files", ".env.example").splitlines()
    not_ignored = subprocess.run(
        ["git", "check-ignore", "--no-index", ".env.example"], cwd=REPO, capture_output=True, text=True
    )
    assert not_ignored.stdout.strip() == ""


def test_no_tracked_file_is_ignored_by_any_rule():
    # The general form: whatever `.gitignore` says, nothing tracked matches.
    assert _git("ls-files", "-ci", "--exclude-standard").splitlines() == []
