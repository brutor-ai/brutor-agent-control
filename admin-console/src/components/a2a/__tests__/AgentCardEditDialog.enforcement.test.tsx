/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * What the agent card dialog says the gateway does with a card's settings:
 * the card's policy and limits and a capability's hourly rate are enforced
 * on every call that addresses the card; the trust tier decides no call.
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup, fireEvent } from '@testing-library/react';

const gateway = { gatewayClient: { listGuardrailConfigs: vi.fn().mockResolvedValue({ guardrails: [] }) } };
vi.mock('../../../hooks/useGateway', () => ({ useGateway: () => gateway }));

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
    capabilities: [{ capability_name: 'pay', handler_type: 'noop', rate_limit_per_hour: 10 }],
} as unknown as AgentCardDetailResponse;

const open = (tab: string) => {
    render(<AgentCardEditDialog initial={CARD} onClose={vi.fn()} onSaved={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: tab }));
};

afterEach(cleanup);

describe('AgentCardEditDialog: what the gateway enforces', () => {
    it('says the policy of the card is applied to every call that addresses it', () => {
        open('Policies');
        expect(screen.getByTestId('card-policies-help')).toHaveTextContent(
            "The policy of this card. The gateway applies it to every call that addresses the card, together with the policy of the caller's group and of every group above it. Each is judged on its own and the strictest verdict wins.",
        );
    });

    it('says the limits of the card are enforced', () => {
        open('Limits');
        expect(screen.getByTestId('card-limits-help')).toHaveTextContent(
            "The limits of this card. The gateway enforces them on every call that addresses the card, together with the limits of the caller's groups: a call has to fit all of them.",
        );
    });

    it('says the hourly rate of a capability is enforced', () => {
        open('Capabilities');
        expect(screen.getByTestId('capability-rate-help')).toHaveTextContent(
            'Rate limit: the most calls of the capability in one hour. The gateway refuses a call above it. Empty: no rate limit of its own.',
        );
    });

    it('says the trust tier is informational, and that a revoked card takes no call', () => {
        open('Signing');
        expect(screen.getByTestId('trust-tier-help')).toHaveTextContent(
            'The trust tier is informational: the gateway decides no call by it. A revoked card takes no call.',
        );
    });
});
