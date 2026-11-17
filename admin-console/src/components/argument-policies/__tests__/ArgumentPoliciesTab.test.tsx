/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Argument policies list.
 *
 * Pinned: server filters reach the list call, delete confirms by name, YAML
 * import reports warnings rather than plain success, and export really produces
 * the file. Messageless failures must name what failed rather than read
 * "Unknown error": `errorMessage(e) ?? 'fallback'` can never reach its
 * fallback. The form has its own suite and is stubbed to its callback.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';

const gateway = {
    listArgumentPolicies: vi.fn(),
    deleteArgumentPolicy: vi.fn(),
    exportArgumentPoliciesYaml: vi.fn(),
    importArgumentPoliciesYaml: vi.fn(),
};
const toast = { success: vi.fn(), error: vi.fn(), warning: vi.fn() };

vi.mock('../../../hooks/useGateway', () => ({ useGateway: () => gateway }));
vi.mock('../../common/Toast', () => ({ useToast: () => toast }));
vi.mock('../../common/GroupTreeDisplay', () => ({ default: () => null }));
vi.mock('../ArgumentPolicyForm', () => ({
    default: (p: { policy: { name: string } | null; onClose: (saved: boolean) => void }) => (
        <div data-testid="form">
            <span>{p.policy ? `editing ${p.policy.name}` : 'creating'}</span>
            <button onClick={() => p.onClose(true)}>form-saved</button>
            <button onClick={() => p.onClose(false)}>form-cancel</button>
        </div>
    ),
}));

import ArgumentPoliciesTab from '../ArgumentPoliciesTab';

const SHELL_POLICY = {
    id: 'a1',
    name: 'no-writes',
    description: 'blocks SQL writes',
    target: 'db.query',
    argument_key: 'sql',
    analyzer: 'sql',
    surface: 'mcp_input',
    severity: 'deny',
    rules: [{}, {}],
    enabled: true,
};
const URL_POLICY = { id: 'a2', name: 'ssrf', target: 'http.get', argument_key: 'url', analyzer: 'url', surface: 'llm_output_tool_calls', severity: 'warn', rules: [], enabled: false };

async function open() {
    render(<ArgumentPoliciesTab />);
    await screen.findByText('no-writes');
}
const rowOf = (name: string) => screen.getByText(name).closest('tr') as HTMLElement;

