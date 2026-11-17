/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Tests for RouterTestSubTab — the routing-group exerciser.
 *
 * Coverage:
 *   - Loads + renders enabled routing groups in the dropdown
 *   - Disabled groups are filtered out (dispatching to a
 *     disabled group would predictably 404)
 *   - First enabled group is auto-selected so the operator
 *     can hit Run without scrolling
 *   - Empty-state when the tenant has no enabled groups
 *   - Run Test calls the API with {routing_group_id, message}
 *     using the trimmed input (or the default if blank)
 *   - Renders selected_model + response_text + latency on
 *     success
 *   - Renders the upstream error verbatim on failure
 *   - There is no model dropdown — a model picker would bypass
 *     routing entirely; this guard fails if anyone introduces
 *     one
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

vi.mock('react-hot-toast', () => ({
    default: { success: vi.fn(), error: vi.fn() },
}));

const mockListGroups = vi.fn();
const mockTestRouting = vi.fn();

vi.mock('../../../../hooks/useGateway', () => ({
    useGateway: () => ({
        gatewayClient: {
            listRoutingGroups: mockListGroups,
            testRouting: mockTestRouting,
        },
    }),
}));

// The permissions of the administrator signed in. A test request needs llm:router:test.
const held = { permissions: new Set<string>(['llm:router:read', 'llm:router:test']) };
vi.mock('../../../../hooks/useAuth', () => ({
    useAuth: () => ({ hasPermission: (permission: string) => held.permissions.has(permission) }),
}));

import RouterTestSubTab from '../RouterTestSubTab';
import { GATEWAY_MESSAGES, proxyReturned } from '../../../../lib/__tests__/fixtures/dispatchRefusals';

const TWO_GROUPS = {
    routing_groups: [
        {
            id: 'rg-prod-chat',
            name: 'Production Chat',
            routing_strategy: 'usage-based-routing-v2',
            enabled: true,
            num_models: 3,
        },
        {
            id: 'rg-research',
            name: 'Research',
            routing_strategy: 'simple-shuffle',
            enabled: true,
            num_models: 1,
        },
    ],
    total: 2,
};

beforeEach(() => {
    mockListGroups.mockReset();
    mockTestRouting.mockReset();
    held.permissions = new Set(['llm:router:read', 'llm:router:test']);
});

describe('RouterTestSubTab: who may send a test request', () => {
    it('lets an administrator who holds llm:router:test run the test', async () => {
        mockListGroups.mockResolvedValueOnce(TWO_GROUPS);
        render(<RouterTestSubTab />);
        await screen.findByRole('combobox');
        expect(screen.getByRole('button', { name: 'Run Test' })).toBeEnabled();
        expect(screen.queryByTestId('router-test-not-permitted')).toBeNull();
    });

    it('does not let one who may only read routing groups run it, and says which permission it needs', async () => {
        held.permissions = new Set(['llm:router:read']);
        mockListGroups.mockResolvedValueOnce(TWO_GROUPS);
        render(<RouterTestSubTab />);
        await screen.findByRole('combobox');
        const run = screen.getByRole('button', { name: 'Run Test' });
        expect(run).toBeDisabled();
        expect(screen.getByTestId('router-test-not-permitted')).toHaveTextContent(
            'A test sends a real, billed request through the routing group. It needs the llm:router:test permission, which you do not hold.',
        );
        fireEvent.click(run);
        expect(mockTestRouting).not.toHaveBeenCalled();
    });
});

