# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# ONE exclusion predicate

Every control-plane signal that grades samples reads
`app/services/ai_system_judgement_exclusion.py`; nothing else decides
whether a sample counts. Class-level guards:

  * **Static** — each signal module calls the helper (the functions named
    in `SIGNAL_MODULES`); no module but the helper (and the writers of the
    two tables) compares a timestamp against an incident window's or a
    baseline epoch's bounds, or names the tables in SQL — a second predicate
    is a second answer.
  * **Behavioural, one case per signal** — samples that degrade the signal
    are seeded, the signal is degraded; an incident window is declared over
    them (for the system, and tenant-wide), the signal is not. Each case
    fails if its module stops calling the predicate (proved by revert).
  * **Parity** — the SQLAlchemy predicate, its SQL-text twin and the pure
    Python rule agree on every sample around a window's edges.
  * **Approval holds** — oversight is a rate (holds decided / holds raised):
    every module that reads `tool_approval_requests` is classified as a
    RATE reader (must call `judged_holds`) or a FACT reader (the ledger, the
    decision endpoints, erasure, sealed-record expiry — sees every hold); an
    unclassified reader fails. A window only excuses what it covers: holds
    raised outside every window that lapsed still count.
"""

from __future__ import annotations

import ast
import os
import re
from datetime import datetime, timedelta, timezone
from itertools import count
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest

from tests.run_outcome_defaults import work_outcome
from sqlalchemy import func, text
from ulid import ULID

from app.core.logging_system import BrutorLogger
from app.database.models.ai_system_drift_resolution import (
    AISystemBaselineEpoch,
    AISystemIncidentWindow,
)
from app.database.models.ai_system_liveness import AISystemLivenessExpectation
from app.database.models.ai_system_run import AISystemRun
from app.database.models.ai_system_run_segment import AISystemRunSegment
from app.database.models.proxy_log import ProxyLog
from app.database.models.resource_group import GroupType, ResourceGroup
from app.services import ai_system_judgement_exclusion as ex
from tests.admin_endpoint_harness import build_admin_harness, throwaway_pg_database
from tests.source_trees import python_sources, source_label

APP = Path(__file__).resolve().parents[1] / "app"
SERVICES = APP / "services"
HELPER = SERVICES / "ai_system_judgement_exclusion.py"

#: signal module → helper functions it must call (the predicate itself).
SIGNAL_MODULES = {
    SERVICES / "ai_system_envelope.py": ("judged_run", "epoch_floors"),
    SERVICES / "ai_system_assurance.py": ("judged_run", "exclusion_summary"),
    SERVICES / "ai_system_service_signals.py": ("judged_segment",),
    SERVICES / "ai_system_liveness_state.py": ("judged_segment",),
    SERVICES / "ai_system_composition.py": ("judged_segment", "judged_log"),
    SERVICES / "ai_system_connection_status.py": ("judged_run",),
    SERVICES / "ai_system_dependency_health.py": ("judged_segment", "judged_log", "evidence_exclusion"),
    # Oversight is a RATE — holds decided / holds raised.
    SERVICES / "ai_system_oversight.py": ("judged_holds",),
    SERVICES / "compliance" / "resolvers" / "oversight.py": ("judged_holds",),
    # The response evaluator and the control plane's own condition emitters
    # ask whether a finding's evidence still counts.
    SERVICES / "ai_system_response" / "actuator.py": ("finding_exclusion",),
    SERVICES / "assurance_checks" / "agent.py": ("evidence_exclusion",),
}

#: Windows excuse PERFORMANCE, never FACTS. These
#: modules decide declared-vs-observed facts (conformance; the composition
#: checks: undeclared dependency, unaccepted caller, role mismatch,
#: grant/edge mismatch; composite compliance) and must NOT apply the sample
#: predicate at all.
FACT_MODULES = (
    SERVICES / "ai_system_conformance.py",
    SERVICES / "assurance_checks" / "composition.py",
    SERVICES / "ai_system_composite_compliance.py",
)
#: The sample predicates — each call must state `purpose=PURPOSE_RATE`.
_PREDICATES = {"judged", "judged_run", "judged_segment", "judged_log", "judged_sql", "judged_holds"}

#: Every module that reads approval holds, classified. RATE readers grade
#: holds (the oversight component, `oversight.exercised`) and must call
#: `judged_holds`; FACT readers list, decide, erase or cite holds and see
#: every one. A new reader must be added to one side — unclassified fails.
HOLD_RATE_READERS = {
    SERVICES / "ai_system_oversight.py",
    SERVICES / "compliance" / "resolvers" / "oversight.py",
}
HOLD_FACT_READERS = {
    # The ledger: list / count / decide holds — every hold, always.
    APP / "api" / "v1" / "endpoints" / "admin" / "tool_approvals.py",
    # GDPR erasure of the subject's tool-call arguments.
    SERVICES / "data_erasure.py",
    # A sealed record's approval expiry (a fact about one record).
    SERVICES / "compliance" / "resolvers" / "records.py",
    # The model registry.
    APP / "database" / "models" / "__init__.py",
    # What one hold on a connection line came to (decided, or lapsed): the
    # line counts lapsed holds apart and never grades them as a rate.
    SERVICES / "ai_system_connection_status.py",
}
_HOLD_SQL = re.compile(r"\b(FROM|JOIN|UPDATE)\s+tool_approval_requests\b", re.IGNORECASE)


def _reads_holds(path: Path) -> bool:
    tree = ast.parse(path.read_text(), str(path))
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and n.id == "ToolApprovalRequest":
            return True
        if isinstance(n, ast.alias) and n.name == "ToolApprovalRequest":
            return True
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and _HOLD_SQL.search(n.value):
            return True
    return False


def _predicate_calls(path: Path, names: frozenset = frozenset()) -> list:
    """(lineno, has purpose=) for every call of a sample predicate (or of
    `names` only, when given)."""
    wanted = names or _PREDICATES
    tree = ast.parse(path.read_text(), str(path))
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            name = n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", None)
            # `c.judged(...)` is the compliance catalog's judged-evidence
            # builder — a different `judged`; the bare helper is a Name.
            if name == "judged" and isinstance(n.func, ast.Attribute):
                continue
            if name in wanted:
                out.append((n.lineno, any(k.arg == "purpose" for k in n.keywords)))
    return out


#: Modules allowed to touch the tables' bounds: the helper (reads), the
#: resolution service (writes, get-or-create on the epoch key), the models.
_BOUNDS_ALLOWED = {
    HELPER,
    SERVICES / "drift_resolution.py",
    APP / "database" / "models" / "ai_system_drift_resolution.py",
    APP / "constants" / "drift_resolution.py",
}
_MODELS = {"AISystemIncidentWindow", "AISystemBaselineEpoch"}
_TABLES = re.compile(r"\bai_system_(incident_windows|baseline_epochs)\b")


def _second_predicates(path: Path) -> list:
    src = path.read_text()
    tree = ast.parse(src, str(path))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for operand in [node.left, *node.comparators]:
                if (
                    isinstance(operand, ast.Attribute)
                    and operand.attr in ("starts_at", "ends_at")
                    and isinstance(operand.value, ast.Name)
                    and operand.value.id in _MODELS
                ):
                    out.append(f"{path.name}:{node.lineno} compares {operand.value.id}.{operand.attr}")
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and _TABLES.search(node.value):
            if "SELECT" in node.value.upper() or "WHERE" in node.value.upper():
                out.append(f"{path.name}:{node.lineno} queries the exclusion tables in SQL")
    return out


class TestStaticGuard:
    @pytest.mark.parametrize("path,fns", sorted(SIGNAL_MODULES.items()),
                             ids=lambda v: getattr(v, "name", str(v)))
    def test_every_signal_reads_the_one_predicate(self, path, fns):
        tree = ast.parse(path.read_text())
        calls = {
            n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", None)
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
        }
        for fn in fns:
            assert fn in calls, f"{path.name} grades samples but never calls {fn}()"

    @pytest.mark.parametrize("path", FACT_MODULES, ids=lambda p: p.name)
    def test_fact_modules_never_apply_the_exclusion(self, path):
        src = path.read_text()
        assert not _predicate_calls(path), f"{path.name} excludes samples from a FACT"
        assert "ai_system_judgement_exclusion" not in src, (
            f"{path.name} decides facts; windows excuse performance, never facts"
        )

    def test_every_predicate_call_states_its_purpose(self):
        missing = [
            f"{source_label(path)}:{line}"
            for path in python_sources(APP)
            if path != HELPER
            for line, has in _predicate_calls(path)
            if not has
        ]
        assert not missing, "pass purpose=PURPOSE_RATE:\n  " + "\n  ".join(missing)

    def test_the_predicate_refuses_any_other_purpose(self):
        from app.database.models.ai_system_run import AISystemRun as R

        with pytest.raises(ValueError):
            ex.judged_run(R, purpose="fact")
        with pytest.raises(ValueError):
            ex.judged_log(ProxyLog, "tenant", purpose="fact")
        with pytest.raises(TypeError):
            ex.judged_run(R)  # purpose is required

    def test_no_second_predicate(self):
        found = []
        for path in python_sources(APP):
            if path in _BOUNDS_ALLOWED:
                continue
            found.extend(_second_predicates(path))
        assert not found, (
            "Judge samples through app/services/ai_system_judgement_exclusion.py:\n  "
            + "\n  ".join(found)
        )

    def test_every_hold_reader_is_classified(self):
        """Oversight is a rate: a module that reads approval holds either
        grades them through `judged_holds` or is a declared FACT reader."""
        readers = {p for p in python_sources(APP) if _reads_holds(p)}
        unclassified = sorted(source_label(p) for p in readers - HOLD_RATE_READERS - HOLD_FACT_READERS)
        assert not unclassified, (
            "Reads approval holds but is not classified. If it grades them as a rate "
            "(decided / raised, lapse rate, a score), call judged_holds(..., "
            "purpose=PURPOSE_RATE) and add it to HOLD_RATE_READERS; if it lists, decides "
            "or cites holds, add it to HOLD_FACT_READERS:\n  " + "\n  ".join(unclassified)
        )
        stale = sorted(str(p) for p in (HOLD_RATE_READERS | HOLD_FACT_READERS) - readers)
        assert not stale, f"classified but no longer reads holds: {stale}"
        for path in HOLD_RATE_READERS:
            calls = _predicate_calls(path, names={"judged_holds"})
            assert calls and all(has for _, has in calls), (
                f"{path.name} grades holds without judged_holds(..., purpose=PURPOSE_RATE)"
            )

    def test_the_hold_guard_sees_orm_and_sql_readers(self, tmp_path):
        orm, sql, none = tmp_path / "orm.py", tmp_path / "sql.py", tmp_path / "none.py"
        orm.write_text("from app.database.models.tool_approval_request import ToolApprovalRequest\n")
        sql.write_text("Q = 'SELECT status FROM tool_approval_requests WHERE tenant_id = :t'\n")
        none.write_text("NOTE = 'a tool_approval_requests row whose token authorised this'\n")
        assert (_reads_holds(orm), _reads_holds(sql), _reads_holds(none)) == (True, True, False)

    def test_the_guard_catches_a_second_predicate(self, tmp_path):
        bad = tmp_path / "bad.py"
        bad.write_text(
            "def f(q, ts):\n"
            "    q.filter(ts >= AISystemIncidentWindow.starts_at, ts < AISystemIncidentWindow.ends_at)\n"
            "    q.filter(AISystemBaselineEpoch.starts_at > ts)\n"
            "    return 'SELECT 1 FROM ai_system_incident_windows WHERE x'\n"
        )
        assert len(_second_predicates(bad)) == 4, _second_predicates(bad)


# ---------------------------------------------------------------------------
# Behavioural — one case per signal
# ---------------------------------------------------------------------------

T1 = "t1"
_n = count(1)
_CLOCK: dict = {}


def _t0() -> datetime:
    """The module's reference time, read on first use inside a test (never
    at import: tests/test_no_import_time_clock.py)."""
    return _CLOCK.setdefault("now", datetime.now(timezone.utc))


def NOW() -> datetime:  # noqa: N802
    return _t0()


def BAD() -> datetime:  # noqa: N802
    """The bad samples sit around BAD; the window [BAD - 1h, BAD + 1h) covers them."""
    return _t0() - timedelta(hours=3)


def GOOD() -> datetime:  # noqa: N802
    return _t0() - timedelta(hours=30)


@pytest.fixture(scope="module")
def database_url():
    yield from throwaway_pg_database("rfc24cp")


@pytest.fixture(scope="module")
def h(database_url):
    from app.api.v1.endpoints.admin.tool_approvals import create_tool_approvals_admin_router

    # The approvals ledger is mounted so the FACT listing can be checked
    # against the oversight rate (a window never hides a hold).
    harness = build_admin_harness(
        [create_tool_approvals_admin_router(BrutorLogger("judgement-exclusion"))],
        logger_name="judgement-exclusion", database_url=database_url,
    )
    yield harness
    harness.close()


@pytest.fixture(autouse=True)
def _no_tenant_wide_leftovers(h):
    yield
    h.seed(lambda s: s.query(AISystemIncidentWindow).filter(
        AISystemIncidentWindow.ai_system_id.is_(None)).delete())


def _system(h, *, kind="agent", clients=None, on_demand=False):
    gid = f"xs-{next(_n)}"

    def fn(s):
        s.add(ResourceGroup(
            id=gid, tenant_id=T1, name=gid, description=gid, hierarchy_level=0,
            inherit_resources=True, is_active=True, is_system=False,
            group_type=GroupType.AI_SYSTEM, system_kind=kind, owner="team", intended_use="x",
            lifecycle_stage="active", intended_client_labels=clients,
        ))
        if on_demand:
            s.flush()
            s.add(AISystemLivenessExpectation(
                id=f"live-{gid}", tenant_id=T1, ai_system_id=gid, mode="on_demand"))
    h.seed(fn)
    return gid


def _run(h, sid, at, *, state="completed", client=None, logs=None):
    rid = f"xr-{next(_n)}"

    def fn(s):
        s.add(AISystemRun(
            id=rid, tenant_id=T1, root_task_id=f"root-{rid}", ai_system_id=sid,
            started_at=at, ended_at=at + timedelta(seconds=5), terminal_state=state, **work_outcome(state),
            finalized_by="explicit", is_replay=False, action_count=1, llm_call_count=1,
            chain_integrity="intact", client_label=client,
        ))
        for status in logs or ():
            s.add(ProxyLog(
                id=f"log-{ULID()}", tenant_id=T1, timestamp=at, proxy_type="llm",
                http_method="POST", endpoint="/v1/chat", server_id="model-x",
                correlation_id=f"corr-{next(_n)}", end_user_session_id="s",
                success=status < 400, http_status_code=status, duration_ms=10,
                root_task_id=f"root-{rid}",
            ))
    h.seed(fn)
    return rid


def _segment(h, root, svc, at, outcome):
    rid = _run(h, root, at)
    h.seed(lambda s: s.add(AISystemRunSegment(
        id=f"xseg-{next(_n)}", run_id=rid, tenant_id=T1, root_task_id=f"root-{rid}",
        root_system_id=root, caller_system_id=root, segment_system_id=svc, min_depth=1,
        first_action_at=at, last_action_at=at + timedelta(seconds=5), action_count=1,
        llm_call_count=1, cost_usd=0.001, outcome=outcome, models_used={},
    )))


def _db(h):
    return h.Factory()


# Each case: seed(h) -> (window system id, measure(db) -> value); bad(value) -> bool.

def _envelope(h):
    from app.services.ai_system_envelope import evaluate_envelope

    sid = _system(h)
    for i in range(25):
        _run(h, sid, GOOD() - timedelta(minutes=i))
    for i in range(60):
        _run(h, sid, BAD() + timedelta(seconds=30 * i), state="errored")

    def measure(db):
        out = evaluate_envelope(db, db.get(ResourceGroup, sid), period_hours=720,
                                envelope={"min_completion_rate": 0.9})
        return out["terms"][0]["status"]
    return sid, measure, lambda v: v == "breached"


def _health_run_facts(h):
    from app.services.ai_system_assurance import _run_facts

    sid = _system(h)
    for i in range(10):
        _run(h, sid, GOOD() - timedelta(minutes=i))
        _run(h, sid, BAD() + timedelta(minutes=i), state="errored")

    def measure(db):
        return _run_facts(db, T1, sid, NOW() - timedelta(days=30))["success_rate"]
    return sid, measure, lambda v: v < 0.8


def _health_recent_runs(h):
    """The down rule over the latest finished runs (health's "failing now")."""
    from app.services.ai_system_assurance import _recent_runs

    sid = _system(h)
    for i in range(10):
        _run(h, sid, GOOD() - timedelta(minutes=i))
    for i in range(3):
        _run(h, sid, BAD() + timedelta(minutes=i), state="errored")

    def measure(db):
        return _recent_runs(db, T1, sid, NOW() - timedelta(days=30))["verdict"]
    return sid, measure, lambda v: v == "down"


def _service_signals(h):
    from app.services.ai_system_service_signals import segment_facts, segment_reliability

    root, svc = _system(h), _system(h, kind="service")
    for i in range(10):
        _segment(h, root, svc, GOOD() - timedelta(minutes=i), "answered")
        _segment(h, root, svc, BAD() + timedelta(minutes=i), "failed")

    def measure(db):
        return segment_reliability(segment_facts(db, T1, svc, NOW() - timedelta(days=30)))
    return svc, measure, lambda v: v < 0.8


def _liveness_down_rule(h):
    from app.services.ai_system_liveness_state import liveness_state

    root, svc = _system(h), _system(h, kind="service", on_demand=True)
    for i in range(5):
        _segment(h, root, svc, GOOD() - timedelta(minutes=i), "answered")
    for i in range(3):
        _segment(h, root, svc, BAD() + timedelta(minutes=i), "timed_out")

    def measure(db):
        return liveness_state(db, db.get(ResourceGroup, svc)).state
    return svc, measure, lambda v: v == "silent"


def _composition_pair(h):
    from app.services.ai_system_composition import CompositionIndex

    root, svc = _system(h), _system(h, kind="service")
    for i in range(5):
        _segment(h, root, svc, NOW() - timedelta(hours=20, minutes=i), "answered")
    for i in range(3):
        _segment(h, root, svc, BAD() + timedelta(minutes=i), "timed_out")

    def measure(db):
        return CompositionIndex(db, T1).pair(root, svc).calls_verdict()
    return svc, measure, lambda v: v == "down"


def _connection_line(h):
    from app.services.ai_system_connection_status import connection_status, own_traffic

    sid = _system(h)
    for i in range(5):
        _run(h, sid, NOW() - timedelta(hours=20, minutes=i), logs=[200])
    for i in range(3):
        _run(h, sid, BAD() + timedelta(minutes=i), logs=[500])

    def measure(db):
        by_key, _ = own_traffic(db, T1, sid, NOW() - timedelta(hours=168))
        return connection_status(by_key.get(("model", "model-x")), (), window_hours=168)["status"]
    return sid, measure, lambda v: v == "down"


def _external_boundary(h):
    """External-edge A2A rows: judged THROUGH THEIR RUN."""
    from app.database.models.agent_card import AgentCard
    from app.database.models.ai_system_dependency import AISystemDependency
    from app.services.ai_system_dependency_health import dependency_health

    sid = _system(h)
    card = f"card-{next(_n)}"

    def fn(s):
        s.add(AgentCard(id=card, tenant_id=T1, name=card, version="1",
                        card_json={"name": card, "capabilities": []}))
        s.flush()
        s.add(AISystemDependency(id=f"dep-{next(_n)}", tenant_id=T1, ai_system_id=sid,
                                 role="external", agent_card_id=card,
                                 supplier_legal_entity="Supplier Ltd"))
    h.seed(fn)
    for i in range(4):
        _a2a(h, sid, card, NOW() - timedelta(hours=20, minutes=i), 200)
    for i in range(4):
        _a2a(h, sid, card, BAD() + timedelta(minutes=i), 502)

    def measure(db):
        (entry,) = dependency_health(db, db.get(ResourceGroup, sid))
        return entry["boundary"]["error_rate"]
    return sid, measure, lambda v: v is not None and v > 0.2


def _a2a(h, sid, card, at, status):
    rid = _run(h, sid, at)
    h.seed(lambda s: s.add(ProxyLog(
        id=f"log-{ULID()}", tenant_id=T1, timestamp=at + timedelta(seconds=1),
        proxy_type="a2a", http_method="POST", endpoint="/a2a", server_id=card,
        correlation_id=f"corr-{next(_n)}", end_user_session_id="s", success=status < 400,
        http_status_code=status, duration_ms=10, resource_group_id=sid,
        root_task_id=f"root-{rid}",
    )))


def _dependency_fallback(h):
    from app.services.ai_system_dependency_health import _fallback_check

    root, svc = _system(h), _system(h, kind="service")
    for i in range(3):
        _segment(h, root, svc, BAD() + timedelta(minutes=i), "timed_out")

    def measure(db):
        return _fallback_check(db, db.get(ResourceGroup, root), svc,
                               NOW() - timedelta(hours=168))["unanswered_calls"]
    return svc, measure, lambda v: v == 3


def _hold(h, gid, at, status, *, by="approver-a"):
    """One approval hold RAISED at `at` by a requester in group `gid`."""
    from app.database.models.tool_approval_request import ToolApprovalRequest

    hid = f"xh-{next(_n)}"
    decided = status in ("approved", "rejected")
    h.seed(lambda s: s.add(ToolApprovalRequest(
        id=hid, tenant_id=T1, capability_type="tool", capability_name="t",
        requester_type="api_key", requester_group_id=gid, status=status,
        timeout_seconds=300, expires_at=at + timedelta(minutes=5),
        resolved_by=by if decided else None,
        resolved_at=(at + timedelta(minutes=1)) if decided else None, created_at=at,
    )))
    return hid


def _oversight_health(h):
    """The health oversight component: holds lapsed inside the window."""
    from app.services.ai_system_oversight import gather_oversight, score_oversight

    sid = _system(h)
    for i in range(6):
        _hold(h, sid, GOOD() - timedelta(minutes=i), "approved", by=f"approver-{i % 3}")
        _hold(h, sid, BAD() + timedelta(minutes=i), "expired")

    def measure(db):
        return score_oversight(gather_oversight(db, T1, [sid], NOW() - timedelta(days=30)))
    return sid, measure, lambda v: v is not None and v < 0.8


def _oversight_compliance(h):
    """Compliance `oversight.exercised` (n of m decided by a human)."""
    from app.services.compliance import resolvers
    from app.services.compliance.catalogs import common

    sid = _system(h)
    for i in range(3):
        _hold(h, sid, GOOD() - timedelta(minutes=i), "approved")
        _hold(h, sid, BAD() + timedelta(minutes=i), "expired")

    def measure(db):
        window = (NOW() - timedelta(days=30), NOW() + timedelta(minutes=1))
        return resolvers.evaluate(db, T1, common.oversight_exercised(), sid, window).status
    return sid, measure, lambda v: v == "not_met"


SIGNALS = {
    "envelope_term": _envelope,
    "health_rate_components": _health_run_facts,
    "health_recent_runs": _health_recent_runs,
    "service_signals": _service_signals,
    "down_rule_liveness": _liveness_down_rule,
    "down_rule_composition_pair": _composition_pair,
    "connection_line": _connection_line,
    "dependency_fallback": _dependency_fallback,
    "external_boundary_rate": _external_boundary,
    "oversight_health": _oversight_health,
    "oversight_compliance_exercised": _oversight_compliance,
}


#: Cases that read through the compliance resolvers, whose SQL is
#: Postgres-only (`= ANY(:gids)`): they run with BRUTOR_PG_TESTS=1 (CI's
#: fresh-install job runs this file on Postgres) and skip on SQLite.
PG_ONLY = {"oversight_compliance_exercised"}


def _needs_pg(database_url):
    if not database_url:
        pytest.skip("compliance resolvers use Postgres-only SQL; set BRUTOR_PG_TESTS=1 (CI fresh-install job)")


def _declare(h, sid, tenant_wide):
    h.seed(lambda s: s.add(AISystemIncidentWindow(
        id=f"iw-{next(_n)}", tenant_id=T1, ai_system_id=None if tenant_wide else sid,
        starts_at=BAD() - timedelta(hours=1), ends_at=BAD() + timedelta(hours=1),
        cause_category="provider_outage", reason="test",
    )))


@pytest.mark.parametrize("scope", ["system", "tenant_wide"])
@pytest.mark.parametrize("signal", sorted(SIGNALS))
def test_in_window_samples_do_not_count(h, database_url, signal, scope):
    if signal in PG_ONLY:
        _needs_pg(database_url)
    sid, measure, bad = SIGNALS[signal](h)
    db = _db(h)
    try:
        before = measure(db)
    finally:
        db.close()
    assert bad(before), f"{signal}: the seeded samples should degrade it (got {before!r})"
    _declare(h, sid, scope == "tenant_wide")
    db = _db(h)
    try:
        after = measure(db)
    finally:
        db.close()
    assert not bad(after), f"{signal}: in-window samples still counted (got {after!r})"


class TestWindowsExcusePerformanceNeverFacts:
    """An undeclared caller seen only inside a window
    is still a finding; its error rate inside the window is not counted."""

    def test_an_in_window_undeclared_client_is_still_a_finding_but_not_a_rate(self, h):
        from app.services.ai_system_assurance import _run_facts
        from app.services.ai_system_conformance import for_system

        sid = _system(h, clients=["portal"])
        for i in range(25):
            _run(h, sid, GOOD() - timedelta(minutes=i), client="portal")
        for i in range(3):
            _run(h, sid, BAD() + timedelta(minutes=i), client="rogue", state="errored")
        _declare(h, sid, False)
        db = _db(h)
        try:
            conf = for_system(db, db.get(ResourceGroup, sid), period_hours=720, granted_resources=[])
            facts = _run_facts(db, T1, sid, NOW() - timedelta(days=30))
        finally:
            db.close()
        assert conf.undeclared_clients == ["rogue"]
        assert any(f.kind == "undeclared_client" and f.subject == "rogue" for f in conf.findings)
        assert conf.observed_call_count == 28
        assert (facts["success_rate"], facts["total_runs"], facts["excluded_runs"]) == (1.0, 25, 3)

    def test_an_in_window_undeclared_dependency_is_still_a_finding(self, h):
        from app.services.ai_system_composition import CompositionIndex
        from app.services.assurance_checks.composition import composition_findings

        root, svc = _system(h), _system(h, kind="service")
        for i in range(3):
            _segment(h, root, svc, BAD() + timedelta(minutes=i), "timed_out")
        _declare(h, root, True)
        db = _db(h)
        try:
            index = CompositionIndex(db, T1)
            st = index.pair(root, svc)
            codes = [f["code"] for f in composition_findings(index, root)]
        finally:
            db.close()
        assert st.calls == 3 and st.judged_calls == 0 and st.calls_verdict() is None
        assert "composition.undeclared_dependency" in codes


class TestOversightHonoursWindows:
    """Oversight is a RATE (holds decided / holds raised), graded like the
    error rate. A window
    only excuses what it covers — a hold raised outside every window that
    lapsed (nobody decided it overnight) is an operations matter and counts.
    """

    @staticmethod
    def _health(h, sid):
        from app.services.ai_system_assurance import compute_health

        db = _db(h)
        try:
            return compute_health(db, db.get(ResourceGroup, sid))
        finally:
            db.close()

    def test_holds_lapsed_inside_a_window_do_not_lower_the_score(self, h):
        sid = _system(h)
        for i in range(3):
            _hold(h, sid, GOOD() - timedelta(minutes=i), "approved", by=f"approver-{i}")
        for i in range(5):
            _hold(h, sid, BAD() + timedelta(minutes=i), "expired")
        before = self._health(h, sid)
        assert before["oversight_score"] == pytest.approx(3 / 8)
        _declare(h, sid, False)
        after = self._health(h, sid)
        assert after["oversight_score"] == 1.0
        section = after["oversight"]
        assert (section["requested"], section["expired"], section["excluded_in_incident_windows"]) == (8, 5, 5)
        assert section["judged"]["requested"] == 3 and section["lapse_rate"] == 0.0
        assert not any("expired without a human decision" in r for r in after["reasons"])
        assert any("5 approval request(s) raised inside declared incident windows" in r for r in after["reasons"])

    def test_holds_lapsed_outside_any_window_still_count(self, h):
        """Overnight lapses count; a hold raised just BEFORE a window opened
        counts even though it lapsed inside it (judged by when it was raised)."""
        sid = _system(h)
        overnight = NOW() - timedelta(hours=10)  # well clear of the window around BAD()
        for i in range(4):
            _hold(h, sid, GOOD() - timedelta(minutes=i), "approved", by=f"approver-{i}")
        for i in range(3):
            _hold(h, sid, overnight + timedelta(minutes=i), "expired")
        _hold(h, sid, BAD() - timedelta(hours=1, minutes=2), "expired")  # raised before, lapsed inside
        _hold(h, sid, BAD(), "expired")  # raised inside: not judged
        _declare(h, sid, False)
        health = self._health(h, sid)
        # 4 judged lapses of 8 judged holds; the in-window one is not judged.
        assert health["oversight_score"] == pytest.approx(0.5)
        section = health["oversight"]
        assert (section["requested"], section["expired"], section["excluded_in_incident_windows"]) == (9, 5, 1)
        assert (section["judged"]["requested"], section["judged"]["expired"]) == (8, 4)
        assert any("4 of 8 judged approval request(s) expired without a human decision" in r
                   for r in health["reasons"])
        # A tenant-wide window elsewhere excuses none of them either.
        h.seed(lambda s: s.add(AISystemIncidentWindow(
            id=f"iw-{next(_n)}", tenant_id=T1, ai_system_id=None,
            starts_at=NOW() - timedelta(hours=2), ends_at=NOW() - timedelta(hours=1),
            cause_category="platform_incident", reason="elsewhere",
        )))
        assert self._health(h, sid)["oversight_score"] == pytest.approx(0.5)

    def test_the_ledger_still_lists_every_hold(self, h):
        from app.services.ai_system_oversight import gather_oversight

        sid = _system(h)
        _hold(h, sid, GOOD(), "approved")
        _hold(h, sid, GOOD() - timedelta(minutes=1), "rejected", by="approver-b")
        ids_inside = {_hold(h, sid, BAD() + timedelta(minutes=i), "expired") for i in range(3)}
        _declare(h, sid, False)

        # The approvals ledger lists every hold, the in-window ones included.
        r = h.client.get(f"/v1/admin/approvals?status_filter=all&group_id={sid}&page_size=200",
                         headers=h.headers(T1))
        assert r.status_code == 200, r.text
        listed = {i["id"] for i in r.json()["items"]}
        assert len(listed) == 5 and ids_inside <= listed
        r = h.client.get(f"/v1/admin/approvals?status_filter=expired&group_id={sid}",
                         headers=h.headers(T1))
        assert {i["id"] for i in r.json()["items"]} == ids_inside

        # The oversight facts count the complete record beside the exclusion.
        db = _db(h)
        try:
            facts = gather_oversight(db, T1, [sid], NOW() - timedelta(days=30))
        finally:
            db.close()
        assert (facts["requested"], facts["expired"], facts["decided"]) == (5, 3, 2)
        assert facts["excluded_in_incident_windows"] == 3 and facts["judged"]["requested"] == 2

    def test_compliance_counts_every_hold_and_grades_the_judged(self, h, database_url):
        _needs_pg(database_url)
        from app.services.compliance import oversight as compliance_oversight
        from app.services.compliance import resolvers
        from app.services.compliance.catalogs import common

        sid = _system(h)
        _hold(h, sid, GOOD(), "approved")
        _hold(h, sid, GOOD() - timedelta(minutes=1), "rejected", by="approver-b")
        for i in range(3):
            _hold(h, sid, BAD() + timedelta(minutes=i), "expired")
        _hold(h, sid, NOW() - timedelta(hours=10), "expired")  # overnight, outside: counts
        _declare(h, sid, False)
        window = (NOW() - timedelta(days=30), NOW() + timedelta(minutes=1))
        db = _db(h)
        try:
            stmt = resolvers.evaluate(db, T1, common.oversight_exercised(), sid, window)
            summary = compliance_oversight.summary(db, T1, sid, *window)
        finally:
            db.close()
        assert (stmt.status, stmt.n, stmt.m) == ("not_met", 2, 3)
        assert stmt.facts == {"requested": 6, "decided": 2, "decided_by_human": 2, "lapsed": 4,
                              "in_flight": 0, "excluded_in_incident_windows": 3}
        assert "3 inside incident windows, not judged" in stmt.detail
        assert summary["approvals"]["requested"] == 6 and summary["approvals"]["lapsed"] == 4
        assert summary["approvals"]["excluded_in_incident_windows"] == 3
        (row,) = summary["systems"]
        assert row["approvals"]["facts"]["excluded_in_incident_windows"] == 3

    def test_every_hold_inside_a_window_is_unmeasured_not_passed(self, h):
        from app.services.ai_system_oversight import gather_oversight, oversight_report_section, score_oversight

        sid = _system(h)
        for i in range(2):
            _hold(h, sid, BAD() + timedelta(minutes=i), "expired")
        _declare(h, sid, True)
        db = _db(h)
        try:
            facts = gather_oversight(db, T1, [sid], NOW() - timedelta(days=30))
        finally:
            db.close()
        assert score_oversight(facts) is None
        section = oversight_report_section(facts)
        assert section["applicable"] is False and "unmeasured" in section["means"]
        assert section["requested"] == 2 and section["excluded_in_incident_windows"] == 2

    def test_the_report_states_the_excluded_holds(self, h):
        import jsonschema

        from app.services.ai_system_assurance import build_report
        from app.standards import REPORT_SPEC, load_schema

        sid = _system(h)
        _run(h, sid, GOOD())
        _hold(h, sid, GOOD(), "approved")
        for i in range(4):
            _hold(h, sid, BAD() + timedelta(minutes=i), "expired")
        _declare(h, sid, False)
        db = _db(h)
        try:
            report = build_report(db, db.get(ResourceGroup, sid))
        finally:
            db.close()
        assert report["judgement_exclusions"]["excluded_approval_requests"] == 4
        period = report["coverage"]["excluded_period"]
        assert period["excluded_approval_requests"] == 4
        assert "4 approval request(s) raised inside them" in period["statement"]
        assert report["health"]["oversight"]["excluded_in_incident_windows"] == 4
        assert report["health"]["oversight"]["excluded_statement"].startswith("4 approval request(s)")
        jsonschema.validate(report, load_schema(REPORT_SPEC))

    def test_a_hold_is_judged_against_its_nearest_ai_system(self, h):
        """A hold raised in a plain child group belongs to the nearest AI
        System above it; another system's window does not cover it."""
        sid, other = _system(h), _system(h)
        child = f"xc-{next(_n)}"
        h.seed(lambda s: s.add(ResourceGroup(
            id=child, tenant_id=T1, name=child, description=child, hierarchy_level=1,
            parent_group_id=sid, inherit_resources=True, is_active=True, is_system=False,
        )))
        _declare(h, sid, False)
        _declare(h, other, False)
        db = _db(h)
        try:
            flags = ex.judged_holds(
                db, T1,
                [(child, BAD()), (sid, BAD()), (child, GOOD()), ("no-such-group", BAD())],
                purpose=ex.PURPOSE_RATE,
            )
        finally:
            db.close()
        assert flags == [False, False, True, True]
        with pytest.raises(ValueError):
            ex.judged_holds(None, T1, [], purpose="fact")


def test_a_window_on_the_run_root_covers_its_segments(h):
    """A segment belongs to its run: a window declared on the ROOT covers it."""
    from app.services.ai_system_service_signals import segment_facts

    root, svc = _system(h), _system(h, kind="service")
    _segment(h, root, svc, BAD(), "failed")
    _declare(h, root, False)
    db = _db(h)
    try:
        facts = segment_facts(db, T1, svc, NOW() - timedelta(days=1))
        assert (facts["served_calls"], facts["excluded_calls"]) == (0, 1)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Parity — SQLAlchemy predicate, SQL text, pure rule
# ---------------------------------------------------------------------------


def test_the_three_forms_agree_at_the_edges(h):
    sid = _system(h)
    start, end = BAD() - timedelta(hours=1), BAD() + timedelta(hours=1)
    _declare(h, sid, False)
    samples = {
        "before": start - timedelta(microseconds=1),
        "at_start": start,
        "inside": BAD(),
        "at_end": end,
        "after": end + timedelta(seconds=1),
    }
    ids = {k: _run(h, sid, at) for k, at in samples.items()}
    db = _db(h)
    try:
        orm = {
            rid for (rid,) in db.query(AISystemRun.id).filter(
                AISystemRun.ai_system_id == sid, ex.judged_run(AISystemRun, purpose=ex.PURPOSE_RATE))
        }
        sql = {
            rid for (rid,) in db.execute(text(
                "SELECT r.id FROM ai_system_runs r WHERE r.ai_system_id = :s AND "
                + ex.judged_sql("r.tenant_id", "r.started_at", "r.ai_system_id", purpose=ex.PURPOSE_RATE)
            ), {"s": sid})
        }
        spans = [ex.Span(w.starts_at, w.ends_at) for w in ex.windows_for(db, T1, [sid])]
        py = {ids[k] for k, at in samples.items() if not ex.sample_excluded(at, windows=spans)}
    finally:
        db.close()
    assert orm == sql == py == {ids["before"], ids["at_end"], ids["after"]}


def test_epoch_floor_reads_the_caller_and_all(h):
    sid, caller = _system(h), _system(h)

    def epoch(metric, caller_id, starts):
        h.seed(lambda s: s.add(AISystemBaselineEpoch(
            id=f"ep-{next(_n)}", tenant_id=T1, ai_system_id=sid, metric_key=metric,
            basis="segments", caller_system_id=caller_id, starts_at=starts,
            kind="accepted_change")))

    epoch("m", "all", NOW() - timedelta(days=3))
    epoch("m", caller, NOW() - timedelta(days=1))
    epoch("other", "all", NOW())
    db = _db(h)
    try:
        f_all = ex.epoch_floor(db, T1, sid, "m", basis="segments")
        f_caller = ex.epoch_floor(db, T1, sid, "m", basis="segments", caller_system_id=caller)
        f_runs = ex.epoch_floor(db, T1, sid, "m", basis="runs")
    finally:
        db.close()
    assert f_all == NOW() - timedelta(days=3)
    assert f_caller == NOW() - timedelta(days=1)  # the later of its own and 'all'
    assert f_runs is None


def test_the_envelope_term_is_graded_from_its_epoch(h):
    """accepted_change on a term's breach: that term is graded only on runs
    at/after the epoch — insufficient evidence until enough arrive."""
    from app.services.ai_system_envelope import MIN_EVIDENCE_RUNS, evaluate_envelope

    sid = _system(h)
    for i in range(30):
        _run(h, sid, GOOD() - timedelta(minutes=i), state="errored")
    floor = NOW() - timedelta(hours=10)
    h.seed(lambda s: s.add(AISystemBaselineEpoch(
        id=f"ep-{next(_n)}", tenant_id=T1, ai_system_id=sid,
        metric_key="envelope:min_completion_rate", basis="runs", caller_system_id="all",
        starts_at=floor, kind="accepted_change")))

    def status():
        db = _db(h)
        try:
            out = evaluate_envelope(db, db.get(ResourceGroup, sid), period_hours=720,
                                    envelope={"min_completion_rate": 0.9, "max_error_rate": 0.5})
            return {t["term"]: (t["status"], t["epoch_starts_at"] is not None) for t in out["terms"]}
        finally:
            db.close()

    got = status()
    assert got["min_completion_rate"] == ("insufficient_evidence", True)
    assert got["max_error_rate"] == ("breached", False)  # other terms keep the period
    for i in range(MIN_EVIDENCE_RUNS):
        _run(h, sid, NOW() - timedelta(hours=5, minutes=i))
    assert status()["min_completion_rate"] == ("met", True)


def test_the_count_of_excluded_runs_is_disclosed(h):
    sid = _system(h)
    _run(h, sid, BAD())
    _run(h, sid, GOOD())
    _declare(h, sid, False)
    db = _db(h)
    try:
        summary = ex.exclusion_summary(db, T1, sid, since=NOW() - timedelta(days=2))
        n_rows = db.query(func.count(AISystemRun.id)).filter(AISystemRun.ai_system_id == sid).scalar()
    finally:
        db.close()
    assert summary["excluded_runs"] == 1 and n_rows == 2  # excluded, never deleted
    assert summary["excluded_hours"] == pytest.approx(2.0, abs=0.01)
