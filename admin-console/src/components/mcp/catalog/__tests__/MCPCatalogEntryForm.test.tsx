/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Catalog entry form: the payload handed to the parent through the window
 * accessor, and the save lifecycle.
 *
 * Also pinned: the slug follows the name as it is typed. Generated only while
 * empty, it would lock to the first keystroke; typing "My Server" would produce
 * the slug "m".
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';

import type { MCPCatalogEntry } from '../../../../lib/mcpCatalogTypes';
import MCPCatalogEntryForm from '../MCPCatalogEntryForm';

const onClose = vi.fn();
const onSave = vi.fn();

const type = (placeholder: string | RegExp, value: string, index = 0) =>
    fireEvent.change(screen.getAllByPlaceholderText(placeholder)[index], { target: { value } });
const value = (placeholder: string) => screen.getByPlaceholderText<HTMLInputElement>(placeholder).value;
const submit = () => screen.getByText(/^(Create|Update) Entry$/).closest('button') as HTMLButtonElement;
const payload = (): Record<string, unknown> =>
    (window as unknown as { __mcpCatalogFormPayload: () => Record<string, unknown> }).__mcpCatalogFormPayload();
const removeButtonNextTo = (el: HTMLElement) => el.closest('.flex')?.querySelector('button') as HTMLButtonElement;

const renderForm = (entry?: MCPCatalogEntry) =>
    render(<MCPCatalogEntryForm isOpen onClose={onClose} onSave={onSave} entry={entry} />);

const remoteEntry = {
    id: 'e1',
    name: 'Existing',
    slug: 'existing-slug',
    short_description: 'short',
    description: 'long',
    icon_url: 'https://x.test/i.png',
    repo_url: 'https://x.test/repo',
    runtime: 'remote',
    auth_pattern: 'oauth',
    categories: ['a', 'b'],
    enabled: false,
    env_vars: [{ key: 'K', name: 'Key', required: true, sensitive: true, description: 'd' }],
    tool_preview: [{ name: 't', description: 'td', params: {} }],
    remote_config: {
        fixedURL: 'https://mcp.test/sse',
        hostname: 'mcp.test',
        headers: [{ key: 'X-A', name: 'A', required: true, sensitive: false }],
    },
} as unknown as MCPCatalogEntry;

