/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * What the gateway needs of a model row before it can send a request for it,
 * as the model form asks for it.
 *
 * The gateway refuses a row it cannot dispatch with 501 and one of four
 * error types (`brutor-gateway-core/src/services/providers.rs`,
 * `dispatch_refusal`); `dispatchRefusal.ts` puts those into words. This file
 * is the form's side: the two things an administrator can leave out.
 */

import { getNumber, getRecord, getString } from '../../../lib/narrow';

/**
 * Must a model of `provider` in `mode` carry its own API URL?
 *
 * The rule is the gateway's, and the control plane holds the one copy of it
 * outside the gateway (`app/services/llm/destination.py`).
 * The console keeps none. It reads the control plane's answer where
 * there is one: `requires_api_url` on a catalog entry, which the control
 * plane computes from the entry's provider and mode and nothing else. So an
 * entry of the same provider and mode answers for a model typed in by hand.
 *
 * `undefined`: no such entry, or an entry without the field. Then the form
 * cannot know, asks for nothing up front, and shows the control plane's
 * refusal (422, `apiUrlRefusal` below) next to the API URL field when it
 * comes. A list kept here would be a second copy that goes stale and
 * disagrees with the control plane's rule, which goes by mode and by route.
 */
export function catalogRequiresApiUrl(
    entries: ReadonlyArray<{ provider_key?: string | null; mode?: string | null; requires_api_url?: boolean | null }>,
    provider: string,
    mode: string,
): boolean | undefined {
    if (!provider) return undefined;
    const entry = entries.find(
        (candidate) =>
            candidate.provider_key === provider && candidate.mode === mode && typeof candidate.requires_api_url === 'boolean',
    );
    return entry?.requires_api_url ?? undefined;
}

/** What the form says when such a model has no API URL. */
export function apiUrlRequiredMessage(provider: string): string {
    return `API URL is required: the gateway knows no address for provider "${provider}".`;
}

/**
 * The control plane's refusal of a model that has no destination, when
 * `error` is one: 422 and a sentence that names `api_url` ("api_url is
 * required: model '...' is of provider '...', ..."), or a validation error of
 * the `api_url` field. The sentence is returned as the control plane wrote
 * it. `error` is what the API client threw, or what a list made of it for
 * its form (`saveFailure`).
 */
export function apiUrlRefusal(error: unknown): string | undefined {
    if (getNumber(error, 'status') !== 422) return undefined;
    const said = getString(getRecord(error, 'details'), 'detail') ?? getString(error, 'message');
    return said !== undefined && /\bapi_url\b/.test(said) ? said : undefined;
}

/** The two Amazon Bedrock providers. The gateway signs both with the model's own AWS credential. */
export const BEDROCK_PROVIDERS: readonly string[] = ['bedrock', 'bedrock_converse'];

export function isBedrockProvider(provider: string | null | undefined): boolean {
    return BEDROCK_PROVIDERS.includes((provider ?? '').trim().toLowerCase());
}

/**
 * The shape of an AWS region name: `eu-west-1`, `us-gov-west-1`,
 * `ap-southeast-2`. The gateway checks again (it accepts any lower-case
 * letters, digits and hyphens up to 32 characters; a region becomes part of
 * a host name), and answers 501 `provider_endpoint_invalid` otherwise.
 */
const AWS_REGION = /^[a-z]{2}(-[a-z]+)+-[0-9]{1,2}$/;

export function isAwsRegionName(region: string): boolean {
    return region.length <= 32 && AWS_REGION.test(region);
}

/**
 * What the administrator typed into the four AWS fields. The three secrets
 * are never filled in from the server; the region starts as the stored one
 * (`aws_region` of the model), which is no secret.
 */
export interface BedrockCredentialInput {
    accessKeyId: string;
    secretAccessKey: string;
    sessionToken: string;
    region: string;
}

export const EMPTY_BEDROCK_CREDENTIAL: BedrockCredentialInput = {
    accessKeyId: '',
    secretAccessKey: '',
    sessionToken: '',
    region: '',
};

