// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Amazon Bedrock Runtime, for the rows that are not Claude
//!
//! Claude on Bedrock is served by `anthropic_adapter` (InvokeModel with an
//! Anthropic body) and is not touched here. This module serves, on the
//! providers `bedrock` and `bedrock_converse` alike:
//!
//! * the chat families `amazon.nova*`, `mistral.*` and `deepseek.*` through
//!   `Converse` and `ConverseStream` ([`converse`]);
//! * the embedding families `amazon.titan-embed-text*` and
//!   `cohere.embed-{english,multilingual}-v3` through `InvokeModel`
//!   ([`embeddings`]).
//!
//! Every other Bedrock row stays refused (`providers::dispatch_refusal`).
//!
//! The wire formats are AWS's API reference
//! (<https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html>,
//! `…/API_runtime_ConverseStream.html`, `…/API_runtime_InvokeModel.html`).
//! A request is signed with SigV4 by the signing code of the Claude backend
//! (`anthropic_adapter::backend::bedrock`), with the credential of the row
//! and nothing else: this module reads no environment variable and no file,
//! and a row without a credential is not sent.

pub mod converse;
pub mod embeddings;

use serde_json::{Value, json};

use crate::models::db::LlmModel;
use crate::services::anthropic_adapter::backend::bedrock::{
    AwsCredentials, BedrockBackend, missing_credential, sign_request,
};

/// What this module serves a row as.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Family {
    /// A chat model on `Converse` / `ConverseStream`.
    ConverseChat,
    /// Amazon Titan Text Embeddings on `InvokeModel`: one text per call.
    TitanEmbed,
    /// Cohere Embed v3 on `InvokeModel`: up to 96 texts per call.
    CohereEmbed,
}

/// The Converse features of a chat model, as AWS documents them. A feature
/// AWS's documentation does not establish for the model is `false`, and a
/// request that needs it is refused, not sent.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Features {
    /// Tool definitions, tool calls and tool results.
    pub tools: bool,
    /// `toolChoice` `any` and `tool` (`auto` comes with `tools`).
    pub forced_tool_choice: bool,
    /// Tools on `ConverseStream`.
    pub streaming_tools: bool,
    /// Image content blocks.
    pub images: bool,
    /// `outputConfig.textFormat` (a JSON schema).
    pub structured_output: bool,
}

fn is_bedrock(provider: &str) -> bool {
    matches!(provider, "bedrock" | "bedrock_converse")
}

/// The Bedrock model id of a row: its name without the catalog's
/// `bedrock/` and `converse/` prefixes. A foundation-model id, an
/// inference-profile id or an ARN.
pub fn model_id(model: &LlmModel) -> &str {
    let name = model.model_name.as_str();
    let name = name.strip_prefix("bedrock/").unwrap_or(name);
    name.strip_prefix("converse/").unwrap_or(name)
}

/// The foundation-model part of an id, lower-cased: after the last `/` of
/// an ARN and without the geography of an inference profile (`us.`, `eu.`,
/// `apac.`, `global.`, …).
fn family_name(id: &str) -> String {
    const VENDORS: &[&str] = &["amazon", "mistral", "deepseek", "cohere"];
    // The geographies of the inference profiles AWS's model cards list.
    const GEOGRAPHIES: &[&str] = &["us", "eu", "apac", "ca", "jp", "global"];
    let id = id.to_ascii_lowercase();
    let base = id.rsplit('/').next().unwrap_or(&id);
    let mut labels = base.splitn(3, '.');
    match (labels.next(), labels.next(), labels.next()) {
        (Some(geo), Some(vendor), Some(rest))
            if GEOGRAPHIES.contains(&geo) && VENDORS.contains(&vendor) =>
        {
            format!("{vendor}.{rest}")
        }
        _ => base.to_string(),
    }
}

/// The family this module serves `model` as; `None` for a row of another
/// provider, a Claude row (served by `anthropic_adapter`) and every Bedrock
/// model this module does not implement.
pub fn family(model: &LlmModel) -> Option<Family> {
    if !is_bedrock(&model.provider)
        || crate::services::anthropic_adapter::should_route_via_openai_surface(model)
    {
        return None;
    }
    let name = family_name(model_id(model));
    if name.starts_with("amazon.nova") {
        // Nova's image, video, speech and embedding models are not chat
        // models.
        let other = ["canvas", "reel", "sonic", "embed"]
            .iter()
            .any(|k| name.contains(k));
        return (!other).then_some(Family::ConverseChat);
    }
    if name.starts_with("mistral.") || name.starts_with("deepseek.") {
        return Some(Family::ConverseChat);
    }
    if name.starts_with("amazon.titan-embed-text") {
        return Some(Family::TitanEmbed);
    }
    if name.starts_with("cohere.embed-english-v3")
        || name.starts_with("cohere.embed-multilingual-v3")
    {
        return Some(Family::CohereEmbed);
    }
    None
}

