# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

from enum import Enum

# OAuth flow types
class GrantType(str, Enum):
    AUTHORIZATION_CODE_PKCE = "authorization_code_pkce"
    AUTHORIZATION_CODE = "authorization_code"
    CLIENT_CREDENTIALS = "client_credentials"
    REFRESH_TOKEN = "refresh_token"

# OAuth client type
class ClientType(str, Enum):
    PUBLIC = "public"                               # Like a local Keycloak Auth Code PKCE flow with Client ID
    PUBLIC_WITH_SECRET = "public_with_secret"       # Like HubSpot Auth Code PKCE flow with Client ID & Client Secret
    CONFIDENTIAL = "confidential"                   # Like GitHub traditional Auth code flow with client ID & secret


class JwtTokenType(str, Enum):
    MCP_AUTH = "mcp_auth"
