# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

from enum import Enum

# Authentication types for MCP Server authentication
class AuthType(str, Enum):
    OAUTH = "oauth"
    PASSTHRU_OAUTH = "passthru_oauth"
    API_KEY = "api_key"
    ANONYMOUS = "anonymous"