/// True for a row served through Converse on the chat route.
pub fn serves_chat(model: &LlmModel) -> bool {
    // The provider first: this runs on every chat request.
    is_bedrock(&model.provider) && family(model) == Some(Family::ConverseChat)
}

/// True for a row served through InvokeModel on the embeddings route.
pub fn serves_embeddings(model: &LlmModel) -> bool {
    is_bedrock(&model.provider)
        && matches!(
            family(model),
            Some(Family::TitanEmbed | Family::CohereEmbed)
        )
}

/// The Converse features of a chat row. From AWS's documentation
/// (the per-model "supported features" table of the Converse
/// guide is gone; these are the model cards and the Nova guides):
///
/// * Nova Pro, Lite, Micro, Premier: "System prompts", "Tool use",
///   "Streaming tool use"; `toolChoice` `auto`, `any`, `tool`; image input
///   for all but Micro (text only).
/// * Nova 2 Lite: tools and the three tool choices, images; streaming tool
///   use is not stated.
/// * Mistral Large 3: image input, structured output; tool calling is
///   listed for the `bedrock-mantle` endpoint only.
/// * DeepSeek V3.2: text only, structured output; tool calling is listed
///   for the `bedrock-mantle` endpoint only.
///
/// Any other model of the three families gets none of them.
pub fn features(model: &LlmModel) -> Features {
    let name = family_name(model_id(model));
    let nova_v1 = |kind: &str| name.starts_with(&format!("amazon.nova-{kind}-v1"));
    if nova_v1("pro") || nova_v1("lite") || nova_v1("premier") || nova_v1("micro") {
        Features {
            tools: true,
            forced_tool_choice: true,
            streaming_tools: true,
            images: !nova_v1("micro"),
            structured_output: false,
        }
    } else if name.starts_with("amazon.nova-2-lite") {
        Features {
            tools: true,
            forced_tool_choice: true,
            streaming_tools: false,
            images: true,
            structured_output: false,
        }
    } else if name.starts_with("mistral.mistral-large-3") {
        Features {
            images: true,
            structured_output: true,
            ..Features::default()
        }
    } else if name.starts_with("deepseek.v3.2") {
        Features {
            structured_output: true,
            ..Features::default()
        }
    } else {
        Features::default()
    }
}

/// The Bedrock Runtime operation of a gateway endpoint for a row this
/// module serves on it; `None` otherwise.
pub fn operation_for(model: &LlmModel, endpoint: &str, stream: bool) -> Option<&'static str> {
    if !is_bedrock(&model.provider) {
        return None;
    }
    match (endpoint, family(model)?) {
        ("/chat/completions", Family::ConverseChat) => Some(if stream {
            "converse-stream"
        } else {
            "converse"
        }),
        ("/embeddings", Family::TitanEmbed | Family::CohereEmbed) => Some("invoke"),
        _ => None,
    }
}

/// The URL of `operation` (`converse`, `converse-stream`, `invoke`) for a
/// row: the Claude backend's builder (`BedrockBackend::endpoint_url`: the
/// row's `api_url` or the Bedrock Runtime host of its checked region, the id
/// as one percent-encoded path segment), with this module's model id.
pub fn endpoint_url(model: &LlmModel, operation: &str) -> Result<String, String> {
    BedrockBackend::endpoint_url(model, model_id(model), operation)
}

/// Why a row this module serves cannot be sent: it has no AWS credential,
/// or no usable endpoint. `(error type, message)`; `None` for a row that
/// can be signed and sent. The gateway never signs with a credential that
/// is not the row's and never sends a Bedrock request unsigned.
pub fn row_problem(model: &LlmModel) -> Option<(&'static str, String)> {
    family(model)?;
    if AwsCredentials::from_model(model).is_none() {
        return Some(("provider_credential_missing", missing_credential(model)));
    }
    endpoint_url(model, "converse")
        .err()
        .map(|reason| ("provider_endpoint_invalid", reason))
}

