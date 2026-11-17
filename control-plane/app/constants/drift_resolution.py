# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Drift resolution — the wire contract

A human resolution is a fact the detector must honour. The control plane
WRITES resolution kinds, baseline epochs and incident windows; the Rust core
READS the epochs and windows in its baseline and drift sweeps, and both
planes apply the SAME exclusion rule to the samples they grade.

WIRE CONTRACT: every literal here is mirrored in
`brutor-gateway-core/src/constants.rs` and pinned in BOTH repos (Python:
`tests/test_wire_contract.py`). A rename is a deliberate two-repo edit.

## The exclusion rule (one predicate per plane)

A sample is EXCLUDED from judgement when

  1. its timestamp is inside an incident window — ``starts_at <= ts <
     ends_at`` (half-open) — whose ``ai_system_id`` is NULL (tenant-wide) or
     one of the sample's systems; or
  2. (for a metric with a key) its timestamp is before the latest baseline
     epoch for its (system, metric, basis) whose ``caller_system_id`` is the
     baseline's caller key or ``'all'`` — ``ts < starts_at``. A per-caller
     baseline honours its caller's epochs and the aggregate's; the aggregate
     baseline (caller ``'all'``) honours only ``'all'`` epochs.

The sample's timestamp and systems:

  * a run — ``ai_system_runs.started_at``; its system ``ai_system_id``;
  * a run segment — ``ai_system_run_segments.first_action_at``; its systems
    ``segment_system_id`` (the service) and ``root_system_id`` (the run it
    belongs to — a window on the root covers the whole run);
  * a proxy_logs row read through its run — the run's ``started_at`` and
    ``ai_system_id`` (a run is excluded whole, never half).

Windows excuse PERFORMANCE, never FACTS: the rule
applies to rate / ratio judgements only (baselines, drift, envelope terms,
health rate components, service signals, the down rule, connection lines,
boundary error rates). Declared-vs-observed facts see every sample.

Excluded samples are never deleted or hidden: they stay recorded, sealed and
exportable, and the assurance report states how many were excluded.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Resolution kinds — ai_system_drift_events.resolution_kind
# ---------------------------------------------------------------------------

#: The shift is intended — the new behaviour is the new normal.
KIND_ACCEPTED_CHANGE = "accepted_change"
#: Something was wrong and has been fixed.
KIND_CAUSE_FIXED = "cause_fixed"
#: The detector was wrong.
KIND_FALSE_POSITIVE = "false_positive"
#: Closed by the control plane because the condition cleared on its own
#: (envelope term back within bound, dependency healthy again, agent check
#: no longer finds it, a superseded t_class finding). No effect on detection.
KIND_AUTO_CLEARED = "auto_cleared"

RESOLUTION_KINDS = (
    KIND_ACCEPTED_CHANGE,
    KIND_CAUSE_FIXED,
    KIND_FALSE_POSITIVE,
    KIND_AUTO_CLEARED,
)
#: The kinds a human may choose when closing a finding.
HUMAN_RESOLUTION_KINDS = (KIND_ACCEPTED_CHANGE, KIND_CAUSE_FIXED, KIND_FALSE_POSITIVE)

#: A platform upgrade changed how a metric is MEASURED. Never a
#: resolution kind — no human closes anything: the control plane writes the
#: epoch at startup from the versioned registry
#: (`app/constants/measurement_changes.py`), so the shift a new measurement
#: produces is never judged as the agent changing.
KIND_MEASUREMENT_CHANGE = "measurement_change"

#: A promoted contract changed what an outcome bucket
#: MEANS: a variant's `expect` or an outcome's `counts_toward_baseline`. The
#: control plane writes one epoch per changed bucket at promotion, so the
#: per-outcome baseline re-learns instead of judging the new meaning against
#: the old normal. Not a human declaration; the contract is the authority.
KIND_CATALOGUE_CHANGE = "catalogue_change"

#: The kinds that write a baseline epoch (`ai_system_baseline_epochs.kind`).
#: The core loads epochs whatever their kind (`drift::exclusion::Epochs`).
EPOCH_KINDS = (
    KIND_ACCEPTED_CHANGE, KIND_FALSE_POSITIVE, KIND_MEASUREMENT_CHANGE, KIND_CATALOGUE_CHANGE,
)

# ---------------------------------------------------------------------------
# Incident windows — ai_system_incident_windows
# ---------------------------------------------------------------------------

CAUSE_PROVIDER_OUTAGE = "provider_outage"
CAUSE_PLATFORM_INCIDENT = "platform_incident"
CAUSE_MAINTENANCE = "maintenance"
CAUSE_OTHER = "other"

INCIDENT_CAUSE_CATEGORIES = (
    CAUSE_PROVIDER_OUTAGE,
    CAUSE_PLATFORM_INCIDENT,
    CAUSE_MAINTENANCE,
    CAUSE_OTHER,
)

#: Display-only narrowing of a window (the exclusion is by system / tenant).
INCIDENT_RESOURCE_TYPES = (
    "model",
    "provider",
    "mcp_server",
    "skill",
    "knowledge_base",
    "agent",
)

#: An epoch whose caller is this covers every caller of its metric.
EPOCH_ALL_CALLERS = "all"
#: An epoch whose bucket is this covers every bucket of its metric
#: (the per-outcome baselines included); an epoch on a named bucket (a full
#: outcome id) covers only that bucket's baseline.
EPOCH_ALL_BUCKETS = "all"

#: The metric key an envelope term's epoch is recorded under — the same key
#: its `contract` drift events carry (`envelope:<term>`).
ENVELOPE_METRIC_PREFIX = "envelope:"

TABLE_BASELINE_EPOCHS = "ai_system_baseline_epochs"
TABLE_INCIDENT_WINDOWS = "ai_system_incident_windows"

__all__ = [
    "CAUSE_MAINTENANCE",
    "CAUSE_OTHER",
    "CAUSE_PLATFORM_INCIDENT",
    "CAUSE_PROVIDER_OUTAGE",
    "ENVELOPE_METRIC_PREFIX",
    "EPOCH_ALL_BUCKETS",
    "EPOCH_ALL_CALLERS",
    "EPOCH_KINDS",
    "HUMAN_RESOLUTION_KINDS",
    "INCIDENT_CAUSE_CATEGORIES",
    "INCIDENT_RESOURCE_TYPES",
    "KIND_ACCEPTED_CHANGE",
    "KIND_AUTO_CLEARED",
    "KIND_CATALOGUE_CHANGE",
    "KIND_CAUSE_FIXED",
    "KIND_FALSE_POSITIVE",
    "KIND_MEASUREMENT_CHANGE",
    "RESOLUTION_KINDS",
    "TABLE_BASELINE_EPOCHS",
    "TABLE_INCIDENT_WINDOWS",
]
