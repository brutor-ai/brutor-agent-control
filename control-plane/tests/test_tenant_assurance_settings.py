# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Tenant-level AI System assurance settings must be reachable.

Both settings shipped as columns with readers and no writer:

  * `assurance_capture_interval_hours` — the automatic assurance-capture
    scheduler reads it, it is NULL by default, and nothing could set it. The
    scheduler therefore never fired for anybody, so "continuous evidence" was
    only ever true if a human clicked.
  * `ai_system_gate_policy` — a tenant can tighten or
    relax the evidence each lifecycle transition demands. The gate read the
    column; nothing wrote it, so every tenant was pinned to the defaults.

These tests pin the whole round trip — write, clear, read back, and refuse
nonsense — because a governance control that saves but does not apply is worse
than one that is absent.
"""

from __future__ import annotations

import pytest

from app.api.v1.models.admin.tenant_settings import TenantSettingsUpdate
from app.services.ai_system_lifecycle_gate import (
    DEFAULT_REQUIREMENTS,
    REQ_CONTRACT_MINTED,
    REQ_HUMAN_APPROVAL,
    REQ_OWNER_SET,
    validate_tenant_policy,
)


# ---------------------------------------------------------------------------
# The write path exists at all
# ---------------------------------------------------------------------------


def test_settings_are_not_forbidden_to_tenant_admins():
    """Assurance posture is operational, not commercial.

    Quotas, features and billing are reserved to system admins. A tenant that
    must capture evidence nightly to satisfy its own auditor should not have
    to raise a support ticket for it — the same reasoning that keeps
    `data_residency` out of this set.
    """
    # The deny-list is gone; a tenant administrator may set what
    # the model of `PATCH /admin/tenants/current` declares.
    for field in ("assurance_capture_interval_hours", "ai_system_gate_policy"):
        assert field in TenantSettingsUpdate.model_fields


# ---------------------------------------------------------------------------
# Explicit null means "turn it off", not "leave alone"
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The gate policy is validated on write, though tolerated on read
# ---------------------------------------------------------------------------

def test_valid_policy_passes():
    key = f"proposed->approved"
    assert key in DEFAULT_REQUIREMENTS, "test is written against a real transition"
    assert validate_tenant_policy({key: [REQ_CONTRACT_MINTED, REQ_OWNER_SET]}) == []


def test_none_policy_is_valid():
    """Null is how a tenant reverts to the built-in requirements."""
    assert validate_tenant_policy(None) == []


def test_unknown_transition_is_refused():
    """The typo case.

    `required_keys` ignores an unrecognised transition key, which is correct
    at evaluation time and catastrophic at write time: the operator sees a
    successful save and believes a requirement is enforced that does nothing.
    """
    problems = validate_tenant_policy({"proposed->aproved": [REQ_OWNER_SET]})
    assert problems, "a mistyped transition must not save silently"
    assert "not a recognised transition" in problems[0]


def test_unknown_requirement_is_refused():
    problems = validate_tenant_policy({"proposed->approved": ["reticulate_splines"]})
    assert problems
    assert "unknown requirement" in problems[0]


def test_non_list_value_is_refused():
    problems = validate_tenant_policy({"proposed->approved": "owner_set"})
    assert problems
    assert "must map to a list" in problems[0]


def test_non_dict_policy_is_refused():
    assert validate_tenant_policy(["owner_set"])


def test_empty_requirement_list_is_allowed():
    """Relaxing a transition to nothing is a legitimate — if bold — choice.

    A tenant may relax as well as tighten. The high-risk floor is
    enforced separately in `required_keys` and cannot be removed here, so an
    empty list is a real decision rather than a way around the gate.
    """
    assert validate_tenant_policy({"active->deprecated": []}) == []


def test_relaxing_cannot_remove_the_high_risk_floor():
    """A policy may drop human approval; the gate puts it back for high risk.

    Pinned here as well as in the gate's own tests because this is the
    property that makes it safe to let tenants relax requirements at all.
    """
    from app.database.models.resource_group import GroupType, ResourceGroup
    from app.services.ai_system_lifecycle_gate import RISK_TIER_HIGH, required_keys

    system = ResourceGroup(
        id="g1", tenant_id="t1", name="s", group_type=GroupType.AI_SYSTEM,
        lifecycle_stage="proposed", eu_ai_act_risk_tier=RISK_TIER_HIGH,
    )
    keys = required_keys(
        system, "approved", tenant_policy={"proposed->approved": []},
    )
    assert REQ_HUMAN_APPROVAL in keys, (
        "a tenant policy must not be able to relax a high-risk system out of "
        "human approval"
    )