describe('RouterTestSubTab', () => {
    it('loads groups, filters out disabled, and auto-selects the first', async () => {
        mockListGroups.mockResolvedValueOnce({
            routing_groups: [
                { id: 'rg-disabled', name: 'Off', enabled: false, routing_strategy: 'simple-shuffle', num_models: 1 },
                ...TWO_GROUPS.routing_groups,
            ],
            total: 3,
        });

        render(<RouterTestSubTab />);

        await waitFor(() => expect(mockListGroups).toHaveBeenCalledTimes(1));
        const select = (await screen.findByRole<HTMLSelectElement>('combobox'));

        const labels = Array.from(select.querySelectorAll('option')).map((o) => o.textContent || '');
        // Disabled group must NOT appear; the two enabled ones must.
        expect(labels.some((l) => l.includes('Off'))).toBe(false);
        expect(labels.some((l) => l.includes('Production Chat'))).toBe(true);
        expect(labels.some((l) => l.includes('Research'))).toBe(true);

        // First enabled is auto-selected so the operator can
        // immediately click Run.
        expect(select.value).toBe('rg-prod-chat');
    });

    it('shows an empty-state when no enabled groups exist', async () => {
        mockListGroups.mockResolvedValueOnce({
            routing_groups: [
                { id: 'rg-off', name: 'Off', enabled: false, routing_strategy: 'simple-shuffle', num_models: 1 },
            ],
            total: 1,
        });

        render(<RouterTestSubTab />);

        await screen.findByText(/No enabled routing groups/i);
        expect(screen.getByRole('button', { name: /Run Test/ })).toBeDisabled();
    });

    it('Run Test calls the API with routing_group_id + message', async () => {
        mockListGroups.mockResolvedValueOnce(TWO_GROUPS);
        mockTestRouting.mockResolvedValueOnce({
            timestamp: '2026-05-10T10:00:00Z',
            routing_group_id: 'rg-prod-chat',
            routing_group_name: 'Production Chat',
            routing_strategy: 'usage-based-routing-v2',
            message: 'custom test',
            selected_model: 'gpt-4o-mini',
            selected_provider: 'openai',
            response_text: 'Hello!',
            latency_ms: 412,
            status_code: 200,
            error: null,
        });

        render(<RouterTestSubTab />);
        await screen.findByRole('combobox');

        // Override the default message.
        const textarea = screen.getByRole('textbox');
        fireEvent.change(textarea, { target: { value: 'custom test' } });

        // Switch to the second group.
        fireEvent.change(screen.getByRole('combobox'), {
            target: { value: 'rg-research' },
        });

        fireEvent.click(screen.getByRole('button', { name: /Run Test/ }));

        await waitFor(() => {
            expect(mockTestRouting).toHaveBeenCalledWith({
                routing_group_id: 'rg-research',
                message: 'custom test',
            });
        });
    });

    it('falls back to the default message when the textarea is blank', async () => {
        mockListGroups.mockResolvedValueOnce(TWO_GROUPS);
        mockTestRouting.mockResolvedValueOnce({
            timestamp: '2026-05-10T10:00:00Z',
            routing_group_id: 'rg-prod-chat',
            routing_group_name: 'Production Chat',
            routing_strategy: 'usage-based-routing-v2',
            message: '',
            selected_model: 'gpt-4o-mini',
            response_text: 'ok',
            latency_ms: 100,
            status_code: 200,
        });

        render(<RouterTestSubTab />);
        await screen.findByRole('combobox');

        const textarea = screen.getByRole('textbox');
        fireEvent.change(textarea, { target: { value: '   ' } });

        fireEvent.click(screen.getByRole('button', { name: /Run Test/ }));

        await waitFor(() => {
            const arg = mockTestRouting.mock.calls[0][0];
            expect(arg.message.length).toBeGreaterThan(0);
            expect(arg.message).not.toMatch(/^\s+$/);
        });
    });

    it('renders selected_model + response_text + latency on success', async () => {
        mockListGroups.mockResolvedValueOnce(TWO_GROUPS);
        mockTestRouting.mockResolvedValueOnce({
            timestamp: '2026-05-10T10:00:00Z',
            routing_group_id: 'rg-prod-chat',
            routing_group_name: 'Production Chat',
            routing_strategy: 'usage-based-routing-v2',
            message: 'ping',
            selected_model: 'claude-sonnet-4',
            selected_provider: 'anthropic',
            response_text: 'Pong!',
            latency_ms: 248,
            status_code: 200,
            error: null,
        });

        render(<RouterTestSubTab />);
        await screen.findByRole('combobox');

        fireEvent.click(screen.getByRole('button', { name: /Run Test/ }));

        await screen.findByText(/Selected model: claude-sonnet-4/);
        expect(screen.getByText(/Provider: anthropic/)).toBeInTheDocument();
        expect(screen.getByText(/248 ms/)).toBeInTheDocument();
        expect(screen.getByText('Pong!')).toBeInTheDocument();
    });

    it('renders the upstream error verbatim on failure', async () => {
        mockListGroups.mockResolvedValueOnce(TWO_GROUPS);
        mockTestRouting.mockResolvedValueOnce({
            timestamp: '2026-05-10T10:00:00Z',
            routing_group_id: 'rg-prod-chat',
            routing_group_name: 'Production Chat',
            routing_strategy: 'usage-based-routing-v2',
            message: 'ping',
            selected_model: null,
            selected_provider: null,
            response_text: null,
            latency_ms: 30,
            status_code: 401,
            error: 'Proxy returned 401: {"error":{"message":"Invalid Authentication"}}',
        });

        render(<RouterTestSubTab />);
        await screen.findByRole('combobox');

        fireEvent.click(screen.getByRole('button', { name: /Run Test/ }));

        await screen.findByText(/Test failed/);
        expect(screen.getByText(/Invalid Authentication/)).toBeInTheDocument();
    });

    it('says what to fix when the gateway cannot dispatch the selected model, and keeps its answer below', async () => {
        const raw = proxyReturned(GATEWAY_MESSAGES.provider_api_url_missing);
        mockListGroups.mockResolvedValueOnce(TWO_GROUPS);
        mockTestRouting.mockResolvedValueOnce({
            timestamp: '2026-05-10T10:00:00Z',
            routing_group_id: 'rg-prod-chat',
            routing_group_name: 'Production Chat',
            routing_strategy: 'usage-based-routing-v2',
            message: 'ping',
            selected_model: null,
            selected_provider: null,
            response_text: null,
            latency_ms: 3,
            status_code: 501,
            error: raw,
        });

        render(<RouterTestSubTab />);
        await screen.findByRole('combobox');
        fireEvent.click(screen.getByRole('button', { name: /Run Test/ }));

        await screen.findByText(/Test failed/);
        expect(screen.getByText(/Edit the model and set API URL/)).toBeInTheDocument();
        // A routing group has several models: the gateway's answer names which.
        expect(screen.getByText(raw)).toBeInTheDocument();
    });

    it('does NOT show a model dropdown — guards against re-introducing the bypass', async () => {
        mockListGroups.mockResolvedValueOnce(TWO_GROUPS);
        render(<RouterTestSubTab />);
        await screen.findByRole('combobox');

        // Exactly one combobox — the routing-group dropdown.
        // If a change adds a model picker that bypasses
        // routing, this fails — forcing a deliberate decision.
        expect(screen.getAllByRole('combobox')).toHaveLength(1);
        // No "Let Router Decide" placeholder either.
        expect(screen.queryByText(/Let Router Decide/)).not.toBeInTheDocument();
    });
});
