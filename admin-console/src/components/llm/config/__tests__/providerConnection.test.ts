/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { describe, expect, it } from 'vitest';

import {
    BEDROCK_PROVIDERS,
    apiUrlRefusal,
    bedrockCredentialErrors,
    bedrockProviderConfig,
    bedrockRegionOfApiUrl,
    cloudModelApiKey,
    isAwsRegionName,
    catalogRequiresApiUrl,
    isBedrockProvider,
    isVertexProvider,
    localAddressHint,
    modelSaveRefusalText,
    storedBedrockCredential,
    storedVertexCredential,
    VERTEX_PROVIDERS,
    vertexCredentialErrors,
    vertexProviderConfig,
} from '../providerConnection';
import { saveFailure } from '../../../../lib/validationErrors';
import { thrownByClient } from '../../../../test/serverAnswers';

const typed = { accessKeyId: 'AKIAUNITTEST', secretAccessKey: 'unit-secret', sessionToken: '', region: 'eu-west-1' };
const empty = { accessKeyId: '', secretAccessKey: '', sessionToken: '', region: '' };

describe('Bedrock providers', () => {
    it('are bedrock and bedrock_converse, and nothing else', () => {
        expect([...BEDROCK_PROVIDERS]).toEqual(['bedrock', 'bedrock_converse']);
        expect(isBedrockProvider('bedrock')).toBe(true);
        expect(isBedrockProvider('bedrock_converse')).toBe(true);
        for (const other of ['sagemaker', 'anthropic', 'openai', 'vertex_ai', '', null, undefined]) {
            expect(isBedrockProvider(other)).toBe(false);
        }
    });
});

describe('isAwsRegionName', () => {
    it.each(['us-east-1', 'eu-west-1', 'ap-southeast-2', 'us-gov-west-1', 'cn-north-1', 'eu-isoe-west-1', 'il-central-1'])(
        'accepts %s',
        (region) => expect(isAwsRegionName(region)).toBe(true),
    );

    // The second is the gateway's own example of a region that sends a
    // signed request to another host.
    it.each(['', 'x.attacker.example/', 'EU-WEST-1', 'eu-west', 'eu west 1', '-eu-west-1', 'eu-west-1-', 'https://eu-west-1', 'eu-west-1.evil'])(
        'refuses %j',
        (region) => expect(isAwsRegionName(region)).toBe(false),
    );
});

const NOTHING_STORED = storedBedrockCredential(null);
const ALL_STORED = storedBedrockCredential({
    aws_region: 'eu-west-1',
    provider_config_keys: { aws_access_key_id: true, aws_secret_access_key: true, aws_session_token: true },
});
// The form of a stored model starts with its region and nothing else.
const untouchedStored = { ...empty, region: 'eu-west-1' };

describe('storedBedrockCredential', () => {
    it('is what the control plane says of the stored credential: the region, and which secrets are set', () => {
        expect(ALL_STORED).toEqual({ accessKeyId: true, secretAccessKey: true, sessionToken: true, region: 'eu-west-1' });
        expect(
            storedBedrockCredential({ aws_region: null, provider_config_keys: { aws_access_key_id: true, aws_secret_access_key: true, aws_session_token: false } }),
        ).toEqual({ accessKeyId: true, secretAccessKey: true, sessionToken: false, region: '' });
    });

    it('is nothing for a new model and for a model with no credential', () => {
        expect(NOTHING_STORED).toEqual({ accessKeyId: false, secretAccessKey: false, sessionToken: false, region: '' });
        expect(storedBedrockCredential({ provider_config_keys: {} })).toEqual(NOTHING_STORED);
    });
});

