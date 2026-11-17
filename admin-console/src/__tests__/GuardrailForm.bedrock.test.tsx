/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * GuardrailForm: an Amazon Bedrock guardrail entry carries its own AWS
 * credential.
 *
 * The gateway calls a Bedrock guardrail with the `region`,
 * `aws_access_key_id` and `aws_secret_access_key` (and optional
 * `aws_session_token`) of the entry's own `config`, and reads no credential
 * of its environment; the control plane's connection test does the
 * same. An entry without them cannot be
 * built, and on a fail-closed policy that blocks the policy's calls. So the
 * form requires them.
 *
 * The control plane returns an entry's `config` as it is stored, the secret
 * access key with it. While it
 * does, the form keeps what it was handed so that a save does not lose
 * it, and shows none of it: a secret field holds only what was typed in it.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

import GuardrailForm from '../components/guardrails/GuardrailForm';
import type { GuardrailProviderConfig, GuardrailProviderConfigRequest } from '../lib/guardrailTypes';

const mockCreate = vi.fn().mockResolvedValue({ config: { id: 'gr-new' } });
const mockUpdate = vi.fn().mockResolvedValue({ config: { id: 'gr-existing' } });
const mockTest = vi.fn();

vi.mock('../hooks/useGateway.ts', () => ({
    useGateway: () => ({
        createGuardrailConfig: mockCreate,
        updateGuardrailConfigById: mockUpdate,
        fetchGroupHierarchy: vi.fn().mockResolvedValue([]),
        gatewayClient: { testGuardrailProviderConnection: mockTest },
    }),
}));

vi.mock('../components/common/Toast', () => ({
    useToast: () => ({ success: vi.fn(), error: vi.fn(), loading: vi.fn(), dismiss: vi.fn() }),
}));

// Made up here; nothing real.
const KEY_ID = 'AKIAUNITTESTGUARD';
const SECRET = 'unit-test-guardrail-secret';
const TOKEN = 'unit-test-guardrail-token';

const PROVIDERS = [
    { id: 'bedrock', name: 'AWS Bedrock Guardrails', description: 'Bedrock guardrail', modes: ['both'], requires_api_key: false },
    { id: 'custom', name: 'Custom Webhook', description: 'Your own', modes: ['both'], requires_api_key: true },
];

function buildExistingConfig(overrides: Partial<Record<string, unknown>> = {}) {
    return {
        id: 'gr-existing',
        tenant_id: 't1',
        name: 'PII on customer endpoint',
        description: '',
        is_default: false,
        priority: 0,
        scope_mcp_server_id: null,
        scope_tool_name: null,
        group_ids: [],
        groups: [],
        enabled: true,
        surfaces: {
            chat_input: true, chat_output: false,
            mcp_input: true, mcp_output: false,
            skill_input: true, skill_output: true,
            a2a_inbound: false, a2a_outbound: false,
        },
        builtin: {
            pii: { enabled: true, action: 'redact', provider: 'built_in', categories: null },
            prompt_injection: { enabled: true, action: 'block', sensitivity: 0.7, provider: 'built_in' },
            jailbreak: { enabled: true, action: 'block', provider: 'built_in' },
            toxic_content: { enabled: false, action: 'warn', threshold: 0.8, provider: 'openai_moderation', category_thresholds: null },
            banned_words: [],
            banned_patterns: [],
            banned_words_match_mode: null,
        },
        providers: [],
        max_check_timeout_ms: 2000,
        fail_open: true,
        fail_open_overrides: null,
        log_all_checks: false,
        ...overrides,
    } as unknown;
}


const bedrockEntry = (config: Record<string, unknown> | null) => ({
    provider: 'bedrock',
    enabled: true,
    endpoint: '',
    mode: 'both',
    config,
    surfaces: ['llm', 'mcp', 'skill'],
});

