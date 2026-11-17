/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * URL-first agent card creation.
 *
 * Create is gated on a successful Connect for exactly the URL in the box: editing
 * the URL afterwards must block it again, or the card imported is not the one the
 * operator previewed. Discovery fills in Name and Description but never overwrites
 * what the operator typed, and a name collision blanks the field.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';

const gatewayClient = {
    discoverA2AAgentCard: vi.fn(),
    importA2AAgentCard: vi.fn(),
};
const gateway = { gatewayClient };
const toast = { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() };

vi.mock('../../../hooks/useGateway', () => ({ useGateway: () => gateway }));
vi.mock('../../common/Toast', () => ({ useToast: () => toast }));

import AgentCardCreateDialog from '../AgentCardCreateDialog';

const DISCOVERY = {
    name: 'hotel-agent',
    description: 'Books hotels',
    name_conflict: false,
    version: '2.1',
    protocol_version: '0.3',
    provider_name: 'Salem',
    icon_url: null,
    capability_count: 2,
    capabilities: [
        { id: 'reserve', name: 'reserve', description: 'Reserve a room', tags: ['booking', 'hotel'] },
        { id: 'cancel', name: 'cancel', description: null, tags: [] },
    ],
    resolved_url: 'https://hotel.test/.well-known/agent-card.json',
};

const NAME = 'e.g. salem-hotel-agent';
const DESCRIPTION = 'What this agent is used for in your org';

function urlBox(): HTMLElement {
    const label = screen.getByText('Agent URL');
    const input = label.closest('div')?.querySelector('input');
    if (!input) throw new Error('no url input');
    return input;
}

function button(name: RegExp | string): HTMLButtonElement {
    return screen.getByRole<HTMLButtonElement>('button', { name });
}

function setup() {
    const onClose = vi.fn();
    const onCreated = vi.fn();
    render(<AgentCardCreateDialog onClose={onClose} onCreated={onCreated} />);
    return { onClose, onCreated };
}

async function connect(url = 'https://hotel.test') {
    fireEvent.change(urlBox(), { target: { value: url } });
    fireEvent.click(button(/Connect/));
    await waitFor(() => expect(gatewayClient.discoverA2AAgentCard).toHaveBeenCalled());
    await waitFor(() => expect(button(/Connect/).disabled).toBe(false));
}