/**
 * What the control plane says of a model's stored AWS credential: the
 * region, and for each secret whether one is stored (`aws_region` and
 * `provider_config_keys` of the model). Never a secret.
 */
export interface StoredBedrockCredential {
    accessKeyId: boolean;
    secretAccessKey: boolean;
    sessionToken: boolean;
    region: string;
}

export function storedBedrockCredential(
    model: { aws_region?: string | null; provider_config_keys?: Record<string, boolean> | null } | null | undefined,
): StoredBedrockCredential {
    const keys = model?.provider_config_keys ?? {};
    return {
        accessKeyId: keys.aws_access_key_id === true,
        secretAccessKey: keys.aws_secret_access_key === true,
        sessionToken: keys.aws_session_token === true,
        region: model?.aws_region ?? '',
    };
}

export type BedrockCredentialErrors = Partial<Record<'accessKeyId' | 'secretAccessKey' | 'region', string>>;

/**
 * The AWS region in the host of a Bedrock Runtime address
 * (`https://bedrock-runtime.<region>.amazonaws.com`); '' when the address
 * names none. The gateway reads the region there when the credential has
 * none.
 */
export function bedrockRegionOfApiUrl(apiUrl: string | null | undefined): string {
    const after = (apiUrl ?? '').split('bedrock-runtime.', 2)[1];
    return after !== undefined && after.includes('.') ? after.split('.', 1)[0] : '';
}

/**
 * The form's check of the AWS fields.
 *
 * The control plane merges what is sent into what is stored (a key present
 * replaces, a key absent is kept), so one part can be changed alone: an
 * empty secret field keeps the stored secret. The rules are the gateway's:
 * the access key id and the secret access key go together, whatever else is
 * true; a model that is enabled must have both, and a region. There is no
 * default region: it is the one typed, the one in the model's metadata
 * (`aws_region`), or the one in the host of a Bedrock Runtime API URL, in
 * that order. A model that is switched off is never sent for and may be
 * saved without (a catalog import arrives so).
 */
export function bedrockCredentialErrors(
    input: BedrockCredentialInput,
    {
        stored,
        enabled,
        apiUrl = '',
        metadata,
    }: { stored: StoredBedrockCredential; enabled: boolean; apiUrl?: string; metadata?: unknown },
): BedrockCredentialErrors {
    const region = input.region.trim();
    const hasId = input.accessKeyId.trim() !== '' || stored.accessKeyId;
    const hasSecret = input.secretAccessKey.trim() !== '' || stored.secretAccessKey;
    const errors: BedrockCredentialErrors = {};
    if (!hasId && (enabled || hasSecret)) {
        errors.accessKeyId = enabled
            ? 'AWS access key ID is required for a model that is enabled. Enter it, or save the model disabled.'
            : 'AWS access key ID is required: the gateway signs with the access key ID and the secret access key together.';
    }
    if (!hasSecret && (enabled || hasId)) {
        errors.secretAccessKey = enabled
            ? 'AWS secret access key is required for a model that is enabled. Enter it, or save the model disabled.'
            : 'AWS secret access key is required: the gateway signs with the access key ID and the secret access key together.';
    }
    if (region) {
        if (!isAwsRegionName(region)) errors.region = 'AWS region must be a region name such as eu-west-1.';
    } else if (enabled && !getString(metadata, 'aws_region') && !bedrockRegionOfApiUrl(apiUrl)) {
        errors.region = 'AWS region is required for a model that is enabled: the gateway has no default region. Enter it, or save the model disabled.';
    }
    return errors;
}

/**
 * The `provider_config` to send: the keys that changed, in the shape the
 * gateway reads (`ProviderCredentials` in
 * `brutor-gateway-core/src/services/cloud_auth.rs`). The control plane
 * merges them into what is stored, and `null` removes a key
 * (`removeSessionToken`: a model that moves from temporary credentials to a
 * long-lived key). `undefined` when nothing changed, which sends no
 * `provider_config` and keeps what is stored.
 */
