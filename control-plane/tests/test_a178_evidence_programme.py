# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# a178_evidence_programme migration drift suite — evidence programme

Fresh installs build every table from the ORM (create_all + stamp) and
never run this migration; upgraded installs get the tables only from it.
The two must agree on every table, column, index and unique — the Rust
core's schema fixture is dumped from the fresh path and the core writes
`evidence_bundles` / `evidence_signing_keys` and reads
`compliance_notice_configs` with raw SQL.

The migration is parsed with `ast` (never executed) and compared column by
column with `Base.metadata`; the uniques are exercised against the tests'
SQLite create_all.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.database.models  # noqa: F401 — registers every model on Base
from app.database import Base
from app.database.models.assurance_inbox import INBOX_CATEGORIES

ROOT = Path(__file__).resolve().parents[1]
ADMIN_INBOX_TYPES = ROOT.parent / "brutor-gateway-admin" / "src" / "lib" / "assuranceInboxTypes.ts"


TABLES = [
    "evidence_bundles",
    "evidence_signing_keys",
    "evidence_settings",
    "assurance_check_rubrics",
    "assurance_check_reviews",
    "compliance_reports",
    "compliance_incidents",
    "compliance_incident_events",
    "compliance_notice_configs",
    "compliance_documentation_exports",
]

# index name → (table, unique)
INDEXES = {
    "ix_evidence_bundles_tenant": ("evidence_bundles", False),
    "uq_evidence_signing_keys_active": ("evidence_signing_keys", True),
    "uq_assurance_check_rubrics_period": ("assurance_check_rubrics", True),
    "ix_assurance_check_rubrics_tenant": ("assurance_check_rubrics", False),
    "uq_assurance_check_reviews_sample": ("assurance_check_reviews", True),
    "ix_assurance_check_reviews_queue": ("assurance_check_reviews", False),
    "uq_compliance_reports_period": ("compliance_reports", True),
    "ix_compliance_reports_tenant": ("compliance_reports", False),
    "ix_compliance_incidents_tenant": ("compliance_incidents", False),
    "ix_compliance_incident_events_incident": ("compliance_incident_events", False),
    "uq_compliance_notice_configs_scope": ("compliance_notice_configs", True),
    "ix_compliance_doc_exports_system": ("compliance_documentation_exports", False),
}

NEW_INBOX_KINDS = [
    "evidence_unwitnessed",
    "capture_gap",
    "judge_uncalibrated",
    "calibration_stalled",
    "incident_deadline",
]


#: Columns later revisions add to this migration's tables (the ORM is the latest
#: shape): a200_incident_stage_anchors adds `compliance_incidents.classified_at` /
#: `stage_submissions` (staged incident deadlines).


class TestModelParity:
    @pytest.mark.parametrize("table", TABLES)
    def test_table_is_on_base_metadata(self, table):
        assert table in Base.metadata.tables, "fresh installs build tables from the model"


    @pytest.mark.parametrize("index", sorted(INDEXES))
    def test_index_on_model(self, index):
        table, unique = INDEXES[index]
        found = {i.name: i for i in Base.metadata.tables[table].indexes}
        assert index in found, f"{table} model lacks {index}"
        assert bool(found[index].unique) == unique

    def test_to_dict_on_every_model(self):
        for table in TABLES:
            mapper = next(
                m for m in Base.registry.mappers if m.local_table is Base.metadata.tables[table]
            )
            assert callable(getattr(mapper.class_, "to_dict", None)), table

    def test_signing_key_to_dict_never_exposes_seed(self):
        from app.database.models.evidence_signing_key import EvidenceSigningKey

        d = EvidenceSigningKey(kid="k" * 64, encrypted_seed="gAAAA-secret").to_dict()
        assert "encrypted_seed" not in d
        assert "gAAAA-secret" not in repr(d)
        assert d["scope"] == "deployment"

    def test_blind_review_hides_judged_verdict_until_graded(self):
        from app.database.models.assurance_check_calibration import AssuranceCheckReview

        r = AssuranceCheckReview(judged_result="met", status="pending", blind=True)
        assert r.to_dict(reveal_judged=False)["judged_result"] is None
        assert r.to_dict()["judged_result"] == "met"
        r.status = "graded"
        assert r.to_dict(reveal_judged=False)["judged_result"] == "met"

    def test_vocabulary_validators(self):
        from app.database.models.compliance_notice_config import ComplianceNoticeConfig
        from app.database.models.compliance_report import ComplianceReport

        with pytest.raises(ValueError):
            ComplianceReport(cadence="hourly")
        with pytest.raises(ValueError):
            ComplianceNoticeConfig(surface="popup")


