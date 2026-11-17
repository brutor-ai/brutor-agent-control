# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
What an MCP limits document holds, for the API description.

A resource group's `mcp_global_limits`, a server binding's `limits` and an
MCP server's `tenant_limits` are stored and answered as JSON objects, held at
save to `ResourceGroup.validate_mcp_limits_structure`. The models here
describe the execution limits the gateway enforces (`app.services.
mcp_exec_limits`); a field typed `McpLimits` is still validated and answered
as the JSON object it was sent as.
"""

from __future__ import annotations

from typing import Annotated, Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.api.v1.models.admin.governance_documents import _DescribedBy
from app.services import mcp_exec_limits as rules


class McpExecutionLimitsDocument(BaseModel):
    """How long a call may take and how many may be in flight at once."""

    model_config = ConfigDict(extra="forbid")

    timeout_seconds: Optional[float] = Field(None, gt=0, description=rules.TIMEOUT_DESCRIPTION)
    max_concurrency_per_user: Optional[int] = Field(None, ge=1, description=rules.PER_USER_DESCRIPTION)
    max_concurrency_per_group: Optional[int] = Field(None, ge=1, description=rules.PER_GROUP_DESCRIPTION)


class McpDataLimitsDocument(BaseModel):
    """How much a call may carry."""

    model_config = ConfigDict(extra="forbid")

    max_payload_size_mb: Optional[float] = Field(None, gt=0, description=rules.PAYLOAD_DESCRIPTION)


class McpLimitsDocument(BaseModel):
    __doc__ = rules.DOCUMENT_DESCRIPTION

    model_config = ConfigDict(extra="forbid")

    execution: Optional[McpExecutionLimitsDocument] = None
    data: Optional[McpDataLimitsDocument] = None
    frequency: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "Rate limits of the server's tool calls: max_calls_per_minute, burst_limit, "
            "max_calls_per_hour, max_calls_per_day, cooldown_seconds, daily_warning_percent."
        ),
    )
    per_tool: Optional[Dict[str, Dict[str, Any]]] = Field(
        None, description="Rate limits of one tool, keyed by tool name: the keys of `frequency`."
    )


McpLimits = Annotated[Dict[str, Any], _DescribedBy(McpLimitsDocument)]
