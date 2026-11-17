// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # A2A Protocol v1.0 types
//!
//! Rust representation of every message + enum in the canonical A2A
//! v1.0 proto file of the A2A v1.0.0 release (proto:
//! <https://github.com/a2aproject/A2A/blob/v1.0.0/specification/a2a.proto>, specification:
//! <https://github.com/a2aproject/A2A/blob/v1.0.0/docs/specification.md>).
//!
//! ## Wire format
//!
//! A2A uses proto3-JSON encoding with the standard snake_case →
//! **lowerCamelCase** field-name mapping (spec §"JSON Serialization"). Every struct below carries
//! `#[serde(rename_all = "camelCase")]` so round-trips match the
//! canonical wire format byte-for-byte. We additionally set
//! `deny_unknown_fields` so incoming traffic from a mis-versioned
//! partner fails loudly rather than silently dropping data.
//!
//! ## Oneof handling
//!
//! Two proto oneofs have special treatment:
//!
//!   * `Part.content` — proto3-JSON for oneofs serialises the selected
//!     variant as a top-level field, NOT under a wrapper. Encoded via
//!     custom serde impls in [`part_codec`] so `{"text": "hi"}` and
//!     `{"url": "…"}` round-trip correctly. Exactly one of
//!     `text`/`raw`/`url`/`data` must be present.
//!   * `SendMessageResponse.payload` and `StreamResponse.payload` —
//!     same treatment via [`response_codec`].
//!
//! ## Timestamps
//!
//! Spec mandates ISO 8601 UTC with millisecond precision
//! (`YYYY-MM-DDTHH:MM:SS.sssZ`). The `chrono::DateTime<Utc>` default
//! emits nanoseconds, which is over-precise but spec-compatible
//! (spec says "at least" millisecond); we truncate on round-trip
//! helpers below to stay canonical.
//!
//! ## Extensibility
//!
//! Our HMAC-signed delegation chain is registered as an A2A extension
//! advertised via [`AgentCapabilities::extensions`]. Its URI is
//! [`BRUTOR_DELEGATION_EXTENSION_URI`]. Cards that rely on it MUST
//! set `required = true` so gateways other than this one reject rather than
//! silently drop the headers.

use std::collections::BTreeMap;

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

pub mod errors;
pub mod guardrail;
pub mod jcs;
pub mod jwks;
pub mod jws;
pub mod part_codec;
pub mod push_store;
pub mod response_codec;
pub mod stream_hub;
pub mod task_store;
pub mod translator;
pub mod version;

#[cfg(test)]
mod tests;

/// URI we publish under `AgentCapabilities.extensions[].uri` for the
/// Brutor HMAC-signed delegation chain. Format follows the proto
/// "opaque URI" recommendation. Versioned so we can ship a successor
/// extension later without breaking existing consumers.
pub const BRUTOR_DELEGATION_EXTENSION_URI: &str =
    "https://brutor.ai/a2a/extensions/delegation-chain/v1";

// =============================================================================
// Enums
// =============================================================================

/// `Role` — sender of a message. See proto `enum Role`.
///
/// Serialised as SCREAMING_SNAKE_CASE strings per the v1.0 wire
/// format. `ROLE_UNSPECIFIED` is the proto zero value and MUST NOT
/// appear on a real Message; we include it for completeness so
/// deserialization of a malformed payload fails cleanly rather than
/// producing a silent default.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Role {
    /// The role is unspecified.
    #[serde(alias = "unspecified")]
    RoleUnspecified,
    /// The message is from the client to the server.
    ///
    /// Aliases accept the lowercase short forms used by upstream
    /// reference clients (Google a2a-samples, the Python a2a-sdk's
    /// `new_text_message`) so external A2A v1.0 clients
    /// interoperate without a wire-format adapter.
    #[serde(alias = "user")]
    RoleUser,
    /// The message is from the server to the client.
    #[serde(alias = "agent")]
    RoleAgent,
}

/// `TaskState` — lifecycle state of a Task. See proto `enum TaskState`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum TaskState {
    /// Unknown or indeterminate.
    TaskStateUnspecified,
    /// Submitted + acknowledged.
    TaskStateSubmitted,
    /// Actively being processed by the agent.
    TaskStateWorking,
    /// Finished successfully. Terminal.
    TaskStateCompleted,
    /// Finished with an error. Terminal.
    TaskStateFailed,
    /// Canceled before completion. Terminal.
    TaskStateCanceled,
    /// Needs additional user input. Interrupted.
    TaskStateInputRequired,
    /// Agent declined the task. Terminal.
    TaskStateRejected,
    /// Needs authentication to proceed. Interrupted.
    TaskStateAuthRequired,
}

