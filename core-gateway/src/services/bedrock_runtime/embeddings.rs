// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! OpenAI `/embeddings` to and from Bedrock Runtime `InvokeModel`, for
//! Amazon Titan Text Embeddings and Cohere Embed v3.
//!
//! * **Titan** (`{"inputText", "dimensions"}` in, `{"embedding",
//!   "inputTextTokenCount"}` out) takes one text per call. An array input
//!   is one call per item, at most [`MAX_CONCURRENT_CALLS`] in flight, the
//!   results in the order of the input, the token counts added.
//! * **Cohere v3** (`{"texts", "input_type"}` in, `{"embeddings"}` out)
//!   takes up to [`COHERE_MAX_TEXTS`] texts per call; a longer input is
//!   split. AWS documents no token count for its reply, so the count is
//!   AWS's own approximation for the model, "1 token is about 4
//!   characters", rounded up per text.
//!
//! One failed call fails the request with Bedrock's status and message: no
//! partial result, no retry. The caller gets floats whatever
//! `encoding_format` asked for (the gateway's rule for every provider but
//! OpenAI and Azure, `providers::prepare_embedding`).

use std::time::Duration;

use futures::StreamExt;
use serde_json::{Value, json};

use super::Family;
use crate::models::db::LlmModel;

/// InvokeModel calls of one embeddings request in flight at once.
pub const MAX_CONCURRENT_CALLS: usize = 4;

/// Texts per Cohere Embed v3 call ("Maximum 96" in AWS's reference).
pub const COHERE_MAX_TEXTS: usize = 96;

/// What one InvokeModel call came to.
enum CallError {
    /// Bedrock answered with an error: its status, and the gateway's error
    /// body for it.
    Http(u16, Value),
    /// Nothing usable came back.
    Transport(reqwest::Error),
}

/// The embeddings and the token count of one call.
struct CallOutput {
    embeddings: Vec<Value>,
    tokens: i64,
}

fn bad_reply(message: &str) -> CallError {
    CallError::Http(
        502,
        json!({"error": {"message": format!("Amazon Bedrock: {message}"),
                         "type": "upstream_error"}}),
    )
}

/// The texts of an OpenAI `input`: a string or an array of strings.
fn texts(input: &Value) -> Result<Vec<String>, String> {
    match input {
        Value::String(s) => Ok(vec![s.clone()]),
        Value::Array(items) if !items.is_empty() => items
            .iter()
            .map(|item| {
                item.as_str().map(str::to_string).ok_or_else(|| {
                    "the embedding models on Amazon Bedrock take text: `input` must be a string \
                     or an array of strings (token arrays are not supported). The request was \
                     not sent."
                        .to_string()
                })
            })
            .collect(),
        _ => Err("`input` must be a string or a non-empty array of strings".to_string()),
    }
}

/// The request bodies of the calls an embeddings request becomes, each
/// with the texts it carries.
fn plan(
    model: &LlmModel,
    family: Family,
    input: &Value,
    dimensions: Option<i32>,
) -> Result<Vec<(Value, Vec<String>)>, String> {
    let texts = texts(input)?;
    match family {
        Family::TitanEmbed => {
            let v2 = super::model_id(model).contains("titan-embed-text-v2");
            match dimensions {
                None => {}
                Some(256 | 512 | 1024) if v2 => {}
                Some(d) => {
                    return Err(format!(
                        "model '{}' does not take dimensions = {d}{}. The request was not sent.",
                        model.model_name,
                        if v2 { " (256, 512 or 1024)" } else { "" }
                    ));
                }
            }
            Ok(texts
                .into_iter()
                .map(|text| {
                    let mut body = json!({"inputText": text});
                    if let Some(d) = dimensions {
                        body["dimensions"] = json!(d);
                    }
                    (body, vec![text])
                })
                .collect())
        }
        Family::CohereEmbed => {
            // Cohere Embed v3 has one size, 1024, and no parameter for it.
            if let Some(d) = dimensions.filter(|d| *d != 1024) {
                return Err(format!(
                    "model '{}' does not take dimensions = {d}: its embeddings have 1024. The \
                     request was not sent.",
                    model.model_name
                ));
            }
            Ok(texts
                .chunks(COHERE_MAX_TEXTS)
                .map(|chunk| {
                    (
                        // `input_type` is required and has no default; an
                        // OpenAI request has no such field. Texts to be
                        // stored and searched are the common case.
                        json!({"texts": chunk, "input_type": "search_document"}),
                        chunk.to_vec(),
                    )
                })
                .collect())
        }
        Family::ConverseChat => Err("not an embedding model".to_string()),
    }
}

