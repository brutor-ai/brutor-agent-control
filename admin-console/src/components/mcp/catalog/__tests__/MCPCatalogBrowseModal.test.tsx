/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * MCPCatalogBrowseModal — server-side filtered, server-side paged browse.
 *
 * Every filter here is a *query parameter*, not a client-side array filter,
 * so the assertions are about the request the modal issues: picking a
 * category must send `category`, switching runtime must send `runtime`, and
 * — the easy bug — changing any filter must reset `offset` back to 0, or the
 * operator lands on page 3 of a one-page result and sees nothing. The error
 * path must also stay honest rather than rendering an empty grid that reads
 * as "no servers exist".
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';

import type {
    CatalogEntryListResponse,
    MCPCatalogEntryBrief,
} from '../../../../lib/mcpCatalogTypes';

const { fetchEntries, fetchEntry, fetchCategories, gatewayClient } = vi.hoisted(() => {
    const fetchEntries = vi.fn();
    const fetchEntry = vi.fn();
    const fetchCategories = vi.fn();
    // One stable object: the component's loaders are `useCallback`s keyed on
    // `gatewayClient`, so handing back a fresh literal per render would spin
    // the load effect forever — a test artefact, not a component bug.
    return {
        fetchEntries,
        fetchEntry,
        fetchCategories,
        gatewayClient: {
            fetchMCPCatalogEntries: fetchEntries,
            fetchMCPCatalogEntry: fetchEntry,
            fetchMCPCatalogCategories: fetchCategories,
        },
    };
});

vi.mock('../../../../hooks/useGateway', () => ({
    useGateway: () => ({ gatewayClient }),
}));

import MCPCatalogBrowseModal from '../MCPCatalogBrowseModal';

const brief = (id: string, name: string): MCPCatalogEntryBrief => ({
    id,
    tenant_id: 't1',
    name,
    slug: name.toLowerCase(),
    short_description: `${name} server`,
    icon_url: null,
    has_icon: false,
    repo_url: null,
    runtime: 'remote',
    categories: ['dev'],
    auth_pattern: 'oauth',
    source: 'system',
    enabled: true,
    created_at: null,
    updated_at: null,
});

const listing = (
    entries: MCPCatalogEntryBrief[],
    total = entries.length
): CatalogEntryListResponse => ({ entries, total });

const lastParams = (): Record<string, unknown> | undefined => {
    const calls = fetchEntries.mock.calls as unknown as Record<string, unknown>[][];
    return calls.length ? calls[calls.length - 1][0] : undefined;
};

