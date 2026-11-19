/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Governance → Identity: what credential a caller of this group must
 * present. The tab shows the effective policy with where each field comes
 * from and the floor the group inherits, explains the lifecycle default and
 * audit against enforce, edits the group's own policy (only for an
 * administrator who may), shows the Control Plane's refusal at the field it
 * names, and lists the credentials an AI System has seen.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';

import { GatewayAPIError } from '../../../../lib/gatewayApi/base';
import { POLICY_FIELDS, type IdentityPolicy, type IdentityPolicyResponse, type PolicySource } from '../../../../lib/identityPolicy';

const mockFetchPolicy = vi.fn();
const mockPutPolicy = vi.fn();
const mockFetchCredentials = vi.fn();
const mockFetchIdps = vi.fn();
const granted = new Set<string>();

vi.mock('../../../../hooks/useAuth', () => {
    const gatewayClient = {
        fetchIdentityPolicy: (...a: unknown[]): unknown => mockFetchPolicy(...a),
        putIdentityPolicy: (...a: unknown[]): unknown => mockPutPolicy(...a),
        fetchIdentityCredentials: (...a: unknown[]): unknown => mockFetchCredentials(...a),
        fetchIdPConfigs: (...a: unknown[]): unknown => mockFetchIdps(...a),
    };
    return { useAuth: () => ({ gatewayClient, hasPermission: (permission: string) => granted.has(permission) }) };
});

import GroupIdentityTab from '../GroupIdentityTab';
import type { ResourceGroup } from '../../../../lib/groupTypes';

const GROUP = { id: 'rg-1', name: 'Loan Pre-screening', group_type: 'ai_system' } as unknown as ResourceGroup;

const DEVELOPMENT: IdentityPolicy = {
    mode: 'audit',
    min_assurance: 'key',
    allow_user_keys: true,
    allowed_issuers: null,
    max_token_lifetime_s: 3600,
    max_key_ttl_days: null,
};
const PRODUCTION: IdentityPolicy = { ...DEVELOPMENT, mode: 'enforce', min_assurance: 'workload', allow_user_keys: false };
const LIFECYCLE: PolicySource = { kind: 'lifecycle', group_id: 'rg-0', group_name: 'Lending', stage: 'active' };

function answer(overrides: Partial<IdentityPolicyResponse> = {}): IdentityPolicyResponse {
    return {
        group_id: 'rg-1',
        group_name: 'Loan Pre-screening',
        is_ai_system: true,
        lifecycle_stage: 'approved',
        eu_ai_act_risk_tier: 'limited',
        attributed_to_ai_system: true,
        policy: null,
        effective: PRODUCTION,
        inherited_floor: PRODUCTION,
        sources: {
            mode: LIFECYCLE,
            min_assurance: LIFECYCLE,
            allow_user_keys: LIFECYCLE,
            allowed_issuers: { kind: 'default' },
            max_token_lifetime_s: { kind: 'default' },
            max_key_ttl_days: { kind: 'default' },
        },
        max_user_key_ttl_days: 90,
        ...overrides,
    };
}

const CREDENTIALS = {
    items: [
        {
            credential_ref: 'api_key:k-1', credential_kind: 'api_key', credential_assurance: 'key',
            calls: 12, below_policy_calls: 12, refused_calls: 0,
            first_seen: '2026-11-10T08:00:00Z', last_seen: '2026-11-17T08:00:00Z',
            api_key_id: 'k-1', api_key_name: 'dev key', agent_id: null,
        },
        {
            credential_ref: 'token:idp-1:spiffe://acme.com/ns/loans/sa/screener', credential_kind: 'workload_token', credential_assurance: 'workload',
            calls: 300, below_policy_calls: 0, refused_calls: 0,
            first_seen: '2026-11-10T08:00:00Z', last_seen: '2026-11-18T08:00:00Z',
            api_key_id: null, api_key_name: null, agent_id: 'agent-7',
        },
    ],
    total: 2,
};

beforeEach(() => {
    vi.clearAllMocks();
    granted.clear();
    granted.add('identity_policy:read');
    mockFetchPolicy.mockResolvedValue(answer());
    mockPutPolicy.mockImplementation((_id: string, policy: IdentityPolicy | null) => Promise.resolve(answer({ policy, effective: policy ?? PRODUCTION })));
    mockFetchCredentials.mockResolvedValue(CREDENTIALS);
    mockFetchIdps.mockResolvedValue({
        idps: { 'idp-1': { id: 'idp-1', name: 'SPIRE', protocol: 'oidc' }, 'idp-2': { id: 'idp-2', name: 'Okta SAML', protocol: 'saml' } },
        total: 2,
        enabled_count: 2,
        accessed_by: null,
    });
});