/// AWS's approximation for Cohere Embed: one token per four characters.
fn approximate_tokens(texts: &[String]) -> i64 {
    texts
        .iter()
        .map(|t| t.chars().count().div_ceil(4) as i64)
        .sum()
}

fn read(family: Family, reply: &Value, sent: &[String]) -> Result<CallOutput, CallError> {
    match family {
        Family::TitanEmbed => {
            let embedding = reply
                .get("embedding")
                .filter(|e| e.is_array())
                .ok_or_else(|| bad_reply("the reply carries no embedding"))?;
            // Not a zero: without the count there is no cost to record.
            let tokens = reply
                .get("inputTextTokenCount")
                .and_then(Value::as_i64)
                .ok_or_else(|| bad_reply("the reply carries no inputTextTokenCount"))?;
            Ok(CallOutput {
                embeddings: vec![embedding.clone()],
                tokens,
            })
        }
        Family::CohereEmbed => {
            let embeddings = reply
                .get("embeddings")
                .and_then(Value::as_array)
                .filter(|e| e.len() == sent.len() && e.iter().all(Value::is_array))
                .ok_or_else(|| bad_reply("the reply does not carry one embedding per text"))?;
            Ok(CallOutput {
                embeddings: embeddings.clone(),
                tokens: approximate_tokens(sent),
            })
        }
        Family::ConverseChat => Err(bad_reply("not an embedding model")),
    }
}

async fn invoke(
    client: &reqwest::Client,
    model: &LlmModel,
    family: Family,
    body: Value,
    sent: Vec<String>,
) -> Result<CallOutput, CallError> {
    let call = super::signed_call(model, "invoke", &body).map_err(|reason| {
        CallError::Http(
            502,
            json!({"error": {"message": reason, "type": "upstream_error"}}),
        )
    })?;
    let mut request = client
        .post(&call.url)
        .header("Content-Type", "application/json")
        .timeout(Duration::from_secs(
            model.timeout.map(|t| t as u64).unwrap_or(120),
        ));
    for (name, value) in &call.headers {
        request = request.header(name.as_str(), value.as_str());
    }
    let response = request
        .body(call.body)
        .send()
        .await
        .map_err(CallError::Transport)?;
    crate::middleware::run_end::record_upstream_response(
        response.status().as_u16(),
        response.headers(),
    );
    let status = response.status().as_u16();
    let error_type = response
        .headers()
        .get("x-amzn-errortype")
        .and_then(|v| v.to_str().ok())
        .map(str::to_string);
    let reply = crate::services::providers::upstream_json(response).await;
    if !(200..300).contains(&status) {
        return Err(CallError::Http(
            status,
            super::error_body(status, &reply, error_type.as_deref()),
        ));
    }
    read(family, &reply, &sent)
}

fn synthesized(status: u16, body: &Value) -> reqwest::Response {
    let mut response = axum::http::Response::new(reqwest::Body::from(body.to_string()));
    *response.status_mut() =
        axum::http::StatusCode::from_u16(status).unwrap_or(axum::http::StatusCode::BAD_GATEWAY);
    response.headers_mut().insert(
        axum::http::header::CONTENT_TYPE,
        axum::http::HeaderValue::from_static("application/json"),
    );
    reqwest::Response::from(response)
}

/// What the calls of a failed embeddings request had already used: AWS
/// bills each InvokeModel call that succeeded, whatever became of the
/// request it was part of.
#[derive(Clone, Debug)]
pub struct PartialUsage {
    /// The row the calls were made to.
    pub model: LlmModel,
    /// The status the request failed with.
    pub status: u16,
    /// The input tokens of the calls that succeeded.
    pub tokens: i64,
    /// How many calls succeeded, of how many the request needed.
    pub calls_succeeded: usize,
    pub calls_planned: usize,
}

