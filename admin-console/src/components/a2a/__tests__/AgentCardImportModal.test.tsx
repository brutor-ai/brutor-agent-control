/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Agent card import: from the public registry catalog, or from a URL.
 *
 * Pins what is sent for the rows picked (the well-known URL wins over the base
 * URL, rows with no URL are not imported) and the per-row outcome: one card that
 * fails must be visible next to the ones that worked, and the parent list only
 * reloads when something was actually imported.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';

const gatewayClient = {
    fetchA2ARegistryCatalog: vi.fn(),
    bulkImportA2AAgentCards: vi.fn(),
    importA2AAgentCard: vi.fn(),
};
const gateway = { gatewayClient };
const toast = { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() };

vi.mock('../../../hooks/useGateway', () => ({ useGateway: () => gateway }));
vi.mock('../../common/Toast', () => ({ useToast: () => toast }));

import AgentCardImportModal, { CardIcon } from '../AgentCardImportModal';

const WELL_KNOWN = {
    id: 'a-1',
    name: 'Hotel Agent',
    description: 'Books hotels',
    provider_name: 'Salem',
    base_url: 'https://hotel.test',
    well_known_url: 'https://hotel.test/.well-known/agent-card.json',
    protocol_version: '0.3',
    healthy: true,
    uptime_pct: 99.5,
    skill_count: 2,
    skills: [{ name: 'reserve' }],
};
const BASE_ONLY = {
    id: 'a-2',
    name: 'Flight Agent',
    description: null,
    base_url: 'https://flight.test',
    healthy: null,
    skill_count: 1,
};
const OFFLINE = { id: 'a-3', name: 'Dead Agent', base_url: 'https://dead.test', healthy: false, skill_count: 0 };
const NO_URL = { id: 'a-4', name: null, healthy: true, skill_count: 0 };

const CATALOG = {
    agents: [WELL_KNOWN, BASE_ONLY, OFFLINE, NO_URL],
    total: 4,
    cached: true,
    fetched_at: '2026-07-01T10:00:00Z',
};

const URL_PLACEHOLDER = 'https://example.com or https://example.com/.well-known/agent-card.json';

function setup() {
    const onClose = vi.fn();
    const onImported = vi.fn();
    render(<AgentCardImportModal onClose={onClose} onImported={onImported} />);
    return { onClose, onImported };
}

function rowFor(text: string): HTMLElement {
    const row = screen.getByText(text).closest('li');
    if (!row) throw new Error(`no row for ${text}`);
    return row;
}

function importButton(): HTMLButtonElement {
    return screen.getByRole<HTMLButtonElement>('button', { name: /^Import\s*\d*$/ });
}

async function loaded() {
    const handlers = setup();
    await screen.findByText('Hotel Agent');
    return handlers;
}

function toUrlMode() {
    fireEvent.click(screen.getByRole('button', { name: /From URL/ }));
}

