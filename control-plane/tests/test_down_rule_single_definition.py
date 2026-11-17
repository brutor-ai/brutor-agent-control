# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# ONE "down" rule (one fact, one computation)

On-demand liveness and dependency health went down on ONE unanswered call
(the latest) while the Composition tab's connection line needed three in a
row: a service read "not answering" on its dependency node and "degraded"
on the line drawn to it. The rule now lives in
`app/services/ai_system_down_rule.py` and all three surfaces import it.

Class-level guards:

  * **Static** — no module in `app/` but the rule module defines a
    consecutive-failure / failure-rate threshold (any name containing
    CONSECUTIVE, DOWN_AFTER or FAILURE_RATE bound to a number), or
    classifies a segment outcome itself in Python (a comparison against
    `SEGMENT_UNANSWERED_OUTCOMES` or a `SEGMENT_<OUTCOME>` constant — the
    old "latest call decides" shape, and the connection line's old
    "rejected is an error"). Each of the three surfaces calls the rule and
    the one classification (`classify_segment_outcome`).
  * **Parity** — for every series of inbound-call outcomes (answered,
    rejected, failed, timed out) the three surfaces reach the same verdict
    (down / degraded / ok / idle). `rejected` is the delegate answering
    (it declined the task) on every surface.
"""

from __future__ import annotations

import ast
import itertools
import os
import pathlib
import re

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services import ai_system_down_rule as rule
from app.services.ai_system_composition import CompositionIndex, PairStats, edge_health
from app.services.ai_system_connection_status import connection_status, segment_evidence
from app.services.ai_system_liveness_state import _judge
from tests.source_trees import python_sources

APP = pathlib.Path(__file__).resolve().parent.parent / "app"
SERVICES = APP / "services"
RULE_MODULE = SERVICES / "ai_system_down_rule.py"
LIVENESS_MODULE = SERVICES / "ai_system_liveness_state.py"
THRESHOLD_NAME = re.compile(r"CONSECUTIVE|DOWN_AFTER|FAILURE_RATE")

#: surface module → the rule functions it must call.
SURFACES = {
    LIVENESS_MODULE: ("latest_calls_verdict", "classify_segment_outcome"),
    SERVICES / "ai_system_composition.py": ("segment_calls_verdict",),
    SERVICES / "ai_system_connection_status.py": ("latest_calls_verdict", "classify_segment_outcome"),
    # The shared-service reliability signal classifies served calls the same way.
    SERVICES / "ai_system_service_signals.py": ("classify_segment_outcome",),
    # Health's "failing now" check over the system's own runs. Which runs
    # are evidence, and which failed, is THE run
    # classification (`ai_system_outcomes.down_rule_failed`).
    SERVICES / "ai_system_assurance.py": ("latest_calls_verdict", "down_rule_failed"),
}
#: Literal segment outcomes a surface must not compare against itself (the
#: old reliability shape: `OUTCOME_ANSWERED = "answered"` … `outcome ==
#: OUTCOME_ANSWERED`, which made a rejected call a failure). `blocked` is
#: also the connection line's own traffic vocabulary, so it is not listed.
OUTCOME_LITERALS = {"answered", "rejected", "failed", "timed_out"}


def _literal_classifications(path: pathlib.Path) -> list:
    tree = ast.parse(path.read_text(), str(path))
    aliases = {
        t.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and node.value.value in OUTCOME_LITERALS
        for t in node.targets
        if isinstance(t, ast.Name)
    }
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        for operand in [node.left, *node.comparators]:
            literal = isinstance(operand, ast.Constant) and operand.value in OUTCOME_LITERALS
            alias = isinstance(operand, ast.Name) and operand.id in aliases
            if literal or alias:
                out.append(f"{path.name}:{node.lineno} compares a segment outcome literal itself")
    return out
#: Segment-outcome constants only the rule module may compare against.
OUTCOME_NAMES = {
    "SEGMENT_UNANSWERED_OUTCOMES", "SEGMENT_ANSWERED", "SEGMENT_REJECTED",
    "SEGMENT_FAILED", "SEGMENT_TIMED_OUT",
}


def _targets(node: ast.AST):
    if isinstance(node, ast.Assign):
        return node.targets, node.value
    if isinstance(node, ast.AnnAssign) and node.value is not None:
        return [node.target], node.value
    return [], None


def _violations(path: pathlib.Path) -> list:
    tree = ast.parse(path.read_text(), str(path))
    out = []
    for node in ast.walk(tree):
        targets, value = _targets(node)
        if (
            isinstance(value, ast.Constant)
            and isinstance(value.value, (int, float))
            and not isinstance(value.value, bool)
            # A counter reset / zero default (`consecutive_failures = 0`) is
            # state, not a threshold.
            and value.value != 0
        ):
            for t in targets:
                name = t.id if isinstance(t, ast.Name) else getattr(t, "attr", "")
                if THRESHOLD_NAME.search(name.upper()):
                    out.append(f"{path.name}:{node.lineno} defines threshold {name}")
        if isinstance(node, ast.Compare):
            for operand in [node.left, *node.comparators]:
                if isinstance(operand, ast.Name) and operand.id in OUTCOME_NAMES:
                    out.append(
                        f"{path.name}:{node.lineno} classifies a segment outcome against "
                        f"{operand.id} itself"
                    )
    return out


class TestStaticGuard:
    def test_no_other_module_defines_the_rule(self):
        found = []
        for path in python_sources(APP):
            if path == RULE_MODULE:
                continue
            found.extend(_violations(path))
        assert not found, (
            "The down rule is defined once (app/services/ai_system_down_rule.py); "
            "import its thresholds and call latest_calls_verdict():\n  " + "\n  ".join(found)
        )

    @pytest.mark.parametrize("path,fns", sorted(SURFACES.items()), ids=lambda v: getattr(v, "name", str(v)))
    def test_every_surface_calls_the_rule(self, path, fns):
        tree = ast.parse(path.read_text())
        calls = {
            n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", None)
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
        }
        for fn in fns:
            assert fn in calls, f"{path.name} judges calls but never calls {fn}()"

    @pytest.mark.parametrize("path", sorted(SURFACES), ids=lambda p: p.name)
    def test_no_surface_compares_outcome_literals(self, path):
        found = _literal_classifications(path)
        assert not found, "Use classify_segment_outcome():\n  " + "\n  ".join(found)

    def test_the_literal_guard_catches_the_old_reliability_shape(self, tmp_path):
        bad = tmp_path / "bad.py"
        bad.write_text(
            "OUTCOME_ANSWERED = 'answered'\n"
            "def f(o):\n"
            "    return o == OUTCOME_ANSWERED or o == 'rejected'\n"
        )
        assert len(_literal_classifications(bad)) == 2

    def test_the_guard_catches_the_old_shapes(self, tmp_path):
        bad = tmp_path / "bad.py"
        bad.write_text(
            "DOWN_AFTER_CONSECUTIVE_FAILURES = 3\n"
            "class X:\n"
            "    _degraded_failure_rate: float = 0.05\n"
            "def f(latest):\n"
            "    return latest in SEGMENT_UNANSWERED_OUTCOMES\n"
            "def g(o):\n"
            "    return 'ok' if o == SEGMENT_ANSWERED else 'error'\n"
        )
        v = _violations(bad)
        assert sum("defines threshold" in x for x in v) == 2, v
        assert any("SEGMENT_UNANSWERED_OUTCOMES" in x for x in v), v
        assert any("SEGMENT_ANSWERED" in x for x in v), v


class TestTheRule:
    @pytest.mark.parametrize("failed,expected", [
        ((), None),
        ((False,), "ok"),
        ((True,), "degraded"),
        ((True, True), "degraded"),
        ((True, True, True), "down"),
        ((True, True, False), "degraded"),
        ((False, True, True, True), "degraded"),  # only the latest three count
        ((True, True, True, False), "down"),
        ((False, False, False, True), "ok"),       # pushed out by three good calls
    ])
    def test_verdicts(self, failed, expected):
        assert rule.latest_calls_verdict(failed) == expected

    @pytest.mark.parametrize("outcome,cls", [
        ("answered", "answered"),
        ("rejected", "answered"),   # the delegate declined — an answer
        ("failed", "unanswered"),
        ("timed_out", "unanswered"),
        # The refusal TOKEN: the core stores a gateway refusal as `failed` +
        # `blocked_by_policy`; `segment_call_outcome` reads it as `blocked`.
        ("blocked", "blocked"),
        ("something_new", "unanswered"),
    ])
    def test_one_classification(self, outcome, cls):
        assert rule.classify_segment_outcome(outcome) == cls

    def test_every_segment_outcome_is_classified(self):
        from app.database.models.ai_system_run_segment import SEGMENT_OUTCOMES

        assert set(rule.SEGMENT_OUTCOME_CLASS) == set(SEGMENT_OUTCOMES) | {rule.SEGMENT_REFUSED}

    @pytest.mark.parametrize("stored,cause,token,cls", [
        ("failed", "blocked_by_policy", "blocked", "blocked"),   # the gateway refused
        ("failed", "agent_unreachable", "failed", "unanswered"),
        ("failed", None, "failed", "unanswered"),
        ("timed_out", "blocked_by_policy", "timed_out", "unanswered"),
        ("answered", "delegate_deviated", "answered", "answered"),
        ("rejected", None, "rejected", "answered"),
    ])
    def test_a_refusal_is_read_back_as_the_gateway_s(self, stored, cause, token, cls):
        """The core's segment rules: `failed` +
        `blocked_by_policy` is the gateway's refusal, never an unanswered
        call — or a held / refused delegation makes the delegate look down."""
        assert rule.segment_call_outcome(stored, cause) == token
        assert rule.classify_segment_outcome(token) == cls

    def test_pinned(self):
        assert rule.DOWN_AFTER_CONSECUTIVE_FAILURES == 3
        assert rule.DEGRADED_FAILURE_RATE == 0.05
        assert rule.VERDICT_SCORE == {"ok": 1.0, "degraded": 0.5, "down": 0.0}


# ---------------------------------------------------------------------------
# Parity — the three surfaces over the same inbound calls
# ---------------------------------------------------------------------------

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
OUTCOMES = ("answered", "timed_out", "failed", "rejected")
#: Every series up to one longer than the rule's window, newest first.
SERIES = [
    seq
    for n in range(0, rule.DOWN_AFTER_CONSECUTIVE_FAILURES + 2)
    for seq in itertools.product(OUTCOMES, repeat=n)
]


def _liveness(seq) -> str:
    system = SimpleNamespace(id="svc", system_kind="agent")
    expectation = SimpleNamespace(mode="on_demand", enabled=True)
    v = _judge(
        system,
        expectation=expectation,
        last_run_at=None,
        last_segment_at=NOW if seq else None,
        runs_in_window=0,
        inbound_calls=len(seq),
        recent_inbound_outcomes=list(seq[: rule.DOWN_AFTER_CONSECUTIVE_FAILURES]),
        window_hours=168,
        now=NOW,
    )
    if v.state == "idle":
        return "idle"
    if v.state == "silent":
        return "down"
    return {1.0: "ok", 0.5: "degraded"}[v.score]


def _pair(seq) -> PairStats:
    st = PairStats(calls=len(seq))
    for i, o in enumerate(seq):
        st.by_outcome[o] = st.by_outcome.get(o, 0) + 1
        if i < rule.DOWN_AFTER_CONSECUTIVE_FAILURES:
            st.recent.append((NOW - timedelta(minutes=i), o))
    st.latest_outcome = seq[0] if seq else None
    st.last_seen_at = NOW if seq else None
    return st


class _Index(CompositionIndex):
    """`dependency_status` over one pair, without a database."""

    def __init__(self, st: PairStats):  # noqa: D401 — no super(): no DB
        self.pairs = {("root", "svc"): st}
        self._failing = {}


def _dependency(seq) -> str:
    edge = SimpleNamespace(role="shared_service", depends_on_system_id="svc", agent_card_id=None)
    st = _pair(seq)
    assert edge_health(edge, st) == _Index(st).dependency_status("root", edge)[0]
    health, signal = _Index(st).dependency_status("root", edge)
    if health == "idle":
        return "idle"
    if health == "healthy":
        return "ok"
    return "down" if signal == "liveness" else "degraded"


def _connection(seq) -> str:
    """The connection line's TRAFFIC rule, fed the same calls."""
    st = _pair(seq)
    ev = segment_evidence(st.calls, st.by_outcome, st.last_seen_at, st.recent)
    return connection_status(ev, (), window_hours=168)["status"]


