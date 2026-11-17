/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { describe, expect, it } from 'vitest';

import { dispatchRefusalKind, explainDispatchRefusal, modelErrorText } from '../dispatchRefusal';
import { GATEWAY_MESSAGES, proxyReturned, type GatewayMessageName } from './fixtures/dispatchRefusals';

const KIND: Record<GatewayMessageName, string> = {
    provider_api_url_missing: 'provider_api_url_missing',
    provider_credential_missing: 'provider_credential_missing',
    provider_endpoint_invalid_region: 'provider_endpoint_invalid',
    provider_endpoint_invalid_query: 'provider_endpoint_invalid',
    provider_endpoint_invalid_policy: 'provider_endpoint_invalid',
    provider_endpoint_invalid_unsignable: 'provider_endpoint_invalid',
    unsupported_provider_api_chat_only: 'unsupported_provider_api',
    unsupported_provider_api_embeddings_only: 'unsupported_provider_api',
    unsupported_provider_api_family: 'unsupported_provider_api',
    provider_credential_missing_vertex: 'provider_credential_missing',
    provider_endpoint_invalid_vertex_project: 'provider_endpoint_invalid',
    provider_endpoint_invalid_vertex_location: 'provider_endpoint_invalid',
    provider_endpoint_invalid_vertex_project_id: 'provider_endpoint_invalid',
    unsupported_provider_api_vertex_claude: 'unsupported_provider_api',
    unsupported_provider_api_vertex_gemini: 'unsupported_provider_api',
    unsupported_provider_api_bedrock_claude: 'unsupported_provider_api',
    provider_auth_unsupported_aws: 'provider_auth_unsupported',
    provider_auth_unsupported_gcp: 'provider_auth_unsupported',
    provider_auth_unsupported_azure: 'provider_auth_unsupported',
};

/** What each explanation must name: the field of the model to change. */
const NAMES: Record<GatewayMessageName, RegExp> = {
    provider_api_url_missing: /API URL/,
    provider_credential_missing: /AWS access key ID and secret access key/,
    provider_endpoint_invalid_region: /AWS region/,
    provider_endpoint_invalid_query: /API URL/,
    provider_endpoint_invalid_policy: /API URL/,
    provider_endpoint_invalid_unsignable: /API URL/,
    unsupported_provider_api_chat_only: /Mode/,
    unsupported_provider_api_embeddings_only: /Mode/,
    unsupported_provider_api_family: /not implemented/,
    provider_credential_missing_vertex: /GCP project ID and the service account JSON key/,
    provider_endpoint_invalid_vertex_project: /GCP project ID/,
    provider_endpoint_invalid_vertex_location: /GCP location/,
    provider_endpoint_invalid_vertex_project_id: /GCP project ID/,
    unsupported_provider_api_vertex_claude: /Mode to Chat/,
    unsupported_provider_api_vertex_gemini: /Mode to Chat/,
    unsupported_provider_api_bedrock_claude: /Mode to Chat/,
    provider_auth_unsupported_aws: /Authentication Type/,
    provider_auth_unsupported_gcp: /Authentication Type/,
    provider_auth_unsupported_azure: /API Key/,
};

const names = Object.keys(GATEWAY_MESSAGES) as GatewayMessageName[];

describe('dispatchRefusalKind', () => {
    it.each(names)('recognises %s in the gateway message and in the health check text', (name) => {
        expect(dispatchRefusalKind(GATEWAY_MESSAGES[name])).toBe(KIND[name]);
        expect(dispatchRefusalKind(proxyReturned(GATEWAY_MESSAGES[name]))).toBe(KIND[name]);
    });

    it.each(['bad key', 'Proxy returned 502: upstream down', '', null, undefined])('is null for %j', (text) => {
        expect(dispatchRefusalKind(text)).toBeNull();
    });
});

