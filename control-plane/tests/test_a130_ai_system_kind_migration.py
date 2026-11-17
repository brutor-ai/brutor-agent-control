# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# a130_ai_system_kind — migration + model drift suite

Two things are pinned here.

**The migration file shape** — revision chain, the `system_kind` column,
the legacy-row conversion, the enum rebuild, and the ordering constraint
that makes the whole thing work (data conversion *before* the enum
shrinks, or the UPDATE would write values the new type rejects).

**The model invariants** — GroupType no longer carries the four AI member
types, `system_kind` validates its membership, and the group_type/kind
coupling holds however the object was built.

Mirrors the textual/regex style of the other `test_a*_migration.py`
suites: these are drift guards, not a live-DB run.
"""

from __future__ import annotations


import pytest

from app.database.models.resource_group import (
    DEFAULT_SYSTEM_KIND,
    LEGACY_GROUP_TYPE_TO_SYSTEM_KIND,
    SYSTEM_KINDS,
    GroupType,
    ResourceGroup,
)


# ---------------------------------------------------------------------------
# File + chain shape
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Legacy conversion
# ---------------------------------------------------------------------------


class TestLegacyConversion:

    def test_legacy_map_covers_exactly_the_removed_members(self):
        assert set(LEGACY_GROUP_TYPE_TO_SYSTEM_KIND) == {
            "APPLICATION",
            "AGENT",
            "ASSISTANT",
            "SERVICE",
        }

    def test_every_legacy_target_is_a_valid_kind(self):
        for kind in LEGACY_GROUP_TYPE_TO_SYSTEM_KIND.values():
            assert kind in SYSTEM_KINDS


# ---------------------------------------------------------------------------
# Enum rebuild
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Model invariants
# ---------------------------------------------------------------------------


class TestGroupTypeCleanup:
    @pytest.mark.parametrize("removed", ["APPLICATION", "AGENT", "ASSISTANT", "SERVICE"])
    def test_legacy_member_is_gone(self, removed: str):
        assert not hasattr(GroupType, removed), (
            f"GroupType.{removed} is back — it was removed so the "
            f"platform has exactly one way to say 'this is an agent'"
        )

    def test_ai_system_and_custom_survive(self):
        assert GroupType.AI_SYSTEM.value == "ai_system"
        assert GroupType.CUSTOM.value == "custom"


class TestSystemKindModel:
    def test_kind_values_are_pinned(self):
        assert SYSTEM_KINDS == {
            "agent",
            "assistant",
            "application",
            "integration",
            "workflow",
            "service",
        }

    def test_default_kind_is_a_member(self):
        assert DEFAULT_SYSTEM_KIND in SYSTEM_KINDS

    def test_validator_rejects_a_non_member(self):
        with pytest.raises(ValueError, match="system_kind must be one of"):
            ResourceGroup(id="g", name="g", system_kind="nonsense")

    def test_validator_allows_none(self):
        g = ResourceGroup(id="g", name="g", system_kind=None)
        assert g.system_kind is None

    def test_coupling_accepts_ai_system_with_a_kind(self):
        g = ResourceGroup(
            id="g", name="g", group_type=GroupType.AI_SYSTEM, system_kind="agent"
        )
        assert g.validate_system_kind() == (True, None)

    def test_coupling_rejects_kind_without_ai_system(self):
        g = ResourceGroup(
            id="g", name="g", group_type=GroupType.TEAM, system_kind="agent"
        )
        ok, err = g.validate_system_kind()
        assert not ok
        assert "only valid when group_type is 'ai_system'" in err

    def test_coupling_rejects_ai_system_without_a_kind(self):
        g = ResourceGroup(id="g", name="g", group_type=GroupType.AI_SYSTEM)
        ok, err = g.validate_system_kind()
        assert not ok
        assert "system_kind is required" in err

    def test_kwarg_order_does_not_affect_validation(self):
        """SQLAlchemy fires column validators in assignment order.

        A validator that read `self.group_type` would reject this purely
        because the kwargs happen to be ordered kind-first, which is why
        the coupling check lives outside the column validator.
        """
        g = ResourceGroup(
            id="g", name="g", system_kind="workflow", group_type=GroupType.AI_SYSTEM
        )
        assert g.validate_system_kind() == (True, None)


class TestToDictContract:
    def test_system_kind_is_serialised(self):
        # Pydantic strips undeclared fields, so a value missing from
        # to_dict() silently disappears from every GET response.
        g = ResourceGroup(
            id="g",
            name="g",
            group_type=GroupType.AI_SYSTEM,
            system_kind="integration",
            hierarchy_level=0,
            inherit_resources=True,
            is_active=True,
            is_system=False,
        )
        assert g.to_dict()["system_kind"] == "integration"

    def test_system_kind_is_null_for_plain_groups(self):
        g = ResourceGroup(
            id="g",
            name="g",
            group_type=GroupType.TEAM,
            hierarchy_level=0,
            inherit_resources=True,
            is_active=True,
            is_system=False,
        )
        assert g.to_dict()["system_kind"] is None
