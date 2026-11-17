# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# ONE definition of a successful run (one fact, one computation)

Before declared outcomes, three modules carried their own copy of "the run did its
job" (`ai_system_assurance._SUCCESSFUL`, `ai_system_envelope.
_COMPLETED_STATES`, `ai_system_dependency_health._COMPLETED`, plus copies in
tokenomics and the mission-control cards), every one of them `terminal_state
in (completed, completed_degraded)` — which is exactly the rule that let
empty polls keep a system "healthy" through a model outage. Success
now also needs a declared outcome that counts toward the baseline, and that
is decided in ONE place: `app/services/ai_system_outcomes.py`.

Static guard: no module in `app/` other than the classification (and the
vocabularies that define the literals) may

  * name `SUCCESSFUL_TERMINAL_STATES`, `TERMINAL_COMPLETED` or
    `TERMINAL_COMPLETED_DEGRADED`;
  * spell the literal `"completed_degraded"`;
  * compare something against the literal `"completed"` where the other
    side reads a `terminal_state` / `run_end_state`.

The guard is proven against the old shapes it replaces.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from tests.source_trees import python_sources

APP = pathlib.Path(__file__).resolve().parent.parent / "app"
ALLOWED = {
    APP / "services" / "ai_system_outcomes.py",       # THE classification
    APP / "database" / "models" / "ai_system_run.py",  # the execution-axis vocabulary
    APP / "constants" / "proxy.py",                    # RunTerminalState wire enum
}
BANNED_NAMES = {"SUCCESSFUL_TERMINAL_STATES", "TERMINAL_COMPLETED", "TERMINAL_COMPLETED_DEGRADED"}
STATE_HINTS = ("terminal_state", "run_end_state", "recorded_terminal_state")


def _violations(path: pathlib.Path) -> list:
    src = path.read_text()
    tree = ast.parse(src, str(path))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            out.append(f"{path.name}:{node.lineno} names {node.id}")
        elif isinstance(node, ast.alias) and node.name in BANNED_NAMES:
            out.append(f"{path.name} imports {node.name}")
        elif isinstance(node, ast.Constant) and node.value == "completed_degraded":
            out.append(f"{path.name}:{node.lineno} spells 'completed_degraded'")
        elif isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            has_literal = any(
                isinstance(o, ast.Constant) and o.value == "completed" for o in operands
            )
            reads_state = any(
                h in (ast.get_source_segment(src, o) or "") for o in operands for h in STATE_HINTS
            )
            if has_literal and reads_state:
                out.append(f"{path.name}:{node.lineno} decides completion from terminal_state")
    return out


def test_no_other_module_decides_success():
    found = []
    for path in python_sources(APP):
        if path in ALLOWED:
            continue
        found.extend(_violations(path))
    assert not found, (
        "Whether a run succeeded is decided once, in app/services/ai_system_outcomes.py "
        "(run_class / success_cond / success_rate):\n  " + "\n  ".join(found)
    )


@pytest.mark.parametrize("snippet,expected", [
    ('_SUCCESSFUL = ("completed", "completed_degraded")\n', 1),
    ("from app.database.models.ai_system_run import SUCCESSFUL_TERMINAL_STATES\n", 1),
    ("def f(run):\n    return run.terminal_state == 'completed'\n", 1),
    ("def f(q, M):\n    return q.filter(M.terminal_state.in_(SUCCESSFUL_TERMINAL_STATES))\n", 1),
    ("X = TERMINAL_COMPLETED\n", 1),
    # A2A task states and other "completed" vocabularies are not runs.
    ("def f(task):\n    return task.state == 'completed'\n", 0),
])
def test_the_guard_catches_the_old_shapes(tmp_path, snippet, expected):
    bad = tmp_path / "bad.py"
    bad.write_text(snippet)
    assert len(_violations(bad)) == expected, _violations(bad)


def test_the_old_copies_are_gone():
    from app.services import ai_system_assurance, ai_system_down_rule, ai_system_envelope

    assert not hasattr(ai_system_assurance, "_SUCCESSFUL")
    assert not hasattr(ai_system_envelope, "_COMPLETED_STATES")
    assert not hasattr(ai_system_down_rule, "RUN_TERMINAL_CLASS")
    assert not hasattr(ai_system_down_rule, "run_failed")
