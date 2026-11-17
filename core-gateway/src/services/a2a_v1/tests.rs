// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Unit tests for [`crate::services::a2a_v1`] — exercise every shape
//! that hits the wire.
//!
//! Strategy: for each type, we construct a canonical JSON string
//! by hand (matching the proto3-JSON mapping the spec mandates) and
//! assert both directions of the round-trip. Hand-written JSON
//! rather than `serde_json::to_string` catches drift where the
//! serializer starts emitting something the deserializer happens to
//! accept but a third-party partner wouldn't.

use std::collections::BTreeMap;

use chrono::{DateTime, TimeZone, Utc};
use serde_json::json;

use super::*;

// =============================================================================
// Enums
// =============================================================================

#[test]
fn role_screaming_snake_case() {
    assert_eq!(
        serde_json::to_string(&Role::RoleUser).unwrap(),
        "\"ROLE_USER\""
    );
    assert_eq!(
        serde_json::to_string(&Role::RoleAgent).unwrap(),
        "\"ROLE_AGENT\""
    );
    assert_eq!(
        serde_json::from_str::<Role>("\"ROLE_UNSPECIFIED\"").unwrap(),
        Role::RoleUnspecified,
    );
}

#[test]
fn role_accepts_lowercase_aliases() {
    // The proto-canonical wire form is SCREAMING_SNAKE_CASE
    // (ROLE_USER / ROLE_AGENT) and serializes that way. We also
    // accept the lowercase short forms used by upstream a2a-sdk
    // reference clients so external A2A v1.0 callers interoperate
    // without a wire-format adapter; the round-trip is asymmetric
    // (decode-only) by design.
    assert_eq!(
        serde_json::from_str::<Role>("\"user\"").unwrap(),
        Role::RoleUser,
    );
    assert_eq!(
        serde_json::from_str::<Role>("\"agent\"").unwrap(),
        Role::RoleAgent,
    );
    // Output is always the canonical form — sanity check the
    // round-trip from a lowercase input.
    let from_lower: Role = serde_json::from_str("\"user\"").unwrap();
    assert_eq!(serde_json::to_string(&from_lower).unwrap(), "\"ROLE_USER\"");
}

#[test]
fn task_state_screaming_snake_case() {
    assert_eq!(
        serde_json::to_string(&TaskState::TaskStateCompleted).unwrap(),
        "\"TASK_STATE_COMPLETED\"",
    );
    assert_eq!(
        serde_json::from_str::<TaskState>("\"TASK_STATE_INPUT_REQUIRED\"").unwrap(),
        TaskState::TaskStateInputRequired,
    );
}

#[test]
fn task_state_terminal_and_interrupted_classification() {
    // Terminal = won't move again.
    for s in [
        TaskState::TaskStateCompleted,
        TaskState::TaskStateFailed,
        TaskState::TaskStateCanceled,
        TaskState::TaskStateRejected,
    ] {
        assert!(s.is_terminal(), "{s:?} should be terminal");
        assert!(!s.is_interrupted(), "{s:?} should not be interrupted");
    }
    // Interrupted = paused pending a signal.
    for s in [
        TaskState::TaskStateInputRequired,
        TaskState::TaskStateAuthRequired,
    ] {
        assert!(!s.is_terminal(), "{s:?} should not be terminal");
        assert!(s.is_interrupted(), "{s:?} should be interrupted");
    }
    // Everything else is neither.
    for s in [
        TaskState::TaskStateUnspecified,
        TaskState::TaskStateSubmitted,
        TaskState::TaskStateWorking,
    ] {
        assert!(!s.is_terminal());
        assert!(!s.is_interrupted());
    }
}

#[test]
fn task_state_rejects_v03_kebab_case() {
    assert!(serde_json::from_str::<TaskState>("\"input-required\"").is_err());
    assert!(serde_json::from_str::<TaskState>("\"completed\"").is_err());
}

// =============================================================================
// Part — the member-presence oneof
// =============================================================================