describe('MCPCatalogBrowseModal', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        fetchEntries.mockResolvedValue(listing([brief('e1', 'GitHub'), brief('e2', 'Jira')]));
        fetchCategories.mockResolvedValue({
            categories: [
                { name: 'dev', count: 4 },
                { name: 'crm', count: 2 },
            ],
        });
    });

    afterEach(() => cleanup());

    it('renders nothing and issues no request while closed', () => {
        const { container } = render(
            <MCPCatalogBrowseModal isOpen={false} onClose={vi.fn()} onImport={vi.fn()} />
        );
        expect(container).toBeEmptyDOMElement();
        expect(fetchEntries).not.toHaveBeenCalled();
    });

    it('loads the first page of enabled entries and the category facets on open', async () => {
        render(<MCPCatalogBrowseModal isOpen onClose={vi.fn()} onImport={vi.fn()} />);

        expect(await screen.findByText('GitHub')).toBeTruthy();
        expect(screen.getByText('Jira')).toBeTruthy();
        expect(lastParams()).toEqual({ offset: 0, limit: 12, enabled_only: true });
        // Facets arrive from a separate call and carry their counts.
        expect(await screen.findByRole('button', { name: /^dev \(4\)$/ })).toBeTruthy();
        expect(screen.getByRole('button', { name: /^crm \(2\)$/ })).toBeTruthy();
    });

    it('sends the runtime, auth and category filters as query parameters', async () => {
        render(<MCPCatalogBrowseModal isOpen onClose={vi.fn()} onImport={vi.fn()} />);
        await screen.findByText('GitHub');

        fireEvent.click(screen.getByRole('button', { name: 'Containerized' }));
        await waitFor(() => expect(lastParams()?.runtime).toBe('containerized'));

        fireEvent.click(screen.getByRole('button', { name: 'API Key' }));
        await waitFor(() => expect(lastParams()?.auth_pattern).toBe('api_key'));

        await waitFor(() => expect(screen.getByRole('button', { name: /^dev/ })).toBeTruthy());
        fireEvent.click(screen.getByRole('button', { name: /^dev/ }));
        await waitFor(() => expect(lastParams()?.category).toBe('dev'));
    });

    it('omits a filter entirely when it is set back to "all"', async () => {
        render(<MCPCatalogBrowseModal isOpen onClose={vi.fn()} onImport={vi.fn()} />);
        await screen.findByText('GitHub');

        fireEvent.click(screen.getByRole('button', { name: 'Remote' }));
        await waitFor(() => expect(lastParams()?.runtime).toBe('remote'));

        fireEvent.click(screen.getByRole('button', { name: 'All Runtimes' }));
        await waitFor(() => expect(lastParams()).not.toHaveProperty('runtime'));
    });

    it('searches server-side rather than filtering the loaded page', async () => {
        render(<MCPCatalogBrowseModal isOpen onClose={vi.fn()} onImport={vi.fn()} />);
        await screen.findByText('GitHub');

        fireEvent.change(screen.getByPlaceholderText(/search catalog entries/i), {
            target: { value: 'jira' },
        });
        await waitFor(() => expect(lastParams()?.search).toBe('jira'));
    });

    it('pages forward, and a new filter resets back to the first page', async () => {
        const page = Array.from({ length: 12 }, (_, i) => brief(`e${i}`, `Server${i}`));
        fetchEntries.mockResolvedValue(listing(page, 30));
        render(<MCPCatalogBrowseModal isOpen onClose={vi.fn()} onImport={vi.fn()} />);

        expect(await screen.findByText('Page 1 of 3')).toBeTruthy();
        expect(screen.getByText('Showing 1-12 of 30')).toBeTruthy();

        const [prev, next] = screen.getAllByRole('button').filter(b => b.className.includes('p-1.5'));
        expect(prev.hasAttribute('disabled')).toBe(true);

        fireEvent.click(next);
        await waitFor(() => expect(lastParams()?.offset).toBe(12));
        expect(await screen.findByText('Page 2 of 3')).toBeTruthy();

        // Narrowing the result set must not strand the operator on page 2.
        fireEvent.click(screen.getByRole('button', { name: 'Remote' }));
        await waitFor(() => expect(lastParams()?.offset).toBe(0));
    });

    it('hides pagination when everything fits on one page', async () => {
        render(<MCPCatalogBrowseModal isOpen onClose={vi.fn()} onImport={vi.fn()} />);
        await screen.findByText('GitHub');
        expect(screen.queryByText(/^Page \d/)).toBeNull();
    });

    it('reports a load failure and retries on demand', async () => {
        fetchEntries.mockRejectedValueOnce(new Error('catalog unreachable'));
        const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
        render(<MCPCatalogBrowseModal isOpen onClose={vi.fn()} onImport={vi.fn()} />);

        expect(await screen.findByText('catalog unreachable')).toBeTruthy();
        // An empty grid would read as "the catalog is empty" — it must not.
        expect(screen.queryByText('No entries found')).toBeNull();

        fireEvent.click(screen.getByRole('button', { name: /try again/i }));
        expect(await screen.findByText('GitHub')).toBeTruthy();
        errorSpy.mockRestore();
    });

    it('distinguishes an empty result from a failure', async () => {
        fetchEntries.mockResolvedValue(listing([], 0));
        render(<MCPCatalogBrowseModal isOpen onClose={vi.fn()} onImport={vi.fn()} />);
        expect(await screen.findByText('No entries found')).toBeTruthy();
    });

    it('fetches the full entry when a card is opened, and imports that entry', async () => {
        const full = { ...brief('e1', 'GitHub'), description: 'Full detail', env_vars: [] };
        fetchEntry.mockResolvedValue(full);
        const onImport = vi.fn();
        render(<MCPCatalogBrowseModal isOpen onClose={vi.fn()} onImport={onImport} />);

        fireEvent.click(await screen.findByText('GitHub'));
        await waitFor(() => expect(fetchEntry).toHaveBeenCalledWith('e1'));
        expect(await screen.findByText('Full detail')).toBeTruthy();

        fireEvent.click(screen.getByRole('button', { name: /import to servers/i }));
        expect(onImport).toHaveBeenCalledWith(full);
    });

    it('returns to the grid from the detail view', async () => {
        fetchEntry.mockResolvedValue({ ...brief('e1', 'GitHub'), description: 'Full detail' });
        render(<MCPCatalogBrowseModal isOpen onClose={vi.fn()} onImport={vi.fn()} />);

        fireEvent.click(await screen.findByText('GitHub'));
        fireEvent.click(await screen.findByRole('button', { name: /back to list/i }));
        expect(await screen.findByText('Jira')).toBeTruthy();
    });

    it('surfaces the entry count in the header', async () => {
        fetchEntries.mockResolvedValue(listing([brief('e1', 'GitHub')], 41));
        render(<MCPCatalogBrowseModal isOpen onClose={vi.fn()} onImport={vi.fn()} />);
        expect(await screen.findByText('(41 entries)')).toBeTruthy();
    });
});
