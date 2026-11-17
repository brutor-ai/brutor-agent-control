# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# End-user groups — Pydantic request/response model tests

Covers:
  1. EndUserGroupCreate — name strip + whitespace-only rejection.
  2. EndUserGroupUpdate — partial update; name validator runs only
     when name is supplied.
  3. {Add,Remove}MembersRequest — min_length=1 enforced (a 0-item
     payload is almost certainly a UI bug).
  4. SetMembersRequest — empty list IS allowed (intentional roster
     wipe).
  5. ResourceGroupBindEndUserGroupRequest / Unbind — min_length=1.
  6. The identity provider models carry no SAML audience field.

These are pure Pydantic tests — no DB, no FastAPI. Fast (<1s total).
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from pydantic import ValidationError

from app.api.v1.models.admin.end_user_group import (
    AddMembersRequest,
    EndUserGroupCreate,
    EndUserGroupDetail,
    EndUserGroupMember,
    EndUserGroupSummary,
    EndUserGroupUpdate,
    RemoveMembersRequest,
    ResourceGroupBindEndUserGroupRequest,
    ResourceGroupUnbindEndUserGroupRequest,
    SetMembersRequest,
)
from app.api.v1.models.admin.idp_server import (
    IdPConfigRequest,
    IdPConfigUpdateRequest,
)


# ---------------------------------------------------------------------------
# EndUserGroupCreate
# ---------------------------------------------------------------------------


class TestEndUserGroupCreate:
    def test_minimum_payload(self):
        m = EndUserGroupCreate(name="engineering")
        assert m.name == "engineering"
        assert m.display_name is None
        assert m.initial_member_ids == []

    def test_name_stripped(self):
        m = EndUserGroupCreate(name="  engineering  ")
        assert m.name == "engineering"

    def test_name_whitespace_only_rejected(self):
        with pytest.raises(ValidationError, match="name must not be empty"):
            EndUserGroupCreate(name="   ")

    def test_name_too_long_rejected(self):
        with pytest.raises(ValidationError):
            EndUserGroupCreate(name="x" * 300)

    def test_initial_member_ids_int_list(self):
        m = EndUserGroupCreate(name="x", initial_member_ids=[1, 2, 3])
        assert m.initial_member_ids == [1, 2, 3]

    def test_source_fields_not_accepted(self):
        # The API surface intentionally hides source/source_external_id
        # from operators. Pydantic's
        # default is "ignore unknown fields", so extra kwargs silently
        # vanish; assert they don't leak onto the model.
        m = EndUserGroupCreate(
            name="x",
            source="other",               # type: ignore[arg-type]
            source_external_id="okta-x",  # type: ignore[arg-type]
        )
        assert not hasattr(m, "source")
        assert not hasattr(m, "source_external_id")


# ---------------------------------------------------------------------------
# EndUserGroupUpdate
# ---------------------------------------------------------------------------


class TestEndUserGroupUpdate:
    def test_all_fields_optional(self):
        m = EndUserGroupUpdate()
        assert m.name is None
        assert m.display_name is None
        assert m.description is None

    def test_name_validator_runs_only_when_supplied(self):
        # None is fine.
        EndUserGroupUpdate(name=None)
        # Empty / whitespace is not.
        with pytest.raises(ValidationError, match="name must not be empty"):
            EndUserGroupUpdate(name="   ")

    def test_partial_update_just_description(self):
        m = EndUserGroupUpdate(description="new wording")
        assert m.name is None
        assert m.description == "new wording"


# ---------------------------------------------------------------------------
# Membership mutation
# ---------------------------------------------------------------------------


class TestMembershipRequests:
    def test_add_members_requires_at_least_one(self):
        with pytest.raises(ValidationError):
            AddMembersRequest(end_user_ids=[])

    def test_add_members_happy_path(self):
        m = AddMembersRequest(end_user_ids=[7, 8])
        assert m.end_user_ids == [7, 8]

    def test_remove_members_requires_at_least_one(self):
        with pytest.raises(ValidationError):
            RemoveMembersRequest(end_user_ids=[])

    def test_set_members_allows_empty_list(self):
        # Set is the "authoritative replace" path — clearing a roster
        # is a legitimate operation when re-seeding from a new IdP.
        m = SetMembersRequest(end_user_ids=[])
        assert m.end_user_ids == []

    def test_set_members_with_roster(self):
        m = SetMembersRequest(end_user_ids=[1, 2, 3])
        assert m.end_user_ids == [1, 2, 3]


# ---------------------------------------------------------------------------
# RG ↔ EndUserGroup binding requests
# ---------------------------------------------------------------------------


class TestRgBindingRequests:
    def test_bind_requires_at_least_one(self):
        with pytest.raises(ValidationError):
            ResourceGroupBindEndUserGroupRequest(end_user_group_ids=[])

    def test_bind_happy_path(self):
        m = ResourceGroupBindEndUserGroupRequest(
            end_user_group_ids=["eug-1", "eug-2"]
        )
        assert m.end_user_group_ids == ["eug-1", "eug-2"]

    def test_unbind_requires_at_least_one(self):
        with pytest.raises(ValidationError):
            ResourceGroupUnbindEndUserGroupRequest(end_user_group_ids=[])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class TestResponseShapes:
    def test_summary_round_trips(self):
        s = EndUserGroupSummary(
            id="eug-1",
            tenant_id="t1",
            name="eng",
            source="manual",
            member_count=0,
        )
        d = s.model_dump()
        assert d["id"] == "eug-1"
        assert d["source"] == "manual"
        # Sanity: members/resource_groups are NOT on the summary model.
        assert "members" not in d
        assert "resource_groups" not in d

    def test_detail_includes_members_and_rgs(self):
        d = EndUserGroupDetail(
            id="eug-1",
            tenant_id="t1",
            name="eng",
            source="manual",
            member_count=2,
            members=[
                EndUserGroupMember(id=1, username="alice"),
                EndUserGroupMember(id=2, username="bob"),
            ],
        )
        dump = d.model_dump()
        assert len(dump["members"]) == 2
        assert dump["resource_groups"] == []


# ---------------------------------------------------------------------------
# The identity provider models carry no SAML audience (SAML single sign-on is
# not in this release).
# ---------------------------------------------------------------------------


def test_the_idp_models_have_no_target_user_type():
    assert "target_user_type" not in IdPConfigRequest.model_fields
    assert "target_user_type" not in IdPConfigUpdateRequest.model_fields
