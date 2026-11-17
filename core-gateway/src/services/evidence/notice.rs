// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! AI-interaction notice.
//!
//! The portal renders a notice before the first substantive turn of every
//! conversation and reports the render; the core seals it as an `fyi`
//! record `ai.brutor.notice` whose only link to the conversation is an
//! opaque, fresh-per-conversation ULID. The first assistant turn's
//! inference record then `references` it (`purpose: "responds_to"`), which
//! is what `notice.before_first_turn` checks — by timestamp *and* by log
//! order.
//!
//! This module builds records, so the never-enters rule applies: it takes
//! no identity of the user or the request at all (a static test enforces
//! it).
//! The route that calls it authenticates the portal user and uses that
//! identity for rate limiting only.
//!
//! Also here: [`mark_ai_generated`], the `X-Brutor-AI-Generated: true`
//! response label on LLM completions (transport-level marking;
//! content-level marking is not provided and the Transparency page says so).

use crate::services::evidence::attribution::Attribution;
use crate::services::evidence::mint::Minted;
use brutor_evidence::record::{
    self, Control, RecordBuilder, RecordError, Reference, System, control_result, kind, verdict,
};
use serde::Serialize;
use serde_json::{Map, Value, json};
use sqlx::PgPool;

pub const RECORD_TYPE: &str = "ai.brutor.notice";
pub const CONTROL_RENDERED: &str = "ai.brutor.notice.rendered";

pub mod method {
    pub const BANNER: &str = "banner";
    pub const INLINE: &str = "inline";
    pub const ALL: &[&str] = &[BANNER, INLINE];
}

/// What the portal reported rendering. Validated before anything is built.
#[derive(Debug, Clone)]
pub struct NoticeRender {
    pub conversation_handle: String,
    pub locale: String,
    pub method: String,
    pub text_sha256: String,
}