@pytest.mark.parametrize("seq", SERIES, ids=lambda s: ",".join(s) or "none")
def test_liveness_and_dependency_health_agree(seq):
    assert _liveness(seq) == _dependency(seq)


@pytest.mark.parametrize(
    "seq",
    [s for s in SERIES if len(s) <= rule.DOWN_AFTER_CONSECUTIVE_FAILURES],
    ids=lambda s: ",".join(s) or "none",
)
def test_the_connection_line_agrees_over_the_latest_calls(seq):
    """Over a window that holds only the latest calls (so the line's
    window-rate rule adds nothing the latest-calls rule does not), the line
    reads exactly what liveness and the dependency node read."""
    assert _connection(seq) == _liveness(seq) == _dependency(seq)


def test_a_single_unanswered_call_is_degraded_everywhere():
    seq = ("timed_out", "answered", "answered")
    assert _liveness(seq) == _dependency(seq) == _connection(seq) == "degraded"


def test_a_rejected_call_is_an_answer_everywhere():
    seq = ("rejected", "rejected", "rejected")
    assert _liveness(seq) == _dependency(seq) == _connection(seq) == "ok"
    st = _pair(seq)
    ev = segment_evidence(st.calls, st.by_outcome, st.last_seen_at, st.recent)
    assert (ev.errors, ev.blocked) == (0, 0)