describe('bedrockCredentialErrors', () => {
    it('asks for nothing when a credential is stored and nothing is changed', () => {
        expect(bedrockCredentialErrors(untouchedStored, { stored: ALL_STORED, enabled: true })).toEqual({});
    });

    it('asks for the key id, the secret and the region of a model that is enabled', () => {
        const errors = bedrockCredentialErrors(empty, { stored: NOTHING_STORED, enabled: true });
        expect(Object.keys(errors).sort()).toEqual(['accessKeyId', 'region', 'secretAccessKey']);
        expect(errors.region).toBe(
            'AWS region is required for a model that is enabled: the gateway has no default region. Enter it, or save the model disabled.',
        );
        expect(errors.accessKeyId).toMatch(/or save the model disabled/);
    });

    it('asks for nothing of a model that is switched off: it is never sent for', () => {
        expect(bedrockCredentialErrors(empty, { stored: NOTHING_STORED, enabled: false })).toEqual({});
        // A region alone may be saved on it.
        expect(bedrockCredentialErrors({ ...empty, region: 'eu-west-1' }, { stored: NOTHING_STORED, enabled: false })).toEqual({});
    });

    it('lets one part of a stored credential be changed alone: the control plane keeps the rest', () => {
        expect(bedrockCredentialErrors({ ...untouchedStored, region: 'eu-central-1' }, { stored: ALL_STORED, enabled: true })).toEqual({});
        expect(bedrockCredentialErrors({ ...untouchedStored, secretAccessKey: 'new' }, { stored: ALL_STORED, enabled: true })).toEqual({});
    });

    it('asks for the other half when one half of the key pair is typed and neither is stored, enabled or not', () => {
        expect(bedrockCredentialErrors({ ...empty, accessKeyId: 'AKIAUNITTEST', region: 'eu-west-1' }, { stored: NOTHING_STORED, enabled: false }))
            .toEqual({ secretAccessKey: 'AWS secret access key is required: the gateway signs with the access key ID and the secret access key together.' });
    });

    it('asks for the region of an enabled model when a stored one is cleared, and has no default to fall back to', () => {
        expect(Object.keys(bedrockCredentialErrors(empty, { stored: ALL_STORED, enabled: true }))).toEqual(['region']);
        expect(bedrockCredentialErrors(empty, { stored: ALL_STORED, enabled: false })).toEqual({});
    });

    it('takes the region from the host of a Bedrock Runtime API URL, as the gateway does', () => {
        expect(bedrockRegionOfApiUrl('https://bedrock-runtime.eu-north-1.amazonaws.com')).toBe('eu-north-1');
        expect(bedrockRegionOfApiUrl('https://proxy.example.test/bedrock')).toBe('');
        expect(bedrockRegionOfApiUrl('')).toBe('');
        expect(
            bedrockCredentialErrors(empty, { stored: ALL_STORED, enabled: true, apiUrl: 'https://bedrock-runtime.eu-north-1.amazonaws.com' }),
        ).toEqual({});
        expect(Object.keys(bedrockCredentialErrors(empty, { stored: ALL_STORED, enabled: true, apiUrl: 'https://proxy.example.test' }))).toEqual(['region']);
    });

    it('does not ask for a region the model names in its metadata, where the gateway reads it too', () => {
        expect(bedrockCredentialErrors(empty, { stored: ALL_STORED, enabled: true, metadata: { aws_region: 'eu-west-1' } })).toEqual({});
        expect(Object.keys(bedrockCredentialErrors(empty, { stored: ALL_STORED, enabled: true, metadata: { aws_region: 7 } }))).toEqual(['region']);
    });

    it('never asks for the session token', () => {
        expect(bedrockCredentialErrors(typed, { stored: NOTHING_STORED, enabled: true })).toEqual({});
    });

    it('refuses a region that is not a region name', () => {
        expect(bedrockCredentialErrors({ ...typed, region: 'x.attacker.example/' }, { stored: NOTHING_STORED, enabled: false }))
            .toEqual({ region: 'AWS region must be a region name such as eu-west-1.' });
    });
});

