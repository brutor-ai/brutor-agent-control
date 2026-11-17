# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
A value of an access, mode or effect setting the gateway does not know is
refused where it is saved.

The gateway reads such a value as the most restrictive one (an MCP default
access as denied, an LLM `enforce` as reject, a skill `execution_mode` as a
refusal, an autonomy level as suspended, a grant's effect as deny), and the
console's merges now rank it the same way
(`tests/test_python_rust_merge_parity.py`). Neither is a reason to store
one: a typo saved as `default_tool_access: "disbled"` denies every tool
call, and the operator who typed it is the one person who could have been
told. Every writer of these settings refuses it at save.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.admin_endpoint_harness import build_admin_harness

from app.api.v1.endpoints.admin.resource_groups import create_resource_groups_router
from app.api.v1.models.admin.llm_model import LLModelCreate, LLModelUpdate
from app.api.v1.models.admin.mcp_server import ServerCreateRequest, ServerUpdateRequest
from app.core.logging_system import BrutorLogger
from app.database.models.agent_identity import AgentGrant, AgentIdentity
from app.database.models.resource_group import ResourceGroup
from app.services.usage_limit_enforcer import UsageLimitEnforcer

UNKNOWN = ("disbled", "", 1, ["disabled"])


@pytest.fixture()
def h():
    log = BrutorLogger("test-unknown-values")
    harness = build_admin_harness(
        [create_resource_groups_router(UsageLimitEnforcer(logger=log), log)],
        logger_name="test-unknown-values",
    )
    yield harness
    harness.close()


def _group(h, name="org"):
    r = h.client.post(
        "/v1/admin/resource-groups", json={"name": name}, headers=h.headers("t1")
    )
    assert r.status_code in (200, 201), r.text
    payload = r.json()
    return payload.get("group", payload)["id"]


def _stored_skill_governance(h, gid):
    r = h.client.get(f"/v1/admin/resource-groups/{gid}/skill-global-governance", headers=h.headers())
    assert r.status_code == 200, r.text
    return r.json()["skill_global_governance"]


class TestResourceGroupGovernance:
    @pytest.mark.parametrize("value", UNKNOWN)
    def test_the_group_update_refuses_an_unknown_execution_mode(self, h, value):
        """The group's own update accepted any skill governance document;
        the dedicated endpoint validated it, this one did not."""
        gid = _group(h)
        r = h.client.patch(
            f"/v1/admin/resource-groups/{gid}",
            json={"skill_global_governance": {"execution_mode": value}},
            headers=h.headers(),
        )
        assert r.status_code == 400, r.text
        assert "execution_mode" in r.json()["detail"]
        assert not _stored_skill_governance(h, gid)

    def test_the_group_update_still_stores_a_known_execution_mode(self, h):
        gid = _group(h)
        r = h.client.patch(
            f"/v1/admin/resource-groups/{gid}",
            json={"skill_global_governance": {"execution_mode": "approval_required"}},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text
        assert _stored_skill_governance(h, gid) == {"execution_mode": "approval_required"}

    @pytest.mark.parametrize(
        "path,governance",
        [
            ("llm-global-governance", {"temperature": {"enforce": "drop"}}),
            ("llm-global-governance", {"context_window": {"enforce": "drop"}}),
            ("llm-global-governance", {"tool_use": {"access": "disbled"}}),
            ("mcp-global-governance", {"default_tool_access": "disbled"}),
            ("mcp-global-governance", {"default_resource_template_access": 2}),
            ("skill-global-governance", {"execution_mode": "disbled"}),
        ],
    )
    def test_the_governance_endpoints_refuse_an_unknown_value(self, h, path, governance):
        gid = _group(h)
        r = h.client.patch(
            f"/v1/admin/resource-groups/{gid}/{path}",
            json={"governance": governance},
            headers=h.headers(),
        )
        assert r.status_code == 400, r.text


class TestMcpServerTenantGovernance:
    """The gateway reads a server's `default_*_access` as a ceiling over
    every group (`usage::mcp_default_access_from_gov`)."""

    @pytest.mark.parametrize("key", [
        "default_tool_access", "default_resource_access",
        "default_prompt_access", "default_resource_template_access",
    ])
    @pytest.mark.parametrize("value", UNKNOWN)
    def test_an_unknown_default_access_is_refused(self, key, value):
        with pytest.raises(ValidationError, match=key):
            ServerCreateRequest(name="s", base_url="https://x", tenant_governance={key: value})
        with pytest.raises(ValidationError, match=key):
            ServerUpdateRequest(tenant_governance={key: value})

    @pytest.mark.parametrize("value", ["enabled", "approval_required", "disabled", None])
    def test_a_known_default_access_is_accepted(self, value):
        gov = {"default_tool_access": value}
        assert ServerCreateRequest(name="s", base_url="https://x", tenant_governance=gov).tenant_governance == gov
        assert ServerUpdateRequest(tenant_governance=gov).tenant_governance == gov


class TestLlmModelTenantGovernance:
    """The gateway folds a model's tenant governance into the same merge as
    a group's LLM governance (`usage::collect_effective_governance`)."""

    @pytest.mark.parametrize("governance", [
        {"temperature": {"enforce": "drop"}},
        {"context_window": {"enforce": 1}},
        {"tool_use": {"access": "disbled"}},
    ])
    def test_an_unknown_enforce_or_access_is_refused(self, governance):
        with pytest.raises(ValidationError):
            LLModelCreate(name="m", provider_id="p", model_name="m", mode="chat",
                          tenant_governance=governance)
        with pytest.raises(ValidationError):
            LLModelUpdate(tenant_governance=governance)

    @pytest.mark.parametrize("governance", [
        {"temperature": {"enforce": "reject", "max": 0.5}},
        {"tool_use": {"access": "disabled"}},
        # A media model's tenant governance is a flat field map of its mode.
        {"voice_whitelist": ["alloy"]},
    ])
    def test_a_known_one_is_accepted(self, governance):
        assert LLModelUpdate(tenant_governance=governance).tenant_governance == governance


class TestAutonomyAndGrantEffect:
    """Both are held at every write by the models' validators."""

    @pytest.mark.parametrize("value", UNKNOWN)
    def test_an_unknown_grant_effect_is_refused(self, value):
        with pytest.raises((ValueError, TypeError)):
            AgentGrant(id="g", tenant_id="t", agent_id="a", action_type="llm_call",
                       target="*", effect=value)

    @pytest.mark.parametrize("value", UNKNOWN)
    def test_an_unknown_autonomy_level_is_refused(self, value):
        with pytest.raises((ValueError, TypeError)):
            ResourceGroup(id="x", tenant_id="t", name="x", autonomy_level=value)
        with pytest.raises((ValueError, TypeError)):
            AgentIdentity(id="a", tenant_id="t", name="a", autonomy_level=value)


class TestContractDiffReadsAnUnknownValueAsTheGatewayDoes:
    """A contract's diff tells an approver whether a system can now do more.
    A grant whose effect the gateway does not know is a deny there, and an
    autonomy level it does not know is suspended."""

    def _grants(self, effect):
        return {"agent_grants": [{"agent_id": "a", "action_type": "llm_call", "target": "*", "effect": effect}]}

    @pytest.mark.parametrize("effect", ["alow", None])
    def test_a_new_grant_with_an_unknown_effect_does_not_widen(self, effect):
        from app.services.ai_system_contract_service import _diff_agent_grants

        widened, narrowed = [], []
        _diff_agent_grants({}, self._grants(effect), widened, narrowed)
        assert widened == [] and len(narrowed) == 1

    def test_an_unknown_effect_relaxed_to_allow_widens(self):
        from app.services.ai_system_contract_service import _diff_agent_grants

        widened, narrowed = [], []
        _diff_agent_grants(self._grants("alow"), self._grants("allow"), widened, narrowed)
        assert len(widened) == 1 and narrowed == []

    def test_an_unknown_autonomy_level_moved_to_a_known_one_widens(self):
        from app.services.ai_system_contract_service import _diff_autonomy

        widened, narrowed = [], []
        _diff_autonomy({"autonomy_level": "freewheeling"}, {"autonomy_level": "restricted"}, widened, narrowed)
        assert len(widened) == 1 and narrowed == []
