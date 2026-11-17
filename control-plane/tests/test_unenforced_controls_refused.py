# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
No control an administrator can set that the product does not enforce.

Every setting below is stored by the Control Plane and read by nothing that
acts on it (or, for `hard_stop: false` and `clamp` on input tokens, acted on
otherwise than the setting says). A value other than the default is refused
when it is saved, with a sentence saying the setting is not available in this
release, and the response does not present the setting as active. What the
gateway is building now (MCP timeout, per-user and per-group concurrency and
payload; the daily media images, audio seconds and video seconds) is still
accepted.

The Enterprise half (the response cache, Policy-as-Code bundles) is in
`enterprise/tests/test_unenforced_controls_refused.py`.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.database.models.resource_group import (
    ResourceGroup,
    validate_a2a_limits,
    validate_llm_governance,
)

NOT_AVAILABLE = "is not available in this release"


# --- MCP limits (group, server binding, MCP server tenant_limits) -------------------------------

UNENFORCED_MCP = [
    ("execution", "memory_limit_mb", 512),
    ("execution", "cpu_limit_percent", 50),
    ("data", "max_context_window_percent", 40),
    ("data", "max_rows_per_query", 100),
    ("data", "max_items_per_response", 10),
]
BEING_BUILT_MCP = [
    ("execution", "timeout_seconds", 30),
    ("execution", "max_concurrency_per_user", 2),
    ("execution", "max_concurrency_per_group", 5),
    ("data", "max_payload_size_mb", 5),
]


@pytest.mark.parametrize("section,key,value", UNENFORCED_MCP)
def test_an_unenforced_mcp_limit_is_refused(section, key, value):
    ok, why = ResourceGroup.validate_mcp_limits_structure({section: {key: value}})
    assert not ok and NOT_AVAILABLE in why and f"{section}.{key}" in why
    from app.api.v1.models.admin.mcp_server import ServerCreateRequest, ServerUpdateRequest

    for model in (ServerCreateRequest, ServerUpdateRequest):
        with pytest.raises(ValidationError, match=NOT_AVAILABLE):
            model.model_validate({"name": "s", "url": "https://mcp.example/mcp", "tenant_limits": {section: {key: value}}})


@pytest.mark.parametrize("section,key,value", BEING_BUILT_MCP)
def test_an_mcp_limit_the_gateway_is_building_is_accepted(section, key, value):
    assert ResourceGroup.validate_mcp_limits_structure({section: {key: value}}) == (True, None)


# --- Media limits and governance ----------------------------------------------------------------


@pytest.mark.parametrize(
    "mode,key,value",
    [
        ("image_generation", "monthly_budget_usd", 10.0),
        ("image_generation", "monthly_requests", 100),
        ("image_generation", "monthly_images", 100),
        ("audio_speech", "monthly_characters", 1000),
        ("audio_transcription", "monthly_audio_seconds", 1000),
        ("video_generation", "monthly_video_seconds", 100),
        ("audio_speech", "daily_warning_percent", 80),
        ("audio_speech", "monthly_warning_percent", 80),
        ("image_generation", "hard_stop", False),
        ("embedding", "daily_budget_usd", 1.0),
        ("embedding", "daily_input_tokens", 1000),
    ],
)
def test_an_unenforced_media_limit_is_refused(mode, key, value):
    from app.api.v1.models.admin.resource_group import MediaGlobalLimitsUpdate

    with pytest.raises(ValidationError, match=NOT_AVAILABLE):
        MediaGlobalLimitsUpdate(media_global_limits={mode: {key: value}})


@pytest.mark.parametrize(
    "mode,key,value",
    [
        ("image_generation", "daily_images", 10),
        ("audio_transcription", "daily_audio_seconds", 600),
        ("video_generation", "daily_video_seconds", 60),
        ("audio_speech", "daily_characters", 1000),
        ("image_generation", "daily_budget_usd", 5.0),
        ("image_generation", "daily_requests", 10),
        ("image_generation", "hard_stop", True),
    ],
)
def test_an_enforced_or_building_media_limit_is_accepted(mode, key, value):
    from app.api.v1.models.admin.resource_group import MediaGlobalLimitsUpdate

    MediaGlobalLimitsUpdate(media_global_limits={mode: {key: value}})


