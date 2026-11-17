/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Mission Control — the Caching tab and the Cache Hit Rate tile.
 *
 * Response cache and prompt cache are Enterprise features, so both pieces
 * reach the page through slots (`mission-control.tabs`,
 * `mission-control.performance-tiles`). The tests of the
 * tab are the page with the tab contributed. The last block is the page
 * with nothing contributed.
 *
 * The tile loads its own numbers: if it read the ones the Caching tab loads,
 * it would show 0.0% until that tab had been opened.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, fireEvent } from '@testing-library/react';

const getCacheStats = vi.fn();
const getVPTMetrics = vi.fn();
const getPromptCacheAggregates = vi.fn();
const getPromptCacheTimeseries = vi.fn();

const page = vi.hoisted(() => () => import('../../../test/monitoring/missionControlPage'));
vi.mock('../../../hooks/useGateway', async () =>
    (await page()).useGatewayModule(() => ({ getCacheStats, getVPTMetrics, getPromptCacheAggregates, getPromptCacheTimeseries })),
);
vi.mock('../../common/Toast', async () => (await page()).toastModule());
vi.mock('../../common/ConfirmationDialog', async () => (await page()).confirmModule());

import MissionControl from '../MissionControl';
import { WithBuiltinSlots } from '../../../test/builtinSlots';
import { resetMissionControlPage, STATS, strip, tab, VPT } from '../../../test/monitoring/missionControlPage';
import { SlotProvider } from '../../../app/SlotProvider';
import type { SlotIcon, SlotItem, SlotRegistry, TabSlotItem } from '../../../app/slots';

vi.setConfig({ testTimeout: 30_000 });

/** `slots: false` renders the page with nothing contributed to its slots. */
async function open({ slots = true } = {}) {
    render(<MissionControl />, slots ? { wrapper: WithBuiltinSlots } : undefined);
    await screen.findByText('srv-alpha');
}

beforeEach(() => {
    resetMissionControlPage();
    getCacheStats.mockResolvedValue(STATS);
    getVPTMetrics.mockResolvedValue(VPT);
    getPromptCacheAggregates.mockResolvedValue({ read_tokens: 77 });
    getPromptCacheTimeseries.mockResolvedValue({ points: [1, 2] });
});

afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
});

describe('MissionControl — with nothing contributed', () => {
    it('has no Caching tab and no cache tile, and asks nothing about caches', async () => {
        await open({ slots: false });
        expect(strip()).toEqual(['Traffic', 'Cost', 'LLM Economics', 'Governance', 'Agents', 'Reliability']);
        tab('Reliability');
        expect(await screen.findByText('Session Reuse')).toBeTruthy();
        expect(screen.getByText('Data Transferred')).toBeTruthy();
        expect(screen.queryByText('Cache Hit Rate')).toBeNull();
        expect(getCacheStats).not.toHaveBeenCalled();
        expect(getVPTMetrics).not.toHaveBeenCalled();
    });
});


describe('MissionControl — what the page gives a contributed tab and a contributed tile', () => {
    const TabIcon: SlotIcon = ({ className }) => <svg className={className} data-testid="fake-tab-icon" />;
    /** A tab that shows the hours it is given. */
    const fakeTab = (id: string, label: string, after?: string): TabSlotItem<{ hours: number }> => ({
        id,
        label,
        icon: TabIcon,
        after,
        render: ({ hours }) => <div data-testid={`tab-${id}`}>{`hours=${hours}`}</div>,
    });
    /** A tile that shows the hours it is given, shaped like the page's own tiles. */
    const fakeTile = (id: string): SlotItem<{ hours: number }> => ({
        id,
        render: ({ hours }) => (
            <div data-testid={`tile-${id}`}>
                <div>{`Tile ${id}`}</div>
                <div>{`hours=${hours}`}</div>
            </div>
        ),
    });
    const period = (label: string) => fireEvent.click(screen.getByRole('button', { name: label }));

    async function openWith(slots: SlotRegistry) {
        render(
            <SlotProvider slots={slots}>
                <MissionControl />
            </SlotProvider>,
        );
        await screen.findByText('srv-alpha');
    }

    it('places a contributed tab after the tab it names, and one that names none last', async () => {
        await openWith({
            'mission-control.tabs': [
                fakeTab('fake-last', 'Fake Last'),
                fakeTab('fake-a', 'Fake A', 'governance'),
                fakeTab('fake-b', 'Fake B', 'governance'),
            ],
        });
        expect(strip()).toEqual([
            'Traffic', 'Cost', 'LLM Economics', 'Governance', 'Fake A', 'Fake B', 'Agents', 'Reliability', 'Fake Last',
        ]);
        // The tab's icon is drawn in the strip, sized as the page's own are.
        const icon = screen.getByRole('tab', { name: 'Fake A' }).querySelector('[data-testid="fake-tab-icon"]');
        expect(icon?.getAttribute('class')).toBe('w-4 h-4');
    });

    it('renders a contributed tab only while it is selected, with the hours of the period shown', async () => {
        await openWith({ 'mission-control.tabs': [fakeTab('fake-a', 'Fake A', 'governance')] });
        // Traffic is the tab the page opens on: the contributed one is not rendered.
        expect(screen.queryByTestId('tab-fake-a')).toBeNull();
        period('7 Days');
        tab('Fake A');
        expect(screen.getByRole('tab', { name: 'Fake A' }).getAttribute('aria-selected')).toBe('true');
        expect(screen.getByTestId('tab-fake-a').textContent).toBe('hours=168');
        // The page's own Traffic panel has made way for it.
        expect(screen.queryByText('srv-alpha')).toBeNull();
        // Another period: the tab is given its hours.
        period('24 Hours');
        expect(screen.getByTestId('tab-fake-a').textContent).toBe('hours=24');
        period('30 Days');
        expect(screen.getByTestId('tab-fake-a').textContent).toBe('hours=720');
        // Leaving the tab takes it off the page.
        tab('Traffic');
        expect(await screen.findByText('srv-alpha')).toBeTruthy();
        expect(screen.queryByTestId('tab-fake-a')).toBeNull();
    });

    it('places contributed tiles in order between Session Reuse and Min / Max Latency, with the hours of the period shown', async () => {
        await openWith({ 'mission-control.performance-tiles': [fakeTile('one'), fakeTile('two')] });
        period('7 Days');
        tab('Reliability');
        const one = await screen.findByTestId('tile-one');
        const labels = Array.from(one.parentElement!.children).map((c) => c.firstElementChild?.textContent);
        expect(labels).toEqual(['Data Transferred', 'Session Reuse', 'Tile one', 'Tile two', 'Min / Max Latency']);
        expect(one.textContent).toBe('Tile onehours=168');
        expect(screen.getByTestId('tile-two').textContent).toBe('Tile twohours=168');
        period('24 Hours');
        await screen.findByTestId('tile-one');
        expect(screen.getByTestId('tile-one').textContent).toBe('Tile onehours=24');
        // A tile is no tab: the strip is the page's own.
        expect(strip()).toEqual(['Traffic', 'Cost', 'LLM Economics', 'Governance', 'Agents', 'Reliability']);
    });
});
