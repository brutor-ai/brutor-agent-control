// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # AWS Bedrock Backend
//!
//! Routes Anthropic Messages format requests to AWS Bedrock.
//! Handles:
//! - Model name mapping (e.g. `claude-sonnet-4-6` → `anthropic.claude-sonnet-4-6-v1:0`)
//! - AWS SigV4 authentication (using hmac/sha2 from existing deps)
//! - Beta header stripping (Bedrock rejects unknown beta flags)
//! - `effort` → `thinking.budget_tokens` mapping
//! - Moving `anthropic_version` into the request body
//! - AWS EventStream binary protocol → Anthropic SSE conversion (streaming)

use chrono::Utc;
use hmac::{Hmac, Mac};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use super::{BackendAdapter, BetaFilter, StreamConverter};
use crate::models::db::LlmModel;

// ---------------------------------------------------------------------------
// AWS SigV4 signing
// ---------------------------------------------------------------------------

/// The AWS credential of a model row: `aws_access_key_id`,
/// `aws_secret_access_key` and `aws_session_token` of its `provider_config`
/// (decrypted), and nothing else. A row's `api_key` is not read: the
/// `ACCESS_KEY_ID:SECRET_ACCESS_KEY` form it once carried is not a
/// credential of a Bedrock row (the same string was
/// sent as a bearer token on the routes the generic forwarder served).
pub(crate) struct AwsCredentials {
    access_key_id: String,
    secret_access_key: String,
    session_token: Option<String>,
}

impl AwsCredentials {
    /// A credential that is not a model row's: the Bedrock guardrail's own.
    pub(crate) fn new(
        access_key_id: String,
        secret_access_key: String,
        session_token: Option<String>,
    ) -> Self {
        Self {
            access_key_id,
            secret_access_key,
            session_token,
        }
    }

    pub(crate) fn from_model(model: &LlmModel) -> Option<Self> {
        let creds = serde_json::from_str::<crate::services::cloud_auth::ProviderCredentials>(
            model.provider_config.as_deref()?,
        )
        .ok()?;
        let nonempty = |v: Option<String>| v.filter(|s| !s.trim().is_empty());
        Some(Self {
            access_key_id: nonempty(creds.aws_access_key_id)?,
            secret_access_key: nonempty(creds.aws_secret_access_key)?,
            session_token: nonempty(creds.aws_session_token),
        })
    }
}

type HmacSha256 = Hmac<Sha256>;

fn hmac_sha256(key: &[u8], data: &[u8]) -> Vec<u8> {
    let mut mac = HmacSha256::new_from_slice(key).expect("HMAC accepts any key length");
    mac.update(data);
    mac.finalize().into_bytes().to_vec()
}

fn sha256_hex(data: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(data);
    hex::encode(hasher.finalize())
}

