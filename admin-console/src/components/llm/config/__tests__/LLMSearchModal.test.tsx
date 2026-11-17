/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Catalog search modal.
 *
 * Pinned: the query it sends, with unset filters left out because the endpoint
 * treats a present parameter as a filter, and the import flow with its per-row
 * spinner.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within, createEvent } from '@testing-library/react';

const llmSearch = {
    search: vi.fn(),
    loading: false,
    error: null as string | null,
    searchResults: null as { catalog: unknown[]; total: number } | null,
};
vi.mock('../../../../hooks/useLLMSearch', () => ({ useLLMSearch: () => llmSearch }));
vi.mock('../providerLogos', () => ({ ProviderLogo: () => <span>logo</span> }));

import LLMSearchModal from '../LLMSearchModal';

const onClose = vi.fn();
const onImportModel = vi.fn();

function entry(overrides: Record<string, unknown> = {}) {
    return {
        id: 'c1',
        display_name: 'GPT Test',
        provider_key: 'openai',
        model_name: 'gpt-test',
        mode: 'chat',
        ...overrides,
    };
}

function renderOpen() {
    return render(<LLMSearchModal isOpen onClose={onClose} onImportModel={onImportModel} />);
}

/** React drops a keypress with charCode 0, and happy-dom ignores charCode in the event init. */
function pressKey(target: HTMLElement, key: string, charCode: number) {
    const event = createEvent.keyPress(target, { key });
    Object.defineProperty(event, 'charCode', { value: charCode });
    fireEvent(target, event);
}

function clickSearch() {
    fireEvent.click(screen.getByRole('button', { name: 'Search' }));
}

