# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# a203_declared_outcomes — declared outcomes: the migration

Static checks run everywhere: the revision chain, the literal starter
catalogue against the app's, the idempotence guards, and that the ORM
declares the shape the migration builds.

The live round trip needs Postgres (`BRUTOR_PG_TESTS=1`, a server at
`BRUTOR_PG_TEST_URL` — never the live `ai_gateway`): a fresh install
(`init_database`, create_all + stamp head) is downgraded to a202_health_recent_runs, seeded
with the OLD shapes (a `blocked_policy` run, a `blocked` segment, a run
with a `client_outcome`, an AI System without a catalogue), upgraded —
checking every data conversion — then downgraded and re-upgraded to prove
the DDL reverses and the upgrade is idempotent.
"""

from __future__ import annotations





class TestShape:


    def test_orm_matches(self):
        from app.database.models.ai_system_dependency import AISystemDependency
        from app.database.models.ai_system_drift import AISystemBaseline
        from app.database.models.ai_system_drift_resolution import AISystemBaselineEpoch
        from app.database.models.ai_system_liveness import AISystemLivenessExpectation
        from app.database.models.ai_system_run import AISystemRun
        from app.database.models.ai_system_run_segment import AISystemRunSegment
        from app.database.models.proxy_log import ProxyLog
        from app.database.models.resource_group import ResourceGroup

        runs = AISystemRun.__table__.columns
        assert "client_outcome" not in runs
        for name, length in (("outcome_id", 128), ("outcome_conformance", 16), ("error_cause", 32)):
            assert runs[name].type.length == length and runs[name].nullable
        assert "outcome_counts_toward_baseline" in runs and "outcome_contract_version" in runs
        seg = AISystemRunSegment.__table__
        assert {"outcome_id", "outcome_conformance", "error_cause"} <= set(seg.columns.keys())
        check = next(c for c in seg.constraints if getattr(c, "name", "") == "ck_ai_system_run_segments_outcome")
        assert "blocked" not in str(check.sqltext)
        assert ProxyLog.__table__.columns["outcome_id"].type.length == 128
        assert "declared_outcomes" in ResourceGroup.__table__.columns
        assert "expected_outcomes" in AISystemDependency.__table__.columns
        assert "max_quiet_processed_seconds" in AISystemLivenessExpectation.__table__.columns
        epochs = AISystemBaselineEpoch.__table__
        assert epochs.columns["bucket"].server_default.arg == "all"
        key = next(c for c in epochs.constraints if getattr(c, "name", "") == "uq_ai_system_baseline_epochs_key")
        assert "bucket" in [c.name for c in key.columns]
        kind = next(c for c in epochs.constraints if getattr(c, "name", "") == "ck_ai_system_baseline_epochs_kind")
        assert "catalogue_change" in str(kind.sqltext)
        assert AISystemBaseline.__table__.columns["bucket"].type.length == 128


# ---------------------------------------------------------------------------
# Live round trip (Postgres)
# ---------------------------------------------------------------------------