/// Where [`embed`] leaves the usage of a failed request's successful calls,
/// for the route to record: the response of a failed candidate may be
/// discarded for a fallback's.
pub type PartialUsageSink = std::sync::Arc<std::sync::Mutex<Vec<PartialUsage>>>;

/// Serve an OpenAI embeddings request with the Bedrock row `model`. The
/// answer is what an OpenAI-compatible provider would have sent: 200 with
/// an embeddings list and its usage, or the status of the failure with an
/// error body. `Err` when Bedrock could not be reached.
///
/// A request of several calls (Titan: one per text) that fails after some
/// calls succeeded: no new call is started, the calls in flight are awaited
/// (they are billed either way), the usage of every call that succeeded is
/// left in `partial`, and the error body carries it as `usage` with a
/// sentence saying so. No embedding is returned.
pub async fn embed(
    client: &reqwest::Client,
    model: &LlmModel,
    input: &Value,
    dimensions: Option<i32>,
    partial: &PartialUsageSink,
) -> Result<reqwest::Response, reqwest::Error> {
    let Some(family) = super::family(model).filter(|f| *f != Family::ConverseChat) else {
        return Ok(synthesized(
            502,
            &json!({"error": {"message": "not an embedding model the gateway serves on Amazon \
                                          Bedrock", "type": "upstream_error"}}),
        ));
    };
    let calls = match plan(model, family, input, dimensions) {
        Ok(calls) => calls,
        // The caller's to fix: nothing is sent.
        Err(reason) => {
            return Ok(synthesized(
                400,
                &json!({"error": {"message": reason, "type": "invalid_request_error"}}),
            ));
        }
    };

    // In order, a bounded number in flight. The first failure starts no
    // further call; those in flight are awaited, so that what they used is
    // known.
    let calls_planned = calls.len();
    let mut pending = calls.into_iter();
    let mut in_flight = futures::stream::FuturesOrdered::new();
    let mut failure: Option<CallError> = None;
    let mut data = Vec::new();
    let mut tokens = 0i64;
    let mut calls_succeeded = 0usize;
    loop {
        while failure.is_none() && in_flight.len() < MAX_CONCURRENT_CALLS {
            let Some((body, sent)) = pending.next() else {
                break;
            };
            in_flight.push_back(invoke(client, model, family, body, sent));
        }
        let Some(result) = in_flight.next().await else {
            break;
        };
        match result {
            Ok(output) => {
                tokens += output.tokens;
                calls_succeeded += 1;
                if failure.is_none() {
                    for embedding in output.embeddings {
                        data.push(json!({
                            "object": "embedding",
                            "index": data.len(),
                            "embedding": embedding,
                        }));
                    }
                }
            }
            Err(e) => {
                if failure.is_none() {
                    failure = Some(e);
                }
            }
        }
    }
    let Some(failure) = failure else {
        return Ok(synthesized(
            200,
            &json!({
                "object": "list",
                "data": data,
                "model": model.model_name,
                "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
            }),
        ));
    };
    let status = match &failure {
        CallError::Http(status, _) => *status,
        CallError::Transport(_) => 502,
    };
    if calls_succeeded > 0
        && let Ok(mut sink) = partial.lock()
    {
        sink.push(PartialUsage {
            model: model.clone(),
            status,
            tokens,
            calls_succeeded,
            calls_planned,
        });
    }
    match failure {
        CallError::Http(status, mut body) => {
            if calls_succeeded > 0 {
                body["usage"] = json!({"prompt_tokens": tokens, "total_tokens": tokens});
                if let Some(message) = body.pointer("/error/message").and_then(Value::as_str) {
                    body["error"]["message"] = json!(format!(
                        "{message} ({calls_succeeded} of the {calls_planned} calls this request \
                         needed had succeeded and are billed by AWS: {tokens} input tokens, \
                         recorded as usage of this failed request. No embedding is returned.)"
                    ));
                }
            }
            Ok(synthesized(status, &body))
        }
        CallError::Transport(e) => Err(e),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::llm_adapter::make_test_model;

    fn titan() -> LlmModel {
        make_test_model("bedrock", "bedrock/amazon.titan-embed-text-v2:0")
    }

    fn cohere() -> LlmModel {
        make_test_model("bedrock", "bedrock/cohere.embed-english-v3")
    }

    #[test]
    fn titan_is_one_body_per_text() {
        let calls = plan(&titan(), Family::TitanEmbed, &json!(["a", "b"]), Some(512)).unwrap();
        assert_eq!(
            calls.iter().map(|c| c.0.clone()).collect::<Vec<_>>(),
            vec![
                json!({"inputText": "a", "dimensions": 512}),
                json!({"inputText": "b", "dimensions": 512})
            ]
        );
        let one = plan(&titan(), Family::TitanEmbed, &json!("a"), None).unwrap();
        assert_eq!(one[0].0, json!({"inputText": "a"}));
    }

    #[test]
    fn dimensions_a_model_does_not_have_are_the_callers_error() {
        assert!(plan(&titan(), Family::TitanEmbed, &json!("a"), Some(300)).is_err());
        let v1 = make_test_model("bedrock", "bedrock/amazon.titan-embed-text-v1");
        assert!(plan(&v1, Family::TitanEmbed, &json!("a"), Some(256)).is_err());
        assert!(plan(&v1, Family::TitanEmbed, &json!("a"), None).is_ok());
        assert!(plan(&cohere(), Family::CohereEmbed, &json!("a"), Some(256)).is_err());
        assert!(plan(&cohere(), Family::CohereEmbed, &json!("a"), Some(1024)).is_ok());
    }

    #[test]
    fn cohere_is_split_at_ninety_six_texts() {
        let input: Vec<String> = (0..200).map(|n| format!("t{n}")).collect();
        let calls = plan(&cohere(), Family::CohereEmbed, &json!(input), None).unwrap();
        assert_eq!(
            calls.iter().map(|c| c.1.len()).collect::<Vec<_>>(),
            vec![96, 96, 8]
        );
        assert_eq!(calls[2].0["texts"][7], json!("t199"));
        assert_eq!(calls[0].0["input_type"], json!("search_document"));
    }

    #[test]
    fn input_that_is_not_text_is_the_callers_error() {
        for bad in [
            json!([1, 2, 3]),
            json!([[1, 2]]),
            json!([]),
            json!(7),
            json!(null),
        ] {
            assert!(texts(&bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn a_reply_without_its_count_or_its_embeddings_is_an_error() {
        let sent = vec!["abcde".to_string()];
        assert!(read(Family::TitanEmbed, &json!({"embedding": [0.1]}), &sent).is_err());
        assert!(
            read(
                Family::TitanEmbed,
                &json!({"inputTextTokenCount": 2}),
                &sent
            )
            .is_err()
        );
        let ok = read(
            Family::TitanEmbed,
            &json!({"embedding": [0.1], "inputTextTokenCount": 2}),
            &sent,
        );
        assert_eq!(ok.ok().map(|o| o.tokens), Some(2));
        // One embedding per text, or none of it is used.
        assert!(read(Family::CohereEmbed, &json!({"embeddings": []}), &sent).is_err());
        assert!(
            read(
                Family::CohereEmbed,
                &json!({"embeddings": {"float": [[0.1]]}}),
                &sent
            )
            .is_err()
        );
        let ok = read(Family::CohereEmbed, &json!({"embeddings": [[0.1]]}), &sent);
        // Five characters: two tokens.
        assert_eq!(ok.ok().map(|o| o.tokens), Some(2));
    }

    #[test]
    fn the_approximation_rounds_up_per_text() {
        let texts: Vec<String> = ["", "a", "abcd", "abcde"]
            .iter()
            .map(|s| s.to_string())
            .collect();
        // 0, 1, 1 and 2.
        assert_eq!(approximate_tokens(&texts), 4);
    }
}