#[derive(Debug, PartialEq, Eq, thiserror::Error)]
pub enum NoticeError {
    #[error("conversation_handle must be a ULID minted fresh for the conversation")]
    Handle,
    #[error("locale must be a BCP 47 language tag")]
    Locale,
    #[error("method must be one of banner, inline")]
    Method,
    #[error("text_sha256 must be 64 lowercase hex characters")]
    TextDigest,
    #[error(transparent)]
    Record(#[from] RecordError),
}

/// BCP 47-shaped: a 2–3 letter language and up to a few alphanumeric
/// subtags. Deliberately narrow — the value lands in clear on a record.
pub fn valid_locale(s: &str) -> bool {
    if s.is_empty() || s.len() > 35 {
        return false;
    }
    let mut parts = s.split(['-', '_']);
    let lang = parts.next().unwrap_or("");
    (2..=3).contains(&lang.len())
        && lang.bytes().all(|b| b.is_ascii_alphabetic())
        && parts.all(|p| (1..=8).contains(&p.len()) && p.bytes().all(|b| b.is_ascii_alphanumeric()))
}

/// Validate a render report; returns it with the handle canonicalised.
pub fn validate(
    handle: &str,
    locale: &str,
    render_method: &str,
    text_sha256: &str,
) -> Result<NoticeRender, NoticeError> {
    let conversation_handle = crate::services::evidence::request::parse_conversation_handle(handle)
        .ok_or(NoticeError::Handle)?;
    if !valid_locale(locale) {
        return Err(NoticeError::Locale);
    }
    if !method::ALL.contains(&render_method) {
        return Err(NoticeError::Method);
    }
    if !record::is_hex64(text_sha256) {
        return Err(NoticeError::TextDigest);
    }
    Ok(NoticeRender {
        conversation_handle,
        locale: locale.to_string(),
        method: render_method.to_string(),
        text_sha256: text_sha256.to_string(),
    })
}

/// The `ai.brutor.notice` record for one render.
pub fn build_notice(
    tenant_id: &str,
    render: &NoticeRender,
    attr: Option<&Attribution>,
    rendered_at: chrono::DateTime<chrono::Utc>,
) -> Result<Minted, NoticeError> {
    let mut b = RecordBuilder::new(
        format!("{RECORD_TYPE}/{}", ulid::Ulid::new()),
        tenant_id.to_string(),
        rendered_at.to_rfc3339_opts(chrono::SecondsFormat::Millis, true),
    );
    b.kind = kind::FYI.into();
    b.verdict = verdict::RECORDED.into();
    b.system = attr.map(|a| System {
        ai_system_id: a.ai_system_id.clone(),
        contract_version: a.contract_version,
        contract_hash: a.contract_hash.clone(),
    });
    let epoch = attr
        .and_then(|a| a.contract_hash.as_ref())
        .map(|h| format!("contract:{h}"));
    b.epoch = epoch.clone();
    b.controls = vec![Control {
        id: CONTROL_RENDERED.into(),
        result: control_result::PASS.into(),
        blocking: false,
        severity: None,
        kind: Some("transparency".into()),
        evidence_digest: None,
    }];
    let mut ctx = Map::new();
    ctx.insert("record_type".into(), json!(RECORD_TYPE));
    ctx.insert("surface".into(), json!("portal"));
    ctx.insert(
        "conversation_handle".into(),
        json!(render.conversation_handle),
    );
    ctx.insert("locale".into(), json!(render.locale));
    ctx.insert("method".into(), json!(render.method));
    b.context = ctx;
    let mut refs = vec![Reference {
        kind: "ai.brutor.notice-text".into(),
        digest_alg: "SHA-256".into(),
        digest: render.text_sha256.clone(),
        purpose: Some("rendered_text".into()),
    }];
    if let Some(h) = attr
        .and_then(|a| a.contract_hash.clone())
        .filter(|h| record::is_hex64(h))
    {
        refs.push(Reference {
            kind: "ai.brutor.ai-system-contract".into(),
            digest_alg: "SHA-256".into(),
            digest: h,
            purpose: None,
        });
    }
    b.references = Some(refs);
    let (rec, record_id) = b.seal()?;
    Ok(Minted {
        row_index: 0,
        tenant_id: tenant_id.to_string(),
        record: rec,
        record_id,
        ai_system_id: attr.map(|a| a.ai_system_id.clone()),
        root_task_id: None,
        proxy_log_id: None,
        verdict: verdict::RECORDED.into(),
        kind: kind::FYI.into(),
        surface: "portal".into(),
        epoch,
        timestamp: rendered_at,
        tags: None,
        approval_request_id: None,
        consumed_approval_request_id: None,
    })
}

// ---------------------------------------------------------------------------
// Notice configuration (compliance_notice_configs, CP-written)
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct NoticeConfig {
    pub enabled: bool,
    pub surface: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub text: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub text_sha256: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub exemption: Option<Value>,
}

impl Default for NoticeConfig {
    /// No row anywhere: on, both surfaces, no text (the portal renders its
    /// own i18n default and digests exactly what it rendered).
    fn default() -> Self {
        Self {
            enabled: true,
            surface: "both".into(),
            text: None,
            text_sha256: None,
            exemption: None,
        }
    }
}

type ConfigRow = (
    Option<String>,
    bool,
    Option<String>,
    Option<Value>,
    Option<Value>,
    Option<Value>,
);

/// The system's row, else the tenant's, else [`NoticeConfig::default`]. A
/// schema without the table (pre-a178) reads as the default.
///
/// A database error is returned: the default configuration shows no
/// notice, so a failed read would silently skip a transparency obligation.
pub async fn load_config(
    pool: &PgPool,
    tenant_id: &str,
    ai_system_id: Option<&str>,
) -> Result<NoticeConfig, sqlx::Error> {
    let exists: Option<String> = sqlx::query_scalar::<_, Option<String>>(
        "SELECT to_regclass('public.compliance_notice_configs')::text",
    )
    .fetch_one(pool)
    .await?;
    if exists.is_none() {
        return Ok(NoticeConfig::default());
    }
    let rows: Vec<ConfigRow> = sqlx::query_as(
        "SELECT ai_system_id, enabled, surface, texts, text_digests, exemption \
         FROM compliance_notice_configs \
         WHERE tenant_id = $1 AND (ai_system_id IS NULL OR ai_system_id = $2)",
    )
    .bind(tenant_id)
    .bind(ai_system_id)
    .fetch_all(pool)
    .await?;
    Ok(pick_config(rows, ai_system_id))
}

fn non_empty(v: Option<Value>) -> Option<Value> {
    v.filter(|x| match x {
        Value::Null => false,
        Value::Object(m) => !m.is_empty(),
        _ => true,
    })
}

fn pick_config(rows: Vec<ConfigRow>, ai_system_id: Option<&str>) -> NoticeConfig {
    let chosen = rows
        .iter()
        .find(|r| ai_system_id.is_some() && r.0.as_deref() == ai_system_id)
        .or_else(|| rows.iter().find(|r| r.0.is_none()));
    match chosen {
        None => NoticeConfig::default(),
        Some((_, enabled, surface, texts, digests, exemption)) => NoticeConfig {
            enabled: *enabled,
            surface: surface.clone().unwrap_or_else(|| "both".into()),
            text: non_empty(texts.clone()),
            text_sha256: non_empty(digests.clone()),
            exemption: non_empty(exemption.clone()),
        },
    }
}

// ---------------------------------------------------------------------------
// X-Brutor-AI-Generated
// ---------------------------------------------------------------------------

/// Label a successful LLM completion response as AI-generated. Applied as a
/// route layer on the completion routes only (not embeddings or model
/// listings), streaming and non-streaming alike — the header is set before
/// the first byte of the body.
pub async fn mark_ai_generated(
    req: axum::extract::Request,
    next: axum::middleware::Next,
) -> axum::response::Response {
    let mut resp = next.run(req).await;
    if resp.status().is_success() {
        resp.headers_mut().insert(
            crate::constants::headers::X_BRUTOR_AI_GENERATED,
            axum::http::HeaderValue::from_static("true"),
        );
    }
    resp
}

#[cfg(test)]
mod tests {
    use super::*;

