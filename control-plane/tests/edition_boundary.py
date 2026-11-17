# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Reading the edition boundary out of the source tree.

The boundary file (`editions.yaml`, in the directory
`BRUTOR_EDITION_RECORDS_DIR` names; `control_plane.enterprise_modules`) says which modules of this tree are the
Enterprise Edition's. Everything else is Community. The functions here answer,
by parsing the source and importing nothing of it:

* `import_edges()`: where a Community module imports an Enterprise one
  (any scope: module level, inside a function, under `TYPE_CHECKING`), and
  where it names one in a string (`importlib.import_module("app....")`).
* `table_uses()`: where a Community module uses an Enterprise-only table
  (`control_plane.enterprise_only_tables`): its model class, its association
  table, SQL text that names it, or a relationship that reaches it.

The guards that hold these to a list that may only shrink are
`tests/test_edition_import_guard.py` and `tests/test_edition_table_guard.py`.

`root` is a parameter everywhere so that a guard can be pointed at a copy of
the tree.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Set, Tuple

REPO = Path(__file__).resolve().parents[1]
# The edition records (the boundary file `editions.yaml` and the
# edition-install contract) are kept outside this repository, and only this
# variable says where: it names the directory that holds them. It has no
# default. Not set: a test that needs them is skipped, and the reason names
# the variable. Set to something that is not a directory: the test fails.
EDITION_RECORDS_ENV = "BRUTOR_EDITION_RECORDS_DIR"


def edition_records_dir() -> Optional[Path]:
    """The directory `BRUTOR_EDITION_RECORDS_DIR` names, or None when it is
    not set. A value that is not a directory fails the calling test: a guard
    pointed at nothing must not pass by skipping."""
    value = os.environ.get(EDITION_RECORDS_ENV, "").strip()
    if not value:
        return None
    path = Path(value)
    assert path.is_dir(), f"{EDITION_RECORDS_ENV}={value} is not a directory"
    return path


def edition_record(name: str) -> Path:
    """The file `name` in the edition records directory. Skips the calling
    test when `BRUTOR_EDITION_RECORDS_DIR` is not set; fails when it is set
    and the file is not there."""
    records = edition_records_dir()
    if records is None:
        import pytest

        pytest.skip(f"{EDITION_RECORDS_ENV} is not set: the edition records are not available")
    path = records / name
    assert path.is_file(), f"{name} is not in {EDITION_RECORDS_ENV}={records}"
    return path


EDITIONS_YAML_NAME = "editions.yaml"

# What is scanned as "this tree's code": the package, and the entry points
# beside it. Tests are not product code; migrations are the one schema both
# editions share.
#
# The Enterprise modules live in `enterprise/brutor_enterprise/`,
# one package per feature; that tree is scanned too (it is absent from a
# Community checkout, and then there is nothing of it to scan). Everything
# under `enterprise/` is Enterprise, listed in the boundary file or not (the
# extension's own modules are not: they belong to no one feature).
SCANNED_DIRS = ("app", "enterprise/brutor_enterprise")
# `main.py` at the repository root is the Community Edition's entry point
# and is scanned as Community code; the Enterprise Edition's is
# `enterprise/main.py`, Enterprise like everything under `enterprise/`.
SCANNED_FILES = ("main.py", "enterprise/main.py", "gunicorn.conf.py")
ENTERPRISE_ROOT = "enterprise/"
ENTERPRISE_PACKAGE = "brutor_enterprise"
# The Enterprise Edition's entry point (not a module of the package: it is a
# script beside it).
ENTERPRISE_ENTRY_POINT = "enterprise/main.py"

# The libraries only Enterprise Edition code uses (from a survey of the imports).
ENTERPRISE_ONLY_LIBRARIES = ("docker", "kubernetes", "qdrant_client", "aiohttp")

MODELS_DIR = "app/database/models/"

# Where the list of Enterprise-only tables lives in the code, and its names.
TABLE_MAP_MODULE = "app/composition/edition.py"
TABLE_MAP_NAMES = ("ENTERPRISE_ONLY_TABLES", "enterprise_only_tables")


def edition_boundary_file() -> dict:
    """The whole boundary file, from the edition records directory (see
    `edition_record`: skipped without `BRUTOR_EDITION_RECORDS_DIR`)."""
    import yaml

    return yaml.safe_load(edition_record(EDITIONS_YAML_NAME).read_text(encoding="utf-8"))


def control_plane_boundary() -> dict:
    """The `control_plane` section of the boundary file."""
    return edition_boundary_file()["control_plane"]


def enterprise_paths(boundary: dict) -> Dict[str, str]:
    """`path -> feature` for every Enterprise module or package listed."""
    return {path: feature for feature, paths in boundary["enterprise_modules"].items() for path in paths}


def is_enterprise(rel: str, paths: Iterable[str]) -> bool:
    return rel.startswith(ENTERPRISE_ROOT) or any(rel.startswith(p) if p.endswith("/") else rel == p for p in paths)