impl TaskState {
    /// Terminal states: the task is done and will not move again.
    pub fn is_terminal(self) -> bool {
        matches!(
            self,
            TaskState::TaskStateCompleted
                | TaskState::TaskStateFailed
                | TaskState::TaskStateCanceled
                | TaskState::TaskStateRejected
        )
    }

    /// Interrupted states: the task is paused pending a signal.
    pub fn is_interrupted(self) -> bool {
        matches!(
            self,
            TaskState::TaskStateInputRequired | TaskState::TaskStateAuthRequired
        )
    }
}

// =============================================================================
// Part — the oneof at the heart of every Message/Artifact
// =============================================================================

/// The discriminant of a [`Part`].
///
/// proto3-JSON encodes oneofs by lifting the selected variant to a
/// top-level field. We expose [`PartContent`] as a strongly-typed
/// tagged union; the custom serde impls in [`part_codec`] translate
/// between the two representations.
#[derive(Debug, Clone, PartialEq)]
pub enum PartContent {
    /// Inline UTF-8 text. Corresponds to proto `string text = 1`.
    Text(String),
    /// Inline binary content. Base64-encoded on the wire.
    /// Corresponds to proto `bytes raw = 2`.
    Raw(Vec<u8>),
    /// URL pointing to the file's content.
    /// Corresponds to proto `string url = 3`.
    Url(String),
    /// Arbitrary structured data (any JSON value).
    /// Corresponds to proto `google.protobuf.Value data = 4`.
    Data(serde_json::Value),
}

/// `Part` — a single section of communication content.
///
/// Exactly one of `Text`/`Raw`/`Url`/`Data` is set via [`PartContent`].
/// `filename` and `media_type` are optional and apply to all variants.
/// `metadata` is a free-form JSON object.
#[derive(Debug, Clone, PartialEq)]
pub struct Part {
    pub content: PartContent,
    pub metadata: Option<serde_json::Map<String, serde_json::Value>>,
    pub filename: Option<String>,
    pub media_type: Option<String>,
}

// =============================================================================
// Message
// =============================================================================

/// `Message` — one unit of communication. See proto `message Message`.
///
/// Required fields (per `field_behavior = REQUIRED`): `message_id`,
/// `role`, `parts`. Missing any of those on deserialization must
/// yield an error.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Message {
    /// Unique identifier (e.g. UUID) of the message, set by the creator.
    pub message_id: String,
    /// Optional. Context id grouping related tasks/messages.
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub context_id: Option<String>,
    /// Optional. Task this message belongs to.
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub task_id: Option<String>,
    /// Required. Sender role.
    pub role: Role,
    /// Required. One-or-more content parts.
    pub parts: Vec<Part>,
    /// Optional. Free-form metadata.
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub metadata: Option<serde_json::Map<String, serde_json::Value>>,
    /// Extension URIs present on this Message.
    #[serde(skip_serializing_if = "Vec::is_empty", default)]
    pub extensions: Vec<String>,
    /// Referenced task IDs for additional context.
    #[serde(skip_serializing_if = "Vec::is_empty", default)]
    pub reference_task_ids: Vec<String>,
}

// =============================================================================
// Artifact
// =============================================================================

/// `Artifact` — task output. See proto `message Artifact`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Artifact {
    /// Unique within the owning Task.
    pub artifact_id: String,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub description: Option<String>,
    /// Required. Must contain at least one part.
    pub parts: Vec<Part>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub metadata: Option<serde_json::Map<String, serde_json::Value>>,
    #[serde(skip_serializing_if = "Vec::is_empty", default)]
    pub extensions: Vec<String>,
}

// =============================================================================
// Task + TaskStatus
// =============================================================================

/// `TaskStatus` — see proto `message TaskStatus`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct TaskStatus {
    pub state: TaskState,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub message: Option<Message>,
    /// ISO 8601 timestamp (spec: millisecond precision minimum).
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub timestamp: Option<DateTime<Utc>>,
}

