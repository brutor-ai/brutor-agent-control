/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The endpoint modal is a URL builder an operator copies from and pastes into
 * a client. Every URL it prints is wrong-until-proven-right in a way nothing
 * downstream will catch: a card name with a space or a slash, unescaped, sends
 * the integrator to a 404 they will blame on the gateway.
 *
 * So the tests pin the encoding, the full endpoint set (the A2A v1.0 spec
 * splits send/stream/cancel/subscribe across separate paths — dropping one
 * silently removes a capability from the docs), the base-URL trailing-slash
 * normalisation, and the public/authenticated distinction that stops someone
 * pasting an `/a2a/...` URL into a browser.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

import AgentCardEndpointModal from '../AgentCardEndpointModal';

const PROPS = {
    cardName: 'revenue-sentinel',
    tenantId: 'default',
    gatewayBaseUrl: 'https://gw.example.com',
    onClose: vi.fn(),
};

function setup(overrides: Partial<typeof PROPS> = {}) {
    const onClose = vi.fn();
    render(<AgentCardEndpointModal {...PROPS} {...overrides} onClose={onClose} />);
    return { onClose };
}

const urls = () =>
    Array.from(document.querySelectorAll('code')).map((c) => c.textContent);

describe('AgentCardEndpointModal — URLs', () => {
    it('builds the public discovery URL at the well-known path', () => {
        setup();
        expect(
            screen.getByText(
                'https://gw.example.com/.well-known/agent-card/default/revenue-sentinel.json'
            )
        ).toBeInTheDocument();
    });

    it('publishes every invocation endpoint the A2A spec splits out', () => {
        setup();
        const base = 'https://gw.example.com/a2a/default/revenue-sentinel';
        for (const path of [
            `${base}/message:send`,
            `${base}/message:stream`,
            `${base}/tasks`,
            `${base}/tasks/{task_id}:subscribe`,
            `${base}/extendedAgentCard`,
        ]) {
            expect(urls()).toContain(path);
        }
    });

    it('percent-encodes card names and tenant ids', () => {
        // A card called "Sales Bot/v2" must not emit a path with a raw space
        // and a raw slash — the second would reshape the route entirely.
        setup({ cardName: 'Sales Bot/v2', tenantId: 'acme corp' });
        expect(urls().some((u) => u?.includes('acme%20corp/Sales%20Bot%2Fv2'))).toBe(
            true
        );
        expect(urls().some((u) => u?.includes('Sales Bot/v2'))).toBe(false);
    });

    it('does not double the slash when the base URL has a trailing one', () => {
        setup({ gatewayBaseUrl: 'https://gw.example.com/' });
        expect(urls().every((u) => !u?.includes('.com//'))).toBe(true);
    });

    it('marks discovery public and invocation authenticated', () => {
        setup();
        expect(screen.getByText('Discovery (public)')).toBeInTheDocument();
        expect(screen.getByText('Invocation (authenticated)')).toBeInTheDocument();
        expect(screen.getByText(/No auth required/i)).toBeInTheDocument();
        expect(screen.getByText('X-API-Key')).toBeInTheDocument();
    });

    it('tags each endpoint with the method a client must use', () => {
        setup();
        // message:send and message:stream are POSTs; the rest are GETs.
        expect(screen.getAllByText('POST')).toHaveLength(2);
        expect(screen.getAllByText('GET')).toHaveLength(4);
    });

    it('names the card and tenant it is describing', () => {
        setup();
        expect(screen.getByText('revenue-sentinel')).toBeInTheDocument();
        expect(screen.getByText('default')).toBeInTheDocument();
    });
});

describe('AgentCardEndpointModal — interaction', () => {
    it('copies the URL of the block whose button was pressed', async () => {
        const writeText = vi.fn().mockResolvedValue(undefined);
        Object.defineProperty(navigator, 'clipboard', {
            configurable: true,
            value: { writeText },
        });

        setup();
        fireEvent.click(screen.getAllByRole('button', { name: /Copy/ })[0]);

        await waitFor(() =>
            expect(writeText).toHaveBeenCalledWith(
                'https://gw.example.com/.well-known/agent-card/default/revenue-sentinel.json'
            )
        );
        // The operator needs confirmation that the click landed.
        expect(await screen.findByText('Copied')).toBeInTheDocument();
    });

    it('survives a browser with no clipboard API', async () => {
        Object.defineProperty(navigator, 'clipboard', {
            configurable: true,
            value: { writeText: vi.fn().mockRejectedValue(new Error('denied')) },
        });

        setup();
        fireEvent.click(screen.getAllByRole('button', { name: /Copy/ })[0]);

        // No "Copied" claim, and nothing throws.
        await waitFor(() => expect(screen.queryByText('Copied')).not.toBeInTheDocument());
        expect(screen.getByText('A2A Proxy Endpoints')).toBeInTheDocument();
    });

    it('closes from the header X, the footer button, and the backdrop', () => {
        const { onClose } = setup();

        fireEvent.click(screen.getByRole('button', { name: 'Close' }));
        expect(onClose).toHaveBeenCalledTimes(1);

        const backdrop = document.querySelector('.fixed.inset-0');
        fireEvent.click(backdrop as Element);
        expect(onClose).toHaveBeenCalledTimes(2);
    });

    it('does not close when the operator clicks inside the panel', () => {
        // The backdrop handler is on the same element tree; without a
        // stopPropagation the modal would vanish on any click in it.
        const { onClose } = setup();
        fireEvent.click(screen.getByText('A2A Proxy Endpoints'));
        expect(onClose).not.toHaveBeenCalled();
    });
});
