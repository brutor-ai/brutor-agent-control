/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Catalog tab: filters reach the server, and create / edit / delete / import
 * call the API with the form's real payload. The real entry form is used because
 * the two communicate through a window accessor that a stub would hide.
 *
 * Also pinned: a save rejection reaches the form (inline error, Saving state),
 * and filtering down to nothing does not replace the whole screen with a
 * spinner on each keystroke, which would unmount the search box being typed in.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, act } from '@testing-library/react';

import type { MCPCatalogEntry } from '../../../../lib/mcpCatalogTypes';

const gateway = {
    gatewayClient: {
        fetchMCPCatalogEntries: vi.fn(),
        fetchMCPCatalogEntry: vi.fn(),
        createMCPCatalogEntry: vi.fn(),
        updateMCPCatalogEntry: vi.fn(),
        deleteMCPCatalogEntry: vi.fn(),
    },
};
const client = gateway.gatewayClient;
const toast = { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() };
const captured: {
    browseImport: ((entry: MCPCatalogEntry) => void) | null;
    imported: (() => void) | null;
} = { browseImport: null, imported: null };

vi.mock('../../../../hooks/useGateway', () => ({ useGateway: () => gateway }));
vi.mock('../../../common/Toast', () => ({ useToast: () => toast }));
vi.mock('../MCPCatalogBrowseModal', () => ({
    default: ({ isOpen, onClose, onImport }: { isOpen: boolean; onClose: () => void; onImport: (e: MCPCatalogEntry) => void }) => {
        captured.browseImport = onImport;
        return isOpen ? <button onClick={onClose}>close browse</button> : null;
    },
}));
vi.mock('../MCPCatalogImportModal', () => ({
    default: ({ entry, onClose, onImported }: { entry: MCPCatalogEntry; onClose: () => void; onImported: () => void }) => {
        captured.imported = onImported;
        return <button onClick={onClose}>importing:{entry.name}</button>;
    },
}));
vi.mock('../../../common/GenericDeleteConfirmationModal', () => ({
    default: ({ isOpen, itemName, errorMessage, onConfirm, onClose }: {
        isOpen: boolean; itemName: string; errorMessage?: string | null; onConfirm: () => void; onClose: () => void;
    }) => (isOpen ? (
        <div>
            <span>confirm-delete:{itemName}</span>
            {errorMessage && <span>delete-error:{errorMessage}</span>}
            <button onClick={onConfirm}>confirm delete</button>
            <button onClick={onClose}>cancel delete</button>
        </div>
    ) : null),
}));

import MCPCatalogTab from '../MCPCatalogTab';

function brief(id: string, overrides: Record<string, unknown> = {}) {
    return {
        id,
        name: `Entry ${id}`,
        slug: id,
        short_description: `about ${id}`,
        runtime: 'remote',
        auth_pattern: 'oauth',
        source: 'custom',
        enabled: true,
        categories: ['one', 'two', 'three'],
        ...overrides,
    };
}
const full = { ...brief('a'), remote_config: {} } as unknown as MCPCatalogEntry;

async function renderList(entries = [brief('a')], total = entries.length) {
    client.fetchMCPCatalogEntries.mockResolvedValue({ entries, total });
    render(<MCPCatalogTab />);
    await screen.findByText(/Manage catalog entries/);
}

