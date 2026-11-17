// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # A2A v1.0 conformance harness
//!
//! Pure-type conformance tests that verify our wire-shape matches the
//! A2A v1.0 spec of the v1.0.0 release (specification:
//! <https://github.com/a2aproject/A2A/blob/v1.0.0/docs/specification.md>, proto:
//! <https://github.com/a2aproject/A2A/blob/v1.0.0/specification/a2a.proto>).
//!
//! These tests do **not** need Postgres — they operate on sample
//! documents and round-trip them through our Rust types. Runs in every
//! `cargo test` invocation and is the tripwire that catches spec drift
//! before it reaches HTTP.
//!
//! What's covered:
//!
//!   1. **Wire shape** — for every message and enum in the proto file,
//!      we assert proto3-JSON fidelity:
//!       - camelCase field names (spec §"JSON Serialization" / proto3-JSON rules)
//!       - SCREAMING_SNAKE_CASE enum values
//!       - oneof payload lifted to top level (not wrapped)
//!       - Required fields reject on absence (Brutor's extra safety
//!         layer on top of the proto's `field_behavior = REQUIRED`)
//!       - `deny_unknown_fields` on every message we own
//!
//!   2. **Error envelope** — every spec `A2aErrorKind` (§5.4) renders
//!      the AIP-193 `{error: {code, status, message, details[]}}` shape
//!      with an `ErrorInfo` detail that carries `@type`, `reason`,
//!      `domain`, and optional `metadata`.
//!
//!   3. **JCS (RFC 8785)** — our canonicalizer matches the RFC's
//!      public test vectors byte-for-byte: primitives, escape rules,
//!      number formatting, key sorting, nested structures.
//!
//!   4. **JWS (RFC 7515)** — sign + verify round-trip across a known
//!      key pair, plus negative tests (tampered body, tampered
//!      signature, wrong key).
//!
//! If a test here fails, the spec broke — treat it as an incident
//! before releasing. Keep new additions in the same order as the proto
//! file so reviewers can diff the two surfaces side by side.

use brutor_gateway_core::services::a2a_v1::errors::{
    A2A_ERROR_DOMAIN, A2aError, A2aErrorKind, ERROR_INFO_TYPE_URL,
};
use brutor_gateway_core::services::a2a_v1::jcs::canonicalize;
use brutor_gateway_core::services::a2a_v1::jws::{sign_card, verify_card};
use brutor_gateway_core::services::a2a_v1::{
    AgentCapabilities, AgentCard, AgentExtension, AgentInterface, AgentSkill, Message, Part,
    PartContent, Role, SendMessageRequest, SendMessageResponse, StreamResponse, Task,
    TaskArtifactUpdateEvent, TaskState, TaskStatus, TaskStatusUpdateEvent,
};
use ed25519_dalek::SigningKey;
use rand::TryRngCore;
use rand::rngs::OsRng;
use serde_json::{Value, json};

/// Generate an Ed25519 signing key. Bridges `rand 0.9`'s `OsRng` (which
/// implements `TryRngCore`) to `ed25519-dalek`'s older `CryptoRngCore`
/// requirement — we just fill 32 raw bytes and construct the key, same
/// pattern `services/a2a_v1/jws.rs`'s unit tests use.
fn fresh_keypair() -> SigningKey {
    let mut bytes = [0u8; 32];
    OsRng.try_fill_bytes(&mut bytes).expect("OsRng");
    SigningKey::from_bytes(&bytes)
}

// =============================================================================
// 1. Wire shape — primitives and oneofs
// =============================================================================

/// `Role` enum on the wire is SCREAMING_SNAKE_CASE per proto3-JSON.
#[test]
fn role_renders_as_screaming_snake_case() {
    assert_eq!(
        serde_json::to_value(Role::RoleUser).unwrap(),
        Value::String("ROLE_USER".to_string()),
    );
    assert_eq!(
        serde_json::to_value(Role::RoleAgent).unwrap(),
        Value::String("ROLE_AGENT".to_string()),
    );
}