describe('LLMSearchModal', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        llmSearch.loading = false;
        llmSearch.error = null;
        llmSearch.searchResults = null;
        llmSearch.search.mockResolvedValue(undefined);
        onImportModel.mockResolvedValue(undefined);
    });
    afterEach(cleanup);

    it('renders nothing while closed', () => {
        const { container } = render(<LLMSearchModal isOpen={false} onClose={onClose} onImportModel={onImportModel} />);
        expect(container.textContent).toBe('');
    });

    it('does not search until asked, and sends only paging for an empty form', async () => {
        renderOpen();
        expect(screen.getByText('Browse the Model Catalog')).toBeTruthy();
        expect(llmSearch.search).not.toHaveBeenCalled();

        clickSearch();
        await waitFor(() => expect(llmSearch.search).toHaveBeenCalledWith({ offset: 0, limit: 100 }));
    });

    it('sends every filter that is set, with the query trimmed', async () => {
        renderOpen();
        fireEvent.change(screen.getByPlaceholderText('Search by name or model ID...'), {
            target: { value: '  gpt  ' },
        });
        const selects = screen.getAllByRole('combobox');
        fireEvent.change(selects[0], { target: { value: 'openai' } });
        fireEvent.click(screen.getByRole('button', { name: 'Advanced Filters' }));
        fireEvent.change(screen.getAllByRole('combobox')[1], { target: { value: 'embedding' } });
        fireEvent.click(screen.getByLabelText('Recommended only'));

        clickSearch();
        await waitFor(() =>
            expect(llmSearch.search).toHaveBeenCalledWith({
                offset: 0,
                limit: 100,
                q: 'gpt',
                provider_key: 'openai',
                mode: 'embedding',
                recommended: true,
            })
        );
    });

    it('searches on Enter and not on other keys', async () => {
        renderOpen();
        const input = screen.getByPlaceholderText('Search by name or model ID...');
        pressKey(input, 'a', 97);
        expect(llmSearch.search).not.toHaveBeenCalled();
        pressKey(input, 'Enter', 13);
        await waitFor(() => expect(llmSearch.search).toHaveBeenCalledTimes(1));
    });

    it('offers real providers only, not the synthetic ones', () => {
        renderOpen();
        const values = within(screen.getAllByRole('combobox')[0])
            .getAllByRole('option')
            .map(o => (o as HTMLOptionElement).value);
        expect(values).toContain('openai');
        expect(values).not.toContain('routing_group');
        expect(values).not.toContain('custom');
    });

    it('resets the filters when it closes', () => {
        const { rerender } = renderOpen();
        const input = screen.getByPlaceholderText<HTMLInputElement>('Search by name or model ID...');
        fireEvent.change(input, { target: { value: 'gpt' } });
        fireEvent.click(screen.getByRole('button', { name: 'Advanced Filters' }));

        rerender(<LLMSearchModal isOpen={false} onClose={onClose} onImportModel={onImportModel} />);
        rerender(<LLMSearchModal isOpen onClose={onClose} onImportModel={onImportModel} />);
        expect(screen.getByPlaceholderText<HTMLInputElement>('Search by name or model ID...').value).toBe('');
        expect(screen.queryByLabelText('Recommended only')).toBeNull();
    });

    it('shows progress and disables the button while searching', () => {
        llmSearch.loading = true;
        renderOpen();
        expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Searching...' }).disabled).toBe(true);
        expect(screen.queryByText('Browse the Model Catalog')).toBeNull();
    });

    it('shows a search failure', () => {
        llmSearch.error = 'catalog unavailable';
        renderOpen();
        expect(screen.getByText('Search Failed')).toBeTruthy();
        expect(screen.getByText('catalog unavailable')).toBeTruthy();
    });

    it('says when nothing matched', () => {
        llmSearch.searchResults = { catalog: [], total: 0 };
        renderOpen();
        expect(screen.getByText('No Models Found')).toBeTruthy();
    });

    it('renders an entry with its details and costs', () => {
        llmSearch.searchResults = {
            total: 42,
            catalog: [
                entry({
                    description: 'A fine model',
                    recommended: true,
                    icon_url: 'https://cdn.test/icon.png',
                    default_cost_per_input_token: 3,
                    default_cost_per_output_token: 0,
                }),
                entry({ id: 'c2', display_name: 'No Cost', model_name: 'free-one' }),
                entry({ id: 'c3', display_name: 'One Side', default_cost_per_input_token: 1.5 }),
            ],
        };
        renderOpen();
        expect(screen.getByText('42')).toBeTruthy();
        expect(screen.getByText('A fine model')).toBeTruthy();
        expect(screen.getByText('Recommended')).toBeTruthy();
        expect(screen.getByRole('img').getAttribute('src')).toBe('https://cdn.test/icon.png');
        expect(screen.getByText(/\$3\.00\/\s*Free/)).toBeTruthy();
        expect(screen.getByText(/\$1\.50\/\s*N\/A/)).toBeTruthy();
        // An entry with no price data shows no price line at all.
        expect(screen.getAllByText('per 1M tokens')).toHaveLength(2);
    });

    it('says which entries need an API URL', () => {
        llmSearch.searchResults = {
            total: 2,
            catalog: [
                entry({ id: 'az', display_name: 'Azure GPT', provider_key: 'azure', requires_api_url: true }),
                entry({ id: 'c2', display_name: 'Other', requires_api_url: false }),
            ],
        };
        renderOpen();
        const marks = screen.getAllByText('Needs API URL');
        expect(marks).toHaveLength(1);
        expect(marks[0].parentElement?.textContent).toContain('Azure GPT');
    });

    it('imports the chosen entry, spinning only its own row', async () => {
        let finish: () => void = () => {};
        onImportModel.mockReturnValue(new Promise<void>(resolve => { finish = resolve; }));
        const first = entry();
        llmSearch.searchResults = { total: 2, catalog: [first, entry({ id: 'c2', display_name: 'Other' })] };
        renderOpen();

        fireEvent.click(screen.getAllByRole('button', { name: 'Add' })[0]);
        expect(onImportModel).toHaveBeenCalledWith(first);
        const adding = await screen.findByRole<HTMLButtonElement>('button', { name: 'Adding...' });
        expect(adding.disabled).toBe(true);
        expect(screen.getAllByRole('button', { name: 'Add' })).toHaveLength(1);

        finish();
        await waitFor(() => expect(screen.getAllByRole('button', { name: 'Add' })).toHaveLength(2));
    });

    it('recovers the row when an import fails', async () => {
        const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
        onImportModel.mockRejectedValue(new Error('exists'));
        llmSearch.searchResults = { total: 1, catalog: [entry()] };
        renderOpen();
        fireEvent.click(screen.getByRole('button', { name: 'Add' }));
        await waitFor(() => expect(consoleError).toHaveBeenCalledWith('Failed to import model:', 'Error'));
        expect(await screen.findByRole('button', { name: 'Add' })).toBeTruthy();
        consoleError.mockRestore();
    });

    it('closes from the footer', () => {
        renderOpen();
        fireEvent.click(screen.getByRole('button', { name: 'Close' }));
        expect(onClose).toHaveBeenCalledTimes(1);
    });
});