const STORED = { guardrail_id: 'gr-abc123', region: 'eu-west-1', aws_access_key_id: KEY_ID, aws_secret_access_key: SECRET, aws_session_token: TOKEN };

function renderForm(config: unknown = null) {
    render(<GuardrailForm config={config as never} providers={PROVIDERS as never} onClose={vi.fn()} onSave={vi.fn().mockResolvedValue(undefined)} />);
}

function openProviders() {
    fireEvent.click(screen.getByText('Providers'));
}

function addBedrock() {
    openProviders();
    fireEvent.click(screen.getByText('Add AWS Bedrock Guardrails'));
    fireEvent.click(screen.getByText('Configure'));
}

function type(label: RegExp, value: string) {
    fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

function fillName() {
    fireEvent.click(screen.getByText('General'));
    fireEvent.change(screen.getByPlaceholderText(/Strict External Partners/i), { target: { value: 'Bedrock policy' } });
}

function submit() {
    fireEvent.click(screen.getByText(/Create Configuration|Save Configuration/));
}

const sentBedrock = (mock: typeof mockCreate, payloadAt: number) => {
    const payload = mock.mock.calls[0][payloadAt] as { providers: Array<{ provider: string; config: Record<string, unknown> }> };
    return payload.providers.find((p) => p.provider === 'bedrock')?.config;
};

beforeEach(() => {
    mockCreate.mockClear();
    mockUpdate.mockClear();
    mockTest.mockReset();
});

describe('GuardrailForm: an Amazon Bedrock guardrail entry', () => {
    it('a provider, as the console holds one, is what the contract names less what it never reads: no mode, no surfaces', () => {
        const held: GuardrailProviderConfig = { provider: 'bedrock', enabled: true };
        // @ts-expect-error the console reads no `mode` of a provider
        void held.mode;
        // @ts-expect-error nor `surfaces`
        void held.surfaces;
        const sent: GuardrailProviderConfigRequest = { provider: 'bedrock', enabled: true, endpoint: null, api_key: null, config: null };
        expect(Object.keys(sent).sort()).toEqual(['api_key', 'config', 'enabled', 'endpoint', 'provider']);
    });

    it('asks for the region and the AWS credential of the entry, the secrets masked', () => {
        renderForm();
        addBedrock();
        expect(screen.getByLabelText(/^AWS region \*/)).toBeInTheDocument();
        // The access key ID is no secret: the Control Plane returns it, and it is shown.
        expect(screen.getByLabelText(/^AWS access key ID \*/)).toHaveAttribute('type', 'text');
        expect(screen.getByLabelText(/^AWS secret access key \*/)).toHaveAttribute('type', 'password');
        expect(screen.getByLabelText(/^AWS session token/)).toHaveAttribute('type', 'password');
        // The form does not say that the gateway host's environment supplies them.
        expect(document.body.textContent).not.toMatch(/gateway host environment|AWS_REGION env var|configured on the gateway host/);
    });

    it('refuses to save an entry without them, says which are missing, and sends nothing', async () => {
        renderForm();
        fillName();
        addBedrock();
        type(/^Bedrock guardrail ID/, 'gr-abc123');
        submit();
        expect(
            await screen.findByText(
                'Amazon Bedrock guardrail: AWS region, AWS access key ID and AWS secret access key are required. The gateway calls a Bedrock guardrail with the credential entered here and uses no other.',
            ),
        ).toBeInTheDocument();
        expect(mockCreate).not.toHaveBeenCalled();
        // The form is on the tab where they are entered.
        expect(screen.getByLabelText(/^AWS region \*/)).toBeInTheDocument();
    });

    it('names only what is missing', async () => {
        renderForm();
        fillName();
        addBedrock();
        type(/^Bedrock guardrail ID/, 'gr-abc123');
        type(/^AWS region \*/, 'eu-west-1');
        type(/^AWS access key ID \*/, KEY_ID);
        submit();
        expect(await screen.findByText(/^Amazon Bedrock guardrail: AWS secret access key is required\./)).toBeInTheDocument();
        expect(mockCreate).not.toHaveBeenCalled();
    });

    it('refuses a region that is not a region name', async () => {
        renderForm();
        fillName();
        addBedrock();
        type(/^Bedrock guardrail ID/, 'gr-abc123');
        type(/^AWS region \*/, 'x.attacker.example/');
        type(/^AWS access key ID \*/, KEY_ID);
        type(/^AWS secret access key \*/, SECRET);
        submit();
        expect(await screen.findByText(/^Amazon Bedrock guardrail: AWS region must be a region name such as eu-west-1\./)).toBeInTheDocument();
        expect(mockCreate).not.toHaveBeenCalled();
    });

    it('sends them in the entry config, as the gateway reads them', async () => {
        renderForm();
        fillName();
        addBedrock();
        type(/^Bedrock guardrail ID/, 'gr-abc123');
        type(/^AWS region \*/, 'eu-west-1');
        type(/^AWS access key ID \*/, KEY_ID);
        type(/^AWS secret access key \*/, SECRET);
        type(/^AWS session token/, TOKEN);
        submit();
        await waitFor(() => expect(mockCreate).toHaveBeenCalledTimes(1));
        expect(sentBedrock(mockCreate, 0)).toEqual(STORED);
    });

    it('does not require them of a disabled entry, or of another provider', async () => {
        renderForm(buildExistingConfig({ providers: [{ ...bedrockEntry(null), enabled: false }, { ...bedrockEntry(null), provider: 'custom' }] }));
        submit();
        await waitFor(() => expect(mockUpdate).toHaveBeenCalledTimes(1));
    });
});

describe('GuardrailForm: a stored Bedrock entry, whose secrets the Control Plane never returns', () => {
    // What a response carries: the config without its secrets, and for each secret whether one is stored.
    const RETURNED = { guardrail_id: 'gr-abc123', region: 'eu-west-1', aws_access_key_id: KEY_ID };
    const storedEntry = (stored: Record<string, boolean> = { aws_secret_access_key: true, aws_session_token: true }) => ({
        ...bedrockEntry(RETURNED),
        config_secrets_stored: stored,
    });
    const existing = (stored?: Record<string, boolean>) => buildExistingConfig({ providers: [storedEntry(stored)] });

    function openEntry() {
        openProviders();
        fireEvent.click(screen.getByText('Configure'));
    }

    it('shows what is no secret, and says of each secret whether one is stored', () => {
        renderForm(existing({ aws_secret_access_key: true, aws_session_token: false }));
        openEntry();
        expect(screen.getByLabelText(/^AWS region/)).toHaveValue('eu-west-1');
        expect(screen.getByLabelText(/^Bedrock guardrail ID/)).toHaveValue('gr-abc123');
        expect(screen.getByLabelText(/^AWS access key ID/)).toHaveValue(KEY_ID);
        expect(screen.getByLabelText(/^AWS secret access key/)).toHaveValue('');
        expect(screen.getByLabelText(/^AWS session token/)).toHaveValue('');
        expect(screen.getByTestId('guardrail-secret-stored-aws_secret_access_key')).toHaveTextContent('A value is stored. It is never shown.');
        expect(screen.getByTestId('guardrail-secret-stored-aws_session_token')).toHaveTextContent('No value is stored.');
        expect(screen.getByLabelText(/^AWS secret access key/)).toHaveAttribute('placeholder', 'Stored. Leave empty to keep it.');
        // A stored secret is not asked for again.
        expect(screen.getByLabelText(/^AWS secret access key/).closest('div')).not.toHaveTextContent('AWS secret access key *');
    });

    it('sends no secret when the configuration is saved without retyping one: the Control Plane keeps what is stored', async () => {
        renderForm(existing());
        submit();
        await waitFor(() => expect(mockUpdate).toHaveBeenCalledTimes(1));
        expect(sentBedrock(mockUpdate, 1)).toEqual(RETURNED);
        // The marks are the Control Plane's to say, not the form's to send.
        const payload = mockUpdate.mock.calls[0][1] as { providers: Array<Record<string, unknown>> };
        expect(payload.providers[0]).not.toHaveProperty('config_secrets_stored');
    });

    it('sends only the secret that was typed, and leaves it out again when the field is emptied', async () => {
        renderForm(existing());
        openEntry();
        type(/^AWS secret access key/, 'rotated-unit-secret');
        type(/^AWS session token/, 'typed-then-removed');
        type(/^AWS session token/, '');
        submit();
        await waitFor(() => expect(mockUpdate).toHaveBeenCalledTimes(1));
        expect(sentBedrock(mockUpdate, 1)).toEqual({ ...RETURNED, aws_secret_access_key: 'rotated-unit-secret' });
    });

    it('removes the stored session token with null when asked to', async () => {
        renderForm(existing());
        openEntry();
        fireEvent.click(screen.getByRole('checkbox', { name: 'Remove the stored value when the policy is saved' }));
        submit();
        await waitFor(() => expect(mockUpdate).toHaveBeenCalledTimes(1));
        expect(sentBedrock(mockUpdate, 1)).toEqual({ ...RETURNED, aws_session_token: null });
    });

    it('asks for the secret access key of an enabled entry that has none stored', async () => {
        renderForm(existing({ aws_secret_access_key: false, aws_session_token: false }));
        submit();
        expect(await screen.findByText(/^Amazon Bedrock guardrail: AWS secret access key is required\./)).toBeInTheDocument();
        expect(mockUpdate).not.toHaveBeenCalled();
    });

    it('tests the connection with what the form holds; the Control Plane adds the stored secrets', async () => {
        mockTest.mockResolvedValue({ ok: true, status: 'healthy', detail: 'Reachable', response_time_ms: 12 });
        renderForm(existing());
        openEntry();
        fireEvent.click(screen.getByText(/Test connection/i));
        await waitFor(() => expect(mockTest).toHaveBeenCalledTimes(1));
        const sent = mockTest.mock.calls[0][0] as { config: unknown; config_id: unknown };
        expect(sent.config).toEqual(RETURNED);
        expect(sent.config_id).toBe('gr-existing');
    });

    it('says in plain words what the Control Plane found missing when it refuses the save', async () => {
        const { thrownByClient } = await import('../test/serverAnswers');
        mockUpdate.mockRejectedValueOnce(
            await thrownByClient(400, {
                detail: {
                    error: 'guardrail_provider_incomplete',
                    message: 'Selected external providers are missing configuration.',
                    issues: ['An enabled Bedrock provider requires config.region — the AWS region of the guardrail.'],
                },
            }),
        );
        renderForm(existing());
        submit();
        expect(
            await screen.findByText(/Amazon Bedrock guardrail provider: AWS region is required while the provider is enabled\. Enter it \(Providers tab\), or switch the provider off\./),
        ).toBeInTheDocument();
    });

    it('says in plain words that a banned pattern uses what the gateway\'s patterns do not have', async () => {
        const { thrownByClient } = await import('../test/serverAnswers');
        mockUpdate.mockRejectedValueOnce(
            await thrownByClient(422, {
                detail: [
                    {
                        type: 'value_error',
                        loc: ['body', 'builtin', 'banned_patterns'],
                        msg: "Value error, Invalid regex pattern at index 0: '(?<=id )\\\\d+' — the gateway cannot compile it: look-around ((?=..), (?!..), (?<=..), (?<!..)) is not supported. Banned patterns are compiled with the Rust regex-lite syntax, which has no look-around, backreferences or Unicode classes",
                    },
                ],
            }),
        );
        renderForm(existing());
        submit();
        expect(await screen.findByText(/Banned pattern 1, \(\?<=id \)\\d\+, cannot be used: it has look-around/)).toBeInTheDocument();
    });
});
