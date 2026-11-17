# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
No function reads a name before an import inside that same function binds it.

An `import` statement anywhere in a function makes the imported name a local
variable of the whole function. A read of that name earlier in the function
then raises `UnboundLocalError`, even when a module-level import of the same
name exists, because the local name hides it.

This is how the secret-consistency preflight never ran: the lifespan called `SessionLocal()` near its top, and two
`from app.database import SessionLocal` statements further down the same
function made that call fail. The `except Exception` around the preflight
logged the error at debug level and startup carried on, so nothing showed it.

The check reads the source with `ast`; nothing is imported or run.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
# `enterprise` is absent from a Community checkout.
SCANNED = ("app", "enterprise", "alembic", "main.py", "gunicorn.conf.py")

_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _own_nodes(function: ast.AST) -> Iterator[ast.AST]:
    """The nodes of a function's own scope, without nested functions and classes."""
    stack = list(ast.iter_child_nodes(function))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, _SCOPES):
            stack.extend(ast.iter_child_nodes(node))


def find_reads_before_local_import(tree: ast.AST) -> List[Tuple[str, str, int, int]]:
    """(function, name, line of the read, line of the import) for every read of
    a name that comes before the import, in the same function, that binds it."""
    hits = []
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        nodes = list(_own_nodes(function))
        # `global x` / `nonlocal x` make the import bind the outer name instead.
        outer = {name for node in nodes if isinstance(node, (ast.Global, ast.Nonlocal)) for name in node.names}
        imported = {}
        for node in nodes:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    name = (alias.asname or alias.name).split(".")[0]
                    if name != "*" and name not in outer:
                        imported[name] = min(imported.get(name, node.lineno), node.lineno)
        # A parameter or an earlier assignment of the same name makes the read legal.
        parameters = {argument.arg for argument in ast.walk(function.args) if isinstance(argument, ast.arg)}
        assigned = {}
        for node in nodes:
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                assigned[node.id] = min(assigned.get(node.id, node.lineno), node.lineno)
        for node in nodes:
            if not (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in imported):
                continue
            if node.id in parameters:
                continue
            first_binding = min(imported[node.id], assigned.get(node.id, imported[node.id]))
            if node.lineno < first_binding:
                hits.append((function.name, node.id, node.lineno, imported[node.id]))
    return sorted(hits, key=lambda hit: hit[2])


def _sources() -> List[Path]:
    files: List[Path] = []
    for entry in SCANNED:
        path = ROOT / entry
        if path.is_dir():
            files.extend(sorted(path.rglob("*.py")))
        elif path.exists():
            files.append(path)
    return files


def test_the_scan_reads_the_code():
    files = _sources()
    assert len(files) > 500
    assert ROOT / "app" / "app.py" in files


def test_no_function_reads_a_name_before_its_local_import():
    offenders = []
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for function, name, read_line, import_line in find_reads_before_local_import(tree):
            offenders.append(
                f"{path.relative_to(ROOT)}:{read_line} `{name}` is read in {function}() "
                f"before the import on line {import_line} binds it"
            )
    assert offenders == [], "\n".join(offenders)


# ---------------------------------------------------------------------------
# The check itself: it reports the bug, and stays quiet on code that is fine.
# ---------------------------------------------------------------------------


def _find(source: str):
    return [(function, name) for function, name, _, _ in find_reads_before_local_import(ast.parse(source))]


def test_it_reports_the_shape_of_the_bug():
    # The shape of the lifespan: a module-level import, a use, a late local import.
    source = (
        "from db import SessionLocal\n"
        "async def lifespan(app):\n"
        "    try:\n"
        "        session = SessionLocal()\n"
        "    except Exception:\n"
        "        pass\n"
        "    try:\n"
        "        from db import SessionLocal\n"
        "        start(SessionLocal)\n"
        "    except Exception:\n"
        "        pass\n"
    )
    assert _find(source) == [("lifespan", "SessionLocal")]


def test_it_reports_plain_imports_and_aliases():
    assert _find("def f():\n    os.getcwd()\n    import os\n") == [("f", "os")]
    assert _find("def f():\n    np.zeros(1)\n    import numpy as np\n") == [("f", "np")]
    assert _find("def f():\n    xml.dom\n    import xml.dom\n") == [("f", "xml")]


def test_an_import_before_the_use_is_fine():
    assert _find("def f():\n    import os\n    return os.getcwd()\n") == []
    assert _find("def f(flag):\n    if flag:\n        import os\n        return os.getcwd()\n") == []


def test_a_nested_function_is_its_own_scope():
    # The inner function reads the module-level name; the outer one never reads it.
    source = (
        "import os\n"
        "def outer():\n"
        "    def inner():\n"
        "        import os\n"
        "        return os.getcwd()\n"
        "    return inner\n"
    )
    assert _find(source) == []
    # And a read in the outer function is not excused by an import in the inner one.
    assert _find("import os\ndef outer():\n    os.getcwd()\n    def inner():\n        import os\n") == []


def test_a_parameter_an_earlier_assignment_or_a_global_statement_is_fine():
    assert _find("def f(os):\n    os.getcwd()\n    import os\n") == []
    assert _find("def f():\n    os = None\n    print(os)\n    import os\n") == []
    assert _find("def f():\n    global os\n    print(os)\n    import os\n") == []