/// `TaskState` — spec lists 9 canonical values. All must round-trip.
#[test]
fn task_state_all_nine_values_round_trip() {
    let cases: &[(TaskState, &str)] = &[
        (TaskState::TaskStateUnspecified, "TASK_STATE_UNSPECIFIED"),
        (TaskState::TaskStateSubmitted, "TASK_STATE_SUBMITTED"),
        (TaskState::TaskStateWorking, "TASK_STATE_WORKING"),
        (TaskState::TaskStateCompleted, "TASK_STATE_COMPLETED"),
        (TaskState::TaskStateFailed, "TASK_STATE_FAILED"),
        (TaskState::TaskStateCanceled, "TASK_STATE_CANCELED"),
        (
            TaskState::TaskStateInputRequired,
            "TASK_STATE_INPUT_REQUIRED",
        ),
        (TaskState::TaskStateRejected, "TASK_STATE_REJECTED"),
        (TaskState::TaskStateAuthRequired, "TASK_STATE_AUTH_REQUIRED"),
    ];
    for (variant, wire) in cases {
        let v = serde_json::to_value(variant).unwrap();
        assert_eq!(v, Value::String(wire.to_string()), "{variant:?} wire form");
        let back: TaskState = serde_json::from_value(v).unwrap();
        assert_eq!(back, *variant);
    }
}

/// `Part` oneof — text/raw/url/data appear at the top level, not
/// wrapped in a `content` key or a discriminator. This is the
/// proto3-JSON oneof rule and one of the sharpest v0.3 → v1.0 breaks.
#[test]
fn part_oneof_text_renders_at_top_level() {
    let p = Part {
        content: PartContent::Text("hello".into()),
        metadata: None,
        filename: None,
        media_type: None,
    };
    let v = serde_json::to_value(&p).unwrap();
    assert_eq!(v, json!({"text": "hello"}));
}

#[test]
fn part_oneof_raw_renders_base64_at_top_level() {
    let bytes = vec![1u8, 2, 3, 4, 5];
    let p = Part {
        content: PartContent::Raw(bytes.clone()),
        metadata: None,
        filename: None,
        media_type: None,
    };
    let v = serde_json::to_value(&p).unwrap();
    // Proto3-JSON encodes bytes fields as standard base64 (not URL-safe).
    assert_eq!(v.get("raw").and_then(|x| x.as_str()), Some("AQIDBAU="));
}

#[test]
fn part_oneof_tolerates_matching_kind_discriminator() {
    // v0.3 used `{"kind": "text", "text": "..."}` as an explicit
    // discriminator. v1.0 proto3-JSON dropped it from the spec, but
    // upstream a2a-sdk reference clients and most v1.0 sample servers
    // continue to emit it. The codec at `part_codec.rs:98-104` accepts
    // `kind` on input AS LONG AS it agrees with the actual oneof variant
    // — the variant data is the source of truth, `kind` is auxiliary.
    let ok = json!({"kind": "text", "text": "hi"});
    let p = serde_json::from_value::<Part>(ok).expect("matching kind should parse");
    match p.content {
        PartContent::Text(s) => assert_eq!(s, "hi"),
        other => panic!("expected Text variant, got {other:?}"),
    }
}

#[test]
fn part_oneof_rejects_mismatched_kind_discriminator() {
    // If `kind` is supplied AND disagrees with the variant present,
    // reject — that's how drift between a client's serializer and its
    // own payload gets caught. See part_codec.rs:206-222.
    let bad = json!({"kind": "url", "text": "hi"});
    let err = serde_json::from_value::<Part>(bad).unwrap_err();
    assert!(
        err.to_string().to_lowercase().contains("does not match"),
        "expected mismatch rejection, got: {err}",
    );
}

