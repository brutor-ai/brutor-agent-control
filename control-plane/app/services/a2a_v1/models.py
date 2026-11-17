# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
A2A Protocol v1.0 Pydantic models — Python mirror of the Rust
`services::a2a_v1` types in `brutor-gateway-core`.

Wire rules (A2A v1.0 specification):

  * Field names are lowerCamelCase on the wire, snake_case in Python.
    Every model sets `populate_by_name=True` and `alias_generator=to_camel`.
  * `extra='forbid'` — unknown fields raise. This is the Python
    counterpart of Rust's `#[serde(deny_unknown_fields)]` and catches
    partner drift at the API boundary instead of silently dropping data.
  * Oneofs (`Part.content`, `SendMessageResponse.payload`,
    `StreamResponse.payload`, `OAuthFlows.flow`, `SecurityScheme.scheme`)
    are modelled as sets of `Optional[...]` fields with a
    `model_validator` that enforces "exactly one is set". That keeps
    the wire shape identical to proto3-JSON's member-presence encoding
    while giving callers a typed way to inspect which variant arrived.

JWS signing + JCS canonicalization use `cryptography` (for Ed25519) and
`rfc8785` (for canonicalization) on the Python side — those live in
`app/services/a2a/card_signing.py`.
"""

from __future__ import annotations

import base64 as _base64
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Dict, List, Optional

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel


def _b64_decode_if_string(value: Any) -> Any:
    """Input validator for the `Part.raw` type.

    * `str` → base64-decode to raw `bytes` (the wire form).
    * `bytes` → pass through unchanged (the Python-constructor form).
    * `None` → pass through (field is Optional).
    * Anything else → hand back so Pydantic raises a type error.

    Kept narrow on purpose: we only want to transform strings that are
    definitely base64. A `str` that isn't valid base64 raises a
    ValueError with a helpful message, matching the Rust
    `JcsError::BadSignatureEncoding` / proto3-JSON behaviour.
    """
    if isinstance(value, str):
        try:
            return _base64.b64decode(value, validate=True)
        except Exception as e:  # binascii.Error or ValueError
            raise ValueError(f"Part.raw is not valid base64: {e}") from e
    return value


def _b64_encode_to_string(value: bytes) -> str:
    """Serializer — emit standard base64 (with padding) per proto3-JSON."""
    return _base64.b64encode(value).decode("ascii")


# `Part.raw` annotation. Stores raw `bytes` in Python; accepts base64
# `str` on the wire; emits base64 string when serialized to JSON. Any
# other input type is rejected by Pydantic's bytes validator.
RawBytes = Annotated[
    bytes,
    BeforeValidator(_b64_decode_if_string),
    PlainSerializer(_b64_encode_to_string, return_type=str, when_used="json"),
]


# =============================================================================
# Extension URI — Brutor delegation chain
# =============================================================================

BRUTOR_DELEGATION_EXTENSION_URI: str = (
    "https://brutor.ai/a2a/extensions/delegation-chain/v1"
)


# =============================================================================
# Base model — one place to keep the wire + strictness rules
# =============================================================================


class _A2AModel(BaseModel):
    """Common base class: camelCase aliases + unknown-field rejection."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        # Serialize enum values as their string forms; matches the
        # proto3-JSON enum encoding the spec mandates.
        use_enum_values=True,
    )


# =============================================================================
# Enums
# =============================================================================


class Role(str, Enum):
    """Proto `enum Role`; wire form is SCREAMING_SNAKE_CASE."""

    ROLE_UNSPECIFIED = "ROLE_UNSPECIFIED"
    ROLE_USER = "ROLE_USER"
    ROLE_AGENT = "ROLE_AGENT"


