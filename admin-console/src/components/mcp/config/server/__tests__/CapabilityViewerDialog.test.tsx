/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * CapabilityViewerDialog — the operator-facing view of what a discovered
 * MCP server can actually do.
 *
 * The load-bearing behaviour here is the `tools/call` curl the dialog hands
 * the operator: it must target the *proxy* runtime base (not the control
 * plane), name the right server id, and build example arguments that a real
 * server would accept. A `minimum: 1` param filled with `0` produces a
 * copy-pasted call that fails validation, which is exactly the kind of thing
 * an operator blames on the gateway. These tests pin the URL, the argument
 * synthesis, the per-tab empty states, and the accordion.
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, within } from '@testing-library/react';

import CapabilityViewerDialog from '../CapabilityViewerDialog';
import { env } from '../../../../../env';

type Props = React.ComponentProps<typeof CapabilityViewerDialog>;

const capabilities: Props['capabilities'] = {
    tools: [
        {
            name: 'search_issues',
            description: 'Search the issue tracker',
            inputSchema: {
                type: 'object',
                properties: {
                    query: { type: 'string', description: 'Full-text query' },
                    limit: { type: 'integer', minimum: 5 },
                    offset: { type: 'integer' },
                    include_closed: { type: 'boolean' },
                    labels: { type: 'array', items: { type: 'string' } },
                    state: { type: 'string', enum: ['open', 'closed'] },
                    mode: { type: 'string', default: 'fuzzy' },
                },
                required: ['query'],
            },
        },
        { name: 'ping' },
    ],
    resources: [{ name: 'file://readme', description: 'Project readme' }],
    prompts: [],
    resource_templates: [],
};

const renderDialog = (overrides: Partial<Props> = {}) =>
    render(
        <CapabilityViewerDialog
            isOpen
            onClose={vi.fn()}
            serverName="GitHub MCP"
            serverId="github-mcp"
            capabilities={capabilities}
            {...overrides}
        />
    );

describe('CapabilityViewerDialog', () => {
    afterEach(() => cleanup());

    it('renders nothing while closed', () => {
        const { container } = renderDialog({ isOpen: false });
        expect(container).toBeEmptyDOMElement();
    });

    it('shows the server name and a per-tab capability count', () => {
        renderDialog();
        expect(screen.getByText('GitHub MCP')).toBeTruthy();
        const toolsTab = screen.getByRole('button', { name: /tools/i });
        expect(within(toolsTab).getByText('2')).toBeTruthy();
        const promptsTab = screen.getByRole('button', { name: /prompts/i });
        expect(within(promptsTab).getByText('0')).toBeTruthy();
    });

    it('lists tools collapsed with their argument count', () => {
        renderDialog();
        expect(screen.getByText('search_issues')).toBeTruthy();
        expect(screen.getByText('7 args')).toBeTruthy();
        // `ping` has no schema at all — singular/plural must still be right.
        expect(screen.getByText('0 args')).toBeTruthy();
        // Collapsed: the curl block is not rendered yet.
        expect(screen.queryByText(/Call this tool via the API/)).toBeNull();
    });

    it('expands one tool at a time and collapses on a second click', () => {
        renderDialog();
        fireEvent.click(screen.getByText('search_issues'));
        expect(screen.getByText(/Call this tool via the API/)).toBeTruthy();

        fireEvent.click(screen.getByText('ping'));
        // Only the newly opened tool is expanded.
        expect(screen.getByText('This tool takes no arguments.')).toBeTruthy();

        fireEvent.click(screen.getByText('ping'));
        expect(screen.queryByText(/Call this tool via the API/)).toBeNull();
    });

    it('builds a tools/call curl against the proxy runtime, not the control plane', () => {
        renderDialog();
        fireEvent.click(screen.getByText('search_issues'));

        const curl = screen.getByText(/curl -X POST/).textContent ?? '';
        expect(curl).toContain(`${env.BRUTOR_GATEWAY_PUBLIC_URL}/v1/proxy/mcp/github-mcp`);
        expect(curl).not.toContain(env.BRUTOR_CONTROL_PLANE_PUBLIC_URL);
        expect(curl).toContain('Authorization: Bearer YOUR_API_KEY');

        const body = JSON.parse(curl.slice(curl.indexOf("-d '") + 4, curl.lastIndexOf("'"))) as {
            method: string;
            params: { name: string; arguments: Record<string, unknown> };
        };
        expect(body.method).toBe('tools/call');
        expect(body.params.name).toBe('search_issues');
        expect(body.params.arguments).toEqual({
            query: '<query>',
            // A declared lower bound wins over the generic `1` — a `0` here
            // would make the pasted example fail server-side validation.
            limit: 5,
            offset: 1,
            include_closed: false,
            labels: [],
            state: 'open',
            mode: 'fuzzy',
        });
    });

    it('marks required arguments and shows array element types', () => {
        renderDialog();
        fireEvent.click(screen.getByText('search_issues'));
        expect(screen.getByText('required')).toBeTruthy();
        expect(screen.getByText('array<string>')).toBeTruthy();
        expect(screen.getByText('Full-text query')).toBeTruthy();
    });

    it('copies the curl to the clipboard and flips the button label back', () => {
        vi.useFakeTimers();
        const writeText = vi.fn().mockResolvedValue(undefined);
        vi.stubGlobal('navigator', { clipboard: { writeText } });

        renderDialog();
        fireEvent.click(screen.getByText('search_issues'));
        fireEvent.click(screen.getByRole('button', { name: /^copy$/i }));

        expect(writeText).toHaveBeenCalledTimes(1);
        expect(String(writeText.mock.calls[0][0])).toContain('/v1/proxy/mcp/github-mcp');

        vi.useRealTimers();
        vi.unstubAllGlobals();
    });

    it('switches tabs and reports an empty tab honestly', () => {
        renderDialog();
        fireEvent.click(screen.getByRole('button', { name: /prompts/i }));
        expect(screen.getByText('No prompts discovered.')).toBeTruthy();

        fireEvent.click(screen.getByRole('button', { name: /resources/i }));
        expect(screen.getByText('file://readme')).toBeTruthy();
        expect(screen.getByText('Project readme')).toBeTruthy();
        // Non-tool tabs are a flat list — no curl builder.
        expect(screen.queryByText(/Call this tool via the API/)).toBeNull();
    });

    it('treats a null capabilities payload as "nothing discovered"', () => {
        renderDialog({ capabilities: null });
        expect(screen.getByText('No tools discovered.')).toBeTruthy();
    });

    it('closes from the footer button', () => {
        const onClose = vi.fn();
        renderDialog({ onClose });
        fireEvent.click(screen.getByRole('button', { name: /^close$/i }));
        expect(onClose).toHaveBeenCalledTimes(1);
    });
});

