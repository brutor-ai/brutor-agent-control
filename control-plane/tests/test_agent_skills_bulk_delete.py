# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for the Agent Skills bulk-delete contract.

The bulk-delete endpoint is thin orchestration over the same per-skill
primitives the single DELETE already exercises (group unassign + delete +
audit + cache invalidation + capability sync). What is genuinely new and
worth pinning down here is the *contract*:

  * the request bounds (1..500 ids per call)
  * the response shape that lets the UI report partial outcomes
    (deleted vs skipped-with-reason)

These mirror the pure-model style used by test_agent_skills_export.py
— no FastAPI TestClient / live DB needed.

Run: pytest tests/test_agent_skills_bulk_delete.py -v
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from pydantic import ValidationError

from app.api.v1.models.admin.agent_skill import (  # noqa: E402
    SkillBulkDeleteRequest,
    SkillBulkDeleteResponse,
    SkillBulkDeleteSkipped,
)


# ---------------------------------------------------------------------------
# Request bounds
# ---------------------------------------------------------------------------


class TestBulkDeleteRequest:
    def test_accepts_single_id(self):
        req = SkillBulkDeleteRequest(skill_ids=["ask-1"])
        assert req.skill_ids == ["ask-1"]

    def test_accepts_many_ids(self):
        ids = [f"ask-{i}" for i in range(500)]
        req = SkillBulkDeleteRequest(skill_ids=ids)
        assert len(req.skill_ids) == 500

    def test_rejects_empty_list(self):
        # An empty bulk delete is a client bug — reject rather than
        # silently no-op.
        with pytest.raises(ValidationError):
            SkillBulkDeleteRequest(skill_ids=[])

    def test_rejects_over_cap(self):
        # 500 is the per-call cap; 501 must be rejected so a runaway
        # client can't try to delete the whole tenant in one request.
        ids = [f"ask-{i}" for i in range(501)]
        with pytest.raises(ValidationError):
            SkillBulkDeleteRequest(skill_ids=ids)

    def test_requires_skill_ids(self):
        with pytest.raises(ValidationError):
            SkillBulkDeleteRequest()


# ---------------------------------------------------------------------------
# Response shape
# ---------------------------------------------------------------------------


class TestBulkDeleteResponse:
    def test_defaults_are_empty(self):
        resp = SkillBulkDeleteResponse()
        assert resp.deleted == []
        assert resp.skipped == []
        assert resp.deleted_count == 0
        assert resp.skipped_count == 0

    def test_carries_deleted_and_skipped(self):
        resp = SkillBulkDeleteResponse(
            deleted=["ask-1", "ask-2"],
            skipped=[
                SkillBulkDeleteSkipped(
                    skill_id="ask-3",
                    reason="Published skills must be deprecated then archived before deletion",
                ),
                SkillBulkDeleteSkipped(skill_id="ask-4", reason="Not found"),
            ],
            deleted_count=2,
            skipped_count=2,
        )
        assert resp.deleted_count == 2
        assert resp.skipped_count == 2
        assert {s.skill_id for s in resp.skipped} == {"ask-3", "ask-4"}
        # The published-skip reason is what the UI surfaces in its warn
        # banner — keep it human-readable.
        assert "deprecated" in resp.skipped[0].reason

    def test_skipped_entry_requires_reason(self):
        with pytest.raises(ValidationError):
            SkillBulkDeleteSkipped(skill_id="ask-1")

    def test_round_trips_through_dict(self):
        # The endpoint returns the model; FastAPI serialises via the
        # response_model. Confirm a dict round-trip preserves the shape
        # so a downstream consumer (admin UI) sees stable keys.
        resp = SkillBulkDeleteResponse(
            deleted=["ask-1"],
            skipped=[SkillBulkDeleteSkipped(skill_id="ask-2", reason="Not found")],
            deleted_count=1,
            skipped_count=1,
        )
        d = resp.model_dump()
        assert d["deleted"] == ["ask-1"]
        assert d["skipped"] == [{"skill_id": "ask-2", "reason": "Not found"}]
        assert d["deleted_count"] == 1
        assert d["skipped_count"] == 1
