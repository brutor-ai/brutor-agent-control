/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Creating a card from the dialog: the create names whether the card is
 * enabled, so a card created with Enabled off is never enabled.
 */

import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react';

const gatewayClient = {
    listGuardrailConfigs: vi.fn().mockResolvedValue({ guardrails: [] }),
    createA2AAgentCard: vi.fn(),
    updateA2AAgentCard: vi.fn(),
};
const gateway = { gatewayClient };
vi.mock('../../../hooks/useGateway', () => ({ useGateway: () => gateway }));

import { AgentCardEditDialog } from '../AgentCardEditDialog';
import type { AgentCardDetailResponse } from '../../../lib/a2aTypes';

beforeEach(() => {
    vi.clearAllMocks();
    gatewayClient.createA2AAgentCard.mockResolvedValue({ id: 'card-9' });
});
afterEach(cleanup);

const create = async () => {
    const onSaved = vi.fn();
    render(<AgentCardEditDialog initial={null} onClose={vi.fn()} onSaved={onSaved} />);
    fireEvent.change(screen.getByPlaceholderText('billing-agent'), { target: { value: 'Billing agent' } });
    return onSaved;
};

describe('AgentCardEditDialog: the approval timeout of the card policy', () => {
    const CARD = {
        id: 'card-1', name: 'Billing agent', version: '1.0', description: '', trust_tier: 'tenant_signed', enabled: true,
        card_json: {}, capabilities: [], tenant_governance: { capability: { default_access: 'approval_required', approval_timeout_seconds: 300 } },
    } as unknown as AgentCardDetailResponse;

    it('keeps Save off, and saves nothing, while the timeout is outside 1 .. 604800 seconds', async () => {
        gatewayClient.updateA2AAgentCard.mockResolvedValue({});
        const onSaved = vi.fn();
        render(<AgentCardEditDialog initial={CARD} onClose={vi.fn()} onSaved={onSaved} />);
        fireEvent.click(screen.getByRole('button', { name: 'Policies' }));
        const timeout = screen.getByDisplayValue('300');
        const save = screen.getByRole('button', { name: 'Save Changes' });
        expect(save).toBeEnabled();

        fireEvent.change(timeout, { target: { value: '0' } });
        expect(save).toBeDisabled();
        expect(save).toHaveAttribute('title', 'An approval timeout is a whole number of seconds from 1 to 604800 (7 days).');
        fireEvent.click(save);
        expect(gatewayClient.updateA2AAgentCard).not.toHaveBeenCalled();

        fireEvent.change(timeout, { target: { value: '900' } });
        expect(save).toBeEnabled();
        fireEvent.click(save);
        await waitFor(() => expect(onSaved).toHaveBeenCalled());
        expect(gatewayClient.updateA2AAgentCard).toHaveBeenCalledWith(
            'card-1',
            expect.objectContaining({ tenant_governance: { capability: { default_access: 'approval_required', approval_timeout_seconds: 900 } } }),
        );
    });
});

describe('AgentCardEditDialog: creating a card', () => {
    it('creates the card disabled when Enabled is off', async () => {
        const onSaved = await create();
        fireEvent.click(screen.getByRole('switch', { name: 'Enabled' }));
        fireEvent.click(screen.getByRole('button', { name: 'Create Card' }));

        await waitFor(() => expect(onSaved).toHaveBeenCalled());
        expect(gatewayClient.createA2AAgentCard).toHaveBeenCalledTimes(1);
        expect(gatewayClient.createA2AAgentCard).toHaveBeenCalledWith(
            expect.objectContaining({ name: 'Billing agent', enabled: false }),
        );
        // One request: the card is not created enabled and then switched off.
        expect(gatewayClient.updateA2AAgentCard).not.toHaveBeenCalled();
    });

    it('creates the card enabled when Enabled is left on', async () => {
        const onSaved = await create();
        fireEvent.click(screen.getByRole('button', { name: 'Create Card' }));

        await waitFor(() => expect(onSaved).toHaveBeenCalled());
        expect(gatewayClient.createA2AAgentCard).toHaveBeenCalledWith(
            expect.objectContaining({ enabled: true }),
        );
    });
});
