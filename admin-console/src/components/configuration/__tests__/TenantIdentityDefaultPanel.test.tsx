/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Settings → Tenant → Workload Identity: the tenant identity default.
 *
 * It is one of the contributions every group's effective policy is resolved
 * from (stricter wins: it can tighten, never relax, a production floor), and
 * it holds the personal-key ceiling. Writing it needs `identity_policy:manage`
 * on top of the permission the tenant update needs, so without it the panel
 * shows the value and says which permission changes it. It is saved alone,
 * as its own field of the tenant update.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

import { GatewayAPIError } from '../../../lib/gatewayApi/base';

const mockUpdate = vi.fn();
const mockFetchIdps = vi.fn();
const granted = new Set<string>();

vi.mock('../../../hooks/useAuth', () => {
    const gatewayClient = {
        updateTenantIdentityPolicyDefault: (...a: unknown[]): unknown => mockUpdate(...a),
        fetchIdPConfigs: (...a: unknown[]): unknown => mockFetchIdps(...a),
    };
    return { useAuth: () => ({ gatewayClient, hasPermission: (permission: string) => granted.has(permission) }) };
});

import TenantIdentityDefaultPanel from '../TenantIdentityDefaultPanel';

const STORED = {
    mode: 'enforce',
    min_assurance: 'workload',
    allow_user_keys: false,
    allowed_issuers: null,
    max_token_lifetime_s: 900,
    max_key_ttl_days: 90,
    max_user_key_ttl_days: 30,
};

beforeEach(() => {
    vi.clearAllMocks();
    granted.clear();
    mockUpdate.mockImplementation((value: unknown) => Promise.resolve(value));
    mockFetchIdps.mockResolvedValue({ idps: {}, total: 0, enabled_count: 0, accessed_by: null });
});

describe('TenantIdentityDefaultPanel', () => {
    it('shows the stored default read-only, and names the permission that changes it', () => {
        render(<TenantIdentityDefaultPanel tenant={{ id: 't1', identity_policy_default: STORED }} />);
        expect(screen.getByTestId('tenant-identity-value-min_assurance')).toHaveTextContent('Workload');
        expect(screen.getByTestId('tenant-identity-value-max_user_key_ttl_days')).toHaveTextContent('30 days');
        expect(screen.getByTestId('tenant-identity-no-manage')).toHaveTextContent('identity_policy:manage');
        expect(screen.queryByRole('button', { name: /save/i })).not.toBeInTheDocument();
    });

    it('says what applies when no default is set: the development default and a 90-day personal-key ceiling', () => {
        render(<TenantIdentityDefaultPanel tenant={{ id: 't1', identity_policy_default: null }} />);
        expect(screen.getByTestId('tenant-identity-none')).toHaveTextContent(/90 days/);
        expect(screen.getByTestId('tenant-identity-explained')).toHaveTextContent(/stricter/i);
        expect(screen.getByTestId('tenant-identity-explained')).toHaveTextContent(/cannot lower/i);
    });

    it('sets a default, starting from the development default, and saves it as its own field', async () => {
        granted.add('identity_policy:manage');
        const onSaved = vi.fn();
        render(<TenantIdentityDefaultPanel tenant={{ id: 't1', identity_policy_default: null }} onSaved={onSaved} />);
        fireEvent.click(screen.getByRole('button', { name: /set a tenant default/i }));
        expect(screen.getByLabelText(/minimum credential level/i)).toHaveValue('key');
        expect(screen.getByTestId('trade-offs-levels')).toBeInTheDocument();
        fireEvent.change(screen.getByLabelText(/personal key ceiling/i), { target: { value: '60' } });
        fireEvent.click(screen.getByRole('button', { name: /save tenant default/i }));
        const sent = {
            mode: 'audit',
            min_assurance: 'key',
            allow_user_keys: true,
            allowed_issuers: null,
            max_token_lifetime_s: 3600,
            max_key_ttl_days: null,
            max_user_key_ttl_days: 60,
        };
        await waitFor(() => expect(mockUpdate).toHaveBeenCalledWith(sent));
        expect(onSaved).toHaveBeenCalledWith(sent);
        expect(await screen.findByTestId('tenant-identity-notice')).toHaveTextContent(/saved/i);
    });

    it('refuses a personal-key ceiling out of range before sending it', () => {
        granted.add('identity_policy:manage');
        render(<TenantIdentityDefaultPanel tenant={{ id: 't1', identity_policy_default: STORED }} />);
        fireEvent.change(screen.getByLabelText(/personal key ceiling/i), { target: { value: '0' } });
        expect(screen.getByTestId('field-error-max_user_key_ttl_days')).toHaveTextContent('Between 1 and 3650 days.');
        expect(screen.getByRole('button', { name: /save tenant default/i })).toBeDisabled();
    });

    it('removes the default', async () => {
        granted.add('identity_policy:manage');
        render(<TenantIdentityDefaultPanel tenant={{ id: 't1', identity_policy_default: STORED }} />);
        fireEvent.click(screen.getByRole('button', { name: /remove the tenant default/i }));
        await waitFor(() => expect(mockUpdate).toHaveBeenCalledWith(null));
        expect(await screen.findByTestId('tenant-identity-none')).toBeInTheDocument();
    });

    it('shows the Control Plane\'s refusal: the permission, or an unknown identity provider at its field', async () => {
        granted.add('identity_policy:manage');
        mockUpdate.mockRejectedValueOnce(
            new GatewayAPIError('Needs identity_policy:manage', 403, undefined, {
                detail: { message: 'Needs identity_policy:manage', code: 'permission_denied', permission: 'identity_policy:manage' },
            }),
        );
        render(<TenantIdentityDefaultPanel tenant={{ id: 't1', identity_policy_default: STORED }} />);
        fireEvent.click(screen.getByRole('button', { name: /save tenant default/i }));
        expect(await screen.findByTestId('tenant-identity-error')).toHaveTextContent('Needs identity_policy:manage');

        mockUpdate.mockRejectedValueOnce(
            new GatewayAPIError('Unknown issuer', 422, undefined, {
                detail: { message: 'Unknown issuer', code: 'identity_policy_unknown_issuer', issuers: ['idp-9'] },
            }),
        );
        fireEvent.click(screen.getByRole('button', { name: /save tenant default/i }));
        expect(await screen.findByTestId('field-error-allowed_issuers')).toHaveTextContent('idp-9');
    });

    it('says when the stored default cannot be read, and offers no form', () => {
        granted.add('identity_policy:manage');
        render(<TenantIdentityDefaultPanel tenant={{ id: 't1', identity_policy_default: { mode: 'enforce' } }} />);
        expect(screen.getByTestId('tenant-identity-error')).toHaveTextContent(/could not be read/);
        expect(screen.queryByRole('button', { name: /save/i })).not.toBeInTheDocument();
    });
});
