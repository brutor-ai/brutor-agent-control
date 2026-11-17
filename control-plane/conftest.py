# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The one place every test session passes: `tests/` and `enterprise/tests/`,
in either edition.

It makes the session hermetic towards the host's container runtime and
cluster and towards the network before any test module (and so any application) is imported:
`tests/hermetic.py` says what the walls are and why. Nothing else belongs
here; the suites' own fixtures are in `tests/conftest.py` and
`enterprise/tests/conftest.py`.
"""

from __future__ import annotations

import pytest

from tests import hermetic

hermetic.install()


@pytest.fixture(scope="session", autouse=True)
def hermetic_host():
    """Every test runs behind the walls, and the session fails if one of
    them was hit: a real Docker or Kubernetes client, a `docker`, `kubectl`
    or `helm` process, a connection or name lookup that leaves this machine,
    or a connection to a port of it that the suite did not start."""
    assert hermetic.installed()
    yield hermetic
    if hermetic.VIOLATIONS:
        pytest.fail(
            "the test session reached for the host's container runtime or cluster, or for the network:\n  "
            + "\n  ".join(hermetic.VIOLATIONS),
            pytrace=False,
        )


def pytest_sessionfinish(session, exitstatus):
    # The fixture's failure is reported on the last test; this makes the
    # exit status say so whatever that test's own outcome was.
    if hermetic.VIOLATIONS and exitstatus == 0:
        session.exitstatus = 1


def pytest_terminal_summary(terminalreporter):
    if hermetic.DOCKER_CALLS or hermetic.KUBERNETES_CALLS:
        terminalreporter.section("hermetic: what the fake Docker daemon was asked")
        for line in hermetic.journal_summary():
            terminalreporter.write_line(line)
    if hermetic.VIOLATIONS:
        terminalreporter.section("hermetic walls hit")
        for line in hermetic.VIOLATIONS:
            terminalreporter.write_line(line)
