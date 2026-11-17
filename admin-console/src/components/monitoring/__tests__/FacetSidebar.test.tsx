/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * A section of the facet sidebar whose values could not be loaded says so
 * and offers Retry, which asks for the values of that field again. A section
 * asks for its values once when it opens and once each time a filter moves
 * while it is open, and shows the answer to the request it made last: values
 * or a failure notice, never both.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';

import FacetSidebar from '../FacetSidebar';
import type { FacetValue } from '../FacetMultiSelect';

const loadFacets = vi.fn<(field: string) => Promise<FacetValue[]>>();

const sidebar = (refetchKey: string) => (
    <FacetSidebar
        fields={[
            { field: 'proxy_type', label: 'Surface' },
            { field: 'status', label: 'Status' },
        ]}
        selected={{}}
        onChange={vi.fn()}
        loadFacets={loadFacets}
        storageKey="facet-sidebar-test"
        refetchKey={refetchKey}
    />
);
const renderSidebar = () => render(sidebar('unchanged'));

/** A request the test answers when it chooses. */
function pending() {
    let resolve!: (rows: FacetValue[]) => void;
    let reject!: (reason: Error) => void;
    const promise = new Promise<FacetValue[]>((res, rej) => {
        resolve = res;
        reject = rej;
    });
    return { promise, resolve, reject };
}
const requestsFor = (field: string) => loadFacets.mock.calls.filter(([asked]) => asked === field).length;

describe('FacetSidebar — values that could not be loaded', () => {
    beforeEach(() => {
        localStorage.clear();
        loadFacets.mockReset();
    });
    afterEach(cleanup);

    it('the section says why and Retry asks for that field again', async () => {
        loadFacets.mockRejectedValue(new Error('facets down'));
        renderSidebar();
        fireEvent.click(screen.getByRole('button', { name: /Surface/ }));
        const notice = await screen.findByRole('alert');
        expect(notice).toHaveTextContent('Failed to load the values: facets down');

        loadFacets.mockResolvedValue([{ value: 'llm', count: 7 }]);
        fireEvent.click(within(notice).getByRole('button', { name: 'Retry' }));
        expect(await screen.findByText('llm')).toBeInTheDocument();
        expect(screen.queryByRole('alert')).toBeNull();
        // One request for the open, one for the Retry, and none for the closed section.
        expect(loadFacets.mock.calls).toEqual([['proxy_type'], ['proxy_type']]);
    });
});

describe('FacetSidebar — how often the values are asked for', () => {
    beforeEach(() => {
        localStorage.clear();
        loadFacets.mockReset();
    });
    afterEach(cleanup);

    it('opening a section asks for its values once', async () => {
        loadFacets.mockResolvedValue([{ value: 'llm', count: 7 }]);
        renderSidebar();
        fireEvent.click(screen.getByRole('button', { name: /Surface/ }));
        expect(await screen.findByText('llm')).toBeInTheDocument();
        await act(async () => {});
        expect(loadFacets.mock.calls).toEqual([['proxy_type']]);
    });

    it('opening a second section does not ask for the first again', async () => {
        loadFacets.mockImplementation((field) => Promise.resolve([{ value: `${field}-value`, count: 1 }]));
        renderSidebar();
        fireEvent.click(screen.getByRole('button', { name: /Surface/ }));
        expect(await screen.findByText('proxy_type-value')).toBeInTheDocument();
        fireEvent.click(screen.getByRole('button', { name: /Status/ }));
        expect(await screen.findByText('status-value')).toBeInTheDocument();
        await act(async () => {});
        expect(loadFacets.mock.calls).toEqual([['proxy_type'], ['status']]);
    });

    it('a section stored as open is asked for once when the sidebar appears, and a closed one is not', async () => {
        localStorage.setItem('facet-sidebar-test', JSON.stringify({ status: true }));
        loadFacets.mockResolvedValue([{ value: 'success', count: 3 }]);
        renderSidebar();
        expect(await screen.findByText('success')).toBeInTheDocument();
        await act(async () => {});
        expect(loadFacets.mock.calls).toEqual([['status']]);
    });

    it('a filter that moved asks again for each open section, once, and for no closed one', async () => {
        loadFacets.mockResolvedValue([{ value: 'llm', count: 7 }]);
        const view = renderSidebar();
        fireEvent.click(screen.getByRole('button', { name: /Surface/ }));
        expect(await screen.findByText('llm')).toBeInTheDocument();
        loadFacets.mockResolvedValue([{ value: 'mcp', count: 2 }]);
        view.rerender(sidebar('moved'));
        expect(await screen.findByText('mcp')).toBeInTheDocument();
        await act(async () => {});
        expect(loadFacets.mock.calls).toEqual([['proxy_type'], ['proxy_type']]);
    });

    it('reopening a section asks again: the filters may have moved while it was closed', async () => {
        loadFacets.mockResolvedValue([{ value: 'llm', count: 7 }]);
        renderSidebar();
        const header = screen.getByRole('button', { name: /Surface/ });
        fireEvent.click(header);
        expect(await screen.findByText('llm')).toBeInTheDocument();
        fireEvent.click(header);
        loadFacets.mockResolvedValue([{ value: 'a2a', count: 1 }]);
        fireEvent.click(header);
        expect(await screen.findByText('a2a')).toBeInTheDocument();
        expect(requestsFor('proxy_type')).toBe(2);
    });
});

