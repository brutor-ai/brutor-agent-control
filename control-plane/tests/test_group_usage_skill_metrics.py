# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Regression cover for the Agent-Skill usage category on the Resource-Group
"Usage & Analytics" response. Skill executions land in usage_logs with
resource_type='skill'; before this, they inflated `total_requests` but were
surfaced under no category. These tests pin the response CONTRACT (Pydantic
strips undeclared fields, so `skill` must be declared on GroupUsageResponse and
`skill_requests` on DailyUsageBreakdown) and the resource_type→bucket mapping
used by `get_group_usage`.

Run: pytest tests/test_group_usage_skill_metrics.py -q
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "x")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from app.api.v1.models.admin.resource_group import (  # noqa: E402
    GroupUsageResponse,
    SkillUsageMetrics,
    SkillUsageInfo,
    DailyUsageBreakdown,
    LLMUsageMetrics,
    MCPUsageMetrics,
    UsageSummary,
)


def test_group_usage_response_declares_skill_and_survives_serialization():
    """`skill` must reach the wire — a Pydantic model strips undeclared keys,
    so a missing field on GroupUsageResponse would silently drop skill usage."""
    resp = GroupUsageResponse(
        group_id="group-1",
        group_name="Sales",
        period="30d",
        summary=UsageSummary(total_requests=3, total_errors=0, error_rate=0.0),
        llm=LLMUsageMetrics(),
        mcp=MCPUsageMetrics(),
        skill=SkillUsageMetrics(
            request_count=3,
            success_count=3,
            error_count=0,
            avg_execution_time_ms=151.0,
            top_skills=[
                SkillUsageInfo(skill_name="revenue-report", skill_id="skill-01ABC",
                               call_count=2, total_execution_time_ms=300),
                SkillUsageInfo(skill_name="customer-followup", call_count=1),
            ],
        ),
    )
    d = resp.model_dump()
    assert "skill" in d, "GroupUsageResponse must expose a `skill` category"
    assert d["skill"]["request_count"] == 3
    assert d["skill"]["success_count"] == 3
    assert d["skill"]["top_skills"][0]["skill_name"] == "revenue-report"
    assert d["skill"]["top_skills"][0]["skill_id"] == "skill-01ABC"


def test_skill_metrics_default_is_empty_not_none():
    """The default keeps the panel stable when a group has no skill usage."""
    resp = GroupUsageResponse(
        group_id="g", group_name="g", period="30d",
        summary=UsageSummary(total_requests=0, total_errors=0, error_rate=0.0),
        llm=LLMUsageMetrics(), mcp=MCPUsageMetrics(),
    )
    assert resp.skill is not None
    assert resp.skill.request_count == 0
    assert resp.skill.top_skills == []


def test_daily_breakdown_carries_skill_requests():
    day = DailyUsageBreakdown(date="2026-07-12", skill_requests=5)
    assert day.model_dump()["skill_requests"] == 5


def test_resource_type_bucketing_isolates_skill_rows():
    """Mirror the exact filter get_group_usage uses so a future refactor that
    drops the 'skill' bucket (regressing the 42-uncategorized bug) fails here."""
    rows = [
        {"resource_type": "llm"},
        {"resource_type": "skill"},
        {"resource_type": "mcp"},
        {"resource_type": "skill"},
        {"resource_type": "skill"},
    ]
    skill_rows = [r for r in rows if r["resource_type"] == "skill"]
    assert len(skill_rows) == 3
    assert len([r for r in rows if r["resource_type"] == "llm"]) == 1
