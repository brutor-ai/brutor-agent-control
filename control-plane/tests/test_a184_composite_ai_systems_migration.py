# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# a184_composite_ai_systems — migration drift suite

Fresh installs build these tables from the ORM (`create_all` + stamp);
upgrades build them from a184_composite_ai_systems. Both deployment shapes must describe the same
tables, and `ai_system_run_segments` is written by the Rust core, whose
INSERT names every column — a column in one shape and not the other fails at
finalisation, in a background task.
"""

from __future__ import annotations



from app.database.models.ai_system_run_segment import AISystemRunSegment


#: Later revisions that ADD columns to the a184_composite_ai_systems tables. The upgrade path is
#: the chain, so parity is against the chain (a186_composite_compliance: supplier_legal_entity,
#: separability_record_id, separability_sealed_at; a187_agent_implementation:
#: the segment's agent_impl_*; a189_segment_delegate_agent: the segment's
#: delegate_agent_id).


def test_segment_types_match_the_run_ledger():
    """Segments partition runs (conservation): the join and sum columns must
    have the run's own types, or a sum overflows or a join casts."""
    from app.database.models.ai_system_run import AISystemRun

    seg, run = AISystemRunSegment.__table__.c, AISystemRun.__table__.c
    assert str(seg.cost_usd.type) == str(run.total_cost_usd.type)
    assert str(seg.root_task_id.type) == str(run.root_task_id.type)
    assert str(seg.run_id.type) == str(run.id.type)
    # sha256:<64 hex> is 71 chars — the a148_run_contract_hash lesson.
    assert seg.contract_hash.type.length >= 71
