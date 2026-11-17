/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * A card's limits, read with a value the Control Plane now refuses
 * (an A2A warning percentage, `hard_stop: false`), are saved without it, so
 * a save of the card is not refused for a setting nobody touched.
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react';

import { STORED_KEPT, STORED_WITH_REFUSED, controlPlaneRefusal } from '../../../test/controlPlaneRefusal';

const gatewayClient = {
    listGuardrailConfigs: vi.fn().mockResolvedValue({ guardrails: [] }),
    updateA2AAgentCard: vi.fn((_id: string, body: { tenant_limits?: unknown }) => {
        const refusal = controlPlaneRefusal('a2a_limits', body.tenant_limits);
        return refusal ? Promise.reject(new Error(refusal)) : Promise.resolve({});
    }),
    setA2AAgentCardGroups: vi.fn().mockResolvedValue({}),
};
vi.mock('../../../hooks/useGateway', () => ({ useGateway: () => ({ gatewayClient }) }));

import { AgentCardEditDialog } from '../AgentCardEditDialog';
import type { AgentCardDetailResponse } from '../../../lib/a2aTypes';

const CARD = {
    id: 'card-1',
    name: 'Billing agent',
    version: '1.0',
    description: '',
    trust_tier: 'tenant_signed',
    enabled: true,
    card_json: {},
    capabilities: [],
    tenant_limits: STORED_WITH_REFUSED.a2a_limits,
} as unknown as AgentCardDetailResponse;

afterEach(cleanup);

describe('AgentCardEditDialog and a stored limit the Control Plane refuses', () => {
    it('a save without an edit sends none of it, and is accepted', async () => {
        const onSaved = vi.fn();
        render(<AgentCardEditDialog initial={CARD} onClose={vi.fn()} onSaved={onSaved} />);
        fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }));
        await waitFor(() => expect(onSaved).toHaveBeenCalled());
        expect(gatewayClient.updateA2AAgentCard).toHaveBeenCalledWith(
            'card-1',
            expect.objectContaining({ tenant_limits: STORED_KEPT.a2a_limits }),
        );
    });
});
