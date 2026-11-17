// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

use super::LlmAdapter;

pub struct DeepseekAdapter;

impl LlmAdapter for DeepseekAdapter {
    fn provider_name(&self) -> &str {
        "deepseek"
    }

    // Provider-wide drop list migrated to
    // `llm_providers.additional_drop_params` — see the
    // deepseek entry in `init_llm_providers.PROVIDER_SEED_CATALOG`.
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn deepseek_provider_name() {
        assert_eq!(DeepseekAdapter.provider_name(), "deepseek");
    }
}