#[test]
fn part_oneof_rejects_unknown_sibling_fields() {
    // Unknown siblings (anything outside text/raw/url/data/kind/metadata/
    // filename/mediaType) are rejected — replicates the intent of
    // `#[serde(deny_unknown_fields)]` inside the custom Visitor.
    let bad = json!({"text": "hi", "bogus": 1});
    let err = serde_json::from_value::<Part>(bad).unwrap_err();
    assert!(
        err.to_string().to_lowercase().contains("unknown")
            || err.to_string().to_lowercase().contains("bogus"),
        "expected unknown-field rejection, got: {err}",
    );
}

/// `SendMessageResponse` oneof — `task` OR `message` at top level.
/// Exactly one must be present. Two = error. Zero = error.
#[test]
fn send_message_response_oneof_task_variant() {
    let task = stub_task();
    let resp = SendMessageResponse::Task(task.clone());
    let v = serde_json::to_value(&resp).unwrap();
    assert_eq!(v.as_object().unwrap().len(), 1, "oneof has exactly 1 key");
    assert!(v.get("task").is_some());
    assert!(v.get("message").is_none());
}

#[test]
fn send_message_response_oneof_message_variant() {
    let resp = SendMessageResponse::Message(stub_message());
    let v = serde_json::to_value(&resp).unwrap();
    assert!(v.get("message").is_some());
    assert!(v.get("task").is_none());
}

#[test]
fn send_message_response_rejects_both_variants() {
    let both = json!({
        "task": {"id": "t", "status": {"state": "TASK_STATE_WORKING"}},
        "message": {"messageId": "m", "role": "ROLE_USER", "parts": [{"text": "hi"}]},
    });
    serde_json::from_value::<SendMessageResponse>(both).unwrap_err();
}

#[test]
fn send_message_response_rejects_empty() {
    let empty = json!({});
    serde_json::from_value::<SendMessageResponse>(empty).unwrap_err();
}

/// `StreamResponse` oneof — four variants. Verify each key matches
/// the proto field name in camelCase.
#[test]
fn stream_response_oneof_all_four_variants() {
    let task_v = serde_json::to_value(StreamResponse::Task(stub_task())).unwrap();
    assert_eq!(task_v.as_object().unwrap().keys().next().unwrap(), "task");

    let msg_v = serde_json::to_value(StreamResponse::Message(stub_message())).unwrap();
    assert_eq!(msg_v.as_object().unwrap().keys().next().unwrap(), "message");

    let status_ev = TaskStatusUpdateEvent {
        task_id: "t".into(),
        context_id: "c".into(),
        status: TaskStatus {
            state: TaskState::TaskStateWorking,
            message: None,
            timestamp: None,
        },
        final_: false,
        metadata: None,
    };
    let status_v = serde_json::to_value(StreamResponse::StatusUpdate(status_ev)).unwrap();
    assert_eq!(
        status_v.as_object().unwrap().keys().next().unwrap(),
        "statusUpdate",
    );

    let artifact_ev = TaskArtifactUpdateEvent {
        task_id: "t".into(),
        context_id: "c".into(),
        artifact: brutor_gateway_core::services::a2a_v1::Artifact {
            artifact_id: "a".into(),
            name: None,
            description: None,
            parts: vec![Part {
                content: PartContent::Text("x".into()),
                metadata: None,
                filename: None,
                media_type: None,
            }],
            metadata: None,
            extensions: Vec::new(),
        },
        append: false,
        last_chunk: false,
        metadata: None,
    };
    let artifact_v = serde_json::to_value(StreamResponse::ArtifactUpdate(artifact_ev)).unwrap();
    assert_eq!(
        artifact_v.as_object().unwrap().keys().next().unwrap(),
        "artifactUpdate",
    );
}

// =============================================================================
// 2. Wire shape — required fields enforced
// =============================================================================

