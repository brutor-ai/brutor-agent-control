# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Live GOVERNANCE suite — Agent Skills, legacy REST execute REMOVAL.

The one-shot REST `POST /v1/proxy/skills/execute` has been removed for
progressive-disclosure skills: a skill is consumed only via the Agent Skills MCP
flow (tools/list → skills__load → skills__run_script), where every layer is
governed. This module proves the removal; the governed-behaviour scenarios it
used to drive over REST (argument policy, input/output guardrails, quota,
approval, access scoping, attribution) now live in `test_skills_progressive.py`
over the MCP path.

A skill left in the legacy `execute` / `both` disclosure mode still answers on
REST during migration — the demo skills default to `progressive`, so they 410.

Provisioning: setup-llm-demo.py (base) + setup-skill-demo.py.
Run:  pytest tests/live/governance/test_skills.py -m live_e2e
"""

from __future__ import annotations

import pytest

from .helpers import (
    demo_env,
    demo_keys,
    run_setup,
    skill_execute,
)


@pytest.fixture(scope="session")
def skills(base_scaffolding) -> dict:
    run_setup("setup-skill-demo.py")
    env = demo_env()
    keys = demo_keys()
    return {
        "sales_key": keys["SALES_KEY"],
        "ops_key": keys["OPS_KEY"],
        "sales_group": env.get("SALES_GROUP"),
    }


def test_rest_execute_removed_for_progressive_skill(proxy_client, skills):
    """A progressive-disclosure skill can no longer be run via the legacy REST
    execute — it returns 410 Gone directing the caller to the MCP flow."""
    call = skill_execute(
        proxy_client, skills["sales_key"], "revenue-report",
        {"query_sql": "SELECT region, SUM(amount) FROM sales GROUP BY region"},
    )
    assert call.status == 410, f"expected 410 Gone, got {call.status}: {call.text}"
    body = call.text.lower()
    assert "skills__load" in body or "progressive" in body or "removed" in body, call.text


@pytest.mark.parametrize("skill_name", ["revenue-report", "customer-followup", "customer-refund"])
def test_rest_execute_removed_for_all_demo_skills(proxy_client, skills, skill_name):
    """Every demo skill defaults to progressive disclosure, so none is runnable
    over the legacy REST execute path."""
    call = skill_execute(proxy_client, skills["sales_key"], skill_name, {"note": "hi"})
    assert call.status == 410, f"{skill_name}: expected 410, got {call.status}: {call.text}"
