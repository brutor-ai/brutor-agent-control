# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
A2A Protocol v1.0 Python support — Pydantic mirror of the Rust
`services::a2a_v1` module.

See `docs/a2a/v1.0.proto` + `docs/a2a/v1.0-specification.md` (pinned
at release tag v1.0.0, 2026-03-12) for the canonical wire contract.
"""

from app.services.a2a_v1.models import (
    AgentCard,
    AgentCardSignature,
    AgentCapabilities,
    AgentExtension,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    AuthenticationInfo,
    Artifact,
    AuthorizationCodeOAuthFlow,
    ClientCredentialsOAuthFlow,
    DeviceCodeOAuthFlow,
    ImplicitOAuthFlow,
    MutualTlsSecurityScheme,
    Message,
    OAuth2SecurityScheme,
    OAuthFlows,
    OpenIdConnectSecurityScheme,
    APIKeySecurityScheme,
    HTTPAuthSecurityScheme,
    Part,
    PartContent,
    PasswordOAuthFlow,
    Role,
    SecurityRequirement,
    SecurityScheme,
    StringList,
    Task,
    TaskArtifactUpdateEvent,
    TaskPushNotificationConfig,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    BRUTOR_DELEGATION_EXTENSION_URI,
)
from app.services.a2a_v1.validator import (
    validate_card_v1,
    InvalidV1CardError,
)
from app.services.a2a_v1.translator import (
    is_v1_card,
    is_v03_card,
    translate_v03_to_v1,
)

__all__ = [
    "AgentCard",
    "AgentCardSignature",
    "AgentCapabilities",
    "AgentExtension",
    "AgentInterface",
    "AgentProvider",
    "AgentSkill",
    "AuthenticationInfo",
    "Artifact",
    "AuthorizationCodeOAuthFlow",
    "ClientCredentialsOAuthFlow",
    "DeviceCodeOAuthFlow",
    "ImplicitOAuthFlow",
    "MutualTlsSecurityScheme",
    "Message",
    "OAuth2SecurityScheme",
    "OAuthFlows",
    "OpenIdConnectSecurityScheme",
    "APIKeySecurityScheme",
    "HTTPAuthSecurityScheme",
    "Part",
    "PartContent",
    "PasswordOAuthFlow",
    "Role",
    "SecurityRequirement",
    "SecurityScheme",
    "StringList",
    "Task",
    "TaskArtifactUpdateEvent",
    "TaskPushNotificationConfig",
    "TaskState",
    "TaskStatus",
    "TaskStatusUpdateEvent",
    "BRUTOR_DELEGATION_EXTENSION_URI",
    "validate_card_v1",
    "InvalidV1CardError",
    "is_v1_card",
    "is_v03_card",
    "translate_v03_to_v1",
]
