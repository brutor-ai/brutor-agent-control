# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

from enum import Enum

# Standard endpoint URLs
class EndpointUrls(str, Enum):
    GATEWAY_MCP_PROXY = "/v1/proxy/mcp"
    GATEWAY_LLM_PROXY = "/v1/proxy/llm"
    MCP_SERVER_ADMIN = "/v1/admin/mcp-servers"