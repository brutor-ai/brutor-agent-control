# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Evidence source. The OTel inlet lets a run reach the ledger without
the gateway having witnessed it. Every surface that reads runs must keep
"reported" and "witnessed" apart: the chain vocabulary, the run list, the
signal split, the report's gaps and recommendations, and the verdict basis.
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.api.v1.endpoints.admin.ai_system_runs import create_ai_system_runs_router
from app.constants.assurance import RUN_FACT_GUIDE, RUN_FACT_KEYS
from app.core.logging_system import BrutorLogger
from app.database.models.ai_system_run import (
    CHAIN_CLIENT_ASSERTED,
    CHAIN_INTACT,
    CHAIN_INTEGRITIES,
    CHAIN_OBSERVED_COMPLETE,
    CHAIN_OBSERVED_EXTERNAL,
    CHAIN_PARTIAL,
    RUN_SOURCE_GATEWAY,
    RUN_SOURCE_MIXED,
    RUN_SOURCE_OTEL,
    RUN_SOURCES,
    AISystemRun,
)
from app.database.models.resource_group import GroupType, ResourceGroup
from tests.admin_endpoint_harness import build_admin_harness

TENANT = "t1"
SYS = "sys-otel"


def _now() -> datetime:
    """The wall clock, read when used — never at import (tests/test_no_import_time_clock.py)."""
    return datetime.now(timezone.utc)


def _run(i: int, chain: str, source: str, state: str = "completed") -> AISystemRun:
    return AISystemRun(
        id=f"run-{i}", tenant_id=TENANT, root_task_id=f"otel:{i:032x}", ai_system_id=SYS,
        started_at=_now() - timedelta(minutes=i), ended_at=_now() - timedelta(minutes=i, seconds=-30),
        terminal_state=state, finalized_by="terminal", is_replay=False,
        chain_integrity=chain, source=source, action_count=3, total_cost_usd=0,
    )


@pytest.fixture(scope="module")
def harness():
    h = build_admin_harness([create_ai_system_runs_router(BrutorLogger("otel-source-tests"))],
                            logger_name="otel-source-tests")

    def seed(s):
        s.add(ResourceGroup(
            id=SYS, tenant_id=TENANT, name="Exported agent", description="a", hierarchy_level=0,
            inherit_resources=True, is_active=True, is_system=False,
            group_type=GroupType.AI_SYSTEM,
        ))
        s.flush()
        # 2 proxied, 1 client-asserted, 2 ingested, 1 partial → 5 observed of 6.
        s.add(_run(1, CHAIN_INTACT, RUN_SOURCE_GATEWAY))
        s.add(_run(2, CHAIN_INTACT, RUN_SOURCE_GATEWAY))
        s.add(_run(3, CHAIN_CLIENT_ASSERTED, RUN_SOURCE_GATEWAY))
        s.add(_run(4, CHAIN_OBSERVED_EXTERNAL, RUN_SOURCE_OTEL))
        s.add(_run(5, CHAIN_OBSERVED_EXTERNAL, RUN_SOURCE_MIXED, state="errored"))
        s.add(_run(6, CHAIN_PARTIAL, RUN_SOURCE_GATEWAY))

    h.seed(seed)
    yield h
    h.close()


# ---------------------------------------------------------------------------
# Vocabulary — the wire contract with gateway-core
# ---------------------------------------------------------------------------

def test_external_is_a_chain_integrity_and_counts_as_observed():
    assert CHAIN_OBSERVED_EXTERNAL == "observed_external"
    assert CHAIN_OBSERVED_EXTERNAL in CHAIN_INTEGRITIES
    # Whole as reported → may teach a baseline. Fragments still may not.
    assert CHAIN_OBSERVED_EXTERNAL in CHAIN_OBSERVED_COMPLETE
    assert CHAIN_PARTIAL not in CHAIN_OBSERVED_COMPLETE


def test_run_sources_are_pinned():
    assert RUN_SOURCES == {"gateway", "otel", "mixed"}


def test_source_is_in_the_check_vocabulary():
    assert "source" in RUN_FACT_KEYS
    assert "source" in RUN_FACT_GUIDE
    assert tuple(RUN_FACT_KEYS) == tuple(sorted(RUN_FACT_KEYS)), "the pinned vocabulary stays sorted"


def test_model_defaults_to_gateway_and_external_runs_are_baseline_eligible():
    r = AISystemRun(
        id="x", tenant_id=TENANT, root_task_id="r", ai_system_id=SYS, started_at=_now(),
        terminal_state="completed", finalized_by="terminal", chain_integrity=CHAIN_OBSERVED_EXTERNAL,
    )
    assert r.is_baseline_eligible
    assert r.to_dict()["source"] == "gateway", "an unset source means proxied, never unknown"


def test_model_rejects_an_unknown_chain_value():
    with pytest.raises(ValueError):
        AISystemRun(
            id="y", tenant_id=TENANT, root_task_id="r", ai_system_id=SYS, started_at=_now(),
            terminal_state="completed", finalized_by="terminal", chain_integrity="observed_somehow",
        )


# ---------------------------------------------------------------------------
# Surfaces
# ---------------------------------------------------------------------------

