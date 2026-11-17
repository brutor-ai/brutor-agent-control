/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The gateway's 501 dispatch refusals, as they reach the console.
 *
 * The messages are the gateway's own, copied from
 * `brutor-gateway-core/src/services/providers.rs` (`upstream_base_for`,
 * `unsupported_provider_api`) and `src/services/bedrock_runtime/mod.rs`
 * (`region`, `endpoint_url`, `missing_credential`), with a
 * model and a provider filled in. The control plane's health check and
 * router test pass the gateway's answer on as
 * `Proxy returned <status>: <first 500 characters of the body>`
 * (`llm_models_manager.py`, `llm_router_service.py`).
 */

export const GATEWAY_MESSAGES = {
    provider_api_url_missing:
        "provider_api_url_missing: model 'llama3' of provider 'ollama' has no api_url, and the gateway knows no LLM base URL for provider 'ollama'. Nothing was sent. Set api_url on the model.",
    provider_credential_missing:
        "provider_credential_missing: model 'amazon.nova-pro-v1:0' of provider 'bedrock_converse' has no AWS credential (an access key id and a secret access key in its provider configuration). Nothing was sent: a Bedrock request is not sent unsigned, and the gateway uses no credential of its own environment.",
    provider_endpoint_invalid_region:
        "provider_endpoint_invalid: the AWS region of model 'amazon.nova-pro-v1:0' (provider 'bedrock_converse') is not a region name. Nothing was sent.",
    provider_endpoint_invalid_query:
        "provider_endpoint_invalid: the api_url of model 'amazon.nova-pro-v1:0' (provider 'bedrock_converse') has a query or a fragment, which a signed Bedrock request cannot carry. Nothing was sent.",
    provider_endpoint_invalid_policy:
        "provider_endpoint_invalid: the endpoint of model 'amazon.nova-pro-v1:0' is refused by the outbound policy (private address). Nothing was sent.",
    provider_endpoint_invalid_unsignable:
        "provider_endpoint_invalid: the endpoint of model 'amazon.nova-pro-v1:0' is not a URL a Bedrock request can be signed for. Nothing was sent.",
    unsupported_provider_api_chat_only:
        "unsupported_provider_api: the gateway serves model 'amazon.nova-pro-v1:0' (provider 'bedrock_converse') on Amazon Bedrock through the Converse API, on the chat completions route only. This route is not implemented for it.",
    unsupported_provider_api_embeddings_only:
        "unsupported_provider_api: the gateway serves model 'amazon.titan-embed-text-v2:0' (provider 'bedrock') on Amazon Bedrock through InvokeModel, on the embeddings route only. This route is not implemented for it.",
    unsupported_provider_api_family:
        "unsupported_provider_api: the gateway does not implement the Amazon Bedrock API for model 'meta.llama3-70b-instruct-v1:0' (provider 'bedrock'). On Bedrock it serves Anthropic Claude models (InvokeModel), the Amazon Nova, Mistral and DeepSeek chat models (Converse) and the Amazon Titan Text and Cohere Embed v3 embedding models (InvokeModel); the request bodies of other model families are not implemented.",
    // A cloud row is served by its own backend or refused,
    // on Vertex AI as on Amazon Bedrock (`providers.rs` `unsupported_cloud_api`
    // and `cloud_auth_refusal`, `anthropic_adapter/backend/vertex.rs`,
    // `cloud_hosts.rs`).
    provider_credential_missing_vertex:
        "provider_credential_missing: model 'gemini-2.5-pro' (provider 'vertex_ai') has no Google service account of its own. A Vertex AI request is authenticated with the model's own service account (the model's cloud credentials, `gcp_credentials_json`), never with the gateway's own Google identity. Nothing was sent.",
    provider_endpoint_invalid_vertex_project:
        "provider_endpoint_invalid: the Vertex AI endpoint of model 'gemini-2.5-pro' (provider 'vertex_ai') cannot be built: the model names no Google Cloud project (set the project in the model's cloud credentials). Nothing was sent.",
    provider_endpoint_invalid_vertex_location:
        "provider_endpoint_invalid: the Vertex AI endpoint of model 'gemini-2.5-pro' (provider 'vertex') cannot be built: the Google Cloud location is not a location name (lower-case letters, digits and '-'). Nothing was sent.",
    provider_endpoint_invalid_vertex_project_id:
        "provider_endpoint_invalid: the Vertex AI endpoint of model 'gemini-2.5-pro' (provider 'vertex_ai') cannot be built: the Google Cloud project is not a project id. Nothing was sent.",
    unsupported_provider_api_vertex_claude:
        "unsupported_provider_api: the gateway serves model 'claude-sonnet-4-5@20250929' (provider 'vertex_ai') on Vertex AI through rawPredict with the Anthropic Messages body, on the chat completions and Messages routes only. This route is not implemented for it, and a Vertex AI model is never sent by a forwarder that authenticates with an API key.",
    unsupported_provider_api_vertex_gemini:
        "unsupported_provider_api: the gateway serves model 'gemini-2.5-pro' (provider 'vertex_ai') on Vertex AI through generateContent, on the Gemini route (and a Gemini model on the chat completions route too). This route is not implemented for it, and a Vertex AI model is never sent by a forwarder that authenticates with an API key.",
    unsupported_provider_api_bedrock_claude:
        "unsupported_provider_api: the gateway serves model 'anthropic.claude-sonnet-4-5-20250929-v1:0' (provider 'bedrock') on Amazon Bedrock through InvokeModel with the Anthropic Messages body, on the chat completions and Messages routes only. This route is not implemented for it, and a Bedrock request is never sent unsigned.",
    provider_auth_unsupported_aws:
        "provider_auth_unsupported: model 'gpt-4o' of provider 'openai' has auth type 'aws_iam', which the gateway cannot authenticate with on this route: AWS IAM authentication (SigV4) is implemented for Amazon Bedrock models on the routes that serve them, not here. Nothing was sent, and the model's cloud credential is never sent to a provider.",
    provider_auth_unsupported_gcp:
        "provider_auth_unsupported: model 'gpt-4o' of provider 'openai' has auth type 'gcp_service_account', which the gateway cannot authenticate with on this route: Google service-account authentication is implemented for Vertex AI models on the chat, Messages and Gemini routes, not here. Nothing was sent, and the model's cloud credential is never sent to a provider.",
    provider_auth_unsupported_azure:
        "provider_auth_unsupported: model 'gpt-4o' of provider 'azure' has auth type 'azure_ad', which the gateway cannot authenticate with on this route: Azure AD (client credentials) authentication is not implemented; give the model the resource's API key. Nothing was sent, and the model's cloud credential is never sent to a provider.",
} as const;

export type GatewayMessageName = keyof typeof GATEWAY_MESSAGES;

/** The health check's `error_message` for a gateway answer on the OpenAI surface. */
export function proxyReturned(message: string): string {
    return `Proxy returned 501: ${JSON.stringify({ error: { message, type: 'api_error' } })}`.slice(0, 500 + 'Proxy returned 501: '.length);
}
