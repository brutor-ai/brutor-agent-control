// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

use super::LlmAdapter;

pub struct CloudflareAdapter;

impl LlmAdapter for CloudflareAdapter {
    fn provider_name(&self) -> &str {
        "cloudflare"
    }

    fn unsupported_params(&self) -> &'static [&'static str] {
        &[
            "temperature",
            "top_p",
            "n",
            "stop",
            "logprobs",
            "top_logprobs",
            "logit_bias",
            "frequency_penalty",
            "presence_penalty",
            "parallel_tool_calls",
            "tools",
            "tool_choice",
            "response_format",
            "seed",
            "suffix",
            "user",
            "stream_options",
        ]
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cloudflare_provider_name() {
        assert_eq!(CloudflareAdapter.provider_name(), "cloudflare");
    }

    #[test]
    fn cloudflare_unsupported_params_count() {
        let params = CloudflareAdapter.unsupported_params();
        assert_eq!(params.len(), 17);
    }

    #[test]
    fn cloudflare_unsupported_params_includes_tools() {
        let params = CloudflareAdapter.unsupported_params();
        assert!(params.contains(&"tools"));
        assert!(params.contains(&"tool_choice"));
        assert!(params.contains(&"temperature"));
        assert!(params.contains(&"response_format"));
    }
}