describe('MCPCatalogEntryForm', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        onSave.mockResolvedValue(undefined);
    });

    afterEach(cleanup);

    it('renders nothing when closed', () => {
        render(<MCPCatalogEntryForm isOpen={false} onClose={onClose} onSave={onSave} />);
        expect(screen.queryByText('Add Catalog Entry')).toBeNull();
    });

    describe('slug', () => {
        it('follows the name as it is typed, not just the first keystroke', () => {
            renderForm();
            for (const v of ['M', 'My', 'My Server!']) type('My MCP Server', v);
            expect(value('my-mcp-server')).toBe('my-server');
        });

        it('a typed slug stops following; clearing it resumes', () => {
            renderForm();
            type('My MCP Server', 'One');
            type('my-mcp-server', 'custom');
            type('My MCP Server', 'Two');
            expect(value('my-mcp-server')).toBe('custom');
            type('my-mcp-server', '');
            expect(value('my-mcp-server')).toBe('two');
        });

        it('is never regenerated when editing', () => {
            renderForm(remoteEntry);
            type('My MCP Server', 'New Name');
            expect(value('my-mcp-server')).toBe('existing-slug');
        });
    });

    describe('payload', () => {
        it('create, minimal: empty optionals are omitted, not sent blank', () => {
            renderForm();
            type('My MCP Server', 'Min');
            expect(payload()).toEqual({
                name: 'Min',
                slug: 'min',
                short_description: undefined,
                description: undefined,
                icon_url: undefined,
                repo_url: undefined,
                runtime: 'remote',
                remote_config: { fixedURL: undefined, hostname: undefined, headers: undefined },
                containerized_config: undefined,
                env_vars: undefined,
                tool_preview: undefined,
                categories: undefined,
                auth_pattern: 'none',
                enabled: true,
            });
        });

        it('remote: every field, trimmed categories, headers, env vars and tools', () => {
            renderForm();
            type('My MCP Server', 'Full');
            type('Brief one-line description', 'brief');
            type('Full description of this MCP server...', 'long');
            type('https://example.com/icon.png', 'https://i.test/a.png');
            type('https://github.com/org/repo', 'https://r.test');
            type('https://mcp.example.com/sse', 'https://m.test/sse');
            type('mcp.example.com', 'm.test');
            type(/comma-separated/, ' search, , dev ');
            fireEvent.change(screen.getByRole('combobox'), { target: { value: 'api_key' } });

            fireEvent.click(screen.getByText('Add Header'));
            type('Header key', 'X-Key');
            type('Display name', 'The Key');
            fireEvent.click(screen.getByText('Req'));
            fireEvent.click(screen.getByText('Sens'));

            fireEvent.click(screen.getByText('Add Variable'));
            type('ENV_KEY', 'TOKEN');
            type('Display name', 'Token', 1);
            type('Description', 'the token');
            fireEvent.click(screen.getByText('Required'));
            fireEvent.click(screen.getByText('Sensitive'));

            fireEvent.click(screen.getByText('Add Tool'));
            type('tool_name', 'search');
            type('Tool description', 'finds');

            const p = payload();
            expect(p).toMatchObject({
                short_description: 'brief',
                description: 'long',
                icon_url: 'https://i.test/a.png',
                repo_url: 'https://r.test',
                auth_pattern: 'api_key',
                categories: ['search', 'dev'],
                remote_config: {
                    fixedURL: 'https://m.test/sse',
                    hostname: 'm.test',
                    headers: [{ key: 'X-Key', name: 'The Key', required: true, sensitive: true }],
                },
                env_vars: [{ key: 'TOKEN', name: 'Token', required: true, sensitive: true, description: 'the token' }],
                tool_preview: [{ name: 'search', description: 'finds', params: {} }],
            });
            expect(p.containerized_config).toBeUndefined();
        });

        it('containerized: sends its config, split args, and no remote config', () => {
            renderForm();
            type('My MCP Server', 'Box');
            fireEvent.click(screen.getByLabelText('Containerized'));
            expect(screen.queryByText('Remote Configuration')).toBeNull();
            type('mcp/fetch:latest', 'mcp/box:1');
            fireEvent.change(screen.getByDisplayValue('8080'), { target: { value: '9000' } });
            type('/mcp', '/rpc');
            type('--verbose --port 8080', '  --a   --b 1 ');
            const p = payload();
            expect(p.containerized_config).toEqual({ image: 'mcp/box:1', port: 9000, path: '/rpc', args: ['--a', '--b', '1'] });
            expect(p.remote_config).toBeUndefined();
        });

        it('removed rows do not reach the payload', () => {
            renderForm();
            type('My MCP Server', 'x');
            fireEvent.click(screen.getByText('Add Header'));
            fireEvent.click(removeButtonNextTo(screen.getByPlaceholderText('Header key')));
            fireEvent.click(screen.getByText('Add Variable'));
            const envRow = screen.getByPlaceholderText('ENV_KEY').closest('.items-start') as HTMLElement;
            fireEvent.click(envRow.querySelector('button') as HTMLButtonElement);
            fireEvent.click(screen.getByText('Add Tool'));
            const toolRow = screen.getByPlaceholderText('tool_name').closest('.items-start') as HTMLElement;
            fireEvent.click(toolRow.querySelector('button') as HTMLButtonElement);

            expect(screen.queryByPlaceholderText('Header key')).toBeNull();
            expect(screen.queryByPlaceholderText('ENV_KEY')).toBeNull();
            expect(screen.queryByPlaceholderText('tool_name')).toBeNull();
            expect(payload()).toMatchObject({ env_vars: undefined, tool_preview: undefined });
        });

        it('disabling is carried', () => {
            renderForm();
            type('My MCP Server', 'x');
            const toggle = screen.getAllByRole('checkbox').find(c => (c as HTMLInputElement).checked) as HTMLInputElement;
            fireEvent.click(toggle);
            expect(payload().enabled).toBe(false);
        });

        it('edit: populates from the entry and round-trips it unchanged', () => {
            renderForm(remoteEntry);
            expect(screen.getByText('Edit Catalog Entry')).toBeTruthy();
            expect(screen.getByText('Update Entry')).toBeTruthy();
            expect(payload()).toEqual({
                name: 'Existing',
                slug: 'existing-slug',
                short_description: 'short',
                description: 'long',
                icon_url: 'https://x.test/i.png',
                repo_url: 'https://x.test/repo',
                runtime: 'remote',
                remote_config: remoteEntry.remote_config,
                containerized_config: undefined,
                env_vars: remoteEntry.env_vars,
                tool_preview: remoteEntry.tool_preview,
                categories: ['a', 'b'],
                auth_pattern: 'oauth',
                enabled: false,
            });
        });

        it('edit: containerized entry populates its config; dropping back to create resets', () => {
            const box = {
                ...remoteEntry,
                runtime: 'containerized',
                remote_config: undefined,
                containerized_config: { image: 'i:1', port: 7000, path: '/p', args: ['--x', '1'] },
            } as unknown as MCPCatalogEntry;
            const view = renderForm(box);
            expect(payload().containerized_config).toEqual({ image: 'i:1', port: 7000, path: '/p', args: ['--x', '1'] });

            view.rerender(<MCPCatalogEntryForm isOpen onClose={onClose} onSave={onSave} />);
            expect(screen.getByText('Add Catalog Entry')).toBeTruthy();
            expect(payload()).toMatchObject({ name: '', slug: '', runtime: 'remote', enabled: true });
        });
    });

    describe('save', () => {
        it('is disabled until name and slug exist', () => {
            renderForm();
            expect(submit().disabled).toBe(true);
            type('My MCP Server', '   ');
            expect(submit().disabled).toBe(true);
            type('My MCP Server', 'Ok');
            expect(submit().disabled).toBe(false);
        });

        it('shows Saving and blocks actions until the parent finishes', async () => {
            let finish: () => void = () => {};
            onSave.mockReturnValue(new Promise<void>(resolve => { finish = resolve; }));
            renderForm();
            type('My MCP Server', 'Ok');
            fireEvent.click(submit());

            expect(onSave).toHaveBeenCalledTimes(1);
            expect((await screen.findByText('Saving...')).closest('button')?.disabled).toBe(true);
            expect(screen.getByText<HTMLButtonElement>('Cancel').disabled).toBe(true);

            finish();
            await waitFor(() => expect(submit().disabled).toBe(false));
        });

        it('a rejected save is shown in the form', async () => {
            onSave.mockRejectedValue(new Error('slug taken'));
            renderForm();
            type('My MCP Server', 'Ok');
            fireEvent.click(submit());
            expect(await screen.findByText('slug taken')).toBeTruthy();
            expect(submit().disabled).toBe(false);
        });

        it('Cancel closes without saving', () => {
            renderForm();
            fireEvent.click(screen.getByText('Cancel'));
            expect(onClose).toHaveBeenCalledTimes(1);
            expect(onSave).not.toHaveBeenCalled();
        });
    });

    it('withdraws the window accessor on unmount', () => {
        const view = renderForm();
        expect(typeof payload).toBe('function');
        view.unmount();
        expect((window as unknown as Record<string, unknown>).__mcpCatalogFormPayload).toBeUndefined();
    });
});
