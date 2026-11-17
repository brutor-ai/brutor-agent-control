/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * ServerConfigForm Backend-type picker
 *
 * The form accepts EITHER a real MCP server OR a Virtual MCP
 * server (VMCP) as its backend. The selector at the top of the
 * form discriminates the two paths and rewrites the rest of the
 * form accordingly.
 *
 * Covered:
 *
 *   1. Default state: Backend type defaults to MCP, MCP picker
 *      renders, VMCP picker is not in the DOM.
 *   2. Switching to VMCP swaps in the VMCP <select>, hides the
 *      MCP picker, and surfaces the sky-blue "VMCP-backed
 *      configuration" info banner once a VMCP is chosen.
 *   3. VMCP info banner shows the `/v1/proxy/vmcp/{id}` route
 *      using the picked VMCP id — this is the contract the
 *      portal reads.
 *   4. Submit on the VMCP branch sends `virtual_server_id` (and
 *      explicitly NOT `mcp_server_id` or `oauth_client_id`,
 *      because VMCPs delegate per-call auth).
 *   5. Edit-mode preload: opening a VMCP-backed config locks the
 *      Backend dropdown (disabled) and pre-selects the VMCP.
 *   6. When the tenant has zero VMCPs, the VMCP option is
 *      disabled in the dropdown so the operator can't pick
 *      something that doesn't exist.
 *
 * Selector strategy: the form's <label> tags don't carry `htmlFor`
 * attributes, so `getByLabelText` doesn't bind them to their inputs.
 * Instead we walk from the label text to its sibling <select>/<input>
 * via the parent <div>. Less idiomatic but matches the existing
 * markup.
 */

import { describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import ServerConfigForm from '../components/mcp/config/server-access/server_configs/ServerConfigForm';


vi.mock('../hooks/useGateway', () => ({
    useGateway: () => ({
        getServerIconUrl: (id: string) => `https://icons.example/${id}.png`,
    }),
}));


const STABLE_SERVERS = [
    {
        id: 'mcp-customer',
        name: 'Customer CRM',
        auth_type: 'none',
        enabled: true,
    },
];

const STABLE_VMCPS = [
    {
        id: 'vs-aggregator',
        name: 'Customer Aggregator',
        enabled: true,
        capabilities: {
            tools: [{ name: 't1' }, { name: 't2' }, { name: 't3' }],
        },
    },
    {
        id: 'vs-trim',
        name: 'Trimmed Surface',
        enabled: true,
        capabilities: { tools: [{ name: 'one' }] },
    },
];


type OnSave = React.ComponentProps<typeof ServerConfigForm>['onSave'];

const renderForm = (overrides?: {
    config?: any;
    virtualServers?: any[];
    onSave?: OnSave;
}) => {
    const onSave = overrides?.onSave ?? vi.fn<OnSave>(async () => undefined);
    const onCancel = vi.fn();
    render(
        <ServerConfigForm
            config={overrides?.config ?? null}
            servers={STABLE_SERVERS}
            virtualServers={overrides?.virtualServers ?? STABLE_VMCPS}
            oauthClients={[]}
            groupTree={[]}
            onSave={onSave}
            onCancel={onCancel}
        />
    );
    return { onSave, onCancel };
};


/** Find the <select> or <input> that sits next to a <label> text.
 * Walks `label text → parent div → first matching descendant`. */
function controlForLabel<K extends 'select' | 'input'>(
    labelText: RegExp | string, tag: K,
): HTMLElementTagNameMap[K] {
    const label = screen.getByText(labelText);
    const wrapper = label.parentElement!;
    const ctrl = wrapper.querySelector<HTMLElementTagNameMap[K]>(tag);
    if (!ctrl) throw new Error(`No <${tag}> found for label: ${labelText}`);
    return ctrl;
}


describe('ServerConfigForm — backend picker', () => {

    it('defaults to the MCP backend with the MCP picker visible', () => {
        renderForm();

        const backendSelect = controlForLabel(
            /^Backend type \*$/, 'select',
        );
        expect(backendSelect.value).toBe('mcp');
        // MCP picker label is present.
        expect(screen.getByText(/^MCP Server \*$/)).toBeTruthy();
        // VMCP picker label is NOT in the DOM yet.
        expect(screen.queryByText(/^Virtual MCP Server \*$/)).toBeNull();
    });

    it('switching to VMCP swaps in the VMCP picker and hides the MCP picker', () => {
        renderForm();

        const backendSelect = controlForLabel(
            /^Backend type \*$/, 'select',
        );
        fireEvent.change(backendSelect, { target: { value: 'vmcp' } });

        expect(screen.getByText(/^Virtual MCP Server \*$/)).toBeTruthy();
        expect(screen.queryByText(/^MCP Server \*$/)).toBeNull();
    });

    it('shows the sky-blue VMCP info banner once a VMCP is picked, with the correct proxy route', () => {
        renderForm();
        fireEvent.change(
            controlForLabel(/^Backend type \*$/, 'select'),
            { target: { value: 'vmcp' } },
        );

        const vmcpSelect = controlForLabel(
            /^Virtual MCP Server \*$/, 'select',
        );
        fireEvent.change(vmcpSelect, { target: { value: 'vs-aggregator' } });

        expect(screen.getByText(/VMCP-backed configuration/i)).toBeTruthy();
        // Route encoded with the picked VMCP id.
        expect(
            screen.getByText((content) =>
                content.includes('/v1/proxy/vmcp/vs-aggregator')
            )
        ).toBeTruthy();
    });

    it('submit on the VMCP branch sends virtual_server_id and omits mcp_server_id / oauth_client_id', async () => {
        const onSave = vi.fn<OnSave>(async () => undefined);
        renderForm({ onSave });

        // Backend → VMCP
        fireEvent.change(
            controlForLabel(/^Backend type \*$/, 'select'),
            { target: { value: 'vmcp' } },
        );
        // Pick the VMCP.
        fireEvent.change(
            controlForLabel(/^Virtual MCP Server \*$/, 'select'),
            { target: { value: 'vs-aggregator' } },
        );

        // Required name field — anchor via placeholder since the
        // label isn't bound to the input.
        fireEvent.change(
            screen.getByPlaceholderText(/GitHub for Web App/i),
            { target: { value: 'My VMCP Access' } },
        );

        fireEvent.click(screen.getByRole('button', { name: /create/i }));

        await waitFor(() => expect(onSave).toHaveBeenCalled());
        const payload = onSave.mock.calls[0][0];
        expect(payload.virtual_server_id).toBe('vs-aggregator');
        expect(payload.mcp_server_id).toBeUndefined();
        expect(payload.oauth_client_id).toBeUndefined();
    });

    it('locks the Backend dropdown when editing an existing VMCP-backed config', () => {
        renderForm({
            config: {
                id: 'cfg-1',
                tenant_id: 't1',
                name: 'Aggregator Access',
                mcp_server_id: null,
                virtual_server_id: 'vs-aggregator',
                server_kind: 'vmcp',
                priority: 100,
                is_default: false,
                enabled: true,
                groups: [],
                group_ids: [],
                created_at: new Date().toISOString(),
                updated_at: new Date().toISOString(),
                usage_count: 0,
            },
        });

        const backendSelect = controlForLabel(
            /^Backend type \*$/, 'select',
        );
        expect(backendSelect.value).toBe('vmcp');
        expect(backendSelect.disabled).toBe(true);
        // Hint surfaces the lock.
        expect(
            screen.getByText((content) =>
                /locked once a config exists/i.test(content)
            )
        ).toBeTruthy();
    });

    it('disables the VMCP option when no VMCPs are defined in the tenant', () => {
        renderForm({ virtualServers: [] });

        const backendSelect = controlForLabel(
            /^Backend type \*$/, 'select',
        );
        const vmcpOption = Array.from(backendSelect.options).find(
            (o) => o.value === 'vmcp',
        )!;
        expect(vmcpOption.disabled).toBe(true);
        expect(vmcpOption.textContent).toMatch(/none defined yet/i);
    });
});
