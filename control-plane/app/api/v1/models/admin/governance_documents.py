# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
What a governance document holds, for the API description.

A resource group's LLM, MCP, skill and A2A governance, and an agent card's
tenant governance, are stored and answered as JSON objects. The rules a
document is held to at save are `validate_llm_governance`,
`validate_mcp_governance`, `validate_skill_governance` and
`validate_a2a_governance` (`app.database.models.resource_group`); the
models here describe the same documents so that the API description says
what the keys are, and `tests/test_governance_documents.py` holds the two
to each other. A field of the API typed with one of the aliases below is
still validated and answered as the JSON object it was sent as.
"""

from __future__ import annotations

from typing import Annotated, Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.database.models.resource_group import MAX_APPROVAL_TIMEOUT_SECONDS

_APPROVAL_WINDOW = (
    f"Seconds a call held for approval waits for a decision: 1 to {MAX_APPROVAL_TIMEOUT_SECONDS} "
    "(7 days). Across a group and its ancestors the shortest applies."
)


class LlmTemperatureGovernance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max: Optional[float] = Field(None, ge=0.0, le=2.0, description="The highest temperature a request may ask for.")
    enforce: Optional[Literal["clamp", "reject"]] = Field(
        None, description="`clamp` lowers a higher temperature to `max`; `reject` refuses the request."
    )


class LlmContextWindowGovernance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_input_tokens: Optional[int] = Field(
        None,
        ge=1,
        description=(
            "The most input tokens a request may carry. Only with `enforce: reject`: an input "
            "cannot be shortened, so `clamp` (the default) is refused with it."
        ),
    )
    max_output_tokens: Optional[int] = Field(None, ge=1, description="The most output tokens a request may ask for.")
    enforce: Optional[Literal["clamp", "reject"]] = Field(
        None,
        description=(
            "`clamp` (the default) lowers a larger `max_output_tokens` request to the ceiling; "
            "`reject` refuses a request over either ceiling. `max_input_tokens` needs `reject`."
        ),
    )


class LlmToolUseGovernance(BaseModel):
    """What a model may do with tools. Nothing is held for a person here:
    there is no `approval_required` access and no approval window."""

    model_config = ConfigDict(extra="forbid")

    access: Optional[Literal["enabled", "disabled"]] = Field(
        None,
        description=(
            "`enabled`: the model may be offered tools and call them. `disabled`: it may not. "
            "`approval_required` is refused: approvals are not available for model tool calls."
        ),
    )
    allowed_tools: Optional[List[str]] = Field(
        None, description="The only tool names allowed. Across a group and its ancestors, the names every level allows."
    )
    blocked_tools: Optional[List[str]] = Field(
        None, description="Tool names refused; a blocked name wins over an allowed one. Every level's are added up."
    )
    banned_argument_patterns: Optional[List[str]] = Field(
        None, description="Regular expressions a tool call's arguments must not match. Every level's are added up."
    )
    max_tool_calls_per_request: Optional[int] = Field(None, ge=1, description="Tool calls allowed in one request.")
    max_tool_calls_per_hour: Optional[int] = Field(None, ge=1, description="Tool calls allowed in an hour.")
    max_tool_calls_per_day: Optional[int] = Field(None, ge=1, description="Tool calls allowed in a day.")


class LlmMandatoryFragment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position: Literal["prepend", "append"] = Field(..., description="Where the text goes in the system prompt.")
    text: str = Field(..., description="The text put into the system prompt of every request.")


class LlmSystemPromptGovernance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mandatory_fragments: Optional[List[LlmMandatoryFragment]] = Field(
        None, description="Text put into every request's system prompt; an ancestor's first, then the group's."
    )
    banned_patterns: Optional[List[str]] = Field(
        None, description="Patterns a request's messages must not contain. Every level's are added up."
    )


class LlmGovernanceDocument(BaseModel):
    """LLM governance: of a resource group for every model, or for one model
    of the group. Any other section is refused."""

    model_config = ConfigDict(extra="forbid")

    temperature: Optional[LlmTemperatureGovernance] = None
    context_window: Optional[LlmContextWindowGovernance] = None
    tool_use: Optional[LlmToolUseGovernance] = None
    system_prompt: Optional[LlmSystemPromptGovernance] = None


_ACCESS = Literal["enabled", "approval_required", "disabled"]
_ACCESS_NOTE = (
    "`enabled`, `approval_required` (the call is held for a person) or `disabled`. "
    "Across a group and its ancestors the strictest applies."
)


class McpGovernanceDocument(BaseModel):
    """MCP governance of a resource group: the access a capability has
    unless it is given one of its own. Any other key is refused."""

    model_config = ConfigDict(extra="forbid")

    default_tool_access: Optional[_ACCESS] = Field(None, description="Tools: " + _ACCESS_NOTE)
    default_resource_access: Optional[_ACCESS] = Field(None, description="Resources: " + _ACCESS_NOTE)
    default_prompt_access: Optional[_ACCESS] = Field(None, description="Prompts: " + _ACCESS_NOTE)
    default_resource_template_access: Optional[_ACCESS] = Field(
        None, description="Resource templates: " + _ACCESS_NOTE
    )
    approval_timeout_seconds: Optional[int] = Field(
        None, ge=1, le=MAX_APPROVAL_TIMEOUT_SECONDS, description=_APPROVAL_WINDOW
    )


class SkillGovernanceDocument(BaseModel):
    """Skill governance: of a resource group for every skill, or for one
    skill of the group. Any other key is refused."""

    model_config = ConfigDict(extra="forbid")

    execution_mode: Optional[Literal["unrestricted", "approval_required", "disabled"]] = Field(
        None,
        description=(
            "`unrestricted`: a skill runs when asked. `approval_required`: the execution is held "
            "for a person. `disabled`: it does not run. The strictest of the levels applies."
        ),
    )
    discovery_enabled: Optional[bool] = Field(
        None, description="Whether the group's skills are listed to its users and agents."
    )
    approval_timeout_seconds: Optional[int] = Field(
        None, ge=1, le=MAX_APPROVAL_TIMEOUT_SECONDS, description=_APPROVAL_WINDOW
    )
    max_concurrent_executions: Optional[int] = Field(None, ge=1, description="Executions running at once.")
    max_tool_calls_per_execution: Optional[int] = Field(None, ge=1, description="Advisory: the gateway hands this number to the agent with the skill, and the agent is expected to stop after it; the gateway does not count the tool calls of an execution.")


class A2ACapabilityGovernance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_access: Optional[_ACCESS] = Field(None, description=_ACCESS_NOTE)
    approval_timeout_seconds: Optional[int] = Field(
        None, ge=1, le=MAX_APPROVAL_TIMEOUT_SECONDS, description=_APPROVAL_WINDOW
    )


class A2ADataGovernance(BaseModel):
    deny_classifications: Optional[List[str]] = Field(
        None, description="Data classifications a call must not carry. Every level's are added up."
    )


class A2ARemoteGovernance(BaseModel):
    allowed_globs: Optional[List[str]] = Field(
        None,
        description=(
            "Patterns of the addresses an outbound call may go to. An address has to match the "
            "list of every level that states one."
        ),
    )


class A2AGovernanceDocument(BaseModel):
    """An A2A policy: a resource group's A2A governance or an agent card's
    tenant governance. Any other section is refused."""

    model_config = ConfigDict(extra="forbid")

    capability: Optional[A2ACapabilityGovernance] = None
    data: Optional[A2ADataGovernance] = None
    remote: Optional[A2ARemoteGovernance] = None


class _DescribedBy:
    """Marks a JSON-object field as holding the document `model` describes:
    the API description gets the model's schema; validation and
    serialisation stay those of the field's own type."""

    def __init__(self, model: type) -> None:
        self.model = model

    def __get_pydantic_json_schema__(self, _core_schema: Any, handler: Any) -> Dict[str, Any]:
        # Through a list, so that the model is published once under its
        # name and referred to, as a model that is a field's type is.
        return handler(TypeAdapter(List[self.model]).core_schema)["items"]


LlmGovernance = Annotated[Dict[str, Any], _DescribedBy(LlmGovernanceDocument)]
McpGovernance = Annotated[Dict[str, Any], _DescribedBy(McpGovernanceDocument)]
SkillGovernance = Annotated[Dict[str, Any], _DescribedBy(SkillGovernanceDocument)]
A2AGovernance = Annotated[Dict[str, Any], _DescribedBy(A2AGovernanceDocument)]