@pytest.mark.parametrize(
    "mode,key,value",
    [
        ("embedding", "max_input_tokens_per_item", 512),
        ("audio_transcription", "require_prompt_guardrail", False),
    ],
)
def test_an_unenforced_media_governance_key_is_refused(mode, key, value):
    from app.api.v1.models.admin.resource_group import MediaGlobalGovernanceUpdate

    with pytest.raises(ValidationError, match=NOT_AVAILABLE):
        MediaGlobalGovernanceUpdate(media_global_governance={mode: {key: value}})
    # The default (the prompt is always checked) is accepted.
    MediaGlobalGovernanceUpdate(media_global_governance={"audio_transcription": {"require_prompt_guardrail": True}})


# --- LLM ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("enforce", [None, "clamp"])
def test_clamp_on_input_tokens_is_refused(enforce):
    ctx = {"max_input_tokens": 1000, **({"enforce": enforce} if enforce else {})}
    ok, why = validate_llm_governance({"context_window": ctx})
    assert not ok and NOT_AVAILABLE in why and "max_input_tokens" in why


def test_reject_on_input_tokens_and_clamp_on_output_tokens_are_accepted():
    assert validate_llm_governance({"context_window": {"max_input_tokens": 1000, "enforce": "reject"}}) == (True, None)
    assert validate_llm_governance({"context_window": {"max_output_tokens": 1000, "enforce": "clamp"}}) == (True, None)


def test_an_llm_budget_hard_stop_false_is_refused():
    group = ResourceGroup(id="g", tenant_id="t", name="g", llm_global_limits={"budget": {"daily_limit_usd": 1.0, "hard_stop": False}})
    ok, why = group.validate_llm_limits()
    assert not ok and NOT_AVAILABLE in why and "hard_stop" in why
    group.llm_global_limits = {"budget": {"daily_limit_usd": 1.0, "hard_stop": True}}
    assert group.validate_llm_limits() == (True, None)


# --- A2A ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key,value", [("daily_warning_percent", 80), ("monthly_warning_percent", 80), ("hard_stop", False)]
)
def test_an_unenforced_a2a_invocation_key_is_refused(key, value):
    ok, why = validate_a2a_limits({"invocations": {"daily_limit": 10, key: value}})
    assert not ok and NOT_AVAILABLE in why and key in why


def test_the_a2a_caps_are_accepted():
    assert validate_a2a_limits({"invocations": {"daily_limit": 10, "monthly_limit": 100, "hard_stop": True}}) == (True, None)


# --- Skills -------------------------------------------------------------------------------------


def test_a_skills_own_policy_is_refused():
    from app.api.v1.models.admin.agent_skill import AgentSkillUpdate

    with pytest.raises(ValidationError, match=NOT_AVAILABLE):
        AgentSkillUpdate(tenant_governance={"execution_mode": "approval_required"})
    assert AgentSkillUpdate(tenant_governance=None).tenant_governance is None
    assert AgentSkillUpdate(tenant_governance={}).tenant_governance in (None, {})


def test_max_tool_calls_per_execution_is_described_as_advice_to_the_agent():
    from app.api.v1.models.admin.governance_documents import SkillGovernanceDocument

    description = SkillGovernanceDocument.model_fields["max_tool_calls_per_execution"].description
    assert "advisory" in description.lower() and "does not count" in description


# --- AI Asset Register --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["warn", "enforce"])
def test_an_asset_enforcement_mode_other_than_off_is_refused(mode):
    from app.api.v1.models.admin.ai_asset import AiAssetUpdateRequest

    with pytest.raises(ValidationError, match=NOT_AVAILABLE):
        AiAssetUpdateRequest(enforcement_mode=mode)
    assert AiAssetUpdateRequest(enforcement_mode="off").enforcement_mode == "off"


# --- Search -------------------------------------------------------------------------------------


@pytest.mark.parametrize("key,value", [("daily_query_limit", 100), ("max_results_per_query", 5)])
def test_an_unenforced_search_setting_is_refused(key, value):
    from app.api.v1.endpoints.admin.system_servers import SearchProviderConfig, UpdateSearchProviderRequest

    with pytest.raises(ValidationError, match=NOT_AVAILABLE):
        UpdateSearchProviderRequest(**{key: value})
    assert SearchProviderConfig().model_dump()[key] is None


# --- Routing groups -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key,value",
    [
        ("alerting_enabled", True),
        ("alerting_threshold", 60),
        ("alerting_webhook_url", "https://hooks.example/x"),
        ("alerting_config", {"a": 1}),
        ("required_capabilities", ["vision"]),
        ("routing_max_retries", 5),
        ("routing_retry_delay", 1.0),
        ("ttl_for_blocked_models", 60),
        ("router_settings", {"x": 1}),
    ],
)
def test_an_unenforced_routing_group_field_is_refused(key, value):
    from app.api.v1.models.admin.llm_routing_group import RoutingGroupResponse, RoutingGroupUpdate

    with pytest.raises(ValidationError, match=NOT_AVAILABLE):
        RoutingGroupUpdate(**{key: value})
    assert key not in RoutingGroupResponse.model_fields


