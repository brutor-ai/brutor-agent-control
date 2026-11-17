# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The effective and inherited limits the console shows are the ones the
gateway enforces.

The gateway merges a group's limits with its ancestors' through one merge
(`usage::restrictive_merge` in `brutor-gateway-core`): a level's `null` or
value of another type leaves the ancestor's limit, and a number that is not
positive is unset except where its family reads it as a limit (a media quota
of 0, an A2A delegation depth of 0). The Control Plane's merge let a child's
`null` or `0` replace the ancestor's limit, so the console showed a group
with no limit (or a limit of 0) where the gateway enforced the ancestor's.
`test_python_rust_merge_parity.py` holds the two merges together case by
case; these tests hold every view the console reads to them.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from ulid import ULID

from app.api.v1.endpoints.admin.resource_groups import (
    _compute_inherited_limits,
    _compute_inherited_media_governance,
    _compute_inherited_skill_governance,
)
from app.database.models.resource_group import ResourceGroup
from tests.test_a2a_resource_group_binding import _tenant, session  # noqa: F401  (fixture)


def _chain(s, *levels):
    """Groups root first, each holding the given columns; the last one."""
    _tenant(s)
    parent = None
    for i, columns in enumerate(levels):
        g = ResourceGroup(
            id=f"rg-{ULID()}",
            tenant_id="t1",
            name=f"g{i}",
            display_name=f"g{i}",
            parent_group_id=parent.id if parent else None,
            hierarchy_level=i,
            hierarchy_path="/".join(f"g{j}" for j in range(i + 1)),
            inherit_resources=True,
            is_active=True,
            **columns,
        )
        s.add(g)
        s.flush()
        parent = g
    s.commit()
    return parent


class TestEffectiveLimits:
    def test_a_childs_null_leaves_the_ancestors_llm_limit(self, session):
        child = _chain(
            session,
            {"llm_global_limits": {"tokens": {"daily_limit": 1000}, "budget": {"hard_stop": True}}},
            {"llm_global_limits": {"tokens": {"daily_limit": None}, "budget": {"hard_stop": None}}},
        )
        assert child.get_effective_llm_limits() == {
            "tokens": {"daily_limit": 1000}, "budget": {"hard_stop": True},
        }

    def test_a_childs_null_section_leaves_the_ancestors_mcp_limits(self, session):
        child = _chain(
            session,
            {"mcp_global_limits": {"frequency": {"max_calls_per_minute": 10}}},
            {"mcp_global_limits": {"frequency": None}},
        )
        assert child.get_effective_mcp_limits() == {"frequency": {"max_calls_per_minute": 10}}

    def test_a_childs_zero_is_no_llm_skill_or_a2a_quota(self, session):
        child = _chain(
            session,
            {"llm_global_limits": {"requests": {"daily_limit": 50}},
             "skill_global_limits": {"execution": {"daily_limit": 5}},
             "a2a_global_limits": {"invocations": {"daily_limit": 100}}},
            {"llm_global_limits": {"requests": {"daily_limit": 0}},
             "skill_global_limits": {"execution": {"daily_limit": 0}},
             "a2a_global_limits": {"invocations": {"daily_limit": 0}}},
        )
        assert child.get_effective_llm_limits() == {"requests": {"daily_limit": 50}}
        assert child.get_effective_skill_limits() == {"execution": {"daily_limit": 5}}
        assert child.get_effective_a2a_limits() == {"invocations": {"daily_limit": 100}}

    def test_a_zero_delegation_depth_and_a_zero_media_quota_are_kept(self, session):
        child = _chain(
            session,
            {"a2a_global_limits": {"chain": {"max_delegation_depth": 3}},
             "media_global_limits": {"image_generation": {"daily_requests": 5}}},
            {"a2a_global_limits": {"chain": {"max_delegation_depth": 0}},
             "media_global_limits": {"image_generation": {"daily_requests": 0}}},
        )
        assert child.get_effective_a2a_limits() == {"chain": {"max_delegation_depth": 0}}
        assert child.get_effective_media_limits() == {"image_generation": {"daily_requests": 0}}

    def test_a_childs_null_media_mode_leaves_the_ancestors(self, session):
        child = _chain(
            session,
            {"media_global_limits": {"image_generation": {"daily_requests": 5}}},
            {"media_global_limits": {"image_generation": None, "audio_speech": {"daily_requests": 2}}},
        )
        assert child.get_effective_media_limits() == {
            "image_generation": {"daily_requests": 5}, "audio_speech": {"daily_requests": 2},
        }

    def test_the_winning_value_keeps_its_type(self, session):
        child = _chain(
            session,
            {"llm_global_limits": {"requests": {"daily_limit": 5}}},
            {"llm_global_limits": {"requests": {"daily_limit": 7.0}}},
        )
        limit = child.get_effective_llm_limits()["requests"]["daily_limit"]
        assert limit == 5 and isinstance(limit, int)


class TestInheritedLimits:
    def test_an_ancestors_null_leaves_the_root_limit(self, session):
        leaf = _chain(
            session,
            {"llm_global_limits": {"tokens": {"daily_limit": 1000}}},
            {"llm_global_limits": {"tokens": {"daily_limit": None}}},
            {"llm_global_limits": {"tokens": {"daily_limit": 9999}}},
        )
        assert _compute_inherited_limits(leaf, "llm_global_limits") == {"tokens": {"daily_limit": 1000}}

    def test_an_ancestors_zero_is_not_inherited_as_a_quota(self, session):
        leaf = _chain(
            session,
            {"skill_global_limits": {"execution": {"daily_limit": 5}}},
            {"skill_global_limits": {"execution": {"daily_limit": 0}}},
            {},
        )
        assert _compute_inherited_limits(leaf, "skill_global_limits") == {"execution": {"daily_limit": 5}}


class TestInheritedGovernance:
    def test_a_childs_zero_concurrency_leaves_the_ancestors_skill_ceiling(self, session):
        child = _chain(
            session,
            {"skill_global_governance": {"max_concurrent_executions": 3}},
            {"skill_global_governance": {"max_concurrent_executions": 0}},
        )
        assert child.get_effective_skill_governance() == {"max_concurrent_executions": 3}

    def test_inherited_skill_governance_reads_the_ancestors_as_the_gateway_does(self, session):
        leaf = _chain(
            session,
            {"skill_global_governance": {"approval_timeout_seconds": 600}},
            {"skill_global_governance": {"approval_timeout_seconds": -1, "execution_mode": "disabled"}},
            {},
        )
        assert _compute_inherited_skill_governance(leaf) == {
            "approval_timeout_seconds": 600, "execution_mode": "disabled",
        }

    def test_a_childs_negative_media_ceiling_leaves_the_ancestors(self, session):
        child = _chain(
            session,
            {"media_global_governance": {"audio_speech": {"max_input_characters": 500}}},
            {"media_global_governance": {"audio_speech": {"max_input_characters": -1}}},
        )
        assert child.get_effective_media_governance() == {"audio_speech": {"max_input_characters": 500}}

    def test_inherited_media_governance_reads_the_ancestors_as_the_gateway_does(self, session):
        leaf = _chain(
            session,
            {"media_global_governance": {"image_generation": {"require_image_input": True}}},
            {"media_global_governance": {"image_generation": {"require_image_input": "no"}}},
            {},
        )
        assert _compute_inherited_media_governance(leaf) == {"image_generation": {"require_image_input": True}}