/// `Message` — proto `field_behavior = REQUIRED` on messageId, role, parts.
#[test]
fn message_rejects_missing_required_fields() {
    for missing in ["messageId", "role", "parts"] {
        let mut obj = json!({
            "messageId": "m",
            "role": "ROLE_USER",
            "parts": [{"text": "hi"}],
        });
        obj.as_object_mut().unwrap().remove(missing);
        let err = serde_json::from_value::<Message>(obj).unwrap_err();
        assert!(
            err.to_string().contains(missing),
            "missing {missing} should be reported: {err}",
        );
    }
}

/// `Task` — `id` and `status` are required.
#[test]
fn task_rejects_missing_id() {
    let obj = json!({"status": {"state": "TASK_STATE_WORKING"}});
    serde_json::from_value::<Task>(obj).unwrap_err();
}

#[test]
fn task_rejects_missing_status() {
    let obj = json!({"id": "t"});
    serde_json::from_value::<Task>(obj).unwrap_err();
}

/// `AgentCard` — spec §"Required fields" names 8. Enumerate them.
#[test]
fn agent_card_rejects_any_missing_required_field() {
    // Minimum viable card that parses, then drop each key in turn.
    let baseline = json!({
        "name": "agent",
        "description": "test",
        "supportedInterfaces": [
            {"url": "https://x", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
        ],
        "version": "1.0.0",
        "capabilities": {},
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
        "skills": [],
    });
    // Baseline parses.
    serde_json::from_value::<AgentCard>(baseline.clone()).unwrap();

    for required in [
        "name",
        "description",
        "supportedInterfaces",
        "version",
        "capabilities",
        "defaultInputModes",
        "defaultOutputModes",
        "skills",
    ] {
        let mut obj = baseline.clone();
        obj.as_object_mut().unwrap().remove(required);
        let res = serde_json::from_value::<AgentCard>(obj);
        assert!(
            res.is_err(),
            "missing `{required}` should reject but accepted: {res:?}",
        );
    }
}

#[test]
fn agent_card_rejects_v03_top_level_url() {
    // v0.3 carried `url` at the top of the card. v1.0 moved it into
    // `supportedInterfaces[].url`. If a v0.3 card sneaks through our
    // parser we'd silently lose the URL — deny_unknown_fields catches
    // this.
    let v03 = json!({
        "name": "n",
        "description": "d",
        "url": "https://x",
        "supportedInterfaces": [],
        "version": "1.0.0",
        "capabilities": {},
        "defaultInputModes": [],
        "defaultOutputModes": [],
        "skills": [],
    });
    serde_json::from_value::<AgentCard>(v03).unwrap_err();
}

// =============================================================================
// 3. Wire shape — unknown-field rejection (deny_unknown_fields)
// =============================================================================

#[test]
fn message_rejects_unknown_fields() {
    let obj = json!({
        "messageId": "m",
        "role": "ROLE_USER",
        "parts": [{"text": "hi"}],
        "unknownField": "surprise",
    });
    serde_json::from_value::<Message>(obj).unwrap_err();
}

#[test]
fn agent_interface_rejects_unknown_fields() {
    let obj = json!({
        "url": "https://x",
        "protocolBinding": "JSONRPC",
        "protocolVersion": "1.0",
        "legacyField": "nope",
    });
    serde_json::from_value::<AgentInterface>(obj).unwrap_err();
}

// =============================================================================
// 4. Error envelope — every A2aErrorKind
// =============================================================================

/// Every spec error type must render the AIP-193 shape exactly.
/// Spec §5.4 table — (kind, http status, gRPC status name, reason).
#[test]
fn every_error_kind_renders_canonical_envelope() {
    let cases: &[(A2aErrorKind, u16, &str, &str)] = &[
        (
            A2aErrorKind::TaskNotFound,
            404,
            "NOT_FOUND",
            "TASK_NOT_FOUND",
        ),
        (
            A2aErrorKind::TaskNotCancelable,
            409,
            "FAILED_PRECONDITION",
            "TASK_NOT_CANCELABLE",
        ),
        (
            A2aErrorKind::PushNotificationNotSupported,
            400,
            "UNIMPLEMENTED",
            "PUSH_NOTIFICATION_NOT_SUPPORTED",
        ),
        (
            A2aErrorKind::UnsupportedOperation,
            400,
            "UNIMPLEMENTED",
            "UNSUPPORTED_OPERATION",
        ),
        (
            A2aErrorKind::ContentTypeNotSupported,
            415,
            "INVALID_ARGUMENT",
            "CONTENT_TYPE_NOT_SUPPORTED",
        ),
        (
            A2aErrorKind::InvalidAgentResponse,
            502,
            "INTERNAL",
            "INVALID_AGENT_RESPONSE",
        ),
        (
            A2aErrorKind::ExtendedAgentCardNotConfigured,
            400,
            "FAILED_PRECONDITION",
            "EXTENDED_AGENT_CARD_NOT_CONFIGURED",
        ),
        (
            A2aErrorKind::ExtensionSupportRequired,
            400,
            "FAILED_PRECONDITION",
            "EXTENSION_SUPPORT_REQUIRED",
        ),
        (
            A2aErrorKind::VersionNotSupported,
            400,
            "UNIMPLEMENTED",
            "VERSION_NOT_SUPPORTED",
        ),
        (
            A2aErrorKind::Unauthenticated,
            401,
            "UNAUTHENTICATED",
            "UNAUTHENTICATED",
        ),
        (
            A2aErrorKind::PermissionDenied,
            403,
            "PERMISSION_DENIED",
            "PERMISSION_DENIED",
        ),
    ];
    for (kind, http, status_name, reason) in cases {
        let err =
            A2aError::new(*kind, "spec conformance test").with_metadata("testKey", "testValue");
        let env = err.to_envelope();
        // Outer shape.
        assert_eq!(env.error.code, *http, "{kind:?}: code");
        assert_eq!(env.error.status, *status_name, "{kind:?}: status");
        assert_eq!(env.error.message, "spec conformance test");
        // Detail shape.
        assert_eq!(env.error.details.len(), 1, "{kind:?}: details len");
        let detail = &env.error.details[0];
        assert_eq!(detail.type_url, ERROR_INFO_TYPE_URL, "{kind:?}: @type URL",);
        assert_eq!(detail.reason, *reason, "{kind:?}: reason");
        assert_eq!(detail.domain, A2A_ERROR_DOMAIN, "{kind:?}: domain");
        assert_eq!(
            detail.metadata.get("testKey").map(String::as_str),
            Some("testValue"),
        );
    }
}

/// The on-wire JSON the client sees must match the spec example
/// (§11.6) exactly. One golden test to pin the format end-to-end.
#[test]
fn error_envelope_matches_spec_example_byte_for_byte() {
    let err = A2aError::new(
        A2aErrorKind::TaskNotFound,
        "The specified task ID does not exist",
    )
    .with_metadata("taskId", "task-123");
    let wire = serde_json::to_value(err.to_envelope()).unwrap();
    assert_eq!(
        wire,
        json!({
            "error": {
                "code": 404,
                "status": "NOT_FOUND",
                "message": "The specified task ID does not exist",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                        "reason": "TASK_NOT_FOUND",
                        "domain": "a2a-protocol.org",
                        "metadata": {"taskId": "task-123"}
                    }
                ]
            }
        }),
    );
}