describe('AgentCardImportModal', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        gatewayClient.fetchA2ARegistryCatalog.mockResolvedValue(CATALOG);
    });

    afterEach(() => cleanup());

    describe('catalog', () => {
        it('fetches on open without forcing a refresh and hides offline agents by default', async () => {
            await loaded();
            expect(gatewayClient.fetchA2ARegistryCatalog).toHaveBeenCalledWith({ force_refresh: undefined });
            expect(screen.queryByText('Dead Agent')).toBeNull();
            expect(screen.getByText(/0 selected · 3\/4 shown/)).toBeTruthy();
            expect(screen.getByText(/Cached snapshot/)).toBeTruthy();

            const row = within(rowFor('Hotel Agent'));
            expect(row.getByText('A2A 0.3')).toBeTruthy();
            expect(row.getByText(/healthy · 99.5%/)).toBeTruthy();
            expect(row.getByText('2 skills')).toBeTruthy();
            expect(within(rowFor('Flight Agent')).getByText('1 skill')).toBeTruthy();
            expect(within(rowFor('(no name)')).getByText('(no URL)')).toBeTruthy();
        });

        it('shows offline agents when asked', async () => {
            await loaded();
            fireEvent.click(screen.getByRole('checkbox', { name: 'Healthy only' }));
            expect(within(rowFor('Dead Agent')).getByText('offline')).toBeTruthy();
        });

        it('searches name, provider and skill names', async () => {
            await loaded();
            const box = screen.getByPlaceholderText('Search by name, skill, provider…');

            fireEvent.change(box, { target: { value: 'RESERVE' } });
            expect(screen.getByText('Hotel Agent')).toBeTruthy();
            expect(screen.queryByText('Flight Agent')).toBeNull();

            fireEvent.change(box, { target: { value: 'salem' } });
            expect(screen.getByText('Hotel Agent')).toBeTruthy();

            fireEvent.change(box, { target: { value: 'zzz' } });
            expect(screen.getByText('No agents match the current filters.')).toBeTruthy();
            expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Select all visible' }).disabled).toBe(true);
        });

        it('forces a refresh on demand', async () => {
            await loaded();
            fireEvent.click(screen.getByTitle('Re-fetch catalog (bypass cache)'));
            await waitFor(() => expect(gatewayClient.fetchA2ARegistryCatalog).toHaveBeenLastCalledWith({ force_refresh: true }));
        });

        it('shows why the catalog failed and retries', async () => {
            gatewayClient.fetchA2ARegistryCatalog.mockRejectedValueOnce(new Error('registry down'));
            setup();
            expect(await screen.findByText('registry down')).toBeTruthy();

            fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
            expect(await screen.findByText('Hotel Agent')).toBeTruthy();
            expect(screen.queryByText('registry down')).toBeNull();
        });

        it('cannot import with nothing picked', async () => {
            await loaded();
            expect(importButton().disabled).toBe(true);
        });

        it('prefers the well-known URL, falls back to the base URL, and reports success', async () => {
            gatewayClient.bulkImportA2AAgentCards.mockResolvedValue({
                succeeded: 2,
                failed: 0,
                results: [
                    { source_url: 'u1', success: true, card_id: 'c1', name: 'Hotel Agent' },
                    { source_url: 'https://flight.test', success: true, card_id: 'c2' },
                ],
            });
            const { onImported } = await loaded();
            fireEvent.click(rowFor('Hotel Agent'));
            fireEvent.click(rowFor('Flight Agent'));
            fireEvent.click(importButton());

            await waitFor(() =>
                expect(gatewayClient.bulkImportA2AAgentCards).toHaveBeenCalledWith({
                    imports: [
                        { source_url: 'https://hotel.test/.well-known/agent-card.json', name_override: 'Hotel Agent', enabled: true },
                        { source_url: 'https://flight.test', name_override: 'Flight Agent', enabled: true },
                    ],
                }),
            );
            await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Import complete', 'Imported 2 cards.'));
            expect(onImported).toHaveBeenCalledTimes(1);
            expect(screen.getByText('2/2 imported')).toBeTruthy();
            // A result with no name falls back to the URL it came from.
            expect(screen.getByText('https://flight.test')).toBeTruthy();
            expect(screen.getByRole('button', { name: 'Done' })).toBeTruthy();
        });

        it('shows the failed row, warns, and still reloads for the ones that worked', async () => {
            const results = [
                { source_url: 'u1', success: true, name: 'Hotel Agent' },
                { source_url: 'https://flight.test', success: false, error: 'name already exists' },
            ];
            gatewayClient.bulkImportA2AAgentCards.mockResolvedValue({ succeeded: 1, failed: 1, results });
            const { onImported } = await loaded();
            fireEvent.click(rowFor('Hotel Agent'));
            fireEvent.click(rowFor('Flight Agent'));
            fireEvent.click(importButton());

            expect(await screen.findByText('name already exists')).toBeTruthy();
            expect(toast.warning).toHaveBeenCalledWith('Imported with errors', '1 succeeded, 1 failed.');
            expect(onImported).toHaveBeenCalledWith(results);
            expect(screen.getByText('1/2 imported')).toBeTruthy();
        });

        it('does not reload the parent when nothing was imported', async () => {
            gatewayClient.bulkImportA2AAgentCards.mockResolvedValue({
                succeeded: 0,
                failed: 1,
                results: [{ source_url: 'u1', success: false, error: 'bad card' }],
            });
            const { onImported } = await loaded();
            fireEvent.click(rowFor('Hotel Agent'));
            fireEvent.click(importButton());

            expect(await screen.findByText('bad card')).toBeTruthy();
            expect(onImported).not.toHaveBeenCalled();
        });

        it('refuses a selection where no row has a URL', async () => {
            await loaded();
            fireEvent.click(rowFor('(no name)'));
            fireEvent.click(importButton());

            await waitFor(() =>
                expect(toast.error).toHaveBeenCalledWith('Nothing to import', 'Selected rows have no source URL.'),
            );
            expect(gatewayClient.bulkImportA2AAgentCards).not.toHaveBeenCalled();
        });

        it('reports a failed request and keeps the selection', async () => {
            gatewayClient.bulkImportA2AAgentCards.mockRejectedValue(new Error('gateway down'));
            await loaded();
            fireEvent.click(rowFor('Hotel Agent'));
            fireEvent.click(importButton());

            await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Bulk import failed', 'gateway down'));
            expect(screen.getByText(/1 selected/)).toBeTruthy();
        });

        it('selects all visible, toggles off, and clears', async () => {
            await loaded();
            fireEvent.click(screen.getByRole('button', { name: 'Select all visible' }));
            expect(screen.getByText(/3 selected/)).toBeTruthy();

            fireEvent.click(rowFor('Hotel Agent'));
            expect(screen.getByText(/2 selected/)).toBeTruthy();

            fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
            expect(screen.getByText(/0 selected/)).toBeTruthy();
        });

        it('starts over from the result list', async () => {
            gatewayClient.bulkImportA2AAgentCards.mockResolvedValue({
                succeeded: 1,
                failed: 0,
                results: [{ source_url: 'u1', success: true, name: 'Hotel Agent' }],
            });
            await loaded();
            fireEvent.click(rowFor('Hotel Agent'));
            fireEvent.click(importButton());
            fireEvent.click(await screen.findByRole('button', { name: 'Import more' }));

            expect(screen.getByText(/0 selected/)).toBeTruthy();
            expect(gatewayClient.fetchA2ARegistryCatalog).toHaveBeenCalledTimes(1);
        });
    });

    describe('from a URL', () => {
        it('cannot submit a blank URL', async () => {
            await loaded();
            toUrlMode();
            expect(importButton().disabled).toBe(true);
            fireEvent.change(screen.getByPlaceholderText(URL_PLACEHOLDER), { target: { value: '   ' } });
            expect(importButton().disabled).toBe(true);
        });

        it('sends the trimmed URL and override and reports the imported card', async () => {
            gatewayClient.importA2AAgentCard.mockResolvedValue({ id: 'c1', name: 'renamed' });
            const { onImported } = await loaded();
            toUrlMode();
            fireEvent.change(screen.getByPlaceholderText(URL_PLACEHOLDER), { target: { value: '  https://a.test  ' } });
            fireEvent.change(screen.getByPlaceholderText('my-team-friendly-name'), { target: { value: ' renamed ' } });
            fireEvent.click(importButton());

            await waitFor(() =>
                expect(gatewayClient.importA2AAgentCard).toHaveBeenCalledWith({ source_url: 'https://a.test', name_override: 'renamed', enabled: true }),
            );
            await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Card imported', 'renamed'));
            expect(onImported).toHaveBeenCalledWith([{ source_url: 'https://a.test', success: true, card_id: 'c1', name: 'renamed' }]);
            expect(screen.getByText('1/1 imported')).toBeTruthy();
        });

        it('sends a null override when none was given', async () => {
            gatewayClient.importA2AAgentCard.mockResolvedValue({ id: 'c1', name: 'n' });
            await loaded();
            toUrlMode();
            fireEvent.change(screen.getByPlaceholderText(URL_PLACEHOLDER), { target: { value: 'https://a.test' } });
            fireEvent.click(importButton());

            await waitFor(() =>
                expect(gatewayClient.importA2AAgentCard).toHaveBeenCalledWith({ source_url: 'https://a.test', name_override: null, enabled: true }),
            );
        });

        it('imports the card switched off when asked, to be enabled once it has been reviewed', async () => {
            gatewayClient.importA2AAgentCard.mockResolvedValue({ id: 'c1', name: 'n' });
            await loaded();
            toUrlMode();
            fireEvent.change(screen.getByPlaceholderText(URL_PLACEHOLDER), { target: { value: 'https://a.test' } });
            fireEvent.click(screen.getByLabelText('Enable the imported card'));
            fireEvent.click(importButton());

            await waitFor(() =>
                expect(gatewayClient.importA2AAgentCard).toHaveBeenCalledWith({ source_url: 'https://a.test', name_override: null, enabled: false }),
            );
        });

        it('shows the failure against the URL and does not reload the parent', async () => {
            gatewayClient.importA2AAgentCard.mockRejectedValue(new Error('no card at that URL'));
            const { onImported } = await loaded();
            toUrlMode();
            fireEvent.change(screen.getByPlaceholderText(URL_PLACEHOLDER), { target: { value: 'https://a.test' } });
            fireEvent.click(importButton());

            expect(await screen.findByText('no card at that URL')).toBeTruthy();
            expect(screen.getByText('https://a.test')).toBeTruthy();
            expect(screen.getByText('0/1 imported')).toBeTruthy();
            expect(toast.error).toHaveBeenCalledWith('Import failed', 'no card at that URL');
            expect(onImported).not.toHaveBeenCalled();
        });

        it('returns to the catalog without refetching it', async () => {
            await loaded();
            toUrlMode();
            fireEvent.click(screen.getByRole('button', { name: /From Catalog/ }));
            expect(await screen.findByText('Hotel Agent')).toBeTruthy();
            expect(gatewayClient.fetchA2ARegistryCatalog).toHaveBeenCalledTimes(1);
        });
    });

    describe('closing', () => {
        it('closes from the header, the footer and the backdrop, but not from inside', async () => {
            const { onClose } = await loaded();
            fireEvent.click(screen.getByText('Import Agent Cards'));
            expect(onClose).not.toHaveBeenCalled();

            fireEvent.click(screen.getByRole('button', { name: 'Close' }));
            fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
            expect(onClose).toHaveBeenCalledTimes(2);

            const backdrop = document.querySelector('.fixed.inset-0');
            if (!backdrop) throw new Error('no backdrop');
            fireEvent.click(backdrop);
            expect(onClose).toHaveBeenCalledTimes(3);
        });
    });

    describe('CardIcon', () => {
        it('shows the image and swaps to the initial when it fails to load', () => {
            render(<CardIcon src="https://x.test/i.png" alt="hotel" size={36} />);
            const img = screen.getByRole('img', { name: 'hotel' });
            expect(img.getAttribute('src')).toBe('https://x.test/i.png');

            fireEvent.error(img);
            expect(screen.queryByRole('img')).toBeNull();
            expect(screen.getByText('H')).toBeTruthy();
        });

        it('shows a question mark when there is no name to take an initial from', () => {
            render(<CardIcon alt="   " size={36} />);
            expect(screen.getByText('?')).toBeTruthy();
        });
    });
});