describe('CapabilityViewerDialog — tab isolation', () => {
    beforeEach(() => cleanup());

    it('resets the expanded tool when the tab changes', () => {
        renderDialog();
        fireEvent.click(screen.getByText('search_issues'));
        expect(screen.getByText(/Call this tool via the API/)).toBeTruthy();

        fireEvent.click(screen.getByRole('button', { name: /resources/i }));
        fireEvent.click(screen.getByRole('button', { name: /tools/i }));
        expect(screen.queryByText(/Call this tool via the API/)).toBeNull();
    });
});

describe('CapabilityViewerDialog — MCP annotations and the other spec fields', () => {
    afterEach(() => cleanup());

    // Shapes as gateway-control stores them (canonical tool normaliser).
    const annotated: Props['capabilities'] = {
        tools: [
            {
                name: 'get_applicant',
                title: 'Get applicant',
                description: "Read one loan applicant's file.",
                inputSchema: { type: 'object', properties: { applicant_id: { type: 'string' } } },
                outputSchema: { type: 'object', properties: { income: { type: 'number' } } },
                annotations: { title: 'Get applicant', readOnlyHint: true, openWorldHint: false },
                _meta: { 'brutor.demo/category': 'lookup' },
            },
            {
                name: 'purge_applicant',
                annotations: { destructiveHint: true, idempotentHint: true, openWorldHint: true },
            },
            { name: 'legacy_tool', inputSchema: { type: 'object' } },
            // Annotations object present but no behaviour hint: still unassessed.
            { name: 'titled_only', annotations: { title: 'Only a title' } },
        ],
    };

    const toolRow = (name: string) => screen.getByText(name).closest('button') as HTMLElement;

    it('renders readable badges for the declared behaviour hints', () => {
        renderDialog({ capabilities: annotated });
        const ro = toolRow('get_applicant');
        expect(within(ro).getByText('Read-only')).toBeTruthy();
        expect(within(ro).getByText('Closed world')).toBeTruthy();
        // destructiveHint is meaningless on a read-only tool — not shown.
        expect(within(ro).queryByText('Destructive')).toBeNull();
        expect(within(ro).getByText('Get applicant')).toBeTruthy();

        const purge = toolRow('purge_applicant');
        expect(within(purge).getByText('Destructive')).toBeTruthy();
        expect(within(purge).getByText('Idempotent')).toBeTruthy();
        expect(within(purge).getByText('Open world')).toBeTruthy();
        expect(within(purge).queryByText('Read-only')).toBeNull();
    });

    it('says out loud that an unannotated tool is treated as side-effecting', () => {
        renderDialog({ capabilities: annotated });
        for (const name of ['legacy_tool', 'titled_only']) {
            expect(
                within(toolRow(name)).getByText('No annotations declared — treated as side-effecting')
            ).toBeTruthy();
        }
        expect(
            within(toolRow('get_applicant')).queryByText(/No annotations declared/)
        ).toBeNull();

        fireEvent.click(screen.getByText('legacy_tool'));
        expect(screen.getByText(/Runtime classification: side-effecting/)).toBeTruthy();
    });

    it('shows the raw hints, the runtime classification, outputSchema and _meta when expanded', () => {
        renderDialog({ capabilities: annotated });
        fireEvent.click(screen.getByText('get_applicant'));

        expect(screen.getByText('Behaviour hints (MCP annotations)')).toBeTruthy();
        expect(screen.getByText('readOnlyHint: true')).toBeTruthy();
        expect(screen.getByText('openWorldHint: false')).toBeTruthy();
        expect(screen.getByText(/Runtime classification: read-only/)).toBeTruthy();

        const output = screen.getByText('Output schema').closest('details') as HTMLElement;
        expect(output.textContent).toContain('"income"');
        const meta = screen.getByText('Metadata (_meta)').closest('details') as HTMLElement;
        expect(meta.textContent).toContain('brutor.demo/category');
    });

    it('omits the outputSchema / _meta sections when a tool declares neither', () => {
        renderDialog({ capabilities: annotated });
        fireEvent.click(screen.getByText('purge_applicant'));
        expect(screen.queryByText('Output schema')).toBeNull();
        expect(screen.queryByText('Metadata (_meta)')).toBeNull();
    });
});
