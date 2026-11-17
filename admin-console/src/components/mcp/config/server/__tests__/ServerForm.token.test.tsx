/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The MCP server form and the static token of a bearer server.
 *
 * The Control Plane stores the token encrypted and never returns it: a
 * server answers `has_auth_token`. The form starts with an empty token
 * field and says whether one is stored. Saving without typing keeps the
 * stored token, typing replaces it, and removing it is asked for in so many
 * words.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react';

const shared = vi.hoisted(() => () => import('../../../../../test/mcp/serverForm'));
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() }));
vi.mock('../../../../../hooks/useGateway.ts', async () => (await shared()).useGatewayModule());
vi.mock('../../../../common/Toast.tsx', () => ({ useToast: () => toast }));
vi.mock('../ServerIcon.tsx', async () => (await shared()).blankModule());
vi.mock('../oauth/OAuthDiscoveryDisplay.tsx', async () => (await shared()).blankModule());
vi.mock('../../../../groups/subtabs/MCPLimitsForm.tsx', async () => (await shared()).blankModule());
vi.mock('../../../../groups/subtabs/MCPGovernanceForm.tsx', async () => (await shared()).blankModule());
vi.mock('../../../../common/AuditInfoTab', async () => (await shared()).blankModule());

import ServerForm from '../ServerForm';
import type { MCPServerConfig } from '../../../../../lib/types.ts';
import { gateway, resetServerForm } from '../../../../../test/mcp/serverForm';
import { STORED_KEPT, STORED_WITH_REFUSED, controlPlaneRefusal } from '../../../../../test/controlPlaneRefusal';

const stored = (over: Partial<MCPServerConfig> = {}): MCPServerConfig => ({
    id: 'srv-1',
    name: 'Payments',
    description: '',
    base_url: 'https://mcp.example.com',
    mcp_endpoint_path: '/mcp',
    auth_type: 'bearer',
    has_auth_token: true,
    timeout: 30,
    enabled: true,
    ...over,
});

const onSave = vi.fn();
const renderForm = (server: MCPServerConfig | null) => render(<ServerForm server={server} onSave={onSave} onCancel={vi.fn()} />);
const tokenField = () => screen.getByLabelText<HTMLInputElement>('Bearer Token');
const save = (name: RegExp) => fireEvent.click(screen.getByRole('button', { name }));
const saved = async (): Promise<Record<string, unknown>> => {
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    return (onSave.mock.calls[0] as [Record<string, unknown>])[0];
};

beforeEach(() => {
    resetServerForm();
    onSave.mockResolvedValue(undefined);
});
afterEach(cleanup);