    fn render() -> NoticeRender {
        validate(
            &ulid::Ulid::new().to_string(),
            "en-GB",
            "banner",
            &"a".repeat(64),
        )
        .unwrap()
    }

    #[test]
    fn validation_rejects_anything_identity_shaped() {
        let h = ulid::Ulid::new().to_string();
        assert_eq!(
            validate("user-42", "en", "banner", &"a".repeat(64)).unwrap_err(),
            NoticeError::Handle
        );
        assert_eq!(
            validate(
                "sess_0123456789abcdef012345",
                "en",
                "banner",
                &"a".repeat(64)
            )
            .unwrap_err(),
            NoticeError::Handle
        );
        assert_eq!(
            validate(&h, "english please", "banner", &"a".repeat(64)).unwrap_err(),
            NoticeError::Locale
        );
        assert_eq!(
            validate(&h, "en", "popup", &"a".repeat(64)).unwrap_err(),
            NoticeError::Method
        );
        assert_eq!(
            validate(&h, "en", "inline", "ABC").unwrap_err(),
            NoticeError::TextDigest
        );
        assert!(validate(&h.to_lowercase(), "pt-BR", "inline", &"b".repeat(64)).is_ok());
        for ok in ["en", "sv", "de-DE", "zh-Hant-TW", "pt_BR"] {
            assert!(valid_locale(ok), "{ok}");
        }
        for bad in ["", "e", "english", "en-", "en-GB!", &"x".repeat(40)] {
            assert!(!valid_locale(bad), "{bad}");
        }
    }