export function bedrockProviderConfig(
    input: BedrockCredentialInput,
    stored: StoredBedrockCredential,
    { removeSessionToken = false }: { removeSessionToken?: boolean } = {},
): Record<string, string | null> | undefined {
    const patch: Record<string, string | null> = {};
    const accessKeyId = input.accessKeyId.trim();
    const secretAccessKey = input.secretAccessKey.trim();
    const sessionToken = input.sessionToken.trim();
    const region = input.region.trim();
    if (accessKeyId) patch.aws_access_key_id = accessKeyId;
    if (secretAccessKey) patch.aws_secret_access_key = secretAccessKey;
    if (region && region !== stored.region) patch.aws_region = region;
    if (sessionToken) patch.aws_session_token = sessionToken;
    else if (removeSessionToken && stored.sessionToken) patch.aws_session_token = null;
    return Object.keys(patch).length > 0 ? patch : undefined;
}

// --- Vertex AI ---------------------------------------------------------------

/**
 * Vertex AI. `vertex` is the same provider as `vertex_ai`. The gateway calls
 * a Vertex model with the model's own service account and uses no Google
 * identity of its own; it needs the project (it has no default one) and
 * takes the location. All three are in `provider_config`
 * (`gcp_project_id`, `gcp_location`, `gcp_credentials_json`), never in
 * `api_key`.
 */
export const VERTEX_PROVIDERS: readonly string[] = ['vertex_ai', 'vertex'];

export function isVertexProvider(provider: string | null | undefined): boolean {
    return VERTEX_PROVIDERS.includes((provider ?? '').trim().toLowerCase());
}

/**
 * What the administrator typed into the three Google Cloud fields. The
 * service-account key is never filled in from the server; the project and
 * the location start as the stored ones, which are no secrets.
 */
export interface VertexCredentialInput {
    projectId: string;
    location: string;
    credentialsJson: string;
}

export const EMPTY_VERTEX_CREDENTIAL: VertexCredentialInput = { projectId: '', location: '', credentialsJson: '' };

/**
 * What the control plane says of a model's stored Google Cloud credential:
 * the project and the location, and whether a service-account key is stored
 * (`gcp_project_id`, `gcp_location` and `provider_config_keys` of the
 * model). Never the key.
 */
export interface StoredVertexCredential {
    projectId: string;
    location: string;
    credentialsJson: boolean;
}

export function storedVertexCredential(
    model:
        | { gcp_project_id?: string | null; gcp_location?: string | null; provider_config_keys?: Record<string, boolean> | null }
        | null
        | undefined,
): StoredVertexCredential {
    return {
        projectId: model?.gcp_project_id ?? '',
        location: model?.gcp_location ?? '',
        credentialsJson: model?.provider_config_keys?.gcp_credentials_json === true,
    };
}

export type VertexCredentialErrors = Partial<Record<keyof VertexCredentialInput, string>>;

/** A location becomes part of a host name: lower-case letters, digits and hyphens (`europe-west4`, `global`). */
const GCP_LOCATION = /^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$/;
/** A project id as the gateway takes it: lower-case letters, digits, `-`, `.` and `:`, starting with a letter or digit. */
const GCP_PROJECT = /^[a-z0-9][a-z0-9.:-]{0,62}$/;

function isServiceAccountKey(text: string): boolean {
    try {
        const parsed: unknown = JSON.parse(text);
        return getString(parsed, 'type') === 'service_account';
    } catch {
        return false;
    }
}

/**
 * The form's check of the Google Cloud fields.
 *
 * An empty key field keeps the stored key. The rules are the gateway's: a
 * model that is enabled must have its own service account, a project and a
 * location. There is no default for either: each is the one typed, or the
 * one in the model's metadata (`project`, `region`). `addressGiven`: a
 * Claude model with an API URL is sent to that address, which needs
 * neither. A model that is switched off may be saved without (a catalog
 * import arrives so).
 */
