// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

use super::LlmAdapter;

pub struct Ai21Adapter;

impl LlmAdapter for Ai21Adapter {
    fn provider_name(&self) -> &str {
        "ai21"
    }

    // Provider-wide drop list migrated to
    // `llm_providers.additional_drop_params` — see the
    // ai21 entry in `init_llm_providers.PROVIDER_SEED_CATALOG`.
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ai21_provider_name() {
        assert_eq!(Ai21Adapter.provider_name(), "ai21");
    }

    // ai21_unsupported_params test removed — drop list moved to the
    // provider seed (covered by `services::providers` tests).

    #[test]
    fn ai21_default_auth_headers() {
        let model = crate::services::llm_adapter::make_test_model("ai21", "jamba-instruct");
        let headers = Ai21Adapter.auth_headers(&model);
        assert_eq!(headers.len(), 1);
        assert_eq!(headers[0].0, "Authorization");
        assert_eq!(headers[0].1, "Bearer sk-test-key");
    }
}