describe('bedrockProviderConfig', () => {
    it('is the object the gateway reads, for a model with nothing stored', () => {
        expect(bedrockProviderConfig({ ...typed, sessionToken: ' tok ' }, NOTHING_STORED)).toEqual({
            aws_access_key_id: 'AKIAUNITTEST',
            aws_secret_access_key: 'unit-secret',
            aws_region: 'eu-west-1',
            aws_session_token: 'tok',
        });
    });

    it('has no session token key when none is typed', () => {
        expect(Object.keys(bedrockProviderConfig(typed, NOTHING_STORED) ?? {}).sort())
            .toEqual(['aws_access_key_id', 'aws_region', 'aws_secret_access_key']);
    });

    it('is absent when nothing is changed, which keeps what is stored', () => {
        expect(bedrockProviderConfig(empty, NOTHING_STORED)).toBeUndefined();
        expect(bedrockProviderConfig(untouchedStored, ALL_STORED)).toBeUndefined();
    });

    it('has only the keys that changed: the control plane merges them into what is stored', () => {
        expect(bedrockProviderConfig({ ...untouchedStored, region: 'eu-central-1' }, ALL_STORED)).toEqual({ aws_region: 'eu-central-1' });
        expect(bedrockProviderConfig({ ...untouchedStored, secretAccessKey: 'rotated' }, ALL_STORED)).toEqual({ aws_secret_access_key: 'rotated' });
        expect(bedrockProviderConfig({ ...untouchedStored, accessKeyId: 'AKIANEW', secretAccessKey: 'rotated' }, ALL_STORED)).toEqual({
            aws_access_key_id: 'AKIANEW',
            aws_secret_access_key: 'rotated',
        });
    });

    it('removes the stored session token with null when asked to', () => {
        expect(bedrockProviderConfig(untouchedStored, ALL_STORED, { removeSessionToken: true })).toEqual({ aws_session_token: null });
        // A token typed in the same save wins: it replaces the stored one.
        expect(bedrockProviderConfig({ ...untouchedStored, sessionToken: 'tok' }, ALL_STORED, { removeSessionToken: true }))
            .toEqual({ aws_session_token: 'tok' });
        // Nothing stored: nothing to remove.
        expect(bedrockProviderConfig(empty, NOTHING_STORED, { removeSessionToken: true })).toBeUndefined();
    });
});

