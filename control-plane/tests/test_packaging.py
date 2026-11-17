# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The Community Edition's package.

Two distributions are built from this repository: the root `pyproject.toml`
is the Community package (`brutor-agent-control-plane`, the `app` package),
`enterprise/pyproject.toml` the Enterprise one, which depends on it. Held
here, for the Community package, its image and its entry point:

* it declares a distribution for every third-party module Community code
  imports, and none that only Enterprise code imports;
* its console script names a function that exists (it once named
  `app.main:main`, a module that never existed);
* the repository root holds one entry point, the Community one, and it names
  no Enterprise module;
* the root `Dockerfile` builds the Community image: the build context has no
  `enterprise/`, the sources stay as sources, no cloud CLI is installed, and
  the command is the Community factory.

The Enterprise package's half is `enterprise/tests/test_packaging.py`.
"""

from __future__ import annotations

import importlib
import re

import pytest

from tests import packaging, workflows
from tests.edition_boundary import ENTERPRISE_ONLY_LIBRARIES
from tests.packaging import COMMUNITY_PROJECT, COMMUNITY_SOURCES, REPO

# Distributions only Enterprise Edition code imports. None may be a
# dependency of the Community package.
ENTERPRISE_ONLY_DISTRIBUTIONS = (
    "docker",
    "kubernetes",
    "qdrant-client",
    "aiohttp",
    "croniter",
    "python-dateutil",
)

# Declared and imported by no Community module, with the reason.
NOT_IMPORTED = {
    "gunicorn": "the server process (`gunicorn app.app:create_app`); never imported",
    "python-multipart": "FastAPI needs it installed to accept form and file uploads; it is not imported by name",
}


def test_the_package_is_the_community_one():
    meta = packaging.project(COMMUNITY_PROJECT)
    assert meta["name"] == "brutor-agent-control-plane"
    assert "enterprise" not in meta["name"]


def test_every_module_community_code_imports_is_declared():
    declared = set(packaging.declared(COMMUNITY_PROJECT))
    imports = packaging.third_party_imports(COMMUNITY_SOURCES)
    assert len(imports) > 15 and "fastapi" in imports and "sqlalchemy" in imports
    undeclared = {}
    for module, files in imports.items():
        providers = packaging.distributions_of(module)
        if not providers & declared:
            undeclared[module] = (sorted(providers), files[0])
    assert undeclared == {}


def test_every_declared_dependency_is_imported_or_has_its_reason():
    imports = packaging.third_party_imports(COMMUNITY_SOURCES)
    provided = set()
    for module in imports:
        provided |= packaging.distributions_of(module)
    unused = sorted(name for name in packaging.declared(COMMUNITY_PROJECT) if name not in provided)
    assert unused == sorted(name for name in NOT_IMPORTED if name not in provided)


def test_no_enterprise_only_library_is_a_community_dependency():
    declared = set(packaging.declared(COMMUNITY_PROJECT))
    assert sorted(declared & set(ENTERPRISE_ONLY_DISTRIBUTIONS)) == []
    # And no Community module imports one (the boundary guard's list).
    imports = packaging.third_party_imports(COMMUNITY_SOURCES)
    assert sorted(set(imports) & set(ENTERPRISE_ONLY_LIBRARIES)) == []


def test_the_undeclared_import_of_the_survey_is_declared_where_it_is_imported():
    """`croniter` is imported by the Enterprise usage-import scheduler only.
    (`hvac` was the Vault key provider's: neither is left in the Community
    package.)"""
    imports = packaging.third_party_imports(COMMUNITY_SOURCES)
    assert "hvac" not in imports and "croniter" not in imports
    declared = packaging.declared(COMMUNITY_PROJECT)
    assert "hvac" not in declared and "croniter" not in declared


def test_the_console_script_names_a_function_that_exists():
    scripts = packaging.console_scripts(COMMUNITY_PROJECT)
    assert list(scripts) == ["brutor-agent-control-plane"]
    for name, target in scripts.items():
        module, _, function = target.partition(":")
        assert callable(getattr(importlib.import_module(module), function)), f"{name} = {target}"
        assert module.split(".")[0] == "app"


def test_the_package_ships_the_community_package_only():
    find = packaging.tomllib.loads(COMMUNITY_PROJECT.read_text("utf-8"))["tool"]["setuptools"]["packages"]["find"]
    assert find["include"] == ["app*"]


def test_the_repository_root_holds_one_entry_point_and_it_is_the_community_one():
    assert sorted(path.name for path in REPO.glob("main*.py")) == ["main.py"]
    source = (REPO / "main.py").read_text("utf-8") + (REPO / "app" / "cli.py").read_text("utf-8")
    assert "brutor_enterprise" not in source.replace("brutor_enterprise.cli", "").replace(
        "brutor-agent-control-plane-enterprise", ""
    )
    assert "from app.app import create_app" in source


# ---------------------------------------------------------------------------
# The image
# ---------------------------------------------------------------------------


def _dockerfile() -> str:
    return (REPO / "Dockerfile").read_text("utf-8")


def _instructions(dockerfile: str) -> list:
    """The instructions of a Dockerfile, comments and continuations folded."""
    lines, current = [], ""
    for raw in dockerfile.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        current += line.rstrip("\\") + " "
        if not line.endswith("\\"):
            lines.append(current.strip())
            current = ""
    return lines


def test_the_root_dockerfile_builds_the_community_image():
    instructions = _instructions(_dockerfile())
    assert instructions[-1] == 'CMD ["gunicorn", "app.app:create_app", "--config", "gunicorn.conf.py"]'
    text = "\n".join(instructions)
    assert "enterprise" not in text.lower()
    # The sources stay: no bytecode-only step.
    assert "compileall" not in text and not re.search(r"-name\s+\"?\*\.py\"?.*-delete", text)
    # No cloud CLI: they serve the Enterprise deployment feature.
    for word in ("CLOUD_CLIS", "awscli", "google-cloud", "azure-cli", "kubelogin"):
        assert word not in text, word


def test_the_community_build_context_has_no_enterprise_directory():
    ignored = [line.strip() for line in (REPO / ".dockerignore").read_text("utf-8").splitlines()]
    assert "enterprise/" in ignored
    # The Enterprise image is built from the same context with its own
    # ignore file, which lets the directory in.
    enterprise_ignore = REPO / "enterprise" / "Dockerfile.dockerignore"
    if (REPO / "enterprise").is_dir():
        assert "enterprise/" not in [line.strip() for line in enterprise_ignore.read_text("utf-8").splitlines()]


@pytest.mark.parametrize("script", ["start_with_gunicorn.sh"])
def test_the_gunicorn_script_takes_the_edition_explicitly(script):
    text = (REPO / script).read_text("utf-8")
    assert "app.app:create_app" in text and "brutor_enterprise.app:create_app" in text
    # No edition is read from the environment or guessed: it is the argument.
    assert re.search(r'case "\$EDITION" in', text)
    assert "community)" in text and "enterprise)" in text


def test_the_community_ci_job_needs_no_secret():
    """The Community repository's CI runs with no secret: the Community job
    reads none (`secrets.` anywhere in it), and checks out no other
    repository. The token that reads the private core is the Enterprise
    jobs' alone. (In this repository the job is `ci.yml`'s; in the public
    repository the root's control plane workflow, `tests/workflows.py`.)"""
    import yaml

    workflow = yaml.safe_load(workflows.workflow_text("ci.yml"))
    job = workflow["jobs"]["test-community"]
    assert "secrets." not in yaml.safe_dump(job)
    checkouts = [step for step in job["steps"] if str(step.get("uses", "")).startswith("actions/checkout")]
    assert checkouts and all("repository" not in step.get("with", {}) for step in checkouts)
    # Without the core the comparisons with it are skipped, and the reason
    # names BRUTOR_CORE_REPO (`tests/core_repo.py`). In this repository
    # nothing sets it: the core is another, private, repository. In the public
    # repository the core is a directory of the same checkout, and the job may
    # point at that directory and nothing else.
    core = workflows.monorepo_core_dir()
    for step in job["steps"]:
        value = (step.get("env") or {}).get("BRUTOR_CORE_REPO")
        if core is None:
            assert "BRUTOR_CORE_REPO" not in (step.get("env") or {}), step.get("name")
        elif value is not None:
            assert value == core, (step.get("name"), value)