def test_a_gateway_block_is_blocked_on_the_line():
    st = _pair(("blocked", "answered", "answered"))
    ev = segment_evidence(st.calls, st.by_outcome, st.last_seen_at, st.recent)
    assert (ev.errors, ev.blocked) == (0, 1)
    out = connection_status(ev, (), window_hours=168)
    assert out["status"] == "degraded" and "blocked by policy" in out["reason"]


def test_refusals_never_make_a_delegate_silent():
    """Three refused calls (stored as `failed` + `blocked_by_policy`) say
    nothing about whether the delegate answers."""
    tokens = tuple(rule.segment_call_outcome("failed", "blocked_by_policy") for _ in range(3))
    assert _liveness(tokens) != "down" and _dependency(tokens) != "down"


def test_the_run_rule_reads_the_one_classification():
    """The recent-runs check reads THE run classification: an
    answer, a failure, or not evidence (skipped)."""
    from app.services.ai_system_outcomes import down_rule_failed

    assert down_rule_failed("success") is False
    assert down_rule_failed("deviation") is True
    assert down_rule_failed("failed") is True
    assert down_rule_failed("no_work") is None
    assert down_rule_failed("not_decisive") is None


def test_three_unanswered_calls_are_down_everywhere():
    seq = ("timed_out", "failed", "timed_out")
    assert _liveness(seq) == _dependency(seq) == _connection(seq) == "down"