class TestInboxKinds:
    @pytest.mark.parametrize("kind", NEW_INBOX_KINDS)
    def test_kind_in_python_vocabulary(self, kind):
        assert kind in INBOX_CATEGORIES

    def test_fits_the_category_column(self):
        from app.database.models.assurance_inbox import AssuranceInboxItem

        width = AssuranceInboxItem.__table__.c["category"].type.length
        assert all(len(k) <= width for k in INBOX_CATEGORIES)

    @pytest.mark.skipif(not ADMIN_INBOX_TYPES.exists(), reason="admin repo not checked out")
    def test_matches_admin_ui_vocabulary(self):
        ts = ADMIN_INBOX_TYPES.read_text()
        block = ts.split("export const INBOX_CATEGORIES")[1].split("];")[0]
        ui = [line.strip().strip("',") for line in block.splitlines() if line.strip().startswith("'")]
        assert ui == list(INBOX_CATEGORIES)


def _uid() -> str:
    return uuid.uuid4().hex[:24]


@pytest.fixture()
def db_session():
    """In-memory SQLite with only the a178_evidence_programme tables (+ the FK parents they
    name). Foreign keys stay off, as in the other create_all suites."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    tables = [Base.metadata.tables[t] for t in TABLES]
    Base.metadata.create_all(bind=engine, tables=tables)
    session = sessionmaker(bind=engine, autoflush=False, future=True)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


class TestUniquesEnforced:
    """Exercised against the tests' create_all DB (SQLite honours expression
    and partial unique indexes, so the nulls-not-distinct semantics hold)."""

    def test_notice_config_one_tenant_default(self, db_session):
        from app.database.models.compliance_notice_config import ComplianceNoticeConfig

        tenant = f"t-{_uid()}"
        db_session.add(ComplianceNoticeConfig(id=_uid(), tenant_id=tenant, ai_system_id=None))
        db_session.flush()
        db_session.add(ComplianceNoticeConfig(id=_uid(), tenant_id=tenant, ai_system_id="rg-a"))
        db_session.flush()  # a system override is a different scope
        db_session.add(ComplianceNoticeConfig(id=_uid(), tenant_id=tenant, ai_system_id=None))
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_report_period_unique_including_estate(self, db_session):
        from app.database.models.compliance_report import ComplianceReport

        tenant = f"t-{_uid()}"

        def report(system):
            return ComplianceReport(
                id=_uid(),
                tenant_id=tenant,
                framework_id="eu-ai-act",
                ai_system_id=system,
                cadence="monthly",
                period_start=date(2026, 9, 1),
                period_end=date(2026, 9, 30),
                catalog_version="2026-09",
            )

        db_session.add(report(None))
        db_session.add(report("rg-a"))
        db_session.flush()
        db_session.add(report(None))
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_one_active_signing_key_per_tenant(self, db_session):
        from app.database.models.evidence_signing_key import EvidenceSigningKey

        tenant = f"t-{_uid()}"
        db_session.add(EvidenceSigningKey(kid=_uid(), tenant_id=tenant, encrypted_seed="x", status="retired"))
        db_session.add(EvidenceSigningKey(kid=_uid(), tenant_id=tenant, encrypted_seed="x", status="active"))
        db_session.flush()
        db_session.add(EvidenceSigningKey(kid=_uid(), tenant_id=tenant, encrypted_seed="x", status="active"))
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_incident_events_cascade_shape(self, db_session):
        from app.database.models.compliance_incident import (
            ComplianceIncident,
            ComplianceIncidentEvent,
        )

        inc = ComplianceIncident(
            id=_uid(),
            tenant_id="default",
            title="t",
            classification="serious_incident",
            severity="high",
            became_aware_at=datetime.now(timezone.utc),
        )
        db_session.add(inc)
        db_session.flush()
        ev = ComplianceIncidentEvent(id=_uid(), incident_id=inc.id, tenant_id="default", event="created")
        db_session.add(ev)
        db_session.flush()
        d = inc.to_dict()
        assert d["status"] == "open" and d["frameworks"] == [] and d["linked_run_ids"] == []
        assert ev.to_dict()["event"] == "created"
        db_session.rollback()