describe('GroupIdentityTab: reading', () => {
    it('asks for the read permission instead of reading without it', async () => {
        granted.clear();
        render(<GroupIdentityTab group={GROUP} />);
        expect(await screen.findByTestId('identity-no-read')).toHaveTextContent('identity_policy:read');
        expect(mockFetchPolicy).not.toHaveBeenCalled();
    });

    it('shows each field of the effective policy with where it comes from and the inherited floor', async () => {
        render(<GroupIdentityTab group={GROUP} />);
        const row = await screen.findByTestId('effective-min_assurance');
        expect(row).toHaveTextContent('Workload');
        expect(row).toHaveTextContent('inherited from Lending (lifecycle: active)');
        expect(screen.getByTestId('floor-min_assurance')).toHaveTextContent('Workload');
        expect(screen.getByTestId('effective-allowed_issuers')).toHaveTextContent('any');
        expect(screen.getByTestId('effective-allowed_issuers')).toHaveTextContent('the development default');
        for (const field of POLICY_FIELDS) expect(screen.getByTestId(`effective-${field}`)).toBeInTheDocument();
    });

    it('explains the lifecycle default and audit against enforce', async () => {
        render(<GroupIdentityTab group={GROUP} />);
        const help = await screen.findByTestId('identity-explained');
        expect(help).toHaveTextContent(/production/i);
        expect(help).toHaveTextContent(/workload/);
        expect(help).toHaveTextContent(/audit/i);
        expect(help).toHaveTextContent(/enforce/i);
        expect(help).toHaveTextContent(/below_policy/);
    });

    it('says when no AI System governs the group', async () => {
        mockFetchPolicy.mockResolvedValue(answer({ is_ai_system: false, attributed_to_ai_system: false, effective: DEVELOPMENT, inherited_floor: DEVELOPMENT }));
        render(<GroupIdentityTab group={{ ...GROUP, group_type: 'standard' } as unknown as ResourceGroup} />);
        expect(await screen.findByTestId('identity-unattributed')).toBeInTheDocument();
        expect(mockFetchCredentials).not.toHaveBeenCalled();
    });

    it('shows a read failure, not an empty policy', async () => {
        mockFetchPolicy.mockRejectedValue(new Error('The Control Plane\'s answer about the identity policy could not be read.'));
        render(<GroupIdentityTab group={GROUP} />);
        expect(await screen.findByTestId('identity-error')).toHaveTextContent(/could not be read/);
        expect(screen.queryByTestId('effective-mode')).not.toBeInTheDocument();
    });
});

describe('GroupIdentityTab: the credentials list', () => {
    it('lists each credential with its kind, level, reference and last seen', async () => {
        render(<GroupIdentityTab group={GROUP} />);
        const list = await screen.findByTestId('identity-credentials');
        expect(mockFetchCredentials).toHaveBeenCalledWith('rg-1', { days: 30 });
        const key = within(list).getByTestId('credential-api_key:k-1');
        expect(key).toHaveTextContent('API key');
        expect(key).toHaveTextContent('Key');
        expect(key).toHaveTextContent('dev key');
        expect(key).toHaveTextContent('12');
        expect(key.querySelector('time')).toHaveAttribute('datetime', '2026-11-17T08:00:00Z');
        const token = within(list).getByTestId('credential-token:idp-1:spiffe://acme.com/ns/loans/sa/screener');
        expect(token).toHaveTextContent('Workload token');
        expect(token).toHaveTextContent('agent-7');
    });

    it('says so when no credential reached the system', async () => {
        mockFetchCredentials.mockResolvedValue({ items: [], total: 0 });
        render(<GroupIdentityTab group={GROUP} />);
        expect(await screen.findByTestId('identity-credentials-empty')).toBeInTheDocument();
    });
});