describe('Vertex AI', () => {
    const SA = '{"type":"service_account","project_id":"p","private_key":"unit-test"}';

    it('is vertex_ai, and vertex is the same provider', () => {
        expect([...VERTEX_PROVIDERS]).toEqual(['vertex_ai', 'vertex']);
        expect(isVertexProvider('vertex')).toBe(true);
        expect(isVertexProvider(' Vertex_AI ')).toBe(true);
        for (const other of ['gemini', 'bedrock', 'openai', '', null, undefined]) expect(isVertexProvider(other)).toBe(false);
    });

    const NOTHING = storedVertexCredential(null);
    const STORED = storedVertexCredential({
        gcp_project_id: 'my-project',
        gcp_location: 'europe-west4',
        provider_config_keys: { gcp_credentials_json: true },
    });
    const nothingTyped = { projectId: '', location: '', credentialsJson: '' };
    // The form of a stored model starts with its project and location, and no key.
    const untouched = { projectId: 'my-project', location: 'europe-west4', credentialsJson: '' };

    it('reads what is stored: the project, the location, and whether there is a key, never the key', () => {
        expect(STORED).toEqual({ projectId: 'my-project', location: 'europe-west4', credentialsJson: true });
        expect(NOTHING).toEqual({ projectId: '', location: '', credentialsJson: false });
        expect(storedVertexCredential({ gcp_project_id: null, gcp_location: null, provider_config_keys: { aws_access_key_id: true } })).toEqual(NOTHING);
    });

    it('needs the service account, the project and the location of a model that is enabled, and names no default', () => {
        const errors = vertexCredentialErrors(nothingTyped, { stored: NOTHING, enabled: true });
        expect(errors).toEqual({
            projectId: 'GCP project ID is required for a model that is enabled: the gateway has no default project. Enter it, or save the model disabled.',
            location:
                'GCP location is required for a model that is enabled: the gateway has no default location. Enter a region such as europe-west4, or global, us or eu; or save the model disabled.',
            credentialsJson:
                'Service account JSON key is required for a model that is enabled: the gateway uses no Google identity of its own. Enter it, or save the model disabled.',
        });
    });

    it('asks for nothing when everything is stored and nothing is changed, and nothing of a model that is switched off', () => {
        expect(vertexCredentialErrors(untouched, { stored: STORED, enabled: true })).toEqual({});
        expect(vertexCredentialErrors(nothingTyped, { stored: NOTHING, enabled: false })).toEqual({});
    });

    it('asks an enabled model for the location when a stored one is cleared', () => {
        expect(Object.keys(vertexCredentialErrors({ ...untouched, location: '' }, { stored: STORED, enabled: true }))).toEqual(['location']);
    });

    it('asks for no project or location of a model that is sent to an address of its own', () => {
        expect(vertexCredentialErrors(nothingTyped, { stored: STORED, enabled: true, addressGiven: true })).toEqual({});
        expect(Object.keys(vertexCredentialErrors(nothingTyped, { stored: NOTHING, enabled: true, addressGiven: true }))).toEqual(['credentialsJson']);
    });

    it('does not ask for a project or a location the model names in its metadata, where the gateway reads them too', () => {
        const keyOnly = storedVertexCredential({ provider_config_keys: { gcp_credentials_json: true } });
        expect(vertexCredentialErrors(nothingTyped, { stored: keyOnly, enabled: true, metadata: { project: 'my-project', region: 'europe-west4' } })).toEqual({});
        expect(Object.keys(vertexCredentialErrors(nothingTyped, { stored: keyOnly, enabled: true, metadata: { project: 'my-project' } }))).toEqual(['location']);
    });

    it('refuses a key that is not a service account JSON object, a location that is not one and a project that is not one', () => {
        const base = { projectId: 'my-project', location: 'europe-west4', credentialsJson: SA };
        const opts = { stored: NOTHING, enabled: true };
        expect(vertexCredentialErrors({ ...base, credentialsJson: 'not json' }, opts).credentialsJson)
            .toBe('Service account JSON key must be the JSON file downloaded from Google Cloud (an object with "type": "service_account").');
        expect(vertexCredentialErrors({ ...base, credentialsJson: '{"type":"authorized_user"}' }, opts).credentialsJson)
            .toMatch(/"type": "service_account"/);
        expect(vertexCredentialErrors({ ...base, location: 'x.attacker.example/' }, opts).location)
            .toBe('GCP location must be a location name: a region such as europe-west4, or global, us or eu.');
        expect(vertexCredentialErrors({ ...base, projectId: 'My Project' }, opts).projectId)
            .toBe('GCP project ID must be a project ID: lower-case letters, digits and hyphens.');
        for (const location of ['europe-west4', 'global', 'us', 'eu']) {
            expect(vertexCredentialErrors({ ...base, location }, opts)).toEqual({});
        }
    });

    it('sends the keys the gateway reads for a model with nothing stored', () => {
        expect(vertexProviderConfig({ projectId: ' my-project ', location: 'europe-west4', credentialsJson: SA }, NOTHING)).toEqual({
            gcp_project_id: 'my-project',
            gcp_location: 'europe-west4',
            gcp_credentials_json: SA,
        });
        expect(vertexProviderConfig(nothingTyped, NOTHING)).toBeUndefined();
    });

    it('sends only the keys that changed: the control plane merges them into what is stored', () => {
        expect(vertexProviderConfig(untouched, STORED)).toBeUndefined();
        expect(vertexProviderConfig({ ...untouched, location: 'global' }, STORED)).toEqual({ gcp_location: 'global' });
        expect(vertexProviderConfig({ ...untouched, credentialsJson: SA }, STORED)).toEqual({ gcp_credentials_json: SA });
    });

    it('removes a stored project or location that was cleared, with null, and never the key', () => {
        expect(vertexProviderConfig({ ...untouched, location: '' }, STORED)).toEqual({ gcp_location: null });
        expect(vertexProviderConfig(nothingTyped, STORED)).toEqual({ gcp_project_id: null, gcp_location: null });
    });
});

describe('cloudModelApiKey', () => {
    it('sends no API key for a new cloud model and for one that was a cloud model already', () => {
        expect(cloudModelApiKey(undefined, 'bedrock')).toBeUndefined();
        expect(cloudModelApiKey('vertex_ai', 'vertex_ai')).toBeUndefined();
        expect(cloudModelApiKey('bedrock', 'vertex_ai')).toBeUndefined();
    });

    it('clears the key a stored model may carry from its old provider when it moves to a cloud provider', () => {
        expect(cloudModelApiKey('openai', 'bedrock_converse')).toBeNull();
        expect(cloudModelApiKey('gemini', 'vertex_ai')).toBeNull();
    });

    it('is nothing for a model of another provider', () => {
        expect(cloudModelApiKey('openai', 'anthropic')).toBeUndefined();
        expect(cloudModelApiKey('bedrock', 'openai')).toBeUndefined();
    });
});