export function vertexCredentialErrors(
    input: VertexCredentialInput,
    {
        stored,
        enabled,
        addressGiven = false,
        metadata,
    }: { stored: StoredVertexCredential; enabled: boolean; addressGiven?: boolean; metadata?: unknown },
): VertexCredentialErrors {
    const projectId = input.projectId.trim();
    const location = input.location.trim();
    const credentialsJson = input.credentialsJson.trim();
    const errors: VertexCredentialErrors = {};
    if (credentialsJson) {
        if (!isServiceAccountKey(credentialsJson)) {
            errors.credentialsJson =
                'Service account JSON key must be the JSON file downloaded from Google Cloud (an object with "type": "service_account").';
        }
    } else if (enabled && !stored.credentialsJson) {
        errors.credentialsJson =
            'Service account JSON key is required for a model that is enabled: the gateway uses no Google identity of its own. Enter it, or save the model disabled.';
    }
    if (projectId) {
        if (!GCP_PROJECT.test(projectId) || projectId.includes('..')) {
            errors.projectId = 'GCP project ID must be a project ID: lower-case letters, digits and hyphens.';
        }
    } else if (enabled && !addressGiven && !getString(metadata, 'project')) {
        errors.projectId = 'GCP project ID is required for a model that is enabled: the gateway has no default project. Enter it, or save the model disabled.';
    }
    if (location) {
        if (!GCP_LOCATION.test(location)) {
            errors.location = 'GCP location must be a location name: a region such as europe-west4, or global, us or eu.';
        }
    } else if (enabled && !addressGiven && !getString(metadata, 'region')) {
        errors.location =
            'GCP location is required for a model that is enabled: the gateway has no default location. Enter a region such as europe-west4, or global, us or eu; or save the model disabled.';
    }
    return errors;
}

/**
 * The `provider_config` to send for a Vertex AI model: the keys that
 * changed, which the control plane merges into what is stored. The key is
 * sent when one is typed; the project and the location when they differ
 * from the stored ones, and as `null` (which removes the key) when a stored
 * one was cleared. `undefined` when nothing changed, which sends no
 * `provider_config` and keeps what is stored.
 */
export function vertexProviderConfig(
    input: VertexCredentialInput,
    stored: StoredVertexCredential,
): Record<string, string | null> | undefined {
    const patch: Record<string, string | null> = {};
    const projectId = input.projectId.trim();
    const location = input.location.trim();
    const credentialsJson = input.credentialsJson.trim();
    if (projectId !== stored.projectId) patch.gcp_project_id = projectId || null;
    if (location !== stored.location) patch.gcp_location = location || null;
    if (credentialsJson) patch.gcp_credentials_json = credentialsJson;
    return Object.keys(patch).length > 0 ? patch : undefined;
}

/**
 * The `api_key` a cloud model sends: none. The gateway reads no API key of
 * an Amazon Bedrock or Vertex AI model, and the control plane refuses one.
 * A stored model that moves to a cloud provider may carry a key from its
 * old provider: `null` clears it. `undefined` sends nothing.
 */
export function cloudModelApiKey(storedProvider: string | null | undefined, provider: string): null | undefined {
    if (storedProvider == null) return undefined;
    const wasCloud = isBedrockProvider(storedProvider) || isVertexProvider(storedProvider);
    const isCloud = isBedrockProvider(provider) || isVertexProvider(provider);
    return isCloud && !wasCloud ? null : undefined;
}

// --- What the control plane refuses when a model is saved or imported ---------

const MODE_NAMES: Record<string, string> = { chat: 'Chat', embedding: 'Embedding' };

/**
 * The control plane's refusal of a model the gateway could not serve, or of
 * a credential it cannot store, in plain words; `undefined` for any other
 * failure. `error` is what the API client threw, or what a list made of it
 * for its form (`saveFailure`).
 *
 *   422 `unsupported_provider_api: ...`    a cloud model the gateway has no backend for, or not in this mode,
 *                                          or not as a member of a routing group or as a fallback
 *   422 `provider_config: ...`             a credential the gateway could not use
 *   422 `api_key: ...`                     an API key on a cloud model, which has none
 *   503 `... cannot be stored`             no `ENCRYPTION_KEY` on the control plane
 */
