/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The "how do I point a client at this gateway?" modal. It is documentation
 * that has to stay correct: the SDK and cURL snippets embed the proxy URL, and
 * an operator will paste them verbatim. If the snippet interpolated a stale or
 * empty URL, the copy/paste silently targets the wrong host.
 *
 * It also has to keep saying that API keys live on Resource Groups — that is
 * the one piece of routing knowledge an operator cannot guess.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

import LLMProxyEndpointModal from '../LLMProxyEndpointModal';

const URL = 'https://gw.example.com/v1/proxy';

function setup(open = true) {
    const onClose = vi.fn();
    const view = render(
        <LLMProxyEndpointModal isOpen={open} onClose={onClose} proxyUrl={URL} />
    );
    return { onClose, ...view };
}

describe('LLMProxyEndpointModal', () => {
    it('renders nothing at all when closed', () => {
        const { container } = setup(false);
        expect(container).toBeEmptyDOMElement();
    });

    it('shows the proxy URL as the base URL to configure', () => {
        setup();
        expect(screen.getByText('LLM Proxy Endpoint')).toBeInTheDocument();
        expect(screen.getAllByText(URL).length).toBeGreaterThan(0);
    });

    it('embeds the live URL in the Python snippet rather than a placeholder', () => {
        setup();
        expect(screen.getByText(`base_url="${URL}",`)).toBeInTheDocument();
    });

    it('points the cURL snippet at the chat-completions path', () => {
        setup();
        expect(
            screen.getByText(new RegExp(`curl ${URL}/chat/completions`))
        ).toBeInTheDocument();
    });

    it('says where API keys actually come from', () => {
        // Keys are Resource-Group scoped; nothing on this screen creates one,
        // so the modal has to send the operator to the right place.
        setup();
        expect(screen.getByText('Need API Keys?')).toBeInTheDocument();
        expect(screen.getByText(/Resource Group level/)).toBeInTheDocument();
    });

    it('copies the URL and confirms it', async () => {
        const writeText = vi.fn().mockResolvedValue(undefined);
        Object.defineProperty(navigator, 'clipboard', {
            configurable: true,
            value: { writeText },
        });

        setup();
        fireEvent.click(screen.getByTitle('Copy to clipboard'));

        await waitFor(() => expect(writeText).toHaveBeenCalledWith(URL));
        expect(await screen.findByText('Copied!')).toBeInTheDocument();
    });

    it('makes no "Copied!" claim when the clipboard is unavailable', async () => {
        vi.spyOn(console, 'error').mockImplementation(() => {});
        Object.defineProperty(navigator, 'clipboard', {
            configurable: true,
            value: { writeText: vi.fn().mockRejectedValue(new Error('denied')) },
        });

        setup();
        fireEvent.click(screen.getByTitle('Copy to clipboard'));

        await waitFor(() =>
            expect(screen.queryByText('Copied!')).not.toBeInTheDocument()
        );
        expect(screen.getByText('Copy')).toBeInTheDocument();
        vi.restoreAllMocks();
    });

    it('closes from the footer button', () => {
        const { onClose } = setup();
        fireEvent.click(screen.getByRole('button', { name: 'Close' }));
        expect(onClose).toHaveBeenCalledTimes(1);
    });
});
