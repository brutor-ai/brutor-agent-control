/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * CapabilityViewerDialog — Tools tab.
 *
 * Operators open this dialog from MCP Servers → Configuration to
 * understand a discovered tool. It must surface:
 *   (a) the full, un-truncated description,
 *   (b) the argument schema (name / type / required), and
 *   (c) a ready-to-run `tools/call` curl,
 * so an operator can exercise the tool without reverse-engineering
 * the JSON-RPC envelope. A 2-line description without arguments is not
 * enough for that — these tests pin the full surface.
 */

import { type ComponentProps } from 'react';
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

import CapabilityViewerDialog from '../components/mcp/config/server/CapabilityViewerDialog';

type Caps = ComponentProps<typeof CapabilityViewerDialog>['capabilities'];

const TOOL = {
    name: 'aws___search_documentation',
    description: 'Search AWS documentation.\n\n## Usage\nProvide a search phrase.',
    inputSchema: {
        type: 'object',
        properties: {
            search_phrase: { type: 'string', description: 'Search phrase to use' },
            limit: { type: 'integer', description: 'Max results' },
        },
        required: ['search_phrase'],
    },
};

function renderDialog(capabilities: Caps) {
    return render(
        <CapabilityViewerDialog
            isOpen
            onClose={vi.fn()}
            serverName="AWS Docs"
            serverId="mcp-test-123"
            capabilities={capabilities}
        />,
    );
}

describe('CapabilityViewerDialog — Tools tab', () => {
    it('expands a tool to show the full description and argument schema', () => {
        renderDialog({ tools: [TOOL] });
        expect(screen.getByText('aws___search_documentation')).toBeTruthy();

        fireEvent.click(screen.getByText('aws___search_documentation'));

        // Full description — "## Usage" only exists past the first two
        // lines, so a clamped description would not show it.
        expect(screen.getByText(/## Usage/)).toBeTruthy();

        // Argument rows with required badge.
        expect(screen.getByText('search_phrase')).toBeTruthy();
        expect(screen.getByText('limit')).toBeTruthy();
        expect(screen.getByText('required')).toBeTruthy();
    });

    it('renders a tools/call curl with the proxy URL, server id and tool name', () => {
        const { container } = renderDialog({ tools: [TOOL] });
        fireEvent.click(screen.getByText('aws___search_documentation'));

        const pre = container.querySelector('pre');
        expect(pre).not.toBeNull();
        const curl = pre!.textContent || '';
        expect(curl).toContain('/v1/proxy/mcp/mcp-test-123');
        expect(curl).toContain('"method":"tools/call"');
        expect(curl).toContain('"name":"aws___search_documentation"');
        expect(curl).toContain('"arguments"');
        expect(curl).toContain('search_phrase');
        expect(curl).toContain('Authorization: Bearer YOUR_API_KEY');
    });

    it('uses safe numeric placeholders — declared default/minimum, else 1 (never 0)', () => {
        const { container } = renderDialog({
            tools: [{
                name: 'paged_search',
                inputSchema: {
                    type: 'object',
                    properties: {
                        limit: { type: 'integer' },
                        offset: { type: 'integer', minimum: 5 },
                        page_size: { type: 'integer', default: 25 },
                    },
                },
            }],
        });
        fireEvent.click(screen.getByText('paged_search'));
        const curl = container.querySelector('pre')!.textContent || '';
        // No constraint → 1, never 0 — 0 fails the very common
        // `minimum: 1` rule on count/limit-style params.
        expect(curl).toContain('"limit":1');
        expect(curl).not.toContain('"limit":0');
        // Declared minimum is honoured.
        expect(curl).toContain('"offset":5');
        // Declared default wins outright.
        expect(curl).toContain('"page_size":25');
    });

    it('handles a tool with no arguments', () => {
        renderDialog({ tools: [{ name: 'ping', description: 'Health check' }] });
        fireEvent.click(screen.getByText('ping'));
        expect(screen.getByText(/takes no arguments/)).toBeTruthy();
    });

    it('shows an empty state when the server has no tools', () => {
        renderDialog({ tools: [] });
        expect(screen.getByText(/No tools discovered/)).toBeTruthy();
    });
});
