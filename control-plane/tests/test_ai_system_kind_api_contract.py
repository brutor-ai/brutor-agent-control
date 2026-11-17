# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# system_kind API contract

Covers the request/response contract for the AI System kind:

  * `ResourceGroupCreate` defaults an AI System's kind to `agent` and
    rejects a kind on any other group type.
  * `ResourceGroupUpdate` rejects the one combination it can see as wrong
    on its own, and stays permissive about the combinations only the
    stored row can adjudicate (PATCH omission means "leave unchanged").
  * `ResourceGroupResponse` and `GroupHierarchyTree` *declare* the field —
    Pydantic strips undeclared ones, which is how a value can survive
    `to_dict()` and still vanish from the GET.
  * The removed ResourceGroupType members are gone from the API enum too, not
    just the DB model.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.v1.models.admin.resource_group import (
    GroupHierarchyTree,
    ResourceGroupType,
    ResourceGroupCreate,
    ResourceGroupResponse,
    ResourceGroupUpdate,
    SystemKind,
)


class TestApiEnums:
    @pytest.mark.parametrize("removed", ["APPLICATION", "AGENT", "ASSISTANT", "SERVICE"])
    def test_legacy_group_types_removed_from_api_enum(self, removed: str):
        # The DB model and the API model are separate enums; removing one
        # without the other leaves the surface accepting values the
        # database can no longer store.
        assert not hasattr(ResourceGroupType, removed)

    def test_system_kind_values(self):
        assert {k.value for k in SystemKind} == {
            "agent",
            "assistant",
            "application",
            "integration",
            "workflow",
            "service",
        }


class TestCreateContract:
    def test_ai_system_without_kind_defaults_to_agent(self):
        model = ResourceGroupCreate(name="Refund Agent", group_type="ai_system")
        assert model.system_kind == SystemKind.AGENT

    def test_ai_system_honours_an_explicit_kind(self):
        model = ResourceGroupCreate(
            name="Nightly Sync", group_type="ai_system", system_kind="integration"
        )
        assert model.system_kind == SystemKind.INTEGRATION

    def test_non_ai_system_with_kind_is_rejected(self):
        with pytest.raises(ValidationError) as exc:
            ResourceGroupCreate(name="Team A", group_type="team", system_kind="agent")
        assert "only valid when group_type is 'ai_system'" in str(exc.value)

    def test_non_ai_system_without_kind_is_fine(self):
        model = ResourceGroupCreate(name="Team A", group_type="team")
        assert model.system_kind is None

    def test_default_group_type_does_not_acquire_a_kind(self):
        # group_type defaults to CUSTOM; the kind must not tag along.
        model = ResourceGroupCreate(name="Anything")
        assert model.group_type == ResourceGroupType.CUSTOM
        assert model.system_kind is None

    def test_unknown_kind_is_rejected(self):
        with pytest.raises(ValidationError):
            ResourceGroupCreate(
                name="X", group_type="ai_system", system_kind="not-a-kind"
            )


class TestUpdateContract:
    def test_rejects_kind_alongside_a_non_ai_system_type(self):
        with pytest.raises(ValidationError) as exc:
            ResourceGroupUpdate(group_type="team", system_kind="agent")
        assert "only valid when group_type is 'ai_system'" in str(exc.value)

    def test_allows_a_kind_only_patch(self):
        """Valid iff the stored row is already an AI System.

        The model cannot know that, so it stays permissive and the
        endpoint re-checks the merged state against the row.
        """
        model = ResourceGroupUpdate(system_kind="workflow")
        assert model.system_kind == SystemKind.WORKFLOW
        assert "group_type" not in model.model_fields_set

    def test_allows_promoting_to_ai_system_with_a_kind(self):
        model = ResourceGroupUpdate(group_type="ai_system", system_kind="service")
        assert model.system_kind == SystemKind.SERVICE

    def test_allows_promoting_without_a_kind(self):
        # The endpoint defaults it, matching create and the flush hook.
        model = ResourceGroupUpdate(group_type="ai_system")
        assert model.system_kind is None

    def test_explicit_null_is_distinguishable_from_omission(self):
        # The endpoint keys off model_fields_set to tell "clear it" from
        # "leave it alone"; both look like None on the attribute.
        cleared = ResourceGroupUpdate(system_kind=None)
        omitted = ResourceGroupUpdate()
        assert "system_kind" in cleared.model_fields_set
        assert "system_kind" not in omitted.model_fields_set


class TestResponseContract:
    def test_response_declares_system_kind(self):
        assert "system_kind" in ResourceGroupResponse.model_fields

    def test_response_survives_round_trip(self):
        payload = {
            "id": "group-1",
            "name": "refund",
            "display_name": None,
            "description": None,
            "group_type": "ai_system",
            "system_kind": "agent",
            "hierarchy_level": 0,
            "hierarchy_path": None,
            "inherit_resources": True,
            "icon": None,
            "color": None,
            "is_active": True,
            "created_by": None,
            "created_at": "2026-07-27T00:00:00Z",
            "updated_at": "2026-07-27T00:00:00Z",
        }
        model = ResourceGroupResponse(**payload)
        assert model.system_kind == SystemKind.AGENT
        assert model.model_dump()["system_kind"] == SystemKind.AGENT

    def test_response_allows_absent_kind(self):
        payload = {
            "id": "group-2",
            "name": "team",
            "display_name": None,
            "description": None,
            "group_type": "team",
            "hierarchy_level": 0,
            "hierarchy_path": None,
            "inherit_resources": True,
            "icon": None,
            "color": None,
            "is_active": True,
            "created_by": None,
            "created_at": "2026-07-27T00:00:00Z",
            "updated_at": "2026-07-27T00:00:00Z",
        }
        assert ResourceGroupResponse(**payload).system_kind is None

    def test_hierarchy_tree_declares_system_kind(self):
        # Mission Control and the fleet board flatten this tree hunting for
        # AI Systems; without the field they would need a fetch per node.
        assert "system_kind" in GroupHierarchyTree.model_fields

    def test_hierarchy_tree_round_trip(self):
        node = GroupHierarchyTree(
            id="group-1",
            name="refund",
            display_name=None,
            group_type="ai_system",
            system_kind="assistant",
            hierarchy_level=0,
            is_active=True,
            parent_group_id=None,
            inherit_resources=True,
            member_count=0,
            server_config_count=0,
            llm_model_count=0,
            access_key_count=0,
        )
        assert node.system_kind == SystemKind.ASSISTANT