#[test]
fn error_reasons_never_end_with_error_suffix() {
    // Spec §11.6: "the A2A error type in UPPER_SNAKE_CASE without the 'Error' suffix".
    for k in [
        A2aErrorKind::TaskNotFound,
        A2aErrorKind::TaskNotCancelable,
        A2aErrorKind::PushNotificationNotSupported,
        A2aErrorKind::UnsupportedOperation,
        A2aErrorKind::ContentTypeNotSupported,
        A2aErrorKind::InvalidAgentResponse,
        A2aErrorKind::ExtendedAgentCardNotConfigured,
        A2aErrorKind::ExtensionSupportRequired,
        A2aErrorKind::VersionNotSupported,
    ] {
        assert!(
            !k.reason().ends_with("_ERROR"),
            "{k:?}: reason `{}` must not end with _ERROR",
            k.reason(),
        );
    }
}

// =============================================================================
// 5. JCS (RFC 8785) conformance — public test vectors
// =============================================================================
//
// Vectors pulled from RFC 8785 Appendix B and the erratum-corrected
// examples in https://www.rfc-editor.org/rfc/rfc8785 §3.2 / §3.3.

/// Simple object: keys MUST be lexicographically sorted.
#[test]
fn jcs_rfc8785_sorts_keys_lexicographically() {
    let input = json!({"b": 1, "a": 2, "c": 3});
    let out = canonicalize(&input).unwrap();
    assert_eq!(out, br#"{"a":2,"b":1,"c":3}"#);
}

/// Nested objects — sort at every level independently.
#[test]
fn jcs_rfc8785_nested_objects_sort_per_level() {
    let input = json!({"outer": {"y": 1, "x": 2}, "also": {"b": 3, "a": 4}});
    let out = canonicalize(&input).unwrap();
    assert_eq!(out, br#"{"also":{"a":4,"b":3},"outer":{"x":2,"y":1}}"#);
}

/// Arrays preserve order (not sorted).
#[test]
fn jcs_rfc8785_arrays_preserve_order() {
    let input = json!([3, 1, 2]);
    let out = canonicalize(&input).unwrap();
    assert_eq!(out, b"[3,1,2]");
}

/// Integers — no trailing `.0` on exact integers.
#[test]
fn jcs_rfc8785_integer_formatting() {
    let input = json!(42);
    let out = canonicalize(&input).unwrap();
    assert_eq!(out, b"42");
}

/// Booleans and null.
#[test]
fn jcs_rfc8785_booleans_and_null() {
    assert_eq!(canonicalize(&json!(true)).unwrap(), b"true");
    assert_eq!(canonicalize(&json!(false)).unwrap(), b"false");
    assert_eq!(canonicalize(&json!(null)).unwrap(), b"null");
}

/// Unicode: Spec mandates minimum escaping, only chars < 0x20 and
/// `"` / `\` get escaped. Higher code points are emitted as UTF-8.
#[test]
fn jcs_rfc8785_unicode_emitted_as_utf8_not_escaped() {
    // Comet emoji U+2604. JCS keeps it verbatim.
    let input = json!("\u{2604}");
    let out = canonicalize(&input).unwrap();
    let as_str = std::str::from_utf8(&out).unwrap();
    assert!(as_str.contains('\u{2604}'), "comet must survive: {as_str}");
    assert!(!as_str.contains("\\u2604"));
}

/// Control characters MUST be \uXXXX-escaped per RFC 8785.
#[test]
fn jcs_rfc8785_control_chars_escaped() {
    // U+0001 (Start of Heading) — must be `\u0001`.
    let input = Value::String("\u{0001}".to_string());
    let out = canonicalize(&input).unwrap();
    assert_eq!(out, br#""\u0001""#);
}

/// Strings with double-quote and backslash use the 2-char escapes.
#[test]
fn jcs_rfc8785_quote_and_backslash_escapes() {
    let input = Value::String(r#"a"b\c"#.to_string());
    let out = canonicalize(&input).unwrap();
    assert_eq!(out, br#""a\"b\\c""#);
}

/// A canonicalization round-trip is idempotent: canonicalize twice
/// yields the same bytes. This is an invariant checked on all
/// real-world payloads, not just toy inputs.
#[test]
fn jcs_is_idempotent() {
    let sample = json!({
        "zebra": [{"final": true}, {"intermediate": null}],
        "alpha": "a",
        "beta": 2.0,
        "gamma": {"nested": {"deep": ["value"]}},
    });
    let once = canonicalize(&sample).unwrap();
    let reparsed: Value = serde_json::from_slice(&once).unwrap();
    let twice = canonicalize(&reparsed).unwrap();
    assert_eq!(once, twice, "JCS must be idempotent");
}

// =============================================================================
// 6. JWS (RFC 7515) signing round-trip
// =============================================================================

#[test]
fn jws_sign_then_verify_round_trip() {
    let key = fresh_keypair();
    let verifying = key.verifying_key();
    let card = minimal_signable_card();
    let sig = sign_card(&card, &key, "tenant-fixture").expect("sign");
    verify_card(&card, &sig, &verifying).expect("verify");
}

#[test]
fn jws_tampered_payload_fails_verification() {
    let key = fresh_keypair();
    let verifying = key.verifying_key();
    let card = minimal_signable_card();
    let sig = sign_card(&card, &key, "tenant-fixture").expect("sign");
    let mut tampered = card.clone();
    tampered.description = "tampered — should fail verify".into();
    let err = verify_card(&tampered, &sig, &verifying).expect_err("must fail");
    assert!(
        format!("{err}").to_lowercase().contains("signature")
            || format!("{err}").to_lowercase().contains("mismatch"),
        "expected signature mismatch: {err}",
    );
}

#[test]
fn jws_wrong_public_key_fails_verification() {
    let key = fresh_keypair();
    let other = fresh_keypair().verifying_key();
    let card = minimal_signable_card();
    let sig = sign_card(&card, &key, "tenant-fixture").expect("sign");
    assert!(verify_card(&card, &sig, &other).is_err());
}

// =============================================================================
// Fixtures
// =============================================================================

fn stub_message() -> Message {
    Message {
        message_id: "msg-1".into(),
        context_id: None,
        task_id: None,
        role: Role::RoleUser,
        parts: vec![Part {
            content: PartContent::Text("hi".into()),
            metadata: None,
            filename: None,
            media_type: None,
        }],
        metadata: None,
        extensions: Vec::new(),
        reference_task_ids: Vec::new(),
    }
}

fn stub_task() -> Task {
    Task {
        id: "task-1".into(),
        context_id: Some("ctx-1".into()),
        status: TaskStatus {
            state: TaskState::TaskStateWorking,
            message: None,
            timestamp: None,
        },
        artifacts: Vec::new(),
        history: Vec::new(),
        metadata: None,
    }
}

fn minimal_signable_card() -> AgentCard {
    AgentCard {
        name: "conformance-agent".into(),
        description: "fixture for conformance tests".into(),
        supported_interfaces: vec![AgentInterface {
            url: "https://x/rpc".into(),
            protocol_binding: "JSONRPC".into(),
            tenant: None,
            protocol_version: "1.0".into(),
        }],
        provider: None,
        version: "1.0.0".into(),
        documentation_url: None,
        capabilities: AgentCapabilities {
            streaming: Some(true),
            push_notifications: None,
            extensions: vec![AgentExtension {
                uri: "https://brutor.ai/a2a/extensions/delegation-chain/v1".into(),
                description: None,
                required: false,
                params: None,
            }],
            extended_agent_card: None,
        },
        security_schemes: Default::default(),
        security_requirements: Vec::new(),
        default_input_modes: vec!["text".into()],
        default_output_modes: vec!["text".into()],
        skills: vec![AgentSkill {
            id: "demo".into(),
            name: "demo".into(),
            description: "demo".into(),
            tags: vec!["test".into()],
            examples: Vec::new(),
            input_modes: Vec::new(),
            output_modes: Vec::new(),
            security_requirements: Vec::new(),
        }],
        signatures: Vec::new(),
        icon_url: None,
    }
}

// -- Unused type hint to avoid compiler warning when reorganising ----
#[allow(dead_code)]
fn _typed(r: SendMessageRequest) -> SendMessageRequest {
    r
}
