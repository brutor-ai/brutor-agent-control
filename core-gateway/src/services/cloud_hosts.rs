// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # The hosts of the clouds the gateway calls, built in one place
//!
//! A cloud endpoint whose host is built from configuration (an AWS region,
//! a Google Cloud location) is built here and nowhere else:
//! a region such as `x.attacker.example/` put inside
//! `https://bedrock-runtime.{region}.amazonaws.com` is a host under another
//! domain, and the signed request, with the prompt, went there. A value that
//! is not a region name builds no URL; the caller refuses the request before
//! any connection (`providers::dispatch_refusal`, 501
//! `provider_endpoint_invalid`).
//!
//! `tests/cloud_host_guard.rs` keeps every other file from formatting a
//! host of `amazonaws.com` or `googleapis.com`.
//!
//! Callers: the Claude backend on Bedrock
//! (`anthropic_adapter::backend::bedrock`), Converse and the Bedrock
//! embeddings (`bedrock_runtime`), the Bedrock guardrail
//! (`guardrails::bedrock`), the Claude backend on Vertex AI
//! (`anthropic_adapter::backend::vertex`) and the Gemini route's Vertex
//! branch (`routes::gemini`). The media store's S3 client and the KMS
//! envelope build no host themselves (the AWS SDK does, from the operator's
//! `BRUTOR_MEDIA_S3_REGION`; the KMS unwrap is not implemented in this binary).

/// The one rule for a name that becomes a DNS label of a cloud host: 1 to
/// 32 of `a-z 0-9 -`, not starting or ending with `-`. Every AWS region
/// (`eu-west-1`, `us-gov-west-1`) and every Google Cloud location
/// (`europe-west4`, `us`, `global`) is one; nothing with a `.`, `/`, `@`,
/// `:`, `#`, `?`, whitespace or an upper-case letter is.
fn is_host_label(name: &str) -> bool {
    (1..=32).contains(&name.len())
        && name
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
        && !name.starts_with('-')
        && !name.ends_with('-')
}

/// `region` if it is an AWS region name; the reason otherwise.
pub fn aws_region(region: &str) -> Result<&str, String> {
    if is_host_label(region) {
        Ok(region)
    } else {
        Err("the AWS region is not a region name (lower-case letters, digits and '-')".to_string())
    }
}

/// The base URL of an AWS service in a region:
/// `https://<service>.<region>.amazonaws.com`. `service` is the caller's
/// constant (`bedrock-runtime`), `region` is configuration and is checked.
pub fn aws_service_base(service: &'static str, region: &str) -> Result<String, String> {
    let region = aws_region(region)?;
    Ok(format!("https://{service}.{region}.amazonaws.com"))
}

/// The Bedrock Runtime endpoint of a region.
pub fn bedrock_runtime_base(region: &str) -> Result<String, String> {
    aws_service_base("bedrock-runtime", region)
}

/// `location` if it is a Google Cloud location name; the reason otherwise.
pub fn gcp_location(location: &str) -> Result<&str, String> {
    if is_host_label(location) {
        Ok(location)
    } else {
        Err(
            "the Google Cloud location is not a location name (lower-case letters, digits \
             and '-')"
                .to_string(),
        )
    }
}

/// `project` if it can be a Google Cloud project id in a URL path: 1 to 63
/// of `a-z 0-9 - . :` (a domain-scoped id is `example.com:project`),
/// starting with a letter or digit. No `/`, `?`, `#`, `%` or whitespace: it
/// is one path segment.
pub fn gcp_project(project: &str) -> Result<&str, String> {
    let ok = (1..=63).contains(&project.len())
        && project.bytes().all(|b| {
            b.is_ascii_lowercase() || b.is_ascii_digit() || matches!(b, b'-' | b'.' | b':')
        })
        && project
            .bytes()
            .next()
            .is_some_and(|b| b.is_ascii_lowercase() || b.is_ascii_digit())
        && !project.contains("..");
    if ok {
        Ok(project)
    } else {
        Err("the Google Cloud project is not a project id".to_string())
    }
}

/// `model` if it is one path segment of a Vertex AI publisher model
/// (`claude-sonnet-5-5`, `claude-sonnet-4-5@20250929`, `gemini-2.5-pro`).
fn vertex_model(model: &str) -> Result<&str, String> {
    let ok = (1..=128).contains(&model.len())
        && model
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'.' | b'_' | b'@'))
        && !model.contains("..");
    if ok {
        Ok(model)
    } else {
        Err("the Vertex AI model name is not a model id".to_string())
    }
}

/// The Vertex AI host of a location. Google Cloud documents three
/// shapes ("Deployments and endpoints":
/// <https://docs.cloud.google.com/vertex-ai/generative-ai/docs/learn/locations>):
///
/// * a region: `<region>-aiplatform.googleapis.com`;
/// * the global endpoint, location `global`: `aiplatform.googleapis.com`;
/// * a multi-region endpoint, location `us` or `eu`:
///   `aiplatform.<location>.rep.googleapis.com`.
///
/// The path names the location in all three (`…/locations/<location>/…`).
pub fn vertex_host(location: &str) -> Result<String, String> {
    Ok(match gcp_location(location)? {
        "global" => "aiplatform.googleapis.com".to_string(),
        multi_region @ ("us" | "eu") => format!("aiplatform.{multi_region}.rep.googleapis.com"),
        region => format!("{region}-aiplatform.googleapis.com"),
    })
}

