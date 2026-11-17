# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""ISO 42001 Annex A control catalog — model + seed smoke tests."""
import os
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "x")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.database.models.iso42001 import (
    Iso42001Control, Iso42001ControlCoverage,
    COVERAGE_STATUSES, COVERAGE_IMPLEMENTED,
)
from app.database.seed_iso42001_controls import seed_iso42001_controls, ISO42001_CONTROLS


@pytest.fixture()
def db():
    e = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True)
    Base.metadata.create_all(e, tables=[Iso42001Control.__table__, Iso42001ControlCoverage.__table__])
    s = sessionmaker(bind=e, autoflush=False)()
    try:
        yield s
    finally:
        s.close(); e.dispose()


def test_seed_populates_full_annex_a(db):
    n = seed_iso42001_controls(db); db.commit()
    assert n == len(ISO42001_CONTROLS)
    assert db.query(Iso42001Control).count() == len(ISO42001_CONTROLS)
    # Every clause A.2–A.10 represented.
    clauses = {c.clause for c in db.query(Iso42001Control).all()}
    assert {"A.2", "A.3", "A.4", "A.5", "A.6", "A.7", "A.8", "A.9", "A.10"} <= clauses


def test_seed_is_idempotent(db):
    seed_iso42001_controls(db); db.commit()
    assert seed_iso42001_controls(db) == 0  # no changes second run


def test_covered_controls_have_notes(db):
    seed_iso42001_controls(db); db.commit()
    covered = db.query(Iso42001Control).filter_by(brutor_capability="covered").all()
    assert len(covered) >= 4  # A.6.2.6, A.6.2.8, A.9.2, A.10.3 at least
    assert all(c.brutor_note for c in covered)


def test_coverage_status_validation(db):
    seed_iso42001_controls(db); db.commit()
    cov = Iso42001ControlCoverage(id="c1", tenant_id="t1", control_id="A.2.2", status=COVERAGE_IMPLEMENTED)
    db.add(cov); db.commit()
    assert db.query(Iso42001ControlCoverage).one().status == COVERAGE_IMPLEMENTED
    with pytest.raises(ValueError):
        Iso42001ControlCoverage(id="c2", tenant_id="t1", control_id="A.2.3", status="bogus")


# --- AIMS service: coverage / SoA / readiness / evidence pack --------------
from app.database.models.resource_group import GroupType, ResourceGroup
from app.services import iso42001_service as isvc
from app.services.iso42001_service import Iso42001Error


def _ai_system_rg(rid: str, name: str, tenant_id: str = "t1") -> ResourceGroup:
    """Design Note 002: an AI System is a Resource Group with group_type='ai_system'."""
    return ResourceGroup(
        id=rid, tenant_id=tenant_id, name=name, group_type=GroupType.AI_SYSTEM,
        hierarchy_level=0, hierarchy_path=rid, is_active=True,
    )


@pytest.fixture()
def aims_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.database import Base
    e = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                      poolclass=StaticPool, future=True)
    # Design Note 002: the AI-System inventory is now Resource Groups, and RG.to_dict()
    # / the selectin end_user_groups relationship touch several association
    # tables, so create the full schema rather than a hand-picked subset.
    Base.metadata.create_all(e)
    s = sessionmaker(bind=e, autoflush=False)()
    seed_iso42001_controls(s); s.commit()
    try:
        yield s
    finally:
        s.close(); e.dispose()


def test_coverage_auto_derivation_and_source(aims_db):
    cov = isvc.get_coverage(aims_db, "t1")
    assert cov["controls_total"] == len(ISO42001_CONTROLS)
    by_id = {c["control_id"]: c for c in cov["controls"]}

    # A 'covered' control is LIVE-derived (source=auto). With no tenant data it
    # is not_implemented — it doesn't assert coverage the tenant hasn't earned.
    a42 = by_id["A.4.2"]
    assert a42["source"] == "auto"
    assert a42["status"] == "not_implemented"
    assert a42["explicit"] is False

    # A 'partial' control has no live check → static capability default.
    a22 = by_id["A.2.2"]
    assert a22["source"] == "default"
    assert a22["status"] == "partially_implemented"

    # Fail-safe: derivers over tables absent from this fixture must not raise;
    # they resolve to auto/not_implemented.
    a_10_3 = by_id["A.10.3"]
    assert a_10_3["source"] == "auto" and a_10_3["status"] == "not_implemented"

    # DOCUMENT a resource (an AI system) → A.4.2 flips to implemented (auto).
    aims_db.add(_ai_system_rg("aisys-1", "Sys"))
    aims_db.commit()
    cov2 = isvc.get_coverage(aims_db, "t1")
    a42b = next(c for c in cov2["controls"] if c["control_id"] == "A.4.2")
    assert a42b["status"] == "implemented" and a42b["source"] == "auto"
    assert cov2["controls_satisfied"] >= 1

    clauses = {c["clause"] for c in cov2["per_clause"]}
    assert {"A.6", "A.9", "A.10"} <= clauses


def test_set_coverage_overrides_default(aims_db):
    isvc.set_coverage(aims_db, "t1", "A.2.2", status="implemented", owner="ciso@acme")
    aims_db.commit()
    cov = isvc.get_coverage(aims_db, "t1")
    a22 = next(c for c in cov["controls"] if c["control_id"] == "A.2.2")
    assert a22["status"] == "implemented" and a22["owner"] == "ciso@acme" and a22["explicit"] is True
    assert a22["source"] == "manual"
    with pytest.raises(Iso42001Error):
        isvc.set_coverage(aims_db, "t1", "A.2.2", status="bogus")
    with pytest.raises(Iso42001Error):
        isvc.set_coverage(aims_db, "t1", "A.99.99", status="implemented")  # unknown control


def test_evidence_pack_includes_system_inventory(aims_db):
    aims_db.add(_ai_system_rg("aisys-1", "Claims Copilot"))
    aims_db.commit()
    pack = isvc.evidence_pack(aims_db, "t1", generated_at="2026-07-02T00:00:00Z")
    assert pack["standard"] == "ISO/IEC 42001:2023"
    assert pack["ai_system_count"] == 1
    assert pack["ai_system_inventory"][0]["name"] == "Claims Copilot"
    assert len(pack["statement_of_applicability"]) == len(ISO42001_CONTROLS)


def test_iso42001_router_builds():
    from app.api.v1.endpoints.admin.iso42001_aims import create_iso42001_aims_router
    from app.core.logging_system import BrutorLogger
    assert create_iso42001_aims_router(BrutorLogger("t")) is not None