# --- Health-check settings ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("mcp_settings", "timeout_seconds", 5),
        ("mcp_settings", "retry_attempts", 1),
        ("mcp_settings", "failure_threshold", 2),
        ("mcp_settings", "recovery_check_interval", 30),
        ("ai_settings", "timeout_seconds", 5),
        ("ai_settings", "retry_attempts", 1),
        ("ai_settings", "recovery_check_interval", 30),
        ("ai_settings", "test_prompt", "hello"),
        ("ai_settings", "max_tokens", 5),
        ("dashboard_settings", "show_ai_models", False),
        ("dashboard_settings", "show_mcp_servers", False),
    ],
)
def test_an_unread_health_check_setting_is_refused_and_not_shown(section, key, value):
    from app.api.v1.models.admin.gateway_config import (
        AIHealthCheckSettings,
        DashboardHealthCheckSettings,
        MCPHealthCheckSettings,
        UpdateHealthCheckConfigRequest,
    )

    with pytest.raises(ValidationError, match=NOT_AVAILABLE):
        UpdateHealthCheckConfigRequest(**{section: {key: value}})
    model = {"mcp_settings": MCPHealthCheckSettings, "ai_settings": AIHealthCheckSettings,
             "dashboard_settings": DashboardHealthCheckSettings}[section]
    assert model().model_dump()[key] is None


def test_the_health_check_settings_the_console_and_the_sweep_read_are_accepted():
    from app.api.v1.models.admin.gateway_config import UpdateHealthCheckConfigRequest

    UpdateHealthCheckConfigRequest(
        mcp_settings={"enabled": False, "interval_seconds": 120, "pause_when_inactive": False},
        ai_settings={"enabled": True, "interval_seconds": 600, "failure_threshold": 5},
        dashboard_settings={"enabled": True, "interval_seconds": 30},
    )


# --- Descriptions -------------------------------------------------------------------------------


def test_a_fallback_call_is_not_counted_against_the_models_own_per_minute_limits():
    from app.api.v1.models.admin import llm_model

    for text in (llm_model.RATE_LIMIT_RPM_DESCRIPTION, llm_model.RATE_LIMIT_TPM_DESCRIPTION):
        assert "fallback" in text and "not counted" in text


def test_drop_params_says_what_it_switches():
    from app.api.v1.models.admin import llm_model

    assert "provider" in llm_model.DROP_PARAMS_DESCRIPTION
    assert "additional_drop_params" in llm_model.DROP_PARAMS_DESCRIPTION
    assert "Used when drop_params=False" not in llm_model.LLModelCreate.model_fields["additional_drop_params"].description


def test_the_feature_flags_that_carry_no_traffic_say_what_they_do():
    from app.constants.tenant import TENANT_FEATURE_DESCRIPTIONS

    for flag in ("guardrails", "policies", "compliance", "model_routing"):
        assert "Admin Console" in TENANT_FEATURE_DESCRIPTIONS[flag], flag
        assert "traffic" in TENANT_FEATURE_DESCRIPTIONS[flag], flag


# --- The skill workflow builder (HTTP) ----------------------------------------------------------


@pytest.fixture(scope="module")
def h():
    from tests.admin_endpoint_harness import build_admin_harness
    from tests.app_factory import create_app

    harness = build_admin_harness([], logger_name="test-unenforced")
    real = create_app()
    harness.app.router.routes.extend(real.router.routes)
    real.dependency_overrides = harness.app.dependency_overrides
    harness.loose = TestClient(harness.app, raise_server_exceptions=False)
    yield harness
    harness.close()


@pytest.mark.parametrize(
    "method,suffix,body",
    [
        ("PATCH", "/workflow", {"nodes": [], "edges": []}),
        ("POST", "/workflow/publish", None),
        ("POST", "/workflow/execute", {}),
    ],
)
def test_the_skill_workflow_builder_is_refused(h, method, suffix, body):
    r = h.loose.request(method, f"/v1/admin/agent-skills/any-skill{suffix}", json=body, headers=h.headers("t1"))
    assert r.status_code == 422, r.text
    assert NOT_AVAILABLE in r.text