describe('ServerForm: the token of a bearer server', () => {
    it('starts with an empty token field and says one is stored', () => {
        renderForm(stored());
        expect(tokenField().value).toBe('');
        expect(screen.getByTestId('server-token-stored')).toHaveTextContent(
            'A token is stored. It is never shown. Leave the field empty to keep it, or type a new one to replace it.',
        );
    });

    it('says so when no token is stored, and offers nothing to remove', () => {
        renderForm(stored({ has_auth_token: false }));
        expect(screen.getByTestId('server-token-stored')).toHaveTextContent('No token is stored. Type the token of this server.');
        expect(screen.queryByLabelText('Remove the stored token')).toBeNull();
    });

    it('shows no token field for a server without bearer authentication', () => {
        renderForm(stored({ auth_type: 'none', has_auth_token: false }));
        expect(screen.queryByLabelText('Bearer Token')).toBeNull();
    });

    it('saving without typing does not name the token, so the stored one is kept', async () => {
        renderForm(stored());
        save(/Update Server/);
        const payload = await saved();
        expect('auth_token' in payload).toBe(false);
        expect(payload.auth_type).toBe('bearer');
    });

    it('typing a token replaces the stored one', async () => {
        renderForm(stored());
        fireEvent.change(tokenField(), { target: { value: 'new-token' } });
        save(/Update Server/);
        expect((await saved()).auth_token).toBe('new-token');
    });

    it('removing the stored token is asked for, and sends an empty string', async () => {
        renderForm(stored());
        fireEvent.click(screen.getByLabelText('Remove the stored token'));
        save(/Update Server/);
        expect((await saved()).auth_token).toBe('');
    });

    it('says that saving without bearer authentication removes the stored token, and removes it', async () => {
        renderForm(stored());
        fireEvent.click(screen.getByRole('radio', { name: /No Authentication/ }));
        expect(screen.getByTestId('server-token-removed-on-save')).toHaveTextContent(
            'A token is stored for this server. Saving the server without bearer authentication removes it.',
        );
        save(/Update Server/);
        const payload = await saved();
        expect(payload.auth_type).toBe('none');
        expect(payload.auth_token).toBe('');
    });

    it('a token typed before the authentication was changed from bearer is not sent: the stored one is removed', async () => {
        renderForm(stored());
        fireEvent.change(tokenField(), { target: { value: 'typed-then-abandoned' } });
        fireEvent.click(screen.getByRole('radio', { name: /oauth/i }));
        expect(screen.queryByLabelText('Bearer Token')).toBeNull();
        save(/Update Server/);
        const payload = await saved();
        expect(payload.auth_type).toBe('oauth');
        expect(payload.auth_token).toBe('');
    });

    it('a server with no stored token, saved without bearer authentication, does not name the token whatever was typed', async () => {
        renderForm(stored({ has_auth_token: false }));
        fireEvent.change(tokenField(), { target: { value: 'typed-then-abandoned' } });
        fireEvent.click(screen.getByRole('radio', { name: /No Authentication/ }));
        expect(screen.queryByLabelText('Bearer Token')).toBeNull();
        save(/Update Server/);
        const payload = await saved();
        expect(payload.auth_type).toBe('none');
        expect('auth_token' in payload).toBe(false);
    });

    it('a new server without bearer authentication offers no token field and names no token, whatever was typed', async () => {
        Object.assign(gateway.gatewayClient, {
            testServerConnection: vi.fn().mockResolvedValue({ status: 'success', duration_ms: 1 }),
        });
        renderForm(null);
        fireEvent.change(screen.getByPlaceholderText('e.g., Todo Management Server'), { target: { value: 'Mine' } });
        fireEvent.change(screen.getByPlaceholderText('http://localhost:3000'), { target: { value: 'https://mcp.example.com' } });
        expect(screen.queryByLabelText('Bearer Token')).toBeNull();
        fireEvent.click(screen.getByRole('radio', { name: /bearer/i }));
        fireEvent.change(tokenField(), { target: { value: 'tok' } });
        fireEvent.click(screen.getByRole('radio', { name: /No Authentication/ }));
        expect(screen.queryByLabelText('Bearer Token')).toBeNull();
        fireEvent.click(screen.getByRole('button', { name: /Test Connection/ }));
        await waitFor(() => expect(toast.success).toHaveBeenCalled());
        save(/Create Server/);
        const payload = await saved();
        expect(payload.auth_type).toBe('none');
        expect('auth_token' in payload).toBe(false);
    });

    it('a new bearer server sends the token that was typed', async () => {
        Object.assign(gateway.gatewayClient, {
            testServerConnection: vi.fn().mockResolvedValue({ status: 'success', duration_ms: 1 }),
        });
        renderForm(null);
        fireEvent.change(screen.getByPlaceholderText('e.g., Todo Management Server'), { target: { value: 'Mine' } });
        fireEvent.change(screen.getByPlaceholderText('http://localhost:3000'), { target: { value: 'https://mcp.example.com' } });
        fireEvent.click(screen.getByRole('radio', { name: /bearer/i }));
        expect(screen.queryByTestId('server-token-stored')).toBeNull();
        fireEvent.change(tokenField(), { target: { value: 'tok' } });
        fireEvent.click(screen.getByRole('button', { name: /Test Connection/ }));
        await waitFor(() => expect(toast.success).toHaveBeenCalled());
        save(/Create Server/);
        const payload = await saved();
        expect(payload).toMatchObject({ name: 'Mine', auth_type: 'bearer', auth_token: 'tok' });
    });

    it('shows what the Control Plane says when it cannot store a token', async () => {
        // 503: an installation with no encryption key stores no token.
        const refused =
            'auth_token cannot be stored: this control plane has no encryption service (ENCRYPTION_KEY), and a credential is never stored in clear. Nothing was saved.';
        onSave.mockRejectedValue(new Error(refused));
        renderForm(stored());
        fireEvent.change(tokenField(), { target: { value: 'new-token' } });
        save(/Update Server/);
        await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Failed to Update Server', refused));
        expect(await screen.findByTestId('server-token-refused')).toHaveTextContent(refused);
        // What was typed is still there to save again.
        expect(tokenField().value).toBe('new-token');
    });
});

// A server's tenant-wide limits, read with a value the Control Plane
// now refuses, are saved without it.
describe('ServerForm and a stored limit the Control Plane refuses', () => {
    it('a save without an edit sends none of it, and is accepted', async () => {
        onSave.mockImplementation((data: { tenant_limits?: unknown }) => {
            const refusal = controlPlaneRefusal('mcp_limits', data.tenant_limits);
            return refusal ? Promise.reject(new Error(refusal)) : Promise.resolve(undefined);
        });
        renderForm(stored({ tenant_limits: STORED_WITH_REFUSED.mcp_limits }));
        save(/Update Server/);
        expect((await saved()).tenant_limits).toEqual(STORED_KEPT.mcp_limits);
        await waitFor(() => expect(toast.success).toHaveBeenCalled());
    });
});