describe('FacetSidebar — the answer shown is the one last asked for', () => {
    beforeEach(() => {
        localStorage.clear();
        loadFacets.mockReset();
    });
    afterEach(cleanup);

    it('a failure of an earlier request does not stand over the values of a later one', async () => {
        const first = pending();
        const second = pending();
        loadFacets.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
        const view = renderSidebar();
        fireEvent.click(screen.getByRole('button', { name: /Surface/ }));
        await waitFor(() => expect(requestsFor('proxy_type')).toBe(1));
        view.rerender(sidebar('moved'));
        await waitFor(() => expect(requestsFor('proxy_type')).toBe(2));

        await act(async () => second.resolve([{ value: 'llm', count: 7 }]));
        expect(await screen.findByText('llm')).toBeInTheDocument();
        await act(async () => first.reject(new Error('facets down')));
        expect(screen.queryByRole('alert')).toBeNull();
        expect(screen.getByText('llm')).toBeInTheDocument();
    });

    it('values of an earlier request do not replace the failure of a later one', async () => {
        const first = pending();
        const second = pending();
        loadFacets.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
        const view = renderSidebar();
        fireEvent.click(screen.getByRole('button', { name: /Surface/ }));
        await waitFor(() => expect(requestsFor('proxy_type')).toBe(1));
        view.rerender(sidebar('moved'));
        await waitFor(() => expect(requestsFor('proxy_type')).toBe(2));

        await act(async () => second.reject(new Error('facets down')));
        expect(await screen.findByRole('alert')).toHaveTextContent('Failed to load the values: facets down');
        await act(async () => first.resolve([{ value: 'llm', count: 7 }]));
        expect(screen.getByRole('alert')).toBeInTheDocument();
        expect(screen.queryByText('llm')).toBeNull();
    });

    it('a failure takes the values that were shown away: the notice never stands over values', async () => {
        loadFacets.mockResolvedValue([{ value: 'llm', count: 7 }]);
        const view = renderSidebar();
        fireEvent.click(screen.getByRole('button', { name: /Surface/ }));
        expect(await screen.findByText('llm')).toBeInTheDocument();
        loadFacets.mockRejectedValue(new Error('facets down'));
        view.rerender(sidebar('moved'));
        const notice = await screen.findByRole('alert');
        expect(screen.queryByText('llm')).toBeNull();

        loadFacets.mockResolvedValue([{ value: 'mcp', count: 2 }]);
        fireEvent.click(within(notice).getByRole('button', { name: 'Retry' }));
        expect(await screen.findByText('mcp')).toBeInTheDocument();
        expect(screen.queryByRole('alert')).toBeNull();
    });
});