describe('MCPCatalogTab', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        vi.spyOn(console, 'error').mockImplementation(() => {});
        captured.browseImport = null;
        captured.imported = null;
    });

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
    });

    describe('listing', () => {
        it('shows entries, badges and the category overflow', async () => {
            await renderList([brief('a'), brief('b', { runtime: 'containerized', auth_pattern: 'api_key', source: 'system', icon_url: 'https://i.test/b.png', enabled: false })], 9);
            expect(client.fetchMCPCatalogEntries).toHaveBeenCalledWith({});
            expect(screen.getByText(/\(2 of 9\)/)).toBeTruthy();
            expect(screen.getByText('Entry a')).toBeTruthy();
            expect(screen.getByText('about b')).toBeTruthy();
            expect(screen.getAllByText('+1').length).toBe(2);
            expect(screen.getByAltText('Entry b').getAttribute('src')).toBe('https://i.test/b.png');
        });

        it('empty catalog offers the first-entry path', async () => {
            await renderList([], 0);
            expect(screen.getByText('No catalog entries')).toBeTruthy();
            fireEvent.click(screen.getByText('Add First Entry'));
            expect(screen.getByText('Add Catalog Entry')).toBeTruthy();
        });

        it('a filtered-empty page says so, without the first-entry path', async () => {
            await renderList([], 5);
            expect(screen.getByText('No entries match your filters')).toBeTruthy();
            expect(screen.queryByText('Add First Entry')).toBeNull();
        });

        it('a load failure toasts the reason', async () => {
            client.fetchMCPCatalogEntries.mockRejectedValue(new Error('down'));
            render(<MCPCatalogTab />);
            await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Load Failed', 'down'));
            expect(await screen.findByText('No catalog entries')).toBeTruthy();
        });

        it('Refresh refetches', async () => {
            await renderList();
            fireEvent.click(screen.getByText('Refresh'));
            await waitFor(() => expect(client.fetchMCPCatalogEntries).toHaveBeenCalledTimes(2));
        });
    });

    describe('filters reach the server', () => {
        it('each filter maps to its query parameter, and they combine', async () => {
            await renderList();
            fireEvent.change(screen.getByPlaceholderText(/Search catalog/), { target: { value: 'gh' } });
            await waitFor(() => expect(client.fetchMCPCatalogEntries).toHaveBeenLastCalledWith({ search: 'gh' }));
            const [runtime, source, auth] = screen.getAllByRole('combobox');
            fireEvent.change(runtime, { target: { value: 'remote' } });
            fireEvent.change(source, { target: { value: 'system' } });
            fireEvent.change(auth, { target: { value: 'oauth' } });
            await waitFor(() => expect(client.fetchMCPCatalogEntries).toHaveBeenLastCalledWith({
                search: 'gh',
                runtime: 'remote',
                source: 'system',
                auth_pattern: 'oauth',
            }));
        });

        it('the search box survives a refetch while the page is empty', async () => {
            await renderList([], 0);
            const box = screen.getByPlaceholderText(/Search catalog/);
            let finish: (v: unknown) => void = () => {};
            client.fetchMCPCatalogEntries.mockReturnValue(new Promise(resolve => { finish = resolve; }));
            fireEvent.change(box, { target: { value: 'z' } });
            await waitFor(() => expect(client.fetchMCPCatalogEntries).toHaveBeenCalledTimes(2));
            expect(screen.getByPlaceholderText(/Search catalog/)).toBe(box);
            await act(async () => { finish({ entries: [], total: 0 }); await Promise.resolve(); });
        });
    });

    describe('create', () => {
        const fillAndSubmit = () => {
            fireEvent.click(screen.getByText('Add Entry'));
            fireEvent.change(screen.getByPlaceholderText('My MCP Server'), { target: { value: 'New One' } });
            fireEvent.click(screen.getByText('Create Entry'));
        };

        it('posts the form payload, closes the form and reloads', async () => {
            await renderList();
            client.createMCPCatalogEntry.mockResolvedValue({});
            fillAndSubmit();

            await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Entry Created', 'New One has been created.'));
            expect(client.createMCPCatalogEntry).toHaveBeenCalledWith(expect.objectContaining({
                name: 'New One',
                slug: 'new-one',
                runtime: 'remote',
                auth_pattern: 'none',
                enabled: true,
            }));
            expect(client.updateMCPCatalogEntry).not.toHaveBeenCalled();
            await waitFor(() => expect(screen.queryByText('Add Catalog Entry')).toBeNull());
            expect(client.fetchMCPCatalogEntries).toHaveBeenCalledTimes(2);
        });

        it('a failure stays in the form with the reason inline, and toasts once', async () => {
            await renderList();
            client.createMCPCatalogEntry.mockRejectedValue(new Error('slug taken'));
            fillAndSubmit();

            expect(await screen.findByText('slug taken')).toBeTruthy();
            expect(screen.getByText('Add Catalog Entry')).toBeTruthy();
            expect(toast.error).toHaveBeenCalledTimes(1);
            expect(toast.error).toHaveBeenCalledWith('Save Failed', 'slug taken');
            expect(toast.success).not.toHaveBeenCalled();
        });
    });

    describe('edit', () => {
        it('fetches the full entry, then updates it by id', async () => {
            await renderList();
            client.fetchMCPCatalogEntry.mockResolvedValue(full);
            client.updateMCPCatalogEntry.mockResolvedValue({});
            fireEvent.click(screen.getByTitle('Edit entry'));

            // Wait for the form to populate, or the entry effect overwrites the edit.
            expect(await screen.findByDisplayValue('Entry a')).toBeTruthy();
            expect(screen.getByText('Edit Catalog Entry')).toBeTruthy();
            expect(client.fetchMCPCatalogEntry).toHaveBeenCalledWith('a');
            fireEvent.change(screen.getByPlaceholderText('My MCP Server'), { target: { value: 'Renamed' } });
            fireEvent.click(screen.getByText('Update Entry'));

            await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Entry Updated', 'Renamed has been updated.'));
            expect(client.updateMCPCatalogEntry).toHaveBeenCalledWith('a', expect.objectContaining({ name: 'Renamed', slug: 'a' }));
            expect(client.createMCPCatalogEntry).not.toHaveBeenCalled();
        });

        it('a failed fetch opens nothing and says why', async () => {
            await renderList();
            client.fetchMCPCatalogEntry.mockRejectedValue(new Error('gone'));
            fireEvent.click(screen.getByTitle('Edit entry'));
            await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Load Failed', 'gone'));
            expect(screen.queryByText('Edit Catalog Entry')).toBeNull();
        });

        it('closing clears the edit, so Add starts blank', async () => {
            await renderList();
            client.fetchMCPCatalogEntry.mockResolvedValue(full);
            fireEvent.click(screen.getByTitle('Edit entry'));
            await screen.findByText('Edit Catalog Entry');
            fireEvent.click(screen.getByText('Cancel'));
            fireEvent.click(screen.getByText('Add Entry'));
            expect(screen.getByText('Add Catalog Entry')).toBeTruthy();
            expect(screen.getByPlaceholderText<HTMLInputElement>('My MCP Server').value).toBe('');
        });
    });

    describe('delete', () => {
        it('asks first, then deletes, confirms and reloads', async () => {
            await renderList();
            fireEvent.click(screen.getByTitle('Delete entry'));
            expect(screen.getByText('confirm-delete:Entry a')).toBeTruthy();
            expect(client.deleteMCPCatalogEntry).not.toHaveBeenCalled();

            client.deleteMCPCatalogEntry.mockResolvedValue({});
            fireEvent.click(screen.getByText('confirm delete'));

            await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Entry Deleted', 'Entry a has been deleted.'));
            expect(client.deleteMCPCatalogEntry).toHaveBeenCalledWith('a');
            expect(screen.queryByText('confirm-delete:Entry a')).toBeNull();
            await waitFor(() => expect(client.fetchMCPCatalogEntries).toHaveBeenCalledTimes(2));
        });

        it('a failure stays in the dialog; cancel clears it', async () => {
            await renderList();
            fireEvent.click(screen.getByTitle('Delete entry'));
            client.deleteMCPCatalogEntry.mockRejectedValue(new Error('system entry'));
            fireEvent.click(screen.getByText('confirm delete'));
            expect(await screen.findByText('delete-error:system entry')).toBeTruthy();
            expect(toast.success).not.toHaveBeenCalled();

            fireEvent.click(screen.getByText('cancel delete'));
            expect(screen.queryByText(/^confirm-delete/)).toBeNull();
            fireEvent.click(screen.getByTitle('Delete entry'));
            expect(screen.queryByText(/^delete-error/)).toBeNull();
        });
    });

    describe('import', () => {
        it('from a row: fetches the full entry; finishing reloads', async () => {
            await renderList();
            client.fetchMCPCatalogEntry.mockResolvedValue(full);
            fireEvent.click(screen.getByTitle('Import as MCP Server'));
            expect(await screen.findByText('importing:Entry a')).toBeTruthy();
            expect(client.fetchMCPCatalogEntry).toHaveBeenCalledWith('a');

            act(() => { captured.imported?.(); });
            await waitFor(() => expect(screen.queryByText('importing:Entry a')).toBeNull());
            await waitFor(() => expect(client.fetchMCPCatalogEntries).toHaveBeenCalledTimes(2));
        });

        it('a failed fetch says why', async () => {
            await renderList();
            client.fetchMCPCatalogEntry.mockRejectedValue(new Error('nope'));
            fireEvent.click(screen.getByTitle('Import as MCP Server'));
            await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Load Failed', 'nope'));
        });

        it('from Browse: closes browse and opens the import; closing it does not reload', async () => {
            await renderList();
            fireEvent.click(screen.getByText('Browse & Import'));
            expect(screen.getByText('close browse')).toBeTruthy();
            act(() => { captured.browseImport?.(full); });
            expect(screen.queryByText('close browse')).toBeNull();
            fireEvent.click(screen.getByText('importing:Entry a'));
            expect(screen.queryByText('importing:Entry a')).toBeNull();
            expect(client.fetchMCPCatalogEntries).toHaveBeenCalledTimes(1);
        });
    });
});
