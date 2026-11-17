# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The control plane's two packages are one release.

The distribution said 0.10.80 while the platform was at 0.11.32: the
release script (`update-version.sh`) sets the
Community version, the Enterprise version and the Enterprise package's exact
pin on the Community one together, and had not run since the packages were
renamed. Held here: the Community package's version is the platform's
`VERSION` where this repository is checked out inside the platform
workspace; and in `enterprise/tests/test_distribution_version.py`: the
Enterprise package has the same version and pins exactly it.
"""

from __future__ import annotations

import re
import tomllib

import pytest

from tests.source_trees import REPO

COMMUNITY = REPO / "pyproject.toml"
PLATFORM_VERSION = REPO.parent / "VERSION"

def _project(path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))["project"]


def test_the_version_is_a_release_version():
    assert re.fullmatch(r"\d+\.\d+\.\d+", _project(COMMUNITY)["version"])


@pytest.mark.skipif(
    not PLATFORM_VERSION.is_file(),
    reason=f"no platform VERSION file at {PLATFORM_VERSION}: not inside the platform workspace",
)
def test_it_is_the_platforms_version():
    assert _project(COMMUNITY)["version"] == PLATFORM_VERSION.read_text(encoding="utf-8").strip()