/// `Task` — see proto `message Task`.
///
/// Required fields: `id`, `status`. `context_id` is optional per the
/// proto but populated in virtually every real response; the spec
/// (§"Context Management") requires agents to include it when set.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Task {
    pub id: String,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub context_id: Option<String>,
    pub status: TaskStatus,
    #[serde(skip_serializing_if = "Vec::is_empty", default)]
    pub artifacts: Vec<Artifact>,
    #[serde(skip_serializing_if = "Vec::is_empty", default)]
    pub history: Vec<Message>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub metadata: Option<serde_json::Map<String, serde_json::Value>>,
}

// =============================================================================
// Stream events
// =============================================================================

/// `TaskStatusUpdateEvent` — see proto.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct TaskStatusUpdateEvent {
    pub task_id: String,
    pub context_id: String,
    pub status: TaskStatus,
    /// A2A v1.0 stream-termination signal: the LAST status frame carries
    /// `final: true`, after which the server closes the connection. Set on the
    /// terminal-state transition AND on the duration-cap / shutdown paths so
    /// clients always get a clean end instead of a silent socket close (it was
    /// previously absent entirely).
    #[serde(rename = "final", default)]
    pub final_: bool,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub metadata: Option<serde_json::Map<String, serde_json::Value>>,
}

/// `TaskArtifactUpdateEvent` — see proto.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct TaskArtifactUpdateEvent {
    pub task_id: String,
    pub context_id: String,
    pub artifact: Artifact,
    /// Append this chunk to a previously-sent artifact with the same
    /// ID. Proto default is `false`.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub append: bool,
    /// Final chunk of the artifact.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub last_chunk: bool,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub metadata: Option<serde_json::Map<String, serde_json::Value>>,
}

// =============================================================================
// Authentication + AgentInterface + AgentCard
// =============================================================================

/// `AuthenticationInfo` — see proto.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AuthenticationInfo {
    /// HTTP auth scheme (IANA registry). Case-insensitive per RFC 9110.
    pub scheme: String,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub credentials: Option<String>,
}

/// `AgentInterface` — one supported combination of URL + binding +
/// protocol version. See proto.
///
/// The three known protocol bindings are `JSONRPC`, `GRPC`, and
/// `HTTP+JSON`. The proto leaves the field open-form so new bindings
/// can be declared by extensions.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AgentInterface {
    pub url: String,
    pub protocol_binding: String,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub tenant: Option<String>,
    pub protocol_version: String,
}

/// `AgentProvider` — see proto.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AgentProvider {
    pub url: String,
    pub organization: String,
}

/// `AgentExtension` — see proto.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AgentExtension {
    pub uri: String,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub description: Option<String>,
    /// If `true`, the client MUST understand this extension.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub required: bool,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub params: Option<serde_json::Map<String, serde_json::Value>>,
}

/// `AgentCapabilities` — see proto.
#[derive(Debug, Clone, PartialEq, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AgentCapabilities {
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub streaming: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub push_notifications: Option<bool>,
    #[serde(skip_serializing_if = "Vec::is_empty", default)]
    pub extensions: Vec<AgentExtension>,
    /// Replaces the v0.3 top-level `supportsAuthenticatedExtendedCard`.
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub extended_agent_card: Option<bool>,
}

/// `AgentSkill` — see proto.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AgentSkill {
    pub id: String,
    pub name: String,
    pub description: String,
    pub tags: Vec<String>,
    #[serde(skip_serializing_if = "Vec::is_empty", default)]
    pub examples: Vec<String>,
    #[serde(skip_serializing_if = "Vec::is_empty", default)]
    pub input_modes: Vec<String>,
    #[serde(skip_serializing_if = "Vec::is_empty", default)]
    pub output_modes: Vec<String>,
    #[serde(skip_serializing_if = "Vec::is_empty", default)]
    pub security_requirements: Vec<SecurityRequirement>,
}

/// `AgentCardSignature` — RFC 7515 JWS JSON Serialization.
///
/// `protected` is a base64url-encoded JSON object (the protected
/// JOSE header); `signature` is base64url-encoded bytes; `header`
/// holds the unprotected header as a JSON object (typically used
/// for `kid` in multi-signature scenarios).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AgentCardSignature {
    pub protected: String,
    pub signature: String,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub header: Option<serde_json::Map<String, serde_json::Value>>,
}

