/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * LLModelForm: what the gateway needs of a model before it can send for it.
 *
 * Amazon Bedrock (`bedrock`, `bedrock_converse`): the gateway signs with the
 * model's own AWS credential, read from `provider_config` as
 * `aws_access_key_id`, `aws_secret_access_key`, `aws_session_token`
 * (optional) and `aws_region`; a row without them is refused with 501
 * `provider_credential_missing`. The control plane stores the object
 * encrypted and never returns it: a model says `has_provider_config`, its
 * `aws_region` and, per secret, whether one is stored
 * (`provider_config_keys`). An update is merged into what is stored: a key
 * present replaces, `null` removes, a key absent is kept. So the form sends
 * only what changed.
 *
 * Vertex AI (`vertex_ai`; `vertex` is the same provider): the model's own
 * service account, `gcp_project_id` (required) and `gcp_location` in
 * `provider_config`, never `api_key`.
 *
 * A model the gateway has no destination for needs `api_url`, or the gateway
 * answers 501 `provider_api_url_missing`. Which models those are is the
 * control plane's to say: `requires_api_url` on a catalog entry of the
 * model's provider and mode, and a 422 naming `api_url` when a model without
 * one is saved.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import toast from 'react-hot-toast';

import LLModelForm from '../components/llm/config/LLModelForm';
import { saveFailure } from '../lib/validationErrors';
import { thrownByClient } from '../test/serverAnswers';

vi.mock('react-hot-toast', () => ({
    default: { success: vi.fn(), error: vi.fn() },
}));

vi.mock('../hooks/useLlmProviders', () => ({
    useLlmProviders: () => ({ providers: [], loading: false }),
}));

const CATALOG = [
    { id: 'pc-openai', key: 'openai', display_name: 'OpenAI', category: 'chat', icon_url: null },
    { id: 'pc-anthropic', key: 'anthropic', display_name: 'Anthropic', category: 'chat', icon_url: null },
    { id: 'pc-bedrock', key: 'bedrock', display_name: 'AWS Bedrock', category: 'chat', icon_url: null },
    { id: 'pc-converse', key: 'bedrock_converse', display_name: 'AWS Bedrock (Converse API)', category: 'chat', icon_url: null },
    { id: 'pc-sagemaker', key: 'sagemaker', display_name: 'AWS SageMaker', category: 'chat', icon_url: null },
    { id: 'pc-vertex', key: 'vertex_ai', display_name: 'Vertex AI', category: 'chat', icon_url: null },
    { id: 'pc-azure', key: 'azure', display_name: 'Azure OpenAI', category: 'chat', icon_url: null },
    { id: 'pc-ollama', key: 'ollama', display_name: 'Ollama', category: 'self_hosted', icon_url: null },
];

// What the control plane says of a catalog entry: `requires_api_url`, per
// provider and mode. No entry of SageMaker here.
const REQUIRES_API_URL: Record<string, boolean> = { azure: true, ollama: true, anthropic: false, openai: false };

const client = {
    listProviderCatalog: vi.fn(),
    listLLMCatalog: vi.fn(),
    getProviderIconUrl: () => 'https://x/icon',
    uploadProviderIcon: vi.fn(),
};

vi.mock('../hooks/useGateway', () => ({
    useGateway: () => ({ gatewayClient: client, groupTree: [] }),
}));

// The values a test types. Made up here; nothing real.
const KEY_ID = 'AKIAUNITTESTKEYID';
const SECRET = 'unit-test-secret-access-key';
const TOKEN = 'unit-test-session-token';

const storedModel = {
    id: 'llm-nova',
    tenant_id: 't1',
    name: 'Nova Pro',
    description: '',
    provider: 'bedrock_converse',
    model_name: 'amazon.nova-pro-v1:0',
    mode: 'chat',
    api_url_auto_discovered: true,
    api_url: '',
    auth_type: 'aws_iam',
    has_provider_config: true,
    aws_region: 'eu-west-1',
    provider_config_keys: { aws_access_key_id: true, aws_secret_access_key: true, aws_session_token: false },
    default_temperature: 0.7,
    default_max_tokens: 4096,
    enabled: true,
    health_status: { status: 'unknown' },
    performance_metrics: {},
};