    #[test]
    fn notice_record_is_an_fyi_with_the_rendered_control_and_text_digest() {
        let r = render();
        let m = build_notice("tenant-a", &r, None, chrono::Utc::now()).unwrap();
        let rec = &m.record;
        assert_eq!(rec["kind"], "fyi");
        assert_eq!(rec["verdict"], "recorded");
        assert_eq!(rec["context"]["record_type"], RECORD_TYPE);
        assert_eq!(
            rec["context"]["conversation_handle"],
            r.conversation_handle.as_str()
        );
        assert_eq!(rec["controls"][0]["id"], CONTROL_RENDERED);
        assert_eq!(rec["controls"][0]["result"], "pass");
        assert_eq!(rec["references"][0]["type"], "ai.brutor.notice-text");
        assert_eq!(rec["references"][0]["digest"], "a".repeat(64));
        assert!(rec.get("effect").is_none());
        assert!(m.proxy_log_id.is_none());
        let v = brutor_evidence::verify(rec, None);
        assert!(v.ok, "{:?}", v.findings);
        // Only the admitted context keys.
        for k in rec["context"].as_object().unwrap().keys() {
            assert!(
                [
                    "record_type",
                    "surface",
                    "conversation_handle",
                    "locale",
                    "method"
                ]
                .contains(&k.as_str()),
                "{k}"
            );
        }
    }

    #[test]
    fn governed_notice_carries_the_system_and_contract() {
        let attr = Attribution {
            ai_system_id: "sys-1".into(),
            contract_id: Some("c".into()),
            contract_hash: Some("d".repeat(64)),
            contract_version: Some(2),
        };
        let m = build_notice("tenant-a", &render(), Some(&attr), chrono::Utc::now()).unwrap();
        assert_eq!(m.record["system"]["ai_system_id"], "sys-1");
        assert_eq!(m.ai_system_id.as_deref(), Some("sys-1"));
        assert!(
            m.record
                .to_string()
                .contains("ai.brutor.ai-system-contract")
        );
    }

    #[test]
    fn config_prefers_the_system_row_then_the_tenant_row_then_the_default() {
        let tenant_row: ConfigRow = (
            None,
            true,
            Some("banner".into()),
            Some(json!({"en": "You are talking to an AI."})),
            Some(json!({"en": "a".repeat(64)})),
            None,
        );
        let system_row: ConfigRow = (
            Some("sys-1".into()),
            false,
            Some("inline".into()),
            Some(json!({})),
            None,
            Some(json!({"asserted": true, "basis": "obvious"})),
        );
        let both = vec![tenant_row.clone(), system_row.clone()];
        let c = pick_config(both.clone(), Some("sys-1"));
        assert!(!c.enabled);
        assert_eq!(c.surface, "inline");
        assert_eq!(c.text, None, "empty texts read as absent");
        assert!(c.exemption.is_some());
        let c = pick_config(both, Some("sys-2"));
        assert_eq!(c.surface, "banner");
        assert!(c.text.is_some());
        assert_eq!(pick_config(vec![], None), NoticeConfig::default());
        let d = serde_json::to_value(NoticeConfig::default()).unwrap();
        assert_eq!(d, json!({"enabled": true, "surface": "both"}));
    }

    /// Identity never enters: the notice builder takes no identity. Grepping
    /// the source is deliberate, as in `mint.rs`.
    #[test]
    fn notice_never_reads_a_never_enters_field() {
        const SRC: &str = include_str!("notice.rs");
        let body = SRC.split("#[cfg(test)]").next().unwrap();
        for forbidden in [
            "user_id",
            "username",
            "session",
            "subject_id",
            "api_key",
            "client_ip",
            "user_agent",
            "email",
            "HeaderMap",
        ] {
            assert!(!body.contains(forbidden), "{forbidden} in notice.rs");
        }
    }
}