describe('modelSaveRefusalText', () => {
    // The control plane's sentences (`app/services/llm/destination.py`,
    // `provider_config.py`, `llm_models.py`).
    const NOT_IMPLEMENTED =
        "unsupported_provider_api: the gateway does not implement the Amazon Bedrock API for model 'ai21.j2-ultra' (provider 'bedrock'), so it would refuse every request for it. On Bedrock it serves Anthropic Claude models and the Amazon Nova, Mistral and DeepSeek chat models as chat models, and the Amazon Titan Text and Cohere Embed v3 embedding models as embedding models.";
    const WRONG_MODE =
        "unsupported_provider_api: the gateway serves model 'amazon.titan-embed-text-v2:0' (provider 'bedrock') on Amazon Bedrock as a embedding model only, so it would refuse every request for it as a chat model, with or without an api_url. Set mode to embedding.";
    const WRONG_MODE_VERTEX =
        "unsupported_provider_api: the gateway serves model 'gemini-2.5-pro' (provider 'vertex_ai') on Vertex AI as a chat model only, so it would refuse every request for it as a embedding model, with or without an api_url. Set mode to chat.";
    const NO_ENCRYPTION =
        'provider_config cannot be stored: this control plane has no encryption service (ENCRYPTION_KEY), and a cloud credential is never stored in clear. Nothing was saved.';
    const HALF = 'provider_config: aws_secret_access_key is missing. The gateway signs with the access key id and the secret access key together, and refuses a model that has one without the other (provider_credential_missing).';

    it('says a Bedrock model family is not one the gateway can call', async () => {
        expect(modelSaveRefusalText(await thrownByClient(422, { detail: NOT_IMPLEMENTED }))).toBe(
            'The gateway cannot call this model on Amazon Bedrock. There it serves Anthropic Claude, Amazon Nova, Mistral and DeepSeek chat models, and Amazon Titan Text and Cohere Embed v3 embedding models. Nothing was saved. Choose one of those models.',
        );
    });

    it('says which mode the gateway serves the model in, on Bedrock and on Vertex AI', async () => {
        expect(modelSaveRefusalText(await thrownByClient(422, { detail: WRONG_MODE }))).toBe(
            'The gateway serves this model on Amazon Bedrock as an embedding model only. Nothing was saved. Set Mode to Embedding (General tab).',
        );
        expect(modelSaveRefusalText(await thrownByClient(422, { detail: WRONG_MODE_VERTEX }))).toBe(
            'The gateway serves this model on Vertex AI as a chat model only. Nothing was saved. Set Mode to Chat (General tab).',
        );
    });

    it('says the credential cannot be stored without an encryption key', async () => {
        expect(modelSaveRefusalText(await thrownByClient(503, { detail: NO_ENCRYPTION }))).toBe(
            'The cloud credential cannot be stored: this Control Plane has no encryption key (ENCRYPTION_KEY), and a credential is never stored unencrypted. Nothing was saved. Ask whoever runs the installation to set ENCRYPTION_KEY, then save again.',
        );
    });

    it('names the credential, not the field of the API, when the control plane refuses it', async () => {
        expect(modelSaveRefusalText(await thrownByClient(422, { detail: HALF }))).toBe(
            'Cloud credential: aws_secret_access_key is missing. The gateway signs with the access key id and the secret access key together, and refuses a model that has one without the other (provider_credential_missing). Nothing was saved.',
        );
    });

    const MEMBER =
        "unsupported_provider_api: model 'anthropic.claude-sonnet-4' (provider 'bedrock') cannot be a member of a routing group: the gateway serves it on Amazon Bedrock only when it is the model a request names, never as a candidate it forwards to, so it would skip it on every request. As a member or a fallback it forwards to Amazon Nova, Mistral and DeepSeek chat models on Bedrock (chat) and to the Amazon Titan Text and Cohere Embed v3 models on Bedrock (embedding), and to no other cloud model.";
    const FALLBACK =
        "fallback_models: unsupported_provider_api: model 'gemini-2.5-pro' (provider 'vertex_ai') cannot be a fallback: the gateway serves it on Vertex AI only when it is the model a request names, never as a candidate it forwards to, so it would skip it on every request. As a member or a fallback it forwards to Amazon Nova, Mistral and DeepSeek chat models on Bedrock (chat) and to the Amazon Titan Text and Cohere Embed v3 models on Bedrock (embedding), and to no other cloud model.";

    it('says a cloud model cannot be a member of a routing group, and which can', async () => {
        expect(modelSaveRefusalText(await thrownByClient(422, { detail: MEMBER }))).toBe(
            'Model "anthropic.claude-sonnet-4" cannot be a member of a routing group. The gateway serves it on Amazon Bedrock only when a request names it, and would skip it every time as a member of a routing group. Nothing was saved. As members and fallbacks the gateway uses, of the cloud models, only Amazon Nova, Mistral and DeepSeek chat models and Amazon Titan Text and Cohere Embed v3 embedding models on Amazon Bedrock. Choose one of those, or a model of another provider.',
        );
    });

    it('says a cloud model cannot be a fallback, whichever list named it', async () => {
        const text = modelSaveRefusalText(await thrownByClient(422, { detail: FALLBACK }));
        expect(text).toMatch(/^Model "gemini-2.5-pro" cannot be a fallback\. The gateway serves it on Vertex AI only when a request names it/);
        expect(text).not.toContain('unsupported_provider_api');
    });

    it('says a cloud model has no API key, and what it has instead', async () => {
        const vertex =
            "api_key: a Vertex AI model has no API key. The gateway authenticates it with the model's own service account and reads its api_key nowhere: put the service-account key file in provider_config.gcp_credentials_json and send no api_key (null clears a stored one).";
        const bedrock =
            "api_key: an Amazon Bedrock model has no API key. The gateway signs its requests with the model's own AWS credential and reads its api_key nowhere: put the credential in provider_config (aws_access_key_id, aws_secret_access_key) and send no api_key (null clears a stored one).";
        expect(modelSaveRefusalText(await thrownByClient(422, { detail: vertex }))).toBe(
            'A Vertex AI model has no API key: the gateway calls it with the service account of the model. Nothing was saved. Enter the service account JSON key (API tab) and no API key.',
        );
        expect(modelSaveRefusalText(await thrownByClient(422, { detail: bedrock }))).toMatch(/^An Amazon Bedrock model has no API key/);
    });

    it('says an API key cannot be stored without an encryption key', async () => {
        const said =
            'api_key cannot be stored: this control plane has no encryption service (ENCRYPTION_KEY), and a credential is never stored in clear. Nothing was saved.';
        expect(modelSaveRefusalText(await thrownByClient(503, { detail: said }))).toMatch(/^The API key cannot be stored: this Control Plane has no encryption key/);
    });

    it('survives the list handing the refusal back to the form', async () => {
        const handed = saveFailure(await thrownByClient(422, { detail: WRONG_MODE }), 'Failed to update model');
        expect(modelSaveRefusalText(handed)).toMatch(/^The gateway serves this model on Amazon Bedrock as an embedding model only/);
        expect(modelSaveRefusalText(saveFailure(await thrownByClient(503, { detail: NO_ENCRYPTION }), 'Failed'))).toMatch(/ENCRYPTION_KEY/);
    });

    it('is undefined for any other refusal', async () => {
        expect(modelSaveRefusalText(await thrownByClient(422, { detail: 'name is required' }))).toBeUndefined();
        expect(modelSaveRefusalText(await thrownByClient(503, { detail: 'database down' }))).toBeUndefined();
        expect(modelSaveRefusalText(await thrownByClient(500, { detail: NOT_IMPLEMENTED }))).toBeUndefined();
        expect(modelSaveRefusalText(undefined)).toBeUndefined();
    });
});

