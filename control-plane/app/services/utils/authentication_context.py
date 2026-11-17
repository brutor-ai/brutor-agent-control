# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Authentication Context

Represents an authenticated end-user client making requests through the gateway.
Authentication and API key validation are handled by the Rust proxy;
this module provides the context dataclasses and API key generation
used by admin endpoints and the skill orchestrator.
"""

import uuid
import secrets
import hashlib
from typing import Optional, Dict, Any, Tuple
from dataclasses import dataclass
from app.constants.auth_type import AuthType
from app.constants.http import HttpHeaders


@dataclass
class APIKeyContext:
    """
    Context for API Key authentication

    Attributes:
        key_id: Unique identifier for the API key
        key_name: Human-readable name of the API key
        scope: Scope of the key ('global' or 'user')
        tenant_id: Tenant the key belongs to
        is_global: Whether this is a global key (scope == 'global')
        user_id: Optional user ID if key is user-scoped
        user_email: Optional user email if key is user-scoped
        group_id: Optional Resource Group ID if key is group-scoped
        group_name: Optional Resource Group name if key is group-scoped
    """
    key_id: str
    key_name: str
    scope: str
    tenant_id: str
    is_global: bool
    user_id: Optional[int] = None
    user_email: Optional[str] = None
    group_id: Optional[str] = None
    group_name: Optional[str] = None

    @property
    def is_user_specific(self) -> bool:
        """Check if this key is scoped to a specific user"""
        return self.scope == 'user' and self.user_id is not None

    @property
    def is_group_specific(self) -> bool:
        """Check if this key is scoped to a Resource Group"""
        return self.group_id is not None

    @property
    def has_user(self) -> bool:
        """Check if this key is associated with a user"""
        return self.user_id is not None

    @property
    def has_group(self) -> bool:
        """Check if this key is associated with a Resource Group"""
        return self.group_id is not None


@dataclass
class GatewayUserContext:
    """
    Context for Gateway User authentication (via JWT)

    Attributes:
        tenant_id: Tenant the Gateway user belongs to
        user_id: Unique identifier for the Gateway user
        username: Gateway username (email or username)
        email: Optional email address
        roles: Optional list of user roles
        groups: Optional list of group IDs user belongs to
    """
    tenant_id: str
    user_id: str
    username: str
    email: Optional[str] = None
    roles: Optional[list] = None
    groups: Optional[list] = None

    @property
    def is_admin(self) -> bool:
        """Check if user has admin role"""
        return self.roles and 'admin' in self.roles if self.roles else False


class AuthenticationContext:
    """
    Represents an authenticated end-user client or system client.

    Authentication and API key validation are handled by the Rust proxy.
    This class provides:
    - API key generation (used by admin endpoints)
    - Context dataclass for passing auth info between services
    - Serialization for logging
    """

    def __init__(self,
                 auth_type: str,
                 gateway_tenant_id: Optional[str] = None,
                 gateway_user_id: Optional[str] = None,
                 gateway_username: Optional[str] = None,
                 idp_user_id: Optional[str] = None,
                 idp_username: Optional[str] = None,
                 idp_user_email: Optional[str] = None,
                 api_key: Optional[str] = None,
                 access_token: Optional[str] = None,
                 client_ip: Optional[str] = None,
                 user_agent: Optional[str] = None,
                 api_key_context: Optional[APIKeyContext] = None):
        self.auth_type = auth_type
        self.gateway_tenant_id = gateway_tenant_id
        self.gateway_user_id = gateway_user_id or "anonymous"
        self.gateway_username = gateway_username or "anonymous"
        self.idp_user_id = idp_user_id or "anonymous"
        self.idp_username = idp_username or "anonymous"
        self.idp_user_email = idp_user_email or "anonymous"
        self.api_key = api_key
        self.access_token = access_token
        self.client_ip = client_ip
        self.user_agent = user_agent
        self.session_id = str(uuid.uuid4())
        self.api_key_context = api_key_context

    # ========================================================================
    # API Key Generation
    # ========================================================================

    @staticmethod
    def generate_api_key() -> Tuple[str, str, str]:
        """
        Generate a secure API key.

        Returns:
            Tuple of (full_key, key_hash, key_prefix)

        Example:
            full_key:   sk_brutor_api_EXAMPLE-not-a-real-key-0123456789abcdefABCD
            key_hash:   a3b2c1d4... (SHA-256, stored in database)
            key_prefix: sk_brutor_api_EXAMPL... (for display in admin UI)
        """
        random_string = secrets.token_urlsafe(32)
        full_key = f"{HttpHeaders.BEARER_API_KEY_PREFIX.value}{random_string}"
        key_hash = hashlib.sha256(full_key.encode()).hexdigest()
        key_prefix = full_key[:20] + "..."

        return full_key, key_hash, key_prefix

    # ========================================================================
    # Properties
    # ========================================================================

    @property
    def user_type(self) -> str:
        """Get the user type for logging"""
        return self.auth_type

    def get_stable_identifier(self) -> str:
        """
        Get a stable identifier for session key generation.

        Priority:
        1. idp_username (OAuth user for MCP server auth)
        2. gateway_username (Gateway user from JWT)
        3. api_key_context key ID
        4. api_key hash
        5. client_ip
        6. anonymous
        """
        if self.idp_username and self.idp_username != "anonymous":
            return f"user:{self.idp_username}"

        if self.gateway_username and self.gateway_username != "anonymous":
            return f"portal:{self.gateway_username}"

        if self.api_key_context:
            key_id = self.api_key_context.key_id if self.api_key_context.key_id else "unknown"
            return f"apikey:{key_id}"

        if self.api_key:
            return f"apikey:{hashlib.sha256(self.api_key.encode()).hexdigest()[:16]}"

        if self.client_ip:
            return f"ip:{self.client_ip}"

        return "anonymous"

    # ========================================================================
    # Serialization
    # ========================================================================

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging"""
        result = {
            "user_type": self.user_type,
            "gateway_username": self.gateway_username,
            "gateway_tenant_id": self.gateway_tenant_id,
            "gateway_user_id": self.gateway_user_id,
            "idp_username": self.idp_username,
            "idp_user_id": self.idp_user_id,
            "idp_user_email": self.idp_user_email,
            "api_key": self.api_key,
            "session_id": self.session_id,
            "client_ip": self.client_ip,
            "user_agent": self.user_agent,
            "has_api_key": self.api_key is not None,
            "has_access_token": self.access_token is not None,
        }

        if self.api_key_context:
            result["api_key_id"] = self.api_key_context.key_id
            result["api_key_name"] = self.api_key_context.key_name
            result["api_key_scope"] = self.api_key_context.scope

        return result

    def get_logging_context(self) -> Dict[str, Any]:
        """Get user context formatted for structured logging"""
        context = {
            "gateway_user": self.gateway_username,
            "gateway_tenant": self.gateway_tenant_id,
            "idp_user": self.idp_username if self.idp_username != "anonymous" else None,
            "user_type": self.user_type,
            "session_id": self.session_id,
            "client_ip": self.client_ip,
            "has_gateway_auth": self.gateway_username != "anonymous",
            "has_idp_auth": self.idp_username != "anonymous",
            "has_api_key": self.api_key is not None,
        }

        if self.api_key_context:
            context["api_key_id"] = self.api_key_context.key_id
            context["api_key_name"] = self.api_key_context.key_name
            context["api_key_scope"] = self.api_key_context.scope
            context["has_api_key"] = True

        return context