describe('GroupIdentityTab: the editor', () => {
    it('is hidden without the manage permission, and says which one', async () => {
        render(<GroupIdentityTab group={GROUP} />);
        expect(await screen.findByTestId('identity-no-manage')).toHaveTextContent('identity_policy:manage');
        expect(screen.queryByTestId('identity-editor')).not.toBeInTheDocument();
    });

    it('starts from the group\'s own policy, or from the floor when it has none, and shows the level trade-offs', async () => {
        granted.add('identity_policy:manage');
        render(<GroupIdentityTab group={GROUP} />);
        const level = await screen.findByLabelText(/minimum credential level/i);
        expect(level).toHaveValue('workload');
        expect(screen.getByTestId('trade-offs-levels')).toBeInTheDocument();
        expect(screen.getByTestId('trade-off-workload')).toHaveAttribute('data-selected', 'true');
    });

    it('writes the whole policy', async () => {
        granted.add('identity_policy:manage');
        render(<GroupIdentityTab group={GROUP} />);
        fireEvent.change(await screen.findByLabelText(/longest workload token/i), { target: { value: '900' } });
        fireEvent.click(screen.getByRole('button', { name: /save policy/i }));
        await waitFor(() => expect(mockPutPolicy).toHaveBeenCalledWith('rg-1', { ...PRODUCTION, max_token_lifetime_s: 900 }));
        expect(await screen.findByTestId('identity-notice')).toHaveTextContent(/saved/i);
    });

    it('warns at the field, before saving, when a value is looser than the inherited floor', async () => {
        granted.add('identity_policy:manage');
        render(<GroupIdentityTab group={GROUP} />);
        fireEvent.change(await screen.findByLabelText(/minimum credential level/i), { target: { value: 'key' } });
        expect(screen.getByTestId('field-error-min_assurance')).toHaveTextContent(/below the inherited floor.*Workload/);
    });

    it('refuses an out-of-range value before sending it', async () => {
        granted.add('identity_policy:manage');
        render(<GroupIdentityTab group={GROUP} />);
        fireEvent.change(await screen.findByLabelText(/longest workload token/i), { target: { value: '30' } });
        expect(screen.getByTestId('field-error-max_token_lifetime_s')).toHaveTextContent('Between 60 and 3600 seconds.');
        expect(screen.getByRole('button', { name: /save policy/i })).toBeDisabled();
    });

    it('shows the floor refusal of the Control Plane at each field it names', async () => {
        granted.add('identity_policy:manage');
        mockPutPolicy.mockRejectedValue(
            new GatewayAPIError('The policy relaxes what this group inherits: allow_user_keys', 422, undefined, {
                detail: {
                    message: 'The policy relaxes what this group inherits: allow_user_keys',
                    code: 'identity_policy_below_floor',
                    problems: [{ field: 'allow_user_keys', requested: true, floor: false, source: LIFECYCLE }],
                },
            }),
        );
        render(<GroupIdentityTab group={GROUP} />);
        fireEvent.click(await screen.findByRole('button', { name: /save policy/i }));
        const at = await screen.findByTestId('field-error-allow_user_keys');
        expect(at).toHaveTextContent('Requested accepted; the floor is refused, inherited from Lending (lifecycle: active).');
        expect(screen.getByTestId('identity-error')).toHaveTextContent('The policy relaxes what this group inherits');
    });

    it('limits the identity providers to the tenant\'s OIDC ones, or any', async () => {
        granted.add('identity_policy:manage');
        render(<GroupIdentityTab group={GROUP} />);
        fireEvent.click(await screen.findByLabelText(/any identity provider/i));
        fireEvent.click(await screen.findByLabelText('SPIRE'));
        expect(screen.queryByLabelText('Okta SAML')).not.toBeInTheDocument();
        fireEvent.click(screen.getByRole('button', { name: /save policy/i }));
        await waitFor(() => expect(mockPutPolicy).toHaveBeenCalledWith('rg-1', { ...PRODUCTION, allowed_issuers: ['idp-1'] }));
    });

    it('says when the identity providers cannot be read, rather than offering none', async () => {
        granted.add('identity_policy:manage');
        mockFetchIdps.mockRejectedValue(new Error('forbidden'));
        render(<GroupIdentityTab group={GROUP} />);
        fireEvent.click(await screen.findByLabelText(/any identity provider/i));
        expect(await screen.findByTestId('issuers-error')).toHaveTextContent('The identity providers of this tenant could not be read: forbidden');
        expect(screen.queryByText(/has no OIDC identity provider/)).not.toBeInTheDocument();
    });

    it('removes the group\'s own policy', async () => {
        granted.add('identity_policy:manage');
        mockFetchPolicy.mockResolvedValue(answer({ policy: PRODUCTION }));
        render(<GroupIdentityTab group={GROUP} />);
        fireEvent.click(await screen.findByRole('button', { name: /remove this group's policy/i }));
        await waitFor(() => expect(mockPutPolicy).toHaveBeenCalledWith('rg-1', null));
    });
});