class TaskState(str, Enum):
    """Proto `enum TaskState`; wire form is SCREAMING_SNAKE_CASE."""

    TASK_STATE_UNSPECIFIED = "TASK_STATE_UNSPECIFIED"
    TASK_STATE_SUBMITTED = "TASK_STATE_SUBMITTED"
    TASK_STATE_WORKING = "TASK_STATE_WORKING"
    TASK_STATE_COMPLETED = "TASK_STATE_COMPLETED"
    TASK_STATE_FAILED = "TASK_STATE_FAILED"
    TASK_STATE_CANCELED = "TASK_STATE_CANCELED"
    TASK_STATE_INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
    TASK_STATE_REJECTED = "TASK_STATE_REJECTED"
    TASK_STATE_AUTH_REQUIRED = "TASK_STATE_AUTH_REQUIRED"

    def is_terminal(self) -> bool:
        return self in (
            TaskState.TASK_STATE_COMPLETED,
            TaskState.TASK_STATE_FAILED,
            TaskState.TASK_STATE_CANCELED,
            TaskState.TASK_STATE_REJECTED,
        )

    def is_interrupted(self) -> bool:
        return self in (
            TaskState.TASK_STATE_INPUT_REQUIRED,
            TaskState.TASK_STATE_AUTH_REQUIRED,
        )


# =============================================================================
# Part — the member-presence oneof
# =============================================================================


class PartContent(str, Enum):
    """Discriminator for the parsed [`Part`] content variant.

    Pydantic uses the four optional fields on [`Part`] for wire
    round-tripping; this enum makes post-validation code readable:
    `part.content == PartContent.TEXT`.
    """

    TEXT = "text"
    RAW = "raw"
    URL = "url"
    DATA = "data"


class Part(_A2AModel):
    """A2A v1.0 `Part` — one section of communication content.

    Wire form: exactly one of `text`/`raw`/`url`/`data` alongside
    optional `filename`, `mediaType`, `metadata`. Proto3-JSON's
    oneof encoding lifts the selected field to the envelope, so
    member presence *is* the discriminator.

    `raw` bytes are base64 on the wire (standard base64 per proto3-JSON
    for `bytes`). Pydantic's `bytes` type accepts base64-encoded
    strings on input; we rely on that here.
    """

    # Oneof variants — exactly one MUST be set.
    #
    # `raw` uses our [`RawBytes`] annotation so wire input/output
    # matches the proto3-JSON `bytes` encoding (standard base64, with
    # padding) while Python callers can still construct `Part(raw=b"…")`
    # with raw bytes.
    text: Optional[str] = None
    raw: Optional[RawBytes] = None
    url: Optional[str] = None
    data: Optional[Any] = None

    # Shared fields.
    filename: Optional[str] = None
    media_type: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

    @model_validator(mode="after")
    def _exactly_one_content(self) -> "Part":
        present = sum(
            1
            for v in (self.text, self.raw, self.url, self.data)
            if v is not None
        )
        if present == 0:
            raise ValueError(
                "Part must set exactly one of text/raw/url/data (none supplied)"
            )
        if present > 1:
            raise ValueError(
                "Part must set exactly one of text/raw/url/data (multiple supplied)"
            )
        return self

    # Helper for Python callers who find "which variant is set?" ugly.
    @property
    def content(self) -> PartContent:
        if self.text is not None:
            return PartContent.TEXT
        if self.raw is not None:
            return PartContent.RAW
        if self.url is not None:
            return PartContent.URL
        return PartContent.DATA


# =============================================================================
# Message + Artifact
# =============================================================================


class Message(_A2AModel):
    """A2A v1.0 `Message`. Required: message_id, role, parts."""

    message_id: str
    context_id: Optional[str] = None
    task_id: Optional[str] = None
    role: Role
    parts: List[Part]
    metadata: Optional[Dict[str, Any]] = None
    extensions: List[str] = Field(default_factory=list)
    reference_task_ids: List[str] = Field(default_factory=list)

    @field_validator("parts")
    @classmethod
    def _at_least_one_part(cls, v: List[Part]) -> List[Part]:
        if not v:
            raise ValueError("Message.parts must contain at least one Part")
        return v


class Artifact(_A2AModel):
    artifact_id: str
    name: Optional[str] = None
    description: Optional[str] = None
    parts: List[Part]
    metadata: Optional[Dict[str, Any]] = None
    extensions: List[str] = Field(default_factory=list)

    @field_validator("parts")
    @classmethod
    def _at_least_one_part(cls, v: List[Part]) -> List[Part]:
        if not v:
            raise ValueError("Artifact.parts must contain at least one Part")
        return v


