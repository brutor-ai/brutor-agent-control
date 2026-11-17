// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

use super::LlmAdapter;

pub struct TogetherFireworksAdapter;

impl LlmAdapter for TogetherFireworksAdapter {
    fn provider_name(&self) -> &str {
        "together_ai"
    }

    // Provider-wide drop list migrated to
    // `llm_providers.additional_drop_params` — see the
    // together_ai entry in `init_llm_providers.PROVIDER_SEED_CATALOG`.
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn together_provider_name() {
        assert_eq!(TogetherFireworksAdapter.provider_name(), "together_ai");
    }
}
