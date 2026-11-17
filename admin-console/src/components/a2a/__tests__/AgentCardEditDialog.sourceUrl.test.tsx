/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * A card's source URL is set when the card is created. The Control Plane's
 * update of a card names no `source_url` and refuses a field it does not
 * name, so the dialog shows the stored URL on an edit, takes no change to
 * it, and sends none.
 */

import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react';

const client = {
    listGuardrailConfigs: vi.fn().mockResolvedValue({ guardrails: [] }),
    updateA2AAgentCard: vi.fn(),
    createA2AAgentCard: vi.fn(),
    replaceA2AAgentCardCapabilities: vi.fn(),
};
vi.mock('../../../hooks/useGateway', () => ({ useGateway: () => ({ gatewayClient: client }) }));

import { AgentCardEditDialog } from '../AgentCardEditDialog';
import type { AgentCardDetailResponse } from '../../../lib/a2aTypes';

const CARD = {
    id: 'card-1',
    name: 'Billing agent',
    version: '1.0',
    description: '',
    source_url: 'https://source.example.com/card.json',
    trust_tier: 'tenant_signed',
    enabled: true,
    card_json: {},
    capabilities: [],
} as unknown as AgentCardDetailResponse;

beforeEach(() => {
    client.updateA2AAgentCard.mockReset().mockResolvedValue(CARD);
    client.createA2AAgentCard.mockReset().mockResolvedValue(CARD);
    client.replaceA2AAgentCardCapabilities.mockReset().mockResolvedValue({});
});
afterEach(cleanup);

describe('AgentCardEditDialog: the source URL', () => {
    it('is shown and cannot be changed on an edit, and the update names no source_url', async () => {
        render(<AgentCardEditDialog initial={CARD} onClose={vi.fn()} onSaved={vi.fn()} />);
        const field = screen.getByDisplayValue('https://source.example.com/card.json');
        expect(field).toHaveAttribute('readonly');
        expect(screen.getByTestId('source-url-help')).toHaveTextContent('Set when the card is created. An update does not change it.');

        fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }));
        await waitFor(() => expect(client.updateA2AAgentCard).toHaveBeenCalledTimes(1));
        const [id, body] = client.updateA2AAgentCard.mock.calls[0] as [string, Record<string, unknown>];
        expect(id).toBe('card-1');
        expect(body).not.toHaveProperty('source_url');
        expect(Object.keys(body).sort()).toEqual(['card_json', 'description', 'enabled', 'name', 'tenant_governance', 'tenant_limits', 'version']);
    });

    it('is a field of a new card', () => {
        render(<AgentCardEditDialog initial={null} onClose={vi.fn()} onSaved={vi.fn()} />);
        const field = screen.getByPlaceholderText('https://source.example.com/agent-card.json');
        expect(field).not.toHaveAttribute('readonly');
        expect(screen.queryByTestId('source-url-help')).toBeNull();
    });
});