describe('ArgumentPoliciesTab', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        vi.spyOn(console, 'error').mockImplementation(() => {});
        for (const fn of Object.values(gateway)) fn.mockReset();
        gateway.listArgumentPolicies.mockResolvedValue({ policies: [SHELL_POLICY, URL_POLICY] });
        gateway.deleteArgumentPolicy.mockResolvedValue(undefined);
    });

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
    });

    it('counts active policies and searches name, description, target and argument', async () => {
        await open();
        expect(screen.getByText('2 policies, 1 active')).toBeInTheDocument();
        const active = within(rowOf('no-writes'));
        expect(active.getByText('deny')).toBeInTheDocument();
        expect(active.getByText('MCP inbound tool/call')).toBeInTheDocument();
        expect(active.getByText('2')).toBeInTheDocument();
        // A disabled policy reads as inactive, not by its severity.
        expect(within(rowOf('ssrf')).getByText('inactive')).toBeInTheDocument();
        const search = screen.getByPlaceholderText('Search by name, description, target, or argument...');
        for (const term of ['BLOCKS SQL', 'db.query', 'sql']) {
            fireEvent.change(search, { target: { value: term } });
            expect(screen.getByText('no-writes')).toBeInTheDocument();
            expect(screen.queryByText('ssrf')).toBeNull();
        }
        fireEvent.change(search, { target: { value: 'zzz' } });
        expect(screen.queryByText('no-writes')).toBeNull();
    });

    it('sends the surface and analyzer filters to the server', async () => {
        await open();
        expect(gateway.listArgumentPolicies).toHaveBeenCalledWith({ surface: undefined, analyzer: undefined });
        const [surface, analyzer] = screen.getAllByRole('combobox');
        const surfaceValue = within(surface).getAllByRole('option')[1].getAttribute('value') ?? '';
        const analyzerValue = within(analyzer).getAllByRole('option')[1].getAttribute('value') ?? '';

        fireEvent.change(surface, { target: { value: surfaceValue } });
        await waitFor(() =>
            expect(gateway.listArgumentPolicies).toHaveBeenLastCalledWith({ surface: surfaceValue, analyzer: undefined }),
        );
        fireEvent.change(analyzer, { target: { value: analyzerValue } });
        await waitFor(() =>
            expect(gateway.listArgumentPolicies).toHaveBeenLastCalledWith({ surface: surfaceValue, analyzer: analyzerValue }),
        );
    });

    it('a failed first load names what failed and Retry recovers', async () => {
        gateway.listArgumentPolicies.mockRejectedValueOnce({});
        render(<ArgumentPoliciesTab />);
        expect(await screen.findByText('Failed to Load Policies')).toBeInTheDocument();
        expect(screen.getByText('Failed to load argument policies')).toBeInTheDocument();
        expect(toast.error).toHaveBeenCalledWith('Failed to Load', 'Unable to load argument policies');
        fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
        expect(await screen.findByText('no-writes')).toBeInTheDocument();
    });

    it('deletes after typed confirmation and drops the row', async () => {
        await open();
        fireEvent.click(within(rowOf('no-writes')).getByTitle(/Delete/));
        const input = await screen.findByPlaceholderText('Enter "no-writes"');
        fireEvent.change(input, { target: { value: 'no-writes' } });
        fireEvent.keyDown(input, { key: 'Enter' });
        await waitFor(() => expect(gateway.deleteArgumentPolicy).toHaveBeenCalledWith('a1'));
        await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Policy Deleted', '"no-writes" has been deleted.'));
        expect(screen.queryByText('no-writes')).toBeNull();
        expect(screen.getByText('ssrf')).toBeInTheDocument();
    });

    it('a refused delete is reported and keeps the row', async () => {
        gateway.deleteArgumentPolicy.mockRejectedValue({});
        await open();
        fireEvent.click(within(rowOf('no-writes')).getByTitle(/Delete/));
        const input = await screen.findByPlaceholderText('Enter "no-writes"');
        fireEvent.change(input, { target: { value: 'no-writes' } });
        fireEvent.keyDown(input, { key: 'Enter' });
        await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Delete Failed', 'Failed to delete policy'));
        expect(screen.getAllByText('Failed to delete policy').length).toBeGreaterThan(0);
    });

    it('exports a dated YAML file', async () => {
        const createUrl = vi.fn(() => 'blob:x');
        const revokeUrl = vi.fn();
        vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: createUrl, revokeObjectURL: revokeUrl }));
        const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
        gateway.exportArgumentPoliciesYaml.mockResolvedValue('policies: []');
        await open();
        fireEvent.click(screen.getByRole('button', { name: 'Export YAML' }));
        await waitFor(() =>
            expect(toast.success).toHaveBeenCalledWith('Export Complete', 'Argument policies downloaded as YAML.'),
        );
        expect(click).toHaveBeenCalledTimes(1);
        const blob = (createUrl.mock.calls as unknown as [Blob][])[0][0];
        expect(blob.type).toBe('application/x-yaml');
        expect(await blob.text()).toBe('policies: []');
        expect(revokeUrl).toHaveBeenCalledWith('blob:x');
        vi.unstubAllGlobals();
    });

    it('a failed export is reported', async () => {
        gateway.exportArgumentPoliciesYaml.mockRejectedValue({});
        await open();
        fireEvent.click(screen.getByRole('button', { name: 'Export YAML' }));
        await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Export Failed', 'Unable to export policies'));
    });

    describe('YAML import', () => {
        const fileInput = () => document.querySelector('input[type="file"]') as HTMLInputElement;
        const pickYaml = () =>
            fireEvent.change(fileInput(), {
                target: { files: [new File(['policies: [x]'], 'p.yaml', { type: 'application/x-yaml' })] },
            });

        it('sends the file text, summarises, and reloads', async () => {
            gateway.importArgumentPoliciesYaml.mockResolvedValue({
                created: ['a'],
                updated: ['b', 'c'],
                unchanged: [],
                errors: [],
                unresolved_groups: [],
            });
            await open();
            pickYaml();
            await waitFor(() => expect(gateway.importArgumentPoliciesYaml).toHaveBeenCalledWith('policies: [x]'));
            await waitFor(() =>
                expect(toast.success).toHaveBeenCalledWith('Import Complete', '1 created, 2 updated, 0 unchanged'),
            );
            expect(gateway.listArgumentPolicies).toHaveBeenCalledTimes(2);
        });

        it('warns rather than claiming success when groups did not resolve', async () => {
            gateway.importArgumentPoliciesYaml.mockResolvedValue({
                created: [],
                updated: [],
                unchanged: ['a'],
                errors: [{ name: 'x' }],
                unresolved_groups: ['g1', 'g2'],
            });
            await open();
            pickYaml();
            await waitFor(() =>
                expect(toast.warning).toHaveBeenCalledWith(
                    'Import Complete (with warnings)',
                    '0 created, 0 updated, 1 unchanged — 1 error(s), 2 unresolved group(s)',
                ),
            );
            expect(toast.success).not.toHaveBeenCalled();
        });

        it('reports a rejected bundle', async () => {
            gateway.importArgumentPoliciesYaml.mockRejectedValue(new Error('bad yaml'));
            await open();
            pickYaml();
            await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Import Failed', 'bad yaml'));
            expect(gateway.listArgumentPolicies).toHaveBeenCalledTimes(1);
        });
    });

    it('opens the form empty or seeded, and reloads only after a save', async () => {
        await open();
        fireEvent.click(screen.getByRole('button', { name: 'New Policy' }));
        expect(screen.getByText('creating')).toBeInTheDocument();
        fireEvent.click(screen.getByText('form-cancel'));
        expect(screen.queryByTestId('form')).toBeNull();
        expect(gateway.listArgumentPolicies).toHaveBeenCalledTimes(1);

        fireEvent.click(within(rowOf('ssrf')).getByTitle(/Edit/));
        expect(screen.getByText('editing ssrf')).toBeInTheDocument();
        fireEvent.click(screen.getByText('form-saved'));
        await waitFor(() => expect(gateway.listArgumentPolicies).toHaveBeenCalledTimes(2));
        expect(screen.queryByTestId('form')).toBeNull();
    });

    it('the empty state offers the first policy', async () => {
        gateway.listArgumentPolicies.mockResolvedValue({ policies: [] });
        render(<ArgumentPoliciesTab />);
        fireEvent.click(await screen.findByRole('button', { name: 'Create First Policy' }));
        expect(screen.getByText('creating')).toBeInTheDocument();
    });
});