/// The URL of a Vertex AI publisher model, without the `:<method>` suffix:
/// `https://<host>/v1/projects/<project>/locations/<location>/publishers/<publisher>/models/<model>`,
/// `<host>` being [`vertex_host`] of the location.
/// `publisher` is the caller's constant (`anthropic`, `google`); the rest is
/// configuration and is checked.
pub fn vertex_publisher_model_url(
    location: &str,
    project: &str,
    publisher: &'static str,
    model: &str,
) -> Result<String, String> {
    let host = vertex_host(location)?;
    let project = gcp_project(project)?;
    let model = vertex_model(model)?;
    Ok(format!(
        "https://{host}/v1/projects/{project}/locations/{location}/publishers/{publisher}/models/{model}"
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    const NOT_NAMES: &[&str] = &[
        "x.attacker.example/",
        "eu-west-1.attacker.example",
        "attacker.example#",
        "eu west",
        "",
        "-eu",
        "eu-",
        "EU-WEST-1",
        "eu-west-1#",
        "eu-west-1@evil",
        "eu-west-1:443",
        "eu-west-1/..",
        "eu-west-1?x",
        "eu-west-1\n",
        "eu%2ewest",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    ];

    #[test]
    fn a_region_is_a_region_name_or_builds_nothing() {
        for good in ["eu-west-1", "us-east-1", "us-gov-west-1", "ap-southeast-4"] {
            assert_eq!(
                bedrock_runtime_base(good).unwrap(),
                format!("https://bedrock-runtime.{good}.amazonaws.com")
            );
        }
        for bad in NOT_NAMES {
            assert!(aws_region(bad).is_err(), "{bad:?}");
            assert!(bedrock_runtime_base(bad).is_err(), "{bad:?}");
        }
    }

    #[test]
    fn the_host_of_every_url_built_here_is_the_clouds() {
        let url = url::Url::parse(&bedrock_runtime_base("eu-west-1").unwrap()).unwrap();
        assert_eq!(
            url.host_str(),
            Some("bedrock-runtime.eu-west-1.amazonaws.com")
        );
        let url = url::Url::parse(
            &vertex_publisher_model_url("europe-west4", "p-1", "anthropic", "claude-sonnet-5-5")
                .unwrap(),
        )
        .unwrap();
        assert_eq!(
            url.host_str(),
            Some("europe-west4-aiplatform.googleapis.com")
        );
        assert_eq!(
            url.path(),
            "/v1/projects/p-1/locations/europe-west4/publishers/anthropic/models/claude-sonnet-5-5"
        );
    }

    /// Three kinds of Vertex AI location, three host
    /// shapes (Google Cloud, "Deployments and endpoints", "Specify the
    /// global endpoint" and "Specify the multi-region endpoint":
    /// <https://docs.cloud.google.com/vertex-ai/generative-ai/docs/learn/locations>):
    /// a region is `<region>-aiplatform.googleapis.com`; `global` is
    /// `aiplatform.googleapis.com`; the multi-regions `us` and `eu` are
    /// `aiplatform.<us|eu>.rep.googleapis.com`. The path names the location
    /// in all three. The builder made `global-aiplatform.googleapis.com` and
    /// `us-aiplatform.googleapis.com`, which are not hosts of the service.
    #[test]
    fn the_host_of_a_vertex_location_is_the_one_google_documents() {
        for (location, host) in [
            ("europe-west4", "europe-west4-aiplatform.googleapis.com"),
            ("us-central1", "us-central1-aiplatform.googleapis.com"),
            ("global", "aiplatform.googleapis.com"),
            ("us", "aiplatform.us.rep.googleapis.com"),
            ("eu", "aiplatform.eu.rep.googleapis.com"),
        ] {
            let built =
                vertex_publisher_model_url(location, "p-1", "google", "gemini-2.5-pro").unwrap();
            let url = url::Url::parse(&built).unwrap();
            assert_eq!(url.scheme(), "https");
            assert_eq!(url.host_str(), Some(host), "{location}");
            assert_eq!(
                url.path(),
                format!(
                    "/v1/projects/p-1/locations/{location}/publishers/google/models/gemini-2.5-pro"
                ),
                "{location}"
            );
            assert_eq!(vertex_host(location).unwrap(), host);
        }
        // A name that only looks like one of the three is a region.
        assert_eq!(
            vertex_host("global1").unwrap(),
            "global1-aiplatform.googleapis.com"
        );
        assert_eq!(
            vertex_host("us-east5").unwrap(),
            "us-east5-aiplatform.googleapis.com"
        );
    }

    #[test]
    fn a_location_a_project_and_a_model_are_names_or_build_nothing() {
        for bad in NOT_NAMES {
            assert!(
                vertex_publisher_model_url(bad, "p", "google", "gemini-2.5-pro").is_err(),
                "location {bad:?}"
            );
        }
        for bad in [
            "",
            "p/../../x",
            "p?x=1",
            "p#f",
            "p q",
            "P",
            "p%2f",
            "-p",
            "p..q",
        ] {
            assert!(
                vertex_publisher_model_url("us-central1", bad, "google", "gemini-2.5-pro").is_err(),
                "project {bad:?}"
            );
            assert!(gcp_project(bad).is_err(), "{bad:?}");
        }
        for bad in ["", "m/x", "m?x", "m#x", "m:predict", "m n", "..", "m%2f"] {
            assert!(
                vertex_publisher_model_url("us-central1", "p", "google", bad).is_err(),
                "model {bad:?}"
            );
        }
        for good in ["my-project", "example.com:my-project", "p123456"] {
            assert!(gcp_project(good).is_ok(), "{good}");
        }
        for good in [
            "claude-sonnet-4-5@20250929",
            "gemini-2.5-pro",
            "claude-opus-4-5",
        ] {
            assert!(
                vertex_publisher_model_url("global", "p", "anthropic", good).is_ok(),
                "{good}"
            );
        }
    }
}
