# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Every admin audit-log call passes the database session.

`UnifiedAdminAuditLogger.log(action, db, ...)` requires the session. The
admin tool-approval approve/reject endpoints called it without one, and
because the call came AFTER `db.commit()`, every approve or reject was
saved and then answered HTTP 500 — the console reported a failure for a
decision that had in fact been recorded.

The rule is checked statically over the whole app: a call to `.log(...)`
on an audit logger must pass `db` (keyword or second positional).
"""

from __future__ import annotations

import ast
from pathlib import Path
from tests.source_trees import python_sources, source_label

APP = Path(__file__).resolve().parent.parent / "app"


def _audit_log_calls_without_db(source: str) -> list[int]:
    lines = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "log" or not isinstance(node.func.value, ast.Name):
            continue
        if "audit" not in node.func.value.id:
            continue
        if any(k.arg == "db" for k in node.keywords) or len(node.args) >= 2:
            continue
        lines.append(node.lineno)
    return lines


def test_every_audit_log_call_passes_the_session():
    offenders = {
        source_label(p): lines
        for p in python_sources(APP)
        if (lines := _audit_log_calls_without_db(p.read_text()))
    }
    assert not offenders, f"audit_logger.log(...) without db: {offenders}"


def test_the_scanner_catches_the_shape_it_guards():
    assert _audit_log_calls_without_db("audit_logger.log(action='x', resource_id='y')\n") == [1]
    assert _audit_log_calls_without_db("audit_logger.log(action='x', db=db)\n") == []
    assert _audit_log_calls_without_db("audit_logger.log('x', db)\n") == []
    assert _audit_log_calls_without_db("logger.log(level, 'msg')\n") == []
