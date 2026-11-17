# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# a192_drift_resolution — migration drift suite

Fresh installs build `ai_system_baseline_epochs` / `ai_system_incident_windows`
and `ai_system_drift_events.resolution_kind` from the ORM (`create_all` +
stamp); upgrades from a192_drift_resolution. The Rust core READS both tables in its baseline
and drift sweeps (and its test fixture is a dump of a migrated database), so
a column, default or constraint present in one shape and not the other is a
failure in a background sweep, not at deploy time.
"""

from __future__ import annotations

import re

import pytest

from app.constants.drift_resolution import (
    EPOCH_KINDS,
    INCIDENT_CAUSE_CATEGORIES,
    INCIDENT_RESOURCE_TYPES,
    RESOLUTION_KINDS,
)
from app.database.models.ai_system_drift import AISystemDriftEvent
from app.database.models.ai_system_drift_resolution import (
    WINDOW_LINK_ROLES,
    AISystemBaselineEpoch,
    AISystemIncidentWindow,
    AISystemIncidentWindowFinding,
)

#: a193_approval_window_epochs amended `ai_system_baseline_epochs` (kind `measurement_change`, the
#: `measurement_change_id` column); the ORM is the a193_approval_window_epochs shape, so parity is
#: checked against the latest migration that defines each piece.
#: a203_declared_outcomes amended it again: the `bucket` column (in the key) and the
#: kind `catalogue_change`.


def _check_sql(model, name) -> str:
    (ck,) = [c for c in model.__table__.constraints if getattr(c, "name", None) == name]
    return re.sub(r"\s+", " ", str(ck.sqltext))


def test_check_vocabularies_are_the_wire_constants():
    def values(model, name):
        return set(re.findall(r"'([^']+)'", _check_sql(model, name)))

    assert values(AISystemDriftEvent, "ck_ai_system_drift_events_resolution_kind") == set(
        RESOLUTION_KINDS
    )
    assert values(AISystemBaselineEpoch, "ck_ai_system_baseline_epochs_kind") == set(EPOCH_KINDS)
    assert values(AISystemIncidentWindow, "ck_ai_system_incident_windows_cause") == set(
        INCIDENT_CAUSE_CATEGORIES
    )
    assert values(
        AISystemIncidentWindow, "ck_ai_system_incident_windows_resource_type"
    ) == set(INCIDENT_RESOURCE_TYPES)
    assert values(
        AISystemIncidentWindowFinding, "ck_ai_system_incident_window_findings_role"
    ) == set(WINDOW_LINK_ROLES) == {"declared", "attached"}


def test_a_finding_links_to_a_window_once():
    assert [c.name for c in AISystemIncidentWindowFinding.__table__.primary_key.columns] == [
        "incident_window_id", "drift_event_id",
    ]


def test_epoch_key_is_the_lookup_key():
    """One epoch per (system, metric, basis, caller, bucket, start): a second
    resolution at the same change point is the same fact, not a new one.
    (a203_declared_outcomes added `bucket` — per-outcome baselines.)"""
    (uq,) = [
        c for c in AISystemBaselineEpoch.__table__.constraints
        if c.name == "uq_ai_system_baseline_epochs_key"
    ]
    assert [c.name for c in uq.columns] == [
        "tenant_id", "ai_system_id", "metric_key", "basis", "caller_system_id", "bucket",
        "starts_at",
    ]


def test_validators():
    with pytest.raises(ValueError):
        AISystemDriftEvent(resolution_kind="dismissed")
    with pytest.raises(ValueError):
        AISystemBaselineEpoch(kind="cause_fixed")
    with pytest.raises(ValueError):
        AISystemIncidentWindow(cause_category="bad_luck")
    with pytest.raises(ValueError):
        AISystemIncidentWindow(resource_type="database")
    e = AISystemDriftEvent(id="drift-1", ai_system_id="rg", metric_key="x")
    assert e.to_dict()["resolution_kind"] is None
