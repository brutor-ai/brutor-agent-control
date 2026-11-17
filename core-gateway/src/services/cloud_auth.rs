// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The cloud credentials of a model row: the shape of its `provider_config`.
//!
//! The credentials are used by the code that authenticates with them (AWS
//! SigV4, the Google access token); they are never copied into a request.

use serde::{Deserialize, Serialize};

/// Parsed provider credentials from the encrypted `provider_config` JSON.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProviderCredentials {
    // AWS IAM
    pub aws_access_key_id: Option<String>,
    pub aws_secret_access_key: Option<String>,
    pub aws_region: Option<String>,
    pub aws_session_token: Option<String>,

    // Azure AD
    pub azure_client_id: Option<String>,
    pub azure_client_secret: Option<String>,
    pub azure_tenant_id: Option<String>,

    // GCP Service Account
    pub gcp_project_id: Option<String>,
    pub gcp_location: Option<String>,
    pub gcp_credentials_json: Option<String>,
}

/// The `auth_type` values that say a row's credential is a cloud identity
/// in its `provider_config`, not an API key.
pub const CLOUD_AUTH_TYPES: &[&str] = &["aws_iam", "azure_ad", "gcp_service_account"];

// No function here puts a credential in a request header.
// `apply_cloud_auth` wrote the AWS secret access key, the Azure
// client secret and the Google service-account key into `x-brutor-*`
// headers of the request the generic forwarder sent; nothing in the gateway
// read them before the request left, so the upstream received them. A cloud
// credential is used where the request is authenticated with it, and only
// there: SigV4 in `anthropic_adapter::backend::bedrock`, the Google token
// in `anthropic_adapter::backend::vertex`. A row with a cloud `auth_type`
// that would go to the generic forwarder is refused
// (`providers::dispatch_refusal`, `provider_auth_unsupported`).

/// Test support: a test body run in a child process that has made-up
/// ambient cloud credentials, the only process that has any.
#[cfg(test)]
pub(crate) mod ambient_test {
    /// Set in the child, and only there.
    const CHILD_MARKER: &str = "BRUTOR_TEST_AMBIENT_CHILD";

    /// Runs `body` in a child process (this test binary, the one test
    /// `module::test`) whose environment has `env` added and whose home
    /// directory is an empty scratch directory. Fails, without running the
    /// body, if THIS process has a cloud credential variable of its own:
    /// the test does not run next to a real ambient credential, and that is
    /// a failure, not a pass. Only the names are read, never a value.
    pub(crate) fn in_child(module: &str, test: &str, env: &[(&str, &str)], body: impl FnOnce()) {
        if std::env::var_os(CHILD_MARKER).is_some() {
            return body();
        }
        let present: Vec<String> = std::env::vars_os()
            .filter_map(|(name, _)| name.into_string().ok())
            .filter(|name| name.starts_with("AWS_") || name.starts_with("GOOGLE_"))
            .collect();
        assert!(
            present.is_empty(),
            "this process has cloud credential variables of its own ({present:?}). The test \
             does not run next to a real ambient credential: unset them."
        );
        // Without the crate: `brutor_gateway_core::a::tests` is `a::tests`.
        let module = module.split_once("::").map_or(module, |(_, rest)| rest);
        let home = std::env::temp_dir().join(format!(
            "brutor-ambient-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map_or(0, |d| d.as_nanos())
        ));
        std::fs::create_dir_all(&home).expect("scratch home");
        let output = std::process::Command::new(std::env::current_exe().expect("test binary"))
            .args([
                format!("{module}::{test}").as_str(),
                "--exact",
                "--nocapture",
            ])
            .env(CHILD_MARKER, "1")
            .env("HOME", &home)
            .envs(env.iter().copied())
            .output()
            .expect("run the child");
        let _removed = std::fs::remove_dir_all(&home);
        let stdout = String::from_utf8_lossy(&output.stdout);
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(
            output.status.success() && stdout.contains("1 passed"),
            "the child `{test}` did not pass:\n{stdout}\n{stderr}"
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn aws_creds() -> ProviderCredentials {
        ProviderCredentials {
            aws_access_key_id: Some("AKIAIOSFODNN7EXAMPLE".into()),
            aws_secret_access_key: Some("wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY".into()),
            aws_region: Some("us-west-2".into()),
            aws_session_token: Some("FwoGZXIvYXdzE...".into()),
            azure_client_id: None,
            azure_client_secret: None,
            azure_tenant_id: None,
            gcp_project_id: None,
            gcp_location: None,
            gcp_credentials_json: None,
        }
    }

    // ── ProviderCredentials serialization ─────────────────────────────

    #[test]
    fn provider_credentials_roundtrip() {
        let creds = aws_creds();
        let json = serde_json::to_string(&creds).unwrap();
        let parsed: ProviderCredentials = serde_json::from_str(&json).unwrap();
        assert_eq!(
            parsed.aws_access_key_id.as_deref(),
            Some("AKIAIOSFODNN7EXAMPLE")
        );
        assert_eq!(parsed.aws_region.as_deref(), Some("us-west-2"));
    }

    #[test]
    fn provider_credentials_partial_json() {
        // Only AWS fields present — all others should default to None
        let json = r#"{"aws_access_key_id":"AKIA","aws_secret_access_key":"secret"}"#;
        let parsed: ProviderCredentials = serde_json::from_str(json).unwrap();
        assert_eq!(parsed.aws_access_key_id.as_deref(), Some("AKIA"));
        assert!(parsed.azure_client_id.is_none());
        assert!(parsed.gcp_project_id.is_none());
    }

    /// This module has no function that takes request headers.
    #[test]
    fn nothing_here_writes_a_header() {
        let src = std::fs::read_to_string(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/src/services/cloud_auth.rs"
        ))
        .expect("module source");
        let production = src.split("#[cfg(test)]").next().unwrap_or_default();
        for forbidden in ["headers", "push((", "Vec<(String, String)>"] {
            assert!(
                !production
                    .lines()
                    .filter(|l| !l.trim_start().starts_with("//"))
                    .any(|l| l.contains(forbidden)),
                "cloud_auth.rs handles request headers again (`{forbidden}`)"
            );
        }
    }
}
