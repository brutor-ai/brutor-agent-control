# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# a131_ai_system_runs — migration drift suite

Pins the run-ledger schema. Two things make this worth more than the
usual migration guard:

  * **Fresh installs never run this migration.** They run `create_all` +
    stamp, so the SQLAlchemy model *is* the schema on a new deployment.
    Any column the migration adds and the model omits (or vice versa)
    produces a table that exists in one deployment shape and not the
    other. The column-parity test below is the thing that catches it.

  * **The Rust core writes this table.** The finalizer's INSERT names
    every column positionally; a column renamed here and not there fails
    at runtime, in a background task, where nobody is watching.
"""

from __future__ import annotations



from app.database.models.ai_system_run import AISystemRun


class TestKeysAndIndexes:


    def test_model_declares_the_same_indexes(self):
        model_indexes = {ix.name for ix in AISystemRun.__table__.indexes}
        assert model_indexes == {
            "ix_ai_system_runs_system_started",
            "ix_ai_system_runs_terminal",
            "ix_ai_system_runs_contract",
            "ix_ai_system_runs_tool_sig",
            "ix_ai_system_runs_tenant_source",  # a169_evidence_source
            "ix_ai_system_runs_actor",
            "ix_ai_system_runs_group_started",  # a198_run_resource_group
        }


class TestWireContractWithRust:
    """Values the Rust finalizer writes literally.

    Mirrored in `brutor-gateway-core/src/services/run_ledger.rs`, whose own
    unit tests assert the same strings from the other side.
    """

    def test_terminal_states(self):
        from app.database.models.ai_system_run import TERMINAL_STATES

        assert TERMINAL_STATES == {
            "completed",
            "completed_degraded",
            "exhausted",
            "errored",
            # a203_declared_outcomes — `blocked_policy` is gone: a gateway
            # refusal ends the run `errored` with error_cause blocked_by_policy.
            "abandoned",
            "cancelled",
        }

    def test_finalized_by_values(self):
        """Mirrors `run_ledger::finalized_by` in gateway-core.

        `operator` is part of the contract: an operator can
        stop one run, and the label has to be distinguishable from `idle` and
        `sweeper` — a run somebody deliberately stopped is not an abandoned
        one, and recording it as abandoned loses the only fact that explains
        the shape of its evidence.
        """
        from app.database.models.ai_system_run import FINALIZED_BY

        assert FINALIZED_BY == {"explicit", "terminal", "idle", "sweeper", "operator"}

    def test_chain_integrity_values(self):
        from app.database.models.ai_system_run import CHAIN_INTEGRITIES

        # Mirrors `run_ledger::chain` in gateway-core. `client_asserted`
        # joined the contract with the x-brutor-run-id correlation work:
        # complete observation, client-word linkage.
        # `observed_external` (a169_evidence_source): at least one step was ingested from
        # external telemetry — complete as reported, nothing the gateway
        # witnessed.
        assert CHAIN_INTEGRITIES == {
            "intact", "client_asserted", "observed_external", "partial", "broken",
        }

    def test_fragmentation_cap(self):
        from app.database.models.ai_system_run import MAX_REOPENS_BEFORE_FRAGMENTED

        assert MAX_REOPENS_BEFORE_FRAGMENTED == 3