/// `AgentCard` — the root self-describing manifest. See proto `message AgentCard`.
///
/// Required fields: `name`, `description`, `supported_interfaces`,
/// `version`, `capabilities`, `default_input_modes`,
/// `default_output_modes`, `skills`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AgentCard {
    pub name: String,
    pub description: String,
    pub supported_interfaces: Vec<AgentInterface>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub provider: Option<AgentProvider>,
    pub version: String,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub documentation_url: Option<String>,
    pub capabilities: AgentCapabilities,
    /// Map of scheme name → SecurityScheme. Serialised as a JSON
    /// object; BTreeMap keeps iteration ordered for reproducible
    /// canonicalization during signing.
    #[serde(skip_serializing_if = "BTreeMap::is_empty", default)]
    pub security_schemes: BTreeMap<String, SecurityScheme>,
    #[serde(skip_serializing_if = "Vec::is_empty", default)]
    pub security_requirements: Vec<SecurityRequirement>,
    pub default_input_modes: Vec<String>,
    pub default_output_modes: Vec<String>,
    pub skills: Vec<AgentSkill>,
    #[serde(skip_serializing_if = "Vec::is_empty", default)]
    pub signatures: Vec<AgentCardSignature>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub icon_url: Option<String>,
}

// =============================================================================
// Security schemes — OpenAPI 3.2 discriminated union
// =============================================================================

/// `SecurityRequirement` — map of scheme name → required scopes.
///
/// Proto form: `map<string, StringList>`. We inline the `StringList`
/// wrapper because the only purpose of that wrapper in proto3 is to
/// make `map<string, repeated string>` expressible. On the JSON wire
/// the value is a direct array of scope strings per the proto3-JSON
/// rules for `map<K,V>` where V has a single-field `list` sub-message.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct SecurityRequirement {
    /// Map of scheme name → StringList wrapper.
    pub schemes: BTreeMap<String, StringList>,
}

/// Wrapper matching proto `message StringList`. Wire form is always
/// `{"list": ["scope1", "scope2"]}`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct StringList {
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub list: Vec<String>,
}