def python_files(root: Path = REPO) -> List[str]:
    """Every scanned Python file, relative to `root`, sorted."""
    found: List[str] = []
    for sub in SCANNED_DIRS:
        for directory, dirs, files in os.walk(root / sub):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            found += [
                os.path.relpath(os.path.join(directory, name), root) for name in files if name.endswith(".py")
            ]
    found += [name for name in SCANNED_FILES if (root / name).exists()]
    return sorted(found)


def module_name(rel: str) -> str:
    if rel.startswith(ENTERPRISE_ROOT):
        rel = rel[len(ENTERPRISE_ROOT) :]  # `enterprise/` is a path entry, not a package
    name = rel[:-3].replace(os.sep, ".").replace("/", ".")
    return name[: -len(".__init__")] if name.endswith(".__init__") else name


def enterprise_module_names(boundary: dict, root: Path = REPO) -> List[str]:
    """Dotted names of the Enterprise modules and packages, as listed (a
    package stands for everything under it)."""
    return sorted(module_name(p.rstrip("/") + ".py") if p.endswith("/") else module_name(p) for p in enterprise_paths(boundary))


def _parse(root: Path, rel: str) -> ast.Module:
    return ast.parse((root / rel).read_text(encoding="utf-8", errors="replace"), filename=rel)


# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------


class _Imports(ast.NodeVisitor):
    """Every import of a file as `(dotted target, bound name, line)`; a
    relative import is resolved against the file's package."""

    def __init__(self, rel: str) -> None:
        name = module_name(rel)
        self.package = name if rel.endswith("__init__.py") else name.rpartition(".")[0]
        self.found: List[Tuple[str, str, int]] = []

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.found.append((alias.name, "", node.lineno))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = node.module or ""
        if node.level:
            parts = self.package.split(".") if self.package else []
            if node.level > 1:
                parts = parts[: len(parts) - (node.level - 1)]
            base = ".".join(parts + ([node.module] if node.module else []))
        for alias in node.names:
            self.found.append((base, alias.name, node.lineno))


def _resolve(dotted: str, modules: Dict[str, str]) -> Optional[str]:
    """The file of the longest prefix of `dotted` that is a module here."""
    parts = dotted.split(".")
    for end in range(len(parts), 0, -1):
        rel = modules.get(".".join(parts[:end]))
        if rel is not None:
            return rel
    return None


def import_edges(boundary: dict, root: Path = REPO) -> List[str]:
    """Every place a Community file reaches an Enterprise module, as
    `source -> target :: name`, sorted and without duplicates.

    `name` is what is imported from the target (`*module*` for the module
    itself), or `*string*` when the target is named in a string constant:
    `importlib.import_module`, a `mock.patch` target, a dotted path in a
    registry. Line numbers are left out so that the list does not change when
    a file is edited elsewhere.
    """
    paths = enterprise_paths(boundary)
    files = python_files(root)
    modules = {module_name(rel): rel for rel in files}
    edges: Set[str] = set()
    for rel in files:
        if is_enterprise(rel, paths):
            continue
        tree = _parse(root, rel)
        visitor = _Imports(rel)
        visitor.visit(tree)
        for base, name, _line in visitor.found:
            target = (_resolve(f"{base}.{name}", modules) if name else None) or _resolve(base, modules)
            if target is None or not is_enterprise(target, paths):
                continue
            # `from package import module` binds the module itself.
            is_module = bool(name) and modules.get(f"{base}.{name}") == target
            edges.add(f"{rel} -> {target} :: {'*module*' if (not name or is_module) else name}")
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and re.fullmatch(r"(?:app|brutor_enterprise)(\.\w+)+", node.value)
            ):
                target = _resolve(node.value, modules)
                if target is not None and is_enterprise(target, paths):
                    edges.add(f"{rel} -> {target} :: *string*")
    return sorted(edges)


# ---------------------------------------------------------------------------
# Enterprise-only tables
# ---------------------------------------------------------------------------


def enterprise_only_tables(boundary: dict) -> Dict[str, str]:
    """`table -> feature`."""
    return {table: feature for feature, tables in boundary["enterprise_only_tables"].items() for table in tables}


def enterprise_model_names(boundary: dict, root: Path = REPO) -> Dict[str, str]:
    """`name -> table` for every model class and every module-level
    association `Table` of an Enterprise-only table, read from the model
    files' source."""
    tables = enterprise_only_tables(boundary)
    names: Dict[str, str] = {}
    for rel in python_files(root):
        if not rel.startswith(MODELS_DIR):
            continue
        tree = _parse(root, rel)
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                for stmt in node.body:
                    if (
                        isinstance(stmt, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == "__tablename__" for t in stmt.targets)
                        and isinstance(stmt.value, ast.Constant)
                        and stmt.value.value in tables
                    ):
                        names[node.name] = stmt.value.value
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                callee = node.value.func
                callee_name = callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", "")
                first = node.value.args[0] if node.value.args else None
                if callee_name == "Table" and isinstance(first, ast.Constant) and first.value in tables:
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            names[target.id] = first.value
    return names


