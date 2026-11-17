/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * Asset Register: the pieces that belong to shadow-AI discovery.
 *
 *   • the Devices and Discovery Integrations buttons and their drill-downs
 *   • the coverage scorecard and the Discovered, Ungoverned table
 *
 * Discovery is an Enterprise feature, so these reach the page through its
 * slots (`asset-register.views`, `asset-register.coverage`). The page is
 * rendered here the way the application renders it, with the built-in slot
 * items; the last block renders it with nothing contributed.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';

import AIAssetsTab from '../components/ai-assets/AIAssetsTab';
import { WithBuiltinSlots } from '../test/builtinSlots';
import { SlotProvider } from '../app/SlotProvider';
import type { AssetCoverageSlotItem, PageViewSlotItem, SlotRegistry } from '../app/slots';
import type { AssetCoverageData } from '../lib/assetCoverageTypes';
import {
    deleteAIAsset,
    fetchAssetCoverage,
    listAIAssets,
    permissions,
    resetDiscovery,
} from '../test/aiAssets/aiAssetsDiscovery';

const page = vi.hoisted(() => () => import('../test/aiAssets/aiAssetsTab'));
const shared = vi.hoisted(() => () => import('../test/aiAssets/aiAssetsDiscovery'));
vi.mock('../components/common/Toast', async () => (await page()).toastModule());
vi.mock('../components/common/ConfirmationDialog', async () => (await page()).confirmModule());
vi.mock('../hooks/useGateway', async () => (await shared()).useGatewayModule());
vi.mock('../hooks/useAuth', async () => (await shared()).useAuthModule());

async function open({ slots = true }: { slots?: boolean } = {}) {
    render(<AIAssetsTab />, slots ? { wrapper: WithBuiltinSlots } : undefined);
    await screen.findByText('GPT-4o');
}

beforeEach(resetDiscovery);

describe('Asset Register — the discovery drill-downs', () => {

    it('hides both from an admin who may not read discovery', async () => {
        permissions.granted = (permission) => permission !== 'discovery:read';
        await open();
        expect(screen.queryByRole('button', { name: 'Devices' })).toBeNull();
        expect(screen.queryByRole('button', { name: 'Discovery Integrations' })).toBeNull();
        expect(screen.getByRole('button', { name: 'Sync now' })).toBeInTheDocument();
    });
});

describe('Asset Register — with nothing contributed', () => {
    it('has no discovery button and no coverage panel, and asks nothing about coverage', async () => {
        await open({ slots: false });
        const header = screen.getByRole('heading', { name: 'Asset Register' }).closest('div.flex.items-center.justify-between')!;
        expect(
            within(header as HTMLElement)
                .getAllByRole('button')
                .map((b) => b.textContent),
        ).toEqual(['Guide for operators & auditors', 'Sync now']);
        expect(screen.queryByText('AI assets seen')).toBeNull();
        expect(screen.queryByRole('heading', { name: 'Discovered — Ungoverned' })).toBeNull();
        expect(fetchAssetCoverage).not.toHaveBeenCalled();
    });

    it('still syncs and deletes', async () => {
        await open({ slots: false });
        fireEvent.click(screen.getByRole('button', { name: 'Sync now' }));
        await waitFor(() => expect(listAIAssets).toHaveBeenCalledTimes(2));
        expect(await screen.findByRole('button', { name: 'Sync now' })).toBeEnabled();
        fireEvent.click(screen.getByRole('button', { name: 'Delete GPT-4o' }));
        await waitFor(() => expect(deleteAIAsset).toHaveBeenCalledWith('asset-1'));
        await waitFor(() => expect(listAIAssets).toHaveBeenCalledTimes(3));
        expect(fetchAssetCoverage).not.toHaveBeenCalled();
    });
});