/// `SecurityScheme` — oneof across the five OpenAPI 3.2 scheme kinds.
///
/// proto3-JSON serialises the selected variant at the top level (same
/// technique as [`Part`]). The oneof field name prefixes are preserved
/// in `camelCase` form on the wire; we use serde's tag/content-less
/// untagged deserialization with explicit discrimination through
/// `#[serde(rename)]` on each variant.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub enum SecurityScheme {
    /// API key in header/query/cookie.
    #[serde(rename = "apiKeySecurityScheme")]
    ApiKey(APIKeySecurityScheme),
    /// HTTP auth (Basic, Bearer, Digest...).
    #[serde(rename = "httpAuthSecurityScheme")]
    HttpAuth(HTTPAuthSecurityScheme),
    /// OAuth 2.0.
    #[serde(rename = "oauth2SecurityScheme")]
    OAuth2(OAuth2SecurityScheme),
    /// OIDC.
    #[serde(rename = "openIdConnectSecurityScheme")]
    OpenIdConnect(OpenIdConnectSecurityScheme),
    /// Mutual TLS.
    #[serde(rename = "mtlsSecurityScheme")]
    MutualTls(MutualTlsSecurityScheme),
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct APIKeySecurityScheme {
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub description: Option<String>,
    /// "query", "header", or "cookie".
    pub location: String,
    pub name: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct HTTPAuthSecurityScheme {
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub description: Option<String>,
    pub scheme: String,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub bearer_format: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct OAuth2SecurityScheme {
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub description: Option<String>,
    pub flows: OAuthFlows,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub oauth2_metadata_url: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct OpenIdConnectSecurityScheme {
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub description: Option<String>,
    pub open_id_connect_url: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct MutualTlsSecurityScheme {
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub description: Option<String>,
}

/// `OAuthFlows` — discriminated union of the five configured flows.
/// Deprecated flows (`implicit`, `password`) are still decodable for
/// interop but we should not generate them on the outbound side.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub enum OAuthFlows {
    #[serde(rename = "authorizationCode")]
    AuthorizationCode(AuthorizationCodeOAuthFlow),
    #[serde(rename = "clientCredentials")]
    ClientCredentials(ClientCredentialsOAuthFlow),
    /// Deprecated in v1.0.
    #[serde(rename = "implicit")]
    Implicit(ImplicitOAuthFlow),
    /// Deprecated in v1.0.
    #[serde(rename = "password")]
    Password(PasswordOAuthFlow),
    #[serde(rename = "deviceCode")]
    DeviceCode(DeviceCodeOAuthFlow),
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AuthorizationCodeOAuthFlow {
    pub authorization_url: String,
    pub token_url: String,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub refresh_url: Option<String>,
    pub scopes: BTreeMap<String, String>,
    /// RFC 7636 PKCE. Proto default `false`, spec strongly recommends `true`.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub pkce_required: bool,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ClientCredentialsOAuthFlow {
    pub token_url: String,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub refresh_url: Option<String>,
    pub scopes: BTreeMap<String, String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ImplicitOAuthFlow {
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub authorization_url: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub refresh_url: Option<String>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub scopes: BTreeMap<String, String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct PasswordOAuthFlow {
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub token_url: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub refresh_url: Option<String>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub scopes: BTreeMap<String, String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct DeviceCodeOAuthFlow {
    pub device_authorization_url: String,
    pub token_url: String,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub refresh_url: Option<String>,
    pub scopes: BTreeMap<String, String>,
}

// =============================================================================
// Push notification config
// =============================================================================

/// `TaskPushNotificationConfig` — see proto.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct TaskPushNotificationConfig {
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub tenant: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub task_id: Option<String>,
    pub url: String,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub token: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub authentication: Option<AuthenticationInfo>,
}

// =============================================================================
// RPC requests / responses
// =============================================================================

/// `SendMessageConfiguration` — see proto.
#[derive(Debug, Clone, PartialEq, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct SendMessageConfiguration {
    #[serde(skip_serializing_if = "Vec::is_empty", default)]
    pub accepted_output_modes: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub task_push_notification_config: Option<TaskPushNotificationConfig>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub history_length: Option<i32>,
    /// Proto default `false`: MUST wait for terminal/interrupted state.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub return_immediately: bool,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct SendMessageRequest {
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub tenant: Option<String>,
    pub message: Message,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub configuration: Option<SendMessageConfiguration>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub metadata: Option<serde_json::Map<String, serde_json::Value>>,
}

/// `SendMessageResponse` — oneof payload. See [`response_codec`] for
/// serde glue.
#[derive(Debug, Clone, PartialEq)]
pub enum SendMessageResponse {
    Task(Task),
    Message(Message),
}

/// `StreamResponse` — oneof payload. See [`response_codec`].
#[derive(Debug, Clone, PartialEq)]
pub enum StreamResponse {
    Task(Task),
    Message(Message),
    StatusUpdate(TaskStatusUpdateEvent),
    ArtifactUpdate(TaskArtifactUpdateEvent),
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct GetTaskRequest {
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub tenant: Option<String>,
    pub id: String,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub history_length: Option<i32>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ListTasksRequest {
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub tenant: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub context_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub status: Option<TaskState>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub page_size: Option<i32>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub page_token: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub history_length: Option<i32>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub status_timestamp_after: Option<DateTime<Utc>>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub include_artifacts: Option<bool>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ListTasksResponse {
    pub tasks: Vec<Task>,
    pub next_page_token: String,
    pub page_size: i32,
    pub total_size: i32,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct CancelTaskRequest {
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub tenant: Option<String>,
    pub id: String,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub metadata: Option<serde_json::Map<String, serde_json::Value>>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct GetTaskPushNotificationConfigRequest {
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub tenant: Option<String>,
    pub task_id: String,
    pub id: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct DeleteTaskPushNotificationConfigRequest {
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub tenant: Option<String>,
    pub task_id: String,
    pub id: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct SubscribeToTaskRequest {
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub tenant: Option<String>,
    pub id: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ListTaskPushNotificationConfigsRequest {
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub tenant: Option<String>,
    pub task_id: String,
    #[serde(default, skip_serializing_if = "is_zero_i32")]
    pub page_size: i32,
    #[serde(skip_serializing_if = "String::is_empty", default)]
    pub page_token: String,
}

fn is_zero_i32(v: &i32) -> bool {
    *v == 0
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ListTaskPushNotificationConfigsResponse {
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub configs: Vec<TaskPushNotificationConfig>,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub next_page_token: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct GetExtendedAgentCardRequest {
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub tenant: Option<String>,
}