/// A signed Bedrock Runtime request, ready to send: `body` is the bytes the
/// signature covers.
pub struct SignedCall {
    pub url: String,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

/// Sign `body` for `operation` of `model` with the row's credential.
pub fn signed_call(model: &LlmModel, operation: &str, body: &Value) -> Result<SignedCall, String> {
    let url = endpoint_url(model, operation)?;
    let bytes = serde_json::to_vec(body).map_err(|e| format!("serialising the request: {e}"))?;
    let headers = sign_request(model, &url, &bytes)?;
    Ok(SignedCall {
        url,
        headers,
        body: bytes,
    })
}

/// A Bedrock error reply as the gateway's provider error: the HTTP status
/// is Bedrock's (429 throttling and model-not-ready, 400 validation, 403
/// access denied, 404, 408, 424, 5xx) and is what the resilience layer and
/// the caller act on; the body is the OpenAI error shape with Bedrock's
/// message. `error_type` is the `x-amzn-errortype` header when Bedrock
/// sent one.
pub fn error_body(status: u16, body: &Value, error_type: Option<&str>) -> Value {
    let message = ["message", "Message"]
        .iter()
        .find_map(|k| body.get(k).and_then(Value::as_str))
        .or_else(|| body.pointer("/error/message").and_then(Value::as_str))
        .map(str::to_string)
        .unwrap_or_else(|| body.to_string().chars().take(1000).collect());
    let kind = match status {
        400 => "invalid_request_error",
        401 | 403 => "permission_error",
        404 => "not_found_error",
        408 => "timeout_error",
        429 => "rate_limit_error",
        _ => "upstream_error",
    };
    // `ThrottlingException:http://…` is a form the header takes.
    let code = error_type.map(|t| t.split(':').next().unwrap_or(t).to_string());
    json!({"error": {
        "message": format!("Amazon Bedrock (HTTP {status}): {message}"),
        "type": kind,
        "code": code,
    }})
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::llm_adapter::make_test_model;

    fn row(provider: &str, name: &str) -> LlmModel {
        let mut m = make_test_model(provider, name);
        m.api_url = None;
        m.api_key = None;
        m.provider_config = Some(
            json!({"aws_access_key_id": "AKIAUNITTEST", "aws_secret_access_key": "unit-secret",
                   "aws_region": "eu-west-1"})
            .to_string(),
        );
        m
    }

    #[test]
    fn the_fourteen_seed_rows_have_a_family() {
        use Family::*;
        for (provider, name, expected) in [
            ("bedrock", "bedrock/amazon.nova-pro-v1:0", ConverseChat),
            ("bedrock", "bedrock/amazon.nova-lite-v1:0", ConverseChat),
            ("bedrock", "bedrock/amazon.nova-micro-v1:0", ConverseChat),
            (
                "bedrock",
                "bedrock/global.amazon.nova-2-lite-v1:0",
                ConverseChat,
            ),
            (
                "bedrock",
                "bedrock/mistral.mistral-large-3-675b-instruct",
                ConverseChat,
            ),
            ("bedrock", "bedrock/deepseek.v3.2", ConverseChat),
            (
                "bedrock",
                "bedrock/amazon.titan-embed-text-v2:0",
                TitanEmbed,
            ),
            ("bedrock", "bedrock/cohere.embed-english-v3", CohereEmbed),
            (
                "bedrock_converse",
                "bedrock/converse/us.amazon.nova-pro-v1:0",
                ConverseChat,
            ),
            (
                "bedrock_converse",
                "bedrock/converse/us.amazon.nova-lite-v1:0",
                ConverseChat,
            ),
            (
                "bedrock_converse",
                "bedrock/converse/us.amazon.nova-micro-v1:0",
                ConverseChat,
            ),
            (
                "bedrock_converse",
                "bedrock/converse/us.amazon.nova-2-lite-v1:0",
                ConverseChat,
            ),
            (
                "bedrock_converse",
                "bedrock/converse/mistral.mistral-large-3-675b-instruct",
                ConverseChat,
            ),
            (
                "bedrock_converse",
                "bedrock/converse/deepseek.v3.2",
                ConverseChat,
            ),
        ] {
            assert_eq!(family(&row(provider, name)), Some(expected), "{name}");
        }
    }

    #[test]
    fn everything_else_has_none() {
        for (provider, name) in [
            // Claude is the Anthropic backend's.
            ("bedrock", "bedrock/global.anthropic.claude-sonnet-5-5"),
            (
                "bedrock_converse",
                "bedrock/converse/us.anthropic.claude-haiku-5-5",
            ),
            ("bedrock", "bedrock/meta.llama3-3-70b-instruct-v1:0"),
            ("bedrock", "bedrock/amazon.titan-text-express-v1"),
            ("bedrock", "bedrock/amazon.titan-embed-image-v1"),
            ("bedrock", "bedrock/amazon.nova-canvas-v1:0"),
            ("bedrock", "bedrock/amazon.nova-reel-v1:0"),
            ("bedrock", "bedrock/amazon.nova-2-sonic-v1:0"),
            (
                "bedrock",
                "bedrock/amazon.nova-2-multimodal-embeddings-v1:0",
            ),
            ("bedrock", "bedrock/cohere.embed-v4:0"),
            ("bedrock", "bedrock/cohere.command-r-plus-v1:0"),
            (
                "bedrock",
                "bedrock/us.meta.llama4-maverick-17b-instruct-v1:0",
            ),
            // A family name somewhere else in the id is not the family.
            ("bedrock", "bedrock/meta.amazon.nova-pro"),
            ("bedrock", "bedrock/xamazon.nova-pro-v1:0"),
            // Another provider's row of the same name.
            ("openai", "amazon.nova-pro-v1:0"),
            ("mistral", "mistral.mistral-large-3-675b-instruct"),
        ] {
            assert_eq!(family(&row(provider, name)), None, "{provider} / {name}");
        }
    }

    #[test]
    fn an_arn_and_an_inference_profile_keep_their_family() {
        for name in [
            "arn:aws:bedrock:eu-west-1:123456789012:inference-profile/eu.amazon.nova-pro-v1:0",
            "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-lite-v1:0",
            "bedrock/apac.amazon.nova-lite-v1:0",
            "bedrock/jp.amazon.nova-2-lite-v1:0",
        ] {
            assert_eq!(
                family(&row("bedrock", name)),
                Some(Family::ConverseChat),
                "{name}"
            );
        }
        assert_eq!(
            family(&row(
                "bedrock",
                "arn:aws:bedrock:eu-west-1:123456789012:application-inference-profile/abc123"
            )),
            None
        );
    }

    #[test]
    fn the_features_follow_the_documentation() {
        let f = |name: &str| features(&row("bedrock", name));
        assert!(f("bedrock/amazon.nova-pro-v1:0").images);
        assert!(f("bedrock/amazon.nova-pro-v1:0").streaming_tools);
        assert!(!f("bedrock/amazon.nova-micro-v1:0").images);
        assert!(f("bedrock/amazon.nova-micro-v1:0").tools);
        assert!(f("bedrock/global.amazon.nova-2-lite-v1:0").tools);
        assert!(!f("bedrock/global.amazon.nova-2-lite-v1:0").streaming_tools);
        assert!(!f("bedrock/mistral.mistral-large-3-675b-instruct").tools);
        assert!(f("bedrock/mistral.mistral-large-3-675b-instruct").images);
        assert!(!f("bedrock/deepseek.v3.2").tools);
        assert!(!f("bedrock/deepseek.v3.2").images);
        assert_eq!(
            f("bedrock/mistral.pixtral-large-2502-v1:0"),
            Features::default()
        );
        assert_eq!(f("bedrock/amazon.nova-9-future-v1:0"), Features::default());
    }

    #[test]
    fn the_url_is_the_regions_host_or_the_rows_api_url() {
        let mut m = row("bedrock", "bedrock/amazon.nova-pro-v1:0");
        assert_eq!(
            endpoint_url(&m, "converse").unwrap(),
            "https://bedrock-runtime.eu-west-1.amazonaws.com/model/amazon.nova-pro-v1%3A0/converse"
        );
        m.api_url = Some("http://127.0.0.1:9000/".into());
        assert_eq!(
            endpoint_url(&m, "converse-stream").unwrap(),
            "http://127.0.0.1:9000/model/amazon.nova-pro-v1%3A0/converse-stream"
        );
        let arn = row(
            "bedrock",
            "arn:aws:bedrock:eu-west-1:123456789012:inference-profile/eu.amazon.nova-pro-v1:0",
        );
        assert_eq!(
            endpoint_url(&arn, "converse").unwrap(),
            "https://bedrock-runtime.eu-west-1.amazonaws.com/model/arn%3Aaws%3Abedrock%3Aeu-west-1\
             %3A123456789012%3Ainference-profile%2Feu.amazon.nova-pro-v1%3A0/converse"
        );
    }

    #[test]
    fn a_region_that_is_not_one_builds_no_url() {
        for bad in [
            "x.attacker.example/",
            "eu-west-1.attacker.example",
            "eu west",
            "",
            "-eu",
            "EU-WEST-1",
            "eu-west-1#",
            "eu-west-1@evil",
        ] {
            let mut m = row("bedrock", "bedrock/amazon.nova-pro-v1:0");
            m.provider_config = Some(
                json!({"aws_access_key_id": "AKIAUNITTEST", "aws_secret_access_key": "s",
                       "aws_region": bad})
                .to_string(),
            );
            assert!(endpoint_url(&m, "converse").is_err(), "{bad:?}");
            assert!(signed_call(&m, "converse", &json!({})).is_err(), "{bad:?}");
            assert_eq!(
                row_problem(&m).map(|p| p.0),
                Some("provider_endpoint_invalid"),
                "{bad:?}"
            );
        }
    }

    #[test]
    fn a_row_without_a_credential_is_a_problem_and_is_not_signed() {
        let mut m = row("bedrock", "bedrock/amazon.nova-pro-v1:0");
        m.provider_config = None;
        assert_eq!(
            row_problem(&m).map(|p| p.0),
            Some("provider_credential_missing")
        );
        assert!(signed_call(&m, "converse", &json!({})).is_err());
        // Half a credential is none.
        m.provider_config = Some(json!({"aws_access_key_id": "AKIAUNITTEST"}).to_string());
        assert!(signed_call(&m, "converse", &json!({})).is_err());
        assert_eq!(
            row_problem(&row("bedrock", "bedrock/amazon.nova-pro-v1:0")),
            None
        );
        // Not this module's row: not this module's problem.
        assert_eq!(row_problem(&make_test_model("openai", "gpt-5.2")), None);
    }

    /// The credential comes from the row and from nowhere else: this module
    /// names no environment variable, no home directory and no credential
    /// file, so an ambient AWS credential cannot be picked up.
    #[test]
    fn nothing_here_reads_an_ambient_credential() {
        for file in ["mod.rs", "converse.rs", "embeddings.rs"] {
            let path = format!(
                "{}/src/services/bedrock_runtime/{file}",
                env!("CARGO_MANIFEST_DIR")
            );
            let src = std::fs::read_to_string(&path).expect("module source");
            let production = src.split("#[cfg(test)]").next().unwrap_or_default();
            for forbidden in [
                concat!("std::", "env"),
                concat!("env::", "var"),
                concat!("AWS_", "ACCESS"),
                concat!("AWS_", "SECRET"),
                concat!("AWS_", "PROFILE"),
                concat!(".a", "ws/"),
                concat!("home", "_dir"),
                concat!("169.254.", "170.2"),
            ] {
                assert!(
                    !production.contains(forbidden),
                    "{file} mentions `{forbidden}`: a credential may only come from the row"
                );
            }
        }
    }

    #[test]
    fn an_error_keeps_bedrocks_message_and_names_its_kind() {
        let e = error_body(
            429,
            &json!({"message": "Too many requests"}),
            Some("ThrottlingException:http://internal.amazon.com/coral/"),
        );
        assert_eq!(e["error"]["type"], "rate_limit_error");
        assert_eq!(e["error"]["code"], "ThrottlingException");
        assert!(
            e["error"]["message"]
                .as_str()
                .unwrap()
                .contains("Too many requests")
        );
        assert_eq!(
            error_body(400, &json!({"message": "bad"}), None)["error"]["type"],
            "invalid_request_error"
        );
        assert_eq!(
            error_body(403, &json!({"Message": "denied"}), None)["error"]["type"],
            "permission_error"
        );
        // A body that is not Bedrock's shape is quoted, not dropped.
        let odd = error_body(500, &json!({"unexpected": true}), None);
        assert!(
            odd["error"]["message"]
                .as_str()
                .unwrap()
                .contains("unexpected")
        );
    }
}
