# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

from enum import Enum

# Custom HTTP Headers
class HttpHeaders(str, Enum):
    X_TENANT_ID = "X-Tenant-ID"
    X_TENANT_SOURCE = "X-Tenant-Source"
    X_AUTH_TYPE = "x-auth-type"
    X_USER_AUTHENTICATED = "x-user-authenticated"
    X_GATEWAY_AUTHORIZATION = "X-Gateway-Authorization"
    X_GATEWAY_REQUEST_ID = "x-gateway-request-id"
    X_GATEWAY_ERROR = "x-gateway-error"
    X_API_KEY = "x-api-key"
    X_CORRELATION_ID = "x-correlation-id"
    X_REQUEST_TYPE = "x-request-type"
    X_SYSTEM_MCP_SERVER = "x-system-mcp-server"
    X_BRUTOR_USAGE_UTILIZATION = "X-Brutor-Usage-Utilization"
    X_BRUTOR_USAGE_WARNING = "X-Brutor-Usage-Warning"

    MCP_SESSION_ID = "mcp-session-id"

    BEARER_API_KEY_PREFIX = "sk_brutor_api_"