def enterprise_relationships(boundary: dict, root: Path = REPO) -> Dict[str, str]:
    """`attribute -> table` for every `relationship(...)` declared on a model
    of a Community table that reaches an Enterprise-only table (its target
    class, or its `secondary` association table)."""
    tables = enterprise_only_tables(boundary)
    names = enterprise_model_names(boundary, root)
    found: Dict[str, str] = {}
    for rel in python_files(root):
        if not rel.startswith(MODELS_DIR):
            continue
        for cls in (n for n in _parse(root, rel).body if isinstance(n, ast.ClassDef)):
            if cls.name in names:
                continue  # an Enterprise model's own relationships
            for stmt in cls.body:
                value = stmt.value if isinstance(stmt, (ast.Assign, ast.AnnAssign)) else None
                if not isinstance(value, ast.Call):
                    continue
                callee = value.func
                if (callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", "")) != "relationship":
                    continue
                reached = None
                first = value.args[0] if value.args else None
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    reached = names.get(first.value.rpartition(".")[2])
                for keyword in value.keywords:
                    if keyword.arg == "secondary":
                        ref = keyword.value
                        text = ref.value if isinstance(ref, ast.Constant) else getattr(ref, "id", "")
                        if text in tables:
                            reached = text
                        elif text in names:
                            reached = names[text]
                if reached is None:
                    continue
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        found[target.id] = reached
    return found


def _annotation_nodes(tree: ast.AST) -> Set[int]:
    inside: Set[int] = set()
    for node in ast.walk(tree):
        annotations: List[ast.AST] = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            annotations += [a.annotation for a in args.posonlyargs + args.args + args.kwonlyargs if a.annotation]
            annotations += [a.annotation for a in (args.vararg, args.kwarg) if a is not None and a.annotation]
            if node.returns:
                annotations.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)
        for annotation in annotations:
            inside.update(id(x) for x in ast.walk(annotation))
    return inside


def _docstrings(tree: ast.AST) -> Set[int]:
    found: Set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                found.add(id(first.value))
    return found


def _sql_pattern(tables: Iterable[str]) -> "re.Pattern[str]":
    names = "|".join(sorted(map(re.escape, tables), key=len, reverse=True))
    return re.compile(
        r"(?i)\b(?:from|join|into|update|table|truncate)\s+(?:only\s+|if\s+exists\s+)?[\"`]?(?:public\.)?(" + names + r")\b"
    )


def table_uses(boundary: dict, root: Path = REPO) -> Dict[str, List[str]]:
    """`file -> sorted uses` for every Community file outside the model
    package that touches an Enterprise-only table. A use is one of:

    * `model:<Name>`: the model class (or association table) is used in code
      that runs: queried, constructed, compared.
    * `import:<Name>`: it is imported and only named in annotations, or not
      used at all.
    * `sql:<table>`: SQL text names the table.
    * `relationship:<attribute>`: an attribute of that name is read or set, and a
      Community model has a relationship of that name into an Enterprise-only
      table. (By name only: the scan does not know the object's type.)
    * `map:<name>`: the file reads the list of Enterprise-only tables itself
      (`app/composition/edition.py`) and so can reach every one of them.
    """
    paths = enterprise_paths(boundary)
    tables = enterprise_only_tables(boundary)
    names = enterprise_model_names(boundary, root)
    relationships = enterprise_relationships(boundary, root)
    sql = _sql_pattern(tables)
    found: Dict[str, List[str]] = {}
    for rel in python_files(root):
        if is_enterprise(rel, paths) or rel.startswith(MODELS_DIR):
            continue
        tree = _parse(root, rel)
        annotations = _annotation_nodes(tree)
        docstrings = _docstrings(tree)
        imported: Set[str] = set()
        runtime: Set[str] = set()
        uses: Set[str] = set()
        aliases: Dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in TABLE_MAP_NAMES and rel != TABLE_MAP_MODULE:
                        uses.add(f"map:{alias.name}")
                    if alias.name in names:
                        imported.add(alias.name)
                        aliases[alias.asname or alias.name] = alias.name
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in aliases:
                if id(node) not in annotations:
                    runtime.add(aliases[node.id])
            elif isinstance(node, ast.Attribute):
                if node.attr in names and id(node) not in annotations:
                    runtime.add(node.attr)
                elif node.attr in relationships:
                    uses.add(f"relationship:{node.attr}")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                uses.update(f"sql:{table}" for table in sql.findall(node.value))
        uses.update(f"model:{name}" for name in runtime)
        uses.update(f"import:{name}" for name in imported - runtime)
        if uses:
            found[rel] = sorted(uses)
    return found
