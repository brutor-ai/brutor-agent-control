# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
What the packaging tests of both suites read: a
`pyproject.toml`, the third-party modules a source tree imports, and which
distribution provides a module.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from importlib import metadata
from pathlib import Path
from typing import Dict, Iterable, List, Set

REPO = Path(__file__).resolve().parents[1]
COMMUNITY_PROJECT = REPO / "pyproject.toml"
ENTERPRISE_PROJECT = REPO / "enterprise" / "pyproject.toml"

# The first-party top-level names: not dependencies.
FIRST_PARTY = {"app", "brutor_enterprise", "tests"}

# What the Community Edition runs from: the package, the migrations, the
# server configuration and the entry point.
COMMUNITY_SOURCES = ("app", "alembic", "gunicorn.conf.py", "main.py")
ENTERPRISE_SOURCES = ("enterprise/brutor_enterprise", "enterprise/main.py")


def project(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))["project"]


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_name(requirement: str) -> str:
    """`qrcode[pil]>=7.4` -> `qrcode`."""
    return normalise(re.split(r"[\s\[<>=!~;]", requirement.strip(), maxsplit=1)[0])


def declared(path: Path) -> List[str]:
    return [requirement_name(requirement) for requirement in project(path)["dependencies"]]


def python_files(roots: Iterable[str]) -> List[Path]:
    files: List[Path] = []
    for root in roots:
        path = REPO / root
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files += sorted(p for p in path.rglob("*.py") if "__pycache__" not in p.parts)
    return files


def third_party_imports(roots: Iterable[str]) -> Dict[str, List[str]]:
    """`{top-level module: [files]}` for every import, at any depth of a
    file (a lazy import inside a function counts), that is neither the
    standard library's nor first-party."""
    found: Dict[str, Set[str]] = {}
    for path in python_files(roots):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules: List[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module]
            for module in modules:
                top = module.split(".")[0]
                if top in sys.stdlib_module_names or top in FIRST_PARTY:
                    continue
                found.setdefault(top, set()).add(str(path.relative_to(REPO)))
    return {module: sorted(files) for module, files in sorted(found.items())}


# Modules whose distribution has another name, for an environment where the
# distribution is not installed (the metadata answers where it is).
_DISTRIBUTION_OF = {
    "yaml": "pyyaml", "jwt": "pyjwt", "nacl": "pynacl", "Crypto": "pycryptodome", "PIL": "pillow",
    "dotenv": "python-dotenv", "ulid": "python-ulid",
    "dateutil": "python-dateutil", "qdrant_client": "qdrant-client",
}


def distributions_of(module: str) -> Set[str]:
    """The distributions that provide the top-level `module`: what the
    installed metadata says, and, for a module that is not installed here (an
    optional import such as the Vault client), the distribution of that name.
    So the comparison with a `pyproject.toml` does not depend on what the
    environment happens to hold."""
    found = {normalise(name) for name in metadata.packages_distributions().get(module, [])}
    return found or {normalise(_DISTRIBUTION_OF.get(module, module))}


def console_scripts(path: Path) -> Dict[str, str]:
    return dict(project(path).get("scripts", {}))