/// `UriEncode()` of the SigV4 reference: every byte except the unreserved
/// characters `A-Z a-z 0-9 - . _ ~` becomes `%XX`, uppercase hex.
pub(crate) fn uri_encode(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'.' | b'_' | b'~') {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

/// SigV4 headers for a POST to `url`, signed over the canonical form of
/// the request as it is sent: the canonical URI is "the URI-encoded version
/// of the absolute path component URI" (each segment of the path of `url`,
/// encoded), and `host` carries the port when the URL names one, as the
/// `Host` header does. `None` for a URL that is not one, or has a query
/// (not signed here).
///
/// Every Bedrock request of the gateway is signed here: Claude through
/// InvokeModel, Converse and the embeddings (the
/// Claude path signed the path as it stood in the URL, unencoded).
pub(crate) fn sigv4_headers_canonical(
    creds: &AwsCredentials,
    region: &str,
    service: &str,
    url: &str,
    body: &[u8],
    now: chrono::DateTime<Utc>,
) -> Option<Vec<(String, String)>> {
    let parsed = url::Url::parse(url).ok()?;
    if parsed.query().is_some() || parsed.fragment().is_some() {
        return None;
    }
    let host = match (parsed.host_str()?, parsed.port()) {
        (h, Some(port)) => format!("{h}:{port}"),
        (h, None) => h.to_string(),
    };
    let canonical_uri = parsed
        .path()
        .split('/')
        .map(uri_encode)
        .collect::<Vec<_>>()
        .join("/");
    Some(sigv4_sign(
        creds,
        region,
        service,
        &host,
        &canonical_uri,
        body,
        now,
    ))
}

/// The SigV4 computation: `path` is the canonical URI, `host` the value of
/// the `host` header.
fn sigv4_sign(
    creds: &AwsCredentials,
    region: &str,
    service: &str,
    host: &str,
    path: &str,
    body: &[u8],
    now: chrono::DateTime<Utc>,
) -> Vec<(String, String)> {
    let date_stamp = now.format("%Y%m%d").to_string();
    let amz_date = now.format("%Y%m%dT%H%M%SZ").to_string();

    let payload_hash = sha256_hex(body);

    // Canonical headers (sorted by lowercase key)
    let mut canonical_headers = format!(
        "content-type:application/json\nhost:{host}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{amz_date}\n"
    );
    let mut signed_headers = "content-type;host;x-amz-content-sha256;x-amz-date".to_string();

    if creds.session_token.is_some() {
        let token = creds.session_token.as_deref().unwrap();
        canonical_headers.push_str(&format!("x-amz-security-token:{token}\n"));
        signed_headers.push_str(";x-amz-security-token");
    }

    // Canonical request
    let canonical_request =
        format!("POST\n{path}\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}");

    let credential_scope = format!("{date_stamp}/{region}/{service}/aws4_request");
    let string_to_sign = format!(
        "AWS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n{}",
        sha256_hex(canonical_request.as_bytes())
    );

    // Signing key
    let k_date = hmac_sha256(
        format!("AWS4{}", creds.secret_access_key).as_bytes(),
        date_stamp.as_bytes(),
    );
    let k_region = hmac_sha256(&k_date, region.as_bytes());
    let k_service = hmac_sha256(&k_region, service.as_bytes());
    let k_signing = hmac_sha256(&k_service, b"aws4_request");

    let signature = hex::encode(hmac_sha256(&k_signing, string_to_sign.as_bytes()));

    let authorization = format!(
        "AWS4-HMAC-SHA256 Credential={}/{}, SignedHeaders={}, Signature={}",
        creds.access_key_id, credential_scope, signed_headers, signature
    );

    let mut headers = vec![
        ("Authorization".into(), authorization),
        ("x-amz-date".into(), amz_date),
        ("x-amz-content-sha256".into(), payload_hash),
    ];

    if let Some(ref token) = creds.session_token {
        headers.push(("x-amz-security-token".into(), token.clone()));
    }

    headers
}

// ---------------------------------------------------------------------------
// AWS EventStream → Anthropic SSE converter
// ---------------------------------------------------------------------------

/// Converts AWS EventStream binary frames to Anthropic SSE events.
///
/// AWS EventStream format (per frame):
///   [4 bytes: total_length] [4 bytes: headers_length] [4 bytes: prelude CRC]
///   [headers...] [payload...] [4 bytes: message CRC]
///
/// Each frame contains a `:message-type` header indicating the event type
/// and a `:event-type` header for the Bedrock event name. The payload is
/// the JSON body that maps to Anthropic SSE events.
///
/// The frames are decoded by [`next_event_stream_message`], the one decoder
/// of the gateway (Converse streams use it too): lengths and checksums are
/// checked before anything is sliced. A
/// stream that is not an event stream, or is corrupt, ends with one
/// Anthropic `error` event; nothing after it is read.
pub struct BedrockEventStreamConverter {
    buffer: Vec<u8>,
    /// Set by a malformed frame or an exception message: the stream is over.
    failed: bool,
}

impl BedrockEventStreamConverter {
    pub fn new() -> Self {
        Self {
            buffer: Vec::with_capacity(8192),
            failed: false,
        }
    }

    /// An Anthropic `error` event, and the end of this stream.
    fn fail(&mut self, kind: &str, message: &str) -> (String, String) {
        self.failed = true;
        self.buffer = Vec::new();
        (
            "error".to_string(),
            json!({"type": "error", "error": {"type": kind, "message": message}}).to_string(),
        )
    }
}

impl Default for BedrockEventStreamConverter {
    fn default() -> Self {
        Self::new()
    }
}

/// Parse the `:event-type` header from an EventStream headers block.
///
/// Header format: [name_length: 1 byte] [name] [type: 1 byte] [value_length: 2 bytes] [value]
/// Type 7 = string.
#[cfg(test)]
fn parse_event_type_header(headers: &[u8]) -> Option<String> {
    parse_string_header(headers, ":event-type")
}

/// The value of the string header `wanted` in an EventStream headers block.
fn parse_string_header(headers: &[u8], wanted: &str) -> Option<String> {
    let mut pos = 0;
    while pos < headers.len() {
        if pos >= headers.len() {
            break;
        }
        let name_len = headers[pos] as usize;
        pos += 1;
        if pos + name_len > headers.len() {
            break;
        }
        let name = std::str::from_utf8(&headers[pos..pos + name_len]).ok()?;
        pos += name_len;

        if pos >= headers.len() {
            break;
        }
        let header_type = headers[pos];
        pos += 1;

        // Type 7 = string (2-byte length prefix)
        if header_type == 7 {
            if pos + 2 > headers.len() {
                break;
            }
            let value_len = u16::from_be_bytes([headers[pos], headers[pos + 1]]) as usize;
            pos += 2;
            if pos + value_len > headers.len() {
                break;
            }
            let value = std::str::from_utf8(&headers[pos..pos + value_len]).ok()?;
            pos += value_len;

            if name == wanted {
                return Some(value.to_string());
            }
        } else {
            // Skip other header types — we only care about strings
            // Type mappings: 0=bool_true(0), 1=bool_false(0), 2=byte(1),
            // 3=short(2), 4=int(4), 5=long(8), 6=bytes(2+len), 7=string(2+len),
            // 8=timestamp(8), 9=uuid(16)
            let skip = match header_type {
                0 | 1 => 0,
                2 => 1,
                3 => 2,
                4 => 4,
                5 | 8 => 8,
                9 => 16,
                6 => {
                    if pos + 2 > headers.len() {
                        break;
                    }
                    let len = u16::from_be_bytes([headers[pos], headers[pos + 1]]) as usize;
                    2 + len
                }
                _ => break, // Unknown type
            };
            pos += skip;
        }
    }
    None
}

/// One decoded EventStream message.
pub(crate) struct EventStreamMessage {
    /// `:message-type`: `event` or `exception`.
    pub message_type: Option<String>,
    /// `:event-type`.
    pub event_type: Option<String>,
    /// `:exception-type`.
    pub exception_type: Option<String>,
    pub payload: Vec<u8>,
}

/// What the front of a buffer of EventStream bytes holds.
pub(crate) enum EventStreamStep {
    /// Not a whole message yet.
    Incomplete,
    /// A message, and the bytes it took.
    Message(EventStreamMessage, usize),
    /// Not a message: the lengths of the prelude cannot be one, or a
    /// checksum is wrong. The stream is not an EventStream, or is corrupt.
    Malformed,
}

/// The largest message accepted (the event-stream encoding allows 16 MiB).
const MAX_EVENT_STREAM_MESSAGE: usize = 16 * 1024 * 1024;

/// The table of CRC-32 (IEEE 802.3, the polynomial of GZIP), which the
/// event-stream encoding uses for both checksums.
const CRC32_TABLE: [u32; 256] = {
    let mut table = [0u32; 256];
    let mut i = 0;
    while i < 256 {
        let mut crc = i as u32;
        let mut bit = 0;
        while bit < 8 {
            crc = if crc & 1 == 1 {
                (crc >> 1) ^ 0xEDB8_8320
            } else {
                crc >> 1
            };
            bit += 1;
        }
        table[i] = crc;
        i += 1;
    }
    table
};

fn crc32(data: &[u8]) -> u32 {
    let mut crc = 0xFFFF_FFFFu32;
    for byte in data {
        crc = CRC32_TABLE[((crc ^ u32::from(*byte)) & 0xFF) as usize] ^ (crc >> 8);
    }
    !crc
}

fn be_u32(bytes: &[u8]) -> Option<u32> {
    Some(u32::from_be_bytes(bytes.get(..4)?.try_into().ok()?))
}

/// Decode the message at the front of `buffer`, the one decoder of AWS's
/// event-stream encoding in the gateway. Nothing is sliced by a length the
/// message states before that length is checked, and both checksums the
/// encoding defines are verified: the prelude CRC (bytes 8..12, over the
/// two lengths) before the lengths are trusted to wait for more bytes, and
/// the message CRC (the last four bytes, over everything before them). A
/// wrong length or a wrong checksum is [`EventStreamStep::Malformed`].
pub(crate) fn next_event_stream_message(buffer: &[u8]) -> EventStreamStep {
    let (Some(total), Some(headers_len), Some(prelude_crc)) = (
        be_u32(buffer),
        buffer.get(4..).and_then(be_u32),
        buffer.get(8..).and_then(be_u32),
    ) else {
        return EventStreamStep::Incomplete;
    };
    let (total, headers_len) = (total as usize, headers_len as usize);
    if buffer.get(..8).map(crc32) != Some(prelude_crc) {
        return EventStreamStep::Malformed;
    }
    // Prelude (12) + headers + payload + message CRC (4).
    if !(16..=MAX_EVENT_STREAM_MESSAGE).contains(&total) || headers_len > total - 16 {
        return EventStreamStep::Malformed;
    }
    let Some(message) = buffer.get(..total) else {
        return EventStreamStep::Incomplete;
    };
    let (Some(covered), Some(headers), Some(payload)) = (
        message.get(..total - 4),
        message.get(12..12 + headers_len),
        message.get(12 + headers_len..total - 4),
    ) else {
        return EventStreamStep::Malformed;
    };
    if message.get(total - 4..).and_then(be_u32) != Some(crc32(covered)) {
        return EventStreamStep::Malformed;
    }
    EventStreamStep::Message(
        EventStreamMessage {
            message_type: parse_string_header(headers, ":message-type"),
            event_type: parse_string_header(headers, ":event-type"),
            exception_type: parse_string_header(headers, ":exception-type"),
            payload: payload.to_vec(),
        },
        total,
    )
}

impl StreamConverter for BedrockEventStreamConverter {
    fn convert(&mut self, chunk: &[u8]) -> Vec<(String, String)> {
        let mut events = Vec::new();
        if self.failed {
            return events;
        }
        self.buffer.extend_from_slice(chunk);

        loop {
            let (message, consumed) = match next_event_stream_message(&self.buffer) {
                EventStreamStep::Incomplete => break,
                EventStreamStep::Malformed => {
                    events.push(self.fail(
                        "api_error",
                        "Amazon Bedrock's response stream is malformed (not an event stream, or \
                         a frame with a wrong length or checksum). The response is incomplete.",
                    ));
                    break;
                }
                EventStreamStep::Message(message, consumed) => (message, consumed),
            };
            self.buffer.drain(..consumed);
            let payload = String::from_utf8_lossy(&message.payload).to_string();

            // An exception message (`:message-type: exception`) ends the
            // stream: throttling, a model stream error, a validation error.
            if message.message_type.as_deref() == Some("exception")
                || message.exception_type.is_some()
            {
                let kind = message
                    .exception_type
                    .unwrap_or_else(|| "api_error".to_string());
                let text = serde_json::from_str::<Value>(&payload)
                    .ok()
                    .and_then(|v| {
                        v.get("message")
                            .or_else(|| v.get("Message"))
                            .and_then(Value::as_str)
                            .map(str::to_string)
                    })
                    .unwrap_or(payload);
                events.push(self.fail(&kind, &format!("Amazon Bedrock: {text}")));
                break;
            }
            let event_type = message.event_type.unwrap_or_else(|| "unknown".to_string());

            // Map Bedrock event types to Anthropic SSE events.
            // Bedrock wraps Anthropic events — the payload JSON
            // is typically the same as what Anthropic sends, but
            // wrapped in a `bytes` field that is base64-encoded
            // in some Bedrock response modes, or directly as JSON.
            let anthropic_data = if let Ok(parsed) = serde_json::from_str::<Value>(&payload) {
                // Check if payload has a `bytes` field (base64-encoded Anthropic JSON)
                if let Some(b64) = parsed.get("bytes").and_then(|b| b.as_str()) {
                    if let Ok(decoded) =
                        base64::Engine::decode(&base64::engine::general_purpose::STANDARD, b64)
                    {
                        String::from_utf8_lossy(&decoded).to_string()
                    } else {
                        payload.clone()
                    }
                } else {
                    payload.clone()
                }
            } else {
                payload.clone()
            };

            // Try to extract the Anthropic event type from the JSON
            let anthropic_event_type = serde_json::from_str::<Value>(&anthropic_data)
                .ok()
                .and_then(|v| v.get("type").and_then(|t| t.as_str()).map(String::from))
                .unwrap_or(event_type);

            if !anthropic_data.is_empty() {
                events.push((anthropic_event_type, anthropic_data));
            }
        }

        events
    }
}

// ---------------------------------------------------------------------------
// BedrockBackend
// ---------------------------------------------------------------------------

pub struct BedrockBackend;

impl BedrockBackend {
    /// Map a friendly model name to a Bedrock model identifier.
    fn bedrock_model_id(model_name: &str) -> String {
        // The catalog seeds Bedrock rows under a prefix of its own
        // (`bedrock/global.anthropic.claude-sonnet-5-5`) that is part of no
        // Bedrock id (it was sent as
        // `anthropic.bedrock/global.anthropic.claude-sonnet-5-5-v1:0`).
        let model_name = model_name.strip_prefix("bedrock/").unwrap_or(model_name);
        // … and the Converse rows under `bedrock/converse/`.
        let model_name = model_name.strip_prefix("converse/").unwrap_or(model_name);
        // A full id is sent as it is: a foundation-model id
        // (`anthropic.claude-…`), an inference profile (`global.anthropic.…`,
        // `us.anthropic.…`), a versioned id or an ARN.
        if model_name.contains("anthropic.") || model_name.contains(':') {
            return model_name.to_string();
        }
        format!("anthropic.{}-v1:0", model_name)
    }

    /// The region of a row, checked: it becomes part of a host name and of
    /// the credential scope, so it is a region name (`eu-west-1`) or the row
    /// is not sent. A row that names no region is
    /// not sent either: the gateway chooses no region for it.
    pub(crate) fn checked_region(model: &LlmModel) -> Result<String, String> {
        let Some(region) = Self::region(model) else {
            return Err(format!(
                "provider_endpoint_invalid: model '{}' (provider '{}') names no AWS region \
                 (set aws_region in the model's cloud credentials). Nothing was sent.",
                model.model_name, model.provider
            ));
        };
        match crate::services::cloud_hosts::aws_region(&region) {
            Ok(_) => Ok(region),
            Err(_) => Err(format!(
                "provider_endpoint_invalid: the AWS region of model '{}' (provider '{}') is \
                 not a region name. Nothing was sent.",
                model.model_name, model.provider
            )),
        }
    }

    /// The URL of `operation` (`invoke`, `invoke-with-response-stream`,
    /// `converse`, `converse-stream`) on `model_id` for a row: the row's
    /// `api_url` if it has one, else the Bedrock Runtime host of the row's
    /// region, then `/model/<id>/<operation>` with the id as one
    /// percent-encoded path segment (a versioned id has `:`, an ARN `:` and
    /// `/`). The one builder of a Bedrock Runtime URL.
    pub(crate) fn endpoint_url(
        model: &LlmModel,
        model_id: &str,
        operation: &str,
    ) -> Result<String, String> {
        let region = Self::checked_region(model)?;
        let invalid = |what: String| {
            format!(
                "provider_endpoint_invalid: the endpoint of model '{}' (provider '{}') {what}. \
                 Nothing was sent.",
                model.model_name, model.provider
            )
        };
        let base = match model
            .api_url
            .as_deref()
            .map(str::trim)
            .filter(|u| !u.is_empty())
        {
            Some(url) => {
                if url.contains('?') || url.contains('#') {
                    return Err(invalid(
                        "has a query or a fragment in its api_url, which a signed Bedrock \
                         request cannot carry"
                            .to_string(),
                    ));
                }
                url.trim_end_matches('/').to_string()
            }
            None => crate::services::cloud_hosts::bedrock_runtime_base(&region)
                .map_err(|reason| invalid(format!("cannot be built: {reason}")))?,
        };
        crate::services::outbound_policy::check_url(&base)
            .map_err(|denied| invalid(format!("is refused by the outbound policy ({denied})")))?;
        Ok(format!("{base}/model/{}/{operation}", uri_encode(model_id)))
    }

    /// Why a Claude row on Bedrock cannot be sent: it has no AWS credential
    /// (it was sent unsigned, with a warning), or no usable endpoint.
    /// `(error type, message)`; `None` for a row that can be signed
    /// and sent.
    pub(crate) fn row_problem(model: &LlmModel) -> Option<(&'static str, String)> {
        if AwsCredentials::from_model(model).is_none() {
            return Some(("provider_credential_missing", missing_credential(model)));
        }
        Self::endpoint_url(model, &Self::bedrock_model_id(&model.model_name), "invoke")
            .err()
            .map(|reason| ("provider_endpoint_invalid", reason))
    }

    /// The AWS region a row names: `aws_region` of its `provider_config`,
    /// then `aws_region` of its metadata, then the region in a Bedrock
    /// Runtime `api_url`. `None` for a row that names none: there is no
    /// default (such a row was signed for and sent to
    /// `us-east-1`, so the gateway decided where a tenant's prompts were
    /// processed).
    pub(crate) fn region(model: &LlmModel) -> Option<String> {
        // Check provider_config first (new UI-based credentials)
        if let Some(ref config_json) = model.provider_config {
            if let Ok(creds) = serde_json::from_str::<
                crate::services::cloud_auth::ProviderCredentials,
            >(config_json)
            {
                if let Some(region) = creds.aws_region {
                    return Some(region);
                }
            }
        }
        // Check metadata
        if let Some(region) = model
            .model_metadata
            .as_ref()
            .and_then(|m| m.get("aws_region"))
            .and_then(|v| v.as_str())
        {
            return Some(region.to_string());
        }
        // Try to extract from URL
        if let Some(ref url) = model.api_url {
            if let Some(start) = url.find("bedrock-runtime.") {
                let after = &url[start + "bedrock-runtime.".len()..];
                if let Some(end) = after.find('.') {
                    return Some(after[..end].to_string());
                }
            }
        }
        None
    }

    /// Map `effort` parameter to `thinking.budget_tokens`.
    fn map_effort_to_thinking(body: &mut Value) {
        let obj = match body.as_object_mut() {
            Some(o) => o,
            None => return,
        };

        if let Some(effort) = obj.remove("effort") {
            if let Some(effort_str) = effort.as_str() {
                let budget = match effort_str {
                    "low" | "minimal" => 1024,
                    "medium" => 10000,
                    "high" => 15000,
                    _ => 10000,
                };
                if !obj.contains_key("thinking") {
                    obj.insert(
                        "thinking".into(),
                        json!({
                            "type": "enabled",
                            "budget_tokens": budget
                        }),
                    );
                }
            }
        }

        // Clamp thinking budget_tokens to minimum 1024
        if let Some(thinking) = obj.get_mut("thinking") {
            if let Some(budget) = thinking.get("budget_tokens").and_then(|b| b.as_i64()) {
                if budget < 1024 {
                    if let Some(t_obj) = thinking.as_object_mut() {
                        t_obj.insert("budget_tokens".into(), Value::from(1024));
                    }
                }
            }
        }
    }
}

impl BackendAdapter for BedrockBackend {
    fn backend_name(&self) -> &str {
        "bedrock"
    }

    fn messages_url(&self, model: &LlmModel, streaming: bool) -> String {
        let operation = if streaming {
            "invoke-with-response-stream"
        } else {
            "invoke"
        };
        // A row that has no usable endpoint is refused at dispatch
        // (`providers::dispatch_refusal`). Should one get here, it gets a URL
        // nothing can be sent to, never a host built from what it carries.
        Self::endpoint_url(model, &Self::bedrock_model_id(&model.model_name), operation)
            .unwrap_or_else(|_| {
                format!(
                    "{}://bedrock/endpoint-invalid",
                    crate::services::providers::NO_UPSTREAM_SCHEME
                )
            })
    }

    fn count_tokens_url(&self, _model: &LlmModel) -> Option<String> {
        None
    }

    fn auth_headers(&self, _model: &LlmModel) -> Vec<(String, String)> {
        // SigV4 covers the body: the request is signed once, when the body
        // is final ([`sign_request`]). Signing an empty body here as well
        // put two `Authorization` headers on the request.
        vec![]
    }

    fn filter_beta_headers(&self, beta_value: &str) -> Option<String> {
        BetaFilter::strip_all().filter(beta_value)
    }

    fn transform_request(&self, body: &mut Value, model: &LlmModel) {
        let obj = match body.as_object_mut() {
            Some(o) => o,
            None => return,
        };

        // Map model name to Bedrock identifier
        obj.insert(
            "model".into(),
            Value::String(Self::bedrock_model_id(&model.model_name)),
        );

        // Move anthropic_version into body
        if !obj.contains_key("anthropic_version") {
            obj.insert(
                "anthropic_version".into(),
                Value::String("2023-06-01".into()),
            );
        }

        // Map effort → thinking.budget_tokens
        Self::map_effort_to_thinking(body);
    }

    fn native_anthropic_streaming(&self) -> bool {
        false
    }

    fn create_stream_converter(&self) -> Option<Box<dyn StreamConverter>> {
        Some(Box::new(BedrockEventStreamConverter::new()))
    }
}

/// The refusal of a Bedrock row that has no AWS credential.
pub(crate) fn missing_credential(model: &LlmModel) -> String {
    format!(
        "provider_credential_missing: model '{}' of provider '{}' has no AWS credential \
         (an access key id and a secret access key in its provider configuration). Nothing was \
         sent: a Bedrock request is not sent unsigned, and the gateway uses no credential of \
         its own environment.",
        model.model_name, model.provider
    )
}

/// The SigV4 service name of a Bedrock Runtime request.
const SIGV4_SERVICE: &str = "bedrock";

/// The SigV4 headers of a Bedrock Runtime request of `model`: `body` is the
/// bytes that are sent, `url` the URL they are sent to. The credential is
/// the row's and nothing else; a row without one, with a region that is not
/// one, or with a URL that cannot be signed is an error, and the request is
/// not sent.
pub(crate) fn sign_request(
    model: &LlmModel,
    url: &str,
    body: &[u8],
) -> Result<Vec<(String, String)>, String> {
    let creds = AwsCredentials::from_model(model).ok_or_else(|| missing_credential(model))?;
    let region = BedrockBackend::checked_region(model)?;
    sigv4_headers_canonical(&creds, &region, SIGV4_SERVICE, url, body, Utc::now()).ok_or_else(
        || {
            format!(
                "provider_endpoint_invalid: the endpoint of model '{}' is not a URL a Bedrock \
                 request can be signed for. Nothing was sent.",
                model.model_name
            )
        },
    )
}

/// [`sign_request`], with no headers for a row that cannot be signed.
pub fn compute_sigv4_headers(model: &LlmModel, url: &str, body: &[u8]) -> Vec<(String, String)> {
    sign_request(model, url, body).unwrap_or_default()
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn test_model(model_name: &str, api_url: Option<&str>) -> LlmModel {
        let mut m = crate::services::llm_adapter::make_test_model("bedrock", model_name);
        m.api_url = api_url.map(String::from);
        m
    }

    /// A row that names its region in its metadata and has no `api_url`.
    fn test_model_in(region: &str, model_name: &str) -> LlmModel {
        let mut m = test_model(model_name, None);
        m.model_metadata = Some(serde_json::json!({ "aws_region": region }));
        m
    }

    fn test_model_with_creds(model_name: &str) -> LlmModel {
        let mut m = crate::services::llm_adapter::make_test_model("bedrock", model_name);
        m.provider_config = Some(
            serde_json::json!({
                "aws_access_key_id": "AKIAIOSFODNN7EXAMPLE",
                "aws_secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                "aws_region": "us-east-1"
            })
            .to_string(),
        );
        m
    }

    #[test]
    fn backend_name() {
        assert_eq!(BedrockBackend.backend_name(), "bedrock");
    }

    #[test]
    fn bedrock_model_id_mapping() {
        assert_eq!(
            BedrockBackend::bedrock_model_id("claude-sonnet-4-6-20250514"),
            "anthropic.claude-sonnet-4-6-20250514-v1:0"
        );
    }

    #[test]
    fn bedrock_model_id_passthrough() {
        assert_eq!(
            BedrockBackend::bedrock_model_id("anthropic.claude-sonnet-4-6-v1:0"),
            "anthropic.claude-sonnet-4-6-v1:0"
        );
    }

    #[test]
    fn messages_url_is_the_host_of_the_rows_region() {
        let model = test_model_in("us-east-1", "claude-sonnet-4-6-20250514");
        let url = BedrockBackend.messages_url(&model, false);
        assert!(url.contains("bedrock-runtime.us-east-1.amazonaws.com"));
        assert!(url.contains("/invoke"));
        assert!(!url.contains("response-stream"));
    }

    /// A row that names no region has no default one. It is refused
    /// at dispatch; the URL it would get here is one nothing is sent to,
    /// and it cannot be signed.
    #[test]
    fn a_row_without_a_region_has_no_url_and_no_signature() {
        let model = test_model("claude-sonnet-4-6-20250514", None);
        for streaming in [false, true] {
            let url = BedrockBackend.messages_url(&model, streaming);
            assert_eq!(url, "no-upstream-base://bedrock/endpoint-invalid");
        }
        let reason = BedrockBackend::checked_region(&model).unwrap_err();
        assert!(
            reason.starts_with("provider_endpoint_invalid: ") && reason.contains("no AWS region"),
            "{reason}"
        );
        let mut with_creds = test_model_with_creds("claude-sonnet-4-6-20250514");
        with_creds.provider_config = Some(
            serde_json::json!({
                "aws_access_key_id": "AKIAIOSFODNN7EXAMPLE",
                "aws_secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
            })
            .to_string(),
        );
        assert_eq!(
            BedrockBackend::row_problem(&with_creds).map(|(t, _)| t),
            Some("provider_endpoint_invalid")
        );
        // The row's `api_url` is not a region: the request is signed for one.
        with_creds.api_url = Some("http://127.0.0.1:9".to_string());
        assert_eq!(
            BedrockBackend::row_problem(&with_creds).map(|(t, _)| t),
            Some("provider_endpoint_invalid")
        );
        let url = "https://bedrock-runtime.us-east-1.amazonaws.com/model/x/invoke";
        assert!(compute_sigv4_headers(&with_creds, url, b"{}").is_empty());
    }

    #[test]
    fn messages_url_streaming() {
        let model = test_model_in("us-east-1", "claude-sonnet-4-6-20250514");
        let url = BedrockBackend.messages_url(&model, true);
        assert!(url.contains("/invoke-with-response-stream"));
    }

    #[test]
    fn messages_url_custom_region() {
        let model = test_model(
            "claude-sonnet-4-6-20250514",
            Some("https://bedrock-runtime.eu-west-1.amazonaws.com"),
        );
        let url = BedrockBackend.messages_url(&model, false);
        assert!(url.contains("eu-west-1"));
    }

    #[test]
    fn count_tokens_not_supported() {
        let model = test_model("claude-sonnet-4-6-20250514", None);
        assert!(BedrockBackend.count_tokens_url(&model).is_none());
    }

    #[test]
    fn beta_headers_stripped() {
        let result = BedrockBackend
            .filter_beta_headers("claude-code-20250219,interleaved-thinking-2025-01-24");
        assert!(result.is_none());
    }

    #[test]
    fn transform_maps_effort_to_thinking() {
        let model = test_model("claude-sonnet-4-6-20250514", None);
        let mut body = json!({
            "model": "claude-sonnet-4-6",
            "messages": [],
            "effort": "high"
        });

        BedrockBackend.transform_request(&mut body, &model);

        assert!(body.get("effort").is_none(), "effort should be removed");
        let thinking = body.get("thinking").expect("thinking should be added");
        assert_eq!(thinking["type"], "enabled");
        assert_eq!(thinking["budget_tokens"], 15000);
    }

    #[test]
    fn transform_adds_anthropic_version() {
        let model = test_model("claude-sonnet-4-6-20250514", None);
        let mut body = json!({"model": "claude-sonnet-4-6", "messages": []});

        BedrockBackend.transform_request(&mut body, &model);

        assert_eq!(body["anthropic_version"], "2023-06-01");
    }

    #[test]
    fn not_native_streaming() {
        assert!(!BedrockBackend.native_anthropic_streaming());
    }

    #[test]
    fn creates_stream_converter() {
        assert!(BedrockBackend.create_stream_converter().is_some());
    }

    #[test]
    fn sigv4_headers_with_credentials() {
        let model = test_model_with_creds("claude-sonnet-4-6-20250514");
        let url = "https://bedrock-runtime.us-east-1.amazonaws.com/model/anthropic.claude-sonnet-4-6-20250514-v1:0/invoke";
        let headers = compute_sigv4_headers(&model, url, b"{}");

        assert!(!headers.is_empty());
        let auth = headers.iter().find(|(k, _)| k == "Authorization").unwrap();
        assert!(
            auth.1
                .starts_with("AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/")
        );
        assert!(
            auth.1
                .contains("SignedHeaders=content-type;host;x-amz-content-sha256;x-amz-date")
        );

        let date = headers.iter().find(|(k, _)| k == "x-amz-date").unwrap();
        assert!(date.1.ends_with('Z'));
        assert_eq!(date.1.len(), 16); // YYYYMMDDTHHMMSSz

        let hash = headers
            .iter()
            .find(|(k, _)| k == "x-amz-content-sha256")
            .unwrap();
        assert_eq!(hash.1.len(), 64); // SHA256 hex
    }

    #[test]
    fn sigv4_headers_no_credentials_returns_empty() {
        let model = test_model("claude-sonnet-4-6-20250514", None);
        // The row has no `provider_config`; its `api_key` is not read.
        let url = "https://bedrock-runtime.us-east-1.amazonaws.com/model/test/invoke";
        let headers = compute_sigv4_headers(&model, url, b"{}");
        assert!(headers.is_empty());
    }

    #[test]
    fn eventstream_parse_event_type_header() {
        // Build a minimal header: name_len(1) + name + type(1) + value_len(2) + value
        let name = b":event-type";
        let value = b"chunk";
        let mut header_bytes = Vec::new();
        header_bytes.push(name.len() as u8);
        header_bytes.extend_from_slice(name);
        header_bytes.push(7); // string type
        header_bytes.extend_from_slice(&(value.len() as u16).to_be_bytes());
        header_bytes.extend_from_slice(value);

        let result = parse_event_type_header(&header_bytes);
        assert_eq!(result, Some("chunk".to_string()));
    }

    #[test]
    fn region_from_metadata() {
        let mut model = test_model("claude-sonnet-4-6-20250514", None);
        model.model_metadata = Some(json!({"aws_region": "ap-southeast-1"}));
        assert_eq!(
            BedrockBackend::region(&model).as_deref(),
            Some("ap-southeast-1")
        );
    }

    // ── provider_config credential tests ──────────────────────────────

    fn test_model_with_provider_config(model_name: &str) -> LlmModel {
        let mut m = crate::services::llm_adapter::make_test_model("bedrock", model_name);
        m.api_key = None; // No legacy key
        m.provider_config = Some(
            serde_json::json!({
                "aws_access_key_id": "AKIAPROVCONFIG",
                "aws_secret_access_key": "provconfig-secret-key",
                "aws_region": "ap-northeast-1",
                "aws_session_token": "session-token-123"
            })
            .to_string(),
        );
        m
    }

    #[test]
    fn aws_credentials_from_provider_config() {
        let model = test_model_with_provider_config("claude-sonnet-4-6-20250514");
        let creds = AwsCredentials::from_model(&model).expect("Should parse provider_config creds");
        assert_eq!(creds.access_key_id, "AKIAPROVCONFIG");
        assert_eq!(creds.secret_access_key, "provconfig-secret-key");
        assert_eq!(creds.session_token.as_deref(), Some("session-token-123"));
    }

    #[test]
    fn aws_credentials_provider_config_takes_priority() {
        let mut model = test_model_with_provider_config("claude-sonnet-4-6-20250514");
        // An `api_key` in the old `id:secret` form changes nothing.
        model.api_key = Some("AKIALEGACY:legacy-secret".to_string());

        let creds = AwsCredentials::from_model(&model).unwrap();
        assert_eq!(creds.access_key_id, "AKIAPROVCONFIG");
    }

    /// `ACCESS_KEY_ID:SECRET_ACCESS_KEY` in `api_key` is not a
    /// credential of a Bedrock row, with or without a session token in the
    /// row's metadata. The credential is in `provider_config`.
    #[test]
    fn aws_credentials_are_not_read_from_api_key() {
        let mut model =
            crate::services::llm_adapter::make_test_model("bedrock", "claude-sonnet-4-6-20250514");
        model.api_key = Some("AKIALEGACY:legacy-secret".to_string());
        model.provider_config = None; // No provider_config
        assert!(AwsCredentials::from_model(&model).is_none());
        model.model_metadata = Some(json!({"aws_session_token": "legacy-token"}));
        assert!(AwsCredentials::from_model(&model).is_none());
        assert_eq!(
            BedrockBackend::row_problem(&model).map(|p| p.0),
            Some("provider_credential_missing")
        );
        // Blank halves are no credential either.
        model.provider_config = Some(
            json!({"aws_access_key_id": "AKIAPROVCONFIG", "aws_secret_access_key": " "})
                .to_string(),
        );
        assert!(AwsCredentials::from_model(&model).is_none());
    }

    #[test]
    fn aws_credentials_provider_config_missing_required_fields() {
        let mut model =
            crate::services::llm_adapter::make_test_model("bedrock", "claude-sonnet-4-6-20250514");
        model.api_key = None;
        // provider_config with only region (no key/secret)
        model.provider_config = Some(serde_json::json!({"aws_region": "us-west-2"}).to_string());

        // Should return None — incomplete AWS credentials
        assert!(AwsCredentials::from_model(&model).is_none());
    }

    #[test]
    fn sigv4_headers_from_provider_config() {
        let model = test_model_with_provider_config("claude-sonnet-4-6-20250514");
        let url = "https://bedrock-runtime.ap-northeast-1.amazonaws.com/model/anthropic.claude-sonnet-4-6-20250514-v1:0/invoke";
        let headers = compute_sigv4_headers(&model, url, b"{}");

        assert!(
            !headers.is_empty(),
            "Should produce SigV4 headers from provider_config"
        );
        let auth = headers.iter().find(|(k, _)| k == "Authorization").unwrap();
        assert!(
            auth.1
                .starts_with("AWS4-HMAC-SHA256 Credential=AKIAPROVCONFIG/")
        );

        // Session token header should be present
        let token = headers
            .iter()
            .find(|(k, _)| k == "x-amz-security-token")
            .unwrap();
        assert_eq!(token.1, "session-token-123");
    }

    #[test]
    fn region_from_provider_config() {
        let model = test_model_with_provider_config("claude-sonnet-4-6-20250514");
        assert_eq!(
            BedrockBackend::region(&model).as_deref(),
            Some("ap-northeast-1")
        );
    }

    #[test]
    fn region_provider_config_takes_priority_over_metadata() {
        let mut model = test_model_with_provider_config("claude-sonnet-4-6-20250514");
        // Also set metadata region — provider_config should win
        model.model_metadata = Some(json!({"aws_region": "eu-west-1"}));

        assert_eq!(
            BedrockBackend::region(&model).as_deref(),
            Some("ap-northeast-1")
        );
    }

    #[test]
    fn region_falls_back_to_metadata_when_no_provider_config() {
        let mut model =
            crate::services::llm_adapter::make_test_model("bedrock", "claude-sonnet-4-6-20250514");
        model.provider_config = None;
        model.model_metadata = Some(json!({"aws_region": "eu-central-1"}));

        assert_eq!(
            BedrockBackend::region(&model).as_deref(),
            Some("eu-central-1")
        );
    }

    #[test]
    fn region_falls_back_to_url_when_no_config() {
        let mut model =
            crate::services::llm_adapter::make_test_model("bedrock", "claude-sonnet-4-6-20250514");
        model.provider_config = None;
        model.model_metadata = None;
        model.api_url = Some("https://bedrock-runtime.sa-east-1.amazonaws.com".into());

        assert_eq!(BedrockBackend::region(&model).as_deref(), Some("sa-east-1"));
    }

    #[test]
    fn a_row_that_names_no_region_has_none() {
        let mut model =
            crate::services::llm_adapter::make_test_model("bedrock", "claude-sonnet-4-6-20250514");
        model.provider_config = None;
        model.model_metadata = None;
        model.api_url = None;

        assert_eq!(BedrockBackend::region(&model), None, "no default region");
    }
}
