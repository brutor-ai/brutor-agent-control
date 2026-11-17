# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The coverage floors, one per edition under test and package measured.

`[tool.coverage.report] fail_under` in `pyproject.toml` is one number: the
Community package (`app`) under the Enterprise-mode run, which is what
`pytest --cov=app` measures. Two measurements had no gate: `app` under the
Community-mode run, and the Enterprise package (`brutor_enterprise`). The
floors are in `[tool.brutor.coverage-floors]` in `pyproject.toml`, and this
plugin (registered by `tests/conftest.py`) holds a run to them:

    pytest --cov=app --cov=brutor_enterprise                    # Enterprise mode: both packages
    BRUTOR_TEST_EDITION=community pytest --cov=app tests/       # Community mode: app

Each package measured by the run is totalled on its own (statements and
branches, as `coverage report --include=<package>/*`) and compared with its
floor for the edition under test. A package below its floor fails the run
(exit status 1) and the summary says which.

`pytest --cov=app` in Enterprise mode is left exactly as it was: pytest-cov
applies `fail_under` to the one total. For any other measurement that
single number is the wrong one (it is neither package's floor), so
pytest-cov's check is switched off for that run and the per-package floors
decide. A `--cov-fail-under` given on the command line is the caller's and
is left alone.

A floor only goes up (`tests/test_coverage_floors.py` pins the numbers).
"""

from __future__ import annotations

import io
import tomllib
from pathlib import Path
from typing import Dict, List, Optional

import pytest

REPO = Path(__file__).resolve().parents[1]
PLUGIN_NAME = "brutor-coverage-floors"

# package -> where its sources are, relative to the repository root.
PACKAGE_DIRS = {
    "app": "app",
    "brutor_enterprise": "enterprise/brutor_enterprise",
}


def floors() -> Dict[str, Dict[str, int]]:
    """`{edition: {package: whole percent}}` from `pyproject.toml`."""
    document = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    return document["tool"]["brutor"]["coverage-floors"]


def below_floor(edition: str, totals: Dict[str, float], table: Optional[Dict[str, Dict[str, int]]] = None,
                precision: int = 0) -> List[str]:
    """The failures of a run of `edition` that measured `totals`
    (`{package: percent}`): one line per package below its floor, and one
    per package measured that has no floor (a measurement nobody gates)."""
    from coverage.results import should_fail_under

    table = floors() if table is None else table
    failures = []
    for package, total in sorted(totals.items()):
        floor = table.get(edition, {}).get(package)
        if floor is None:
            failures.append(f"{package}: no coverage floor for the {edition} run in [tool.brutor.coverage-floors]")
        elif should_fail_under(total, floor, precision):
            failures.append(f"{package}: {total:.2f} % is below the {edition} run's floor of {floor} %")
    return failures


def measured_packages(cov_source) -> List[str]:
    """The packages with a floor among what `--cov` names."""
    found = []
    for source in cov_source or []:
        if source is True or source is None:
            continue
        name = str(source).rstrip("/")
        for package, directory in PACKAGE_DIRS.items():
            if name in (package, directory) or Path(name).resolve() == (REPO / directory).resolve():
                if package not in found:
                    found.append(package)
    return found


class CoverageFloors:
    def __init__(self, config) -> None:
        self.config = config
        self.results: List[str] = []
        self.failures: List[str] = []
        self.packages: List[str] = []
        self.cov = config.pluginmanager.getplugin("_cov")
        if self.cov is None or getattr(self.cov.options, "no_cov", False):
            self.cov = None
            return
        from tests import app_factory

        self.edition = app_factory.edition_under_test()
        given_on_the_command_line = any(
            str(arg).startswith("--cov-fail-under") for arg in config.invocation_params.args
        )
        if given_on_the_command_line:
            self.cov = None
            return
        self.packages = measured_packages(self.cov.options.cov_source)
        if self.edition == app_factory.ENTERPRISE and self.packages == ["app"]:
            # The gate that was there: pytest-cov and `fail_under`.
            self.packages = []
            return
        if self.packages:
            self.cov.options.cov_fail_under = 0

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session, exitstatus) -> None:
        if self.cov is None or not self.packages or self.cov.cov_controller is None:
            return
        if getattr(self.config.option, "collectonly", False):
            return
        coverage = self.cov.cov_controller.cov
        precision = self.cov.options.cov_precision or 0
        totals = {}
        for package in self.packages:
            include = [str(REPO / PACKAGE_DIRS[package] / "*")]
            try:
                totals[package] = coverage.report(include=include, file=io.StringIO(), skip_empty=False)
            except Exception as error:  # noqa: BLE001 - nothing of the package was measured
                self.failures.append(f"{package}: could not be totalled ({type(error).__name__}: {error})")
        table = floors()
        for package, total in sorted(totals.items()):
            self.results.append(
                f"{package}: {total:.2f} % (floor {table.get(self.edition, {}).get(package, 'none')} %, {self.edition} run)"
            )
        self.failures.extend(below_floor(self.edition, totals, table, precision))
        if self.failures and session.exitstatus == pytest.ExitCode.OK:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED

    def pytest_terminal_summary(self, terminalreporter) -> None:
        if not self.results and not self.failures:
            return
        terminalreporter.section("coverage floors")
        for line in self.results:
            terminalreporter.write_line(line)
        for line in self.failures:
            terminalreporter.write_line(f"FAIL {line}", red=True, bold=True)
        if not self.failures:
            terminalreporter.write_line("Every measured package is at or above its floor.", green=True)


def register(config) -> None:
    """Idempotent: both conftests call it."""
    if not config.pluginmanager.hasplugin(PLUGIN_NAME):
        config.pluginmanager.register(CoverageFloors(config), PLUGIN_NAME)