#[test]
fn part_text_round_trip() {
    let p = Part {
        content: PartContent::Text("hello".into()),
        metadata: None,
        filename: None,
        media_type: Some("text/plain".into()),
    };
    let j = serde_json::to_value(&p).unwrap();
    assert_eq!(j, json!({"text": "hello", "mediaType": "text/plain"}));
    assert_eq!(serde_json::from_value::<Part>(j).unwrap(), p);
}

#[test]
fn part_raw_base64_round_trip() {
    let bytes = b"brutor-a2a-test\x00\x01\xff".to_vec();
    let p = Part {
        content: PartContent::Raw(bytes.clone()),
        metadata: None,
        filename: Some("blob.bin".into()),
        media_type: Some("application/octet-stream".into()),
    };
    let s = serde_json::to_string(&p).unwrap();
    // Sanity: the "raw" field must be a base64 string, not an array.
    assert!(s.contains("\"raw\":\""));
    let decoded: Part = serde_json::from_str(&s).unwrap();
    assert_eq!(decoded, p);
    // Explicit content check — the bytes round-trip exactly.
    match decoded.content {
        PartContent::Raw(b) => assert_eq!(b, bytes),
        _ => panic!("expected Raw variant"),
    }
}

#[test]
fn part_url_round_trip() {
    let p = Part {
        content: PartContent::Url("https://example.com/doc.pdf".into()),
        metadata: None,
        filename: Some("doc.pdf".into()),
        media_type: Some("application/pdf".into()),
    };
    let j = serde_json::to_value(&p).unwrap();
    assert_eq!(
        j,
        json!({
            "url": "https://example.com/doc.pdf",
            "filename": "doc.pdf",
            "mediaType": "application/pdf"
        }),
    );
    assert_eq!(serde_json::from_value::<Part>(j).unwrap(), p);
}

#[test]
fn part_data_round_trip() {
    let p = Part {
        content: PartContent::Data(json!({"key": "value", "n": 42})),
        metadata: None,
        filename: None,
        media_type: Some("application/json".into()),
    };
    let j = serde_json::to_value(&p).unwrap();
    assert_eq!(
        j,
        json!({"data": {"key": "value", "n": 42}, "mediaType": "application/json"}),
    );
    assert_eq!(serde_json::from_value::<Part>(j).unwrap(), p);
}

#[test]
fn part_rejects_zero_oneof_variants() {
    let j = json!({"mediaType": "text/plain"});
    let err = serde_json::from_value::<Part>(j).unwrap_err().to_string();
    assert!(
        err.contains("exactly one"),
        "error should mention the constraint: {err}"
    );
}

#[test]
fn part_rejects_multiple_oneof_variants() {
    let j = json!({"text": "hi", "url": "https://x/"});
    let err = serde_json::from_value::<Part>(j).unwrap_err().to_string();
    assert!(
        err.contains("exactly one") || err.contains("multiple"),
        "error should mention the constraint: {err}"
    );
}

#[test]
fn part_rejects_unknown_sibling_fields() {
    // deny_unknown_fields equivalent on the Visitor — siblings
    // outside the documented set are still rejected.
    let j = json!({"text": "hi", "wat": 1});
    let err = serde_json::from_value::<Part>(j).unwrap_err().to_string();
    assert!(
        err.contains("wat"),
        "expected unknown-field error mentioning `wat`: {err}"
    );
}

#[test]
fn part_accepts_consistent_kind_discriminator() {
    // Upstream a2a-sdk reference clients emit a JSON-Schema-friendly
    // `kind` discriminator alongside the proto3-JSON oneof field.
    // We tolerate it on input as long as it agrees with the variant
    // actually present.
    let j = json!({"kind": "text", "text": "hi"});
    let p = serde_json::from_value::<Part>(j).unwrap();
    assert_eq!(p.content, PartContent::Text("hi".into()));
}

#[test]
fn part_rejects_inconsistent_kind_discriminator() {
    // ``kind`` saying one thing while the variant supplied says
    // another is a malformed payload — the wire stays unambiguous.
    let j = json!({"kind": "url", "text": "hi"});
    let err = serde_json::from_value::<Part>(j).unwrap_err().to_string();
    assert!(
        err.to_lowercase().contains("kind"),
        "expected kind/variant mismatch error: {err}"
    );
}

