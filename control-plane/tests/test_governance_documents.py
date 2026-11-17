# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The API description says what a governance document holds.

LLM, MCP, skill and A2A governance are stored and answered as JSON
objects, held at save to the validators of
`app.database.models.resource_group`. The description of the API carries a
schema for each (`app.api.v1.models.admin.governance_documents`): the same
keys, the same values, so that a client generated from the description
sends what is accepted. The keys of LLM governance are the ones the
gateway reads.
"""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from app.api.v1.models.admin import governance_documents as documents
from app.database.models import resource_group as rules
from tests.app_factory import create_app
from tests.core_repo import core_revision, core_source

VALIDATORS = {
    documents.LlmGovernanceDocument: rules.validate_llm_governance,
    documents.McpGovernanceDocument: rules.validate_mcp_governance,
    documents.SkillGovernanceDocument: rules.validate_skill_governance,
    documents.A2AGovernanceDocument: rules.validate_a2a_governance,
}

ACCEPTED = {
    documents.LlmGovernanceDocument: [
        {"temperature": {"max": 0.7, "enforce": "clamp"}},
        {"context_window": {"max_input_tokens": 1000, "max_output_tokens": 10, "enforce": "reject"}},
        {"tool_use": {"access": "disabled"}},
        {"tool_use": {"access": "enabled", "allowed_tools": ["a"], "blocked_tools": ["b"],
                      "banned_argument_patterns": ["rm -rf"], "max_tool_calls_per_request": 1,
                      "max_tool_calls_per_hour": 2, "max_tool_calls_per_day": 3}},
        {"system_prompt": {"mandatory_fragments": [{"position": "prepend", "text": "Be kind."}],
                           "banned_patterns": ["secret"]}},
    ],
    documents.McpGovernanceDocument: [
        {"default_tool_access": "approval_required", "default_resource_access": "enabled",
         "default_prompt_access": "disabled", "default_resource_template_access": "enabled",
         "approval_timeout_seconds": 300},
        {"approval_timeout_seconds": 604800},
    ],
    documents.SkillGovernanceDocument: [
        {"execution_mode": "approval_required", "discovery_enabled": False, "approval_timeout_seconds": 60,
         "max_concurrent_executions": 2, "max_tool_calls_per_execution": 5},
        {"execution_mode": "unrestricted"},
    ],
    documents.A2AGovernanceDocument: [
        {"capability": {"default_access": "approval_required", "approval_timeout_seconds": 300}},
        {"data": {"deny_classifications": ["PHI"]}, "remote": {"allowed_globs": ["https://*.partner.example/*"]}},
    ],
}

REFUSED = {
    documents.LlmGovernanceDocument: [
        {"budget": {}},
        {"temperature": {"max": 2.5}},
        {"temperature": {"enforce": "warn"}},
        {"temperature": {"min": 0.1}},
        {"context_window": {"max_input_tokens": 0}},
        {"context_window": {"max_tokens": 5}},
        {"tool_use": {"access": "approval_required"}},
        {"tool_use": {"access": "sometimes"}},
        {"tool_use": {"approval_timeout_seconds": 300}},
        {"tool_use": {"max_tool_calls_per_request": 0}},
        {"tool_use": {"allow_parallel": True}},
        {"system_prompt": {"mandatory_fragments": [{"position": "middle", "text": "x"}]}},
        {"system_prompt": {"mandatory_fragments": [{"position": "append"}]}},
        {"system_prompt": {"prefix": "x"}},
        {"system_prompt": {"mandatory_fragments": [{"position": "append", "text": "x", "role": "system"}]}},
    ],
    documents.McpGovernanceDocument: [
        {"default_tool_access": "sometimes"},
        {"approval_timeout_seconds": 0},
        {"approval_timeout_seconds": 604801},
        {"default_access": "enabled"},
    ],
    documents.SkillGovernanceDocument: [
        {"execution_mode": "enabled"},
        {"approval_timeout_seconds": 604801},
        {"max_concurrent_executions": 0},
        {"max_tool_calls_per_execution": 0},
        {"discovery": True},
    ],
    documents.A2AGovernanceDocument: [
        {"capability": {"default_access": "sometimes"}},
        {"capability": {"approval_timeout_seconds": 604801}},
        {"capability": {"access": "enabled"}},
        {"limits": {}},
    ],
}


@pytest.mark.parametrize("model", list(VALIDATORS), ids=lambda m: m.__name__)
def test_the_described_document_is_the_one_that_is_accepted(model):
    for document in ACCEPTED[model]:
        assert VALIDATORS[model](document) == (True, None), document
        model.model_validate(document)
    for document in REFUSED[model]:
        ok, problem = VALIDATORS[model](document)
        assert ok is False and problem, document
        with pytest.raises(ValidationError):
            model.model_validate(document)


def test_the_keys_of_llm_governance_are_the_rules_own():
    sections = {
        "temperature": documents.LlmTemperatureGovernance,
        "context_window": documents.LlmContextWindowGovernance,
        "tool_use": documents.LlmToolUseGovernance,
        "system_prompt": documents.LlmSystemPromptGovernance,
    }
    assert set(documents.LlmGovernanceDocument.model_fields) == set(sections) == set(rules.LLM_GOVERNANCE_KEYS)
    for section, model in sections.items():
        assert set(model.model_fields) == set(rules.LLM_GOVERNANCE_KEYS[section]), section


def _struct_fields(source: str, name: str) -> set:
    body = source[source.index(f"pub struct {name} {{"):]
    return set(re.findall(r"pub ([a-z_]+):", body[: body.index("}")]))


def test_the_keys_of_llm_governance_are_the_ones_the_gateway_reads():
    source = core_source("models", "governance.rs").read_text(encoding="utf-8")
    read = f" [the gateway's source read: {core_revision()}]"
    assert _struct_fields(source, "LlmGovernance") == set(rules.LLM_GOVERNANCE_KEYS), read
    for struct, section in (
        ("TemperatureGovernance", "temperature"),
        ("ContextWindowGovernance", "context_window"),
        ("SystemPromptGovernance", "system_prompt"),
    ):
        assert _struct_fields(source, struct) == set(rules.LLM_GOVERNANCE_KEYS[section]), struct + read
    # The gateway still has a field for an approval window on tool use;
    # nothing is held there, so the control plane does not accept one.
    assert _struct_fields(source, "ToolUseGovernance") - set(rules.LLM_GOVERNANCE_KEYS["tool_use"]) == {
        "approval_timeout_seconds"
    }, read
    assert set(rules.LLM_GOVERNANCE_KEYS["tool_use"]) <= _struct_fields(source, "ToolUseGovernance"), read
    assert _struct_fields(source, "MandatoryFragment") == set(documents.LlmMandatoryFragment.model_fields), read


def _schema_of(document: dict, path: str, method: str, where: str, field: str) -> dict:
    operation = document["paths"][path][method]
    if where == "request":
        schema = operation["requestBody"]["content"]["application/json"]["schema"]
    else:
        schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    body = document["components"]["schemas"][schema["$ref"].rsplit("/", 1)[1]]
    return body["properties"][field]


def _refs(schema: dict) -> set:
    found = set()
    if "$ref" in schema:
        found.add(schema["$ref"].rsplit("/", 1)[1])
    for option in schema.get("anyOf", []):
        found |= _refs(option)
    return found


GROUP = "/v1/admin/resource-groups/{group_id}"


@pytest.mark.parametrize(
    "path, method, where, field, schema",
    [
        (f"{GROUP}/llm-global-governance", "patch", "request", "governance", "LlmGovernanceDocument"),
        (f"{GROUP}/llm-global-governance", "get", "response", "llm_global_governance", "LlmGovernanceDocument"),
        (f"{GROUP}/llm-global-governance", "get", "response", "effective_llm_governance", "LlmGovernanceDocument"),
        (f"{GROUP}/mcp-global-governance", "patch", "request", "governance", "McpGovernanceDocument"),
        (f"{GROUP}/mcp-global-governance", "get", "response", "effective_mcp_governance", "McpGovernanceDocument"),
        (f"{GROUP}/skill-global-governance", "patch", "request", "governance", "SkillGovernanceDocument"),
        (f"{GROUP}/skill-global-governance", "get", "response", "inherited_skill_governance", "SkillGovernanceDocument"),
        (f"{GROUP}/a2a-global-governance", "get", "response", "a2a_global_governance", "A2AGovernanceDocument"),
        (f"{GROUP}/a2a-global-governance", "get", "response", "effective_a2a_governance", "A2AGovernanceDocument"),
    ],
)
def test_the_api_description_carries_the_schema(path, method, where, field, schema):
    document = create_app().openapi()
    assert _refs(_schema_of(document, path, method, where, field)) == {schema}
    published = document["components"]["schemas"]
    assert schema in published
    if schema == "LlmGovernanceDocument":
        tool_use = published["LlmToolUseGovernance"]
        assert set(tool_use["properties"]) == set(rules.LLM_GOVERNANCE_KEYS["tool_use"])
        assert tool_use["additionalProperties"] is False
        assert tool_use["properties"]["access"]["anyOf"][0]["enum"] == ["enabled", "disabled"]


def test_a_document_is_still_answered_as_it_is_stored():
    """The schema describes the field; it does not rewrite what is stored."""
    from app.api.v1.models.admin.resource_group import LLMGlobalGovernanceResponse

    stored = {"tool_use": {"access": "enabled"}}
    answered = LLMGlobalGovernanceResponse(group_id="g", group_name="g", llm_global_governance=stored).model_dump()
    assert answered["llm_global_governance"] == stored
