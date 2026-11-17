# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Where running code touches the process environment.

`offences(path)` reads one Python file and returns every place it reaches
the environment by itself: `os.environ` in any form (read, written,
iterated, passed on), `os.getenv`, `os.putenv`, `os.unsetenv`,
`os.path.expandvars`, the same names imported from `os`, and the `dotenv`
library. Strings and comments are not code and are not reported.

The one file that may is the registry module (`app/env_registry.py`): the
guards in `tests/test_env_registry.py` and
`enterprise/tests/test_env_registry.py` hold every other file of both
packages, the entry points and the scripts to an empty list.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List

REPO = Path(__file__).resolve().parents[1]
REGISTRY_MODULE = "app/env_registry.py"

_OS_ATTRIBUTES = {"environ", "environb", "getenv", "getenvb", "putenv", "unsetenv"}
_OS_PATH_ATTRIBUTES = {"expandvars"}


def offences(path: Path) -> List[str]:
    """`file:line: what` for every direct use of the environment in `path`."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    try:
        shown = path.resolve().relative_to(REPO).as_posix()
    except ValueError:
        shown = path.name
    found: List[str] = []
    # Names bound to the `os` module, to `os.path`, and to the functions themselves.
    os_names, os_path_names, direct = {"os"}, set(), {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "os":
                    os_names.add(alias.asname or "os")
                if alias.name == "os.path" and alias.asname:
                    os_path_names.add(alias.asname)
                if alias.name.split(".")[0] == "dotenv":
                    found.append(f"{shown}:{node.lineno}: imports {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.split(".")[0] == "dotenv":
                found.append(f"{shown}:{node.lineno}: imports from {module}")
            for alias in node.names:
                if module == "os" and alias.name in _OS_ATTRIBUTES:
                    direct[alias.asname or alias.name] = f"os.{alias.name}"
                if module == "os" and alias.name == "path":
                    os_path_names.add(alias.asname or "path")
                if module == "os.path" and alias.name in _OS_PATH_ATTRIBUTES:
                    direct[alias.asname or alias.name] = f"os.path.{alias.name}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            value = node.value
            if isinstance(value, ast.Name) and value.id in os_names and node.attr in _OS_ATTRIBUTES:
                found.append(f"{shown}:{node.lineno}: os.{node.attr}")
            if node.attr in _OS_PATH_ATTRIBUTES and (
                (isinstance(value, ast.Name) and value.id in os_path_names)
                or (
                    isinstance(value, ast.Attribute)
                    and value.attr == "path"
                    and isinstance(value.value, ast.Name)
                    and value.value.id in os_names
                )
            ):
                found.append(f"{shown}:{node.lineno}: os.path.{node.attr}")
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in direct:
            found.append(f"{shown}:{node.lineno}: {direct[node.id]}")
    return sorted(set(found), key=lambda line: (line.split(":")[0], int(line.split(":")[1])))


def python_files(*roots: str) -> List[Path]:
    """Every Python file under the given paths of the repository (a file
    is itself), without the registry module."""
    files: List[Path] = []
    for root in roots:
        path = REPO / root
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files += sorted(p for p in path.rglob("*.py") if "__pycache__" not in p.parts)
    return [p for p in files if p.relative_to(REPO).as_posix() != REGISTRY_MODULE]
