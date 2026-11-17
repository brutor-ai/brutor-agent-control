# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# a185_service_baselines — migration drift suite

Fresh installs build `ai_system_baselines` / `ai_system_drift_events` from the
ORM (`create_all` + stamp); upgrades from a185_service_baselines. The Rust core upserts
baselines ON CONFLICT on the unique key, so a key that differs between the two
shapes fails in the core's background sweeper, not at deploy time.
"""

from __future__ import annotations


import pytest

from app.database.models.ai_system_drift import (
    BASELINE_ALL_CALLERS,
    BASES,
    BASIS_RUNS,
    AISystemBaseline,
    AISystemDriftEvent,
)


def test_basis_vocabulary_and_validators():
    assert BASES == {"runs", "segments"}
    assert BASIS_RUNS == "runs" and BASELINE_ALL_CALLERS == "all"
    with pytest.raises(ValueError):
        AISystemBaseline(basis="calls")
    with pytest.raises(ValueError):
        AISystemDriftEvent(basis="calls")


def test_to_dict_exposes_basis_and_caller():
    b = AISystemBaseline(
        id="base-1", ai_system_id="rg", metric_key="action_count",
        basis="segments", caller_system_id="rg-caller",
    )
    d = b.to_dict()
    assert d["basis"] == "segments" and d["caller_system_id"] == "rg-caller"
    e = AISystemDriftEvent(id="drift-1", ai_system_id="rg", metric_key="x", basis="segments")
    d = e.to_dict()
    assert d["basis"] == "segments" and d["caller_system_id"] is None