describe('AgentCardCreateDialog', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        gatewayClient.discoverA2AAgentCard.mockResolvedValue(DISCOVERY);
        gatewayClient.importA2AAgentCard.mockResolvedValue({ id: 'c-1', name: 'hotel-agent' });
    });

    afterEach(() => cleanup());

    it('cannot connect or create before a URL is entered', () => {
        setup();
        expect(button(/Connect/).disabled).toBe(true);
        expect(button(/Create/).disabled).toBe(true);
    });

    it('discovers the trimmed URL and fills in name and description', async () => {
        setup();
        await connect('  https://hotel.test  ');

        expect(gatewayClient.discoverA2AAgentCard).toHaveBeenCalledWith({ source_url: 'https://hotel.test' });
        expect(screen.getByDisplayValue('hotel-agent')).toBeTruthy();
        expect(screen.getByDisplayValue('Books hotels')).toBeTruthy();
        expect(button(/Create/).disabled).toBe(false);
    });

    it('previews what was discovered, before anything is saved', async () => {
        setup();
        await connect();

        expect(screen.getByText('A2A 0.3')).toBeTruthy();
        expect(screen.getByText('Provider: Salem')).toBeTruthy();
        expect(screen.getByText('2 skills')).toBeTruthy();
        expect(screen.getByText('Reserve a room')).toBeTruthy();
        expect(screen.getByText('#booking #hotel')).toBeTruthy();
        expect(screen.getByText('https://hotel.test/.well-known/agent-card.json')).toBeTruthy();
        expect(screen.queryByText('legacy 0.2.x path')).toBeNull();
        expect(gatewayClient.importA2AAgentCard).not.toHaveBeenCalled();
    });

    it('flags a card served from the legacy path', async () => {
        gatewayClient.discoverA2AAgentCard.mockResolvedValue({
            ...DISCOVERY,
            name: null,
            description: null,
            version: null,
            protocol_version: null,
            provider_name: null,
            capability_count: 1,
            capabilities: [],
            resolved_url: 'https://hotel.test/.well-known/agent.json',
        });
        setup();
        await connect();

        expect(screen.getByText('legacy 0.2.x path')).toBeTruthy();
        expect(screen.getByText('1 skill')).toBeTruthy();
        expect(screen.getByText('(unnamed agent)')).toBeTruthy();
        expect(screen.getByText('(no description)')).toBeTruthy();
        expect(screen.queryByText('Skills the agent advertises')).toBeNull();
    });

    it('never overwrites what the operator typed', async () => {
        setup();
        fireEvent.change(screen.getByPlaceholderText(NAME), { target: { value: 'mine' } });
        fireEvent.change(screen.getByPlaceholderText(DESCRIPTION), { target: { value: 'my words' } });
        await connect();

        expect(screen.getByDisplayValue('mine')).toBeTruthy();
        expect(screen.getByDisplayValue('my words')).toBeTruthy();
    });

    it('blanks the name on a collision, explains it, and blocks create until a new name is typed', async () => {
        gatewayClient.discoverA2AAgentCard.mockResolvedValue({ ...DISCOVERY, name_conflict: true });
        setup();
        await connect();

        expect((screen.getByPlaceholderText<HTMLInputElement>(NAME)).value).toBe('');
        expect(screen.getByText(/already taken in this tenant/)).toBeTruthy();
        expect(button(/Create/).disabled).toBe(true);

        fireEvent.change(screen.getByPlaceholderText(NAME), { target: { value: 'hotel-2' } });
        expect(button(/Create/).disabled).toBe(false);
    });

    it('blocks create again once the URL changes, until it reconnects', async () => {
        setup();
        await connect();
        fireEvent.change(urlBox(), { target: { value: 'https://other.test' } });
        expect(button(/Create/).disabled).toBe(true);

        fireEvent.click(button(/Connect/));
        await waitFor(() =>
            expect(gatewayClient.discoverA2AAgentCard).toHaveBeenLastCalledWith({ source_url: 'https://other.test' }),
        );
        await waitFor(() => expect(button(/Create/).disabled).toBe(false));
    });

    it('shows a connection failure and keeps create blocked', async () => {
        gatewayClient.discoverA2AAgentCard.mockRejectedValue(new Error('no card found'));
        setup();
        await connect();

        expect(await screen.findByText(/no card found/)).toBeTruthy();
        expect(button(/Create/).disabled).toBe(true);
    });

    it('imports with the overrides and hands the card to the parent', async () => {
        const { onCreated } = setup();
        await connect();
        fireEvent.change(screen.getByPlaceholderText(NAME), { target: { value: '  renamed  ' } });
        fireEvent.change(screen.getByPlaceholderText(DESCRIPTION), { target: { value: '  Summary  ' } });
        fireEvent.click(button(/Create/));

        await waitFor(() =>
            expect(gatewayClient.importA2AAgentCard).toHaveBeenCalledWith({
                source_url: 'https://hotel.test',
                name_override: 'renamed',
                description_override: 'Summary',
            }),
        );
        await waitFor(() => expect(onCreated).toHaveBeenCalledWith({ id: 'c-1', name: 'hotel-agent' }));
        expect(toast.success).toHaveBeenCalledWith('Agent card created', 'hotel-agent');
    });

    it('sends a null description override when it is left empty', async () => {
        gatewayClient.discoverA2AAgentCard.mockResolvedValue({ ...DISCOVERY, description: null });
        setup();
        await connect();
        fireEvent.click(button(/Create/));

        await waitFor(() =>
            expect(gatewayClient.importA2AAgentCard).toHaveBeenCalledWith({
                source_url: 'https://hotel.test',
                name_override: 'hotel-agent',
                description_override: null,
            }),
        );
    });

    it('reports a failed create and keeps the dialog usable', async () => {
        gatewayClient.importA2AAgentCard.mockRejectedValue(new Error('409 conflict'));
        const { onCreated } = setup();
        await connect();
        fireEvent.click(button(/Create/));

        await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Create failed', '409 conflict'));
        expect(onCreated).not.toHaveBeenCalled();
        await waitFor(() => expect(button(/Create/).disabled).toBe(false));
    });

    it('closes from the header and the backdrop, not from inside', () => {
        const { onClose } = setup();
        fireEvent.click(screen.getByText('Add Agent Card'));
        expect(onClose).not.toHaveBeenCalled();

        fireEvent.click(button('Close'));
        const backdrop = document.querySelector('.fixed.inset-0');
        if (!backdrop) throw new Error('no backdrop');
        fireEvent.click(backdrop);
        expect(onClose).toHaveBeenCalledTimes(2);
    });
});