describe('localAddressHint', () => {
    it.each(['http://localhost:11434', 'http://127.0.0.1:8000/v1', 'http://[::1]:1234', 'HTTP://LOCALHOST:11434/api/chat'])(
        'says %s is the gateway\'s own machine',
        (url) => {
            expect(localAddressHint(url)).toBe(
                'This address is the gateway\'s own machine, not yours: the gateway sends the requests, and "localhost" is where it runs (its container, when it runs in one). Use an address the gateway can reach, such as http://host.docker.internal:11434 for a model server on the same host as a gateway in Docker.',
            );
        },
    );

    it.each(['', 'http://ollama:11434', 'https://localhost.example.com/v1', 'http://host.docker.internal:11434', 'not a url'])(
        'has nothing to say of %j',
        (url) => expect(localAddressHint(url)).toBeUndefined(),
    );
});

// The control plane's refusal of a model with no destination, as it words it
// (`missing_api_url_problem` in app/services/llm/destination.py).
const NO_DESTINATION =
    "api_url is required: model 'gpt-4o' is of provider 'azure', for which the gateway knows no base URL for a chat " +
    'model, so it would refuse every request for it (provider_api_url_missing). Set api_url to the address of your own endpoint.';

describe('catalogRequiresApiUrl', () => {
    const entries = [
        { provider_key: 'azure', mode: 'chat', requires_api_url: true },
        { provider_key: 'anthropic', mode: 'chat', requires_api_url: false },
        { provider_key: 'replicate', mode: 'image_generation', requires_api_url: false },
        { provider_key: 'replicate', mode: 'chat', requires_api_url: true },
    ];

    it('is what the control plane says of a catalog entry of the same provider and mode', () => {
        expect(catalogRequiresApiUrl(entries, 'azure', 'chat')).toBe(true);
        expect(catalogRequiresApiUrl(entries, 'anthropic', 'chat')).toBe(false);
        // Per mode: the control plane's answer, with no exception kept here.
        expect(catalogRequiresApiUrl(entries, 'replicate', 'image_generation')).toBe(false);
        expect(catalogRequiresApiUrl(entries, 'replicate', 'chat')).toBe(true);
    });

    it('is undefined when the catalog has no entry of that provider and mode: the form cannot know', () => {
        expect(catalogRequiresApiUrl(entries, 'azure', 'embedding')).toBeUndefined();
        expect(catalogRequiresApiUrl(entries, 'my-own-server', 'chat')).toBeUndefined();
        expect(catalogRequiresApiUrl([], 'azure', 'chat')).toBeUndefined();
        expect(catalogRequiresApiUrl(entries, '', 'chat')).toBeUndefined();
    });

    it('does not guess from an entry that does not carry the field', () => {
        expect(catalogRequiresApiUrl([{ provider_key: 'azure', mode: 'chat' }], 'azure', 'chat')).toBeUndefined();
    });
});

