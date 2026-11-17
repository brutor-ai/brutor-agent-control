# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""No test module may read the wall clock at import time.

A module-level ``NOW = datetime.now(...)`` is taken when pytest *collects*
the file, not when the test runs. The full suite takes ~45 minutes, so by
the time a late test compares ``NOW + 1h`` against the real clock inside the
code under test, "one hour from now" is already in the past. That made
``test_rollup_exclusion::test_a_stored_exclusion_counts_until_it_expires``
pass alone and fail in the full run, and the same shape sat in
five other files waiting for a slow enough run.

The rule, checked statically over every test module: a module-level
statement (assignment, annotated assignment, or the value of one) must not
call a clock. Read the clock inside the test or fixture that uses it.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.source_trees import test_label, test_sources

TESTS_DIR = Path(__file__).parent

# Calls that read the wall clock. Matched on the dotted tail so both
# `datetime.now(...)` and `datetime.datetime.now(...)` are caught.
CLOCK_CALLS = {
    ("datetime", "now"), ("datetime", "utcnow"), ("datetime", "today"),
    ("date", "today"), ("time", "time"), ("time", "monotonic"),
}


def _dotted_tail(func: ast.expr) -> tuple[str, str] | None:
    if isinstance(func, ast.Attribute):
        owner = func.value
        if isinstance(owner, ast.Attribute):
            return owner.attr, func.attr
        if isinstance(owner, ast.Name):
            return owner.id, func.attr
    return None


class _ClockCalls(ast.NodeVisitor):
    """Collect clock calls in an expression, not descending into deferred
    code (lambdas and comprehensions inside a lambda run later)."""

    def __init__(self) -> None:
        self.lines: list[int] = []

    def visit_Lambda(self, node: ast.Lambda) -> None:  # deferred — runs at call time
        return

    def visit_Call(self, node: ast.Call) -> None:
        if _dotted_tail(node.func) in CLOCK_CALLS:
            self.lines.append(node.lineno)
        self.generic_visit(node)


def import_time_clock_reads(source: str) -> list[int]:
    """Line numbers of clock calls evaluated when the module is imported."""
    tree = ast.parse(source)
    found: list[int] = []
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                             ast.Import, ast.ImportFrom)):
            continue
        visitor = _ClockCalls()
        visitor.visit(stmt)
        found.extend(visitor.lines)
    return found


def findings() -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for path in test_sources():
        lines = import_time_clock_reads(path.read_text())
        if lines:
            out[test_label(path)] = lines
    return out


def test_no_test_module_reads_the_clock_at_import():
    offenders = findings()
    assert not offenders, (
        "module-level clock reads (taken at collection time, stale by the time "
        f"a late test runs): {offenders} — read the clock inside the test or fixture"
    )


def test_the_scanner_catches_the_shapes_it_guards():
    assert import_time_clock_reads("from datetime import datetime\nNOW = datetime.now()\n") == [2]
    assert import_time_clock_reads("import datetime\nX = {'a': datetime.datetime.utcnow()}\n") == [2]
    assert import_time_clock_reads("import time\nT: float = time.time()\n") == [2]
    # Deferred reads are fine.
    assert import_time_clock_reads("from datetime import datetime\ndef now():\n    return datetime.now()\n") == []
    assert import_time_clock_reads("from datetime import datetime\nF = lambda: datetime.now()\n") == []
