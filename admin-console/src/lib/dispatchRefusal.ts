/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The gateway's dispatch refusals, in words an administrator can act on.
 *
 * A model the gateway cannot send for is answered 501 before anything goes
 * out, with a message that starts with one of five error types
 * (`brutor-gateway-core/src/services/providers.rs`, `dispatch_refusal` and
 * `cloud_auth_refusal`). A cloud model, on Amazon Bedrock or on Vertex AI,
 * is served by its cloud's own backend or refused: the words name the cloud
 * the model is on.
 * The console never calls the gateway with a model itself: the control
 * plane's health check, its bulk check and its router test do, and hand the
 * answer on as text (`Proxy returned 501: {"error":{"message":"..."}}`, cut
 * at 500 characters). So the error type is looked for in the text, wherever
 * it is, and the answer is not parsed.
 */

export type DispatchRefusalKind =
    | 'unsupported_provider_api'
    | 'provider_api_url_missing'
    | 'provider_credential_missing'
    | 'provider_endpoint_invalid'
    | 'provider_auth_unsupported';

const KINDS: readonly DispatchRefusalKind[] = [
    'unsupported_provider_api',
    'provider_api_url_missing',
    'provider_credential_missing',
    'provider_endpoint_invalid',
    'provider_auth_unsupported',
];

/** Which of the refusals `text` carries; null when it carries none. */
export function dispatchRefusalKind(text: string | null | undefined): DispatchRefusalKind | null {
    if (!text) return null;
    let first: DispatchRefusalKind | null = null;
    let at = Infinity;
    for (const kind of KINDS) {
        const index = text.indexOf(kind);
        if (index !== -1 && index < at) {
            first = kind;
            at = index;
        }
    }
    return first;
}

const NOTHING_SENT = 'Nothing was sent to the provider.';

/**
 * Is the refused model on Vertex AI? The gateway's sentences for a Vertex AI
 * model say so early ("on Vertex AI", "the Vertex AI endpoint", "Google
 * service account"), within what the control plane keeps of the answer.
 */
const onVertex = (text: string): boolean => /Vertex AI|Google service account/.test(text);

function explain(kind: DispatchRefusalKind, text: string): string {
    switch (kind) {
        case 'provider_api_url_missing':
            return `The gateway has no address to send this model's requests to: the model has no API URL and the gateway knows none for its provider. Edit the model and set API URL (API tab). ${NOTHING_SENT}`;
        case 'provider_credential_missing':
            // A credential is stored and the gateway cannot open it: said
            // before the cloud is asked, because nothing is missing.
            if (text.includes('cannot be decrypted')) {
                return `The gateway cannot read the stored credential of this model: it was encrypted with another encryption key than the gateway has now. Edit the model and enter its credential again (API tab). ${NOTHING_SENT}`;
            }
            if (onVertex(text)) {
                return `This Vertex AI model has no Google Cloud service account of its own, and the gateway uses none of its own. Edit the model and enter the GCP project ID and the service account JSON key (API tab). ${NOTHING_SENT}`;
            }
            return `This Amazon Bedrock model has no AWS credential. Edit the model and enter the AWS access key ID and secret access key, with the AWS region (API tab). ${NOTHING_SENT}`;
        case 'provider_endpoint_invalid':
            if (onVertex(text)) {
                if (text.includes('names no Google Cloud project')) {
                    return `This Vertex AI model names no Google Cloud project, and the gateway has no default one. Edit the model and enter GCP project ID (API tab). ${NOTHING_SENT}`;
                }
                if (text.includes('location is not a location name')) {
                    return `The GCP location of this Vertex AI model is not a location name such as us-central1. Edit the model and correct GCP location (API tab). ${NOTHING_SENT}`;
                }
                if (text.includes('project is not a project id')) {
                    return `The GCP project ID of this Vertex AI model is not a project ID. Edit the model and correct GCP project ID (API tab). ${NOTHING_SENT}`;
                }
                return `The address of this Vertex AI model cannot be built from its GCP project ID, GCP location and model name. Edit the model and correct them (API tab, General tab). ${NOTHING_SENT}`;
            }
            if (text.includes('AWS region')) {
                return `The AWS region of this model is not a region name such as eu-west-1. Edit the model and correct AWS region (API tab). ${NOTHING_SENT}`;
            }
            if (text.includes('query or a fragment')) {
                return `The API URL of this model has a query ("?") or a fragment ("#"), which a signed Amazon Bedrock request cannot carry. Edit the model and remove it from API URL, or clear API URL to use the address of the AWS region (API tab). ${NOTHING_SENT}`;
            }
            if (text.includes('outbound policy')) {
                return `The gateway's outbound policy does not allow the address of this model. Edit the model and correct API URL (API tab), or have the address allowed in the gateway's outbound policy. ${NOTHING_SENT}`;
            }
            return `The address of this Amazon Bedrock model is not one a request can be signed for. Edit the model and correct API URL, or clear it to use the address of the AWS region (API tab). ${NOTHING_SENT}`;
        case 'unsupported_provider_api':
            if (onVertex(text)) {
                return `The gateway serves this Vertex AI model as a chat model only. Edit the model and set Mode to Chat (General tab). ${NOTHING_SENT}`;
            }
            if (text.includes('chat completions route only') || text.includes('chat completions and Messages routes only')) {
                return `The gateway serves this Amazon Bedrock model for chat only. Edit the model and set Mode to Chat (General tab). ${NOTHING_SENT}`;
            }
            if (text.includes('embeddings route only')) {
                return `The gateway serves this Amazon Bedrock model for embeddings only. Edit the model and set Mode to Embedding (General tab). ${NOTHING_SENT}`;
            }
            return `The gateway cannot call this model: its model family is not implemented for Amazon Bedrock. Supported there are Anthropic Claude, Amazon Nova, Mistral and DeepSeek chat models, and Amazon Titan Text and Cohere Embed v3 embedding models. No field of the model changes this; use a supported model. ${NOTHING_SENT}`;
        case 'provider_auth_unsupported':
            if (text.includes("'azure_ad'")) {
                return `The gateway does not sign in to Azure with client credentials (Azure AD). Edit the model, set Authentication Type to API Key and enter the API key of your Azure OpenAI resource (API tab). ${NOTHING_SENT}`;
            }
            if (text.includes("'gcp_service_account'")) {
                return `The gateway signs in with a Google Cloud service account for Vertex AI models only. Edit the model and choose another Authentication Type for its provider (API tab). ${NOTHING_SENT}`;
            }
            if (text.includes("'aws_iam'")) {
                return `The gateway signs requests with an AWS credential for Amazon Bedrock models only. Edit the model and choose another Authentication Type for its provider (API tab). ${NOTHING_SENT}`;
            }
            return `The gateway cannot authenticate with the Authentication Type of this model for its provider. Edit the model and choose another Authentication Type (API tab). ${NOTHING_SENT}`;
    }
}

/** What to fix, when `text` carries a dispatch refusal; null otherwise. */
export function explainDispatchRefusal(text: string | null | undefined): string | null {
    const kind = dispatchRefusalKind(text);
    return kind && text ? explain(kind, text) : null;
}

/**
 * A model's health or test error for display: what to fix when the gateway
 * refused to dispatch it, the text as it came otherwise, `fallback` when
 * there is none.
 */
export function modelErrorText(text: string | null | undefined, fallback: string): string {
    return explainDispatchRefusal(text) ?? (text || fallback);
}
