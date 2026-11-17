/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * EndpointModal hands the operator the one URL an MCP client needs. The two
 * things that can go wrong are the URL itself (wrong path, or an id with a
 * slash/space pasted straight in, which produces a URL that resolves to a
 * different server) and the backdrop-click dismissal swallowing clicks that
 * landed inside the dialog. Both are pinned below.
 */

import React from 'react';
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';

import EndpointModal from '../EndpointModal';

const renderModal = (
    overrides: Partial<React.ComponentProps<typeof EndpointModal>> = {}
) =>
    render(
        <EndpointModal
            serverId="github-mcp"
            serverName="GitHub MCP"
            gatewayBaseUrl="http://localhost:8100"
            onClose={vi.fn()}
            {...overrides}
        />
    );

describe('EndpointModal', () => {
    afterEach(() => cleanup());

    it('shows the proxy URL for the server', () => {
        renderModal();
        expect(screen.getByText('http://localhost:8100/v1/proxy/mcp/github-mcp')).toBeTruthy();
        expect(screen.getByText('GitHub MCP')).toBeTruthy();
    });

    it('percent-encodes a server id that would otherwise change the path', () => {
        renderModal({ serverId: 'team/github mcp' });
        expect(
            screen.getByText('http://localhost:8100/v1/proxy/mcp/team%2Fgithub%20mcp')
        ).toBeTruthy();
    });

    it('copies the proxy URL and confirms it to the operator', async () => {
        const writeText = vi.fn().mockResolvedValue(undefined);
        vi.stubGlobal('navigator', { clipboard: { writeText } });

        renderModal();
        fireEvent.click(screen.getByRole('button', { name: /copy/i }));

        expect(writeText).toHaveBeenCalledWith('http://localhost:8100/v1/proxy/mcp/github-mcp');
        expect(await screen.findByText('Copied!')).toBeTruthy();

        vi.unstubAllGlobals();
    });

    it('does not claim a copy happened when the clipboard is unavailable', async () => {
        const writeText = vi.fn().mockRejectedValue(new Error('denied'));
        const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
        vi.stubGlobal('navigator', { clipboard: { writeText } });

        renderModal();
        fireEvent.click(screen.getByRole('button', { name: /copy/i }));
        await vi.waitFor(() => expect(errorSpy).toHaveBeenCalled());
        expect(screen.queryByText('Copied!')).toBeNull();

        errorSpy.mockRestore();
        vi.unstubAllGlobals();
    });

    it('dismisses on a backdrop click but not on a click inside the dialog', () => {
        const onClose = vi.fn();
        const { container } = renderModal({ onClose });

        fireEvent.click(screen.getByText('MCP Proxy Endpoint'));
        expect(onClose).not.toHaveBeenCalled();

        fireEvent.click(container.firstChild as Element);
        expect(onClose).toHaveBeenCalledTimes(1);
    });

    it('closes from the footer button', () => {
        const onClose = vi.fn();
        renderModal({ onClose });
        fireEvent.click(screen.getByRole('button', { name: /^close$/i }));
        expect(onClose).toHaveBeenCalledTimes(1);
    });
});