type FormModel = Parameters<typeof LLModelForm>[0]['model'];

function renderForm(model: Record<string, unknown> | null = null) {
    const onSave = vi.fn((_data: Record<string, unknown>) => Promise.resolve());
    render(<LLModelForm model={model as FormModel} onSave={onSave} onClose={vi.fn()} />);
    return onSave;
}

async function chooseProvider(key: string) {
    const option = await screen.findByRole('option', { name: CATALOG.find((p) => p.key === key)?.display_name });
    const select = option.closest('select');
    if (!select) throw new Error('no provider select');
    fireEvent.change(select, { target: { value: key } });
}

function openApiTab() {
    fireEvent.click(screen.getByRole('button', { name: 'API' }));
}

function type(label: string | RegExp, value: string) {
    fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

function nameTheModel() {
    fireEvent.change(document.getElementById('name') as HTMLInputElement, { target: { value: 'A model' } });
    fireEvent.change(document.getElementById('model_name') as HTMLInputElement, { target: { value: 'some-model' } });
}

async function save(onSave: ReturnType<typeof renderForm>): Promise<Record<string, unknown>> {
    fireEvent.click(screen.getByRole('button', { name: /save model/i }));
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    return onSave.mock.calls[0][0];
}

const AWS_LABELS = ['AWS access key ID', 'AWS secret access key', 'AWS session token', 'AWS region'];

beforeEach(() => {
    vi.mocked(toast.error).mockClear();
    client.listProviderCatalog.mockReset();
    client.listProviderCatalog.mockResolvedValue({ providers: CATALOG, total: CATALOG.length });
    client.listLLMCatalog.mockReset();
    client.listLLMCatalog.mockImplementation(({ provider_key, mode }: { provider_key: string; mode: string }) => {
        const known = provider_key in REQUIRES_API_URL;
        return Promise.resolve({
            catalog: known ? [{ id: `cat-${provider_key}`, provider_key, mode, requires_api_url: REQUIRES_API_URL[provider_key] }] : [],
            total: known ? 1 : 0,
        });
    });
});

describe('LLModelForm: Amazon Bedrock credential', () => {
    it.each(['bedrock', 'bedrock_converse'])('asks for the four AWS fields for provider %s', async (provider) => {
        renderForm();
        await chooseProvider(provider);
        openApiTab();
        for (const label of AWS_LABELS) {
            expect(screen.getByLabelText(new RegExp(`^${label}`, 'i'))).toBeInTheDocument();
        }
        // The credential is the AWS one: no API key is asked for, and the
        // authentication type is not a choice.
        expect(screen.queryByText('API Key')).toBeNull();
        expect(screen.queryByRole('option', { name: 'OAuth' })).toBeNull();
    });

    // Vertex AI has one way to authenticate too (below), so no choice of type.
    it.each(CATALOG.map((p) => p.key).filter((key) => !key.startsWith('bedrock') && key !== 'vertex_ai'))(
        'does not ask for them for provider %s, whatever the authentication type',
        async (provider) => {
            renderForm();
            await chooseProvider(provider);
            openApiTab();
            const authType = screen.getByLabelText('Authentication Type');
            const offered = Array.from(authType.querySelectorAll('option')).map((o) => o.value);
            expect(offered).not.toContain('aws_iam');
            for (const value of offered) {
                fireEvent.change(authType, { target: { value } });
                for (const label of AWS_LABELS) {
                    expect(screen.queryByLabelText(new RegExp(`^${label}`, 'i'))).toBeNull();
                }
            }
        },
    );

    it('does not ask for them for a model of another provider stored with aws_iam', () => {
        renderForm({ ...storedModel, provider: 'sagemaker', has_provider_config: false });
        openApiTab();
        for (const label of AWS_LABELS) {
            expect(screen.queryByLabelText(new RegExp(`^${label}`, 'i'))).toBeNull();
        }
        expect(screen.getByText(/signs requests with an AWS credential for Amazon Bedrock only/i)).toBeInTheDocument();
    });

    it('sends the credential as the provider_config the gateway reads, with auth type aws_iam', async () => {
        const onSave = renderForm();
        nameTheModel();
        await chooseProvider('bedrock_converse');
        openApiTab();
        type(/^AWS access key ID/i, ` ${KEY_ID} `);
        type(/^AWS secret access key/i, SECRET);
        type(/^AWS region/i, 'eu-west-1');
        const sent = await save(onSave);
        expect(sent.auth_type).toBe('aws_iam');
        expect(sent.provider_config).toEqual({
            aws_access_key_id: KEY_ID,
            aws_secret_access_key: SECRET,
            aws_region: 'eu-west-1',
        });
        expect(sent.api_key).toBeUndefined();
    });

    it('sends the session token when one is typed', async () => {
        const onSave = renderForm();
        nameTheModel();
        await chooseProvider('bedrock');
        openApiTab();
        type(/^AWS access key ID/i, KEY_ID);
        type(/^AWS secret access key/i, SECRET);
        type(/^AWS session token/i, TOKEN);
        type(/^AWS region/i, 'us-gov-west-1');
        const sent = await save(onSave);
        expect(sent.provider_config).toEqual({
            aws_access_key_id: KEY_ID,
            aws_secret_access_key: SECRET,
            aws_session_token: TOKEN,
            aws_region: 'us-gov-west-1',
        });
    });

    it('masks the secret access key and the session token', async () => {
        renderForm();
        await chooseProvider('bedrock');
        openApiTab();
        expect(screen.getByLabelText(/^AWS secret access key/i)).toHaveAttribute('type', 'password');
        expect(screen.getByLabelText(/^AWS session token/i)).toHaveAttribute('type', 'password');
    });

    it('never fills a secret from the model, even if the server were to send the credential', () => {
        // The control plane does not return `provider_config`. A form that
        // read it would show a secret the day a response carried one.
        renderForm({
            ...storedModel,
            provider_config: {
                aws_access_key_id: KEY_ID,
                aws_secret_access_key: SECRET,
                aws_session_token: TOKEN,
                aws_region: 'ap-south-1',
            },
        });
        openApiTab();
        for (const label of AWS_LABELS.filter((l) => l !== 'AWS region')) {
            expect(screen.getByLabelText(new RegExp(`^${label}`, 'i'))).toHaveValue('');
        }
        expect(document.body.innerHTML).not.toContain(SECRET);
        expect(document.body.innerHTML).not.toContain(TOKEN);
        expect(document.body.innerHTML).not.toContain(KEY_ID);
        // The region shown is the one the control plane states, not one read out of a credential.
        expect(screen.getByLabelText(/^AWS region/i)).toHaveValue('eu-west-1');
    });

    it('shows the stored region, and which secrets are set without showing them', () => {
        renderForm(storedModel);
        openApiTab();
        expect(screen.getByLabelText(/^AWS region/i)).toHaveValue('eu-west-1');
        expect(screen.getByTestId('aws-stored-accessKeyId')).toHaveTextContent(/^Stored$/);
        expect(screen.getByTestId('aws-stored-secretAccessKey')).toHaveTextContent(/^Stored$/);
        expect(screen.getByTestId('aws-stored-sessionToken')).toHaveTextContent(/^Not stored$/);
        expect(screen.getByLabelText(/^AWS secret access key/i)).toHaveAttribute('placeholder', 'Stored. Leave empty to keep it.');
        expect(screen.getByLabelText(/^AWS session token/i)).toHaveAttribute('placeholder', '');
        expect(screen.getByText(/Leave a field empty to keep what is stored/i)).toBeInTheDocument();
        // Nothing to remove: no session token is stored.
        expect(screen.queryByLabelText(/Remove the stored session token/i)).toBeNull();
    });

    it('keeps the stored credential when the model is saved without changing it', async () => {
        const onSave = renderForm(storedModel);
        const sent = await save(onSave);
        expect('provider_config' in sent ? sent.provider_config : undefined).toBeUndefined();
        expect(sent.auth_type).toBe('aws_iam');
    });

    it('sends only the region when only the region is changed: the control plane keeps the secrets', async () => {
        const onSave = renderForm(storedModel);
        openApiTab();
        type(/^AWS region/i, 'eu-central-1');
        const sent = await save(onSave);
        expect(sent.provider_config).toEqual({ aws_region: 'eu-central-1' });
    });

    it('sends only the secret access key when it is rotated', async () => {
        const onSave = renderForm(storedModel);
        openApiTab();
        type(/^AWS secret access key/i, SECRET);
        const sent = await save(onSave);
        expect(sent.provider_config).toEqual({ aws_secret_access_key: SECRET });
    });

    it('removes a stored session token with null when asked to', async () => {
        const onSave = renderForm({
            ...storedModel,
            provider_config_keys: { aws_access_key_id: true, aws_secret_access_key: true, aws_session_token: true },
        });
        openApiTab();
        expect(screen.getByTestId('aws-stored-sessionToken')).toHaveTextContent(/^Stored$/);
        fireEvent.click(screen.getByLabelText(/Remove the stored session token/i));
        const sent = await save(onSave);
        expect(sent.provider_config).toEqual({ aws_session_token: null });
    });

    it('refuses a stored model whose region is cleared', async () => {
        const onSave = renderForm(storedModel);
        openApiTab();
        type(/^AWS region/i, '');
        fireEvent.click(screen.getByRole('button', { name: /save model/i }));
        await waitFor(() => expect(toast.error).toHaveBeenCalled());
        expect(onSave).not.toHaveBeenCalled();
        expect(screen.getAllByText(/AWS region is required/i).length).toBeGreaterThan(0);
    });

    it('refuses a new Bedrock model with no credential', async () => {
        const onSave = renderForm();
        nameTheModel();
        await chooseProvider('bedrock');
        fireEvent.click(screen.getByRole('button', { name: /save model/i }));
        await waitFor(() => expect(toast.error).toHaveBeenCalled());
        expect(onSave).not.toHaveBeenCalled();
    });

    it('refuses a region that is not a region name', async () => {
        const onSave = renderForm();
        nameTheModel();
        await chooseProvider('bedrock');
        openApiTab();
        type(/^AWS access key ID/i, KEY_ID);
        type(/^AWS secret access key/i, SECRET);
        type(/^AWS region/i, 'x.attacker.example/');
        fireEvent.click(screen.getByRole('button', { name: /save model/i }));
        await waitFor(() => expect(toast.error).toHaveBeenCalled());
        expect(onSave).not.toHaveBeenCalled();
        expect(screen.getAllByText(/must be a region name/i).length).toBeGreaterThan(0);
    });

    // A catalog entry on Amazon Bedrock arrives so: switched off, with no credential.
    const withoutCredential = { ...storedModel, has_provider_config: false, aws_region: null, provider_config_keys: {} };

    it('lets a Bedrock model that is switched off be saved without a credential, and says what enabling it needs', async () => {
        const onSave = renderForm({ ...withoutCredential, enabled: false });
        openApiTab();
        expect(
            screen.getByText('No AWS credential is stored for this model. It can be saved disabled without one; to enable it, enter the credential and the region.'),
        ).toBeInTheDocument();
        const sent = await save(onSave);
        expect(sent.provider_config).toBeUndefined();
        expect(sent.enabled).toBe(false);
    });

    it('refuses to save an enabled Bedrock model without its credential and region, and names no default region', async () => {
        const onSave = renderForm(withoutCredential);
        openApiTab();
        fireEvent.click(screen.getByRole('button', { name: /save model/i }));
        await waitFor(() => expect(toast.error).toHaveBeenCalled());
        expect(onSave).not.toHaveBeenCalled();
        expect(
            screen.getAllByText('AWS region is required for a model that is enabled: the gateway has no default region. Enter it, or save the model disabled.').length,
        ).toBeGreaterThan(0);
        // No field suggests a region to fall back to.
        expect(screen.getByLabelText(/^AWS region/i)).not.toHaveAttribute('placeholder');
    });

    it('sends no API key for a cloud model, and clears the one a model carries from another provider', async () => {
        const onSave = renderForm({ ...withoutCredential, provider: 'openai', auth_type: 'api_key', enabled: false });
        await chooseProvider('bedrock_converse');
        const sent = await save(onSave);
        expect(sent.auth_type).toBe('aws_iam');
        expect(sent.api_key).toBeNull();
    });
});

describe('LLModelForm: what the control plane refuses at save, in plain words', () => {
    async function refusedWith(status: number, detail: string, provider = 'bedrock') {
        const refused = saveFailure(await thrownByClient(status, { detail }), 'Failed to create model');
        const onSave = vi.fn<(data: Record<string, unknown>) => Promise<void>>(() => Promise.reject(refused));
        render(<LLModelForm model={null} onSave={onSave} onClose={vi.fn()} />);
        nameTheModel();
        await chooseProvider(provider);
        openApiTab();
        type(/^AWS access key ID/i, KEY_ID);
        type(/^AWS secret access key/i, SECRET);
        type(/^AWS region/i, 'eu-west-1');
        fireEvent.click(screen.getByRole('button', { name: /save model/i }));
        await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    }

    it('422 unsupported_provider_api: says the gateway cannot call the model, without the code', async () => {
        await refusedWith(
            422,
            "unsupported_provider_api: the gateway does not implement the Amazon Bedrock API for model 'some-model' (provider 'bedrock'), so it would refuse every request for it. On Bedrock it serves Anthropic Claude models and the Amazon Nova, Mistral and DeepSeek chat models as chat models, and the Amazon Titan Text and Cohere Embed v3 embedding models as embedding models.",
        );
        expect(await screen.findByText(/^The gateway cannot call this model on Amazon Bedrock\./)).toBeInTheDocument();
        expect(document.body.textContent).not.toContain('unsupported_provider_api');
    });

    it('422 unsupported_provider_api for the mode: says which Mode to set', async () => {
        await refusedWith(
            422,
            "unsupported_provider_api: the gateway serves model 'some-model' (provider 'bedrock') on Amazon Bedrock as a embedding model only, so it would refuse every request for it as a chat model, with or without an api_url. Set mode to embedding.",
        );
        expect(
            await screen.findByText('The gateway serves this model on Amazon Bedrock as an embedding model only. Nothing was saved. Set Mode to Embedding (General tab).'),
        ).toBeInTheDocument();
    });

    it('503 for provider_config: says the Control Plane has no encryption key', async () => {
        await refusedWith(
            503,
            'provider_config cannot be stored: this control plane has no encryption service (ENCRYPTION_KEY), and a cloud credential is never stored in clear. Nothing was saved.',
        );
        expect(await screen.findByText(/^The cloud credential cannot be stored: this Control Plane has no encryption key \(ENCRYPTION_KEY\)/)).toBeInTheDocument();
        expect(document.body.textContent).not.toContain('provider_config');
        // What was typed is still in its masked field, to save again.
        const secret = screen.getByLabelText(/^AWS secret access key/i);
        expect(secret).toHaveValue(SECRET);
        expect(secret).toHaveAttribute('type', 'password');
    });
});

describe('LLModelForm: Azure', () => {
    it('does not offer Azure AD: the gateway refuses client credentials', async () => {
        renderForm();
        await chooseProvider('azure');
        openApiTab();
        const offered = Array.from(screen.getByLabelText('Authentication Type').querySelectorAll('option')).map((o) => o.value);
        expect(offered).toEqual(['api_key', 'oauth', 'none']);
        expect(screen.queryByText(/Azure AD Client/i)).toBeNull();
    });

    it('a model stored with Azure AD is shown as it is, says what to do, and asks for no client credentials', async () => {
        const onSave = renderForm({ ...storedModel, provider: 'azure', auth_type: 'azure_ad', has_provider_config: false, aws_region: null, provider_config_keys: {}, api_url: 'https://unit.openai.azure.com', api_url_auto_discovered: false });
        openApiTab();
        expect(screen.getByLabelText('Authentication Type')).toHaveValue('azure_ad');
        expect(
            screen.getByText(/The gateway does not sign in to Azure with client credentials \(Azure AD\)\. Choose API Key and enter the API key of your Azure OpenAI resource\./),
        ).toBeInTheDocument();
        expect(screen.queryByText(/Azure AD Client/i)).toBeNull();
        const sent = await save(onSave);
        expect(sent.provider_config).toBeUndefined();
    });
});

describe('LLModelForm: a model server on localhost', () => {
    it('does not fill in a localhost address for Ollama', async () => {
        renderForm();
        nameTheModel();
        await chooseProvider('ollama');
        openApiTab();
        const url = await screen.findByLabelText(/^API URL/);
        expect(url).toHaveValue('');
        expect(url).toHaveAttribute('placeholder', 'http://host.docker.internal:11434');
    });

    it('says that localhost is the gateway\'s own machine when one is typed', async () => {
        renderForm();
        nameTheModel();
        await chooseProvider('ollama');
        openApiTab();
        const url = await screen.findByLabelText(/^API URL/);
        expect(screen.queryByTestId('local-address-hint')).toBeNull();
        fireEvent.change(url, { target: { value: 'http://localhost:11434' } });
        expect(screen.getByTestId('local-address-hint')).toHaveTextContent(/^This address is the gateway's own machine, not yours/);
        fireEvent.change(url, { target: { value: 'http://ollama.internal:11434' } });
        expect(screen.queryByTestId('local-address-hint')).toBeNull();
    });

    it('says no authentication is for a model server the gateway can reach, not for "local" ones', async () => {
        renderForm();
        await chooseProvider('ollama');
        openApiTab();
        fireEvent.change(screen.getByLabelText('Authentication Type'), { target: { value: 'none' } });
        expect(screen.getByText(/The address must be one the gateway can reach/)).toBeInTheDocument();
    });
});

describe('LLModelForm: Vertex AI', () => {
    const SA = '{"type":"service_account","project_id":"unit","private_key":"unit-test-private-key"}';
    const GCP_LABELS = [/^GCP project ID/i, /^GCP location/i, /^Service account JSON key/i];

    it('asks for the project, the location and the service account, and for no API key or authentication type', async () => {
        renderForm();
        await chooseProvider('vertex_ai');
        openApiTab();
        for (const label of GCP_LABELS) expect(screen.getByLabelText(label)).toBeInTheDocument();
        expect(screen.queryByLabelText('Authentication Type')).toBeNull();
        expect(screen.queryByText('API Key')).toBeNull();
    });

    it('sends them as the provider_config the gateway reads, with no api_key', async () => {
        const onSave = renderForm();
        nameTheModel();
        await chooseProvider('vertex_ai');
        openApiTab();
        type(GCP_LABELS[0], ' unit-project ');
        type(GCP_LABELS[1], 'europe-west4');
        type(GCP_LABELS[2], SA);
        const sent = await save(onSave);
        expect(sent.auth_type).toBe('gcp_service_account');
        expect(sent.provider_config).toEqual({ gcp_project_id: 'unit-project', gcp_location: 'europe-west4', gcp_credentials_json: SA });
        expect(sent.api_key).toBeUndefined();
    });

    it('refuses a new Vertex AI model without a project: the gateway has no default one', async () => {
        const onSave = renderForm();
        nameTheModel();
        await chooseProvider('vertex_ai');
        openApiTab();
        type(GCP_LABELS[2], SA);
        fireEvent.click(screen.getByRole('button', { name: /save model/i }));
        await waitFor(() => expect(toast.error).toHaveBeenCalled());
        expect(onSave).not.toHaveBeenCalled();
        expect(
            screen.getAllByText('GCP project ID is required for a model that is enabled: the gateway has no default project. Enter it, or save the model disabled.').length,
        ).toBeGreaterThan(0);
    });

    it('refuses an enabled Vertex AI model without a location: the gateway has no default one, and the form suggests none', async () => {
        const onSave = renderForm();
        nameTheModel();
        await chooseProvider('vertex_ai');
        openApiTab();
        type(GCP_LABELS[0], 'unit-project');
        type(GCP_LABELS[2], SA);
        fireEvent.click(screen.getByRole('button', { name: /save model/i }));
        await waitFor(() => expect(toast.error).toHaveBeenCalled());
        expect(onSave).not.toHaveBeenCalled();
        expect(screen.getAllByText(/^GCP location is required for a model that is enabled: the gateway has no default location\./).length).toBeGreaterThan(0);
        expect(screen.getByLabelText(GCP_LABELS[1])).not.toHaveAttribute('placeholder');
        expect(screen.getByLabelText(GCP_LABELS[0])).not.toHaveAttribute('placeholder');
    });

    const storedVertex = {
        ...storedModel,
        provider: 'vertex',
        model_name: 'gemini-2.5-pro',
        auth_type: 'api_key',
        aws_region: null,
        // What the Control Plane says of the stored credential: the project
        // and the location, and that a key is stored. Never the key.
        gcp_project_id: 'unit-project',
        gcp_location: 'europe-west4',
        provider_config_keys: { gcp_credentials_json: true },
    };

    it('shows the stored project and location, and that a key is stored without showing it', () => {
        renderForm(storedVertex);
        openApiTab();
        expect(screen.getByLabelText(GCP_LABELS[0])).toHaveValue('unit-project');
        expect(screen.getByLabelText(GCP_LABELS[1])).toHaveValue('europe-west4');
        expect(screen.getByLabelText(GCP_LABELS[2])).toHaveValue('');
        expect(screen.getByTestId('gcp-stored-credentialsJson')).toHaveTextContent(/^Stored$/);
    });

    it('says no key is stored for a Vertex AI model that has none', () => {
        renderForm({ ...storedVertex, enabled: false, gcp_project_id: null, gcp_location: null, provider_config_keys: {} });
        openApiTab();
        expect(screen.getByTestId('gcp-stored-credentialsJson')).not.toHaveTextContent(/^Stored$/);
        expect(
            screen.getByText('No service account key is stored for this model. It can be saved disabled without one; to enable it, enter the key, the project and the location.'),
        ).toBeInTheDocument();
    });

    it('provider vertex is the same: a stored model keeps its credential when nothing is changed, and stops being an API-key model', async () => {
        const onSave = renderForm(storedVertex);
        openApiTab();
        expect(screen.getByText('A service account key is stored for this model. Leave the key field empty to keep it: only what you change is saved.')).toBeInTheDocument();
        const sent = await save(onSave);
        expect(sent.provider_config).toBeUndefined();
        expect(sent.auth_type).toBe('gcp_service_account');
        expect(sent.api_key).toBeUndefined();
    });

    it('sends only the location when only the location is changed: the control plane keeps the rest', async () => {
        const onSave = renderForm(storedVertex);
        openApiTab();
        type(GCP_LABELS[1], 'global');
        const sent = await save(onSave);
        expect(sent.provider_config).toEqual({ gcp_location: 'global' });
    });

    it('sends only the key when it is replaced', async () => {
        const onSave = renderForm(storedVertex);
        openApiTab();
        type(GCP_LABELS[2], SA);
        const sent = await save(onSave);
        expect(sent.provider_config).toEqual({ gcp_credentials_json: SA });
    });

    it('never shows a service account the server were to send', () => {
        renderForm({ ...storedVertex, provider_config: { gcp_credentials_json: SA, gcp_project_id: 'leaked-project' } });
        openApiTab();
        expect(document.body.innerHTML).not.toContain('unit-test-private-key');
        expect(document.body.innerHTML).not.toContain('leaked-project');
    });
});

describe('LLModelForm: a model with no destination needs an API URL', () => {
    it.each(['azure', 'ollama'])('marks the API URL required for provider %s, as the catalog says, and refuses to save without it', async (provider) => {
        const onSave = renderForm();
        nameTheModel();
        await chooseProvider(provider);
        openApiTab();
        const url = await screen.findByLabelText(/^API URL/);
        expect(url).toBeRequired();
        expect(screen.getByLabelText(/Auto-discover API URL/i)).toBeDisabled();
        expect(client.listLLMCatalog).toHaveBeenCalledWith({ provider_key: provider, mode: 'chat', limit: 1 });
        fireEvent.change(url, { target: { value: '' } });
        fireEvent.click(screen.getByRole('button', { name: /save model/i }));
        await waitFor(() => expect(toast.error).toHaveBeenCalled());
        expect(onSave).not.toHaveBeenCalled();
        expect(screen.getAllByText(/API URL is required: the gateway knows no address for provider/i).length).toBeGreaterThan(0);
    });

    it('sends the API URL typed for such a provider', async () => {
        const onSave = renderForm();
        nameTheModel();
        await chooseProvider('azure');
        openApiTab();
        fireEvent.change(await screen.findByLabelText(/^API URL/), { target: { value: 'https://unit.openai.azure.com' } });
        const sent = await save(onSave);
        expect(sent.api_url).toBe('https://unit.openai.azure.com');
        expect(sent.api_url_auto_discovered).toBe(false);
    });

    it('does not require one for a provider the catalog says the gateway has an address for', async () => {
        const onSave = renderForm();
        nameTheModel();
        await chooseProvider('anthropic');
        openApiTab();
        await waitFor(() => expect(client.listLLMCatalog).toHaveBeenCalledWith({ provider_key: 'anthropic', mode: 'chat', limit: 1 }));
        expect(screen.getByLabelText(/Auto-discover API URL/i)).toBeEnabled();
        expect(screen.queryByLabelText(/^API URL/)).toBeNull();
        expect(screen.queryByText(/cannot tell/i)).toBeNull();
        const sent = await save(onSave);
        expect(sent.api_url_auto_discovered).toBe(true);
    });

    it('asks for nothing up front when the catalog has no entry of the provider, and says the control plane decides', async () => {
        const onSave = renderForm();
        nameTheModel();
        await chooseProvider('sagemaker');
        openApiTab();
        expect(await screen.findByText(/cannot tell whether the gateway has an address/i)).toBeInTheDocument();
        expect(screen.getByLabelText(/Auto-discover API URL/i)).toBeEnabled();
        expect(screen.queryByLabelText(/^API URL/)).toBeNull();
        const sent = await save(onSave);
        expect(sent.api_url_auto_discovered).toBe(true);
    });

    it('asks for nothing up front when the catalog cannot be read', async () => {
        client.listLLMCatalog.mockRejectedValue(new Error('catalog 500'));
        renderForm();
        nameTheModel();
        await chooseProvider('azure');
        openApiTab();
        expect(await screen.findByText(/cannot tell whether the gateway has an address/i)).toBeInTheDocument();
        expect(screen.getByLabelText(/Auto-discover API URL/i)).toBeEnabled();
    });

    it('shows the control plane\'s refusal of a model with no destination next to the API URL field', async () => {
        const refusal =
            "api_url is required: model 'some-model' is of provider 'sagemaker', for which the gateway knows no base URL for a chat " +
            'model, so it would refuse every request for it (provider_api_url_missing). Set api_url to the address of your own endpoint.';
        const refused = saveFailure(await thrownByClient(422, { detail: refusal }), 'Failed to create model');
        const onSave = vi.fn<(data: Record<string, unknown>) => Promise<void>>(() => Promise.reject(refused));
        render(<LLModelForm model={null} onSave={onSave} onClose={vi.fn()} />);
        nameTheModel();
        await chooseProvider('sagemaker');
        fireEvent.click(screen.getByRole('button', { name: /save model/i }));
        await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));

        // The form opens the API tab, shows the field and puts the sentence by it.
        const url = await screen.findByLabelText(/^API URL/);
        expect(url.parentElement).toHaveTextContent(refusal);
        expect(screen.getByLabelText(/Auto-discover API URL/i)).not.toBeChecked();

        // Typed, it is sent.
        onSave.mockImplementation(() => Promise.resolve());
        fireEvent.change(url, { target: { value: 'https://runtime.unit.example/endpoint' } });
        fireEvent.click(screen.getByRole('button', { name: /save model/i }));
        await waitFor(() => expect(onSave).toHaveBeenCalledTimes(2));
        expect(onSave.mock.calls[1][0].api_url).toBe('https://runtime.unit.example/endpoint');
        expect(onSave.mock.calls[1][0].api_url_auto_discovered).toBe(false);
    });

    it('leaves any other refusal where it was: above the form, not by the API URL field', async () => {
        const onSave = vi.fn((_data: Record<string, unknown>) =>
            Promise.reject(saveFailure(new Error('Name already taken'), 'Failed to create model')),
        );
        render(<LLModelForm model={null} onSave={onSave} onClose={vi.fn()} />);
        nameTheModel();
        await chooseProvider('anthropic');
        fireEvent.click(screen.getByRole('button', { name: /save model/i }));
        expect(await screen.findByText('Name already taken')).toBeInTheDocument();
        expect(screen.queryByLabelText(/^API URL/)).toBeNull();
    });
});