def test_run_list_carries_the_source(harness):
    r = harness.client.get(f"/v1/admin/ai-systems/{SYS}/runs?limit=50", headers=harness.headers(TENANT))
    assert r.status_code == 200, r.text
    by_id = {run["id"]: run for run in r.json()["runs"]}
    assert by_id["run-1"]["source"] == "gateway"
    assert by_id["run-4"]["source"] == "otel"
    assert by_id["run-4"]["chain_integrity"] == "observed_external"
    assert by_id["run-5"]["source"] == "mixed"


def test_stats_split_reports_external_inside_observed(harness):
    r = harness.client.get(f"/v1/admin/ai-systems/{SYS}/runs/stats?period_hours=24",
                           headers=harness.headers(TENANT))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["external_pct"] == pytest.approx(2 / 6, abs=1e-5)
    assert body["asserted_pct"] == pytest.approx(1 / 6, abs=1e-5)
    assert body["chain_integrity_pct"] == pytest.approx(2 / 6, abs=1e-5)
    # Observed = signed + asserted + external; the partial run is the only gap.
    assert body["observed_pct"] == pytest.approx(5 / 6, abs=1e-5)


def test_report_facts_carry_the_same_split(harness):
    from app.services.ai_system_assurance import _run_facts

    s = harness.Factory()
    try:
        facts = _run_facts(s, TENANT, SYS, _now() - timedelta(days=1))
    finally:
        s.close()
    assert facts["total_runs"] == 6
    assert facts["external_pct"] == pytest.approx(2 / 6)
    assert facts["observed_pct"] == pytest.approx(5 / 6)
    assert facts["chain_integrity_pct"] == pytest.approx(2 / 6)


def test_not_covered_names_ingested_runs():
    from app.services.ai_system_assurance import _not_covered

    gaps = _not_covered(
        runs={"total_runs": 6, "observed_pct": 1.0, "asserted_pct": 0.0, "external_pct": 0.5},
        suite=None, conf=SimpleNamespace(scope_note=""),
    )
    hit = [g for g in gaps if "ingested from external telemetry" in g]
    assert len(hit) == 1, gaps
    assert "50%" in hit[0]
    assert "applied no control" in hit[0]


def test_not_covered_is_silent_when_everything_was_proxied():
    from app.services.ai_system_assurance import _not_covered

    gaps = _not_covered(
        runs={"total_runs": 6, "observed_pct": 1.0, "asserted_pct": 0.0, "external_pct": 0.0},
        suite=None, conf=SimpleNamespace(scope_note=""),
    )
    assert not any("external telemetry" in g for g in gaps)


def test_recommends_routing_through_the_gateway_when_any_run_was_ingested():
    from app.services.ai_system_assurance import _recommended_controls

    import inspect
    params = inspect.signature(_recommended_controls).parameters
    kw = dict(
        runs={"total_runs": 6, "asserted_pct": 0.0, "chain_integrity_pct": 1.0, "external_pct": 0.2},
        contract_compliance={"under_signature": True, "envelope": None},
        open_drift=[],
    )
    for name, p in params.items():
        if name not in kw and p.default is inspect.Parameter.empty:
            kw[name] = None
    recs = _recommended_controls(**kw)
    rec = next((r for r in recs if r["control"] == "route_through_gateway"), None)
    assert rec is not None, [r["control"] for r in recs]
    assert rec["severity"] == "high"

    kw["runs"] = {"total_runs": 6, "asserted_pct": 0.0, "chain_integrity_pct": 1.0, "external_pct": 0.0}
    assert not any(r["control"] == "route_through_gateway" for r in _recommended_controls(**kw))


# ---------------------------------------------------------------------------
# Verdict — reported-only evidence cannot earn an envelope-basis verdict
# ---------------------------------------------------------------------------

def _verdict_for(external_pct: float, *, under_signature: bool):
    from app.services.ai_system_assurance import _assurance_status, _verdict

    v = _verdict(
        runs={"total_runs": 10, "external_pct": external_pct, "observed_pct": 1.0},
        envelope_eval=None, exceptions={"total": 0}, open_drift=0,
        under_signature=under_signature,
    )
    return v, _assurance_status(v)


def test_all_ingested_runs_cap_the_verdict_at_conditionally_assured():
    v, status = _verdict_for(1.0, under_signature=True)
    assert v["status"] == "assured"
    assert v["basis"] == "reported_telemetry"
    assert "exporter's account" in v["conclusion"]
    assert status["code"] == "conditionally_assured"


def test_partly_ingested_runs_keep_the_signed_basis():
    v, status = _verdict_for(0.4, under_signature=True)
    assert v["basis"] == "signed_envelope"
    assert status["code"] == "assured"


def test_no_runs_is_insufficient_evidence_not_reported_telemetry():
    from app.services.ai_system_assurance import _verdict

    v = _verdict(
        runs={"total_runs": 0, "external_pct": None}, envelope_eval=None,
        exceptions={"total": 0}, open_drift=0, under_signature=True,
    )
    assert v["status"] == "insufficient_evidence"
    assert v["basis"] == "signed_envelope"