# =============================================================================
# Task + TaskStatus
# =============================================================================


class TaskStatus(_A2AModel):
    state: TaskState
    message: Optional[Message] = None
    timestamp: Optional[datetime] = None


class Task(_A2AModel):
    id: str
    context_id: Optional[str] = None
    status: TaskStatus
    artifacts: List[Artifact] = Field(default_factory=list)
    history: List[Message] = Field(default_factory=list)
    metadata: Optional[Dict[str, Any]] = None


# =============================================================================
# Stream events
# =============================================================================


class TaskStatusUpdateEvent(_A2AModel):
    task_id: str
    context_id: str
    status: TaskStatus
    metadata: Optional[Dict[str, Any]] = None


class TaskArtifactUpdateEvent(_A2AModel):
    task_id: str
    context_id: str
    artifact: Artifact
    append: bool = False
    last_chunk: bool = False
    metadata: Optional[Dict[str, Any]] = None


# =============================================================================
# Agent interface, provider, capabilities, extensions
# =============================================================================


class AgentInterface(_A2AModel):
    """One supported (url, binding, version) triple. Replaces v0.3's
    top-level url + protocolVersion + preferredTransport."""

    url: str
    protocol_binding: str
    tenant: Optional[str] = None
    protocol_version: str


class AgentProvider(_A2AModel):
    url: str
    organization: str


class AgentExtension(_A2AModel):
    uri: str
    description: Optional[str] = None
    required: bool = False
    params: Optional[Dict[str, Any]] = None


class AgentCapabilities(_A2AModel):
    streaming: Optional[bool] = None
    push_notifications: Optional[bool] = None
    extensions: List[AgentExtension] = Field(default_factory=list)
    extended_agent_card: Optional[bool] = None


# =============================================================================
# Security schemes (oneof) + requirements
# =============================================================================


class StringList(_A2AModel):
    """Wire form of proto `message StringList`. Always emitted as
    `{"list": ["scope1", ...]}`."""

    list: List[str] = Field(default_factory=list)


class SecurityRequirement(_A2AModel):
    """Map of scheme name → required scopes (wrapped in StringList)."""

    schemes: Dict[str, StringList]


class APIKeySecurityScheme(_A2AModel):
    description: Optional[str] = None
    location: str
    name: str


class HTTPAuthSecurityScheme(_A2AModel):
    description: Optional[str] = None
    scheme: str
    bearer_format: Optional[str] = None


class AuthorizationCodeOAuthFlow(_A2AModel):
    authorization_url: str
    token_url: str
    refresh_url: Optional[str] = None
    scopes: Dict[str, str]
    # Proto default `false`; RFC 7636 mandates `true` in practice.
    pkce_required: bool = False


class ClientCredentialsOAuthFlow(_A2AModel):
    token_url: str
    refresh_url: Optional[str] = None
    scopes: Dict[str, str]


class ImplicitOAuthFlow(_A2AModel):
    """Deprecated in v1.0 — retained for interop reads only."""

    authorization_url: Optional[str] = None
    refresh_url: Optional[str] = None
    scopes: Dict[str, str] = Field(default_factory=dict)


class PasswordOAuthFlow(_A2AModel):
    """Deprecated in v1.0 — retained for interop reads only."""

    token_url: Optional[str] = None
    refresh_url: Optional[str] = None
    scopes: Dict[str, str] = Field(default_factory=dict)


class DeviceCodeOAuthFlow(_A2AModel):
    device_authorization_url: str
    token_url: str
    refresh_url: Optional[str] = None
    scopes: Dict[str, str]


class OAuthFlows(_A2AModel):
    """Oneof — exactly one flow variant set.

    Wire encoding: the selected variant appears under its camelCase
    key in the enclosing OAuth2SecurityScheme's `flows` object.
    """

    authorization_code: Optional[AuthorizationCodeOAuthFlow] = None
    client_credentials: Optional[ClientCredentialsOAuthFlow] = None
    implicit: Optional[ImplicitOAuthFlow] = None
    password: Optional[PasswordOAuthFlow] = None
    device_code: Optional[DeviceCodeOAuthFlow] = None

    @model_validator(mode="after")
    def _exactly_one_flow(self) -> "OAuthFlows":
        present = sum(
            1
            for v in (
                self.authorization_code,
                self.client_credentials,
                self.implicit,
                self.password,
                self.device_code,
            )
            if v is not None
        )
        if present != 1:
            raise ValueError(
                "OAuthFlows must set exactly one of "
                "authorizationCode/clientCredentials/implicit/password/deviceCode"
            )
        return self