describe('apiUrlRefusal', () => {
    it('is the control plane\'s sentence when it refuses a model for its missing api_url', async () => {
        expect(apiUrlRefusal(await thrownByClient(422, { detail: NO_DESTINATION }))).toBe(NO_DESTINATION);
    });

    it('survives the list handing the refusal back to the form', async () => {
        expect(apiUrlRefusal(saveFailure(await thrownByClient(422, { detail: NO_DESTINATION }), 'Failed to create model')))
            .toBe(NO_DESTINATION);
    });

    it('is a validation error of the api_url field', async () => {
        const caught = await thrownByClient(422, { detail: [{ loc: ['body', 'api_url'], msg: 'not a URL', input: 'x' }] });
        expect(apiUrlRefusal(saveFailure(caught, 'Failed'))).toBe('api_url: not a URL');
    });

    it('is undefined for any other refusal', async () => {
        expect(apiUrlRefusal(await thrownByClient(422, { detail: 'name is required' }))).toBeUndefined();
        expect(apiUrlRefusal(await thrownByClient(400, { detail: NO_DESTINATION }))).toBeUndefined();
        expect(apiUrlRefusal(await thrownByClient(422, { detail: [{ loc: ['body', 'name'], msg: 'too short' }] }))).toBeUndefined();
        expect(apiUrlRefusal(new Error('api_url is required'))).toBeUndefined();
        expect(apiUrlRefusal(undefined)).toBeUndefined();
    });
});
