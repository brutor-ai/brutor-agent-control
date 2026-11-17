/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * ProxyUrlGenerator derives three transport URLs from one base. The
 * derivations are where bugs hide: the WebSocket variant is produced by a
 * string rewrite of the scheme (an https base must become wss, not ws), the
 * SSE variant appends a suffix, and the server id is interpolated into a
 * path. These tests pin all three plus the reveal/copy interactions.
 */

import React from 'react';
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';

import ProxyUrlGenerator from '../ProxyUrlGenerator';

const BASE = 'http://localhost:5050';
const PATH = '/v1/admin/mcp/transport/github-mcp';

const renderGenerator = (
    overrides: Partial<React.ComponentProps<typeof ProxyUrlGenerator>> = {}
) =>
    render(
        <ProxyUrlGenerator
            serverId="github-mcp"
            serverName="GitHub MCP"
            gatewayBaseUrl={BASE}
            {...overrides}
        />
    );

describe('ProxyUrlGenerator', () => {
    afterEach(() => cleanup());

    it('keeps the URLs hidden until the operator asks for them', () => {
        renderGenerator();
        expect(screen.getByText(/Show Proxy URLs/)).toBeTruthy();
        expect(screen.queryByText(BASE + PATH)).toBeNull();
        expect(screen.getByText(/"GitHub MCP"/)).toBeTruthy();
    });

    it('reveals one URL per transport, and hides them again', () => {
        renderGenerator();
        fireEvent.click(screen.getByText(/Show Proxy URLs/));

        expect(screen.getByText(BASE + PATH)).toBeTruthy();
        expect(screen.getByText(`ws://localhost:5050${PATH}`)).toBeTruthy();
        expect(screen.getByText(`${BASE + PATH}/sse`)).toBeTruthy();

        fireEvent.click(screen.getByText(/Hide Proxy URLs/));
        expect(screen.queryByText(BASE + PATH)).toBeNull();
    });

    it('upgrades the WebSocket scheme along with the base — https gives wss', () => {
        renderGenerator({ gatewayBaseUrl: 'https://gateway.example.com' });
        fireEvent.click(screen.getByText(/Show Proxy URLs/));
        expect(screen.getByText(`wss://gateway.example.com${PATH}`)).toBeTruthy();
    });

    it('percent-encodes the server id in every transport URL', () => {
        renderGenerator({ serverId: 'team/github mcp' });
        fireEvent.click(screen.getByText(/Show Proxy URLs/));
        const encoded = '/v1/admin/mcp/transport/team%2Fgithub%20mcp';
        expect(screen.getByText(BASE + encoded)).toBeTruthy();
        expect(screen.getByText(`ws://localhost:5050${encoded}`)).toBeTruthy();
    });

    it('copies each transport URL independently', async () => {
        const writeText = vi.fn().mockResolvedValue(undefined);
        vi.stubGlobal('navigator', { clipboard: { writeText } });

        renderGenerator();
        fireEvent.click(screen.getByText(/Show Proxy URLs/));
        const [httpCopy, wsCopy] = screen.getAllByRole('button', { name: /copy/i });

        fireEvent.click(httpCopy);
        expect(writeText).toHaveBeenLastCalledWith(BASE + PATH);
        // Only the copied transport flips its label.
        expect(await screen.findByText('Copied!')).toBeTruthy();
        expect(screen.getAllByText('Copy')).toHaveLength(2);

        fireEvent.click(wsCopy);
        expect(writeText).toHaveBeenLastCalledWith(`ws://localhost:5050${PATH}`);

        vi.unstubAllGlobals();
    });

    it('opens the endpoint info page in a new tab', () => {
        const open = vi.fn();
        vi.stubGlobal('open', open);

        renderGenerator();
        fireEvent.click(screen.getByRole('button', { name: /endpoint info/i }));
        expect(open).toHaveBeenCalledWith(`${BASE + PATH}/info`, '_blank');

        vi.unstubAllGlobals();
    });
});