export function modelSaveRefusalText(error: unknown): string | undefined {
    const status = getNumber(error, 'status');
    const said = getString(getRecord(error, 'details'), 'detail') ?? getString(error, 'message');
    if (said === undefined) return undefined;
    if (status === 503 && said.startsWith('provider_config cannot be stored')) {
        return 'The cloud credential cannot be stored: this Control Plane has no encryption key (ENCRYPTION_KEY), and a credential is never stored unencrypted. Nothing was saved. Ask whoever runs the installation to set ENCRYPTION_KEY, then save again.';
    }
    if (status === 503 && said.startsWith('api_key cannot be stored')) {
        return 'The API key cannot be stored: this Control Plane has no encryption key (ENCRYPTION_KEY), and a credential is never stored unencrypted. Nothing was saved. Ask whoever runs the installation to set ENCRYPTION_KEY, then save again.';
    }
    if (status !== 422) return undefined;
    // A fallback list names its field first ("fallback_models: unsupported_provider_api: ...").
    const forwarded = /unsupported_provider_api: model '([^']*)' .* cannot be (a member of a routing group|a fallback)/.exec(said);
    if (forwarded) {
        const cloud = said.includes('on Vertex AI only') ? 'Vertex AI' : 'Amazon Bedrock';
        const role = forwarded[2] === 'a fallback' ? 'a fallback' : 'a member of a routing group';
        return `Model "${forwarded[1]}" cannot be ${role}. The gateway serves it on ${cloud} only when a request names it, and would skip it every time as ${role}. Nothing was saved. As members and fallbacks the gateway uses, of the cloud models, only Amazon Nova, Mistral and DeepSeek chat models and Amazon Titan Text and Cohere Embed v3 embedding models on Amazon Bedrock. Choose one of those, or a model of another provider.`;
    }
    if (said.startsWith('api_key:')) {
        return said.includes('Vertex AI')
            ? 'A Vertex AI model has no API key: the gateway calls it with the service account of the model. Nothing was saved. Enter the service account JSON key (API tab) and no API key.'
            : 'An Amazon Bedrock model has no API key: the gateway signs its requests with the AWS credential of the model. Nothing was saved. Enter the AWS access key ID and secret access key (API tab) and no API key.';
    }
    if (said.startsWith('unsupported_provider_api')) {
        const cloud = said.includes('Vertex AI') ? 'Vertex AI' : 'Amazon Bedrock';
        const served = /as an? (\w+) model only/.exec(said)?.[1];
        if (served) {
            const article = /^[aeiou]/.test(served) ? 'an' : 'a';
            return `The gateway serves this model on ${cloud} as ${article} ${served} model only. Nothing was saved. Set Mode to ${MODE_NAMES[served] ?? served} (General tab).`;
        }
        return 'The gateway cannot call this model on Amazon Bedrock. There it serves Anthropic Claude, Amazon Nova, Mistral and DeepSeek chat models, and Amazon Titan Text and Cohere Embed v3 embedding models. Nothing was saved. Choose one of those models.';
    }
    if (said.startsWith('provider_config:')) {
        return `Cloud credential:${said.slice('provider_config:'.length)} Nothing was saved.`;
    }
    return undefined;
}

// --- A model server on "localhost" ---------------------------------------------

/**
 * What to say of an API URL that names the local machine. The gateway sends
 * the requests, so `localhost` is the gateway's machine (its container, when
 * it runs in one), not the administrator's: an Ollama on the administrator's
 * laptop is not there. `undefined` for any other address.
 */
export function localAddressHint(apiUrl: string): string | undefined {
    let host: string;
    try {
        host = new URL(apiUrl.trim()).hostname.toLowerCase();
    } catch {
        return undefined;
    }
    if (host !== 'localhost' && host !== '127.0.0.1' && host !== '[::1]' && host !== '::1') return undefined;
    return 'This address is the gateway\'s own machine, not yours: the gateway sends the requests, and "localhost" is where it runs (its container, when it runs in one). Use an address the gateway can reach, such as http://host.docker.internal:11434 for a model server on the same host as a gateway in Docker.';
}
