# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The coverage floors of both editions and both packages
(`tests/coverage_floors.py`).

Until now one measurement had a gate: `app` under the Enterprise-mode run
(`fail_under`). `app` under the Community-mode run (measured at 80.51 %)
and the Enterprise package (73.44 %) had none.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib

import pytest

from tests import coverage_floors
from tests.source_trees import REPO

# The floors as decided. One only goes up: raise the number here with the
# number in `pyproject.toml`.
AT_LEAST = {
    "enterprise": {"app": 82, "brutor_enterprise": 73},
    "community": {"app": 80},
}


def test_every_measurement_has_a_floor_and_none_went_down():
    table = coverage_floors.floors()
    assert {e: sorted(p) for e, p in table.items()} == {e: sorted(p) for e, p in AT_LEAST.items()}
    for edition, packages in AT_LEAST.items():
        for package, floor in packages.items():
            assert isinstance(table[edition][package], int)
            assert table[edition][package] >= floor, (edition, package)


def test_the_enterprise_runs_floor_for_app_is_fail_under():
    # `pytest --cov=app` is held by pytest-cov; the table says the same number.
    document = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert document["tool"]["coverage"]["report"]["fail_under"] == coverage_floors.floors()["enterprise"]["app"]


TABLE = {"enterprise": {"app": 82, "brutor_enterprise": 73}, "community": {"app": 80}}


@pytest.mark.parametrize(
    "edition, totals",
    [
        ("enterprise", {"app": 82.0, "brutor_enterprise": 73.0}),
        ("enterprise", {"app": 91.3, "brutor_enterprise": 73.44}),
        ("community", {"app": 80.51}),
        ("community", {"app": 80.0}),
    ],
)
def test_at_or_above_the_floor_passes(edition, totals):
    assert coverage_floors.below_floor(edition, totals, TABLE, precision=2) == []


@pytest.mark.parametrize(
    "edition, totals, named",
    [
        ("community", {"app": 79.99}, ["app"]),
        ("enterprise", {"app": 81.99, "brutor_enterprise": 73.5}, ["app"]),
        ("enterprise", {"app": 82.5, "brutor_enterprise": 72.99}, ["brutor_enterprise"]),
        ("enterprise", {"app": 10.0, "brutor_enterprise": 10.0}, ["app", "brutor_enterprise"]),
        # A measurement with no floor is not a pass.
        ("community", {"brutor_enterprise": 99.0}, ["brutor_enterprise"]),
    ],
)
def test_below_the_floor_fails_and_names_the_package(edition, totals, named):
    failures = coverage_floors.below_floor(edition, totals, TABLE, precision=2)
    assert [line.split(":")[0] for line in failures] == named


def test_what_cov_names_is_mapped_to_packages():
    assert coverage_floors.measured_packages(["app"]) == ["app"]
    assert coverage_floors.measured_packages(["app", "brutor_enterprise"]) == ["app", "brutor_enterprise"]
    assert coverage_floors.measured_packages(["enterprise/brutor_enterprise"]) == ["brutor_enterprise"]
    assert coverage_floors.measured_packages([str(REPO / "app")]) == ["app"]
    assert coverage_floors.measured_packages(["tests"]) == []
    assert coverage_floors.measured_packages(None) == []


# --- the plugin, in a pytest run of its own ---------------------------------

# One fast test of the suite, in a pytest run of its own.
PROBE = "tests/test_distribution_version.py::test_the_version_is_a_release_version"


def _run(tmp_path, *args, edition=None):
    """A real pytest run over one trivial test, measuring coverage: nearly
    nothing of a package is covered, so every floor is missed."""
    import os

    env = {**os.environ, "COVERAGE_FILE": str(tmp_path / ".coverage")}
    env.pop("BRUTOR_TEST_EDITION", None)
    if edition:
        env["BRUTOR_TEST_EDITION"] = edition
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--cov-report=", *args, PROBE],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=600,
    )


def test_a_community_run_below_its_floor_fails_and_says_so(tmp_path):
    result = _run(tmp_path, "--cov=app", edition="community")
    assert result.returncode == 1, result.stdout[-2000:]
    assert "1 passed" in result.stdout
    assert "coverage floors" in result.stdout
    assert "FAIL app:" in result.stdout and "community run's floor of 80 %" in result.stdout


# The Enterprise runs (they need `brutor_enterprise`): `enterprise/tests/test_coverage_floors.py`.


def test_without_coverage_nothing_is_held(tmp_path):
    result = _run(tmp_path, "--no-cov", edition="community")
    assert result.returncode == 0, result.stdout[-2000:]
    assert "coverage floors" not in result.stdout