describe('Asset Register — what the page gives a contributed view and a contributed coverage panel', () => {
    /** A view: a header button, and a page of its own with a way back. */
    const fakeView = (id: string): PageViewSlotItem => ({
        id,
        Button: ({ onOpen }) => (
            <button type="button" onClick={onOpen}>
                {`open ${id}`}
            </button>
        ),
        View: ({ onBack }) => (
            <div>
                <p>{`view ${id}`}</p>
                <button type="button" onClick={onBack}>
                    {`back from ${id}`}
                </button>
            </div>
        ),
    });

    const data = (totalSeen: number): AssetCoverageData => ({
        coverage: { governed: 1, observed: 0, ungoverned: 0, total_seen: totalSeen },
        ungoverned: [],
    });
    type Load = AssetCoverageSlotItem['load'];
    /** A coverage item: both panels show the total they are given. */
    const fakeCoverage = (id: string, load: Load): AssetCoverageSlotItem => ({
        id,
        load,
        Summary: ({ data: d }) => <div data-testid={`summary-${id}`}>{`summary ${d.coverage.total_seen}`}</div>,
        Detail: ({ data: d }) => <div data-testid={`detail-${id}`}>{`detail ${d.coverage.total_seen}`}</div>,
    });

    /** True when `a` comes before `b` in the document. */
    const precedes = (a: Element, b: Element): boolean =>
        Boolean(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);

    async function openWith(slots: SlotRegistry) {
        render(
            <SlotProvider slots={slots}>
                <AIAssetsTab />
            </SlotProvider>,
        );
        await screen.findByText('GPT-4o');
    }

    it('places the view buttons in order before the guide and the sync', async () => {
        await openWith({ 'asset-register.views': [fakeView('one'), fakeView('two')] });
        const header = screen.getByRole('heading', { name: 'Asset Register' }).closest('div.flex.items-center.justify-between')!;
        expect(
            within(header as HTMLElement)
                .getAllByRole('button')
                .map((b) => b.textContent),
        ).toEqual(['open one', 'open two', 'Guide for operators & auditors', 'Sync now']);
        // No view is shown until its button is pressed.
        expect(screen.queryByText('view one')).toBeNull();
        expect(screen.queryByText('view two')).toBeNull();
    });

    it('an opened view replaces the page, and its Back restores the page', async () => {
        await openWith({ 'asset-register.views': [fakeView('one'), fakeView('two')] });
        fireEvent.click(screen.getByRole('button', { name: 'open two' }));
        expect(screen.getByText('view two')).toBeInTheDocument();
        expect(screen.queryByText('view one')).toBeNull();
        expect(screen.queryByRole('heading', { name: 'Asset Register' })).toBeNull();
        expect(screen.queryByText('GPT-4o')).toBeNull();
        fireEvent.click(screen.getByRole('button', { name: 'back from two' }));
        expect(await screen.findByRole('heading', { name: 'Asset Register' })).toBeInTheDocument();
        expect(screen.getByText('GPT-4o')).toBeInTheDocument();
        expect(screen.queryByText('view two')).toBeNull();
        // The other view opens the same way afterwards.
        fireEvent.click(screen.getByRole('button', { name: 'open one' }));
        expect(screen.getByText('view one')).toBeInTheDocument();
        expect(screen.queryByRole('heading', { name: 'Asset Register' })).toBeNull();
    });

    it('loads coverage once with the gateway client and shows the summary above the filters and the detail below the register', async () => {
        const load = vi.fn<Load>().mockResolvedValue(data(37));
        await openWith({ 'asset-register.coverage': [fakeCoverage('cov', load)] });
        const summary = await screen.findByTestId('summary-cov');
        const detail = screen.getByTestId('detail-cov');
        // Both panels are given what the item's `load` answered.
        expect(summary).toHaveTextContent('summary 37');
        expect(detail).toHaveTextContent('detail 37');
        expect(load).toHaveBeenCalledTimes(1);
        // `load` is handed the gateway client; the page itself asks nothing about coverage.
        expect(load.mock.calls[0][0].fetchAssetCoverage).toBe(fetchAssetCoverage);
        expect(fetchAssetCoverage).not.toHaveBeenCalled();
        expect(precedes(summary, screen.getByLabelText('Filter by asset kind'))).toBe(true);
        expect(precedes(screen.getByLabelText('Filter by asset kind'), detail)).toBe(true);
        expect(precedes(screen.getByText('GPT-4o'), detail)).toBe(true);
    });

    it('shows neither panel of an item whose load answers null or fails; the others and the register still show', async () => {
        const failing = vi.fn<Load>().mockRejectedValue(new Error('down'));
        const empty = vi.fn<Load>().mockResolvedValue(null);
        const good = vi.fn<Load>().mockResolvedValue(data(5));
        await openWith({
            'asset-register.coverage': [fakeCoverage('failing', failing), fakeCoverage('empty', empty), fakeCoverage('good', good)],
        });
        expect(await screen.findByTestId('summary-good')).toHaveTextContent('summary 5');
        expect(screen.getByTestId('detail-good')).toHaveTextContent('detail 5');
        expect(failing).toHaveBeenCalledTimes(1);
        expect(empty).toHaveBeenCalledTimes(1);
        for (const id of ['failing', 'empty']) {
            expect(screen.queryByTestId(`summary-${id}`)).toBeNull();
            expect(screen.queryByTestId(`detail-${id}`)).toBeNull();
        }
        expect(screen.getByText('GPT-4o')).toBeInTheDocument();
    });

    it('a sync loads coverage again and is not finished until the load has answered', async () => {
        const load = vi.fn<Load>().mockResolvedValue(data(37));
        await openWith({ 'asset-register.coverage': [fakeCoverage('cov', load)] });
        await screen.findByTestId('summary-cov');
        let answer: (value: AssetCoverageData) => void = () => {};
        load.mockReturnValueOnce(new Promise<AssetCoverageData>((resolve) => (answer = resolve)));

        fireEvent.click(screen.getByRole('button', { name: 'Sync now' }));
        await waitFor(() => expect(listAIAssets).toHaveBeenCalledTimes(2));
        await waitFor(() => expect(load).toHaveBeenCalledTimes(2));
        // The list has answered; the coverage load has not. The panels keep what they had.
        expect(screen.getByRole('button', { name: 'Syncing…' })).toBeDisabled();
        expect(screen.getByTestId('summary-cov')).toHaveTextContent('summary 37');

        answer(data(41));
        expect(await screen.findByRole('button', { name: 'Sync now' })).toBeEnabled();
        expect(screen.getByTestId('summary-cov')).toHaveTextContent('summary 41');
        expect(screen.getByTestId('detail-cov')).toHaveTextContent('detail 41');
    });

    it('deleting an asset loads coverage again', async () => {
        const load = vi.fn<Load>().mockResolvedValue(data(37));
        await openWith({ 'asset-register.coverage': [fakeCoverage('cov', load)] });
        await screen.findByTestId('summary-cov');
        load.mockResolvedValue(data(36));
        fireEvent.click(screen.getByRole('button', { name: 'Delete GPT-4o' }));
        await waitFor(() => expect(deleteAIAsset).toHaveBeenCalledWith('asset-1'));
        await waitFor(() => expect(load).toHaveBeenCalledTimes(2));
        await waitFor(() => expect(screen.getByTestId('summary-cov')).toHaveTextContent('summary 36'));
    });
});