class OAuth2SecurityScheme(_A2AModel):
    description: Optional[str] = None
    flows: OAuthFlows
    oauth2_metadata_url: Optional[str] = None


class OpenIdConnectSecurityScheme(_A2AModel):
    description: Optional[str] = None
    open_id_connect_url: str


class MutualTlsSecurityScheme(_A2AModel):
    description: Optional[str] = None


class SecurityScheme(_A2AModel):
    """Oneof — exactly one scheme variant set.

    Wire encoding matches the proto oneof: exactly one of the five
    camelCase keys appears alongside the rest of the enclosing
    AgentCard.securitySchemes[name] entry.
    """

    api_key_security_scheme: Optional[APIKeySecurityScheme] = None
    http_auth_security_scheme: Optional[HTTPAuthSecurityScheme] = None
    oauth2_security_scheme: Optional[OAuth2SecurityScheme] = None
    open_id_connect_security_scheme: Optional[OpenIdConnectSecurityScheme] = None
    mtls_security_scheme: Optional[MutualTlsSecurityScheme] = None

    @model_validator(mode="after")
    def _exactly_one_scheme(self) -> "SecurityScheme":
        present = sum(
            1
            for v in (
                self.api_key_security_scheme,
                self.http_auth_security_scheme,
                self.oauth2_security_scheme,
                self.open_id_connect_security_scheme,
                self.mtls_security_scheme,
            )
            if v is not None
        )
        if present != 1:
            raise ValueError(
                "SecurityScheme must set exactly one of "
                "apiKey/httpAuth/oauth2/openIdConnect/mtls"
            )
        return self


# =============================================================================
# Agent Card + skill + signature
# =============================================================================


class AgentSkill(_A2AModel):
    id: str
    name: str
    description: str
    tags: List[str]
    examples: List[str] = Field(default_factory=list)
    input_modes: List[str] = Field(default_factory=list)
    output_modes: List[str] = Field(default_factory=list)
    security_requirements: List[SecurityRequirement] = Field(default_factory=list)


class AgentCardSignature(_A2AModel):
    """Flattened JWS JSON Serialization of a signature over the card.

    `protected` is base64url(JSON) of the protected header; `signature`
    is base64url-raw of the 64-byte Ed25519 signature; `header` is an
    optional unprotected header object.
    """

    protected: str
    signature: str
    header: Optional[Dict[str, Any]] = None


class AgentCard(_A2AModel):
    name: str
    description: str
    supported_interfaces: List[AgentInterface]
    provider: Optional[AgentProvider] = None
    version: str
    documentation_url: Optional[str] = None
    capabilities: AgentCapabilities
    security_schemes: Dict[str, SecurityScheme] = Field(default_factory=dict)
    security_requirements: List[SecurityRequirement] = Field(default_factory=list)
    default_input_modes: List[str]
    default_output_modes: List[str]
    skills: List[AgentSkill]
    signatures: List[AgentCardSignature] = Field(default_factory=list)
    icon_url: Optional[str] = None

    @field_validator("supported_interfaces")
    @classmethod
    def _at_least_one_interface(
        cls, v: List[AgentInterface]
    ) -> List[AgentInterface]:
        if not v:
            raise ValueError(
                "AgentCard.supportedInterfaces must contain at least one interface"
            )
        return v


# =============================================================================
# Authentication / push-notification support
# =============================================================================


class AuthenticationInfo(_A2AModel):
    scheme: str
    credentials: Optional[str] = None


class TaskPushNotificationConfig(_A2AModel):
    tenant: Optional[str] = None
    id: Optional[str] = None
    task_id: Optional[str] = None
    url: str
    token: Optional[str] = None
    authentication: Optional[AuthenticationInfo] = None
