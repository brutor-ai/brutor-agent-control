/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The MCP server form and `max_concurrent_requests`: how many requests to
 * the server one gateway instance has in flight at once. Empty is the
 * Control Plane's default of 10 (sent as null); anything else must be a
 * positive whole number, and the form says so before it saves. A save the
 * Control Plane refuses shows its sentence.
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
import { resetServerForm } from '../../../../../test/mcp/serverForm';

const LABEL = 'Max concurrent requests (per gateway instance)';

const stored = (over: Partial<MCPServerConfig> = {}): MCPServerConfig => ({
    id: 'srv-1',
    name: 'Payments',
    description: '',
    base_url: 'https://mcp.example.com',
    mcp_endpoint_path: '/mcp',
    auth_type: 'none',
    timeout: 30,
    enabled: true,
    ...over,
});

const onSave = vi.fn();
const renderAdvanced = (server: MCPServerConfig) => {
    render(<ServerForm server={server} onSave={onSave} onCancel={vi.fn()} />);
    fireEvent.click(screen.getByText('Advanced'));
};
const field = () => screen.getByLabelText<HTMLInputElement>(LABEL);
const type = (value: string) => fireEvent.change(field(), { target: { value } });
const save = () => fireEvent.click(screen.getByRole('button', { name: /Update Server/ }));
const saved = async (): Promise<Record<string, unknown>> => {
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    return (onSave.mock.calls[0] as [Record<string, unknown>])[0];
};

beforeEach(() => {
    resetServerForm();
    onSave.mockResolvedValue(undefined);
});
afterEach(cleanup);

describe('ServerForm: max concurrent requests', () => {
    it('is empty for a server that states none, and says empty is 10', () => {
        renderAdvanced(stored({ max_concurrent_requests: null }));
        expect(field().value).toBe('');
        expect(field().placeholder).toBe('10');
        expect(screen.getByTestId('server-max-concurrent-requests-help')).toHaveTextContent('Empty: 10.');
    });

    it('shows what the server states', () => {
        renderAdvanced(stored({ max_concurrent_requests: 25 }));
        expect(field().value).toBe('25');
    });

    it('left empty, the save sends null: the default of 10', async () => {
        renderAdvanced(stored({ max_concurrent_requests: 25 }));
        type('');
        save();
        expect((await saved()).max_concurrent_requests).toBeNull();
    });

    it('a positive whole number is sent as a number', async () => {
        renderAdvanced(stored());
        type('40');
        save();
        expect((await saved()).max_concurrent_requests).toBe(40);
    });

    it.each(['0', '-3', '2.5', '1e3', 'ten'])('%s is refused before the save, and the form says why', (value) => {
        renderAdvanced(stored());
        type(value);
        expect(screen.getByTestId('server-max-concurrent-requests-error')).toHaveTextContent(
            'Max concurrent requests must be a positive whole number, or empty for 10.',
        );
        const button = screen.getByRole<HTMLButtonElement>('button', { name: /Update Server/ });
        expect(button.disabled).toBe(true);
        fireEvent.submit(button.closest('form')!);
        expect(onSave).not.toHaveBeenCalled();
    });

    it('a save the Control Plane refuses shows its sentence', async () => {
        onSave.mockRejectedValueOnce(new Error('max_concurrent_requests: Input should be greater than or equal to 1'));
        renderAdvanced(stored());
        type('7');
        save();
        expect(await screen.findByTestId('server-save-refusal')).toHaveTextContent(
            'max_concurrent_requests: Input should be greater than or equal to 1',
        );
    });
});
