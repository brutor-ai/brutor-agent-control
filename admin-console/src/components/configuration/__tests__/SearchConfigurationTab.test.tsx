/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Web-search provider settings.
 *
 * Pinned: the exact save body (an empty API key must be omitted so it does not
 * overwrite the stored one, and a daily limit of 0 is a limit, which `|| null`
 * would turn into unlimited). A failed load is reported: a silent one leaves a form
 * of built-in defaults that one click on Save would write over the real config.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';

const gatewayClient = {
    getSearchProviderConfig: vi.fn(),
    updateSearchProviderConfig: vi.fn(),
    clearSearchApiKey: vi.fn(),
};
const gateway = { gatewayClient };
vi.mock('../../../hooks/useGateway', () => ({ useGateway: () => gateway }));

import SearchConfigurationTab from '../SearchConfigurationTab';

const CONFIG = {
    provider: 'brave',
    api_key_configured: true,
    google_cx_id: null,
    searxng_base_url: null,
    max_results_per_query: 5,
    safe_search: true,
    allowed_domains: ['github.com'],
    blocked_domains: ['spam.test', 'junk.test'],
    daily_query_limit: 100,
    enabled: true,
};

const open = async () => {
    render(<SearchConfigurationTab />);
    await screen.findByRole('button', { name: 'Save Configuration' });
};
const save = () => fireEvent.click(screen.getByRole('button', { name: 'Save Configuration' }));
const sent = () => gatewayClient.updateSearchProviderConfig.mock.calls[0][0] as Record<string, unknown>;

describe('SearchConfigurationTab', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        vi.spyOn(console, 'error').mockImplementation(() => {});
        for (const fn of Object.values(gatewayClient)) fn.mockReset();
        gatewayClient.getSearchProviderConfig.mockResolvedValue(CONFIG);
        gatewayClient.updateSearchProviderConfig.mockResolvedValue({});
        gatewayClient.clearSearchApiKey.mockResolvedValue({});
    });

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
    });

    it('sends the loaded config back, without an api_key it was not given and without the two settings it does not offer', async () => {
        await open();
        expect(screen.getByPlaceholderText('••••••••• (configured)')).toBeInTheDocument();
        save();
        await waitFor(() => expect(gatewayClient.updateSearchProviderConfig).toHaveBeenCalledTimes(1));
        expect(sent()).toEqual({
            provider: 'brave',
            google_cx_id: null,
            searxng_base_url: null,
            safe_search: true,
            allowed_domains: ['github.com'],
            blocked_domains: ['spam.test', 'junk.test'],
            enabled: true,
        });
        expect(await screen.findByText('Search configuration saved successfully')).toBeInTheDocument();
        expect(gatewayClient.getSearchProviderConfig).toHaveBeenCalledTimes(2);
    });

    it('sends a typed key trimmed, tidies domain lists, and sends every setting edited', async () => {
        await open();
        fireEvent.change(screen.getByPlaceholderText('••••••••• (configured)'), { target: { value: '  key-1  ' } });
        fireEvent.change(screen.getByPlaceholderText(/github\.com/), { target: { value: ' a.test \n\n b.test ' } });
        fireEvent.change(screen.getByPlaceholderText(/pinterest\.com/), { target: { value: '' } });
        fireEvent.click(screen.getByRole('checkbox', { name: /Enforce safe search/ }));
        const checkboxes = screen.getAllByRole('checkbox');
        fireEvent.click(checkboxes[0]);
        save();
        await waitFor(() => expect(gatewayClient.updateSearchProviderConfig).toHaveBeenCalled());
        expect(sent()).toMatchObject({
            api_key: 'key-1',
            allowed_domains: ['a.test', 'b.test'],
            blocked_domains: [],
            safe_search: false,
            enabled: false,
        });
    });

    it('offers no daily query limit and no maximum of results, and shows the stored ones read-only', async () => {
        await open();
        expect(screen.queryAllByRole('spinbutton')).toHaveLength(0);
        expect(screen.getByTestId('search-daily-limit-unavailable')).toHaveTextContent('100 — not available in this release');
        expect(screen.getByTestId('search-max-results-unavailable')).toHaveTextContent('5 — not available in this release');
    });

    it('Google needs a CX id and SearXNG a URL, and neither asks for the other', async () => {
        await open();
        fireEvent.click(screen.getByRole('button', { name: /Google Custom Search/ }));
        fireEvent.change(screen.getByPlaceholderText('e.g. a1b2c3d4e5f6g7h8i'), { target: { value: 'cx-1' } });
        expect(screen.getByText('Google Cloud Console API key')).toBeInTheDocument();
        expect(screen.queryByPlaceholderText('e.g. http://searxng:8080')).toBeNull();
        save();
        await waitFor(() => expect(gatewayClient.updateSearchProviderConfig).toHaveBeenCalledTimes(1));
        expect(sent()).toMatchObject({ provider: 'google', google_cx_id: 'cx-1' });

        fireEvent.click(await screen.findByRole('button', { name: /SearXNG/ }));
        fireEvent.change(screen.getByPlaceholderText('e.g. http://searxng:8080'), { target: { value: 'http://s:8080' } });
        expect(screen.queryByPlaceholderText(/configured|Enter API key/)).toBeNull();
        save();
        await waitFor(() => expect(gatewayClient.updateSearchProviderConfig).toHaveBeenCalledTimes(2));
        expect(gatewayClient.updateSearchProviderConfig.mock.calls[1][0]).toMatchObject({
            provider: 'searxng',
            searxng_base_url: 'http://s:8080',
        });
    });

    it('a refused save is shown', async () => {
        gatewayClient.updateSearchProviderConfig.mockRejectedValue(new Error('bad url'));
        await open();
        save();
        expect(await screen.findByText('bad url')).toBeInTheDocument();
        expect(screen.queryByText('Search configuration saved successfully')).toBeNull();
    });

    it('clears the stored key, and shows a refusal', async () => {
        await open();
        fireEvent.click(screen.getByTitle('Clear API key'));
        await waitFor(() => expect(gatewayClient.clearSearchApiKey).toHaveBeenCalledTimes(1));
        expect(await screen.findByText('API key cleared')).toBeInTheDocument();
        expect(gatewayClient.getSearchProviderConfig).toHaveBeenCalledTimes(2);

        gatewayClient.clearSearchApiKey.mockRejectedValue({});
        fireEvent.click(await screen.findByTitle('Clear API key'));
        expect(await screen.findByText('Failed to clear API key')).toBeInTheDocument();
    });

    it('offers no key clearing when no key is stored', async () => {
        gatewayClient.getSearchProviderConfig.mockResolvedValue({ ...CONFIG, api_key_configured: false });
        await open();
        expect(screen.queryByTitle('Clear API key')).toBeNull();
    });

    it('a failed load says so, instead of quietly showing defaults that Save would write', async () => {
        gatewayClient.getSearchProviderConfig.mockRejectedValue(new Error('cp down'));
        await open();
        expect(screen.getByText('cp down')).toBeInTheDocument();
    });

    it('reads a config with no domain lists', async () => {
        gatewayClient.getSearchProviderConfig.mockResolvedValue({ ...CONFIG, allowed_domains: undefined, blocked_domains: undefined });
        await open();
        expect(screen.getByPlaceholderText(/github\.com/)).toHaveValue('');
        expect(screen.getByPlaceholderText(/pinterest\.com/)).toHaveValue('');
    });
});