#[test]
fn part_omits_optional_fields_on_serialize() {
    let p = Part {
        content: PartContent::Text("ok".into()),
        metadata: None,
        filename: None,
        media_type: None,
    };
    let s = serde_json::to_string(&p).unwrap();
    assert_eq!(s, r#"{"text":"ok"}"#);
}

// =============================================================================
// Message
// =============================================================================

#[test]
fn message_minimum_required_fields() {
    let m = Message {
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
        extensions: vec![],
        reference_task_ids: vec![],
    };
    let j = serde_json::to_value(&m).unwrap();
    assert_eq!(
        j,
        json!({
            "messageId": "msg-1",
            "role": "ROLE_USER",
            "parts": [{"text": "hi"}]
        }),
    );
    assert_eq!(serde_json::from_value::<Message>(j).unwrap(), m);
}

#[test]
fn message_rejects_missing_message_id() {
    let j = json!({"role": "ROLE_USER", "parts": [{"text": "hi"}]});
    assert!(serde_json::from_value::<Message>(j).is_err());
}

#[test]
fn message_rejects_missing_role() {
    let j = json!({"messageId": "m", "parts": [{"text": "hi"}]});
    assert!(serde_json::from_value::<Message>(j).is_err());
}

#[test]
fn message_rejects_missing_parts() {
    let j = json!({"messageId": "m", "role": "ROLE_USER"});
    assert!(serde_json::from_value::<Message>(j).is_err());
}

#[test]
fn message_rejects_unknown_fields() {
    let j = json!({
        "messageId": "m",
        "role": "ROLE_USER",
        "parts": [{"text": "hi"}],
        "kind": "message"  // v0.3 lingo
    });
    assert!(serde_json::from_value::<Message>(j).is_err());
}

#[test]
fn message_preserves_extensions_and_refs() {
    let m = Message {
        message_id: "m-1".into(),
        context_id: Some("ctx-1".into()),
        task_id: Some("task-1".into()),
        role: Role::RoleAgent,
        parts: vec![Part {
            content: PartContent::Text("reply".into()),
            metadata: None,
            filename: None,
            media_type: None,
        }],
        metadata: None,
        extensions: vec![BRUTOR_DELEGATION_EXTENSION_URI.into()],
        reference_task_ids: vec!["other-task".into()],
    };
    let s = serde_json::to_string(&m).unwrap();
    let round: Message = serde_json::from_str(&s).unwrap();
    assert_eq!(round, m);
    assert!(s.contains(BRUTOR_DELEGATION_EXTENSION_URI));
}

// =============================================================================
// Task / TaskStatus
// =============================================================================

#[test]
fn task_status_round_trip_with_timestamp() {
    let ts = Utc.with_ymd_and_hms(2026, 3, 15, 10, 0, 0).unwrap();
    let status = TaskStatus {
        state: TaskState::TaskStateWorking,
        message: None,
        timestamp: Some(ts),
    };
    let j = serde_json::to_value(&status).unwrap();
    let state_str = j.get("state").and_then(|v| v.as_str()).unwrap();
    assert_eq!(state_str, "TASK_STATE_WORKING");
    let decoded: TaskStatus = serde_json::from_value(j).unwrap();
    assert_eq!(decoded.state, TaskState::TaskStateWorking);
    assert_eq!(decoded.timestamp.unwrap(), ts);
}

#[test]
fn task_round_trip_with_artifacts_and_history() {
    let task = Task {
        id: "task-42".into(),
        context_id: Some("ctx-x".into()),
        status: TaskStatus {
            state: TaskState::TaskStateCompleted,
            message: None,
            timestamp: Some(Utc.with_ymd_and_hms(2026, 4, 1, 9, 0, 0).unwrap()),
        },
        artifacts: vec![Artifact {
            artifact_id: "art-1".into(),
            name: Some("output.json".into()),
            description: None,
            parts: vec![Part {
                content: PartContent::Data(json!({"ok": true})),
                metadata: None,
                filename: None,
                media_type: Some("application/json".into()),
            }],
            metadata: None,
            extensions: vec![],
        }],
        history: vec![Message {
            message_id: "m-hist".into(),
            context_id: None,
            task_id: None,
            role: Role::RoleUser,
            parts: vec![Part {
                content: PartContent::Text("run it".into()),
                metadata: None,
                filename: None,
                media_type: None,
            }],
            metadata: None,
            extensions: vec![],
            reference_task_ids: vec![],
        }],
        metadata: None,
    };
    let s = serde_json::to_string(&task).unwrap();
    let decoded: Task = serde_json::from_str(&s).unwrap();
    assert_eq!(decoded, task);
}

// =============================================================================
// Agent Card
// =============================================================================

#[test]
fn agent_card_minimum_viable() {
    let card = AgentCard {
        name: "billing-agent".into(),
        description: "Issues refunds and reads invoices.".into(),
        supported_interfaces: vec![AgentInterface {
            url: "https://api.example.com/a2a/v1".into(),
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
            extensions: vec![],
            extended_agent_card: None,
        },
        security_schemes: BTreeMap::new(),
        security_requirements: vec![],
        default_input_modes: vec!["text/plain".into()],
        default_output_modes: vec!["application/json".into()],
        skills: vec![AgentSkill {
            id: "billing.refund".into(),
            name: "Refund".into(),
            description: "Issue a refund for an invoice".into(),
            tags: vec!["billing".into()],
            examples: vec![],
            input_modes: vec![],
            output_modes: vec![],
            security_requirements: vec![],
        }],
        signatures: vec![],
        icon_url: None,
    };
    let v = serde_json::to_value(&card).unwrap();
    let top_obj = v.as_object().expect("card is object");
    // v0.3 top-level fields must not leak onto the wire. The same
    // field names are legitimately present inside `supportedInterfaces`
    // (where protocolVersion IS spec-correct on each AgentInterface),
    // so we check only the top level.
    assert!(
        !top_obj.contains_key("protocolVersion"),
        "top-level protocolVersion is v0.3-only"
    );
    assert!(
        !top_obj.contains_key("preferredTransport"),
        "preferredTransport is v0.3-only"
    );
    assert!(!top_obj.contains_key("url"), "top-level url is v0.3-only");
    assert!(
        top_obj.contains_key("supportedInterfaces"),
        "supportedInterfaces must be present"
    );
    // Inside the interface the binding is a string constant.
    let iface = v
        .pointer("/supportedInterfaces/0")
        .and_then(|i| i.as_object())
        .expect("first supported interface");
    assert_eq!(
        iface.get("protocolBinding").and_then(|x| x.as_str()),
        Some("JSONRPC")
    );
    assert_eq!(
        iface.get("protocolVersion").and_then(|x| x.as_str()),
        Some("1.0")
    );
    let round: AgentCard = serde_json::from_value(v).unwrap();
    assert_eq!(round, card);
}

#[test]
fn agent_card_rejects_v03_top_level_url_field() {
    // v0.3 had a top-level `url` on AgentCard — v1.0 removed it in
    // favour of supportedInterfaces[]. Reject loudly.
    let j = json!({
        "name": "x",
        "description": "y",
        "url": "https://x.example/",
        "version": "1.0.0",
        "capabilities": {},
        "defaultInputModes": [],
        "defaultOutputModes": [],
        "skills": [],
        "supportedInterfaces": []
    });
    assert!(serde_json::from_value::<AgentCard>(j).is_err());
}

#[test]
fn agent_card_with_signature() {
    let card = AgentCard {
        name: "n".into(),
        description: "d".into(),
        supported_interfaces: vec![AgentInterface {
            url: "https://x/".into(),
            protocol_binding: "JSONRPC".into(),
            tenant: None,
            protocol_version: "1.0".into(),
        }],
        provider: None,
        version: "1".into(),
        documentation_url: None,
        capabilities: AgentCapabilities::default(),
        security_schemes: BTreeMap::new(),
        security_requirements: vec![],
        default_input_modes: vec!["*/*".into()],
        default_output_modes: vec!["*/*".into()],
        skills: vec![],
        signatures: vec![AgentCardSignature {
            protected: "eyJhbGciOiJFZERTQSJ9".into(),
            signature: "c29tZS1zaWduYXR1cmU".into(),
            header: Some({
                let mut h = serde_json::Map::new();
                h.insert("kid".into(), json!("tenant-1"));
                h
            }),
        }],
        icon_url: None,
    };
    let s = serde_json::to_string(&card).unwrap();
    assert!(s.contains("\"signatures\""));
    let round: AgentCard = serde_json::from_str(&s).unwrap();
    assert_eq!(round, card);
}

// =============================================================================
// Security schemes oneof
// =============================================================================

#[test]
fn security_scheme_api_key_round_trip() {
    let scheme = SecurityScheme::ApiKey(APIKeySecurityScheme {
        description: Some("Tenant API key".into()),
        location: "header".into(),
        name: "X-API-Key".into(),
    });
    let j = serde_json::to_value(&scheme).unwrap();
    // Oneof lifts the key to the envelope.
    assert_eq!(
        j,
        json!({
            "apiKeySecurityScheme": {
                "description": "Tenant API key",
                "location": "header",
                "name": "X-API-Key"
            }
        }),
    );
    let round: SecurityScheme = serde_json::from_value(j).unwrap();
    assert_eq!(round, scheme);
}

#[test]
fn security_scheme_oauth2_with_auth_code_pkce() {
    let scopes = {
        let mut m = BTreeMap::new();
        m.insert("read".into(), "Read access".into());
        m.insert("write".into(), "Write access".into());
        m
    };
    let scheme = SecurityScheme::OAuth2(OAuth2SecurityScheme {
        description: None,
        flows: OAuthFlows::AuthorizationCode(AuthorizationCodeOAuthFlow {
            authorization_url: "https://auth/".into(),
            token_url: "https://auth/token".into(),
            refresh_url: None,
            scopes,
            pkce_required: true,
        }),
        oauth2_metadata_url: None,
    });
    let s = serde_json::to_string(&scheme).unwrap();
    assert!(s.contains("\"oauth2SecurityScheme\""));
    assert!(s.contains("\"authorizationCode\""));
    assert!(s.contains("\"pkceRequired\":true"));
    let round: SecurityScheme = serde_json::from_str(&s).unwrap();
    assert_eq!(round, scheme);
}

#[test]
fn security_scheme_mtls() {
    let scheme = SecurityScheme::MutualTls(MutualTlsSecurityScheme { description: None });
    let s = serde_json::to_string(&scheme).unwrap();
    assert_eq!(s, r#"{"mtlsSecurityScheme":{}}"#);
    let round: SecurityScheme = serde_json::from_str(&s).unwrap();
    assert_eq!(round, scheme);
}

#[test]
fn security_requirement_schemes_wire_form() {
    // map<string, StringList> where StringList is {"list": [...]}.
    let mut schemes = BTreeMap::new();
    schemes.insert(
        "oauth2".into(),
        StringList {
            list: vec!["read".into(), "write".into()],
        },
    );
    let req = SecurityRequirement { schemes };
    let j = serde_json::to_value(&req).unwrap();
    assert_eq!(
        j,
        json!({
            "schemes": {
                "oauth2": {"list": ["read", "write"]}
            }
        }),
    );
    assert_eq!(
        serde_json::from_value::<SecurityRequirement>(j).unwrap(),
        req
    );
}

// =============================================================================
// Stream events
// =============================================================================

#[test]
fn status_update_no_kind_discriminator() {
    let ev = TaskStatusUpdateEvent {
        task_id: "t-1".into(),
        context_id: "ctx-1".into(),
        status: TaskStatus {
            state: TaskState::TaskStateWorking,
            message: None,
            timestamp: None,
        },
        final_: false,
        metadata: None,
    };
    let s = serde_json::to_string(&ev).unwrap();
    // v1.0 drops the v0.3 `"kind":"status-update"` discriminator (the oneof
    // StreamResponse wrapper carries the type) ...
    assert!(!s.contains("\"kind\""));
    // ... but DOES carry `final` — the spec's stream-termination signal,
    // which was previously missing entirely (see TaskStatusUpdateEvent docs).
    assert!(s.contains("\"final\""));
    let round: TaskStatusUpdateEvent = serde_json::from_str(&s).unwrap();
    assert_eq!(round, ev);
}

#[test]
fn status_update_final_true_on_terminal_frame() {
    // Regression: the terminal frame must carry `final: true` so clients get
    // a clean stream-termination signal.
    let ev = TaskStatusUpdateEvent {
        task_id: "t-1".into(),
        context_id: "ctx-1".into(),
        status: TaskStatus {
            state: TaskState::TaskStateCompleted,
            message: None,
            timestamp: None,
        },
        final_: true,
        metadata: None,
    };
    let j = serde_json::to_value(&ev).unwrap();
    assert_eq!(j["final"], serde_json::json!(true));
    assert_eq!(
        serde_json::from_value::<TaskStatusUpdateEvent>(j).unwrap(),
        ev
    );
}

#[test]
fn artifact_update_has_append_and_last_chunk() {
    let ev = TaskArtifactUpdateEvent {
        task_id: "t-1".into(),
        context_id: "ctx-1".into(),
        artifact: Artifact {
            artifact_id: "a-1".into(),
            name: None,
            description: None,
            parts: vec![Part {
                content: PartContent::Text("chunk1".into()),
                metadata: None,
                filename: None,
                media_type: None,
            }],
            metadata: None,
            extensions: vec![],
        },
        append: true,
        last_chunk: false,
        metadata: None,
    };
    let s = serde_json::to_string(&ev).unwrap();
    assert!(s.contains("\"append\":true"));
    // last_chunk defaults to false, should be skipped.
    assert!(!s.contains("\"lastChunk\""));
    let round: TaskArtifactUpdateEvent = serde_json::from_str(&s).unwrap();
    assert_eq!(round, ev);
}

// =============================================================================
// SendMessageResponse / StreamResponse oneof
// =============================================================================

#[test]
fn send_message_response_task_variant() {
    let resp = SendMessageResponse::Task(Task {
        id: "t".into(),
        context_id: None,
        status: TaskStatus {
            state: TaskState::TaskStateSubmitted,
            message: None,
            timestamp: None,
        },
        artifacts: vec![],
        history: vec![],
        metadata: None,
    });
    let j = serde_json::to_value(&resp).unwrap();
    assert!(j.get("task").is_some());
    assert!(j.get("message").is_none());
    assert_eq!(
        serde_json::from_value::<SendMessageResponse>(j).unwrap(),
        resp
    );
}

#[test]
fn send_message_response_rejects_both_variants_set() {
    let j = json!({
        "task": {
            "id": "t",
            "status": {"state": "TASK_STATE_SUBMITTED"}
        },
        "message": {
            "messageId": "m",
            "role": "ROLE_AGENT",
            "parts": [{"text": "x"}]
        }
    });
    assert!(serde_json::from_value::<SendMessageResponse>(j).is_err());
}

#[test]
fn send_message_response_rejects_empty() {
    let j = json!({});
    assert!(serde_json::from_value::<SendMessageResponse>(j).is_err());
}

#[test]
fn stream_response_all_four_variants() {
    let task_r = StreamResponse::Task(Task {
        id: "t".into(),
        context_id: None,
        status: TaskStatus {
            state: TaskState::TaskStateWorking,
            message: None,
            timestamp: None,
        },
        artifacts: vec![],
        history: vec![],
        metadata: None,
    });
    let j = serde_json::to_value(&task_r).unwrap();
    assert!(j.get("task").is_some());
    assert_eq!(serde_json::from_value::<StreamResponse>(j).unwrap(), task_r);

    let msg_r = StreamResponse::Message(Message {
        message_id: "m".into(),
        context_id: None,
        task_id: None,
        role: Role::RoleAgent,
        parts: vec![Part {
            content: PartContent::Text("reply".into()),
            metadata: None,
            filename: None,
            media_type: None,
        }],
        metadata: None,
        extensions: vec![],
        reference_task_ids: vec![],
    });
    let j = serde_json::to_value(&msg_r).unwrap();
    assert!(j.get("message").is_some());
    assert_eq!(serde_json::from_value::<StreamResponse>(j).unwrap(), msg_r);

    let su = StreamResponse::StatusUpdate(TaskStatusUpdateEvent {
        task_id: "t".into(),
        context_id: "c".into(),
        status: TaskStatus {
            state: TaskState::TaskStateWorking,
            message: None,
            timestamp: None,
        },
        final_: false,
        metadata: None,
    });
    let j = serde_json::to_value(&su).unwrap();
    assert!(j.get("statusUpdate").is_some());
    assert_eq!(serde_json::from_value::<StreamResponse>(j).unwrap(), su);

    let au = StreamResponse::ArtifactUpdate(TaskArtifactUpdateEvent {
        task_id: "t".into(),
        context_id: "c".into(),
        artifact: Artifact {
            artifact_id: "a".into(),
            name: None,
            description: None,
            parts: vec![Part {
                content: PartContent::Text("chunk".into()),
                metadata: None,
                filename: None,
                media_type: None,
            }],
            metadata: None,
            extensions: vec![],
        },
        append: false,
        last_chunk: true,
        metadata: None,
    });
    let j = serde_json::to_value(&au).unwrap();
    assert!(j.get("artifactUpdate").is_some());
    assert_eq!(serde_json::from_value::<StreamResponse>(j).unwrap(), au);
}

#[test]
fn stream_response_rejects_unknown_variant_key() {
    let j = json!({"unknown": {}});
    let err = serde_json::from_value::<StreamResponse>(j)
        .unwrap_err()
        .to_string();
    assert!(err.contains("unknown"));
}

// =============================================================================
// Timestamps
// =============================================================================

#[test]
fn timestamp_iso8601_utc() {
    // Build a timestamp and check it round-trips via ISO-8601.
    let ts: DateTime<Utc> = Utc.with_ymd_and_hms(2026, 3, 15, 10, 0, 0).unwrap();
    let s = serde_json::to_string(&ts).unwrap();
    // chrono's Serialize emits RFC 3339 with a trailing Z, compatible
    // with ISO 8601. Format includes ms precision or higher.
    assert!(s.starts_with("\"2026-03-15T10:00:00"));
    assert!(s.ends_with("Z\""), "must end with Z (UTC): {s}");
    let round: DateTime<Utc> = serde_json::from_str(&s).unwrap();
    assert_eq!(round, ts);
}

// =============================================================================
// RPC envelopes — ensure the shape matches HTTP body expectations
// =============================================================================

#[test]
fn send_message_request_round_trip() {
    let req = SendMessageRequest {
        tenant: Some("acme".into()),
        message: Message {
            message_id: "m".into(),
            context_id: None,
            task_id: None,
            role: Role::RoleUser,
            parts: vec![Part {
                content: PartContent::Text("hello".into()),
                metadata: None,
                filename: None,
                media_type: None,
            }],
            metadata: None,
            extensions: vec![],
            reference_task_ids: vec![],
        },
        configuration: Some(SendMessageConfiguration {
            accepted_output_modes: vec!["text/plain".into()],
            task_push_notification_config: None,
            history_length: Some(5),
            return_immediately: true,
        }),
        metadata: None,
    };
    let s = serde_json::to_string(&req).unwrap();
    let round: SendMessageRequest = serde_json::from_str(&s).unwrap();
    assert_eq!(round, req);
    assert!(s.contains("\"tenant\":\"acme\""));
    assert!(s.contains("\"returnImmediately\":true"));
}

#[test]
fn list_tasks_response_required_fields_all_emitted() {
    let r = ListTasksResponse {
        tasks: vec![],
        next_page_token: String::new(),
        page_size: 50,
        total_size: 0,
    };
    let s = serde_json::to_string(&r).unwrap();
    // All four required fields MUST appear even when default-valued,
    // so consumers don't depend on absence as a signal.
    assert!(s.contains("\"tasks\""));
    assert!(s.contains("\"nextPageToken\""));
    assert!(s.contains("\"pageSize\""));
    assert!(s.contains("\"totalSize\""));
}

#[test]
fn delegation_extension_uri_is_versioned() {
    // Protect against accidental bumps that would break existing
    // cards' advertised extension URI.
    assert_eq!(
        BRUTOR_DELEGATION_EXTENSION_URI,
        "https://brutor.ai/a2a/extensions/delegation-chain/v1"
    );
}