describe('explainDispatchRefusal', () => {
    it.each(names)('says in plain words what to fix for %s', (name) => {
        const said = explainDispatchRefusal(proxyReturned(GATEWAY_MESSAGES[name]));
        expect(said).toMatch(NAMES[name]);
        // Plain words: not the error type, not the raw body, not a status line.
        expect(said).not.toContain(KIND[name]);
        expect(said).not.toContain('Proxy returned');
        expect(said).not.toContain('{');
    });

    it('says nothing was sent', () => {
        for (const name of names) {
            expect(explainDispatchRefusal(GATEWAY_MESSAGES[name])).toMatch(/Nothing was sent/);
        }
    });

    it('tells the three unsupported cases apart', () => {
        expect(explainDispatchRefusal(GATEWAY_MESSAGES.unsupported_provider_api_chat_only)).toMatch(/Mode to Chat/);
        expect(explainDispatchRefusal(GATEWAY_MESSAGES.unsupported_provider_api_embeddings_only)).toMatch(/Mode to Embedding/);
        expect(explainDispatchRefusal(GATEWAY_MESSAGES.unsupported_provider_api_family)).toMatch(/model family/);
    });

    it('names the cloud the model is on: a Vertex AI model is not told about AWS, nor the reverse', () => {
        const vertex = names.filter((name) => name.includes('vertex'));
        expect(vertex.length).toBe(6);
        for (const name of vertex) {
            const said = explainDispatchRefusal(proxyReturned(GATEWAY_MESSAGES[name]));
            expect(said).toMatch(/Vertex AI/);
            expect(said).not.toMatch(/AWS|Amazon|Bedrock/);
        }
        for (const name of ['provider_credential_missing', 'provider_endpoint_invalid_region', 'unsupported_provider_api_bedrock_claude'] as const) {
            const said = explainDispatchRefusal(proxyReturned(GATEWAY_MESSAGES[name]));
            expect(said).toMatch(/Amazon Bedrock|AWS/);
            expect(said).not.toMatch(/Vertex|Google|GCP/);
        }
    });

    it('says a Vertex AI model with no project has none, and that the gateway has no default', () => {
        expect(explainDispatchRefusal(GATEWAY_MESSAGES.provider_endpoint_invalid_vertex_project)).toBe(
            'This Vertex AI model names no Google Cloud project, and the gateway has no default one. Edit the model and enter GCP project ID (API tab). Nothing was sent to the provider.',
        );
    });

    it('says an authentication type the gateway cannot use for the provider, and what to choose', () => {
        expect(explainDispatchRefusal(GATEWAY_MESSAGES.provider_auth_unsupported_azure)).toBe(
            'The gateway does not sign in to Azure with client credentials (Azure AD). Edit the model, set Authentication Type to API Key and enter the API key of your Azure OpenAI resource (API tab). Nothing was sent to the provider.',
        );
        expect(explainDispatchRefusal(GATEWAY_MESSAGES.provider_auth_unsupported_aws)).toMatch(/AWS credential for Amazon Bedrock models only/);
        expect(explainDispatchRefusal(GATEWAY_MESSAGES.provider_auth_unsupported_gcp)).toMatch(/Google Cloud service account for Vertex AI models only/);
    });

    it('says a stored credential the gateway cannot decrypt has to be entered again, whatever the provider', () => {
        // The gateway's sentence for a row whose stored credential is a ciphertext it cannot open.
        const unreadable = (model: string, provider: string) =>
            `provider_credential_missing: the stored credential of model '${model}' (provider '${provider}') cannot be decrypted with this gateway's ENCRYPTION_KEY, so the model has no credential the gateway can use. Save the model's credential again in the Control Plane, or give the gateway the ENCRYPTION_KEY it was stored under.`;
        const said =
            'The gateway cannot read the stored credential of this model: it was encrypted with another encryption key than the gateway has now. Edit the model and enter its credential again (API tab). Nothing was sent to the provider.';
        expect(explainDispatchRefusal(proxyReturned(unreadable('gpt-5.2', 'openai')))).toBe(said);
        // Not the "has no AWS credential" or "no service account" sentence: one is stored.
        expect(explainDispatchRefusal(unreadable('amazon.nova-pro-v1:0', 'bedrock'))).toBe(said);
        expect(explainDispatchRefusal(unreadable('gemini-2.5-pro', 'vertex_ai'))).toBe(said);
    });

    it('is null for anything else', () => {
        expect(explainDispatchRefusal('bad key')).toBeNull();
        expect(explainDispatchRefusal(undefined)).toBeNull();
    });
});

describe('modelErrorText', () => {
    it('is the explanation for a dispatch refusal', () => {
        expect(modelErrorText(proxyReturned(GATEWAY_MESSAGES.provider_api_url_missing), 'Unknown error'))
            .toBe(explainDispatchRefusal(GATEWAY_MESSAGES.provider_api_url_missing));
    });

    it('is the text as it came for any other error, and the fallback for none', () => {
        expect(modelErrorText('bad key', 'Unknown error')).toBe('bad key');
        expect(modelErrorText(undefined, 'Unknown error')).toBe('Unknown error');
        expect(modelErrorText('', 'Unknown error')).toBe('Unknown error');
    });
});
